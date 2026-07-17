"""Tests for folding head-pat detection into the ``listen`` sdk loop.

``listen`` now does BOTH at once in its sdk loop: it keeps orienting toward sound
*and* detects proprioceptive head pats (a downward press → ``scratch``, a sideways
nudge → ``side_pat``), leaning into them. Pat detection used to live only in the
separate ``pat`` noun — but running ``pat`` and ``listen`` together is impossible
(single-consumer sdk mic + they fight over the head), and a separate ``pat``
process is throttled to ~1 Hz by sdk contention. So the detection is folded into
``listen``'s loop, which already owns the one sdk client.

These tests exercise the seam directly (:class:`~reachy.motion.listen_pat.PatHook`
via :func:`reachy.motion.server.run`'s ``on_tick``) and end-to-end through
``reachy listen run --json`` with a fake sdk transport. No robot, no daemon, no
network, no real sleeps; clocks are injected.

Coverage (mirrors the spec):

1. A deviating ``head_pose()`` → :class:`PatReaction` enqueues a lean onto the
   loop queue AND the ``pat_signal`` flag is written (tmp state dir).
2. Baseline ``head_pose()`` ≈ (0, 0) → NO pat event, and listen's normal
   sound-orient still drives motion.
3. During the reaction window the head pose is NOT sensed (no double-trigger).
4. ``server.run`` with ``on_tick=None`` behaves byte-identically to before.
5. The http loop installs no pat hook.
6. ``--no-pat`` disables it.
"""

from __future__ import annotations

import contextlib
import json

import numpy as np
import pytest

import reachy.motion.pat_signal as ps
from reachy.cli import main
from reachy.motion.listen_pat import PatHook
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionAction, MotionQueue
from reachy.motion.server import LoopHooks, run

# ---------------------------------------------------------------------------
# Isolation: pin the pat-active flag into a throwaway state dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ps.clear()
    yield
    ps.clear()


# ---------------------------------------------------------------------------
# Fake sdk media sessions + transports (mirror tests/test_listen_acceptance.py)
# ---------------------------------------------------------------------------


class _QuietFrontSession:
    """Quiet, front-facing audio: never a snap, never off-axis, no speech.

    Front sound (angle ≈ pi/2 rad → desired ≈ 0°) so the sound-orient path stays
    quiet; this isolates the pat path from spurious sound-orient motion. RMS is
    above the floor so the loop runs normally but never snaps.
    """

    _SAMPLE = np.full(512, 0.001, dtype=np.float32)  # below min_rms → no snap

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}  # front, no speech

    def get_audio_sample(self):
        return self._SAMPLE

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _SpeechOffAxisSession:
    """Steady quiet-but-present audio + speech_detected=True from the left.

    Drives listen's Tier-2 sound-orient turn (used to assert sound-orient still
    works alongside / without pat).
    """

    _SAMPLE = np.full(512, 0.03, dtype=np.float32)

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": 0.0, "speech_detected": True}

    def get_audio_sample(self):
        return self._SAMPLE

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _PatSdkTransport:
    """A fake sdk transport: exposes media_session() AND a scripted head_pose().

    ``head_pose`` alternates between a deep downward press and a release on
    successive reads — a constant deviation registers only one edge-triggered
    press, but alternating yields several distinct presses so the detector clears
    ``min_presses`` and fires a scratch. Every ``move_goto`` is recorded.
    """

    name = "sdk-pat"

    def __init__(self, session, *, pressed_pitch: float = -20.0, baseline: bool = False):
        self.gotos: list[dict] = []
        self._session = session
        self._tick = 0
        self.pose_calls = 0
        self._pressed_pitch = pressed_pitch
        self._baseline = baseline

    def head_pose(self) -> tuple[float, float]:
        self.pose_calls += 1
        if self._baseline:
            return (0.0, 0.0)
        self._tick += 1
        pressed = self._tick % 2 == 1  # alternate pressed / released each read
        return (self._pressed_pitch, 0.0) if pressed else (0.0, 0.0)

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(
            {
                "head": head,
                "antennas": antennas,
                "body_yaw": body_yaw,
                "duration": duration,
            }
        )
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        yield self._session


class _HttpTransport:
    """A fake http/remote transport: doa() but NO media_session, NO head_pose."""

    name = "http"

    def __init__(self):
        self.gotos: list[dict] = []

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append({"head": head, "duration": duration})
        return {"uuid": "fake"}


# ---------------------------------------------------------------------------
# 1. PatHook seam: a deviating head_pose enqueues a lean + writes the flag
# ---------------------------------------------------------------------------


class _ConstantPressTransport:
    """A bare transport whose head_pose alternates a deep press / release."""

    name = "sdk"

    def __init__(self):
        self._tick = 0

    def head_pose(self) -> tuple[float, float]:
        self._tick += 1
        pressed = self._tick % 2 == 1
        return (-20.0, 0.0) if pressed else (0.0, 0.0)


def test_pathook_enqueues_lean_and_writes_flag() -> None:
    """A deviating head_pose drives PatHook → a lean is queued and the flag is set."""
    queue: MotionQueue = MotionQueue()
    # Deterministic detector: 2 presses, no inter-pat cooldown, fixed level2 threshold.
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)
    transport = _ConstantPressTransport()

    fired_at = None
    for i in range(8):
        t = 0.4 * i  # well inside the pat_window (3.0 s) so presses accumulate
        hook(transport, queue, t)
        if hook.events >= 1 and fired_at is None:
            fired_at = t
            break

    assert hook.events >= 1, "expected a pat detection from the deviating head pose"
    # A lean→nuzzle→settle gesture was enqueued onto the loop's queue.
    labels = [a.label for a in queue.pending()]
    assert any("lean" in label for label in labels), labels
    assert any(label.startswith("pat_") for label in labels), labels
    # The pat-active flag is written while the reaction window is open.
    assert ps.is_active() is True


