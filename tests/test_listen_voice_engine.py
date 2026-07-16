"""Tests for ``listen run --live --voice-engine {tts,harmonic}`` wiring.

``reachy/speech/voice.py`` (merged separately) resolves a
:class:`~reachy.speech.voice.VoiceEngine` — a ``(name, synthesize, samplerate)``
record — from an explicit name, the ``REACHY_VOICE_ENGINE`` env var, or the
``"tts"`` default. This module threads that choice through ``listen run --live``'s
folded cognition composition, mirroring the ``--export`` / ``--transcribe`` "only
meaningful with --live" pattern already established in ``_commands/listen.py``.

Coverage:

1. ``--voice-engine`` without ``--live`` is a clean exit-1 user error (mirrors
   ``--export``/``--transcribe``), validated before ``get_transport``.
2. ``--live --voice-engine harmonic`` composes the folded cognition engine with
   :func:`reachy.speech.harmonic.synthesize` and an empty ``tts_kwargs``.
3. Bare ``--live`` (no flag) stays byte-identical: the engine still gets an
   explicit ``synthesize``/``tts_kwargs``, but bound to
   :func:`reachy.speech.tts.synthesize` — the exact function
   :class:`~reachy.speech.cognition.CognitionEngine` already defaults to.
4. ``REACHY_VOICE_ENGINE=harmonic`` selects harmonic without the flag; an
   explicit ``--voice-engine tts`` overrides the env back.
5. The self-mute ``play_audio`` wrapper (``_make_self_mute_play_audio``) stamps
   ``mute["until"]`` using the ACTIVE engine's sample rate — regression-tested
   directly (unit level) and end-to-end (wiring level: the CLI threads
   ``HARMONIC_SAMPLE_RATE`` into the wrapper only for ``"harmonic"``; the default
   ``"tts"`` path leaves the wrapper's ``samplerate`` unset, byte-identical to
   before this feature).
6. The ``--live`` startup banner (stderr) names the active voice engine; a bare
   (non-``--live``) ``listen run`` banner is unchanged (no voice note at all).
7. Under ``--export``, stdout stays pure JSONL — the banner (now carrying the
   engine name) lives on stderr only, exactly as before this feature.

No robot, no daemon, no network, no real LLM/TTS calls: the cognition engine is
replaced with a capturing fake (mirrors ``tests/test_listen_export.py``), and the
self-mute wrapper is exercised directly with a stubbed ``play_audio``.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json
import sys

import numpy as np
import pytest

import reachy.cli._commands.listen as listen_mod
import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
import reachy.speech.harmonic as harmonic_mod
import reachy.speech.tts as tts_mod
from reachy.cli import main
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.speech.voice import VOICE_ENGINE_ENV, VoiceEngine

# ---------------------------------------------------------------------------
# Isolation: pin every *_active flag into a throwaway state dir, no env leakage
# (mirrors tests/test_listen_live.py's autouse fixture).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    for sig in (ps, ss, cs):
        sig.clear()
    yield
    for sig in (ps, ss, cs):
        sig.clear()


# ---------------------------------------------------------------------------
# A minimal fake sdk media session + transport (mirrors tests/test_listen_live.py)
# ---------------------------------------------------------------------------


class _Session:
    """The ONE open client for the loop: audio + DoA + pose + move + frame."""

    _SAMPLE = np.full(512, 0.001, dtype=np.float32)  # below min_rms -> no snap

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}  # front, no speech

    def get_audio_sample(self):
        return self._SAMPLE

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)  # flat: no pat

    def get_frame(self):
        return None  # no camera frame -> vision is a quiet no-op

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _LiveSdkTransport:
    """A fake sdk transport with one open media session."""

    name = "sdk-live"

    def __init__(self):
        self._session = _Session()

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)

    def get_frame(self):
        return None

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        yield self._session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_capture(monkeypatch, argv, *, transport=None):
    """Run ``reachy <argv>``; return (rc, stdout, stderr)."""
    if transport is not None:
        monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _a: transport)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def _live_argv(*extra: str) -> list[str]:
    return [
        "listen",
        "run",
        "--live",
        "--transport",
        "sdk",
        "--deadband",
        "0",
        "--idle-energy",
        "0",
        "--max-ticks",
        "2",
        *extra,
    ]


class _FakeEngine:
    """Captures the kwargs ``_build_think_hook`` constructs the engine with.

    Mirrors ``tests/test_listen_export.py``'s ``_FakeEngine`` — a full
    replacement of ``CognitionEngine`` so no real network / LLM / TTS call can
    ever happen, regardless of what ``ThinkHook``'s background worker does with
    ``.run()``.
    """

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeEngine.last_kwargs = kwargs
        self.buffer = kwargs.get("buffer")

    def run(self, *_a, **_k):  # never meaningfully driven in these tests
        return 0


def _patch_engine(monkeypatch):
    _FakeEngine.last_kwargs = {}
    monkeypatch.setattr("reachy.speech.cognition.CognitionEngine", _FakeEngine)


# ---------------------------------------------------------------------------
# 1. --voice-engine without --live is a clean exit-1 user error
# ---------------------------------------------------------------------------


def test_voice_engine_without_live_is_clean_exit_1_before_transport(monkeypatch) -> None:
    """A bare ``--voice-engine`` (no ``--live``) is rejected BEFORE ``get_transport``.

    Mirrors ``--export``'s / ``--transcribe``'s guard exactly: the combo error
    fires regardless of whether the sdk extra is installed.
    """
    called = {"transport": False}

    def _tripwire(_args):
        called["transport"] = True
        raise AssertionError("get_transport must not be reached")

    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", _tripwire)

    rc, _out, err = _run_capture(
        monkeypatch,
        ["listen", "run", "--voice-engine", "harmonic", "--transport", "sdk", "--max-ticks", "1"],
    )

    assert rc == EXIT_USER_ERROR
    assert "--voice-engine needs --live" in err
    assert "hint:" in err
    assert called["transport"] is False, "validation must run before get_transport"


def test_voice_engine_resolver_requires_live_unit() -> None:
    """The resolver helper raises the documented exit-1 CliError without ``--live``."""
    args = argparse.Namespace(voice_engine="harmonic", live=False)
    with pytest.raises(CliError) as ei:
        listen_mod._resolve_voice_engine(args)
    assert ei.value.code == EXIT_USER_ERROR
    assert "--voice-engine" in ei.value.message and "--live" in ei.value.message

    # No explicit flag + no --live -> resolves the default engine, no error.
    args_bare = argparse.Namespace(voice_engine=None, live=False)
    engine = listen_mod._resolve_voice_engine(args_bare)
    assert isinstance(engine, VoiceEngine)
    assert engine.name == "tts"

    # Explicit choice + --live -> resolves the requested engine.
    args_live = argparse.Namespace(voice_engine="harmonic", live=True)
    engine2 = listen_mod._resolve_voice_engine(args_live)
    assert engine2.name == "harmonic"


def test_voice_engine_flag_rejects_unknown_choice() -> None:
    """argparse's own ``choices=`` rejects a value outside {tts, harmonic}.

    This is a parse-time error (before dispatch), so ``main()`` raises
    ``SystemExit`` directly rather than returning an ``int`` rc — unlike the
    ``CliError``s raised from inside a handler (see ``_CliArgumentParser.error``
    in ``reachy/cli/__init__.py``).
    """
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        with pytest.raises(SystemExit) as ei:
            main(["listen", "run", "--voice-engine", "bogus", "--max-ticks", "1"])
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    assert ei.value.code == EXIT_USER_ERROR
    assert "--voice-engine" in err.getvalue()


# ---------------------------------------------------------------------------
# 2 + 3. --live composes the correct synthesize + tts_kwargs
# ---------------------------------------------------------------------------


def test_live_bare_composes_tts_synthesize(monkeypatch) -> None:
    """Bare ``--live`` (no ``--voice-engine``) composes the ``tts`` engine explicitly.

    The composed ``synthesize`` is the SAME function object
    :class:`~reachy.speech.cognition.CognitionEngine` already defaults to, so this
    is behaviourally byte-identical to before the feature — only now observable.
    """
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert _FakeEngine.last_kwargs.get("synthesize") is tts_mod.synthesize
    assert _FakeEngine.last_kwargs.get("tts_kwargs") == {}


def test_live_voice_engine_harmonic_composes_harmonic_synthesize(monkeypatch) -> None:
    """``--live --voice-engine harmonic`` swaps in the harmonic synthesize callable."""
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--voice-engine", "harmonic"), transport=transport
    )

    assert rc == 0
    assert _FakeEngine.last_kwargs.get("synthesize") is harmonic_mod.synthesize
    assert _FakeEngine.last_kwargs.get("tts_kwargs") == {}


# ---------------------------------------------------------------------------
# 4. env fallback + flag override
# ---------------------------------------------------------------------------


def test_live_voice_engine_env_fallback_harmonic(monkeypatch) -> None:
    """``REACHY_VOICE_ENGINE=harmonic`` selects harmonic with no ``--voice-engine`` flag."""
    _patch_engine(monkeypatch)
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert _FakeEngine.last_kwargs.get("synthesize") is harmonic_mod.synthesize


def test_live_voice_engine_flag_overrides_env(monkeypatch) -> None:
    """An explicit ``--voice-engine tts`` overrides ``REACHY_VOICE_ENGINE=harmonic``."""
    _patch_engine(monkeypatch)
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--voice-engine", "tts"), transport=transport
    )

    assert rc == 0
    assert _FakeEngine.last_kwargs.get("synthesize") is tts_mod.synthesize


# ---------------------------------------------------------------------------
# 5. Self-mute wrapper stamps mute["until"] from the ACTIVE engine's samplerate
# ---------------------------------------------------------------------------


def test_make_self_mute_play_audio_stamps_from_harmonic_samplerate(monkeypatch) -> None:
    """Direct unit test: the harmonic samplerate drives the duration math exactly.

    A negligible ``mute_after`` (still ``> 0``, so the stamping branch fires —
    ``mute_after=0`` disables stamping entirely, see the ``after > 0`` guard)
    isolates the clip-duration contribution so the exact expected value can be
    pinned (rather than the looser ``>=`` bound alone, which a wrong-but-still-
    positive rate could also satisfy).
    """
    monkeypatch.setattr("reachy.speech.playback.play_audio", lambda *a, **k: None)
    mute: dict[str, float] = {"until": 0.0}
    clock_time = {"t": 100.0}
    margin = 1e-6

    play = listen_mod._make_self_mute_play_audio(
        mute,
        lambda: clock_time["t"],
        mute_after=margin,
        samplerate=harmonic_mod.HARMONIC_SAMPLE_RATE,
    )

    pcm = b"\x00\x00" * harmonic_mod.HARMONIC_SAMPLE_RATE  # 1.0s of PCM16 @ 16kHz
    play(pcm)

    assert mute["until"] == pytest.approx(100.0 + 1.0 + margin)
    # The documented acceptance bound: advanced by >= N / 2 / 16000.
    assert mute["until"] - 100.0 >= len(pcm) / 2 / harmonic_mod.HARMONIC_SAMPLE_RATE


def test_make_self_mute_play_audio_injects_samplerate_into_playback_kwargs(monkeypatch) -> None:
    """The active engine's samplerate is ALSO forwarded to the real ``play_audio`` call."""
    captured: dict[str, object] = {}

    def _fake_play(pcm, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("reachy.speech.playback.play_audio", _fake_play)
    mute: dict[str, float] = {"until": 0.0}

    play = listen_mod._make_self_mute_play_audio(
        mute, lambda: 0.0, playback_transport="http", samplerate=harmonic_mod.HARMONIC_SAMPLE_RATE
    )
    play(b"\x00\x00")

    assert captured.get("samplerate") == harmonic_mod.HARMONIC_SAMPLE_RATE
    assert captured.get("transport") == "http"


def test_make_self_mute_play_audio_tts_default_is_byte_identical(monkeypatch) -> None:
    """``samplerate=None`` (the ``tts`` default) keeps today's behaviour exactly.

    No ``samplerate`` kwarg is injected into the playback call, and the
    duration math falls back to the hardcoded TTS default — a regression guard
    for the "Engine tts: byte-identical to today" requirement.
    """
    captured: dict[str, object] = {}

    def _fake_play(pcm, **kwargs):
        captured.update(kwargs)

    monkeypatch.setattr("reachy.speech.playback.play_audio", _fake_play)
    mute: dict[str, float] = {"until": 0.0}
    margin = 1e-6

    play = listen_mod._make_self_mute_play_audio(mute, lambda: 100.0, mute_after=margin)
    pcm = b"\x00\x00" * tts_mod.DEFAULT_SAMPLE_RATE  # 1.0s of PCM16 @ the TTS default rate
    play(pcm)

    assert "samplerate" not in captured
    assert mute["until"] == pytest.approx(100.0 + 1.0 + margin)


def test_live_harmonic_threads_samplerate_into_self_mute_wrapper(monkeypatch) -> None:
    """End-to-end: ``--live --voice-engine harmonic`` passes ``HARMONIC_SAMPLE_RATE``
    into ``_make_self_mute_play_audio`` at the CLI composition point."""
    captured: dict[str, object] = {}
    real = listen_mod._make_self_mute_play_audio

    def _spy(*a, **k):
        captured["kwargs"] = k
        return real(*a, **k)

    monkeypatch.setattr(listen_mod, "_make_self_mute_play_audio", _spy)
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--voice-engine", "harmonic"), transport=transport
    )

    assert rc == 0
    assert captured["kwargs"].get("samplerate") == harmonic_mod.HARMONIC_SAMPLE_RATE


