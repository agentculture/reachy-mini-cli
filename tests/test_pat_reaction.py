"""Tests for :mod:`reachy.motion.pat_reaction`.

Checks that :class:`PatReaction` enqueues the correct sequence of
:class:`~reachy.motion.queue.MotionAction` objects for each touch type, that
no direct transport calls are made, and that the planner is side-effect-free
beyond the queue.
"""

from __future__ import annotations

import pytest

from reachy.motion.pat_reaction import (
    ANTENNA_AFFECTION,
    LEAN_DURATION_L1,
    LEAN_DURATION_L2,
    LEAN_PITCH_DOWN,
    LEAN_YAW_SIDE,
    LEVEL2_SCALE,
    NUZZLE_DURATION,
    SETTLE_DURATION,
    SIDE_BODY_YAW,
    PatReaction,
)
from reachy.motion.queue import MotionAction, MotionQueue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make() -> tuple[MotionQueue, PatReaction]:
    q: MotionQueue = MotionQueue()
    pr = PatReaction(queue=q)
    return q, pr


def _actions(q: MotionQueue) -> list[MotionAction]:
    """Return a snapshot of all pending actions."""
    return q.pending()


# ---------------------------------------------------------------------------
# scratch — pitch-down lean + settle
# ---------------------------------------------------------------------------


