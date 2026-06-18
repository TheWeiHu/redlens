"""AI profile inference — the "intelligent lens" the project is named for.

``summarize_user`` builds a representative, token-budgeted payload from the
locally archived data (the user's most-active communities + a sample of their
actual posts and comments — content, not raw counts/karma) and asks one LLM to
infer a profile as **structured JSON**: ranked gender/age/country/state/city
guesses (each a confidence + reason), Big Five trait scores, and short
interests/beliefs/tone paragraphs. Returning JSON instead of prose lets
consumers (CLI, ``--json``, an HTML view) render it deterministically. The
result is generated on demand — nothing is persisted (it's cheap and depends
on the changing archive, so there's nothing worth caching).

The sample is **not** the most recent items only: it blends top-by-score
content (most upvoted = most defining, drawn from the whole history) with a
slice of recent activity. How much is sampled is the ``depth`` knob — see
:data:`redlens.constants.SUMMARY_DEPTHS`. The prompt wording lives in
``redlens/prompts/profile.txt``.
"""
from __future__ import annotations

import json
from collections import Counter
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from redlens import constants, llm, prompts
from redlens.config import llm_api_key
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.models import (
    Comment,
    Post,
    Profile,
    Topic,
    TopicPost,
    TopicSummary,
    User,
)
from redlens.topics import get_topic

_Activity = TypeVar("_Activity", Post, Comment)


def summarize_user(session: Session, username: str, *,
                   depth: str | None = None) -> Profile:
    """Infer a profile for ``username`` from their archived activity.

    ``depth`` picks a :data:`~redlens.constants.SUMMARY_DEPTHS` preset
    (default ``standard``). Raises :class:`NotFound` if the user isn't synced,
    :class:`RedlensError` for an unknown depth, and :class:`MissingKey` if no
    LLM key is configured.
    """
    resolved_depth = _resolve_depth(depth)

    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")
    canon = user.username

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_prompt(session, canon, resolved_depth)
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)
    try:
        return Profile.model_validate(
            {"username": canon, "model": llm.model_name(),
             "depth": resolved_depth, **data})
    except ValidationError as exc:
        raise RedlensError(
            f"LLM profile didn't match the expected shape: {exc}") from exc


def summarize_topic(session: Session, name: str, *,
                    depth: str | None = None) -> TopicSummary:
    """Infer a narrative of what tracked topic ``name``'s discussion is about.

    Samples the topic's matched posts/comments (the same top-voted + recent
    blend as :func:`summarize_user`, sized by ``depth``) and asks one LLM for a
    structured summary: an overview, the prominent themes, overall sentiment,
    and where opinion splits. Raises :class:`NotFound` if the topic isn't
    tracked, :class:`RedlensError` for an unknown depth, and :class:`MissingKey`
    if no LLM key is configured.
    """
    resolved_depth = _resolve_depth(depth)

    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_topic_prompt(session, topic, resolved_depth)
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)
    try:
        return TopicSummary.model_validate(
            {"topic": topic.name, "model": llm.model_name(),
             "depth": resolved_depth, **data})
    except ValidationError as exc:
        raise RedlensError(
            f"LLM topic summary didn't match the expected shape: {exc}") from exc


def _resolve_depth(depth: str | None) -> str:
    """Validate an optional ``--depth`` and fall back to the default."""
    if depth is not None and depth not in constants.SUMMARY_DEPTHS:
        raise RedlensError(
            f"unknown depth {depth!r} "
            f"(choose from {', '.join(constants.SUMMARY_DEPTHS)})")
    return depth or constants.SUMMARY_DEFAULT_DEPTH


def _parse_json(raw: str) -> dict[str, Any]:
    """The JSON object from a completion, tolerant of markdown fences/prose
    around it (we take the outermost ``{...}``)."""
    i, j = raw.find("{"), raw.rfind("}")
    if i == -1 or j <= i:
        raise RedlensError("LLM did not return a JSON object")
    try:
        obj = json.loads(raw[i:j + 1])
    except json.JSONDecodeError as exc:
        raise RedlensError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RedlensError("LLM JSON was not an object")
    return obj


