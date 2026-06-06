"""Load top-100-subreddit moderator lists into the SubredditModerator table.

One row per (subreddit, moderator). `as_of_date` is the Internet Archive
snapshot date the row was accurate on — most lists are archival because Reddit
gated logged-out moderator access in 2021.

Usage:
    python scripts/load_moderators.py --db important.db --json /tmp/mods_result.json
"""
from __future__ import annotations

import argparse
import json
from datetime import datetime, timezone

from redditpages.db import connect, data_db, init_schema, session, upsert
from redditpages.models import SubredditModerator

# Subs whose capped front-page sidebar was unioned across snapshots.
UNION_SUBS = {"travel", "tattoos", "CryptoCurrency", "stocks", "AnimalsBeingDerps"}


def ts_to_epoch(ts: str | None) -> int | None:
    if not ts:
        return None
    try:
        dt = datetime.strptime(ts[:14], "%Y%m%d%H%M%S").replace(tzinfo=timezone.utc)
        return int(dt.timestamp())
    except ValueError:
        return None


def consolidate(sub: str, rec: dict):
    """Return (moderators, total_indicated, list_complete, source)."""
    mods = rec.get("moderators") or []
    if sub in UNION_SUBS and rec.get("moderators_union"):
        mods = rec["moderators_union"]
    total = rec.get("max_total_indicated") or rec.get("total_mods") or len(mods)
    complete = len(mods) >= (total or len(mods))
    if rec.get("source"):
        source = "front-page sidebar (union)" if sub in UNION_SUBS else "front-page sidebar"
    else:
        source = "about-page" if mods else None
    return mods, total, complete, source


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--db", default=data_db("important.db"))
    ap.add_argument("--json", default="/tmp/mods_result.json")
    args = ap.parse_args()

    data = json.load(open(args.json))
    engine = connect(args.db)
    init_schema(engine)

    rows: list[SubredditModerator] = []
    subs_with_data = 0
    for sub, rec in data.items():
        mods, total, complete, source = consolidate(sub, rec)
        if not mods:
            continue  # e.g. r/ChatGPT — postdates the gate, no public archive
        subs_with_data += 1
        ts = rec.get("snapshot")
        for i, mod in enumerate(mods, start=1):
            rows.append(SubredditModerator(
                subreddit_name=sub,
                moderator_username=mod,
                rank=i,
                as_of_date=rec.get("snapshot_date"),
                as_of_utc=ts_to_epoch(ts),
                snapshot_timestamp=ts,
                source=source,
                list_complete=complete,
            ))

    total_rows = 0
    with session(engine) as s:
        for j in range(0, len(rows), 200):  # chunk to stay under SQLite param limit
            total_rows += upsert(s, rows[j:j + 200])
        s.commit()

    print(f"loaded {total_rows} moderator rows across {subs_with_data} subreddits "
          f"into {args.db}")


if __name__ == "__main__":
    main()
