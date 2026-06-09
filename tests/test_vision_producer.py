"""Tests for :class:`reachy.vision.producer.VisionProducer`.

Pure / offline: the producer is fed synthetic camera frames through a fake
transport that also records every ``move_goto`` it is handed, and is driven with
an injected clock + a bounded ``max_ticks`` — so no robot, camera, daemon, or
wall-clock is involved. The detectors are the real pure-numpy ones.
"""

from __future__ import annotations

import time

import numpy as np

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.motion.queue import LOOK_KEY, MotionQueue
from reachy.vision.light import LightDetector
from reachy.vision.motion import MotionDetector
from reachy.vision.producer import (
    VisionEvent,
    VisionParams,
    VisionProducer,
    _choose_event,
)

H, W = 48, 64  # small synthetic frame (downsampled further by the detectors)


# --------------------------------------------------------------------------- #
# frame helpers + fakes                                                        #
# --------------------------------------------------------------------------- #


def _blank() -> np.ndarray:
    """A dim, uniform grey frame (no motion, no distinct bright region)."""
    return np.full((H, W, 3), 40, dtype=np.uint8)


def _blob_frame(center_col: int, *, radius: int = 8, value: int = 255) -> np.ndarray:
    """A frame with one bright square blob centred on ``center_col``."""
    frame = _blank()
    half = radius
    c0 = max(0, center_col - half)
    c1 = min(W, center_col + half)
    r0 = H // 2 - half
    r1 = H // 2 + half
    frame[r0:r1, c0:c1, :] = value
    return frame


def _wave_frames(n: int = 8) -> list[np.ndarray]:
    """A bright blob sweeping left→right across ``n`` frames (a 'hand wave')."""
    cols = np.linspace(W * 0.15, W * 0.85, n).astype(int)
    return [_blob_frame(int(c)) for c in cols]


class _FakeTransport:
    """Yields a scripted list of frames and records every ``move_goto`` call."""

    name = "fake"

    def __init__(self, frames: list[np.ndarray], *, loop_last: bool = True):
        self._frames = list(frames)
        self._i = 0
        self._loop_last = loop_last
        self.gotos: list[dict] = []

    def get_frame(self) -> np.ndarray:
        if self._i < len(self._frames):
            frame = self._frames[self._i]
            self._i += 1
            return frame
        if self._loop_last and self._frames:
            return self._frames[-1]  # hold on the last frame (static)
        raise IndexError("no more frames")

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(
            {
                "head": head,
                "antennas": antennas,
                "body_yaw": body_yaw,
                "duration": duration,
                "interpolation": interpolation,
            }
        )
        return {"status": "ok"}


class _BoomTransport:
    """A transport whose camera read always raises a non-CliError exception."""

    name = "boom"

    def get_frame(self):
        raise RuntimeError("camera exploded")

    def move_goto(self, **_kwargs):  # pragma: no cover - never reached in the test
        return {}


class _Clock:
    """A deterministic monotonic clock that advances ``dt`` each call."""

    def __init__(self, dt: float = 0.1):
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


# --------------------------------------------------------------------------- #
# event selection                                                             #
# --------------------------------------------------------------------------- #


def test_choose_event_prefers_motion_over_light() -> None:
    from reachy.vision.light import LightResult
    from reachy.vision.motion import MotionResult

    motion = MotionResult(direction=0.5, magnitude=0.2)
    light = LightResult(direction=-0.9, mean_luma=200.0, changed=True)
    event = _choose_event(motion, light, motion_threshold=0.0)
    assert event is not None and event.source == "motion" and event.direction == 0.5


def test_choose_event_falls_back_to_light_change() -> None:
    from reachy.vision.light import LightResult

    light = LightResult(direction=0.7, mean_luma=200.0, changed=True)
    event = _choose_event(None, light, motion_threshold=0.0)
    assert event is not None and event.source == "light" and event.direction == 0.7


def test_choose_event_none_when_nothing_fires() -> None:
    from reachy.vision.light import LightResult

    light = LightResult(direction=0.7, mean_luma=200.0, changed=False)
    assert _choose_event(None, light, motion_threshold=0.0) is None


