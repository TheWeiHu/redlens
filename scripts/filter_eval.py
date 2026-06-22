#!/usr/bin/env python3
"""Offline eval for the topic relevance filter.

Scores the PRODUCTION filter path (``redlens/prompts/filter.txt`` + ``redlens.filter``)
over a frozen, hand-labeled gold set: ``tests/fixtures/filter_gold.csv`` — ~270 Reddit
posts across 9 ambiguous brands (126 on-topic / 143 off-topic), built once via the real
``track`` discovery (PullPush + name search) so each brand carries both senses.

Sweeps the 3 JSON key orderings (where ``relevant`` sits vs its ``reason``) × N runs
concurrently and writes per-(order, run, brand) + OVERALL metrics to CSV on stdout:
``recall`` (on-topic kept) · ``drop_precision`` (drops that were off-topic) · ``agreement``.

Finding: ``--about`` (the one-line authoritative sense per brand) is by far the biggest
lever — junk caught ~0.50 → ~0.91, clearing the bar — dwarfing key order and batch size.

    python scripts/filter_eval.py --runs 10 --batch 50            # infer-only
    python scripts/filter_eval.py --runs 10 --batch 50 --about    # with descriptions
"""
from __future__ import annotations

import argparse
import csv
import sys
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere

from redlens import config, constants, llm, prompts  # noqa: E402
from redlens.errors import RedlensError  # noqa: E402
from redlens.filter import _chunked, _item_block, _parse_verdicts, about_clause  # noqa: E402
from redlens.models import Post  # noqa: E402

GOLD = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "filter_gold.csv"

# One-line authoritative sense per brand — the `--about` path that pins the meaning.
ABOUT: dict[str, str] = {
    "arc": "the Arc web browser by The Browser Company — not the game Arc Raiders, "
           "electric arcs, story arcs, or architecture",
    "bolt": "a bolt fastener / hardware bolt as discussed in DIY and construction — "
            "not Pokemon cards (Raging/Black Bolt), lightning bolts, or Usain Bolt",
    "corona": "Corona the Mexican beer — not the COVID virus, a cigar size, the "
              "Xbox-360 'Corona' board, or the city",
    "dove": "Dove personal-care products (soap, body wash, deodorant) by Unilever — "
            "not Dove Cameron, the bird, or Italian 'dove' meaning 'where'",
    "linear": "Linear the issue-tracking / project-management app (linear.app) — not "
              "linear algebra, linear keyboard switches, or the adjective 'linear'",
    "monday": "monday.com the project-management SaaS — not the weekday Monday",
    "notion": "Notion the note-taking / productivity app (notion.so) — not the "
              "everyday word 'notion' meaning an idea",
    "shell": "Shell the oil and energy company — not a seashell, tortoise shell, a "
             "Unix/command shell, or a shell company",
    "square": "Square the payments company (Block) and its POS — not square footage, "
              "a crochet granny square, 'back to square one', or Squarespace",
}

# The single experimental factor: where the `relevant` boolean sits vs its `reason`
# (writing the reason first is a chain-of-thought nudge). filter.txt ships reason-first.
_F = {"id": '"id": "<the post id, copied exactly>"', "verdict": '"relevant": true',
      "score": '"confidence": 0.0', "reason": '"reason": "<≤12 words>"'}
ORDERS = {
    "verdict-score-reason": ["id", "verdict", "score", "reason"],
    "reason-verdict-score": ["id", "reason", "verdict", "score"],   # shipped
    "reason-score-verdict": ["id", "reason", "score", "verdict"],
}
_SHIPPED = "reason-verdict-score"


def _example(order: str) -> str:
    return "{" + ", ".join(_F[k] for k in ORDERS[order]) + "}"


