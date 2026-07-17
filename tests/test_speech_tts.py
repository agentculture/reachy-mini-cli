"""Tests for reachy.speech.tts — TTS synth client (stdlib urllib, Magpie-style).

Tests are written test-first per the acceptance criteria:
  1. synthesize() returns non-empty PCM bytes from a stub HTTP endpoint.
  2. clean_for_tts() strips markdown/emoji; split_for_tts() splits multi-sentence text.
  3. An unreachable TTS URL raises CliError(code=2) with a hint line.
"""

from __future__ import annotations

import io
import urllib.error
import urllib.request

import pytest

from reachy.cli._errors import CliError
from reachy.speech.tts import clean_for_tts, split_for_tts, synthesize

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _fake_pcm(n_bytes: int = 1024) -> bytes:
    """Return plausible fake PCM16 bytes (non-empty, length divisible by 2)."""
    return b"\x00\x01" * (n_bytes // 2)


class _FakeResponse:
    """Minimal file-like object that urllib.request.urlopen returns."""

    def __init__(self, data: bytes, status: int = 200) -> None:
        self._data = io.BytesIO(data)
        self.status = status

    def read(self) -> bytes:
        return self._data.read()

    def getcode(self) -> int:
        return self.status

    def __enter__(self):
        return self

    def __exit__(self, *_):
        pass


# ---------------------------------------------------------------------------
# Acceptance criterion 1 — synthesize() returns non-empty PCM bytes
# ---------------------------------------------------------------------------


def test_synthesize_returns_pcm_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """A stub HTTP endpoint → synthesize() returns non-empty PCM bytes."""
    pcm = _fake_pcm(2048)
    fake_resp = _FakeResponse(pcm)

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: fake_resp,
    )

    result = synthesize("Hello, robot!", tts_url="http://stub:9000")
    assert isinstance(result, bytes)
    assert len(result) > 0


def test_synthesize_returns_pcm_for_multi_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    """Long text that splits into multiple chunks concatenates all PCM results."""
    pcm_chunk = _fake_pcm(512)

    def _fake_urlopen(req, timeout=None):
        return _FakeResponse(pcm_chunk)

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    # force a split by passing a short max_chars so we exercise chunk joining
    long_text = "Hello world. " * 60  # ~780 chars — exceeds default 600-char max
    result = synthesize(long_text, tts_url="http://stub:9000")
    assert len(result) > len(pcm_chunk)  # multiple chunks concatenated


def test_synthesize_empty_text_returns_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """Text that cleans to empty returns b'' without hitting the network."""
    called = []

    def _should_not_call(req, timeout=None):
        called.append(True)
        return _FakeResponse(b"")

    monkeypatch.setattr("urllib.request.urlopen", _should_not_call)

    result = synthesize("   ### *** 🤖🎉  ", tts_url="http://stub:9000")
    assert result == b""
    assert called == [], "network should not be called for empty-after-clean text"


def test_synthesize_sends_json_body(monkeypatch: pytest.MonkeyPatch) -> None:
    """synthesize() POSTs a JSON {"text","voice"} body to /v1/audio/synthesize (Chatterbox)."""
    import json as _json

    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        # Full-length clip so the truncation guard doesn't retry (1 call expected).
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    synthesize("Speak this.", tts_url="http://stub:9000", voice="en-US-female")
    assert len(captured) == 1
    req = captured[0]
    assert req.full_url.endswith("/v1/audio/synthesize")
    assert req.method == "POST"
    assert req.headers["Content-type"] == "application/json"
    payload = _json.loads(req.data.decode("utf-8"))
    assert payload == {"text": "Speak this.", "voice": "en-US-female"}


