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

import hashlib
import json
from collections import Counter, defaultdict
from datetime import date, timedelta
from typing import TypeVar

from pydantic import ValidationError
from sqlalchemy import func
from sqlmodel import Session, select
from sqlmodel.sql.expression import SelectOfScalar

from redlens import cache, constants, llm, prompts
from redlens.config import require_llm_key
from redlens.errors import NotFound, RedlensError
from redlens.models import (
    Comment,
    MentionGroup,
    Post,
    Profile,
    Topic,
    TopicPost,
    TopicSummary,
    User,
)
from redlens.sentiment import DaySentiment, _day_start
from redlens.topics import (
    relevant_clause,
    require_topic,
    topic_comments,
    topic_data_version,
    topic_posts,
)

_Activity = TypeVar("_Activity", Post, Comment)


def _engagement_score(post: Post) -> int:
    """How much a post drew engagement — upvotes plus weighted replies. The
    shared ranking for "most-engaged" sampling across the LLM extractors."""
    return max(post.score, 0) + constants.COMMENT_WEIGHT * post.num_comments


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

    key = require_llm_key()
    prompt = _build_prompt(session, canon, resolved_depth)
    data = llm.complete_json(prompt, key)
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

    topic = require_topic(session, name)
    assert topic.id is not None
    # Read-through cache (see cache.py), keyed by depth + data-version and checked
    # before the key, so an unchanged re-render stays keyless.
    version = topic_data_version(session, topic.name)
    cached = cache.get(session, topic.id, "summary", resolved_depth, version)
    if cached is not None:
        return TopicSummary.model_validate_json(cached)

    key = require_llm_key()
    prompt = _build_topic_prompt(session, topic, resolved_depth)
    data = llm.complete_json(prompt, key)
    try:
        summary = TopicSummary.model_validate(
            {"topic": topic.name, "model": llm.model_name(),
             "depth": resolved_depth, **data})
    except ValidationError as exc:
        raise RedlensError(
            f"LLM topic summary didn't match the expected shape: {exc}") from exc
    cache.put(session, topic.id, "summary", resolved_depth, version,
              summary.model_dump_json(), summary.model)
    return summary


