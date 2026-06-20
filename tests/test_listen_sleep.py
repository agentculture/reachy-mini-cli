"""Tests for folding the sleep decay→wake state machine into ``listen``'s loop.

``sleep`` used to run as its own noun against its own single-consumer SDK media
session.  Running it alongside ``listen`` is impossible (the hardware has ONE
single-consumer media subsystem; a second reader throttles to ~1 Hz) — exactly
the constraint that drove the #43 ``PatHook`` fold-in.  So sleep's decay-to-sleep
and wake logic is folded into ``listen``'s loop as a per-tick ``on_tick`` hook
(:class:`~reachy.motion.listen_sleep.SleepHook`) that consumes the loop's *shared*
per-tick :class:`~reachy.motion.sense_sample.SenseSample` (via an injected
:data:`~reachy.motion.sense_sample.SampleProvider`) and the loop's commanded head
pose — never opening a second media session.

These tests drive the hook seam directly with a fake sample provider, a fake
clock, and (for pat wake) a fake transport read-back.  No robot, no daemon, no
network, no real sleeps; everything is injected and deterministic.

Coverage (mirrors the acceptance criteria):

1. With no qualifying stimulation the injected-clock idle timer decays
   ALERT → DROWSY → ASLEEP and the hook raises ``sleep_active.flag`` when entering
   DROWSY/ASLEEP (the strongest idle interrupt).
2. A wake stimulus — speech / a loud RMS snap in the ``SenseSample``, or a head-pat
   deviation against the commanded pose — clears ``sleep_active.flag`` and resets
   the machine to ALERT.
3. The hook consumes the shared sample via the injected provider (NO second media
   session / no ``ReachyMini``), uses an injected clock, mirrors ``PatHook``'s
   shape (``(transport, queue, t, commanded_head)`` signature, silent degradation,
   ``close()`` clears the flag), and degrades silently on a missing sample / a
   read-back error.
"""

from __future__ import annotations

import pytest

import reachy.motion.sleep_signal as ss
from reachy.motion.listen_sleep import SleepHook
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample
from reachy.sleep.state import SleepState

# ---------------------------------------------------------------------------
# Isolation: pin the sleep-active flag into a throwaway state dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ss.clear()
    yield
    ss.clear()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FixedProvider:
    """A sample provider returning a fixed :class:`SenseSample` (or ``None``)."""

    def __init__(self, sample: SenseSample | None):
        self.sample = sample
        self.calls = 0

    def __call__(self) -> SenseSample | None:
        self.calls += 1
        return self.sample


_QUIET = SenseSample(rms=0.0, doa=None, speech=False, ts=0.0)


class _BaselinePoseTransport:
    """A transport whose head_pose holds flat at (0, 0) — no deviation, no pat."""

    name = "sdk"

    def __init__(self) -> None:
        self.pose_calls = 0

    def head_pose(self) -> tuple[float, float]:
        self.pose_calls += 1
        return (0.0, 0.0)


class _PressPoseTransport:
    """A transport whose head_pose alternates a deep downward press / release.

    A constant deviation registers only one edge-triggered press; alternating
    yields several distinct presses so the :class:`PatDetector` clears
    ``min_presses`` and fires a pat — modelling a real hand pressing the head.
    """

    name = "sdk"

    def __init__(self) -> None:
        self._tick = 0
        self.pose_calls = 0

    def head_pose(self) -> tuple[float, float]:
        self.pose_calls += 1
        self._tick += 1
        pressed = self._tick % 2 == 1
        return (-20.0, 0.0) if pressed else (0.0, 0.0)


# ---------------------------------------------------------------------------
# 1. Decay: ALERT → DROWSY → ASLEEP raises the flag with no stimulation
# ---------------------------------------------------------------------------


