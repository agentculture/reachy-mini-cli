"""Tests for reachy.speech.stt — the shared Parakeet transcription client.

`Transcriber` is the reusable transcription leg factored out of
`reachy.sleep.wakeword.HttpSttBackend`. Unlike the wake-word backend it returns
the transcript TEXT (the OpenAI/Parakeet ``{"text": "..."}`` shape, with the
legacy ``transcript`` alias) rather than a wake-word boolean. It owns the same
WAV/multipart/urllib/rolling-window/throttle machinery and the same never-raises
degradation contract.

Acceptance criteria covered (one section each):

1. POSTs a PCM16-mono WAV multipart form to ``{REACHY_STT_URL}/v1/audio/
   transcriptions`` and returns the response ``"text"`` (str) or ``None``.
2. An unreachable host, HTTP>=400, empty body, or non-JSON response returns
   ``None`` and NEVER raises.
3. Accumulates a rolling window and throttles to <=1 POST per ``min_interval``
   (both injectable); a sub-window chunk returns ``None`` until the window fills.
4. No new runtime dependency: stdlib ``urllib`` + ``numpy`` only — importing the
   module pulls in no ``requests`` / ``openai``.
"""

from __future__ import annotations

import io
import json
import subprocess
import sys
import wave
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _chunk(n: int = 512) -> np.ndarray:
    return np.full(n, 0.001, dtype=np.float32)


def _nowindow(**kw):
    """A Transcriber with windowing/throttle off — posts on every transcribe().

    Most matching/HTTP-leg tests want to exercise a single transcribe() -> POST
    without accumulating a full window first, so they disable the rolling window.
    """
    from reachy.speech.stt import Transcriber

    kw.setdefault("window_seconds", 0.0)
    kw.setdefault("min_interval", 0.0)
    return Transcriber(**kw)


def _module_pulls_in(dotted: str, forbidden: str) -> bool:
    """True if importing *dotted* pulls *forbidden* into sys.modules.

    Probe in a fresh SUBPROCESS so it has ZERO effect on this interpreter's
    sys.modules (mirrors test_sleep_wakeword.py)."""
    code = f"import sys; import {dotted}; print({forbidden!r} in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip() == "True"


class _FakeResp:
    def __init__(self, *, status=200, body=b""):
        self._status = status
        self._body = body

    status = property(lambda self: self._status)

    def getcode(self):
        return self._status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


# ---------------------------------------------------------------------------
# Criterion 1 — returns the response "text" string (or None)
# ---------------------------------------------------------------------------


