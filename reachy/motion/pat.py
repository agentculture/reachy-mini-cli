"""Proprioceptive pat (head-touch) detector.

Cited from ``reachy_nova.tracking.PatDetector`` — logic ported faithfully;
all transport / I/O / YOLO coupling removed so this module depends only on
``numpy`` and the standard library.

Algorithm:
- Per sample: compute ``deviation = actual - commanded`` for both pitch and yaw.
- Apply a slow EMA baseline to cancel steady-state servo bias:
  ``_baseline_offset += _baseline_alpha * (raw_dev - _baseline_offset)``;
  corrected deviation = ``raw_dev - _baseline_offset``.
- Pitch press: ``deviation < -press_threshold`` (head pushed down = "scratch").
- Yaw press: ``abs(yaw_dev) > yaw_press_threshold`` (head nudged sideways = "side_pat").
- Both axes use hysteresis (separate release thresholds).
- Recent presses accumulated in a ``pat_window``-second sliding window.
- Two-level state machine:
    idle         → level1 when ``recent_presses >= min_presses`` and cooldown
                   elapsed; fires ``("level1", touch_type)``.
    level1       → level2_cooldown only when a fresh press arrives after a random
                   4–8 s threshold; fires the level2 event. Elapsed time without
                   a fresh edge cannot escalate.
                 → idle on interaction gap (no presses for ``interaction_gap_timeout``).
    level2_cooldown → idle after ``level2_cooldown`` seconds.
- Touch type is classified by pitch-vs-yaw press count inside the window.

Determinism for unit tests:
- ``update()`` accepts an optional ``now`` parameter (float, seconds).
  Omit it in production — the default is ``time.monotonic()``.
- The random level2 threshold is injectable via the ``level2_threshold_fn``
  constructor argument (a zero-arg callable returning float).  The default draws
  from ``random.uniform(4.0, 8.0)``.  Tests pass a fixed lambda to get
  repeatable results.
"""

from __future__ import annotations

import logging
import random
import time
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal

import numpy as np  # noqa: F401  # required: callers may type-hint ndarray inputs

logger = logging.getLogger(__name__)

TouchType = Literal["scratch", "side_pat"]
PatLevel = Literal["level1", "level2"]


@dataclass(frozen=True, slots=True)
class PatEvidence:
    """Event-stable evidence from the most recent qualifying press.

    This is deliberately separate from update's legacy event tuple. A caller
    can inspect contact direction and timing without changing any existing
    event consumer:

    * pressed reports whether either threshold hysteresis is currently active;
    * touch_type, yaw_deg, and last_press_at persist across release/deadband
      samples until clear_presses, an interaction gap, or cooldown expiry;
    * yaw_deg is the corrected robot-frame deviation in degrees, never abs();
    * level changes only when the legacy state machine emits that level.

    Mixed-axis samples use normalized threshold dominance, with pitch winning
    exact ties. Across samples the latest qualifying press wins. A deadband
    sample never replaces signed evidence.
    """

    pressed: bool = False
    touch_type: TouchType | None = None
    level: PatLevel | None = None
    yaw_deg: float | None = None
    last_press_at: float | None = None


