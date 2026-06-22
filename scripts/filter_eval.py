#!/usr/bin/env python3
"""Offline eval harness for the topic relevance filter.

`track`'s relevance filter hides posts a cheap model (gpt-4o-mini) judges off-topic,
so before trusting it we must *prove* the cheap model clears the bar against gold
labels. This is that proof loop, split so the expensive parts run once:

  pull   Track each brand against real arctic ONCE and freeze every RAW match
         (id, subreddit, title, selftext) to tests/fixtures/filter_gold/<brand>.jsonl
         with "gold": null. Opus 4.8 then hand-labels each item's gold
         (true = on-topic / false = false positive). One bounded arctic pull per
         brand — polite by design; every later prompt iteration scores against the
         frozen set, never a re-pull.

  score  Run the PRODUCTION filter path (the same redlens/prompts/filter.txt prompt
         and redlens.filter parsing) with gpt-4o-mini over the frozen, labeled gold
         and print a per-brand + overall confusion matrix vs the bar:

             recall on true brand mentions  >= 0.95   (we almost never purge a real one)
             precision on false-positive calls >= 0.85 (when it says "junk", it's right)
             overall agreement              >= 0.90

`score` is fully offline (no arctic) and needs only an LLM key. Because it reuses the
real prompt + parser, tuning redlens/prompts/filter.txt and re-running `score` measures
exactly what production would do.

Usage:
    python scripts/filter_eval.py pull                  # all brands, last 30 days
    python scripts/filter_eval.py pull --brand bolt --days 30
    # ... Opus labels the "gold" field in each tests/fixtures/filter_gold/*.jsonl ...
    python scripts/filter_eval.py score                 # score every labeled brand
    python scripts/filter_eval.py score --brand bolt
"""
from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import sys
import tempfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

from sqlalchemy import func
from sqlmodel import Session, select

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # run from anywhere

from redlens import config, constants, llm, prompts  # noqa: E402
from redlens.db import connect, init_schema
from redlens.errors import RedlensError
from redlens.filter import _chunked, _item_block, _parse_verdicts, about_clause
from redlens.models import Post, Topic, TopicPost
from redlens.topics import track_topic

GOLD_DIR = Path(__file__).resolve().parent.parent / "tests" / "fixtures" / "filter_gold"

# The 10 brands from the task: a name whose everyday sense creates the false
# positives, paired with subreddits where the *product* sense actually shows up.
# keywords default to [brand]; `about` stays empty so the eval measures pure
# sense-inference (the MVP path) — set one only to test an --about hint.
BRANDS: dict[str, dict[str, object]] = {
    "arc":       {"subreddits": ["browsers", "ArcBrowser"]},
    "linear":    {"subreddits": ["ProductManagement", "projectmanagement"]},
    "monday":    {"subreddits": ["projectmanagement", "Productivity"]},
    "bolt":      {"subreddits": ["DIY", "electricians"]},
    "square":    {"subreddits": ["smallbusiness", "Payments"]},
    "shell":     {"subreddits": ["energy", "stocks"]},
    "corona":    {"subreddits": ["beer", "CraftBeer"]},
    "notion":    {"subreddits": ["Notion", "productivity"]},
    "dove":      {"subreddits": ["SkincareAddiction", "beauty"]},
}


# One-line authoritative sense per brand, for the `--about` path (the prompt's
# $about slot). Pins the intended meaning so the model isn't guessing from context.
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


def _brand_keys(brand: str | None) -> list[str]:
    if brand is None:
        return list(BRANDS)
    if brand not in BRANDS:
        sys.exit(f"unknown brand {brand!r}; known: {', '.join(BRANDS)}")
    return [brand]


# --- pull: freeze raw matches for labeling ---------------------------------

