"""Tests for reachy.speech.playback — audio playback via sdk and http transports.

All tests use fake/stub objects; no real robot, daemon, or SDK is needed.

Coverage:
  AC1 — sdk path feeds PCM samples to a fake media session via push_audio_sample
        (start_playing() called first; samples received match converted input).
  AC2 — http path uploads a WAV and calls /media/play_sound against a stub daemon
        (both HTTP calls happen with the right shapes).
  AC3 — sdk path without [sdk] extra raises CliError exit-2 pointing at [sdk].
"""

from __future__ import annotations

from unittest.mock import MagicMock

import numpy as np
import pytest

from reachy.cli._errors import CliError
from reachy.speech.playback import play_audio

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_pcm_bytes(n_samples: int = 1024, samplerate: int = 22050) -> bytes:
    """Return raw int16 PCM bytes (silence — zeros)."""
    return (np.zeros(n_samples, dtype=np.int16)).tobytes()


def _make_pcm_float32(n_samples: int = 1024) -> np.ndarray:
    """Return a float32 ndarray of sine-wave samples in [-1, 1]."""
    t = np.linspace(0, 1, n_samples, endpoint=False)
    return (np.sin(2 * np.pi * 440 * t)).astype(np.float32)


# ---------------------------------------------------------------------------
# AC1 — sdk path feeds PCM via push_audio_sample; start_playing called first
# ---------------------------------------------------------------------------


class _FakeOutputMedia:
    """Minimal stand-in for ``ReachyMini.media`` on the output (playback) side."""

    def __init__(self, output_samplerate: int = 22050, output_channels: int = 1) -> None:
        self._output_samplerate = output_samplerate
        self._output_channels = output_channels
        self.started = False
        self.push_calls: list[np.ndarray] = []
        self._start_order: list[str] = []

    def get_output_audio_samplerate(self) -> int:
        return self._output_samplerate

    def get_output_channels(self) -> int:
        return self._output_channels

    def start_playing(self) -> None:
        self.started = True
        self._start_order.append("start_playing")

    def push_audio_sample(self, data: np.ndarray) -> None:
        # Record call order so we can assert start_playing came first.
        if not self._start_order:
            self._start_order.append("push_audio_sample_before_start")
        else:
            self._start_order.append("push_audio_sample")
        self.push_calls.append(data)


def test_sdk_calls_start_playing_before_push(monkeypatch: pytest.MonkeyPatch) -> None:
    """start_playing() must be called before the first push_audio_sample()."""
    media = _FakeOutputMedia()
    pcm = _make_pcm_bytes(n_samples=512)

    play_audio(pcm, transport="sdk", media_session=media)

    assert media.started, "start_playing() was never called"
    # Verify order: start_playing must precede any push
    assert (
        "push_audio_sample_before_start" not in media._start_order
    ), "push_audio_sample was called before start_playing"


def test_sdk_push_receives_float32_samples(monkeypatch: pytest.MonkeyPatch) -> None:
    """push_audio_sample() must receive float32 ndarrays whose values match int16/32768."""
    n = 256
    # Use a non-trivial pattern so we can verify the normalisation.
    int16_data = np.arange(n, dtype=np.int16) - 128
    pcm = int16_data.tobytes()
    expected_f32 = int16_data.astype(np.float32) / 32768.0

    media = _FakeOutputMedia()
    play_audio(pcm, transport="sdk", media_session=media)

    assert len(media.push_calls) >= 1, "No push_audio_sample calls recorded"
    # Concatenate all pushed chunks and compare with expected.
    received = np.concatenate(media.push_calls)
    np.testing.assert_allclose(received, expected_f32, rtol=1e-5)


def test_sdk_push_dtype_is_float32(monkeypatch: pytest.MonkeyPatch) -> None:
    """Every chunk fed to push_audio_sample must have dtype float32."""
    media = _FakeOutputMedia()
    pcm = _make_pcm_bytes(n_samples=512)

    play_audio(pcm, transport="sdk", media_session=media)

    for chunk in media.push_calls:
        assert chunk.dtype == np.float32, f"Expected float32, got {chunk.dtype}"


