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
    level1       → level2_cooldown when sustained past a random 4–8 s threshold;
                   fires ``("level2", touch_type)``.
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

import numpy as np  # noqa: F401  # required: callers may type-hint ndarray inputs

logger = logging.getLogger(__name__)


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

        self._track_pitch(commanded_pitch, actual_pitch, now)
        self._track_yaw(commanded_yaw, actual_yaw, now)
        return self._advance_state(now)

    # ------------------------------------------------------------------
    # Per-axis press tracking + state machine (split out of update for clarity
    # and to keep each unit's cognitive complexity low)
    # ------------------------------------------------------------------

    def _track_pitch(self, commanded_pitch: float, actual_pitch: float, now: float) -> None:
        """Update the EMA-baselined pitch deviation and its press edge state."""
        raw_deviation: float = actual_pitch - commanded_pitch
        self._baseline_offset += self._baseline_alpha * (raw_deviation - self._baseline_offset)
        deviation: float = raw_deviation - self._baseline_offset
        self.deviation_history.append((now, deviation))

        if deviation < -self.press_threshold and not self._in_press:
            self._in_press = True
            self.press_times.append((now, "pitch"))
            self._last_press_time = now
            logger.debug("Pat pitch press: deviation=%.2f deg", deviation)
        elif deviation > -self.release_threshold:
            self._in_press = False

    def _track_yaw(self, commanded_yaw: float, actual_yaw: float, now: float) -> None:
        """Update the EMA-baselined yaw deviation and its press edge state."""
        raw_yaw_dev: float = actual_yaw - commanded_yaw
        self._yaw_baseline_offset += self._baseline_alpha * (
            raw_yaw_dev - self._yaw_baseline_offset
        )
        yaw_dev: float = raw_yaw_dev - self._yaw_baseline_offset
        self.yaw_deviation_history.append((now, yaw_dev))

        if abs(yaw_dev) > self.yaw_press_threshold and not self._yaw_in_press:
            self._yaw_in_press = True
            self.press_times.append((now, "yaw"))
            self._last_press_time = now
            logger.debug("Pat yaw press: deviation=%.2f deg", yaw_dev)
        elif abs(yaw_dev) < self.yaw_release_threshold:
            self._yaw_in_press = False

    def _advance_state(self, now: float) -> tuple[str, str] | None:
        """Run the two-level state machine for one tick; return any event."""
        if self._state == "idle":
            return self._advance_idle(now)
        if self._state == "level1":
            return self._advance_level1(now)
        if self._state == "level2_cooldown":
            if now - self.last_pat_time > self._level2_cooldown:
                logger.info("Pat cooldown expired — ready for new detection")
                self._state = "idle"
                self.press_times.clear()
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

    def _advance_level1(self, now: float) -> tuple[str, str] | None:
        """level1 → level2 on a sustained hold, or → idle on an interaction gap."""
        if (
            self._last_press_time > 0
            and now - self._last_press_time > self._interaction_gap_timeout
        ):
            logger.info("Pat interaction gap — resetting to idle")
            self._state = "idle"
            return None

        elapsed = now - self._level1_time
        if elapsed > self._level2_threshold:
            touch_type = self._current_touch_type
            self.last_pat_time = now
            self.press_times.clear()
            self._state = "level2_cooldown"
            logger.info("Pat level2! type=%s (sustained %.1f s)", touch_type, elapsed)
            return ("level2", touch_type)
        return None

    def reset(self) -> None:
        """Reset all detector state to initial values."""
        self.deviation_history.clear()
        self.yaw_deviation_history.clear()
        self.press_times.clear()
        self._in_press = False
        self._yaw_in_press = False
        self._baseline_offset = 0.0
        self._yaw_baseline_offset = 0.0
        self._current_touch_type = "scratch"
        self._state = "idle"
        self._level1_time = 0.0
        self._level2_threshold = 0.0
        self._last_press_time = 0.0
        self.last_pat_time = 0.0

    # ------------------------------------------------------------------
    # Dunder
    # ------------------------------------------------------------------

    def __repr__(self) -> str:  # pragma: no cover
        return (
            f"PatDetector(state={self._state!r}, "
            f"press_threshold={self.press_threshold}, "
            f"min_presses={self.min_presses})"
        )
