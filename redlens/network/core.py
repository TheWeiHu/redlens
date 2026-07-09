"""The coordinated-network data layer: the read-only ``Network`` query object.

``Network`` is a thin FACADE — each public method delegates in one line to a
pipeline-stage module (discover → expand → catalogue → verify → profile):

- :mod:`~redlens.network.coactivity` (DISCOVER) — pairs / subreddits / threads
  / pair_evidence / matrix accounts + cells,
- :mod:`~redlens.network.brands` (CATALOGUE) — mentions / share_of_voice,
- :mod:`~redlens.network.seeding` (VERIFY) — seeding waves / raster / cohort
  comparison, timeline, bridges / domain catalogue,
- :mod:`~redlens.network.leads` (EXPAND) — suggested_coordinated,
- :mod:`~redlens.network.profiles` (PROFILE) — profile / ai_profile / content
  / account drill-down items.

The shared state + low-level helpers every stage needs live on :class:`Store`;
overview / listening / accounts stay here as they don't fit a single stage. The
DB is opened read-only; nothing here mutates data and no LLM key is required
for any deterministic view.
"""
from __future__ import annotations

import sqlite3
from collections import Counter
from contextlib import closing
from pathlib import Path
from typing import Any

from redlens.network.rosters import BrandRoster, CohortLabels, _term_pattern

MAX_ROWS = 60        # shared-subreddit / co-commented-thread / mention rows shown
MAX_CONTENT = 100    # account drill-down page cap
MAX_ACCOUNTS = 40    # matrix columns — top accounts by activity

# All accounts' activity, one row per post/comment (the network's event log).
_ACTIVITY = ("SELECT author_username u, subreddit_name sub FROM post "
             "UNION ALL SELECT author_username, subreddit_name FROM comment")


# --------------------------------------------------------------------------- #
# Store: shared state + low-level helpers every pipeline stage needs          #
# --------------------------------------------------------------------------- #

class Store:
    """The shared state + low-level helpers behind every pipeline stage.

    Holds the DB path, the roster + cohort labels + promoted set, and the
    read-only helpers used by two or more stages (scoping, author/text scans,
    the memoized roster-mention counts, cohort ordering). One ``Store`` is
    built per ``Network`` and threaded into each stage function.
    """

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

    def conn(self) -> sqlite3.Connection:
        con = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        return con

    def scope_clause(self, col: str) -> tuple[str, list[str]]:
        """``(" AND <col> IN (?, …)", params)`` restricting to the curated
        cohort, or ``("", [])`` when unscoped (no labels file). Applied to the
        network matrices so ingested organic authors don't swamp them."""
        if not self._scope:
            return "", []
        return f" AND {col} IN ({','.join('?' * len(self._scope))})", list(
            self._scope)

    def authors(self, con: sqlite3.Connection) -> list[str]:
        return [
            r[0] for r in con.execute(
                "SELECT author_username FROM post "
                "UNION SELECT author_username FROM comment ORDER BY 1"
            )
        ]

    def texts(self) -> list[sqlite3.Row]:
        """Every account's text, one row per post/comment: ``(u, t, ts)``."""
        with closing(self.conn()) as con:
            return con.execute(
                "SELECT author_username u, coalesce(title,'') || ' ' || "
                "coalesce(selftext,'') t, created_utc ts FROM post "
                "UNION ALL SELECT author_username, coalesce(body,''), created_utc "
                "FROM comment"
            ).fetchall()

    def roster_counts(self) -> dict[str, Counter[str]]:
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
            for r in self.texts():
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

    def labeled_first_seen(self) -> dict[str, dict[str, int]]:
        """``{brand -> {account -> first-mention utc}}`` for labeled accounts —
        filled as a side effect of the shared roster scan (no extra pass)."""
        self.roster_counts()
        return self._first_seen

    def cohort_names(self) -> list[str]:
        """Distinct cohort labels, in labels-file order."""
        return sorted(set(self.cohorts.values()),
                      key=lambda c: self._cohort_rank.get(c, 0))

    @property
    def multi_cohort(self) -> bool:
        return len(self.cohort_names()) > 1


# --------------------------------------------------------------------------- #
# Network: the read-only facade (every public method delegates in one line)   #
# --------------------------------------------------------------------------- #