def test_decay_to_drowsy_then_asleep_raises_flag() -> None:
    """No stimulation → idle timer decays and the flag goes up on DROWSY/ASLEEP."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(
        provider,
        drowsy_after=10.0,
        asleep_after=20.0,
        audio_wake=False,  # no pat read-back needed; purely the idle decay path
    )
    transport = _BaselinePoseTransport()

    # t=0 — first tick anchors the idle clock; still ALERT, flag down.
    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ALERT
    assert ss.is_active() is False

    # t=12 — past drowsy_after (10) → DROWSY, flag raised.
    hook(transport, queue, 12.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.DROWSY
    assert ss.is_active() is True

    # t=25 — past asleep_after (20) → ASLEEP, flag still up.
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True


# ---------------------------------------------------------------------------
# 2a. Wake on speech in the SenseSample clears the flag + resets to ALERT
# ---------------------------------------------------------------------------


def test_speech_sample_wakes_and_clears_flag() -> None:
    """A speech cue in the sample resets the machine to ALERT and clears the flag."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=True)
    transport = _BaselinePoseTransport()

    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True

    # A speech sample arrives → wake, reset to ALERT, clear the flag.
    provider.sample = SenseSample(rms=0.0, doa=None, speech=True, ts=26.0)
    hook(transport, queue, 26.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ALERT
    assert ss.is_active() is False


# ---------------------------------------------------------------------------
# 2b. Wake on a loud RMS snap transient in the SenseSample
# ---------------------------------------------------------------------------


def test_loud_rms_snap_wakes_and_clears_flag() -> None:
    """A loud RMS spike (after a quiet history) snaps the robot awake."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(SenseSample(rms=0.001, doa=None, speech=False, ts=0.0))
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=True)
    transport = _BaselinePoseTransport()

    # Feed several quiet ticks to build the snap detector's rolling-average floor
    # while the idle clock decays to ASLEEP.
    for i in range(6):
        hook(transport, queue, float(i), {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True

    # A loud RMS spike (>> the quiet floor) fires the snap detector → wake.
    provider.sample = SenseSample(rms=0.5, doa=None, speech=False, ts=26.0)
    hook(transport, queue, 26.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ALERT
    assert ss.is_active() is False


# ---------------------------------------------------------------------------
# 2c. Wake on a head-pat deviation against the commanded pose
# ---------------------------------------------------------------------------


def test_head_pat_deviation_wakes_and_clears_flag() -> None:
    """A pat (read-back deviating from the commanded pose) wakes the robot.

    ``audio_wake=False`` (pat-only): acoustic cues are ignored, so the ONLY path
    to wake is a head pat — a read-back deviating from the commanded pose past the
    detector threshold.  The transport alternates a deep press / release to
    produce distinct press edges that clear ``min_presses``.
    """
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(
        provider,
        drowsy_after=10.0,
        asleep_after=20.0,
        audio_wake=False,
        pat_cooldown=0.0,
    )
    transport = _PressPoseTransport()

    # Decay to ASLEEP while the head holds flat (the press transport returns
    # baseline (0,0) on even reads; the commanded pose is also neutral here, so
    # no spurious pat yet because we only press once the loop is asleep — but the
    # press transport alternates from tick 0). To keep decay clean, drive the
    # FSM forward in time directly.
    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    # Commanded pose is neutral; the transport presses the head down by 20°, which
    # the detector reads as an external press against the commanded baseline.
    woke = False
    now = 0.4
    for _ in range(10):
        hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
        if hook.state is SleepState.ALERT and hook.woke_events >= 1:
            woke = True
            break
        now += 0.4

    assert woke, "a head pat must wake the robot in pat-only mode"
    assert hook.state is SleepState.ALERT
    assert ss.is_active() is False


def test_pat_only_ignores_speech_sample() -> None:
    """In pat-only mode (audio_wake=False) a speech cue does NOT wake the robot."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=False)
    transport = _BaselinePoseTransport()

    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP

    # Speech arrives but audio wake is off + head holds flat → stays asleep.
    provider.sample = SenseSample(rms=0.0, doa=None, speech=True, ts=26.0)
    hook(transport, queue, 26.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True


# ---------------------------------------------------------------------------
# 2d. A DoA shift wakes the robot (a new sound direction is a stimulus)
# ---------------------------------------------------------------------------


def test_doa_shift_wakes_and_clears_flag() -> None:
    """A DoA angle move past the deadband counts as a qualifying stimulus → wake."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(SenseSample(rms=0.0, doa=10.0, speech=False, ts=0.0))
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=True)
    transport = _BaselinePoseTransport()

    # Establish a stable DoA, then decay to ASLEEP (same direction = no shift).
    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP

    # The sound source jumps far to a new direction → DoA shift → wake.
    provider.sample = SenseSample(rms=0.0, doa=170.0, speech=False, ts=26.0)
    hook(transport, queue, 26.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ALERT
    assert ss.is_active() is False


# ---------------------------------------------------------------------------
# 3. Shared-sample contract + silent degradation + close() clears the flag
# ---------------------------------------------------------------------------


def test_hook_reads_only_the_injected_provider() -> None:
    """The hook reads cues ONLY from the injected provider (no second session)."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=True)
    transport = _BaselinePoseTransport()

    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 1.0, {"pitch": 0.0, "yaw": 0.0})
    # The provider was consulted once per tick — that is the ONLY audio source.
    assert provider.calls == 2


def test_missing_sample_degrades_silently() -> None:
    """A provider returning ``None`` (no fresh sample) must not raise; FSM still ticks."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(None)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=True)
    transport = _BaselinePoseTransport()

    # No sample → treated as "no stimulation"; the idle timer still decays.
    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True


def test_head_pose_error_degrades_silently() -> None:
    """A transport whose head_pose raises is treated as no deviation, never crashes."""
    from reachy.cli._errors import CliError

    class _RaisingTransport:
        name = "sdk"

        def head_pose(self):
            raise CliError(code=2, message="no sdk", remediation="install [sdk]")

    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=False)

    # A raising head_pose in pat-only mode → no pat, no crash; FSM still decays.
    hook(_RaisingTransport(), queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(_RaisingTransport(), queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert hook.state is SleepState.ASLEEP
    assert ss.is_active() is True


def test_close_clears_flag() -> None:
    """close() clears the sleep-active flag even when the robot was asleep."""
    queue: MotionQueue = MotionQueue()
    provider = _FixedProvider(_QUIET)
    hook = SleepHook(provider, drowsy_after=10.0, asleep_after=20.0, audio_wake=False)
    transport = _BaselinePoseTransport()

    hook(transport, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    hook(transport, queue, 25.0, {"pitch": 0.0, "yaw": 0.0})
    assert ss.is_active() is True

    hook.close()
    assert ss.is_active() is False


def test_close_is_idempotent_when_awake() -> None:
    """close() is safe (a no-op) when the flag was never raised."""
    hook = SleepHook(_FixedProvider(_QUIET), audio_wake=False)
    hook.close()  # never ran a tick → flag absent → no error
    assert ss.is_active() is False


def test_on_tick_signature_matches_pathook() -> None:
    """The hook is callable as ``(transport, queue, t, commanded_head)`` like PatHook.

    ``commanded_head`` is optional (defaults to neutral) so the hook drops cleanly
    into the same ``on_tick`` seam ``PatHook`` uses.
    """
    queue: MotionQueue = MotionQueue()
    hook = SleepHook(_FixedProvider(_QUIET), audio_wake=False)
    transport = _BaselinePoseTransport()
    # Both call shapes (with and without commanded_head) must work.
    hook(transport, queue, 0.0)
    hook(transport, queue, 1.0, {"pitch": 3.0, "yaw": -5.0})
    assert hook.state is SleepState.ALERT
