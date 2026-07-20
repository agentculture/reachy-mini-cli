"""Tests for the shared RMS (loudness) helper — TDD-first, written before rms.py.

``reachy.motion.snap.SnapDetector.feed`` already computes ``sqrt(mean(chunk**2))``
inline. Task t12 (an ``rms`` sense provider for the symbolic behavior runtime,
``reachy/behavior/rms_sense.py``) needs the exact same maths and must not invent
a second definition, so this extracts the one-line formula into
:func:`reachy.motion.rms.compute_rms` and re-points ``SnapDetector`` at it. These
tests pin both halves of that contract: the formula itself, and that
``SnapDetector`` keeps behaving identically after the extraction.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy.motion.rms import compute_rms
from reachy.motion.snap import SnapDetector


def _quiet(n: int = 512, amplitude: float = 0.001) -> np.ndarray:
    return (np.random.default_rng(0).uniform(-amplitude, amplitude, n)).astype(np.float32)


def _loud(n: int = 512, amplitude: float = 0.5) -> np.ndarray:
    return (np.random.default_rng(1).uniform(-amplitude, amplitude, n)).astype(np.float32)


class TestComputeRms:
    def test_matches_the_inline_formula_snap_detector_used(self) -> None:
        chunk = _loud()
        expected = float(np.sqrt(np.mean(chunk.astype(np.float32) ** 2)))
        assert compute_rms(chunk) == pytest.approx(expected)

    def test_silence_is_zero(self) -> None:
        assert compute_rms(np.zeros(256, dtype=np.float32)) == pytest.approx(0.0)

    def test_a_constant_chunk_equals_its_own_absolute_value(self) -> None:
        chunk = np.full(100, 0.25, dtype=np.float32)
        assert compute_rms(chunk) == pytest.approx(0.25, abs=1e-6)

    def test_returns_a_plain_python_float(self) -> None:
        assert isinstance(compute_rms(_loud()), float)

    def test_accepts_a_plain_list_like_the_original_inline_call_shape(self) -> None:
        # np.asarray(...) inside compute_rms means a non-ndarray numeric
        # sequence works too -- a slightly more permissive but still pure leaf.
        assert compute_rms([1.0, -1.0]) == pytest.approx(1.0)


class TestSnapDetectorUnchangedAfterExtraction:
    """Regression guard: SnapDetector.feed must fire exactly as before t12."""

    def test_fires_on_first_spike_after_quiet_priming(self) -> None:
        det = SnapDetector()
        for _ in range(25):
            assert not det.feed(_quiet())
        assert det.feed(_loud())

    def test_does_not_refire_without_a_quiet_gap(self) -> None:
        det = SnapDetector()
        for _ in range(25):
            det.feed(_quiet())
        assert det.feed(_loud())
        assert not det.feed(_loud())
