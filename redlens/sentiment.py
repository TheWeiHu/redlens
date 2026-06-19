"""Offline, lexicon-based sentiment scoring for the sentiment-over-time chart.

Keyless and deterministic — the same archive always scores the same — so it
fits the topic page's offline-by-default design (no LLM, no network). Each
word's valence comes from the bundled VADER lexicon
(``data/sentiment_lexicon.txt``, MIT). A post's score is the mean valence of
its sentiment-bearing words, normalized to ``[-1, 1]``; a light negation rule
flips a word's sign when a negator precedes it. Posts with no sentiment words
score ``None`` (neutral — excluded from the trend rather than pulling it to 0).

This is a lightweight lexicon scorer, **not** full VADER: no intensifier or
punctuation boosting, just word valence + negation. It's meant for an aggregate
weekly trend over hundreds/thousands of posts, not for judging one post alone.
"""
from __future__ import annotations

import re
from collections import Counter, defaultdict
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from functools import cache
from pathlib import Path

_DATA = Path(__file__).resolve().parent / "data" / "sentiment_lexicon.txt"
_WORD_RE = re.compile(r"[a-z']+")
# Words that negate the sentiment word that follows them (a small, deliberately
# conservative set — VADER's full list, minus rare forms).
_NEGATORS = frozenset({
    "not", "no", "never", "none", "nobody", "nothing", "neither", "nor",
    "cannot", "cant", "without", "hardly", "barely", "rarely", "seldom",
    "aint", "dont", "doesnt", "isnt", "wasnt", "arent", "werent", "wont",
})
_NEG_WINDOW = 3        # a negator this many tokens back flips a word's sign
_VALENCE_MAX = 4.0     # VADER valence spans [-4, 4]; divide to reach [-1, 1]


@cache
def _lexicon() -> dict[str, float]:
    """``word -> valence`` from the bundled lexicon (cached; tab-delimited, with
    ``#`` header lines skipped)."""
    out: dict[str, float] = {}
    for line in _DATA.read_text(encoding="utf-8").splitlines():
        if not line or line.startswith("#"):
            continue
        token, _, score = line.partition("\t")
        if score:
            out[token] = float(score)
    return out


def score_text(text: str) -> float | None:
    """Mean valence of ``text``'s sentiment words, normalized to ``[-1, 1]``, or
    ``None`` when it carries no scored words. A negator within the preceding
    :data:`_NEG_WINDOW` tokens flips the next sentiment word's sign, so
    "not good" reads negative."""
    lex = _lexicon()
    tokens = _WORD_RE.findall(text.lower())
    vals: list[float] = []
    neg_at = -_NEG_WINDOW - 1
    for i, tok in enumerate(tokens):
        if tok in _NEGATORS:
            neg_at = i
            continue
        v = lex.get(tok)
        if v is None:
            continue
        if i - neg_at <= _NEG_WINDOW:
            v = -v
        vals.append(v)
    if not vals:
        return None
    return max(-1.0, min(1.0, (sum(vals) / len(vals)) / _VALENCE_MAX))


@dataclass(frozen=True)
class WeekSentiment:
    """One UTC ISO-week bucket. ``week`` is the Monday as ``YYYY-MM-DD``,
    ``mean`` the average document sentiment in ``[-1, 1]`` (0.0 when nothing in
    the week scored), and ``posts``/``comments`` the counts that fell in it —
    both contribute to ``mean`` when present."""
    week: str
    mean: float
    posts: int
    comments: int


def _week_start(ts: int) -> str:
    """The Monday (UTC) of ``ts``'s ISO week, as ``YYYY-MM-DD``."""
    d = datetime.fromtimestamp(ts, tz=UTC).date()
    return (d - timedelta(days=d.weekday())).isoformat()


def weekly_sentiment(posts: Iterable[tuple[int, str]],
                     comments: Iterable[tuple[int, str]] = ()) -> list[WeekSentiment]:
    """Bucket ``(created_utc, text)`` pairs — posts, and optionally the comments
    under them — into UTC ISO weeks (Monday start), gaps zero-filled, returning
    each week's mean sentiment over its *scored* documents (posts and comments
    together) in chronological order."""
    scores: dict[str, list[float]] = defaultdict(list)
    nposts: Counter[str] = Counter()
    ncomments: Counter[str] = Counter()
    for created_utc, text in posts:
        wk = _week_start(created_utc)
        nposts[wk] += 1
        s = score_text(text)
        if s is not None:
            scores[wk].append(s)
    for created_utc, text in comments:
        wk = _week_start(created_utc)
        ncomments[wk] += 1
        s = score_text(text)
        if s is not None:
            scores[wk].append(s)
    seen = set(nposts) | set(ncomments)
    if not seen:
        return []
    weeks = sorted(seen)
    out: list[WeekSentiment] = []
    cur = datetime.fromisoformat(weeks[0]).date()
    end = datetime.fromisoformat(weeks[-1]).date()
    while cur <= end:
        wk = cur.isoformat()
        vals = scores.get(wk, [])
        mean = sum(vals) / len(vals) if vals else 0.0
        out.append(WeekSentiment(wk, mean, nposts.get(wk, 0), ncomments.get(wk, 0)))
        cur += timedelta(days=7)
    return out
