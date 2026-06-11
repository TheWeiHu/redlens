"""Pull every Reddit post that mentions "dua lipa" in the last year.

Arctic's full-text search has no global mode — ``query``/``title``/``selftext``
all require an ``author`` or ``subreddit`` scope. So "all of Reddit" is cast as
the widest practical net: a broad, easily-extended list of subreddits where Dua
Lipa plausibly surfaces (her fan subs, pop/music, celeb gossip, charts,
festivals, and big general subs). We run a full-text ``query=dua lipa`` per
subreddit over the trailing year, dedupe by post id, and upsert ``Post`` rows.

The destination is a sibling ``data/dua_lipa.db`` on the project's exact ``Post``
schema, so the existing tooling reads it directly::

    python scripts/explore.py --db ../data/dua_lipa.db
    redditpages analytics <user> --db ../data/dua_lipa.db

Extend ``SUBREDDITS`` to widen coverage; re-running is idempotent (upsert).
"""

from __future__ import annotations

import argparse
import time

from sqlmodel import Session

from redditpages import arctic
from redditpages.db import connect, data_db, init_schema, upsert
from redditpages.models import Post, Subreddit

# The net. Not exhaustive (the API forbids that) but broad — fan subs, pop/music,
# celebrity & gossip, charts/awards, festivals, streaming, and large generals.
# Non-existent or empty subs simply return nothing and cost one request.
SUBREDDITS = [
    # Dua Lipa fan subs
    "dualipa", "DUA_Lipa", "DuaLipa",
    # Pop / music
    "popheads", "Music", "music", "LetsTalkMusic", "listentothis",
    "electronicmusic", "popmusic", "femalepopdiscussion", "PopGirlAriana",
    "MusicCharts", "charts", "billboard", "GurleyPop", "TheGreatPopGirlBattle",
    # Celebrity / gossip / pop culture
    "popculturechat", "Fauxmoi", "popculture", "entertainment", "celebrities",
    "popheadscirclejerk", "Deuxmoi", "BravoRealHousewives",
    # Awards / events / festivals
    "Grammys", "Coachella", "festivals", "musicfestivals", "Glastonbury_Festival",
    # Streaming / discovery
    "Spotify", "spotify", "AppleMusic",
    # Regional / general large
    "unitedkingdom", "AskReddit", "entertainment", "television", "Music",
    "popping", "fragrance",
]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--db", default=data_db("dua_lipa.db"),
                   help="destination SQLite DB (default: ../data/dua_lipa.db)")
    p.add_argument("--query", default="dua lipa")
    p.add_argument("--days", type=int, default=365,
                   help="trailing window in days (default: 365)")
    args = p.parse_args()

    now = int(time.time())
    after = now - args.days * 86400
    seen: set[str] = set()

    # De-dupe the subreddit list (a couple repeat above by design of editing).
    subs = list(dict.fromkeys(SUBREDDITS))

    engine = connect(args.db)
    init_schema(engine)

    start = time.monotonic()
    grand_total = 0
    with Session(engine) as s:
        for i, sub in enumerate(subs, 1):
            t0 = time.monotonic()
            posts: list[Post] = []
            try:
                for raw in arctic.iter_subreddit_query(
                    sub, args.query, after=after, before=now
                ):
                    pid = raw.get("id")
                    if not pid or pid in seen:
                        continue
                    seen.add(pid)
                    posts.append(Post.from_arctic(raw))
            except Exception as exc:
                print(f"[{i:2d}/{len(subs)}] r/{sub:28s} ERROR: {exc}", flush=True)
                continue

            if posts:
                upsert(s, posts)
                upsert(s, [Subreddit(name=sub)])
                s.commit()
            grand_total += len(posts)
            print(f"[{i:2d}/{len(subs)}] r/{sub:28s} "
                  f"{len(posts):>4d} new  (running {grand_total:>5d})  "
                  f"({time.monotonic() - t0:.1f}s)", flush=True)

    print(f"\ndone in {time.monotonic() - start:.1f}s. "
          f"{grand_total} unique posts mentioning {args.query!r} "
          f"in the last {args.days} days -> {args.db}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
