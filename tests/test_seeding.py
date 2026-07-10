"""``seeding_verdicts`` + the ``redlens seeding`` verb — deterministic
SEEDED-vs-ORGANIC brand verdicts.

A synthetic two-cohort DB with three PLANTED roster brands:

- ``Seeded`` — only the coordinated block mentions it, in a tight first-push
  wave, zero organic → verdict ``seeded`` (and ``organic_lag_days`` None);
- ``Organic`` — organic authors mention it first and more than the network →
  ``organic-first``;
- ``Mixed`` — coordinated first-pushes it but organic adopts too and no tight
  wave forms → ``inconclusive``.

No LLM: fully deterministic, so these run offline and pin the fields, the JSON
shape, and the verb's exit codes (including the single-cohort gate). Synthetic
names only.
"""
from __future__ import annotations

import json

import pytest
from sqlmodel import Session

from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post
from redlens.network.core import Network
from redlens.network.seeding import BrandVerdict, seeding_verdicts

_BRANDS = ["Seeded", "Organic", "Mixed"]
_ROSTER = "\n".join(_BRANDS) + "\n"
_DAY = 86_400
_T0 = 1_700_000_000

_BLOCK = ["seed1", "seed2", "seed3", "seed4"]
_ORGANIC = ["org1", "org2", "org3"]


def _seed_db(path: str) -> None:
    """Three planted brands over a coordinated block + an organic cohort.

    Seeded: all 4 block accounts first-mention it within ~2 days (a tight
    wave), no organic author ever does. Organic: organic authors mention it
    first + more; the block barely touches it, late. Mixed: the block first-
    pushes it but only 2 accounts (no tight wave), and organic adopts too.
    """
    engine = connect(path)
    init_schema(engine)
    posts: list[Post] = []
    comments: list[Comment] = []

    # --- Seeded: 4 block accounts, tight first-push wave, zero organic.
    for ai, acc in enumerate(_BLOCK):
        posts.append(Post(
            post_id=f"seeded-{acc}", author_username=acc,
            subreddit_name="coord", created_utc=_T0 + ai * _DAY // 2,
            title="try Seeded", selftext="Seeded is great", score=5))

    # --- Organic: organic authors first + repeatedly; the block only late,
    #     once. Organic out-mentions and predates the network.
    for i, acc in enumerate(_ORGANIC):
        posts.append(Post(
            post_id=f"organic-p-{acc}", author_username=acc,
            subreddit_name=f"life{i}", created_utc=_T0 + 5 * _DAY + i * _DAY,
            title="Organic worked for me", selftext="Organic Organic",
            score=1))
        comments.append(Comment(
            comment_id=f"organic-c-{acc}", author_username=acc,
            subreddit_name=f"life{i}", link_id=f"t-org-{i}",
            created_utc=_T0 + 6 * _DAY + i * _DAY, body="Organic again",
            score=1))
    posts.append(Post(
        post_id="organic-block", author_username="seed1",
        subreddit_name="coord", created_utc=_T0 + 200 * _DAY,
        title="Organic mention", selftext="Organic", score=1))

    # --- Mixed: block first-pushes it but only 2 accounts (no tight wave), and
    #     organic adopts later — the network still out-mentions organic, but
    #     without ratio dominance and without a wave → neither seeded nor
    #     organic-first.
    for ai, acc in enumerate(_BLOCK[:2]):
        for k in range(2):
            posts.append(Post(
                post_id=f"mixed-{acc}-{k}", author_username=acc,
                subreddit_name="coord",
                created_utc=_T0 + 10 * _DAY + ai * _DAY + k * 3600,
                title="Mixed here", selftext="Mixed", score=2))
    for i, acc in enumerate(_ORGANIC):
        posts.append(Post(
            post_id=f"mixed-org-{acc}", author_username=acc,
            subreddit_name=f"life{i}", created_utc=_T0 + 40 * _DAY + i * _DAY,
            title="Mixed too", selftext="Mixed also here", score=1))

    with Session(engine) as s:
        upsert(s, posts)
        upsert(s, comments)
        s.commit()


