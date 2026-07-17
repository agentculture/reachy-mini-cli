"""Tests for folding vision motion/light detection into the ``listen`` sdk loop.

``listen`` owns the single-consumer SDK client; a *separate* ``vision`` process
would contend for that one media/camera subsystem and get throttled. So vision's
per-tick frame→look decision is folded into ``listen``'s loop the same way pat is
(``reachy/motion/listen_pat.py``): :class:`~reachy.motion.listen_vision.VisionHook`
is a per-tick ``on_tick`` hook ``(transport, queue, t, commanded_head) -> None``.

The crux is **non-blocking frame access**: vision's live SDK ``get_frame()`` path
has hung before (issue #28). The hook must therefore read frames off a background
grabber thread and never call ``get_frame()`` synchronously on the tick thread —
so a stalling camera can never block the loop. These tests inject a fake frame
source + a fake detector and assert:

1. A detection (detector returns a :class:`MotionAction`) → that move is enqueued
   onto the shared queue.
2. No frame available → no-op (nothing enqueued, no raise).
3. A frame source that *stalls* (blocks forever in ``get_frame``) does NOT block
   the per-tick ``__call__`` — the tick returns promptly.
4. The hook mirrors :class:`~reachy.motion.listen_pat.PatHook`: same ``on_tick``
   signature, silent degradation on a raising frame source / detector, and a
   guarded :meth:`close`.
5. (issue #32) A motion/light decision also feeds an injected cognition
   ``buffer``'s ``feed_vision(motion_direction, brightness_delta)`` — coalesced
   into at most one cue per episode, and fault-isolated (a raising buffer never
   breaks the enqueued reflex). A missing ``buffer``, or a decider with no
   ``last_event``, stays byte-identical to before (no cue, no crash).
6. The real :class:`~reachy.motion.listen_vision._DefaultDecider` (the one the
   live composition actually uses) populates ``last_event`` from its own shadow
   detector pair, fed real synthetic frames.

No robot, no daemon, no network, no real camera; clocks/threads are deterministic
(the grabber is exercised through real threads but every wait is bounded).
"""

from __future__ import annotations

import threading
import time

import numpy as np

from reachy.motion.listen_vision import VisionHook, _DefaultDecider
from reachy.motion.queue import MotionAction, MotionQueue

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeFrameSource:
    """A callable frame source returning a fixed synthetic frame each call."""

    def __init__(self, frame: object | None = None) -> None:
        self._frame = frame if frame is not None else np.zeros((4, 4, 3), dtype=np.uint8)
        self.calls = 0

    def __call__(self) -> object:
        self.calls += 1
        return self._frame


class _RaisingFrameSource:
    """A frame source that always raises (camera drop) — must degrade silently."""

    def __call__(self) -> object:
        raise RuntimeError("camera exploded")


class _StallingFrameSource:
    """A frame source that blocks forever — the hung-camera (#28) case.

    A real :class:`threading.Event` is used as the block so the grabber thread can
    be released in teardown without leaking; the per-tick ``__call__`` must never
    wait on it.
    """

    def __init__(self) -> None:
        self.released = threading.Event()
        self.entered = threading.Event()

    def __call__(self) -> object:
        self.entered.set()
        self.released.wait(timeout=5.0)
        return np.zeros((4, 4, 3), dtype=np.uint8)


class _FakeDetector:
    """A fake decision engine matching the injected-detector seam.

    ``decide(frame, t) -> MotionAction | None``. Records every frame it was fed so
    a test can confirm the grabber's frame reached the detector. Deliberately has
    NO ``last_event`` attribute — mirrors a decider that predates the issue #32 cue
    contract, proving :class:`VisionHook` degrades to "no cue" rather than raising.
    """

    def __init__(self, action: MotionAction | None) -> None:
        self._action = action
        self.frames: list[object] = []

    def decide(self, frame: object, t: float) -> MotionAction | None:  # noqa: ARG002
        self.frames.append(frame)
        return self._action


