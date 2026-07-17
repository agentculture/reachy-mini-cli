"""Sense-event buffer — live sensor readings → human-readable cue strings.

This module is the "what the robot perceives" feed for the (future) think engine.
It does NOT perform STT itself — transcription happens upstream (in the
listen-loop hook) and finished text is handed in via :meth:`EventBuffer.feed_transcript`.
It turns already-read sensory sample values
into timestamped :class:`SenseCue` strings held in a rolling, thread-safe buffer
that the think engine can snapshot at any time.

Four feed methods accept values that callers read from hardware/daemons:

* :meth:`EventBuffer.feed_doa` — Direction-of-Arrival angle (radians), RMS
  loudness, and speech-detected flag from the mic array.  Produces cues like
  ``"speech from the left"``, ``"loud sound ahead"``, ``"sound on the right"``.

* :meth:`EventBuffer.feed_vision` — motion centroid direction and brightness
  delta from the camera.  Produces cues like ``"motion on the right"``,
  ``"the light brightened"``, ``"the light dimmed"``.

* :meth:`EventBuffer.feed_transcript` — already-transcribed spoken words (and
  optionally the direction they came from).  Produces cues like
  ``'heard someone say: "hello"'``.

* :meth:`EventBuffer.feed_pat` — a detected proprioceptive touch event (kind
  and intensity level) from :class:`~reachy.motion.pat.PatDetector`.  Produces
  cues like ``"felt a gentle scratch on the head"``.

* :meth:`EventBuffer.feed_face` — the name of a recognised (known, named) face
  from :class:`~reachy.motion.listen_face.FaceHook`.  Produces cues like
  ``"saw Ada"``.  An unknown/empty name yields no cue.

* :meth:`EventBuffer.feed_scene` — a VLM scene description from
  :class:`~reachy.motion.listen_scene.SceneHook` (or the ``describe_scene`` agent
  tool).  Produces cues like ``"noticed: a person waving at the desk"``.  An
  empty/whitespace description yields no cue.

* :meth:`EventBuffer.feed_forge` — a forge self-extension lifecycle event from
  :class:`~reachy.forge.activate.ForgeActivator` (e.g. a newly learned skill).
  The text is passed through verbatim, e.g. ``"learned a new skill:
  wave-hello"`` — unlike :meth:`feed_scene` this is not a scene observation,
  so no ``"noticed: "`` prefix is added.  An empty/whitespace text yields no
  cue.

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
import uuid
from collections import deque
from dataclasses import dataclass
from typing import Callable

from reachy import senselog

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

# Touch: kind -> the noun phrase used in the cue text.  Keys match the
# touch-type strings PatDetector emits (reachy/motion/pat.py).
_PAT_KIND_PHRASE: dict[str, str] = {
    "scratch": "scratch",
    "side_pat": "sideways nudge",
}

# Touch: level -> the intensity adjective used in the cue text.  Keys match
# the level strings PatDetector emits (reachy/motion/pat.py).
_PAT_LEVEL_INTENSITY: dict[str, str] = {
    "level1": "gentle",
    "level2": "firm",
}


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

        self._append(text, source="doa")

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
                self._append("motion ahead", source="vision")
            else:
                self._append(f"motion on the {direction}", source="vision")

        if abs(brightness_delta) >= _BRIGHTNESS_THRESHOLD:
            if brightness_delta > 0:
                self._append("the light brightened", source="vision")
            else:
                self._append("the light dimmed", source="vision")

    def feed_transcript(self, text: str, *, direction: str | None = None) -> None:
        """Append a cue for already-transcribed spoken words.

        Parameters
        ----------
        text:
            The transcribed text (already produced upstream by STT).  Stripped
            of surrounding whitespace.  If empty after stripping, no cue is
            appended.
        direction:
            Optional direction word the words came from (``"left"`` / ``"right"``
            / ``"ahead"``, from the DoA of the transcribed chunk).  When given the
            cue names where the speaker is — so cognition hears *words and where
            they came from*.

        Cue rules
        ---------
        * Non-empty text, no direction → ``'heard someone say: "<text>"'``
        * Non-empty text + direction → ``'heard someone say (from the <dir>): "<text>"'``
        * Empty / whitespace-only → no cue.
        """
        stripped = text.strip()
        if not stripped:
            return
        if direction:
            self._append(
                f'heard someone say (from the {direction}): "{stripped}"', source="transcript"
            )
        else:
            self._append(f'heard someone say: "{stripped}"', source="transcript")

    def feed_pat(self, kind: str, level: str) -> None:
        """Translate one detected touch event into zero or one cue and append it.

        Parameters
        ----------
        kind:
            The touch type reported by :class:`~reachy.motion.pat.PatDetector`:
            ``"scratch"`` (pitch-dominated, head pushed down) or ``"side_pat"``
            (yaw-dominated, head nudged sideways).
        level:
            The touch intensity reported by ``PatDetector``: ``"level1"``
            (first detection) or ``"level2"`` (sustained hold).

        Cue rules
        ---------
        * ``kind="scratch"``, ``level="level1"`` → ``"felt a gentle scratch on
          the head"``
        * ``kind="scratch"``, ``level="level2"`` → ``"felt a firm scratch on
          the head"``
        * ``kind="side_pat"``, ``level="level1"`` → ``"felt a gentle sideways
          nudge on the head"``
        * ``kind="side_pat"``, ``level="level2"`` → ``"felt a firm sideways
          nudge on the head"``
        * Any other *kind* or *level* → no cue (defensive default; never
          raises).
        """
        phrase = _PAT_KIND_PHRASE.get(kind)
        intensity = _PAT_LEVEL_INTENSITY.get(level)
        if phrase is None or intensity is None:
            return

        self._append(f"felt a {intensity} {phrase} on the head", source="pat")

    def feed_face(self, name: str) -> None:
        """Translate one recognised face into zero or one cue and append it.

        Parameters
        ----------
        name:
            The name of the matched permanent-tier face, as reported by
            :class:`~reachy.motion.listen_face.FaceHook`
            (:class:`~reachy.vision.face_store.FaceMatch.name`).  Stripped of
            surrounding whitespace.

        Cue rules
        ---------
        * Non-empty *name* → ``"saw <name>"``
        * ``None`` / empty / whitespace-only → no cue (an unknown or unnamed face
          is never announced by name; defensive default, never raises).
        """
        if not name or not str(name).strip():
            return

        self._append(f"saw {str(name).strip()}", source="face")

    def feed_scene(self, text: str) -> None:
        """Translate one VLM scene description into zero or one cue and append it.

        Parameters
        ----------
        text:
            The scene description produced by :func:`reachy.vision.scene.describe_frame`
            (via :class:`~reachy.motion.listen_scene.SceneHook` or the
            ``describe_scene`` agent tool).  Stripped of surrounding whitespace.

        Cue rules
        ---------
        * Non-empty *text* → ``"noticed: <text>"``
        * ``None`` / empty / whitespace-only → no cue (defensive default, never
          raises).
        """
        if not text or not str(text).strip():
            return

        self._append(f"noticed: {str(text).strip()}", source="scene")

    def feed_forge(self, text: str) -> None:
        """Translate one forge self-extension event into zero or one cue and append it.

        Parameters
        ----------
        text:
            The forge lifecycle cue text, e.g. ``"learned a new skill:
            wave-hello"``, produced by
            :class:`~reachy.forge.activate.ForgeActivator` on a successful skill
            activation.  Stripped of surrounding whitespace.

        Cue rules
        ---------
        * Non-empty *text* → the stripped text, passed through **verbatim** — a
          forge event is already a complete human-readable sentence, so (unlike
          :meth:`feed_scene`) no prefix is added.
        * ``None`` / empty / whitespace-only → no cue (defensive default, never
          raises).
        """
        if not text or not str(text).strip():
            return

        self._append(str(text).strip(), source="forge")

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

    def _append(self, text: str, *, source: str) -> None:
        """Append a new cue under the lock and emit its [SENSE stage=cue] line.

        ``source`` names the feed kind that produced this cue (``"doa"``,
        ``"vision"``, ``"transcript"``, ``"pat"``, ``"face"``, ``"scene"``,
        ``"forge"``) —
        every ``feed_*`` call that actually appends a cue routes through here, so
        every cue is logged exactly once. A ``feed_*`` call that produces *no* cue because of a
        threshold never reaches this method — it stays silent, not a drop (see the
        module/method docstrings' "Cue rules").
        """
        cue = SenseCue(text=text, timestamp=self._clock())
        with self._lock:
            self._buf.append(cue)
        senselog.stage("cue", source, uuid.uuid4().hex[:8], text)
