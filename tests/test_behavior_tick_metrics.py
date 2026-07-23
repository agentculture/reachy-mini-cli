"""Tick-budget observability — overrun counter + ``[SENSE]`` line (t14, t121).

Assumption c22 says the engine's tick budget (``1 / compose_hz``) holds on real
hardware but is unmeasured. :class:`~reachy.behavior.tick_metrics.TickMetrics`
wraps the engine's ONE per-tick seam and makes an overrun observable: a real
wall-clock duration probe (injectable, NOT ``ctx.now``) compared against a
budget, logged via the same ``[SENSE ...]`` convention
:mod:`reachy.behavior.rule_engine` uses.

t121 ported the #99 episode-suppression pattern in here (see the module
docstring): logging every overrunning tick unconditionally flooded a 30-minute
journal with 69,696 lines at a measured 77.4% overrun rate on the deployed
box. The tests below pin BOTH the original per-tick-overrun mechanics (still
exactly true — ``.overruns`` never lies) and the new streak behavior:

* a normal bounded run (fast fake ``duration_clock``) emits ZERO overrun lines
  and leaves ``.overruns == 0``;
* the FIRST overrun tick of a streak still emits the ORIGINAL, UNCHANGED
  ``[SENSE stage=rule source=tick event=overrun]`` line, with measured +
  budget milliseconds in the detail, and increments ``.overruns``;
* every CONTINUING tick of the same streak (same "reason" — there is only one
  here) is counted but logged SILENTLY;
* a tick more than 5x over budget always reports immediately, even mid-streak
  (the spike-bypass rule, so a 425-1213 ms startup stall never hides inside a
  streak summary);
* the first in-budget tick after a streak closes it with ONE
  ``event=overrun-summary`` line naming count/mean/max/budget;
* :meth:`~reachy.behavior.tick_metrics.TickMetrics.close` flushes a streak
  still open (the shutdown path), idempotently;
* a simulated 30-minute run at a 77% overrun rate produces O(10) journal
  lines, not O(70,000), while ``.overruns`` stays the exact true count.

Everything is deterministic except the ``TickBus`` integration tests, which
use a tiny real ``time.sleep`` in the slow driver against a tiny budget so an
overrun is unambiguous without needing to fake wall-clock time end to end (the
engine's OWN loop clock/sleep stay fully injected/fake, so the run itself is
fast).
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


class _DurationClock:
    """A ``duration_clock`` stub scripted by PER-TICK DURATIONS, not readings.

    ``TickMetrics`` calls ``duration_clock()`` twice per tick (start, end);
    this precomputes the full reading sequence as one flat, index-addressed
    list up front so each call is O(1) — unlike ``_QueueClock``'s
    ``list.pop(0)``, which is O(n) per call and would make a 90,000-tick
    simulated run (criterion 1 below) quadratic. Consecutive ticks share no
    gap (each tick's end reading is the next tick's start reading), which is
    irrelevant to ``TickMetrics`` since it only ever reads the difference
    within one tick's own pair.
    """

    def __init__(self, durations):
        self._readings: list[float] = []
        t = 0.0
        for duration in durations:
            self._readings.append(t)
            t += duration
            self._readings.append(t)
        self._i = 0

    def __call__(self) -> float:
        value = self._readings[self._i]
        self._i += 1
        return value


def _ctx(tick: int) -> SimpleNamespace:
    """A minimal stand-in ``TickContext`` — only ``.tick`` is read by TickMetrics."""
    return SimpleNamespace(tick=tick)


def _run(metrics, durations) -> None:
    """Drive *metrics* through one call per scripted duration, ticks 1..N."""
    for i in range(1, len(durations) + 1):
        metrics(_ctx(i))


def _overrun_lines(caplog) -> list[str]:
    """Per-tick overrun entries only — excludes closing-summary lines.

    ``event=overrun-summary]`` is deliberately NOT matched by ``"event=
    overrun]"`` (the trailing bracket requires an exact token), so this and
    :func:`_summary_lines` partition ``_sense_lines`` cleanly.
    """
    return [ln for ln in _sense_lines(caplog) if "event=overrun]" in ln]


def _summary_lines(caplog) -> list[str]:
    return [ln for ln in _sense_lines(caplog) if "event=overrun-summary]" in ln]


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


def test_a_fast_tick_between_two_slow_ticks_closes_and_reopens_a_streak(caplog) -> None:
    """Superseded ``test_exactly_one_overrun_line_per_slow_tick_not_per_normal_tick``.

    Pre-t121 this asserted 2 unconditional overrun lines for 2 slow ticks. The
    #121 fix now also emits a closing-summary line when the fast tick between
    them ends the first (one-tick) streak — this pins the new shape instead.
    """
    budget_s = 0.02
    # tick1 slow (0.03) opens a streak + entry line; tick2 fast (0.001) closes
    # it + summary line; tick3 slow (0.029) opens a NEW streak + entry line.
    clock = _DurationClock([0.030, 0.001, 0.029])
    # calm_ticks_to_close=1: this test is about the open/close mechanic itself,
    # not about the shipped hysteresis (pinned by the alternating-regime test).
    metrics = TickMetrics(
        lambda _ctx: None, budget_s=budget_s, duration_clock=clock, calm_ticks_to_close=1
    )

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, [0.030, 0.001, 0.029])

    entries = _overrun_lines(caplog)
    summaries = _summary_lines(caplog)
    assert len(entries) == 2
    assert "tick=1" in entries[0]
    assert "tick=3" in entries[1]
    assert len(summaries) == 1
    assert "tick=1" in summaries[0]  # the summary names the streak's LAST tick
    assert "count=1" in summaries[0]
    assert metrics.overruns == 2


# --------------------------------------------------------------------------- #
# #121 — episode suppression: silent continuation, spike bypass, summaries,   #
# shutdown flush, exactness, and the 30-minute simulated-flood acceptance     #
# --------------------------------------------------------------------------- #


def test_continuation_of_a_streak_under_budget_logs_silently(caplog) -> None:
    """The flood-prevention step itself: only the streak's FIRST tick logs."""
    budget_s = 0.02
    durations = [0.025, 0.024, 0.023, 0.026]  # 4 moderate (non-spike) overruns
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    entries = _overrun_lines(caplog)
    assert len(entries) == 1  # ticks 2-4 counted, not logged
    assert "tick=1" in entries[0]
    assert _summary_lines(caplog) == []  # streak never closed in this run
    assert metrics.overruns == 4  # exact true count regardless of suppression


