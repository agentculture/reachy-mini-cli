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

No robot, no daemon, no network, no real camera; clocks/threads are deterministic
(the grabber is exercised through real threads but every wait is bounded).
"""

from __future__ import annotations

import threading
import time

import numpy as np

from reachy.motion.listen_vision import VisionHook
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
    a test can confirm the grabber's frame reached the detector.
    """

    def __init__(self, action: MotionAction | None) -> None:
        self._action = action
        self.frames: list[object] = []

    def decide(self, frame: object, t: float) -> MotionAction | None:  # noqa: ARG002
        self.frames.append(frame)
        return self._action


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