# ---------------------------------------------------------------------------
# 2b. listen's own sound-orienting turn does NOT false-fire the pat detector
# ---------------------------------------------------------------------------


class _TurningHeadTransport:
    """A transport whose head_pose follows a scripted yaw ramp (a listen turn).

    Models listen committing a smooth minjerk turn to a held heading: the actual
    pose ramps up to the target and stays there. The pat hook commands baseline
    (0, 0) (no ``commanded_head`` passed), so the read-back appears as a yaw
    deviation — the test verifies a *sustained* turn does not register as a side_pat.
    """

    name = "sdk"

    def __init__(self, yaws: list[float]):
        self._yaws = yaws
        self._i = 0

    def head_pose(self) -> tuple[float, float]:
        yaw = self._yaws[min(self._i, len(self._yaws) - 1)]
        self._i += 1
        return (0.0, yaw)


def test_sustained_listen_turn_does_not_false_fire_pat() -> None:
    """A single smooth turn to a held heading must NOT register as a side_pat.

    The yaw deviation crosses the press threshold once (an edge), setting the
    in-press latch; while the head holds, the deviation stays high so no further
    press edges register — a single edge is below ``min_presses``, so no pat
    fires. (The slow EMA baseline additionally pulls the held deviation down over
    time, releasing the latch gently rather than as fresh impulses.) This is the
    common case the design relies on: listen's reactive turn never feels like a pat.
    """
    # Ramp 0 → 30° over 24 ticks (a 1.2 s minjerk turn at 20 Hz), then hold at 30°.
    ramp = [30.0 * (i + 1) / 24.0 for i in range(24)]
    hold = [30.0] * 80
    transport = _TurningHeadTransport(ramp + hold)

    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)

    now = 0.0
    for _ in range(len(ramp) + len(hold)):
        hook(transport, queue, now)
        now += 0.05

    assert hook.events == 0, "a sustained sound-orient turn must not fire a pat"
    assert not queue.pending(), "no pat gesture should be enqueued for a held turn"


# ---------------------------------------------------------------------------
# 2c. (FIX 1) actual that FOLLOWS the commanded baseline never fires a pat
# ---------------------------------------------------------------------------


class _FollowsCommandedTransport:
    """A transport whose head_pose exactly equals whatever it is told to track.

    The test sets each read-back to the commanded pose handed to the hook on that
    tick, modelling the robot settling onto ``listen``'s own non-neutral idle /
    orient pose. With deviation = actual − commanded = 0, no press should register.
    """

    name = "sdk"

    def __init__(self) -> None:
        self.next_pose = (0.0, 0.0)

    def head_pose(self) -> tuple[float, float]:
        return self.next_pose


def test_actual_following_nonzero_commanded_pose_does_not_fire_pat() -> None:
    """When the read-back tracks a non-zero commanded idle pose, no pat fires.

    This is the Qodo bug fix: ``listen`` commands non-neutral pitch/yaw (idle
    wander + orient turns), and the hook is now told that commanded pose. The
    actual pose equalling the commanded pose means zero deviation — so the robot's
    own deliberate motion is never mistaken for an external press.
    """
    transport = _FollowsCommandedTransport()
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)

    now = 0.0
    for i in range(40):
        # listen drives a lively idle pose: pitch breathes, yaw wanders far past
        # the press threshold — and the head exactly follows it.
        commanded = {"pitch": 5.0 + 2.0 * (i % 3), "yaw": 25.0 * ((i % 5) - 2)}
        transport.next_pose = (commanded["pitch"], commanded["yaw"])
        hook(transport, queue, now, commanded)
        now += 0.05

    assert hook.events == 0, "actual following the commanded pose must not fire a pat"
    assert not queue.pending(), "listen's own non-neutral motion must enqueue no pat gesture"


def test_actual_deviating_from_commanded_baseline_fires_pat() -> None:
    """A press *on top of* a non-zero commanded pose still fires a pat.

    The robot holds a non-neutral commanded idle pose; an external hand presses
    the head down, so the read-back deviates from the commanded baseline by more
    than the press threshold (alternating to produce distinct press edges). The
    deviation is measured against the commanded pose — so the real press is caught
    even while ``listen`` is mid-pose.
    """
    transport = _FollowsCommandedTransport()
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)

    commanded = {"pitch": 6.0, "yaw": 15.0}  # a held, non-neutral listen pose
    fired = False
    now = 0.0
    for i in range(8):
        pressed = i % 2 == 0
        # Alternate a deep downward press (−20° below commanded) and a release
        # (back to commanded) to produce distinct press edges past the threshold.
        actual_pitch = commanded["pitch"] - 20.0 if pressed else commanded["pitch"]
        transport.next_pose = (actual_pitch, commanded["yaw"])
        hook(transport, queue, now, commanded)
        if hook.events >= 1:
            fired = True
            break
        now += 0.4  # inside the pat_window (3.0 s) so presses accumulate

    assert fired, "a press deviating from the commanded baseline must fire a pat"
    labels = [a.label for a in queue.pending()]
    assert any(label.startswith("pat_") for label in labels), labels
    assert ps.is_active() is True


# ---------------------------------------------------------------------------
# 3. During the reaction window the head pose is NOT sensed (no double-trigger)
# ---------------------------------------------------------------------------


def test_pathook_pauses_sensing_during_reaction_window() -> None:
    """Once a pat fires, head_pose is not read again until the window elapses.

    The reaction window is :func:`reaction_duration` (~3.5 s for level1). We drive
    25 ticks at 0.1 s = 2.4 s total so the run stays *inside* the window after the
    fire: a second pat must not re-fire, and head_pose must stop being read.
    """
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)
    transport = _ConstantPressTransport()

    calls_at_fire = None
    fire_tick = None
    for i in range(25):
        t = 0.1 * i
        hook(transport, queue, t)
        if hook.events >= 1 and calls_at_fire is None:
            calls_at_fire = transport._tick  # head_pose call count at the fire tick
            fire_tick = i

    assert hook.events == 1, "the reaction window must suppress a second pat re-fire"
    assert fire_tick is not None and fire_tick < 24, "the pat should fire early in the run"
    # head_pose is NOT polled after the fire — sensing is paused inside the window.
    assert transport._tick == calls_at_fire, "head_pose must not be read during the window"
    # The flag is held up while the window is open, then cleared on close().
    assert ps.is_active() is True
    hook.close()
    assert ps.is_active() is False


