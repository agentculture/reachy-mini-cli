"""Tests for the stdlib-urllib LLM streaming client (``reachy.speech.llm``).

These tests stub ``urllib.request.urlopen`` so no live server is needed: an
SSE byte stream is fed to the parser and we assert that complete sentences are
emitted *incrementally* — the first sentence must be yielded before the later
deltas have even been consumed.
"""

from __future__ import annotations

import io
import json

import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
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
    """Clear every LLM env var so config resolution is hermetic.

    The operator box exports ``REACHY_OPENAI_*`` in ``.bashrc``; without this the
    real shell env would leak into the resolution tests and shadow the
    monkeypatched values.
    """
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


def _sse_chunk(content: str) -> bytes:
    """Encode one OpenAI SSE ``data:`` line carrying a content delta."""
    payload = {"choices": [{"delta": {"content": content}}]}
    return f"data: {json.dumps(payload)}\n\n".encode("utf-8")


class _RecordingByteStream(io.BytesIO):
    """A BytesIO that records the byte offset reached at each read.

    Lets a test observe *when* (relative to the producer) each byte range was
    consumed, proving the parser pulls lazily rather than buffering whole.
    """

    def __init__(self, data: bytes):
        super().__init__(data)
        self.read_marks: list[int] = []

    def read(self, size=-1):
        chunk = super().read(size)
        self.read_marks.append(self.tell())
        return chunk

    def readline(self, size=-1):
        line = super().readline(size)
        self.read_marks.append(self.tell())
        return line


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

    # urllib's response is a file-like object; TextIOWrapper wraps it.
    def read(self, size=-1):
        return self._stream.read(size)

    def readline(self, size=-1):
        return self._stream.readline(size)

    def readable(self):
        return True


def _stub_urlopen(monkeypatch, body: bytes, status: int = 200):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return captured


# ---------------------------------------------------------------------------
# Sentence splitter (pure logic, ported from the reference)
# ---------------------------------------------------------------------------


def test_split_buffer_quote_aware():
    text = '"Hey! What\'s up?" he asked. Then he left.'
    sentences, remaining = llm._split_buffer(text)
    # The "!" and "?" inside the quotes must NOT split.
    assert sentences == ['"Hey! What\'s up?" he asked.']
    assert remaining.strip() == "Then he left."


def test_split_buffer_paren_aware():
    text = "He paused (well, really!) and went on. Done now."
    sentences, remaining = llm._split_buffer(text)
    assert sentences == ["He paused (well, really!) and went on."]
    assert remaining.strip() == "Done now."


def test_split_buffer_loose_fallback():
    text = "no caps after this. lowercase keeps going"
    # Normal mode finds no break (next char is lowercase).
    assert llm._split_buffer(text)[0] == []
    # Loose mode splits on any terminal punctuation + whitespace.
    sentences, _ = llm._split_buffer(text, loose=True)
    assert sentences == ["no caps after this."]


# ---------------------------------------------------------------------------
# SSE parsing + incremental yielding
# ---------------------------------------------------------------------------


def test_stream_chat_completion_parses_deltas(monkeypatch):
    body = _sse_chunk("Hello ") + _sse_chunk("world.") + b"data: [DONE]\n\n"
    _stub_urlopen(monkeypatch, body)
    deltas = list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert deltas == ["Hello ", "world."]


def test_done_sentinel_stops_stream(monkeypatch):
    body = _sse_chunk("First. ") + b"data: [DONE]\n\n" + _sse_chunk("never seen")
    _stub_urlopen(monkeypatch, body)
    deltas = list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert deltas == ["First. "]


def test_stream_sentences_yields_early(monkeypatch):
    """The first complete sentence must be emitted before later deltas arrive.

    We build a generator whose later chunks are produced lazily; if
    ``stream_sentences`` buffered the whole response it would consume every
    chunk before yielding. Instead, after pulling the first sentence we assert
    the producer has NOT yet been drained.
    """
    chunks = [
        _sse_chunk("First sentence here. "),
        _sse_chunk("Second one follows. "),
        _sse_chunk("Third and last."),
        b"data: [DONE]\n\n",
    ]
    consumed: list[int] = []

    class LazyResponse:
        def __init__(self):
            self._idx = 0
            self._buf = b""

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def getcode(self):
            return 200

        def readable(self):
            return True

        def _pull(self):
            if self._idx < len(chunks):
                self._buf += chunks[self._idx]
                consumed.append(self._idx)
                self._idx += 1

        def read(self, size=-1):
            # Pull one chunk at a time so consumption is observable.
            while len(self._buf) < (size if size and size > 0 else 1):
                before = self._idx
                self._pull()
                if before == self._idx:
                    break
            if size and size > 0:
                out, self._buf = self._buf[:size], self._buf[size:]
            else:
                out, self._buf = self._buf, b""
            return out

        def readline(self, size=-1):
            while b"\n" not in self._buf:
                before = self._idx
                self._pull()
                if before == self._idx:
                    break
            nl = self._buf.find(b"\n")
            if nl == -1:
                out, self._buf = self._buf, b""
            else:
                out, self._buf = self._buf[: nl + 1], self._buf[nl + 1 :]
            return out

    monkeypatch.setattr(llm.urllib.request, "urlopen", lambda req, timeout=None: LazyResponse())

    gen = llm.stream_sentences([{"role": "user", "content": "hi"}])
    first = next(gen)
    assert first == "First sentence here."
    # The whole stream must NOT have been consumed to produce the first sentence.
    assert (
        max(consumed) < len(chunks) - 1
    ), f"stream over-consumed: drained chunks {consumed} to get first sentence"

    rest = list(gen)
    assert rest == ["Second one follows.", "Third and last."]


