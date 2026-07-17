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
from dataclasses import replace

import numpy as np
import pytest

import reachy.cli._commands.listen as listen_mod
import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
from reachy.cli import main
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.forge import ForgeActivator
from reachy.motion.listen_face import FaceHook
from reachy.motion.listen_pat import PatHook
from reachy.motion.listen_scene import SceneHook
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_transcribe import TranscribeHook, TranscribeTuning
from reachy.motion.listen_vision import VisionHook
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
        # The endpointing knobs now travel as one TranscribeTuning; override just
        # the two fields this test cares about, keeping every other default.
        base = kw.pop("tuning", None) or TranscribeTuning()
        kw["tuning"] = replace(base, max_utterance_s=0.0, min_utterance_s=0.0)
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


# ---------------------------------------------------------------------------
# 5. PatHook shares the live cognition EventBuffer (t4)
#
# t3 already gave PatHook an optional duck-typed ``buffer`` seam
# (reachy/motion/listen_pat.py) that feeds ``buffer.feed_pat(kind, level)`` on
# every detection. This suite proves the *composition* layer
# (_run_sdk_loop / _build_live_hooks / _build_pat_hook in
# reachy/cli/_commands/listen.py) threads the SAME shared EventBuffer the folded
# ThinkHook/agent engine (and, under --transcribe, the TranscribeHook) consumes
# into the PatHook too — so a pat cue reaches cognition directly, bypassing the
# --transcribe engagement gate entirely (that gate, reachy/speech/engagement.py,
# only judges transcribed WORDS and is built solely inside
# _compose_transcribe_hook, never on the pat path).
# ---------------------------------------------------------------------------


class _PatPressSession(_Session):
    """A live session whose head_pose reports a sustained deep downward press.

    The live loop reads ``head_pose`` through the session-bound transport proxy
    (``_SessionBoundTransport``), which prefers the open session's ``head_pose``
    over the outer transport's — AND under ``--live``, ``SleepHook``'s own
    pat-wake probe (``reachy/motion/listen_sleep.py``) polls ``head_pose`` every
    tick too, alongside ``PatHook``'s own read. A *constant* deviation (never
    released) still produces exactly ONE press edge no matter how many readers
    share this session or how their reads interleave — unlike an
    alternating/scripted press (see ``tests/test_listen_pat.py``'s
    ``_ConstantPressTransport``, which relies on being the ONLY reader), which
    depends on call parity and silently breaks once a second reader is added.
    The test pairs this with ``--min-presses 1`` so that single edge suffices.
    """

    def head_pose(self) -> tuple[float, float]:
        return (-20.0, 0.0)


class _PatPressTransport(_LiveSdkTransport):
    """A live sdk transport whose open session fires a real pat detection."""

    def __init__(self):
        super().__init__()
        self._session = _PatPressSession()


def _spy_pat_hook_buffer(monkeypatch) -> dict:
    """Capture the ``buffer`` kwarg every constructed :class:`PatHook` receives."""
    captured: dict = {}
    real_init = PatHook.__init__

    def _init(self, queue, **kw):
        captured["buffer"] = kw.get("buffer")
        return real_init(self, queue, **kw)

    monkeypatch.setattr(PatHook, "__init__", _init)
    return captured


def test_live_agent_pathook_buffer_identical_to_agent_engine_and_transcribe_hook(
    monkeypatch,
) -> None:
    """``--live --cognition agent --transcribe``: PatHook/ThinkHook/TranscribeHook share ONE buffer.

    The crux of t4: the EventBuffer object handed to the PatHook is IDENTICAL
    (is-identity) to the one the agent engine consumes (via the folded ThinkHook)
    and the one the TranscribeHook feeds.
    """
    pat_captured = _spy_pat_hook_buffer(monkeypatch)
    think_captured: dict = {}
    transcribe_captured: dict = {}
    real_think_init = ThinkHook.__init__
    real_tr_init = TranscribeHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    def _tr_init(self, provider, **kw):
        transcribe_captured["buffer"] = kw.get("buffer")
        return real_tr_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)
    monkeypatch.setattr(TranscribeHook, "__init__", _tr_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent", "--transcribe"), transport=transport
    )
    assert rc == 0

    shared = pat_captured.get("buffer")
    assert shared is not None, "PatHook must receive a shared buffer under --live"
    assert (
        think_captured.get("buffer") is shared
    ), "the agent engine (behind ThinkHook) must consume the SAME buffer PatHook feeds"
    assert (
        transcribe_captured.get("buffer") is shared
    ), "the TranscribeHook must feed the SAME buffer too"


