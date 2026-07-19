"""Focused contract tests for the stateful, pettable feel-alive generator."""

from __future__ import annotations

import inspect
import random

import pytest

from reachy.behavior.arbitration import arbitrate
from reachy.behavior.feel_alive import (
    HOLD_S,
    MAX_TIME_TO_WINDOW_S,
    MOVE_MAX_S,
    MOVE_MIN_S,
    SENSE_GATE_S,
    make_feel_alive,
)
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


def test_injected_cadence_moves_then_holds_complete_vector_exactly() -> None:
    jitter = _Jitter(8.0, 12.0)
    fn = make_feel_alive(jitter=jitter)
    clock = _Clock()

    moving = [_vector(_contribution(fn, clock.at(t))) for t in (1.0, 3.0, 5.0, 7.0)]
    assert len(set(moving)) == len(moving), "the 8-second move interval became still early"

    held = [_vector(_contribution(fn, clock.at(t))) for t in (8.0, 8.5, 11.5, 11.999999)]
    assert held == [held[0]] * len(held)
    assert len(held[0]) == 9  # six head axes, two antennas, and body yaw

    # The next cadence starts continuously from the held vector, then moves.
    assert _vector(_contribution(fn, clock.at(12.0))) == held[0]
    assert _vector(_contribution(fn, clock.at(12.5))) != held[0]
    assert jitter.calls == [(MOVE_MIN_S, MOVE_MAX_S), (MOVE_MIN_S, MOVE_MAX_S)]


def test_hold_leaves_full_sense_window_and_time_to_window_is_bounded() -> None:
    assert HOLD_S == 4.0
    assert HOLD_S - SENSE_GATE_S >= 3.5
    assert MAX_TIME_TO_WINDOW_S == MOVE_MAX_S == 12.0

    # Even a broken injector cannot silently stretch the time-to-window promise.
    fn = make_feel_alive(jitter=lambda _low, _high: 99.0)
    held = [_vector(_contribution(fn, t)) for t in (12.0, 12.5, 15.999999)]
    assert held == [held[0]] * len(held)


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
