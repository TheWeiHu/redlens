"""AI profile summaries — the "intelligent lens" the project is named for.

``summarize_user`` builds a compact, token-budgeted payload from the locally
archived data (the :class:`UserAnalytics` rollup + most-active subreddits + a
bounded sample of recent post titles and comment snippets), asks one LLM for a
prose summary, and caches it in the ``summary`` table. Re-running returns the
cached row unless ``refresh`` regenerates it. The whole archive is never
shipped to the model — only the bounded sample below.
"""
from __future__ import annotations

from collections import Counter

from sqlalchemy import func
from sqlmodel import Session, select

from redlens import constants, llm
from redlens.analytics import compute_user_analytics
from redlens.config import llm_api_key
from redlens.db import upsert
from redlens.errors import MissingKey, NotFound
from redlens.models import Comment, Post, Summary, User


def summarize_user(session: Session, username: str, *,
                   refresh: bool = False) -> Summary:
    """Return a cached or freshly generated profile summary for ``username``.

    Raises :class:`NotFound` if the user isn't synced and :class:`MissingKey`
    if a summary must be generated but no LLM key is configured.
    """
    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")
    canon = user.username

    if not refresh:
        cached = session.get(Summary, canon)
        if cached is not None:
            return cached

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_prompt(session, canon)
    _, model = llm.provider_and_model(key)
    text = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS).strip()

    summary = Summary(username=canon, model=model, text=text)
    upsert(session, [summary])  # overwrites the prior row on --refresh
    session.commit()
    return summary


def _build_prompt(session: Session, canon: str) -> str:
    """A compact, bounded description of the user for the model to summarize."""
    an = compute_user_analytics(session, canon)

    posts = session.exec(
        select(Post)
        .where(Post.author_username == canon)
        .order_by(Post.created_utc.desc())  # type: ignore[attr-defined]
        .limit(constants.SUMMARY_POST_SAMPLE)
    ).all()
    comments = session.exec(
        select(Comment)
        .where(Comment.author_username == canon)
        .order_by(Comment.created_utc.desc())  # type: ignore[attr-defined]
        .limit(constants.SUMMARY_COMMENT_SAMPLE)
    ).all()

    subs = Counter(
        session.exec(
            select(Post.subreddit_name).where(Post.author_username == canon)
        ).all()
    )
    subs.update(
        session.exec(
            select(Comment.subreddit_name).where(Comment.author_username == canon)
        ).all()
    )
    top_subs = ", ".join(
        f"r/{name} ({n})" for name, n in subs.most_common(constants.SUMMARY_TOP_SUBS)
    ) or "—"

    lines = [
        f"Reddit user u/{canon}.",
        f"Activity: {an.total_posts} posts, {an.total_comments} comments across "
        f"{an.distinct_subreddits} subreddits; karma {an.total_karma:+} "
        f"(posts {an.post_karma:+}, comments {an.comment_karma:+}); "
        f"active on {an.active_days} distinct days.",
        f"Most-active subreddits: {top_subs}.",
        "",
        "Recent post titles:",
    ]
    lines += [f"- {p.title}" for p in posts if p.title] or ["(none)"]
    lines.append("")
    lines.append("Recent comment snippets:")
    snippets = [
        "- " + c.body.strip().replace("\n", " ")[:constants.SUMMARY_COMMENT_CHARS]
        for c in comments if c.body and c.body.strip()
    ]
    lines += snippets or ["(none)"]
    lines += [
        "",
        "Write a concise 2-3 paragraph summary of this user's interests, "
        "communities, and posting style, grounded only in the data above. "
        "Do not invent facts or speculate about real-world identity.",
    ]
    return "\n".join(lines)
