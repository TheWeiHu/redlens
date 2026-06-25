"""Shared types for the sentiment-over-time chart.

Sentiment is LLM-scored — see :func:`redlens.summarize.daily_topic_sentiment`,
which asks one model call to judge each day's mood, handling the sarcasm and
negation a word lexicon can't ("X no longer works" is negative; "another amazing
feature" may be sarcastic). This module holds only the day-bucket type and the
day helper that the scorer and the page renderer share.
"""
from __future__ import annotations

from datetime import UTC, datetime

from pydantic import BaseModel


class DaySentiment(BaseModel):
    """One UTC calendar-day bucket. ``day`` is the date as ``YYYY-MM-DD``,
    ``mean`` the day's sentiment in ``[-1, 1]`` or ``None`` when the day was
    *not scored* (a gap with no activity, or a day the model left out of its
    response) — distinct from a real 0.0 neutral so the chart can skip it rather
    than draw a confident neutral. ``posts``/``comments`` are the counts that
    fell on the day."""
    day: str
    mean: float | None
    posts: int
    comments: int


def _day_start(ts: int) -> str:
    """The UTC calendar day of ``ts``, as ``YYYY-MM-DD``."""
    return datetime.fromtimestamp(ts, tz=UTC).date().isoformat()
