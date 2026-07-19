"""Stateful feel-alive motion with regular, sense-safe pettable windows.

The behavior engine streams immediate targets, so proprioceptive pat sensing can
only distinguish contact while the *complete* command vector is still.  This
generator therefore alternates bounded organic motion with an exact four-second
hold.  Its final second of motion eases into the hold; the following motion
eases out from the same vector, keeping every phase boundary continuous.

The engine supplies behavior-local monotonic time to the returned contribution
function.  Cadence jitter is per-instance state.  Tests can inject a seeded
``Random.uniform``-shaped callable; production entropy is deliberately hidden
behind :func:`make_feel_alive`, so this module promises deterministic behavior
for an injected source but no repeatable sequence across process restarts.

This is a leaf generator only.  Registration under the existing ``feel-alive``
library name is composition work and intentionally does not happen here.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import Callable, cast

from reachy.behavior.model import Contribution, neutral_head
from reachy.behavior.sense import Sense

Jitter = Callable[[float, float], float]

# One cadence is MOVE (including its one-second ease into stillness), then HOLD.
# The 0.5-second commanded-still gate leaves 3.5 seconds of the exact hold open
# for contact sensing even at the shortest/longest jittered cadence.
MOVE_MIN_S = 8.0
MOVE_MAX_S = 12.0
SETTLE_S = 1.0
HOLD_S = 4.0
SENSE_GATE_S = 0.5
MAX_TIME_TO_WINDOW_S = MOVE_MAX_S

_INTRO_S = 1.0
_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")


@dataclass(frozen=True)
class _Cycle:
    start: float
    move_s: float

    @property
    def hold_start(self) -> float:
        return self.start + self.move_s

    @property
    def end(self) -> float:
        return self.hold_start + HOLD_S


def _production_jitter() -> Jitter:
    """Return one process-local entropy source without exposing seed semantics."""
    # SystemRandom keeps the production choice opaque.  This entropy only shapes
    # expressive idle motion; it is not used for a security decision.
    return random.SystemRandom().uniform


def _sample_move_s(jitter: Jitter) -> float:
    """Sample a finite duration and contain an out-of-contract injector."""
    value = float(jitter(MOVE_MIN_S, MOVE_MAX_S))
    if not math.isfinite(value):
        value = MOVE_MAX_S
    return max(MOVE_MIN_S, min(MOVE_MAX_S, value))


def _minjerk(x: float) -> float:
    """A clamped C2 easing curve with zero velocity at both endpoints."""
    if x <= 0.0:
        return 0.0
    if x >= 1.0:
        return 1.0
    return x * x * x * (10.0 + x * (-15.0 + 6.0 * x))


def _raw_motion(t: float, p: dict) -> Contribution:
    """The existing feel-alive amplitudes and energy meaning, as one full pose."""
    energy = p["energy"]
    breathe_period = p["breathe_period"]
    phase = 2.0 * math.pi * t / breathe_period if breathe_period else 0.0
    z = p["breathe_z"] * energy * math.sin(phase)
    breathe_pitch = p["breathe_pitch"] * energy * math.sin(phase)
    yaw = energy * p["gaze_yaw"] * (0.6 * math.sin(0.13 * t) + 0.4 * math.sin(0.37 * t + 1.3))
    gaze_pitch = (
        energy * p["gaze_pitch"] * (0.6 * math.sin(0.11 * t + 0.7) + 0.4 * math.sin(0.29 * t))
    )
    antenna_period = p["antenna_period"]
    sway = (
        p["antenna"] * energy * math.sin(2.0 * math.pi * t / antenna_period)
        if antenna_period
        else 0.0
    )
    body_yaw = energy * p["body_yaw"] * math.sin(0.07 * t + 0.5)
    head = neutral_head()
    head.update(z=z, pitch=breathe_pitch + gaze_pitch, yaw=yaw)
    return Contribution(head=head, antennas=(sway, -sway), body_yaw=body_yaw)


def _zero() -> Contribution:
    return Contribution(head=neutral_head(), antennas=(0.0, 0.0), body_yaw=0.0)


def _lerp(start: Contribution, target: Contribution, progress: float) -> Contribution:
    """Interpolate two complete contributions without dropping a channel."""
    # All callers construct complete private poses.  Cast that internal invariant
    # for the type checker without relying on runtime ``assert`` safety behavior.
    start_head = cast(dict[str, float], start.head)
    target_head = cast(dict[str, float], target.head)
    start_antennas = cast(tuple[float, float], start.antennas)
    target_antennas = cast(tuple[float, float], target.antennas)
    start_body_yaw = cast(float, start.body_yaw)
    target_body_yaw = cast(float, target.body_yaw)

    def between(a: float, b: float) -> float:
        return a + (b - a) * progress

    return Contribution(
        head={axis: between(start_head[axis], target_head[axis]) for axis in _HEAD_AXES},
        antennas=(
            between(start_antennas[0], target_antennas[0]),
            between(start_antennas[1], target_antennas[1]),
        ),
        body_yaw=between(start_body_yaw, target_body_yaw),
    )


class _FeelAlive:
    """One private cadence schedule; the callable itself is the public seam."""

    def __init__(self, jitter: Jitter):
        self._jitter = jitter
        self._cycles = [_Cycle(start=0.0, move_s=_sample_move_s(jitter))]

    def _cycle_at(self, t: float) -> tuple[int, _Cycle]:
        # Retain prior cycles so an injected/replayed clock can seek backwards
        # without resampling or corrupting future cadence.
        while t >= self._cycles[-1].end:
            previous = self._cycles[-1]
            self._cycles.append(_Cycle(start=previous.end, move_s=_sample_move_s(self._jitter)))
        for index, cycle in enumerate(self._cycles):
            if t < cycle.end:
                return index, cycle
        raise AssertionError("cycle schedule did not cover local time")

    @staticmethod
    def _hold_pose(cycle: _Cycle, params: dict) -> Contribution:
        return _raw_motion(cycle.hold_start, params)

    def __call__(self, t_local: float, params: dict, _sense: Sense) -> Contribution:
        t = max(0.0, float(t_local))
        index, cycle = self._cycle_at(t)
        elapsed = t - cycle.start
        hold = self._hold_pose(cycle, params)

        if elapsed >= cycle.move_s:
            # Recomputing this pure endpoint yields bit-for-bit equal scalar
            # values for every tick of the entire four-second hold.
            return hold

        settle_start = cycle.move_s - SETTLE_S
        if elapsed >= settle_start:
            moving_edge = _raw_motion(cycle.start + settle_start, params)
            progress = _minjerk((elapsed - settle_start) / SETTLE_S)
            return _lerp(moving_edge, hold, progress)

        if elapsed < _INTRO_S:
            previous_hold = (
                _zero() if index == 0 else self._hold_pose(self._cycles[index - 1], params)
            )
            moving_edge = _raw_motion(cycle.start + _INTRO_S, params)
            return _lerp(previous_hold, moving_edge, _minjerk(elapsed / _INTRO_S))

        return _raw_motion(t, params)


def make_feel_alive(
    *, jitter: Jitter | None = None
) -> Callable[[float, dict, Sense], Contribution]:
    """Build one fresh stateful feel-alive contribution function.

    ``jitter`` has the shape of ``random.Random.uniform(low, high)``.  Supplying
    one makes cadence reproducible for tests/replays.  Omitting it selects fresh
    production entropy; there is intentionally no public seed or cross-process
    sequence contract.
    """
    return _FeelAlive(jitter if jitter is not None else _production_jitter())


__all__ = [
    "HOLD_S",
    "MAX_TIME_TO_WINDOW_S",
    "MOVE_MAX_S",
    "MOVE_MIN_S",
    "SENSE_GATE_S",
    "SETTLE_S",
    "make_feel_alive",
]
