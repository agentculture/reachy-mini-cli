"""Tests for the non-streaming ``complete()`` function in ``reachy.speech.llm``.

These tests stub ``urllib.request.urlopen`` so no live server is needed.
They assert:
  - ``complete()`` sends ``"stream": false`` in the request body.
  - The full assistant text is returned as one string.
  - ``REACHY_OPENAI_*`` / legacy ``REACHY_LLM_*`` env vars are honoured.
  - A network error / timeout propagates as an OSError (never swallowed).
  - The ``timeout`` argument is threaded through to ``urlopen``.
"""

from __future__ import annotations

import io
import json
import urllib.error

import pytest

from reachy.speech import llm

_LLM_ENV_VARS = (
    "REACHY_OPENAI_URL_BASE",
    "REACHY_OPENAI_MODEL_ID",
    "REACHY_OPENAI_API_KEY",
    "REACHY_LLM_BASE_URL",
    "REACHY_LLM_MODEL",
    "REACHY_LLM_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Clear every LLM env var so config resolution is hermetic."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _non_streaming_response(content: str, status: int = 200) -> bytes:
    """Build an OpenAI-compatible non-streaming JSON response body."""
    payload = {
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
                "index": 0,
            }
        ]
    }
    return json.dumps(payload).encode("utf-8")


class _FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200):
        self._stream = io.BytesIO(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._stream.read(size)

    def readline(self, size=-1):
        return self._stream.readline(size)

    def readable(self):
        return True


def _stub_urlopen(monkeypatch, body: bytes, status: int = 200):
    """Patch urlopen; return a dict that captures the request + timeout."""
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return captured


# ---------------------------------------------------------------------------
# AC-1: returns full text as one string
# ---------------------------------------------------------------------------


def test_complete_returns_full_text(monkeypatch):
    """``complete()`` returns the full assistant message as one string."""
    body = _non_streaming_response("Hello, I am Reachy.")
    _stub_urlopen(monkeypatch, body)
    result = llm.complete([{"role": "user", "content": "hi"}])
    assert result == "Hello, I am Reachy."


def test_complete_concatenates_content(monkeypatch):
    """Content with spaces / punctuation is returned verbatim."""
    text = "First sentence. Second sentence. Third."
    body = _non_streaming_response(text)
    _stub_urlopen(monkeypatch, body)
    result = llm.complete([{"role": "user", "content": "tell me things"}])
    assert result == text


# ---------------------------------------------------------------------------
# AC-2a: request body carries "stream": false
# ---------------------------------------------------------------------------


def test_complete_sends_stream_false(monkeypatch):
    """``complete()`` must set ``"stream": false`` in the POST body."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    sent_body = json.loads(captured["req"].data)
    assert sent_body["stream"] is False


def test_complete_does_not_set_stream_true(monkeypatch):
    """Sanity: make sure we're not accidentally sending ``stream: true``."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    sent_body = json.loads(captured["req"].data)
    assert sent_body.get("stream") is False


# ---------------------------------------------------------------------------
# AC-2b: env-var resolution
# ---------------------------------------------------------------------------


def test_complete_honours_openai_env_vars(monkeypatch):
    """``REACHY_OPENAI_*`` vars reach the request URL and headers."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://custom-host:1234")
    monkeypatch.setenv("REACHY_OPENAI_MODEL_ID", "custom-model")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "custom-key")

    llm.complete([{"role": "user", "content": "hi"}])

    assert "custom-host:1234" in captured["req"].full_url
    sent_body = json.loads(captured["req"].data)
    assert sent_body["model"] == "custom-model"
    assert captured["req"].get_header("Authorization") == "Bearer custom-key"


def test_complete_honours_legacy_llm_env_vars(monkeypatch):
    """Legacy ``REACHY_LLM_*`` names still work for ``complete()``."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_LLM_BASE_URL", "http://legacy-host:5678")
    monkeypatch.setenv("REACHY_LLM_MODEL", "legacy-model")
    monkeypatch.setenv("REACHY_LLM_API_KEY", "legacy-key")

    llm.complete([{"role": "user", "content": "hi"}])

    assert "legacy-host:5678" in captured["req"].full_url
    sent_body = json.loads(captured["req"].data)
    assert sent_body["model"] == "legacy-model"


