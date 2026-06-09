"""Pure-numpy pixel-based motion detector via frame differencing.

Algorithm:
- Maintain the previous grayscale, downsampled frame.
- Per :meth:`~MotionDetector.feed` call: convert to grayscale, downsample by
  striding, compute the absolute per-pixel difference against the stored frame,
  threshold to a binary motion mask.
- If the total motion (fraction of pixels above the threshold) is below
  *threshold* → return ``None`` (no significant motion).
- Otherwise return a :class:`MotionResult` whose *direction* is the normalised
  horizontal position of the motion-mask centroid in ``[-1, 1]`` (``-1`` = far
  left, ``+1`` = far right) and whose *magnitude* is the fraction of pixels that
  exceeded the diff threshold.
- The first call (no previous frame) always returns ``None``.
"""

from __future__ import annotations

from typing import NamedTuple

import numpy as np


class MotionResult(NamedTuple):
    """Result returned by :meth:`MotionDetector.feed` when motion is detected.

    Attributes
    ----------
    direction:
        Normalised horizontal position of the motion centroid in ``[-1, 1]``.
        ``-1`` means the centroid is at the far-left column, ``+1`` at the
        far-right column.
    magnitude:
        Fraction of (downsampled) pixels that exceeded the diff threshold,
        in ``[0, 1]``.
    """

    direction: float
    magnitude: float


class MotionDetector:
    """Detect motion in a stream of raw frames using frame differencing.

    Parameters
    ----------
    threshold:
        Minimum fraction of pixels that must exceed the per-pixel diff
        cutoff before motion is reported.  Values in ``(0, 1]``; default
        ``0.01`` (1 % of pixels).
    downsample:
        Stride for spatial downsampling before differencing.  A value of
        ``4`` reduces a 480×640 frame to 120×160 before any arithmetic,
        keeping CPU usage low on embedded hardware.  Default ``4``.
    diff_cutoff:
        Per-pixel absolute difference (in uint8 grey-level units) required
        to count a pixel as "moving".  Default ``20``.
    """

    def __init__(
        self,
        *,
        threshold: float = 0.01,
        downsample: int = 4,
        diff_cutoff: float = 20.0,
    ) -> None:
        if threshold <= 0 or threshold > 1:
            raise ValueError("threshold must be in (0, 1]")
        if downsample < 1:
            raise ValueError("downsample must be >= 1")
        if diff_cutoff <= 0:
            raise ValueError("diff_cutoff must be > 0")

        self._threshold = threshold
        self._downsample = downsample
        self._diff_cutoff = diff_cutoff
        self._prev: np.ndarray | None = None  # last downsampled grayscale frame

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def feed(self, frame: np.ndarray) -> MotionResult | None:
        """Feed one camera frame and return a :class:`MotionResult` or ``None``.

        Parameters
        ----------
        frame:
            ``H × W`` (grayscale) or ``H × W × C`` (colour) uint8 numpy array.
            Colour channels are collapsed to grey via the standard BT.601
            luminance coefficients.  Float frames are accepted; values are
            assumed to be in ``[0, 255]`` (or ``[0, 1]`` if all ≤ 1.0, in
            which case they are rescaled automatically).

        Returns
        -------
        MotionResult | None
            ``None`` when this is the first frame, or when total motion is
            below *threshold*.  Otherwise a :class:`MotionResult` with a
            *direction* in ``[-1, 1]`` and a *magnitude* in ``(0, 1]``.
        """
        grey = self._to_grey(frame)
        small = self._downsample_frame(grey)

        if self._prev is None:
            self._prev = small
            return None

        diff = np.abs(small.astype(np.float32) - self._prev.astype(np.float32))
        self._prev = small

        mask = diff > self._diff_cutoff
        magnitude = float(mask.mean())

        if magnitude < self._threshold:
            return None

        direction = self._centroid_direction(mask)
        return MotionResult(direction=direction, magnitude=magnitude)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_grey(frame: np.ndarray) -> np.ndarray:
        """Convert a frame to uint8 grayscale."""
        arr = np.asarray(frame)

        # Rescale float [0,1] → [0,255]
        if arr.dtype.kind == "f" and arr.max() <= 1.0:
            arr = (arr * 255.0).astype(np.float32)

        if arr.ndim == 2:
            return arr.astype(np.uint8)

        if arr.ndim == 3:
            if arr.shape[2] == 1:
                return arr[:, :, 0].astype(np.uint8)
            # BT.601 luminance: 0.299 R + 0.587 G + 0.114 B
            r = arr[:, :, 0].astype(np.float32)
            g = arr[:, :, 1].astype(np.float32)
            b = arr[:, :, 2].astype(np.float32)
            grey = 0.299 * r + 0.587 * g + 0.114 * b
            return grey.astype(np.uint8)

        raise ValueError(f"Unsupported frame shape: {arr.shape}")

    def _downsample_frame(self, grey: np.ndarray) -> np.ndarray:
        """Stride-downsample a 2-D grayscale frame."""
        s = self._downsample
        return grey[::s, ::s]

    @staticmethod
    def _centroid_direction(mask: np.ndarray) -> float:
        """Return normalised horizontal centroid of *mask* in ``[-1, 1]``.

        If the mask is entirely empty (shouldn't happen after the magnitude
        check, but guarded for safety) returns ``0.0``.
        """
        cols = np.nonzero(mask)[1]
        if cols.size == 0:
            return 0.0
        centroid_col = float(cols.mean())
        width = mask.shape[1]
        # Map [0, width-1] → [-1, 1]
        return 2.0 * centroid_col / max(width - 1, 1) - 1.0
