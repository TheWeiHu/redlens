"""Offline sentiment scoring + weekly bucketing for the sentiment-over-time
chart. Deterministic and keyless — no DB, no network, no LLM."""
from datetime import UTC, datetime

from redlens.reporting.page import _sentiment_chart
from redlens.sentiment import (
    WeekSentiment,
    _lexicon,
    score_text,
    weekly_sentiment,
)


def _ts(y: int, m: int, d: int) -> int:
    return int(datetime(y, m, d, 12, 0, tzinfo=UTC).timestamp())


def test_lexicon_has_known_polarity():
    lex = _lexicon()
    assert lex["good"] > 0 and lex["love"] > 0
    assert lex["bad"] < 0 and lex["broken"] < 0


def test_score_text_polarity():
    assert score_text("this update is great and wonderful") > 0
    assert score_text("this is awful, slow and broken") < 0


def test_score_text_neutral_is_none():
    assert score_text("") is None
    assert score_text("the device connects to the server") is None  # no lexicon hits


def test_score_text_negation_flips():
    pos = score_text("good")
    assert pos is not None and pos > 0
    flipped = score_text("not good")
    assert flipped is not None and flipped < 0


def test_score_text_stays_in_range():
    s = score_text("love love love amazing wonderful excellent")
    assert s is not None and 0 < s <= 1.0


def test_weekly_sentiment_buckets_and_zero_fill():
    items = [
        (_ts(2024, 1, 3), "great and wonderful"),   # week of Mon 2024-01-01
        (_ts(2024, 1, 4), "happy and good"),         # same week
        # 2024-01-08 week deliberately empty -> must zero-fill
        (_ts(2024, 1, 17), "awful and broken"),      # week of Mon 2024-01-15
        (_ts(2024, 1, 18), "the server endpoint"),   # same week, no lexicon hit
    ]
    weeks = weekly_sentiment(items)
    assert [w.week for w in weeks] == ["2024-01-01", "2024-01-08", "2024-01-15"]
    first, gap, last = weeks
    assert first.mean > 0 and first.scored == 2 and first.total == 2
    assert gap.mean == 0.0 and gap.scored == 0 and gap.total == 0  # zero-filled
    assert last.mean < 0 and last.scored == 1 and last.total == 2  # 1 of 2 scored


def test_weekly_sentiment_empty():
    assert weekly_sentiment([]) == []


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
