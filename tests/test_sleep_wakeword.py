"""Tests for reachy.sleep.wakeword — the pluggable wake-word backend layer.

Two backends only:
  * external HTTP STT (the DEFAULT) — stdlib urllib only; a configured-but-
    unreachable/absent server degrades to "no wake-word" (returns False) and
    NEVER raises.
  * openwakeword — optional on-box `[cpu]` path; lazy-imported only when its
    backend is selected.

Boundary contract: importing the wake path (reachy.sleep.wake /
reachy.sleep.wakeword) pulls in NO openwakeword by default — the engine import
is guarded inside a function. The import-boundary probes run in a SUBPROCESS so
they never pollute this interpreter's sys.modules (the pattern established in
test_sleep_boundary.py).
"""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import numpy as np

_REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_sense(speech: bool = False):
    from reachy.behavior.sense import Sense

    return Sense(doa_angle=None, speech_detected=speech)


def _chunk(n: int = 512) -> np.ndarray:
    return np.full(n, 0.001, dtype=np.float32)


def _module_pulls_in(dotted: str, forbidden: str) -> bool:
    """True if importing *dotted* pulls *forbidden* into sys.modules.

    Probe in a fresh SUBPROCESS so it has ZERO effect on this interpreter's
    sys.modules (mirrors test_sleep_boundary.py)."""
    code = f"import sys; import {dotted}; print({forbidden!r} in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        check=True,
    )
    return proc.stdout.strip() == "True"


# ---------------------------------------------------------------------------
# 1. resolve_backend — the public factory
# ---------------------------------------------------------------------------


class TestResolveBackend:
    """resolve_backend(...) returns a backend object with update(sense, audio)->bool."""

    def test_disabled_returns_backend_that_never_fires(self):
        from reachy.sleep.wakeword import resolve_backend

        backend = resolve_backend(enabled=False)
        assert hasattr(backend, "update")
        assert backend.update(_make_sense(), _chunk()) is False

    def test_default_backend_is_http_stt(self):
        """With enabled=True and no explicit kind, the DEFAULT backend is HTTP STT."""
        from reachy.sleep.wakeword import HttpSttBackend, resolve_backend

        backend = resolve_backend(enabled=True)
        assert isinstance(backend, HttpSttBackend)

    def test_explicit_http_kind(self):
        from reachy.sleep.wakeword import HttpSttBackend, resolve_backend

        backend = resolve_backend(enabled=True, kind="http")
        assert isinstance(backend, HttpSttBackend)

    def test_openwakeword_kind_degrades_when_absent(self):
        """Selecting openwakeword without the [cpu] extra installed must not raise.

        It returns a backend whose update() degrades to False (no crash)."""
        from reachy.sleep.wakeword import resolve_backend

        backend = resolve_backend(enabled=True, kind="openwakeword")
        # The package is not installed in CI/base — update must degrade, never raise.
        assert backend.update(_make_sense(), _chunk()) is False

    def test_backend_update_returns_bool(self):
        from reachy.sleep.wakeword import resolve_backend

        backend = resolve_backend(enabled=True, kind="http", stt_url="http://127.0.0.1:1")
        out = backend.update(_make_sense(), _chunk())
        assert out in (True, False)


# ---------------------------------------------------------------------------
# 2. HTTP STT backend — unreachable degrades to False, never raises
# ---------------------------------------------------------------------------