class TestReturnsText:
    """transcribe() returns the JSON `text` string, honouring the `transcript` alias."""

    def test_text_field_returned_verbatim(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"text": "hello there reachy"}  # noqa: SLF001
        assert backend.transcribe(_chunk()) == "hello there reachy"

    def test_transcript_alias_returned(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"transcript": "legacy alias text"}  # noqa: SLF001
        assert backend.transcribe(_chunk()) == "legacy alias text"

    def test_text_preferred_over_transcript(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {  # noqa: SLF001
            "text": "preferred",
            "transcript": "legacy",
        }
        assert backend.transcribe(_chunk()) == "preferred"

    def test_post_returning_none_yields_none(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: None  # noqa: SLF001
        assert backend.transcribe(_chunk()) is None

    def test_payload_without_text_yields_none(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"detected": True}  # noqa: SLF001
        assert backend.transcribe(_chunk()) is None

    def test_empty_text_yields_none(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"text": ""}  # noqa: SLF001
        assert backend.transcribe(_chunk()) is None

    def test_non_str_text_yields_none(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"text": 12345}  # noqa: SLF001
        assert backend.transcribe(_chunk()) is None


# ---------------------------------------------------------------------------
# transcribe_payload — returns the RAW JSON dict (no text extraction)
# ---------------------------------------------------------------------------


class TestTranscribePayload:
    """transcribe_payload() returns the parsed JSON dict, or None on any failure."""

    def test_returns_full_payload_dict(self):
        payload = {"text": "hi", "detected": True, "phrase": "p"}
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: dict(payload)  # noqa: SLF001
        assert backend.transcribe_payload(_chunk()) == payload

    def test_payload_without_text_still_returned(self):
        """Unlike transcribe(), a textless payload is returned verbatim (not None)."""
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"detected": True}  # noqa: SLF001
        assert backend.transcribe_payload(_chunk()) == {"detected": True}

    def test_post_returning_none_yields_none(self):
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: None  # noqa: SLF001
        assert backend.transcribe_payload(_chunk()) is None

    def test_sub_window_yields_none(self):
        from reachy.speech.stt import Transcriber

        # 1.0 s window @ 1000 Hz = 1000 samples; one 400-sample chunk is too short.
        backend = Transcriber(
            stt_url="http://stt.local", sample_rate=1000, window_seconds=1.0, min_interval=0.0
        )
        backend._post = lambda audio: {"text": "x"}  # noqa: SLF001
        assert backend.transcribe_payload(_chunk(400)) is None

    def test_throttled_yields_none(self):
        clock = {"t": 100.0}
        backend = _nowindow(stt_url="http://stt.local", min_interval=5.0)
        backend._clock = lambda: clock["t"]  # noqa: SLF001
        backend._post = lambda audio: {"text": "x"}  # noqa: SLF001
        assert backend.transcribe_payload(_chunk()) == {"text": "x"}  # first post
        assert backend.transcribe_payload(_chunk()) is None  # throttled (same time)

    def test_post_raising_is_swallowed(self):
        def _boom(audio):
            raise RuntimeError("network exploded")

        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = _boom  # noqa: SLF001
        assert backend.transcribe_payload(_chunk()) is None

    def test_transcribe_delegates_to_payload(self):
        """transcribe() == _extract_text(transcribe_payload(...)) — same window state."""
        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = lambda audio: {"text": "delegated text"}  # noqa: SLF001
        assert backend.transcribe(_chunk()) == "delegated text"


# ---------------------------------------------------------------------------
# Criterion 2 — failure modes return None and NEVER raise
# ---------------------------------------------------------------------------


class TestNeverRaises:
    """Unreachable / HTTP>=400 / empty / non-JSON all degrade to None, never raise."""

    def test_unreachable_returns_none(self):
        # Port 1 on loopback is not listening — the request fails fast.
        backend = _nowindow(stt_url="http://127.0.0.1:1")
        for _ in range(5):
            assert backend.transcribe(_chunk()) is None

    def test_post_raising_is_swallowed(self):
        def _boom(audio):
            raise RuntimeError("network exploded")

        backend = _nowindow(stt_url="http://stt.invalid")
        backend._post = _boom  # noqa: SLF001
        assert backend.transcribe(_chunk()) is None

    def _patch_urlopen(self, monkeypatch, resp):
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)

    def test_http_error_returns_none(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(status=503, body=b"oops"))
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) is None

    def test_empty_body_returns_none(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b""))
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) is None

    def test_non_json_returns_none(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b"\xff\xfe not json"))
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) is None

    def test_non_dict_json_returns_none(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b"[1, 2, 3]"))
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) is None

    def test_urlopen_raising_is_swallowed(self, monkeypatch):
        import urllib.request

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) is None

    def test_success_through_real_post_leg(self, monkeypatch):
        body = json.dumps({"text": "real post leg text"}).encode()
        self._patch_urlopen(monkeypatch, _FakeResp(body=body))
        assert _nowindow(stt_url="http://stt.local").transcribe(_chunk()) == "real post leg text"


# ---------------------------------------------------------------------------
# Criterion 3 — rolling window + throttle
# ---------------------------------------------------------------------------


