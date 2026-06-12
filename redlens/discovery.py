"""Discovery sources for a topic's subreddit net.

Four sources, all optional, all feeding one user-curated picker:

1. **name** — arctic's keyless subreddit search (names matching the topic);
   lives in :mod:`redlens.topics` as ``search_subreddits``.
2. **web** — a DuckDuckGo search for the topic + reddit, mining result URLs
   for subreddit names. Keyless; finds communities that merely *discuss*
   the topic (r/loseit for "ozempic").
3. **popular** — a maintained list of the ~100 largest general subreddits,
   cast over wholesale (an empty subreddit costs one request).
4. **llm** — one cheap LLM call suggesting subreddits, available when an
   LLM API key is configured.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter
from typing import Any

from redlens import arctic, config
from redlens.errors import RedlensError

WEB_SEARCH_URL = "https://html.duckduckgo.com/html/"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
OPENAI_URL = "https://api.openai.com/v1/chat/completions"
DEFAULT_ANTHROPIC_MODEL = "claude-haiku-4-5"
DEFAULT_OPENAI_MODEL = "gpt-4o-mini"
MAX_WEB_RESULTS = 10
MAX_LLM_RESULTS = 10

_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]{2,21})")
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_JUNK_NAMES = {"all", "popular", "search", "subreddits"}

# The ~100 largest general-interest subreddits, for casting a wide net over
# mainstream discussion. Maintained by hand; order is rough size.
POPULAR_SUBREDDITS = [
    "funny", "AskReddit", "gaming", "worldnews", "todayilearned", "aww",
    "Music", "memes", "movies", "Showerthoughts", "science", "pics",
    "Jokes", "news", "space", "askscience", "DIY", "books", "nottheonion",
    "food", "mildlyinteresting", "explainlikeimfive", "LifeProTips", "IAmA",
    "gadgets", "EarthPorn", "sports", "dataisbeautiful", "GetMotivated",
    "gifs", "videos", "Art", "television", "UpliftingNews",
    "photoshopbattles", "Futurology", "WritingPrompts", "OldSchoolCool",
    "history", "personalfinance", "philosophy", "Documentaries",
    "InternetIsBeautiful", "listentothis", "technology",
    "interestingasfuck", "wallstreetbets", "Damnthatsinteresting",
    "politics", "relationship_advice", "NoStupidQuestions",
    "AmItheAsshole", "facepalm", "NatureIsFuckingLit", "BeAmazed",
    "unpopularopinion", "oddlysatisfying", "Unexpected", "nba", "soccer",
    "nfl", "anime", "PS5",
    "NintendoSwitch", "pcmasterrace", "buildapc", "apple", "android",
    "ChatGPT", "artificial", "OpenAI", "cars", "Fitness",
    "malefashionadvice", "femalefashionadvice", "SkincareAddiction",
    "MakeupAddiction", "Parenting", "Cooking", "gardening",
    "travel", "solotravel", "investing", "stocks", "CryptoCurrency",
    "Bitcoin", "legaladvice", "AskMen", "AskWomen", "dating_advice",
    "TwoXChromosomes", "teenagers", "college", "jobs", "antiwork",
    "popculturechat", "Fauxmoi", "entertainment", "celebrities",
    "popheads",
]


def _http(req: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            status = int(getattr(r, "status", 200) or 200)
            if status != 200:
                # urllib only raises for >=400; DuckDuckGo answers bot
                # challenges with 202 + an empty "anomaly" page, which must
                # surface as a failure, not as zero results.
                raise RedlensError(
                    f"{req.full_url}: HTTP {status} (likely bot challenge)")
            return bytes(r.read())
    except RedlensError:
        raise
    except Exception as exc:
        raise RedlensError(f"GET {req.full_url}: {exc}") from exc


def search_web(topic: str) -> list[str]:
    """Subreddit names mined from a DuckDuckGo search of the topic,
    most-mentioned first."""
    qs = urllib.parse.urlencode({"q": f"{topic} reddit"})
    req = urllib.request.Request(
        f"{WEB_SEARCH_URL}?{qs}", headers={"User-Agent": arctic.UA}
    )
    page = urllib.parse.unquote(_http(req).decode("utf-8", errors="replace"))
    found = Counter(
        name for name in _SUBREDDIT_RE.findall(page)
        if name.lower() not in _JUNK_NAMES
    )
    return [name for name, _ in found.most_common(MAX_WEB_RESULTS)]


def _llm_complete(prompt: str, api_key: str) -> str:
    """One small completion via raw HTTP — this package is stdlib-only by
    design, so no provider SDKs. Anthropic or OpenAI, by key shape (or the
    [llm] provider/model config overrides)."""
    settings = config.load_config().get("llm", {})
    provider = settings.get("provider") or (
        "anthropic" if api_key.startswith("sk-ant") else "openai"
    )
    if provider == "anthropic":
        url = ANTHROPIC_URL
        headers = {"x-api-key": api_key, "anthropic-version": "2023-06-01"}
        body: dict[str, Any] = {
            "model": settings.get("model") or DEFAULT_ANTHROPIC_MODEL,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }
    else:
        url = OPENAI_URL
        headers = {"Authorization": f"Bearer {api_key}"}
        body = {
            "model": settings.get("model") or DEFAULT_OPENAI_MODEL,
            "max_tokens": 300,
            "messages": [{"role": "user", "content": prompt}],
        }
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={**headers, "Content-Type": "application/json"},
        method="POST",
    )
    data = json.loads(_http(req))
    if provider == "anthropic":
        return "".join(
            block.get("text", "") for block in data.get("content", [])
            if block.get("type") == "text"
        )
    return str(data["choices"][0]["message"]["content"])


def suggest_llm(topic: str) -> list[str]:
    """Subreddit names suggested by one cheap LLM call, or [] if no key
    is configured."""
    api_key = config.llm_api_key()
    if not api_key:
        return []
    prompt = (
        f"List up to {MAX_LLM_RESULTS} subreddits where {topic!r} is "
        "actively discussed. Include both dedicated communities and broader "
        "ones where it comes up often. Reply with one subreddit name per "
        "line, no r/ prefix, no commentary. Only real subreddits."
    )
    text = _llm_complete(prompt, api_key)
    names = []
    for line in text.splitlines():
        name = line.strip().lstrip("-* ").removeprefix("r/").strip()
        if name and _VALID_NAME_RE.match(name):
            names.append(name)
    return names[:MAX_LLM_RESULTS]
