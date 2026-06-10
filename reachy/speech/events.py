"""Sense-event buffer — live sensor readings → human-readable cue strings.

This module is the "what the robot perceives" feed for the (future) think engine.
It is NOT transcription (no STT).  It turns already-read sensory sample values
into timestamped :class:`SenseCue` strings held in a rolling, thread-safe buffer
that the think engine can snapshot at any time.

Two feed methods accept values that callers read from hardware/daemons:

* :meth:`EventBuffer.feed_doa` — Direction-of-Arrival angle (radians), RMS
  loudness, and speech-detected flag from the mic array.  Produces cues like
  ``"speech from the left"``, ``"loud sound ahead"``, ``"sound on the right"``.

* :meth:`EventBuffer.feed_vision` — motion centroid direction and brightness
  delta from the camera.  Produces cues like ``"motion on the right"``,
  ``"the light brightened"``, ``"the light dimmed"``.

Design constraints
------------------
* **Pure in-process** — no I/O, no hardware access, no new dependencies.
  ``numpy`` is available but not needed here; this module is stdlib-only.
* **Thread-safe** — a :class:`threading.Lock` guards the deque; callers on
  different threads may call ``feed_*`` and ``snapshot()`` concurrently without
  losing cues.
* **Atomic snapshot** — :meth:`snapshot` swaps in a fresh deque under the lock
  and returns the old contents as a list; a producer racing a snapshot never sees
  its cue silently dropped.
* **Rolling window** — the deque has a *maxlen*; oldest cues are evicted when
  the buffer is full, so a slow consumer never causes unbounded growth.
* **Injectable clock** — the default is :func:`time.monotonic`; tests inject a
  deterministic counter so timestamps are predictable.

DoA angle convention (from ``reachy/behavior/sense.py``)
---------------------------------------------------------
``0`` = left, ``π/2`` = front/ahead, ``π`` = right.

The directional mapping used here applies a ±*AHEAD_BAND_RAD* band around
``π/2`` (front) to label a sound as "ahead"; outside the band, the sound is
"left" (angle < π/2 − band) or "right" (angle > π/2 + band).

Vision direction convention (from ``reachy/vision/motion.py``)
--------------------------------------------------------------
``direction`` is the normalised horizontal centroid in ``[-1, 1]``:
``-1`` = far left, ``0`` = centre, ``+1`` = far right.  A ±*VISION_AHEAD_BAND*
band around ``0`` maps to "ahead".
"""

from __future__ import annotations

import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Callable

# ---------------------------------------------------------------------------
# Thresholds
# ---------------------------------------------------------------------------

# Sound direction: how close to pi/2 (front) a DoA angle must be before we
# label it "ahead" rather than "left" or "right".  In radians; pi/12 ≈ 15°.
_AHEAD_BAND_RAD: float = 0.26  # ~15° of arc around front

# RMS threshold: below this level a non-speech sound is ambient noise and does
# not warrant a cue.  Matches the snap detector's documented "min_rms" default.
_LOUD_RMS_THRESHOLD: float = 0.02

# Vision: motion direction band around 0 that counts as "ahead".
_VISION_AHEAD_BAND: float = 0.25  # normalised [-1, 1]

# Vision: minimum |brightness_delta| (mean luma, 0–255 scale) before we emit
# a brightness cue.  Mirrors LightDetector's default change_threshold of 8.0.
_BRIGHTNESS_THRESHOLD: float = 8.0

# Default rolling-window size.
_DEFAULT_MAXLEN: int = 256


# ---------------------------------------------------------------------------
# Data types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SenseCue:
    """A single human-readable perception event with a monotonic timestamp.

    Attributes
    ----------
    text:
        A short English phrase describing what the robot perceived, e.g.
        ``"speech from the left"`` or ``"motion on the right"``.
    timestamp:
        Monotonic clock value (seconds) at the moment the cue was appended.
        Produced by the injectable *clock* passed to :class:`EventBuffer`.
    """

    text: str
    timestamp: float

    def __repr__(self) -> str:  # pragma: no cover
        return f"SenseCue(text={self.text!r}, timestamp={self.timestamp:.3f})"


# ---------------------------------------------------------------------------
# Direction helpers
# ---------------------------------------------------------------------------


def _doa_direction(angle_rad: float) -> str:
    """Map a DoA angle in radians to a direction word.

    Angle convention: ``0`` = left, ``pi/2`` = front, ``pi`` = right.
    Returns one of ``"left"``, ``"ahead"``, or ``"right"``.
    """
    import math

    front = math.pi / 2.0
    if angle_rad < front - _AHEAD_BAND_RAD:
        return "left"
    if angle_rad > front + _AHEAD_BAND_RAD:
        return "right"
    return "ahead"


def _vision_direction(direction: float) -> str:
    """Map a normalised motion centroid ``[-1, 1]`` to a direction word.

    Returns one of ``"left"``, ``"ahead"``, or ``"right"``.
    """
    if direction < -_VISION_AHEAD_BAND:
        return "left"
    if direction > _VISION_AHEAD_BAND:
        return "right"
    return "ahead"


# ---------------------------------------------------------------------------
# Buffer
# ---------------------------------------------------------------------------


