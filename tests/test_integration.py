"""End-to-end: fetch a real (tiny) user from arctic-shift, persist to a real
sqlite file, compute analytics, assert the numbers add up.

Hits the network. Marked as ``integration`` so ``pytest -m 'not integration'``
skips it on normal runs.

The test user is ``funny_mod`` — chosen because arctic has it indexed
with ~210 events total (121 posts + 89 comments), small enough for the
run to finish in a few seconds but big enough that every analytics
field is non-trivially populated.
"""

import pytest
from sqlmodel import Session

from redthread import compute_user_analytics, connect, init_schema, sync_user
from redthread.models import Comment, Post

TEST_USER = "funny_mod"


@pytest.mark.integration
def test_end_to_end_against_real_arctic(tmp_path):
    db_path = tmp_path / "integration.db"
    engine = connect(db_path)
    init_schema(engine)

    result = sync_user(TEST_USER, engine)

    # User row landed
    assert result.user.username == TEST_USER

    # arctic meta arrived flattened onto the user row (not a JSON blob)
    assert result.user.num_comments is not None

    # Counts match what got written
    with Session(engine) as s:
        from sqlmodel import func, select
        n_posts = s.exec(select(func.count()).select_from(Post)).one()
        n_comments = s.exec(select(func.count()).select_from(Comment)).one()
    assert n_posts == result.posts_written
    assert n_comments == result.comments_written

    # Analytics agree with the raw counts
    with Session(engine) as s:
        a = compute_user_analytics(s, TEST_USER)
    assert a.username == TEST_USER
    assert a.total_posts == result.posts_written
    assert a.total_comments == result.comments_written
    assert a.total_karma == a.post_karma + a.comment_karma

    # The user has at least one comment in arctic, so analytics should reflect it
    if a.total_comments + a.total_posts > 0:
        assert a.first_event_at is not None
        assert a.last_event_at is not None
        assert a.first_event_at <= a.last_event_at
        assert a.active_days >= 1
        assert a.top_subreddit is not None
        assert a.distinct_subreddits >= 1

    # And the file is real on disk
    assert db_path.exists()
    assert db_path.stat().st_size > 0