class _FakeCueDetector:
    """A fake decider that also exposes ``last_event`` (the issue #32 cue contract).

    ``decide(frame, t)`` always returns the constructor-supplied ``action``;
    ``last_event`` is whatever the test sets directly — mirroring how
    :class:`~reachy.motion.listen_vision._DefaultDecider` stamps
    ``(motion_direction, brightness_delta)`` after a real decision.
    """

    def __init__(
        self,
        action: MotionAction | None,
        last_event: tuple[float | None, float] | None = None,
    ) -> None:
        self._action = action
        self.last_event = last_event
        self.frames: list[object] = []

    def decide(self, frame: object, t: float) -> MotionAction | None:  # noqa: ARG002
        self.frames.append(frame)
        return self._action


class _FakeBuffer:
    """Records every ``feed_vision(motion_direction, brightness_delta)`` call."""

    def __init__(self) -> None:
        self.calls: list[tuple[float | None, float]] = []

    def feed_vision(self, motion_direction: float | None, brightness_delta: float) -> None:
        self.calls.append((motion_direction, brightness_delta))


class _RaisingBuffer:
    """A cognition sink whose ``feed_vision`` always raises (must never break the reflex)."""

    def feed_vision(self, motion_direction: float | None, brightness_delta: float) -> None:
        raise RuntimeError("cognition buffer exploded")