def test_pathook_close_clears_flag_even_mid_reaction() -> None:
    """close() never leaks the pat-active flag, even called mid-window."""
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)
    transport = _ConstantPressTransport()
    for i in range(8):
        hook(transport, queue, 0.4 * i)
    # If a pat fired the flag is up; close() must clear it regardless.
    hook.close()
    assert ps.is_active() is False


# ---------------------------------------------------------------------------
# 3b. (t1 fix) large-move gating + re-baseline kill the false-fire loop —
#     WITHOUT starving detection under the always-alive idle cadence
#
# A minjerk goto takes >1 s in transit; during it the actual pose lags the
# commanded target by construction, so the OLD hook read that lag as an external
# press and false-fired (147 phantom pats in 51 min, nobody touching the robot).
# But a binary any-move gate over-corrects: the idle layer keeps a (small) move
# in flight ~90 % of wall time, so "skip sensing whenever busy" silently killed
# real pats on the live robot (a scratch produced nothing). The fix is
# amplitude-aware: only a commanded jump > LARGE_MOVE_THRESHOLD_DEG suspends
# sensing, and only until THAT move's published busy horizon; holds and
# sub-degree breaths are sensed straight through. The first sensing pass after
# any suspension re-baselines the detector so the settled pose reads as zero
# deviation (no self-sustaining loop). A genuine press on a steady-or-idling
# head must still fire at today's thresholds.
# ---------------------------------------------------------------------------


class _ScriptedPoseTransport:
    """A bare sdk transport whose head_pose returns a caller-set pose, counted.

    ``pose`` is set by the test each tick; ``pose_calls`` records how many times
    the hook actually read the head pose back (so a test can prove sensing was
    suppressed while a move was in flight — the read never happens).
    """

    name = "sdk"

    def __init__(self) -> None:
        self.pose: tuple[float, float] = (0.0, 0.0)
        self.pose_calls = 0

    def head_pose(self) -> tuple[float, float]:
        self.pose_calls += 1
        return self.pose


def test_in_flight_move_suppresses_sensing_no_pat() -> None:
    """While an unknown-start move is in flight at startup, sensing rides it out.

    On the very first tick the hook has no previous commanded pose to diff against,
    so an in-flight move's start is unknown and no expected trajectory can be
    computed: sensing skips to the published horizon, so a mid-transit pose (which
    reads like a deep press) never reaches the detector and no pat fires. This is
    the core bug: transit lag must never be mistaken for a hand. It is also the
    ONLY unsensed window — every tracked move afterwards is sensed via its
    expected minjerk pose.
    """
    busy = {"until": 100.0}  # a move in flight from before the hook's first tick
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    now = 0.0
    for i in range(40):
        # A hand-like alternating deep press — would fire if it were ever unmasked.
        transport.pose = (-20.0, 0.0) if i % 2 == 0 else (0.0, 0.0)
        hook(transport, queue, now)
        now += 0.1

    assert hook.events == 0, "no pat may fire while a commanded move is in flight"
    assert transport.pose_calls == 0, "an unknown-start move is ridden out unsensed"
    assert not queue.pending(), "no pat gesture should be enqueued during a commanded move"


def test_first_pass_after_reaction_window_rebaselines() -> None:
    """The first sensing pass after a reaction window closes re-baselines the detector.

    After a pat reaction the idle wander resumes with a fresh goto whose transit used
    to re-trigger the detector — a self-sustaining loop. The first sensing pass once
    the window elapses must call ``detector.reset()`` so the settled resume pose reads
    as zero deviation, and no second pat fires from the robot's own motion.
    """
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)

    # Count press-state clears (the suspension re-baseline) via an instance spy.
    # NOTE: this is clear_presses, not reset — the EMA baselines must survive.
    resets = {"n": 0}
    orig_clear = detector.clear_presses

    def _counting_clear() -> None:
        resets["n"] += 1
        orig_clear()

    detector.clear_presses = _counting_clear  # type: ignore[method-assign]

    hook = PatHook(queue, detector=detector)
    transport = _ScriptedPoseTransport()

    # Phase 1: a real pat fires (alternating deep presses against a neutral command).
    now = 0.0
    for i in range(8):
        transport.pose = (-20.0, 0.0) if i % 2 == 0 else (0.0, 0.0)
        hook(transport, queue, now)
        if hook.events >= 1:
            break
        now += 0.4
    assert hook.events == 1
    resets_at_fire = resets["n"]  # the fire path already reset once
    calls_at_fire = transport.pose_calls
    reacting_until = hook._reacting_until

    # Phase 2: advance through the reaction window — sensing is paused (no pose reads).
    t = now + 0.1
    while t < reacting_until:
        hook(transport, queue, t)
        t += 0.1
    assert transport.pose_calls == calls_at_fire, "head_pose must not be read during the window"

    # Phase 3: first sensing pass AFTER the window, with a settled pose (actual ==
    # commanded == neutral). It must re-baseline and read zero deviation — no re-fire.
    transport.pose = (0.0, 0.0)
    hook(transport, queue, reacting_until + 0.05)

    assert resets["n"] == resets_at_fire + 1, "the first post-window pass must re-baseline"
    assert hook.events == 1, "the settled resume pose must not re-fire a pat"
    assert len(detector.press_times) == 0, "re-baseline must clear stale press state"


