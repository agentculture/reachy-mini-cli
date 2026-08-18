"""Task t1 (plan pettable-wireless-168) — the deg/s stillness gate is at least
as tight as the retired per-tick gate at a clean 50 Hz cadence (issue #168).

The stillness gate used to judge "the commanded pose is still" with a
PER-TICK tolerance (``DEFAULT_STILL_EPS``, retired). That makes the gate's
open-fraction a function of tick cadence: on the Reachy Wireless the runtime
ticks at ~6.8 Hz instead of the 50 Hz design point, so per-tick deltas run
~7x design and the old 0.035 deg/tick gate never opened at all. The fix
dt-normalizes the tolerance to degrees per SECOND (``eps_deg_s``,
:data:`~reachy.behavior.pat_sense.DEFAULT_STILL_EPS_DEG_S`).

This module pins that the replacement does not silently loosen the gate at
the cadence the retired one was tuned for. It replays one synthetic clean
50 Hz trajectory (the shipped ``feel-alive`` swing, deterministic jitter)
through BOTH the retired per-tick predicate — reimplemented verbatim here as
a frozen comparison baseline, since the source is gone — and the ACTUAL
shipped ``PatSenseDriver._commanded_still``, then asserts the deg/s gate's
blocked-tick set is a SUPERSET of the per-tick gate's. At a clean 50 Hz
cadence, 1.25 deg/s == 0.025 deg/tick, strictly tighter than the retired
0.035 deg/tick, so this is expected to hold and pins that the wander ghost
class's exposure cannot grow under the new gate.
"""

from __future__ import annotations

import pytest

from reachy.behavior.feel_alive import make_feel_alive
from reachy.behavior.pat_sense import (
    DEFAULT_STILL_EPS_DEG_S,
    DEFAULT_STILL_HOLD_S,
    PatSenseDriver,
)

pytestmark = pytest.mark.offline

_DT = 0.02  # the clean 50 Hz design cadence the retired gate was tuned at
_RETIRED_STILL_EPS = 0.035  # the retired per-tick tolerance (issue #168 removed it)
_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

_PARAMS = {
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


class _MidpointJitter:
    """A fixed, deterministic move-duration draw so the trajectory is reproducible."""

    def __call__(self, low: float, high: float) -> float:
        return (low + high) / 2.0


def _synthetic_trajectory(n_ticks: int, *, dt: float = _DT) -> list[tuple[float, ...]]:
    """``n_ticks`` of the shipped feel-alive contribution as 9-axis pose tuples.

    Exercises the full still/moving cycle a real presence produces: continuous
    idle motion warped by the swing clock (``feel_alive.swing_time``),
    including the slow decelerate-pause-accelerate windows the stillness gate
    is built to open inside — the same trajectory shape issue #82/#168 reason
    about.
    """
    fn = make_feel_alive(jitter=_MidpointJitter())
    poses = []
    for i in range(n_ticks):
        t = i * dt
        contribution = fn(t, _PARAMS, None)  # feel-alive never reads `_sense`
        head = contribution.head
        assert head is not None
        assert contribution.antennas is not None
        assert contribution.body_yaw is not None
        poses.append(
            tuple(head[axis] for axis in _HEAD_AXES)
            + (contribution.body_yaw, contribution.antennas[0], contribution.antennas[1])
        )
    return poses


def _retired_per_tick_blocked_ticks(
    poses: list[tuple[float, ...]], *, still_eps: float, still_hold_s: float, dt: float
) -> set[int]:
    """Reimplementation of the RETIRED per-tick ``_commanded_still`` predicate.

    Mirrors the deleted logic verbatim (issue #168 removed it from
    ``reachy/behavior/pat_sense.py``): a per-axis change beyond ``still_eps``
    restamps the motion clock; the gate opens once ``still_hold_s`` has
    elapsed with no such change. Kept here ONLY as a frozen comparison
    baseline — never call this from production code.
    """
    blocked: set[int] = set()
    last_cmd: tuple[float, ...] | None = None
    last_motion_t: float | None = None
    now = 0.0
    for i, commanded in enumerate(poses):
        prev = last_cmd
        last_cmd = commanded
        if prev is None or max(abs(c - p) for c, p in zip(commanded, prev)) > still_eps:
            last_motion_t = now
            blocked.add(i)
        elif last_motion_t is None:
            last_motion_t = now
            blocked.add(i)
        elif (now - last_motion_t) < still_hold_s:
            blocked.add(i)
        now += dt
    return blocked


def _shipped_deg_per_second_blocked_ticks(
    poses: list[tuple[float, ...]], *, eps_deg_s: float, still_hold_s: float, dt: float
) -> set[int]:
    """Drive the ACTUAL shipped ``PatSenseDriver._commanded_still`` directly."""
    driver = PatSenseDriver(
        reader=lambda: None,
        eps_deg_s=eps_deg_s,
        still_hold_s=still_hold_s,
        warmup_s=0.0,
    )
    blocked: set[int] = set()
    now = 0.0
    for i, commanded in enumerate(poses):
        if not driver._commanded_still(commanded, now):
            blocked.add(i)
        now += dt
    return blocked


def test_deg_per_second_gate_blocks_a_superset_of_the_retired_per_tick_gate_at_50hz() -> None:
    """At a clean 50 Hz cadence, the shipped deg/s gate is at least as tight.

    1.25 deg/s == 0.025 deg/tick at this dt, strictly tighter than the retired
    0.035 deg/tick — so this pins that the ghost-class exposure cannot grow
    under the dt-normalized gate at the cadence the retired one shipped at.
    """
    poses = _synthetic_trajectory(n_ticks=6000)  # 120 s at 50 Hz -- several swing periods

    retired_blocked = _retired_per_tick_blocked_ticks(
        poses, still_eps=_RETIRED_STILL_EPS, still_hold_s=DEFAULT_STILL_HOLD_S, dt=_DT
    )
    shipped_blocked = _shipped_deg_per_second_blocked_ticks(
        poses, eps_deg_s=DEFAULT_STILL_EPS_DEG_S, still_hold_s=DEFAULT_STILL_HOLD_S, dt=_DT
    )

    assert retired_blocked, "fixture sanity: the retired gate must block SOME ticks"
    assert len(retired_blocked) < len(poses), "fixture sanity: the retired gate must OPEN sometimes"
    assert shipped_blocked >= retired_blocked, (
        "the dt-normalized deg/s gate must block at least every tick the retired "
        "per-tick gate blocked at a clean 50 Hz cadence (issue #168)"
    )


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
