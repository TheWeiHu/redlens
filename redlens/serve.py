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

import csv
import datetime as _dt
import html
import json
import re
import sqlite3
import sys
import threading
import webbrowser
from collections import Counter
from collections.abc import Callable
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.resources import files
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from redlens import config, constants, llm, prompts

MAX_ROWS = 60        # shared-subreddit / co-commented-thread / mention rows shown
MAX_CONTENT = 100    # account drill-down page cap
MAX_ACCOUNTS = 40    # matrix columns — top accounts by activity
AI_SAMPLE = 20       # posts / comments sampled into the AI-profile prompt
AI_SNIPPET = 240     # chars of a comment fed to the prompt
SUGGEST_MIN_BRANDS = 3  # unlabeled accounts pushing ≥ this many distinct roster
                        # brands are flagged as likely-undetected seeders

# Reddit collapses every deleted account and removed author into these two
# placeholders, so a single "[deleted]" row is really many different people —
# never a real account. Kept in the counts but sorted last + flagged so it
# never reads as the top suspect.
_NON_ACCOUNTS = ("[deleted]", "[removed]")

# All accounts' activity, one row per post/comment (the network's event log).
_ACTIVITY = ("SELECT author_username u, subreddit_name sub FROM post "
             "UNION ALL SELECT author_username, subreddit_name FROM comment")

# Brand-ish term mining (the fallback brand proxy behind /api/mentions when
# no roster file is given).
_TOKEN_RE = re.compile(r"\b[A-Za-z][A-Za-z0-9]{2,}\b")
# outbound domains for the per-cohort catalogue view
_DOMAIN_RE = re.compile(
    r"\b([a-z0-9][a-z0-9\-]{1,30}\.(?:com|net|io|co|app|xyz|ai|org|tv|gg|me|"
    r"shop|store|link|to|vip|club))\b", re.I)
_SKIP_DOMAINS = frozenset({
    "reddit.com", "google.com", "youtube.com", "youtu.be", "facebook.com",
    "amazon.com", "twitter.com", "x.com", "instagram.com", "tiktok.com",
    "medium.com", "whatsapp.com", "gmail.com", "chatgpt.com", "cloudfront.net",
    "vercel.app", "redgifs.com", "imgur.com", "github.com", "apple.com",
    "linkedin.com", "discord.gg", "t.me", "wikipedia.org"})
_CAP_MIN_RATIO = 0.75  # a name is capitalized nearly every time it appears
_SKIP_TERMS = frozenset(constants.data_lines("stopwords.txt")) | frozenset({
    "monday", "tuesday", "wednesday", "thursday", "friday", "saturday",
    "sunday", "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
    "reddit", "redditor", "redditors"})

BrandRoster = list[tuple[str, list[str]]]  # (display name, match terms)
CohortLabels = dict[str, str]              # account -> cohort name


def _csv_rows(path: Path) -> list[list[str]]:
    """Non-empty CSV rows, cells stripped; blank lines and ``#`` comments
    skipped. The shared reader behind the roster and cohort files."""
    out = []
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.reader(fh):
            cells = [c.strip() for c in row if c.strip()]
            if cells and not cells[0].startswith("#"):
                out.append(cells)
    return out


def load_brands(path: Path) -> BrandRoster:
    """Parse a brand-roster CSV into ``(name, terms)`` rows.

    One brand per line: the display name, then the terms that count as a
    mention (``NordVPN, nordvpn, nord vpn``). A name with no terms matches
    itself.
    """
    return [(cells[0], cells[1:] or cells[:1]) for cells in _csv_rows(path)]


