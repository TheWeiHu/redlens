"""``verify_leads`` + the ``redlens leads`` verb — deterministic lead scoring.

A synthetic two-cohort DB (a coordinated block + an organic cohort) with a
PLANTED candidate that trips all three signals (many roster brands, high
co-activity with the block, an early push into a seeding wave) and a CONTROL
that trips none. No LLM: ``verify_leads`` is fully deterministic, so these run
offline and pin the score ordering, the promote-ready CSV round-trip, the JSON
shape, and the verb's exit codes. Synthetic names only.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post
from redlens.network import build_network
from redlens.network.leads import LeadVerdict, verify_leads
from redlens.network.rosters import load_cohorts

# The coordinated catalogue: five brands the block pushes as a wave.
_BRANDS = ["Alpha", "Beta", "Gamma", "Delta", "Epsilon"]
_DAY = 86_400
_T0 = 1_700_000_000

# roster.csv contents (name only — the name is its own match term)
_ROSTER = "\n".join(_BRANDS) + "\n"


def _seed_db(path: str) -> None:
    """Coordinated block (seed1..seed4) pushing the whole catalogue in a wave,
    an organic cohort (org1..org2) each touching one brand in their own subs,
    a PLANTED candidate (plant) tripping all three signals, and a CONTROL
    (control) tripping none."""
    engine = connect(path)
    init_schema(engine)
    posts: list[Post] = []
    comments: list[Comment] = []

    # --- coordinated block: 4 accounts, each first-mentions every brand within
    #     a tight window (a seeding wave), all in r/coord + one shared thread.
    block = ["seed1", "seed2", "seed3", "seed4"]
    for bi, brand in enumerate(_BRANDS):
        for ai, acc in enumerate(block):
            # first mentions cluster within ~2 days → a wave per brand
            ts = _T0 + bi * 30 * _DAY + ai * _DAY // 2
            posts.append(Post(
                post_id=f"cp-{brand}-{acc}", author_username=acc,
                subreddit_name="coord", created_utc=ts,
                title=f"try {brand}", selftext=f"{brand} rocks", score=5))
    # every block account co-comments in one shared thread
    for ai, acc in enumerate(block):
        comments.append(Comment(
            comment_id=f"cc-{acc}", author_username=acc,
            subreddit_name="coord", link_id="thread-x",
            created_utc=_T0 + ai, body="nice", score=1))

    # --- organic cohort: each mentions ONE brand, in its own subreddit, late.
    for i, acc in enumerate(["org1", "org2"]):
        posts.append(Post(
            post_id=f"op-{acc}", author_username=acc,
            subreddit_name=f"life{i}", created_utc=_T0 + 500 * _DAY,
            title=f"{_BRANDS[i]} worked for me", score=1))

    # --- PLANTED candidate: pushes 4 brands (breadth), shares r/coord + the
    #     shared thread (co-activity), and its first mentions land inside the
    #     block's waves (wave participation). Unlabeled.
    for bi, brand in enumerate(_BRANDS[:4]):
        posts.append(Post(
            post_id=f"pp-{brand}", author_username="plant",
            subreddit_name="coord",
            created_utc=_T0 + bi * 30 * _DAY + _DAY,   # inside each wave window
            title=f"love {brand}", selftext=f"{brand} again", score=2))
    comments.append(Comment(
        comment_id="pc", author_username="plant", subreddit_name="coord",
        link_id="thread-x", created_utc=_T0 + 10, body="agreed", score=1))

    # --- CONTROL: one brand, its own subreddit, its own thread, long after the
    #     waves. Trips nothing. Unlabeled.
    posts.append(Post(
        post_id="ctp", author_username="control", subreddit_name="hobby",
        created_utc=_T0 + 900 * _DAY, title=f"{_BRANDS[0]} is fine", score=1))
    comments.append(Comment(
        comment_id="ctc", author_username="control", subreddit_name="hobby",
        link_id="thread-solo", created_utc=_T0 + 900 * _DAY, body="hm",
        score=1))

    with Session(engine) as s:
        upsert(s, posts)
        upsert(s, comments)
        s.commit()


@pytest.fixture
def scored(tmp_path):
    """``(path, roster, cohorts)`` for a freshly seeded two-cohort DB — two
    cohorts so the multi-cohort seeding-wave view is available."""
    path = str(tmp_path / "leads.db")
    _seed_db(path)
    cohorts = {**dict.fromkeys(["seed1", "seed2", "seed3", "seed4"],
                               "coordinated"),
               **dict.fromkeys(["org1", "org2"], "organic")}
    roster = [(b, [b]) for b in _BRANDS]
    return path, roster, cohorts


def _net(path, roster, cohorts):
    from redlens.network.core import Network
    return Network(path, roster=roster, cohorts=cohorts)


def test_planted_scores_high_control_low(scored):
    path, roster, cohorts = scored
    net = _net(path, roster, cohorts)
    verdicts = verify_leads(net._store, candidates=["plant", "control"])
    by = {v.account: v for v in verdicts}
    assert set(by) == {"plant", "control"}
    plant, control = by["plant"], by["control"]
    # planted trips all three signals; control trips none
    assert plant.score >= 0.6
    assert control.score <= 0.15
    assert plant.score > control.score
    # ranking is score-descending
    assert [v.account for v in verdicts] == ["plant", "control"]
    # signal readings are surfaced for auditing
    assert plant.roster_brands == 4
    assert plant.coactivity > 0.5
    assert plant.wave_hits >= 1
    assert control.wave_hits == 0
    assert "roster brand" in plant.evidence


def test_default_candidates_are_the_suggested_pushers(scored):
    path, roster, cohorts = scored
    net = _net(path, roster, cohorts)
    # candidates=None → suggested_coordinated (unlabeled ≥3-brand pushers).
    # 'plant' pushes 4 brands and is unlabeled → a candidate; 'control' (1
    # brand) is below the suggestion threshold and drops out.
    accounts = {v.account for v in verify_leads(net._store)}
    assert "plant" in accounts
    assert "control" not in accounts
    # labeled accounts are never scored as their own leads
    assert not (accounts & set(cohorts))


def test_no_roster_or_cohort_yields_no_leads(scored):
    path, roster, cohorts = scored
    from redlens.network.core import Network
    # no coordinated cohort → nothing to score against → empty
    assert verify_leads(Network(path, roster=roster)._store) == []
    # explicit candidate but no coordinated block → all-zero, still returned
    v = verify_leads(Network(path, roster=roster)._store, candidates=["plant"])
    assert v and v[0].score == 0.0


def test_single_cohort_marks_wave_signal_na(scored):
    path, roster, _ = scored
    from redlens.network.core import Network
    # ONLY the coordinated block is labeled → one cohort → seeding_waves is
    # gated off. The wave score is a forced 0, so the evidence must say so
    # rather than implying a real zero-participation reading.
    solo = dict.fromkeys(["seed1", "seed2", "seed3", "seed4"], "coordinated")
    net = Network(path, roster=roster, cohorts=solo)
    assert not net._store.multi_cohort
    v = verify_leads(net._store, candidates=["plant"])
    assert v and v[0].wave_hits == 0
    assert "wave signal: n/a (needs ≥2 cohorts)" in v[0].evidence
    # and a genuinely-available wave signal does NOT carry the note
    two = _net(path, roster, scored[2])
    tv = verify_leads(two._store, candidates=["plant"])
    assert "wave signal: n/a" not in tv[0].evidence


def test_leads_verdict_as_dict_round_trips():
    v = LeadVerdict(account="plant", score=0.7, roster_brands=4,
                    coactivity=0.8, wave_hits=2, evidence="x")
    d = v.as_dict()
    assert d == {"account": "plant", "score": 0.7, "roster_brands": 4,
                 "coactivity": 0.8, "wave_hits": 2, "evidence": "x"}


# --- CLI verb ---------------------------------------------------------------

def _sidecars(tmp_path, roster_text, cohorts):
    (tmp_path / "brands.csv").write_text(roster_text, encoding="utf-8")
    lines = "".join(f"{a}, {c}\n" for a, c in cohorts.items())
    (tmp_path / "cohorts.csv").write_text(lines, encoding="utf-8")


def test_verb_text_table_ranks_leads(tmp_path, capsys):
    db = str(tmp_path / "leads.db")
    _seed_db(db)
    cohorts = {**dict.fromkeys(["seed1", "seed2", "seed3", "seed4"],
                               "coordinated"),
               **dict.fromkeys(["org1", "org2"], "organic")}
    _sidecars(tmp_path, _ROSTER, cohorts)
    rc = main(["--db", db, "leads"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "account" in out and "evidence" in out
    assert "plant" in out          # the planted lead surfaces


def test_verb_json_shape(tmp_path, capsys):
    db = str(tmp_path / "leads.db")
    _seed_db(db)
    cohorts = {**dict.fromkeys(["seed1", "seed2", "seed3", "seed4"],
                               "coordinated"),
               **dict.fromkeys(["org1", "org2"], "organic")}
    _sidecars(tmp_path, _ROSTER, cohorts)
    rc = main(["--db", db, "leads", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    row = data[0]
    assert set(row) == {"account", "score", "roster_brands", "coactivity",
                        "wave_hits", "evidence"}
    assert any(r["account"] == "plant" for r in data)


def test_verb_out_csv_round_trips_as_cohorts(tmp_path, capsys):
    db = str(tmp_path / "leads.db")
    _seed_db(db)
    cohorts = {**dict.fromkeys(["seed1", "seed2", "seed3", "seed4"],
                               "coordinated"),
               **dict.fromkeys(["org1", "org2"], "organic")}
    _sidecars(tmp_path, _ROSTER, cohorts)
    out = tmp_path / "promote.csv"
    rc = main(["--db", db, "leads", "--min-score", "0.5", "--out", str(out)])
    assert rc == 0
    # header + at least the planted lead; the extra score/evidence columns are
    # ignored by load_cohorts (reads only the first two cells) → valid --promote.
    loaded = load_cohorts(out)
    assert loaded.get("plant") == "coordinated"
    assert "control" not in loaded          # below --min-score, not written
    # the written file is itself a usable --promote input
    net = build_network(db, brands=None, cohorts=None, promote=out)
    assert "plant" in net.promoted
    assert net._store._coordinated >= {"plant"}


def test_verb_missing_sidecar_exits_2(tmp_path, capsys):
    db = str(tmp_path / "leads.db")
    _seed_db(db)
    rc = main(["--db", db, "leads", "--brands", str(tmp_path / "nope.csv")])
    assert rc == 2
    assert "file not found" in capsys.readouterr().err