def test_genuine_press_on_steady_head_still_detects_at_default_thresholds() -> None:
    """A hand press on a steady (not-in-flight) head still fires a pat — today's thresholds.

    The published horizon is in the past (the robot holds a steady commanded pose),
    so the hook senses every tick and a genuine external press still crosses the
    DEFAULT detector thresholds (only the level2 jitter is pinned for determinism). The
    fix suppresses self-motion, never a real pat on a settled head.
    """
    busy = {"until": 0.0}  # never in flight → steady head
    queue: MotionQueue = MotionQueue()
    # Default thresholds (min_presses=2, press_threshold=1.2, pat_cooldown=2.0) — only
    # the level2 jitter is pinned so the test is deterministic.
    detector = PatDetector(level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    commanded = {"pitch": 0.0, "yaw": 0.0}
    fired = False
    now = 0.0
    for i in range(12):
        transport.pose = (-20.0, 0.0) if i % 2 == 0 else (0.0, 0.0)
        hook(transport, queue, now, commanded)
        if hook.events >= 1:
            fired = True
            break
        now += 0.4

    assert fired, "a genuine press on a steady head must still fire a pat at today's thresholds"
    labels = [a.label for a in queue.pending()]
    assert any(label.startswith("pat_") for label in labels), labels
    assert ps.is_active() is True


def test_server_run_publishes_busy_and_gates_pathook() -> None:
    """server.run publishes its busy horizon so a folded PatHook skips transit end-to-end.

    A producer emits one long LARGE move (a 30° pitch jump); the transport's actual
    pose stays pinned (a lag-shaped worst case). Measured against the expected
    minjerk pose, that reads as one long sustained deviation — a single press with
    no release edges — which can never reach ``min_presses``, so no pat fires
    through the real ``server.run`` seam. (A real hand alternates press/release
    edges; sustained transit-shaped deviation does not.)
    """
    busy = {"until": 0.0}
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])

    class _TransitTransport:
        name = "sdk"

        def __init__(self) -> None:
            self.pose_calls = 0
            self.gotos: list[float] = []

        def head_pose(self) -> tuple[float, float]:
            self.pose_calls += 1
            return (-20.0, 0.0)  # would look like a deep press if sensed mid-transit

        def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
            self.gotos.append(duration)
            return {"uuid": "x"}

    class _OneLongMove:
        def __init__(self) -> None:
            self.done = False

        def update(self, t, sense, **_kwargs):
            if self.done:
                return None
            self.done = True
            return MotionAction(label="idle", head={"pitch": 30.0}, duration=2.0)

    transport = _TransitTransport()
    run(
        transport,
        _OneLongMove(),
        now=_Clock(0.1),
        sleep=lambda *_: None,
        tick=0.1,
        settle=0.2,
        max_ticks=15,  # 1.5 s < move (2.0) + settle (0.2) → the move stays in flight
        queue=queue,
        hooks=LoopHooks(on_tick=hook),
        busy=busy,
    )
    assert hook.events == 0, "no pat may fire while the loop's move is in flight"
    # The pose is read and sensed every tick — expectation-based sensing never skips;
    # the sustained transit-shaped deviation simply never produces press edges.
    assert transport.pose_calls >= 2, "the pose keeps being read during the transit"


def test_press_detected_through_continuous_small_idle_moves() -> None:
    """THE live regression: a real press must detect while small idle moves run non-stop.

    The always-alive idle layer dispatches back-to-back holds/sub-degree breaths, so a
    (small) move is in flight ~90 % of wall time. The binary any-move gate starved the
    detector completely — a real head scratch on the live robot produced nothing. With
    amplitude-aware gating, small commanded jitter (jump <= LARGE_MOVE_THRESHOLD_DEG)
    never suspends sensing, so the press still fires even though the published busy
    horizon is perpetually in the future.
    """
    busy = {"until": 0.0}  # nothing in flight on the very first tick (as at live start)
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    now = 0.0
    fired = False
    for i in range(16):
        # From tick 2 on, a small idle move is ALWAYS in flight (live cadence).
        busy["until"] = now + 2.4
        # Sub-threshold commanded breathing jitter (jump 0.8 deg <= threshold 1.0).
        commanded = {"pitch": 0.8 if i % 2 else 0.0, "yaw": 0.0}
        # The hand: alternating deep press; on release the untouched head TRACKS its
        # commanded pose (deviation ~0), as on the real robot.
        transport.pose = (-20.0, 0.0) if i % 2 == 0 else (commanded["pitch"], 0.0)
        hook(transport, queue, now, commanded)
        if hook.events >= 1:
            fired = True
            break
        now += 0.4

    assert fired, "a real press must still detect under the always-alive idle cadence"
    assert transport.pose_calls > 0, "small in-flight moves must not suppress sensing"


def test_pitch_scratch_detects_through_yaw_look_transit() -> None:
    """Expectation sensing: clean transit reads zero; a scratch mid-look still fires.

    Sequence mirrors the live journal's active wander (large yaw drifts nearly
    back-to-back, which starved every gating variant for minutes). During a 13° yaw
    look, an actual pose that TRACKS the expected minjerk trajectory reads ≈ 0
    deviation and never fires — and a deep alternating pitch press layered ON TOP
    of that tracking pose (a hand scratching mid-move) fires a scratch while the
    move is still in flight.
    """
    busy = {"until": 0.0}
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    # Phase 1: steady at neutral, settled (nothing in flight) — sensing runs clean.
    now = 0.0
    for _ in range(3):
        transport.pose = (0.0, 0.0)
        hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
        now += 0.1
    assert hook.events == 0

    # Phase 2: the look dispatches — commanded yaw jumps to 13°, horizon now+1.7.
    # The actual pose follows the expected trajectory exactly (a head tracking its
    # plan). On the dispatch tick the expectation starts at the move's start pose.
    look_cmd = {"pitch": 0.0, "yaw": 13.0}
    busy["until"] = now + 1.7
    transport.pose = (0.0, 0.0)  # dispatch tick: still at the start pose
    hook(transport, queue, now, look_cmd)
    now += 0.1
    for _ in range(4):
        expected = hook._expected_head(now, look_cmd)
        transport.pose = expected  # clean transit: tracking the plan
        hook(transport, queue, now, look_cmd)
        now += 0.1
    assert hook.events == 0, "a head tracking its planned trajectory must not fire"

    # Phase 3: still INSIDE the look's transit, a hand scratches — deep alternating
    # pitch deviation layered on top of the tracking pose. It fires mid-move.
    fired = False
    for i in range(8):
        expected = hook._expected_head(now, look_cmd)
        press = -20.0 if i % 2 == 0 else 0.0
        transport.pose = (expected[0] + press, expected[1])
        hook(transport, queue, now, look_cmd)
        if hook.events >= 1:
            fired = True
            break
        now += 0.2

    assert fired, "a pitch scratch during a yaw look's transit must still fire"
    labels = [a.label for a in queue.pending()]
    assert any(label.startswith("pat_scratch") for label in labels), labels