class TestHttpSttBackend:
    """The external HTTP STT backend uses stdlib urllib and degrades cleanly."""

    def test_unreachable_returns_false(self):
        from reachy.sleep.wakeword import HttpSttBackend

        # Port 1 on loopback is not listening — the request fails fast.
        backend = HttpSttBackend(stt_url="http://127.0.0.1:1", phrase="hey reachy")
        # Many ticks; never raises, always False while unreachable.
        for _ in range(5):
            assert backend.update(_make_sense(), _chunk()) is False

    def test_fires_when_transcript_matches_phrase(self):
        """A successful STT response containing the phrase fires True.

        We inject the HTTP POST so no real server is needed."""
        from reachy.sleep.wakeword import HttpSttBackend

        backend = HttpSttBackend(stt_url="http://stt.invalid", phrase="hey reachy")
        backend._post = lambda audio: {"transcript": "well, hey Reachy, wake up"}  # noqa: SLF001
        assert backend.update(_make_sense(), _chunk()) is True

    def test_no_match_returns_false(self):
        from reachy.sleep.wakeword import HttpSttBackend

        backend = HttpSttBackend(stt_url="http://stt.invalid", phrase="hey reachy")
        backend._post = lambda audio: {"transcript": "the weather is nice today"}  # noqa: SLF001
        assert backend.update(_make_sense(), _chunk()) is False

    def test_detected_field_fires(self):
        """A response with an explicit boolean `detected` field is honoured."""
        from reachy.sleep.wakeword import HttpSttBackend

        backend = HttpSttBackend(stt_url="http://stt.invalid", phrase="hey reachy")
        backend._post = lambda audio: {"detected": True}  # noqa: SLF001
        assert backend.update(_make_sense(), _chunk()) is True

    def test_post_returning_none_is_false(self):
        from reachy.sleep.wakeword import HttpSttBackend

        backend = HttpSttBackend(stt_url="http://stt.invalid", phrase="hey reachy")
        backend._post = lambda audio: None  # noqa: SLF001
        assert backend.update(_make_sense(), _chunk()) is False

    def test_post_raising_is_swallowed(self):
        """Even if the post leg raises, update() must degrade to False."""
        from reachy.sleep.wakeword import HttpSttBackend

        def _boom(audio):
            raise RuntimeError("network exploded")

        backend = HttpSttBackend(stt_url="http://stt.invalid", phrase="hey reachy")
        backend._post = _boom  # noqa: SLF001
        assert backend.update(_make_sense(), _chunk()) is False

    def test_env_var_default_url(self, monkeypatch):
        from reachy.sleep import wakeword

        monkeypatch.setenv("REACHY_STT_URL", "http://configured-stt:8080")
        backend = wakeword.HttpSttBackend()
        assert backend.stt_url == "http://configured-stt:8080"

    def test_env_var_default_phrase(self, monkeypatch):
        from reachy.sleep import wakeword

        monkeypatch.setenv("REACHY_STT_PHRASE", "yo robot")
        backend = wakeword.HttpSttBackend()
        assert backend.phrase == "yo robot"

    def test_default_phrase_is_hey_reachy(self, monkeypatch):
        from reachy.sleep import wakeword

        monkeypatch.delenv("REACHY_STT_PHRASE", raising=False)
        backend = wakeword.HttpSttBackend()
        assert backend.phrase == "hey reachy"


# ---------------------------------------------------------------------------
# 3. Import boundary — default/HTTP path imports NO openwakeword
# ---------------------------------------------------------------------------


class TestImportBoundary:
    """Importing the wake path must not pull in openwakeword."""

    def test_wakeword_module_does_not_import_openwakeword(self):
        assert not _module_pulls_in("reachy.sleep.wakeword", "openwakeword"), (
            "Importing reachy.sleep.wakeword pulled in openwakeword — "
            "the engine import must be guarded inside a function."
        )

    def test_wake_module_does_not_import_openwakeword(self):
        assert not _module_pulls_in("reachy.sleep.wake", "openwakeword"), (
            "Importing reachy.sleep.wake pulled in openwakeword — "
            "the engine import must stay lazy via resolve_backend."
        )

    def test_openwakeword_import_is_inside_function(self):
        import reachy.sleep.wakeword as ww_mod

        src = inspect.getsource(ww_mod)
        for line in src.splitlines():
            stripped = line.lstrip()
            if "openwakeword" in stripped and stripped.startswith(("import ", "from ")):
                assert line != stripped, (
                    "Found a top-level import of openwakeword in wakeword.py — "
                    "it must live inside a function to stay lazy."
                )

    def test_no_asr_libs_pulled_in(self):
        for lib in ("nemo", "speechbrain", "whisper", "faster_whisper"):
            assert not _module_pulls_in(
                "reachy.sleep.wakeword", lib
            ), f"reachy.sleep.wakeword pulled in ASR library {lib!r}"

    def test_wakeword_module_does_not_import_reachy_mini(self):
        assert not _module_pulls_in(
            "reachy.sleep.wakeword", "reachy_mini"
        ), "reachy.sleep.wakeword pulled in reachy_mini — it must be SDK-free."


# ---------------------------------------------------------------------------
# Timeout resolution (env precedence + bad value)
# ---------------------------------------------------------------------------


class TestResolveSttTimeout:
    """_resolve_stt_timeout: explicit arg > REACHY_STT_TIMEOUT > default; bad env → default."""

    def test_explicit_override_wins(self):
        from reachy.sleep import wakeword

        assert wakeword._resolve_stt_timeout(2.5) == 2.5

    def test_env_var_parsed(self, monkeypatch):
        from reachy.sleep import wakeword

        monkeypatch.setenv("REACHY_STT_TIMEOUT", "0.4")
        assert wakeword._resolve_stt_timeout(None) == 0.4

    def test_bad_env_falls_back_to_default(self, monkeypatch):
        from reachy.sleep import wakeword

        monkeypatch.setenv("REACHY_STT_TIMEOUT", "not-a-number")
        assert wakeword._resolve_stt_timeout(None) == wakeword.DEFAULT_STT_TIMEOUT


# ---------------------------------------------------------------------------
# HTTP _post leg + _encode_audio (stubbed urlopen, no real network)
# ---------------------------------------------------------------------------


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


def _http_backend():
    from reachy.sleep.wakeword import HttpSttBackend

    return HttpSttBackend(stt_url="http://stt.local", phrase="hey reachy")


