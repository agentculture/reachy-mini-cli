"""Direction invariants regression suite (t5).

Pins two invariants of the ``--transcribe`` words-only cognition design
(``docs/plans/2026-07-17-event-based-senses-pipeline.md``, task t5) so later
work on the senses pipeline (t6-t11: vision, face, scene) can't silently
regress them:

1. **Raw DoA cues are OFF under ``--transcribe``.** ``listen run --live
   --transcribe`` composes the folded :class:`~reachy.motion.listen_think.ThinkHook`
   (the seam behind BOTH the marker ``CognitionEngine`` and the tool-use
   ``AgentTurnEngine`` — see ``reachy/cli/_commands/listen.py``'s
   ``_build_think_hook`` / ``_build_agent_think_hook`` / ``_build_live_hooks``)
   with ``feed_doa_cues=False`` — this is the self-feedback fix that stops the
   robot reacting to its own TTS as "loud sound". The agent-engine half of this
   wiring is already pinned in ``tests/test_listen_cognition_agent.py``; this
   module closes the gap for the DEFAULT ("marker") cognition engine, which had
   no composition-level test.
2. **Direction still rides transcripts.** Even with raw DoA cues off, a
   transcribed utterance's DoA is still translated into a direction word and
   carried on the transcript cue itself
   (:meth:`~reachy.speech.events.EventBuffer.feed_transcript`, called by
   :class:`~reachy.motion.listen_transcribe.TranscribeHook._flush`) — so
   direction is not lost, only decoupled from the noisy per-tick raw feed. This
   pins both the exact cue wording (``EventBuffer.feed_transcript``, already
   covered directly in ``tests/test_speech_events.py``) AND the end-to-end
   wiring from a real ``TranscribeHook`` tick into a real ``EventBuffer``.

A third section documents (but does NOT implement in production) the rate-limit
contract a FUTURE standalone ``audio_direction`` sense event must honour, per
the same plan doc: "one direction event per 2s unless the bearing jumps 15
degrees", ported from ``reachy_nova``'s
``tracking.py::TrackingManager._maybe_fire_audio_direction``. No such event
exists in ``reachy/speech/events.py`` yet (direction rides transcripts only,
per invariant 2 above) — the reference implementation here is TEST-ONLY, so the
contract is proven by a fake-clock test ahead of any production code landing.

No robot, no daemon, no network, no real LLM/STT/TTS, no real threads, no real
sleeps.
"""

from __future__ import annotations

import contextlib
import io
import sys
import time

import numpy as np
import pytest

import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
from reachy.cli import main
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_transcribe import TranscribeHook, TranscribeTuning
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample
from reachy.speech.events import EventBuffer

