"""Focused contract tests for the stateful, pettable feel-alive generator."""

from __future__ import annotations

import inspect
import random

import pytest

from reachy.behavior.arbitration import arbitrate
from reachy.behavior.feel_alive import _raw_motion, make_feel_alive, swing_time
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass
from reachy.behavior.sense import EMPTY_SENSE

pytestmark = pytest.mark.offline


PARAMS = {
    "energy": 1.0,
    "breathe_period": 5.0,
    "breathe_z": 3.0,
    "breathe_pitch": 2.0,
    "gaze_yaw": 12.0,
    "gaze_pitch": 7.0,
    "antenna": 12.0,
    "antenna_period": 6.0,
    "body_yaw": 6.0,
}


class _Jitter:
    """A labelled deterministic replacement for ``Random.uniform``."""

    def __init__(self, *values: float):
        self.values = iter(values)
        self.calls: list[tuple[float, float]] = []

    def __call__(self, low: float, high: float) -> float:
        self.calls.append((low, high))
        return next(self.values)


class _Clock:
    def __init__(self, start: float = 0.0):
        self.t = start

    def at(self, t: float) -> float:
        self.t = t
        return self.t


def _contribution(fn, t: float, *, energy: float = 1.0) -> Contribution:
    params = {**PARAMS, "energy": energy}
    return fn(t, params, EMPTY_SENSE)


def _vector(value: Contribution) -> tuple[float, ...]:
    assert value.head is not None
    assert value.antennas is not None
    assert value.body_yaw is not None
    return (
        *(value.head[axis] for axis in ("x", "y", "z", "roll", "pitch", "yaw")),
        *value.antennas,
        value.body_yaw,
    )


def test_idle_motion_is_continuous_and_never_holds_the_vector_still() -> None:
    """INTENT INVERTED — the freeze is gone.

    This test previously asserted the opposite: that the complete command
    vector held EXACTLY constant for a four-second ``HOLD_S`` window after each
    move interval (``test_injected_cadence_moves_then_holds_complete_vector_
    exactly``). That dead-still hold existed so the pat sense's stillness gate
    could open inside it. The operator rejected the freeze on hardware — it
    reads as the robot stopping, or "midway to sleep" — so the hold was removed
    and the pre-#82 continuous motion restored.

    What this now pins is the new promise: the vector is ALWAYS moving. If a
    hold is ever reintroduced, this test fails and names the reason.
    """
    fn = make_feel_alive(jitter=_Jitter(8.0, 12.0))
    clock = _Clock()

    # Sample straight through the window the old cadence used to freeze in.
    samples = [
        _vector(_contribution(fn, clock.at(t)))
        for t in (1.0, 3.0, 5.0, 7.0, 8.0, 8.5, 11.5, 12.0, 12.5)
    ]
    assert len(set(samples)) == len(samples), "idle motion held still — the freeze is back"
    assert len(samples[0]) == 9  # six head axes, two antennas, and body yaw

    # Densely inside the old hold window (8.0-12.0 s): still no repeat.
    dense = [_vector(_contribution(fn, 8.0 + i * 0.1)) for i in range(41)]
    assert len(set(dense)) == len(dense), "the old HOLD_S window is still dead-still"


def test_no_sense_window_is_promised_now_that_motion_is_continuous() -> None:
    """INTENT INVERTED — there is no pettable window any more.

    This test previously asserted the sense-window arithmetic: ``HOLD_S == 4.0``
    and ``HOLD_S - SENSE_GATE_S >= 3.5``, i.e. every cadence guaranteed at least
    3.5 seconds in which the pat sense's 0.5 s stillness gate could open, with
    ``MAX_TIME_TO_WINDOW_S`` bounding the wait for one.

    With the freeze removed there is no window and no such promise. Stated
    plainly so it is not mistaken for an oversight: the behavior-engine pat
    sense is INERT under continuous motion unless its gate is explicitly
    disabled (``REACHY_PAT_STILL_HOLD_S=0``). Restoring pettability on top of
    continuous motion is follow-up work; this test exists so nobody re-derives
    a pettability guarantee that the motion no longer provides.
    """
    fn = make_feel_alive(jitter=lambda _low, _high: 99.0)

    # Even with an absurd injected cadence, nothing ever settles into a hold.
    samples = [_vector(_contribution(fn, t)) for t in (12.0, 12.5, 15.999999, 40.0, 90.0)]
    assert len(set(samples)) == len(samples), "a pettable hold window reappeared"


def test_seeded_instances_reproduce_cadence_without_sharing_state() -> None:
    one = make_feel_alive(jitter=random.Random(73).uniform)
    two = make_feel_alive(jitter=random.Random(73).uniform)
    times = [i * 0.125 for i in range(241)]

    # Interleave calls: equal output then proves each instance owns its schedule.
    for t in times:
        assert _vector(_contribution(one, t)) == _vector(_contribution(two, t))


def test_production_factory_exposes_no_seed_repeatability_contract() -> None:
    signature = inspect.signature(make_feel_alive)
    assert "seed" not in signature.parameters
    assert tuple(signature.parameters) == ("jitter",)


def test_energy_keeps_existing_zero_and_linear_amplitude_meaning() -> None:
    full = make_feel_alive(jitter=_Jitter(9.0))
    half = make_feel_alive(jitter=_Jitter(9.0))
    zero = make_feel_alive(jitter=_Jitter(9.0))

    for t in (0.0, 1.0, 4.5, 8.5, 9.0, 11.0):
        full_v = _vector(_contribution(full, t, energy=1.0))
        half_v = _vector(_contribution(half, t, energy=0.5))
        zero_v = _vector(_contribution(zero, t, energy=0.0))
        assert half_v == pytest.approx(tuple(0.5 * value for value in full_v))
        assert zero_v == (0.0,) * 9


