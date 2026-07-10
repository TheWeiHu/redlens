"""Static, self-contained export of the ``serve`` dashboard.

``redlens report`` renders the coordinated-network dashboard as **one HTML
file** with no server — a shareable exposé. It reuses the *exact* ``Network``
computations ``serve`` uses: it builds the same ``Network`` object and, rather
than duplicating any analysis, iterates ``serve.ENDPOINTS`` and calls each
route's handler to pre-compute a ``SNAPSHOT`` of every payload the SPA would
fetch. That snapshot is embedded in the page and the SPA's single
``getJSON(url)`` seam resolves against it instead of the network (see the shim
injected into ``serve_assets/index.html``).

What's baked:

- every **parameterless** ``ENDPOINTS`` path (overview, accounts, pairs,
  mentions, share-of-voice, listening, suggested-coordinated, the cohort/
  seeding views, subreddits, threads) — keyed by its exact request URL,
- ``pair_evidence`` for the **top-K** entangled pairs (the matrix cells a
  reader is most likely to click),
- ``profile`` for each **labeled** account (the cohort members — not every
  ingested organic author, which would bloat the file).

Everything else (``ai-profile`` — it needs an LLM key —, deep content
pagination, evidence for un-baked pairs) resolves to a small "not captured in
static export" marker so those sections degrade gracefully instead of throwing.
"""
from __future__ import annotations

import html
import json
from pathlib import Path
from typing import Any
from urllib.parse import quote

from redlens import serve
from redlens.network import Network, build_network

# The four ``ENDPOINTS`` handlers that read a query param (``_one``); every
# other route is parameterless, so the snapshot builder skips these by identity
# and pre-bakes a bounded set of their calls by hand. Keeping this as an
# identity set (not a path list) means the snapshot can't silently drift if a
# parameterless route is added to ``ENDPOINTS`` — that route auto-snapshots.
_PARAMETERIZED = {
    serve._profile, serve._ai_profile, serve._evidence, serve._content}

# How many of the most-entangled pairs get their ``pair_evidence`` pre-baked.
TOP_PAIRS = 50

# What an un-baked request resolves to (ai-profile, deep pagination, evidence
# for a pair outside the top-K). The SPA's getJSON shim raises on ``error`` and
# every caller of an optional section catches it → a muted "unavailable" line.
_MISS = {"error": "not captured in static export"}


def _parameterless_snapshot(net: Network) -> dict[str, Any]:
    """Call every parameterless ``ENDPOINTS`` handler and key the result by the
    route's own path — the exact URL the SPA fetches for it."""
    snap: dict[str, Any] = {}
    for path, handler in serve.ENDPOINTS.items():
        if handler in _PARAMETERIZED:
            continue
        snap[path] = handler(net, {})
    return snap


# The extra chars JS ``encodeURIComponent`` leaves unescaped that Python's
# ``quote`` would percent-encode by default; matching them keeps a baked key
# byte-identical to the URL the SPA builds (else the lookup misses → ``_miss``).
_ENC_SAFE = "!'()*-._~"


def _enc(v: str) -> str:
    return quote(v, safe=_ENC_SAFE)


def _pair_key(a: str, b: str) -> str:
    """The exact URL ``openPair`` builds via ``encodeURIComponent``."""
    return f"/api/evidence?type=pair&a={_enc(a)}&b={_enc(b)}"


def _profile_key(u: str) -> str:
    return f"/api/profile?u={_enc(u)}"


def _prebaked(net: Network) -> dict[str, Any]:
    """The bounded parameterized calls: top-K pair evidence + labeled-account
    profiles, keyed by the same URL the SPA would request."""
    snap: dict[str, Any] = {}
    pairs_res = net.pairs()
    top = sorted(pairs_res.get("pairs", []),
                 key=lambda p: p["subs"] + p["threads"],
                 reverse=True)[:TOP_PAIRS]
    for p in top:
        snap[_pair_key(p["a"], p["b"])] = net.pair_evidence(p["a"], p["b"])
    # Only the labeled (cohort) accounts — the network the report is about.
    # Unlabeled DBs have no cohorts, so fall back to the matrix accounts.
    labeled = list(net.cohorts) or pairs_res.get("accounts", [])
    for u in labeled:
        snap[_profile_key(u)] = net.profile(u)
    return snap


def _serialize(snapshot: dict[str, Any]) -> str:
    """JSON for embedding in a ``<script>`` tag. ``default=str`` mirrors
    serve's JSON encoder; ``ensure_ascii=False`` keeps names readable. The
    ``<`` → ``\\u003c`` escape is the injection guard: ``json.dumps`` does not
    escape ``<``, so an account/brand name containing ``</script>`` (or any
    casing of it) would otherwise close the tag and break out of the script
    context. Escaping *every* ``<`` is the unambiguous, industry-standard
    guard — ``\\u003c`` is valid JSON/JS and parses back to a literal ``<``,
    so the embedded data is byte-for-byte unchanged."""
    return json.dumps(snapshot, default=str, ensure_ascii=False).replace(
        "<", "\\u003c")


def render_report(db: str | Path, *, brands: str | Path | None = None,
                  cohorts: str | Path | None = None, promote: bool = False,
                  title: str = "coordinated network",
                  out: str | Path = "report.html") -> Path:
    """Render the ``serve`` dashboard as a self-contained static HTML file.

    Builds the same ``Network`` ``serve`` uses, snapshots every parameterless
    endpoint plus a bounded set of parameterized calls, and injects them into
    the SPA so it renders from the embedded data with no server. Returns the
    written path.
    """
    # ``promote`` mirrors serve's flag for signature parity; the static export
    # has no separate reviewed-suggestions file, so it only toggles whether
    # promoted accounts (if any live in the cohorts file) count — build_network
    # takes a path, and there's none here, so it's a no-op on the network.
    _ = promote
    net = build_network(db, brands=Path(brands) if brands else None,
                        cohorts=Path(cohorts) if cohorts else None,
                        promote=None)
    net.overview()  # fail fast on a missing / unreadable DB

    snapshot = {**_parameterless_snapshot(net), **_prebaked(net), "_miss": _MISS}

    page = (serve.INDEX_HTML
            .replace("$TITLE", html.escape(title)))
    inject = (f'<script>const SNAPSHOT = {_serialize(snapshot)};</script>\n')
    # Inject the snapshot immediately before the app's <script> so SNAPSHOT is
    # defined by the time getJSON (and the boot IIFE) run. The shim in getJSON
    # picks it up; serve never injects this, so it keeps fetching live.
    marker = "<script>\nconst $ = s => document.querySelector(s);"
    if marker not in page:  # index.html's app-script preamble was reshaped
        raise RuntimeError(
            "expose: snapshot injection point not found in index.html "
            "(the app <script> preamble changed) — the shim seam must move")
    page = page.replace(marker, inject + marker, 1)

    out_path = Path(out)
    out_path.write_text(page, encoding="utf-8")
    return out_path
