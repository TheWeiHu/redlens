"""AI profile inference — the "intelligent lens" the project is named for.

``summarize_user`` builds a representative, token-budgeted payload from the
locally archived data (the user's most-active communities + a sample of their
actual posts and comments — content, not raw counts/karma) and asks one LLM to
infer a profile as **structured JSON**: ranked gender/age/country/state/city
guesses (each a confidence + reason), Big Five trait scores, and short
interests/beliefs/tone paragraphs. Returning JSON instead of prose lets
consumers (CLI, ``--json``, an HTML view) render it deterministically. The
result is generated on demand — nothing is persisted (it's cheap and depends
on the changing archive, so there's nothing worth caching).

The sample is **not** the most recent items only: it blends top-by-score
content (most upvoted = most defining, drawn from the whole history) with a
slice of recent activity. How much is sampled is the ``depth`` knob — see
:data:`redlens.constants.SUMMARY_DEPTHS`. The prompt wording lives in
``redlens/prompts/profile.txt``.
"""
from __future__ import annotations

import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import Any, TypeVar

from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from redlens import constants, llm, prompts
from redlens.config import llm_api_key
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.models import (
    Brand,
    Comment,
    Post,
    Profile,
    Topic,
    TopicPost,
    TopicSummary,
    User,
)
from redlens.sentiment import WeekSentiment, _week_start
from redlens.topics import get_topic, topic_comments

_Activity = TypeVar("_Activity", Post, Comment)


def summarize_user(session: Session, username: str, *,
                   depth: str | None = None) -> Profile:
    """Infer a profile for ``username`` from their archived activity.

    ``depth`` picks a :data:`~redlens.constants.SUMMARY_DEPTHS` preset
    (default ``standard``). Raises :class:`NotFound` if the user isn't synced,
    :class:`RedlensError` for an unknown depth, and :class:`MissingKey` if no
    LLM key is configured.
    """
    resolved_depth = _resolve_depth(depth)

    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")
    canon = user.username

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_prompt(session, canon, resolved_depth)
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)
    try:
        return Profile.model_validate(
            {"username": canon, "model": llm.model_name(),
             "depth": resolved_depth, **data})
    except ValidationError as exc:
        raise RedlensError(
            f"LLM profile didn't match the expected shape: {exc}") from exc


def summarize_topic(session: Session, name: str, *,
                    depth: str | None = None) -> TopicSummary:
    """Infer a narrative of what tracked topic ``name``'s discussion is about.

    Samples the topic's matched posts/comments (the same top-voted + recent
    blend as :func:`summarize_user`, sized by ``depth``) and asks one LLM for a
    structured summary: an overview, the prominent themes, overall sentiment,
    and where opinion splits. Raises :class:`NotFound` if the topic isn't
    tracked, :class:`RedlensError` for an unknown depth, and :class:`MissingKey`
    if no LLM key is configured.
    """
    resolved_depth = _resolve_depth(depth)

    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    prompt = _build_topic_prompt(session, topic, resolved_depth)
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)
    try:
        return TopicSummary.model_validate(
            {"topic": topic.name, "model": llm.model_name(),
             "depth": resolved_depth, **data})
    except ValidationError as exc:
        raise RedlensError(
            f"LLM topic summary didn't match the expected shape: {exc}") from exc


def weekly_topic_sentiment(session: Session, name: str) -> list[WeekSentiment]:
    """LLM-scored weekly sentiment trend for a tracked topic.

    Buckets the topic's matched posts into UTC ISO weeks, samples each week's
    most-engaged titles, and asks ONE LLM call to score every week from -100 to
    +100 — handling the sarcasm and negation a lexicon can't ("X no longer
    works" is negative; "another amazing feature" may be sarcastic). Returns one
    :class:`~redlens.sentiment.WeekSentiment` per week (``mean`` in [-1, 1]),
    gaps zero-filled. Raises :class:`NotFound`/:class:`MissingKey` like
    :func:`summarize_topic`; returns ``[]`` for a topic with no posts."""
    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    topic_id = topic.id
    assert topic_id is not None
    posts = list(session.exec(
        select(Post)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id)
    ))
    if not posts:
        return []
    comments = topic_comments(session, topic.name)

    posts_by_week: dict[str, list[Post]] = defaultdict(list)
    for p in posts:
        posts_by_week[_week_start(p.created_utc)].append(p)
    comments_by_week: dict[str, list[Comment]] = defaultdict(list)
    for c in comments:
        comments_by_week[_week_start(c.created_utc)].append(c)
    weeks = sorted(posts_by_week)

    def _engagement(p: Post) -> int:
        return max(p.score, 0) + constants.COMMENT_WEIGHT * p.num_comments

    def _snip(text: str) -> str:
        return text.strip().replace("\n", " ")[:140]

    blocks = []
    for wk in weeks:
        wkp = sorted(posts_by_week[wk], key=lambda p: -_engagement(p)
                     )[:constants.SENTIMENT_WEEK_SAMPLE]
        wkc = sorted(comments_by_week.get(wk, []), key=lambda c: -c.score
                     )[:constants.SENTIMENT_WEEK_SAMPLE]
        lines = [f"Week {wk} ({len(posts_by_week[wk])} posts, "
                 f"{len(comments_by_week.get(wk, []))} comments):", "posts:"]
        lines += [f"- {_snip(p.title)}" for p in wkp
                  if p.title and p.title.strip()] or ["- (none)"]
        if any(c.body and c.body.strip() for c in wkc):
            lines.append("comments:")
            lines += [f"- {_snip(c.body)}" for c in wkc if c.body and c.body.strip()]
        blocks.append("\n".join(lines))

    prompt = prompts.render(
        "sentiment", topic=topic.name,
        keywords=", ".join(topic.keyword_list) or topic.name,
        weeks="\n\n".join(blocks))
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)

    rows = data.get("weeks")
    scores: dict[str, float] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        wk = str(row.get("week", ""))
        val = row.get("score")
        if (wk in posts_by_week and isinstance(val, (int, float))
                and not isinstance(val, bool)):
            scores[wk] = max(-1.0, min(1.0, float(val) / 100.0))

    out: list[WeekSentiment] = []
    cur, end = date.fromisoformat(weeks[0]), date.fromisoformat(weeks[-1])
    while cur <= end:
        wk = cur.isoformat()
        n_posts = len(posts_by_week.get(wk, []))
        n_comments = len(comments_by_week.get(wk, []))
        out.append(WeekSentiment(wk, scores.get(wk, 0.0) if n_posts else 0.0,
                                 n_posts, n_comments))
        cur += timedelta(days=7)
    return out


