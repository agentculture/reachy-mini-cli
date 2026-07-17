"""Tests for ``listen run --live --cognition {marker,agent}`` wiring (t7).

``reachy/speech/agent_turn.py`` (merged separately) provides
:class:`~reachy.speech.agent_turn.AgentTurnEngine` — the tool-use counterpart of the
marker :class:`~reachy.speech.cognition.CognitionEngine`. It consumes the *same*
:class:`~reachy.speech.events.EventBuffer`, exposes the same ``.buffer`` /
``run(stop=..., before_turn=...)`` surface, and feeds the same export sinks, so it
drops in behind ``listen``'s folded :class:`~reachy.motion.listen_think.ThinkHook`
seam unchanged.

This module threads a ``--cognition`` choice through ``listen run --live``'s folded
cognition composition, mirroring the ``--voice-engine`` / ``_resolve_voice_engine``
"only meaningful with --live" pattern already established in ``_commands/listen.py``.

Coverage (mirrors the t7 acceptance criteria):

1. ``--cognition`` without ``--live`` is a clean exit-1 user error (mirrors
   ``--voice-engine`` / ``--transcribe`` / ``--export``), validated *before*
   ``get_transport``. The resolver honours ``REACHY_COGNITION`` and defaults to
   ``marker``; an unknown value is a clean exit-1.
2. ``--live --cognition agent`` builds an :class:`AgentTurnEngine` behind the same
   ThinkHook seam (not a :class:`CognitionEngine`), with the :class:`ToolRegistry`
   wired to the REAL seams — ``express`` -> an
   :class:`~reachy.motion.expression.ExpressionProducer` bound to the loop's ONE
   :class:`~reachy.motion.queue.MotionQueue`, ``speak_engine`` / ``harmonic_engine``
   -> ``resolve_voice_engine("tts")`` / ``("harmonic")`` (BOTH available regardless
   of ``--voice-engine``), and ``play`` -> the SAME self-mute wrapper ``--transcribe``
   uses (so the robot never transcribes its own tool-spoken voice). No new OS process
   and exactly one media session.
3. Utterances reach agent cognition only on ENGAGE verdicts: the engagement gate is
   wired UNCHANGED. A fake classifier proves ambient chatter drives ZERO agent LLM
   turns while an ENGAGE-gated (named) utterance reaches the agent engine's buffer.
4. Bare ``listen run`` and non-agent ``--live`` behave identically to before: NO
   ``AgentTurnEngine`` is built; the default remains the marker ``CognitionEngine``.

No robot, no daemon, no network, no real LLM/STT/TTS: the LLM turn functions are
patched to safe fakes so a background cognition worker can never hit the network.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import sys

import numpy as np
import pytest

import reachy.cli._commands.listen as listen_mod
import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
from reachy.cli import main
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_transcribe import TranscribeHook
from reachy.speech.agent_turn import AgentTurnEngine
from reachy.speech.llm import TurnResult
from reachy.speech.voice import VoiceEngine

# Records every agent LLM turn (stream_turn) call's messages, per test.
_TURN_CALLS: list = []


# ---------------------------------------------------------------------------
# Isolation: pin flags into a throwaway state dir, no env leakage, and — crucially —
# patch the LLM turn functions so no background cognition worker can ever network.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("REACHY_COGNITION", raising=False)
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    monkeypatch.delenv("REACHY_ENGAGE_HEURISTIC", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    _TURN_CALLS.clear()

    def _fake_turn(messages, **kwargs):
        _TURN_CALLS.append(list(messages))
        return TurnResult(content="", tool_calls=[])

    # The agent engine's default turn_fn (resolved at construction).
    monkeypatch.setattr("reachy.speech.llm.stream_turn", _fake_turn)
    # The marker engine's default streamer (resolved at construction) — a no-op
    # iterator so a marker worker never networks either.
    monkeypatch.setattr("reachy.speech.llm.stream_sentences", lambda *a, **k: iter(()))

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

    def __init__(self):
        self.media_opens_seen = 0

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
    """A fake sdk transport with one open media session (counts opens)."""

    name = "sdk-live"

    def __init__(self):
        self.media_opens = 0
        self._session = _Session()

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)

    def get_frame(self):
        return None

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        self.media_opens += 1
        yield self._session


class _SpeechSession(_Session):
    """A session that always reports speech, so ``--transcribe`` accumulates a chunk."""

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": True}


class _SpeechTransport(_LiveSdkTransport):
    def __init__(self):
        super().__init__()
        self._session = _SpeechSession()


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


class _FakeRegistry:
    """Captures the kwargs ``_build_agent_think_hook`` constructs the registry with."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeRegistry.last_kwargs = kwargs

    def tools(self) -> list[dict]:
        return []

    def names(self) -> list[str]:
        return ["speak", "harmonics", "apply_pose"]

    def dispatch(self, name, arguments_json, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": "{}"}


def _patch_registry(monkeypatch):
    _FakeRegistry.last_kwargs = {}
    monkeypatch.setattr("reachy.speech.tools.ToolRegistry", _FakeRegistry)


def _spy_motion_queue(monkeypatch) -> list:
    """Capture every MotionQueue the loop constructs (there should be exactly one)."""
    created: list = []
    real_mq = listen_mod.MotionQueue

    def _make(*a, **k):
        q = real_mq(*a, **k)
        created.append(q)
        return q

    monkeypatch.setattr(listen_mod, "MotionQueue", _make)
    return created


def _force_flush_transcribe(monkeypatch):
    """Make the TranscribeHook flush a whole utterance on the first speech tick."""
    real_tr = TranscribeHook.__init__

    def _tr(self, provider, **kw):
        kw.setdefault("max_utterance_s", 0.0)
        kw.setdefault("min_utterance_s", 0.0)
        return real_tr(self, provider, **kw)

    monkeypatch.setattr(TranscribeHook, "__init__", _tr)


def _record_agent_feeds(monkeypatch) -> tuple[list, dict]:
    """Wrap ``AgentTurnEngine`` so we capture it + every ``feed_transcript`` on its buffer.

    Recording at feed time (on the main loop thread) makes the assertion independent
    of the background worker consuming/clearing the buffer.
    """
    fed: list = []
    captured: dict = {}
    real_init = AgentTurnEngine.__init__

    def _spy(self, **kw):
        real_init(self, **kw)
        captured["engine"] = self
        buf = self.buffer
        real_feed = buf.feed_transcript

        def _rec(text, *, direction=None):
            fed.append(text)
            return real_feed(text, direction=direction)

        buf.feed_transcript = _rec  # instance-level shadow

    monkeypatch.setattr(AgentTurnEngine, "__init__", _spy)
    return fed, captured


# ---------------------------------------------------------------------------
# 1. Flag surface: --cognition requires --live; env + default + unknown value
# ---------------------------------------------------------------------------


def test_cognition_agent_without_live_is_clean_exit_1_before_transport(monkeypatch) -> None:
    """A bare ``--cognition`` (no ``--live``) is rejected BEFORE ``get_transport``."""
    called = {"transport": False}

    def _tripwire(_args):
        called["transport"] = True
        raise AssertionError("get_transport must not be reached")

    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", _tripwire)

    rc, _out, err = _run_capture(
        monkeypatch,
        ["listen", "run", "--cognition", "agent", "--transport", "sdk", "--max-ticks", "1"],
    )

    assert rc == EXIT_USER_ERROR
    assert "--cognition needs --live" in err
    assert "hint:" in err
    assert called["transport"] is False, "validation must run before get_transport"


def test_cognition_resolver_requires_live_unit(monkeypatch) -> None:
    """The resolver helper raises the documented exit-1 CliError without ``--live``."""
    monkeypatch.delenv("REACHY_COGNITION", raising=False)
    args = argparse.Namespace(cognition="agent", live=False)
    with pytest.raises(CliError) as ei:
        listen_mod._resolve_cognition(args)
    assert ei.value.code == EXIT_USER_ERROR
    assert "--cognition" in ei.value.message and "--live" in ei.value.message

    # No explicit flag + no --live -> resolves the default engine, no error.
    args_bare = argparse.Namespace(cognition=None, live=False)
    assert listen_mod._resolve_cognition(args_bare) == "marker"

    # Explicit choice + --live -> resolves the requested engine.
    args_live = argparse.Namespace(cognition="agent", live=True)
    assert listen_mod._resolve_cognition(args_live) == "agent"

    # Explicit "marker" + --live stays marker.
    args_m = argparse.Namespace(cognition="marker", live=True)
    assert listen_mod._resolve_cognition(args_m) == "marker"


def test_cognition_env_fallback_and_override(monkeypatch) -> None:
    """``REACHY_COGNITION=agent`` selects agent without the flag; the flag overrides env."""
    monkeypatch.setenv("REACHY_COGNITION", "agent")
    args_env = argparse.Namespace(cognition=None, live=True)
    assert listen_mod._resolve_cognition(args_env) == "agent"

    # An explicit --cognition marker overrides the env back to marker.
    args_override = argparse.Namespace(cognition="marker", live=True)
    assert listen_mod._resolve_cognition(args_override) == "marker"


def test_cognition_env_unknown_is_clean_exit_1(monkeypatch) -> None:
    """An unknown ``REACHY_COGNITION`` value is a clean exit-1 (like resolve_voice_engine)."""
    monkeypatch.setenv("REACHY_COGNITION", "bogus")
    args = argparse.Namespace(cognition=None, live=True)
    with pytest.raises(CliError) as ei:
        listen_mod._resolve_cognition(args)
    assert ei.value.code == EXIT_USER_ERROR


def test_cognition_flag_rejects_unknown_choice() -> None:
    """argparse's own ``choices=`` rejects a value outside {marker, agent}."""
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        with pytest.raises(SystemExit) as ei:
            main(["listen", "run", "--cognition", "bogus", "--max-ticks", "1"])
    finally:
        sys.stdout, sys.stderr = old_out, old_err

    assert ei.value.code == EXIT_USER_ERROR
    assert "--cognition" in err.getvalue()


def test_cognition_flag_defaults_none(monkeypatch) -> None:
    """The ``--cognition`` flag defaults to ``None`` (→ marker) when not passed."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    listen_mod.register(sub)
    ns = parser.parse_args(["listen", "run"])
    assert getattr(ns, "cognition", None) is None
    ns2 = parser.parse_args(["listen", "run", "--cognition", "agent"])
    assert ns2.cognition == "agent"


# ---------------------------------------------------------------------------
# 2. --live --cognition agent builds the agent engine behind the ThinkHook seam
# ---------------------------------------------------------------------------


def _fake_engine_counters(monkeypatch) -> dict:
    built = {"agent": 0, "cognition": 0}

    class _FakeAgent:
        def __init__(self, **kw):
            built["agent"] += 1
            self.buffer = kw.get("buffer")

        def run(self, *a, **k):
            return 0

    class _FakeCog:
        def __init__(self, **kw):
            built["cognition"] += 1
            self.buffer = kw.get("buffer")

        def run(self, *a, **k):
            return 0

    monkeypatch.setattr("reachy.speech.agent_turn.AgentTurnEngine", _FakeAgent)
    monkeypatch.setattr("reachy.speech.cognition.CognitionEngine", _FakeCog)
    return built


def test_live_agent_builds_agent_engine_not_cognition(monkeypatch) -> None:
    """``--live --cognition agent`` builds an AgentTurnEngine, NOT a CognitionEngine."""
    built = _fake_engine_counters(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )

    assert rc == 0
    assert built["agent"] == 1, "agent mode must build exactly one AgentTurnEngine"
    assert built["cognition"] == 0, "agent mode must NOT build a marker CognitionEngine"


def test_live_default_builds_marker_cognition_engine(monkeypatch) -> None:
    """Default ``--live`` (no ``--cognition``) builds the marker CognitionEngine (regression)."""
    built = _fake_engine_counters(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert built["cognition"] == 1, "the default must still build the marker CognitionEngine"
    assert built["agent"] == 0, "the default must NOT build an AgentTurnEngine"


def test_bare_and_marker_live_build_no_agent_engine(monkeypatch) -> None:
    """Criterion 4: a bare ``listen run`` AND marker ``--live`` build NO AgentTurnEngine."""
    built = _fake_engine_counters(monkeypatch)

    # 1) bare listen run (no --live).
    transport = _LiveSdkTransport()
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)
    rc, _o, _e = _run_capture(
        monkeypatch,
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "3"],
    )
    assert rc == 0
    assert built["agent"] == 0, "a bare listen run must build NO AgentTurnEngine"

    # 2) marker --live (no --cognition).
    transport2 = _LiveSdkTransport()
    rc2, _o2, _e2 = _run_capture(monkeypatch, _live_argv(), transport=transport2)
    assert rc2 == 0
    assert built["agent"] == 0, "marker --live must build NO AgentTurnEngine"


def test_agent_thinkhook_seam_and_one_media_session(monkeypatch) -> None:
    """Agent mode rides the SAME ThinkHook seam and opens EXACTLY ONE media session.

    The agent engine is wrapped in a ThinkHook (the established fold-in seam), so no
    new OS process and no second media_session is introduced.
    """
    seen = {"think": 0}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        seen["think"] += 1
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )

    assert rc == 0
    assert seen["think"] == 1, "agent mode must wrap the engine in exactly one ThinkHook"
    assert transport.media_opens == 1, "agent mode must open exactly one media session"


# ---------------------------------------------------------------------------
# 2b. The ToolRegistry is wired with the REAL seams
# ---------------------------------------------------------------------------


def test_agent_registry_wired_with_real_seams(monkeypatch) -> None:
    """The agent registry gets express (loop queue), tts+harmonic voices, and a play seam."""
    _patch_registry(monkeypatch)
    created = _spy_motion_queue(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    kw = _FakeRegistry.last_kwargs
    # express -> ExpressionProducer.express bound to the loop's ONE MotionQueue.
    from reachy.motion.expression import ExpressionProducer

    express = kw.get("express")
    assert callable(express), "registry must receive an express seam"
    assert isinstance(express.__self__, ExpressionProducer), "express must be a producer's method"
    assert created, "the loop must construct a MotionQueue"
    assert express.__self__.queue is created[0], "express must be bound to the loop's MotionQueue"

    # speak_engine / harmonic_engine -> the tts / harmonic voice engines.
    speak_engine = kw.get("speak_engine")
    harmonic_engine = kw.get("harmonic_engine")
    assert isinstance(speak_engine, VoiceEngine) and speak_engine.name == "tts"
    assert isinstance(harmonic_engine, VoiceEngine) and harmonic_engine.name == "harmonic"

    # play -> a (self-mute-wrapping) playback seam.
    assert callable(kw.get("play")), "registry must receive a play seam"


def test_agent_registry_has_both_voices_regardless_of_voice_engine(monkeypatch) -> None:
    """``--voice-engine harmonic`` does NOT change the agent registry: BOTH voices present.

    In agent mode ``--voice-engine`` controls only the (unused-here) marker engine; the
    tool registry always exposes both ``speak`` (tts) and ``harmonics`` regardless.
    """
    _patch_registry(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--voice-engine", "harmonic"),
        transport=transport,
    )
    assert rc == 0

    kw = _FakeRegistry.last_kwargs
    assert kw.get("speak_engine").name == "tts", "the speak tool must stay the tts voice"
    assert kw.get("harmonic_engine").name == "harmonic", "the harmonics tool must stay harmonic"


def test_agent_self_mute_wraps_tool_play(monkeypatch) -> None:
    """The registry's ``play`` seam IS the self-mute wrapper the TranscribeHook reads.

    Playing a tool clip through the registry's ``play`` must move the mute deadline
    the TranscribeHook consults — so the robot never transcribes its own tool-spoken
    voice.
    """
    _patch_registry(monkeypatch)

    captured: dict = {}
    real_tr = TranscribeHook.__init__

    def _tr(self, provider, **kw):
        captured["mute_until"] = kw.get("mute_until")
        return real_tr(self, provider, **kw)

    monkeypatch.setattr(TranscribeHook, "__init__", _tr)
    monkeypatch.setattr("reachy.speech.playback.play_audio", lambda *a, **k: None)
    monkeypatch.setattr("time.monotonic", lambda: 100.0)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent", "--transcribe"), transport=transport
    )
    assert rc == 0

    play = _FakeRegistry.last_kwargs.get("play")
    mute_until = captured.get("mute_until")
    assert callable(play), "the registry must receive a wrapped play seam"
    assert callable(mute_until), "the TranscribeHook must receive a mute_until callable"

    # Before any clip, not muted.
    assert mute_until() <= 100.0
    # A tool speak call plays through the same wrapper (the registry passes samplerate=).
    play(b"\x00\x00", samplerate=24000)
    assert mute_until() > 100.0, "the registry's play must stamp the mute window the hook reads"


# ---------------------------------------------------------------------------
# 2c. feed_doa_cues wiring preserved for the agent engine (words-only under --transcribe)
# ---------------------------------------------------------------------------


def test_agent_thinkhook_feed_doa_cues_false_under_transcribe(monkeypatch) -> None:
    """Under ``--transcribe`` the agent ThinkHook is built ``feed_doa_cues=False``."""
    captured: dict = {}
    real_init = ThinkHook.__init__

    def _spy(self, provider, **kw):
        captured["feed_doa_cues"] = kw.get("feed_doa_cues")
        return real_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _spy)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent", "--transcribe"), transport=transport
    )
    assert rc == 0
    assert captured.get("feed_doa_cues") is False, "words-only mode must not feed raw DoA cues"


def test_agent_thinkhook_feed_doa_cues_true_without_transcribe(monkeypatch) -> None:
    """Without ``--transcribe`` the agent ThinkHook keeps ``feed_doa_cues=True``."""
    captured: dict = {}
    real_init = ThinkHook.__init__

    def _spy(self, provider, **kw):
        captured["feed_doa_cues"] = kw.get("feed_doa_cues")
        return real_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _spy)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0
    assert captured.get("feed_doa_cues") is True


# ---------------------------------------------------------------------------
# 3. Engagement gate unchanged: ambient -> zero agent turns; engaged -> reaches buffer
# ---------------------------------------------------------------------------


def test_agent_ambient_chatter_drives_zero_agent_turns(monkeypatch) -> None:
    """A fake classifier that DROPS ambient chatter: zero feeds, zero agent LLM turns."""

    class _NoClassifier:
        def judge(self, text, context) -> bool:
            return False

    monkeypatch.setattr("reachy.speech.engagement.EngagementClassifier", _NoClassifier)
    monkeypatch.setattr(
        "reachy.speech.stt.Transcriber.transcribe_once",
        lambda self, audio: "the weather looks nice today",
    )
    _force_flush_transcribe(monkeypatch)
    fed, _captured = _record_agent_feeds(monkeypatch)

    transport = _SpeechTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--transcribe", "--max-ticks", "6"),
        transport=transport,
    )
    assert rc == 0

    assert fed == [], "ambient chatter must NOT be fed to agent cognition"
    assert _TURN_CALLS == [], "ambient chatter must drive ZERO agent LLM turns"


def test_agent_engaged_utterance_reaches_agent_buffer(monkeypatch) -> None:
    """A named (ENGAGE-gated) utterance reaches the agent engine's shared buffer."""

    class _NoClassifier:
        # A named utterance short-circuits on the name fast-path (this is never
        # consulted), but patch it anyway so nothing can ever network.
        def judge(self, text, context) -> bool:
            return False

    monkeypatch.setattr("reachy.speech.engagement.EngagementClassifier", _NoClassifier)
    monkeypatch.setattr(
        "reachy.speech.stt.Transcriber.transcribe_once",
        lambda self, audio: "reachy tell me a joke",
    )
    _force_flush_transcribe(monkeypatch)
    fed, captured = _record_agent_feeds(monkeypatch)

    transport = _SpeechTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--transcribe", "--max-ticks", "4"),
        transport=transport,
    )
    assert rc == 0

    assert any("reachy tell me a joke" in t for t in fed), fed
    assert captured.get("engine") is not None, "the agent engine must have been built"
    # The words were fed into the SAME buffer the agent engine consumes.
    assert captured["engine"].buffer is not None


# ---------------------------------------------------------------------------
# 4. Banner names the agent cognition mode (stderr); marker default is unchanged
# ---------------------------------------------------------------------------


def test_live_banner_names_agent_cognition(monkeypatch) -> None:
    transport = _LiveSdkTransport()
    rc, _out, err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0
    assert "cognition: agent" in err


def test_live_banner_marker_default_has_no_agent_note(monkeypatch) -> None:
    transport = _LiveSdkTransport()
    rc, _out, err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0
    assert "cognition: agent" not in err
