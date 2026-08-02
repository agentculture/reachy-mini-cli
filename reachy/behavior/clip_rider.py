"""The clip rider — a rolling video clip of the last X seconds (spec claim c18).

Seeing, in the embodiment layer, is two things: the already-shipped ``face`` /
``frame_available`` cues (:mod:`reachy.behavior.face_sense`) telling a rule
"who is here" and "is the camera producing frames" — and a rolling VIDEO CLIP a
worker model can actually watch, so it can answer "what is happening" rather
than "who". Probe evidence (``docs/evidence/2026-08-01-probe-video-wire-
format.md``, task t2) confirmed the deployed lobes gateway accepts a short MP4
as an OpenAI-style ``video_url`` data URI and returns an accurate, streamed
description — so this module's job is narrow: keep the ring, write the file,
publish where it is. Turning the file into a request is a LATER task's job
(the embody turn engine, t10); this module never calls out to a model.

This is a companion to :mod:`reachy.behavior.face_sense`, not a fork of it
--------------------------------------------------------------------------
:class:`~reachy.behavior.face_sense.FaceSenseDriver` is the ONLY thing allowed
to call ``HeldMediaClient.frame()`` (the single-SDK-owner model in
``CLAUDE.md``). Its ``add_frame_sink`` seam (added alongside this module) PUSHES
every usable frame to registered sinks the moment it is read — the same shape
:class:`reachy.cli._commands.behavior._AudioTap`'s ``add_sink`` already uses for
the audio leg. :meth:`ClipRider.offer` is that sink: this module never opens a
media session, never imports ``reachy_mini``, and never calls ``.frame()``
itself. That is what makes "no second camera read" structural rather than a
call-site convention someone could quietly undo in a later refactor.

Threading — zero encoding on the tick thread
----------------------------------------------
Exactly the split :mod:`reachy.behavior.face_sense` documents for its own
detection leg, restated for encoding: the deployed box has already measured
what inline blocking of a heavy per-tick call costs (425-1213 ms tick
overruns, ``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section
3), and a real video encode is exactly that class of work.

* :meth:`ClipRider.offer` (the tick thread, called synchronously from
  :meth:`~reachy.behavior.face_sense.FaceSenseDriver._update_frame`) does
  nothing but stamp a timestamp and append to a bounded, lock-guarded inbox —
  O(1), no cv2, no filesystem I/O, and it NEVER RAISES.
* a BACKGROUND WORKER thread drains that inbox into a time-bounded ring,
  evicts anything older than ``clip_seconds`` (X — see below), and — cadence-
  gated so encoding runs far less often than frames arrive — hands the ring's
  current contents to the injected *encoder* and atomically replaces the one
  clip file on disk.

The worker is spawned only when there is actually an encoder to run (mirroring
:class:`FaceSenseDriver`'s "no recognizer, no thread" rule); with none, a call
to :meth:`offer` is a checked no-op, so a cv2-less box never accumulates frame
references it will never use.

X (``clip_seconds``) — configurable, with a shipped default
-------------------------------------------------------------
:data:`DEFAULT_CLIP_SECONDS` is 6.0 s: double the 3 s / 24-frame clip the t2
probe verified end-to-end (long enough to carry a short gesture or scratch,
short enough that the in-memory ring holds only a few dozen frames at a
webcam's typical cadence, and short enough that a periodic re-encode stays
"now" rather than minutes-stale). Override with the :data:`CLIP_SECONDS_ENV`
environment variable (read by :func:`clip_seconds_from_env`, following the
"composition reads env, the driver takes a plain constructor argument" split
used throughout :mod:`reachy.cli._commands.behavior`) or the ``clip_seconds``
constructor argument directly, for a bench profile or a test.

Bounded retention — memory AND disk
--------------------------------------
Two independent bounds, because they fail for different reasons:

* the RING is evicted by TIME (anything older than ``clip_seconds`` falls off
  the left) — that is what "the last X seconds" means — with a hard frame-count
  cap (:data:`DEFAULT_MAX_RING_FRAMES`) as defence in depth against a
  pathologically fast frame source;
* the CLIP FILE is a single path, OVERWRITE-IN-PLACE: every successful encode
  writes to a temp file under the same directory and ``os.replace``s it onto
  :data:`DEFAULT_CLIP_FILENAME`, exactly the spool's own temp-then-rename
  discipline (``reachy/behavior/control.py``). Disk usage for this module is
  therefore O(1) — one clip, forever — never a ring of numbered files that
  would need its own cleanup policy.

The path lives under :func:`reachy.behavior.control.behavior_dir` — NEVER
``reachy.daemon.state_dir`` directly, even though both resolve the same
directory in production. ``reachy.daemon`` also owns the daemon OS process's
``start``/``stop``; a module that only ever needs a directory path must not
hold a reference to that wider surface (the same containment rule
``reachy/embody/media.py`` was fixed to follow when composing t6 exposed it).

The bus carries a reference, never bytes
-------------------------------------------
The clip's location is published the SAME way every other seam rider
publishes standing state — :class:`~reachy.behavior.sense_availability.
SenseAvailabilityDriver`'s exact mechanism, restated: :meth:`ClipRider.__call__`
(a ``TickBus`` driver) read-modify-writes the ONE ``state.json`` the engine's
own heartbeat writes, merging in a ``"clip"`` key
(:data:`STATE_KEY`) only when it changed. Composition hands every rider the
SAME ``main_control`` spool instance ``cmd_engine_run`` later wraps with
:meth:`~reachy.export.mqtt.NervousPublisher.state_writer`, so this key reaches
the retained ``reachy/state/clip`` bus topic for free — no new publish path,
no second transport, "the existing path" the design brief asked for. The
published value is ``{available, reason, path, ts, duration_s, frame_count}``:
a boolean, a short name, a filesystem path STRING, and three scalars — never
frame bytes, never a data URI. :func:`reachy.export.mqtt.is_text_reference_only`
holds on :meth:`ClipRider.block`'s return value in every state the rider can be
in, and a test asserts this directly.

Degradation — the [vision] extra
-----------------------------------
Writing an actual video clip needs cv2's ``VideoWriter`` (reading raw camera
frames does not — that is why :meth:`~reachy.behavior.face_sense.
FaceSenseDriver.add_frame_sink` fans frames out regardless of whether face
recognition itself is composed). So the ``[vision]`` extra gates the CLIP the
same way it gates face recognition:
:func:`build_clip_encoder` probes with ``importlib.util.find_spec`` exactly
like :func:`reachy.behavior.face_sense.build_face_recognition`, logs ONE
warning (a separate latch from the face driver's — a different fact, a
different fix), and returns ``None``. A rider built with ``encoder=None``
spawns no worker, :meth:`offer` becomes a no-op, and :meth:`block` reports
``available=False`` with a NAMED reason (:data:`VISION_EXTRA_ABSENT` /
:data:`VISION_STACK_UNAVAILABLE`, borrowed verbatim from ``face_sense`` so the
two senses' reasons never drift into synonyms of the same fact) — the same
"the once-only log line is not the only copy of the fact" discipline
``reachy/behavior/sense_availability.py``'s docstring documents for issue #120.

Stdlib plus numpy (already a base dependency, used only via the frames it is
handed — this module never imports it directly). No ``cv2`` at import time
(lazy, inside :func:`build_clip_encoder`, only after the probe confirms it is
importable), no ``reachy_mini``, no network.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from collections import deque
from pathlib import Path
from typing import Any, Callable

from reachy import senselog
from reachy.behavior import control as control_mod
from reachy.behavior import face_sense

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- #
# Senselog identity                                                           #
# --------------------------------------------------------------------------- #

#: The senselog ``stage`` every line from this module carries — shared with
#: ``face_sense``'s modality on purpose (``grep 'stage=vision'`` shows both).
STAGE = "vision"
#: The senselog ``source`` every line from this module carries.
SOURCE = "clip"
#: Fixed senselog ``event`` tag (mirrors ``audio_tee``'s fixed-string events —
#: there is exactly one clip per rider, so a per-call id adds no information).
_EVENT = "clip"

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

#: X — how many seconds of rolling context the ring keeps. See the module
#: docstring's "X (clip_seconds)" section for the justification.
DEFAULT_CLIP_SECONDS: float = 6.0
#: Env override for X.
CLIP_SECONDS_ENV = "REACHY_CLIP_SECONDS"

#: Minimum seconds between two encode attempts on the worker thread. Coarser
#: than face detection's cadence (encoding is heavier, and "the last X
#: seconds" does not need frame-rate freshness on the published reference).
DEFAULT_ENCODE_INTERVAL_S: float = 5.0

#: Tick-thread -> worker bounded handoff. Small: the worker polls fast enough
#: (:data:`_POLL_INTERVAL`) that this rarely holds more than one or two frames.
DEFAULT_INBOX_SIZE: int = 32
#: Hard cap on the ring, independent of time-based eviction — defence in depth
#: against a pathologically fast frame source.
DEFAULT_MAX_RING_FRAMES: int = 256
#: Minimum frames before an encode attempt makes sense at all (need at least
#: two timestamps to infer a duration/fps).
_MIN_FRAMES_TO_ENCODE = 2

#: Worker poll cadence when idle. Bounded so :meth:`ClipRider.close` joins
#: promptly, matching ``face_sense``'s ``_POLL_INTERVAL``.
_POLL_INTERVAL: float = 0.02
#: Bound on how long :meth:`ClipRider.close` waits for the worker thread.
DEFAULT_JOIN_TIMEOUT_S: float = 1.0

#: Fallback / clamp bounds for the inferred playback fps (see :func:`_infer_fps`).
DEFAULT_ENCODE_FPS: float = 8.0
_MIN_FPS: float = 1.0
_MAX_FPS: float = 30.0

#: The one clip file this rider ever writes (overwrite-in-place retention).
DEFAULT_CLIP_FILENAME = "clip.mp4"
_TMP_SUFFIX = ".tmp"

#: The additive top-level ``state.json`` key this rider owns.
STATE_KEY = "clip"

#: NAMED reasons — see the module docstring's "Degradation" section.
#: Borrowed verbatim from ``face_sense`` so the ``[vision]``-absent fact never
#: drifts into two different-looking strings for two senses.
VISION_EXTRA_ABSENT = face_sense.VISION_EXTRA_ABSENT
VISION_STACK_UNAVAILABLE = face_sense.VISION_STACK_UNAVAILABLE
#: NAMED reason: the rider is structurally capable (an encoder exists) but has
#: not produced a clip yet (a normal, transient startup condition — never
#: folded into the two reasons above, which mean "structurally dead").
REASON_NO_CLIP_YET = "no-clip-yet"

#: Process-wide latch for the missing-``[vision]`` warning — this module's OWN
#: latch, separate from ``face_sense``'s: a different capability, a different
#: fix, so a different one-shot fact.
_VISION_WARNED = False


# --------------------------------------------------------------------------- #
# Configuration readers                                                       #
# --------------------------------------------------------------------------- #


def clip_seconds_from_env(env: dict | None = None) -> float:
    """X, read from :data:`CLIP_SECONDS_ENV`, or :data:`DEFAULT_CLIP_SECONDS`.

    A missing, unparsable or non-positive value falls back to the default —
    the same forgiving-parse convention used across this package's env readers
    (e.g. ``reachy.behavior.audio_tee.tee_enabled``): a malformed override must
    never crash composition, only quietly not apply.
    """
    source = os.environ if env is None else env
    raw = source.get(CLIP_SECONDS_ENV)
    if not raw:
        return DEFAULT_CLIP_SECONDS
    try:
        value = float(raw)
    except ValueError:
        return DEFAULT_CLIP_SECONDS
    return value if value > 0 else DEFAULT_CLIP_SECONDS


# --------------------------------------------------------------------------- #
# The [vision] gate — mirrors face_sense.build_face_recognition             #
# --------------------------------------------------------------------------- #


def clip_unavailable_reason(
    encoder_ready: bool, *, find_spec: Callable[[str], object] | None = None
) -> str | None:
    """The NAMED reason the clip rider cannot produce anything, or ``None``.

    Same precedence as :func:`reachy.behavior.face_sense.
    face_recognition_unavailable_reason`: a MISSING extra is reported ahead of
    a failed build, because it is the one an operator can fix with one install.
    """
    reason = face_sense.vision_unavailable_reason(find_spec=find_spec)
    if reason is not None:
        return reason
    return None if encoder_ready else VISION_STACK_UNAVAILABLE


def build_clip_encoder(
    *, find_spec: Callable[[str], object] | None = None
) -> Callable[[list[tuple[float, Any]], float, Path], bool] | None:
    """Build the ``encode(frames, fps, path) -> bool`` callable, or ``None``.

    Returns ``None`` — after exactly one process-wide logged warning — when the
    ``[vision]`` extra (opencv) is absent, which is the default state of a bare
    install, of CI, and of the HTTP remote profile. The import is lazy and
    happens only after the probe succeeds, so this module stays importable
    with no opencv installed.

    The returned callable writes *frames* (a list of ``(timestamp, ndarray)``
    pairs, oldest first) to *path* as an MP4 using ``cv2.VideoWriter`` at
    *fps*, returning whether the writer actually opened and every frame was
    written. It never raises for an ordinary encoding failure (an unopenable
    writer) — only a genuinely unexpected cv2 error propagates, which the
    caller (:meth:`ClipRider._encode_and_publish`) already catches and names.
    """
    global _VISION_WARNED  # noqa: PLW0603 — one process-wide warning, by design
    if face_sense.vision_unavailable_reason(find_spec=find_spec) is not None:
        if not _VISION_WARNED:
            _VISION_WARNED = True
            logger.warning(
                "behavior: clip rider needs the [vision] extra (opencv); the rolling "
                "video clip stays unavailable (install: pip install 'reachy-mini-cli[vision]')"
            )
        return None
    try:
        import cv2  # local: lazy, only after the probe confirms it is importable
    except Exception:  # noqa: BLE001 — a broken vision stack disables the clip, nothing more
        if not _VISION_WARNED:
            _VISION_WARNED = True
            logger.warning(
                "behavior: clip encoder unavailable; the rolling video clip stays unavailable",
                exc_info=True,
            )
        return None

    def _encode(frames: list[tuple[float, Any]], fps: float, path: Path) -> bool:
        if not frames:
            return False
        try:
            height, width = int(frames[0][1].shape[0]), int(frames[0][1].shape[1])
        except (AttributeError, IndexError, TypeError):
            return False
        fourcc = cv2.VideoWriter_fourcc(*"mp4v")
        writer = cv2.VideoWriter(str(path), fourcc, max(_MIN_FPS, float(fps)), (width, height))
        if not writer.isOpened():
            writer.release()
            return False
        try:
            for _, frame in frames:
                writer.write(frame)
        finally:
            writer.release()
        return True

    return _encode


def _infer_fps(frames: list[tuple[float, Any]]) -> float:
    """A defensible playback fps from the ring's own timestamps.

    Frames arrive at whatever cadence the camera + tick loop deliver, not on a
    metronome, so this reads the ACTUAL span rather than assuming one. Fewer
    than two frames, or a non-positive span (a clock that did not advance),
    falls back to :data:`DEFAULT_ENCODE_FPS` — the cadence the t2 probe's clip
    used. Clamped to a sane playback range either way.
    """
    if len(frames) < 2:
        return DEFAULT_ENCODE_FPS
    duration = frames[-1][0] - frames[0][0]
    if duration <= 0:
        return DEFAULT_ENCODE_FPS
    fps = (len(frames) - 1) / duration
    return min(_MAX_FPS, max(_MIN_FPS, fps))


def _unavailable_block(reason: str) -> dict:
    return {
        "available": False,
        "reason": reason,
        "path": None,
        "ts": None,
        "duration_s": None,
        "frame_count": None,
    }


class _Latch:
    """Lock-guarded latest-value holder — a PEEK, not a consuming take.

    Distinct from :class:`reachy.behavior.face_sense._Slot` on purpose: the
    tick-thread reader here wants "the current best-known descriptor", not a
    one-shot event, so a value once published stays visible until replaced.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._value: dict | None = None

    def publish(self, value: dict) -> None:
        with self._lock:
            self._value = value

    def peek(self) -> dict | None:
        with self._lock:
            return self._value