def test_synthesize_default_voice_is_null(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no voice set, the JSON body sends "voice": null (Chatterbox default voice)."""
    import json as _json

    captured: list[bytes] = []

    def _capture(req, timeout=None):
        captured.append(req.data)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    synthesize("Hello.", tts_url="http://stub:9000")
    assert captured
    payload = _json.loads(captured[0].decode("utf-8"))
    assert payload["voice"] is None


# ---------------------------------------------------------------------------
# Acceptance criterion 2 — clean_for_tts() and split_for_tts()
# ---------------------------------------------------------------------------


def test_clean_strips_markdown() -> None:
    assert clean_for_tts("**bold** _italic_ `code` #hash") == "bold italic code hash"


def test_clean_strips_emoji() -> None:
    text = "Hello 🤖 world 🎉"
    result = clean_for_tts(text)
    assert "🤖" not in result
    assert "🎉" not in result
    assert "Hello" in result
    assert "world" in result


def test_clean_normalizes_dashes() -> None:
    text = "one—two–three"
    result = clean_for_tts(text)
    assert "—" not in result
    assert "–" not in result
    # em/en dash should become separators or spaces
    assert "one" in result
    assert "two" in result


def test_clean_normalizes_quotes() -> None:
    text = "‘smart’ and “curly”"
    result = clean_for_tts(text)
    assert "‘" not in result
    assert "’" not in result
    assert "“" not in result
    assert "”" not in result
    assert "smart" in result
    assert "curly" in result


def test_clean_collapses_whitespace() -> None:
    text = "  hello   world  \n\n  yes  "
    result = clean_for_tts(text)
    assert result == "hello world yes"


def test_clean_strips_list_markers() -> None:
    text = "- item one\n- item two\n1. numbered"
    result = clean_for_tts(text)
    assert result.startswith("item one") or "item one" in result
    assert "- " not in result


def test_split_short_text_is_single_chunk() -> None:
    """Text under the max_chars limit → returned as-is in a list."""
    text = "Short text."
    chunks = split_for_tts(text, max_chars=600)
    assert chunks == [text]


def test_split_long_text_into_multiple_chunks() -> None:
    """Text exceeding max_chars → split into multiple chunks, each ≤ max_chars."""
    text = "word " * 200  # ~1000 chars
    chunks = split_for_tts(text, max_chars=100)
    assert len(chunks) > 1
    for chunk in chunks:
        assert len(chunk) <= 100


def test_split_preserves_full_content() -> None:
    """After splitting and re-joining, all words are present."""
    text = "The quick brown fox jumps over the lazy dog. " * 20
    chunks = split_for_tts(text, max_chars=100)
    joined = " ".join(chunks)
    # Every word in original should appear in the joined result
    for word in ["quick", "brown", "fox", "lazy", "dog"]:
        assert word in joined


def test_split_prefers_comma_break_point() -> None:
    """Splitter prefers breaking at ', ' rather than arbitrary spaces."""
    # Build a string that has a comma+space near the limit
    prefix = "a" * 90
    text = prefix + ", more text here that exceeds the limit somewhat"
    chunks = split_for_tts(text, max_chars=100)
    # The first chunk should end at the comma break (before 'more')
    assert len(chunks) >= 1
    assert len(chunks[0]) <= 100


def test_split_hard_cut_when_no_break_point() -> None:
    """If there are no spaces or commas within the window, a hard cut is applied."""
    text = "a" * 250  # no spaces at all
    chunks = split_for_tts(text, max_chars=100)
    for chunk in chunks:
        assert len(chunk) <= 100
    assert "".join(chunks) == text


# ---------------------------------------------------------------------------
# Acceptance criterion 3 — unreachable URL raises CliError(code=2)
# ---------------------------------------------------------------------------


def test_synthesize_raises_cli_error_on_unreachable(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unreachable TTS URL raises CliError with exit code 2 and a hint."""

    def _fail(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://nowhere:9999")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation, "CliError must include a non-empty remediation (hint) line"


def test_synthesize_raises_cli_error_on_http_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A 503 HTTP response raises CliError with exit code 2."""

    def _fail(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url,
            code=503,
            msg="Service Unavailable",
            hdrs=None,
            fp=None,
        )

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://stub:9000")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation


def test_synthesize_no_traceback_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors are wrapped in CliError — no raw exceptions escape."""

    def _fail(req, timeout=None):
        raise OSError("unexpected socket error")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError):
        synthesize("Hello.", tts_url="http://stub:9000")


# ---------------------------------------------------------------------------
# Env-var configuration
# ---------------------------------------------------------------------------


def test_synthesize_uses_env_tts_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """REACHY_TTS_URL env var is used when no tts_url arg is given."""
    monkeypatch.setenv("REACHY_TTS_URL", "http://envhost:9000")
    captured: list[str] = []

    def _capture(req, timeout=None):
        captured.append(req.full_url)
        return _FakeResponse(_fake_pcm())

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    synthesize("Test.")
    assert any("envhost:9000" in url for url in captured)


def test_synthesize_uses_env_tts_voice(monkeypatch: pytest.MonkeyPatch) -> None:
    """REACHY_TTS_VOICE env var is included in the POST body."""
    monkeypatch.setenv("REACHY_TTS_VOICE", "custom-voice-v1")
    captured: list[bytes] = []

    def _capture(req, timeout=None):
        captured.append(req.data)
        return _FakeResponse(_fake_pcm())

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    synthesize("Test.", tts_url="http://stub:9000")
    assert captured
    assert b"custom-voice-v1" in captured[0]


def test_voice_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Explicit voice= arg overrides REACHY_TTS_VOICE."""
    monkeypatch.setenv("REACHY_TTS_VOICE", "env-voice")
    captured: list[bytes] = []

    def _capture(req, timeout=None):
        captured.append(req.data)
        return _FakeResponse(_fake_pcm())

    monkeypatch.setattr("urllib.request.urlopen", _capture)

    synthesize("Test.", tts_url="http://stub:9000", voice="explicit-voice")
    assert b"explicit-voice" in captured[0]
    assert b"env-voice" not in captured[0]


# ---------------------------------------------------------------------------
# Live-test regressions — the Magpie server returns a WAV container and
# intermittently truncates; synthesize must unwrap to bare PCM and retry.
# ---------------------------------------------------------------------------


def _wav_bytes(pcm: bytes, *, rate: int = 22050) -> bytes:
    """Wrap raw PCM16 mono in a RIFF/WAVE container (what the Magpie server returns)."""
    import wave as _wave

    buf = io.BytesIO()
    with _wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(rate)
        w.writeframes(pcm)
    return buf.getvalue()


def test_synthesize_unwraps_wav_container(monkeypatch: pytest.MonkeyPatch) -> None:
    """A RIFF/WAVE response is unwrapped to its bare PCM data chunk (no RIFF header)."""
    pcm = _fake_pcm(4096)
    wav = _wav_bytes(pcm)
    assert wav[:4] == b"RIFF"  # the server returns a container

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(wav))

    result = synthesize("Speak this please.", tts_url="http://stub:9000")
    assert result[:4] != b"RIFF", "WAV header leaked into the PCM stream"
    assert result == pcm


def test_synthesize_retries_truncated_then_returns_full(monkeypatch: pytest.MonkeyPatch) -> None:
    """A truncated first response triggers a retry; the full clip is returned."""
    text = "Hello there friend, this is a longer sentence for synthesis."
    truncated = _fake_pcm(800)  # << 15ms/char floor -> flagged truncated
    full = _fake_pcm(80_000)  # plausible full clip
    responses = iter([truncated, full])

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(next(responses)),
    )

    result = synthesize(text, tts_url="http://stub:9000")
    assert result == full, "should retry past the truncated clip and return the full one"


def test_synthesize_keeps_longest_when_all_truncated(monkeypatch: pytest.MonkeyPatch) -> None:
    """If every attempt is truncated, the longest clip is returned (not empty)."""
    text = "Another long-enough sentence to exceed the truncation floor here."
    clips = [_fake_pcm(400), _fake_pcm(1200), _fake_pcm(600)]
    responses = iter(clips)

    monkeypatch.setattr(
        "urllib.request.urlopen",
        lambda req, timeout=None: _FakeResponse(next(responses)),
    )

    result = synthesize(text, tts_url="http://stub:9000")
    assert result == clips[1], "should keep the longest of the truncated attempts"


# ---------------------------------------------------------------------------
# Gateway OpenAI-style route (/v1/audio/speech) — REACHY_TTS_ROUTE=openai
# ---------------------------------------------------------------------------


def test_synthesize_openai_route_posts_to_speech_endpoint(monkeypatch: pytest.MonkeyPatch) -> None:
    """route="openai" posts an OpenAI-shaped {model, input, voice} body to /v1/audio/speech."""
    import json as _json

    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "test-key-123")

    synthesize("Speak this.", tts_url="http://gateway:8001", route="openai")

    assert len(captured) == 1
    req = captured[0]
    assert req.full_url.endswith("/v1/audio/speech")
    assert req.method == "POST"
    assert req.headers["Content-type"] == "application/json"
    assert req.headers["Authorization"] == "Bearer test-key-123"
    payload = _json.loads(req.data.decode("utf-8"))
    assert payload["input"] == "Speak this."
    assert payload["voice"] is None
    assert "model" in payload and payload["model"]


def test_synthesize_openai_route_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """REACHY_TTS_ROUTE=openai selects the gateway route without the kwarg."""
    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.setenv("REACHY_TTS_ROUTE", "openai")
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "test-key-123")

    synthesize("Hi there.", tts_url="http://gateway:8001")

    assert captured[0].full_url.endswith("/v1/audio/speech")


def test_route_kwarg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit route= kwarg wins over REACHY_TTS_ROUTE."""
    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.setenv("REACHY_TTS_ROUTE", "openai")

    synthesize("Hi there.", tts_url="http://stub:9000", route="chatterbox")

    assert captured[0].full_url.endswith("/v1/audio/synthesize")


def test_synthesize_chatterbox_route_is_default_and_unchanged(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """With no route selected at all (no env, no kwarg), behavior is byte-identical."""
    import json as _json

    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.delenv("REACHY_TTS_ROUTE", raising=False)

    synthesize("Hi there.", tts_url="http://stub:9000")

    assert captured[0].full_url.endswith("/v1/audio/synthesize")
    payload = _json.loads(captured[0].data.decode("utf-8"))
    assert payload == {"text": "Hi there.", "voice": None}
    assert "Authorization" not in captured[0].headers


def test_synthesize_openai_route_no_auth_header_without_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Without REACHY_OPENAI_API_KEY set, no Authorization header is sent (no crash)."""
    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)

    synthesize("Hi there.", tts_url="http://gateway:8001", route="openai")

    assert "Authorization" not in captured[0].headers


def test_synthesize_openai_route_uses_gateway_env_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """With no tts_url override, the gateway route reads REACHY_OPENAI_URL_BASE."""
    captured: list[str] = []

    def _capture(req, timeout=None):
        captured.append(req.full_url)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.setenv("REACHY_OPENAI_URL_BASE", "http://gatewayhost:8001")
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)

    synthesize("Hi there.", route="openai")

    assert any("gatewayhost:8001" in url for url in captured)


def test_synthesize_openai_route_unwraps_wav(monkeypatch: pytest.MonkeyPatch) -> None:
    """The gateway's verified WAV response (24 kHz mono PCM16) unwraps via _extract_pcm."""
    pcm = _fake_pcm(4096)
    wav = _wav_bytes(pcm, rate=24000)
    assert wav[:4] == b"RIFF"

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(wav))
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)

    result = synthesize("Speak this please.", tts_url="http://gateway:8001", route="openai")
    assert result[:4] != b"RIFF", "WAV header leaked into the PCM stream"
    assert result == pcm