def load_cohorts(path: Path) -> CohortLabels:
    """Parse a cohort-labels CSV: ``account, cohort`` per line.

    Cohort names are free-form (``coordinated``, ``organic``, …); accounts
    absent from the file count as unlabeled. File order matters: matrices
    group cohorts in the order they first appear, unlabeled last.
    """
    return {cells[0]: cells[1] for cells in _csv_rows(path) if len(cells) >= 2}


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

    def __init__(self, path: str, roster: BrandRoster | None = None,
                 cohorts: CohortLabels | None = None,
                 promoted: set[str] | None = None) -> None:
        self.path = str(Path(path).resolve())
        self.roster = roster or []
        self.cohorts = cohorts or {}
        # accounts folded into the cohort from a --promote suggestions file
        # (verified seeders, kept separate from hand-labeled ground truth so
        # the UI can mark them); they're already in `cohorts`.
        self.promoted = promoted or set()
        # cohort display order = first appearance in the labels file
        self._cohort_rank: dict[str, int] = {}
        for c in self.cohorts.values():
            self._cohort_rank.setdefault(c, len(self._cohort_rank))
        # The curated network scope: with cohort labels the DB also holds the
        # organic authors that brand-tracking pulled in (thousands), but the
        # coordinated-network matrices describe only the *labeled* accounts —
        # otherwise they'd balloon. Empty scope (no labels) = every author.
        self._scope: list[str] = sorted(self.cohorts)
        self._coordinated: frozenset[str] = frozenset(
            u for u, c in self.cohorts.items() if c == "coordinated")
        # AI profiles cached per server run (the DB is read-only, so no
        # persistence); one LLM call per account per run.
        self._ai_cache: dict[str, dict[str, Any]] = {}
        # Per-(brand, author) mention counts — the shared primitive behind the
        # mentions matrix, share-of-voice, and the suspected-seeder scan. The
        # roster scan is O(texts × brands); computed once here (the DB is
        # read-only) so those three don't each re-scan the whole DB per request.
        self._brand_counts: dict[str, Counter[str]] | None = None
        # first-mention utc per (brand, labeled account), filled by the same
        # scan as _brand_counts; powers seeding_waves at no extra cost.
        self._first_seen: dict[str, dict[str, int]] = {}

    def _scope_clause(self, col: str) -> tuple[str, list[str]]:
        """``(" AND <col> IN (?, …)", params)`` restricting to the curated
        cohort, or ``("", [])`` when unscoped (no labels file). Applied to the
        network matrices so ingested organic authors don't swamp them."""
        if not self._scope:
            return "", []
        return f" AND {col} IN ({','.join('?' * len(self._scope))})", list(
            self._scope)

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
            authors = self._authors(con)
            if self.cohorts:
                # scoped: the headline is the curated cohort; the rest of the
                # DB is the organic pool that brand-tracking pulled in.
                labeled = [u for u in authors if u in self.cohorts]
                out["accounts"] = len(labeled)
                out["organic_authors"] = len(authors) - len(labeled)
                tally = Counter(self.cohorts[u] for u in labeled)
                out["cohorts"] = [
                    {"cohort": c, "accounts": n} for c, n in sorted(
                        tally.items(),
                        key=lambda kv: self._cohort_rank.get(kv[0], 0))
                ]
                out["promoted"] = sum(1 for u in labeled if u in self.promoted)
            else:
                out["accounts"] = len(authors)
            out["brands"] = len(self.roster)   # tracked brand roster size
            return out

    def listening(self) -> dict[str, Any]:
        """The topic-tracking layer fused into the network view: tracked topics
        by share-of-voice (matched-post volume) and the *crossings* — which of
        our accounts show up in which topic. Empty ``topics`` for a
        network-only DB (no tracked topics, e.g. mydata), so the section
        stays hidden and this view is unchanged for coordinated-network work."""
        # Honor the same relevance verdict every other surface applies: a
        # topicpost the LLM filter judged off-topic (relevant = 0/False) is
        # hidden; unscored (NULL) and on-topic (1) rows are kept — sqlite's
        # null-safe `IS NOT 0` mirrors topics.relevant_clause()'s `IS NOT False`.
        # Without this the topic volume + crossings would double-count junk the
        # rest of the app already drops.
        rel = "tp.relevant IS NOT 0"
        with closing(self._conn()) as con:
            topics = [dict(r) for r in con.execute(
                "SELECT t.name AS name, count(tp.post_id) AS matched "
                "FROM topic t LEFT JOIN topicpost tp "
                f"ON tp.topic_id = t.id AND {rel} "
                "GROUP BY t.id ORDER BY matched DESC, t.name"
            ) if r["matched"]]
            if not topics:
                return {"topics": [], "crossings": []}
            total = sum(t["matched"] for t in topics)
            for t in topics:
                t["share"] = round(100 * t["matched"] / total) if total else 0
            # Crossings are scoped to the accounts of interest — the labeled
            # cohort when there is one, else the synced watchlist (the `user`
            # table) — so topic-tracking's thousands of organic authors don't
            # swamp the list.
            watch = self._scope or [
                r[0] for r in con.execute("SELECT username FROM user")]
            marks = ",".join("?" * len(watch))
            crossings = [dict(r) for r in con.execute(
                f"SELECT p.author_username AS account, t.name AS topic, "
                f"count(*) AS n FROM topicpost tp "
                f"JOIN post p ON p.post_id = tp.post_id "
                f"JOIN topic t ON t.id = tp.topic_id "
                f"WHERE p.author_username IN ({marks}) AND {rel} "
                f"GROUP BY p.author_username, t.name ORDER BY n DESC, account",
                watch)] if watch else []
        return {"topics": topics, "crossings": crossings}

    def _authors(self, con: sqlite3.Connection) -> list[str]:
        return [
            r[0] for r in con.execute(
                "SELECT author_username FROM post "
                "UNION SELECT author_username FROM comment ORDER BY 1"
            )
        ]

    def _matrix_accounts(self, con: sqlite3.Connection) -> list[str]:
        """The matrix column order: top accounts by total activity, grouped
        by cohort (labels-file order, unlabeled last) when labels exist."""
        scope, params = self._scope_clause("u")
        rows = [
            r["u"] for r in con.execute(
                f"SELECT u, count(*) n FROM ({_ACTIVITY}) "
                f"WHERE 1=1{scope} GROUP BY u ORDER BY n DESC, u LIMIT ?",
                (*params, MAX_ACCOUNTS),
            )
        ]
        if self.cohorts:
            unlabeled = len(self._cohort_rank)
            rows.sort(key=lambda u: self._cohort_rank.get(
                self.cohorts.get(u, ""), unlabeled))  # stable: activity kept
        return rows

    def accounts(self) -> list[dict[str, Any]]:
        """Per-account volume, karma, active window, and busiest subreddit —
        restricted to the curated cohort when labels exist."""
        scope, params = self._scope_clause("a.u")
        with closing(self._conn()) as con:
            rows = con.execute(
                f"""
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
                WHERE 1=1{scope}
                GROUP BY a.u
                """, params
            ).fetchall()
            top = self._top_subreddit(con)
            out = []
            for r in rows:
                d = dict(r)
                d["total"] = d["posts"] + d["comments"]
                d["top_subreddit"] = top.get(d["username"], "")
                d["cohort"] = self.cohorts.get(d["username"], "")
                d["promoted"] = d["username"] in self.promoted
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
                "cohorts": {u: c for u in accounts
                            if (c := self.cohorts.get(u))},
                "total_accounts": len(self._authors(con)),
                "pairs": [{"a": a, "b": b, **v}
                          for (a, b), v in sorted(cells.items())],
            }

    def _texts(self) -> list[sqlite3.Row]:
        """Every account's text, one row per post/comment: ``(u, t, ts)``."""
        with closing(self._conn()) as con:
            return con.execute(
                "SELECT author_username u, coalesce(title,'') || ' ' || "
                "coalesce(selftext,'') t, created_utc ts FROM post "
                "UNION ALL SELECT author_username, coalesce(body,''), created_utc "
                "FROM comment"
            ).fetchall()

    def _roster_counts(self) -> dict[str, Counter[str]]:
        """``{brand -> Counter(author -> # posts/comments mentioning it)}``,
        scanned once and memoized. One pass over the DB text testing every
        roster pattern, so ``mentions``/``share_of_voice``/
        ``suggested_coordinated``/``seeding_waves`` share the scan instead of
        each re-running it. The same pass also records the first time each
        *labeled* account mentions a brand (``self._first_seen``), so the
        seeding-wave view costs nothing extra.
        """
        if self._brand_counts is None:
            pats = [(name, _term_pattern(terms)) for name, terms in self.roster]
            counts: dict[str, Counter[str]] = {name: Counter() for name, _ in pats}
            first: dict[str, dict[str, int]] = {name: {} for name, _ in pats}
            labeled = set(self.cohorts)
            for r in self._texts():
                u, text, ts = r["u"], r["t"], r["ts"]
                lab = u in labeled
                for name, pat in pats:
                    if pat.search(text):
                        counts[name][u] += 1
                        if lab and ts is not None:
                            d = first[name]
                            if u not in d or ts < d[u]:
                                d[u] = ts
            self._brand_counts = counts
            self._first_seen = first
        return self._brand_counts

    def mentions(self) -> dict[str, Any]:
        """Brand/name mentions per account, for the mention matrix.

        With a roster (``brands.csv`` / ``--brands``) the counting is exact:
        deterministic, case-insensitive, whole-word over each brand's terms —
        a mention is a post/comment that matches. Without one it falls back
        to mined proper names (see ``_mined_mentions``).
        """
        return self._roster_mentions() if self.roster else self._mined_mentions()

    def _roster_mentions(self) -> dict[str, Any]:
        rows: list[dict[str, Any]] = []
        for name, cells in self._roster_counts().items():
            if not cells:
                continue
            rows.append({"term": name, "accounts": len(cells),
                         "uses": sum(cells.values()), "cells": dict(cells)})
        rows.sort(key=lambda r: (-r["accounts"], -r["uses"],
                                 str(r["term"]).lower()))
        return {"source": "roster", "total": len(rows), "rows": rows[:MAX_ROWS]}

    def share_of_voice(self) -> dict[str, Any]:
        """Per roster brand, the coordinated cohort's share of its Reddit
        conversation: mentions by ``coordinated``-labeled accounts ÷ all
        mentions. Meaningful only once brand-tracking has pulled organic
        discussion into the DB (before that every brand is 100% coordinated,
        since the DB holds only the seeders). Ranked most-coordinated first —
        the brands the network most dominates float to the top.

        Needs both a roster (which brands) and cohort labels (who is
        coordinated); returns an empty set if either is missing.
        """
        if not self.roster or not self._coordinated:
            return {"available": False, "rows": []}
        # A share is only meaningful once the brand's ORGANIC conversation is
        # in the DB (the brand was tracked as a topic, or organic authors
        # mention it). Without that baseline, "100% coordinated" would just
        # mean "we only archived the seeders" — flag those rows instead.
        with closing(self._conn()) as con:
            tracked = {r[0].lower() for r in con.execute(
                "SELECT name FROM topic")}
        rows: list[dict[str, Any]] = []
        for name, cells in self._roster_counts().items():
            coord = Counter({u: n for u, n in cells.items()
                             if u in self._coordinated})
            org = Counter({u: n for u, n in cells.items()
                           if u not in self._coordinated})
            total = sum(coord.values()) + sum(org.values())
            if not total:
                continue
            baseline = bool(org) or name.lower() in tracked
            coord_pct = round(100 * sum(coord.values()) / total)
            # Live verdict (no external data): with a real organic baseline, a
            # brand the network overwhelmingly owns is SEEDED; one with healthy
            # independent discussion it merely name-drops is CAMOUFLAGE. When
            # Firecrawl/other verdicts are integrated they'd override this.
            if baseline and len(coord) >= 3 and coord_pct >= 90 and len(org) <= 2:
                verdict = "seeded"
            elif baseline and len(org) >= 5 and coord_pct <= 60:
                verdict = "camouflage"
            else:
                verdict = ""
            rows.append({
                "term": name,
                "baseline": baseline,
                "verdict": verdict,
                "total": total,
                "coordinated": sum(coord.values()),
                "organic": sum(org.values()),
                "coord_pct": coord_pct,
                "coord_authors": len(coord),
                "organic_authors": len(org),
                "top_coordinated": [u for u, _ in coord.most_common(10)],
                "top_organic": [u for u, _ in org.most_common(10)],
            })
        rows.sort(key=lambda r: (not r["baseline"], -r["coord_pct"],
                                 -int(r["coordinated"]), str(r["term"]).lower()))
        return {"available": True,
                "coordinated_accounts": len(self._coordinated),
                "total": len(rows), "rows": rows[:MAX_ROWS]}

    def suggested_coordinated(self) -> dict[str, Any]:
        """Unlabeled accounts that push many distinct roster brands — likely
        undetected seeders. The keyless verification signal behind the "these
        49 organic authors" question: a genuine organic user mentions a brand
        or two they actually use; a seeder pushes a catalog. An account not in
        ``cohorts.csv`` mentioning ≥ ``SUGGEST_MIN_BRANDS`` distinct roster
        brands (across whatever history is synced) is surfaced for review —
        confirm from its profile, then add it to ``cohorts.csv``.

        Only as strong as the archived history: an account with only a single
        pulled post can't show breadth. Sync the pool's full histories first
        (author-scoped) for this to bite. Needs a roster and cohort labels.
        """
        if not self.roster or not self.cohorts:
            return {"available": False, "rows": []}
        brands_by: dict[str, set[str]] = {}
        mentions_by: Counter[str] = Counter()
        for name, cells in self._roster_counts().items():
            for u, n in cells.items():
                if u in self.cohorts:    # already labeled — not a suggestion
                    continue
                brands_by.setdefault(u, set()).add(name)
                mentions_by[u] += n
        # '[deleted]'/'[removed]' are many people merged under one name, not a
        # real account — keep the row (its brand breadth is real signal) but
        # sort it below every genuine account and flag it.
        flagged = sorted(
            (u for u, bs in brands_by.items() if len(bs) >= SUGGEST_MIN_BRANDS),
            key=lambda u: (u in _NON_ACCOUNTS, -len(brands_by[u]),
                           -mentions_by[u], u))
        rows: list[dict[str, Any]] = [
            {"account": u, "brands": sorted(brands_by[u]),
             "brand_count": len(brands_by[u]), "mentions": mentions_by[u],
             "placeholder": u in _NON_ACCOUNTS}
            for u in flagged
        ]
        return {"available": True, "threshold": SUGGEST_MIN_BRANDS,
                "total": len(rows), "rows": rows[:MAX_ROWS]}

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

    def _cells(self, con: sqlite3.Connection, sql: str, keys: list[str],
               extra: list[str] | None = None) -> dict[str, dict[str, int]]:
        """Per-(row, account) matrix cells for the rows a section shows.

        ``sql`` must select ``k`` (the row key), ``u`` and ``n``, with an
        ``IN ({ph})`` placeholder for ``keys``; ``extra`` are any trailing
        params (e.g. a cohort scope) bound after ``keys``.
        """
        cells: dict[str, dict[str, int]] = {k: {} for k in keys}
        if keys:
            ph = ",".join("?" * len(keys))
            for r in con.execute(sql.format(ph=ph), [*keys, *(extra or [])]):
                cells[r["k"]][r["u"]] = r["n"]
        return cells

    # ---- cohort comparison views (only meaningful with >1 labeled cohort) ----

    def _cohort_names(self) -> list[str]:
        """Distinct cohort labels, in labels-file order."""
        return sorted(set(self.cohorts.values()),
                      key=lambda c: self._cohort_rank.get(c, 0))

    @property
    def multi_cohort(self) -> bool:
        return len(self._cohort_names()) > 1

    def _labeled_first_seen(self) -> dict[str, dict[str, int]]:
        """``{brand -> {account -> first-mention utc}}`` for labeled accounts —
        filled as a side effect of the shared roster scan (no extra pass)."""
        self._roster_counts()
        return self._first_seen

    def cohort_comparison(self) -> dict[str, Any]:
        """Per roster brand, how many accounts in each cohort mention it —
        splitting brands into shared (pushed by ≥2 cohorts) vs cohort-specific.
        The two-operation catalogue view.
        """
        if not self.multi_cohort or not self.roster:
            return {"available": False, "cohorts": [], "rows": []}
        names = self._cohort_names()
        rows: list[dict[str, Any]] = []
        for brand, cells in self._roster_counts().items():
            by = {c: 0 for c in names}
            org = 0
            for u in cells:
                c = self.cohorts.get(u)
                if c in by:
                    by[c] += 1
                elif u not in self.cohorts:
                    org += 1
            present = [c for c in names if by[c]]
            if not present:
                continue
            rows.append({"brand": brand, "by": by, "organic": org,
                         "shared": len(present) > 1,
                         "total": sum(by.values())})
        rows.sort(key=lambda r: (not r["shared"], -r["total"], r["brand"].lower()))
        return {"available": True, "cohorts": names,
                "shared": sum(1 for r in rows if r["shared"]),
                "rows": rows[:MAX_ROWS]}

    def seeding_waves(self, window_days: int = 14) -> dict[str, Any]:
        """Brands that arrive in a *wave* — ≥3 labeled accounts first mentioning
        the brand within ``window_days`` of each other. Organic brands trickle;
        seeded ones cascade.
        """
        if not self.multi_cohort or not self.roster:
            return {"available": False, "rows": []}
        span = window_days * 86400
        waves: list[dict[str, Any]] = []
        for brand, seen in self._labeled_first_seen().items():
            if len(seen) < 3:
                continue
            pts = sorted(seen.items(), key=lambda x: x[1])
            best: dict[str, Any] | None = None
            for i in range(len(pts)):
                j = i
                while j + 1 < len(pts) and pts[j + 1][1] - pts[i][1] <= span:
                    j += 1
                if j - i + 1 >= 3 and (best is None or j - i + 1 > best["n"]):
                    best = {"n": j - i + 1, "start": pts[i][1], "end": pts[j][1],
                            "accounts": [a for a, _ in pts[i:j + 1]]}
            if best:
                best.update(brand=brand, total=len(pts),
                            cohorts=sorted({self.cohorts[a] for a in best["accounts"]
                                            if a in self.cohorts}))
                waves.append(best)
        waves.sort(key=lambda w: -w["n"])
        return {"available": True, "window_days": window_days,
                "rows": waves[:MAX_ROWS]}

    def coordination_raster(self, top_brands: int = 12) -> dict[str, Any]:
        """The coordination raster: one row per labeled account (y), time (x),
        a dot each time an account *first* pushes one of the top brands, dot
        colour = brand. A coordinated push shows as a vertical band — many
        accounts, one colour, one narrow window. Limited to the widest-spread
        brands so the colour legend stays legible.
        """
        if not self.multi_cohort or not self.roster:
            return {"available": False, "accounts": [], "brands": [], "events": []}
        first = self._labeled_first_seen()          # {brand: {account: first ts}}
        ranked = sorted(((b, a) for b, a in first.items() if len(a) >= 3),
                        key=lambda kv: -len(kv[1]))
        brands = [b for b, _ in ranked[:top_brands]]
        if not brands:
            return {"available": False, "accounts": [], "brands": [], "events": []}
        acct_first: dict[str, int] = {}
        for b in brands:
            for a, ts in first[b].items():
                acct_first[a] = min(acct_first.get(a, ts), ts)
        # order accounts by cohort, then by when they first appear (so the
        # earliest pushers sit together and bands read top-to-bottom)
        accounts = sorted(acct_first, key=lambda a: (
            self._cohort_rank.get(self.cohorts.get(a, ""), 99), acct_first[a]))
        aidx = {a: i for i, a in enumerate(accounts)}
        bidx = {b: i for i, b in enumerate(brands)}
        events = [{"a": aidx[a], "b": bidx[b], "ts": ts}
                  for b in brands for a, ts in first[b].items()]
        return {"available": True,
                "accounts": [{"name": a, "cohort": self.cohorts.get(a, "")}
                             for a in accounts],
                "brands": brands, "events": events}

    def cohort_timeline(self) -> dict[str, Any]:
        """Monthly post+comment volume per cohort — the activity lifecycle."""
        if not self.multi_cohort:
            return {"available": False, "cohorts": [], "months": [], "series": {}}
        names = self._cohort_names()
        series: dict[str, Counter[str]] = {c: Counter() for c in names}
        with closing(self._conn()) as con:
            for r in con.execute(
                "SELECT author_username u, created_utc ts FROM post "
                "UNION ALL SELECT author_username, created_utc FROM comment"):
                c = self.cohorts.get(r["u"])
                if c and r["ts"]:
                    ym = _dt.datetime.fromtimestamp(
                        r["ts"], _dt.UTC).strftime("%Y-%m")
                    series[c][ym] += 1
        months = sorted({m for s in series.values() for m in s})
        return {"available": True, "cohorts": names, "months": months,
                "series": {c: [series[c].get(m, 0) for m in months]
                           for c in names}}

    def cohort_bridges(self) -> dict[str, Any]:
        """Cross-cohort links: pairs of labeled accounts in *different* cohorts
        that comment in the same threads — the accounts stitching two operations
        together. Plus the subreddits both cohorts work.
        """
        if not self.multi_cohort:
            return {"available": False, "edges": [], "shared_subs": []}
        labeled = sorted(self.cohorts)
        ph = ",".join("?" * len(labeled))
        edges: list[dict[str, Any]] = []
        with closing(self._conn()) as con:
            for r in con.execute(
                f"""WITH ut AS (SELECT DISTINCT author_username u, link_id t
                               FROM comment WHERE author_username IN ({ph}))
                    SELECT a.u ua, b.u ub, count(*) n
                    FROM ut a JOIN ut b ON a.t = b.t AND a.u < b.u
                    GROUP BY ua, ub""", labeled):
                ca, cb = self.cohorts.get(r["ua"]), self.cohorts.get(r["ub"])
                if ca != cb:
                    edges.append({"a": r["ua"], "b": r["ub"],
                                  "coh_a": ca, "coh_b": cb, "shared": r["n"]})
            edges.sort(key=lambda e: -e["shared"])
            # subreddits where >1 cohort is active
            sub_coh: dict[str, dict[str, int]] = {}
            for r in con.execute(
                f"SELECT DISTINCT u, sub FROM ({_ACTIVITY}) WHERE u IN ({ph})",
                labeled):
                c = self.cohorts.get(r["u"])
                if c:
                    sub_coh.setdefault(r["sub"], {})[c] = \
                        sub_coh.setdefault(r["sub"], {}).get(c, 0) + 1
        shared_subs: list[dict[str, Any]] = [
            {"sub": s, "by": d} for s, d in sub_coh.items() if len(d) > 1]
        shared_subs.sort(key=lambda r: -min(r["by"].values()))
        return {"available": True, "cohorts": self._cohort_names(),
                "edges": edges[:MAX_ROWS], "shared_subs": shared_subs[:MAX_ROWS]}

    def domain_catalogue(self) -> dict[str, Any]:
        """Outbound domains each cohort links to — the products it pushes, by
        cohort. Domains from post URLs and from links in text.
        """
        if not self.multi_cohort:
            return {"available": False, "cohorts": [], "rows": []}
        names = self._cohort_names()
        labeled = set(self.cohorts)
        dom: dict[str, dict[str, set[str]]] = {}
        with closing(self._conn()) as con:
            rows = con.execute(
                "SELECT author_username u, coalesce(url,'') || ' ' || "
                "coalesce(selftext,'') t FROM post "
                "UNION ALL SELECT author_username, coalesce(body,'') FROM comment"
            ).fetchall()
        for r in rows:
            c = self.cohorts.get(r["u"])
            if r["u"] not in labeled or not c:
                continue
            for m in _DOMAIN_RE.finditer(r["t"]):
                d = m.group(1).lower()
                if d in _SKIP_DOMAINS:
                    continue
                dom.setdefault(d, {n: set() for n in names})[c].add(r["u"])
        rows_out: list[dict[str, Any]] = []
        for d, by in dom.items():
            counts = {c: len(by[c]) for c in names}
            if sum(counts.values()) >= 2:
                rows_out.append({"domain": d, "by": counts,
                                 "total": sum(counts.values())})
        rows_out.sort(key=lambda r: -r["total"])
        return {"available": True, "cohorts": names, "rows": rows_out[:MAX_ROWS]}

    def subreddits(self) -> dict[str, Any]:
        """Shared-subreddit footprint: subs where ≥2 accounts are active.

        Long tails are common (a real network shares hundreds of subs), so this
        returns the ``MAX_ROWS`` widest-shared plus ``total`` for a "top N of M"
        caption. Each row carries per-account activity ``cells`` for the matrix.
        """
        scope, params = self._scope_clause("u")
        with closing(self._conn()) as con:
            total = con.execute(
                f"""
                SELECT count(*) FROM (
                  SELECT sub FROM ({_ACTIVITY})
                  WHERE 1=1{scope}
                  GROUP BY sub
                  HAVING count(DISTINCT u) >= 2)
                """, params
            ).fetchone()[0]
            rows = con.execute(
                f"""
                SELECT sub                              AS subreddit,
                       count(DISTINCT u)                AS accounts,
                       sum(kind = 'post')               AS posts,
                       sum(kind = 'comment')            AS comments
                FROM (
                  SELECT author_username u, subreddit_name sub, 'post' kind
                  FROM post
                  UNION ALL
                  SELECT author_username, subreddit_name, 'comment' FROM comment)
                WHERE 1=1{scope}
                GROUP BY sub
                HAVING accounts >= 2
                ORDER BY accounts DESC, (posts + comments) DESC, subreddit
                LIMIT ?
                """,
                (*params, MAX_ROWS),
            ).fetchall()
            out = [dict(r) for r in rows]
            cscope, cparams = self._scope_clause("u")
            cells = self._cells(
                con,
                f"SELECT sub AS k, u, count(*) n FROM ({_ACTIVITY}) "
                "WHERE sub IN ({ph})" + cscope + " GROUP BY sub, u",
                [d["subreddit"] for d in out], cparams)
            for d in out:
                d["cells"] = cells[d["subreddit"]]
            return {"total": total, "rows": out}

    def threads(self) -> dict[str, Any]:
        """Threads (``link_id``) commented in by ≥2 accounts — co-activity."""
        scope, params = self._scope_clause("author_username")
        with closing(self._conn()) as con:
            total = con.execute(
                f"""
                SELECT count(*) FROM (
                  SELECT link_id FROM comment
                  WHERE 1=1{scope}
                  GROUP BY link_id
                  HAVING count(DISTINCT author_username) >= 2)
                """, params
            ).fetchone()[0]
            rows = con.execute(
                f"""
                SELECT link_id                          AS link_id,
                       subreddit_name                   AS subreddit,
                       count(DISTINCT author_username)  AS accounts,
                       count(*)                         AS comments
                FROM comment
                WHERE 1=1{scope}
                GROUP BY link_id
                HAVING accounts >= 2
                ORDER BY accounts DESC, comments DESC
                LIMIT ?
                """,
                (*params, MAX_ROWS),
            ).fetchall()
            out = [dict(r) for r in rows]
            cscope, cparams = self._scope_clause("author_username")
            cells = self._cells(
                con,
                "SELECT link_id AS k, author_username u, count(*) n "
                "FROM comment WHERE link_id IN ({ph})" + cscope + " "
                "GROUP BY link_id, author_username",
                [d["link_id"] for d in out], cparams)
            for d in out:
                d["cells"] = cells[d["link_id"]]
                title = con.execute(
                    "SELECT title FROM post WHERE post_id = ?", (d["link_id"],)
                ).fetchone()
                d["title"] = title[0] if title and title[0] else ""
            return {"total": total, "rows": out}

    def profile(self, username: str) -> dict[str, Any]:
        """One account's profile view: identity stats, where it is active,
        and its top co-actors (the accounts it shares subs/threads with)."""
        with closing(self._conn()) as con:
            row = con.execute(
                """
                SELECT sum(kind = 'post')    AS posts,
                       sum(kind = 'comment') AS comments,
                       min(t)                AS first_utc,
                       max(t)                AS last_utc,
                       count(DISTINCT sub)   AS subreddits
                FROM (SELECT author_username u, subreddit_name sub,
                             created_utc t, 'post' kind FROM post
                      UNION ALL
                      SELECT author_username, subreddit_name, created_utc,
                             'comment' FROM comment)
                WHERE u = ?
                """, (username,)).fetchone()
            if row["posts"] is None:
                raise ValueError(f"unknown account: {username}")
            karma = con.execute(
                "SELECT post_karma, comment_karma FROM user "
                "WHERE username = ?", (username,)).fetchone()
            subs = con.execute(
                """
                SELECT sub                  AS subreddit,
                       sum(kind = 'post')   AS posts,
                       sum(kind = 'comment') AS comments
                FROM (SELECT author_username u, subreddit_name sub,
                             'post' kind FROM post
                      UNION ALL
                      SELECT author_username, subreddit_name, 'comment'
                      FROM comment)
                WHERE u = ?
                GROUP BY sub
                ORDER BY (posts + comments) DESC, sub LIMIT ?
                """, (username, MAX_ROWS)).fetchall()
            co: dict[str, dict[str, int]] = {}
            for u, n in con.execute(
                f"""
                SELECT u, count(DISTINCT sub) FROM ({_ACTIVITY})
                WHERE u != ? AND sub IN
                  (SELECT DISTINCT sub FROM ({_ACTIVITY}) WHERE u = ?)
                GROUP BY u
                """, (username, username)):
                co.setdefault(u, {"subs": 0, "threads": 0})["subs"] = n
            for u, n in con.execute(
                """
                SELECT author_username, count(DISTINCT link_id) FROM comment
                WHERE author_username != ? AND link_id IN
                  (SELECT DISTINCT link_id FROM comment
                   WHERE author_username = ?)
                GROUP BY author_username
                """, (username, username)):
                co.setdefault(u, {"subs": 0, "threads": 0})["threads"] = n
            coactors: list[dict[str, Any]] = [
                {"account": u, **v} for u, v in co.items()]
            coactors.sort(key=lambda c: (-(int(c["subs"]) + int(c["threads"])),
                                         str(c["account"])))
            return {
                "username": username,
                "cohort": self.cohorts.get(username, ""),
                **dict(row),
                "post_karma": karma["post_karma"] if karma else None,
                "comment_karma": karma["comment_karma"] if karma else None,
                "top_subreddits": [dict(r) for r in subs],
                "coactors": coactors[:MAX_ROWS],
            }

    # ---- AI profile: gpt-4o-mini persona + coordinated? verdict ---- #

    def _ai_prompt(self, username: str) -> str:
        """Fill ``prompts/coordination.txt``: the account's sampled content
        plus the deterministic network signals serve already computes — the
        LLM judges, it doesn't recount."""
        p = self.profile(username)  # raises ValueError for unknown accounts
        with closing(self._conn()) as con:
            titles = [r[0] for r in con.execute(
                "SELECT DISTINCT title FROM ("
                "  SELECT title, score, created_utc FROM post"
                "  WHERE author_username = ? AND coalesce(title,'') != ''"
                "  ORDER BY score DESC LIMIT ?)"
                "UNION SELECT title FROM ("
                "  SELECT title, score, created_utc FROM post"
                "  WHERE author_username = ? AND coalesce(title,'') != ''"
                "  ORDER BY created_utc DESC LIMIT ?)",
                (username, AI_SAMPLE, username, AI_SAMPLE))]
            snippets = [r[0] for r in con.execute(
                "SELECT DISTINCT body FROM ("
                "  SELECT body, score, created_utc FROM comment"
                "  WHERE author_username = ? AND coalesce(body,'') != ''"
                "  ORDER BY score DESC LIMIT ?)"
                "UNION SELECT body FROM ("
                "  SELECT body, score, created_utc FROM comment"
                "  WHERE author_username = ? AND coalesce(body,'') != ''"
                "  ORDER BY created_utc DESC LIMIT ?)",
                (username, AI_SAMPLE, username, AI_SAMPLE))]
        signals = [
            f"- volume: {p['posts']} posts, {p['comments']} comments across "
            f"{p['subreddits']} subreddits",
        ]
        for c in p["coactors"][:5]:
            signals.append(
                f"- co-activity with u/{c['account']}: active in "
                f"{c['subs']} of the same subreddits, commented in "
                f"{c['threads']} of the same threads")
        brand_rows = [row for row in self.mentions()["rows"]
                      if row["cells"].get(username)]
        if self.roster:
            # Breadth is the strongest cheap tell: organic accounts mention a
            # couple of tracked brands at most; seeders push dozens.
            signals.append(
                f"- mentions {len(brand_rows)} distinct brands from the "
                f"tracked roster of {len(self.roster)}")
        for row in brand_rows[:12]:
            n = row["cells"][username]
            signals.append(
                f"- mentions \"{row['term']}\" in {n} posts/comments "
                f"(a brand {row['accounts']} tracked accounts mention)")
        if len(brand_rows) > 12:
            signals.append(
                f"- … plus {len(brand_rows) - 12} more tracked brands")
        communities = ", ".join(
            f"r/{s['subreddit']}" for s in p["top_subreddits"][:10]) or "—"
        return prompts.render(
            "coordination",
            username=username,
            communities=communities,
            post_titles="\n".join(f"- {t}" for t in titles) or "(none)",
            comment_snippets="\n".join(
                "- " + s.strip().replace("\n", " ")[:AI_SNIPPET]
                for s in snippets) or "(none)",
            signals="\n".join(signals),
        )

    def ai_profile(self, username: str) -> dict[str, Any]:
        """LLM persona + promotional-behavior read + ``coordinated?`` verdict
        for one account, cached per server run. Raises ``MissingKey`` when no
        LLM key is configured (the report stays fully keyless without it)."""
        if username in self._ai_cache:
            return self._ai_cache[username]
        key = config.require_llm_key()
        data = llm.complete_json(self._ai_prompt(username), key)
        verdict = data.get("coordinated") or {}
        out = {
            "username": username,
            "model": llm.model_name(),
            "persona": str(data.get("persona", "")),
            "promotion": str(data.get("promotion", "")),
            "coordinated": {
                "verdict": str(verdict.get("verdict", "uncertain")),
                "confidence": int(verdict.get("confidence") or 0),
                "reason": str(verdict.get("reason", "")),
            },
        }
        self._ai_cache[username] = out
        return out

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
    roster = load_brands(brands_path) if brands_path else []
    labels = load_cohorts(cohorts_path) if cohorts_path else {}
    # --promote folds a reviewed suggestions file (account, cohort, …) into the
    # cohort: verified-but-not-hand-labeled seeders join the coordinated set so
    # scoping + share-of-voice reflect the real network, without editing the
    # ground-truth cohorts.csv. Kept separate so the UI can mark them.
    promoted_labels = load_cohorts(promote_path) if promote_path else {}
    labels = {**labels, **promoted_labels}    # promotions win on conflict

    net = Network(str(db), roster=roster, cohorts=labels,
                  promoted=set(promoted_labels))
    net.overview()  # fail fast if the DB is missing or unreadable
    if roster:
        print(f"brand roster: {len(roster)} brands from {brands_path}")
    if labels:
        print(f"cohort labels: {len(labels)} accounts"
              + (f" ({len(promoted_labels)} promoted from {promote_path})"
                 if promoted_labels else ""))

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
