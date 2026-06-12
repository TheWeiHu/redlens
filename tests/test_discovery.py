import json
import re

import pytest

from redlens import cli, discovery
from redlens.topics import SubredditCandidate


@pytest.fixture(autouse=True)
def isolate_config(monkeypatch, tmp_path):
    for var in ("REDLENS_LLM_API_KEY", "ANTHROPIC_API_KEY", "OPENAI_API_KEY"):
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("REDLENS_CONFIG", str(tmp_path / "absent.toml"))


def test_popular_list_is_sane():
    assert len(discovery.POPULAR_SUBREDDITS) == 100
    assert len(set(s.lower() for s in discovery.POPULAR_SUBREDDITS)) == 100
    assert all(re.match(r"^[A-Za-z0-9_]{2,21}$", s)
               for s in discovery.POPULAR_SUBREDDITS)


def test_web_search_mines_and_ranks_subreddit_names(monkeypatch):
    html = b"""
    <a href="https://www.reddit.com/r/Ozempic/comments/abc/x/">one</a>
    <a href="//duckduckgo.com/l/?uddg=https%3A%2F%2Freddit.com%2Fr%2Floseit%2F">two</a>
    <a href="https://reddit.com/r/loseit/comments/d/">three</a>
    <a href="https://reddit.com/r/all/">junk</a>
    """
    monkeypatch.setattr(discovery, "_http", lambda req: html)
    names = discovery.search_web("ozempic")
    assert names[0] == "loseit"            # two mentions (one URL-encoded)
    assert "Ozempic" in names
    assert "all" not in names              # junk filtered


def test_global_search_counts_subreddits(monkeypatch):
    payload = json.dumps({"data": [
        {"subreddit": "Ozempic"}, {"subreddit": "Mounjaro"},
        {"subreddit": "Ozempic"}, {"subreddit": "u_someuser"},
        {"subreddit": None}, {"subreddit": "Semaglutide"},
    ]}).encode()
    monkeypatch.setattr(discovery, "_http", lambda req: payload)
    names = discovery.search_global("ozempic")
    assert names[0] == "Ozempic"                       # two mentions
    assert set(names) == {"Ozempic", "Mounjaro", "Semaglutide"}  # profiles dropped


def test_suggest_llm_without_key_is_empty(monkeypatch):
    monkeypatch.setattr(
        discovery, "_http",
        lambda req: pytest.fail("must not call out without a key"),
    )
    assert discovery.suggest_llm("ozempic") == []


def test_suggest_llm_anthropic_shape(monkeypatch):
    monkeypatch.setenv("REDLENS_LLM_API_KEY", "sk-ant-test")
    captured = {}

    def fake_http(req):
        captured["url"] = req.full_url
        captured["body"] = json.loads(req.data)
        return json.dumps({"content": [
            {"type": "text",
             "text": "Ozempic\n- r/diabetes\n* loseit\nnot a subreddit!!\n"},
        ]}).encode()

    monkeypatch.setattr(discovery, "_http", fake_http)
    names = discovery.suggest_llm("ozempic")
    assert names == ["Ozempic", "diabetes", "loseit"]
    assert "anthropic.com" in captured["url"]
    assert captured["body"]["model"] == discovery.DEFAULT_ANTHROPIC_MODEL


def test_suggest_llm_openai_shape(monkeypatch):
    monkeypatch.setenv("OPENAI_API_KEY", "sk-proj-test")
    monkeypatch.setattr(
        discovery, "_http",
        lambda req: json.dumps(
            {"choices": [{"message": {"content": "loseit\ndiabetes"}}]}
        ).encode(),
    )
    assert discovery.suggest_llm("ozempic") == ["loseit", "diabetes"]


def _tty(monkeypatch, *answers):
    monkeypatch.setattr("sys.stdin.isatty", lambda: True)
    monkeypatch.setattr("sys.stdout.isatty", lambda: True)
    lines = iter(answers)
    monkeypatch.setattr("builtins.input", lambda: next(lines))


def test_choose_sources_defaults_and_skip(monkeypatch):
    monkeypatch.setattr("sys.stdin.isatty", lambda: False)
    assert cli._choose_sources(assume_yes=False) == ["name"]

    _tty(monkeypatch, "")
    assert cli._choose_sources(assume_yes=False) == ["name", "global"]
    _tty(monkeypatch, "s")
    assert cli._choose_sources(assume_yes=False) == []
    _tty(monkeypatch, "4")
    assert cli._choose_sources(assume_yes=False) == ["popular"]
    _tty(monkeypatch, "1 5")          # llm chosen but no key configured
    assert cli._choose_sources(assume_yes=False) == ["name"]


def test_gather_merges_sources_and_tags(monkeypatch):
    monkeypatch.setattr(
        "redlens.cli.search_subreddits",
        lambda topic: [SubredditCandidate(
            name="Ozempic", subscribers=111_787, description="", over_18=False)],
    )
    monkeypatch.setattr(discovery, "search_web",
                        lambda topic: ["Ozempic", "loseit"])
    monkeypatch.setattr(discovery, "suggest_llm", lambda topic: ["loseit"])

    cands, popular = cli._gather_candidates(["ozempic"],
                                            ["name", "web", "llm", "popular"])
    by_name = {c.name: c for c in cands}
    assert by_name["Ozempic"].source == "name+web"
    assert by_name["Ozempic"].subscribers == 111_787   # name data wins
    assert by_name["loseit"].source == "web+llm"
    assert popular == discovery.POPULAR_SUBREDDITS


def test_gather_notes_an_empty_source(monkeypatch, capsys):
    monkeypatch.setattr("redlens.cli.search_subreddits", lambda topic: [])
    monkeypatch.setattr(discovery, "search_web", lambda topic: [])
    cands, _ = cli._gather_candidates(["x"], ["name", "web"])
    assert cands == []
    assert "web search found no subreddits" in capsys.readouterr().err


def test_gather_fans_out_across_query_terms(monkeypatch):
    searched = []
    monkeypatch.setattr(
        "redlens.cli.search_subreddits",
        lambda term: searched.append(term) or [],
    )
    cli._gather_candidates(["ubi", "universal basic income"], ["name"])
    assert searched == ["ubi", "universal basic income"]


def test_gather_survives_a_failing_source(monkeypatch):
    from redlens.errors import RedlensError

    def boom(topic):
        raise RedlensError("ddg unreachable")

    monkeypatch.setattr("redlens.cli.search_subreddits", lambda topic: [])
    monkeypatch.setattr(discovery, "search_web", boom)
    cands, popular = cli._gather_candidates(["x"], ["name", "web"])
    assert cands == [] and popular == []