def test_live_bare_leaves_self_mute_wrapper_samplerate_unset(monkeypatch) -> None:
    """End-to-end regression: bare ``--live`` (tts) passes ``samplerate=None`` — unchanged."""
    captured: dict[str, object] = {}
    real = listen_mod._make_self_mute_play_audio

    def _spy(*a, **k):
        captured["kwargs"] = k
        return real(*a, **k)

    monkeypatch.setattr(listen_mod, "_make_self_mute_play_audio", _spy)
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert captured["kwargs"].get("samplerate") is None


# ---------------------------------------------------------------------------
# 6. Banner (stderr) names the active voice engine
# ---------------------------------------------------------------------------


def test_live_banner_names_active_voice_engine_harmonic(monkeypatch) -> None:
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, err = _run_capture(
        monkeypatch, _live_argv("--voice-engine", "harmonic"), transport=transport
    )

    assert rc == 0
    assert "voice: harmonic" in err


def test_live_banner_names_active_voice_engine_tts_default(monkeypatch) -> None:
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert "voice: tts" in err


def test_bare_listen_run_banner_has_no_voice_note(monkeypatch) -> None:
    """Without ``--live`` the banner is unchanged: no ``(voice: ...)`` note at all."""
    transport = _LiveSdkTransport()

    rc, _out, err = _run_capture(
        monkeypatch,
        ["listen", "run", "--transport", "sdk", "--deadband", "0", "--max-ticks", "1"],
        transport=transport,
    )

    assert rc == 0
    assert "voice:" not in err


# ---------------------------------------------------------------------------
# 7. Export purity: stdout stays pure JSONL with the harmonic engine active
# ---------------------------------------------------------------------------


def test_export_stdout_stays_pure_jsonl_with_harmonic_voice_engine(monkeypatch) -> None:
    """``--export -`` with ``--voice-engine harmonic``: stdout carries ONLY JSONL.

    The banner (which now names the active engine) must still land on stderr
    only — the export purity guarantee is unaffected by the voice engine choice.
    """
    _patch_engine(monkeypatch)
    transport = _LiveSdkTransport()

    rc, out, err = _run_capture(
        monkeypatch,
        _live_argv("--voice-engine", "harmonic", "--export", "-"),
        transport=transport,
    )

    assert rc == 0
    for line in out.splitlines():
        if not line.strip():
            continue
        json.loads(line)  # every non-blank stdout line must be valid JSON
    assert "voice: harmonic" in err
    assert "voice: harmonic" not in out
    assert "voice:" not in out