class TestWindowing:
    """transcribe() accumulates audio and only POSTs a full window, <=1/min_interval."""

    def _counting_backend(self, **kw):
        backend = _nowindow(stt_url="http://stt.local", **kw)
        calls = {"n": 0}

        def _post(audio):
            calls["n"] += 1
            return {"text": "transcribed"}

        backend._post = _post  # noqa: SLF001
        return backend, calls

    def test_no_post_until_window_full(self):
        from reachy.speech.stt import Transcriber

        # 1.0 s window @ 1000 Hz = 1000 samples; feed 400-sample chunks.
        backend = Transcriber(
            stt_url="http://stt.local", sample_rate=1000, window_seconds=1.0, min_interval=0.0
        )
        posted = {"n": 0}
        backend._post = lambda audio: posted.__setitem__("n", posted["n"] + 1) or {  # noqa: SLF001
            "text": "x"
        }

        assert backend.transcribe(_chunk(400)) is None  # 400 < 1000
        assert backend.transcribe(_chunk(400)) is None  # 800 < 1000
        assert posted["n"] == 0
        assert backend.transcribe(_chunk(400)) == "x"  # 1200 >= 1000 -> posts
        assert posted["n"] == 1

    def test_throttle_limits_post_rate(self):
        clock = {"t": 100.0}
        backend, calls = self._counting_backend()
        backend._clock = lambda: clock["t"]  # noqa: SLF001
        backend._min_interval = 5.0  # noqa: SLF001

        assert backend.transcribe(_chunk()) == "transcribed"  # first post
        assert backend.transcribe(_chunk()) is None  # throttled (same time)
        assert calls["n"] == 1
        clock["t"] += 6.0  # past the interval
        assert backend.transcribe(_chunk()) == "transcribed"
        assert calls["n"] == 2

    def test_reset_clears_window_and_throttle(self):
        backend, calls = self._counting_backend(min_interval=5.0)
        clock = {"t": 0.0}
        backend._clock = lambda: clock["t"]  # noqa: SLF001
        assert backend.transcribe(_chunk()) == "transcribed"
        backend.reset()
        assert backend._buffered == 0  # noqa: SLF001
        assert backend._last_post is None  # noqa: SLF001

    def test_empty_chunk_yields_none_through_real_post(self, monkeypatch):
        """An empty chunk -> empty WAV -> _post returns None (no urlopen call)."""
        import urllib.request

        def _must_not_call(*a, **k):
            raise AssertionError("urlopen must not be called for an empty window")

        monkeypatch.setattr(urllib.request, "urlopen", _must_not_call)
        backend = _nowindow(stt_url="http://stt.local")
        assert backend.transcribe(np.zeros(0, dtype=np.float32)) is None


# ---------------------------------------------------------------------------
# WAV header / encoding / multipart (PCM16 mono @ sample rate)
# ---------------------------------------------------------------------------


class TestEncodeAudio:
    def test_empty_audio_encodes_to_empty_bytes(self):
        from reachy.speech.stt import Transcriber

        assert Transcriber._encode_audio(np.zeros(0, dtype=np.float32)) == b""

    def test_none_audio_encodes_to_empty_bytes(self):
        from reachy.speech.stt import Transcriber

        assert Transcriber._encode_audio(None) == b""

    def test_normal_audio_encodes_to_pcm16(self):
        from reachy.speech.stt import Transcriber

        out = Transcriber._encode_audio(np.array([0.0, 1.0, -1.0], dtype=np.float32))
        assert isinstance(out, bytes) and len(out) == 6  # 3 samples * 2 bytes


class TestWavBytes:
    """_wav_bytes wraps the PCM16 window in a self-describing WAV container."""

    def test_empty_audio_is_empty_wav(self):
        from reachy.speech.stt import Transcriber

        assert Transcriber._wav_bytes(np.zeros(0, dtype=np.float32), 16000) == b""

    def test_wav_has_riff_header_and_rate(self):
        from reachy.speech.stt import Transcriber

        out = Transcriber._wav_bytes(np.zeros(800, dtype=np.float32), 16000)
        assert out[:4] == b"RIFF" and out[8:12] == b"WAVE"
        with wave.open(io.BytesIO(out), "rb") as wf:
            assert wf.getframerate() == 16000
            assert wf.getnchannels() == 1
            assert wf.getsampwidth() == 2
            assert wf.getnframes() == 800

    def test_sample_rate_threaded_into_header(self):
        from reachy.speech.stt import Transcriber

        out = Transcriber._wav_bytes(np.zeros(100, dtype=np.float32), 48000)
        with wave.open(io.BytesIO(out), "rb") as wf:
            assert wf.getframerate() == 48000

    def test_constructed_sample_rate_carried_into_post(self, monkeypatch):
        """The constructor sample_rate must reach the WAV header on a real POST."""
        from reachy.speech.stt import Transcriber

        captured = {}

        def _capture_urlopen(req, *a, **k):
            captured["data"] = req.data
            return _FakeResp(body=json.dumps({"text": "ok"}).encode())

        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", _capture_urlopen)
        backend = Transcriber(
            stt_url="http://stt.local", sample_rate=44100, window_seconds=0.0, min_interval=0.0
        )
        assert backend.transcribe(_chunk(64)) == "ok"
        # The multipart body embeds a WAV; find its RIFF chunk and read the rate.
        body = captured["data"]
        riff_at = body.index(b"RIFF")
        with wave.open(io.BytesIO(body[riff_at:]), "rb") as wf:
            assert wf.getframerate() == 44100