class ClipRider:
    """A ``TickBus`` rider keeping a rolling clip and publishing where it is.

    Construct one with an injected *encoder* (:func:`build_clip_encoder`'s
    result, or ``None`` on a cv2-less box), register :meth:`offer` on a
    :class:`~reachy.behavior.face_sense.FaceSenseDriver` via
    ``face_driver.add_frame_sink(rider.offer)``, register :meth:`__call__` on
    the engine's ``tick_seam``, and call :meth:`close` at teardown. See the
    module docstring for the threading split, the retention scheme and the
    degradation contract.
    """

    def __init__(
        self,
        *,
        encoder: Callable[[list[tuple[float, Any]], float, Path], bool] | None = None,
        clip_seconds: float = DEFAULT_CLIP_SECONDS,
        encode_interval_s: float = DEFAULT_ENCODE_INTERVAL_S,
        inbox_size: int = DEFAULT_INBOX_SIZE,
        max_ring_frames: int = DEFAULT_MAX_RING_FRAMES,
        clock: Callable[[], float] = time.monotonic,
        main_control: control_mod.CommandSpool | None = None,
        root: Path | None = None,
        start_worker: bool = True,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    ) -> None:
        self._encoder = encoder
        self._enabled = encoder is not None
        self._reason = None if self._enabled else clip_unavailable_reason(False)
        self._clip_seconds = max(0.0, float(clip_seconds))
        self._encode_interval_s = max(0.0, float(encode_interval_s))
        self._clock = clock
        self._join_timeout_s = max(0.0, float(join_timeout_s))

        # The SAME root a caller's main_control resolves against — in
        # production both default to state_dir() via behavior_dir(), so the
        # spool and the clip file always live under the same tree.
        self._main = main_control or control_mod.CommandSpool(root=root)
        clip_dir = control_mod.behavior_dir(root)
        self._clip_path = clip_dir / DEFAULT_CLIP_FILENAME
        self._tmp_path = clip_dir / (DEFAULT_CLIP_FILENAME + _TMP_SUFFIX)

        self._inbox: deque = deque(maxlen=max(1, int(inbox_size)))
        self._inbox_lock = threading.Lock()
        self.inbox_dropped = 0

        self._ring: deque = deque()
        self._max_ring_frames = max(1, int(max_ring_frames))
        self._last_encode_at: float | None = None

        self._latest = _Latch()
        self._published: dict | None = None

        self._closed = False
        self._stop = threading.Event()
        self._worker: threading.Thread | None = None
        # No encoder means no heavy leg — so no thread is spawned at all,
        # mirroring FaceSenseDriver's "no recognizer, no worker" rule.
        if start_worker and self._enabled:
            self._worker = threading.Thread(
                target=self._worker_loop, name="behavior-clip-worker", daemon=True
            )
            self._worker.start()

    # ------------------------------------------------------------------ #
    # Frame-sink target (tick thread)                                    #
    # ------------------------------------------------------------------ #

    def offer(self, frame: object) -> None:
        """The frame-sink target — O(1), never raises, never encodes.

        Registered via ``face_driver.add_frame_sink(rider.offer)``. A disabled
        rider (no encoder) is a checked no-op: nothing will ever drain the
        inbox, so there is no reason to hold onto frame references at all.
        """
        if not self._enabled or self._closed:
            return
        try:
            now = self._clock()
            with self._inbox_lock:
                if len(self._inbox) >= self._inbox.maxlen:
                    self.inbox_dropped += 1
                self._inbox.append((now, frame))
        except Exception as err:  # noqa: BLE001 — a sink must never break the tick
            logger.debug("ClipRider: offer() raised (%s); frame not queued", err)

    # ------------------------------------------------------------------ #
    # background worker                                                  #
    # ------------------------------------------------------------------ #

    @property
    def worker_alive(self) -> bool:
        worker = self._worker
        return worker is not None and worker.is_alive()

    @property
    def ring_size(self) -> int:
        """Current ring occupancy — diagnostics / tests."""
        return len(self._ring)

    def _worker_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._worker_tick()
            except Exception:  # noqa: BLE001 — never let the worker die on a bad frame
                logger.warning("ClipRider worker tick raised; continuing", exc_info=True)
            self._stop.wait(_POLL_INTERVAL)

    def _worker_tick(self) -> None:
        """One worker iteration: drain, evict, and (cadence-gated) encode."""
        if not self._enabled:
            return
        self._drain_inbox()
        now = self._clock()
        self._evict_old(now)
        last = self._last_encode_at
        if last is not None and (now - last) < self._encode_interval_s:
            return
        frames = list(self._ring)
        if len(frames) < _MIN_FRAMES_TO_ENCODE:
            return
        self._last_encode_at = now
        self._encode_and_publish(frames, now)

    def _drain_inbox(self) -> None:
        with self._inbox_lock:
            items = list(self._inbox)
            self._inbox.clear()
        self._ring.extend(items)
        while len(self._ring) > self._max_ring_frames:
            self._ring.popleft()

    def _evict_old(self, now: float) -> None:
        cutoff = now - self._clip_seconds
        while self._ring and self._ring[0][0] < cutoff:
            self._ring.popleft()

    def _encode_and_publish(self, frames: list[tuple[float, Any]], now: float) -> None:
        duration = max(0.0, frames[-1][0] - frames[0][0])
        fps = _infer_fps(frames)
        try:
            ok = bool(self._encoder(frames, fps, self._tmp_path))
        except Exception as err:  # noqa: BLE001 — an encoder fault is a named drop, not a crash
            logger.warning("ClipRider: encoder raised (%s); clip not written", err, exc_info=True)
            senselog.drop(STAGE, SOURCE, _EVENT, f"encode-raised ({type(err).__name__}: {err})")
            self._cleanup_tmp()
            return
        if not ok:
            senselog.drop(STAGE, SOURCE, _EVENT, "encode-refused")
            self._cleanup_tmp()
            return
        try:
            os.replace(self._tmp_path, self._clip_path)  # atomic on the same filesystem
        except OSError as err:
            logger.warning("ClipRider: replacing the clip file raised (%s)", err)
            senselog.drop(STAGE, SOURCE, _EVENT, f"replace-failed ({type(err).__name__}: {err})")
            self._cleanup_tmp()
            return
        descriptor = {
            "available": True,
            "reason": None,
            "path": str(self._clip_path),
            "ts": now,
            "duration_s": round(duration, 3),
            "frame_count": len(frames),
        }
        self._latest.publish(descriptor)
        senselog.stage(
            STAGE, SOURCE, _EVENT, f"clip written frames={len(frames)} duration_s={duration:.2f}"
        )

    def _cleanup_tmp(self) -> None:
        try:
            self._tmp_path.unlink()
        except FileNotFoundError:
            pass
        except OSError:  # noqa: BLE001 — cleanup must never raise
            logger.debug("ClipRider: could not remove a stale temp clip file", exc_info=True)

    # ------------------------------------------------------------------ #
    # the state.json rider (tick thread)                                 #
    # ------------------------------------------------------------------ #

    def block(self) -> dict:
        """The current published-shape block. Never raises."""
        if self._reason is not None:
            return _unavailable_block(self._reason)
        descriptor = self._latest.peek()
        if descriptor is None:
            return _unavailable_block(REASON_NO_CLIP_YET)
        return dict(descriptor)

    def __call__(self, ctx=None) -> None:  # noqa: ARG002 - ctx unused; the block is structural
        try:
            self._publish()
        except Exception:  # noqa: BLE001 — a sense tap must never crash the loop
            logger.warning("ClipRider tick raised; skipping this tick", exc_info=True)

    def _publish(self) -> None:
        block = self.block()
        current = self._main.read_state()
        if not isinstance(current, dict):
            current = {}
        if current.get(STATE_KEY) == block:
            return
        merged = dict(current)
        merged[STATE_KEY] = block
        self._main.write_state(merged)
        self._report(block)

    def _report(self, block: dict) -> None:
        previous = self._published
        self._published = dict(block)
        if previous == block:
            return
        if block["available"]:
            senselog.stage(STAGE, SOURCE, _EVENT, f"clip reference published path={block['path']}")
        else:
            senselog.drop(STAGE, SOURCE, _EVENT, block["reason"])

    # ------------------------------------------------------------------ #
    # lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        worker = self._worker
        if worker is not None and worker.is_alive():
            worker.join(timeout=self._join_timeout_s)
        self._cleanup_tmp()

    def __enter__(self) -> "ClipRider":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "ClipRider",
    "build_clip_encoder",
    "clip_seconds_from_env",
    "clip_unavailable_reason",
    "DEFAULT_CLIP_FILENAME",
    "DEFAULT_CLIP_SECONDS",
    "DEFAULT_ENCODE_FPS",
    "DEFAULT_ENCODE_INTERVAL_S",
    "DEFAULT_INBOX_SIZE",
    "DEFAULT_JOIN_TIMEOUT_S",
    "DEFAULT_MAX_RING_FRAMES",
    "REASON_NO_CLIP_YET",
    "STATE_KEY",
    "VISION_EXTRA_ABSENT",
    "VISION_STACK_UNAVAILABLE",
]