def label_themes(topic: str, themes: list[list[str]]) -> list[str]:
    """Short, human-readable labels for LDA keyword clusters — one LLM call for
    all of them. Returns one label per input theme, in order; any theme the
    model skips or mangles falls back to its joined keywords, so the result is
    always aligned and non-empty. Raises :class:`MissingKey` with no key."""
    if not themes:
        return []
    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )
    listed = "\n".join(f"{i + 1}. {', '.join(words)}"
                       for i, words in enumerate(themes))
    prompt = prompts.render("theme_labels", topic=topic, themes=listed)
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)
    given = data.get("labels")
    given = given if isinstance(given, list) else []
    out: list[str] = []
    for i, words in enumerate(themes):
        label = given[i].strip() if i < len(given) and isinstance(given[i], str) else ""
        out.append(label or ", ".join(words[:4]))
    return out


def identify_brands(session: Session, name: str) -> list[Brand]:
    """One LLM call to surface the OTHER brands/products that come up in a
    tracked topic's discussion (competitors, alternatives) — with the spelling
    variants to count them by. The caller does the actual counting; the LLM only
    recognizes. Raises :class:`NotFound`/:class:`MissingKey` like
    :func:`summarize_topic`; returns ``[]`` for a topic with no posts."""
    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked yet — run `redlens track` first")

    key = llm_api_key()
    if not key:
        raise MissingKey(
            "no LLM API key — run `redlens setup` or set "
            "OPENAI_API_KEY / REDLENS_LLM_API_KEY"
        )

    topic_id = topic.id
    assert topic_id is not None
    posts = list(session.exec(
        select(Post)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id)
    ))
    if not posts:
        return []
    comments = topic_comments(session, topic.name)

    def _eng(p: Post) -> int:
        return max(p.score, 0) + constants.COMMENT_WEIGHT * p.num_comments

    top_posts = sorted(posts, key=lambda p: -_eng(p))[:constants.BRAND_SAMPLE_POSTS]
    top_comments = sorted(comments, key=lambda c: -c.score
                          )[:constants.BRAND_SAMPLE_COMMENTS]
    lines = [f"- {p.title.strip()[:160]}" for p in top_posts
             if p.title and p.title.strip()]
    lines += [f"- {c.body.strip().replace(chr(10), ' ')[:160]}"
              for c in top_comments if c.body and c.body.strip()]
    prompt = prompts.render("brands", topic=topic.name, sample="\n".join(lines))
    raw = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS)
    data = _parse_json(raw)

    out: list[Brand] = []
    rows = data.get("brands")
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        bn = str(row.get("name", "")).strip()
        if not bn:
            continue
        raw_aliases = row.get("aliases")
        aliases = [str(a).strip() for a in raw_aliases
                   if isinstance(a, str) and str(a).strip()
                   ] if isinstance(raw_aliases, list) else []
        out.append(Brand(name=bn, aliases=aliases or [bn]))
    return out


def _resolve_depth(depth: str | None) -> str:
    """Validate an optional ``--depth`` and fall back to the default."""
    if depth is not None and depth not in constants.SUMMARY_DEPTHS:
        raise RedlensError(
            f"unknown depth {depth!r} "
            f"(choose from {', '.join(constants.SUMMARY_DEPTHS)})")
    return depth or constants.SUMMARY_DEFAULT_DEPTH


