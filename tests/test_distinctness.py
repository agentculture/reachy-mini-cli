"""Tests for reachy.speech.distinctness — expression distinctness scoring.

Acceptance criteria:
  1. A distinctness function scores any two catalog expressions over head/antenna/body
     params and flags pairs whose score is below a threshold (too similar).
  2. On a deliberately-duplicated catalog (two emojis with identical/near-identical
     poses) it flags at least one too-similar pair; on the shipped distinct starter
     set it reports clean (no flagged pairs). Unit-test both.
"""

from __future__ import annotations

import math

import pytest

from reachy.speech.distinctness import (
    DEFAULT_THRESHOLD,
    distance,
    find_too_similar,
)
from reachy.speech.expressions import (
    NEUTRAL_KEY,
    Catalog,
    ExpressionPose,
    load_catalog,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NEUTRAL = ExpressionPose()

# Two deliberately identical/near-identical poses — used in the "duplicate" catalog
_POSE_ALPHA = ExpressionPose(
    head_x=0.0,
    head_y=0.0,
    head_z=3.0,
    head_roll=0.0,
    head_pitch=-3.0,
    head_yaw=0.0,
    antenna_right=5.0,
    antenna_left=5.0,
    body_yaw=0.0,
)
_POSE_ALPHA_CLONE = ExpressionPose(
    head_x=0.0,
    head_y=0.0,
    head_z=3.0,
    head_roll=0.0,
    head_pitch=-3.0,
    head_yaw=0.0,
    antenna_right=5.0,
    antenna_left=5.0,
    body_yaw=0.0,
)
# A tiny tweak — still within threshold
_POSE_ALPHA_NEAR = ExpressionPose(
    head_x=0.0,
    head_y=0.0,
    head_z=3.05,  # 0.05 mm difference in z
    head_roll=0.0,
    head_pitch=-3.0,
    head_yaw=0.0,
    antenna_right=5.0,
    antenna_left=5.0,
    body_yaw=0.0,
)


# ---------------------------------------------------------------------------
# Acceptance criterion 1a — distance() scores two poses
# ---------------------------------------------------------------------------


def test_distance_returns_float() -> None:
    """distance() returns a float value for any two ExpressionPose inputs."""
    pose_a = ExpressionPose(head_z=5.0, antenna_right=30.0)
    pose_b = ExpressionPose(head_z=2.0, head_roll=8.0)
    result = distance(pose_a, pose_b)
    assert isinstance(result, float)


def test_distance_identical_poses_is_zero() -> None:
    """distance(a, a) == 0.0 for any pose."""
    for pose in [
        _NEUTRAL,
        _POSE_ALPHA,
        ExpressionPose(head_roll=8.0, head_pitch=6.0, antenna_right=-10.0),
    ]:
        assert distance(pose, pose) == 0.0


def test_distance_is_non_negative() -> None:
    """distance() is always ≥ 0."""
    pairs = [
        (_NEUTRAL, _POSE_ALPHA),
        (_POSE_ALPHA, _POSE_ALPHA_NEAR),
        (ExpressionPose(head_z=5.0), ExpressionPose(head_z=-5.0)),
    ]
    for a, b in pairs:
        assert distance(a, b) >= 0.0


def test_distance_is_symmetric() -> None:
    """distance(a, b) == distance(b, a) (metric symmetry)."""
    pairs = [
        (_NEUTRAL, _POSE_ALPHA),
        (_POSE_ALPHA, _POSE_ALPHA_NEAR),
        (ExpressionPose(head_roll=8.0), ExpressionPose(antenna_left=30.0)),
    ]
    for a, b in pairs:
        assert distance(a, b) == pytest.approx(distance(b, a))


def test_distance_uses_all_axes() -> None:
    """distance() is sensitive to changes on every pose axis."""
    base = ExpressionPose()
    axes_and_values = [
        ("head_x", 5.0),
        ("head_y", 5.0),
        ("head_z", 5.0),
        ("head_roll", 10.0),
        ("head_pitch", 10.0),
        ("head_yaw", 10.0),
        ("antenna_right", 30.0),
        ("antenna_left", 30.0),
        ("body_yaw", 10.0),
    ]
    for field, val in axes_and_values:
        tweaked = ExpressionPose(**{field: val})  # type: ignore[arg-type]
        assert distance(base, tweaked) > 0.0, f"distance() ignored change in {field}"


def test_distance_larger_deviation_gives_larger_score() -> None:
    """A larger deviation on the same axis produces a strictly larger distance."""
    base = ExpressionPose()
    small_dev = ExpressionPose(head_z=1.0)
    large_dev = ExpressionPose(head_z=5.0)
    assert distance(base, small_dev) < distance(base, large_dev)


def test_distance_normalises_units() -> None:
    """mm and deg axes are weight-normalised: 5 mm ≈ 1 unit; 10 deg ≈ 1 unit.

    A 5 mm head translation and a 10 deg head rotation should contribute
    roughly equally (within a factor of 2) to the total distance.
    """
    base = ExpressionPose()
    trans_only = ExpressionPose(head_z=5.0)  # 5 mm → weight 1/5 → contribution 1.0
    rot_only = ExpressionPose(head_pitch=10.0)  # 10 deg → weight 1/10 → contribution 1.0
    d_trans = distance(base, trans_only)
    d_rot = distance(base, rot_only)
    # Both should land at 1.0 (unit contribution)
    assert d_trans == pytest.approx(1.0, abs=1e-9)
    assert d_rot == pytest.approx(1.0, abs=1e-9)


# ---------------------------------------------------------------------------
# Acceptance criterion 1b — find_too_similar() flags below-threshold pairs
# ---------------------------------------------------------------------------


def test_find_too_similar_returns_list() -> None:
    """find_too_similar() always returns a list (possibly empty)."""
    catalog = load_catalog()
    result = find_too_similar(catalog)
    assert isinstance(result, list)


def test_find_too_similar_result_triples() -> None:
    """Each item in the result is a (str, str, float) triple."""
    catalog = load_catalog()
    result = find_too_similar(catalog, threshold=10.0)  # permissive — may return entries
    for item in result:
        assert len(item) == 3
        key_a, key_b, score = item
        assert isinstance(key_a, str)
        assert isinstance(key_b, str)
        assert isinstance(score, float)


def test_find_too_similar_scores_match_distance() -> None:
    """Each triple's score equals distance(pose_a, pose_b) exactly."""
    catalog = load_catalog()
    result = find_too_similar(catalog, threshold=10.0)
    for key_a, key_b, score in result:
        expected = distance(catalog[key_a], catalog[key_b])
        assert score == pytest.approx(expected)


def test_find_too_similar_sorted_ascending() -> None:
    """Returned pairs are sorted by score ascending (most similar first)."""
    catalog = load_catalog()
    result = find_too_similar(catalog, threshold=10.0)
    scores = [s for _, _, s in result]
    assert scores == sorted(scores)


def test_find_too_similar_accepts_catalog_instance() -> None:
    """find_too_similar() accepts a Catalog instance, not only a raw dict."""
    cat = Catalog()
    result = find_too_similar(cat)
    assert isinstance(result, list)


def test_find_too_similar_excludes_neutral() -> None:
    """The neutral entry is excluded from pairwise comparison.

    A catalog with only one non-neutral emoji (plus neutral) should always
    return an empty list — there is nothing to compare against.
    """
    catalog = {
        NEUTRAL_KEY: _NEUTRAL,
        "🙂": _POSE_ALPHA,
    }
    result = find_too_similar(catalog, threshold=10.0)
    assert result == [], f"Neutral should not be compared; got {result}"


def test_find_too_similar_custom_threshold() -> None:
    """Raising the threshold flags more pairs; lowering it flags fewer."""
    catalog = load_catalog()
    # With a very high threshold, every pair is flagged
    wide = find_too_similar(catalog, threshold=100.0)
    # With a very low threshold, nothing is flagged
    tight = find_too_similar(catalog, threshold=0.001)
    assert len(wide) >= len(tight)
    assert tight == []


def test_find_too_similar_none_threshold_uses_default() -> None:
    """Passing threshold=None uses DEFAULT_THRESHOLD."""
    catalog = load_catalog()
    result_none = find_too_similar(catalog, threshold=None)
    result_default = find_too_similar(catalog, threshold=DEFAULT_THRESHOLD)
    assert result_none == result_default


# ---------------------------------------------------------------------------
# Acceptance criterion 2a — duplicate catalog flags at least one pair
# ---------------------------------------------------------------------------


def test_duplicate_catalog_flags_identical_pair() -> None:
    """A catalog with two emojis sharing the same pose is flagged as too similar."""
    duplicate_catalog: dict[str, ExpressionPose] = {
        NEUTRAL_KEY: _NEUTRAL,
        "😀": _POSE_ALPHA,
        "😃": _POSE_ALPHA_CLONE,  # identical pose
    }
    result = find_too_similar(duplicate_catalog)
    assert len(result) >= 1, "Identical poses should be flagged as too similar"
    keys_flagged = {(a, b) for a, b, _ in result}
    assert ("😀", "😃") in keys_flagged or ("😃", "😀") in keys_flagged


def test_duplicate_catalog_flags_near_identical_pair() -> None:
    """A catalog with two emojis with near-identical poses is flagged."""
    duplicate_catalog: dict[str, ExpressionPose] = {
        NEUTRAL_KEY: _NEUTRAL,
        "😀": _POSE_ALPHA,
        "😄": _POSE_ALPHA_NEAR,  # 0.05 mm difference — well below threshold
    }
    result = find_too_similar(duplicate_catalog)
    assert len(result) >= 1, "Near-identical poses should be flagged as too similar"


def test_duplicate_catalog_identical_score_is_zero() -> None:
    """Identical poses have distance 0.0 and are always flagged regardless of threshold."""
    duplicate_catalog: dict[str, ExpressionPose] = {
        NEUTRAL_KEY: _NEUTRAL,
        "A": _POSE_ALPHA,
        "B": _POSE_ALPHA_CLONE,
    }
    result = find_too_similar(duplicate_catalog, threshold=DEFAULT_THRESHOLD)
    assert any(
        math.isclose(score, 0.0) for _, _, score in result
    ), "An identical pair should have score 0.0"


def test_duplicate_catalog_multiple_pairs_all_flagged() -> None:
    """A catalog of three identical expressions produces three flagged pairs."""
    pose = ExpressionPose(head_z=4.0, head_pitch=8.0)
    dup_catalog: dict[str, ExpressionPose] = {
        NEUTRAL_KEY: _NEUTRAL,
        "A": pose,
        "B": pose,
        "C": pose,
    }
    result = find_too_similar(dup_catalog)
    # 3 choose 2 = 3 pairs, all should be flagged
    assert len(result) == 3, f"Expected 3 flagged pairs, got {len(result)}: {result}"


# ---------------------------------------------------------------------------
# Acceptance criterion 2b — shipped starter set reports clean
# ---------------------------------------------------------------------------


def test_starter_catalog_is_clean_with_default_threshold() -> None:
    """The shipped starter expression set has no too-similar pairs at DEFAULT_THRESHOLD.

    This is the primary regression guard: adding a near-duplicate expression to
    expressions.toml must cause this test to fail, alerting the developer.
    """
    catalog = load_catalog()
    result = find_too_similar(catalog)
    assert result == [], (
        f"Shipped starter catalog has too-similar pairs (threshold={DEFAULT_THRESHOLD}): "
        f"{[(a, b, f'{s:.4f}') for a, b, s in result]}"
    )


def test_starter_catalog_is_clean_via_catalog_instance() -> None:
    """Same check via Catalog() instance rather than raw dict."""
    cat = Catalog()
    result = find_too_similar(cat)
    assert result == [], (
        f"Catalog() finds too-similar pairs: " f"{[(a, b, f'{s:.4f}') for a, b, s in result]}"
    )


def test_starter_catalog_min_distance_above_threshold() -> None:
    """All pairwise distances in the starter set (ex-neutral) exceed DEFAULT_THRESHOLD."""
    catalog = load_catalog()
    non_neutral = {k: v for k, v in catalog.items() if k != NEUTRAL_KEY}
    keys = list(non_neutral.keys())
    for i in range(len(keys)):
        for j in range(i + 1, len(keys)):
            d = distance(non_neutral[keys[i]], non_neutral[keys[j]])
            assert d >= DEFAULT_THRESHOLD, (
                f"Pair ({keys[i]!r}, {keys[j]!r}) is too similar: "
                f"distance={d:.4f} < threshold={DEFAULT_THRESHOLD}"
            )


# ---------------------------------------------------------------------------
# Module-level API surface checks
# ---------------------------------------------------------------------------


def test_default_threshold_is_float() -> None:
    """DEFAULT_THRESHOLD is a positive float."""
    assert isinstance(DEFAULT_THRESHOLD, float)
    assert DEFAULT_THRESHOLD > 0.0


def test_module_exports() -> None:
    """The module exports exactly the documented public names."""
    import reachy.speech.distinctness as mod

    assert hasattr(mod, "distance")
    assert hasattr(mod, "find_too_similar")
    assert hasattr(mod, "DEFAULT_THRESHOLD")
    assert callable(mod.distance)
    assert callable(mod.find_too_similar)


def test_module_has_docstring() -> None:
    """reachy.speech.distinctness has a module-level docstring covering the weighting."""
    import reachy.speech.distinctness as mod

    assert mod.__doc__, "distinctness.py must have a module-level docstring"
    assert "weight" in mod.__doc__.lower() or "normaliz" in mod.__doc__.lower()
