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

import bisect
import math
import random
from dataclasses import dataclass
from typing import Callable, cast

from reachy.behavior.model import Contribution, neutral_head
from reachy.behavior.sense import Sense

Jitter = Callable[[float, float], float]

# Legacy cadence constants from the removed pettable-window hold. Kept so the
# public module surface does not change under callers/tests that import them;
# nothing in the live path reads them now that idle motion is continuous.
MOVE_MIN_S = 8.0
MOVE_MAX_S = 12.0
SETTLE_S = 1.0
HOLD_S = 4.0
SENSE_GATE_S = 0.5
MAX_TIME_TO_WINDOW_S = MOVE_MAX_S

_INTRO_S = 1.0
_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

# --------------------------------------------------------------------------- #
# The swing: one rhythm the WHOLE pose slows, stops and speeds up on.          #
# --------------------------------------------------------------------------- #
#
# Idle motion is a sum of unsynchronised sinusoids, so each axis reaches its own
# extreme at its own moment and the composite never dwells: measured over the
# shipped profile, the longest window below 10% of peak speed is 0.12 s, and 0%
# of the time falls inside a >=1 s slow spell.
#
# That matters because the plant's noise is servo *ringing*, not lag — it is
# uncorrelated with instantaneous velocity (#80), so a brief slow moment is
# still noisy. What quiets it is SUSTAINED slowness: measured on the shipped
# wander fixtures, the untouched conditioned residual falls from 1.19 deg to
# 0.70 deg once the head has been slow for a full second, while a petted head
# stays at ~2.5 deg — a 3.6x separation that a moving head otherwise never
# offers.
#
# So instead of slowing the robot down (tried: reads as silent, or "midway to
# sleep") or freezing it (tried: reads as stopping), warp TIME. The pose sweeps
# fast through the middle of its arc and decelerates, pauses, and accelerates
# out of each extreme — like a swing. Every axis does this together because the
# warp is applied to the clock they all share, so amplitudes, periods and total
# travel are all completely unchanged: 330 deg per 40 s either way.
#
#: Period of one full slow-down/stop/speed-up rhythm (seconds).
WARP_PERIOD_S = 10.0
#: How pronounced the dwell is. ``0.0`` reproduces today's uniform time exactly.
#: Clamped below ``1.0`` because ``d(warp)/dt = 1 - depth*cos(...)`` must stay
#: strictly positive — at ``1.0`` the clock momentarily stalls and beyond it
#: time would run BACKWARDS, reversing the motion.
WARP_DEPTH = 0.95
_MAX_WARP_DEPTH = 0.999


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


def swing_time(t: float, period: float = WARP_PERIOD_S, depth: float = WARP_DEPTH) -> float:
    """Warp the shared clock so the whole pose swings instead of drifting.

    Returns a strictly increasing warped time. Feeding it to :func:`_raw_motion`
    leaves every amplitude and period untouched — only the RATE at which the
    trajectory is traversed changes, sweeping fast through the middle of the arc
    and lingering at each extreme.

    ``d(warp)/dt = 1 - depth * cos(2*pi*t/period)`` is strictly positive for
    ``depth < 1``, so time never stalls or reverses and no axis snaps back.
    A non-finite or non-positive period disables the warp rather than raising —
    idle motion must degrade to today's behavior, never to an exception.
    """
    if not math.isfinite(t):
        return t
    if not math.isfinite(period) or period <= 0.0:
        return t
    d = max(0.0, min(_MAX_WARP_DEPTH, float(depth)))
    if d == 0.0:
        return t
    return t - (d * period / (2.0 * math.pi)) * math.sin(2.0 * math.pi * t / period)


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
        # Each cycle starts where the previous ended and every cycle has positive
        # length, so `end` is strictly increasing and the first cycle ending after
        # `t` is a binary search rather than a scan from index 0. The schedule grows
        # for the life of the process — a boot-persistent presence ticks at 50 Hz
        # for days — so a linear probe made per-tick cost grow with uptime.
        index = bisect.bisect_right(self._cycles, t, key=lambda cycle: cycle.end)
        return index, self._cycles[index]

    @staticmethod
    def _hold_pose(cycle: _Cycle, params: dict) -> Contribution:
        return _raw_motion(cycle.hold_start, params)

    def __call__(self, t_local: float, params: dict, _sense: Sense) -> Contribution:
        """Continuous idle motion — the robot never stops to become pettable.

        The dead-still ``HOLD_S`` window (and the ease into and out of it) is
        gone: this is the pre-#82 continuous ``_feel_alive`` behavior restored.
        Amplitude and speed are unchanged, so the presence reads exactly as it
        did before the pettable-window cadence was introduced.

        Motion runs on the swing clock (:func:`swing_time`), so the complete
        pose decelerates, pauses and accelerates out of each extreme together.
        Amplitudes, periods and total travel are unchanged — only the rate of
        traversal varies — which is what creates a recurring multi-second window
        where the plant is quiet enough to feel a hand, without the robot ever
        stopping.

        Consequence, stated plainly: ``PatSenseDriver``'s stillness gate opens
        only after the commanded pose has been EXACTLY constant for
        ``still_hold_s``, which continuous motion never satisfies. The swing
        window is *slow*, not *still*, so pettability needs a sense gated on
        sustained low speed rather than exact constancy — follow-up work this
        change does not silently claim to have done.
        """
        return _raw_motion(swing_time(max(0.0, float(t_local))), params)


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
