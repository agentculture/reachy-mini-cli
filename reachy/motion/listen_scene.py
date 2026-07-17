"""Fold periodic scene description into the ``listen`` motion loop.

``listen`` owns the one in-process SDK client (and its one camera). A *separate*
scene-description process could not run alongside it: both would contend for that
single-consumer client and get throttled — the same single-SDK-owner constraint
that motivated folding ``pat`` (#43), ``vision`` (t7), and ``face`` (t9) into
``listen``'s loop. So scene description is folded the same way: :class:`SceneHook`
is a per-tick hook ``(transport, queue, t, commanded_head) -> None`` that mirrors
:class:`~reachy.motion.listen_face.FaceHook`'s shape and is composed into the
loop's single ``on_tick`` seam via :class:`~reachy.motion.listen_hooks.HookChain`.

**Frame-sharing design — ONE grabber, never two.** The VLM ``describe_frame`` call
(JPEG-encode + a multimodal HTTP request) is far too heavy/slow for the ~20 Hz
tick, so it runs on a background *describe* worker thread. But the hook does **not**
own a frame *grabber* — that would be a second thread hammering
``transport.get_frame()`` alongside :class:`~reachy.motion.listen_vision.VisionHook`'s
grabber, exactly the contention we are avoiding. Instead ``SceneHook`` takes a
``frame_provider`` callable: the non-consuming latest-frame peek
:meth:`VisionHook.latest_frame`, which reads VisionHook's ONE background grabber's
holder without stealing frames from vision's own per-tick consumer. The per-tick
:meth:`__call__` (on the tick thread) only:

* publishes the latest shared frame into the worker's input slot (a cheap peek,
  never a blocking ``get_frame`` — a stalled camera can't freeze the tick), and
* drains the worker's latest completed description and feeds the cognition
  ``buffer`` via :meth:`~reachy.speech.events.EventBuffer.feed_scene`.

The heavy :func:`~reachy.vision.scene.describe_frame` runs only on the worker,
bounded to at most one describe per ``interval`` (default 30 s, matching
``reachy_nova.nova_vision``'s fallback cadence). A slow/hung describe parks the
worker, never the tick.

**One shared describe path.** The very same :func:`~reachy.vision.scene.describe_frame`
is the ``describe`` seam here AND the callable behind the on-demand ``describe_scene``
agent tool (:mod:`reachy.speech.tools`, wired at composition) — the periodic hook and
the on-demand tool are two consumers of one path, not two implementations.

**Failure episodes.** A :class:`~reachy.vision.scene.SceneError` (unreachable/slow/
malformed VLM) logs exactly ONE loud ``senselog.drop(reason=vlm-unreachable)`` per
*episode* — a run of consecutive failures — not once per describe: an endpoint that
is down for minutes yields one drop line, not one every 30 s. The latch clears on the
next success. A describe failure never stalls or crashes the loop (the worker loop
swallows it) and never blocks the tick.

Determinism seams for tests: ``clock`` (the worker's cadence clock) is injectable
(default :func:`time.monotonic`); the ``describe`` seam is injected (a fake in tests;
:func:`~reachy.vision.scene.describe_frame` in production). The synchronous core
(:meth:`_describe_once`, :meth:`_worker_tick`, the :meth:`__call__` drain) is directly
callable, so tests drive it without racing the worker thread.

Pure standard library at import time — no cv2, no numpy: :func:`describe_frame` is
imported lazily only when no ``describe`` seam is injected (i.e. production), so this
module stays importable on a bare install.
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable

from reachy import senselog
from reachy.vision.scene import SceneError

logger = logging.getLogger(__name__)

#: Minimum wall-clock gap (seconds) between two describes on the worker thread.
#: A VLM round-trip is heavy; 30 s matches ``reachy_nova.nova_vision``'s fallback
#: analysis cadence. Injectable via ``interval=`` / ``clock=`` for deterministic tests.
DEFAULT_DESCRIBE_INTERVAL: float = 30.0

#: How long the worker parks between iterations when idle (bounded so :meth:`close`
#: joins promptly and the cadence gate stays responsive).
_POLL_INTERVAL: float = 0.02

#: Bounded join timeout for the describe worker on :meth:`close`. A describe in flight
#: (a slow/hung HTTP request) may not finish instantly; the worker is a daemon thread
#: so it dies with the process if the timed join gives up.
_JOIN_TIMEOUT: float = 1.0


def _event_id() -> str:
    """A short id for a single [SENSE] log line (mirrors EventBuffer._append)."""
    return uuid.uuid4().hex[:8]


class _Slot:
    """A lock-guarded, latest-wins, consume-once value slot.

    The producer :meth:`publish`\\ es (overwriting any un-taken value with the
    latest); the consumer :meth:`take`\\ s at most once per published value. Used
    twice: the tick thread publishes frames for the worker to take, and the worker
    publishes descriptions for the tick thread to take.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: object | None = None
        self._fresh = False

    def publish(self, value: object) -> None:
        with self._lock:
            self._value = value
            self._fresh = True

    def take(self) -> object | None:
        with self._lock:
            if not self._fresh:
                return None
            self._fresh = False
            return self._value


