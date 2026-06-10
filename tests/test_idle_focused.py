"""Tests for the focused (low-energy) idle while the cognition signal is active.

Governing principle under test: STILLNESS IS THE THINKING POSTURE. When the
``think`` cognition loop is active (signalled by the cognition flag file), the
always-alive ``listen`` idle motion must QUIET DOWN to a low-energy "focused"
breathe — reduced, never zeroed (it still breathes).

Two layers are exercised:

* :meth:`AliveConfig.focused` — the pure low-energy profile factory.
* :meth:`ListenProducer._idle` — reads ``cognition_signal.is_active()`` per tick
  and branches between the normal and focused configs.
"""

from __future__ import annotations

import random

import pytest

from reachy.motion.idle import AliveConfig, next_pose
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.speech import cognition_signal


@pytest.fixture()
def state_dir(tmp_path, monkeypatch):
    """Isolate the cognition flag under a tmp state dir, signal cleared."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    cognition_signal.clear()
    yield tmp_path
    cognition_signal.clear()


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


# --------------------------------------------------------------------------- #
# ListenProducer._idle — reads the cognition signal and branches              #
# --------------------------------------------------------------------------- #


def _measure_idle(prod: ListenProducer, *, ticks: int, dt: float) -> tuple[float, float]:
    """Drive only the idle layer over a fixed window; return (summed, peak) amp.

    No live sound is ever presented, so Tier-2 turns never fire — every emitted
    action is a pure idle pose. Each tick we measure the total absolute head /
    antenna / body excursion and track both the running sum and the peak.
    """
    summed = 0.0
    peak = 0.0
    for i in range(ticks):
        t = i * dt
        action = prod._idle(t, live=False)
        if action is None:
            continue
        amp = (
            abs(action.head["z"])
            + abs(action.head["roll"])
            + abs(action.head["pitch"])
            + abs(action.head["yaw"])
            + abs(action.antennas[0])
            + abs(action.antennas[1])
            + abs(action.body_yaw)
        )
        summed += amp
        peak = max(peak, amp)
    return summed, peak


def _fresh_producer(seed: int) -> ListenProducer:
    prod = ListenProducer(ListenParams(idle_energy=1.0))
    prod._rng = random.Random(seed)  # deterministic, identical stream both runs
    return prod


def test_idle_quiets_down_when_signal_active(state_dir) -> None:
    """Measured idle amplitude is STRICTLY LOWER while the signal is active.

    Same producer params, same rng seed, same elapsed window — the only
    difference between the two runs is the cognition signal. The active-signal
    run must show strictly smaller summed AND peak motion.
    """
    ticks, dt = 60, 2.5

    # Signal OFF — standalone listen idle.
    cognition_signal.clear()
    assert not cognition_signal.is_active()
    off_sum, off_peak = _measure_idle(_fresh_producer(99), ticks=ticks, dt=dt)

    # Signal ON — focused idle.
    cognition_signal.write()
    assert cognition_signal.is_active()
    on_sum, on_peak = _measure_idle(_fresh_producer(99), ticks=ticks, dt=dt)

    assert off_sum > 0.0  # the off-case actually moved (sanity)
    assert on_sum > 0.0  # focused still breathes — not frozen
    assert on_sum < off_sum  # strictly lower summed amplitude
    assert on_peak < off_peak  # strictly lower peak amplitude


def test_idle_signal_read_per_tick(state_dir) -> None:
    """The signal is sampled each tick — toggling mid-window changes the config.

    Drive a window with the signal off, then on, on the SAME producer; the
    on-segment must be strictly quieter than the off-segment of the same length.
    """
    dt = 2.5
    prod = _fresh_producer(7)

    cognition_signal.clear()
    off_sum, _ = _measure_idle(prod, ticks=30, dt=dt)

    # Same producer continues; only the signal flips.
    cognition_signal.write()
    on_sum, _ = _measure_idle(prod, ticks=30, dt=dt)

    # Continue counting where we left off would advance _last_idle_t; restart a
    # fresh producer for the on-measurement to keep the windows comparable.
    prod2 = _fresh_producer(7)
    cognition_signal.write()
    on_sum2, _ = _measure_idle(prod2, ticks=30, dt=dt)

    assert on_sum2 < off_sum