def test_stream_sentences_flushes_tail(monkeypatch):
    # "Two." breaks before the uppercase "Then"; the lowercase "dangling tail"
    # has no boundary, so it is flushed as the final partial buffer at EOF.
    body = (
        _sse_chunk("One. ")
        + _sse_chunk("Two. ")
        + _sse_chunk("Then dangling tail")
        + b"data: [DONE]\n\n"
    )
    _stub_urlopen(monkeypatch, body)
    out = list(llm.stream_sentences([{"role": "user", "content": "hi"}]))
    assert out == ["One.", "Two.", "Then dangling tail"]


# ---------------------------------------------------------------------------
# Config resolution
# ---------------------------------------------------------------------------


def test_config_from_env(monkeypatch):
    """Canonical ``REACHY_OPENAI_*`` vars drive resolution."""
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://env-host:9000")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "env-key")
    monkeypatch.setenv("REACHY_OPENAI_MODEL_ID", "env-model")
    cfg = llm.LlmConfig.resolve()
    assert cfg.base_url == "http://env-host:9000"
    assert cfg.api_key == "env-key"
    assert cfg.model == "env-model"


def test_config_legacy_llm_env_fallback(monkeypatch):
    """Legacy ``REACHY_LLM_*`` names still resolve when no ``REACHY_OPENAI_*`` set."""
    monkeypatch.setenv("REACHY_LLM_BASE_URL", "http://legacy-host:9000")
    monkeypatch.setenv("REACHY_LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("REACHY_LLM_MODEL", "legacy-model")
    cfg = llm.LlmConfig.resolve()
    assert cfg.base_url == "http://legacy-host:9000"
    assert cfg.api_key == "legacy-key"
    assert cfg.model == "legacy-model"


def test_config_openai_env_takes_precedence_over_legacy(monkeypatch):
    """When both name sets are present, ``REACHY_OPENAI_*`` wins."""
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://new-host:1111")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "new-key")
    monkeypatch.setenv("REACHY_OPENAI_MODEL_ID", "new-model")
    monkeypatch.setenv("REACHY_LLM_BASE_URL", "http://legacy-host:9000")
    monkeypatch.setenv("REACHY_LLM_API_KEY", "legacy-key")
    monkeypatch.setenv("REACHY_LLM_MODEL", "legacy-model")
    cfg = llm.LlmConfig.resolve()
    assert cfg.base_url == "http://new-host:1111"
    assert cfg.api_key == "new-key"
    assert cfg.model == "new-model"


def test_config_arg_overrides_env(monkeypatch):
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://env-host:9000")
    monkeypatch.setenv("REACHY_OPENAI_MODEL_ID", "env-model")
    cfg = llm.LlmConfig.resolve(base_url="http://override:1234", model="override-model")
    assert cfg.base_url == "http://override:1234"
    assert cfg.model == "override-model"


def test_empty_openai_key_does_not_fall_back_to_legacy(monkeypatch):
    """An explicitly empty ``REACHY_OPENAI_API_KEY`` must NOT leak the legacy key.

    Precedence is presence-based: a *set* (even empty) primary wins over the
    legacy name, so an empty canonical key means "no auth", not "use
    ``REACHY_LLM_API_KEY``" (Qodo PR #52 finding 1 — empty env breaks precedence).
    """
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "")
    monkeypatch.setenv("REACHY_LLM_API_KEY", "legacy-secret")
    cfg = llm.LlmConfig.resolve()
    assert cfg.api_key == ""


def test_empty_openai_key_sends_no_bearer_header(monkeypatch):
    """The empty-key precedence fix reaches the wire: no stale legacy Bearer."""
    body = b"data: [DONE]\n\n"
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "")
    monkeypatch.setenv("REACHY_LLM_API_KEY", "legacy-secret")
    list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert captured["req"].get_header("Authorization") is None


def test_empty_openai_url_base_overrides_legacy(monkeypatch):
    """A set-but-empty ``REACHY_OPENAI_URL_BASE`` wins over legacy + default."""
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "")
    monkeypatch.setenv("REACHY_LLM_BASE_URL", "http://legacy-host:9000")
    cfg = llm.LlmConfig.resolve()
    assert cfg.base_url == ""


def test_bearer_header_set_when_key_present(monkeypatch):
    body = b"data: [DONE]\n\n"
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "secret123")
    list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert captured["req"].get_header("Authorization") == "Bearer secret123"


def test_bearer_header_absent_for_empty_key(monkeypatch):
    body = b"data: [DONE]\n\n"
    captured = _stub_urlopen(monkeypatch, body)
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "EMPTY")
    list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert captured["req"].get_header("Authorization") is None


# ---------------------------------------------------------------------------
# Error contract
# ---------------------------------------------------------------------------


def test_unreachable_raises_clierror(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)

    with pytest.raises(CliError) as ei:
        list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert ei.value.code == EXIT_ENV_ERROR
    assert ei.value.remediation  # a non-empty hint, no traceback


def test_non_200_raises_clierror(monkeypatch):
    _stub_urlopen(monkeypatch, b"upstream boom", status=500)
    with pytest.raises(CliError) as ei:
        list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert ei.value.code == EXIT_ENV_ERROR