def test_live_marker_pathook_buffer_identical_to_cognition_engine(monkeypatch) -> None:
    """Regression: the default (marker) ``--live`` engine shares the buffer too."""
    pat_captured = _spy_pat_hook_buffer(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0

    shared = pat_captured.get("buffer")
    assert shared is not None, "PatHook must receive a shared buffer under --live (marker too)"
    assert think_captured.get("buffer") is shared


def test_live_agent_pat_cue_feeds_shared_buffer_without_engagement_gate(monkeypatch) -> None:
    """A real pat detection lands in the SAME buffer the agent engine consumes.

    No ``--transcribe`` here at all: proves pat cues reach cognition directly,
    with the ``--transcribe`` engagement gate (the LLM addressed-vs-ambient
    classifier) never even touched — ``EngagementClassifier`` is only ever built
    from ``_compose_transcribe_hook``, which only runs under ``--transcribe``.
    """
    classifier_builds = {"n": 0}

    class _CountingClassifier:
        def __init__(self, *a, **k):
            classifier_builds["n"] += 1

        def judge(self, text, context) -> bool:
            return True

    monkeypatch.setattr("reachy.speech.engagement.EngagementClassifier", _CountingClassifier)
    # The cold-start warmup is a live-deployment concern (EMA sag learning over
    # real seconds); this bounded fast-spin run exercises the cue wiring.
    monkeypatch.setattr("reachy.cli._commands.listen.WARMUP_SECONDS", 0.0)

    real_init = AgentTurnEngine.__init__
    captured: dict = {}
    pat_calls: list = []

    def _spy(self, **kw):
        real_init(self, **kw)
        captured["engine"] = self
        buf = self.buffer
        real_feed_pat = buf.feed_pat

        def _rec(kind, level):
            pat_calls.append((kind, level))
            return real_feed_pat(kind, level)

        buf.feed_pat = _rec  # instance-level shadow, mirrors _record_agent_feeds

    monkeypatch.setattr(AgentTurnEngine, "__init__", _spy)

    transport = _PatPressTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--min-presses", "1", "--max-ticks", "5"),
        transport=transport,
    )
    assert rc == 0

    assert captured.get("engine") is not None, "the agent engine must have been built"
    assert pat_calls, "a detected pat must feed the shared buffer the agent engine consumes"
    assert all(kind in {"scratch", "side_pat"} for kind, _level in pat_calls), pat_calls
    assert (
        classifier_builds["n"] == 0
    ), "the pat path must never build the --transcribe engagement classifier"


def test_bare_listen_run_pathook_has_no_buffer(monkeypatch) -> None:
    """A non-live ``listen run`` still builds PatHook with ``buffer=None`` (unchanged).

    No cognition stack exists outside ``--live``, so there is nothing to share.
    """
    captured = _spy_pat_hook_buffer(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch,
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "3"],
        transport=transport,
    )
    assert rc == 0
    assert "buffer" in captured, "PatHook must have been constructed"
    assert captured["buffer"] is None


# ---------------------------------------------------------------------------
# 6. VisionHook shares the live cognition EventBuffer too (t7 / issue #32)
#
# PatHook already proved (suite 5, above) that the composition layer threads
# ONE shared EventBuffer into PatHook + the folded ThinkHook/agent engine (and,
# under --transcribe, TranscribeHook). This suite proves VisionHook is wired
# into the SAME buffer under --live — an object-identity check, mirroring suite
# 5's pattern exactly, just for the fourth folded sense hook.
# ---------------------------------------------------------------------------


def _spy_vision_hook_buffer(monkeypatch) -> dict:
    """Capture the ``buffer`` kwarg every constructed :class:`VisionHook` receives."""
    captured: dict = {}
    real_init = VisionHook.__init__

    def _init(self, **kw):
        captured["buffer"] = kw.get("buffer")
        return real_init(self, **kw)

    monkeypatch.setattr(VisionHook, "__init__", _init)
    return captured


