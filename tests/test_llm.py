"""The one LLM HTTP call: structured-output (JSON mode) wiring. urlopen is
mocked, so no network — we inspect the request body that would be sent."""
import json
import urllib.request

from redlens import llm


class _Resp:
    def __init__(self, payload: dict) -> None:
        self._b = json.dumps(payload).encode()

    def read(self) -> bytes:
        return self._b

    def __enter__(self) -> "_Resp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _capture_body(monkeypatch) -> dict:
    """Patch urlopen to record the outgoing request body and return a canned
    completion. Returns a dict that gets a 'body' key once complete() runs."""
    seen: dict = {}

    def fake_urlopen(req, *a, **k):
        seen["body"] = json.loads(req.data)
        return _Resp({"choices": [{"message": {"content": "ok"}}]})

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
