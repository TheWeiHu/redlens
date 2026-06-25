"""Sentiment-over-time chart rendering + the calendar-day helper. Sentiment
scoring itself is LLM-based (see tests/test_summarize.py); this covers the
keyless plumbing the page shares."""
from datetime import UTC, datetime

from redlens.reporting.page import _sentiment_chart
from redlens.sentiment import DaySentiment, _day_start


def test_day_start_is_the_calendar_day_utc():
    ts = int(datetime(2024, 1, 3, 12, 0, tzinfo=UTC).timestamp())
    assert _day_start(ts) == "2024-01-03"


def test_sentiment_chart_renders_both_polarities():
    series = [
        DaySentiment(day="2024-01-01", mean=0.5, posts=3, comments=3),
        DaySentiment(day="2024-01-02", mean=-0.4, posts=2, comments=2),
    ]
    svg = _sentiment_chart(series)
    assert svg.startswith("<svg") and 'class="pos"' in svg and 'class="neg"' in svg
    assert "+0.50 · 3 posts" in svg and "2024-01-02" in svg


def test_sentiment_chart_empty_when_no_signal():
    assert _sentiment_chart([]) == ""
    # posts present but every day neutral -> nothing to show
    assert _sentiment_chart([DaySentiment(day="2024-01-01", mean=0.0, posts=0, comments=5)]) == ""
