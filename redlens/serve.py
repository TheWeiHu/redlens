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

    redlens serve                          # over the default DB
    redlens --db redrover.db serve         # dogfood on the redrover network
    redlens serve --brands brands.csv --port 9000 --no-browser

The page follows the redlens report style (light, one ``constants.ACCENT``
red). The database is opened **read-only**; nothing here can mutate data and no
LLM key is required. Per-account ``gpt-4o-mini`` profiles with a
``coordinated?`` flag, brand share-of-voice, and view-time NL-plots are later
slices.
"""
from __future__ import annotations

import csv
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from collections import Counter
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from redlens import constants

MAX_ROWS = 60        # shared-subreddit / co-commented-thread / mention rows shown
MAX_CONTENT = 100    # account drill-down page cap
MAX_ACCOUNTS = 40    # matrix columns — top accounts by activity

# All accounts' activity, one row per post/comment (the network's event log).
_ACTIVITY = ("SELECT author_username u, subreddit_name sub FROM post "
             "UNION ALL SELECT author_username, subreddit_name FROM comment")

# Brand-ish term mining (the fallback brand proxy behind /api/mentions when
# no roster file is given).
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{2,}\b")
_CAP_MIN_RATIO = 0.75  # a name is capitalized nearly every time it appears
_SKIP_TERMS = frozenset(constants.data_lines("stopwords.txt")) | frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "reddit", "redditor", "redditors"})

BrandRoster = list[tuple[str, list[str]]]  # (display name, match terms)


def load_brands(path: Path) -> BrandRoster:
    """Parse a brand-roster CSV into ``(name, terms)`` rows.

    One brand per line: the display name, then the terms that count as a
    mention (``NordVPN, nordvpn, nord vpn``). A name with no terms matches
    itself. Blank lines and ``#`` comments are skipped.
    """
    roster: BrandRoster = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            cells = [c.strip() for c in row if c.strip()]
            if not cells or cells[0].startswith("#"):
                continue
            roster.append((cells[0], cells[1:] or cells[:1]))
    return roster


def _term_pattern(terms: list[str]) -> re.Pattern[str]:
    # (?<!\w)…(?!\w) instead of \b…\b: a plain \b needs a word char on the
    # boundary, so a symbol-edged term ("C++", "222.place") would never match.
    # Lookarounds assert only that the *adjacent* char isn't a word char, so
    # symbol-edged names count while "Go" still won't hit "Google". (The same
    # matcher as reporting/page.py's mention counting — and case-insensitive,
    # so a roster brand the network writes lowercase still counts.)
    return re.compile(
        r"(?<!\w)(?:" + "|".join(re.escape(t) for t in terms) + r")(?!\w)",
        re.IGNORECASE)


# --------------------------------------------------------------------------- #
# Data access (every request gets its own read-only connection)               #
# --------------------------------------------------------------------------- #

class Network:
    """Read-only queries that describe the account network in one DB."""

    def __init__(self, path: str, roster: BrandRoster | None = None) -> None:
        self.path = str(Path(path).resolve())
        self.roster = roster or []

    def _conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def overview(self) -> dict[str, Any]:
        with closing(self._conn()) as con:
            row = con.execute(
                """
                SELECT
                  (SELECT count(*) FROM post)                        AS posts,
                  (SELECT count(*) FROM comment)                     AS comments,
                  (SELECT count(DISTINCT subreddit_name) FROM (
                     SELECT subreddit_name FROM post
                     UNION SELECT subreddit_name FROM comment))      AS subreddits,
                  (SELECT min(t) FROM (
                     SELECT min(created_utc) t FROM post
                     UNION SELECT min(created_utc) FROM comment))    AS first_utc,
                  (SELECT max(t) FROM (
                     SELECT max(created_utc) t FROM post
                     UNION SELECT max(created_utc) FROM comment))    AS last_utc
                """
            ).fetchone()
            out = dict(row)
            out["accounts"] = len(self._authors(con))
            return out

    def _authors(self, con: sqlite3.Connection) -> list[str]:
        return [
            r[0] for r in con.execute(
                "SELECT author_username FROM post "
                "UNION SELECT author_username FROM comment ORDER BY 1"
            )
        ]

    def _matrix_accounts(self, con: sqlite3.Connection) -> list[str]:
        """The matrix column order: top accounts by total activity."""
        return [
            r["u"] for r in con.execute(
                f"SELECT u, count(*) n FROM ({_ACTIVITY}) "
                "GROUP BY u ORDER BY n DESC, u LIMIT ?",
                (MAX_ACCOUNTS,),
            )
        ]

    def accounts(self) -> list[dict[str, Any]]:
        """Per-account volume, karma, active window, and busiest subreddit."""
        with closing(self._conn()) as con:
            rows = con.execute(
                """
                WITH activity AS (
                  SELECT author_username AS u, subreddit_name AS sub,
                         created_utc AS t, 'post' AS kind FROM post
                  UNION ALL
                  SELECT author_username, subreddit_name, created_utc, 'comment'
                  FROM comment
                )
                SELECT
                  a.u                                              AS username,
                  sum(a.kind = 'post')                             AS posts,
                  sum(a.kind = 'comment')                          AS comments,
                  min(a.t)                                         AS first_utc,
                  max(a.t)                                         AS last_utc,
                  count(DISTINCT a.sub)                            AS subreddits,
                  u.post_karma                                     AS post_karma,
                  u.comment_karma                                  AS comment_karma
                FROM activity a
                LEFT JOIN user u ON u.username = a.u
                GROUP BY a.u
                """
            ).fetchall()
            top = self._top_subreddit(con)
            out = []
            for r in rows:
                d = dict(r)
                d["total"] = d["posts"] + d["comments"]
                d["top_subreddit"] = top.get(d["username"], "")
                out.append(d)
            out.sort(key=lambda d: (-d["total"], d["username"]))
            return out

    def _top_subreddit(self, con: sqlite3.Connection) -> dict[str, str]:
        """Busiest subreddit per author, across posts and comments."""
        rows = con.execute(
            f"""
            SELECT u, sub FROM (
              SELECT u, sub, row_number() OVER (
                       PARTITION BY u ORDER BY n DESC, sub) AS rn
              FROM (
                SELECT u, sub, count(*) n FROM ({_ACTIVITY}) GROUP BY u, sub))
            WHERE rn = 1
            """
        ).fetchall()
        return {r["u"]: r["sub"] for r in rows}

    def pairs(self) -> dict[str, Any]:
        """Account × account co-activity — the network-matrix heatmap.

        For each pair among the top ``MAX_ACCOUNTS`` accounts: how many
        subreddits both are active in and how many threads both commented in.
        Also carries the matrix column order every matrix on the page shares.
        """
        with closing(self._conn()) as con:
            accounts = self._matrix_accounts(con)
            if len(accounts) < 2:
                return {"accounts": accounts,
                        "total_accounts": len(accounts), "pairs": []}
            ph = ",".join("?" * len(accounts))
            cells: dict[tuple[str, str], dict[str, int]] = {}

            def tally(sql: str, key: str) -> None:
                for r in con.execute(sql, accounts):
                    pair = cells.setdefault(
                        (r["ua"], r["ub"]), {"subs": 0, "threads": 0})
                    pair[key] = r["n"]

            tally(
                f"""
                WITH us AS (SELECT DISTINCT u, sub FROM ({_ACTIVITY})
                            WHERE u IN ({ph}))
                SELECT a.u AS ua, b.u AS ub, count(*) AS n
                FROM us a JOIN us b ON a.sub = b.sub AND a.u < b.u
                GROUP BY ua, ub
                """, "subs")
            tally(
                f"""
                WITH ut AS (SELECT DISTINCT author_username u, link_id t
                            FROM comment WHERE author_username IN ({ph}))
                SELECT a.u AS ua, b.u AS ub, count(*) AS n
                FROM ut a JOIN ut b ON a.t = b.t AND a.u < b.u
                GROUP BY ua, ub
                """, "threads")
            return {
                "accounts": accounts,
                "total_accounts": len(self._authors(con)),
                "pairs": [{"a": a, "b": b, **v}
                          for (a, b), v in sorted(cells.items())],
            }

    def _texts(self) -> list[sqlite3.Row]:
        """Every account's text, one row per post/comment: ``(u, t)``."""
        with closing(self._conn()) as con:
            return con.execute(
                "SELECT author_username u, coalesce(title,'') || ' ' || "
                "coalesce(selftext,'') t FROM post "
                "UNION ALL SELECT author_username, coalesce(body,'') "
                "FROM comment"
            ).fetchall()

    def mentions(self) -> dict[str, Any]:
        """Brand/name mentions per account, for the mention matrix.

        With a roster (``brands.csv`` / ``--brands``) the counting is exact:
        deterministic, case-insensitive, whole-word over each brand's terms —
        a mention is a post/comment that matches. Without one it falls back
        to mined proper names (see ``_mined_mentions``).
        """
        return self._roster_mentions() if self.roster else self._mined_mentions()

    def _roster_mentions(self) -> dict[str, Any]:
        texts = self._texts()
        rows: list[dict[str, Any]] = []
        for name, terms in self.roster:
            pat = _term_pattern(terms)
            cells = Counter(r["u"] for r in texts if pat.search(r["t"]))
            if not cells:
                continue
            rows.append({"term": name, "accounts": len(cells),
                         "uses": sum(cells.values()), "cells": dict(cells)})
        rows.sort(key=lambda r: (-r["accounts"], -r["uses"],
                                 str(r["term"]).lower()))
        return {"source": "roster", "total": len(rows), "rows": rows[:MAX_ROWS]}

    def _mined_mentions(self) -> dict[str, Any]:
        """Co-mentioned proper names, mined keylessly — the no-roster fallback.

        A token counts as a *name* when, looking only at **mid-sentence**
        occurrences (sentence starts prove nothing — every word is capitalized
        there), it is capitalized at least ``_CAP_MIN_RATIO`` of the time.
        Products and proper names are; prose words show up lowercase
        mid-sentence and drop out. Once a term qualifies, every casing counts
        as a mention. Ranked by how many accounts use the term (≥2).

        Honest limit: a brand the network *always* writes lowercase never
        qualifies — that's what the roster (and later the LLM slice) is for.
        """
        texts = self._texts()
        mid_total: Counter[str] = Counter()            # mid-sentence, any case
        mid_cap: Counter[str] = Counter()              # mid-sentence, capital
        casings: dict[str, Counter[str]] = {}          # low -> seen spellings
        by_account: dict[str, Counter[str]] = {}       # low -> account -> n
        for row in texts:
            text = row["t"]
            for m in _TOKEN_RE.finditer(text):
                tok = m.group()
                low = tok.lower()
                by_account.setdefault(low, Counter())[row["u"]] += 1
                head = text[:m.start()].rstrip(" \"'([*_")
                if head and head[-1] not in ".!?:;\n-•":
                    mid_total[low] += 1
                    if tok[0].isupper():
                        mid_cap[low] += 1
                if tok[0].isupper():
                    casings.setdefault(low, Counter())[tok] += 1
        rows: list[dict[str, Any]] = []
        for low, caps in mid_cap.items():
            if low in _SKIP_TERMS or caps / mid_total[low] < _CAP_MIN_RATIO:
                continue
            accounts = by_account[low]
            if len(accounts) < 2:
                continue
            spelling = sorted(casings[low].items(),
                              key=lambda kv: (-kv[1], kv[0]))[0][0]
            rows.append({"term": spelling, "accounts": len(accounts),
                         "uses": sum(accounts.values()), "cells": dict(accounts)})
        rows.sort(key=lambda r: (-r["accounts"], -r["uses"],
                                 str(r["term"]).lower()))
        return {"source": "mined", "total": len(rows), "rows": rows[:MAX_ROWS]}

    def _cells(self, con: sqlite3.Connection, sql: str,
               keys: list[str]) -> dict[str, dict[str, int]]:
        """Per-(row, account) matrix cells for the rows a section shows.

        ``sql`` must select ``k`` (the row key), ``u`` and ``n``, with an
        ``IN ({ph})`` placeholder for ``keys``.
        """
        cells: dict[str, dict[str, int]] = {k: {} for k in keys}
        if keys:
            ph = ",".join("?" * len(keys))
            for r in con.execute(sql.format(ph=ph), keys):
                cells[r["k"]][r["u"]] = r["n"]
        return cells

    def subreddits(self) -> dict[str, Any]:
        """Shared-subreddit footprint: subs where ≥2 accounts are active.

        Long tails are common (a real network shares hundreds of subs), so this
        returns the ``MAX_ROWS`` widest-shared plus ``total`` for a "top N of M"
        caption. Each row carries per-account activity ``cells`` for the matrix.
        """
        with closing(self._conn()) as con:
            total = con.execute(
                f"""
                SELECT count(*) FROM (
                  SELECT sub FROM ({_ACTIVITY})
                  GROUP BY sub
                  HAVING count(DISTINCT u) >= 2)
                """
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT sub                              AS subreddit,
                       count(DISTINCT u)                AS accounts,
                       sum(kind = 'post')               AS posts,
                       sum(kind = 'comment')            AS comments
                FROM (
                  SELECT author_username u, subreddit_name sub, 'post' kind
                  FROM post
                  UNION ALL
                  SELECT author_username, subreddit_name, 'comment' FROM comment)
                GROUP BY sub
                HAVING accounts >= 2
                ORDER BY accounts DESC, (posts + comments) DESC, subreddit
                LIMIT ?
                """,
                (MAX_ROWS,),
            ).fetchall()
            out = [dict(r) for r in rows]
            cells = self._cells(
                con,
                f"SELECT sub AS k, u, count(*) n FROM ({_ACTIVITY}) "
                "WHERE sub IN ({ph}) GROUP BY sub, u",
                [d["subreddit"] for d in out])
            for d in out:
                d["cells"] = cells[d["subreddit"]]
            return {"total": total, "rows": out}

    def threads(self) -> dict[str, Any]:
        """Threads (``link_id``) commented in by ≥2 accounts — co-activity."""
        with closing(self._conn()) as con:
            total = con.execute(
                """
                SELECT count(*) FROM (
                  SELECT link_id FROM comment
                  GROUP BY link_id
                  HAVING count(DISTINCT author_username) >= 2)
                """
            ).fetchone()[0]
            rows = con.execute(
                """
                SELECT link_id                          AS link_id,
                       subreddit_name                   AS subreddit,
                       count(DISTINCT author_username)  AS accounts,
                       count(*)                         AS comments
                FROM comment
                GROUP BY link_id
                HAVING accounts >= 2
                ORDER BY accounts DESC, comments DESC
                LIMIT ?
                """,
                (MAX_ROWS,),
            ).fetchall()
            out = [dict(r) for r in rows]
            cells = self._cells(
                con,
                "SELECT link_id AS k, author_username u, count(*) n "
                "FROM comment WHERE link_id IN ({ph}) "
                "GROUP BY link_id, author_username",
                [d["link_id"] for d in out])
            for d in out:
                d["cells"] = cells[d["link_id"]]
                title = con.execute(
                    "SELECT title FROM post WHERE post_id = ?", (d["link_id"],)
                ).fetchone()
                d["title"] = title[0] if title and title[0] else ""
            return {"total": total, "rows": out}

    # ---- cell evidence: the posts/comments behind any matrix cell ---- #

    def pair_evidence(self, a: str, b: str) -> dict[str, Any]:
        """What entangles two accounts — the exact units the network-matrix
        cell counts: subreddits both are active in and threads both
        commented in, with each side's activity count."""
        with closing(self._conn()) as con:
            subs = con.execute(
                f"""
                SELECT sub                AS subreddit,
                       sum(u = ?)         AS a_n,
                       sum(u = ?)         AS b_n
                FROM ({_ACTIVITY}) WHERE u IN (?, ?)
                GROUP BY sub HAVING a_n > 0 AND b_n > 0
                ORDER BY (a_n + b_n) DESC, subreddit LIMIT ?
                """, (a, b, a, b, MAX_ROWS)).fetchall()
            threads = con.execute(
                """
                SELECT link_id, subreddit_name        AS subreddit,
                       sum(author_username = ?)       AS a_n,
                       sum(author_username = ?)       AS b_n
                FROM comment WHERE author_username IN (?, ?)
                GROUP BY link_id HAVING a_n > 0 AND b_n > 0
                ORDER BY (a_n + b_n) DESC, link_id LIMIT ?
                """, (a, b, a, b, MAX_ROWS)).fetchall()
            out = []
            for r in threads:
                d = dict(r)
                title = con.execute(
                    "SELECT title FROM post WHERE post_id = ?", (d["link_id"],)
                ).fetchone()
                d["title"] = title[0] if title and title[0] else ""
                out.append(d)
            return {"subs": [dict(r) for r in subs], "threads": out}

    @staticmethod
    def _items_payload(rows: list[sqlite3.Row]) -> dict[str, Any]:
        items = sorted((dict(r) for r in rows),
                       key=lambda d: -(d["created_utc"] or 0))
        return {"total": len(items), "items": items[:MAX_CONTENT]}

    def account_sub_items(self, username: str, sub: str) -> dict[str, Any]:
        """One account's posts + comments in one subreddit (a footprint cell)."""
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT 'post' AS kind, subreddit_name AS subreddit, title, "
                "selftext, url, score, created_utc FROM post "
                "WHERE author_username = ? AND subreddit_name = ? "
                "UNION ALL "
                "SELECT 'comment', subreddit_name, NULL, body, NULL, score, "
                "created_utc FROM comment "
                "WHERE author_username = ? AND subreddit_name = ?",
                (username, sub, username, sub)).fetchall()
        return self._items_payload(rows)

    def account_thread_items(self, username: str, link_id: str) -> dict[str, Any]:
        """One account's comments in one thread (a co-commented cell)."""
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT 'comment' AS kind, subreddit_name AS subreddit, "
                "NULL AS title, body AS selftext, NULL AS url, score, "
                "created_utc FROM comment "
                "WHERE author_username = ? AND link_id = ?",
                (username, link_id)).fetchall()
            title = con.execute(
                "SELECT title FROM post WHERE post_id = ?", (link_id,)
            ).fetchone()
        out = self._items_payload(rows)
        out["title"] = title[0] if title and title[0] else ""
        return out

    def account_term_items(self, username: str, term: str) -> dict[str, Any]:
        """One account's posts + comments mentioning a brand/name (a mention
        cell). ``term`` is a roster name (matched by its terms) or a mined
        term (matched by itself)."""
        terms = next((t for n, t in self.roster if n == term), [term])
        pat = _term_pattern(terms)
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT 'post' AS kind, subreddit_name AS subreddit, title, "
                "selftext, url, score, created_utc FROM post "
                "WHERE author_username = ? "
                "UNION ALL "
                "SELECT 'comment', subreddit_name, NULL, body, NULL, score, "
                "created_utc FROM comment WHERE author_username = ?",
                (username, username)).fetchall()
        hits = [r for r in rows
                if pat.search(f"{r['title'] or ''} {r['selftext'] or ''}")]
        return self._items_payload(hits)

    def content(self, username: str, kind: str, *, limit: int,
                offset: int) -> dict[str, Any]:
        """One account's raw posts or comments, newest first (drill-down)."""
        limit = max(1, min(limit, MAX_CONTENT))
        offset = max(0, offset)
        with closing(self._conn()) as con:
            if kind == "comments":
                total = con.execute(
                    "SELECT count(*) FROM comment WHERE author_username = ?",
                    (username,),
                ).fetchone()[0]
                rows = con.execute(
                    "SELECT subreddit_name AS subreddit, body, score, "
                    "created_utc AS created_utc, link_id "
                    "FROM comment WHERE author_username = ? "
                    "ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                    (username, limit, offset),
                ).fetchall()
            else:
                total = con.execute(
                    "SELECT count(*) FROM post WHERE author_username = ?",
                    (username,),
                ).fetchone()[0]
                rows = con.execute(
                    "SELECT subreddit_name AS subreddit, title, selftext, url, "
                    "score, num_comments, created_utc AS created_utc, post_id "
                    "FROM post WHERE author_username = ? "
                    "ORDER BY created_utc DESC LIMIT ? OFFSET ?",
                    (username, limit, offset),
                ).fetchall()
            return {"kind": kind, "total": total, "limit": limit,
                    "offset": offset, "items": [dict(r) for r in rows]}