def test_spike_over_5x_budget_reports_immediately_even_mid_streak(caplog) -> None:
    """Criterion 3: a >5x-budget spike bypasses suppression on the tick it occurs."""
    budget_s = 0.02  # budget_ms = 20.00; 5x = 100.00ms
    # tick1: moderate overrun opens the streak (entry line).
    # tick2: moderate overrun continues it SILENTLY.
    # tick3: a genuine spike (120ms, 6x budget) must still report immediately.
    # tick4: moderate overrun continues silently again (streak never closed).
    durations = [0.025, 0.024, 0.120, 0.022]
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    entries = _overrun_lines(caplog)
    assert len(entries) == 2  # tick1 (streak open) + tick3 (spike bypass)
    assert "tick=1" in entries[0]
    assert "tick=3" in entries[1]
    assert "duration_ms=120.00" in entries[1]
    assert "budget_ms=20.00" in entries[1]
    assert metrics.overruns == 4  # every over-budget tick still counted exactly


def test_spike_exactly_at_5x_budget_does_not_bypass(caplog) -> None:
    """Boundary: "exceeding" 5x is strict — exactly 5x is an ordinary continuation."""
    budget_s = 0.02  # 5x budget = 0.100s exactly
    durations = [0.025, 0.100]
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    entries = _overrun_lines(caplog)
    assert len(entries) == 1  # tick2 at exactly 5x stays a silent continuation
    assert metrics.overruns == 2


def test_closing_summary_carries_count_mean_and_max_vs_budget(caplog) -> None:
    """Criterion 1: the closing summary reports count/mean/max vs the budget."""
    budget_s = budget_from_hz(50.0)  # 20.00ms
    # Three overruns (21, 30, 24 ms) then one fast tick (5ms) closes the streak.
    durations = [0.021, 0.030, 0.024, 0.005]
    clock = _DurationClock(durations)
    metrics = TickMetrics(
        lambda _ctx: None, budget_s=budget_s, duration_clock=clock, calm_ticks_to_close=1
    )

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    summaries = _summary_lines(caplog)
    assert len(summaries) == 1
    line = summaries[0]
    assert line.startswith("[SENSE stage=rule source=tick event=overrun-summary]")
    assert "tick=3" in line  # names the streak's last (not the closing) tick
    assert "count=3" in line
    mean_ms = (21.0 + 30.0 + 24.0) / 3
    assert f"mean_ms={mean_ms:.2f}" in line
    assert "max_ms=30.00" in line
    assert "budget_ms=20.00" in line
    assert metrics.overruns == 3


def test_overruns_counter_is_exact_regardless_of_suppression(caplog) -> None:
    """Criterion 2: ``.overruns`` is the TRUE per-tick count, suppression or not."""
    budget_s = 0.02
    # 50 consecutive moderate overruns (one streak, one logged entry line),
    # then 10 fast ticks (closes it, one summary line), then 5 more overruns
    # (a second streak).
    durations = [0.025] * 50 + [0.005] * 10 + [0.03] * 5
    clock = _DurationClock(durations)
    metrics = TickMetrics(
        lambda _ctx: None, budget_s=budget_s, duration_clock=clock, calm_ticks_to_close=10
    )

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    assert metrics.overruns == 55
    assert len(_overrun_lines(caplog)) == 2  # one entry per streak (50 + 5)
    assert len(_summary_lines(caplog)) == 1  # only the first streak ever closed