def _sample(session: Session, model: type[_Activity], pk_attr: str,
            base: SelectOfScalar[_Activity], n: int) -> list[_Activity]:
    """A representative ``n``-item sample drawn from ``base`` (a select already
    filtered to the membership set — a user's rows, or a topic's matched rows).

    Reserves a recent slice (``SUMMARY_RECENT_FRACTION``) then fills the rest
    top-by-score, deduped — so the result spans the whole history (the
    most-upvoted content) while still reflecting recent activity. Two bounded
    ``LIMIT n`` queries, so it never loads the full archive.
    """
    if n <= 0:
        return []
    by_score = session.exec(
        base.order_by(model.score.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()
    by_recency = session.exec(
        base.order_by(model.created_utc.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()

    recent_quota = max(constants.SUMMARY_MIN_RECENT,
                       round(n * constants.SUMMARY_RECENT_FRACTION))
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
    """Fill ``prompts/profile.txt`` with this user's communities and a
    representative sample of their posts/comments. Deliberately content-first:
    no raw counts/karma/dates (``analytics`` reports those, and feeding them
    here just tempts the model to recite numbers instead of inferring)."""
    preset = constants.SUMMARY_DEPTHS[depth]
    posts = _sample(session, Post, "post_id",
                    select(Post).where(Post.author_username == canon),
                    preset.posts)
    comments = _sample(session, Comment, "comment_id",
                       select(Comment).where(Comment.author_username == canon),
                       preset.comments)

    subs: Counter[str] = Counter(
        session.exec(
            select(Post.subreddit_name).where(Post.author_username == canon)
        ).all()
    )
    subs.update(
        session.exec(
            select(Comment.subreddit_name).where(Comment.author_username == canon)
        ).all()
    )
    # Names only, ranked by activity — the order conveys where they're most at
    # home without putting raw tallies in front of the model.
    communities = ", ".join(
        f"r/{name}" for name, _ in subs.most_common(constants.SUMMARY_TOP_SUBS)
    ) or "—"

    titles = "\n".join(f"- {p.title}" for p in posts if p.title) or "(none)"
    snippets = "\n".join(
        "- " + c.body.strip().replace("\n", " ")[:preset.comment_chars]
        for c in comments if c.body and c.body.strip()
    ) or "(none)"

    return prompts.render(
        "profile",
        username=canon,
        communities=communities,
        post_titles=titles,
        comment_snippets=snippets,
    )


def _build_topic_prompt(session: Session, topic: Topic, depth: str) -> str:
    """Fill ``prompts/topic.txt`` with the topic's keywords, the communities
    discussing it, and a representative sample of its matched posts/comments.

    Membership mirrors the topic page / comment bridge: posts via the
    ``topicpost`` join, comments via ``topicpost.post_id == comment.link_id``.
    Like the profile prompt, it feeds content (titles + snippets), not raw
    stats — ``show --topic`` reports the numbers."""
    topic_id = topic.id
    assert topic_id is not None
    preset = constants.SUMMARY_DEPTHS[depth]
    post_base = (
        select(Post)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id)
    )
    comment_base = (
        select(Comment)
        .join(TopicPost, TopicPost.post_id == Comment.link_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id)
    )
    posts = _sample(session, Post, "post_id", post_base, preset.posts)
    comments = _sample(session, Comment, "comment_id", comment_base, preset.comments)

    subs: Counter[str] = Counter(
        session.exec(
            select(Post.subreddit_name)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic_id)
        ).all()
    )
    communities = ", ".join(
        f"r/{name}" for name, _ in subs.most_common(constants.SUMMARY_TOP_SUBS)
    ) or "—"

    titles = "\n".join(f"- {p.title}" for p in posts if p.title) or "(none)"
    snippets = "\n".join(
        "- " + c.body.strip().replace("\n", " ")[:preset.comment_chars]
        for c in comments if c.body and c.body.strip()
    ) or "(none)"

    return prompts.render(
        "topic",
        topic=topic.name,
        keywords=", ".join(topic.keyword_list) or topic.name,
        communities=communities,
        post_titles=titles,
        comment_snippets=snippets,
    )
