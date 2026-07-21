"""Deadline-based tick scheduling — cadence honours ``compose_hz`` (#97).

Measured on the deployed robot, the behavior engine achieved 23.16 Hz against
its 50 Hz target (mean tick 43.17 ms at 9% CPU) because ``_drive`` slept the
FULL period AFTER each tick's work — cadence = work + period (~20.7 ms work +
20 ms sleep = 43.2 ms; the arithmetic reproduces the measurement exactly).
These tests pin the fix: each tick sleeps only the time REMAINING to an
absolute deadline, so work is absorbed into the gap and cadence equals the
period. They also pin the two guard rails around it:

* a tick whose work exceeds the period sleeps zero — never a negative sleep;
* falling behind by more than one full period RESETS the deadline to "now"
  instead of running back-to-back catch-up ticks (a burst would violate the
  one-move-at-a-time motion discipline downstream);

and the per-tick timing seam: the loop's ``emit`` dict carries additive
``work_s`` / ``sleep_s`` keys, the numbers the on-box profile run reads to
apportion the measured ~20.7 ms of work.

All tests drive the REAL ``engine.run`` loop through the existing ``now`` /
``sleep`` injection seams with a manually advanced clock — the seam models the
tick's work by advancing the clock, the sleep function advances it by exactly
what was requested — so every schedule below is deterministic.
"""

from __future__ import annotations

import contextlib

import pytest

import reachy.behavior.engine as E
from reachy.behavior.engine import EngineConfig

PERIOD = 0.02  # compose_hz=50


class _FakeSink:
    def __init__(self) -> None:
        self.calls = 0

    def set_target(self, *, head, antennas, body_yaw):
        self.calls += 1
        return {"ok": True}


class _FakeTransport:
    name = "fake"

    def __init__(self) -> None:
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _ManualClock:
    """A monotonic clock that advances ONLY when told to (never per call).

    Reading it is free, so the schedule a test observes is exactly the work
    (seam-advanced) plus the sleeps (sleep-fn-advanced) — nothing else.
    """

    def __init__(self) -> None:
        self.t = 0.0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def _run_schedule(work_durations, *, base_layer=False):
    """Run ``engine.run`` for ``len(work_durations)`` ticks on a manual clock.

    Tick *n*'s work is modelled by the tick seam advancing the clock by
    ``work_durations[n]``. Returns (tick start times, per-tick emit events,
    individual sleep-chunk durations actually requested from ``sleep``).
    """
    clock = _ManualClock()
    starts: list[float] = []
    events: list[dict] = []
    slept: list[float] = []

    def seam(ctx) -> None:
        starts.append(ctx.now)
        clock.advance(work_durations[ctx.tick - 1])

    def sleep(seconds: float) -> None:
        slept.append(seconds)
        clock.advance(seconds)

    ticks = E.run(
        _FakeTransport(),
        EngineConfig(compose_hz=50, base_layer=base_layer, settle=False),
        sleep=sleep,
        now=clock,
        max_ticks=len(work_durations),
        emit=events.append,
        tick_seam=seam,
    )
    assert ticks == len(work_durations)
    return starts, events, slept


# --------------------------------------------------------------------------- #
# 1. Cadence equals the period: sleeps shrink by the elapsed work             #
# --------------------------------------------------------------------------- #


def test_cadence_equals_period_with_work_inside_the_budget() -> None:
    """~30% of the period spent working -> tick starts land exactly one period
    apart (the old full-period-after-work loop would space them 1.3 periods)."""
    work = 0.006  # 30% of the 20 ms period
    starts, events, slept = _run_schedule([work] * 5)
    assert starts == pytest.approx([0.0, 0.02, 0.04, 0.06, 0.08])
    # Each requested sleep is the REMAINDER of the period, not the full period.
    assert slept == pytest.approx([PERIOD - work] * 4)  # final tick breaks before sleeping
    assert [e["sleep_s"] for e in events][:-1] == pytest.approx([PERIOD - work] * 4)


def test_varying_work_still_holds_the_absolute_schedule() -> None:
    """Jittery work durations -> each sleep absorbs its own tick's work and the
    start times never drift off the absolute deadline grid."""
    works = [0.004, 0.012, 0.001, 0.009]
    starts, _events, slept = _run_schedule(works)
    assert starts == pytest.approx([0.0, 0.02, 0.04, 0.06])
    assert slept == pytest.approx([PERIOD - w for w in works[:-1]])


# --------------------------------------------------------------------------- #
# 2. Overrun: zero sleep + deadline reset, never a catch-up burst             #
# --------------------------------------------------------------------------- #


def test_overrun_tick_sleeps_zero_and_resets_the_deadline() -> None:
    """One 2.5x-period tick, then light ticks: the loop does NOT claw the lost
    time back — the very next sleep is again the full remainder of one period."""
    works = [0.05, 0.002, 0.002, 0.002]
    starts, events, slept = _run_schedule(works)
    # The overrun tick requests no sleep at all (and never a negative one).
    assert events[0]["sleep_s"] == 0.0
    assert all(s > 0.0 for s in slept)
    # Deadline was RESET to the overrun's end: the following ticks run on a
    # fresh one-period grid from there, with full-remainder sleeps (no burst).
    assert starts == pytest.approx([0.0, 0.05, 0.07, 0.09])
    assert slept == pytest.approx([PERIOD - 0.002] * 2)  # final tick breaks before sleeping


def test_sustained_overrun_free_runs_without_any_sleep() -> None:
    """Every tick over budget -> the loop free-runs at work rate: zero sleeps,
    no negative sleeps, and no back-to-back burst faster than the work itself."""
    works = [0.05] * 4
    starts, events, slept = _run_schedule(works)
    assert slept == []  # never called: nothing to sleep, nothing negative
    assert [e["sleep_s"] for e in events] == [0.0] * 4
    assert starts == pytest.approx([0.0, 0.05, 0.10, 0.15])


# --------------------------------------------------------------------------- #
# 3. The per-tick timing seam: emit carries work_s / sleep_s                  #
# --------------------------------------------------------------------------- #


def test_emit_carries_work_and_sleep_durations_per_tick() -> None:
    """The emit dict grows additive work_s/sleep_s keys next to tick/ownership —
    the seam the on-box profile run reads to apportion the ~20.7 ms of work."""
    work = 0.006
    _starts, events, _slept = _run_schedule([work] * 3, base_layer=True)
    assert len(events) == 3
    for e in events:
        assert {"tick", "ownership", "work_s", "sleep_s"} <= set(e)
        assert e["work_s"] == pytest.approx(work)
        assert e["sleep_s"] >= 0.0
    assert [e["sleep_s"] for e in events][:-1] == pytest.approx([PERIOD - work] * 2)
