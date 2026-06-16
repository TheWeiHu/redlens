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
SUMMARY_MAX_TOKENS = 700         # cap on the summarize completion
SUMMARY_TOP_SUBS = 10            # most-active subreddits named in the payload
# How much of the archive to feed the model, per --depth preset:
# (post titles, comment snippets, chars per comment). Sized so even `deep`
# stays far under the smallest provider context window (gpt-4o-mini, 128K):
# ~200 comments x 400 chars ~= 20K input tokens. Cost is trivial at these
# sizes (gpt-4o-mini is $0.15/1M in), so the cap is about quality + window,
# not money.
SUMMARY_DEPTHS: dict[str, tuple[int, int, int]] = {
    "quick":    (15, 20, 200),
    "standard": (40, 60, 300),
    "deep":     (100, 200, 400),
}
SUMMARY_DEFAULT_DEPTH = "standard"
# Share of each sample reserved for the most-recent items; the rest is filled
# top-by-score, so the payload is representative of the whole history (most
# upvoted = most defining) rather than just the latest activity.
SUMMARY_RECENT_FRACTION = 0.34

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