def test_complete_explicit_kwargs_override_env(monkeypatch):
    """Explicit ``base_url=`` / ``model=`` / ``api_key=`` kwargs win over env."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://env-host:9999")
    monkeypatch.setenv("REACHY_OPENAI_MODEL_ID", "env-model")

    llm.complete(
        [{"role": "user", "content": "hi"}],
        base_url="http://kwarg-host:1111",
        model="kwarg-model",
    )

    assert "kwarg-host:1111" in captured["req"].full_url
    sent_body = json.loads(captured["req"].data)
    assert sent_body["model"] == "kwarg-model"


# ---------------------------------------------------------------------------
# AC-3a: connection errors propagate (not swallowed)
# ---------------------------------------------------------------------------


def test_complete_propagates_url_error(monkeypatch):
    """A ``URLError`` (unreachable endpoint) must propagate as an exception."""

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)

    with pytest.raises((urllib.error.URLError, OSError)):
        llm.complete([{"role": "user", "content": "hi"}])


def test_complete_propagates_timeout_error(monkeypatch):
    """A ``socket.timeout`` (or ``TimeoutError``) must propagate, not hang."""
    import socket

    def timeout_urlopen(req, timeout=None):  # noqa: ANN001
        raise socket.timeout("timed out")

    monkeypatch.setattr(llm.urllib.request, "urlopen", timeout_urlopen)

    with pytest.raises((socket.timeout, OSError, TimeoutError)):
        llm.complete([{"role": "user", "content": "hi"}], timeout=1.0)


# ---------------------------------------------------------------------------
# AC-3b: timeout is threaded through to urlopen
# ---------------------------------------------------------------------------


def test_complete_passes_timeout_to_urlopen(monkeypatch):
    """The ``timeout`` argument must be forwarded to ``urllib.request.urlopen``."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}], timeout=7.5)
    assert captured["timeout"] == 7.5


def test_complete_default_timeout_is_bounded(monkeypatch):
    """The default timeout for ``complete()`` must be finite and short (≤ 30 s).

    Unlike the streaming path (120 s default), ``complete()`` targets a
    classifier use-case where a slow/unreachable endpoint should fail quickly.
    """
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    assert captured["timeout"] is not None
    assert captured["timeout"] <= 30.0


# ---------------------------------------------------------------------------
# Bonus: request carries enable_thinking=False (same as streaming path)
# ---------------------------------------------------------------------------


def test_complete_disables_thinking_in_template_kwargs(monkeypatch):
    """``chat_template_kwargs.enable_thinking`` must be ``False``."""
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    sent_body = json.loads(captured["req"].data)
    assert sent_body.get("chat_template_kwargs", {}).get("enable_thinking") is False


# ---------------------------------------------------------------------------
# Qodo #2: Accept header matches the response shape we parse
# ---------------------------------------------------------------------------


def test_complete_requests_json_accept_header(monkeypatch):
    """``complete()`` parses a JSON body, so it must send ``Accept: application/json``.

    A hardcoded ``Accept: text/event-stream`` (the streaming default) can cause an
    OpenAI-compatible server to reply with SSE even for ``stream=false``, breaking the
    ``json.loads`` and degrading the engagement classifier for no reason.
    """
    body = _non_streaming_response("ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    assert captured["req"].get_header("Accept") == "application/json"


def test_streaming_request_still_sends_sse_accept_header():
    """The streaming path is unchanged: ``_build_request(stream=True)`` keeps SSE Accept."""
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(
        cfg, [{"role": "user", "content": "hi"}], temperature=0.8, max_tokens=None, stream=True
    )
    assert req.get_header("Accept") == "text/event-stream"


def test_non_streaming_build_request_sends_json_accept_header():
    """``_build_request(stream=False)`` advertises a JSON response."""
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(
        cfg, [{"role": "user", "content": "hi"}], temperature=0.8, max_tokens=None, stream=False
    )
    assert req.get_header("Accept") == "application/json"