def daily_topic_sentiment(session: Session, name: str,
                          days_cap: int | None = None) -> list[DaySentiment]:
    """LLM-scored daily sentiment trend for a tracked topic.

    Buckets the topic's matched posts into UTC calendar days, samples each day's
    most-engaged titles, and asks ONE LLM call to score every day from -100 to
    +100 — handling the sarcasm and negation a lexicon can't ("X no longer
    works" is negative; "another amazing feature" may be sarcastic). Returns one
    :class:`~redlens.sentiment.DaySentiment` per day (``mean`` in [-1, 1]),
    gaps zero-filled. Raises :class:`NotFound`/:class:`MissingKey` like
    :func:`summarize_topic`; returns ``[]`` for a topic with no posts.

    ``days_cap`` (when > 0) limits the trend to the most recent N calendar days
    of activity. Since the whole series is sent in ONE prompt, the cap is the
    lever that bounds prompt/context size on long high-volume topics (the real
    limit, not dollars) — it shrinks both the LLM prompt and the charted window.
    """
    topic = require_topic(session, name)
    assert topic.id is not None
    # Read-through cache (see cache.py). The cap goes in the variant so capped and
    # uncapped renders keep distinct rows instead of clobbering each other.
    variant = f"d{days_cap}" if days_cap and days_cap > 0 else ""
    version = topic_data_version(session, topic.name)
    cached = cache.get(session, topic.id, "sentiment", variant, version)
    if cached is not None:
        return [DaySentiment.model_validate(row) for row in json.loads(cached)]

    key = require_llm_key()

    posts = topic_posts(session, topic.name)
    if not posts:
        return []
    comments = topic_comments(session, topic.name)

    posts_by_day: dict[str, list[Post]] = defaultdict(list)
    for p in posts:
        posts_by_day[_day_start(p.created_utc)].append(p)
    comments_by_day: dict[str, list[Comment]] = defaultdict(list)
    for c in comments:
        comments_by_day[_day_start(c.created_utc)].append(c)
    # Bucket on posts AND comments: a comment-only day is real activity the
    # prompt is told to weigh, so it must be shown to the model and charted.
    active_days = set(posts_by_day) | set(comments_by_day)
    days = sorted(active_days)
    # Cap the charted window to the most recent N calendar days of activity.
    # Trimming here (before blocks AND the zero-fill loop) bounds the prompt and
    # the chart in one place; the dropped days never reach the LLM.
    if days_cap and days_cap > 0 and days:
        cutoff = (date.fromisoformat(days[-1])
                  - timedelta(days=days_cap - 1)).isoformat()
        days = [dy for dy in days if dy >= cutoff]
        active_days = set(days)

    def _snip(text: str) -> str:
        # Untrusted post/comment text goes into the prompt; sentiment.txt tells
        # the model to treat it as data, not instructions (defense-in-depth).
        # Residual risk is low: the output is an advisory chart and the blast
        # radius of any slip is a single day's bar.
        return text.strip().replace("\n", " ")[:140]

    blocks = []
    for dy in days:
        dyp = sorted(posts_by_day.get(dy, []), key=lambda p: -_engagement_score(p)
                     )[:constants.SENTIMENT_DAY_SAMPLE]
        dyc = sorted(comments_by_day.get(dy, []), key=lambda c: -c.score
                     )[:constants.SENTIMENT_DAY_SAMPLE]
        lines = [f"Day {dy} ({len(posts_by_day.get(dy, []))} posts, "
                 f"{len(comments_by_day.get(dy, []))} comments):", "posts:"]
        lines += [f"- {_snip(p.title)}" for p in dyp
                  if p.title and p.title.strip()] or ["- (none)"]
        if any(c.body and c.body.strip() for c in dyc):
            lines.append("comments:")
            lines += [f"- {_snip(c.body)}" for c in dyc if c.body and c.body.strip()]
        blocks.append("\n".join(lines))

    prompt = prompts.render(
        "sentiment", topic=topic.name,
        keywords=", ".join(topic.keyword_list) or topic.name,
        days="\n\n".join(blocks))
    data = llm.complete_json(prompt, key)

    rows = data.get("days")
    scores: dict[str, float] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        dy = str(row.get("day", ""))
        val = row.get("score")
        if (dy in active_days and isinstance(val, (int, float))
                and not isinstance(val, bool)):
            scores[dy] = max(-1.0, min(1.0, float(val) / 100.0))

    # mean is None for any day the model didn't score (or a true gap): distinct
    # from a real 0.0 neutral, so the chart skips it instead of inventing one.
    out: list[DaySentiment] = []
    cur, end = date.fromisoformat(days[0]), date.fromisoformat(days[-1])
    while cur <= end:
        dy = cur.isoformat()
        out.append(DaySentiment(day=dy, mean=scores.get(dy),
                                posts=len(posts_by_day.get(dy, [])),
                                comments=len(comments_by_day.get(dy, []))))
        cur += timedelta(days=1)
    cache.put(session, topic.id, "sentiment", variant, version,
              json.dumps([d.model_dump() for d in out]), llm.model_name())
    return out


