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
* ``face_bbox`` / ``face_age_s`` are the same detection's **position** — a
  TTL-HELD level (:data:`DEFAULT_FACE_BBOX_TTL_S`), like ``frame_available``
  and unlike the name. Three things follow from that split, and all three are
  deliberate: the position survives the per-name re-announce cooldown (a face
  that lingers is exactly the case a gaze behavior needs a position for); an
  UNRECOGNISED face still has one (``face_bbox`` set while ``face`` is
  ``None`` — before this, an unmatched detection was discarded whole and a
  stranger looking at the robot produced no reading at all); and when several
  faces share a frame, :func:`select_face` — pure, and unit-tested as such —
  names which one it is: a recognised face first, biggest among equals.
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

So the split mirrors the retired ``listen_face.FaceHook``'s, restated for
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
Frame fan-out — a PUSH seam for a second in-process consumer
--------------------------------------------------------------------------
This driver is the ONLY thing in the runtime allowed to call
:meth:`HeldMediaClient.frame`: any other piece that wants to see camera frames
(the clip rider, a future consumer) must never open a second read against the
one held media client — that is exactly the single-SDK-owner contention this
module's docstring already warns about for the SDK's media session generally.
:meth:`add_frame_sink` registers a zero-arg-return callable that
:meth:`_update_frame` PUSHES every USABLE frame to, the moment it is read — the
same shape :class:`reachy.cli._commands.behavior._AudioTap`'s ``add_sink`` uses
for the audio leg. Push rather than a second peek is what makes "no second
camera read" structural rather than a call-site convention: a sink cannot be
wired to anything but this one read. A sink is called with the frame the tick
already validated (never ``None`` or a degenerate array — :func:`usable_frame`
gates it first), runs UNCONDITIONALLY of whether a face recognizer is
composed (a cv2-less-but-still-camera'd box can still feed a sink that has its
own reason to want frames), and its faults are swallowed here: a misbehaving
sink degrades to a debug log line, never a tick fault.

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

That once-only warning is the right LOG policy and it stays exactly as it was.
Issue #120 is what happens when it is the ONLY copy of the fact: the deployed
box lacked the extra, logged its one line at boot, and six hours of journal
afterwards held nothing distinguishing "no face has been in view" from "this
sense has been dead since boot". So the *reason* is now also readable as a
value — :func:`vision_unavailable_reason` and
:func:`face_recognition_unavailable_reason` return the NAMED strings
:data:`VISION_EXTRA_ABSENT` / :data:`VISION_STACK_UNAVAILABLE`, which
:mod:`reachy.behavior.sense_availability` publishes into the runtime's standing
``state.json``. Same discipline as ``senselog.drop``: a dead sense always names
its reason.

Every other failure degrades the same way, never raising out of the driver or a
provider (mirroring :class:`reachy.behavior.pat_sense.PatSenseDriver` and
:func:`reachy.behavior.sense._peek`): a raising media client, a raising
``camera_available``, a raising detector or store, a closed driver — each is "no
reading" for that tick.

