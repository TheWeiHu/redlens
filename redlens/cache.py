"""Read-through cache for a topic's expensive LLM renders.

Persists each render in the :class:`~redlens.models.TopicCache` table, keyed by
a *data-version* (see :func:`redlens.topics.topic_data_version`) and read back
while the version still matches. The version is keyless, so a cached render
never touches the model. ``get`` returns the raw JSON payload or ``None`` on a
miss; ``put`` upserts the single live row; ``invalidate`` drops a topic's rows.
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
    """Store ``payload`` as the live cache row for ``(topic_id, kind, variant)``,
    replacing any existing row for that key. Commits, since the page sections
    each run in their own short-lived read session."""
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