def test_dispatch_tracking_continues_through_reaction_window() -> None:
    """The reaction's own moves are tracked while sensing is paused — no phantom chain.

    Live failure mode: during the reaction window the hook paused BOTH sensing and
    dispatch tracking, so the reaction's lean/nuzzle/settle and the idle-resume move
    left the previous-commanded state stale; the first post-window expectation then
    interpolated from a wrong start, and its bogus deviation re-seeded a fresh
    phantom reaction — a self-sustaining chain (six back-to-back cycles in the
    14:38 journal). Tracking must stay fresh through the window, and a head that
    tracks its plan after the window must not re-fire.
    """
    busy = {"until": 0.0}
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    # Phase 1: a genuine press on a settled head fires the first (real) detection.
    now = 0.0
    for i in range(8):
        transport.pose = (-20.0, 0.0) if i % 2 == 0 else (0.0, 0.0)
        hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
        if hook.events >= 1:
            break
        now += 0.4
    assert hook.events == 1
    window_end = hook._reacting_until

    # Phase 2 (inside the window): the reaction lean dispatches — commanded pitch
    # jumps to 12°. Sensing is paused, but the dispatch MUST still be tracked.
    now += 0.1
    lean_t = now
    busy["until"] = lean_t + 1.2
    hook(transport, queue, now, {"pitch": 12.0, "yaw": 0.0})
    assert hook._move_target == {"pitch": 12.0, "yaw": 0.0}, "tracking froze in the window"
    assert hook._move_t0 == lean_t

    # Later in the window: the settle returns toward baseline, then idle resumes
    # with a yaw look — each tracked as it happens.
    now += 1.3
    busy["until"] = now + 1.5
    hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
    now = max(now + 1.6, window_end + 0.05)
    look_cmd = {"pitch": 0.0, "yaw": 15.0}
    busy["until"] = now + 1.7
    transport.pose = (0.0, 0.0)  # dispatch tick: still at the start pose
    hook(transport, queue, now, look_cmd)

    # Phase 3 (window over): the head TRACKS the resume look's expected profile.
    # With fresh tracking, deviation reads ~0 and no phantom fires.
    for _ in range(12):
        now += 0.2
        transport.pose = hook._expected_head(now, look_cmd)
        hook(transport, queue, now, look_cmd)
    assert hook.events == 1, "a tracked resume move must not re-seed a phantom reaction"


def test_large_dispatch_resets_press_accumulation() -> None:
    """Press edges must not pair across a large dispatch — boundary artifacts can't fire.

    Live autopsies showed phantom fires landing exactly on dispatch-observation
    ticks: a press edge counted just before a reaction/expression move dispatched
    paired with a boundary-artifact edge right after it. Observing a large dispatch
    now resets the detector's press accumulation, so an artifact edge starts from
    zero and can never reach ``min_presses`` on its own — while a real hand simply
    re-earns its edges within the next quiet second.
    """
    busy = {"until": 0.0}
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, busy_horizon=lambda: busy["until"])
    transport = _ScriptedPoseTransport()

    # One genuine press edge accumulates on a settled head.
    now = 0.0
    transport.pose = (0.0, 0.0)
    hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
    now += 0.4
    transport.pose = (-20.0, 0.0)
    hook(transport, queue, now, {"pitch": 0.0, "yaw": 0.0})
    assert len(detector.press_times) == 1

    # A large move dispatches (an expression / reaction / look) — accumulation resets.
    now += 0.4
    busy["until"] = now + 1.7
    transport.pose = (0.0, 0.0)
    hook(transport, queue, now, {"pitch": 3.0, "yaw": 0.0})
    assert len(detector.press_times) == 0, "a large dispatch must reset press accumulation"
    assert hook.events == 0

    # Sub-threshold breathe dispatches must NOT reset a fresh accumulation.
    now += 2.0  # the move has landed
    transport.pose = (-20.0, 0.0)
    hook(transport, queue, now, {"pitch": 3.0, "yaw": 0.0})
    assert len(detector.press_times) == 1
    now += 0.4
    transport.pose = (3.0, 0.0)  # released, tracking the commanded pose
    hook(transport, queue, now, {"pitch": 3.5, "yaw": 0.0})  # 0.5 deg breathe dispatch
    now += 0.4
    transport.pose = (-20.0, 0.0)
    hook(transport, queue, now, {"pitch": 3.5, "yaw": 0.0})
    assert hook.events == 1, "a real press across small breathe dispatches must still fire"


# ---------------------------------------------------------------------------
# 4. server.run with on_tick=None is byte-identical to before
# ---------------------------------------------------------------------------


class _Clock:
    def __init__(self, dt=0.05):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


class _RecTransport:
    name = "rec"

    def __init__(self):
        self.gotos: list[float] = []

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(duration)
        return {"uuid": "x"}