# ---------------------------------------------------------------------------
# Isolation: pin every *_active flag into a throwaway state dir, no env
# leakage, and — crucially — patch the LLM streamer so the marker engine's
# background cognition worker (a REAL daemon thread once --live composes it)
# can never hit the network (mirrors tests/test_listen_cognition_agent.py).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("REACHY_COGNITION", raising=False)
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    monkeypatch.delenv("REACHY_ENGAGE_HEURISTIC", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    # The marker CognitionEngine resolves its streamer at construction time
    # (`self._stream_sentences = stream_sentences or _llm.stream_sentences`),
    # so patching the module-level default BEFORE listen composes the engine
    # makes the background worker's LLM turn a no-op iterator instead of a
    # real network call.
    monkeypatch.setattr("reachy.speech.llm.stream_sentences", lambda *a, **k: iter(()))

    for sig in (ps, ss, cs):
        sig.clear()
    yield
    for sig in (ps, ss, cs):
        sig.clear()


# ---------------------------------------------------------------------------
# A minimal fake sdk media session + transport (mirrors test_listen_live.py /
# test_listen_cognition_agent.py — kept self-contained here per the task's
# "no existing test file touched" constraint).
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


def _spy_thinkhook_feed_doa_cues(monkeypatch) -> dict:
    """Capture the ``feed_doa_cues`` kwarg every constructed ThinkHook receives."""
    captured: dict = {}
    real_init = ThinkHook.__init__

    def _spy(self, provider, **kw):
        captured["feed_doa_cues"] = kw.get("feed_doa_cues")
        return real_init(self, provider, **kw)

    monkeypatch.setattr(ThinkHook, "__init__", _spy)
    return captured


# ---------------------------------------------------------------------------
# 1. feed_doa_cues composition invariant — DEFAULT ("marker") cognition engine
#
# The --cognition agent half of this wiring is already pinned in
# tests/test_listen_cognition_agent.py
# (test_agent_thinkhook_feed_doa_cues_{false_under_transcribe,true_without_transcribe}).
# ThinkHook is the SAME class behind both engines (only the engine object built
# behind the seam differs, per _build_live_hooks's own docstring), but no
# existing test exercised the DEFAULT cognition path's composition — this
# closes that gap.
# ---------------------------------------------------------------------------


def test_marker_thinkhook_feed_doa_cues_false_under_transcribe(monkeypatch) -> None:
    """``listen run --live --transcribe`` (default cognition) builds ThinkHook
    with ``feed_doa_cues=False`` — words-only cognition, no raw DoA cue feed.
    """
    captured = _spy_thinkhook_feed_doa_cues(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv("--transcribe"), transport=transport)

    assert rc == 0
    assert captured.get("feed_doa_cues") is False, "words-only mode must not feed raw DoA cues"


def test_marker_thinkhook_feed_doa_cues_true_without_transcribe(monkeypatch) -> None:
    """``listen run --live`` (no ``--transcribe``, default cognition) keeps
    ``feed_doa_cues=True`` — the pre-existing raw-DoA-cue baseline is unchanged.
    """
    captured = _spy_thinkhook_feed_doa_cues(monkeypatch)
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(monkeypatch, _live_argv(), transport=transport)

    assert rc == 0
    assert captured.get("feed_doa_cues") is True


# ---------------------------------------------------------------------------
# 2. Direction still rides transcripts, even with raw DoA cues off
# ---------------------------------------------------------------------------


def test_feed_transcript_direction_cue_wording_pinned() -> None:
    """Pin :meth:`EventBuffer.feed_transcript`'s exact direction-tagged wording.

    A regression here would silently change what cognition "reads" as the
    speaker's direction — this is the wording the LLM prompt is built from.
    """
    buf = EventBuffer()

    buf.feed_transcript("hello there", direction="left")

    cues = buf.snapshot()
    assert len(cues) == 1
    assert cues[0].text == 'heard someone say (from the left): "hello there"'


def test_transcribe_hook_direction_reaches_real_event_buffer_cue() -> None:
    """End-to-end: a real ``TranscribeHook`` tick against a real ``EventBuffer``
    still renders the ``heard someone say (from the <dir>)`` cue.

    This is the invariant's OTHER half: raw per-tick DoA cues are off under
    ``--transcribe`` (section 1 above), but the direction of a transcribed
    utterance must still reach cognition via the transcript cue itself — it is
    decoupled from the noisy per-tick feed, not lost. Uses a name-matching
    utterance ("reachy ...") so the built-in heuristic engagement gate engages
    with zero classifier/network calls (mirrors the existing pattern in
    ``tests/test_listen_transcribe.py``).
    """

    class _FakeTranscriber:
        def __init__(self, text: str) -> None:
            self._text = text

        def transcribe_once(self, audio):  # noqa: ARG002
            return self._text

    buffer = EventBuffer()
    holder: dict = {"s": None}
    hook = TranscribeHook(
        lambda: holder["s"],
        buffer=buffer,
        transcriber=_FakeTranscriber("reachy hello there"),
        tuning=TranscribeTuning(min_utterance_s=0.0),
    )
    chunk = np.full(256, 0.05, dtype=np.float32)

    # doa=10.0 degrees is near the left in the 0=left/90=front/180=right
    # convention (see reachy.speech.events._doa_direction), matching the
    # already-pinned mapping in tests/test_listen_transcribe.py.
    holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=chunk)
    hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
    # A silent tick past silence_hold_s (default 0.7s) flushes the utterance.
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})

    cues = buffer.snapshot()
    assert len(cues) == 1
    assert cues[0].text == 'heard someone say (from the left): "reachy hello there"'


# ---------------------------------------------------------------------------
# 3. FUTURE direction-event rate-limit contract (test-local reference spec)
#
# No standalone "audio_direction" sense event exists in production yet —
# direction rides transcripts only (section 2 above). This class documents the
# CONTRACT any future such event must honour, ported from reachy_nova's
# tracking.py::TrackingManager._maybe_fire_audio_direction (the same proven
# algorithm this repo already cites for other rate-limited detectors). It is
# NOT wired to any production code path — do not import it outside this file.
# ---------------------------------------------------------------------------