@pytest.fixture
def seeded(tmp_path):
    """``(path, roster, cohorts)`` for a freshly seeded two-cohort DB."""
    path = str(tmp_path / "seeding.db")
    _seed_db(path)
    cohorts = {**dict.fromkeys(_BLOCK, "coordinated"),
               **dict.fromkeys(_ORGANIC, "organic")}
    roster = [(b, [b]) for b in _BRANDS]
    return path, roster, cohorts


def _verdicts(path, roster, cohorts):
    net = Network(path, roster=roster, cohorts=cohorts)
    return {v.brand: v for v in seeding_verdicts(net._store)}


def test_seeded_brand_reads_seeded(seeded):
    by = _verdicts(*seeded)
    v = by["Seeded"]
    assert v.verdict == "seeded"
    assert v.first_mover_cohort == "coordinated"
    assert v.net_accounts == 4
    assert v.net_mentions == 4
    assert v.organic_mentions == 0
    assert v.wave_size >= 3               # a tight first-push wave
    assert v.organic_lag_days is None     # organic never adopted
    assert "wave" in v.evidence


def test_organic_first_brand_reads_organic_first(seeded):
    by = _verdicts(*seeded)
    v = by["Organic"]
    assert v.verdict == "organic-first"
    assert v.first_mover_cohort == "organic"
    # organic out-mentions the lone late block mention
    assert v.organic_mentions > v.net_mentions
    assert v.net_mentions >= 1


def test_mixed_brand_reads_inconclusive(seeded):
    by = _verdicts(*seeded)
    v = by["Mixed"]
    assert v.verdict == "inconclusive"
    # coordinated pushed first but with no tight wave and organic adoption
    assert v.first_mover_cohort == "coordinated"
    assert v.wave_size < 3
    assert v.organic_mentions >= 1
    assert v.organic_lag_days is not None


def test_verdicts_sorted_seeded_first(seeded):
    path, roster, cohorts = seeded
    net = Network(path, roster=roster, cohorts=cohorts)
    order = [v.verdict for v in seeding_verdicts(net._store)]
    # seeded rows precede inconclusive precede organic-first
    rank = {"seeded": 0, "inconclusive": 1, "organic-first": 2}
    assert order == sorted(order, key=lambda x: rank[x])


def test_no_roster_or_no_coordinated_yields_nothing(seeded):
    path, roster, cohorts = seeded
    # no roster → nothing to judge
    assert seeding_verdicts(Network(path, cohorts=cohorts)._store) == []
    # no coordinated cohort → no baseline → empty
    organic_only = dict.fromkeys(_ORGANIC, "organic")
    assert seeding_verdicts(
        Network(path, roster=roster, cohorts=organic_only)._store) == []


def test_non_organic_contrast_cohort_still_reads_organic_first(seeded):
    # The contrast cohort need not be named 'organic': a 'control' cohort that
    # mentions a brand first is still the first mover → organic-first, not
    # inconclusive. (The verdict keys on coordinated-vs-not, not the literal
    # label.)
    path, roster, _ = seeded
    cohorts = {**dict.fromkeys(_BLOCK, "coordinated"),
               **dict.fromkeys(_ORGANIC, "control")}
    by = {v.brand: v for v in
          seeding_verdicts(Network(path, roster=roster, cohorts=cohorts)._store)}
    v = by["Organic"]
    assert v.verdict == "organic-first"
    assert v.first_mover_cohort == "control"


def test_tie_resolves_to_organic_not_coordinated(tmp_path):
    # A coordinated and a non-coordinated account first-mention a brand at the
    # SAME timestamp — the block only wins on a strictly earlier push, so a tie
    # reads as organic-first (a synchronized same-instant arrival isn't a seed).
    path = str(tmp_path / "tie.db")
    engine = connect(path)
    init_schema(engine)
    with Session(engine) as s:
        upsert(s, [
            Post(post_id="tie-c", author_username="seed1",
                 subreddit_name="coord", created_utc=_T0,
                 title="Tie", selftext="Tie", score=1),
            Post(post_id="tie-o", author_username="org1",
                 subreddit_name="life", created_utc=_T0,
                 title="Tie", selftext="Tie", score=1)])
        s.commit()
    cohorts = {"seed1": "coordinated", "org1": "organic"}
    by = {v.brand: v for v in seeding_verdicts(
        Network(path, roster=[("Tie", ["Tie"])], cohorts=cohorts)._store)}
    assert by["Tie"].first_mover_cohort == "organic"
    assert by["Tie"].verdict == "organic-first"


