"""Pure RMS-based snap (sudden loud transient) detector.

Cited from ``reachy_nova.tracking.TrackingManager.detect_snap`` — logic adapted
verbatim; all DoA / transport / I/O coupling removed so this module has no
dependencies beyond ``numpy`` and the standard library.

Algorithm:
- Maintain a rolling window (deque, maxlen=*history*) of recent per-chunk RMS values.
- Per chunk: ``rms = sqrt(mean(audio**2))``.
- ``rolling_avg = mean(history[:-1])``; skip if too few samples or rolling_avg ≈ 0.
- Fire when ``rms > ratio * rolling_avg AND rms > min_rms AND prev_chunk_low``
  (edge-triggered — won't re-fire while the signal stays loud).
- Update ``_prev_chunk_low`` flag (``rms < 2 * rolling_avg``) after each chunk.
"""

from __future__ import annotations

from collections import deque

import numpy as np


class SnapDetector:
    """Detect sharp audio transients (snaps, claps) from a stream of mic chunks.

    Parameters
    ----------
    ratio:
        How many times louder than the rolling average the current chunk must be
        to count as a snap (default 5.0 — matches reachy_nova source).
    min_rms:
        Absolute RMS floor.  Chunks below this are treated as ambient noise
        regardless of their ratio to the rolling average (default 0.02).
    history:
        Rolling-window length in chunks (default 30).
    """

    def __init__(
        self,
        *,
        ratio: float = 5.0,
        min_rms: float = 0.02,
        history: int = 30,
    ) -> None:
        self._ratio = ratio
        self._min_rms = min_rms
        self._history: deque[float] = deque(maxlen=history)
        self._prev_chunk_low: bool = True  # edge-trigger gate

    @property
    def min_rms(self) -> float:
        """Absolute RMS floor below which a chunk is treated as ambient noise."""
        return self._min_rms

    def feed(self, audio: np.ndarray) -> bool:
        """Feed one mic chunk (float32 ndarray). Return True only on a fresh loud spike.

        Parameters
        ----------
        audio:
            1-D float32 numpy array of audio samples for a single chunk.

        Returns
        -------
        bool
            ``True`` when a snap is detected on *this* chunk (edge-triggered —
            returns ``False`` on every subsequent chunk until a quiet gap occurs).
        """
        if audio is None or len(audio) == 0:
            return False

        rms: float = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        self._history.append(rms)

        # Need at least 5 samples to form a meaningful rolling average
        if len(self._history) < 5:
            return False

        rolling_avg: float = float(np.mean(list(self._history)[:-1]))

        if rolling_avg < 1e-6:
            self._prev_chunk_low = True
            return False

        is_spike = rms > self._ratio * rolling_avg and rms > self._min_rms
        was_quiet = self._prev_chunk_low

        fired = is_spike and was_quiet

        # Update edge-trigger flag: chunk is "quiet" when it is below 2× average
        self._prev_chunk_low = rms < 2.0 * rolling_avg

        return fired
