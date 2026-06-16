"""Discovery sources for a topic's subreddit net.

Five sources, all optional, all feeding one user-curated picker:

1. **name** — arctic's keyless subreddit search (names matching the topic);
   lives in :mod:`redlens.topics` as ``search_subreddits``.
2. **global** — PullPush (the keyless Pushshift-style mirror) full-text
   searches *all* of Reddit and we take the subreddits its matching posts
   live in. The only source with no scope requirement, so it finds related
   communities by name (r/Semaglutide, r/Mounjaro for "ozempic").
3. **web** — a DuckDuckGo search for the topic + reddit, mining result URLs
   for subreddit names. Keyless; finds discussed-in communities (r/loseit
   for "ozempic"). Best-effort — DDG bot-walls automated queries.
4. **popular** — the ~100 largest general subreddits (``data/
   popular_subreddits.txt``), cast over wholesale (empty subs cost one
   request each).
5. **llm** — one cheap LLM call suggesting subreddits, when an LLM API key
   is configured.

The per-topic behavioral round (``--discover`` in :mod:`redlens.topics`)
complements these by following the authors of matching posts to wherever
else they discuss the topic.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from collections import Counter

from redlens import arctic, config, constants, llm, prompts
from redlens.constants import MAX_LLM_RESULTS, MAX_WEB_RESULTS
from redlens.errors import RedlensError

_SUBREDDIT_RE = re.compile(r"reddit\.com/r/([A-Za-z0-9_]{2,21})")
_VALID_NAME_RE = re.compile(r"^[A-Za-z0-9_]{2,21}$")
_JUNK_NAMES = {"all", "popular", "search", "subreddits"}

# The ~100 largest general subreddits, in data/popular_subreddits.txt so the
# list is easy to update without touching code.
POPULAR_SUBREDDITS = constants.data_lines("popular_subreddits.txt")


def _http(req: urllib.request.Request) -> bytes:
    try:
        with urllib.request.urlopen(req, timeout=constants.HTTP_TIMEOUT_S) as r:
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


def search_global(topic: str) -> list[str]:
    """Subreddits hosting posts that match the topic, via PullPush's global
    full-text search — the one keyless API with no scope requirement.

    This finds semantically related communities (r/Semaglutide and
    r/Mounjaro for "ozempic") that name matching structurally cannot.
    """
    qs = urllib.parse.urlencode({"q": topic, "size": constants.PULLPUSH_SIZE})
    req = urllib.request.Request(
        f"{constants.PULLPUSH_URL}?{qs}", headers={"User-Agent": arctic.UA}
    )
    data = json.loads(_http(req))
    found: Counter[str] = Counter()
    for post in data.get("data") or []:
        sub = post.get("subreddit") or ""
        if sub and not sub.startswith("u_"):
            found[sub] += 1
    return [name for name, _ in found.most_common(MAX_WEB_RESULTS)]


def search_web(topic: str) -> list[str]:
    """Subreddit names mined from a DuckDuckGo search of the topic,
    most-mentioned first."""
    qs = urllib.parse.urlencode({"q": f"{topic} reddit"})
    req = urllib.request.Request(
        f"{constants.DUCKDUCKGO_URL}?{qs}", headers={"User-Agent": arctic.UA}
    )
    page = urllib.parse.unquote(_http(req).decode("utf-8", errors="replace"))
    found = Counter(
        name for name in _SUBREDDIT_RE.findall(page)
        if name.lower() not in _JUNK_NAMES
    )
    return [name for name, _ in found.most_common(MAX_WEB_RESULTS)]


def suggest_llm(topic: str) -> list[str]:
    """Subreddit names suggested by one cheap LLM call, or [] if no key
    is configured."""
    api_key = config.llm_api_key()
    if not api_key:
        return []
    prompt = prompts.render("subreddits", count=str(MAX_LLM_RESULTS), topic=topic)
    text = llm.complete(prompt, api_key)
    names = []
    for line in text.splitlines():
        name = line.strip().lstrip("-* ").removeprefix("r/").strip()
        if name and _VALID_NAME_RE.match(name):
            names.append(name)
    return names[:MAX_LLM_RESULTS]
