"""Read-through cache for a topic's expensive LLM renders.

The topic narrative (``summarize_topic``) and the daily-sentiment series
(``daily_topic_sentiment``) are LLM-scored and used to be recomputed from
scratch on every ``page`` render — re-sending identical data to the model and
re-paying even when nothing about the topic changed. This module persists each
result in the :class:`~redlens.models.TopicCache` table, keyed by a
*data-version* (see :func:`redlens.topics.topic_data_version`), and reads it
back when the version still matches.

The shape mirrors the relevance filter's verdict caching (see
``topic-filter-precision``): a soft, additive record that a stale read simply
ignores and a recompute overwrites. The version is computed without an LLM key,
so a cached render never touches the model — exactly the "a deterministic render
shouldn't call the LLM" goal.

``get`` returns the raw JSON payload (the caller deserializes — a pydantic model
or a list of dataclasses), or ``None`` on a miss; ``put`` upserts the single
live row for ``(topic_id, kind, variant)``; ``invalidate`` drops every cached
row for a topic (used by ``untrack``).
"""
from __future__ import annotations

from sqlalchemy import delete
from sqlmodel import Session, col, select

from redlens.models import TopicCache


def get(session: Session, topic_id: int, kind: str, variant: str,
        version: str) -> str | None:
    """The cached payload for ``(topic_id, kind, variant)`` if it was computed
    for ``version``; ``None`` on a miss (no row, or the data has changed since)."""
    row = session.exec(
        select(TopicCache).where(
            TopicCache.topic_id == topic_id,
            TopicCache.kind == kind,
            TopicCache.variant == variant,
        )
    ).first()
    if row is None or row.version != version:
        return None
    return row.payload


def put(session: Session, topic_id: int, kind: str, variant: str,
        version: str, payload: str, model: str) -> None:
    """Store ``payload`` as the live cache row for ``(topic_id, kind, variant)``.

    Replaces any existing row for that key (one live row per flavor — a stale
    version is overwritten, never accumulated) and commits, since the page
    sections each run in their own short-lived read session.
    """
    row = session.exec(
        select(TopicCache).where(
            TopicCache.topic_id == topic_id,
            TopicCache.kind == kind,
            TopicCache.variant == variant,
        )
    ).first()
    if row is None:
        row = TopicCache(topic_id=topic_id, kind=kind, variant=variant)
    row.version = version
    row.payload = payload
    row.model = model
    session.add(row)
    session.commit()


def invalidate(session: Session, topic_id: int) -> None:
    """Drop every cached render for a topic (called when it's untracked)."""
    session.execute(
        delete(TopicCache).where(col(TopicCache.topic_id) == topic_id)
    )