_LOOK = MotionAction(label="look +10", head={"yaw": 10.0}, duration=0.5)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    """Poll ``predicate`` until true or ``timeout`` elapses (bounded, no infinite wait)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


# ---------------------------------------------------------------------------
# 1. detection → reaction enqueued
# ---------------------------------------------------------------------------


def test_detection_enqueues_reaction():
    queue = MotionQueue()
    src = _FakeFrameSource()
    detector = _FakeDetector(_LOOK)
    hook = VisionHook(queue=queue, frame_source=src, detector=detector)
    try:
        # Let the grabber publish at least one frame before the tick reads it.
        assert _wait_until(lambda: src.calls >= 1)
        # Drive ticks until the move lands (the first tick may run before the
        # grabber has published — the hook holds rather than blocking on a frame).
        assert _wait_until(lambda: (hook(None, queue, t=float(0)) or True) and len(queue) >= 1)
    finally:
        hook.close()
    pending = queue.pending()
    assert any(a.label == "look +10" for a in pending)
    assert detector.frames, "detector should have been fed the grabbed frame"


# ---------------------------------------------------------------------------
# 2. no frame → no-op
# ---------------------------------------------------------------------------


def test_no_frame_is_noop():
    queue = MotionQueue()
    detector = _FakeDetector(_LOOK)
    # No frame source returns None before any frame is published; with no frame the
    # hook must not feed the detector and must not enqueue anything.
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector)
    try:
        hook(None, queue, t=0.0)
        hook(None, queue, t=0.1)
    finally:
        hook.close()
    assert len(queue) == 0
    assert detector.frames == []


def test_raising_frame_source_degrades_silently():
    queue = MotionQueue()
    detector = _FakeDetector(_LOOK)
    hook = VisionHook(queue=queue, frame_source=_RaisingFrameSource(), detector=detector)
    try:
        # The grabber raising must not surface on the tick thread, and the tick is a
        # clean no-op (no frame ever published).
        hook(None, queue, t=0.0)
    finally:
        hook.close()
    assert len(queue) == 0


def test_raising_detector_degrades_silently():
    queue = MotionQueue()
    src = _FakeFrameSource()

    class _BoomDetector:
        def decide(self, frame, t):  # noqa: ANN001, ARG002
            raise RuntimeError("detector boom")

    hook = VisionHook(queue=queue, frame_source=src, detector=_BoomDetector())
    try:
        assert _wait_until(lambda: src.calls >= 1)
        # A detector that raises must not kill the tick.
        for _ in range(5):
            hook(None, queue, t=0.0)
    finally:
        hook.close()
    assert len(queue) == 0  # nothing enqueued, no traceback


# ---------------------------------------------------------------------------
# 3. a stalling frame source does NOT block the tick (issue #28)
# ---------------------------------------------------------------------------


def test_stalling_frame_source_does_not_block_tick():
    queue = MotionQueue()
    src = _StallingFrameSource()
    detector = _FakeDetector(_LOOK)
    hook = VisionHook(queue=queue, frame_source=src, detector=detector)
    try:
        # The grabber thread is now blocked inside get_frame(). The per-tick call
        # must still return promptly — it reads the (empty) latest-frame holder and
        # never waits on the camera.
        assert _wait_until(src.entered.is_set, timeout=2.0), "grabber never entered get_frame"
        start = time.monotonic()
        hook(None, queue, t=0.0)
        elapsed = time.monotonic() - start
        assert elapsed < 0.5, f"tick blocked on a stalled camera ({elapsed:.2f}s)"
        # No frame was ever published, so nothing is enqueued.
        assert len(queue) == 0
    finally:
        src.released.set()  # release the grabber so close() joins cleanly
        hook.close()


# ---------------------------------------------------------------------------
# 4. mirrors PatHook: on_tick signature + guarded close()
# ---------------------------------------------------------------------------


def test_on_tick_signature_accepts_commanded_head():
    queue = MotionQueue()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=_FakeDetector(None))
    try:
        # Same 4-arg on_tick contract as PatHook: (transport, queue, t, commanded_head).
        hook(None, queue, 0.0, {"pitch": 0.0, "yaw": 0.0})
    finally:
        hook.close()
    assert len(queue) == 0


def test_close_is_idempotent_and_stops_grabber():
    queue = MotionQueue()
    src = _FakeFrameSource()
    hook = VisionHook(queue=queue, frame_source=src, detector=_FakeDetector(None))
    assert _wait_until(lambda: src.calls >= 1)
    hook.close()
    # The grabber thread must have stopped.
    assert _wait_until(lambda: not hook._grabber.is_alive(), timeout=2.0)
    calls_after_close = src.calls
    # A second close() is a safe no-op, and no more frames are grabbed after close.
    hook.close()
    time.sleep(0.05)
    assert src.calls == calls_after_close


def test_default_frame_source_is_shared_transport_get_frame():
    """With no injected frame_source, the hook reads the SHARED transport's get_frame.

    No new ReachyMini / media session is constructed — the hook binds to the
    transport handed to it (the same one ``listen``'s loop already owns).
    """
    queue = MotionQueue()

    class _FakeTransport:
        def __init__(self) -> None:
            self.frame_calls = 0

        def get_frame(self) -> object:
            self.frame_calls += 1
            return np.zeros((4, 4, 3), dtype=np.uint8)

    transport = _FakeTransport()
    detector = _FakeDetector(None)
    hook = VisionHook(queue=queue, transport=transport, detector=detector)
    try:
        assert _wait_until(lambda: transport.frame_calls >= 1)
    finally:
        hook.close()
    assert transport.frame_calls >= 1


# ---------------------------------------------------------------------------
# 5. cognition feed (issue #32): a decision also feeds buffer.feed_vision(...)
# ---------------------------------------------------------------------------


def test_decision_feeds_cognition_buffer():
    """A motion/light decision (detector returns a MotionAction) feeds the buffer."""
    queue = MotionQueue()
    src = _FakeFrameSource()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=src, detector=detector, buffer=buffer)
    try:
        assert _wait_until(lambda: src.calls >= 1)
        assert _wait_until(lambda: (hook(None, queue, t=float(0)) or True) and len(queue) >= 1)
    finally:
        hook.close()
    assert buffer.calls == [(0.6, 0.0)]


def test_no_buffer_is_byte_identical_no_op():
    """``buffer=None`` (the default) never touches ``last_event`` — no crash, no cue."""
    queue = MotionQueue()
    src = _FakeFrameSource()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    hook = VisionHook(queue=queue, frame_source=src, detector=detector)
    try:
        assert _wait_until(lambda: src.calls >= 1)
        assert _wait_until(lambda: (hook(None, queue, t=float(0)) or True) and len(queue) >= 1)
    finally:
        hook.close()
    pending = queue.pending()
    assert any(a.label == "look +10" for a in pending)


def test_decider_without_last_event_attribute_skips_cue_silently():
    """A decider that predates the cue contract (no ``last_event``) is a silent no-op."""
    queue = MotionQueue()
    src = _FakeFrameSource()
    detector = _FakeDetector(_LOOK)  # no last_event attribute at all
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=src, detector=detector, buffer=buffer)
    try:
        assert _wait_until(lambda: src.calls >= 1)
        assert _wait_until(lambda: (hook(None, queue, t=float(0)) or True) and len(queue) >= 1)
    finally:
        hook.close()
    assert buffer.calls == []


def test_raising_buffer_does_not_break_reflex():
    """A ``feed_vision`` that raises must not stop the look from being enqueued."""
    queue = MotionQueue()
    src = _FakeFrameSource()
    detector = _FakeCueDetector(_LOOK, last_event=(0.5, 0.0))
    hook = VisionHook(queue=queue, frame_source=src, detector=detector, buffer=_RaisingBuffer())
    try:
        assert _wait_until(lambda: src.calls >= 1)
        assert _wait_until(lambda: (hook(None, queue, t=float(0)) or True) and len(queue) >= 1)
    finally:
        hook.close()
    pending = queue.pending()
    assert any(a.label == "look +10" for a in pending)


def test_hold_decision_does_not_feed_a_cue():
    """No decision (detector returns ``None``) → no cue, even with a buffer + last_event.

    ``last_event`` is only ever consulted after a non-``None`` decision (see
    :meth:`~reachy.motion.listen_vision.VisionHook.__call__`); a bare detector that
    ships ``last_event`` but decides to hold must not leak a stale cue.
    """
    queue = MotionQueue()
    detector = _FakeCueDetector(None, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        hook._holder.publish(np.zeros((4, 4, 3), dtype=np.uint8))
        hook(None, queue, t=0.0)
    finally:
        hook.close()
    assert buffer.calls == []


# ---------------------------------------------------------------------------
# 6. per-episode coalescing (issue #32): at most one cue per episode
#
# These tests bypass the background grabber's async cadence by publishing a
# frame directly into ``hook._holder`` immediately before each synchronous
# ``hook(...)`` call, so every call is guaranteed to see a FRESH frame and
# therefore reach the decider — deterministic, no timing race against the
# grabber thread's ~50ms cadence.
# ---------------------------------------------------------------------------


def _tick_with_fresh_frame(hook: VisionHook, queue: MotionQueue, t: float) -> None:
    hook._holder.publish(np.zeros((4, 4, 3), dtype=np.uint8))
    hook(None, queue, t)


def test_coalescing_continuous_motion_yields_one_cue():
    """A fake-clock, continuous same-direction sequence yields exactly ONE cue."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        # 20 ticks, 0.1s apart (1.9s total) — all inside the 2.0s default gap, same
        # direction every time: a continuous detection every tick, one episode.
        for i in range(20):
            _tick_with_fresh_frame(hook, queue, float(i) * 0.1)
    finally:
        hook.close()
    assert detector.frames, "the decider must have been fed a frame every tick"
    assert len(detector.frames) == 20
    assert buffer.calls == [(0.6, 0.0)]


def test_coalescing_new_cue_after_quiet_gap():
    """A gap longer than ``coalesce_gap`` starts a fresh episode, even unchanged."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        _tick_with_fresh_frame(hook, queue, 0.0)
        _tick_with_fresh_frame(hook, queue, 1.0)  # inside the 2.0s gap -> coalesced
        _tick_with_fresh_frame(hook, queue, 2.5)  # > 2.0s since the last CUE -> fresh episode
    finally:
        hook.close()
    assert buffer.calls == [(0.6, 0.0), (0.6, 0.0)]


def test_coalescing_direction_change_starts_new_episode_early():
    """A genuine direction swing fires a new cue immediately, even inside the gap."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        _tick_with_fresh_frame(hook, queue, 0.0)
        detector.last_event = (-0.6, 0.0)  # a swing to the other side
        _tick_with_fresh_frame(hook, queue, 0.2)  # well inside the 2.0s gap
    finally:
        hook.close()
    assert buffer.calls == [(0.6, 0.0), (-0.6, 0.0)]


def test_coalescing_small_direction_drift_stays_one_episode():
    """Frame-to-frame centroid jitter within the coalescing band stays one episode."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(0.5, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        _tick_with_fresh_frame(hook, queue, 0.0)
        detector.last_event = (0.55, 0.0)  # a 0.05 drift, well inside the 0.4 band
        _tick_with_fresh_frame(hook, queue, 0.2)
    finally:
        hook.close()
    assert buffer.calls == [(0.5, 0.0)]


def test_coalescing_applies_to_brightness_cues_too():
    """The same episode logic coalesces a run of same-sign brightness cues."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(None, 12.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        for i in range(5):
            _tick_with_fresh_frame(hook, queue, float(i) * 0.1)
    finally:
        hook.close()
    assert buffer.calls == [(None, 12.0)]


def test_coalescing_motion_then_light_is_a_kind_change():
    """A switch from a motion cue to a light cue counts as a new episode too."""
    queue = MotionQueue()
    detector = _FakeCueDetector(_LOOK, last_event=(0.6, 0.0))
    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, detector=detector, buffer=buffer)
    try:
        _tick_with_fresh_frame(hook, queue, 0.0)
        detector.last_event = (None, 12.0)
        _tick_with_fresh_frame(hook, queue, 0.2)
    finally:
        hook.close()
    assert buffer.calls == [(0.6, 0.0), (None, 12.0)]


