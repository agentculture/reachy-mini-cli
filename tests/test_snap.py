"""Tests for SnapDetector — TDD-first: write tests before implementation."""

from __future__ import annotations

import numpy as np

from reachy.motion.snap import SnapDetector


def _quiet(n: int = 512, amplitude: float = 0.001) -> np.ndarray:
    """Return a quiet float32 audio chunk with given amplitude RMS."""
    return (np.random.default_rng(0).uniform(-amplitude, amplitude, n)).astype(np.float32)


def _loud(n: int = 512, amplitude: float = 0.5) -> np.ndarray:
    """Return a loud float32 audio chunk with given amplitude."""
    return (np.random.default_rng(1).uniform(-amplitude, amplitude, n)).astype(np.float32)


class TestSnapFiresOnce:
    """A sequence of quiet chunks followed by one loud chunk fires exactly once."""

    def test_fires_on_first_spike(self):
        det = SnapDetector()
        # Prime the rolling window with quiet chunks
        for _ in range(25):
            fired = det.feed(_quiet())
            assert not fired, "Should not fire on quiet chunks"

        # One loud chunk must fire
        assert det.feed(_loud()), "Should fire on sudden loud chunk after quiet window"

    def test_does_not_fire_second_time_immediately(self):
        det = SnapDetector()
        for _ in range(25):
            det.feed(_quiet())

        # First loud chunk fires
        assert det.feed(_loud())
        # Immediately feeding another loud chunk should NOT re-fire (no quiet gap)
        assert not det.feed(_loud()), "Should not re-fire without a quiet gap"


class TestSustainedLoudNoRefire:
    """Sustained loud audio (no quiet gap between loud chunks) must not re-fire each chunk."""

    def test_sustained_loud_fires_once_only(self):
        det = SnapDetector()
        for _ in range(25):
            det.feed(_quiet())

        fires = sum(det.feed(_loud()) for _ in range(10))
        assert fires == 1, f"Expected exactly 1 fire for sustained loud, got {fires}"


class TestSubFloorNeverFires:
    """Noise below min_rms floor must never fire, even with large relative spikes."""

    def test_sub_floor_no_fire(self):
        det = SnapDetector(min_rms=0.02)
        # Prime with near-zero quiet
        for _ in range(25):
            det.feed(np.zeros(512, dtype=np.float32))

        # "Loud" chunk that is still below the 0.02 floor
        sub_floor_loud = np.full(512, 0.005, dtype=np.float32)
        assert not det.feed(sub_floor_loud), "Sub-floor chunk must not fire"

    def test_zero_chunks_never_fire(self):
        det = SnapDetector()
        for _ in range(50):
            assert not det.feed(np.zeros(512, dtype=np.float32))


class TestGentleAmbientNeverFires:
    """Gentle ambient fluctuation (small relative change) must never fire."""

    def test_gentle_fluctuation_no_fire(self):
        det = SnapDetector(ratio=5.0, min_rms=0.02)
        rng = np.random.default_rng(42)
        # Alternate between two mild amplitudes — neither crosses ratio * avg
        for i in range(60):
            amp = 0.03 if i % 2 == 0 else 0.04  # ratio ~1.33x, well below 5x
            chunk = rng.uniform(-amp, amp, 512).astype(np.float32)
            fired = det.feed(chunk)
            assert not fired, f"Gentle fluctuation fired on chunk {i}"

    def test_2x_spike_does_not_fire(self):
        """A 2x spike (ratio=5.0 default) must not be mistaken for a snap."""
        det = SnapDetector(ratio=5.0, min_rms=0.02)
        for _ in range(25):
            det.feed(_quiet(amplitude=0.05))

        # 2x spike — well below the 5x ratio threshold
        moderate = np.full(512, 0.1, dtype=np.float32)
        assert not det.feed(moderate), "2x spike must not fire with ratio=5.0"


class TestEdgeBehaviours:
    """Miscellaneous edge cases."""

    def test_refires_after_quiet_gap(self):
        """After a snap, quiet gap then another loud chunk should fire again."""
        det = SnapDetector()
        for _ in range(25):
            det.feed(_quiet())

        assert det.feed(_loud())  # first snap

        # Re-prime with quiet chunks
        for _ in range(10):
            det.feed(_quiet())

        assert det.feed(_loud()), "Should re-fire after a quiet gap"

    def test_custom_ratio(self):
        """A lower ratio makes detection more sensitive."""
        det = SnapDetector(ratio=2.0, min_rms=0.01)
        for _ in range(25):
            det.feed(_quiet(amplitude=0.05))

        # A 3x spike should fire with ratio=2.0
        spike = np.full(512, 0.15, dtype=np.float32)
        assert det.feed(spike), "3x spike should fire with ratio=2.0"

    def test_history_too_short_no_fire(self):
        """With fewer than 5 samples in history, no snap should fire."""
        det = SnapDetector()
        for _ in range(3):
            det.feed(_loud())  # only 3 samples — should never fire