class TestMultipartBody:
    """_multipart_body emits a file=WAV + language form the STT server can parse."""

    def test_body_contains_file_and_language_fields(self):
        backend = _nowindow(stt_url="http://stt.local", language="en")
        body, content_type = backend._multipart_body(b"RIFFfake")
        assert content_type.startswith("multipart/form-data; boundary=")
        assert b'name="file"; filename="' in body
        assert b"Content-Type: audio/wav" in body
        assert b'name="language"' in body and b"\r\nen\r\n" in body
        assert b"RIFFfake" in body


# ---------------------------------------------------------------------------
# Endpoint + env-var defaults (Parakeet contract)
# ---------------------------------------------------------------------------


class TestDefaults:
    def test_default_url_is_parakeet_localhost(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.delenv("REACHY_STT_URL", raising=False)
        assert stt.Transcriber().stt_url == "http://localhost:9002"

    def test_env_var_default_url(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.setenv("REACHY_STT_URL", "http://configured-stt:8080")
        assert stt.Transcriber().stt_url == "http://configured-stt:8080"

    def test_default_path_is_openai_transcriptions(self):
        from reachy.speech import stt

        assert stt.DEFAULT_STT_PATH == "/v1/audio/transcriptions"
        backend = stt.Transcriber(stt_url="http://stt.local")
        assert backend._endpoint == "http://stt.local/v1/audio/transcriptions"

    def test_default_language_is_en(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.delenv("REACHY_STT_LANGUAGE", raising=False)
        assert stt.Transcriber().language == "en"

    def test_language_env_var(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.setenv("REACHY_STT_LANGUAGE", "fr")
        assert stt.Transcriber().language == "fr"

    def test_timeout_explicit_override_wins(self):
        from reachy.speech import stt

        assert stt._resolve_stt_timeout(2.5) == 2.5

    def test_timeout_env_var_parsed(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.setenv("REACHY_STT_TIMEOUT", "0.4")
        assert stt._resolve_stt_timeout(None) == 0.4

    def test_timeout_bad_env_falls_back_to_default(self, monkeypatch):
        from reachy.speech import stt

        monkeypatch.setenv("REACHY_STT_TIMEOUT", "not-a-number")
        assert stt._resolve_stt_timeout(None) == stt.DEFAULT_STT_TIMEOUT


# ---------------------------------------------------------------------------
# Criterion 4 — stdlib + numpy only; no requests/openai
# ---------------------------------------------------------------------------


class TestImportBoundary:
    def test_module_does_not_import_requests(self):
        assert not _module_pulls_in(
            "reachy.speech.stt", "requests"
        ), "reachy.speech.stt pulled in requests — it must be stdlib urllib only."

    def test_module_does_not_import_openai(self):
        assert not _module_pulls_in(
            "reachy.speech.stt", "openai"
        ), "reachy.speech.stt pulled in openai — it must be stdlib urllib only."

    def test_module_does_not_import_reachy_mini(self):
        assert not _module_pulls_in(
            "reachy.speech.stt", "reachy_mini"
        ), "reachy.speech.stt pulled in reachy_mini — it must be SDK-free."
