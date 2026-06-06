"""Bridge: read posts/comments from the redditpages SQLite DB and emit the
JSON payload shape that the legacy ``render.py`` (in the repo root)
consumes.

This keeps ``redditpages`` itself focused on the raw archive + the one tidy
``UserAnalytics`` model, while still letting us produce the rich HTML
report by reusing what's already polished.

Usage:
    python scripts/build_payload.py jav_city --db jav_city.db --out jav_city.json
"""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import Counter, defaultdict
from datetime import UTC, datetime
from urllib.parse import urlparse

from sqlmodel import Session, select

from redditpages.db import connect, data_db
from redditpages.models import Comment, Post, User

STOP = set("""
the and to a of in i is it that for on was you he but with as his this they at
not have are from her had she be we one all by will or so what about my their
there can if more when an out so them up some when no your me how do has just
were i'm i've you're they're it's that's don't won't would should could been
who which than then because while where why these those into over under after
before about much many also too very still even another like other any our
""".split())


def utc(ts: int) -> datetime:
    return datetime.fromtimestamp(ts, tz=UTC)


def fmt_date(ts: int | None) -> str:
    return utc(ts).strftime("%b %d, %Y") if ts else ""


def _serialize_post_brief(p: Post) -> dict:
    return {
        "title": p.title or "",
        "body": (p.selftext or "")[:300],
        "score": p.score,
        "id": p.post_id,
    }


def _serialize_comment_brief(c: Comment) -> dict:
    return {
        "body": (c.body or "")[:300],
        "score": c.score,
        "id": c.comment_id,
        "post_id": c.link_id,
    }


def _build_best_worst(posts: list[Post], comments: list[Comment]) -> dict:
    def submission(p: Post | None) -> dict | None:
        if not p:
            return None
        return {
            "title": p.title or "",
            "body": (p.selftext or "")[:600],
            "score": p.score,
            "subreddit": p.subreddit_name,
            "created_utc": p.created_utc,
            "id": p.post_id,
            "post_id": p.post_id,
        }

    def comment(c: Comment | None) -> dict | None:
        if not c:
            return None
        return {
            "body": (c.body or "")[:600],
            "score": c.score,
            "subreddit": c.subreddit_name,
            "created_utc": c.created_utc,
            "id": c.comment_id,
            "post_id": c.link_id,
        }

    return {
        "best_submission": submission(max(posts, key=lambda p: p.score, default=None)),
        "worst_submission": submission(min(posts, key=lambda p: p.score, default=None)),
        "best_comment": comment(max(comments, key=lambda c: c.score, default=None)),
        "worst_comment": comment(min(comments, key=lambda c: c.score, default=None)),
    }


def _build_activity(posts: list[Post], comments: list[Comment]) -> dict:
    by_day: dict[str, int] = defaultdict(int)
    by_weekday = [0] * 7
    by_hour = [0] * 24
    by_weekday_sub: dict[str, list[int]] = defaultdict(lambda: [0] * 7)
    by_hour_sub: dict[str, list[int]] = defaultdict(lambda: [0] * 24)
    for item in posts + comments:
        d = utc(item.created_utc)
        by_day[d.strftime("%Y-%m-%d")] += 1
        by_weekday[d.weekday()] += 1
        by_hour[d.hour] += 1
        sub = item.subreddit_name
        by_weekday_sub[sub][d.weekday()] += 1
        by_hour_sub[sub][d.hour] += 1
    return {
        "by_day": dict(by_day),
        "by_weekday": by_weekday,
        "by_hour": by_hour,
        "by_weekday_sub": dict(by_weekday_sub),
        "by_hour_sub": dict(by_hour_sub),
    }


