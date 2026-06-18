"""Dump a user's archived posts and comments to a stream.

Three formats, all written to a caller-supplied text stream (stdout by
default) so ``redlens export`` is pipeable:

- ``json``  — one object ``{username, posts: [...], comments: [...]}``.
- ``jsonl`` — one record per line, each tagged with a ``kind`` field; good
  for streaming into jq or a line-oriented loader.
- ``csv``   — a single table; posts and comments share a ``kind`` column and
  the union of their fields (blank where a column doesn't apply).
"""
from __future__ import annotations

import csv
import json
from typing import TextIO

from sqlalchemy import func
from sqlmodel import Session, select

from redlens.errors import NotFound, RedlensError
from redlens.models import Comment, Post, User
from redlens.topics import get_topic, topic_comments, topic_posts

FORMATS = ("json", "csv", "jsonl")


def _dump(
    header: dict[str, object],
    posts: list[Post],
    comments: list[Comment],
    fmt: str,
    out: TextIO,
) -> tuple[int, int]:
    """Write ``posts`` + ``comments`` to ``out`` in ``fmt``, prefixed with the
    scope ``header`` (e.g. ``{"username": ...}`` or ``{"topic": ...}``). Shared
    by the user and topic exports so the three formats stay identical."""
    if fmt not in FORMATS:
        raise RedlensError(f"unknown export format {fmt!r} (choose from {', '.join(FORMATS)})")

    if fmt == "json":
        json.dump(
            {
                **header,
                "posts": [p.model_dump() for p in posts],
                "comments": [c.model_dump() for c in comments],
            },
            out,
            indent=2,
        )
        out.write("\n")
    elif fmt == "jsonl":
        for p in posts:
            out.write(json.dumps({"kind": "post", **p.model_dump()}) + "\n")
        for c in comments:
            out.write(json.dumps({"kind": "comment", **c.model_dump()}) + "\n")
    else:  # csv
        rows = [{"kind": "post", **p.model_dump()} for p in posts]
        rows += [{"kind": "comment", **c.model_dump()} for c in comments]
        fields = ["kind"]
        for row in rows:
            for key in row:
                if key not in fields:
                    fields.append(key)
        writer = csv.DictWriter(out, fieldnames=fields)
        writer.writeheader()
        writer.writerows(rows)

    return len(posts), len(comments)


def export_user(session: Session, username: str, fmt: str, out: TextIO) -> tuple[int, int]:
    """Write ``username``'s posts and comments to ``out`` in ``fmt``.

    Returns ``(posts_written, comments_written)``. Raises ``NotFound`` if the
    user isn't in the DB yet.
    """
    user = session.exec(
        select(User).where(func.lower(User.username) == username.lower())
    ).first()
    if user is None:
        raise NotFound(f"u/{username} not in DB — sync first")
    canon = user.username

    posts = list(session.exec(
        select(Post).where(Post.author_username == canon)
        .order_by(Post.created_utc.asc())  # type: ignore[attr-defined]
    ).all())
    comments = list(session.exec(
        select(Comment).where(Comment.author_username == canon)
        .order_by(Comment.created_utc.asc())  # type: ignore[attr-defined]
    ).all())

    return _dump({"username": canon}, posts, comments, fmt, out)


def export_topic(session: Session, name: str, fmt: str, out: TextIO) -> tuple[int, int]:
    """Write a tracked topic's matched posts (and any pulled comments) to
    ``out`` in ``fmt``.

    Returns ``(posts_written, comments_written)``. Raises ``NotFound`` if the
    topic isn't tracked yet.
    """
    topic = get_topic(session, name)
    if topic is None:
        raise NotFound(f"topic {name!r} not tracked — run `redlens track` first")
    canon = topic.name

    posts = topic_posts(session, canon)
    comments = topic_comments(session, canon)

    return _dump({"topic": canon}, posts, comments, fmt, out)
