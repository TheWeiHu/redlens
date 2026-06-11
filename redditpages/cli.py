from __future__ import annotations

import argparse
import sys
from datetime import UTC, datetime

from redditpages import __version__, explore, onboarding
from redditpages.analytics import compute_user_analytics
from redditpages.config import resolve_db
from redditpages.db import connect, init_schema, session
from redditpages.errors import NotFound, RedditPagesError
from redditpages.ingest import sync_user


def _ts(s: int | None) -> str:
    if not s:
        return "—"
    return datetime.fromtimestamp(s, tz=UTC).strftime("%Y-%m-%d %H:%MZ")


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="redditpages")
    p.add_argument("--version", action="version", version=f"redditpages {__version__}")
    p.add_argument("--db", default=None, help="SQLite file (default: REDDITPAGES_DB, "
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
    except RedditPagesError as e:
        print(f"error: {e}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