class SceneHook:
    """A per-tick ``on_tick`` hook running periodic scene description in ``listen``'s loop.

    Construct with a ``frame_provider`` (the shared
    :meth:`VisionHook.latest_frame`), the shared cognition ``buffer``, and
    optionally an injected ``describe`` seam. Pass :meth:`__call__` as one of the
    hooks in the loop's :class:`~reachy.motion.listen_hooks.HookChain`, and call
    :meth:`close` in the loop's ``finally`` so the describe worker is stopped.

    Parameters
    ----------
    frame_provider:
        A zero-arg callable returning the latest camera frame (or ``None``). This is
        the shared, non-consuming :meth:`VisionHook.latest_frame` — SceneHook opens
        NO camera and spawns NO frame grabber, so there is never a second grabber
        contending for the one SDK client. Required (a ``None`` provider raises).
    buffer:
        An optional duck-typed cognition sink exposing ``feed_scene(text)`` (the
        shape of :meth:`~reachy.speech.events.EventBuffer.feed_scene`). On a
        completed describe the hook feeds this sink, fault-isolated (a raising sink
        is logged and swallowed). ``None`` (the default) → no cognition feed.
    describe:
        The describe seam — a callable ``(frame) -> str`` (raises
        :class:`~reachy.vision.scene.SceneError` on failure). Defaults to
        :func:`reachy.vision.scene.describe_frame` (imported lazily, so this module
        stays importable with no cv2). Called only on the worker thread. Tests inject
        a fake.
    interval:
        Minimum seconds between two describes on the worker (default
        :data:`DEFAULT_DESCRIBE_INTERVAL`), measured on ``clock``.
    clock:
        The worker's cadence clock; default :func:`time.monotonic`. Injectable for
        deterministic cadence tests.
    """

    def __init__(
        self,
        *,
        frame_provider: Callable[[], object | None] | None,
        buffer: object | None = None,
        describe: Callable[[object], str] | None = None,
        interval: float = DEFAULT_DESCRIBE_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if frame_provider is None:
            raise ValueError(
                "SceneHook needs a frame_provider (the shared VisionHook.latest_frame) — "
                "it never opens its own camera grabber"
            )
        if describe is None:
            # Lazy import so this module stays importable on a bare install (no cv2):
            # the real describe path only pulls reachy.vision.scene when no seam is
            # injected (i.e. production; tests always inject a fake).
            from reachy.vision.scene import describe_frame

            describe = describe_frame
        self._frame_provider = frame_provider
        self._buffer = buffer
        self._describe = describe
        self._interval = interval
        self._clock = clock

        #: Tick thread → worker: the latest frame to describe.
        self._input = _Slot()
        #: Worker → tick thread: the latest completed description.
        self._output = _Slot()
        #: Worker-thread-only: wall-clock of the last describe (cadence gate).
        self._last_describe: float | None = None
        #: Worker-thread-only: whether we are inside a failure episode (one drop
        #: per episode — see :meth:`_note_failure` / :meth:`_note_success`).
        self._failing = False
        #: Count of scene cues fed this run (for diagnostics / tests).
        self.events = 0

        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="scene-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ #
    # per-tick hook (tick thread)                                        #
    # ------------------------------------------------------------------ #

    def __call__(
        self,
        transport: object,  # noqa: ARG002
        queue: object,  # noqa: ARG002
        t: float,  # noqa: ARG002
        commanded_head: dict[str, float] | None = None,  # noqa: ARG002
    ) -> None:
        """One tick: publish the latest shared frame, then drain a description → a cue.

        Peeks the shared ``frame_provider`` (non-blocking; a raising provider degrades
        to "no frame") and hands the latest frame to the worker. Then drains the
        worker's latest completed description and feeds the cognition ``buffer``.
        ``transport`` / ``queue`` / ``t`` / ``commanded_head`` are part of the shared
        ``on_tick`` contract; scene description consults none of them (it reacts to
        what it *sees*).
        """
        try:
            frame = self._frame_provider()
        except Exception:  # noqa: BLE001 — a raising provider must never break the tick
            frame = None
        if frame is not None:
            self._input.publish(frame)

        result = self._output.take()
        if result is None:
            return
        self.events += 1
        self._feed_cue(str(result))

    def _feed_cue(self, text: str) -> None:
        """Feed the cognition buffer, fault-isolated (a raising sink never breaks the tick)."""
        buffer = self._buffer
        if buffer is None:
            return
        try:
            buffer.feed_scene(text)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a raising buffer must never break the loop
            logger.warning("SceneHook buffer feed raised; cue dropped", exc_info=True)

    # ------------------------------------------------------------------ #
    # background describe worker                                         #
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        """Drive :meth:`_worker_tick` until stopped; one iteration must never raise out."""
        while not self._stop.is_set():
            try:
                self._worker_tick()
            except Exception:  # noqa: BLE001 — never let the worker die on a bad frame
                logger.warning("SceneHook worker tick raised; continuing", exc_info=True)
            self._stop.wait(_POLL_INTERVAL)

    def _worker_tick(self) -> None:
        """One worker iteration: cadence-gated describe on the latest frame.

        The cadence gate is checked FIRST (cheap) so a frame is only consumed when a
        describe is actually due — the freshest available frame is used, and the heavy
        describe runs at most once per ``interval``.
        """
        now = self._clock()
        if self._last_describe is not None and (now - self._last_describe) < self._interval:
            return
        frame = self._input.take()
        if frame is None:
            return
        self._last_describe = now
        text = self._describe_once(frame)
        if text is not None:
            self._output.publish(text)

    def _describe_once(self, frame: object) -> str | None:
        """Describe one frame → a stripped sentence, or ``None``.

        A :class:`~reachy.vision.scene.SceneError` degrades to ``None`` and (once per
        failure episode) logs a loud drop. Any other error degrades to ``None`` too
        (logged, never propagated). An empty/whitespace description is ``None``.
        """
        try:
            text = self._describe(frame)
        except SceneError as err:
            self._note_failure(str(err))
            return None
        except Exception:  # noqa: BLE001 — never let a describe fault kill the worker
            logger.warning("SceneHook describe raised; skipping frame", exc_info=True)
            return None
        self._note_success()
        if not text or not str(text).strip():
            return None
        return str(text).strip()

    def _note_failure(self, reason: str) -> None:
        """Record one describe failure — log ONE loud drop per failure episode.

        The first failure of an episode emits ``senselog.drop(reason=vlm-unreachable)``
        and a WARNING; subsequent consecutive failures are silent (the latch holds)
        until a success clears it — so a VLM that is down for minutes yields one drop
        line, not one every ``interval``.
        """
        if self._failing:
            return
        self._failing = True
        logger.warning("SceneHook: scene description unavailable (%s); pausing scene cues", reason)
        senselog.drop("scene", "scene", _event_id(), "vlm-unreachable")

    def _note_success(self) -> None:
        """Clear the failure-episode latch after a successful describe."""
        self._failing = False

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Stop the describe worker (idempotent, bounded join).

        Sets the stop event and joins the worker under :data:`_JOIN_TIMEOUT`. A worker
        mid-describe (a slow/hung HTTP request) may not finish in time — the timed join
        returns and the daemon thread dies with the process. Always safe to call more
        than once; the ``listen`` loop calls this in its ``finally``.
        """
        self._stop.set()
        worker = self._worker
        if worker.is_alive():
            worker.join(timeout=_JOIN_TIMEOUT)


__all__ = ["SceneHook", "DEFAULT_DESCRIBE_INTERVAL"]
