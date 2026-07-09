"""End-to-end characterization of ``redlens.cli.main`` — every verb, in
process, against a tmp ``--db``.

These pin the CLI's *current* observable contract (stdout/stderr text shapes
and exit codes: 0 ok, 1 RedlensError / declined confirm, 2 NotFound or
MissingKey) before logic moves out of cli.py. Arctic and the LLM are always
mocked; no test touches the network or spends a key.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from redlens import arctic
from redlens.cli import main

NOW = int(time.time())


# --- offline stand-ins -------------------------------------------------------

class FakeArctic:
    """Stands in for ``arctic._get`` (the sync path): serves a fixed user's
    posts/comments honoring the after/before window, like tests/test_sync.py."""

    def __init__(self, posts: list[dict[str, Any]],
                 comments: list[dict[str, Any]],
                 meta: dict[str, Any] | None) -> None:
        self.posts, self.comments, self.meta = posts, comments, meta

    def get(self, path: str, **params: Any) -> dict[str, Any]:
        if path == "/api/users/search":
            return {"data": [self.meta] if self.meta else []}
        data = self.posts if "posts" in path else self.comments
        after, before = params.get("after"), params.get("before")
        items = [d for d in data
                 if (after is None or d["created_utc"] > after)
                 and (before is None or d["created_utc"] < before)]
        items.sort(key=lambda d: int(d["created_utc"]), reverse=True)
        return {"data": items[:50]}


def raw_post(pid: str, sub: str, *, author: str = "sampleuser",
             title: str = "about the topic", ts: int | None = None,
             score: int = 10) -> dict[str, Any]:
    return {"id": pid, "subreddit": sub, "author": author,
            "created_utc": ts or NOW - 3600, "title": title,
            "score": score, "num_comments": 1}


def fake_query(data: dict[str, list[dict[str, Any]]]):
    """Stands in for ``arctic.iter_subreddit_query`` (the track path)."""
    def it(subreddit, query, after=None, before=None):
        yield from data.get(subreddit, [])
    return it


@pytest.fixture(autouse=True)
def _absent_config(tmp_path, monkeypatch):
    """Point REDLENS_CONFIG at a nonexistent file so a developer's real
    config.toml (which may hold an LLM key) can't leak in. setenv runs after
    conftest's autouse delenv, so this override wins — by design."""
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "absent-config.toml"))


@pytest.fixture
def db(tmp_path) -> str:
    return str(tmp_path / "cli.db")


def _sync_sample_user(db: str, monkeypatch, *, posts: int = 2,
                      comments: int = 1) -> None:
    fake = FakeArctic(
        [raw_post(f"p{i}", "python", ts=NOW - 1000 + i) for i in range(posts)],
        [{"id": f"c{i}", "author": "sampleuser", "subreddit": "python",
          "link_id": f"t3_p{i}", "created_utc": NOW - 500 + i, "body": "b",
          "score": 2} for i in range(comments)],
        meta={"author": "sampleuser", "id": "t2_abc"})
    monkeypatch.setattr(arctic, "_get", fake.get)
    assert main(["--db", db, "sync", "sampleuser"]) == 0


def _track_sample_topic(db: str, monkeypatch, topic: str = "widgetco") -> None:
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"gadgets": [raw_post("p1", "gadgets")]}))
    assert main(["--db", db, "track", topic,
                 "--subreddits", "gadgets", "--sources", "none", "--yes"]) == 0


# --- init / version ----------------------------------------------------------

def test_init_creates_db_and_is_idempotent(db, capsys):
    assert main(["--db", db, "init"]) == 0
    assert Path(db).exists()
    assert f"schema applied to {db}" in capsys.readouterr().out
    assert main(["--db", db, "init"]) == 0            # re-running migrates, ok


def test_version_and_unknown_verb_exit_via_argparse(capsys):
    with pytest.raises(SystemExit) as e:
        main(["--version"])
    assert e.value.code == 0
    assert capsys.readouterr().out.startswith("redlens ")
    with pytest.raises(SystemExit) as e:
        main(["frobnicate"])
    assert e.value.code == 2


# --- sync / list / show ------------------------------------------------------

