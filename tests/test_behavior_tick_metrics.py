"""Tick-budget observability — overrun counter + ``[SENSE]`` line (task t14).

Assumption c22 says the engine's tick budget (``1 / compose_hz``) holds on real
hardware but is unmeasured. :class:`~reachy.behavior.tick_metrics.TickMetrics`
wraps the engine's ONE per-tick seam and makes an overrun observable: a real
wall-clock duration probe (injectable, NOT ``ctx.now``) compared against a
budget, logged via the same ``[SENSE ...]`` convention
:mod:`reachy.behavior.rule_engine` uses.

These tests pin the acceptance criterion exactly:

* a normal bounded run (fast fake ``duration_clock``) emits ZERO overrun lines
  and leaves ``.overruns == 0``;
* a synthetic slow tick (``duration_clock`` jumps past budget) emits EXACTLY
  one ``[SENSE stage=rule source=tick event=overrun]`` line per slow tick, with
  measured + budget milliseconds in the detail, and increments ``.overruns``;
* an integration run: a real :class:`~reachy.behavior.rule_engine.TickBus` with
  a genuinely slow driver, wrapped in :class:`TickMetrics`, driven through a
  real bounded ``reachy.behavior.engine.run`` — proving the wrapper is a
  drop-in ``tick_seam`` and ``ctx.emit`` still reaches consumers through it.

Everything is deterministic except the integration test, which uses a tiny
real ``time.sleep`` in the slow driver against a tiny budget so an overrun is
unambiguous without needing to fake wall-clock time end to end (the engine's
OWN loop clock/sleep stay fully injected/fake, so the run itself is fast).
"""

from __future__ import annotations

import contextlib
import logging
import time
from types import SimpleNamespace

import pytest

from reachy.behavior import engine as E
from reachy.behavior.engine import EngineConfig
from reachy.behavior.rule_engine import TickBus
from reachy.behavior.tick_metrics import TickMetrics, budget_from_hz

SENSE_LOGGER = "reachy.sense"


def _sense_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


class _QueueClock:
    """A ``duration_clock`` stub returning a scripted sequence of readings.

    Each call pops the next reading, so a test can script exact elapsed
    durations (``end - start`` per tick) with no real time involved.
    """

    def __init__(self, readings):
        self._readings = list(readings)

    def __call__(self) -> float:
        return self._readings.pop(0)


def _ctx(tick: int) -> SimpleNamespace:
    """A minimal stand-in ``TickContext`` — only ``.tick`` is read by TickMetrics."""
    return SimpleNamespace(tick=tick)


# --------------------------------------------------------------------------- #
# budget_from_hz                                                              #
# --------------------------------------------------------------------------- #


def test_budget_from_hz_derives_seconds_per_tick() -> None:
    assert budget_from_hz(50.0) == pytest.approx(0.02)
    assert budget_from_hz(100.0) == pytest.approx(0.01)


def test_budget_from_hz_rejects_non_positive_rate() -> None:
    with pytest.raises(ValueError):
        budget_from_hz(0.0)
    with pytest.raises(ValueError):
        budget_from_hz(-1.0)


# --------------------------------------------------------------------------- #
# Normal bounded run — zero overrun lines, counter stays 0                    #
# --------------------------------------------------------------------------- #


