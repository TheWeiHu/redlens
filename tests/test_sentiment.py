"""Sentiment-over-time chart rendering + the ISO-week helper. Sentiment scoring
itself is LLM-based (see tests/test_summarize.py); this covers the keyless
plumbing the page shares."""
from datetime import UTC, datetime

from redlens.reporting.page import _sentiment_chart
from redlens.sentiment import WeekSentiment, _week_start


def test_week_start_is_the_monday_utc():
    # 2024-01-03 is a Wednesday -> its ISO week starts Mon 2024-01-01.
    ts = int(datetime(2024, 1, 3, 12, 0, tzinfo=UTC).timestamp())
    assert _week_start(ts) == "2024-01-01"


def test_sentiment_chart_renders_both_polarities():
    series = [
        WeekSentiment("2024-01-01", 0.5, 3, 3),
        WeekSentiment("2024-01-08", -0.4, 2, 2),
    ]
    svg = _sentiment_chart(series)
    assert svg.startswith("<svg") and 'class="pos"' in svg and 'class="neg"' in svg
    assert "+0.50 · 3 posts" in svg and "2024-01-08" in svg


def test_sentiment_chart_empty_when_no_signal():
    assert _sentiment_chart([]) == ""
    # posts present but every week neutral -> nothing to show
    assert _sentiment_chart([WeekSentiment("2024-01-01", 0.0, 0, 5)]) == ""