# ---------------------------------------------------------------------------
# 7. the REAL default decider populates last_event from its own shadow detectors
# ---------------------------------------------------------------------------


def test_default_decider_starts_with_no_last_event():
    decider = _DefaultDecider()
    assert decider.last_event is None


def test_default_decider_feeds_real_motion_cue_through_vision_hook():
    """End-to-end with the PRODUCTION decider: real frames -> a real motion cue.

    Two synthetic frames differing sharply on the right half trigger the real
    :class:`~reachy.vision.motion.MotionDetector` (via ``_DefaultDecider``'s shadow
    pair, in lockstep with the wrapped :class:`~reachy.vision.producer.VisionProducer`).
    The resulting look decision must also land a ``(direction, 0.0)`` motion cue.
    """
    queue = MotionQueue()
    frame1 = np.zeros((40, 40, 3), dtype=np.uint8)
    frame2 = frame1.copy()
    frame2[:, 20:] = 255  # a sharp bright block on the right half -> clear frame-diff motion

    buffer = _FakeBuffer()
    hook = VisionHook(queue=queue, frame_source=lambda: None, buffer=buffer)
    try:
        hook._holder.publish(frame1)
        hook(None, queue, t=0.0)  # primes the detectors (first frame -> no result yet)
        hook._holder.publish(frame2)
        hook(None, queue, t=0.5)  # motion detected -> a look decision -> a cue
    finally:
        hook.close()

    assert len(queue) >= 1, "a real frame-diff motion event should have enqueued a look"
    assert buffer.calls, "a real frame-diff motion event should have fed a cue"
    direction, brightness = buffer.calls[0]
    assert direction is not None
    assert brightness == 0.0