def test_sdk_push_not_empty(monkeypatch: pytest.MonkeyPatch) -> None:
    """At least one push_audio_sample call must happen for non-empty PCM."""
    media = _FakeOutputMedia()
    pcm = _make_pcm_bytes(n_samples=1024)

    play_audio(pcm, transport="sdk", media_session=media)

    assert len(media.push_calls) >= 1


# ---------------------------------------------------------------------------
# AC1 (injection path) — open an SDK media session when none is provided
# ---------------------------------------------------------------------------


def test_sdk_opens_media_session_when_none_provided(monkeypatch: pytest.MonkeyPatch) -> None:
    """When media_session=None, play_audio must open one via _open_sdk_media()."""
    media = _FakeOutputMedia()

    # Patch the lazy SDK import inside playback so we control what's returned.
    from reachy.speech import playback as pb_module

    monkeypatch.setattr(pb_module, "_open_sdk_media", lambda: media)

    pcm = _make_pcm_bytes(n_samples=256)
    play_audio(pcm, transport="sdk")  # no media_session kwarg

    assert media.started
    assert len(media.push_calls) >= 1


# ---------------------------------------------------------------------------
# AC2 — http path uploads WAV + calls /media/play_sound
# ---------------------------------------------------------------------------


class _FakeHttpCall:
    """Record info about each urlopen call so tests can assert the shapes."""

    def __init__(self, json_response: dict) -> None:
        self._json = json_response
        self.calls: list[dict] = []

    def __call__(self, req, timeout=None):  # noqa: ANN001 - test shim
        import json as _json

        info: dict = {
            "method": req.get_method(),
            "url": req.full_url,
            "headers": dict(req.headers),
        }
        if req.data:
            try:
                info["json_body"] = _json.loads(req.data)
            except Exception:  # noqa: BLE001
                info["raw_data"] = req.data
        self.calls.append(info)
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = _json.dumps(self._json).encode()
        return resp


def test_http_upload_and_play(monkeypatch: pytest.MonkeyPatch) -> None:
    """http path: POST to /media/sounds/upload then /media/play_sound — both called."""
    fake_handler = _FakeHttpCall({"path": "sounds/hello.wav"})
    monkeypatch.setattr("urllib.request.urlopen", fake_handler)

    pcm = _make_pcm_bytes(n_samples=2048)
    play_audio(pcm, transport="http", base_url="http://localhost:8000")

    assert len(fake_handler.calls) == 2, (
        f"Expected 2 HTTP calls, got {len(fake_handler.calls)}: "
        f"{[c['url'] for c in fake_handler.calls]}"
    )
    upload_call, play_call = fake_handler.calls

    # First call: multipart upload (daemon mounts media under /api)
    assert upload_call["url"].endswith(
        "/api/media/sounds/upload"
    ), f"Upload URL mismatch: {upload_call['url']}"
    assert upload_call["method"] == "POST"

    # Second call: play_sound with the returned path
    assert play_call["url"].endswith(
        "/api/media/play_sound"
    ), f"play_sound URL mismatch: {play_call['url']}"
    assert play_call["method"] == "POST"
    assert "file" in play_call.get("json_body", {}), f"play_sound body missing 'file': {play_call}"
    assert play_call["json_body"]["file"] == "sounds/hello.wav"


def test_http_upload_sends_wav_bytes(monkeypatch: pytest.MonkeyPatch) -> None:
    """The upload body must be a valid WAV (RIFF header check)."""
    uploaded_bodies: list[bytes] = []

    import json as _json

    call_count = 0

    def _fake_urlopen(req, timeout=None):
        nonlocal call_count
        call_count += 1
        # Capture the raw request data for the upload call (call 1).
        if call_count == 1:
            uploaded_bodies.append(req.data)
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = _json.dumps({"path": "sounds/test.wav"}).encode()
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    pcm = _make_pcm_bytes(n_samples=1024)
    play_audio(pcm, transport="http")

    assert uploaded_bodies, "No data captured from upload call"
    body = uploaded_bodies[0]
    # The multipart body should contain the WAV magic bytes somewhere.
    assert b"RIFF" in body, "RIFF header not found in uploaded multipart body"
    assert b"WAVE" in body, "WAVE marker not found in uploaded multipart body"