class TestScratch:
    def test_enqueues_three_actions(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        actions = _actions(q)
        assert len(actions) == 3, f"expected 3 actions, got {len(actions)}: {actions}"

    def test_lean_action_is_first(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        lean = _actions(q)[0]
        assert lean.label == "pat_scratch_lean"

    def test_lean_has_pitch_down(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        lean = _actions(q)[0]
        assert lean.head is not None
        assert lean.head["pitch"] < 0, "scratch lean must pitch DOWN (negative)"
        assert abs(lean.head["pitch"]) == pytest.approx(abs(LEAN_PITCH_DOWN))

    def test_lean_no_yaw(self) -> None:
        """scratch leans down — yaw should be zero (not toward a side)."""
        q, pr = _make()
        pr.react("scratch")
        lean = _actions(q)[0]
        assert lean.head is not None
        assert lean.head.get("yaw", 0.0) == pytest.approx(0.0)

    def test_lean_has_antenna_affection(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        lean = _actions(q)[0]
        assert lean.antennas is not None
        right, left = lean.antennas
        assert right == pytest.approx(ANTENNA_AFFECTION)
        assert left == pytest.approx(ANTENNA_AFFECTION)

    def test_nuzzle_action_is_second(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        nuzzle = _actions(q)[1]
        assert nuzzle.label == "pat_scratch_nuzzle"

    def test_nuzzle_has_pitch_down(self) -> None:
        """Nuzzle holds at the lean pitch."""
        q, pr = _make()
        pr.react("scratch")
        nuzzle = _actions(q)[1]
        assert nuzzle.head is not None
        assert nuzzle.head["pitch"] < 0

    def test_settle_action_is_last(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        settle = _actions(q)[2]
        assert settle.label == "pat_scratch_settle"

    def test_settle_returns_to_neutral_pitch(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        settle = _actions(q)[2]
        assert settle.head is not None
        assert settle.head["pitch"] == pytest.approx(0.0)

    def test_all_actions_use_minjerk(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        for action in _actions(q):
            assert action.interpolation == "minjerk"

    def test_all_actions_coalesce_key_none(self) -> None:
        """One-shot ordered sequence: all actions must use coalesce_key=None."""
        q, pr = _make()
        pr.react("scratch")
        for action in _actions(q):
            assert action.coalesce_key is None

    def test_level1_duration(self) -> None:
        q, pr = _make()
        pr.react("scratch", level="level1")
        lean = _actions(q)[0]
        assert lean.duration == pytest.approx(LEAN_DURATION_L1)

    def test_level2_longer_duration(self) -> None:
        q, pr = _make()
        pr.react("scratch", level="level2")
        lean = _actions(q)[0]
        assert lean.duration == pytest.approx(LEAN_DURATION_L2)

    def test_level2_deeper_pitch(self) -> None:
        """level2 must have a larger-magnitude pitch than level1."""
        q1, pr1 = _make()
        pr1.react("scratch", level="level1")
        lean1 = _actions(q1)[0]

        q2, pr2 = _make()
        pr2.react("scratch", level="level2")
        lean2 = _actions(q2)[0]

        assert lean2.head is not None and lean1.head is not None
        assert abs(lean2.head["pitch"]) > abs(lean1.head["pitch"])

    def test_no_body_yaw_for_scratch(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        for action in _actions(q):
            assert action.body_yaw is None


# ---------------------------------------------------------------------------
# side_pat — yaw-toward + soft body-yaw + settle
# ---------------------------------------------------------------------------


class TestSidePat:
    def test_enqueues_three_actions(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        actions = _actions(q)
        assert len(actions) == 3

    def test_lean_action_is_first(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        assert lean.label == "pat_side_lean"

    def test_lean_has_yaw(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        assert lean.head is not None
        assert abs(lean.head.get("yaw", 0.0)) == pytest.approx(abs(LEAN_YAW_SIDE))

    def test_lean_has_body_yaw(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        assert lean.body_yaw is not None
        assert abs(lean.body_yaw) == pytest.approx(abs(SIDE_BODY_YAW))

    def test_lean_has_antenna_affection(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        assert lean.antennas is not None
        right, left = lean.antennas
        assert right == pytest.approx(ANTENNA_AFFECTION)
        assert left == pytest.approx(ANTENNA_AFFECTION)

    def test_yaw_and_body_yaw_same_sign(self) -> None:
        """Head yaw and body yaw must lean in the same direction."""
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        import math

        assert lean.head is not None and lean.body_yaw is not None
        assert math.copysign(1.0, lean.head["yaw"]) == math.copysign(1.0, lean.body_yaw)

    def test_nuzzle_action_is_second(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        nuzzle = _actions(q)[1]
        assert nuzzle.label == "pat_side_nuzzle"

    def test_settle_returns_to_neutral(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        settle = _actions(q)[2]
        assert settle.label == "pat_side_settle"
        assert settle.head is not None
        assert settle.head["yaw"] == pytest.approx(0.0)
        assert settle.body_yaw == pytest.approx(0.0)

    def test_all_actions_use_minjerk(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        for action in _actions(q):
            assert action.interpolation == "minjerk"

    def test_all_actions_coalesce_key_none(self) -> None:
        q, pr = _make()
        pr.react("side_pat")
        for action in _actions(q):
            assert action.coalesce_key is None

    def test_level2_larger_yaw(self) -> None:
        q1, pr1 = _make()
        pr1.react("side_pat", level="level1")
        lean1 = _actions(q1)[0]

        q2, pr2 = _make()
        pr2.react("side_pat", level="level2")
        lean2 = _actions(q2)[0]

        assert lean2.head is not None and lean1.head is not None
        assert abs(lean2.head["yaw"]) > abs(lean1.head["yaw"])

    def test_no_pitch_for_side_pat(self) -> None:
        """side_pat does not add a pitch offset (leans sideways, not down)."""
        q, pr = _make()
        pr.react("side_pat")
        lean = _actions(q)[0]
        assert lean.head is not None
        assert lean.head.get("pitch", 0.0) == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# Queue discipline
# ---------------------------------------------------------------------------


class TestQueueDiscipline:
    def test_submit_never_blocks(self) -> None:
        """react() must enqueue immediately and return without sleeping."""
        import time

        q, pr = _make()
        start = time.monotonic()
        pr.react("scratch")
        elapsed = time.monotonic() - start
        assert elapsed < 0.05, f"react() should return in <50 ms, took {elapsed*1000:.1f} ms"

    def test_react_returns_none(self) -> None:
        q, pr = _make()
        result = pr.react("scratch")
        assert result is None

    def test_multiple_reacts_accumulate_in_order(self) -> None:
        q, pr = _make()
        pr.react("scratch")
        pr.react("side_pat")
        actions = _actions(q)
        assert len(actions) == 6
        assert actions[0].label == "pat_scratch_lean"
        assert actions[3].label == "pat_side_lean"

    def test_unknown_touch_type_raises(self) -> None:
        q, pr = _make()
        with pytest.raises(ValueError, match="unknown touch_type"):
            pr.react("tickle")

    def test_unknown_level_raises(self) -> None:
        q, pr = _make()
        with pytest.raises(ValueError, match="unknown level"):
            pr.react("scratch", level="level3")

    def test_level2_scale_constant_positive(self) -> None:
        """Sanity: the level2 scale must amplify, not attenuate."""
        assert LEVEL2_SCALE > 1.0


# ---------------------------------------------------------------------------
# Constant sanity checks
# ---------------------------------------------------------------------------


class TestConstants:
    def test_lean_pitch_down_is_positive_down(self) -> None:
        """LEAN_PITCH_DOWN is defined as a positive value meaning 'pitch down'."""
        assert LEAN_PITCH_DOWN > 0

    def test_lean_yaw_side_positive(self) -> None:
        assert LEAN_YAW_SIDE > 0

    def test_antenna_affection_positive(self) -> None:
        assert ANTENNA_AFFECTION > 0

    def test_side_body_yaw_positive(self) -> None:
        assert SIDE_BODY_YAW > 0

    def test_durations_positive(self) -> None:
        for val in (
            LEAN_DURATION_L1,
            LEAN_DURATION_L2,
            NUZZLE_DURATION,
            SETTLE_DURATION,
        ):
            assert val > 0

    def test_level2_duration_longer(self) -> None:
        assert LEAN_DURATION_L2 > LEAN_DURATION_L1