def _raw_matches(brand: str, subreddits: list[str], days: int) -> list[Post]:
    """Track ``brand`` against real arctic in a throwaway DB and return every raw
    match (the filter never deletes, so all matches are present regardless of
    whether a key would flag any). Runs keyless so the gold pull pays no LLM."""
    saved = {v: os.environ.pop(v, None)
             for v in ("REDLENS_LLM_API_KEY", "OPENAI_API_KEY")}
    prev_cfg = os.environ.get("REDLENS_CONFIG")
    try:
        with tempfile.TemporaryDirectory() as tmp:
            # Point config at a nonexistent file so no config-file key leaks in.
            os.environ["REDLENS_CONFIG"] = str(Path(tmp) / "no-config.toml")
            engine = connect(Path(tmp) / "eval.db")
            init_schema(engine)
            track_topic(engine, brand, subreddits=subreddits, days=days,
                        on_progress=lambda sub, n: print(
                            f"  r/{sub}: {n} new", file=sys.stderr))
            with Session(engine) as s:
                return list(s.exec(
                    select(Post)
                    .join(TopicPost, TopicPost.post_id == Post.post_id)
                    .join(Topic, Topic.id == TopicPost.topic_id)
                    .where(func.lower(Topic.name) == brand.lower())
                    .order_by(Post.post_id)))
    finally:
        for var, val in saved.items():
            if val is not None:
                os.environ[var] = val
        if prev_cfg is None:
            os.environ.pop("REDLENS_CONFIG", None)
        else:
            os.environ["REDLENS_CONFIG"] = prev_cfg


# Obvious NSFW / hookup subreddits — valid "off-topic" but unwanted in a
# committed fixture, so they're dropped from the discovered net.
_NSFW = re.compile(
    r"nsfw|lewd|porn|booty|tits|boob|fuck|fap|cock|nude|gonewild|cum|sissy|"
    r"deciders|personals|r4r|hookup|onlyfans|nsf|milf|gw\b", re.I)


def _offtopic_net(brand: str, product_subs: list[str], want: int) -> list[str]:
    """Communities where the brand WORD appears in some OTHER sense — the source
    of negatives. Uses the same online discovery the real ``track`` does (PullPush
    global full-text + arctic name search), minus the product subreddits and minus
    NSFW noise."""
    from redlens import discovery
    from redlens.topics import search_subreddits as name_search
    cand: list[str] = []
    with contextlib.suppress(RedlensError):
        cand += discovery.search_global(brand)        # PullPush: diverse senses
    with contextlib.suppress(RedlensError):
        cand += [c.name for c in name_search(brand)]  # arctic name match
    prod = {p.lower() for p in product_subs}
    out: list[str] = []
    seen: set[str] = set()
    for n in cand:
        k = n.lower()
        if k in prod or k in seen or _NSFW.search(n):
            continue
        seen.add(k)
        out.append(n)
    return out[:want]


def _freeze(fh: Any, brand: str, about: str, posts: list[Post], seen: set[str]) -> int:
    n = 0
    for p in posts:
        if p.post_id in seen:
            continue
        seen.add(p.post_id)
        fh.write(json.dumps({
            "brand": brand, "keywords": [brand], "about": about,
            "id": p.post_id, "subreddit": p.subreddit_name,
            "title": p.title or "", "selftext": p.selftext or "",
            "gold": None,  # Opus labels this: true=on-topic, false=off-topic
        }) + "\n")
        n += 1
    return n


