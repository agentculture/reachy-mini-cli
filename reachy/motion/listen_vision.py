"""Fold vision motion/light detection into the ``listen`` motion loop.

``listen`` already owns the single-consumer SDK client (and its one media/camera
subsystem). A *separate* ``vision`` process can't run alongside it: both would
contend for that one client and get throttled — the same single-SDK-owner
constraint that motivated folding ``pat`` into ``listen`` (#43). So vision's
per-tick frame→look decision is folded in the same way: :class:`VisionHook` is a
per-tick hook ``(transport, queue, t, commanded_head) -> None`` that mirrors
:class:`~reachy.motion.listen_pat.PatHook`'s shape and is passed as ``on_tick=``
to :func:`reachy.motion.server.run`. On every tick it:

* reads the *latest* camera frame from a background grabber (see below) — never a
  synchronous ``get_frame()`` on the tick thread,
* feeds that frame to an injected vision detector (default: a
  :class:`~reachy.vision.producer.VisionProducer` driving the two pixel detectors
  — :class:`~reachy.vision.motion.MotionDetector` and
  :class:`~reachy.vision.light.LightDetector` — through its ``decide`` method, so
  the detection math is reused, not reinvented), and
* on a decision (a non-``None`` :class:`~reachy.motion.queue.MotionAction`)
  enqueues that look onto the *same* serial :class:`~reachy.motion.queue.MotionQueue`
  the loop drives.

Non-blocking frame access is the crux. Vision's live SDK ``get_frame()`` has hung
before (issue #28); a synchronous read on the tick thread would freeze the whole
``listen`` loop on a stalled camera. So a **background grabber thread** calls the
frame source in a loop and publishes the latest frame into a lock-guarded holder;
the per-tick :meth:`VisionHook.__call__` only ever *reads* that holder and returns
promptly even while the grabber is blocked inside a hung ``get_frame()``. A
stale-frame guard means an old frame is not re-fed every tick — each frame is
consumed at most once. The grabber swallows any frame-source error (degrade
silently, never a traceback), and :meth:`close` joins it under a bounded timeout.

``commanded_head`` is accepted to satisfy the shared ``on_tick`` contract (the
same 4-arg signature ``PatHook`` uses); vision turns toward what it *sees*, not
proprioceptive force, so the head pose is not consulted here.
"""

from __future__ import annotations

import threading
from typing import Callable, Protocol

from reachy.motion.queue import MotionAction, MotionQueue

#: How long the grabber sleeps between frame reads when a read returns promptly.
#: Slow relative to the loop tick — the camera updates far slower than the loop,
#: so a modest cadence keeps the latest frame fresh without burning CPU.
_GRAB_INTERVAL = 0.05

#: Bounded join timeout for the grabber thread on :meth:`close`. A grabber stuck
#: inside a hung ``get_frame()`` (issue #28) cannot be joined — give up after this
#: and let the daemon thread die with the process rather than blocking shutdown.
_JOIN_TIMEOUT = 0.5


class _Decider(Protocol):
    """The injected vision decision engine: one frame + a clock → a look or hold."""

    def decide(self, frame: object, t: float) -> MotionAction | None: ...  # noqa: E704


class _DefaultDecider:
    """Default decider wrapping a :class:`~reachy.vision.producer.VisionProducer`.

    Reuses vision's exact detection math — the two pixel detectors plus the
    deadband/hold decision in :meth:`VisionProducer.decide` — rather than
    reinventing it. The producer's queue/executor halves are unused here (the hook
    owns enqueueing onto ``listen``'s shared queue); only its pure ``decide`` leg
    is driven. Imported lazily so a test or a frame-source-only construction never
    pulls numpy/vision at import time.
    """

    def __init__(self) -> None:
        from reachy.vision.producer import VisionProducer

        # A transport is required by VisionProducer's dataclass but never used: we
        # only call ``decide(frame, t)``, which touches neither get_frame nor
        # move_goto. Pass a harmless placeholder.
        self._producer = VisionProducer(transport=object())

    def decide(self, frame: object, t: float) -> MotionAction | None:
        return self._producer.decide(frame, t)


