"""Tests for the sleep pat-wake source (reachy.sleep.patwake).

These exercise ``PatWakeSource`` standalone — no robot, no SDK. The actual
head-pose read-back, the *current* commanded sleep pose, and the clock are all
injected, so the deviation that drives the reused :class:`PatDetector` is fully
deterministic.

The defining property under test: deviation is measured against the **MOVING**
commanded sleep-breathe pose at each tick, not a fixed baseline. A read-back
that diverges from the *current* commanded pose fires; a read-back that tracks
the commanded pose exactly (zero deviation) never fires — even while the
commanded pose itself is moving.
"""

from __future__ import annotations

import itertools

import pytest

from reachy.motion.pat import PatDetector
from reachy.sleep.patwake import PatWakeSource

# ---------------------------------------------------------------------------
# Helpers — fake injected seams (read-back, commanded provider, clock)
# ---------------------------------------------------------------------------


def make_detector() -> PatDetector:
    """A detector with a tiny, deterministic trigger so a couple of presses fire.

    ``min_presses=1`` so a single deep press fires level1; the level2 threshold
    is pinned so any sustained-hold path is repeatable.
    """
    return PatDetector(min_presses=1, level2_threshold_fn=lambda: 4.0)


class FakeClock:
    """A monotonically advancing clock; each ``__call__`` steps by ``step``."""

    def __init__(self, start: float = 100.0, step: float = 0.05) -> None:
        self._t = start
        self._step = step

    def __call__(self) -> float:
        now = self._t
        self._t += self._step
        return now


def constant_commanded(pitch: float, yaw: float = 0.0):
    """A commanded-pose provider returning a fixed (pitch, yaw)."""

    def _provider() -> tuple[float, float]:
        return (pitch, yaw)

    return _provider


def moving_commanded(poses: list[tuple[float, float]]):
    """A commanded-pose provider that walks a script of (pitch, yaw) poses.

    Models the sleep-breathe motion: the commanded pose changes every tick.
    """
    it = iter(poses)
    last: list[tuple[float, float]] = [poses[-1]]

    def _provider() -> tuple[float, float]:
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return _provider


def readback(samples: list[tuple[float, float]]):
    """A head_pose read-back returning a script of (pitch, yaw) actuals."""
    it = iter(samples)
    last: list[tuple[float, float]] = [samples[-1]]

    def _read() -> tuple[float, float]:
        try:
            last[0] = next(it)
        except StopIteration:
            pass
        return last[0]

    return _read


# ---------------------------------------------------------------------------
# Test 1 — deviation against a MOVING commanded pose fires
# ---------------------------------------------------------------------------


class TestDeviationVsMovingPoseFires:
    """A press read against the *current* (moving) commanded pose triggers."""

    def test_deviation_from_moving_commanded_pose_fires(self) -> None:
        # The sleep-breathe commanded pitch drifts up across ticks.
        commanded_poses = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (3.0, 0.0)]
        # The actual read-back sits well BELOW each commanded pitch — a steady
        # downward press relative to the moving commanded pose (deviation ~ -5 deg).
        actuals = [(p - 5.0, y) for (p, y) in commanded_poses]

        src = PatWakeSource(
            read_head_pose=readback(actuals),
            commanded_pose=moving_commanded(commanded_poses),
            detector=make_detector(),
        )

        clock = FakeClock(step=0.05)
        fired = [src.poll(now=clock()) for _ in commanded_poses]
        assert any(fired), "a press relative to the moving commanded pose must fire"

    def test_returns_true_only_on_the_firing_tick(self) -> None:
        # Single deep press, single tick: with min_presses=1 it fires immediately.
        src = PatWakeSource(
            read_head_pose=lambda: (-5.0, 0.0),  # 5 deg below commanded
            commanded_pose=constant_commanded(0.0),
            detector=make_detector(),
        )
        # now past the detector's pat_cooldown (2.0 s) so idle->level1 can fire.
        assert src.poll(now=100.0) is True


# ---------------------------------------------------------------------------
# Test 2 — zero deviation (actual == commanded) does NOT fire
# ---------------------------------------------------------------------------