def cmd_pull(args: argparse.Namespace) -> int:
    GOLD_DIR.mkdir(parents=True, exist_ok=True)
    for brand in _brand_keys(args.brand):
        spec = BRANDS[brand]
        product_subs = list(spec["subreddits"])  # type: ignore[arg-type]
        about = str(spec.get("about", ""))
        out = GOLD_DIR / f"{brand}.jsonl"
        seen: set[str] = set()
        with out.open("w", encoding="utf-8") as fh:
            if not args.discover:
                print(f"pulling r/{', r/'.join(product_subs)} for {brand!r}…",
                      file=sys.stderr)
                npos = _freeze(fh, brand, about,
                               _raw_matches(brand, product_subs, args.days), seen)
                print(f"  froze {npos} matches -> {out}", file=sys.stderr)
                continue
            # Discovery mode (option A): positives from the product seed, negatives
            # from the online-discovered net, each capped so the brand has both.
            off_subs = _offtopic_net(brand, product_subs, args.net)
            print(f"{brand}: product r/{', r/'.join(product_subs)}  "
                  f"| off-topic r/{', r/'.join(off_subs) or '(none found)'}",
                  file=sys.stderr)
            npos = _freeze(fh, brand, about,
                           _raw_matches(brand, product_subs, args.days)[:args.pos], seen)
            nneg = _freeze(fh, brand, about,
                           _raw_matches(brand, off_subs, args.days)[:args.neg], seen) \
                if off_subs else 0
            print(f"  froze {npos} product-sub + {nneg} off-topic-sub = {npos + nneg} "
                  f"-> {out}  (label gold next)", file=sys.stderr)
    return 0


# --- score: run the production filter over the frozen gold -----------------

def _load_gold(brand: str) -> list[dict]:
    path = GOLD_DIR / f"{brand}.jsonl"
    if not path.exists():
        sys.exit(f"no gold for {brand!r} at {path} — run `pull` first")
    rows = [json.loads(ln) for ln in path.read_text(encoding="utf-8").splitlines()
            if ln.strip()]
    return rows


# The verdict-object field order is a single experimental factor: where the
# `relevant` boolean sits relative to its free-text `reason`. The JSON parser is
# order-insensitive, but the *requested* order changes the model's answers —
# making it write the reason before committing the boolean is a chain-of-thought
# nudge. The three orderings move `relevant` through positions 1/2/3 so the effect
# is monotonic. filter.txt ships `reason-first`; the others rewrite the example.
_ID = '"id": "<the post id, copied exactly>"'
_REL = '"relevant": true'
_CONF = '"confidence": 0.0'
_REASON = '"reason": "<≤12 words>"'
ORDERS: dict[str, list[str]] = {
    "relevant-first": [_ID, _REL, _CONF, _REASON],   # decide, then justify
    "reason-first":   [_ID, _REASON, _REL, _CONF],   # justify, then decide (SHIPPED)
    "relevant-last":  [_ID, _REASON, _CONF, _REL],   # justify fully, then decide
}


def _example(order: str) -> str:
    return "{" + ", ".join(ORDERS[order]) + "}"


def _field_seq(order: str) -> str:
    """The literal key sequence for an ordering, e.g. ``id>reason>relevant>confidence``
    — so a CSV row says what the ordering actually is, not just its name."""
    return ">".join(snippet.split('"')[1] for snippet in ORDERS[order])


_SHIPPED_EXAMPLE = _example("reason-first")  # must match prompts/filter.txt verbatim


def _predict(rows: list[dict], key: str, order: str = "reason-first",
             batch: int = constants.FILTER_BATCH) -> dict[str, bool]:
    """Classify ``rows`` exactly as production does: same prompt, same parser,
    same batch size, same keep-when-unsure default for an omitted id. ``order``
    selects the requested JSON field order in the prompt's example object
    (``reason-first`` = the shipped form, a no-op rewrite)."""
    brand = rows[0]["brand"]
    keywords = rows[0].get("keywords") or [brand]
    about = rows[0].get("about", "")
    pred: dict[str, bool] = {}
    for chunk in _chunked([r["id"] for r in rows], batch):
        by_id = {r["id"]: r for r in rows}
        posts = [Post(post_id=r["id"], author_username="", subreddit_name=r["subreddit"],
                      created_utc=0, title=r["title"], selftext=r["selftext"],
                      score=0, num_comments=0)
                 for r in (by_id[i] for i in chunk)]
        prompt = prompts.render(
            "filter", brand=brand, keywords=", ".join(keywords),
            about=about_clause(about), items=_item_block(posts, list(keywords)))
        prompt = prompt.replace(_SHIPPED_EXAMPLE, _example(order))
        try:
            raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS,
                               json_object=True)
            verdicts = _parse_verdicts(raw)
        except RedlensError as exc:
            print(f"  batch failed ({exc}); those ids kept (unscored)",
                  file=sys.stderr)
            verdicts = {}
        for pid in chunk:
            v = verdicts.get(pid)
            pred[pid] = True if v is None else v[0]  # omitted id -> keep (recall bias)
    return pred


