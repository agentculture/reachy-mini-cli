"""Tests for reachy.vision.motion.MotionDetector."""

from __future__ import annotations

import numpy as np
import pytest

from reachy.vision.motion import MotionDetector, MotionResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_frame(height: int, width: int, background: int = 80) -> np.ndarray:
    """Return a flat grey uint8 frame."""
    return np.full((height, width), background, dtype=np.uint8)


def _place_blob(
    frame: np.ndarray,
    col_center: int,
    blob_width: int = 20,
    blob_height: int = 20,
    value: int = 200,
) -> np.ndarray:
    """Return a copy of *frame* with a bright rectangular blob placed at *col_center*."""
    out = frame.copy()
    h, w = out.shape
    row_start = max(0, h // 2 - blob_height // 2)
    row_end = min(h, h // 2 + blob_height // 2)
    col_start = max(0, col_center - blob_width // 2)
    col_end = min(w, col_center + blob_width // 2)
    out[row_start:row_end, col_start:col_end] = value
    return out


# ---------------------------------------------------------------------------
# First-frame test
# ---------------------------------------------------------------------------


class TestFirstFrame:
    def test_first_frame_returns_none(self) -> None:
        det = MotionDetector()
        frame = _make_frame(64, 64)
        assert det.feed(frame) is None


# ---------------------------------------------------------------------------
# Below-threshold noise test
# ---------------------------------------------------------------------------


class TestBelowThresholdNoise:
    def test_low_noise_returns_none(self) -> None:
        """Uniform tiny perturbations across every pixel should stay below threshold."""
        rng = np.random.default_rng(42)
        det = MotionDetector(threshold=0.05, diff_cutoff=15.0)
        h, w = 64, 64
        # Seed the detector with a base frame
        base = _make_frame(h, w, background=100)
        det.feed(base)
        # Feed 20 noisy frames — each pixel wiggles ±3 grey levels (well below cutoff)
        for _ in range(20):
            noise = rng.integers(-3, 4, size=(h, w), dtype=np.int16)
            noisy = np.clip(base.astype(np.int16) + noise, 0, 255).astype(np.uint8)
            result = det.feed(noisy)
            assert result is None, f"Expected None for below-threshold noise, got {result}"


# ---------------------------------------------------------------------------
# Blob sweep left → right
# ---------------------------------------------------------------------------


class TestBlobSweep:
    """A bright blob sweeping from left to right should produce directions
    that are negative on the left half and positive on the right half,
    and should increase (move right) as the blob advances."""

    def _sweep(
        self,
        positions: list[int],
        width: int = 120,
        height: int = 80,
    ) -> list[MotionResult | None]:
        det = MotionDetector(threshold=0.005, diff_cutoff=10.0, downsample=2)
        bg = _make_frame(height, width, background=50)
        results: list[MotionResult | None] = []
        prev_col = positions[0]
        # Seed with the first blob position (no diff yet)
        det.feed(_place_blob(bg, prev_col))
        for col in positions[1:]:
            frame = _place_blob(bg, col)
            results.append(det.feed(frame))
        return results

    def test_left_blob_gives_negative_direction(self) -> None:
        """Blob on the left quarter of the frame → direction < 0."""
        width = 120
        # Move from col 20 to col 22 (left quarter)
        results = self._sweep([20, 22], width=width)
        non_none = [r for r in results if r is not None]
        assert non_none, "Expected at least one MotionResult from a left-blob move"
        for r in non_none:
            assert r.direction < 0, f"Left blob should give negative direction, got {r.direction}"

    def test_right_blob_gives_positive_direction(self) -> None:
        """Blob on the right quarter of the frame → direction > 0."""
        width = 120
        # Move from col 98 to col 100 (right quarter)
        results = self._sweep([98, 100], width=width)
        non_none = [r for r in results if r is not None]
        assert non_none, "Expected at least one MotionResult from a right-blob move"
        for r in non_none:
            assert r.direction > 0, f"Right blob should give positive direction, got {r.direction}"

    def test_direction_increases_across_sweep(self) -> None:
        """Directions returned during a left-to-right sweep should be ordered."""
        width = 120
        # Sweep the blob across the full width in 6 steps
        positions = [10, 30, 50, 70, 90, 110]
        results = self._sweep(positions, width=width)
        non_none = [r for r in results if r is not None]
        assert (
            len(non_none) >= 3
        ), f"Expected at least 3 MotionResults from the sweep, got {len(non_none)}"
        directions = [r.direction for r in non_none]
        # Each successive direction should be greater than the previous
        for i in range(1, len(directions)):
            assert (
                directions[i] > directions[i - 1]
            ), f"Direction should increase step {i}: {directions[i - 1]:.3f} → {directions[i]:.3f}"

    def test_magnitude_in_range(self) -> None:
        """Magnitude must be in (0, 1] for any returned result."""
        positions = [10, 40, 70, 100]
        results = self._sweep(positions)
        for r in results:
            if r is not None:
                assert 0 < r.magnitude <= 1.0, f"magnitude out of range: {r.magnitude}"


# ---------------------------------------------------------------------------
# Direction boundary tests
# ---------------------------------------------------------------------------


class TestDirectionBounds:
    def test_direction_in_minus1_to_plus1(self) -> None:
        """direction must always lie in [-1, 1]."""
        det = MotionDetector(threshold=0.001, diff_cutoff=5.0, downsample=1)
        h, w = 40, 40
        bg = _make_frame(h, w, background=30)
        det.feed(bg)
        for col in range(0, w, 4):
            frame = _place_blob(bg, col, blob_width=8, blob_height=8, value=220)
            result = det.feed(frame)
            if result is not None:
                assert (
                    -1.0 <= result.direction <= 1.0
                ), f"direction {result.direction} out of [-1, 1] for col {col}"
            det.feed(bg)  # reset to background so next blob fires a diff


# ---------------------------------------------------------------------------
# MotionResult is a NamedTuple with correct field names
# ---------------------------------------------------------------------------


class TestMotionResultType:
    def test_result_fields(self) -> None:
        r = MotionResult(direction=0.5, magnitude=0.1)
        assert r.direction == 0.5
        assert r.magnitude == 0.1

    def test_result_is_namedtuple(self) -> None:
        r = MotionResult(direction=-0.3, magnitude=0.05)
        assert isinstance(r, tuple)
        assert r._fields == ("direction", "magnitude")


# ---------------------------------------------------------------------------
# Constructor validation
# ---------------------------------------------------------------------------


class TestConstructorValidation:
    def test_invalid_threshold_zero(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            MotionDetector(threshold=0.0)

    def test_invalid_threshold_negative(self) -> None:
        with pytest.raises(ValueError, match="threshold"):
            MotionDetector(threshold=-0.1)

    def test_invalid_downsample(self) -> None:
        with pytest.raises(ValueError, match="downsample"):
            MotionDetector(downsample=0)

    def test_invalid_diff_cutoff(self) -> None:
        with pytest.raises(ValueError, match="diff_cutoff"):
            MotionDetector(diff_cutoff=0.0)


# ---------------------------------------------------------------------------
# Colour frame acceptance
# ---------------------------------------------------------------------------


class TestColourFrame:
    def test_rgb_frame_accepted(self) -> None:
        """MotionDetector must handle H×W×3 colour frames."""
        det = MotionDetector(threshold=0.005, diff_cutoff=10.0)
        h, w = 64, 64
        bg_rgb = np.full((h, w, 3), 80, dtype=np.uint8)
        det.feed(bg_rgb)
        # Place a bright blob in the right half
        moved = bg_rgb.copy()
        moved[20:40, 50:60, :] = 220
        result = det.feed(moved)
        # Either detects motion (positive direction, right side) or returns None
        # — but must not raise
        if result is not None:
            assert result.direction > 0