# --------------------------------------------------------------------------- #
# HTTP handler                                                                 #
# --------------------------------------------------------------------------- #

class Handler(BaseHTTPRequestHandler):
    net: Network  # injected on the server

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
        q = parse_qs(u.query)

        def one(k: str, d: str = "") -> str:
            return q.get(k, [d])[0]

        try:
            if u.path == "/":
                self._send(200, INDEX_HTML.encode(), "text/html; charset=utf-8")
            elif u.path == "/api/overview":
                self._json({"db": self.net.path, **self.net.overview()})
            elif u.path == "/api/accounts":
                self._json({"accounts": self.net.accounts()})
            elif u.path == "/api/pairs":
                self._json(self.net.pairs())
            elif u.path == "/api/mentions":
                self._json(self.net.mentions())
            elif u.path == "/api/evidence":
                kind = one("type")
                if kind == "pair":
                    self._json(self.net.pair_evidence(one("a"), one("b")))
                elif kind == "sub":
                    self._json(self.net.account_sub_items(one("u"), one("sub")))
                elif kind == "thread":
                    self._json(
                        self.net.account_thread_items(one("u"), one("link")))
                elif kind == "mention":
                    self._json(
                        self.net.account_term_items(one("u"), one("term")))
                else:
                    self._json({"error": "unknown evidence type"}, 400)
            elif u.path == "/api/subreddits":
                self._json(self.net.subreddits())
            elif u.path == "/api/threads":
                self._json(self.net.threads())
            elif u.path == "/api/content":
                self._json(self.net.content(
                    one("u"),
                    one("kind", "posts"),
                    limit=int(one("limit", "50") or 50),
                    offset=int(one("offset", "0") or 0),
                ))
            else:
                self._json({"error": "not found"}, 404)
        except Exception as e:  # noqa: BLE001
            self._json({"error": str(e)}, 400)


