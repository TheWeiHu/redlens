"""Tunable constants and external endpoints, in one place.

Magic numbers that used to live scattered across the arctic client, the
discovery sources, topic tracking, and the report renderer are collected
here so they're easy to find and adjust. Large, frequently-edited lists
(popular subreddits, stopwords) live as plain-text data files under
``data/`` and are loaded by :func:`data_lines`.
"""
from __future__ import annotations

from pathlib import Path

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

# --- discovery --------------------------------------------------------------
DISCOVER_MAX_AUTHORS = 8          # top posters followed out of the seed subs
DISCOVER_MAX_NEW_SUBREDDITS = 12  # new subs one --discover round may add
MAX_WEB_RESULTS = 10              # subreddits taken from a web/global search
MAX_LLM_RESULTS = 10             # subreddits taken from one LLM suggestion call
# Authors that scope to noise rather than people.
NON_AUTHORS = frozenset({"[deleted]", "automoderator", "automod"})

# --- report (topic page) ----------------------------------------------------
TOP_POSTS = 25
TOP_SUBREDDITS = 15
TOP_AUTHORS = 10
TOP_DOMAINS = 8
MIN_POST_ENGAGEMENT = 5          # score + 2x comments below this = didn't land

_DATA_DIR = Path(__file__).resolve().parent / "data"


def data_lines(name: str) -> list[str]:
    """Whitespace-separated tokens from a ``data/`` text file (``#`` comments
    and blank lines ignored)."""
    out: list[str] = []
    for line in (_DATA_DIR / name).read_text(encoding="utf-8").splitlines():
        line = line.split("#", 1)[0].strip()
        out.extend(line.split())
    return out