def _load_gold() -> dict[str, list[dict]]:
    """``brand -> [rows]`` from the combined gold CSV (``gold`` as bool)."""
    gold: dict[str, list[dict]] = defaultdict(list)
    with GOLD.open(encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            r["gold"] = r["gold"] == "1"
            gold[r["brand"]].append(r)
    return gold


def _predict(rows: list[dict], key: str, order: str, batch: int, about: str) -> dict[str, bool]:
    """Classify a brand's posts exactly as production does (same prompt + parser),
    keeping any post the model omits (keep-when-unsure)."""
    brand = rows[0]["brand"]
    by_id = {r["id"]: Post(post_id=r["id"], author_username="", subreddit_name=r["subreddit"],
                           created_utc=0, title=r["title"], selftext=r["selftext"],
                           score=0, num_comments=0) for r in rows}
    about_line = about_clause(about)
    pred: dict[str, bool] = {}
    for chunk in _chunked(list(by_id), batch):
        prompt = prompts.render("filter", brand=brand, keywords=brand, about=about_line,
                                items=_item_block([by_id[i] for i in chunk], [brand]))
        prompt = prompt.replace(_example(_SHIPPED), _example(order))
        try:
            verdicts = _parse_verdicts(llm.complete(
                prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS, json_object=True))
        except RedlensError:
            verdicts = {}
        for pid in chunk:
            v = verdicts.get(pid)
            pred[pid] = True if v is None else v[0]
    return pred


def _confusion(rows: list[dict], pred: dict[str, bool]) -> dict[str, int]:
    """Raw counts for one (brand, run): on/off-topic totals, drops, correct drops,
    and matches — poolable by summation, so OVERALL is just a sum of these."""
    c = dict(n=len(rows), on=0, off=0, drops=0, correct=0, agree=0)
    for r in rows:
        g, p = r["gold"], pred.get(r["id"], True)
        c["agree"] += p == g
        c["on" if g else "off"] += 1
        if not p:
            c["drops"] += 1
            c["correct"] += not g
    return c


def _rate(num: int, den: int) -> str:
    return f"{num / den:.4f}" if den else ""  # blank when undefined (e.g. 0 drops)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--runs", type=int, default=10)
    ap.add_argument("--batch", type=int, default=constants.FILTER_BATCH)
    ap.add_argument("--order", choices=(*ORDERS, "all"), default="all")
    ap.add_argument("--about", action="store_true",
                    help="inject the per-brand authoritative sense (the --about path)")
    ap.add_argument("--workers", type=int, default=8)
    args = ap.parse_args(argv)
    key = config.llm_api_key()
    if not key:
        sys.exit("needs an LLM key (REDLENS_LLM_API_KEY / OPENAI_API_KEY / config)")

    gold = _load_gold()
    orders = list(ORDERS) if args.order == "all" else [args.order]
    tasks = [(o, run, b) for o in orders for run in range(1, args.runs + 1) for b in gold]
    print(f"model={llm.model_name()} orders={orders} runs={args.runs} batch={args.batch} "
          f"about={'on' if args.about else 'off'} ({len(tasks)} calls)", file=sys.stderr)

    def work(t: tuple[str, int, str]) -> tuple[tuple[str, int, str], dict[str, int]]:
        o, _, b = t
        return t, _confusion(gold[b], _predict(
            gold[b], key, o, args.batch, ABOUT.get(b, "") if args.about else ""))

    results: dict[tuple[str, int, str], dict[str, int]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for t, m in pool.map(work, tasks):
            results[t] = m

    w = csv.writer(sys.stdout)
    w.writerow(["order", "run", "scope", "n", "on_topic", "off_topic", "dropped",
                "recall", "drop_precision", "agreement"])

    def emit(o: str, run: int, scope: str, c: dict[str, int]) -> None:
        kept_on = c["on"] - (c["drops"] - c["correct"])  # on-topic not wrongly dropped
        w.writerow([o, run, scope, c["n"], c["on"], c["off"], c["drops"],
                    _rate(kept_on, c["on"]), _rate(c["correct"], c["drops"]),
                    _rate(c["agree"], c["n"])])

    for o in orders:
        for run in range(1, args.runs + 1):
            total = dict(n=0, on=0, off=0, drops=0, correct=0, agree=0)
            for b in gold:
                c = results[(o, run, b)]
                emit(o, run, b, c)
                for k in total:
                    total[k] += c[k]
            emit(o, run, "OVERALL", total)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