def test_live_agent_visionhook_buffer_identical_to_pathook_and_agent_engine(
    monkeypatch,
) -> None:
    """``--live --cognition agent``: VisionHook shares the SAME buffer as PatHook/ThinkHook.

    The crux (mirrors t4's PatHook proof): the EventBuffer object handed to
    VisionHook is IDENTICAL (is-identity) to the one the agent engine consumes
    (via the folded ThinkHook) and the one PatHook feeds.
    """
    vision_captured = _spy_vision_hook_buffer(monkeypatch)
    pat_captured = _spy_pat_hook_buffer(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    shared = pat_captured.get("buffer")
    assert shared is not None, "PatHook must receive a shared buffer under --live"
    assert think_captured.get("buffer") is shared
    assert (
        vision_captured.get("buffer") is shared
    ), "VisionHook must receive the SAME shared buffer PatHook/ThinkHook consume"


def test_live_marker_visionhook_buffer_identical_to_cognition_engine(monkeypatch) -> None:
    """Regression: the default (marker) ``--live`` engine shares the buffer with VisionHook too."""
    vision_captured = _spy_vision_hook_buffer(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0

    shared = think_captured.get("buffer")
    assert shared is not None, "the marker engine must receive a shared buffer under --live"
    assert vision_captured.get("buffer") is shared


def test_bare_listen_run_visionhook_is_never_built(monkeypatch) -> None:
    """A non-live ``listen run`` builds NO VisionHook at all (unchanged).

    Vision only folds into the loop under ``--live`` (:func:`_build_live_hooks`);
    the bare loop never constructs one, so there is nothing to share a buffer with.
    """
    vision_captured = _spy_vision_hook_buffer(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch,
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "3"],
        transport=transport,
    )
    assert rc == 0
    assert vision_captured == {}, "a non-live run must never construct a VisionHook"


# ---------------------------------------------------------------------------
# 7. FaceHook shares the live cognition EventBuffer + VisionHook's frame source (t9)
#
# FaceHook folds face recognition into the loop the same way vision does. Two
# properties matter under --live: (a) it feeds the SAME shared EventBuffer the
# cognition engine consumes (so a recognised face reaches cognition), and (b) it
# reuses VisionHook's frame source — NO second grabber. Both are object-identity
# checks. Gated on cv2: the composition only builds FaceHook when the [vision]
# extra is importable (CI's bare install has no cv2, so FaceHook is skipped there
# with a warning — see _build_face_hook).
# ---------------------------------------------------------------------------


def _spy_vision_hook_instance(monkeypatch) -> dict:
    """Capture the ``buffer`` kwarg + the constructed :class:`VisionHook` instance."""
    captured: dict = {}
    real_init = VisionHook.__init__

    def _init(self, **kw):
        captured["buffer"] = kw.get("buffer")
        captured["instance"] = self
        return real_init(self, **kw)

    monkeypatch.setattr(VisionHook, "__init__", _init)
    return captured


def _spy_face_hook(monkeypatch) -> dict:
    """Capture the ``buffer`` + ``frame_provider`` every constructed :class:`FaceHook` gets."""
    captured: dict = {}
    real_init = FaceHook.__init__

    def _init(self, **kw):
        captured["buffer"] = kw.get("buffer")
        captured["frame_provider"] = kw.get("frame_provider")
        return real_init(self, **kw)

    monkeypatch.setattr(FaceHook, "__init__", _init)
    return captured


def test_live_agent_facehook_shares_buffer_and_vision_frame_source(monkeypatch) -> None:
    """``--live --cognition agent``: FaceHook shares the buffer AND VisionHook's frames."""
    pytest.importorskip("cv2")
    vision_captured = _spy_vision_hook_instance(monkeypatch)
    face_captured = _spy_face_hook(monkeypatch)
    pat_captured = _spy_pat_hook_buffer(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    shared = pat_captured.get("buffer")
    assert shared is not None, "PatHook must receive a shared buffer under --live"
    assert think_captured.get("buffer") is shared
    assert (
        face_captured.get("buffer") is shared
    ), "FaceHook must receive the SAME shared buffer PatHook/ThinkHook consume"

    # No second grabber: FaceHook's frame_provider is VisionHook's own latest-frame
    # peek — bound to the exact VisionHook instance built in this run.
    provider = face_captured.get("frame_provider")
    assert provider is not None, "FaceHook must be given a shared frame_provider"
    assert getattr(provider, "__self__", None) is vision_captured.get(
        "instance"
    ), "FaceHook must reuse VisionHook's frame source (no second grabber)"


def test_live_marker_facehook_shares_buffer_with_cognition_engine(monkeypatch) -> None:
    """Regression: the default (marker) ``--live`` engine shares the buffer with FaceHook too."""
    pytest.importorskip("cv2")
    face_captured = _spy_face_hook(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0

    shared = think_captured.get("buffer")
    assert shared is not None, "the marker engine must receive a shared buffer under --live"
    assert face_captured.get("buffer") is shared


def test_bare_listen_run_facehook_is_never_built(monkeypatch) -> None:
    """A non-live ``listen run`` builds NO FaceHook (vision folds only under --live)."""
    face_captured = _spy_face_hook(monkeypatch)
    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "3"],
        transport=transport,
    )
    assert rc == 0
    assert face_captured == {}, "a non-live run must never construct a FaceHook"


# ---------------------------------------------------------------------------
# 8. SceneHook + describe_scene tool share the live buffer + VisionHook frames (t10)
#
# Scene description folds into the loop the same way vision/face do. Under --live:
# (a) the periodic SceneHook feeds the SAME shared EventBuffer the cognition engine
# consumes (so a described scene reaches cognition), (b) it reuses VisionHook's
# frame source (NO second grabber), and (c) in --cognition agent mode the tool-use
# ToolRegistry additionally receives a ``describe_scene`` seam so the agent can look
# on demand — the SAME shared describe path. All gated on cv2 (the composition only
# builds the scene path when the [vision] extra is importable; CI's bare install
# skips these with a warning — see _build_scene_hook / _build_describe_scene_seam).
# ---------------------------------------------------------------------------


def _spy_scene_hook(monkeypatch) -> dict:
    """Capture the ``buffer`` + ``frame_provider`` every constructed :class:`SceneHook` gets."""
    captured: dict = {}
    real_init = SceneHook.__init__

    def _init(self, **kw):
        captured["buffer"] = kw.get("buffer")
        captured["frame_provider"] = kw.get("frame_provider")
        return real_init(self, **kw)

    monkeypatch.setattr(SceneHook, "__init__", _init)
    return captured


def test_live_agent_scenehook_shares_buffer_and_vision_frame_source(monkeypatch) -> None:
    """``--live --cognition agent``: SceneHook shares the buffer AND VisionHook's frames."""
    pytest.importorskip("cv2")
    vision_captured = _spy_vision_hook_instance(monkeypatch)
    scene_captured = _spy_scene_hook(monkeypatch)
    pat_captured = _spy_pat_hook_buffer(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    shared = pat_captured.get("buffer")
    assert shared is not None, "PatHook must receive a shared buffer under --live"
    assert think_captured.get("buffer") is shared
    assert (
        scene_captured.get("buffer") is shared
    ), "SceneHook must receive the SAME shared buffer PatHook/ThinkHook consume"

    # No second grabber: SceneHook's frame_provider is VisionHook's own latest-frame
    # peek — bound to the exact VisionHook instance built in this run.
    provider = scene_captured.get("frame_provider")
    assert provider is not None, "SceneHook must be given a shared frame_provider"
    assert getattr(provider, "__self__", None) is vision_captured.get(
        "instance"
    ), "SceneHook must reuse VisionHook's frame source (no second grabber)"


def test_live_marker_scenehook_shares_buffer_with_cognition_engine(monkeypatch) -> None:
    """Regression: the default (marker) ``--live`` engine shares the buffer with SceneHook."""
    pytest.importorskip("cv2")
    scene_captured = _spy_scene_hook(monkeypatch)
    think_captured: dict = {}
    real_think_init = ThinkHook.__init__

    def _think_init(self, provider, **kw):
        think_captured["buffer"] = kw.get("buffer")
        return real_think_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _think_init)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0

    shared = think_captured.get("buffer")
    assert shared is not None, "the marker engine must receive a shared buffer under --live"
    assert scene_captured.get("buffer") is shared


def test_bare_listen_run_scenehook_is_never_built(monkeypatch) -> None:
    """A non-live ``listen run`` builds NO SceneHook (scene folds only under --live)."""
    scene_captured = _spy_scene_hook(monkeypatch)
    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        ["listen", "run", "--json", "--transport", "sdk", "--deadband", "0", "--max-ticks", "3"],
        transport=transport,
    )
    assert rc == 0
    assert scene_captured == {}, "a non-live run must never construct a SceneHook"