class EventBuffer:
    """A rolling, thread-safe buffer of :class:`SenseCue` perception events.

    Parameters
    ----------
    maxlen:
        Maximum number of cues to retain.  The oldest cue is silently evicted
        when the buffer is full.  Default ``256``.
    clock:
        Zero-argument callable that returns the current time as a ``float``
        (seconds, monotonic).  Defaults to :func:`time.monotonic`.  Inject a
        deterministic counter in tests for reproducible timestamps.

    Usage
    -----
    ::

        buf = EventBuffer()

        # From the listen loop (already-read sense values):
        buf.feed_doa(angle_rad=0.0, rms=0.08, is_speech=True)

        # From the vision loop (already-read frame results):
        buf.feed_vision(motion_direction=-0.6, brightness_delta=12.0)

        # Think engine snapshots and clears:
        cues = buf.snapshot()   # → [SenseCue(...), ...]
        # Buffer is now empty; the cues are owned by the caller.
    """

    def __init__(
        self,
        maxlen: int = _DEFAULT_MAXLEN,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._maxlen = maxlen
        self._clock = clock
        self._lock = threading.Lock()
        self._buf: deque[SenseCue] = deque(maxlen=maxlen)

    # ------------------------------------------------------------------
    # Feed methods
    # ------------------------------------------------------------------

    def feed_doa(
        self,
        angle_rad: float | None,
        rms: float,
        is_speech: bool,
    ) -> None:
        """Translate one mic-array reading into zero or one cue and append it.

        Parameters
        ----------
        angle_rad:
            Sound Direction of Arrival in radians.  Convention from
            ``reachy/behavior/sense.py``: ``0``=left, ``pi/2``=front, ``pi``=right.
            Pass ``None`` (``Sense.doa_angle is None``) for "no reading" — no
            cue is emitted.
        rms:
            RMS loudness of the current audio chunk (same units as
            :class:`~reachy.motion.snap.SnapDetector`; values in ``[0, 1]``
            for a normalised audio stream).
        is_speech:
            The daemon's speech-vs-any-sound flag for this reading
            (``Sense.speech_detected``).

        Cue rules
        ---------
        * No angle → no cue.
        * ``is_speech=True`` → ``"speech from the <direction>"``
        * ``is_speech=False`` and ``rms >= _LOUD_RMS_THRESHOLD`` →
          ``"loud sound <direction>"``
        * Otherwise (quiet non-speech) → no cue (ambient noise, not notable).
        """
        if angle_rad is None:
            return

        direction = _doa_direction(angle_rad)

        if is_speech:
            text = f"speech from the {direction}"
        elif rms >= _LOUD_RMS_THRESHOLD:
            text = f"loud sound {direction}"
        else:
            return  # quiet, non-speech — below perceptual threshold

        self._append(text)

    def feed_vision(
        self,
        motion_direction: float | None,
        brightness_delta: float,
    ) -> None:
        """Translate one camera-frame reading into zero, one, or two cues.

        Parameters
        ----------
        motion_direction:
            Normalised horizontal centroid of detected motion in ``[-1, 1]``
            (``MotionResult.direction``).  Pass ``None`` when no motion was
            detected (the caller already filtered ``MotionDetector.feed()``
            returning ``None``).
        brightness_delta:
            Change in mean luma (0–255 scale) relative to the rolling baseline
            (positive = brighter, negative = darker).  Derived from
            ``LightResult.mean_luma`` minus the caller's baseline.  Pass ``0.0``
            when no light-change event occurred (``LightResult.changed=False``).

        Cue rules
        ---------
        * ``motion_direction is not None`` → ``"motion on the <direction>"``
          (or ``"motion ahead"``).
        * ``|brightness_delta| >= _BRIGHTNESS_THRESHOLD`` and delta > 0 →
          ``"the light brightened"``
        * ``|brightness_delta| >= _BRIGHTNESS_THRESHOLD`` and delta < 0 →
          ``"the light dimmed"``
        * Small brightness delta → no cue.
        """
        if motion_direction is not None:
            direction = _vision_direction(motion_direction)
            if direction == "ahead":
                self._append("motion ahead")
            else:
                self._append(f"motion on the {direction}")

        if abs(brightness_delta) >= _BRIGHTNESS_THRESHOLD:
            if brightness_delta > 0:
                self._append("the light brightened")
            else:
                self._append("the light dimmed")

    # ------------------------------------------------------------------
    # Snapshot
    # ------------------------------------------------------------------

    def snapshot(self) -> list[SenseCue]:
        """Return all buffered cues and atomically clear the buffer.

        The swap (drain + reset) is performed under the lock so no cue can be
        lost between a producer's append and the consumer's read.  The returned
        list is a new object owned by the caller; the buffer is immediately
        ready for new cues.

        Returns
        -------
        list[SenseCue]
            Cues in the order they were appended (oldest first), or an empty
            list when the buffer was empty.
        """
        with self._lock:
            old = self._buf
            self._buf = deque(maxlen=self._maxlen)
        return list(old)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _append(self, text: str) -> None:
        """Append a new cue under the lock."""
        cue = SenseCue(text=text, timestamp=self._clock())
        with self._lock:
            self._buf.append(cue)