class _DirectionEventRateLimiter:
    """TEST-ONLY reference spec for a FUTURE ``audio_direction`` sense event.

    Ported from ``reachy_nova``'s
    ``tracking.py::TrackingManager._maybe_fire_audio_direction`` (see
    ``docs/plans/2026-07-17-event-based-senses-pipeline.md`` t5's acceptance
    criteria: "a fake-clock test documents the nova rate-limit contract — one
    direction event per 2s unless the bearing jumps 15 degrees — for any future
    direction event"). This class exists ONLY so a fake-clock test can pin that
    contract ahead of any real ``audio_direction`` event landing in
    ``reachy/speech/events.py`` (tracked as t9-t11 in the same plan). It has no
    caller in production code.

    Contract
    --------
    :meth:`should_emit` returns ``True`` (and latches the new emit time +
    bearing) when EITHER:

    * at least ``rate_limit_s`` (default 2.0s) has elapsed since the last
      emitted event, OR
    * the bearing has moved at least ``bearing_jump_deg`` (default 15.0
      degrees) since the last emitted event's bearing —

    and latches immediately in either case, resetting the window. The very
    first call always emits (there is no "last" event yet). A tiny epsilon
    absorbs float round-trip noise so a jump of exactly the threshold reliably
    counts as "15+ degrees" rather than landing a hair under it (mirrors
    nova's own comment on this exact edge case).
    """

    def __init__(
        self,
        *,
        rate_limit_s: float = 2.0,
        bearing_jump_deg: float = 15.0,
        clock=None,
    ) -> None:
        self._rate_limit_s = rate_limit_s
        self._bearing_jump_deg = bearing_jump_deg
        self._clock = clock if clock is not None else time.monotonic
        self._last_emit_t: float | None = None
        self._last_bearing: float | None = None

    def should_emit(self, bearing_deg: float) -> bool:
        now = self._clock()
        first_emit = self._last_emit_t is None
        window_elapsed = first_emit or (now - self._last_emit_t) >= self._rate_limit_s
        bearing_jumped = (
            self._last_bearing is not None
            and abs(bearing_deg - self._last_bearing) >= self._bearing_jump_deg - 1e-9
        )
        if not (window_elapsed or bearing_jumped):
            return False
        self._last_emit_t = now
        self._last_bearing = bearing_deg
        return True


class _FakeClock:
    """A manually-advanced clock for deterministic rate-limit tests."""

    def __init__(self, t: float = 0.0) -> None:
        self._t = t

    def advance(self, dt: float) -> None:
        self._t += dt

    def __call__(self) -> float:
        return self._t


def test_direction_rate_limiter_first_call_always_emits() -> None:
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)

    assert limiter.should_emit(0.0) is True


def test_direction_rate_limiter_suppresses_within_window_same_bearing() -> None:
    """No further emission before 2.0s elapses, with the bearing unchanged."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.5)
    assert limiter.should_emit(0.0) is False

    clock.advance(1.4)  # total 1.9s since the last emit — still inside the window
    assert limiter.should_emit(0.0) is False


def test_direction_rate_limiter_emits_once_window_elapses() -> None:
    """At >= 2.0s elapsed the window re-opens even with an unchanged bearing."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(2.0)  # exactly the rate limit — the boundary counts as elapsed
    assert limiter.should_emit(0.0) is True


def test_direction_rate_limiter_bearing_jump_emits_immediately() -> None:
    """A bearing jump of exactly 15 degrees emits despite the window not elapsing."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)  # well inside the 2.0s window
    assert limiter.should_emit(15.0) is True, "a >=15 degree jump must emit immediately"


def test_direction_rate_limiter_bearing_jump_below_threshold_is_suppressed() -> None:
    """A jump just under 15 degrees does NOT bypass the rate limit."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)
    assert limiter.should_emit(14.9) is False


def test_direction_rate_limiter_jump_resets_the_window() -> None:
    """After a jump-triggered emit, the next call re-measures from THAT emit."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)
    assert limiter.should_emit(15.0) is True  # jump -> emits, resets window + bearing

    clock.advance(0.1)  # only 0.1s since the jump-emit, and no further jump
    assert limiter.should_emit(15.0) is False

    clock.advance(2.0)  # now >= 2.0s since the jump-emit -> window re-opens
    assert limiter.should_emit(15.0) is True