class PatDetector:
    """Detect patting gestures on the Reachy Mini head.

    Compares the commanded head pose with the actual pose read back from the
    servos.  When someone pats the head the actual pose deviates from the
    commanded pose; repeated impulses within a short window are classified as
    a pat.

    Tracks both pitch (forward/down push = ``"scratch"``) and yaw
    (side-to-side nudge = ``"side_pat"``) to differentiate touch types.

    Parameters
    ----------
    press_threshold:
        Pitch deviation (degrees) below ``-press_threshold`` counts as a press.
        Default 1.2.
    release_threshold:
        Pitch deviation must rise above ``-release_threshold`` to count as
        released.  Default 0.5.
    yaw_press_threshold:
        Absolute yaw deviation (degrees) above this counts as a yaw press.
        Default 1.2.
    yaw_release_threshold:
        Absolute yaw deviation must drop below this to release.  Default 0.5.
    min_presses:
        Minimum press-count inside ``pat_window`` to trigger level1.  Default 2.
    pat_window:
        Sliding window (seconds) in which presses are counted.  Default 3.0.
    pat_cooldown:
        Minimum gap (seconds) between successive level1 events.  Default 2.0.
    interaction_gap_timeout:
        If no presses arrive for this many seconds while in level1, reset to
        idle.  Default 5.0.
    level2_cooldown:
        Cooldown duration (seconds) after a level2 event before returning to
        idle.  Default 5.0.
    baseline_alpha:
        EMA coefficient for the slow servo-bias baseline.  Default 0.003.
    level2_threshold_fn:
        Zero-argument callable returning the level2 hold-duration threshold
        (seconds).  Default: ``lambda: random.uniform(4.0, 8.0)``.
        Override in tests for determinism.
    """

    def __init__(
        self,
        *,
        press_threshold: float = 1.2,
        release_threshold: float = 0.5,
        yaw_press_threshold: float = 1.2,
        yaw_release_threshold: float = 0.5,
        min_presses: int = 2,
        pat_window: float = 3.0,
        pat_cooldown: float = 2.0,
        interaction_gap_timeout: float = 5.0,
        level2_cooldown: float = 5.0,
        baseline_alpha: float = 0.003,
        level2_threshold_fn: Callable[[], float] | None = None,
    ) -> None:
        # --- Tunable parameters ---
        self.press_threshold: float = press_threshold
        self.release_threshold: float = release_threshold
        self.yaw_press_threshold: float = yaw_press_threshold
        self.yaw_release_threshold: float = yaw_release_threshold
        self.min_presses: int = min_presses
        self.pat_window: float = pat_window
        self.pat_cooldown: float = pat_cooldown
        self._interaction_gap_timeout: float = interaction_gap_timeout
        self._level2_cooldown: float = level2_cooldown
        self._baseline_alpha: float = baseline_alpha
        self._level2_threshold_fn: Callable[[], float] = (
            level2_threshold_fn
            if level2_threshold_fn is not None
            else lambda: random.uniform(4.0, 8.0)  # nosec B311 — jitter, not crypto
        )

        # --- Rolling history (timestamp, corrected_deviation) ---
        self.deviation_history: deque[tuple[float, float]] = deque(maxlen=150)
        self.yaw_deviation_history: deque[tuple[float, float]] = deque(maxlen=150)

        # --- Press impulse log: (timestamp, axis) where axis ∈ {"pitch", "yaw"} ---
        self.press_times: deque[tuple[float, str]] = deque(maxlen=20)

        # --- EMA baselines (cancel slow servo offset) ---
        self._baseline_offset: float = 0.0
        self._yaw_baseline_offset: float = 0.0

        # --- Edge-trigger press state ---
        self._in_press: bool = False
        self._yaw_in_press: bool = False

        # --- Two-level state machine ---
        self._state: str = "idle"  # "idle" | "level1" | "level2_cooldown"
        self._level1_time: float = 0.0  # monotonic time when level1 fired
        self._level2_threshold: float = 0.0  # drawn at level1 fire time
        self._last_press_time: float = 0.0  # monotonic time of most recent press
        self.last_pat_time: float = 0.0  # monotonic time of last level1 event

        # Touch type propagated from level1 to level2
        self._current_touch_type: str = "scratch"

        # --- Separate signed evidence snapshot (legacy update tuple unchanged) ---
        self._evidence_touch_type: TouchType | None = None
        self._evidence_level: PatLevel | None = None
        self._evidence_yaw_deg: float | None = None
        self._evidence_last_press_at: float | None = None

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _classify_touch(self, now: float) -> str:
        """Return ``"scratch"`` or ``"side_pat"`` based on recent press axes."""
        cutoff = now - self.pat_window
        pitch_count = sum(1 for t, axis in self.press_times if t > cutoff and axis == "pitch")
        yaw_count = sum(1 for t, axis in self.press_times if t > cutoff and axis == "yaw")
        return "side_pat" if yaw_count > pitch_count else "scratch"

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(
        self,
        commanded_pitch: float,
        actual_pitch: float,
        commanded_yaw: float = 0.0,
        actual_yaw: float = 0.0,
        *,
        now: float | None = None,
    ) -> tuple[str, str] | None:
        """Feed one sample of commanded-vs-actual pitch and yaw.

        Parameters
        ----------
        commanded_pitch:
            The pitch commanded to the servo (degrees, positive = up).
        actual_pitch:
            The pitch read back from the servo (degrees).
        commanded_yaw:
            The yaw commanded to the servo (degrees).
        actual_yaw:
            The yaw read back from the servo (degrees).
        now:
            Current time in seconds (monotonic).  Pass this in tests for full
            determinism; omit in production to use ``time.monotonic()``.

        Returns
        -------
        tuple[str, str] | None
            ``("level1", touch_type)`` or ``("level2", touch_type)`` on a
            detection event; ``None`` otherwise.
            *touch_type* is ``"scratch"`` (pitch-dominated) or ``"side_pat"``
            (yaw-dominated).
        """
        if now is None:
            now = time.monotonic()

        pitch_edge, pitch_dev = self._track_pitch(commanded_pitch, actual_pitch, now)
        yaw_edge, yaw_dev = self._track_yaw(commanded_yaw, actual_yaw, now)
        fresh_press = pitch_edge or yaw_edge
        if fresh_press:
            self._record_evidence(
                now=now,
                pitch_edge=pitch_edge,
                pitch_dev=pitch_dev,
                yaw_edge=yaw_edge,
                yaw_dev=yaw_dev,
            )
        event = self._advance_state(now, fresh_press=fresh_press)
        if event is not None:
            self._evidence_level = event[0]
        return event

    def snapshot(self) -> PatEvidence:
        """Return immutable signed evidence without consuming or aging it."""
        return PatEvidence(
            pressed=self._in_press or self._yaw_in_press,
            touch_type=self._evidence_touch_type,
            level=self._evidence_level,
            yaw_deg=self._evidence_yaw_deg,
            last_press_at=self._evidence_last_press_at,
        )

    # ------------------------------------------------------------------
    # Per-axis press tracking + state machine (split out of update for clarity
    # and to keep each unit's cognitive complexity low)
    # ------------------------------------------------------------------

    def _track_pitch(
        self, commanded_pitch: float, actual_pitch: float, now: float
    ) -> tuple[bool, float]:
        """Update the EMA-baselined pitch deviation and its press edge state."""
        raw_deviation: float = actual_pitch - commanded_pitch
        self._baseline_offset += self._baseline_alpha * (raw_deviation - self._baseline_offset)
        deviation: float = raw_deviation - self._baseline_offset
        self.deviation_history.append((now, deviation))

        pressed = False
        if deviation < -self.press_threshold and not self._in_press:
            self._in_press = True
            self.press_times.append((now, "pitch"))
            self._last_press_time = now
            pressed = True
            logger.debug("Pat pitch press: deviation=%.2f deg", deviation)
        elif deviation > -self.release_threshold:
            self._in_press = False
        return pressed, deviation

    def _track_yaw(self, commanded_yaw: float, actual_yaw: float, now: float) -> tuple[bool, float]:
        """Update the EMA-baselined yaw deviation and its press edge state."""
        raw_yaw_dev: float = actual_yaw - commanded_yaw
        self._yaw_baseline_offset += self._baseline_alpha * (
            raw_yaw_dev - self._yaw_baseline_offset
        )
        yaw_dev: float = raw_yaw_dev - self._yaw_baseline_offset
        self.yaw_deviation_history.append((now, yaw_dev))

        pressed = False
        if abs(yaw_dev) > self.yaw_press_threshold and not self._yaw_in_press:
            self._yaw_in_press = True
            self.press_times.append((now, "yaw"))
            self._last_press_time = now
            pressed = True
            logger.debug("Pat yaw press: deviation=%.2f deg", yaw_dev)
        elif abs(yaw_dev) < self.yaw_release_threshold:
            self._yaw_in_press = False
        return pressed, yaw_dev

    def _record_evidence(
        self,
        *,
        now: float,
        pitch_edge: bool,
        pitch_dev: float,
        yaw_edge: bool,
        yaw_dev: float,
    ) -> None:
        """Apply the documented per-sample dominance and cross-sample recency rule."""
        yaw_wins = yaw_edge and (
            not pitch_edge
            or self._normalized_strength(yaw_dev, self.yaw_press_threshold)
            > self._normalized_strength(pitch_dev, self.press_threshold)
        )
        if yaw_wins:
            self._evidence_touch_type = "side_pat"
            self._evidence_yaw_deg = yaw_dev
        else:
            self._evidence_touch_type = "scratch"
            self._evidence_yaw_deg = None
        self._evidence_last_press_at = now

    @staticmethod
    def _normalized_strength(deviation: float, threshold: float) -> float:
        if threshold <= 0.0:
            return float("inf")
        return abs(deviation) / threshold

    def _advance_state(self, now: float, *, fresh_press: bool) -> tuple[str, str] | None:
        """Run the two-level state machine for one tick; return any event."""
        if self._state == "idle":
            return self._advance_idle(now)
        if self._state == "level1":
            return self._advance_level1(now, fresh_press=fresh_press)
        if self._state == "level2_cooldown":
            if now - self.last_pat_time > self._level2_cooldown:
                logger.info("Pat cooldown expired — ready for new detection")
                self._state = "idle"
                self.clear_presses()
        return None

    def _advance_idle(self, now: float) -> tuple[str, str] | None:
        """Idle → level1 once enough recent presses land outside the cooldown."""
        cutoff = now - self.pat_window
        recent_presses = sum(1 for t, _ in self.press_times if t > cutoff)
        if not (
            recent_presses >= self.min_presses and now - self.last_pat_time > self.pat_cooldown
        ):
            return None

        touch_type = self._classify_touch(now)
        self._current_touch_type = touch_type
        self.last_pat_time = now
        self.press_times.clear()
        self._state = "level1"
        self._level1_time = now
        self._level2_threshold = self._level2_threshold_fn()
        logger.info(
            "Pat level1! type=%s (%d presses, level2 threshold=%.1f s)",
            touch_type,
            recent_presses,
            self._level2_threshold,
        )
        return ("level1", touch_type)

    def _advance_level1(self, now: float, *, fresh_press: bool) -> tuple[str, str] | None:
        """level1 → level2 on a sustained hold, or → idle on an interaction gap."""
        if (
            self._last_press_time > 0
            and now - self._last_press_time > self._interaction_gap_timeout
        ):
            logger.info("Pat interaction gap — resetting to idle")
            self._state = "idle"
            self.clear_presses()
            return None

        elapsed = now - self._level1_time
        if elapsed > self._level2_threshold and fresh_press:
            touch_type = self._current_touch_type
            self.last_pat_time = now
            self.press_times.clear()
            self._state = "level2_cooldown"
            logger.info("Pat level2! type=%s (sustained %.1f s)", touch_type, elapsed)
            return ("level2", touch_type)
        return None

    def reset(self) -> None:
        """Reset all detector state to initial values."""
        self.clear_presses()
        self.deviation_history.clear()
        self.yaw_deviation_history.clear()
        self._baseline_offset = 0.0
        self._yaw_baseline_offset = 0.0
        self._current_touch_type = "scratch"
        self._state = "idle"
        self._level1_time = 0.0
        self._level2_threshold = 0.0
        self._last_press_time = 0.0
        self.last_pat_time = 0.0

    def clear_presses(self) -> None:
        """Clear press accumulation and edge state — but KEEP the learned baselines.

        The EMA baselines cancel steady-state bias (gravity sag, servo/calibration
        offset — a live head rests several degrees off its commanded neutral).
        Wiping them with a full :meth:`reset` makes that bias read as a fresh press
        until the slow EMA re-learns, which is exactly how the folded ``listen``
        hook's phantom-pat chains were re-seeding. Callers that only need to stop
        press edges pairing across a suspension (a reaction window, a large move's
        dispatch) call this instead, so the bias stays cancelled.
        """
        self.press_times.clear()
        self._in_press = False
        self._yaw_in_press = False
        self._evidence_touch_type = None
        self._evidence_level = None
        self._evidence_yaw_deg = None
        self._evidence_last_press_at = None

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"PatDetector(state={self._state!r}, "
            f"press_threshold={self.press_threshold}, "
            f"min_presses={self.min_presses})"
        )
