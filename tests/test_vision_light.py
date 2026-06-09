"""Tests for reachy.vision.light.LightDetector.

Covers:
1. Bright spot on the left → direction < 0.
2. Bright spot on the right → direction > 0.
3. Uniform exposure shift (whole frame brightens evenly, no localized spot,
   no centroid shift) → does NOT fire changed=True.
4. Sudden localized bright spot appearing after a uniform baseline → fires changed=True.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy.vision.light import LightDetector, LightResult

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _uniform_frame(height: int = 80, width: int = 80, value: float = 100.0) -> np.ndarray:
    """Return a flat gray frame (2-D, float32)."""
    return np.full((height, width), value, dtype=np.float32)


def _frame_with_spot(
    height: int = 80,
    width: int = 80,
    base: float = 50.0,
    spot_value: float = 240.0,
    spot_col_start: int = 0,
    spot_col_end: int = 10,
) -> np.ndarray:
    """Return a frame that is mostly *base* with a bright column band."""
    frame = np.full((height, width), base, dtype=np.float32)
    frame[:, spot_col_start:spot_col_end] = spot_value
    return frame


# ---------------------------------------------------------------------------
# Test 1 & 2: direction on correct side
# ---------------------------------------------------------------------------


class TestBrightSpotDirection:
    """LightDetector correctly locates the bright region."""

    def _make_detector(self) -> LightDetector:
        return LightDetector(
            threshold=0.80,
            change_threshold=1.0,  # very low so direction tests are not gated by change
            centroid_shift_threshold=0.0,
            history=20,
            downsample=1,
            min_bright_fraction=0.001,
        )

    def test_bright_spot_left_gives_negative_direction(self) -> None:
        """A bright band on the left quarter → direction < 0."""
        detector = self._make_detector()
        width = 80
        frame = _frame_with_spot(
            height=80,
            width=width,
            base=50.0,
            spot_value=240.0,
            spot_col_start=0,
            spot_col_end=width // 4,
        )
        result = detector.feed(frame)
        assert isinstance(result, LightResult)
        assert result.direction is not None, "expected a bright-region direction"
        assert result.direction < 0.0, f"expected direction < 0, got {result.direction}"

    def test_bright_spot_right_gives_positive_direction(self) -> None:
        """A bright band on the right quarter → direction > 0."""
        detector = self._make_detector()
        width = 80
        frame = _frame_with_spot(
            height=80,
            width=width,
            base=50.0,
            spot_value=240.0,
            spot_col_start=3 * width // 4,
            spot_col_end=width,
        )
        result = detector.feed(frame)
        assert result.direction is not None, "expected a bright-region direction"
        assert result.direction > 0.0, f"expected direction > 0, got {result.direction}"

    def test_result_direction_is_clamped(self) -> None:
        """direction is always in [-1, 1]."""
        detector = self._make_detector()
        # Spot in the very last column
        frame = np.full((40, 40), 10.0, dtype=np.float32)
        frame[:, -1] = 255.0
        result = detector.feed(frame)
        assert result.direction is not None
        assert -1.0 <= result.direction <= 1.0


# ---------------------------------------------------------------------------
# Test 3: uniform exposure shift does NOT fire changed
# ---------------------------------------------------------------------------


class TestUniformExposureNoChange:
    """A whole-frame brightness increase with no localized spot → not a light event."""

    def test_uniform_brightness_increase_does_not_fire(self) -> None:
        """Feed a flat baseline, then a uniformly brighter flat frame → changed=False.

        A uniform exposure shift has no localized bright region (the bright-region
        centroid does not shift because the *whole* frame brightens equally).
        The detector must not report changed=True for this case.
        """
        detector = LightDetector(
            threshold=0.85,
            change_threshold=5.0,
            centroid_shift_threshold=0.10,
            history=20,
            downsample=1,
            min_bright_fraction=0.005,
        )
        # Prime the rolling baseline with uniform dark frames
        for _ in range(10):
            detector.feed(_uniform_frame(value=80.0))

        # Now a uniformly brighter frame — no localized spot
        result = detector.feed(_uniform_frame(value=130.0))

        assert not result.changed, (
            "A uniform exposure shift with no localized bright region should NOT "
            f"fire changed=True; got direction={result.direction}, changed={result.changed}"
        )

    def test_direction_is_none_for_uniform_frame(self) -> None:
        """A perfectly uniform frame has no distinct bright region → direction=None."""
        detector = LightDetector(downsample=1, min_bright_fraction=0.005)
        result = detector.feed(_uniform_frame(value=150.0))
        # All pixels equal max → the bright mask covers the whole frame which is
        # above min_bright_fraction, BUT the centroid will land exactly in the centre.
        # The important thing here is the algorithm doesn't crash; we accept either
        # None or a centre-ish direction for a perfectly flat frame.
        assert (
            result.direction is None or abs(result.direction) < 0.05
        ), f"Uniform frame should give direction≈0 or None, got {result.direction}"


# ---------------------------------------------------------------------------
# Test 4: sudden localized bright spot → fires changed=True
# ---------------------------------------------------------------------------


class TestLocalizedSpotFiresChanged:
    """A sudden localized bright spot after a uniform dark baseline → changed=True."""

    def test_localized_spot_fires_changed(self) -> None:
        """Prime with dark uniform frames, then feed a frame with a bright left spot."""
        detector = LightDetector(
            threshold=0.80,
            change_threshold=5.0,
            centroid_shift_threshold=0.10,
            history=20,
            downsample=1,
            min_bright_fraction=0.001,
        )
        width = 80
        # Prime baseline: uniform dark
        for _ in range(10):
            detector.feed(_uniform_frame(width=width, value=60.0))

        # Sudden bright spot on the left
        spot_frame = _frame_with_spot(
            width=width,
            base=60.0,
            spot_value=240.0,
            spot_col_start=0,
            spot_col_end=width // 5,
        )
        result = detector.feed(spot_frame)

        assert result.changed, (
            "A sudden bright spot after a uniform baseline should fire changed=True; "
            f"got direction={result.direction}, changed={result.changed}"
        )
        assert result.direction is not None
        assert result.direction < 0.0, "spot is on the left, expected direction < 0"

    def test_no_change_when_baseline_already_has_spot(self) -> None:
        """If the same bright spot has been there for many frames, changed should settle."""
        detector = LightDetector(
            threshold=0.80,
            change_threshold=5.0,
            centroid_shift_threshold=0.10,
            history=20,
            downsample=1,
            min_bright_fraction=0.001,
        )
        width = 80
        spot_frame = _frame_with_spot(
            width=width,
            base=60.0,
            spot_value=200.0,
            spot_col_start=0,
            spot_col_end=width // 5,
        )
        # Prime with many identical spot frames → luma baseline = mean_luma of spot frame
        for _ in range(25):
            detector.feed(spot_frame)

        # The same frame again — should not fire because it matches the baseline
        result = detector.feed(spot_frame)
        assert not result.changed, (
            "An already-stable scene should not fire changed=True; " f"got changed={result.changed}"
        )


# ---------------------------------------------------------------------------
# Misc / type contract
# ---------------------------------------------------------------------------


class TestReturnType:
    """feed() always returns a LightResult with the correct field types."""

    @pytest.mark.parametrize("ndim", [2, 3])
    def test_feed_accepts_2d_and_3d_frames(self, ndim: int) -> None:
        detector = LightDetector(downsample=1)
        if ndim == 2:
            frame: np.ndarray = np.random.randint(0, 200, (64, 64), dtype=np.uint8).astype(
                np.float32
            )
        else:
            frame = np.random.randint(0, 200, (64, 64, 3), dtype=np.uint8).astype(np.float32)
        result = detector.feed(frame)
        assert isinstance(result, LightResult)
        assert isinstance(result.mean_luma, float)
        assert isinstance(result.changed, bool)
        if result.direction is not None:
            assert isinstance(result.direction, float)
            assert -1.0 <= result.direction <= 1.0

    def test_feed_black_frame_returns_none_direction(self) -> None:
        detector = LightDetector(downsample=1)
        black = np.zeros((64, 64), dtype=np.float32)
        result = detector.feed(black)
        assert result.direction is None
        assert result.mean_luma == pytest.approx(0.0)