def label_themes(topic: str, themes: list[list[str]], *,
                 session: Session | None = None) -> list[str]:
    """Short, human-readable labels for LDA keyword clusters — one LLM call for
    all of them. Returns one label per input theme, in order; any theme the
    model skips or mangles falls back to its joined keywords, so the result is
    always aligned and non-empty. Raises :class:`MissingKey` with no key.

    When ``session`` is given (the page render path), the labels are read-through
    cached (see cache.py). LDA is deterministically seeded and keywords are in the
    data-version, so the cluster set is stable for a version and the cached labels
    stay aligned to it. Called without a session it always recomputes.
    """
    if not themes:
        return []
    topic_id: int | None = None
    version = ""
    if session is not None:
        cached_topic = require_topic(session, topic)
        assert cached_topic.id is not None
        topic_id = cached_topic.id
        version = topic_data_version(session, cached_topic.name)
        cached = cache.get(session, topic_id, "themes", "", version)
        if cached is not None:
            labels: list[str] = json.loads(cached)
            return labels
    key = require_llm_key()
    listed = "\n".join(f"{i + 1}. {', '.join(words)}"
                       for i, words in enumerate(themes))
    prompt = prompts.render("theme_labels", topic=topic, themes=listed)
    data = llm.complete_json(prompt, key)
    given = data.get("labels")
    given = given if isinstance(given, list) else []
    out: list[str] = []
    for i, words in enumerate(themes):
        label = given[i].strip() if i < len(given) and isinstance(given[i], str) else ""
        out.append(label or ", ".join(words[:4]))
    if session is not None and topic_id is not None:
        cache.put(session, topic_id, "themes", "", version,
                  json.dumps(out), llm.model_name())
    return out


def identify_brands(session: Session, name: str) -> list[MentionGroup]:
    """One LLM call to surface the OTHER brands/products that come up in a
    tracked topic's discussion (competitors, alternatives) — with the spelling
    variants to count them by. The caller does the actual counting; the LLM only
    recognizes. Raises :class:`NotFound`/:class:`MissingKey` like
    :func:`summarize_topic`; returns ``[]`` for a topic with no posts."""
    return _extract_labeled_terms(
        session, name, prompt_name="brands", terms_key="aliases")


def pin_brands(spec: str) -> list[MentionGroup]:
    """Build a FIXED brand list from a comma-separated ``spec``, bypassing
    :func:`identify_brands` (no LLM call, no key) — a trustworthy competitor
    ranking needs a fixed entity list, not the recognizer's jittery output. Each
    name is its own whole-word term; blanks are dropped and case-insensitive
    duplicates collapse to their first spelling, so the result is stable
    run-to-run. (Matching semantics are pinned by the test suite.)"""
    out: list[MentionGroup] = []
    seen: set[str] = set()
    for raw in spec.split(","):
        name = raw.strip()
        if not name or name.casefold() in seen:
            continue
        seen.add(name.casefold())
        out.append(MentionGroup(name=name, terms=[name]))
    return out


def extract_categories(session: Session, name: str, kind: str) -> list[MentionGroup]:
    """One LLM call to surface discussion categories — ``kind`` is
    ``"complaints"`` (recurring problems) or ``"use_cases"`` (what people use the
    topic for) — each with the signature phrases to count it by. Same
    recognize-here / count-in-the-caller split as :func:`identify_brands`."""
    prompt_name = "complaints" if kind == "complaints" else "use_cases"
    return _extract_labeled_terms(
        session, name, prompt_name=prompt_name, terms_key="phrases")