# --------------------------------------------------------------------------- #
# Entry point                                                                  #
# --------------------------------------------------------------------------- #

def serve(db: str | Path, *, host: str = "127.0.0.1", port: int = 8000,
          open_browser: bool = True, brands: str | Path | None = None) -> int:
    # Brand roster: an explicit --brands path must exist; otherwise a
    # brands.csv sitting next to the DB is picked up automatically.
    brands_path = Path(brands) if brands else Path(db).resolve().parent / "brands.csv"
    roster: BrandRoster = []
    if brands and not brands_path.is_file():
        print(f"brands file not found: {brands_path}", file=sys.stderr)
        return 2
    if brands_path.is_file():
        roster = load_brands(brands_path)

    net = Network(str(db), roster=roster)
    net.overview()  # fail fast if the DB is missing or unreadable
    if roster:
        print(f"brand roster: {len(roster)} brands from {brands_path}")

    handler = type("BoundHandler", (Handler,), {"net": net})
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
# --------------------------------------------------------------------------- #

_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>coordinated network · redlens</title>
<style>
  body { font-family: system-ui, sans-serif; max-width: 1150px; margin: 2rem auto;
         padding: 0 1rem; line-height: 1.4; color: #222; }
  h1 { text-align: center; font-weight: 600; margin: 0 0 .2rem; }
  h2 { margin: 2.4rem 0 .3rem; font-size: 1rem; font-weight: 600;
       text-transform: uppercase; letter-spacing: .05em; color: $ACCENT;
       border-bottom: 2px solid $ACCENT; padding-bottom: .2rem; }
  h2 .count { color: #888; font-weight: 400; text-transform: none;
              letter-spacing: 0; font-size: .85rem; }
  a { color: $ACCENT; text-decoration: none; }
  a:hover { text-decoration: underline; }
  .muted { color: #888; font-size: .85rem; }
  .sub { color: #888; font-size: .85rem; margin: 0 0 .8rem; }
  .db { text-align: center; color: #888; font-size: .8rem; word-break: break-all; }
  .stats { display: flex; flex-wrap: wrap; gap: .4rem 2.2rem;
           justify-content: center; margin: 1.2rem 0 0; }
  .stat { text-align: center; }
  .stat b { display: block; font-size: 1.35rem; color: $ACCENT;
            font-variant-numeric: tabular-nums; }
  .stat span { font-size: .7rem; color: #888; text-transform: uppercase;
               letter-spacing: .06em; }
  table { border-collapse: collapse; width: 100%; font-size: .85rem; }
  th, td { border-bottom: 1px solid #eee; padding: .3rem .5rem; text-align: left;
           vertical-align: middle; }
  th { color: #888; font-weight: 600; white-space: nowrap; }
  td.num, th.num { text-align: right; font-variant-numeric: tabular-nums;
                   white-space: nowrap; }
  table.plain tbody tr:nth-child(even) { background: #fafafa; }
  table.plain tbody tr:hover { background: #faf3f0; }
  #accounts th { cursor: pointer; user-select: none; }
  .u { color: $ACCENT; cursor: pointer; }
  .bar { height: 3px; background: $ACCENT; margin-top: 3px; }
  .wrap { overflow-x: auto; }
  /* matrices — account columns, dot/heat cells */
  .matrix th.acct { writing-mode: vertical-rl; transform: rotate(180deg);
                    font-weight: 400; font-size: .75rem; padding: .2rem .15rem;
                    border-bottom: none; }
  .matrix td.cell { text-align: center; padding: .1rem; min-width: 1.35rem;
                    line-height: 1; }
  .matrix td.lbl { max-width: 24rem; overflow: hidden; text-overflow: ellipsis;
                   white-space: nowrap; }
  .matrix tbody tr:hover td { background: #faf3f0; }
  .matrix tbody tr:hover td[style] { filter: brightness(.92); }
  .matrix td.click { cursor: pointer; }
  .matrix td.click:hover { outline: 2px solid $ACCENT; outline-offset: -2px; }
  .dot { display: inline-block; border-radius: 50%; background: $ACCENT;
         vertical-align: middle; }
  .heat td.cell { height: 1.35rem; }
  .heat td.diag { background: #eee; }
  /* account drill-down drawer */
  .drawer { position: fixed; top: 0; right: 0; width: min(680px, 92vw);
            height: 100vh; background: #fff; border-left: 1px solid #eee;
            transform: translateX(100%); transition: transform .15s ease;
            overflow-y: auto; box-shadow: -12px 0 30px rgba(0,0,0,.12); }
  .drawer.open { transform: translateX(0); }
  .drawer .dh { position: sticky; top: 0; background: #fff; padding: 14px 20px;
                border-bottom: 2px solid $ACCENT; display: flex;
                align-items: center; justify-content: space-between; gap: 12px; }
  .drawer .dh h3 { margin: 0; font-size: 1rem; }
  .drawer h4 { margin: 1.1rem 0 .3rem; font-size: .8rem; color: $ACCENT;
               text-transform: uppercase; letter-spacing: .05em; }
  .drawer .tabs { display: flex; gap: 4px; }
  .drawer .tab { padding: 3px 10px; border: 1px solid #eee; border-radius: 4px;
                 cursor: pointer; color: #888; font-size: .85rem; }
  .drawer .tab.active { color: $ACCENT; border-color: $ACCENT; }
  .drawer .close { cursor: pointer; color: #888; font-size: 18px; border: none;
                   background: none; }
  .drawer .body { padding: 12px 20px 40px; }
  .item { border-bottom: 1px solid #eee; padding: 10px 0; font-size: .85rem; }
  .item .meta { color: #888; font-size: .8rem; margin-bottom: 3px; }
  .item .meta b { color: #222; }
  .item .txt { white-space: pre-wrap; word-break: break-word; }
  .item .title { font-weight: 600; }
  .pager { display: flex; gap: 10px; align-items: center; margin-top: 12px; }
  .pager button { background: #fff; color: $ACCENT; border: 1px solid #eee;
                  border-radius: 4px; padding: 4px 12px; cursor: pointer;
                  font: inherit; }
  .pager button:disabled { opacity: .35; cursor: default; }
  .warn { color: $ACCENT; }
</style>
</head>
<body>
<h1>coordinated network</h1>
<div class="db" id="db">…</div>
<div class="stats" id="stats"></div>

<h2>Network matrix</h2>
<p class="sub">How entangled each pair of accounts is — shared subreddits plus
  co-commented threads. Darker = more co-activity; click any cell for the
  subreddits and threads behind it. <span id="pairs-note"></span></p>
<div class="wrap" id="heat"></div>

<h2>Accounts</h2>
<p class="sub">Every account in this database, treated as one cohort. Click a
  name to drill into its raw posts and comments.</p>
<div class="wrap"><table id="accounts" class="plain"></table></div>

<h2>Brand mentions <span class="count" id="mention-count"></span></h2>
<p class="sub" id="mention-sub"></p>
<div class="wrap" id="mentions"></div>

<h2>Shared subreddit footprint <span class="count" id="sub-count"></span></h2>
<p class="sub">Subreddits where ≥2 accounts are active — where the network
  overlaps. Dot area ~ that account's posts + comments there; click a dot to
  read them. A column of dots down the same subreddits is a coordination
  signal.</p>
<div class="wrap" id="subreddits"></div>

<h2>Co-commented threads <span class="count" id="thread-count"></span></h2>
<p class="sub">Threads touched by ≥2 accounts — the strongest cheap co-activity
  signal (they show up in the same conversations). Dot area ~ comments in the
  thread; click a dot to read them.</p>
<div class="wrap" id="threads"></div>

<div class="drawer" id="drawer">
  <div class="dh">
    <h3 id="d-user"></h3>
    <div class="tabs" id="d-tabs">
      <div class="tab active" data-kind="posts" id="tab-posts">posts</div>
      <div class="tab" data-kind="comments" id="tab-comments">comments</div>
    </div>
    <button class="close" id="d-close">✕</button>
  </div>
  <div class="body" id="d-body"></div>
</div>

<script>
const $ = s => document.querySelector(s);
const fmt = n => (n ?? 0).toLocaleString();
const esc = s => String(s).replace(/[&<>"]/g,
  c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;'}[c]));
const day = t => t ? new Date(t*1000).toISOString().slice(0,10) : '—';
const plural = (n, w) => `${fmt(n)} ${w}${n===1?'':'s'}`;

async function getJSON(url){ const r = await fetch(url); const j = await r.json();
  if(!r.ok || j.error) throw new Error(j.error || r.statusText); return j; }

// ---- overview ----
async function loadOverview(){
  const o = await getJSON('/api/overview');
  $('#db').textContent = o.db;
  $('#stats').innerHTML = [
    ['accounts', o.accounts], ['posts', o.posts], ['comments', o.comments],
    ['subreddits', o.subreddits],
  ].map(([k,v]) => `<div class="stat"><b>${fmt(v)}</b><span>${k}</span></div>`).join('')
    + `<div class="stat"><b>${day(o.first_utc)}</b><span>first seen</span></div>`
    + `<div class="stat"><b>${day(o.last_utc)}</b><span>last seen</span></div>`;
}

// ---- shared matrix helpers ----
const userCell = u =>
  `<span class="u" onclick="openUser('${esc(u)}')">${esc(u)}</span>`;
// One vertical account-label header row, shared by every matrix so the same
// column always means the same account.
const acctHead = accounts =>
  accounts.map(u => `<th class="acct">${userCell(u)}</th>`).join('');
function dotCell(n, peak, tip, ri, ci){
  if(!n) return '<td class="cell"></td>';
  const d = (4 + 14 * Math.sqrt(n / peak)).toFixed(1);
  return `<td class="cell click" data-r="${ri}" data-c="${ci}" title="${tip}">` +
         `<span class="dot" style="width:${d}px;height:${d}px"></span></td>`;
}
function topOf(total, shown){
  return total > shown ? `top ${fmt(shown)} of ${fmt(total)}` : `${fmt(total)}`;
}

// ---- network matrix (account × account heatmap) ----
async function loadPairs(){
  const p = await getJSON('/api/pairs');
  const A = p.accounts;
  if(p.total_accounts > A.length)
    $('#pairs-note').textContent =
      `Columns: the ${fmt(A.length)} most active of ${fmt(p.total_accounts)} accounts.`;
  if(A.length < 2){
    $('#heat').innerHTML = '<p class="muted">Need ≥2 accounts to relate.</p>';
    return A;
  }
  const val = {};
  p.pairs.forEach(x => { val[x.a+'|'+x.b] = x; });
  const get = (a,b) => val[a+'|'+b] || val[b+'|'+a] || {subs:0, threads:0};
  const peak = Math.max(1, ...p.pairs.map(x => x.subs + x.threads));
  const cell = (a,b,ri,ci) => {
    if(a === b) return '<td class="cell diag"></td>';
    const v = get(a,b), t = v.subs + v.threads;
    if(!t) return '<td class="cell"></td>';
    const alpha = (.12 + .78 * t / peak).toFixed(2);
    return `<td class="cell click" data-r="${ri}" data-c="${ci}" ` +
           `style="background:rgba($ACCENT_RGB,${alpha})" ` +
           `title="${esc(a)} × ${esc(b)} — ${plural(v.subs,'shared subreddit')}` +
           ` · ${plural(v.threads,'co-commented thread')}"></td>`;
  };
  $('#heat').innerHTML =
    `<table class="matrix heat"><thead><tr><th></th>${acctHead(A)}</tr></thead><tbody>` +
    A.map((a,ri) => `<tr><td class="lbl">${userCell(a)}</td>` +
               A.map((b,ci) => cell(a,b,ri,ci)).join('') + '</tr>').join('') +
    '</tbody></table>';
  $('#heat').querySelectorAll('td.click').forEach(td => td.onclick =
    () => openPair(A[+td.dataset.r], A[+td.dataset.c]));
  return A;
}

// ---- accounts ----
function sortable(table, rows, cols, render){
  let sort = cols.find(c => c.def) || cols[0], asc = false;
  const draw = () => {
    const data = [...rows].sort((a,b) => {
      const x=a[sort.key], y=b[sort.key];
      const c = (x<y?-1:x>y?1:0); return asc ? c : -c;
    });
    table.innerHTML =
      '<thead><tr>' + cols.map(c =>
        `<th class="${c.num?'num':''}" data-k="${c.key}">${c.label}` +
        (c.key===sort.key ? (asc?' ▲':' ▼') : '') + '</th>').join('') + '</tr></thead>' +
      '<tbody>' + data.map(render).join('') + '</tbody>';
    table.querySelectorAll('th').forEach(th => th.onclick = () => {
      const k = th.dataset.k;
      if(sort.key===k) asc=!asc; else { sort=cols.find(c=>c.key===k); asc=false; }
      draw();
    });
  };
  draw();
}

async function loadAccounts(){
  const { accounts } = await getJSON('/api/accounts');
  const max = Math.max(1, ...accounts.map(a => a.total));
  sortable($('#accounts'), accounts, [
    {key:'username', label:'account'},
    {key:'total', label:'total', num:true, def:true},
    {key:'posts', label:'posts', num:true},
    {key:'comments', label:'comments', num:true},
    {key:'subreddits', label:'subs', num:true},
    {key:'post_karma', label:'post karma', num:true},
    {key:'comment_karma', label:'cmt karma', num:true},
    {key:'first_utc', label:'first'},
    {key:'last_utc', label:'last'},
    {key:'top_subreddit', label:'top sub'},
  ], a => `<tr>
    <td>${userCell(a.username)}</td>
    <td class="num">${fmt(a.total)}<div class="bar" style="width:${100*a.total/max}%"></div></td>
    <td class="num">${fmt(a.posts)}</td>
    <td class="num">${fmt(a.comments)}</td>
    <td class="num">${fmt(a.subreddits)}</td>
    <td class="num">${a.post_karma==null?'—':fmt(a.post_karma)}</td>
    <td class="num">${a.comment_karma==null?'—':fmt(a.comment_karma)}</td>
    <td class="muted">${day(a.first_utc)}</td>
    <td class="muted">${day(a.last_utc)}</td>
    <td><a href="https://reddit.com/r/${esc(a.top_subreddit)}" target="_blank">${esc(a.top_subreddit)}</a></td>
  </tr>`);
}

// ---- row × account dot matrices (brands, shared subs, threads) ----
// onCell(row, account) opens the evidence behind a clicked dot.
function dotMatrix(el, rows, accounts, cols, tipFn, onCell){
  if(!rows.length) return;
  const peak = Math.max(1, ...rows.flatMap(
    r => accounts.map(u => r.cells[u] || 0)));
  el.innerHTML =
    `<table class="matrix"><thead><tr>` +
    cols.map(c => `<th class="${c.num?'num':''}">${c.label}</th>`).join('') +
    `${acctHead(accounts)}</tr></thead><tbody>` +
    rows.map((r, ri) =>
      '<tr>' + cols.map(c => c.cell(r)).join('') +
      accounts.map((u, ci) =>
        dotCell(r.cells[u] || 0, peak, tipFn(r, u), ri, ci)).join('') +
      '</tr>').join('') +
    '</tbody></table>';
  el.querySelectorAll('td.click').forEach(td => td.onclick =
    () => onCell(rows[+td.dataset.r], accounts[+td.dataset.c]));
}

async function loadMentions(accounts){
  const { source, total, rows } = await getJSON('/api/mentions');
  $('#mention-sub').textContent = source === 'roster'
    ? 'Roster brands (brands.csv next to the DB, or --brands), matched ' +
      'case-insensitively as whole words. Dot area ~ that account’s ' +
      'posts + comments mentioning the brand; click a dot to read them.'
    : 'No brand roster found (add brands.csv next to the DB, or --brands) ' +
      '— falling back to mined proper names: terms capitalized nearly ' +
      'every time they appear mid-sentence, used by ≥2 accounts. ' +
      'Click a dot to read the mentions.';
  $('#mention-count').textContent = rows.length ? topOf(total, rows.length) : '';
  if(!rows.length){
    $('#mentions').innerHTML = source === 'roster'
      ? '<p class="muted">No roster brand is mentioned in this database.</p>'
      : '<p class="muted">No name is mentioned by ≥2 accounts.</p>';
    return;
  }
  dotMatrix($('#mentions'), rows, accounts, [
    {label:'brand / name', cell: r => `<td class="lbl">${esc(r.term)}</td>`},
    {label:'accounts', num:true, cell: r => `<td class="num">${fmt(r.accounts)}</td>`},
    {label:'mentions', num:true, cell: r => `<td class="num">${fmt(r.uses)}</td>`},
  ], (r, u) => `${esc(u)} — ${plural(r.cells[u], 'mention')} of ${esc(r.term)}`,
  (r, u) => openEvidence(`${u} · ${r.term}`,
    `/api/evidence?type=mention&u=${encodeURIComponent(u)}&term=${encodeURIComponent(r.term)}`,
    itemsHtml));
}

async function loadSubreddits(accounts){
  const { total, rows } = await getJSON('/api/subreddits');
  $('#sub-count').textContent = rows.length ? topOf(total, rows.length) : '';
  if(!rows.length){
    $('#subreddits').innerHTML =
      '<p class="muted">No subreddit is shared by ≥2 accounts.</p>';
    return;
  }
  dotMatrix($('#subreddits'), rows, accounts, [
    {label:'subreddit', cell: r => `<td class="lbl">` +
      `<a href="https://reddit.com/r/${esc(r.subreddit)}" target="_blank">r/${esc(r.subreddit)}</a></td>`},
    {label:'accounts', num:true, cell: r => `<td class="num">${fmt(r.accounts)}</td>`},
    {label:'posts', num:true, cell: r => `<td class="num">${fmt(r.posts)}</td>`},
    {label:'comments', num:true, cell: r => `<td class="num">${fmt(r.comments)}</td>`},
  ], (r, u) => `${esc(u)} in r/${esc(r.subreddit)} — ` +
               plural(r.cells[u], 'post/comment'),
  (r, u) => openEvidence(`${u} · r/${r.subreddit}`,
    `/api/evidence?type=sub&u=${encodeURIComponent(u)}&sub=${encodeURIComponent(r.subreddit)}`,
    itemsHtml));
}

async function loadThreads(accounts){
  const { total, rows } = await getJSON('/api/threads');
  $('#thread-count').textContent = rows.length ? topOf(total, rows.length) : '';
  if(!rows.length){
    $('#threads').innerHTML =
      '<p class="muted">No thread is shared by ≥2 accounts.</p>';
    return;
  }
  dotMatrix($('#threads'), rows, accounts, [
    {label:'thread', cell: t => `<td class="lbl">` +
      `<a href="https://redd.it/${esc(t.link_id)}" target="_blank" ` +
      `title="${esc(t.title)}">${esc(t.title) || t.link_id}</a></td>`},
    {label:'subreddit', cell: t =>
      `<td><a href="https://reddit.com/r/${esc(t.subreddit)}" target="_blank">r/${esc(t.subreddit)}</a></td>`},
    {label:'accounts', num:true, cell: t => `<td class="num">${fmt(t.accounts)}</td>`},
    {label:'comments', num:true, cell: t => `<td class="num">${fmt(t.comments)}</td>`},
  ], (t, u) => `${esc(u)} — ${plural(t.cells[u], 'comment')} in this thread`,
  (t, u) => openEvidence(`${u} · in thread`,
    `/api/evidence?type=thread&u=${encodeURIComponent(u)}&link=${encodeURIComponent(t.link_id)}`,
    r => (r.title ? `<p class="muted">${esc(r.title)}</p>` : '') + itemsHtml(r)));
}

// ---- drawer ----
// Two modes share it: an account's paginated posts/comments (tabs shown), and
// one-shot cell evidence (tabs hidden).
let cur = { user:null, kind:'posts', offset:0, limit:50 };
function openDrawer(title, tabs){
  $('#d-user').textContent = title;
  $('#d-tabs').style.display = tabs ? 'flex' : 'none';
  $('#drawer').classList.add('open');
}
function openUser(u){ cur = { user:u, kind:'posts', offset:0, limit:50 };
  openDrawer(u, true); setTab('posts'); loadContent(); }
$('#d-close').onclick = () => $('#drawer').classList.remove('open');

// ---- cell evidence ----
function renderEvidenceItem(it){
  const head = `<div class="meta">r/${esc(it.subreddit)} · <b>${fmt(it.score)}</b> pts · ${day(it.created_utc)} · ${esc(it.kind)}</div>`;
  return `<div class="item">${head}
    ${it.title ? `<div class="title">${esc(it.title)}</div>` : ''}
    ${it.selftext ? `<div class="txt">${esc(it.selftext)}</div>` : ''}
    ${it.url ? `<div><a href="${esc(it.url)}" target="_blank">${esc(it.url)}</a></div>` : ''}</div>`;
}
const itemsHtml = r =>
  (r.items.map(renderEvidenceItem).join('') ||
    '<p class="muted">Nothing here.</p>') +
  (r.total > r.items.length
    ? `<p class="muted">first ${fmt(r.items.length)} of ${fmt(r.total)}</p>` : '');

async function openEvidence(title, url, render){
  openDrawer(title, false);
  $('#d-body').innerHTML = '<p class="muted">loading…</p>';
  try { const r = await getJSON(url); $('#d-body').innerHTML = render(r); }
  catch(e){ $('#d-body').innerHTML = `<p class="warn">${esc(e.message)}</p>`; }
}

function openPair(a, b){
  openEvidence(`${a} × ${b}`,
    `/api/evidence?type=pair&a=${encodeURIComponent(a)}&b=${encodeURIComponent(b)}`,
    r => {
      const head = `<thead><tr><th></th><th class="num">${esc(a)}</th><th class="num">${esc(b)}</th></tr></thead>`;
      const subs = r.subs.map(s =>
        `<tr><td><a href="https://reddit.com/r/${esc(s.subreddit)}" target="_blank">r/${esc(s.subreddit)}</a></td>
         <td class="num">${fmt(s.a_n)}</td><td class="num">${fmt(s.b_n)}</td></tr>`).join('');
      const threads = r.threads.map(t =>
        `<tr><td><a href="https://redd.it/${esc(t.link_id)}" target="_blank">${esc(t.title) || t.link_id}</a>
         <div class="muted">r/${esc(t.subreddit)}</div></td>
         <td class="num">${fmt(t.a_n)}</td><td class="num">${fmt(t.b_n)}</td></tr>`).join('');
      return `<h4>Shared subreddits (${fmt(r.subs.length)})</h4>
        <p class="muted">posts + comments by each account</p>
        <table class="plain">${head}<tbody>${subs ||
          '<tr><td class="muted">none</td></tr>'}</tbody></table>
        <h4>Co-commented threads (${fmt(r.threads.length)})</h4>
        <p class="muted">comments by each account</p>
        <table class="plain">${head}<tbody>${threads ||
          '<tr><td class="muted">none</td></tr>'}</tbody></table>`;
    });
}
function setTab(kind){
  cur.kind = kind; cur.offset = 0;
  $('#tab-posts').classList.toggle('active', kind==='posts');
  $('#tab-comments').classList.toggle('active', kind==='comments');
}
$('#tab-posts').onclick = () => { setTab('posts'); loadContent(); };
$('#tab-comments').onclick = () => { setTab('comments'); loadContent(); };

function renderItem(kind, it){
  const head = `<div class="meta">r/${esc(it.subreddit)} · <b>${fmt(it.score)}</b> pts · ${day(it.created_utc)}</div>`;
  if(kind==='comments')
    return `<div class="item">${head}<div class="txt">${esc(it.body||'')}</div></div>`;
  return `<div class="item">${head}
    <div class="title">${esc(it.title||'(link)')}</div>
    ${it.selftext ? `<div class="txt">${esc(it.selftext)}</div>` : ''}
    ${it.url ? `<div><a href="${esc(it.url)}" target="_blank">${esc(it.url)}</a></div>` : ''}</div>`;
}

async function loadContent(){
  $('#d-user').textContent = cur.user;
  const q = `/api/content?u=${encodeURIComponent(cur.user)}&kind=${cur.kind}&limit=${cur.limit}&offset=${cur.offset}`;
  const r = await getJSON(q);
  const from = r.total ? r.offset+1 : 0, to = Math.min(r.offset+r.limit, r.total);
  $('#d-body').innerHTML =
    (r.items.map(it => renderItem(r.kind, it)).join('') ||
      '<p class="muted">Nothing here.</p>') +
    `<div class="pager">
       <button id="p-prev" ${r.offset<=0?'disabled':''}>‹ prev</button>
       <button id="p-next" ${to>=r.total?'disabled':''}>next ›</button>
       <span class="muted">${from}–${to} of ${fmt(r.total)}</span></div>`;
  $('#p-prev').onclick = () => { cur.offset=Math.max(0,cur.offset-cur.limit); loadContent(); };
  $('#p-next').onclick = () => { cur.offset+=cur.limit; loadContent(); };
  $('#d-body').scrollTo(0,0);
}
document.onkeydown = e => { if(e.key==='Escape') $('#drawer').classList.remove('open'); };

// ---- boot ----
(async () => {
  try {
    await loadOverview();
    // The heatmap's account order is every matrix's column order.
    const accounts = await loadPairs();
    await Promise.all([
      loadAccounts(), loadMentions(accounts),
      loadSubreddits(accounts), loadThreads(accounts)]);
  } catch (e) { document.body.insertAdjacentHTML('afterbegin',
    `<p class="warn">${esc(e.message)}</p>`); }
})();
</script>
</body>
</html>
"""

# The page carries the same single accent as every redlens report; injected
# from constants so the two can't drift.
_ACCENT_RGB = ",".join(
    str(int(constants.ACCENT[i:i + 2], 16)) for i in (1, 3, 5))
INDEX_HTML = _PAGE.replace("$ACCENT_RGB", _ACCENT_RGB).replace(
    "$ACCENT", constants.ACCENT)