def _parse_json(raw: str) -> dict[str, Any]:
    """The JSON object from a completion, tolerant of markdown fences/prose
    around it (we take the outermost ``{...}``)."""
    i, j = raw.find("{"), raw.rfind("}")
    if i == -1 or j <= i:
        raise RedlensError("LLM did not return a JSON object")
    try:
        obj = json.loads(raw[i:j + 1])
    except json.JSONDecodeError as exc:
        raise RedlensError(f"LLM returned invalid JSON: {exc}") from exc
    if not isinstance(obj, dict):
        raise RedlensError("LLM JSON was not an object")
    return obj


def _sample(session: Session, model: type[_Activity], pk_attr: str,
            base: SelectOfScalar[_Activity], n: int) -> list[_Activity]:
    """A representative ``n``-item sample drawn from ``base`` (a select already
    filtered to the membership set — a user's rows, or a topic's matched rows).

    Reserves a recent slice (``SUMMARY_RECENT_FRACTION``) then fills the rest
    top-by-score, deduped — so the result spans the whole history (the
    most-upvoted content) while still reflecting recent activity. Two bounded
    ``LIMIT n`` queries, so it never loads the full archive.
    """
    if n <= 0:
        return []
    by_score = session.exec(
        base.order_by(model.score.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()
    by_recency = session.exec(
        base.order_by(model.created_utc.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()

    recent_quota = max(constants.SUMMARY_MIN_RECENT,
                       round(n * constants.SUMMARY_RECENT_FRACTION))
    chosen: dict[str, _Activity] = {}
    for item in by_recency[:recent_quota]:        # reserve recency slots
        chosen[getattr(item, pk_attr)] = item
    for pool in (by_score, by_recency):           # fill from top-score, then rest
        for item in pool:
            if len(chosen) >= n:
                break
            chosen.setdefault(getattr(item, pk_attr), item)
    # Newest-first for a readable payload.
    return sorted(chosen.values(), key=lambda x: x.created_utc, reverse=True)


def _render_sample_prompt(
    session: Session, *, template: str, depth: str,
    post_base: SelectOfScalar[Post], comment_base: SelectOfScalar[Comment],
    sub_bases: list[SelectOfScalar[str]], **extra: str,
) -> str:
    """Sample posts/comments from the given membership selects and render
    ``template`` with the shared communities/titles/snippets fields plus any
    caller-specific ``extra`` kwargs.

    Deliberately content-first: titles + snippets, not raw counts/karma/dates
    (``analytics`` / ``show --topic`` report those, and feeding numbers here just
    tempts the model to recite them instead of inferring). ``sub_bases`` are the
    one-or-more ``subreddit_name`` selects whose rows, ranked by activity, name
    the communities — order conveys where the discussion lives without tallies.
    """
    preset = constants.SUMMARY_DEPTHS[depth]
    posts = _sample(session, Post, "post_id", post_base, preset.posts)
    comments = _sample(session, Comment, "comment_id", comment_base, preset.comments)

    subs: Counter[str] = Counter()
    for base in sub_bases:
        subs.update(session.exec(base).all())
    communities = ", ".join(
        f"r/{name}" for name, _ in subs.most_common(constants.SUMMARY_TOP_SUBS)
    ) or "—"

    titles = "\n".join(f"- {p.title}" for p in posts if p.title) or "(none)"
    snippets = "\n".join(
        "- " + c.body.strip().replace("\n", " ")[:preset.comment_chars]
        for c in comments if c.body and c.body.strip()
    ) or "(none)"

    return prompts.render(
        template,
        communities=communities,
        post_titles=titles,
        comment_snippets=snippets,
        **extra,
    )


def _build_prompt(session: Session, canon: str, depth: str) -> str:
    """Fill ``prompts/profile.txt`` for a user — their communities and a
    representative sample of their posts/comments."""
    return _render_sample_prompt(
        session, template="profile", depth=depth,
        post_base=select(Post).where(Post.author_username == canon),
        comment_base=select(Comment).where(Comment.author_username == canon),
        sub_bases=[
            select(Post.subreddit_name).where(Post.author_username == canon),
            select(Comment.subreddit_name).where(Comment.author_username == canon),
        ],
        username=canon,
    )


def _build_topic_prompt(session: Session, topic: Topic, depth: str) -> str:
    """Fill ``prompts/topic.txt`` for a tracked topic — its keywords, the
    communities discussing it, and a sample of its matched posts/comments.

    Membership mirrors the topic page / comment bridge: posts via the
    ``topicpost`` join, comments via ``topicpost.post_id == comment.link_id``.
    """
    topic_id = topic.id
    assert topic_id is not None
    return _render_sample_prompt(
        session, template="topic", depth=depth,
        post_base=select(Post)
        .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id),
        comment_base=select(Comment)
        .join(TopicPost, TopicPost.post_id == Comment.link_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id),
        sub_bases=[
            select(Post.subreddit_name)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic_id),
        ],
        topic=topic.name,
        keywords=", ".join(topic.keyword_list) or topic.name,
    )