def test_close_flushes_a_streak_still_open_at_shutdown(caplog) -> None:
    """Criterion 4: an episode open at shutdown is flushed by the close path."""
    budget_s = 0.02
    durations = [0.025, 0.024, 0.026]  # the streak never closes on its own
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)
        assert _summary_lines(caplog) == []  # nothing closed it yet

        metrics.close()

    summaries = _summary_lines(caplog)
    assert len(summaries) == 1
    assert "tick=3" in summaries[0]
    assert "count=3" in summaries[0]
    assert metrics.overruns == 3


def test_close_is_a_noop_when_no_streak_is_open(caplog) -> None:
    metrics = TickMetrics(lambda _ctx: None, budget_s=0.02)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        metrics.close()  # nothing open; must not raise or log
    assert _sense_lines(caplog) == []


def test_close_is_idempotent(caplog) -> None:
    budget_s = 0.02
    durations = [0.025]
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)
        metrics.close()
        metrics.close()  # a second flush of an already-closed streak is a no-op

    assert len(_summary_lines(caplog)) == 1


def test_simulated_30_minute_run_at_77_percent_overrun_emits_order_10_lines(caplog) -> None:
    """Criterion 1, in full: the flood fix at the measured deployed-box rate.

    ~90,000 ticks (30 minutes at the default 50 Hz) split into 10 evenly-sized
    blocks, each 77% overrun (moderate, non-spike) then 23% comfortably under
    budget — one streak per block. Pre-t121 this rate produced 69,696
    unconditional overrun lines on the deployed box; post-t121 it must produce
    O(10) lines (open + close per streak), an order of magnitude, not O(70,000).
    """
    budget_s = budget_from_hz(50.0)  # 0.02s
    total_ticks = 90_000
    n_blocks = 10
    block = total_ticks // n_blocks
    overrun_per_block = round(block * 0.77)
    idle_per_block = block - overrun_per_block

    durations: list[float] = []
    for _ in range(n_blocks):
        durations.extend([0.025] * overrun_per_block)  # 25ms > 20ms budget, moderate
        durations.extend([0.005] * idle_per_block)  # 5ms, comfortably under budget

    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    total_lines = len(_sense_lines(caplog))
    entries = _overrun_lines(caplog)
    summaries = _summary_lines(caplog)

    # .overruns is exact regardless of how many lines got suppressed.
    assert metrics.overruns == n_blocks * overrun_per_block
    # O(10): the real fix — tens of lines, nowhere near the 69,696-line flood.
    assert 0 < total_lines < 100
    assert len(entries) == n_blocks  # one entry line per streak
    assert len(summaries) == n_blocks  # one closing summary per streak


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


def test_the_live_alternating_regime_stays_one_episode(caplog) -> None:
    """The regime measured on the robot, which single-tick closing got wrong.

    Ticks alternate over/under budget rather than overrunning in long blocks.
    With ``calm_ticks_to_close=1`` this produced a 3-4 tick episode -- and TWO
    lines per episode -- roughly every 5 ticks: 578 summaries in 60 s live,
    worse per episode than the single line #121 set out to replace. With the
    shipped hysteresis the whole run is ONE episode.
    """
    budget_s = 0.02
    # 600 ticks: 4 over, 1 under, repeating -- the shape seen in the journal.
    durations = ([0.021] * 4 + [0.010]) * 120
    clock = _DurationClock(durations)
    metrics = TickMetrics(lambda _ctx: None, budget_s=budget_s, duration_clock=clock)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    assert metrics.overruns == 480  # every overrunning tick still counted
    assert len(_overrun_lines(caplog)) == 1  # one entry for the whole run
    assert len(_summary_lines(caplog)) == 0  # never calm long enough to close


def test_a_long_open_episode_checkpoints_instead_of_going_silent(caplog) -> None:
    """A never-closing episode must still report itself.

    With hysteresis the live engine legitimately never closes its episode, so
    without a checkpoint the journal shows ONE line at boot and nothing after —
    an operator cannot tell a healthy engine from one wedged in permanent
    overrun. That is the silent-no-op class #120 was about; do not trade the
    flood for silence.
    """
    budget_s = 0.02
    durations = [0.021] * 1000
    clock = _DurationClock(durations)
    metrics = TickMetrics(
        lambda _ctx: None,
        budget_s=budget_s,
        duration_clock=clock,
        checkpoint_every_ticks=250,
    )

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run(metrics, durations)

    checkpoints = [
        r.getMessage() for r in caplog.records if "event=overrun-ongoing" in r.getMessage()
    ]
    assert len(checkpoints) == 4  # 1000 ticks / 250
    assert "overrun streak ongoing" in checkpoints[0]
    assert "count=250" in checkpoints[0]
    assert len(_overrun_lines(caplog)) == 1  # still just the one entry line
    assert len(_summary_lines(caplog)) == 0  # never closed
    assert metrics.overruns == 1000
