"""Shared types for the sentiment-over-time chart.

Sentiment is LLM-scored — see :func:`redlens.summarize.weekly_topic_sentiment`,
which asks one model call to judge each week's mood, handling the sarcasm and
negation a word lexicon can't ("X no longer works" is negative; "another amazing
feature" may be sarcastic). This module holds only the week-bucket type and the
ISO-week helper that the scorer and the page renderer share.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta


@dataclass(frozen=True)
class WeekSentiment:
    """One UTC ISO-week bucket. ``week`` is the Monday as ``YYYY-MM-DD``,
    ``mean`` the week's sentiment in ``[-1, 1]`` or ``None`` when the week was
    *not scored* (a gap with no activity, or a week the model left out of its
    response) — distinct from a real 0.0 neutral so the chart can skip it rather
    than draw a confident neutral. ``posts``/``comments`` are the counts that
    fell in the week."""
    week: str
    mean: float | None
    posts: int
    comments: int


def _week_start(ts: int) -> str:
    """The Monday (UTC) of ``ts``'s ISO week, as ``YYYY-MM-DD``."""
    d = datetime.fromtimestamp(ts, tz=UTC).date()
    return (d - timedelta(days=d.weekday())).isoformat()