class TestZeroDeviationDoesNotFire:
    """Tracking the moving commanded pose exactly never fires a pat-wake."""

    def test_actual_equals_moving_commanded_never_fires(self) -> None:
        # The commanded pose moves (sleep-breathe) and the actual read-back
        # tracks it EXACTLY — zero deviation at every tick.
        commanded_poses = [(0.0, 0.0), (1.0, 0.0), (2.0, 0.0), (1.0, 0.0), (0.0, 0.0)] * 4
        provider = moving_commanded(commanded_poses)
        # Read-back mirrors whatever the commanded provider yields this tick.
        # We snapshot the same script so actual == commanded each tick.
        actual = moving_commanded(list(commanded_poses))

        src = PatWakeSource(
            read_head_pose=actual,
            commanded_pose=provider,
            detector=make_detector(),
        )

        clock = FakeClock(step=0.05)
        fired = [src.poll(now=clock()) for _ in commanded_poses]
        assert not any(fired), "zero deviation vs the moving commanded pose must never fire"

    def test_naive_fixed_baseline_would_misfire_but_moving_does_not(self) -> None:
        # The commanded pose swings to a large pitch (sleep-breathe dip). The
        # actual read-back tracks it exactly. A NAIVE detector comparing against
        # a FIXED baseline of 0 would read a big deviation and misfire; comparing
        # against the moving commanded pose yields zero deviation -> no fire.
        commanded_poses = [(0.0, 0.0), (4.0, 0.0), (8.0, 0.0), (4.0, 0.0), (0.0, 0.0)] * 4
        actual = moving_commanded(list(commanded_poses))

        src = PatWakeSource(
            read_head_pose=actual,
            commanded_pose=moving_commanded(commanded_poses),
            detector=make_detector(),
        )

        clock = FakeClock(step=0.05)
        fired = [src.poll(now=clock()) for _ in commanded_poses]
        assert not any(fired)


# ---------------------------------------------------------------------------
# Test 3 — injected seams: fake readback + injected commanded pose + fake clock
# ---------------------------------------------------------------------------


class TestInjectedSeams:
    """All three determinism seams are injectable and exercised, no robot."""

    def test_all_seams_injected_no_robot(self) -> None:
        read_calls = {"n": 0}
        cmd_calls = {"n": 0}

        def read() -> tuple[float, float]:
            read_calls["n"] += 1
            return (-5.0, 0.0)

        def commanded() -> tuple[float, float]:
            cmd_calls["n"] += 1
            return (0.0, 0.0)

        ticks = itertools.count(start=0.0, step=0.05)

        src = PatWakeSource(
            read_head_pose=read,
            commanded_pose=commanded,
            detector=make_detector(),
        )

        for _ in range(3):
            src.poll(now=next(ticks))

        assert read_calls["n"] == 3, "read_head_pose must be polled once per tick"
        assert cmd_calls["n"] == 3, "commanded_pose provider must be queried once per tick"

    def test_detector_is_reused_not_reimplemented(self) -> None:
        # The source must drive a real PatDetector instance (the contract: reuse,
        # don't reimplement detection).
        detector = make_detector()
        src = PatWakeSource(
            read_head_pose=lambda: (-5.0, 0.0),
            commanded_pose=constant_commanded(0.0),
            detector=detector,
        )
        assert src.detector is detector
        assert isinstance(src.detector, PatDetector)

    def test_default_detector_built_when_none_given(self) -> None:
        src = PatWakeSource(
            read_head_pose=lambda: (0.0, 0.0),
            commanded_pose=constant_commanded(0.0),
        )
        assert isinstance(src.detector, PatDetector)

    def test_yaw_side_pat_against_moving_pose_fires(self) -> None:
        # A sideways nudge relative to a moving commanded yaw also fires.
        commanded_poses = [(0.0, 0.0), (0.0, 1.0), (0.0, 2.0)]
        actuals = [(p, y + 5.0) for (p, y) in commanded_poses]  # 5 deg yaw deviation
        src = PatWakeSource(
            read_head_pose=readback(actuals),
            commanded_pose=moving_commanded(commanded_poses),
            detector=make_detector(),
        )
        clock = FakeClock(step=0.05)
        fired = [src.poll(now=clock()) for _ in commanded_poses]
        assert any(fired)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