class TestHttpPostLeg:
    """Drive _post through a stubbed urllib.request.urlopen."""

    def _patch_urlopen(self, monkeypatch, resp):
        import urllib.request

        monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)

    def test_post_success_returns_dict(self, monkeypatch):
        import json as _json

        self._patch_urlopen(monkeypatch, _FakeResp(body=_json.dumps({"detected": True}).encode()))
        backend = _http_backend()
        assert backend.update(_make_sense(), _chunk()) is True

    def test_post_http_error_returns_false(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(status=503, body=b"oops"))
        backend = _http_backend()
        assert backend.update(_make_sense(), _chunk()) is False

    def test_post_empty_body_returns_false(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b""))
        assert _http_backend().update(_make_sense(), _chunk()) is False

    def test_post_non_json_returns_false(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b"\xff\xfe not json"))
        assert _http_backend().update(_make_sense(), _chunk()) is False

    def test_post_non_dict_json_returns_false(self, monkeypatch):
        self._patch_urlopen(monkeypatch, _FakeResp(body=b"[1, 2, 3]"))
        assert _http_backend().update(_make_sense(), _chunk()) is False

    def test_post_transcript_substring_matches(self, monkeypatch):
        import json as _json

        body = _json.dumps({"transcript": "well HEY REACHY there"}).encode()
        self._patch_urlopen(monkeypatch, _FakeResp(body=body))
        assert _http_backend().update(_make_sense(), _chunk()) is True

    def test_post_unreachable_raises_swallowed(self, monkeypatch):
        import urllib.request

        def _boom(*a, **k):
            raise OSError("connection refused")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert _http_backend().update(_make_sense(), _chunk()) is False


class TestEncodeAudio:
    def test_empty_audio_encodes_to_empty_bytes(self):
        from reachy.sleep.wakeword import HttpSttBackend

        assert HttpSttBackend._encode_audio(np.zeros(0, dtype=np.float32)) == b""

    def test_none_audio_encodes_to_empty_bytes(self):
        from reachy.sleep.wakeword import HttpSttBackend

        assert HttpSttBackend._encode_audio(None) == b""

    def test_normal_audio_encodes_to_pcm16(self):
        from reachy.sleep.wakeword import HttpSttBackend

        out = HttpSttBackend._encode_audio(np.array([0.0, 1.0, -1.0], dtype=np.float32))
        assert isinstance(out, bytes) and len(out) == 6  # 3 samples * 2 bytes


# ---------------------------------------------------------------------------
# openwakeword backend — fake engine injected (no openwakeword package needed)
# ---------------------------------------------------------------------------


class _FakeEngine:
    def __init__(self, *, result=True, raises=False, has_reset=True):
        self._result = result
        self._raises = raises
        self.reset_called = False
        if not has_reset:
            del self.reset

    def detect(self, audio):
        if self._raises:
            raise RuntimeError("engine boom")
        return self._result

    def reset(self):
        self.reset_called = True


def _oww_backend_with(engine):
    from reachy.sleep.wakeword import OpenWakeWordBackend

    b = OpenWakeWordBackend(phrase="hey reachy")
    b._engine = engine
    b._engine_loaded = True  # skip the lazy import
    return b


class TestOpenWakeWordBackend:
    def test_detect_true_fires(self):
        assert _oww_backend_with(_FakeEngine(result=True)).update(_make_sense(), _chunk()) is True

    def test_detect_false_no_fire(self):
        assert _oww_backend_with(_FakeEngine(result=False)).update(_make_sense(), _chunk()) is False

    def test_engine_crash_degrades_to_false(self):
        b = _oww_backend_with(_FakeEngine(raises=True))
        assert b.update(_make_sense(), _chunk()) is False

    def test_absent_engine_returns_false(self):
        b = _oww_backend_with(None)
        assert b.update(_make_sense(), _chunk()) is False

    def test_reset_delegates_to_engine(self):
        eng = _FakeEngine()
        _oww_backend_with(eng).reset()
        assert eng.reset_called is True

    def test_reset_safe_when_engine_absent(self):
        _oww_backend_with(None).reset()  # must not raise


# ---------------------------------------------------------------------------
# _matches: a `phrase` echo only fires when it EQUALS the configured phrase
# (regression: any truthy phrase used to wake — Qodo bug #3 on PR #37)
# ---------------------------------------------------------------------------


class TestPhraseMatching:
    def test_phrase_echo_of_other_phrase_does_not_fire(self):
        backend = _http_backend()  # configured phrase = "hey reachy"
        assert backend._matches({"phrase": "good morning"}) is False

    def test_phrase_equal_to_configured_fires(self):
        backend = _http_backend()
        assert backend._matches({"phrase": "Hey Reachy"}) is True  # case-insensitive

    def test_empty_phrase_does_not_fire(self):
        assert _http_backend()._matches({"phrase": ""}) is False

    def test_detected_true_still_fires(self):
        assert _http_backend()._matches({"detected": True}) is True

    def test_transcript_substring_still_fires(self):
        assert _http_backend()._matches({"transcript": "oh HEY REACHY hi"}) is True
