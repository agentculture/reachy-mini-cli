"""Fold face recognition into the ``listen`` motion loop.

``listen`` owns the one in-process SDK client (and its one camera). A *separate*
face-recognition process could not run alongside it: both would contend for that
single-consumer client and get throttled — the same single-SDK-owner constraint
that motivated folding ``pat`` (#43) and ``vision`` (t7) into ``listen``'s loop.
So face detection is folded the same way: :class:`FaceHook` is a per-tick hook
``(transport, queue, t, commanded_head) -> None`` that mirrors
:class:`~reachy.motion.listen_vision.VisionHook`'s shape and is composed into the
loop's single ``on_tick`` seam via :class:`~reachy.motion.listen_hooks.HookChain`.

**Frame-sharing design — ONE grabber, never two.** YuNet detection + SFace
embedding is far too heavy for the ~20 Hz tick, so it runs on a background
*detection* worker thread. But the hook does **not** own a frame *grabber* — that
would be a second thread hammering ``transport.get_frame()`` alongside
:class:`~reachy.motion.listen_vision.VisionHook`'s grabber, exactly the contention
we are avoiding. Instead ``FaceHook`` takes a ``frame_provider`` callable: the
non-consuming latest-frame peek :meth:`VisionHook.latest_frame`, which reads
VisionHook's ONE background grabber's holder without stealing frames from vision's
own per-tick consumer. The per-tick :meth:`__call__` (on the tick thread) only:

* publishes the latest shared frame into the worker's input slot (a cheap peek,
  never a blocking ``get_frame`` — a stalled camera can't freeze the tick), and
* drains the worker's latest completed match result, applies the per-name
  re-announce cooldown, and feeds the cognition ``buffer``.

The heavy ``FaceEngine.detect`` + ``FaceStore.match`` run only on the worker,
bounded to at most one detection per ``detect_interval`` (default 0.5 s).

**What reaches cognition.** On a permanent-tier match to a *named* face the hook
feeds ``buffer.feed_face(name)`` — but at most once per ``reannounce_cooldown``
(default 30 s) per name, so a face lingering in frame does not spam cognition with
"saw Ada" every half-second. Unknown / unnamed faces never produce a name cue. The
buffer feed is fault-isolated (a raising sink is logged and swallowed), exactly
like :class:`~reachy.motion.listen_pat.PatHook` / VisionHook.

**Enrollment seam.** :meth:`enroll_from_frame` grabs a frame (via the shared
provider, or an explicit one), detects + embeds it, and enrolls it into the
:class:`~reachy.vision.face_store.FaceStore` under a name — the callable the
operator-facing ``scripts/face_enroll.py`` and a future agent tool drive. There is
deliberately no ``reachy face`` CLI noun in this task.

Determinism seams for tests: ``clock`` (the worker's cadence clock) is injectable
(default :func:`time.monotonic`); the re-announce cooldown is keyed on the loop
clock ``t`` handed to :meth:`__call__` (like VisionHook's coalescing), so the whole
announce path is deterministic without a second clock. The synchronous core
(:meth:`_detect_once`, :meth:`_worker_tick`, the :meth:`__call__` drain) is
directly callable, so tests drive it without racing the worker thread.

Pure standard library at import time — no cv2, no numpy: the ``engine`` and
``store`` are injected (the composition layer lazily builds the real
:class:`~reachy.vision.face.FaceEngine` / :class:`~reachy.vision.face_store.FaceStore`
only when the ``[vision]`` extra is importable).
"""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Callable

from reachy import senselog

logger = logging.getLogger(__name__)

#: Minimum wall-clock gap (seconds) between two detections on the worker thread.
#: YuNet+SFace is heavy; ~2 Hz is plenty to catch a face that enters the frame.
#: Injectable via ``clock=`` for deterministic tests.
DEFAULT_DETECT_INTERVAL: float = 0.5

#: Per-name re-announce cooldown (seconds): a matched named face feeds cognition at
#: most once per this window, keyed on the loop clock ``t``. Without it a face that
#: simply stays in view would emit "saw <name>" every detection cycle.
DEFAULT_REANNOUNCE_COOLDOWN: float = 30.0

#: How long the worker parks between iterations when idle (bounded so :meth:`close`
#: joins promptly and the cadence gate stays responsive).
_POLL_INTERVAL: float = 0.02

#: Bounded join timeout for the detection worker on :meth:`close`. A detection in
#: flight (cv2) may not finish instantly; the worker is a daemon thread so it dies
#: with the process if the timed join gives up.
_JOIN_TIMEOUT: float = 1.0


