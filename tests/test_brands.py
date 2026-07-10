"""``extract_brand_roster`` + the ``redlens brands`` verb — mine + canonicalize.

A synthetic coordinated cohort that co-mentions two brands (``Widgetco``,
``Zorptech``) mid-sentence, so both trip the capitalization-ratio +
multi-account-spread mining machinery, plus a prose word (``Really``) that only
ever starts sentences and must NOT qualify. Covers: (a) the keyless path yields
mined candidates in the CSV; (b) a FAKE ``llm.complete_json`` canonicalizes them
and the output CSV round-trips through ``load_brands``; (c) merge preserves a
curated row when a mined variant collides; (d) the verb exits 0 and writes the
file. Synthetic brand names only.
"""
from __future__ import annotations

import csv

import pytest
from sqlmodel import Session

from redlens import llm
from redlens.cli import main
from redlens.db import connect, init_schema, upsert
from redlens.models import Comment, Post
from redlens.network import load_brands
from redlens.network.brands import extract_brand_roster, parse_brand_extract
from redlens.network.core import Network

_T0 = 1_700_000_000


def _seed_db(path: str) -> None:
    """Four coordinated accounts that co-mention ``Widgetco`` and ``Zorptech``
    mid-sentence (so both clear the capitalization + ≥2-account thresholds), and
    a lone prose word (``Really``) that only starts sentences (never qualifies)."""
    engine = connect(path)
    init_schema(engine)
    posts: list[Post] = []
    comments: list[Comment] = []
    block = ["seed1", "seed2", "seed3", "seed4"]
    for i, acc in enumerate(block):
        # both brands appear MID-sentence ("I love Widgetco and Zorptech both"),
        # so they clear _CAP_MIN_RATIO; "Really" only ever starts a sentence.
        posts.append(Post(
            post_id=f"p-{acc}", author_username=acc, subreddit_name="coord",
            created_utc=_T0 + i,
            title="Really nice",
            selftext="Honestly I love Widgetco and Zorptech both a lot.",
            score=5))
        comments.append(Comment(
            comment_id=f"c-{acc}", author_username=acc, subreddit_name="coord",
            link_id="thread-x", created_utc=_T0 + i,
            body="Yeah Widgetco beats Zorptech in my view.", score=1))
    with Session(engine) as s:
        upsert(s, posts)
        upsert(s, comments)
        s.commit()


@pytest.fixture
def cohort(tmp_path):
    """``(path, cohorts)`` for a freshly seeded coordinated cohort."""
    path = str(tmp_path / "brands.db")
    _seed_db(path)
    cohorts = dict.fromkeys(["seed1", "seed2", "seed3", "seed4"], "coordinated")
    return path, cohorts


def _net(path, cohorts):
    return Network(path, cohorts=cohorts)


# --- extract_brand_roster ---------------------------------------------------

def test_keyless_yields_mined_candidates(cohort):
    path, cohorts = cohort
    roster = extract_brand_roster(_net(path, cohorts)._store, key=None,
                                  existing=[])
    names = {n for n, _ in roster}
    # both co-mentioned brands are mined; each is its own single match term
    assert {"Widgetco", "Zorptech"} <= names
    assert ("Widgetco", ["Widgetco"]) in roster
    # a sentence-start-only prose word never qualifies as a name
    assert "Really" not in names


def test_mining_scoped_to_coordinated_cohort(tmp_path):
    """Organic-authored brand chatter must NOT leak into the mined roster.

    The DB holds the coordinated cohort's ``Widgetco``/``Zorptech`` co-mentions
    PLUS two organic authors who co-mention a distinct ``Orgbrand`` — the exact
    shape brand-tracking produces once it pulls organic discussion in. Scoping
    the mine to ``store._coordinated`` keeps the roster to the network."""
    path = str(tmp_path / "mixed.db")
    _seed_db(path)                                  # coordinated cohort
    engine = connect(path)
    posts: list[Post] = []
    comments: list[Comment] = []
    for i, acc in enumerate(["org1", "org2"]):      # unlabeled → organic
        posts.append(Post(
            post_id=f"op-{acc}", author_username=acc, subreddit_name="organic",
            created_utc=_T0 + 100 + i, title="Nice day",
            selftext="Honestly I love Orgbrand a lot lately.", score=1))
        comments.append(Comment(
            comment_id=f"oc-{acc}", author_username=acc,
            subreddit_name="organic", link_id="thread-o",
            created_utc=_T0 + 100 + i, body="Yeah Orgbrand is fine.", score=1))
    with Session(engine) as s:
        upsert(s, posts)
        upsert(s, comments)
        s.commit()
    # only the coordinated block is labeled; org1/org2 are organic
    cohorts = dict.fromkeys(["seed1", "seed2", "seed3", "seed4"], "coordinated")
    roster = extract_brand_roster(Network(path, cohorts=cohorts)._store,
                                  key=None, existing=[])
    names = {n for n, _ in roster}
    assert {"Widgetco", "Zorptech"} <= names        # coordinated brands mined
    assert "Orgbrand" not in names                  # organic-only brand excluded


