"""Camera-derived sense for the 50 Hz behavior engine — ``face`` + ``frame_available``.

:class:`~reachy.behavior.sense.Sense` has declared ``face`` and
``frame_available`` (and :class:`~reachy.behavior.sense.SenseProviders` has held
slots for them) since the snapshot shape was written, but nothing in the
symbolic runtime ever fed them: a rule keyed on either field validated at load
and then silently never fired. This module is the missing producer, and it is
deliberately the *only* new thing needed — the composition root just wires the
two provider callables it exposes.

The two cues are different KINDS of signal, and the module treats them that way:

* ``face`` is an **event** — "a named face was recognised" — so it is published
  through a ONE-TICK LATCH, exactly like
  :attr:`reachy.behavior.pat_sense.PatSenseDriver.peek`'s ``pat_event``. A rule
  keyed on it sees the name in exactly one :class:`Sense` snapshot; the
  per-name re-announce cooldown below stops a face that merely lingers in view
  from re-firing every detection cycle.
* ``frame_available`` is a **condition** — "the camera is producing frames" —
  so it is a TTL-held level, not a per-tick pulse. The camera runs slower than
  the 50 Hz tick (and a steady-state read returns ``None`` whenever nothing is
  ready this instant), so a raw per-tick reading would flap at the camera's
  cadence and make any rule keyed on it useless. A frame seen within
  :data:`DEFAULT_FRAME_TTL_S` holds the condition true.

--------------------------------------------------------------------------
Threading — the heavy leg never touches the tick thread
--------------------------------------------------------------------------
YuNet detection + SFace embedding costs far more than the engine's 20 ms tick
budget. The deployed box already shows what inline blocking of this class costs:
tick overruns of **425, 974, 991, 1103 and 1213 ms** against that 20 ms budget,
reproducibly, from a single blocking construction on the tick thread
(``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).

So the split mirrors :class:`reachy.motion.listen_face.FaceHook`'s, restated for
a ``TickBus`` driver:

* the TICK THREAD only peeks one frame off the injected media client, validates
  it, publishes it into a latest-wins slot, and drains whatever the worker has
  finished — all O(1), no detection, no blocking wait;
* a BACKGROUND WORKER does the detect + match, cadence-gated to at most one
  detection per ``detect_interval``.

The worker is started only when there is actually a recognizer to run (see
"Degradation"), and :meth:`close` stops it with a bounded join.

--------------------------------------------------------------------------
Frame source — injected, never constructed
--------------------------------------------------------------------------
The frame source is :class:`reachy.robot.media_client.HeldMediaClient`, the ONE
media owner in the runtime process (the single-SDK-owner model in ``CLAUDE.md``).
This driver takes it as an **injected** dependency and never constructs one, never
opens a second grabber, and never closes it — the composition root owns its
lifecycle. Only two members are consumed, both duck-typed so this module imports
neither ``reachy_mini`` nor the media client:

* ``frame()`` -> a BGR ndarray or ``None``;
* ``camera_available`` -> a pollable predicate, used as the cheap negative that
  skips the read entirely on a camera-less robot.

**A ``None`` frame is the ORDINARY case, not a fault.** ``HeldMediaClient.frame``
documents it as "nothing ready this instant", and issue #73 is what happens when
that is forgotten: a fresh-client-per-frame path returned ``None`` on every read,
``np.asarray(None)`` produced a 0-d object array whose ``.shape`` is ``()``, and
that reached the grey/luma conversion in
:meth:`reachy.vision.motion.MotionDetector._to_grey`, which raised
``ValueError: Unsupported frame shape: ()`` and crashed ``vision run``. So
:func:`usable_frame` gates EVERY frame before it reaches the worker (or counts
toward ``frame_available``): ``None``, a 0-d array, an empty array, a wrong
number of dimensions or channels, and anything numpy cannot convert are all
skipped silently. A degenerate frame is a non-reading, never an exception.

--------------------------------------------------------------------------
Degradation
--------------------------------------------------------------------------
Face recognition needs the ``[vision]`` extra (opencv). :func:`build_face_recognition`
probes for it and, when it is absent — CI, a bare install, the HTTP remote
profile — returns ``None`` after **exactly one** logged warning (latched for the
process; the extra cannot appear mid-run, so repeating the warning would only be
noise). A driver built with no recognizer is fully functional otherwise: it
spawns no worker, ``face`` stays permanently ``None``, and ``frame_available``
keeps working — the frame-validity leg is numpy-only and needs no cv2 at all.

Every other failure degrades the same way, never raising out of the driver or a
provider (mirroring :class:`reachy.behavior.pat_sense.PatSenseDriver` and
:func:`reachy.behavior.sense._peek`): a raising media client, a raising
``camera_available``, a raising detector or store, a closed driver — each is "no
reading" for that tick.

Stdlib plus numpy (already a base dependency, used only for the frame-shape
guard). No cv2 at import time, no ``reachy_mini``, no transport: the engine and
store are injected, and :func:`build_face_recognition` imports the real ones
lazily only after confirming opencv is present.
"""

