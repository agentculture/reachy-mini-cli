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

**Cognition feed (issue #32).** Mirroring :class:`~reachy.motion.listen_pat.PatHook`'s
``buffer`` parameter, :class:`VisionHook` optionally feeds an injected duck-typed
cognition sink's ``feed_vision(motion_direction, brightness_delta)`` (the shape of
:meth:`~reachy.speech.events.EventBuffer.feed_vision`) whenever it makes a
motion/light decision — fault-isolated exactly like PatHook's cue feed, so a
raising buffer can never stop the look from being enqueued. Unlike a pat press
(an edge-triggered, self-suppressing event), vision's per-tick detector can decide
*every* tick while a subject keeps moving, so a naive feed would flood cognition
with one cue per tick. :meth:`VisionHook._maybe_feed_cue` therefore coalesces:
continuous motion in roughly the same place (or a brightness change of the same
sign) produces at most one cue per **episode** — a new cue fires only after a
quiet gap (``coalesce_gap``, default :data:`DEFAULT_COALESCE_GAP`) has elapsed
since the last one, or when the reported direction/brightness shifts by more than
:data:`_DIRECTION_COALESCE_BAND` (a genuine swing, reported immediately even
inside the gap). The injected decider reports the raw ``(motion_direction,
brightness_delta)`` behind a decision via an optional ``last_event`` attribute
(duck-typed, absent on a decider that does not support cue feeding — e.g. the
bare fakes the existing tests already inject, which simply never feed a cue).
:class:`_DefaultDecider` (the real, live decider) populates it by running its own
shadow :class:`~reachy.vision.motion.MotionDetector` /
:class:`~reachy.vision.light.LightDetector` pair over the SAME frame the wrapped
:class:`~reachy.vision.producer.VisionProducer` decides with — the producer's own
``decide()`` does not expose the raw event it chose internally, so the shadow pair
(default-constructed exactly like the producer's own, fed the identical frame
stream in lockstep) recovers it without reaching into the producer's private
state. ``None`` (the default, unchanged) keeps this hook byte-identical to before
— no cue, no buffer call, no behavior change; the standalone ``vision`` noun does
not use this hook at all (it drives :class:`VisionProducer` directly) and stays
byte-identical too.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Protocol

from reachy.motion.queue import MotionAction, MotionQueue

logger = logging.getLogger(__name__)

#: How long the grabber sleeps between frame reads when a read returns promptly.
#: Slow relative to the loop tick — the camera updates far slower than the loop,
#: so a modest cadence keeps the latest frame fresh without burning CPU.
_GRAB_INTERVAL = 0.05

#: Bounded join timeout for the grabber thread on :meth:`close`. A grabber stuck
#: inside a hung ``get_frame()`` (issue #28) cannot be joined — give up after this
#: and let the daemon thread die with the process rather than blocking shutdown.
_JOIN_TIMEOUT = 0.5

#: Default per-episode coalescing gap (seconds), measured on the loop's own tick
#: clock ``t`` (no separate wall clock — tests drive ``t`` directly, exactly like
#: PatHook's ``now``). A cue arriving within this many seconds of the last one, at
#: roughly the same direction/brightness, is folded into the SAME episode (no new
#: buffer feed); once the gap elapses a fresh cue is allowed even with no change.
DEFAULT_COALESCE_GAP: float = 2.0

#: How far apart two decisions' reported values (normalised motion direction in
#: ``[-1, 1]``, or brightness delta) must be before they count as a "direction
#: change" that starts a new episode early, even inside the quiet gap. Wide enough
#: to tolerate a moving subject's frame-to-frame centroid jitter, narrow enough to
#: still catch a genuine swing from one side to the other.
_DIRECTION_COALESCE_BAND: float = 0.4