def _confusion(rows: list[dict], pred: dict[str, bool]) -> dict[str, float]:
    """Per-brand counts + the three bar metrics over labeled rows only."""
    gold_t = gold_f = tp_true = pred_junk = junk_ok = agree = labeled = 0
    for r in rows:
        gold = r.get("gold")
        if not isinstance(gold, bool):
            continue  # unlabeled — excluded from the metrics
        labeled += 1
        p = pred.get(r["id"], True)
        agree += int(p == gold)
        if gold:
            gold_t += 1
            tp_true += int(p is True)
        else:
            gold_f += 1
        if p is False:
            pred_junk += 1
            junk_ok += int(gold is False)
    return {
        "labeled": labeled, "gold_true": gold_t, "gold_false": gold_f,
        "pred_junk": pred_junk,
        "recall_true": (tp_true / gold_t) if gold_t else float("nan"),
        "precision_junk": (junk_ok / pred_junk) if pred_junk else float("nan"),
        "agreement": (agree / labeled) if labeled else float("nan"),
    }


def _fmt(x: float) -> str:
    return "  n/a" if x != x else f"{x:5.2f}"  # x!=x catches NaN


def _pooled(agg: list[dict]) -> dict[str, float]:
    """Pool per-brand counts (not a mean of rates) so small brands don't skew it."""
    tot_t = sum(m["gold_true"] for m in agg)
    tot_junk = sum(m["pred_junk"] for m in agg)
    tot_n = sum(m["labeled"] for m in agg)
    return {
        "labeled": tot_n, "gold_true": tot_t,
        "gold_false": sum(m["gold_false"] for m in agg),
        "recall_true": sum(m["recall_true"] * m["gold_true"]
                           for m in agg if m["gold_true"]) / tot_t if tot_t else float("nan"),
        "precision_junk": sum(m["precision_junk"] * m["pred_junk"]
                              for m in agg if m["pred_junk"]) / tot_junk if tot_junk else float("nan"),
        "agreement": sum(m["agreement"] * m["labeled"] for m in agg) / tot_n if tot_n else float("nan"),
    }


def _stdev(xs: list[float]) -> float:
    xs = [x for x in xs if x == x]  # drop NaN
    if len(xs) < 2:
        return 0.0
    mean = sum(xs) / len(xs)
    return (sum((x - mean) ** 2 for x in xs) / (len(xs) - 1)) ** 0.5


def _score_pass(brands: list[str], key: str, order: str,
                show_brands: bool) -> dict[str, float] | None:
    """One full scoring pass over every labeled brand; returns pooled overall."""
    header = f"{'brand':<12} {'n':>4} {'+':>4} {'-':>4} {'recall':>7} {'prec':>7} {'agree':>7}"
    if show_brands:
        print(header)
        print("-" * len(header))
    agg: list[dict] = []
    for brand in brands:
        if not (GOLD_DIR / f"{brand}.jsonl").exists():
            continue
        rows = _load_gold(brand)
        if not any(isinstance(r.get("gold"), bool) for r in rows):
            continue
        m = _confusion(rows, _predict(rows, key, order))
        agg.append(m)
        if show_brands:
            print(f"{brand:<12} {m['labeled']:>4} {m['gold_true']:>4} {m['gold_false']:>4} "
                  f"{_fmt(m['recall_true'])} {_fmt(m['precision_junk'])} {_fmt(m['agreement'])}")
    if not agg:
        return None
    o = _pooled(agg)
    if show_brands:
        print("-" * len(header))
    print(f"{'OVERALL':<12} {o['labeled']:>4} {o['gold_true']:>4} {o['gold_false']:>4} "
          f"{_fmt(o['recall_true'])} {_fmt(o['precision_junk'])} {_fmt(o['agreement'])}")
    return o


