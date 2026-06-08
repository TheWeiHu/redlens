from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from redditpages.errors import RedditPagesError

BASE = "https://arctic-shift.photon-reddit.com"
UA = "redditpages/0.1"
PAGINATION_SLEEP_S = 0.25
# Hard cap on items per stream (posts or comments). Override at runtime by
# setting ``arctic.MAX_ITEMS_PER_STREAM = N``. Default None = unbounded.
MAX_ITEMS_PER_STREAM: int | None = None


# Arctic rate-limits bursts with HTTP 429. Retry those (and transient 5xx) with
# exponential backoff, honoring a Retry-After header when present.
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.0


def _get(path: str, **params: Any) -> dict[str, Any]:
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
    url = f"{BASE}{path}?{qs}" if qs else f"{BASE}{path}"
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data: dict[str, Any] = json.loads(r.read())
            return data
        except urllib.error.HTTPError as exc:
            if exc.code in (429, 500, 502, 503, 504) and attempt < MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else BACKOFF_BASE_S * (2 ** attempt)
                )
                time.sleep(wait)
                continue
            raise RedditPagesError(f"arctic GET {url}: {exc}") from exc
        except Exception as exc:
            raise RedditPagesError(f"arctic GET {url}: {exc}") from exc
    raise RedditPagesError(f"arctic GET {url}: exhausted retries")


def fetch_user_meta(username: str) -> dict[str, Any] | None:
    arr = _get("/api/users/search", author=username, limit=1).get("data") or []
    return arr[0] if arr else None


def _iter_kind(kind: str, username: str) -> Iterator[dict[str, Any]]:
    before: int | None = None
    yielded = 0
    while True:
        batch = (
            _get(f"/api/{kind}/search",
                 author=username, limit="auto", sort="desc", before=before)
            .get("data") or []
        )
        if not batch:
            return
        for item in batch:
            yield item
            yielded += 1
            if MAX_ITEMS_PER_STREAM is not None and yielded >= MAX_ITEMS_PER_STREAM:
                return
        if len(batch) < 50:
            return
        oldest = min(int(b.get("created_utc") or 0) for b in batch)
        if not oldest or oldest == before:
            return
        before = oldest
        time.sleep(PAGINATION_SLEEP_S)


def iter_subreddit_query(
    subreddit: str,
    query: str,
    after: int | None = None,
    before: int | None = None,
) -> Iterator[dict[str, Any]]:
    """Yield posts in ``subreddit`` whose title or body match ``query``.

    Arctic's full-text params (``query``/``title``/``selftext``) only work when
    scoped to an ``author`` or ``subreddit`` — there is no global text search —
    so callers fan this out across a set of subreddits. ``after``/``before`` are
    epoch seconds bounding ``created_utc``; pagination walks backwards in time
    via the ``before`` cursor, mirroring :func:`_iter_kind`.
    """
    cursor = before
    yielded = 0
    while True:
        # arctic rejects limit="auto" alongside a full-text query; 100 is the max.
        batch = (
            _get("/api/posts/search",
                 subreddit=subreddit, query=query, limit=100,
                 sort="desc", after=after, before=cursor)
            .get("data") or []
        )
        if not batch:
            return
        for item in batch:
            yield item
            yielded += 1
            if MAX_ITEMS_PER_STREAM is not None and yielded >= MAX_ITEMS_PER_STREAM:
                return
        oldest = min(int(b.get("created_utc") or 0) for b in batch)
        if not oldest or oldest == cursor:
            return
        cursor = oldest
        time.sleep(PAGINATION_SLEEP_S)


def iter_posts(username: str) -> Iterator[dict[str, Any]]:
    return _iter_kind("posts", username)


def iter_comments(username: str) -> Iterator[dict[str, Any]]:
    return _iter_kind("comments", username)