class Network:
    """Read-only queries that describe the account network in one DB."""

    def __init__(self, path: str, roster: BrandRoster | None = None,
                 cohorts: CohortLabels | None = None,
                 promoted: set[str] | None = None) -> None:
        self._store = Store(path, roster=roster, cohorts=cohorts,
                            promoted=promoted)

    # ---- shared state exposed for callers + the safety-net tests ---- #

    @property
    def path(self) -> str:
        return self._store.path

    @property
    def roster(self) -> BrandRoster:
        return self._store.roster

    @property
    def cohorts(self) -> CohortLabels:
        return self._store.cohorts

    @property
    def promoted(self) -> set[str]:
        return self._store.promoted

    @property
    def _coordinated(self) -> frozenset[str]:
        return self._store._coordinated

    @property
    def multi_cohort(self) -> bool:
        return self._store.multi_cohort

    # ---- overview / listening / accounts: cross-stage glue on core ---- #

    def overview(self) -> dict[str, Any]:
        store = self._store
        with closing(store.conn()) as con:
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
            authors = store.authors(con)
            if store.cohorts:
                # scoped: the headline is the curated cohort; the rest of the
                # DB is the organic pool that brand-tracking pulled in.
                labeled = [u for u in authors if u in store.cohorts]
                out["accounts"] = len(labeled)
                out["organic_authors"] = len(authors) - len(labeled)
                tally = Counter(store.cohorts[u] for u in labeled)
                out["cohorts"] = [
                    {"cohort": c, "accounts": n} for c, n in sorted(
                        tally.items(),
                        key=lambda kv: store._cohort_rank.get(kv[0], 0))
                ]
                out["promoted"] = sum(1 for u in labeled if u in store.promoted)
            else:
                out["accounts"] = len(authors)
            out["brands"] = len(store.roster)   # tracked brand roster size
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
        store = self._store
        rel = "tp.relevant IS NOT 0"
        with closing(store.conn()) as con:
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
            watch = store._scope or [
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

    def accounts(self) -> list[dict[str, Any]]:
        """Per-account volume, karma, active window, and busiest subreddit —
        restricted to the curated cohort when labels exist."""
        store = self._store
        scope, params = store.scope_clause("a.u")
        with closing(store.conn()) as con:
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
                d["cohort"] = store.cohorts.get(d["username"], "")
                d["promoted"] = d["username"] in store.promoted
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

    # ---- DISCOVER (coactivity) ---- #

    def pairs(self) -> dict[str, Any]:
        return coactivity.pairs(self._store)

    def subreddits(self) -> dict[str, Any]:
        return coactivity.subreddits(self._store)

    def threads(self) -> dict[str, Any]:
        return coactivity.threads(self._store)

    def pair_evidence(self, a: str, b: str) -> dict[str, Any]:
        return coactivity.pair_evidence(self._store, a, b)

    # ---- CATALOGUE (brands) ---- #

    def mentions(self) -> dict[str, Any]:
        return brands.mentions(self._store)

    def share_of_voice(self) -> dict[str, Any]:
        return brands.share_of_voice(self._store)

    # ---- VERIFY (seeding) ---- #

    def seeding_waves(self, window_days: int = 14) -> dict[str, Any]:
        return seeding.seeding_waves(self._store, window_days=window_days)

    def coordination_raster(self, top_brands: int = 12) -> dict[str, Any]:
        return seeding.coordination_raster(self._store, top_brands=top_brands)

    def cohort_comparison(self) -> dict[str, Any]:
        return seeding.cohort_comparison(self._store)

    def cohort_timeline(self) -> dict[str, Any]:
        return seeding.cohort_timeline(self._store)

    def cohort_bridges(self) -> dict[str, Any]:
        return seeding.cohort_bridges(self._store)

    def domain_catalogue(self) -> dict[str, Any]:
        return seeding.domain_catalogue(self._store)

    # ---- EXPAND (leads) ---- #

    def suggested_coordinated(self) -> dict[str, Any]:
        return leads.suggested_coordinated(self._store)

    # ---- PROFILE (profiles) ---- #

    def profile(self, username: str) -> dict[str, Any]:
        return profiles.profile(self._store, username)

    def ai_profile(self, username: str) -> dict[str, Any]:
        return profiles.ai_profile(self._store, username)

    def content(self, username: str, kind: str, *, limit: int,
                offset: int) -> dict[str, Any]:
        return profiles.content(self._store, username, kind,
                                limit=limit, offset=offset)

    def account_sub_items(self, username: str, sub: str) -> dict[str, Any]:
        return profiles.account_sub_items(self._store, username, sub)

    def account_thread_items(self, username: str, link_id: str) -> dict[str, Any]:
        return profiles.account_thread_items(self._store, username, link_id)

    def account_term_items(self, username: str, term: str) -> dict[str, Any]:
        return profiles.account_term_items(self._store, username, term)


# Imported at the bottom to break the cycle: the stage modules import ``Store``
# (and the shared module-level constants) from here.
from redlens.network import (  # noqa: E402
    brands,
    coactivity,
    leads,
    profiles,
    seeding,
)