def _csv_cell(x: float) -> str:
    return "" if x != x else f"{x:.4f}"  # blank for NaN (e.g. precision with 0 junk calls)


def cmd_grid(args: argparse.Namespace) -> int:
    """The clean factorial: one gold set × every ordering × N runs → CSV on stdout.
    One row per (order, run, scope); scope is each brand plus OVERALL.

    Every (order, run, brand) classification is an independent LLM call, so they
    run concurrently through a bounded thread pool — the work is latency-bound
    (hundreds of round-trips), so this is the difference between ~1 min and ~1 hr.
    Output is reassembled in deterministic (order, run, brand) order regardless of
    which task finishes first."""
    key = config.llm_api_key()
    if not key:
        sys.exit("grid needs an LLM key (REDLENS_LLM_API_KEY / OPENAI_API_KEY / config)")
    brands = _brand_keys(args.brand)
    orders = list(ORDERS) if args.order == "all" else [args.order]
    # Load + filter each brand's gold once, not per (order, run).
    gold = {b: _load_gold(b) for b in brands
            if (GOLD_DIR / f"{b}.jsonl").exists()
            and any(isinstance(r.get("gold"), bool) for r in _load_gold(b))}
    if not gold:
        sys.exit("no labeled gold — run `pull` and label first")
    if args.about:  # inject the authoritative one-line sense (the --about path)
        for b, rows in gold.items():
            for r in rows:
                r["about"] = ABOUT.get(b, "")
    print(f"about={'on' if args.about else 'off'} "
          f"model={llm.model_name()} orders={orders} runs={args.runs} "
          f"brands={len(gold)} workers={args.workers} "
          f"({len(orders) * args.runs * len(gold)} classifications)", file=sys.stderr)

    tasks = [(order, run, brand)
             for order in orders
             for run in range(1, args.runs + 1)
             for brand in gold]
    done = [0]

    def work(t: tuple[str, int, str]) -> tuple[tuple[str, int, str], dict[str, float]]:
        order, run, brand = t
        m = _confusion(gold[brand], _predict(gold[brand], key, order, args.batch))
        done[0] += 1
        if done[0] % 20 == 0 or done[0] == len(tasks):
            print(f"  {done[0]}/{len(tasks)} classifications", file=sys.stderr)
        return t, m

    results: dict[tuple[str, int, str], dict[str, float]] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        for t, m in pool.map(work, tasks):
            results[t] = m

    cols = ["order", "fields", "run", "scope", "n", "on_topic", "junk", "pred_junk",
            "recall", "precision_junk", "agreement"]
    print(",".join(cols))

    def emit(order: str, run: int, scope: str, m: dict[str, float]) -> None:
        print(",".join([order, _field_seq(order), str(run), scope,
                        str(int(m["labeled"])), str(int(m["gold_true"])),
                        str(int(m["gold_false"])), str(int(m.get("pred_junk", 0))),
                        _csv_cell(m["recall_true"]), _csv_cell(m["precision_junk"]),
                        _csv_cell(m["agreement"])]))

    for order in orders:
        for run in range(1, args.runs + 1):
            per_brand = [(b, results[(order, run, b)]) for b in gold]
            if args.by_brand:
                for brand, m in per_brand:
                    emit(order, run, brand, m)
            overall = _pooled([m for _, m in per_brand])
            overall["pred_junk"] = sum(m["pred_junk"] for _, m in per_brand)
            emit(order, run, "OVERALL", overall)
    return 0


