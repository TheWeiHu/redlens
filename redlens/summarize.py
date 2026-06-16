"""AI profile inference — the "intelligent lens" the project is named for.

``summarize_user`` builds a representative, token-budgeted payload from the
locally archived data (the user's most-active communities + a sample of their
actual posts and comments — content, not raw counts/karma) and asks one LLM to
infer a profile: likely gender/age/location (with confidence), Big Five
personality, interests, beliefs, and tone. The result is generated on demand
and returned for printing — nothing is persisted (summaries are cheap and
depend on the changing archive, so there's nothing worth caching).

The sample is **not** the most recent items only: it blends top-by-score
content (most upvoted = most defining, drawn from the whole history) with a
slice of recent activity. How much is sampled is the ``depth`` knob — see
:data:`redlens.constants.SUMMARY_DEPTHS`. The prompt wording lives in
``redlens/prompts/profile.txt``.
"""
from __future__ import annotations

from collections import Counter
from typing import TypeVar

from sqlalchemy import func
from sqlmodel import Session, select

from redlens import constants, llm, prompts
from redlens.config import llm_api_key
from redlens.errors import MissingKey, NotFound, RedlensError
from redlens.models import Comment, Post, Profile, User

_Activity = TypeVar("_Activity", Post, Comment)


def summarize_user(session: Session, username: str, *,
                   depth: str | None = None) -> Profile:
    """Infer a profile for ``username`` from their archived activity.

    ``depth`` picks a :data:`~redlens.constants.SUMMARY_DEPTHS` preset
    (default ``standard``). Raises :class:`NotFound` if the user isn't synced,
    :class:`RedlensError` for an unknown depth, and :class:`MissingKey` if no
    LLM key is configured.
    """
    if depth is not None and depth not in constants.SUMMARY_DEPTHS:
        raise RedlensError(
            f"unknown depth {depth!r} "
            f"(choose from {', '.join(constants.SUMMARY_DEPTHS)})")
    resolved_depth = depth or constants.SUMMARY_DEFAULT_DEPTH

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
    text = llm.complete(prompt, key, max_tokens=constants.SUMMARY_MAX_TOKENS).strip()
    return Profile(username=canon, model=llm.model_name(), depth=resolved_depth,
                   text=text)


def _sample(session: Session, model: type[_Activity], pk_attr: str,
            canon: str, n: int) -> list[_Activity]:
    """A representative ``n``-item sample of ``model`` rows for ``canon``.

    Reserves a recent slice (``SUMMARY_RECENT_FRACTION``) then fills the rest
    top-by-score, deduped — so the result spans the user's whole history
    (their most-upvoted content) while still reflecting recent activity. Two
    bounded ``LIMIT n`` queries, so it never loads the full archive.
    """
    if n <= 0:
        return []
    author = model.author_username
    by_score = session.exec(
        select(model).where(author == canon)
        .order_by(model.score.desc())  # type: ignore[attr-defined]
        .limit(n)
    ).all()
    by_recency = session.exec(
        select(model).where(author == canon)
        .order_by(model.created_utc.desc())  # type: ignore[attr-defined]
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


def _build_prompt(session: Session, canon: str, depth: str) -> str:
    """Fill ``prompts/profile.txt`` with this user's communities and a
    representative sample of their posts/comments. Deliberately content-first:
    no raw counts/karma/dates (``analytics`` reports those, and feeding them
    here just tempts the model to recite numbers instead of inferring)."""
    preset = constants.SUMMARY_DEPTHS[depth]
    posts = _sample(session, Post, "post_id", canon, preset.posts)
    comments = _sample(session, Comment, "comment_id", canon, preset.comments)

    subs: Counter[str] = Counter(
        session.exec(
            select(Post.subreddit_name).where(Post.author_username == canon)
        ).all()
    )
    subs.update(
        session.exec(
            select(Comment.subreddit_name).where(Comment.author_username == canon)
        ).all()
    )
    # Names only, ranked by activity — the order conveys where they're most at
    # home without putting raw tallies in front of the model.
    communities = ", ".join(
        f"r/{name}" for name, _ in subs.most_common(constants.SUMMARY_TOP_SUBS)
    ) or "—"

    titles = "\n".join(f"- {p.title}" for p in posts if p.title) or "(none)"
    snippets = "\n".join(
        "- " + c.body.strip().replace("\n", " ")[:preset.comment_chars]
        for c in comments if c.body and c.body.strip()
    ) or "(none)"

    return prompts.render(
        "profile",
        username=canon,
        communities=communities,
        post_titles=titles,
        comment_snippets=snippets,
    )