def _event_id() -> str:
    """A short id for a single [SENSE] log line (mirrors EventBuffer._append)."""
    return uuid.uuid4().hex[:8]


class _Slot:
    """A lock-guarded, latest-wins, consume-once value slot.

    The producer :meth:`publish`\\ es (overwriting any un-taken value with the
    latest); the consumer :meth:`take`\\ s at most once per published value. Used
    twice: the tick thread publishes frames for the worker to take, and the worker
    publishes match names for the tick thread to take.
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


class FaceHook:
    """A per-tick ``on_tick`` hook running face recognition inside ``listen``'s loop.

    Construct with a lazily-built :class:`~reachy.vision.face.FaceEngine` and
    :class:`~reachy.vision.face_store.FaceStore`, the shared cognition
    ``buffer``, and a ``frame_provider`` (the shared
    :meth:`VisionHook.latest_frame`). Pass :meth:`__call__` as one of the hooks in
    the loop's :class:`~reachy.motion.listen_hooks.HookChain`, and call
    :meth:`close` in the loop's ``finally`` so the detection worker is stopped.

    Parameters
    ----------
    engine:
        The face detector/embedder — anything exposing ``detect(frame) ->
        FaceDetection | None`` (the real :class:`~reachy.vision.face.FaceEngine`, or
        a test fake). Called only on the worker thread.
    store:
        The face store — anything exposing ``match(embedding) -> FaceMatch | None``
        and ``enroll(name, embedding)`` (the real
        :class:`~reachy.vision.face_store.FaceStore`, or a fake).
    frame_provider:
        A zero-arg callable returning the latest camera frame (or ``None``). This is
        the shared, non-consuming :meth:`VisionHook.latest_frame` — FaceHook opens
        NO camera and spawns NO frame grabber, so there is never a second grabber
        contending for the one SDK client. Required (a ``None`` provider raises).
    buffer:
        An optional duck-typed cognition sink exposing ``feed_face(name)`` (the
        shape of :meth:`~reachy.speech.events.EventBuffer.feed_face`). On a
        cooldown-cleared match the hook feeds this sink, fault-isolated (a raising
        sink is logged and swallowed). ``None`` (the default) → no cognition feed.
    detect_interval:
        Minimum seconds between two detections on the worker (default
        :data:`DEFAULT_DETECT_INTERVAL`), measured on ``clock``.
    reannounce_cooldown:
        Minimum seconds between two ``feed_face`` cues for the SAME name (default
        :data:`DEFAULT_REANNOUNCE_COOLDOWN`), measured on the loop clock ``t``.
    clock:
        The worker's cadence clock; default :func:`time.monotonic`. Injectable for
        deterministic cadence tests.
    """

    def __init__(
        self,
        *,
        engine: object,
        store: object,
        frame_provider: Callable[[], object | None] | None,
        buffer: object | None = None,
        detect_interval: float = DEFAULT_DETECT_INTERVAL,
        reannounce_cooldown: float = DEFAULT_REANNOUNCE_COOLDOWN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if frame_provider is None:
            raise ValueError(
                "FaceHook needs a frame_provider (the shared VisionHook.latest_frame) — "
                "it never opens its own camera grabber"
            )
        self._engine = engine
        self._store = store
        self._frame_provider = frame_provider
        self._buffer = buffer
        self._detect_interval = detect_interval
        self._reannounce_cooldown = reannounce_cooldown
        self._clock = clock

        #: Tick thread → worker: the latest frame to detect on.
        self._input = _Slot()
        #: Worker → tick thread: the latest matched (named) face.
        self._output = _Slot()
        #: Worker-thread-only: wall-clock of the last detection (cadence gate).
        self._last_detect: float | None = None
        #: Tick-thread-only: name → loop-clock ``t`` of its last announce (cooldown).
        self._last_announced: dict[str, float] = {}
        #: Count of face cues announced this run (for diagnostics / tests).
        self.events = 0

        self._stop = threading.Event()
        self._worker = threading.Thread(target=self._worker_loop, name="face-worker", daemon=True)
        self._worker.start()

    # ------------------------------------------------------------------ #
    # per-tick hook (tick thread)                                        #
    # ------------------------------------------------------------------ #

    def __call__(
        self,
        transport: object,  # noqa: ARG002
        queue: object,  # noqa: ARG002
        t: float,
        commanded_head: dict[str, float] | None = None,  # noqa: ARG002
    ) -> None:
        """One tick: publish the latest shared frame, then drain a match → a cue.

        Peeks the shared ``frame_provider`` (non-blocking; a raising provider
        degrades to "no frame") and hands the latest frame to the worker. Then
        drains the worker's latest completed match: a named face clears the
        per-name re-announce cooldown (keyed on ``t``) at most once per window
        before feeding the cognition ``buffer``. ``transport`` / ``queue`` /
        ``commanded_head`` are part of the shared ``on_tick`` contract; face
        recognition consults none of them (it reacts to what it *sees*).
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
        name = str(result)
        last = self._last_announced.get(name)
        if last is not None and (t - last) < self._reannounce_cooldown:
            senselog.drop("reannounce", "face", _event_id(), "cooldown")
            return
        self._last_announced[name] = t
        self.events += 1
        self._feed_cue(name)

    def _feed_cue(self, name: str) -> None:
        """Feed the cognition buffer, fault-isolated (a raising sink never breaks the tick)."""
        buffer = self._buffer
        if buffer is None:
            return
        try:
            buffer.feed_face(name)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a raising buffer must never break the loop
            logger.warning("FaceHook buffer feed raised; cue dropped", exc_info=True)

    # ------------------------------------------------------------------ #
    # background detection worker                                        #
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        """Drive :meth:`_worker_tick` until stopped; one iteration must never raise out."""
        while not self._stop.is_set():
            try:
                self._worker_tick()
            except Exception:  # noqa: BLE001 — never let the worker die on a bad frame
                logger.warning("FaceHook worker tick raised; continuing", exc_info=True)
            self._stop.wait(_POLL_INTERVAL)

    def _worker_tick(self) -> None:
        """One worker iteration: cadence-gated detection on the latest frame.

        The cadence gate is checked FIRST (cheap) so a frame is only consumed when a
        detection is actually due — the freshest available frame is used, and the
        heavy detect runs at most once per ``detect_interval``.
        """
        now = self._clock()
        if self._last_detect is not None and (now - self._last_detect) < self._detect_interval:
            return
        frame = self._input.take()
        if frame is None:
            return
        self._last_detect = now
        name = self._detect_once(frame)
        if name is not None:
            self._output.publish(name)

    def _detect_once(self, frame: object) -> str | None:
        """Detect + embed + match one frame → a known name, or ``None``.

        Every heavy/foreign call (``engine.detect`` / ``store.match``) is guarded:
        a raise degrades to "no match" and is logged, never propagated. An unknown
        face (no match) or an unnamed match (empty name) is ``None`` — only a
        named, matched face becomes a name cue.
        """
        try:
            detection = self._engine.detect(frame)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("FaceHook detection raised; skipping frame", exc_info=True)
            return None
        if detection is None:
            return None
        try:
            match = self._store.match(detection.embedding)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("FaceHook store match raised; skipping frame", exc_info=True)
            return None
        if match is None:
            return None
        name = getattr(match, "name", None)
        if not name or not str(name).strip():
            return None  # unknown / unnamed face — never announced by name
        return str(name).strip()

    # ------------------------------------------------------------------ #
    # enrollment seam                                                    #
    # ------------------------------------------------------------------ #

    def enroll_from_frame(self, name: str, frame: object | None = None) -> str | None:
        """Enroll the largest face in a frame under *name*. Returns the new id or ``None``.

        Grabs *frame* (or the latest shared frame when omitted), detects + embeds
        it, and enrolls the embedding into the store's permanent tier. Returns
        ``None`` when no frame is available or no face is found. This is the seam
        the operator-facing ``scripts/face_enroll.py`` (and a future agent tool)
        drive — a synchronous, one-shot call, not part of the per-tick loop.
        """
        if frame is None:
            try:
                frame = self._frame_provider()
            except Exception:  # noqa: BLE001
                frame = None
        if frame is None:
            return None
        detection = self._engine.detect(frame)  # type: ignore[attr-defined]
        if detection is None:
            return None
        return self._store.enroll(name, detection.embedding)  # type: ignore[attr-defined]

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Stop the detection worker (idempotent, bounded join).

        Sets the stop event and joins the worker under :data:`_JOIN_TIMEOUT`. A
        worker mid-detection (cv2) may not finish in time — the timed join returns
        and the daemon thread dies with the process. Always safe to call more than
        once; the ``listen`` loop calls this in its ``finally``.
        """
        self._stop.set()
        worker = self._worker
        if worker.is_alive():
            worker.join(timeout=_JOIN_TIMEOUT)


__all__ = ["FaceHook", "DEFAULT_DETECT_INTERVAL", "DEFAULT_REANNOUNCE_COOLDOWN"]
