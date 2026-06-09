"""Pure-numpy light / brightness detector for Reachy Mini vision.

Algorithm:
- Maintain a rolling baseline of mean luma (grayscale) values.
- Per frame: compute mean luma; threshold near the per-frame maximum to find the
  "bright region"; take that region's column centroid and normalise to [-1, 1]
  (left = -1, right = +1).
- Fire ``changed=True`` ONLY when the luma delta *and/or* the centroid shift
  exceed their respective thresholds compared to the rolling baseline — i.e. the
  detector reacts to CHANGE, not to absolute brightness.
- Edge-triggered for centroid direction: does not keep firing while the same
  bright spot stays on the same side.

Cited design mirrors ``reachy.motion.snap.SnapDetector``:
  small class · type hints · docstring · pure numpy.
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass

import numpy as np


@dataclass(frozen=True)
class LightResult:
    """Result returned by :meth:`LightDetector.feed`.

    Attributes
    ----------
    direction:
        Normalised horizontal position of the brightest region in [-1, 1].
        -1 = far left, 0 = centre, +1 = far right.  ``None`` when no distinct
        bright region can be located (e.g. flat/uniform exposure).
    mean_luma:
        Mean grayscale value of the whole frame (0–255 scale).
    changed:
        ``True`` only when the scene has changed meaningfully relative to the
        running baseline — a sudden bright spot or a significant exposure shift
        with a detectable centroid shift.
    """

    direction: float | None
    mean_luma: float
    changed: bool


class LightDetector:
    """Detect meaningful light changes from a stream of camera frames.

    Parameters
    ----------
    threshold:
        Fraction below the per-frame maximum used to define the "bright region".
        Pixels with luma >= ``max_luma * threshold`` are included.  Lower value =
        more pixels included (default 0.85).
    change_threshold:
        Minimum luma delta (absolute, 0–255 scale) vs the rolling baseline mean
        luma required before ``changed`` can be True (default 8.0).
    centroid_shift_threshold:
        Minimum normalised centroid shift (0–2 range) vs the rolling baseline
        centroid required to additionally flag a change.  Set to 0 to rely only
        on luma delta (default 0.15).
    history:
        Rolling-window length in frames for the luma baseline (default 20).
    downsample:
        Spatial downsampling factor applied before any computation (default 4).
        A value of 4 reduces a 480×640 frame to 120×160 — fast on Pi 4.
    min_bright_fraction:
        Minimum fraction of total pixels that must be in the bright region for a
        centroid to be meaningful.  Below this the frame is considered
        featureless / uniform (default 0.005).
    """

    def __init__(
        self,
        *,
        threshold: float = 0.85,
        change_threshold: float = 8.0,
        centroid_shift_threshold: float = 0.15,
        history: int = 20,
        downsample: int = 4,
        min_bright_fraction: float = 0.005,
    ) -> None:
        self._threshold = threshold
        self._change_threshold = change_threshold
        self._centroid_shift_threshold = centroid_shift_threshold
        self._downsample = max(1, int(downsample))
        self._min_bright_fraction = min_bright_fraction
        self._luma_history: deque[float] = deque(maxlen=history)
        self._centroid_history: deque[float] = deque(maxlen=history)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _to_luma(self, frame: np.ndarray) -> np.ndarray:
        """Convert an arbitrary frame to a 2-D float32 luma array.

        Accepts:
        * 2-D (H, W) — treated as already greyscale.
        * 3-D (H, W, 3) — RGB: ``luma = 0.299R + 0.587G + 0.114B``.
        * 3-D (H, W, 4) — RGBA: alpha channel dropped, same weights.
        """
        f = frame.astype(np.float32)
        if f.ndim == 2:
            return f
        if f.ndim == 3 and f.shape[2] >= 3:
            return 0.299 * f[:, :, 0] + 0.587 * f[:, :, 1] + 0.114 * f[:, :, 2]
        raise ValueError(f"Unsupported frame shape: {frame.shape}")

    def _downsample_frame(self, luma: np.ndarray) -> np.ndarray:
        """Spatially downsample by striding (no interpolation)."""
        d = self._downsample
        return luma[::d, ::d]

    def _bright_centroid(self, luma: np.ndarray) -> float | None:
        """Return normalised horizontal centroid of the bright region, or None.

        Thresholds near ``max_luma * _threshold``; returns ``None`` when the
        bright region is too small to be meaningful (featureless / uniform frame).

        Returns a value in [-1, 1] where -1 = leftmost column, +1 = rightmost.
        """
        max_luma = float(luma.max())
        if max_luma < 1.0:
            return None  # basically black frame

        bright_mask = luma >= max_luma * self._threshold
        bright_count = int(bright_mask.sum())
        total = luma.size

        if bright_count < self._min_bright_fraction * total:
            return None  # no distinct bright region

        # Column centroid from 0..W-1, normalised to [-1, 1]
        col_indices = np.arange(luma.shape[1], dtype=np.float32)
        col_sum = float(bright_mask.sum(axis=0) @ col_indices)
        centroid_col = col_sum / bright_count
        normalised = (centroid_col / (luma.shape[1] - 1)) * 2.0 - 1.0
        return float(np.clip(normalised, -1.0, 1.0))

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, frame: np.ndarray) -> LightResult:
        """Feed one camera frame and return a :class:`LightResult`.

        Parameters
        ----------
        frame:
            A numpy array of shape (H, W) or (H, W, 3) or (H, W, 4).
            Values can be uint8 (0–255) or float (0–1 or 0–255); the luma
            conversion operates on the raw values so keep them consistent.

        Returns
        -------
        LightResult
            ``changed=True`` only when the scene changed meaningfully vs the
            running baseline (luma delta above *change_threshold* **and** a
            detectable centroid shift above *centroid_shift_threshold*, or a
            large luma delta on its own when no baseline centroid exists yet).
        """
        luma_full = self._to_luma(frame)
        luma = self._downsample_frame(luma_full)

        mean_luma = float(luma.mean())
        direction = self._bright_centroid(luma)

        # Determine whether this frame represents a meaningful change
        changed = self._evaluate_change(mean_luma, direction)

        # Update rolling baselines
        self._luma_history.append(mean_luma)
        if direction is not None:
            self._centroid_history.append(direction)

        return LightResult(direction=direction, mean_luma=mean_luma, changed=changed)

    def _evaluate_change(self, mean_luma: float, direction: float | None) -> bool:
        """Return True only when the scene changed meaningfully vs baseline."""
        if len(self._luma_history) < 3:
            # Not enough history — never fire on the first few frames
            return False

        baseline_luma = float(np.mean(list(self._luma_history)))
        luma_delta = abs(mean_luma - baseline_luma)

        if luma_delta < self._change_threshold:
            return False  # brightness hasn't changed enough

        # Brightness did change — also require a centroid shift (or no baseline yet)
        if direction is None:
            # No distinct bright region in this frame; a pure uniform exposure
            # change with no localized spot → not a "light event"
            return False

        if len(self._centroid_history) < 3:
            # No centroid baseline yet but there IS a distinct bright region
            # and brightness changed — fire
            return True

        baseline_centroid = float(np.mean(list(self._centroid_history)))
        centroid_shift = abs(direction - baseline_centroid)
        return centroid_shift >= self._centroid_shift_threshold
