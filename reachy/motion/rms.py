"""Pure RMS (loudness) computation for one mic chunk.

Extracted from :meth:`reachy.motion.snap.SnapDetector.feed`'s inline
``sqrt(mean(chunk**2))`` so there is exactly ONE definition of "loudness" in
this repo. :class:`~reachy.motion.snap.SnapDetector` now calls this helper
instead of computing it inline, and
:mod:`reachy.behavior.rms_sense` (the symbolic behavior runtime's ``Sense.rms``
provider, task t12) reuses the identical maths rather than inventing a second
one.

Deliberately tiny and pure: no state, no I/O, numpy + stdlib only.
"""

from __future__ import annotations

import numpy as np


def compute_rms(chunk) -> float:
    """Root-mean-square loudness of *chunk*: ``sqrt(mean(chunk**2))``.

    *chunk* is a 1-D (or any-shape) numeric array-like of audio samples for one
    mic read; cast to ``float32`` before squaring, matching the pre-extraction
    inline computation exactly (bit-for-bit, for the same input). This function
    assumes non-empty, numeric input -- callers filter out ``None``/empty
    chunks first (see :class:`reachy.motion.snap.SnapDetector.feed`'s guard and
    :func:`reachy.behavior.rms_sense.rms_from_chunk`'s), exactly as the
    original inline call site did.
    """
    return float(np.sqrt(np.mean(np.asarray(chunk).astype(np.float32) ** 2)))