--------------------------------------------------------------------------
Camera-stream-ended staleness (issue #138) — DETECT ONLY
--------------------------------------------------------------------------
Live evidence, 2026-08-02: the daemon's GStreamer pipeline EOS'd and no camera
frame arrived again for **1h45m**, while the runtime kept ticking, rules kept
firing, and ``state.json`` stayed fresh — nothing distinguished "the room is
quiet" from "the camera is dead" until an operator noticed by eye and
restarted the service by hand.

The connection-level signal cannot see this: ``HeldMediaClient.connected``
stays ``True`` across a dead pipeline (only the daemon's own supervisor
would notice the process exited, and it did not exit), so a supervisor
polling ``connected`` to decide whether to re-warm never fires. The only
honest signal left is **how long ago the last USABLE frame arrived**, so
:meth:`FaceSenseDriver._check_stream_staleness` watches
:attr:`_last_frame_at` directly, on every read attempt where the client still
claims to be connected AND camera-available (see :meth:`_update_frame`).
:data:`DEFAULT_STREAM_STALE_S` is ten times :data:`DEFAULT_FRAME_TTL_S` and a
hundred times :data:`DEFAULT_FRAME_INTERVAL_S` — comfortably past either
magnitude, so ordinary TTL flicker or single-SDK-owner contention (which
still throttles to ~1 fps, never to zero) can never trip it, while it still
names a dead stream in well under a minute rather than the 1h45m an operator
went unwarned.

A camera that **legitimately never existed** — no camera at all, or one that
has simply never produced a single usable frame yet — must never trip this:
:attr:`_last_frame_at` starts ``None`` and only a real frame ever sets it, so
"stale since the beginning of time" is structurally impossible to reach. The
drop is emitted through :mod:`reachy.senselog` exactly once per silent
episode (a fresh usable frame clears the latch, so a LATER episode is
reported again, mirroring :class:`reachy.speech.realtime._SessionState`'s
``session-down``/recovered discipline) — never once per tick, which at 50 Hz
would flood the journal for as long as the pipeline stays dead.

This is **detection only**, a deliberate boundary (spec claim c21): nobody
has probed whether a GStreamer EOS is recoverable in-process at all, so
:meth:`_check_stream_staleness` does nothing but read state and call
:func:`reachy.senselog.drop` — it never touches :meth:`HeldMediaClient.warm_up`,
never constructs a client, and never signals the composition root to rebuild
or restart anything.

Stdlib plus numpy (already a base dependency, used only for the frame-shape
guard). No cv2 at import time, no ``reachy_mini``, no transport: the engine and
store are injected, and :func:`build_face_recognition` imports the real ones
lazily only after confirming opencv is present.
"""

from __future__ import annotations

import logging
import os
import threading
import time
from dataclasses import dataclass
from importlib.util import find_spec as _find_spec
from typing import Any, Callable

import numpy as np

from reachy import senselog

logger = logging.getLogger(__name__)

#: Senselog ``stage`` every line from this module carries — shared with
#: ``clip_rider``'s modality on purpose (``grep 'stage=vision'`` shows both).
STAGE = "vision"
#: Senselog ``source`` every line from this module carries.
SOURCE = "face"

#: Minimum wall-clock gap (seconds) between two detections on the worker thread.
#: YuNet+SFace is heavy; ~2 Hz is ample for catching a face entering the frame,
#: and matches the retired ``listen_face.DEFAULT_DETECT_INTERVAL``.
DEFAULT_DETECT_INTERVAL: float = 0.5

#: ``REACHY_FACE_DETECT_MAX_WIDTH`` off value — no downscaling. Matches the
#: constructor default and reads exactly like "0 pixels" would mean anything
#: else: it is the explicit off switch, not a real width.
DEFAULT_DETECT_MAX_WIDTH: int = 0

#: Per-name re-announce cooldown (seconds): the same name latches into ``face``
#: at most once per this window. Without it, a face that simply stays in view
#: would re-fire its rule every detection cycle.
DEFAULT_REANNOUNCE_COOLDOWN: float = 30.0

#: Minimum seconds between ``media.frame()`` reads on the tick thread.
#: 10 Hz: above the fastest real consumer (the rolling clip's
#: :data:`~reachy.behavior.clip_rider.DEFAULT_ENCODE_FPS`, 8 fps) and an order of
#: magnitude inside :data:`DEFAULT_FRAME_TTL_S`, so a held ``frame_available``
#: can never go stale between reads. Reading once per tick instead cost a
#: sustained ~5% tick-budget overrun for as long as frames flowed — see issue
#: #145 and ``docs/evidence/2026-08-02-t8-tick-overrun-attribution.md``.
DEFAULT_FRAME_INTERVAL_S: float = 0.1

#: How long a successfully read frame keeps ``frame_available`` true (seconds).
#: The camera is slower than the 50 Hz tick and a steady-state read returns
#: ``None`` whenever nothing is ready, so the condition is held rather than
#: pulsed — 1 s is many camera periods but still collapses promptly when the
#: camera genuinely stops.
DEFAULT_FRAME_TTL_S: float = 1.0

#: How long the last USABLE frame may go unrefreshed — while the injected
#: client still reports itself connected and camera-available — before the
#: pipeline is presumed to have died silently (issue #138). Ten times
#: :data:`DEFAULT_FRAME_TTL_S` and a hundred times
#: :data:`DEFAULT_FRAME_INTERVAL_S`: comfortably past either magnitude, so
#: ordinary TTL flicker or single-SDK-owner contention (still ~1 fps, never
#: zero) can never trip it, while it still names a dead stream in well under a
#: minute against the 1h45m an operator went unwarned live on 2026-08-02. See
#: the module docstring's "Camera-stream-ended staleness" section.
DEFAULT_STREAM_STALE_S: float = 10.0

#: NAMED reason for the latched drop :meth:`FaceSenseDriver._check_stream_staleness`
#: emits: frames were flowing and then stopped while the client still claims to
#: be there. DETECT ONLY — see the module docstring; nothing on this path
#: constructs, rebuilds or restarts a media client or pipeline.
REASON_STREAM_ENDED = "camera-stream-ended"

#: How long a face POSITION reading (``face_bbox``/``face_age_s``) is held
#: before it expires (seconds). The position is a HELD level, not a one-tick
#: event — a gaze behavior needs it on every tick, not on the one tick a
#: detection happened to land — but a held reading with no expiry is a lie the
#: moment the person walks away: the robot would keep staring at where a face
#: used to be. Several detection cycles wide (``DEFAULT_DETECT_INTERVAL`` is
#: 0.5 s) so an ordinary missed detection never blanks it, and short enough
#: that a departed face stops being "there" in about a second.
DEFAULT_FACE_BBOX_TTL_S: float = 1.5

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

#: NAMED reason: the ``[vision]`` extra (opencv) is not installed at all. The
#: string an operator can act on — ``pip install 'reachy-mini-cli[vision]'`` —
#: and the exact value issue #120's deployed box would have reported.
VISION_EXTRA_ABSENT = "vision-extra-absent"

#: NAMED reason: opencv IS importable but the recognizer pair could not be
#: built (a broken vision stack, missing model files, ...). A *different* fact
#: from the one above and a different fix, so it gets its own name rather than
#: being folded into it.
VISION_STACK_UNAVAILABLE = "vision-stack-unavailable"


def vision_unavailable_reason(find_spec: Callable[[str], object] | None = None) -> str | None:
    """:data:`VISION_EXTRA_ABSENT` when opencv is absent, else ``None``.

    The probe half of the degradation contract, exposed as a VALUE so the fact
    outlives the one-shot boot warning (issue #120 — see the module docstring).
    Pure: no import of cv2, no side effect, no latch consulted or set, so it is
    equally correct before :func:`build_face_recognition` has ever run and long
    after its warning has been spent.

    ``find_spec`` is the injectable seam (a test drives both directions with no
    install); ``None`` resolves the module-level probe **at call time**, so
    monkeypatching ``face_sense._find_spec`` works too. A default ARGUMENT would
    bind the function object at definition time and silently ignore the
    monkeypatch — the same trap ``EngagementClassifier``'s ``complete_fn=None``
    avoids.
    """
    probe = _find_spec if find_spec is None else find_spec
    return VISION_EXTRA_ABSENT if probe("cv2") is None else None


def face_recognition_unavailable_reason(
    recognizer_ready: bool, *, find_spec: Callable[[str], object] | None = None
) -> str | None:
    """The NAMED reason the ``face`` cue cannot recognise anyone, or ``None``.

    Precedence is deliberate and load-bearing: a MISSING extra is reported ahead
    of a failed build, because the missing extra is *why* the build failed and is
    the only one of the two an operator can fix with one install. ``[vision]``
    present but ``recognizer_ready`` false is then a genuinely different fault
    and says so.

    :param recognizer_ready: whether :func:`build_face_recognition` returned a
        pair (the composition root knows; this module deliberately does not
        cache it, so no stale answer can survive a rebuild).
    """
    reason = vision_unavailable_reason(find_spec=find_spec)
    if reason is not None:
        return reason
    return None if recognizer_ready else VISION_STACK_UNAVAILABLE


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
    except Exception:  # an unconvertible object is just not a frame
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
    global _VISION_WARNED  # one process-wide warning, by design
    # The SAME probe the availability block reads, so the boot log and the
    # standing `state.json` reason can never disagree about why face is dead.
    if vision_unavailable_reason() is not None:
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
    except Exception:  # a broken vision stack disables the cue, nothing more
        if not _VISION_WARNED:
            _VISION_WARNED = True
            logger.warning(
                "behavior: face recognition unavailable; the `face` sense stays unavailable",
                exc_info=True,
            )
        return None
    return (engine, store)


def detect_interval_from_env(
    value: str | None = None, *, default: float = DEFAULT_DETECT_INTERVAL
) -> float:
    """Parse ``REACHY_FACE_DETECT_INTERVAL`` into a positive seconds float.

    Mirrors ``transcript_sense._env_truthy``'s style — a small, unit-testable
    stdlib helper that never raises: unset, empty, unparsable, zero, or
    negative all fall back to *default* (:data:`DEFAULT_DETECT_INTERVAL`) so an
    operator who never sets the var, or sets it to garbage, gets exactly
    today's behaviour rather than a startup crash.

    *value* is the string to parse; when omitted, it is read from
    ``os.environ["REACHY_FACE_DETECT_INTERVAL"]`` — the seam a test can bypass
    by passing a string directly.
    """
    if value is None:
        value = os.environ.get("REACHY_FACE_DETECT_INTERVAL")
    if value is None:
        return default
    text = value.strip()
    if not text:
        return default
    try:
        parsed = float(text)
    except ValueError:
        return default
    if parsed != parsed or abs(parsed) == float("inf") or parsed <= 0.0:
        return default
    return parsed


def detect_max_width_from_env(
    value: str | None = None, *, default: int = DEFAULT_DETECT_MAX_WIDTH
) -> int:
    """Parse ``REACHY_FACE_DETECT_MAX_WIDTH`` into a non-negative pixel width.

    Same fail-open shape as :func:`detect_interval_from_env`: unset, empty,
    unparsable, or negative all fall back to *default*
    (:data:`DEFAULT_DETECT_MAX_WIDTH`, ``0`` — off, no downscaling) rather than
    raising. ``0`` itself is a valid, meaningful setting (explicitly off), so
    it is returned as-is rather than treated as "unset".

    *value* is the string to parse; when omitted, it is read from
    ``os.environ["REACHY_FACE_DETECT_MAX_WIDTH"]`` — the seam a test can
    bypass by passing a string directly.
    """
    if value is None:
        value = os.environ.get("REACHY_FACE_DETECT_MAX_WIDTH")
    if value is None:
        return default
    text = value.strip()
    if not text:
        return default
    try:
        parsed = int(text)
    except ValueError:
        return default
    if parsed < 0:
        return default
    return parsed


def downscale_for_detection(frame: object, max_width: int | None) -> object:
    """Downscale *frame* to at most *max_width* pixels wide, aspect preserved.

    Returns *frame* UNCHANGED — the SAME object, no copy — whenever there is
    nothing to do: *max_width* is falsy (``None`` or ``<= 0``, the default
    "off" state) or the frame is already at or narrower than *max_width*.
    Otherwise resizes with ``cv2.resize`` / ``cv2.INTER_AREA``, height scaled
    to preserve aspect ratio.

    ``cv2`` is imported LAZILY, inside this function, so the module stays
    importable with no ``[vision]`` extra installed — exactly like
    :func:`build_face_recognition`'s imports. If cv2 turns out to be missing
    (or the resize itself raises), the frame passes through unscaled rather
    than raising: this is a throughput knob, never a hard dependency.

    Two consequences follow for the caller, both intentional:

    * The face engine's ``bbox_norm`` is normalised to the frame it actually
      saw, so the published ``face_bbox`` needs NO rescaling back to the
      original frame regardless of what this helper did — a fraction of a
      640-wide frame is the same fraction of a 1280-wide one.
    * The face EMBEDDING is computed on the (possibly downscaled) frame handed
      to the engine — a smaller face crop is a real recognition-quality
      trade-off, not a free lunch, and the operator setting
      ``REACHY_FACE_DETECT_MAX_WIDTH`` is choosing that trade explicitly.
    """
    if not max_width or max_width <= 0:
        return frame
    try:
        shape = frame.shape  # type: ignore[attr-defined]
        height, width = int(shape[0]), int(shape[1])
    except Exception:
        return frame
    if width <= max_width:
        return frame
    try:
        import importlib

        cv2 = importlib.import_module("cv2")
    except Exception:
        return frame
    scale = max_width / float(width)
    new_height = max(1, int(round(height * scale)))
    try:
        return cv2.resize(frame, (int(max_width), new_height), interpolation=cv2.INTER_AREA)
    except Exception:
        return frame


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


@dataclass(frozen=True)
class FaceCandidate:
    """One detected face as SELECTION sees it: where it is, and who it is.

    ``bbox`` is ``(x, y, w, h)`` normalised to the frame (``None`` when the
    detector gave no box), ``name`` the store's match for it (``None`` for an
    unknown or unnamed face — which is a perfectly good candidate here; only
    the ``face`` NAME cue insists on a name).
    """

    bbox: tuple[float, float, float, float] | None
    name: str | None = None


@dataclass(frozen=True)
class FaceObservation:
    """What one detection cycle publishes from the worker to the tick thread.

    Carries the chosen face's ``name`` (or ``None``) AND its ``bbox`` — the
    pair is the whole point of t2: before it, an unmatched detection was
    discarded entirely and a stranger looking at the robot produced no reading
    at all.
    """

    name: str | None = None
    bbox: tuple[float, float, float, float] | None = None


def bbox_area(bbox: tuple[float, float, float, float] | None) -> float:
    """Normalised area of an ``(x, y, w, h)`` box; a missing/degenerate box is 0."""
    if bbox is None:
        return 0.0
    return max(0.0, float(bbox[2])) * max(0.0, float(bbox[3]))


def select_face(candidates) -> "FaceCandidate | None":
    """Pick the one face a behavior should care about; ``None`` for none at all.

    PURE — no clock, no state, no I/O — so the policy can be unit-tested over a
    plain list and the driver just applies it.

    The rule, in order:

    1. **A recognised face wins over any unrecognised face, regardless of
       size.** The operator decision behind issue #175: "any face qualifies,
       but prefer an enrolled one" — a stranger standing closer to the camera
       than a known person no longer steals the robot's attention.
    2. **Among faces with the same recognition status, biggest wins.** Box
       area is the only distance proxy the detector gives, and the nearest
       face is the one a person expects a robot to look at; ties resolve to
       the first candidate the detector reported (a stable, deterministic
       order).

    A candidate with no box (no position to report) never outranks one with a
    box, named or not — see issue #127: an unnamed face still needs a
    position, so "any face qualifies" is preserved even for a single,
    completely unknown face in frame. Among BOXLESS candidates the same
    recognised-first rule applies, so a name cue is never lost to an unnamed
    detection that merely came first (before this ordering was explicit, two
    boxless detections resolved to whichever the detector reported first).

    The whole rule is ONE sort key — ``(has a box, is named, area)`` — so the
    three tiers cannot drift apart as separate filters.
    """
    ranked = [
        (
            candidate.bbox is not None,
            bool((candidate.name or "").strip()),
            bbox_area(candidate.bbox),
        )
        for candidate in candidates
    ]
    if not ranked:
        return None
    best = max(range(len(ranked)), key=lambda index: ranked[index])
    return list(candidates)[best]


def to_xywh(bbox_norm) -> "tuple[float, float, float, float] | None":
    """Corner-form ``(x1, y1, x2, y2)`` -> ``(x, y, w, h)``; junk -> ``None``.

    :class:`reachy.vision.face.FaceDetection` reports CORNERS, while
    :attr:`reachy.behavior.sense.Sense.face_bbox` carries origin + size — the
    shape a consumer that wants a face's CENTRE (``x + w/2``) needs without
    re-deriving it. The conversion happens once, here, so neither side has to
    know the other's convention. A missing or malformed box is a non-reading,
    never a raise.
    """
    if bbox_norm is None or isinstance(bbox_norm, (str, bytes)):
        return None
    try:
        x1, y1, x2, y2 = bbox_norm
        box = (float(x1), float(y1), float(x2) - float(x1), float(y2) - float(y1))
    except (TypeError, ValueError):
        return None
    if not all(np.isfinite(value) for value in box):
        return None
    return box


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
    detect_max_width:
        When set (pixels) and the frame handed to the engine is wider, it is
        downscaled first (aspect preserved) via
        :func:`downscale_for_detection`. ``None`` or ``0`` (the default) means
        no downscaling — the frame the engine sees is exactly the frame read
        off the media client. See that function's docstring for the bbox / embedding
        consequences.
    reannounce_cooldown:
        Minimum seconds between two ``face`` latches for the SAME name, measured
        on the engine's tick clock ``ctx.now``. Default
        :data:`DEFAULT_REANNOUNCE_COOLDOWN`.
    frame_ttl_s:
        How long a read frame holds ``frame_available`` true. Default
        :data:`DEFAULT_FRAME_TTL_S`.
    face_bbox_ttl_s:
        How long a face POSITION reading (``face_bbox``/``face_age_s``) is held
        after its detection before it expires. Default
        :data:`DEFAULT_FACE_BBOX_TTL_S`.
    stream_stale_s:
        How long the last usable frame may go unrefreshed, while the client
        still claims to be connected and camera-available, before a latched
        :data:`REASON_STREAM_ENDED` drop is emitted (issue #138; see the
        module docstring's "Camera-stream-ended staleness" section). Default
        :data:`DEFAULT_STREAM_STALE_S`. DETECT ONLY — never triggers a
        reconnect or rebuild.
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
        detect_max_width: int | None = None,
        reannounce_cooldown: float = DEFAULT_REANNOUNCE_COOLDOWN,
        frame_ttl_s: float = DEFAULT_FRAME_TTL_S,
        frame_interval_s: float = DEFAULT_FRAME_INTERVAL_S,
        face_bbox_ttl_s: float = DEFAULT_FACE_BBOX_TTL_S,
        stream_stale_s: float = DEFAULT_STREAM_STALE_S,
        clock: Callable[[], float] = time.monotonic,
        start_worker: bool = True,
    ) -> None:
        self._media = media
        self._engine = engine
        self._store = store
        self._detect_interval = max(0.0, float(detect_interval))
        self._detect_max_width = int(detect_max_width) if detect_max_width else 0
        self._reannounce_cooldown = max(0.0, float(reannounce_cooldown))
        self._frame_ttl_s = max(0.0, float(frame_ttl_s))
        self._frame_interval_s = max(0.0, float(frame_interval_s))
        self._face_bbox_ttl_s = max(0.0, float(face_bbox_ttl_s))
        self._stream_stale_s = max(0.0, float(stream_stale_s))
        self._last_read_at: float | None = None
        self._clock = clock

        #: Tick thread -> worker: the latest validated frame to detect on.
        self._input = _Slot()
        #: Worker -> tick thread: the latest matched, named face.
        self._output = _Slot()
        #: PUSH consumers of every usable frame (see the module docstring's
        #: "Frame fan-out" section) — the clip rider registers here.
        self._frame_sinks: list[Callable[[object], None]] = []
        #: Worker-thread-only: clock reading of the last detection (cadence gate).
        self._last_detect: float | None = None
        #: Tick-thread-only: name -> ``ctx.now`` of its last latch (cooldown).
        self._last_announced: dict[str, float] = {}
        #: The ONE-TICK ``face`` latch (see the module docstring).
        self._face: str | None = None
        #: The TTL-HELD face POSITION and the tick-clock reading it landed at.
        #: Held, not pulsed: a gaze behavior needs a position on EVERY tick,
        #: and it is kept independently of ``_face`` so the per-name
        #: re-announce cooldown (a NAME-cue concern) can never freeze it.
        self._face_bbox: tuple[float, float, float, float] | None = None
        self._face_bbox_at: float | None = None
        self._face_age_s: float | None = None
        #: The TTL-held ``frame_available`` condition and its freshness anchor.
        self._frame_available = False
        self._last_frame_at: float | None = None
        #: One-episode latch for the #138 ``camera-stream-ended`` drop — cleared
        #: the moment a fresh usable frame arrives, so a LATER silent episode is
        #: reported again rather than only the first one for the process's life.
        self._stream_ended_logged = False
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
        except Exception:  # a sense tap must never crash the loop
            logger.warning("FaceSenseDriver tick raised; face cue dropped", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """The tick-thread body: O(1) frame publish + result drain, never detection."""
        if self._closed:
            self._frame_available = False
            self._clear_position()
            return
        now = self._now(ctx)
        self._update_frame(now)
        self._drain_match(now)
        self._age_position(now)

    # -- frame leg ------------------------------------------------------ #

    def _update_frame(self, now: float | None) -> None:
        """Peek one frame, gate it (#73), publish it, and refresh the condition."""
        if not self._read_due(now):
            # Not due: hold the condition on its TTL rather than re-reading. The
            # interval is many times shorter than the TTL, so a held condition is
            # never stale — see :meth:`_read_due`.
            self._frame_available = self._within_ttl(now)
            return
        if not self._connected():
            # NO CLIENT IS HELD — not warmed yet, dropped, or mid-backoff. That
            # is a different condition from a camera whose stream ended, and the
            # two must not share a name: a reconnect window would otherwise
            # report `camera-stream-ended` every time, which is precisely the
            # kind of misleading diagnosis #138 exists to remove.
            self._frame_available = False
            return
        if not self._camera_available():
            # A client IS held and the camera is gone beneath it. On a
            # camera-less robot that is the ordinary resting state; after a
            # stream has run it is the #138 failure, and measured on the
            # deployed box it is the shape a died GStreamer pipeline takes —
            # the daemon reports camera_available FALSE, which is why a detector
            # watching only the believed-present path never fired on it.
            #
            # `_last_frame_at` is deliberately NOT cleared: it is the evidence a
            # stream existed, and clearing it would re-exempt the loss as "a
            # camera that never was". One that genuinely never existed still has
            # it `None`, so that exemption is untouched.
            self._frame_available = False
            self._check_stream_staleness(now)
            return

        # Stamped only where a read is actually attempted: a disconnected or
        # camera-less tick must not consume the interval, or a client that comes
        # back would wait one out before its first frame.
        self._last_read_at = now
        frame = self._read_frame()
        if usable_frame(frame):
            self._last_frame_at = now
            self._frame_available = True
            # A real frame ends any silent episode — the NEXT one gets its own
            # report rather than being swallowed by an old latch (#138).
            self._stream_ended_logged = False
            self._fan_out_frame(frame)
            if self._recognizer_ready:
                self._input.publish(frame)
            return

        # A None or degenerate frame is a NON-READING, never a fault (#73): the
        # condition simply rides its TTL until frames genuinely stop.
        self._frame_available = self._within_ttl(now)
        self._check_stream_staleness(now)

    def _read_due(self, now: float | None) -> bool:
        """Whether enough time has passed since the last ``frame()`` read.

        The read is the one leg of this driver that runs ON the tick thread, and
        it used to run every tick — 50 Hz against consumers that need at most 8
        (issue #145). Measured on the deployed box, that sustained the whole 20 ms
        budget about 5% over for as long as frames flowed
        (``docs/evidence/2026-08-02-t8-tick-overrun-attribution.md``).

        A clock-less tick reads every time: without ``ctx.now`` there is no
        interval to measure, and declining to read would silence the sense.
        """
        if now is None or self._frame_interval_s <= 0.0:
            return True
        last = self._last_read_at
        if last is None:
            return True
        # A clock that jumped backwards reads now rather than waiting it out.
        return not (0.0 <= now - last < self._frame_interval_s)

    def _within_ttl(self, now: float | None) -> bool:
        """Whether the last good frame is still recent enough to hold the condition."""
        last = self._last_frame_at
        if last is None:
            return False
        if now is None or self._frame_ttl_s <= 0.0:
            return False
        elapsed = now - last
        return 0.0 <= elapsed <= self._frame_ttl_s

    def _check_stream_staleness(self, now: float | None) -> None:
        """Latch :data:`REASON_STREAM_ENDED` once frames stop for :data:`DEFAULT_STREAM_STALE_S`.

        DETECT ONLY (issue #138): reads :attr:`_last_frame_at` and calls
        :func:`reachy.senselog.drop` — nothing more. It never touches
        ``warm_up``, never constructs a client, never reconnects, rebuilds or
        restarts a pipeline; see the module docstring's "Camera-stream-ended
        staleness" section for why that boundary is deliberate here.

        Only reached from the branch of :meth:`_update_frame` where the client
        still claims to be connected and camera-available but the read produced
        no usable frame — the camera is BELIEVED present, which is what makes a
        long silence meaningful rather than an ordinary camera-less reading.
        ``_last_frame_at is None`` (no frame has EVER arrived — a camera that
        never existed, never streamed, or is still warming up) is exempt by
        construction: there is no "stream" to have ended.
        """
        if self._stream_ended_logged or now is None:
            return
        last = self._last_frame_at
        if last is None:
            return
        if now - last < self._stream_stale_s:
            return
        self._stream_ended_logged = True
        senselog.drop(STAGE, SOURCE, "stream", REASON_STREAM_ENDED)

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
        except Exception:  # a raising probe is not a liveness verdict
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
        except Exception:  # a raising probe is not a camera verdict
            logger.debug("FaceSenseDriver camera probe raised; assuming a camera", exc_info=True)
            return True

    def _read_frame(self) -> object | None:
        """One non-blocking frame peek off the injected client; a raise is no frame."""
        media = self._media
        if media is None:
            return None
        try:
            return media.frame()  # type: ignore[attr-defined]
        except Exception:  # a read fault degrades, never propagates
            logger.debug("FaceSenseDriver frame read raised; no frame this tick", exc_info=True)
            return None

    def add_frame_sink(self, sink: Callable[[object], None]) -> None:
        """Register a PUSH consumer of every usable frame (module docstring).

        Called once at composition, e.g. ``face_driver.add_frame_sink(
        clip_rider.offer)``. Never called on the tick thread itself.
        """
        self._frame_sinks.append(sink)

    def _fan_out_frame(self, frame: object) -> None:
        """Push *frame* to every registered sink — O(1), never raises into the tick.

        Mirrors :meth:`reachy.cli._commands.behavior._AudioTap.pull`'s sink
        fan-out: a sink is called with the frame the moment it is read, so a
        consumer can never be wired to anything but THIS read — no second
        ``media.frame()`` call, no second camera contention. A misbehaving sink
        degrades to a debug log line, never a tick fault.
        """
        for sink in self._frame_sinks:
            try:
                sink(frame)
            except Exception as err:  # a fan-out consumer must never break the tick
                logger.debug("FaceSenseDriver: frame sink raised (%s); frame not delivered", err)

    # -- match leg ------------------------------------------------------ #

    def _drain_match(self, now: float | None) -> None:
        """Take the worker's latest observation: hold its POSITION, latch its NAME.

        The two halves are deliberately independent. The position is refreshed
        unconditionally — it is a level, and the thing a gaze behavior reads
        every tick — while the name still obeys the per-name re-announce
        cooldown that keeps a lingering face from re-firing its rule. Before
        t2 the cooldown's early return dropped the whole result, which would
        have blanked the position for the entire 30 s window on exactly the
        case that matters most: someone standing there, being looked at.
        """
        result = self._output.take()
        if result is None:
            return
        observation = self._as_observation(result)
        if observation is None:
            return
        if observation.bbox is not None:
            self._face_bbox = observation.bbox
            self._face_bbox_at = now
            self._face_age_s = 0.0 if now is not None else None
        name = (observation.name or "").strip()
        if not name:
            return
        if now is not None:
            last = self._last_announced.get(name)
            if last is not None and 0.0 <= (now - last) < self._reannounce_cooldown:
                return
            self._last_announced[name] = now
        self._face = name
        self.events += 1

    @staticmethod
    def _as_observation(result: object) -> "FaceObservation | None":
        """Normalise whatever the worker published into a :class:`FaceObservation`.

        A bare string is still accepted — that is what this slot carried before
        t2, and a name with no box is exactly what a detector that reports no
        bbox produces — so no publisher has to be updated in lockstep.
        """
        if isinstance(result, FaceObservation):
            return result
        if isinstance(result, str):
            name = result.strip()
            return FaceObservation(name=name) if name else None
        return None

    def _age_position(self, now: float | None) -> None:
        """Re-age the held position each tick and expire it past its TTL.

        Age is measured on the ENGINE'S tick clock (``ctx.now``) at the moment
        the observation was drained, never on the worker's own clock: mixing
        two clocks in one reading is how a "seconds ago" ends up meaningless.
        The drain happens on the tick after the detection completes, so the
        reading is late by at most one tick — far inside anything a motion
        behavior can act on.
        """
        anchor = self._face_bbox_at
        if self._face_bbox is None or anchor is None or now is None:
            return
        age = now - anchor
        if age < 0.0 or age > self._face_bbox_ttl_s:
            # Past the TTL (or a clock that jumped backwards): a held position
            # that is no longer true must read as NO reading, not as a stale one.
            self._clear_position()
            return
        self._face_age_s = age

    def _clear_position(self) -> None:
        """Drop the held position reading — no bbox, no age."""
        self._face_bbox = None
        self._face_bbox_at = None
        self._face_age_s = None

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
            except Exception:  # never let the worker die on a bad frame
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
        observation = self._detect_once(frame)
        if observation is not None:
            self._output.publish(observation)

    def _detect_once(self, frame: object) -> "FaceObservation | None":
        """Detect + embed + match one frame -> the chosen face, or ``None``.

        Returns the SELECTED face's position and (when the store knows it) its
        name. An unmatched face is a real observation here — it has a position,
        which is what a gaze behavior needs — and only the ``face`` NAME cue
        goes on insisting on a named, matched face. ``None`` means nothing
        usable was seen at all: no detection, or a detection with neither a box
        nor a name.

        Every foreign call is guarded: a raise degrades to "no observation" and
        is logged, never propagated.

        The frame is downscaled here, ONCE, before it reaches the engine — see
        :func:`downscale_for_detection` for why the resulting ``bbox_norm``
        needs no rescaling and why the embedding trade-off is real.
        """
        frame = downscale_for_detection(frame, self._detect_max_width)
        detections = self._detect_faces(frame)
        if not detections:
            return None
        candidates = [
            FaceCandidate(
                bbox=to_xywh(getattr(detection, "bbox_norm", None)),
                name=self._match_name(detection),
            )
            for detection in detections
        ]
        chosen = select_face(candidates)
        if chosen is None or (chosen.bbox is None and not chosen.name):
            return None
        return FaceObservation(name=chosen.name, bbox=chosen.bbox)

    def _detect_faces(self, frame: object) -> list:
        """Every face in *frame*, via ``detect_all`` when the engine offers it.

        ``detect_all`` is what makes SELECTION possible at all — a single
        largest-face ``detect`` cannot express "prefer the person I recognise".
        An engine that only has ``detect`` (an older build, a test fake) still
        works and simply yields at most one candidate.
        """
        engine = self._engine
        if engine is None:
            return []
        detect_all = getattr(engine, "detect_all", None)
        try:
            if callable(detect_all):
                return [d for d in (detect_all(frame) or []) if d is not None]
            detection = engine.detect(frame)  # type: ignore[attr-defined]
        except Exception:
            logger.warning("FaceSenseDriver detection raised; skipping frame", exc_info=True)
            return []
        return [] if detection is None else [detection]

    def _match_name(self, detection: object) -> str | None:
        """The store's name for *detection*, or ``None`` — an unknown face is fine."""
        store = self._store
        if store is None:
            return None
        try:
            match = store.match(getattr(detection, "embedding", None))  # type: ignore[attr-defined]
        except Exception:
            logger.warning("FaceSenseDriver store match raised; skipping match", exc_info=True)
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

    def peek_face_bbox(self) -> tuple[float, float, float, float] | None:
        """The TTL-held face POSITION ``(x, y, w, h)`` — a non-consuming PEEK."""
        return self._face_bbox

    def as_face_bbox_provider(self) -> Callable[[], tuple[float, float, float, float] | None]:
        """The zero-arg ``face_bbox`` provider callable."""
        return self.peek_face_bbox

    def peek_face_age_s(self) -> float | None:
        """Seconds since the held position's detection, or ``None`` — a PEEK."""
        return self._face_age_s

    def as_face_age_provider(self) -> Callable[[], float | None]:
        """The zero-arg ``face_age_s`` provider callable."""
        return self.peek_face_age_s

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
        self._clear_position()
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
    "detect_interval_from_env",
    "detect_max_width_from_env",
    "downscale_for_detection",
    "face_recognition_unavailable_reason",
    "usable_frame",
    "vision_unavailable_reason",
    "DEFAULT_DETECT_INTERVAL",
    "DEFAULT_DETECT_MAX_WIDTH",
    "DEFAULT_FRAME_INTERVAL_S",
    "DEFAULT_FRAME_TTL_S",
    "DEFAULT_REANNOUNCE_COOLDOWN",
    "DEFAULT_STREAM_STALE_S",
    "REASON_STREAM_ENDED",
    "VISION_EXTRA_ABSENT",
    "VISION_STACK_UNAVAILABLE",
]