def test_http_play_sound_uses_returned_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """play_sound must use whatever path the upload response returns."""
    import json as _json

    custom_path = "custom/dir/synth_12345.wav"
    call_index = 0
    play_bodies: list[dict] = []

    def _fake_urlopen(req, timeout=None):
        nonlocal call_index
        call_index += 1
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        if call_index == 1:
            resp.read.return_value = _json.dumps({"path": custom_path}).encode()
        else:
            body = _json.loads(req.data)
            play_bodies.append(body)
            resp.read.return_value = _json.dumps({"status": "playing"}).encode()
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)
    pcm = _make_pcm_bytes()
    play_audio(pcm, transport="http")

    assert play_bodies, "play_sound was never called"
    assert play_bodies[0]["file"] == custom_path


# ---------------------------------------------------------------------------
# AC3 — sdk path without [sdk] extra raises CliError exit-2
# ---------------------------------------------------------------------------


def test_sdk_without_extra_raises_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """Requesting sdk transport when reachy_mini is not installed → CliError exit-2."""
    from reachy.speech import playback as pb_module

    def _raise_import():
        raise ImportError("No module named 'reachy_mini'")

    monkeypatch.setattr(pb_module, "_open_sdk_media", _raise_import)

    pcm = _make_pcm_bytes()
    with pytest.raises(CliError) as exc_info:
        play_audio(pcm, transport="sdk")

    err = exc_info.value
    assert err.code == 2, f"Expected exit code 2, got {err.code}"
    assert (
        "sdk" in err.remediation.lower() or "reachy-mini-cli" in err.remediation.lower()
    ), f"Remediation should mention [sdk] install: {err.remediation}"


def test_sdk_import_error_message_mentions_sdk_extra(monkeypatch: pytest.MonkeyPatch) -> None:
    """The CliError remediation must point at 'pip install reachy-mini-cli[sdk]'."""
    from reachy.speech import playback as pb_module

    def _raise_import():
        raise ImportError("No module named 'reachy_mini'")

    monkeypatch.setattr(pb_module, "_open_sdk_media", _raise_import)

    pcm = _make_pcm_bytes()
    with pytest.raises(CliError) as exc_info:
        play_audio(pcm, transport="sdk")

    assert "[sdk]" in exc_info.value.remediation


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------


def test_empty_pcm_no_push_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Empty PCM bytes must not trigger any push_audio_sample calls."""
    media = _FakeOutputMedia()
    play_audio(b"", transport="sdk", media_session=media)
    assert len(media.push_calls) == 0


def test_transport_env_var_selects_http(monkeypatch: pytest.MonkeyPatch) -> None:
    """REACHY_TRANSPORT=http must select the http path."""
    import json as _json

    monkeypatch.setenv("REACHY_TRANSPORT", "http")

    call_urls: list[str] = []

    def _fake_urlopen(req, timeout=None):
        call_urls.append(req.full_url)
        resp = MagicMock()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        resp.read.return_value = _json.dumps({"path": "sounds/t.wav"}).encode()
        return resp

    monkeypatch.setattr("urllib.request.urlopen", _fake_urlopen)

    pcm = _make_pcm_bytes()
    # No transport kwarg — should read from env.
    play_audio(pcm)

    assert any("/media/" in u for u in call_urls), f"No /media/ call made; URLs were: {call_urls}"


def test_transport_env_var_selects_sdk(monkeypatch: pytest.MonkeyPatch) -> None:
    """REACHY_TRANSPORT=sdk must select the sdk path."""
    monkeypatch.setenv("REACHY_TRANSPORT", "sdk")
    media = _FakeOutputMedia()
    from reachy.speech import playback as pb_module

    monkeypatch.setattr(pb_module, "_open_sdk_media", lambda: media)

    pcm = _make_pcm_bytes(256)
    play_audio(pcm)  # transport inferred from env

    assert media.started