def test_live_agent_registry_receives_a_describe_scene_seam(monkeypatch) -> None:
    """``--live --cognition agent``: the tool registry gets an on-demand describe_scene seam."""
    pytest.importorskip("cv2")
    _patch_registry(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    describe_scene = _FakeRegistry.last_kwargs.get("describe_scene")
    assert callable(describe_scene), "agent registry must receive a describe_scene seam"


# ---------------------------------------------------------------------------
# 9. forge tool + validator-gated auto-activation + startup reload (t13)
# ---------------------------------------------------------------------------


def test_live_agent_registry_receives_a_forge_seam(monkeypatch) -> None:
    """``--live --cognition agent``: the tool registry gets an injected forge dispatch seam."""
    _patch_registry(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    forge = _FakeRegistry.last_kwargs.get("forge")
    assert callable(forge), "agent registry must receive a forge dispatch seam"


def test_bare_marker_cognition_registry_has_no_forge_seam(monkeypatch) -> None:
    """The forge seam is only wired for the tool-use agent engine, never the marker path."""
    _patch_registry(monkeypatch)
    transport = _LiveSdkTransport()

    # marker is the default cognition — the agent registry is never built, so the
    # _FakeRegistry (patched over the agent path) is never constructed with a forge seam.
    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)
    assert rc == 0
    assert "forge" not in _FakeRegistry.last_kwargs


def test_live_agent_startup_reloads_active_forged_skills(monkeypatch, tmp_path) -> None:
    """Active forged skills are hot-registered into the live registry at composition (boot)."""
    active = tmp_path / "forge" / "active" / "wave-hello"
    active.mkdir(parents=True)
    (active / "SKILL.md").write_text(
        "---\nname: wave-hello\ndescription: Wave hello to a person.\n---\nbody\n"
    )
    (active / "executor.py").write_text("def execute(params, ctx):\n    return 'waved'\n")

    _fed, captured = _record_agent_feeds(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    engine = captured.get("engine")
    assert engine is not None, "the agent engine must be built"
    assert "wave-hello" in engine._registry.names(), "an active forged skill must reload at boot"


def test_forge_activation_announces_via_feed_forge_not_feed_scene(monkeypatch) -> None:
    """The forge auto-activation announce seam must be a forge-labeled cue, not a scene cue.

    Regression test for a Qodo review finding: ``_activate_forge`` wired the announce
    callable to ``EventBuffer.feed_scene``, so a self-extension event like "learned a new
    skill: wave-hello" rendered as ``"noticed: learned a new skill: wave-hello"`` with
    ``[SENSE source=scene]`` — a forge lifecycle event mislabeled as a VLM scene
    observation. It must render verbatim via ``feed_forge`` with ``source=forge``.
    """
    _patch_registry(monkeypatch)
    transport = _LiveSdkTransport()

    captured: dict = {}
    real_init = ForgeActivator.__init__

    def _spy_init(self, *, announce=None, **kw):
        captured["announce"] = announce
        return real_init(self, announce=announce, **kw)

    monkeypatch.setattr(ForgeActivator, "__init__", _spy_init)

    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent"), transport=transport
    )
    assert rc == 0

    announce = captured.get("announce")
    assert announce is not None, "forge activation must be wired with an announce callable"
    assert getattr(announce, "__name__", None) == "feed_forge", (
        "the announce seam must be EventBuffer.feed_forge, not feed_scene "
        "(a forge activation is not a scene observation)"
    )

    # Exercise the seam directly and confirm the cue lands verbatim, not scene-prefixed.
    buf = announce.__self__
    buf.snapshot()  # drain anything the boot run already produced
    announce("learned a new skill: wave-hello")
    cues = buf.snapshot()
    assert len(cues) == 1
    assert cues[0].text == "learned a new skill: wave-hello"
    assert not cues[0].text.startswith("noticed:")
