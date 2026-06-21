"""The one LLM HTTP call: structured-output (JSON mode) wiring. urlopen is
mocked, so no network — we inspect the request body that would be sent."""
from __future__ import annotations

import json
import urllib.request

import pytest

from redlens import llm
from redlens.errors import RedlensError


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _capture_body(monkeypatch, *, finish_reason: str | None = None) -> dict:
    """Patch urlopen to record the outgoing request body and return a canned
    completion (optionally with a ``finish_reason``). Returns a dict that gets a
    'body' key once complete() runs."""
    seen: dict = {}
    choice: dict = {"message": {"content": "ok"}}
    if finish_reason is not None:
        choice["finish_reason"] = finish_reason

    def fake_urlopen(req, *a, **k):
        seen["body"] = json.loads(req.data)
        return _Resp({"choices": [choice]})

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    return seen


def test_json_object_sets_response_format(monkeypatch):
    seen = _capture_body(monkeypatch)
    out = llm.complete("return a json object", "sk-test", json_object=True)
    assert out == "ok"
    assert seen["body"]["response_format"] == {"type": "json_object"}


def test_default_completion_has_no_response_format(monkeypatch):
    seen = _capture_body(monkeypatch)
    llm.complete("just some prose", "sk-test")
    assert "response_format" not in seen["body"]


def test_truncated_json_reply_raises_clearly(monkeypatch):
    # JSON mode + finish_reason 'length' means the object was cut off -> half a
    # JSON document. complete() must flag the truncation, not hand back junk.
    _capture_body(monkeypatch, finish_reason="length")
    with pytest.raises(RedlensError, match="truncated"):
        llm.complete("return a json object", "sk-test", json_object=True)


def test_length_finish_is_fine_for_free_text(monkeypatch):
    # Free-text calls (e.g. discovery's subreddit list) legitimately take what
    # fits; a 'length' finish there is not an error.
    _capture_body(monkeypatch, finish_reason="length")
    assert llm.complete("list things", "sk-test") == "ok"