def test_normal_run_emits_no_overrun_lines_and_counter_stays_zero(caplog) -> None:
    # 3 ticks, each measured elapsed = 0.001s, well under a 0.02s (50Hz) budget.
    clock = _QueueClock([0.000, 0.001, 0.001, 0.002, 0.002, 0.003])
    calls = []
    metrics = TickMetrics(calls.append, budget_s=budget_from_hz(50.0), duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        for i in range(1, 4):
            metrics(_ctx(i))

    assert calls == [_ctx(1), _ctx(2), _ctx(3)]
    assert _sense_lines(caplog) == []
    assert metrics.overruns == 0


def test_normal_run_still_calls_inner_seam_every_tick() -> None:
    """The wrapper must not swallow or skip the wrapped seam's own work."""
    seen = []
    clock = _QueueClock([0.0, 0.0001] * 5)
    metrics = TickMetrics(seen.append, budget_s=1.0, duration_clock=clock)
    for i in range(1, 6):
        metrics(_ctx(i))
    assert len(seen) == 5


# --------------------------------------------------------------------------- #
# Synthetic slow tick — exactly one [SENSE] line per overrun                  #
# --------------------------------------------------------------------------- #


def test_slow_tick_emits_exactly_one_sense_line_with_measured_and_budget_ms(caplog) -> None:
    budget_s = budget_from_hz(50.0)  # 0.02s -> 20.00ms
    clock = _QueueClock([0.000, 0.030])  # elapsed = 0.030s -> 30.00ms, over budget
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        metrics(_ctx(1))

    lines = _sense_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("[SENSE stage=rule source=tick event=overrun]")
    assert "tick=1" in lines[0]
    assert "duration_ms=30.00" in lines[0]
    assert "budget_ms=20.00" in lines[0]
    assert metrics.overruns == 1


def test_exactly_one_overrun_line_per_slow_tick_not_per_normal_tick(caplog) -> None:
    budget_s = 0.02
    # tick1 slow (0.03), tick2 fast (0.001), tick3 slow (0.029)
    readings = [0.000, 0.030, 0.030, 0.031, 0.031, 0.060]
    clock = _QueueClock(readings)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        for i in range(1, 4):
            metrics(_ctx(i))

    lines = [ln for ln in _sense_lines(caplog) if "event=overrun" in ln]
    assert len(lines) == 2
    assert "tick=1" in lines[0]
    assert "tick=3" in lines[1]
    assert metrics.overruns == 2


def test_overrun_is_measured_even_when_inner_raises(caplog) -> None:
    """A tick that errors out slowly still counts + logs as an overrun."""
    clock = _QueueClock([0.000, 0.030])

    def boom(_ctx):
        raise RuntimeError("seam boom")

    metrics = TickMetrics(boom, budget_s=0.02, duration_clock=clock)
    ctx = _ctx(1)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        with pytest.raises(RuntimeError):
            metrics(ctx)

    assert metrics.overruns == 1
    assert any("event=overrun" in ln for ln in _sense_lines(caplog))


# --------------------------------------------------------------------------- #
# .emit proxy — transparent to existing TickBus consumers                     #
# --------------------------------------------------------------------------- #


def test_emit_proxies_to_the_wrapped_seams_emit() -> None:
    seen = []
    bus = TickBus(consumers=[seen.append])
    metrics = TickMetrics(bus, budget_s=1.0)
    metrics.emit({"type": "rule.fire"})
    assert seen == [{"type": "rule.fire"}]


def test_emit_is_a_safe_noop_when_wrapped_seam_exposes_none() -> None:
    metrics = TickMetrics(lambda _ctx: None, budget_s=1.0)
    metrics.emit({"type": "anything"})  # must not raise


# --------------------------------------------------------------------------- #
# Integration — TickBus + a slow driver, wrapped, through a real bounded run  #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.poses = []

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    """The engine's own injected LOGICAL clock — fast, no real sleeping."""

    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


class _SlowDriver:
    """A TickBus driver that genuinely takes real wall-clock time per tick.

    Only this driver's own real ``time.sleep`` consumes wall time; the
    engine's outer loop clock/sleep are fully injected/fake (see ``_Clock`` /
    ``sleep=lambda *_: None`` below), so the run stays fast overall.
    """

    def __init__(self, seconds: float):
        self._seconds = seconds
        self.calls = 0

    def __call__(self, ctx) -> None:
        self.calls += 1
        time.sleep(self._seconds)


def test_tickbus_with_slow_driver_overruns_through_a_real_bounded_engine_run(caplog) -> None:
    slow = _SlowDriver(0.01)  # 10ms of real sleep per tick
    events: list[dict] = []
    bus = TickBus(drivers=[slow], consumers=[events.append])
    # A deliberately tiny budget (1ms) so the real 10ms sleep unambiguously overruns
    # on any hardware, with the DEFAULT real duration_clock (time.perf_counter).
    metrics = TickMetrics(bus, budget_s=0.001)

    tr = _FakeTransport()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ticks = E.run(
            tr,
            EngineConfig(compose_hz=50, base_layer=True, settle=False),
            sleep=lambda *_: None,
            now=_Clock(),
            max_ticks=3,
            tick_seam=metrics,
        )

    assert ticks == 3
    assert slow.calls == 3
    lines = [ln for ln in _sense_lines(caplog) if "event=overrun" in ln]
    assert len(lines) == 3
    assert metrics.overruns == 3


def test_bounded_run_with_a_fast_driver_emits_no_overrun_lines(caplog) -> None:
    """Control: a fast driver under a generous budget never overruns (real clock)."""
    fast_calls = {"n": 0}

    def fast_driver(ctx):
        fast_calls["n"] += 1

    bus = TickBus(drivers=[fast_driver])
    metrics = TickMetrics(bus, budget_s=budget_from_hz(50.0))  # 20ms, generous

    tr = _FakeTransport()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ticks = E.run(
            tr,
            EngineConfig(compose_hz=50, base_layer=True, settle=False),
            sleep=lambda *_: None,
            now=_Clock(),
            max_ticks=5,
            tick_seam=metrics,
        )

    assert ticks == 5
    assert fast_calls["n"] == 5
    assert _sense_lines(caplog) == []
    assert metrics.overruns == 0
