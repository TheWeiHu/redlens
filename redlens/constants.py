"""Tunable constants and external endpoints, in one place.

Magic numbers that used to live scattered across the arctic client, the
discovery sources, topic tracking, and the report renderer are collected
here so they're easy to find and adjust. Large, frequently-edited lists
(popular subreddits, stopwords) live as plain-text data files under
``data/`` and are loaded by :func:`data_lines`.
"""
from __future__ import annotations

from pathlib import Path
from typing import NamedTuple

# --- external endpoints -----------------------------------------------------
ARCTIC_BASE = "https://arctic-shift.photon-reddit.com"
PULLPUSH_URL = "https://api.pullpush.io/reddit/search/submission/"
DUCKDUCKGO_URL = "https://html.duckduckgo.com/html/"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"

# --- arctic HTTP client -----------------------------------------------------
HTTP_TIMEOUT_S = 60
PAGINATION_SLEEP_S = 0.25
MAX_RETRIES = 6
BACKOFF_BASE_S = 1.0
# Arctic rate-limits bursts with 429; deep full-text scans intermittently 422
# (transient — succeeds on retry). Retry these and transient 5xx.
RETRYABLE_STATUS = (422, 429, 500, 502, 503, 504)

# --- tracking ---------------------------------------------------------------
COMMIT_BATCH = 500               # flush posts to the DB every N (bounds memory)

# --- discovery --------------------------------------------------------------
DISCOVER_MAX_AUTHORS = 8          # top posters followed out of the seed subs
DISCOVER_MAX_NEW_SUBREDDITS = 12  # new subs one --discover round may add
MAX_WEB_RESULTS = 10              # subreddits taken from a web/global search
MAX_LLM_RESULTS = 10             # subreddits taken from one LLM suggestion call
# Authors that scope to noise rather than people.
NON_AUTHORS = frozenset({"[deleted]", "automoderator", "automod"})

# --- search page sizes ------------------------------------------------------
ARCTIC_PAGE_LIMIT = 100          # max rows per full-text search request
PULLPUSH_SIZE = 100              # rows per PullPush global search
LLM_MAX_TOKENS = 300            # cap on the discovery LLM call

# --- profile summary (BYO LLM key) ------------------------------------------
# Output budget: ~700 tokens is two or three tight paragraphs — enough for a
# qualitative character sketch, short enough to stay cheap and skimmable.
SUMMARY_MAX_TOKENS = 700
# Communities are a qualitative fact about a user (where they choose to spend
# time), so we name their most-active subreddits. Ten is enough to show the
# shape of their participation — a primary home or two plus the long tail —
# without padding the prompt with subreddits they barely touch.
SUMMARY_TOP_SUBS = 10


class DepthPreset(NamedTuple):
    """How much of the archive a ``--depth`` level samples. Named fields so the
    numbers below aren't opaque positional magic at the call site."""
    posts: int          # post titles sampled
    comments: int       # comment snippets sampled
    comment_chars: int  # chars kept per comment snippet (longer = more nuance)


# Why these sizes. The binding constraint is NOT cost — even `deep` is ~11K
# input tokens for a heavy user, about $0.0016 on gpt-4o-mini ($0.15/1M in).
# It's two things: (1) the provider context window — `deep` must fit the
# *smallest* key a user might bring (gpt-4o-mini, 128K), and ~11K leaves en
# ormous headroom; (2) diminishing returns — a model writes a sharper profile
# from a tight, representative sample than from a giant dump it skims ("lost in
# the middle"), and a smaller prompt is faster. So the levels trade breadth for
# focus/latency, not for money:
#   quick    — a fast, cheap sketch from a user's headline content.
#   standard — the default; enough range to characterize most users well.
#   deep      — for prolific users whose range only shows over hundreds of items.
# comment_chars grows with depth because at higher depth we're spending the
# budget on nuance, not just count (a 400-char snippet keeps an argument intact).
SUMMARY_DEPTHS: dict[str, DepthPreset] = {
    "quick":    DepthPreset(posts=15,  comments=20,  comment_chars=200),
    "standard": DepthPreset(posts=40,  comments=60,  comment_chars=300),
    "deep":     DepthPreset(posts=100, comments=200, comment_chars=400),
}
SUMMARY_DEFAULT_DEPTH = "standard"
# The sample blends top-by-score (most upvoted = most defining, drawn from the
# user's whole history) with the most-recent items, so a profile reflects both
# who they've been and what they're into now. ~1/3 recent is the balance point:
# enough to catch a current phase or shift in interests, not so much that the
# recency bias we're fixing creeps back in (empirically, recency-only gave
# ~113x less signal — see PR #18). The remaining ~2/3 goes to top-voted.
SUMMARY_RECENT_FRACTION = 0.34
# Always keep at least one recent item even when 1/3 of a small sample rounds
# to zero, so even a tiny depth still reflects current activity.
SUMMARY_MIN_RECENT = 1

# --- LDA topic modeling -----------------------------------------------------
LDA_TOPICS = 6                   # themes to find
LDA_ITERATIONS = 25              # Gibbs sampling sweeps
LDA_VOCAB_SIZE = 1500            # most-frequent words kept
LDA_MAX_DOCS = 1500              # docs sampled (caps runtime)
LDA_TOP_WORDS = 8                # words shown per theme
LDA_ALPHA = 0.1                  # document-topic prior
LDA_BETA = 0.01                  # topic-word prior
LDA_SEED = 42                    # fixed → deterministic output

# --- report (topic page) ----------------------------------------------------
TOP_POSTS = 25
TOP_SUBREDDITS = 15
TOP_AUTHORS = 10
TOP_DOMAINS = 8
MIN_POST_ENGAGEMENT = 5          # score + COMMENT_WEIGHT*comments below = didn't land
COMMENT_WEIGHT = 2              # a comment counts this many votes toward engagement
TITLE_MAX = 110                 # chars of a post title shown before truncating
DRILL_POSTS = 25                # posts listed inside each expandable group
ACCENT = "#d93a00"             # redlens red — the page's one accent color

_DATA_DIR = Path(__file__).resolve().parent / "data"


def data_lines(name: str) -> list[str]:
    """Whitespace-separated tokens from a ``data/`` text file (``#`` comments
    and blank lines ignored)."""
    out: list[str] = []
    for line in (_DATA_DIR / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        out.extend(line.split())
    return out
