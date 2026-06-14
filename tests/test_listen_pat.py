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


# ---------------------------------------------------------------------------
# End-to-end through the CLI: listen run --json with a fake sdk transport
# ---------------------------------------------------------------------------


def _run_listen_cli(monkeypatch, transport, *, max_ticks, extra_args=None):
    """Run ``reachy listen run --json`` against *transport*; return (rc, actions)."""
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: transport)
    monkeypatch.setattr("time.sleep", lambda *_: None)

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
