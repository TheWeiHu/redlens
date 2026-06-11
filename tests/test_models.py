from redditpages.models import Comment, Post, User

ARCTIC_USER = {
    "author": "KimJongFunk",
    "id": "rbpdo",
    "_meta": {
        "num_posts": 258, "num_comments": 10754,
        "post_karma": 178954, "comment_karma": 604006, "total_karma": 782960,
        "earliest_post_at": 1300000000, "last_post_at": 1700000000,
        "earliest_comment_at": 1290000000, "last_comment_at": 1780411725,
        "post_stats_updated_at": 1742860804, "comment_stats_updated_at": 1742860804,
    },
}

ARCTIC_POST = {
    "id": "1tgox1i", "author": "KimJongFunk", "subreddit": "watercolor101",
    "created_utc": 1779115753, "title": "Mod Team: Can we get user and post flairs?",
    "selftext": "Hi mod team! ...", "url": "https://www.reddit.com/...",
    "score": 40, "num_comments": 7,
    "all_awardings": [], "approved_by": None,  # noise — should be ignored
}

ARCTIC_COMMENT = {
    "id": "opbtldl", "author": "KimJongFunk", "subreddit": "nottheonion",
    "link_id": "t3_1tuss1h", "parent_id": "t1_opbt3t8",
    "created_utc": 1780411725, "body": "Depends on the casino...",
    "score": 1,
}


def test_user_from_arctic_flattens_meta_to_columns():
    u = User.from_arctic(ARCTIC_USER)
    assert u.username == "KimJongFunk"
    assert u.author_fullname == "rbpdo"
    assert u.num_posts == 258
    assert u.num_comments == 10754
    assert u.post_karma == 178954
    assert u.comment_karma == 604006
    assert u.last_post_at == 1700000000
    assert u.last_comment_at == 1780411725


def test_user_from_arctic_without_meta_leaves_stats_null():
    u = User.from_arctic({"author": "ghost", "id": "abc"})
    assert u.username == "ghost"
    assert u.num_posts is None
    assert u.post_karma is None
    assert u.last_comment_at is None


def test_post_keeps_signal_drops_noise():
    p = Post.from_arctic(ARCTIC_POST)
    assert p.post_id == "1tgox1i"
    assert p.author_username == "KimJongFunk"
    assert p.subreddit_name == "watercolor101"
    assert p.created_utc == 1779115753
    assert p.score == 40
    assert p.num_comments == 7


def test_post_empty_selftext_becomes_none():
    p = Post.from_arctic({**ARCTIC_POST, "selftext": ""})
    assert p.selftext is None


def test_comment_strips_link_id_prefix_keeps_parent_prefix():
    c = Comment.from_arctic(ARCTIC_COMMENT)
    assert c.comment_id == "opbtldl"
    assert c.link_id == "1tuss1h"           # t3_ stripped
    assert c.parent_id == "t1_opbt3t8"      # prefix preserved
    assert c.subreddit_name == "nottheonion"
    assert c.score == 1
