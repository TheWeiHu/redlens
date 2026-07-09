"""PROFILE stage: one account's view + the on-demand LLM read + drill-downs.

``profile`` is the deterministic per-account rollup (stats, communities,
co-actors); ``ai_profile`` is the single LLM call (persona + coordinated?
verdict), grounded by ``_ai_prompt`` on the profile + roster signals; the
``account_*_items`` / ``content`` helpers are the raw-content drill-downs
behind every matrix cell. All take the shared
:class:`~redlens.network.core.Store`.
"""
from __future__ import annotations

import sqlite3
from contextlib import closing
from typing import Any

from redlens import config, llm, prompts
from redlens.network import brands
from redlens.network.core import _ACTIVITY, MAX_CONTENT, MAX_ROWS, Store
from redlens.network.rosters import _term_pattern

AI_SAMPLE = 20       # posts / comments sampled into the AI-profile prompt
AI_SNIPPET = 240     # chars of a comment fed to the prompt


def profile(store: Store, username: str) -> dict[str, Any]:
    """One account's profile view: identity stats, where it is active,
    and its top co-actors (the accounts it shares subs/threads with)."""
    with closing(store.conn()) as con:
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
            "cohort": store.cohorts.get(username, ""),
            **dict(row),
            "post_karma": karma["post_karma"] if karma else None,
            "comment_karma": karma["comment_karma"] if karma else None,
            "top_subreddits": [dict(r) for r in subs],
            "coactors": coactors[:MAX_ROWS],
        }


def _ai_prompt(store: Store, username: str) -> str:
    """Fill ``prompts/coordination.txt``: the account's sampled content
    plus the deterministic network signals serve already computes — the
    LLM judges, it doesn't recount."""
    p = profile(store, username)  # raises ValueError for unknown accounts
    with closing(store.conn()) as con:
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
    brand_rows = [row for row in brands.mentions(store)["rows"]
                  if row["cells"].get(username)]
    if store.roster:
        # Breadth is the strongest cheap tell: organic accounts mention a
        # couple of tracked brands at most; seeders push dozens.
        signals.append(
            f"- mentions {len(brand_rows)} distinct brands from the "
            f"tracked roster of {len(store.roster)}")
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


def ai_profile(store: Store, username: str) -> dict[str, Any]:
    """LLM persona + promotional-behavior read + ``coordinated?`` verdict
    for one account, cached per server run. Raises ``MissingKey`` when no
    LLM key is configured (the report stays fully keyless without it)."""
    if username in store._ai_cache:
        return store._ai_cache[username]
    key = config.require_llm_key()
    data = llm.complete_json(_ai_prompt(store, username), key)
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
    store._ai_cache[username] = out
    return out


def _items_payload(rows: list[sqlite3.Row]) -> dict[str, Any]:
    items = sorted((dict(r) for r in rows),
                   key=lambda d: -(d["created_utc"] or 0))
    return {"total": len(items), "items": items[:MAX_CONTENT]}


def account_sub_items(store: Store, username: str, sub: str) -> dict[str, Any]:
    """One account's posts + comments in one subreddit (a footprint cell)."""
    with closing(store.conn()) as con:
        rows = con.execute(
            "SELECT 'post' AS kind, subreddit_name AS subreddit, title, "
            "selftext, url, score, created_utc FROM post "
            "WHERE author_username = ? AND subreddit_name = ? "
            "UNION ALL "
            "SELECT 'comment', subreddit_name, NULL, body, NULL, score, "
            "created_utc FROM comment "
            "WHERE author_username = ? AND subreddit_name = ?",
            (username, sub, username, sub)).fetchall()
    return _items_payload(rows)


def account_thread_items(store: Store, username: str,
                         link_id: str) -> dict[str, Any]:
    """One account's comments in one thread (a co-commented cell)."""
    with closing(store.conn()) as con:
        rows = con.execute(
            "SELECT 'comment' AS kind, subreddit_name AS subreddit, "
            "NULL AS title, body AS selftext, NULL AS url, score, "
            "created_utc FROM comment "
            "WHERE author_username = ? AND link_id = ?",
            (username, link_id)).fetchall()
        title = con.execute(
            "SELECT title FROM post WHERE post_id = ?", (link_id,)
        ).fetchone()
    out = _items_payload(rows)
    out["title"] = title[0] if title and title[0] else ""
    return out


def account_term_items(store: Store, username: str,
                       term: str) -> dict[str, Any]:
    """One account's posts + comments mentioning a brand/name (a mention
    cell). ``term`` is a roster name (matched by its terms) or a mined
    term (matched by itself)."""
    terms = next((t for n, t in store.roster if n == term), [term])
    pat = _term_pattern(terms)
    with closing(store.conn()) as con:
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
    return _items_payload(hits)


def content(store: Store, username: str, kind: str, *, limit: int,
            offset: int) -> dict[str, Any]:
    """One account's raw posts or comments, newest first (drill-down)."""
    limit = max(1, min(limit, MAX_CONTENT))
    offset = max(0, offset)
    with closing(store.conn()) as con:
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