class _AlwaysLook:
    def update(self, t, sense, **_kwargs):
        from reachy.motion.queue import LOOK_KEY, MotionAction

        return MotionAction(label="look", head={"yaw": 20.0}, duration=1.0, coalesce_key=LOOK_KEY)


def test_run_on_tick_none_is_unchanged() -> None:
    """run(on_tick=None) drives exactly as before (regression for the new seam)."""
    tr_a = _RecTransport()
    ticks_a = run(
        tr_a,
        _AlwaysLook(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=60,
    )
    tr_b = _RecTransport()
    ticks_b = run(
        tr_b,
        _AlwaysLook(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=60,
        hooks=LoopHooks(on_tick=None),
    )
    assert ticks_a == ticks_b == 60
    assert tr_a.gotos == tr_b.gotos  # identical move stream with and without the kwarg


def test_run_on_tick_invoked_before_producer_each_tick() -> None:
    """on_tick fires once per tick, before the producer is consulted, with 4 args."""
    tr = _RecTransport()
    seen: list[tuple[object, object, float, dict]] = []

    class _NullProducer:
        def update(self, *_a, **_k):
            return None

    def _hook(transport, queue, t, commanded_head):
        seen.append((transport, queue, t, commanded_head))

    ticks = run(
        tr,
        _NullProducer(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        max_ticks=5,
        hooks=LoopHooks(on_tick=_hook),
    )
    assert ticks == 5
    assert len(seen) == 5
    for transport_seen, queue_seen, t, commanded_head in seen:
        assert transport_seen is tr
        assert isinstance(queue_seen, MotionQueue)
        assert t > 0.0
        # No move dispatched (null producer) → commanded head stays neutral.
        assert commanded_head == {"pitch": 0.0, "yaw": 0.0}


def test_on_tick_receives_last_dispatched_commanded_head() -> None:
    """The commanded_head handed to on_tick tracks the loop's last dispatched move.

    Before the first move it is neutral; after the loop dispatches a move with a
    non-zero head pose, on_tick sees that exact pitch/yaw — the baseline a folded
    pat detector measures deviation against.
    """
    tr = _RecTransport()
    commanded_seen: list[dict] = []

    class _LookThenIdle:
        """Emit one (pitch+yaw) move on the first tick, then nothing."""

        def __init__(self):
            self.done = False

        def update(self, t, sense, **_kwargs):
            if self.done:
                return None
            self.done = True
            return MotionAction(label="orient", head={"pitch": 4.0, "yaw": 12.0}, duration=1.0)

    def _hook(transport, queue, t, commanded_head):
        commanded_seen.append(dict(commanded_head))

    run(
        tr,
        _LookThenIdle(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=6,
        hooks=LoopHooks(on_tick=_hook),
    )
    # First tick: before the move is dispatched → neutral.
    assert commanded_seen[0] == {"pitch": 0.0, "yaw": 0.0}
    # Once the move has been accepted, on_tick sees the commanded head pose.
    assert commanded_seen[-1] == {"pitch": 4.0, "yaw": 12.0}


def test_blocking_goto_still_publishes_an_honest_busy_horizon() -> None:
    """A move_goto that BLOCKS for the move's duration must not stale the horizon.

    The live SDK media session's move_goto blocks until the move completes. Basing
    busy_until on the pre-call clock then published a horizon that was already
    ~expired at the next tick — every reader saw a "0.14 s move" for a 2.2 s goto
    (the phantom-pat horizon bug). The horizon must be the LATER of the plan-based
    end and the post-call clock, plus settle.
    """
    clock = _Clock(0.05)
    busy: dict[str, float] = {"until": 0.0}
    horizons: list[float] = []

    class _BlockingTransport:
        name = "sdk"

        def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
            clock.t += duration  # the SDK call blocks for the whole move
            return {"uuid": "x"}

    class _OneMove:
        def __init__(self):
            self.done = False

        def update(self, t, sense, **_k):
            if self.done:
                return None
            self.done = True
            return MotionAction(label="look", head={"pitch": 0.0, "yaw": -20.0}, duration=2.0)

    def _hook(transport, queue, t, commanded_head):
        horizons.append(busy["until"] - t)

    run(
        _BlockingTransport(),
        _OneMove(),
        now=clock,
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=6,
        hooks=LoopHooks(on_tick=_hook),
        busy=busy,
    )
    # The tick right after the (blocking) dispatch must still see a future horizon
    # of about settle (0.2 s) — never an already-expired one from the stale clock.
    post_dispatch = [h for h in horizons if h > 0.0]
    assert post_dispatch, f"no future horizon ever published; horizons={horizons}"
    assert max(post_dispatch) >= 0.1, f"published horizon still stale: {horizons}"


def test_headless_actions_do_not_flap_commanded_head() -> None:
    """Antenna-only actions leave the commanded head where the last head move put it.

    THE phantom-pat root cause: head-less actions (Tier-1 antenna leans, dispatched
    near-continuously) used to stamp commanded_head back to (0, 0), flapping the
    commanded state target<->zero on every antenna dispatch. Expectation-based pat
    sensing read each flap as a huge instant move and false-fired on the transit
    that "move" implied. A held head must stay commanded where it was.
    """
    tr = _RecTransport()
    commanded_seen: list[dict] = []

    class _LookThenAntennas:
        """One head move, then a stream of antenna-only leans (head=None)."""

        def __init__(self):
            self.n = 0

        def update(self, t, sense, **_kwargs):
            self.n += 1
            if self.n == 1:
                return MotionAction(label="look", head={"pitch": 0.0, "yaw": -20.0}, duration=0.1)
            if self.n <= 6:
                return MotionAction(label="antenna lean", antennas=(10.0, -5.0), duration=0.05)
            return None

    def _hook(transport, queue, t, commanded_head):
        commanded_seen.append(dict(commanded_head))

    run(
        tr,
        _LookThenAntennas(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.0,
        max_ticks=40,
        hooks=LoopHooks(on_tick=_hook),
    )
    # After the look is dispatched, EVERY later tick — through all the antenna-only
    # dispatches — must still report the held head pose, never a (0, 0) flap.
    after_look = [c for c in commanded_seen if c != {"pitch": 0.0, "yaw": 0.0}]
    assert after_look, "the look must be observed at all"
    assert all(c == {"pitch": 0.0, "yaw": -20.0} for c in after_look)
    assert commanded_seen[-1] == {
        "pitch": 0.0,
        "yaw": -20.0,
    }, "antenna-only dispatches flapped the commanded head back to neutral"


# ---------------------------------------------------------------------------
# End-to-end through the CLI: listen run --json with a fake sdk transport
# ---------------------------------------------------------------------------


def _run_listen_cli(monkeypatch, transport, *, max_ticks, extra_args=None):
    """Run ``reachy listen run --json`` against *transport*; return (rc, actions)."""
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    # The cold-start warmup is a live-deployment concern (EMA sag learning over
    # real seconds); these bounded fast-spin runs exercise detection mechanics.
    monkeypatch.setattr("reachy.cli._commands.listen.WARMUP_SECONDS", 0.0)

    argv = [
        "listen",
        "run",
        "--json",
        "--transport",
        "sdk",
        "--deadband",
        "0",
        "--max-ticks",
        str(max_ticks),
    ]
    if extra_args:
        argv.extend(extra_args)

    import io
    import sys

    buf = io.StringIO()
    old = sys.stdout
    sys.stdout = buf
    try:
        rc = main(argv)
    finally:
        sys.stdout = old

    actions = []
    for ln in buf.getvalue().splitlines():
        if not ln.strip():
            continue
        with contextlib.suppress(json.JSONDecodeError):
            obj = json.loads(ln)
            if "action" in obj:
                actions.append(obj)
    return rc, actions


# --- 1 (e2e). a deviating head pose leans + writes the flag through the CLI ---


def test_listen_cli_pat_leans_and_writes_flag(monkeypatch) -> None:
    """A press during ``listen run`` enqueues a lean goto AND writes the pat flag.

    Use a quiet, front-facing session so the only motion comes from the pat path,
    then assert a pat-labelled lean reached the transport. ``time.sleep`` is a
    no-op so the loop ticks fast: with the real default detector (min_presses=2,
    pat_window=3.0) all the alternating presses land inside one window and fire a
    scratch — ``pat_cooldown`` is cleared trivially on the first pat (last_pat_time
    starts at 0). ``--min-presses 2`` is explicit for clarity.
    """
    transport = _PatSdkTransport(_QuietFrontSession())

    # --idle-energy 0 keeps the idle wander out of the queue so the only motion
    # competing for the serial executor is the pat lean — it dispatches on the tick
    # it is enqueued (the executor starts idle), letting a bounded fast-spin run
    # observe the dispatched lean without needing wall-clock time to pass.
    rc, actions = _run_listen_cli(
        monkeypatch,
        transport,
        max_ticks=40,
        extra_args=["--min-presses", "2", "--idle-energy", "0"],
    )
    assert rc == 0

    # The pat reaction reached the executor: a pat-labelled lean was dispatched
    # (the _on_action callback emits the action label per dispatched move).
    labels = [a.get("action", "") for a in actions]
    assert any("pat_" in label for label in labels), f"expected a pat lean action; labels={labels}"
    assert any("lean" in label for label in labels), f"expected a lean action; labels={labels}"


# --- 2 (e2e). baseline head pose → no pat, but sound-orient still drives motion ---


def test_listen_cli_baseline_no_pat_but_sound_orients(monkeypatch) -> None:
    """Baseline head_pose ≈ (0,0) fires no pat, yet listen's sound-orient still turns.

    The session has off-axis speech (Tier-2 turn) while head_pose is held flat at
    (0, 0). Assert: at least one sound-orient yaw turn fires (sound still works)
    and NO pat lean reaches the transport (a scratch lean is a pitch-down move,
    a side_pat carries a body_yaw — neither should appear).
    """
    transport = _PatSdkTransport(_SpeechOffAxisSession(), baseline=True)

    rc, actions = _run_listen_cli(
        monkeypatch,
        transport,
        max_ticks=40,
        extra_args=["--speed", "1000", "--hold", "0"],
    )
    assert rc == 0

    # Sound-orient still drives the head: at least one yaw turn was emitted.
    yaw_actions = [a for a in actions if a.get("yaw") is not None]
    assert yaw_actions, f"expected a sound-orient turn; actions={actions}"

    # No pat lean fired: no pat-labelled action was dispatched.
    labels = [a.get("action", "") for a in actions]
    assert not any("pat_" in label for label in labels), f"baseline must not lean; labels={labels}"


def test_listen_cli_baseline_no_pat_flag(monkeypatch) -> None:
    """Baseline pose during ``listen run`` never raises the pat-active flag."""
    transport = _PatSdkTransport(_QuietFrontSession(), baseline=True)
    rc, _ = _run_listen_cli(monkeypatch, transport, max_ticks=30)
    assert rc == 0
    assert ps.is_active() is False


# --- 5. the http loop installs no pat hook ---


def test_http_loop_installs_no_pat_hook(monkeypatch) -> None:
    """The http transport (no head_pose) drives listen with no pat hook, no crash."""
    transport = _HttpTransport()
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    # The http path never reads head_pose; if a hook were wrongly installed it would
    # AttributeError. A clean exit-0 with no pat flag proves no hook ran.
    rc = main(
        ["listen", "run", "--json", "--transport", "http", "--deadband", "0", "--max-ticks", "10"]
    )
    assert rc == 0
    assert ps.is_active() is False


def test_build_pat_hook_none_for_transport_without_head_pose() -> None:
    """_build_pat_hook returns None when the transport cannot read head_pose."""
    import argparse

    from reachy.cli._commands.listen import _build_pat_hook

    args = argparse.Namespace(pat=True)
    queue: MotionQueue = MotionQueue()
    assert _build_pat_hook(args, _HttpTransport(), queue) is None


# --- 6. --no-pat disables the pat hook ---


def test_no_pat_disables_pat_hook_build() -> None:
    """--no-pat (args.pat False) yields no PatHook even on an sdk transport."""
    import argparse

    from reachy.cli._commands.listen import _build_pat_hook

    args = argparse.Namespace(pat=False)
    queue: MotionQueue = MotionQueue()
    transport = _PatSdkTransport(_QuietFrontSession())
    assert _build_pat_hook(args, transport, queue) is None


def test_no_pat_cli_does_not_lean(monkeypatch) -> None:
    """``listen run --no-pat`` ignores a deviating head pose — no pat lean, no flag."""
    transport = _PatSdkTransport(_QuietFrontSession())

    rc, actions = _run_listen_cli(
        monkeypatch, transport, max_ticks=40, extra_args=["--no-pat", "--min-presses", "2"]
    )
    assert rc == 0

    # --no-pat installs no hook: a deviating head pose never produces a pat lean.
    labels = [a.get("action", "") for a in actions]
    assert not any("pat_" in label for label in labels), f"--no-pat must not lean; labels={labels}"
    # head_pose is never even read when the hook is absent.
    assert transport.pose_calls == 0, "--no-pat must not read head_pose at all"
    assert ps.is_active() is False


# ---------------------------------------------------------------------------
# (t3) PatHook feeds the pat cue to cognition — one per reaction cycle
#
# EventBuffer.feed_pat(kind, level) already exists (reachy/speech/events.py); this
# gives PatHook an optional duck-typed ``buffer`` seam so every detection ALSO
# feeds a cue to cognition, fault-isolated so a raising buffer can never break the
# reflex, and naturally capped to one cue per reaction cycle by the same window
# suppression that already caps detections.
# ---------------------------------------------------------------------------


class _FakeBuffer:
    """A minimal duck-typed cognition buffer recording ``feed_pat`` calls."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def feed_pat(self, kind: str, level: str) -> None:
        self.calls.append((kind, level))


class _RaisingBuffer:
    """A buffer whose ``feed_pat`` always raises — must never break the reflex."""

    def feed_pat(self, kind: str, level: str) -> None:
        raise RuntimeError("boom")


def test_pathook_feeds_buffer_once_on_detection() -> None:
    """A detection feeds exactly one cue (correct kind + level); the reflex still fires."""
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    buffer = _FakeBuffer()
    hook = PatHook(queue, detector=detector, buffer=buffer)
    transport = _ConstantPressTransport()

    for i in range(8):
        t = 0.4 * i
        hook(transport, queue, t)
        if hook.events >= 1:
            break

    assert hook.events == 1
    # _ConstantPressTransport presses pitch only → touch_type "scratch"; first
    # detection is always "level1".
    assert buffer.calls == [("scratch", "level1")], buffer.calls
    labels = [a.label for a in queue.pending()]
    assert any("lean" in label for label in labels), labels
    assert any(label.startswith("pat_") for label in labels), labels
    assert ps.is_active() is True


def test_pathook_without_buffer_behaves_unchanged() -> None:
    """No ``buffer`` injected (the default): no cue path, reflex identical to today."""
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)  # buffer defaults to None
    transport = _ConstantPressTransport()

    for i in range(8):
        t = 0.4 * i
        hook(transport, queue, t)  # must not raise / AttributeError with no buffer
        if hook.events >= 1:
            break

    assert hook.events == 1
    labels = [a.label for a in queue.pending()]
    assert any("lean" in label for label in labels), labels
    assert any(label.startswith("pat_") for label in labels), labels
    assert ps.is_active() is True


def test_pathook_buffer_raise_does_not_break_reflex() -> None:
    """A buffer whose ``feed_pat`` raises must not prevent the reflex or the window."""
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector, buffer=_RaisingBuffer())
    transport = _ConstantPressTransport()

    for i in range(8):
        t = 0.4 * i
        hook(transport, queue, t)  # the RuntimeError must never escape this call
        if hook.events >= 1:
            break

    assert hook.events == 1, "a raising buffer must not prevent the detection/reflex"
    labels = [a.label for a in queue.pending()]
    assert any("lean" in label for label in labels), labels
    assert any(label.startswith("pat_") for label in labels), labels
    assert ps.is_active() is True, "the reaction window/flag must still open despite the raise"


def test_pathook_feeds_at_most_one_cue_per_reaction_cycle() -> None:
    """A continuous stroke yields at most one cue per reaction cycle — never more.

    Drive :class:`PatHook` through several fire → window → resume cycles with a
    fake buffer injected. The reaction-window suppression that already limits
    detections to one per cycle must carry through to the cue feed: the number of
    ``feed_pat`` calls never exceeds ``hook.events`` at any point during the run,
    and after several cycles the two counts are exactly equal (one cue per cycle,
    never more) — proven across N reaction cycles, not just one.
    """
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    buffer = _FakeBuffer()
    hook = PatHook(queue, detector=detector, buffer=buffer)
    transport = _ConstantPressTransport()

    target_cycles = 3
    t = 0.0
    max_t = 60.0  # generous ceiling — several reaction windows (~3.5s each) fit easily
    while hook.events < target_cycles and t < max_t:
        hook(transport, queue, t)
        # Never more cues than detections, at every single tick along the way.
        assert len(buffer.calls) <= hook.events, (buffer.calls, hook.events)
        t += 0.1

    assert hook.events >= target_cycles, "expected multiple reaction cycles to fire"
    assert len(buffer.calls) == hook.events, "exactly one cue per reaction cycle, never more"
    assert all(
        kind in {"scratch", "side_pat"} and level in {"level1", "level2"}
        for kind, level in buffer.calls
    ), buffer.calls