def cmd_score(args: argparse.Namespace) -> int:
    key = config.llm_api_key()
    if not key:
        sys.exit("score needs an LLM key (REDLENS_LLM_API_KEY / OPENAI_API_KEY / config)")
    REC, PREC, AGREE = 0.95, 0.85, 0.90
    brands = _brand_keys(args.brand)
    print(f"model: {llm.model_name()}  order: {args.order}  runs: {args.runs}\n",
          file=sys.stderr)

    runs: list[dict[str, float]] = []
    for i in range(args.runs):
        if args.runs > 1:
            print(f"=== run {i + 1}/{args.runs} ===")
        o = _score_pass(brands, key, args.order, show_brands=(args.runs == 1))
        if o is None:
            sys.exit("\nno labeled gold found — run `pull`, then label the 'gold' fields")
        runs.append(o)
        print()

    if args.runs > 1:
        # Nondeterminism across identical prompts: report mean ± stdev per metric.
        print("=== variance across runs (mean ± stdev) ===")
        for met in ("recall_true", "precision_junk", "agreement"):
            vals = [r[met] for r in runs]
            mean = sum(v for v in vals if v == v) / len(vals)
            print(f"  {met:<15} {mean:5.3f} ± {_stdev(vals):.3f}   "
                  f"(min {min(vals):.3f}, max {max(vals):.3f})")
        recall = sum(r["recall_true"] for r in runs) / len(runs)
        # Average precision only over runs where it is defined (a run that made 0
        # junk calls has none) — dividing by all runs would understate it.
        precs = [r["precision_junk"] for r in runs if r["precision_junk"] == r["precision_junk"]]
        precision = sum(precs) / len(precs) if precs else float("nan")
        agreement = sum(r["agreement"] for r in runs) / len(runs)
    else:
        recall, precision, agreement = (runs[0]["recall_true"], runs[0]["precision_junk"],
                                        runs[0]["agreement"])

    ok = (recall >= REC and (precision != precision or precision >= PREC) and agreement >= AGREE)
    print(f"bar: recall>={REC} precision-on-junk>={PREC} agreement>={AGREE}")
    print("RESULT:", "PASS ✓" if ok else "FAIL ✗", f"(mean over {args.runs} run(s))")
    return 0 if ok else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("pull", help="freeze raw matches per brand for labeling")
    p.add_argument("--brand", help="one brand (default: all 10)")
    p.add_argument("--days", type=int, default=120, help="trailing window (default 120)")
    p.add_argument("--discover", action="store_true",
                   help="option A: positives from the product seed + negatives from the "
                        "online-discovered net (PullPush/name), so each brand has both")
    p.add_argument("--pos", type=int, default=15, help="cap on product-sub (positive) matches")
    p.add_argument("--neg", type=int, default=15, help="cap on off-topic-sub (negative) matches")
    p.add_argument("--net", type=int, default=8, help="how many off-topic subreddits to use")
    p.set_defaults(func=cmd_pull)
    s = sub.add_parser("score", help="score gpt-4o-mini over the labeled gold")
    s.add_argument("--brand", help="one brand (default: all labeled)")
    s.add_argument("--runs", type=int, default=1,
                   help="repeat N times and report metric mean ± stdev (LLM nondeterminism)")
    s.add_argument("--order", choices=tuple(ORDERS), default="reason-first",
                   help="JSON field order in the prompt example (default: shipped reason-first)")
    s.set_defaults(func=cmd_score)
    g = sub.add_parser("grid", help="factorial: gold × orderings × N runs → CSV on stdout")
    g.add_argument("--brand", help="one brand (default: all labeled)")
    g.add_argument("--runs", type=int, default=10, help="runs per ordering (default 10)")
    g.add_argument("--order", choices=(*ORDERS, "all"), default="all",
                   help="which ordering(s) to sweep (default: all three)")
    g.add_argument("--by-brand", action="store_true",
                   help="also emit one CSV row per brand (not just OVERALL)")
    g.add_argument("--workers", type=int, default=16,
                   help="concurrent LLM requests (default 16; the calls are independent)")
    g.add_argument("--batch", type=int, default=constants.FILTER_BATCH,
                   help=f"posts per LLM call (default {constants.FILTER_BATCH}; "
                        "smaller = more attention per post)")
    g.add_argument("--about", action="store_true",
                   help="pass the per-brand authoritative one-line sense (the --about path)")
    g.set_defaults(func=cmd_grid)
    args = parser.parse_args(argv)
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
