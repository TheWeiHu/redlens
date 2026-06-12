from __future__ import annotations

import argparse
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

from redlens import __version__, explore, onboarding
from redlens.analytics import compute_user_analytics
from redlens.config import resolve_db
from redlens.db import connect, init_schema, session
from redlens.errors import NotFound, RedlensError
from redlens.ingest import sync_user
from redlens.page import render_topic_page
from redlens.topics import (
    SubredditCandidate,
    get_topic,
    search_subreddits,
    track_topic,
)


def _ts(s: int | None) -> str:
    if not s:
        return "—"
    return datetime.fromtimestamp(s, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def _slug(name: str) -> str:
    return "-".join(re.findall(r"[a-z0-9]+", name.lower())) or "topic"


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
        desc = c.description[:48] + ("…" if len(c.description) > 48 else "")
        print(f"  [{i:2d}] r/{c.name:<24} {c.subscribers:>10,} members{tag}  {desc}",
              file=sys.stderr)
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
    t.add_argument("--query", help="full-text query (default: the topic name)")
    t.add_argument("--days", type=int, help="trailing window (default: 180)")
    t.add_argument("--subreddits", help="comma-separated subreddits to add to the net")
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
            # First track of a topic: find the net and let the user curate
            # it. Re-tracks reuse the stored net without asking again.
            with session(engine) as s:
                existing = get_topic(s, args.topic)
            if not (existing and existing.subreddit_list):
                found = search_subreddits(args.topic)
                if found:
                    subs = (subs or []) + _pick_subreddits(
                        found, assume_yes=args.yes)
            res = track_topic(
                engine, args.topic,
                query=args.query, subreddits=subs,
                days=args.days, discover=args.discover,
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