def _build_subreddits(posts: list[Post], comments: list[Comment]) -> list[dict]:
    sub_posts: dict[str, list[Post]] = defaultdict(list)
    sub_comments: dict[str, list[Comment]] = defaultdict(list)
    for p in posts:
        sub_posts[p.subreddit_name].append(p)
    for c in comments:
        sub_comments[c.subreddit_name].append(c)
    out = []
    for sub in set(sub_posts) | set(sub_comments):
        ps = sub_posts[sub]
        cs = sub_comments[sub]
        karma = sum(p.score for p in ps) + sum(c.score for c in cs)
        out.append({
            "name": sub,
            "posts": len(ps),
            "comments": len(cs),
            "karma": karma,
            "total": len(ps) + len(cs),
            "avg_upvote_ratio": None,
            "top_posts": [_serialize_post_brief(p)
                          for p in sorted(ps, key=lambda x: x.score, reverse=True)[:3]],
            "top_comments": [_serialize_comment_brief(c)
                             for c in sorted(cs, key=lambda x: x.score, reverse=True)[:3]],
        })
    out.sort(key=lambda s: s["total"], reverse=True)
    return out


_WORD_RE = re.compile(r"[a-z][a-z']{2,}")


def _build_words(posts: list[Post], comments: list[Comment]) -> tuple[dict, dict]:
    # Unlike _build_domains, we do NOT dedupe per source: saying "data" ten
    # times in one comment IS ten uses of "data" — that intensity is the
    # signal the cloud wants. The `docs` set still tracks distinct source
    # items so the payload can show "X uses across Y posts" if rendered.
    counts: Counter[str] = Counter()
    docs: dict[str, set[tuple[str, int]]] = defaultdict(set)
    for i, p in enumerate(posts):
        text = f"{p.title or ''} {p.selftext or ''}".lower()
        for w in _WORD_RE.findall(text):
            if w not in STOP:
                counts[w] += 1
                docs[w].add(("p", i))
    for i, c in enumerate(comments):
        text = (c.body or "").lower()
        for w in _WORD_RE.findall(text):
            if w not in STOP:
                counts[w] += 1
                docs[w].add(("c", i))

    total = sum(counts.values())
    unique = len(counts)
    top = counts.most_common(250)
    words = {w: [n, len(docs[w])] for w, n in top}
    corpus = {
        "total_words": total,
        "unique_words": unique,
        "unique_pct": round((unique / max(total, 1)) * 100, 2),
        "hours_typing": round(total / 40 / 60, 2),
        "karma_per_word": 0.0,  # filled by caller
    }
    return words, corpus


_URL_RE = re.compile(r'https?://[^\s\)\]\>"]+', re.IGNORECASE)
# Substring-matched against the host. ``redd.it`` and ``redditmedia.com``
# are Reddit's image/video/asset CDNs — a plain "reddit.com" check would
# miss them and they'd show up as "outlinks" (which they aren't).
_REDDIT_HOSTS = ("reddit.com", "redd.it", "redditmedia.com")


def _host_of(u: str) -> str | None:
    try:
        h = urlparse(u).netloc.lower().removeprefix("www.")
    except Exception:
        return None
    if not h or any(s in h for s in _REDDIT_HOSTS):
        return None
    return h


def _build_domains(posts: list[Post], comments: list[Comment]) -> dict[str, int]:
    # URLs live in three places: Post.url (the structured link field),
    # Post.selftext (text-post bodies), and Comment.body (the biggest source
    # for most users — citations and replies). Earlier versions only read
    # Post.url, which hid 60%+ of outlinks for any user who shares links
    # mostly via comments or selftext markdown.
    #
    # Dedup per source item: Reddit posts very commonly include the same
    # URL twice in selftext — once as the bare URL ("Source: https://..")
    # and once inside ``[label](url)`` markdown. Both match the regex.
    # Without this set, every such post double-counts its citation.
    # (Unlike words, where repetition IS the signal — see _build_words.)
    domains: Counter[str] = Counter()

    def _add_from_text(text: str | None, seen: set[str]) -> None:
        if not text:
            return
        for u in _URL_RE.findall(text):
            if u in seen:
                continue
            seen.add(u)
            if (h := _host_of(u)):
                domains[h] += 1

    for p in posts:
        seen: set[str] = set()
        if p.url and (h := _host_of(p.url)):
            domains[h] += 1
            seen.add(p.url)
        _add_from_text(p.selftext, seen)

    for c in comments:
        _add_from_text(c.body, set())

    return dict(domains.most_common(50))


