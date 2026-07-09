"""Local listening-report server — the coordinated-network view.

The first slice of the paid listening report (see ``DESIGN.md``). It serves a
localhost dashboard over an existing redlens SQLite file, framed as a
*coordinated network*: every account in the DB is treated as one cohort and the
report surfaces the deterministic, keyless coordination signals between them —

- the **network matrix**: an account × account heatmap of pairwise co-activity
  (shared subreddits + co-commented threads), darker = more entangled,
- who the accounts are and how much each posts/comments,
- the **brand mentions** matrix: a curated roster (``brands.csv`` next to the
  DB, or ``--brands PATH``) counted exactly — case-insensitive, whole-word —
  with mined proper names as the keyless fallback when no roster exists,
- the **subreddit footprint** they share (subs ≥2 accounts are active in),
  drawn the same way,
- the **threads they co-occur in** (``link_id`` touched by ≥2 accounts) — the
  strongest cheap co-activity signal.

Every matrix cell is **clickable**: the drawer opens with the exact
posts/comments (or shared subs + threads, for a heatmap pair) behind that
cell, and any account drills into its raw history.

With **cohort labels** (``cohorts.csv`` next to the DB, or ``--cohorts PATH``:
``account, cohort`` per line) the matrices group accounts by cohort with
separators — the coordinated block reads as a block — and every account
carries its cohort chip.

    redlens serve                          # over the default DB
    redlens --db mydata.db serve         # dogfood on the mydata network
    redlens serve --brands brands.csv --cohorts cohorts.csv --no-browser

The page follows the redlens report style (light, one ``constants.ACCENT``
red). The database is opened **read-only**; nothing here can mutate data and
no LLM key is required for any of the above. With a key configured
(``redlens setup``), each profile view can additionally run an on-demand
**AI profile** — a cheap-model persona + promotional-behavior read + a
``coordinated?`` verdict, grounded in the sampled content and the
deterministic signals. Brand share-of-voice and view-time NL-plots are later
slices.
"""
from __future__ import annotations

import html
import json
import sys
import threading
import webbrowser
from collections.abc import Callable
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from redlens import config, constants, llm
from redlens.network import Network, build_network, load_brands, load_cohorts

# ``Network`` + the roster loaders are re-exported so their old import paths
# (``from redlens.serve import Network``) keep working now that they live in
# ``network``. ``config``/``llm`` are held as module attributes so the
# AI-profile tests can monkeypatch ``serve.config`` / ``serve.llm``, even
# though serve's own code no longer calls them directly.
__all__ = ["Network", "config", "llm", "load_brands", "load_cohorts", "serve"]


# --------------------------------------------------------------------------- #
# HTTP handler                                                                 #
# --------------------------------------------------------------------------- #

# --------------------------------------------------------------------------- #
# Route table                                                                  #
#                                                                              #
# Every ``/api/*`` path maps to a handler ``(net, query) -> payload`` in the   #
# ``ENDPOINTS`` dict below; ``Handler.do_GET`` parses the URL, looks the path  #
# up, calls the handler, and JSON-serializes the result. Parameterized routes  #
# read from ``query`` (parsed ``parse_qs`` dict) via ``_one``; the rest ignore #
# it, so a future static export can snapshot every parameterless entry by      #
# iterating ``ENDPOINTS``. A handler returning a ``_Coded`` overrides the 200  #
# status (used for the "unknown evidence type" 400, whose body differs from    #
# the generic exception 400).                                                  #
# --------------------------------------------------------------------------- #

Query = dict[str, list[str]]


class _Coded:
    """A payload paired with a non-200 status a handler wants to force."""

    def __init__(self, payload: Any, code: int) -> None:
        self.payload = payload
        self.code = code


def _one(q: Query, k: str, d: str = "") -> str:
    return q.get(k, [d])[0]


def _overview(net: Network, q: Query) -> Any:
    return {"db": net.path, **net.overview()}


def _accounts(net: Network, q: Query) -> Any:
    return {"accounts": net.accounts()}


def _profile(net: Network, q: Query) -> Any:
    return net.profile(_one(q, "u"))


def _ai_profile(net: Network, q: Query) -> Any:
    return net.ai_profile(_one(q, "u"))


def _evidence(net: Network, q: Query) -> Any:
    kind = _one(q, "type")
    if kind == "pair":
        return net.pair_evidence(_one(q, "a"), _one(q, "b"))
    if kind == "sub":
        return net.account_sub_items(_one(q, "u"), _one(q, "sub"))
    if kind == "thread":
        return net.account_thread_items(_one(q, "u"), _one(q, "link"))
    if kind == "mention":
        return net.account_term_items(_one(q, "u"), _one(q, "term"))
    return _Coded({"error": "unknown evidence type"}, 400)


def _content(net: Network, q: Query) -> Any:
    return net.content(
        _one(q, "u"),
        _one(q, "kind", "posts"),
        limit=int(_one(q, "limit", "50") or 50),
        offset=int(_one(q, "offset", "0") or 0),
    )