from __future__ import annotations

import logging
import threading
import time
from importlib.util import find_spec as _find_spec
from typing import Any, Callable

import numpy as np

logger = logging.getLogger(__name__)

#: Minimum wall-clock gap (seconds) between two detections on the worker thread.
#: YuNet+SFace is heavy; ~2 Hz is ample for catching a face entering the frame,
#: and matches :data:`reachy.motion.listen_face.DEFAULT_DETECT_INTERVAL`.
DEFAULT_DETECT_INTERVAL: float = 0.5

#: Per-name re-announce cooldown (seconds): the same name latches into ``face``
#: at most once per this window. Without it, a face that simply stays in view
#: would re-fire its rule every detection cycle.
DEFAULT_REANNOUNCE_COOLDOWN: float = 30.0

#: How long a successfully read frame keeps ``frame_available`` true (seconds).
#: The camera is slower than the 50 Hz tick and a steady-state read returns
#: ``None`` whenever nothing is ready, so the condition is held rather than
#: pulsed — 1 s is many camera periods but still collapses promptly when the
#: camera genuinely stops.
DEFAULT_FRAME_TTL_S: float = 1.0

#: How long the worker parks between iterations when idle. Bounded so
#: :meth:`FaceSenseDriver.close` joins promptly.
_POLL_INTERVAL: float = 0.02

#: Bounded join timeout for the detection worker on :meth:`FaceSenseDriver.close`.
#: A detection in flight (cv2) may not finish instantly; the worker is a daemon
#: thread, so it dies with the process if the timed join gives up.
_JOIN_TIMEOUT: float = 1.0

#: Channel counts a usable colour/grey frame may carry (grey, BGR, BGRA).
_VALID_CHANNELS = (1, 3, 4)

#: Process-wide latch for the missing-``[vision]`` warning (see the module
#: docstring's Degradation section). Module-level rather than per-instance
#: because the extra's absence is a property of the process, not of a driver.
_VISION_WARNED = False


def usable_frame(frame: object) -> bool:
    """Whether *frame* is an image a detector could actually consume.

    The issue-#73 guard, and the reason it is a module-level function: the shape
    check must happen at the boundary, BEFORE anything downstream calls
    ``np.asarray`` and indexes the result. ``None`` (the ordinary "nothing ready
    this instant" reading), a 0-d array — ``np.asarray(None).shape == ()``, the
    exact value that crashed ``vision run`` — an empty array, a 1-d or 4-d array,
    an odd channel count, and anything numpy refuses to convert are all ``False``.

    Never raises: an unconvertible object is simply not a frame.
    """
    if frame is None:
        return False
    try:
        arr = np.asarray(frame)
    except Exception:  # noqa: BLE001 — an unconvertible object is just not a frame
        return False
    if arr.dtype == object or arr.ndim not in (2, 3) or arr.size == 0:
        return False
    if arr.ndim == 3 and arr.shape[2] not in _VALID_CHANNELS:
        return False
    return True


def build_face_recognition(
    *,
    models_dir: object | None = None,
    store_base_dir: object | None = None,
) -> tuple[Any, Any] | None:
    """Build ``(engine, store)`` for :class:`FaceSenseDriver`, or ``None``.

    Returns ``None`` — after **exactly one** process-wide logged warning — when
    the ``[vision]`` extra (opencv) is absent, which is the default state of a
    bare install, of CI, and of the HTTP remote profile. A ``None`` return is not
    an error: :class:`FaceSenseDriver` accepts it and keeps reporting
    ``frame_available``, with ``face`` permanently quiet.

    The imports are lazy and happen only after the probe succeeds, so this module
    stays importable with no opencv installed. ``models_dir`` / ``store_base_dir``
    are passed through for test isolation when given.
    """
    global _VISION_WARNED  # noqa: PLW0603 — one process-wide warning, by design
    if _find_spec("cv2") is None:
        if not _VISION_WARNED:
            _VISION_WARNED = True
            logger.warning(
                "behavior: face recognition needs the [vision] extra (opencv); the `face` "
                "sense stays unavailable (install: pip install 'reachy-mini-cli[vision]')"
            )
        return None
    try:
        from reachy.vision.face import FaceEngine
        from reachy.vision.face_store import FaceStore

        engine = FaceEngine(models_dir=models_dir) if models_dir is not None else FaceEngine()
        store = FaceStore(base_dir=store_base_dir) if store_base_dir is not None else FaceStore()
    except Exception:  # noqa: BLE001 — a broken vision stack disables the cue, nothing more
        if not _VISION_WARNED:
            _VISION_WARNED = True
            logger.warning(
                "behavior: face recognition unavailable; the `face` sense stays unavailable",
                exc_info=True,
            )
        return None
    return (engine, store)


