"""DISCOVER stage: account × account co-activity — the network matrices.

Who shares subreddits and threads with whom (``pairs``), the shared-subreddit
and co-commented-thread footprints (``subreddits`` / ``threads``), and the
exact units behind any matrix cell (``pair_evidence``). Every function takes
the shared :class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from redlens.network.core import (
    _ACTIVITY,
    MAX_ACCOUNTS,
    MAX_ROWS,
    Store,
)


def _matrix_accounts(store: Store, con: sqlite3.Connection) -> list[str]:
    """The matrix column order: top accounts by total activity, grouped
    by cohort (labels-file order, unlabeled last) when labels exist."""
    scope, params = store.scope_clause("u")
    rows = [
        r["u"] for r in con.execute(
            f"SELECT u, count(*) n FROM ({_ACTIVITY}) "
            f"WHERE 1=1{scope} GROUP BY u ORDER BY n DESC, u LIMIT ?",
            (*params, MAX_ACCOUNTS),
        )
    ]
    if store.cohorts:
        unlabeled = len(store._cohort_rank)
        rows.sort(key=lambda u: store._cohort_rank.get(
            store.cohorts.get(u, ""), unlabeled))  # stable: activity kept
    return rows


def _cells(con: sqlite3.Connection, sql: str, keys: list[str],
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


def pairs(store: Store) -> dict[str, Any]:
    """Account × account co-activity — the network-matrix heatmap.

    For each pair among the top ``MAX_ACCOUNTS`` accounts: how many
    subreddits both are active in and how many threads both commented in.
    Also carries the matrix column order every matrix on the page shares.
    """
    with closing(store.conn()) as con:
        accounts = _matrix_accounts(store, con)
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
                        if (c := store.cohorts.get(u))},
            "total_accounts": len(store.authors(con)),
            "pairs": [{"a": a, "b": b, **v}
                      for (a, b), v in sorted(cells.items())],
        }


def subreddits(store: Store) -> dict[str, Any]:
    """Shared-subreddit footprint: subs where ≥2 accounts are active.

    Long tails are common (a real network shares hundreds of subs), so this
    returns the ``MAX_ROWS`` widest-shared plus ``total`` for a "top N of M"
    caption. Each row carries per-account activity ``cells`` for the matrix.
    """
    scope, params = store.scope_clause("u")
    with closing(store.conn()) as con:
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
        cscope, cparams = store.scope_clause("u")
        cells = _cells(
            con,
            f"SELECT sub AS k, u, count(*) n FROM ({_ACTIVITY}) "
            "WHERE sub IN ({ph})" + cscope + " GROUP BY sub, u",
            [d["subreddit"] for d in out], cparams)
        for d in out:
            d["cells"] = cells[d["subreddit"]]
        return {"total": total, "rows": out}


def threads(store: Store) -> dict[str, Any]:
    """Threads (``link_id``) commented in by ≥2 accounts — co-activity."""
    scope, params = store.scope_clause("author_username")
    with closing(store.conn()) as con:
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
        cscope, cparams = store.scope_clause("author_username")
        cells = _cells(
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


def pair_evidence(store: Store, a: str, b: str) -> dict[str, Any]:
    """What entangles two accounts — the exact units the network-matrix
    cell counts: subreddits both are active in and threads both
    commented in, with each side's activity count."""
    with closing(store.conn()) as con:
        subs = con.execute(
            f"""
            SELECT sub                AS subreddit,
                   sum(u = ?)         AS a_n,
                   sum(u = ?)         AS b_n
            FROM ({_ACTIVITY}) WHERE u IN (?, ?)
            GROUP BY sub HAVING a_n > 0 AND b_n > 0
            ORDER BY (a_n + b_n) DESC, subreddit LIMIT ?
            """, (a, b, a, b, MAX_ROWS)).fetchall()
        thread_rows = con.execute(
            """
            SELECT link_id, subreddit_name        AS subreddit,
                   sum(author_username = ?)       AS a_n,
                   sum(author_username = ?)       AS b_n
            FROM comment WHERE author_username IN (?, ?)
            GROUP BY link_id HAVING a_n > 0 AND b_n > 0
            ORDER BY (a_n + b_n) DESC, link_id LIMIT ?
            """, (a, b, a, b, MAX_ROWS)).fetchall()
        out = []
        for r in thread_rows:
            d = dict(r)
            title = con.execute(
                "SELECT title FROM post WHERE post_id = ?", (d["link_id"],)
            ).fetchone()
            d["title"] = title[0] if title and title[0] else ""
            out.append(d)
        return {"subs": [dict(r) for r in subs], "threads": out}
