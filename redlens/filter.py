"""LLM relevance filtering for tracked topics — kill brand-name false positives.

``track`` matches a topic's keywords against arctic by case-insensitive substring,
which has no notion of *meaning*: a topic named for a common word ("conductor",
"shell", "bolt", "square") matches mostly posts about orchestras, seashells, and
hardware, not the product. Those pollute the archive so ``page`` / ``summarize`` /
``export`` describe the wrong thing.

This module asks one cheap LLM (the same OpenAI-compatible path as ``summarize``) to
classify each matched post as **on-topic** vs **false positive**, inferring the
intended sense of the topic from its name, the user's keywords, and the subreddits
the posts came from — there is no user-supplied description. The verdict is stored on
the ``topicpost`` join row (:class:`~redlens.models.TopicPost`), so it is computed
once, never re-paid on re-runs, fully auditable, and reversible:

- **soft-flag only** — nothing fetched is ever deleted; downstream surfaces hide
  only posts explicitly judged ``False`` and keep everything else (including
  unscored rows, so keyless ``track`` is unaffected);
- **keep-when-unsure** — the prompt and the defaults bias toward recall of true
  mentions: an omitted or malformed verdict, or a whole batch the LLM couldn't
  answer, never marks a post as junk.

``track`` calls :func:`filter_topic` on the posts it just matched whenever an LLM key
is configured; with no key it is skipped entirely.
"""
from __future__ import annotations

import time
from dataclasses import dataclass

from sqlmodel import Session

from redlens import constants, llm, prompts
from redlens.errors import RedlensError
from redlens.models import Post, Topic, TopicPost


@dataclass
class FilterResult:
    """What one :func:`filter_topic` pass did, for the CLI to report."""
    scored: int = 0       # posts that received a verdict
    relevant: int = 0     # judged on-topic (kept)
    filtered: int = 0     # judged false positive (hidden, not deleted)
    errored: int = 0      # posts in a batch the LLM couldn't classify (left unscored)


def _now() -> int:
    return int(time.time())


def _chunked(items: list[str], size: int) -> list[list[str]]:
    return [items[i:i + size] for i in range(0, len(items), size)]


def _item_block(posts: list[Post]) -> str:
    """One text block listing the batch's posts for the prompt — id, subreddit,
    title, and a short snippet so the model has enough context to judge sense."""
    lines: list[str] = []
    for p in posts:
        title = (p.title or "").strip().replace("\n", " ")
        lines.append(f"- id={p.post_id} | r/{p.subreddit_name} | {title}")
        snippet = (p.selftext or "").strip().replace("\n", " ")
        if snippet:
            lines.append(f"    {snippet[:constants.FILTER_SNIPPET_CHARS]}")
    return "\n".join(lines)


def _parse_verdicts(raw: str) -> dict[str, tuple[bool, float, str]]:
    """Map post id -> (relevant, confidence, reason) from one LLM reply.

    Tolerant by design (keep-when-unsure): a missing/garbled field falls back to
    relevant=True, and unparseable rows are simply dropped (the caller then leaves
    those posts unscored rather than guessing them junk)."""
    from redlens.summarize import _parse_json  # shared fence-tolerant JSON reader
    data = _parse_json(raw)
    rows = data.get("verdicts")
    out: dict[str, tuple[bool, float, str]] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        pid = str(row.get("id", "")).strip()
        if not pid:
            continue
        rel = row.get("relevant")
        relevant = True if not isinstance(rel, bool) else rel  # default keep
        raw_conf = row.get("confidence")
        confidence = (float(raw_conf)
                      if isinstance(raw_conf, (int, float))
                      and not isinstance(raw_conf, bool) else 0.0)
        confidence = max(0.0, min(1.0, confidence))
        reason = str(row.get("reason", "")).strip()[:200]
        out[pid] = (relevant, confidence, reason)
    return out


def about_clause(about: str) -> str:
    """The prompt's authoritative-sense line. A user-supplied ``--about`` pins
    *which* sense of an ambiguous name is meant (the Mac app "conductor", not the
    orchestra), removing the guesswork; empty means the model infers the sense
    from the keywords + subreddits + usage."""
    about = about.strip()
    if not about:
        return ("No explicit definition was given, so INFER the intended subject "
                "from the signals below.")
    return (f'The intended subject is, authoritatively: {about}. '
            "Judge every post against THAT meaning — keep posts about it, drop "
            "posts where the name appears only in another sense.")


def filter_topic(
    session: Session,
    topic: Topic,
    post_ids: list[str],
    key: str,
    *,
    about: str = "",
    batch: int = constants.FILTER_BATCH,
) -> FilterResult:
    """Classify ``post_ids`` (a topic's freshly matched posts) as on-topic vs
    false positive and persist the verdict on their ``topicpost`` rows.

    ``key`` is the LLM API key (caller checks it is present). ``about`` is an
    optional one-line definition of the intended sense (``track --about``); when
    given it is authoritative, otherwise the model infers the sense from the
    topic's name + keywords + the posts' subreddits. One LLM request per ``batch``
    posts; a batch whose request fails leaves its posts unscored (kept) and counts
    toward ``errored`` — never a false-positive verdict — so a flaky LLM degrades
    to today's unfiltered behavior instead of hiding real matches.
    """
    topic_id = topic.id
    assert topic_id is not None
    result = FilterResult()
    keywords = ", ".join(topic.keyword_list) or topic.name
    about_line = about_clause(about)

    for chunk in _chunked(post_ids, batch):
        posts = [p for pid in chunk
                 if (p := session.get(Post, pid)) is not None]
        if not posts:
            continue
        prompt = prompts.render(
            "filter", brand=topic.name, keywords=keywords,
            about=about_line, items=_item_block(posts))
        try:
            raw = llm.complete(prompt, key,
                               max_tokens=constants.SUMMARY_MAX_TOKENS,
                               json_object=True)
            verdicts = _parse_verdicts(raw)
        except RedlensError:
            # One bad batch (request failed, truncated, unparseable) must not sink
            # the rest or mark anything junk — leave these posts unscored (kept).
            result.errored += len(posts)
            continue

        model = llm.model_name()
        at = _now()
        for p in posts:
            verdict = verdicts.get(p.post_id)
            if verdict is None:
                # The model omitted this id: keep it (recall bias), but record the
                # verdict so we don't re-pay the LLM on the next track.
                relevant, confidence, reason = True, 0.0, "no verdict returned"
            else:
                relevant, confidence, reason = verdict
            row = session.get(TopicPost, (topic_id, p.post_id))
            if row is None:
                continue
            row.relevant = relevant
            row.relevance_confidence = confidence
            row.relevance_reason = reason
            row.relevance_model = model
            row.relevance_at = at
            session.add(row)
            result.scored += 1
            if relevant:
                result.relevant += 1
            else:
                result.filtered += 1
        session.commit()

    return result