def test_choose_event_ignores_light_change_without_direction() -> None:
    from reachy.vision.light import LightResult

    light = LightResult(direction=None, mean_luma=200.0, changed=True)
    assert _choose_event(None, light, motion_threshold=0.0) is None


def test_choose_event_motion_below_threshold_ignored() -> None:
    from reachy.vision.light import LightResult
    from reachy.vision.motion import MotionResult

    motion = MotionResult(direction=0.5, magnitude=0.001)
    light = LightResult(direction=None, mean_luma=10.0, changed=False)
    assert _choose_event(motion, light, motion_threshold=0.05) is None


# --------------------------------------------------------------------------- #
# yaw mapping (sign convention)                                               #
# --------------------------------------------------------------------------- #


def test_target_yaw_right_blob_turns_head_right() -> None:
    # Camera +1 (right of frame) => negative head yaw (right turn), matching the CLI.
    prod = VisionProducer(transport=_FakeTransport([]), params=VisionParams(gain=1.0, max_yaw=35.0))
    assert prod._target_yaw(1.0) == -35.0
    assert prod._target_yaw(-1.0) == 35.0
    assert prod._target_yaw(0.0) == 0.0


def test_target_yaw_clamped_to_max() -> None:
    prod = VisionProducer(transport=_FakeTransport([]), params=VisionParams(gain=4.0, max_yaw=20.0))
    assert prod._target_yaw(1.0) == -20.0  # clamped, not -80
    assert prod._target_yaw(-1.0) == 20.0


# --------------------------------------------------------------------------- #
# the hand-wave: a move toward the correct side is enqueued                    #
# --------------------------------------------------------------------------- #


def test_hand_wave_enqueues_look_toward_the_blob() -> None:
    # A bright blob sweeping left->right. By the time the blob is on the RIGHT, the
    # producer should turn the head RIGHT (negative yaw).
    frames = _wave_frames(n=10)
    tr = _FakeTransport(frames)
    prod = VisionProducer(
        transport=tr,
        params=VisionParams(deadband=3.0, hold=0.0, max_yaw=35.0, gain=1.0),
    )
    prod.run(now=_Clock(0.1), sleep=lambda *_: None, tick=0.1, max_ticks=len(frames) + 4)

    assert tr.gotos, "the hand wave should have produced at least one head-orient goto"
    yaws = [g["head"]["yaw"] for g in tr.gotos if g["head"] is not None]
    assert yaws, "every look move must carry a head yaw"
    # The blob ends on the right of the frame -> the final committed look turns right (yaw < 0).
    assert yaws[-1] < 0.0, f"expected a rightward (negative) yaw, got {yaws}"
    # And every issued move is a smooth minjerk look on the LOOK_KEY contract.
    for g in tr.gotos:
        assert g["interpolation"] == "minjerk"


