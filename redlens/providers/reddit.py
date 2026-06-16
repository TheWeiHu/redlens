"""Reddit's official API — fresh data to top up the arctic-shift backfill.

Arctic-shift lags live Reddit by weeks. With user-supplied credentials
(:func:`redlens.config.reddit_credentials`), :func:`redlens.ingest.sync_user`
calls this module to pull the most recent posts/comments straight from Reddit.

Stdlib-only (``urllib``), mirroring :mod:`redlens.arctic`: same retry/backoff
on 429/5xx, the same descriptive User-Agent. Auth is the OAuth2
client-credentials ("application-only") flow, which can read public listings.
Reddit caps listing history at ~1000 items per endpoint — that is fine, arctic
covers the deep backfill; this is just the fresh tip.
"""
from __future__ import annotations

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Iterator
from typing import Any

from redlens import __version__, constants
from redlens.constants import (
    BACKOFF_BASE_S,
    MAX_RETRIES,
    PAGINATION_SLEEP_S,
    RETRYABLE_STATUS,
)
from redlens.errors import RedlensError

# Reddit requires a descriptive User-Agent that identifies the app; reuse
# arctic's format (it already points at the repo).
UA = f"redlens/{__version__} (+https://github.com/TheWeiHu/redlens)"

TOKEN_URL = "https://www.reddit.com/api/v1/access_token"
OAUTH_BASE = "https://oauth.reddit.com"
LISTING_LIMIT = 100


def _request(req: urllib.request.Request, *, what: str) -> dict[str, Any]:
    """Send ``req`` with arctic-style retry on 429/transient 5xx."""
    for attempt in range(MAX_RETRIES + 1):
        try:
            with urllib.request.urlopen(req, timeout=constants.HTTP_TIMEOUT_S) as r:
                data: dict[str, Any] = json.loads(r.read())
            return data
        except urllib.error.HTTPError as exc:
            if exc.code in RETRYABLE_STATUS and attempt < MAX_RETRIES:
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                wait = (
                    float(retry_after) if retry_after and retry_after.isdigit()
                    else BACKOFF_BASE_S * (2 ** attempt)
                )
                time.sleep(wait)
                continue
            raise RedlensError(f"reddit {what}: {exc}") from exc
        except Exception as exc:
            raise RedlensError(f"reddit {what}: {exc}") from exc
    raise RedlensError(f"reddit {what}: exhausted retries")


def get_token(client_id: str, client_secret: str) -> str:
    """An application-only bearer token via the client-credentials flow."""
    body = urllib.parse.urlencode({"grant_type": "client_credentials"}).encode()
    basic = base64.b64encode(f"{client_id}:{client_secret}".encode()).decode()
    req = urllib.request.Request(
        TOKEN_URL,
        data=body,
        headers={"Authorization": f"Basic {basic}", "User-Agent": UA},
        method="POST",
    )
    data = _request(req, what="token")
    token = data.get("access_token")
    if not token:
        raise RedlensError("reddit token: no access_token in response")
    return str(token)


def _iter_listing(token: str, path: str) -> Iterator[dict[str, Any]]:
    """Yield the ``data`` of each child in a paginated listing, newest first.

    Walks the ``after`` fullname cursor Reddit returns until it runs out
    (or hits Reddit's ~1000-item history cap)."""
    after: str | None = None
    while True:
        qs = urllib.parse.urlencode(
            {k: v for k, v in
             {"limit": LISTING_LIMIT, "after": after, "raw_json": 1}.items()
             if v is not None}
        )
        req = urllib.request.Request(
            f"{OAUTH_BASE}{path}?{qs}",
            headers={"Authorization": f"bearer {token}", "User-Agent": UA},
        )
        listing = _request(req, what=path).get("data") or {}
        children = listing.get("children") or []
        if not children:
            return
        for child in children:
            yield child.get("data") or {}
        after = listing.get("after")
        if not after:
            return
        time.sleep(PAGINATION_SLEEP_S)


def iter_submitted(token: str, username: str) -> Iterator[dict[str, Any]]:
    return _iter_listing(token, f"/user/{username}/submitted")


def iter_comments(token: str, username: str) -> Iterator[dict[str, Any]]:
    return _iter_listing(token, f"/user/{username}/comments")