def test_default_decider_feeds_real_light_cue_when_motion_is_quiet():
    """The light-source branch of ``_DefaultDecider.decide()`` (no motion, a light event).

    A direct unit test of the decider (not routed through VisionHook's grabber):
    swaps in fakes for BOTH the shadow pair and the producer's own pair (so a
    single ``decide()`` call is internally consistent regardless of how many times
    each is fed) reporting "no motion, a changed+directional light" — proving
    ``last_event`` takes the ``(None, brightness_delta)`` shape, mirroring
    :class:`~reachy.vision.producer.VisionProducer`'s own light-fallback priority
    (motion wins when it fires; light is the fallback).
    """
    from reachy.vision.light import LightResult

    decider = _DefaultDecider()

    class _NoMotion:
        def feed(self, frame: object) -> None:  # noqa: ARG002
            return None

    class _ChangedLight:
        def feed(self, frame: object) -> LightResult:  # noqa: ARG002
            return LightResult(direction=0.4, mean_luma=120.0, changed=True)

    no_motion, changed_light = _NoMotion(), _ChangedLight()
    decider._shadow_motion = no_motion
    decider._shadow_light = changed_light
    decider._producer.motion_detector = no_motion
    decider._producer.light_detector = changed_light

    action = decider.decide(np.zeros((4, 4, 3), dtype=np.uint8), 0.0)

    assert action is not None, "a changed, directional light event must yield a look"
    assert decider.last_event is not None
    motion_direction, _brightness_delta = decider.last_event
    assert motion_direction is None, "a light-sourced cue reports no motion direction"
