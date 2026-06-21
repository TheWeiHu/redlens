#!/usr/bin/env python3
"""Offline evaluation harness for the topic relevance filter (task 0024, part B).

The filter (``redlens/filter.py``) asks gpt-4o-mini to judge whether each matched
post is really about the tracked brand. This script *measures* that judgement
against a frozen, human-labeled gold set so the prompt can be iterated until it
clears the acceptance bar:

    recall on true brand mentions >= 0.95   (we almost never hide a real mention)
    precision on false-positive calls >= 0.85
    overall agreement / F1 >= 0.90

Three subcommands form the loop — politely to arctic, the live pull happens ONCE:

    pull     track each brand into a local eval DB (raw matches, NO filtering) and
             write tests/fixtures/filter_gold/<brand>.jsonl with label="" stubs.
    score    run gpt-4o-mini over the labeled gold and print a per-brand + overall
             confusion matrix vs the gold labels. Re-runnable offline against the
             frozen fixture — no re-pull — so prompt iterations are fast + cheap.

Between the two, a human (Opus 4.8 for this task) fills each row's "label" with
"relevant" or "junk". That hand-labeled file is the gold standard; commit it.

Usage:
    python scripts/filter_eval.py pull   [--days 30] [--db .context/eval.db] [brands…]
    python scripts/filter_eval.py score  [--db .context/eval.db] [brands…]

With no brands listed, the 10 default brands below are used. ``score`` needs an
LLM key (OPENAI_API_KEY / REDLENS_LLM_API_KEY); ``pull`` is keyless.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

# Run from a checkout without needing an editable install: put the repo root
# (this file's parent's parent) first on the path so `import redlens` resolves
# to the working tree being evaluated.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlmodel import Session, select  # noqa: E402

from redlens import llm  # noqa: E402
from redlens.config import llm_api_key  # noqa: E402
from redlens.db import connect, init_schema  # noqa: E402
from redlens.filter import _item_block, _parse_verdicts  # noqa: E402
from redlens.models import Post, TopicPost  # noqa: E402
from redlens.prompts import render  # noqa: E402
from redlens.topics import get_topic, track_topic  # noqa: E402

# brand -> the sense to KEEP (everyday meaning that creates false positives).
BRANDS: dict[str, str] = {
    "conductor": "the Mac AI-agent app (vs orchestra/train/physics)",
    "arc": "the Arc browser (vs geometry/story arc)",
    "linear": "the issue tracker (vs math)",
    "monday": "Monday.com (vs the weekday)",
    "bolt": "bolt.new / Bolt rides (vs lightning/hardware)",
    "square": "the payments company (vs shape/Times Square)",
    "shell": "the oil company (vs seashell/Unix shell)",
    "corona": "the beer (vs COVID/the sun)",
    "notion": "the app (vs 'an idea')",
    "dove": "the soap (vs the bird / past tense of dive)",
}

GOLD_DIR = Path("tests/fixtures/filter_gold")


@dataclass
class Row:
    id: str
    subreddit: str
    title: str
    body: str
    label: str = ""   # human gold: "relevant" | "junk" | "" (unlabeled)


def _gold_path(brand: str) -> Path:
    return GOLD_DIR / f"{brand}.jsonl"


def _load_gold(brand: str) -> list[Row]:
    path = _gold_path(brand)
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line:
            out.append(Row(**json.loads(line)))
    return out


def _write_gold(brand: str, rows: list[Row]) -> None:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    _gold_path(brand).write_text(
        "\n".join(json.dumps(r.__dict__, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8")


def cmd_pull(brands: list[str], days: int, db: str) -> int:
    """Pull raw matches for each brand into the eval DB and stub a gold file.

    The filter is suppressed during the pull (key cleared) so we capture the raw,
    unfiltered substring matches — that's exactly the population the gold labels
    and the scorer judge."""
    engine = connect(db)
    init_schema(engine)
    saved = {k: os.environ.pop(k, None)
             for k in ("OPENAI_API_KEY", "REDLENS_LLM_API_KEY")}
    try:
        for brand in brands:
            res = track_topic(engine, brand, days=days)
            print(f"{brand}: {res.posts_new} raw matches across "
                  f"{res.subreddits_searched} subreddits", file=sys.stderr)
            with Session(engine) as s:
                topic = get_topic(s, brand)
                assert topic is not None
                posts = list(s.exec(
                    select(Post)
                    .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
                    .where(TopicPost.topic_id == topic.id)
                    .order_by(Post.score.desc())))  # type: ignore[attr-defined]
            existing = {r.id: r.label for r in _load_gold(brand)}  # keep labels
            rows = [Row(id=p.post_id, subreddit=p.subreddit_name,
                        title=p.title or "", body=(p.selftext or "")[:500],
                        label=existing.get(p.post_id, "")) for p in posts]
            _write_gold(brand, rows)
            print(f"  wrote {len(rows)} rows -> {_gold_path(brand)} "
                  f"({sum(1 for r in rows if r.label)} already labeled)")
    finally:
        for k, v in saved.items():
            if v is not None:
                os.environ[k] = v
    print(f"\nNow hand-label each row's \"label\" as \"relevant\"/\"junk\", "
          f"then: python {sys.argv[0]} score")
    return 0


def _score_brand(brand: str, key: str) -> tuple[int, int, int, int, int]:
    """Run the filter prompt over a brand's labeled gold; return the confusion
    counts (tp, fp, tn, fn, skipped-unlabeled) where 'positive' = relevant."""
    rows = [r for r in _load_gold(brand) if r.label in ("relevant", "junk")]
    skipped = len(_load_gold(brand)) - len(rows)
    if not rows:
        return (0, 0, 0, 0, skipped)
    posts = [Post(post_id=r.id, author_username="x", subreddit_name=r.subreddit,
                  created_utc=0, title=r.title, selftext=r.body) for r in rows]
    keywords = brand
    prompt = render("filter", brand=brand, keywords=keywords,
                    items=_item_block(posts))
    raw = llm.complete(prompt, key, max_tokens=2400, json_object=True)
    verdicts = _parse_verdicts(raw)
    tp = fp = tn = fn = 0
    for r in rows:
        v = verdicts.get(r.id)
        pred_relevant = True if v is None else v[0]   # keep-when-unsure
        gold_relevant = r.label == "relevant"
        if gold_relevant and pred_relevant:
            tp += 1
        elif gold_relevant and not pred_relevant:
            fn += 1            # hid a real mention — the costly error
        elif not gold_relevant and not pred_relevant:
            tn += 1            # correctly dropped junk
        else:
            fp += 1            # kept junk
    return (tp, fp, tn, fn, skipped)


def cmd_score(brands: list[str]) -> int:
    key = llm_api_key()
    if not key:
        print("score needs an LLM key (OPENAI_API_KEY / REDLENS_LLM_API_KEY)",
              file=sys.stderr)
        return 2
    print(f"{'brand':12} {'n':>4} {'recall':>7} {'prec_junk':>10} "
          f"{'agree':>6}  (tp/fp/tn/fn)")
    TP = FP = TN = FN = 0
    for brand in brands:
        tp, fp, tn, fn, _ = _score_brand(brand, key)
        n = tp + fp + tn + fn
        if not n:
            print(f"{brand:12}    0   (no labeled gold — run pull + label first)")
            continue
        TP, FP, TN, FN = TP + tp, FP + fp, TN + tn, FN + fn
        recall = tp / (tp + fn) if (tp + fn) else float("nan")
        prec_junk = tn / (tn + fn) if (tn + fn) else float("nan")
        agree = (tp + tn) / n
        print(f"{brand:12} {n:>4} {recall:>7.2f} {prec_junk:>10.2f} "
              f"{agree:>6.2f}  ({tp}/{fp}/{tn}/{fn})")
    n = TP + FP + TN + FN
    if n:
        recall = TP / (TP + FN) if (TP + FN) else float("nan")
        # precision on false-positive *calls*: of posts we called junk, how many
        # were truly junk.
        prec_junk = TN / (TN + FN) if (TN + FN) else float("nan")
        agree = (TP + TN) / n
        print("-" * 50)
        print(f"{'OVERALL':12} {n:>4} {recall:>7.2f} {prec_junk:>10.2f} "
              f"{agree:>6.2f}  ({TP}/{FP}/{TN}/{FN})")
        print("\nbar: recall>=0.95  precision_junk>=0.85  agreement>=0.90")
    return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)
    pull = sub.add_parser("pull")
    pull.add_argument("brands", nargs="*")
    pull.add_argument("--days", type=int, default=30)
    pull.add_argument("--db", default=".context/eval.db")
    score = sub.add_parser("score")
    score.add_argument("brands", nargs="*")
    args = p.parse_args(argv)

    brands = args.brands or list(BRANDS)
    if args.cmd == "pull":
        return cmd_pull(brands, args.days, args.db)
    return cmd_score(brands)


if __name__ == "__main__":
    raise SystemExit(main())
