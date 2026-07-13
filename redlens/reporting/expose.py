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
import re
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


def _pseudonym_map(snapshot: dict[str, Any]) -> dict[str, str]:
    """Map every real username to a stable, neutral ``user-NN`` pseudonym.

    The universe is every account in the ``/api/accounts`` payload (which lists
    *every* post/comment author, labeled or organic) plus every cohort-map key —
    the complete set of names that can surface in any payload. Sorting by the
    real username makes the numbering reproducible run-to-run, and the width is
    zero-padded to the account count so labels sort naturally (``user-01`` …).
    """
    names: set[str] = {
        a["username"] for a in
        snapshot.get("/api/accounts", {}).get("accounts", [])
        if isinstance(a, dict) and isinstance(a.get("username"), str)}
    names |= set(snapshot.get("/api/pairs", {}).get("cohorts", {}))
    ordered = sorted(names)
    width = max(2, len(str(len(ordered))))
    return {u: f"user-{i:0{width}d}" for i, u in enumerate(ordered, start=1)}


def _anon_value(v: Any, mapping: dict[str, str],
                pat: re.Pattern[str] | None) -> Any:
    """Recursively pseudonymize a snapshot value: exact-match a username in a
    string, replace username-valued dict *keys* and *values*, and rewrite any
    username embedded in free text (titles, snippets) via ``pat``."""
    if isinstance(v, dict):
        return {mapping.get(k, k): _anon_value(val, mapping, pat)
                for k, val in v.items()}
    if isinstance(v, list):
        return [_anon_value(x, mapping, pat) for x in v]
    if isinstance(v, str):
        if v in mapping:                       # the value *is* a username
            return mapping[v]
        if pat is not None:                    # a username sits inside free text
            return pat.sub(lambda m: mapping[m.group(0)], v)
    return v


def _anonymize(snapshot: dict[str, Any], mapping: dict[str, str]
               ) -> dict[str, Any]:
    """Return a copy of ``snapshot`` with every real username replaced by its
    pseudonym — in payload keys, values, lists, free text, and the top-level
    request-URL keys (``/api/profile?u=…``, ``/api/evidence?…a=…&b=…``), whose
    usernames are URL-encoded so we re-encode the pseudonym to match."""
    pat = (re.compile("|".join(re.escape(u) for u in
                      sorted(mapping, key=len, reverse=True)))
           if mapping else None)
    out: dict[str, Any] = {}
    for key, val in snapshot.items():
        out[_anon_key(key, mapping)] = _anon_value(val, mapping, pat)
    return out


def _anon_key(key: str, mapping: dict[str, str]) -> str:
    """Rewrite a top-level snapshot key (a request URL). Only the profile and
    pair-evidence URLs carry usernames — in ``u``/``a``/``b`` query params that
    were built with ``_enc``, so we swap the encoded real name for the encoded
    pseudonym. Any other key is returned unchanged."""
    def _sub(m: re.Match[str]) -> str:
        param, enc = m.group(1), m.group(2)
        # Reverse ``_enc`` to recover the raw name, map it, re-encode.
        for real, pseudo in mapping.items():
            if _enc(real) == enc:
                return f"{param}={_enc(pseudo)}"
        return m.group(0)
    return re.sub(r"([?&](?:u|a|b))=([^&]*)", _sub, key)


def render_report(db: str | Path, *, brands: str | Path | None = None,
                  cohorts: str | Path | None = None, promote: bool = False,
                  title: str = "coordinated network", anon: bool = False,
                  out: str | Path = "report.html") -> Path:
    """Render the ``serve`` dashboard as a self-contained static HTML file.

    Builds the same ``Network`` ``serve`` uses, snapshots every parameterless
    endpoint plus a bounded set of parameterized calls, and injects them into
    the SPA so it renders from the embedded data with no server. Returns the
    written path.

    With ``anon=True`` every Reddit account username is consistently replaced by
    a stable ``user-NN`` pseudonym across every payload (keys, values, free text,
    and the request-URL keys) so the report is shareable without naming people.
    Brands and the title are left untouched.
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
    if anon:
        snapshot = _anonymize(snapshot, _pseudonym_map(snapshot))

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
