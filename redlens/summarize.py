"""AI profile summaries — the "intelligent lens" the project is named for.

``summarize_user`` builds a representative, token-budgeted payload from the
locally archived data (the :class:`UserAnalytics` rollup + most-active
subreddits + a sample of the user's posts and comments) and asks one LLM for a
prose summary, cached in the ``summary`` table. The sample is **not** the most
recent items only: it blends top-by-score content (most upvoted = most
defining, drawn from the whole history) with a slice of recent activity, so a
prolific user's older, defining posts aren't invisible. How much is sampled is
the ``depth`` knob (``quick``/``standard``/``deep`` — see
:data:`redlens.constants.SUMMARY_DEPTHS`); even ``deep`` stays well under the
smallest provider context window, and the whole archive is never shipped raw.
"""
from __future__ import annotations

from collections import Counter
from typing import TypeVar

from sqlalchemy import func
from sqlmodel import Session, select

from redlens import constants, llm
from redlens.analytics import compute_user_analytics
from redlens.config import llm_api_key
from redlens.db import upsert
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.models import Comment, Post, Summary, User

_Activity = TypeVar("_Activity", Post, Comment)


def summarize_user(session: Session, username: str, *, refresh: bool = False,
                   depth: str | None = None) -> Summary:
    """Return a cached or freshly generated profile summary for ``username``.

    ``depth`` picks a :data:`~redlens.constants.SUMMARY_DEPTHS` preset. When it
    is ``None`` the cached row is returned as-is (any depth); an explicit depth
    that differs from the cached row regenerates at that depth. ``refresh``
    forces regeneration, keeping the prior depth unless one is given.

    Raises :class:`NotFound` if the user isn't synced, :class:`RedlensError`
    for an unknown depth, and :class:`MissingKey` if a summary must be
    generated but no LLM key is configured.
    """
    if depth is not None and depth not in constants.SUMMARY_DEPTHS:
        raise RedlensError(
            f"unknown depth {depth!r} "
            f"(choose from {', '.join(constants.SUMMARY_DEPTHS)})")

    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")
    canon = user.username

    cached = session.get(Summary, canon)
    if cached is not None and not refresh and (depth is None or depth == cached.depth):
        return cached

    # No explicit depth: on refresh keep what was used before, else default.
    resolved_depth = depth or (cached.depth if cached else None) \
        or constants.SUMMARY_DEFAULT_DEPTH

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "ANTHROPIC_API_KEY / OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_prompt(session, canon, resolved_depth)
    _, model = llm.provider_and_model(key)
    text = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS).strip()

    summary = Summary(username=canon, model=model, depth=resolved_depth, text=text)
    upsert(session, [summary])  # overwrites the prior row in place
    session.commit()
    return summary


def _sample(session: Session, model: type[_Activity], pk_attr: str,
            canon: str, n: int) -> list[_Activity]:
    """A representative ``n``-item sample of ``model`` rows for ``canon``.

    Reserves a recent slice (``SUMMARY_RECENT_FRACTION``) then fills the rest
    top-by-score, deduped — so the result spans the user's whole history
    (their most-upvoted content) while still reflecting recent activity. Two
    bounded ``LIMIT n`` queries, so it never loads the full archive.
    """
    if n <= 0:
        return []
    author = model.author_username
    by_score = session.exec(
        select(model).where(author == canon)
        .order_by(model.score.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()
    by_recency = session.exec(
        select(model).where(author == canon)
        .order_by(model.created_utc.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()

    recent_quota = max(1, round(n * constants.SUMMARY_RECENT_FRACTION))
    chosen: dict[str, _Activity] = {}
    for item in by_recency[:recent_quota]:        # reserve recency slots
        chosen[getattr(item, pk_attr)] = item
    for pool in (by_score, by_recency):           # fill from top-score, then rest
        for item in pool:
            if len(chosen) >= n:
                break
            chosen.setdefault(getattr(item, pk_attr), item)
    # Newest-first for a readable payload.
    return sorted(chosen.values(), key=lambda x: x.created_utc, reverse=True)


def _build_prompt(session: Session, canon: str, depth: str) -> str:
    """A representative, bounded description of the user for the model."""
    posts_n, comments_n, comment_chars = constants.SUMMARY_DEPTHS[depth]
    an = compute_user_analytics(session, canon)

    posts = _sample(session, Post, "post_id", canon, posts_n)
    comments = _sample(session, Comment, "comment_id", canon, comments_n)

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
        "Representative post titles (top-voted and recent):",
    ]
    lines += [f"- {p.title}" for p in posts if p.title] or ["(none)"]
    lines.append("")
    lines.append("Representative comment snippets (top-voted and recent):")
    snippets = [
        "- " + c.body.strip().replace("\n", " ")[:comment_chars]
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