def _extract_labeled_terms(
    session: Session, name: str, *, prompt_name: str, terms_key: str,
) -> list[MentionGroup]:
    """Shared core for the LLM entity extractors (brands, complaints, use cases):
    sample the most-engaged posts/comments, ask ``prompt_name`` for a
    ``{"categories": [{"name", <terms_key>}]}`` list, and return one
    :class:`MentionGroup` per entry — blank names dropped, empty term lists
    falling back to ``[name]`` so every group is countable.

    Read-through cached (see cache.py) under ``kind=prompt_name``, so brands /
    complaints / use-cases don't clobber each other; persisting the output also
    pins the otherwise jittery entity SET across re-renders."""
    topic = require_topic(session, name)
    assert topic.id is not None
    version = topic_data_version(session, topic.name)
    # The brands prompt disambiguates the subject via topic.about, so an `--about`
    # edit must bust the brands cache even when the matched set is unchanged.
    # complaints/use_cases ignore `about`, so their key is untouched.
    if prompt_name == "brands":
        about_hash = hashlib.sha256(topic.about.strip().encode()).hexdigest()[:12]
        version = f"{version}:{about_hash}"
    cached = cache.get(session, topic.id, prompt_name, "", version)
    if cached is not None:
        return [MentionGroup(name=n, terms=list(terms))
                for n, terms in json.loads(cached)]

    key = require_llm_key()

    posts = topic_posts(session, topic.name)
    if not posts:
        return []
    comments = topic_comments(session, topic.name)

    top_posts = sorted(posts, key=lambda p: -_engagement_score(p)
                       )[:constants.EXTRACT_SAMPLE_POSTS]
    top_comments = sorted(comments, key=lambda c: -c.score
                          )[:constants.EXTRACT_SAMPLE_COMMENTS]
    lines = [f"- {p.title.strip()[:160]}" for p in top_posts
             if p.title and p.title.strip()]
    lines += [f"- {c.body.strip().replace(chr(10), ' ')[:160]}"
              for c in top_comments if c.body and c.body.strip()]
    # Pin the authoritative sense of an ambiguous topic so the brands recognizer
    # knows what the subject IS — and therefore what counts as the subject's own
    # products (excluded) vs a real competitor. Empty `about` → no line; brands.txt
    # is the only prompt with an `$about` slot, so this is ignored elsewhere.
    about = topic.about.strip()
    about_line = (f'The subject "{topic.name}" is, authoritatively: {about}.\n'
                  if about else "")
    prompt = prompts.render(prompt_name, topic=topic.name, about=about_line,
                            sample="\n".join(lines))
    data = llm.complete_json(prompt, key)

    # brands.txt returns {"brands": [...]}; complaints/use_cases return
    # {"categories": [...]} — accept whichever list the object carries.
    rows = data.get("categories")
    if not isinstance(rows, list):
        rows = data.get("brands")
    out: list[tuple[str, list[str]]] = []
    seen: dict[str, int] = {}   # normalized name -> index in out (first wins)
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        label = str(row.get("name", "")).strip()
        if not label:
            continue
        raw_terms = row.get(terms_key)
        terms = [str(t).strip() for t in raw_terms
                 if isinstance(t, str) and str(t).strip()
                 ] if isinstance(raw_terms, list) else []
        # Merge near-duplicate model output ("ExpressVPN" / "Express VPN") so it
        # doesn't render as two near-identical bars; whitespace + case folded.
        norm = "".join(label.split()).casefold()
        if norm in seen:
            kept = out[seen[norm]][1]
            kept.extend(t for t in (terms or [label]) if t not in kept)
            continue
        seen[norm] = len(out)
        out.append((label, terms or [label]))
    # The cache payload stays a (name, terms) pair list — the shape older rows
    # already hold — so the read path above keeps working across versions.
    cache.put(session, topic.id, prompt_name, "", version,
              json.dumps(out), llm.model_name())
    return [MentionGroup(name=n, terms=t) for n, t in out]


def _resolve_depth(depth: str | None) -> str:
    """Validate an optional ``--depth`` and fall back to the default."""
    if depth is not None and depth not in constants.SUMMARY_DEPTHS:
        raise RedlensError(
            f"unknown depth {depth!r} "
            f"(choose from {', '.join(constants.SUMMARY_DEPTHS)})")
    return depth or constants.SUMMARY_DEFAULT_DEPTH


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
        .where(TopicPost.topic_id == topic_id, relevant_clause()),
        comment_base=select(Comment)
        .join(TopicPost, TopicPost.post_id == Comment.link_id)  # type: ignore[arg-type]
        .where(TopicPost.topic_id == topic_id, relevant_clause()),
        sub_bases=[
            select(Post.subreddit_name)
            .join(TopicPost, TopicPost.post_id == Post.post_id)  # type: ignore[arg-type]
            .where(TopicPost.topic_id == topic_id, relevant_clause()),
        ],
        topic=topic.name,
        keywords=", ".join(topic.keyword_list) or topic.name,
    )