def test_boundaries_are_continuous_with_bounded_default_slew() -> None:
    fn = make_feel_alive(jitter=_Jitter(8.0, 12.0))
    dt = 0.005
    samples = [_vector(_contribution(fn, i * dt)) for i in range(int(13.0 / dt) + 1)]

    # Friendly-unit ceilings are deliberately loose but finite: the defaults may
    # breathe and sway, never snap at move/settle/hold boundaries.
    max_head_translation_rate = 0.0
    max_rotation_rate = 0.0
    for previous, current in zip(samples, samples[1:]):
        rates = [abs(b - a) / dt for a, b in zip(previous, current)]
        max_head_translation_rate = max(max_head_translation_rate, *rates[:3])
        max_rotation_rate = max(max_rotation_rate, *rates[3:])

    assert max_head_translation_rate <= 10.0  # mm/s
    assert max_rotation_rate <= 45.0  # deg/s across head, antennas, and body

    # C0 continuity is also pinned immediately around every phase edge.
    for boundary in (0.0, 7.0, 8.0, 12.0):
        before = _vector(_contribution(fn, max(0.0, boundary - dt)))
        after = _vector(_contribution(fn, boundary + dt))
        assert max(abs(b - a) for a, b in zip(before, after)) < 0.5


def test_complete_generator_remains_a_passive_base_when_wrapped() -> None:
    fn = make_feel_alive(jitter=_Jitter(8.0))
    base = Behavior(
        id="feel-alive-1",
        name="feel-alive",
        channels=frozenset({"head", "antennas", "body_yaw"}),
        stop_class=StopClass.PASSIVE,
        lifetime=Lifetime(looping=True, duration=None),
        params=PARAMS,
        fn=fn,
    )
    foreground = Behavior(
        id="gaze-2",
        name="gaze-hold",
        channels=frozenset({"head"}),
        stop_class=StopClass.STOPPABLE,
        lifetime=Lifetime(looping=False, duration=1.0),
        params={},
        fn=lambda _t, _p, _s: Contribution(head={}),
    )

    contribution = base.contribution(2.0)
    assert contribution.head is not None
    assert contribution.antennas is not None
    assert contribution.body_yaw is not None
    owners = arbitrate(
        [base, foreground], {base.id: contribution, foreground.id: foreground.contribution(0)}
    )
    assert owners["head"] is foreground
    assert owners["antennas"] is base
    assert owners["body_yaw"] is base


def test_swing_clock_is_strictly_increasing_and_degrades_safely() -> None:
    """Time may slow to a crawl but must never stall or run backwards.

    ``d(swing_time)/dt = 1 - depth*cos(...)``, so a depth at or above 1.0 would
    stall the clock and beyond it would reverse the motion — every axis would
    snap back. The depth is clamped for exactly that reason.
    """
    ts = [i * 0.005 for i in range(4000)]
    warped = [swing_time(t) for t in ts]
    assert all(b > a for a, b in zip(warped, warped[1:])), "the swing clock stalled or reversed"

    # An over-large depth must clamp, not reverse.
    hard = [swing_time(t, depth=5.0) for t in ts]
    assert all(b > a for a, b in zip(hard, hard[1:])), "an out-of-range depth reversed time"

    # depth=0 is exactly today's uniform clock, and bad inputs degrade to it.
    assert all(swing_time(t, depth=0.0) == t for t in ts[:200])
    assert swing_time(3.0, period=0.0) == 3.0
    assert swing_time(3.0, period=float("nan")) == 3.0


def test_swing_creates_a_sustained_slow_window_without_losing_travel() -> None:
    """The point of the swing: a pettable window at zero cost to liveliness.

    The plant's noise is servo ringing, so it needs SUSTAINED slowness to quiet
    down (measured: untouched residual 1.19 -> 0.70 deg after a full second
    slow, while petted stays ~2.5). Uniform-time idle motion never provides one
    — its longest sub-10%-speed window is 0.12 s. The swing must provide one
    while moving exactly as much overall.
    """
    fn = make_feel_alive(jitter=_Jitter(9.0))
    dt = 0.02
    poses = [_vector(_contribution(fn, i * dt)) for i in range(int(120 / dt))]
    speeds = [max(abs(a - b) for a, b in zip(u, v)) for u, v in zip(poses, poses[1:])]

    slow = 0.10 * max(speeds)
    longest = run = 0
    for s in speeds:
        run = run + 1 if s <= slow else 0
        longest = max(longest, run)
    assert longest * dt >= 1.0, f"no sustained slow window: longest was {longest * dt:.2f}s"

    # Liveliness is NOT the price: total travel matches the unwarped trajectory.
    #
    # The baseline MUST come from `_raw_motion` directly. Feeding
    # `swing_time(t, depth=0.0)` into the callable does not disable anything —
    # `__call__` applies its own warp to whatever time it is handed — so that
    # would compare the warped trajectory against itself and pass vacuously.
    unwarped = [
        _vector(_raw_motion(i * dt, {**PARAMS, "energy": 1.0})) for i in range(int(40 / dt))
    ]
    unwarped_travel = sum(
        max(abs(a - b) for a, b in zip(u, v)) for u, v in zip(unwarped, unwarped[1:])
    )
    warped_travel = sum(speeds[: int(40 / dt) - 1])
    assert warped_travel == pytest.approx(unwarped_travel, rel=0.05)