def test_blob_on_left_turns_head_left() -> None:
    # A blob that appears, then moves to the far LEFT, should turn the head LEFT (yaw > 0).
    frames = [_blank(), _blob_frame(W // 2), _blob_frame(int(W * 0.12))]
    tr = _FakeTransport(frames)
    prod = VisionProducer(
        transport=tr, params=VisionParams(deadband=3.0, hold=0.0, max_yaw=35.0, gain=1.0)
    )
    prod.run(now=_Clock(0.1), sleep=lambda *_: None, tick=0.1, max_ticks=len(frames) + 4)

    yaws = [g["head"]["yaw"] for g in tr.gotos if g["head"] is not None]
    assert yaws, "expected at least one look toward the left blob"
    assert yaws[-1] > 0.0, f"expected a leftward (positive) yaw, got {yaws}"


# --------------------------------------------------------------------------- #
# static frames + deadband => holds (no move)                                 #
# --------------------------------------------------------------------------- #


def test_static_frames_no_move() -> None:
    # Identical, uniform frames: no motion (after the first), no light change -> hold.
    tr = _FakeTransport([_blank() for _ in range(12)])
    prod = VisionProducer(transport=tr, params=VisionParams(deadband=8.0, hold=0.0))
    ticks = prod.run(now=_Clock(0.1), sleep=lambda *_: None, tick=0.1, max_ticks=12)
    assert ticks == 12
    assert tr.gotos == [], "static frames must enqueue no move (head holds)"


def test_centered_blob_within_deadband_holds() -> None:
    # A blob that appears dead-centre: its target yaw (~0) is inside the deadband -> hold.
    frames = [_blank(), _blob_frame(W // 2)]
    tr = _FakeTransport(frames, loop_last=True)
    prod = VisionProducer(transport=tr, params=VisionParams(deadband=20.0, hold=0.0, max_yaw=35.0))
    prod.run(now=_Clock(0.1), sleep=lambda *_: None, tick=0.1, max_ticks=6)
    # The centred blob maps to ~0 yaw, well inside a 20deg deadband -> no move.
    assert tr.gotos == [], "a centred target inside the deadband must not move the head"


# --------------------------------------------------------------------------- #
# serial execution: at most one move per tick, never overlapping              #
# --------------------------------------------------------------------------- #


def test_run_is_bounded_and_serial() -> None:
    # An ever-changing scene wants to move constantly; serialization + busy_until must
    # keep moves to at most one per tick and far fewer than the tick count (no overlap).
    frames = [_blob_frame(int(c)) for c in np.random.randint(8, W - 8, size=40)]
    tr = _FakeTransport(frames)
    prod = VisionProducer(
        transport=tr,
        # long moves + settle, short ticks -> the loop is busy most of the time.
        params=VisionParams(deadband=1.0, hold=0.0, speed=5.0, min_dur=1.0, max_dur=2.0),
    )
    ticks = prod.run(now=_Clock(0.1), sleep=lambda *_: None, tick=0.1, max_ticks=40)
    assert ticks == 40
    # 40 ticks * 0.1s = 4.0s of sim time; each move >= 1.0s + 0.2s settle -> only a few moves.
    assert 1 <= len(tr.gotos) <= 6, f"expected a few serialized moves, got {len(tr.gotos)}"


def test_tick_dispatches_at_most_one_move_each() -> None:
    # Drive single ticks and assert the queue never dispatches two moves in one tick.
    frames = [_blob_frame(int(c)) for c in (10, 54, 10, 54, 10, 54)]
    tr = _FakeTransport(frames)
    prod = VisionProducer(
        transport=tr, params=VisionParams(deadband=1.0, hold=0.0, min_dur=1.0, speed=5.0)
    )
    clock = _Clock(0.1)
    for _ in range(6):
        before = len(tr.gotos)
        prod.tick(clock())
        assert len(tr.gotos) - before <= 1, "no tick may dispatch more than one move"


def test_busy_until_blocks_a_second_concurrent_move() -> None:
    # While a move is in flight (busy_until in the future) no new move may start.
    # Frame 0 only primes the MotionDetector (first feed returns None); frames 1+ move it.
    frames = [_blank(), _blob_frame(10), _blob_frame(54), _blob_frame(10)]
    tr = _FakeTransport(frames)
    prod = VisionProducer(
        transport=tr, params=VisionParams(deadband=1.0, hold=0.0, min_dur=2.0, speed=2.0)
    )
    prod.tick(0.1)  # frame 0: primes detectors, no move
    prod.tick(0.2)  # frame 1: decides + dispatches one move; busy_until ~ 0.2 + dur + settle
    assert len(tr.gotos) == 1
    prod.tick(0.3)  # still busy -> the new decision queues but does NOT dispatch
    assert len(tr.gotos) == 1, "a move in flight must block a second concurrent dispatch"


# --------------------------------------------------------------------------- #
# on_action callback + injectable queue                                       #
# --------------------------------------------------------------------------- #


def test_on_action_callback_fires_per_dispatched_move() -> None:
    frames = _wave_frames(n=10)
    tr = _FakeTransport(frames)
    prod = VisionProducer(transport=tr, params=VisionParams(deadband=3.0, hold=0.0))
    seen: list[str] = []
    prod.run(
        now=_Clock(0.1),
        sleep=lambda *_: None,
        tick=0.1,
        max_ticks=len(frames) + 4,
        on_action=lambda a: seen.append(a.label),
    )
    assert len(seen) == len(tr.gotos)
    assert all(label.startswith("look") for label in seen)


class _SpyQueue(MotionQueue):
    """A MotionQueue that records the coalesce_key of every submitted action."""

    def __init__(self) -> None:
        super().__init__()
        self.submitted: list[str | None] = []

    def submit(self, action) -> None:
        self.submitted.append(action.coalesce_key)
        super().submit(action)


def test_injected_queue_is_used() -> None:
    q = _SpyQueue()
    tr = _FakeTransport(_wave_frames(n=8))
    prod = VisionProducer(transport=tr, queue=q, params=VisionParams(deadband=3.0, hold=0.0))
    # tick() routes decisions through *our* injected queue.
    prod.tick(0.1)  # first frame primes detectors (no submit)
    prod.tick(0.2)  # second frame -> a look submitted onto our queue
    assert LOOK_KEY in q.submitted


# --------------------------------------------------------------------------- #
# hold window suppresses re-commits                                           #
# --------------------------------------------------------------------------- #


def test_hold_window_suppresses_recommit() -> None:
    # After committing a turn, the hold window must suppress an immediate re-target.
    prod = VisionProducer(
        transport=_FakeTransport([]),
        params=VisionParams(deadband=1.0, hold=5.0, min_dur=0.5, speed=30.0),
    )
    a0 = prod._look_action(20.0, t=0.0)  # commit; hold_until ~ 0 + dur + 5.0
    assert a0.head["yaw"] == 20.0
    # A fresh strong event during the hold window must be suppressed.
    assert prod.decide(_blob_frame(60), t=1.0) is None


# --------------------------------------------------------------------------- #
# error contract: frame-read failure -> CliError (no traceback)               #
# --------------------------------------------------------------------------- #


def test_frame_read_failure_raises_clierror() -> None:
    prod = VisionProducer(transport=_BoomTransport())
    try:
        prod.tick(0.1)
    except CliError as err:
        assert err.code == EXIT_ENV_ERROR
        assert "frame" in err.message.lower()
    else:  # pragma: no cover
        raise AssertionError("expected a CliError from a failing camera read")


def test_cli_error_from_transport_propagates_unchanged() -> None:
    sentinel = CliError(code=EXIT_ENV_ERROR, message="no local camera", remediation="connect it")

    class _NoCamera:
        name = "nocam"

        def get_frame(self):
            raise sentinel

        def move_goto(self, **_kwargs):  # pragma: no cover
            return {}

    prod = VisionProducer(transport=_NoCamera())
    try:
        prod.tick(0.1)
    except CliError as err:
        assert err is sentinel  # the transport's own CliError, not re-wrapped
    else:  # pragma: no cover
        raise AssertionError("expected the transport's CliError to propagate")


# --------------------------------------------------------------------------- #
# cheap-per-tick budget (not flaky — just a generous ceiling on pure numpy)   #
# --------------------------------------------------------------------------- #


def test_per_tick_is_cheap_pure_numpy() -> None:
    # A generous budget: a single tick over a small frame is pure-numpy + a few floats
    # and must stay well under a 10 FPS (100ms) frame budget. Loose enough to never flake.
    tr = _FakeTransport(_wave_frames(n=4))
    prod = VisionProducer(transport=tr, params=VisionParams(hold=0.0))
    # Warm up the detectors (first feed allocates the previous-frame buffer).
    prod.tick(0.1)
    start = time.perf_counter()
    for i in range(2, 6):
        prod.tick(i * 0.1)
    elapsed = time.perf_counter() - start
    assert elapsed < 0.5, f"4 ticks took {elapsed:.3f}s — far above a 10 FPS budget"


def test_default_detectors_are_real_pure_numpy_instances() -> None:
    prod = VisionProducer(transport=_FakeTransport([]))
    assert isinstance(prod.motion_detector, MotionDetector)
    assert isinstance(prod.light_detector, LightDetector)


def test_vision_event_dataclass_shape() -> None:
    ev = VisionEvent(direction=0.3, strength=0.1, source="motion")
    assert ev.direction == 0.3 and ev.source == "motion"