#: Rolling window (frames) for the shadow light-baseline :class:`_DefaultDecider`
#: tracks to compute a brightness delta — mirrors
#: :class:`~reachy.vision.light.LightDetector`'s own default ``history`` window.
_LUMA_BASELINE_LEN = 20


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

    **Recovering the cue behind a decision.** ``VisionProducer.decide()`` returns
    only the resulting :class:`~reachy.motion.queue.MotionAction` — it does not
    expose which detector fired or the raw direction/brightness values (they are
    local variables inside its ``decide()``). Feeding the producer's OWN detectors
    a second time would corrupt their frame-differencing state (a second
    ``feed()`` on the same frame reads as "no motion", since the stored previous
    frame would already equal the current one). So this decider keeps its own
    *shadow* :class:`~reachy.vision.motion.MotionDetector` /
    :class:`~reachy.vision.light.LightDetector` pair, default-constructed exactly
    like the producer's own, and feeds them the SAME frame immediately before
    delegating to the producer — since both pairs see an identical frame stream in
    the same order, the shadow pair's results are exactly what the producer's own
    detectors computed internally, recovered without touching the producer's
    private state. ``last_event`` is a duck-typed contract read by
    :meth:`VisionHook._maybe_feed_cue`; it is not part of the ``decide()`` return
    value.
    """

    def __init__(self) -> None:
        from reachy.vision.light import LightDetector
        from reachy.vision.motion import MotionDetector
        from reachy.vision.producer import VisionProducer

        # A transport is required by VisionProducer's dataclass but never used: we
        # only call ``decide(frame, t)``, which touches neither get_frame nor
        # move_goto. Pass a harmless placeholder.
        self._producer = VisionProducer(transport=object())
        self._shadow_motion = MotionDetector()
        self._shadow_light = LightDetector()
        #: Rolling mean-luma history, used only to derive a brightness DELTA (the
        #: producer's LightResult carries the absolute mean_luma, not a delta).
        self._luma_baseline: deque[float] = deque(maxlen=_LUMA_BASELINE_LEN)
        #: ``(motion_direction, brightness_delta)`` behind the most recent
        #: non-``None`` decision, or ``None`` before the first decision / when the
        #: last tick held. Read by :meth:`VisionHook._maybe_feed_cue` right after
        #: calling :meth:`decide`.
        self.last_event: tuple[float | None, float] | None = None

    def decide(self, frame: object, t: float) -> MotionAction | None:
        motion = self._shadow_motion.feed(frame)
        light = self._shadow_light.feed(frame)
        baseline = (
            sum(self._luma_baseline) / len(self._luma_baseline)
            if self._luma_baseline
            else light.mean_luma
        )
        brightness_delta = light.mean_luma - baseline
        self._luma_baseline.append(light.mean_luma)

        action = self._producer.decide(frame, t)
        self.last_event = None
        if action is None:
            return None
        if motion is not None:
            # Motion is the producer's primary cue too (_choose_event: motion wins
            # whenever it fired) — reuse that same priority here.
            self.last_event = (motion.direction, 0.0)
        elif light.changed and light.direction is not None:
            self.last_event = (None, brightness_delta)
        return action


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

    def peek(self) -> object | None:
        """Return the latest published frame WITHOUT consuming it.

        Unlike :meth:`take` (which hands a frame out at most once so vision's own
        per-tick detector never re-reads a stale frame), :meth:`peek` leaves the
        ``_fresh`` flag untouched — so a *second* consumer (the folded
        :class:`~reachy.motion.listen_face.FaceHook`) can read the most recent
        grabbed frame off this ONE grabber without stealing it from vision's own
        :meth:`take`. Face detection at ~2 Hz just wants the freshest frame
        available; whether vision already looked at it is irrelevant.
        """
        with self._lock:
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
    buffer:
        An optional duck-typed cognition sink exposing ``feed_vision(motion_direction,
        brightness_delta)`` (the shape of
        :meth:`~reachy.speech.events.EventBuffer.feed_vision`) — kept loose rather
        than typed as ``EventBuffer``, mirroring :class:`~reachy.motion.listen_pat.PatHook`'s
        ``buffer`` parameter. On a motion/light decision the hook feeds this sink
        (coalesced — see :meth:`_maybe_feed_cue`), wrapped in its own
        ``try/except`` so a raising buffer degrades to "no cue" and never prevents
        the look from being enqueued. ``None`` (the default) keeps this hook
        byte-identical to before — no cue, no buffer call, no behavior change.
    coalesce_gap:
        Seconds (measured on the loop's own tick clock ``t``) a cue must be
        separated from the previous one, at the same rough direction/brightness,
        before it counts as a NEW episode — see :data:`DEFAULT_COALESCE_GAP` and
        the module docstring's "Cognition feed" section.
    """

    def __init__(
        self,
        *,
        queue: MotionQueue,
        transport: object | None = None,
        frame_source: Callable[[], object | None] | None = None,
        detector: _Decider | None = None,
        buffer: object | None = None,
        coalesce_gap: float = DEFAULT_COALESCE_GAP,
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

        #: Optional duck-typed cognition sink: ``feed_vision(motion_direction,
        #: brightness_delta) -> None``.
        self._buffer = buffer
        self._coalesce_gap = coalesce_gap
        #: Per-episode coalescing state (see :meth:`_maybe_feed_cue`): the kind
        #: ("motion"/"light") and value of the last cue actually fed, and when
        #: (loop-clock ``t``) it fired. ``None`` before the first cue.
        self._last_cue_kind: str | None = None
        self._last_cue_value: float = 0.0
        self._last_cue_time: float | None = None

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
        consults neither (it turns toward what it sees, not toward force). On a
        decision this also (coalesced, fault-isolated) feeds the optional
        cognition ``buffer`` — see :meth:`_maybe_feed_cue`.
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
        self._maybe_feed_cue(t)

    def _maybe_feed_cue(self, t: float) -> None:
        """Feed the cognition buffer at most once per coalesced vision episode.

        Reads the optional ``last_event`` the decider stamped alongside its
        decision (duck-typed; a decider that does not support cue feeding — e.g.
        the bare fakes in ``tests/test_listen_vision.py`` — simply has no such
        attribute, and this is a silent no-op). A missing ``buffer`` is likewise a
        no-op: the reflex (the move already enqueued in :meth:`__call__`) never
        depends on this.

        **Coalescing.** Unlike a pat press (edge-triggered, self-suppressing —
        see :class:`~reachy.motion.listen_pat.PatHook`), vision's per-tick
        detector can decide on EVERY tick while a subject keeps moving, so an
        unconditional feed would flood cognition with one cue per tick for a
        single continuous event. A new cue is fed only when either:

        * more than ``coalesce_gap`` seconds (on the loop's tick clock ``t``, no
          separate wall clock) have elapsed since the last cue actually fed — a
          quiet gap ends the previous episode, so even an unchanged direction
          starts a fresh one, or
        * the reported value (``motion_direction`` or ``brightness_delta``) has
          shifted by more than :data:`_DIRECTION_COALESCE_BAND` — a genuine
          swing is reported immediately, even inside the gap.

        Otherwise the decision is folded into the current episode and no buffer
        call is made. The buffer call itself is wrapped in ``try/except`` — a
        raising buffer is logged once and swallowed, mirroring
        :class:`~reachy.motion.listen_pat.PatHook`'s ``buffer.feed_pat`` guard.
        """
        if self._buffer is None:
            return
        last_event = getattr(self.detector, "last_event", None)
        if last_event is None:
            return
        motion_direction, brightness_delta = last_event
        kind = "motion" if motion_direction is not None else "light"
        value = motion_direction if motion_direction is not None else brightness_delta
        if (
            self._last_cue_time is not None
            and self._last_cue_kind == kind
            and (t - self._last_cue_time) <= self._coalesce_gap
            and abs(value - self._last_cue_value) <= _DIRECTION_COALESCE_BAND
        ):
            return  # folded into the current episode — no new cue
        buffer = self._buffer
        try:
            buffer.feed_vision(motion_direction, brightness_delta)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a raising buffer must never break the reflex
            logger.warning("VisionHook buffer feed raised; cue dropped", exc_info=True)
        self._last_cue_kind = kind
        self._last_cue_value = value
        self._last_cue_time = t

    def latest_frame(self) -> object | None:
        """Non-consuming peek at the most recent grabbed frame (the FaceHook seam).

        Returns the latest frame this hook's background grabber has published
        WITHOUT consuming it (see :meth:`_FrameHolder.peek`), so the folded
        :class:`~reachy.motion.listen_face.FaceHook` can share this ONE grabber's
        frames instead of opening a second camera grabber (single-SDK-owner). The
        composition layer (:func:`reachy.cli._commands.listen._build_live_hooks`)
        passes this bound method to ``FaceHook(frame_provider=...)``; vision's own
        per-tick :meth:`__call__` is unaffected (it still uses :meth:`take`).
        """
        return self._holder.peek()

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
