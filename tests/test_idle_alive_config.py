"""Tests for :meth:`AliveConfig.focused` — the pure low-energy idle profile.

Governing principle: STILLNESS IS THE FOCUSED POSTURE. The focused profile is a
REDUCED variant of an :class:`AliveConfig` — quieter on every wander amplitude,
never zeroed (it still breathes) — and it preserves pacing/bookkeeping so a
caller can swap configs mid-run without changing tempo.

Its live consumer is :class:`reachy.motion.sleep.SleepProducer` (the drowsy
fade). The ``listen`` idle layer used to swap it in while ``think_active.flag``
was present; that reader retired with the folded ``listen --live`` cognition
loop (and the flag with it), so the two ``ListenProducer._idle`` tests that
drove it were dropped — the profile factory's own coverage below is what
survives, unchanged.
"""

from __future__ import annotations

import random

from reachy.motion.idle import AliveConfig, next_pose

# --------------------------------------------------------------------------- #
# AliveConfig.focused — the pure low-energy profile                           #
# --------------------------------------------------------------------------- #


def test_focused_still_breathes_not_zero() -> None:
    """Focused idle is REDUCED, not zero — breathing amplitudes stay positive."""
    base = AliveConfig()
    focused = base.focused()

    # It still breathes: vertical + pitch breathing remain strictly positive.
    assert focused.breathe_z_mm > 0.0
    assert focused.breathe_pitch_deg > 0.0
    assert focused.energy > 0.0
    # ...but smaller than the standalone breathe.
    assert focused.breathe_z_mm < base.breathe_z_mm
    assert focused.breathe_pitch_deg < base.breathe_pitch_deg


def test_focused_reduces_wander_amplitudes() -> None:
    """Gaze / antenna / body wander all back off in the focused profile."""
    base = AliveConfig()
    focused = base.focused()

    assert focused.gaze_yaw_deg < base.gaze_yaw_deg
    assert focused.gaze_pitch_deg < base.gaze_pitch_deg
    assert focused.gaze_roll_deg < base.gaze_roll_deg
    assert focused.antenna_deg < base.antenna_deg
    assert focused.body_yaw_deg < base.body_yaw_deg
    assert focused.glance_probability < base.glance_probability
    assert focused.energy < base.energy


def test_focused_preserves_pacing_and_bookkeeping() -> None:
    """Swapping configs must not change tempo / breathe period / bookkeeping."""
    base = AliveConfig(seed=7)
    focused = base.focused()

    assert focused.interval == base.interval
    assert focused.breathe_period == base.breathe_period
    assert focused.interpolation == base.interpolation
    assert focused.seed == base.seed
    assert focused.max_errors == base.max_errors


def _pose_excursion(config: AliveConfig, *, seed: int, ticks: int, dt: float) -> float:
    """Sum the absolute motion excursion of a config's poses over a window.

    Deterministic given ``seed`` (same rng stream for both configs being
    compared), so the only difference is the config's amplitudes.
    """
    rng = random.Random(seed)
    total = 0.0
    for i in range(ticks):
        pose = next_pose(i * dt, rng, config)
        head = pose["head"]
        total += abs(head["z"]) + abs(head["roll"]) + abs(head["pitch"]) + abs(head["yaw"])
        right, left = pose["antennas"]
        total += abs(right) + abs(left)
        total += abs(float(pose["body_yaw"]))
    return total


def test_focused_pose_excursion_strictly_lower() -> None:
    """Pure next_pose excursion under focused config is strictly lower."""
    base = AliveConfig()
    focused = base.focused()
    # Same seed/window → identical rng draws; only amplitudes differ.
    base_amp = _pose_excursion(base, seed=123, ticks=40, dt=2.5)
    focused_amp = _pose_excursion(focused, seed=123, ticks=40, dt=2.5)
    assert focused_amp < base_amp
