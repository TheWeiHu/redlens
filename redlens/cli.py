from __future__ import annotations

import argparse
import dataclasses
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from redlens import __version__, discovery, explore, onboarding
from redlens.analytics import compute_user_analytics
from redlens.config import llm_api_key, resolve_db
from redlens.db import connect, init_schema, session
from redlens.errors import NotFound, RedlensError
from redlens.ingest import sync_user
from redlens.page import render_topic_page
from redlens.topics import (
    SubredditCandidate,
    get_topic,
    query_terms,
    search_subreddits,
    track_topic,
)

# Discovery sources for a topic's subreddit net, in display order.
# (key, label, on by default)
SOURCES = (
    ("name", "subreddits whose name matches (keyless, via arctic)", True),
    ("global", "subreddits with matching posts (keyless, via PullPush)", True),
    ("web", "web search (DuckDuckGo; may hit bot walls)", False),
    ("popular", f"cast over the {len(discovery.POPULAR_SUBREDDITS)} most "
                "popular subreddits", False),
    ("llm", "LLM suggestions", False),
)


def _ts(s: int | None) -> str:
    if not s:
        return "—"
    return datetime.fromtimestamp(s, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _slug(name: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", name.lower())) or "topic"


def _choose_sources(*, assume_yes: bool) -> list[str]:
    """Ask which discovery sources to use for a new topic's net.

    Non-interactive runs and --yes use name matching only — the other
    sources (web scrape, 100-subreddit cast, paid LLM call) are opt-in
    choices a human should make.
    """
    if assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return ["name"]

    llm_ready = llm_api_key() is not None
    print("how should redlens find subreddits?", file=sys.stderr)
    for i, (key, label, default) in enumerate(SOURCES, 1):
        mark = " *" if default else ""
        note = "" if key != "llm" or llm_ready else \
            "  (needs an LLM API key — not configured)"
        print(f"  [{i}] {label}{mark}{note}", file=sys.stderr)
    print('sources ("1 2 4"), Enter for defaults (*), "s" skips discovery',
          file=sys.stderr)
    print("> ", end="", file=sys.stderr, flush=True)
    line = input().strip().lower()

    if line == "s":
        return []
    if not line:
        chosen = [key for key, _, default in SOURCES if default]
    else:
        chosen = [SOURCES[int(tok) - 1][0] for tok in line.split()
                  if tok.isdigit() and 1 <= int(tok) <= len(SOURCES)]
    if "llm" in chosen and not llm_ready:
        print("  skipping llm: set ANTHROPIC_API_KEY/OPENAI_API_KEY or "
              "[llm] api_key in config.toml", file=sys.stderr)
        chosen.remove("llm")
    return chosen


def _gather_candidates(
    terms: list[str], sources: list[str]
) -> tuple[list[SubredditCandidate], list[str]]:
    """Run the chosen discovery sources, fanned across all query terms.

    Returns (candidates for the picker, net additions that bypass it —
    the popular-subreddits cast is all-or-nothing, not row-by-row).
    """
    merged: dict[str, SubredditCandidate] = {}

    def add(candidates: list[SubredditCandidate]) -> None:
        for c in candidates:
            existing = merged.get(c.name.lower())
            if existing:
                if c.source not in existing.source:
                    merged[c.name.lower()] = dataclasses.replace(
                        existing, source=f"{existing.source}+{c.source}")
            else:
                merged[c.name.lower()] = c

    if "name" in sources:
        for term in terms:
            add(search_subreddits(term))
    for key, fetch in (("global", discovery.search_global),
                       ("web", discovery.search_web),
                       ("llm", discovery.suggest_llm)):
        if key not in sources:
            continue
        names: list[str] = []
        try:
            if key == "llm":  # one call covers every term
                names = fetch(", ".join(terms))
            else:
                for term in terms:
                    names += fetch(term)
        except RedlensError as exc:
            print(f"warning: {key} discovery failed: {exc}", file=sys.stderr)
        if not names:
            print(f"note: {key} search found no subreddits", file=sys.stderr)
        add([SubredditCandidate(name=n, subscribers=0, description="",
                                over_18=False, source=key)
             for n in names])

    popular = list(discovery.POPULAR_SUBREDDITS) if "popular" in sources else []
    return list(merged.values()), popular


def _pick_subreddits(
    candidates: list[SubredditCandidate], *, assume_yes: bool
) -> list[str]:
    """Show the found subreddits and let the user curate the net.

    Edits are '-N' to drop a row and '+name' to add a subreddit; Enter
    accepts. Non-interactive runs (pipes, cron) and --yes take the list
    as-is.
    """
    if assume_yes or not (sys.stdin.isatty() and sys.stdout.isatty()):
        return [c.name for c in candidates]

    print("subreddits found — this is the net:", file=sys.stderr)
    for i, c in enumerate(candidates, 1):
        tag = " [nsfw]" if c.over_18 else ""
        members = f"{c.subscribers:,}" if c.subscribers else "—"
        desc = c.description[:40] + ("…" if len(c.description) > 40 else "")
        print(f"  [{i:2d}] r/{c.name:<24} {members:>10} members "
              f"({c.source}){tag}  {desc}", file=sys.stderr)
    print('edit with "-2 -5" (drop) and "+popheads" (add); Enter accepts',
          file=sys.stderr)

    keep = dict(enumerate((c.name for c in candidates), 1))
    extras: list[str] = []
    while True:
        print("> ", end="", file=sys.stderr, flush=True)
        line = input().strip()
        if not line:
            return [n for _, n in sorted(keep.items())] + extras
        for tok in line.split():
            if tok.startswith("+") and len(tok) > 1:
                extras.append(tok[1:].removeprefix("r/"))
            elif tok.startswith("-") and tok[1:].isdigit() and int(tok[1:]) in keep:
                del keep[int(tok[1:])]
        current = [n for _, n in sorted(keep.items())] + extras
        print(f"net: {', '.join('r/' + n for n in current) or '(empty)'}",
              file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="redlens")
    p.add_argument("--version", action="version", version=f"redlens {__version__}")
    p.add_argument("--db", default=None, help="SQLite file (default: REDLENS_DB, "
                   "config.toml, or the per-user data dir)")
    sub = p.add_subparsers(dest="verb", required=True)
    sub.add_parser("init")
    sub.add_parser("sync").add_argument("username")
    a = sub.add_parser("analytics")
    a.add_argument("username")
    a.add_argument("--json", action="store_true")
    e = sub.add_parser("explore")
    e.add_argument("--host", default="127.0.0.1")
    e.add_argument("--port", type=int, default=8000)
    e.add_argument("--no-browser", action="store_true")
    t = sub.add_parser("track", help="follow a topic across public discussion")
    t.add_argument("topic")
    t.add_argument("--query", help="full-text query; comma-separated terms "
                   "are OR'd, e.g. 'ubi, universal basic income' "
                   "(default: the topic name)")
    t.add_argument("--days", type=int, help="trailing window (default: 180)")
    t.add_argument("--subreddits", help="comma-separated subreddits to add to the net")
    t.add_argument("--exclude", help="comma-separated terms; posts containing "
                   "any are dropped, e.g. 'ubisoft, rainbow six' for topic ubi")
    t.add_argument("--discover", action="store_true",
                   help="widen the net one round via authors of matching posts")
    t.add_argument("-y", "--yes", action="store_true",
                   help="accept the found subreddit list without the picker")
    g = sub.add_parser("page", help="render a tracked topic as a standalone HTML page")
    g.add_argument("topic")
    g.add_argument("-o", "--out", help="output path (default: ./<topic>.html)")
    if onboarding.ENABLED:
        sub.add_parser("setup")
    args = p.parse_args(argv)

    try:
        if args.verb == "setup":
            return onboarding.run_wizard()
        onboarding.offer_setup_on_first_run()
        db = resolve_db(args.db)
        if args.verb == "explore":
            return explore.serve(db, host=args.host, port=args.port,
                                 open_browser=not args.no_browser)
        engine = connect(db)
        init_schema(engine)
        if args.verb == "init":
            print(f"schema applied to {db}")
        elif args.verb == "sync":
            r = sync_user(args.username, engine)
            print(f"u/{r.user.username}: "
                  f"{r.posts_written:,} posts, {r.comments_written:,} comments")
        elif args.verb == "track":
            subs = ([s.strip() for s in args.subreddits.split(",") if s.strip()]
                    if args.subreddits else None)
            # First track of a topic: pick discovery sources, gather, and
            # let the user curate the resulting net. Re-tracks reuse the
            # stored net without asking again.
            with session(engine) as s:
                existing = get_topic(s, args.topic)
            if not (existing and existing.subreddit_list):
                sources = _choose_sources(assume_yes=args.yes)
                terms = query_terms(args.query) if args.query else [args.topic]
                found, popular = _gather_candidates(terms, sources)
                if found:
                    subs = (subs or []) + _pick_subreddits(
                        found, assume_yes=args.yes)
                if popular:
                    print(f"+ casting over {len(popular)} popular subreddits",
                          file=sys.stderr)
                    subs = (subs or []) + popular
            res = track_topic(
                engine, args.topic,
                query=args.query, subreddits=subs,
                days=args.days, exclude=args.exclude, discover=args.discover,
                on_progress=lambda sub, n: print(
                    f"  r/{sub}: {n} new", file=sys.stderr),
            )
            if res.discovered:
                print(f"discovered: {', '.join('r/' + s for s in res.discovered)}",
                      file=sys.stderr)
            for failed_sub, err in res.failed.items():
                print(f"warning: r/{failed_sub} skipped: {err}", file=sys.stderr)
            print(f"{res.topic.name!r}: {res.posts_new:,} new posts across "
                  f"{res.subreddits_searched} subreddits "
                  f"(query {res.topic.query!r}, last {res.topic.days} days)")
            print(f"next: redlens page {res.topic.name!r}")
        elif args.verb == "page":
            html_doc = render_topic_page(engine, args.topic)
            out = Path(args.out or f"{_slug(args.topic)}.html")
            out.write_text(html_doc, encoding="utf-8")
            print(f"wrote {out} ({len(html_doc):,} bytes)")
        else:
            with session(engine) as s:
                an = compute_user_analytics(s, args.username)
            if args.json:
                print(an.model_dump_json(indent=2))
            else:
                print(f"u/{an.username}: {an.total_posts:,} posts, "
                      f"{an.total_comments:,} comments, "
                      f"karma {an.total_karma:+,} "
                      f"(posts {an.post_karma:+,}, comments {an.comment_karma:+,})")
                print(f"  active {an.active_days:,} days · "
                      f"{an.distinct_subreddits:,} subs · "
                      f"top r/{an.top_subreddit} "
                      f"({an.top_subreddit_event_count:,} events)")
                print(f"  first {_ts(an.first_event_at)} · last {_ts(an.last_event_at)}")
        return 0
    except NotFound as e:
        print(f"not found: {e}", file=sys.stderr)
        return 2
    except RedlensError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