def test_llm_canonicalizes_and_round_trips(cohort, tmp_path, monkeypatch):
    path, cohorts = cohort

    # FAKE the LLM: canned canonicalization folding a mined alias into one brand
    # and dropping the rest. Asserts extract_brand_roster consumes it verbatim.
    def fake_complete_json(prompt, key, **kw):
        assert "Widgetco" in prompt          # mined candidates reach the prompt
        return {"brands": [
            {"name": "Widgetco", "match_terms": ["Widgetco", "widget co"]},
            {"name": "Zorptech", "match_terms": ["Zorptech"]},
            # a duplicate alias entry — parse_brand_extract must merge it in
            {"name": "widget co", "match_terms": ["widgetco inc"]},
        ]}

    monkeypatch.setattr(llm, "complete_json", fake_complete_json)
    roster = extract_brand_roster(_net(path, cohorts)._store, key="sk-test",
                                  existing=[])
    by = dict(roster)
    assert set(by) == {"Widgetco", "Zorptech"}     # deduped to two brands
    assert by["Widgetco"] == ["Widgetco", "widget co", "widgetco inc"]

    # the roster round-trips through the CSV load_brands reads
    out = tmp_path / "out.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        for name, terms in roster:
            w.writerow([name] if terms == [name] else [name, *terms])
    assert load_brands(out) == roster


def test_merge_preserves_curated_row(cohort):
    path, cohorts = cohort
    # a curated Widgetco with hand-tuned terms; mining must NOT overwrite it.
    existing = [("Widgetco", ["Widgetco", "widgetco.io", "WGCO"])]
    roster = extract_brand_roster(_net(path, cohorts)._store, key=None,
                                  existing=existing)
    by = dict(roster)
    # curated terms survive untouched (mined variant collides, existing wins)
    assert by["Widgetco"] == ["Widgetco", "widgetco.io", "WGCO"]
    # the genuinely new mined brand is still appended
    assert "Zorptech" in by
    # curated row keeps its leading position (never reordered)
    assert roster[0][0] == "Widgetco"


def test_parse_brand_extract_drops_and_defaults():
    parsed = parse_brand_extract({"brands": [
        {"name": "  Widgetco ", "match_terms": ["Widgetco"]},
        {"name": "", "match_terms": ["x"]},        # blank name → dropped
        {"name": "Zorptech"},                       # no terms → [name]
        "not-a-dict",                               # ignored
    ]})
    assert parsed == [("Widgetco", ["Widgetco"]), ("Zorptech", ["Zorptech"])]


# --- CLI verb ---------------------------------------------------------------

def _write_cohorts(tmp_path, cohorts):
    lines = "".join(f"{a}, {c}\n" for a, c in cohorts.items())
    (tmp_path / "cohorts.csv").write_text(lines, encoding="utf-8")


def test_verb_writes_roster_keyless(cohort, tmp_path, capsys, monkeypatch):
    path, cohorts = cohort
    _write_cohorts(tmp_path, cohorts)
    # force keyless regardless of the dev's local config
    monkeypatch.setattr("redlens.cli.llm_api_key", lambda: None)
    out = tmp_path / "brands.csv"
    rc = main(["--db", path, "brands", "--out", str(out)])
    assert rc == 0
    err = capsys.readouterr().err
    assert "classification skipped" in err or "skipped" in err
    loaded = load_brands(out)
    names = {n for n, _ in loaded}
    assert {"Widgetco", "Zorptech"} <= names