def test_synthesize_openai_route_bare_pcm_passthrough(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bare-PCM gateway response (response_format=pcm, audio/pcm) passes through unchanged."""
    pcm = _fake_pcm(40_000)

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _FakeResponse(pcm))
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)

    result = synthesize("Speak this please.", tts_url="http://gateway:8001", route="openai")
    assert result == pcm


def test_synthesize_openai_route_unreachable_raises_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An unreachable gateway raises CliError(code=2), matching the Chatterbox contract."""

    def _fail(req, timeout=None):
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://gateway:8001", route="openai")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation


def test_synthesize_openai_route_http_error_raises_cli_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A 401/HTTP-error gateway response raises CliError(code=2)."""

    def _fail(req, timeout=None):
        raise urllib.error.HTTPError(
            url=req.full_url, code=401, msg="Unauthorized", hdrs=None, fp=None
        )

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://gateway:8001", route="openai")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation


def test_synthesize_openai_route_no_traceback_leaks(monkeypatch: pytest.MonkeyPatch) -> None:
    """Errors on the gateway route are wrapped in CliError — no raw exception escapes."""

    def _fail(req, timeout=None):
        raise OSError("unexpected socket error")

    monkeypatch.setattr("urllib.request.urlopen", _fail)

    with pytest.raises(CliError):
        synthesize("Hello.", tts_url="http://gateway:8001", route="openai")


def test_synthesize_invalid_route_raises_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised route name is a clean exit-1 user error, not a crash."""
    monkeypatch.delenv("REACHY_TTS_ROUTE", raising=False)

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://stub:9000", route="bogus")

    err = exc_info.value
    assert err.code == 1
    assert err.remediation


def test_synthesize_invalid_route_env_raises_user_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """An unrecognised REACHY_TTS_ROUTE env value is also a clean exit-1 user error."""
    monkeypatch.setenv("REACHY_TTS_ROUTE", "bogus-route")

    with pytest.raises(CliError) as exc_info:
        synthesize("Hello.", tts_url="http://stub:9000")

    assert exc_info.value.code == 1


def test_synthesize_openai_route_model_override(monkeypatch: pytest.MonkeyPatch) -> None:
    """An explicit model= kwarg is sent verbatim in the OpenAI-shaped payload."""
    import json as _json

    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.delenv("REACHY_OPENAI_API_KEY", raising=False)

    synthesize("Hi.", tts_url="http://gateway:8001", route="openai", model="custom/tts-model")

    payload = _json.loads(captured[0].data.decode("utf-8"))
    assert payload["model"] == "custom/tts-model"


def test_synthesize_openai_route_no_auth_when_api_key_is_empty_sentinel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """REACHY_OPENAI_API_KEY='EMPTY' means no auth header (same convention as llm.py)."""
    captured: list[urllib.request.Request] = []

    def _capture(req, timeout=None):
        captured.append(req)
        return _FakeResponse(_fake_pcm(40_000))

    monkeypatch.setattr("urllib.request.urlopen", _capture)
    monkeypatch.setenv("REACHY_OPENAI_API_KEY", "EMPTY")

    synthesize("Hi there.", tts_url="http://gateway:8001", route="openai")

    assert "Authorization" not in captured[0].headers