def test_sync_reports_counts(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    assert "u/sampleuser: 2 posts, 1 comments" in capsys.readouterr().out


def test_sync_unknown_user_exits_2(db, monkeypatch, capsys):
    fake = FakeArctic([], [], meta=None)              # arctic knows nothing
    monkeypatch.setattr(arctic, "_get", fake.get)
    assert main(["--db", db, "sync", "ghostuser"]) == 2
    assert "not found: u/ghostuser not in arctic" in capsys.readouterr().err


def test_list_empty_then_text_then_json(db, monkeypatch, capsys):
    assert main(["--db", db, "list"]) == 0
    assert "no users in DB" in capsys.readouterr().err

    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "list"]) == 0
    assert "u/sampleuser: 2 posts, 1 comments" in capsys.readouterr().out

    assert main(["--db", db, "list", "--json"]) == 0
    rows = json.loads(capsys.readouterr().out)
    assert [r["username"] for r in rows] == ["sampleuser"]
    assert rows[0]["total_posts"] == 2


def test_show_user_text_and_json(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "show", "sampleuser"]) == 0
    out = capsys.readouterr().out
    assert "u/sampleuser: 2 posts, 1 comments" in out
    assert "top r/python" in out

    assert main(["--db", db, "show", "sampleuser", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["username"] == "sampleuser"
    assert payload["total_posts"] == 2


def test_show_unknown_user_exits_2(db, capsys):
    assert main(["--db", db, "show", "ghostuser"]) == 2
    assert capsys.readouterr().err.startswith("not found: u/ghostuser")


def test_show_without_user_or_topic_exits_1(db, capsys):
    assert main(["--db", db, "show"]) == 1
    assert "error: show: give a username or --topic" in capsys.readouterr().err


def test_analytics_alias_still_works_with_deprecation_note(db, monkeypatch,
                                                           capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "analytics", "sampleuser"]) == 0
    got = capsys.readouterr()
    assert "'analytics' is deprecated; use 'show'" in got.err
    assert "u/sampleuser" in got.out


# --- export ------------------------------------------------------------------

def test_export_json_to_stdout(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "export", "sampleuser"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["username"] == "sampleuser"
    assert len(doc["posts"]) == 2 and len(doc["comments"]) == 1


def test_export_jsonl_tags_each_record(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "export", "sampleuser", "--format", "jsonl"]) == 0
    lines = [json.loads(ln) for ln in
             capsys.readouterr().out.strip().splitlines()]
    assert [r["kind"] for r in lines] == ["post", "post", "comment"]


def test_export_csv_to_file(db, tmp_path, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    out = tmp_path / "dump.csv"
    capsys.readouterr()
    assert main(["--db", db, "export", "sampleuser",
                 "--format", "csv", "-o", str(out)]) == 0
    assert "wrote 2 posts + 1 comments for u/sampleuser" in \
        capsys.readouterr().err
    header = out.read_text().splitlines()[0]
    assert header.startswith("kind,")


def test_export_topic_scope(db, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "export", "--topic", "widgetco"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["topic"] == "widgetco"
    assert [p["post_id"] for p in doc["posts"]] == ["p1"]


def test_export_scope_errors(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    # neither, and both, are RedlensError -> exit 1
    assert main(["--db", db, "export"]) == 1
    assert main(["--db", db, "export", "sampleuser", "--topic", "x"]) == 1
    assert capsys.readouterr().err.count(
        "error: export needs exactly one of <username> or --topic") == 2
    assert main(["--db", db, "export", "ghostuser"]) == 2   # NotFound
    assert "not found: u/ghostuser not in DB — sync first" in \
        capsys.readouterr().err


# --- track / topics ----------------------------------------------------------

def test_track_non_interactive_with_yes(db, monkeypatch, capsys):
    monkeypatch.setattr(arctic, "iter_subreddit_query",
                        fake_query({"gadgets": [raw_post("p1", "gadgets")]}))
    # --yes limits discovery to the keyless name search; stub it offline.
    searched: list[str] = []
    monkeypatch.setattr("redlens.cli.search_subreddits",
                        lambda term: searched.append(term) or [])
    assert main(["--db", db, "track", "widgetco",
                 "--subreddits", "gadgets", "--yes"]) == 0
    assert searched == ["widgetco"]                   # name source, topic term
    got = capsys.readouterr()
    assert "'widgetco': 1 new posts across 1 subreddits" in got.out
    assert "next: redlens page 'widgetco'" in got.out
    assert "relevance filter off (no LLM key)" in got.err


def test_track_sources_none_skips_discovery(db, monkeypatch, capsys):
    monkeypatch.setattr(arctic, "iter_subreddit_query", fake_query({}))
    monkeypatch.setattr("redlens.cli.search_subreddits",
                        lambda term: pytest.fail("--sources none must not "
                                                 "run name discovery"))
    assert main(["--db", db, "track", "widgetco",
                 "--subreddits", "gadgets", "--sources", "none", "--yes"]) == 0
    assert "0 new posts across 1 subreddits" in capsys.readouterr().out


def test_track_all_invalid_sources_exits_1(db, capsys):
    assert main(["--db", db, "track", "widgetco", "--sources", "bogus",
                 "--yes"]) == 1
    err = capsys.readouterr().err
    assert "ignoring unknown --sources: bogus" in err
    assert "error: --sources 'bogus' has no valid source" in err


def test_topics_text_and_json(db, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "topics"]) == 0
    assert "widgetco: 1 posts across 1 subreddits" in capsys.readouterr().out
    assert main(["--db", db, "topics", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "widgetco"


# --- untrack: the confirm matrix ----------------------------------------------

def test_untrack_unknown_topic_exits_2(db, capsys):
    assert main(["--db", db, "untrack", "ghost-topic", "-y"]) == 2
    assert "not found: topic 'ghost-topic' is not tracked" in \
        capsys.readouterr().err


def test_untrack_non_interactive_without_yes_aborts_1(db, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    capsys.readouterr()
    # stdin under pytest is not a tty -> the confirm declines by design.
    assert main(["--db", db, "untrack", "widgetco"]) == 1
    assert "untrack: aborted (pass -y to confirm non-interactively)" in \
        capsys.readouterr().err
    assert main(["--db", db, "topics", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)[0]["name"] == "widgetco"


def test_untrack_with_yes_deletes(db, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "untrack", "widgetco", "-y"]) == 0
    assert "untracked 'widgetco'" in capsys.readouterr().out


@pytest.mark.parametrize(("answer", "code"), [("y", 0), ("n", 1)])
def test_untrack_interactive_confirm(db, tmp_path, monkeypatch, capsys,
                                     answer, code):
    _track_sample_topic(db, monkeypatch)
    # A config file must exist or the first-run wizard would grab the tty.
    (tmp_path / "absent-config.toml").touch()
    monkeypatch.setattr(sys.stdin, "isatty", lambda: True)
    monkeypatch.setattr(sys.stdout, "isatty", lambda: True)
    monkeypatch.setattr("builtins.input", lambda: answer)
    capsys.readouterr()
    assert main(["--db", db, "untrack", "widgetco"]) == code


# --- page ---------------------------------------------------------------------

def test_page_writes_html_to_out(db, tmp_path, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    out = tmp_path / "widgetco.html"
    capsys.readouterr()
    assert main(["--db", db, "page", "widgetco", "-o", str(out)]) == 0
    assert f"wrote {out}" in capsys.readouterr().out
    doc = out.read_text()
    assert doc.startswith("<!doctype html>")
    assert "r/gadgets" in doc


def test_page_unknown_topic_exits_2(db, capsys):
    assert main(["--db", db, "page", "ghost-topic"]) == 2
    assert capsys.readouterr().err.startswith("not found:")


# --- summarize ----------------------------------------------------------------

def test_summarize_without_llm_key_exits_2(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "summarize", "sampleuser"]) == 2
    assert capsys.readouterr().err.startswith("error: no LLM API key")


def test_summarize_unknown_user_is_notfound_before_key_check(db, capsys):
    assert main(["--db", db, "summarize", "ghostuser"]) == 2
    assert capsys.readouterr().err.startswith("not found: u/ghostuser")


def test_summarize_without_user_or_topic_exits_1(db, capsys):
    assert main(["--db", db, "summarize"]) == 1
    assert "error: summarize: give a username or --topic" in \
        capsys.readouterr().err


def test_summarize_user_with_mocked_llm(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    monkeypatch.setenv("REDLENS_LLM_API_KEY", "test-key")
    monkeypatch.setattr("redlens.llm.complete_json", lambda prompt, key: {
        "demographics": {"gender": [
            {"label": "unknown", "confidence": 40, "reason": "r"}]},
        "interests": "python, gadgets"})
    capsys.readouterr()
    assert main(["--db", db, "summarize", "sampleuser"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("u/sampleuser (via ")
    assert "standard depth" in out                    # the default --depth
    assert "unknown (40%)" in out
    assert "Interests: python, gadgets" in out

    assert main(["--db", db, "summarize", "sampleuser", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["username"] == "sampleuser"
    assert payload["interests"] == "python, gadgets"


def test_summarize_topic_with_mocked_llm(db, monkeypatch, capsys):
    _track_sample_topic(db, monkeypatch)
    monkeypatch.setenv("REDLENS_LLM_API_KEY", "test-key")
    monkeypatch.setattr("redlens.llm.complete_json", lambda prompt, key: {
        "overview": "People discuss widgets.",
        "themes": [{"title": "Pricing", "summary": "too high"}],
        "sentiment": "Mixed.", "viewpoints": "Split."})
    capsys.readouterr()
    assert main(["--db", db, "summarize", "--topic", "widgetco"]) == 0
    out = capsys.readouterr().out
    assert out.startswith("'widgetco' (via ")
    assert "People discuss widgets." in out
    assert "• Pricing: too high" in out
    assert "Sentiment: Mixed." in out


# --- doctor -------------------------------------------------------------------

def test_doctor_no_network_passes_on_clean_env(db, capsys):
    assert main(["--db", db, "doctor", "--no-network"]) == 0
    out = capsys.readouterr().out
    assert "redlens doctor" in out
    assert "all required checks passed" in out
    assert "network probe skipped (--no-network)" in out


def test_doctor_json_shape(db, capsys):
    assert main(["--db", db, "doctor", "--no-network", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert {c["name"] for c in payload["checks"]} == {
        "database", "schema", "config file", "arctic-shift", "LLM key"}


def test_doctor_malformed_config_exits_1(db, tmp_path, monkeypatch, capsys):
    cfg = tmp_path / "broken.toml"
    cfg.write_text("[storage\n")                      # unclosed table header
    monkeypatch.setenv("REDLENS_CONFIG", str(cfg))
    assert main(["--db", db, "doctor", "--no-network"]) == 1
    assert "some required checks failed" in capsys.readouterr().out


# --- completions --------------------------------------------------------------

@pytest.mark.parametrize("shell", ["bash", "zsh", "fish"])
def test_completions_prints_a_script_with_every_verb(shell, capsys):
    assert main(["completions", shell]) == 0
    script = capsys.readouterr().out
    for verb in ("sync", "track", "untrack", "export", "doctor"):
        assert verb in script
    assert "redlens __complete" in script             # DB-backed value helper


def test_complete_helper_lists_db_values(db, monkeypatch, capsys):
    _sync_sample_user(db, monkeypatch)
    _track_sample_topic(db, monkeypatch)
    capsys.readouterr()
    assert main(["--db", db, "__complete", "users"]) == 0
    assert capsys.readouterr().out.splitlines() == ["sampleuser"]
    assert main(["--db", db, "__complete", "topics"]) == 0
    assert capsys.readouterr().out.splitlines() == ["widgetco"]


def test_complete_helper_never_creates_the_db(tmp_path, capsys):
    missing = tmp_path / "nope.db"
    assert main(["--db", str(missing), "__complete", "users"]) == 0
    assert capsys.readouterr().out == ""
    assert not missing.exists()                       # read-only, no side effect


# --- BrokenPipeError ------------------------------------------------------------

def test_broken_pipe_exits_0(db, monkeypatch):
    # The handler dup2s devnull over stdout's fd so the interpreter's final
    # flush can't re-raise; stub the fd surgery (it would clobber pytest's
    # capture fd) and characterize the contract: swallowed, exit 0.
    repointed: list[tuple[int, int]] = []
    monkeypatch.setattr("redlens.cli.os.dup2",
                        lambda a, b: repointed.append((a, b)))
    monkeypatch.setattr("redlens.cli.list_users",
                        lambda s: (_ for _ in ()).throw(BrokenPipeError()))
    assert main(["--db", db, "list"]) == 0
    assert repointed                                  # stdout was re-pointed