def _build_timeline(posts: list[Post], comments: list[Comment],
                    cap: int | None = None) -> list[dict]:
    # No cap by default — render.py's karma-over-time chart reads months
    # from this timeline, so a 500-item cap was silently truncating the
    # chart to ~6 years for power-user accounts. The browser embeds this
    # JSON inline so the cost is just file size (~200 bytes/event).
    items: list[dict] = []
    for p in posts:
        items.append({
            "ts": p.created_utc, "type": "post", "sub": p.subreddit_name,
            "score": p.score, "id": p.post_id, "title": p.title or "",
        })
    for c in comments:
        items.append({
            "ts": c.created_utc, "type": "comment", "sub": c.subreddit_name,
            "score": c.score, "id": c.comment_id,
            "body": (c.body or "")[:300], "post_id": c.link_id,
        })
    items.sort(key=lambda e: e["ts"], reverse=True)
    return items if cap is None else items[:cap]


def _longest_gap(timestamps: list[int]) -> tuple[int, int, int]:
    timestamps = sorted(timestamps)
    days = 0
    frm = to = 0
    for a, b in zip(timestamps, timestamps[1:]):
        gap = (b - a) // 86400
        if gap > days:
            days, frm, to = gap, a, b
    return days, frm, to


# Friendly city anchors per UTC offset. Offsets are wall-clock without DST
# awareness; for offsets that span DST (e.g. UTC-4 is US East in summer and
# Atlantic in winter), we name both candidates.
_TZ_ANCHORS: dict[int, str] = {
    -12: "Baker Island",
    -11: "American Samoa",
    -10: "Honolulu",
    -9:  "Anchorage",
    -8:  "Los Angeles · Vancouver (PST)",
    -7:  "Denver · Phoenix · Los Angeles in summer (MST/PDT)",
    -6:  "Chicago · Mexico City · Denver in summer (CST/MDT)",
    -5:  "New York · Toronto · Chicago in summer (EST/CDT)",
    -4:  "Halifax · Caracas · New York in summer (AST/EDT)",
    -3:  "Buenos Aires · São Paulo",
    -2:  "Mid-Atlantic",
    -1:  "Azores · Cape Verde",
    0:   "London · Lisbon · Accra (GMT)",
    1:   "Berlin · Paris · Rome · Lagos (CET)",
    2:   "Helsinki · Cairo · Johannesburg",
    3:   "Moscow · Istanbul · Nairobi",
    4:   "Dubai · Baku · Tbilisi",
    5:   "Karachi · Tashkent",
    6:   "Dhaka · Almaty",
    7:   "Bangkok · Jakarta · Hanoi",
    8:   "Beijing · Singapore · Perth · Manila",
    9:   "Tokyo · Seoul",
    10:  "Sydney · Melbourne · Brisbane",
    11:  "Nouméa · Magadan",
    12:  "Auckland · Fiji",
}


def _timezone_guess(by_hour: list[int]) -> dict:
    if not any(by_hour):
        return {"timezone": "UTC", "description": "no activity to infer timezone"}
    # Lowest 6-hour rolling window ≈ sleep window
    best_i, best_sum = 0, sum(by_hour[:6])
    for i in range(24):
        s = sum(by_hour[(i + k) % 24] for k in range(6))
        if s < best_sum:
            best_sum, best_i = s, i
    # Sleep center ≈ best_i+3; assume that's local ~3am → offset = 3 - sleep_mid_utc
    sleep_mid = (best_i + 3) % 24
    offset = (3 - sleep_mid) % 24
    if offset > 12:
        offset -= 24
    anchor = _TZ_ANCHORS.get(offset, "?")
    return {
        "timezone": f"UTC{offset:+d}",
        "description": (
            f"Quietest window {best_i:02d}:00–{(best_i+6)%24:02d}:00 UTC "
            f"(likely sleep). UTC{offset:+d} ≈ {anchor}."
        ),
    }