ENDPOINTS: dict[str, Callable[[Network, Query], Any]] = {
    "/api/overview": _overview,
    "/api/accounts": _accounts,
    "/api/pairs": lambda net, q: net.pairs(),
    "/api/mentions": lambda net, q: net.mentions(),
    "/api/share-of-voice": lambda net, q: net.share_of_voice(),
    "/api/listening": lambda net, q: net.listening(),
    "/api/suggested-coordinated": lambda net, q: net.suggested_coordinated(),
    "/api/cohort-comparison": lambda net, q: net.cohort_comparison(),
    "/api/cohort-timeline": lambda net, q: net.cohort_timeline(),
    "/api/cohort-bridges": lambda net, q: net.cohort_bridges(),
    "/api/cohort-domains": lambda net, q: net.domain_catalogue(),
    "/api/seeding-waves": lambda net, q: net.seeding_waves(),
    "/api/coordination-raster": lambda net, q: net.coordination_raster(),
    "/api/profile": _profile,
    "/api/ai-profile": _ai_profile,
    "/api/evidence": _evidence,
    "/api/subreddits": lambda net, q: net.subreddits(),
    "/api/threads": lambda net, q: net.threads(),
    "/api/content": _content,
}


class Handler(BaseHTTPRequestHandler):
    net: Network  # injected on the server
    page_html: str  # the index HTML with the title baked in, injected per-serve

    def log_message(self, format: str, *args: Any) -> None:  # quiet
        pass

    def _send(self, code: int, body: bytes, ctype: str) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, obj: Any, code: int = 200) -> None:
        self._send(code, json.dumps(obj, default=str).encode(), "application/json")

    def do_GET(self) -> None:
        u = urlparse(self.path)
        if u.path == "/":
            self._send(200, self.page_html.encode(), "text/html; charset=utf-8")
            return
        handler = ENDPOINTS.get(u.path)
        if handler is None:
            self._json({"error": "not found"}, 404)
            return
        try:
            result = handler(self.net, parse_qs(u.query))
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)
            return
        if isinstance(result, _Coded):
            self._json(result.payload, result.code)
        else:
            self._json(result)


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def _sidecar(db: str | Path, explicit: str | Path | None,
             default_name: str) -> Path | None:
    """Resolve an optional sidecar file (brand roster, cohort labels): an
    explicit path must exist; otherwise the default next to the DB is picked
    up automatically when present. Returns None for "no file"."""
    if explicit:
        p = Path(explicit)
        if not p.is_file():
            raise FileNotFoundError(p)
        return p
    p = Path(db).resolve().parent / default_name
    return p if p.is_file() else None


def serve(db: str | Path, *, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = True, brands: str | Path | None = None,
          cohorts: str | Path | None = None,
          promote: str | Path | None = None,
          title: str = "coordinated network") -> int:
    try:
        brands_path = _sidecar(db, brands, "brands.csv")
        cohorts_path = _sidecar(db, cohorts, "cohorts.csv")
        promote_path = Path(promote) if promote else None
        if promote_path and not promote_path.is_file():
            raise FileNotFoundError(promote_path)
    except FileNotFoundError as e:
        print(f"file not found: {e}", file=sys.stderr)
        return 2
    net = build_network(db, brands=brands_path, cohorts=cohorts_path,
                        promote=promote_path)
    net.overview()  # fail fast if the DB is missing or unreadable
    if net.roster:
        print(f"brand roster: {len(net.roster)} brands from {brands_path}")
    if net.cohorts:
        print(f"cohort labels: {len(net.cohorts)} accounts"
              + (f" ({len(net.promoted)} promoted from {promote_path})"
                 if net.promoted else ""))

    page_html = INDEX_HTML.replace("$TITLE", html.escape(title))
    handler = type("BoundHandler", (Handler,), {"net": net, "page_html": page_html})
    httpd = ThreadingHTTPServer((host, port), handler)
    url = f"http://{host}:{port}/"
    print(f"redlens listening report → {url}  (Ctrl-C to stop)")
    if open_browser:
        threading.Thread(target=lambda: webbrowser.open(url), daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print()
    finally:
        httpd.server_close()
    return 0


# --------------------------------------------------------------------------- #
# Frontend (single self-contained page, no external assets) — styled after    #
# the redlens report (reporting/style.css): light, one red accent.            #
#                                                                              #
# The page ships as ``serve_assets/index.html`` and is loaded once at import   #
# via importlib.resources (same pattern as reporting/style.css). The single    #
# accent is injected from ``constants`` so the page can't drift from the       #
# reports; ``$TITLE`` stays a placeholder, substituted per-serve in serve().   #
# --------------------------------------------------------------------------- #

# ``$ACCENT_RGB`` before ``$ACCENT`` — the former is a prefix of the latter.
_ACCENT_RGB = ",".join(
    str(int(constants.ACCENT[i:i + 2], 16)) for i in (1, 3, 5))
INDEX_HTML = (
    files("redlens.serve_assets").joinpath("index.html")
    .read_text(encoding="utf-8")
    .replace("$ACCENT_RGB", _ACCENT_RGB)
    .replace("$ACCENT", constants.ACCENT)
)