class _FrameHolder:
    """Lock-guarded latest-frame holder with a stale-frame guard.

    The grabber thread :meth:`publish`\\ es; the tick thread :meth:`take`\\ s. A
    frame is handed out at most once — :meth:`take` returns ``None`` once a frame
    has been consumed and no newer one has arrived — so an old frame is never
    re-fed to the detector every tick.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._frame: object | None = None
        self._fresh = False

    def publish(self, frame: object) -> None:
        with self._lock:
            self._frame = frame
            self._fresh = True

    def take(self) -> object | None:
        with self._lock:
            if not self._fresh:
                return None
            self._fresh = False
            return self._frame


class VisionHook:
    """A per-tick ``on_tick`` hook running vision detection inside ``listen``'s loop.

    Construct one with the loop's :class:`~reachy.motion.queue.MotionQueue` and
    either a ``transport`` (its ``get_frame`` becomes the frame source) or an
    explicit ``frame_source`` callable (tests inject a fake). Pass
    :meth:`__call__` as ``on_tick=`` to :func:`reachy.motion.server.run`, and call
    :meth:`close` in the loop's ``finally`` so the grabber thread is stopped.

    Parameters
    ----------
    queue:
        The shared serial queue the look move is enqueued onto (the same one
        ``listen``'s producer submits sound-orient moves to).
    transport:
        The shared SDK transport. When ``frame_source`` is omitted, the frame
        source defaults to ``transport.get_frame`` — the *same* one SDK client the
        loop already owns (no new media session, no second ``ReachyMini``).
    frame_source:
        A zero-arg callable returning the latest camera frame (or ``None`` when
        none is available). Defaults to ``transport.get_frame``. Tests inject a
        fake. The grabber calls this on a background thread, never on the tick.
    detector:
        An injected vision decider exposing ``decide(frame, t) -> MotionAction |
        None``; defaults to a :class:`_DefaultDecider` wrapping a
        :class:`~reachy.vision.producer.VisionProducer` (vision's real detection
        math). Tests inject a fake.
    """

    def __init__(
        self,
        *,
        queue: MotionQueue,
        transport: object | None = None,
        frame_source: Callable[[], object | None] | None = None,
        detector: _Decider | None = None,
    ) -> None:
        if frame_source is None:
            if transport is None:
                raise ValueError("VisionHook needs a transport or an explicit frame_source")
            frame_source = transport.get_frame  # type: ignore[attr-defined]
        self.queue = queue
        self._frame_source = frame_source
        self.detector: _Decider = detector if detector is not None else _DefaultDecider()
        #: Count of look moves enqueued this run (for diagnostics / tests).
        self.events = 0

        self._holder = _FrameHolder()
        self._stop = threading.Event()
        self._grabber = threading.Thread(target=self._grab_loop, name="vision-grabber", daemon=True)
        self._grabber.start()

    # ------------------------------------------------------------------ #
    # background grabber (never runs on the tick thread)                 #
    # ------------------------------------------------------------------ #

    def _grab_loop(self) -> None:
        """Continuously read the frame source and publish the latest frame.

        Runs on the background grabber thread. Any frame-source error (a camera
        drop, a :class:`~reachy.cli._errors.CliError`, a stalled read that finally
        raises) is swallowed — vision degrades to "no frame" rather than killing
        the loop. A frame source that *blocks* (the hung ``get_frame`` of issue
        #28) parks this thread forever, but the tick thread is unaffected because
        it only reads the holder. The loop ends when :meth:`close` sets the stop
        event.
        """
        while not self._stop.is_set():
            try:
                frame = self._frame_source()
            except Exception:  # noqa: BLE001
                frame = None
            if frame is not None:
                self._holder.publish(frame)
            # Bounded wait so a quiet stop is observed promptly without busy-looping.
            self._stop.wait(_GRAB_INTERVAL)

    # ------------------------------------------------------------------ #
    # per-tick hook                                                      #
    # ------------------------------------------------------------------ #

    def __call__(
        self,
        transport: object,
        queue: MotionQueue,
        t: float,
        commanded_head: dict[str, float] | None = None,  # noqa: ARG002
    ) -> None:
        """One tick: read the latest frame (non-blocking) and maybe enqueue a look.

        Reads the latest published frame from the background grabber's holder —
        never calls ``get_frame()`` here, so a stalled camera (#28) cannot block
        the tick. With no fresh frame this is a no-op (the head holds). With a
        frame, it is fed to the detector; a non-``None``
        :class:`~reachy.motion.queue.MotionAction` is submitted to ``queue``. A
        detector error is swallowed (silent degradation). ``transport`` and
        ``commanded_head`` are part of the shared ``on_tick`` contract; vision
        consults neither (it turns toward what it sees, not toward force).
        """
        frame = self._holder.take()
        if frame is None:
            return
        try:
            action = self.detector.decide(frame, t)
        except Exception:  # noqa: BLE001
            return
        if action is None:
            return
        queue.submit(action)
        self.events += 1

    def close(self) -> None:
        """Stop the background grabber thread (idempotent, bounded join).

        Sets the stop event and joins the grabber under :data:`_JOIN_TIMEOUT`. A
        grabber parked inside a hung ``get_frame()`` cannot be joined — the timed
        join returns and the daemon thread dies with the process. Always safe to
        call more than once; the ``listen`` loop calls this in its ``finally``.
        """
        self._stop.set()
        grabber = self._grabber
        if grabber.is_alive():
            grabber.join(timeout=_JOIN_TIMEOUT)


__all__ = ["VisionHook"]