def build_payload(db_path: str, username: str) -> dict:
    engine = connect(db_path)
    with Session(engine) as s:
        user = s.exec(
            select(User).where(User.username == username)
        ).first()
        if user is None:
            raise SystemExit(f"u/{username} not in {db_path} — sync first")
        posts = list(s.exec(select(Post).where(Post.author_username == user.username)))
        comments = list(s.exec(select(Comment).where(Comment.author_username == user.username)))

    post_karma = sum(p.score for p in posts)
    comment_karma = sum(c.score for c in comments)
    timestamps = [p.created_utc for p in posts] + [c.created_utc for c in comments]
    first_ts = min(timestamps, default=None)
    last_ts = max(timestamps, default=None)
    gap_days, gap_from, gap_to = _longest_gap(timestamps)

    activity = _build_activity(posts, comments)
    subreddits = _build_subreddits(posts, comments)
    best_worst = _build_best_worst(posts, comments)
    words, corpus = _build_words(posts, comments)
    corpus["karma_per_word"] = round(
        (post_karma + comment_karma) / max(corpus["total_words"], 1), 2
    )
    domains = _build_domains(posts, comments)
    timeline = _build_timeline(posts, comments)
    tz = _timezone_guess(activity["by_hour"])

    nsfw_post_count = sum(1 for p in posts if getattr(p, "over_18", False))
    nsfw = {
        "post_count": nsfw_post_count,
        "post_pct": round((nsfw_post_count / max(len(posts), 1)) * 100, 1),
        "account_flagged": nsfw_post_count > 0,
    }

    now = int(time.time())
    return {
        "state": "done",
        "result": {
            "username": user.username,
            "generated_at": now,
            "generated_ago": "just now",
            "fetched_at": now,
            "nsfw": nsfw,
            "profile": {
                "display_name": "",
                "active_since": fmt_date(first_ts),
                "active_since_ts": first_ts or 0,
                "account_created": fmt_date(first_ts),
                "account_created_ts": first_ts or 0,
                "longest_gap_days": gap_days,
                "longest_gap_from": fmt_date(gap_from),
                "longest_gap_from_ts": gap_from,
                "longest_gap_to": fmt_date(gap_to),
                "longest_gap_to_ts": gap_to,
            },
            "karma": {
                "submission_count": len(posts),
                "submission_karma": post_karma,
                "submission_avg": round(post_karma / max(len(posts), 1), 1),
                "comment_count": len(comments),
                "comment_karma": comment_karma,
                "comment_avg": round(comment_karma / max(len(comments), 1), 1),
            },
            "corpus": corpus,
            "timezone_guess": tz,
            "profile_summary": [],
            "best_worst": best_worst,
            "subreddits": subreddits,
            "subreddit_summaries": {},
            "activity": activity,
            "words": words,
            "domains": domains,
            "timeline": timeline,
            "hidden_subreddits": {},
            "visible_subreddits": {},
            "all_history_hidden": False,
            "post_listing_capped": False,
            "comment_listing_capped": False,
        },
    }


def main() -> int:
    p = argparse.ArgumentParser()
    p.add_argument("username")
    p.add_argument("--db", default=data_db("important.db"))
    p.add_argument("--out", default=None)
    args = p.parse_args()
    payload = build_payload(args.db, args.username)
    out = args.out or f"{args.username}.json"
    with open(out, "w") as f:
        json.dump(payload, f, indent=2)
    print(f"wrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