class _Slot:
    """A lock-guarded, latest-wins, consume-once value slot.

    The producer :meth:`publish`\\ es (overwriting any un-taken value with the
    latest); the consumer :meth:`take`\\ s at most once per published value. Used
    twice: the tick thread publishes frames for the worker to take, and the
    worker publishes matched names for the tick thread to take. Latest-wins is
    the point — a slow detector must never make the tick thread queue or wait.
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


class FaceSenseDriver:
    """A ``TickBus`` driver feeding the ``face`` and ``frame_available`` cues.

    Construct one with the process's single injected media client, register
    :meth:`__call__` as a driver on the engine's ``tick_seam``, wire
    :meth:`as_face_provider` / :meth:`as_frame_available_provider` into
    :class:`~reachy.behavior.sense.SenseProviders`, and call :meth:`close` at
    teardown. See the module docstring for the cue semantics (one-tick event vs
    TTL-held condition), the threading split, and the degradation contract.

    Parameters
    ----------
    media:
        The injected media client — anything exposing ``frame() -> frame | None``
        and (optionally) a ``camera_available`` predicate. In production the
        process's one :class:`reachy.robot.media_client.HeldMediaClient`; in
        tests, a fake. NEVER constructed or closed here. ``None`` is accepted and
        means "no camera in this process": a permanent no-reading.
    engine, store:
        The face detector/embedder (``detect(frame) -> detection | None``) and
        matcher (``match(embedding) -> match | None``) — the pair
        :func:`build_face_recognition` returns. Both ``None`` (the default, and
        what a cv2-less box gets) disables the ``face`` cue entirely and starts
        no worker thread.
    detect_interval:
        Minimum seconds between two detections on the worker, measured on
        ``clock``. Default :data:`DEFAULT_DETECT_INTERVAL`.
    reannounce_cooldown:
        Minimum seconds between two ``face`` latches for the SAME name, measured
        on the engine's tick clock ``ctx.now``. Default
        :data:`DEFAULT_REANNOUNCE_COOLDOWN`.
    frame_ttl_s:
        How long a read frame holds ``frame_available`` true. Default
        :data:`DEFAULT_FRAME_TTL_S`.
    clock:
        The worker's cadence clock (default :func:`time.monotonic`). The tick
        thread uses ``ctx.now`` instead, so the driver inherits the engine's
        determinism without a second clock — the same choice
        :class:`~reachy.behavior.pat_sense.PatSenseDriver` makes.
    start_worker:
        Whether to spawn the detection worker. ``False`` lets a test drive
        :meth:`_worker_tick` synchronously, with no thread and no races.
    """

    def __init__(
        self,
        *,
        media: object | None,
        engine: object | None = None,
        store: object | None = None,
        detect_interval: float = DEFAULT_DETECT_INTERVAL,
        reannounce_cooldown: float = DEFAULT_REANNOUNCE_COOLDOWN,
        frame_ttl_s: float = DEFAULT_FRAME_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self._media = media
        self._engine = engine
        self._store = store
        self._detect_interval = max(0.0, float(detect_interval))
        self._reannounce_cooldown = max(0.0, float(reannounce_cooldown))
        self._frame_ttl_s = max(0.0, float(frame_ttl_s))
        self._clock = clock

        #: Tick thread -> worker: the latest validated frame to detect on.
        self._input = _Slot()
        #: Worker -> tick thread: the latest matched, named face.
        self._output = _Slot()
        #: Worker-thread-only: clock reading of the last detection (cadence gate).
        self._last_detect: float | None = None
        #: Tick-thread-only: name -> ``ctx.now`` of its last latch (cooldown).
        self._last_announced: dict[str, float] = {}
        #: The ONE-TICK ``face`` latch (see the module docstring).
        self._face: str | None = None
        #: The TTL-held ``frame_available`` condition and its freshness anchor.
        self._frame_available = False
        self._last_frame_at: float | None = None
        #: Count of face cues latched this run (diagnostics / tests).
        self.events = 0

        self._closed = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        # No recognizer means no heavy leg — so no thread is spawned at all.
        if start_worker and self._recognizer_ready:
            self._worker = threading.Thread(
                target=self._worker_loop, name="behavior-face-worker", daemon=True
            )
            self._worker.start()

    # ------------------------------------------------------------------ #
    # TickBus driver entry point (tick thread)                           #
    # ------------------------------------------------------------------ #

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """One tick: clear the face latch, peek a frame, drain a match.

        Never raises. The latch is cleared first — a plain assignment that cannot
        fail — so the one-tick contract holds on every path, including the early
        returns below; then the body runs under a broad guard so a misbehaving
        media client or worker degrades to "no cue this tick" rather than
        propagating into the 50 Hz loop.
        """
        self._face = None
        try:
            self._process(ctx)
        except Exception:  # noqa: BLE001 — a sense tap must never crash the loop
            logger.warning("FaceSenseDriver tick raised; face cue dropped", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """The tick-thread body: O(1) frame publish + result drain, never detection."""
        if self._closed:
            self._frame_available = False
            return
        now = self._now(ctx)
        self._update_frame(now)
        self._drain_match(now)

    # -- frame leg ------------------------------------------------------ #

    def _update_frame(self, now: float | None) -> None:
        """Peek one frame, gate it (#73), publish it, and refresh the condition."""
        if not self._connected() or not self._camera_available():
            # A camera-less robot: the condition collapses at once — no TTL hold,
            # nothing to be stale about.
            self._frame_available = False
            self._last_frame_at = None
            return

        frame = self._read_frame()
        if usable_frame(frame):
            self._last_frame_at = now
            self._frame_available = True
            if self._recognizer_ready:
                self._input.publish(frame)
            return

        # A None or degenerate frame is a NON-READING, never a fault (#73): the
        # condition simply rides its TTL until frames genuinely stop.
        self._frame_available = self._within_ttl(now)

    def _within_ttl(self, now: float | None) -> bool:
        """Whether the last good frame is still recent enough to hold the condition."""
        last = self._last_frame_at
        if last is None:
            return False
        if now is None or self._frame_ttl_s <= 0.0:
            return False
        elapsed = now - last
        return 0.0 <= elapsed <= self._frame_ttl_s

    def _connected(self) -> bool:
        """The one FREE liveness check — and the reason it is checked FIRST.

        On :class:`~reachy.robot.media_client.HeldMediaClient`, ``connected`` is a
        pure predicate that never constructs, whereas BOTH ``camera_available``
        and ``frame()`` may trigger the lazy construction of the full media chain
        — which blocks for order-of-seconds. Doing that on the tick thread is
        precisely the 425-1213 ms overrun class this driver exists to avoid, so a
        cold or dropped client is simply "no reading" here and the owner re-warms
        it off-thread.

        A client without the attribute (a fake, an older holder) is assumed live:
        the authoritative answer is then whether a usable frame arrives.
        """
        if self._media is None:
            return False
        try:
            return bool(getattr(self._media, "connected", True))
        except Exception:  # noqa: BLE001 — a raising probe is not a liveness verdict
            logger.debug("FaceSenseDriver liveness probe raised; assuming live", exc_info=True)
            return True

    def _camera_available(self) -> bool:
        """The injected client's cheap negative; a missing/raising probe assumes yes.

        A fake (or a client build) without the attribute must not be treated as
        camera-less — the authoritative answer is then simply whether
        :meth:`_read_frame` produces a usable frame.
        """
        if self._media is None:
            return False
        try:
            return bool(getattr(self._media, "camera_available", True))
        except Exception:  # noqa: BLE001 — a raising probe is not a camera verdict
            logger.debug("FaceSenseDriver camera probe raised; assuming a camera", exc_info=True)
            return True

    def _read_frame(self) -> object | None:
        """One non-blocking frame peek off the injected client; a raise is no frame."""
        media = self._media
        if media is None:
            return None
        try:
            return media.frame()  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001 — a read fault degrades, never propagates
            logger.debug("FaceSenseDriver frame read raised; no frame this tick", exc_info=True)
            return None

    # -- match leg ------------------------------------------------------ #

    def _drain_match(self, now: float | None) -> None:
        """Take the worker's latest name and latch it, honouring the re-announce cooldown."""
        result = self._output.take()
        if result is None:
            return
        name = str(result).strip()
        if not name:
            return
        if now is not None:
            last = self._last_announced.get(name)
            if last is not None and 0.0 <= (now - last) < self._reannounce_cooldown:
                return
            self._last_announced[name] = now
        self._face = name
        self.events += 1

    # ------------------------------------------------------------------ #
    # background detection worker                                        #
    # ------------------------------------------------------------------ #

    @property
    def _recognizer_ready(self) -> bool:
        """Whether there is a recognizer pair to run at all."""
        return self._engine is not None and self._store is not None

    @property
    def worker_alive(self) -> bool:
        """Whether the detection worker thread is currently running."""
        worker = self._worker
        return worker is not None and worker.is_alive()

    def _worker_loop(self) -> None:
        """Drive :meth:`_worker_tick` until stopped; one iteration never raises out."""
        while not self._stop.is_set():
            try:
                self._worker_tick()
            except Exception:  # noqa: BLE001 — never let the worker die on a bad frame
                logger.warning("FaceSenseDriver worker tick raised; continuing", exc_info=True)
            self._stop.wait(_POLL_INTERVAL)

    def _worker_tick(self) -> None:
        """One worker iteration: cadence-gated detection on the freshest frame.

        The cadence gate is checked FIRST (cheap) so a frame is consumed only when
        a detection is actually due — the freshest available frame is then used,
        and the heavy leg runs at most once per ``detect_interval``.
        """
        if not self._recognizer_ready:
            return
        now = self._clock()
        last = self._last_detect
        if last is not None and (now - last) < self._detect_interval:
            return
        frame = self._input.take()
        if not usable_frame(frame):
            return
        self._last_detect = now
        name = self._detect_once(frame)
        if name is not None:
            self._output.publish(name)

    def _detect_once(self, frame: object) -> str | None:
        """Detect + embed + match one frame -> a known name, or ``None``.

        Every foreign call is guarded: a raise degrades to "no match" and is
        logged, never propagated. No match, or a match with no usable name, is
        ``None`` — only a named, matched face becomes a cue.
        """
        try:
            detection = self._engine.detect(frame)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("FaceSenseDriver detection raised; skipping frame", exc_info=True)
            return None
        if detection is None:
            return None
        try:
            match = self._store.match(detection.embedding)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("FaceSenseDriver store match raised; skipping frame", exc_info=True)
            return None
        if match is None:
            return None
        name = getattr(match, "name", None)
        if not name or not str(name).strip():
            return None  # unknown / unnamed face — never announced by name
        return str(name).strip()

    # ------------------------------------------------------------------ #
    # provider seams                                                     #
    # ------------------------------------------------------------------ #

    def peek_face(self) -> str | None:
        """The current one-tick ``face`` latch — a non-consuming PEEK. Never raises."""
        return self._face

    def as_face_provider(self) -> Callable[[], str | None]:
        """The zero-arg ``face`` provider callable (an alias for :meth:`peek_face`)."""
        return self.peek_face

    def peek_frame_available(self) -> bool:
        """The current TTL-held ``frame_available`` condition. Never raises."""
        return self._frame_available

    def as_frame_available_provider(self) -> Callable[[], bool]:
        """The zero-arg ``frame_available`` provider callable."""
        return self.peek_frame_available

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Stop the detection worker and go inert. Idempotent, bounded join.

        Does NOT close the injected media client — the composition root owns it.
        After ``close()`` every tick is a no-op and both cues read as no reading;
        a worker mid-detection (cv2) may outlive the timed join, but it is a
        daemon thread and dies with the process.
        """
        self._closed = True
        self._face = None
        self._frame_available = False
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=_JOIN_TIMEOUT)

    def __enter__(self) -> "FaceSenseDriver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # defensive reader of the duck-typed TickContext                     #
    # ------------------------------------------------------------------ #

    @staticmethod
    def _now(ctx) -> float | None:  # type: ignore[no-untyped-def]
        """The engine's injected monotonic clock for this tick (``ctx.now``)."""
        now = getattr(ctx, "now", None)
        if not isinstance(now, (int, float)) or isinstance(now, bool):
            return None
        value = float(now)
        return value if value == value and abs(value) != float("inf") else None


__all__ = [
    "FaceSenseDriver",
    "build_face_recognition",
    "usable_frame",
    "DEFAULT_DETECT_INTERVAL",
    "DEFAULT_FRAME_TTL_S",
    "DEFAULT_REANNOUNCE_COOLDOWN",
]