def test_brand_verdict_as_dict_round_trips():
    v = BrandVerdict(brand="Seeded", verdict="seeded", net_accounts=4,
                     net_mentions=4, organic_mentions=0,
                     first_mover_cohort="coordinated", wave_size=4,
                     organic_lag_days=None, evidence="x")
    assert v.as_dict() == {
        "brand": "Seeded", "verdict": "seeded", "net_accounts": 4,
        "net_mentions": 4, "organic_mentions": 0,
        "first_mover_cohort": "coordinated", "wave_size": 4,
        "organic_lag_days": None, "evidence": "x"}


# --- CLI verb ---------------------------------------------------------------

def _sidecars(tmp_path, roster_text, cohorts):
    (tmp_path / "brands.csv").write_text(roster_text, encoding="utf-8")
    lines = "".join(f"{a}, {c}\n" for a, c in cohorts.items())
    (tmp_path / "cohorts.csv").write_text(lines, encoding="utf-8")


def _two_cohorts():
    return {**dict.fromkeys(_BLOCK, "coordinated"),
            **dict.fromkeys(_ORGANIC, "organic")}


def test_verb_text_table_lists_verdicts(tmp_path, capsys):
    db = str(tmp_path / "seeding.db")
    _seed_db(db)
    _sidecars(tmp_path, _ROSTER, _two_cohorts())
    rc = main(["--db", db, "seeding"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "brand" in out and "verdict" in out and "evidence" in out
    assert "Seeded" in out and "seeded" in out
    # seeded-first ordering: the seeded row precedes the organic-first row
    assert out.index("Seeded") < out.index("Organic")


def test_verb_json_shape(tmp_path, capsys):
    db = str(tmp_path / "seeding.db")
    _seed_db(db)
    _sidecars(tmp_path, _ROSTER, _two_cohorts())
    rc = main(["--db", db, "seeding", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert isinstance(data, list) and data
    assert set(data[0]) == {
        "brand", "verdict", "net_accounts", "net_mentions", "organic_mentions",
        "first_mover_cohort", "wave_size", "organic_lag_days", "evidence"}
    seeded = next(r for r in data if r["brand"] == "Seeded")
    assert seeded["verdict"] == "seeded"
    assert seeded["organic_lag_days"] is None


def test_verb_single_cohort_gate_exits_2(tmp_path, capsys):
    db = str(tmp_path / "seeding.db")
    _seed_db(db)
    # only one labeled cohort → nothing to contrast → non-zero, clear message
    _sidecars(tmp_path, _ROSTER, dict.fromkeys(_BLOCK, "coordinated"))
    rc = main(["--db", db, "seeding"])
    assert rc == 2
    assert "≥2 labeled cohorts" in capsys.readouterr().err


def test_verb_no_coordinated_cohort_gate_exits_2(tmp_path, capsys):
    db = str(tmp_path / "seeding.db")
    _seed_db(db)
    # two cohorts but neither is 'coordinated' → no network side → non-zero
    _sidecars(tmp_path, _ROSTER,
              {**dict.fromkeys(_BLOCK, "teamA"),
               **dict.fromkeys(_ORGANIC, "teamB")})
    rc = main(["--db", db, "seeding"])
    assert rc == 2
    assert "coordinated" in capsys.readouterr().err


def test_verb_missing_sidecar_exits_2(tmp_path, capsys):
    db = str(tmp_path / "seeding.db")
    _seed_db(db)
    rc = main(["--db", db, "seeding", "--brands", str(tmp_path / "nope.csv")])
    assert rc == 2
    assert "file not found" in capsys.readouterr().err
