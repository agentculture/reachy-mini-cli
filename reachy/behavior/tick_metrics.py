"""Tick-budget observability — measure tick duration, log overruns loudly.

Assumption c22 says the engine's 20 ms tick budget (``1 / compose_hz`` at the
default 50 Hz) holds in practice, but nothing in the engine measures it on real
hardware — a slow rule, a slow goto interpolation, or a GC pause could blow the
budget silently. This module makes an overrun *observable* without touching
``engine.py`` or ``rule_engine.py``: it is a thin WRAPPER around the engine's
ONE per-tick seam (whatever callable is passed as ``tick_seam=``, typically a
:class:`~reachy.behavior.rule_engine.TickBus`), so it sees the *real* wall-clock
duration of a tick's full seam work — every driver the bus fans out to, not
just one of them.

Why a wrapper, not one more ``TickBus`` driver
-----------------------------------------------
A :class:`~reachy.behavior.rule_engine.TickBus` driver only ever times *itself*
— it has no visibility into how long its sibling drivers take. "Each tick's
seam work" means the total time the whole seam call takes, so
:class:`TickMetrics` wraps the seam callable itself (compose it as
``TickMetrics(inner=bus, budget_s=...)`` and pass the result as
``tick_seam=``), timing ``inner(ctx)`` end to end. It is still a drop-in
``tick_seam`` — same one-arg-callable contract as a bare driver or a
``TickBus`` — so composition nests without engine.py knowing anything changed.

Two clocks, not one
--------------------
``ctx.now`` is the engine's *logical* clock (injected, deterministic under
tests — see :class:`reachy.behavior.engine.TickContext`); it says nothing about
how long the tick took to *compute*. Measuring real overrun needs a second,
independent *duration* clock — wall-clock, monotonic, real time —
:data:`time.perf_counter` by default, but injectable (``duration_clock=``) so
tests can script exact readings without sleeping for real.

Overruns are logged per EPISODE, not per tick (#121, porting the #99 fix)
---------------------------------------------------------------------------
This is a LOGGING defect fix, not a latency fix: a slow tick is acceptable —
work that does not fit one 20 ms tick is fine to pick up on the next tick(s).
Logging every overrunning tick unconditionally is what is wrong. Measured on
the deployed box (2026-07-23): 69,696 ``[SENSE ... event=overrun]`` lines over
a 30-minute window, 77.4% of ticks, ~39 lines/second — enough to bury real
events and make ``journalctl | grep`` time out at 120 s.

:mod:`reachy.behavior.rule_engine` solved exactly this defect class for #99
(see its module docstring and ``_suppress``/``_release_suppression``); this
module ports that shape onto its own single "reason" (there is only one kind
of overrun here, unlike a rule's several suppression reasons):

* the FIRST overrun tick of a streak emits the usual
  ``[SENSE stage=rule source=tick event=overrun]`` line — UNCHANGED format
  from before this fix, naming the measured and budgeted duration in
  milliseconds;
* every CONTINUING tick of the same streak is counted (``.overruns`` still
  increments) but logged silently — this is the flood-prevention step;
* the first non-overrunning tick after a streak closes it with ONE
  ``[SENSE stage=rule source=tick event=overrun-summary]`` line naming the
  streak's tick count, mean duration, max duration and the budget;
* a tick whose duration exceeds :data:`SPIKE_MULTIPLIER` times the budget
  (5x) ALWAYS reports immediately, even mid-streak, bypassing suppression —
  so the 425-1213 ms startup-stall class (21x-61x over budget) stays visible
  and is never hidden inside a streak summary;
* an episode still open when the runtime stops is not silently lost: calling
  :meth:`TickMetrics.close` flushes it with the same closing-summary line.
  ``rule_engine``'s own #99 fix has a documented edge here — a streak open at
  loop stop emits no summary, only its entry line — but for a tick-overrun
  streak that edge matters more: at a sustained 77% overrun rate the streak
  may never close on its own, so a caller that never flushes it would show no
  usable count/mean/max for however long the process ran overrun. ``close()``
  is idempotent, so calling it from a shutdown path that may run more than
  once (or a path that also races an already-closed streak) is always safe.

``.overruns`` is UNCHANGED semantics throughout: it is the exact count of
every tick whose measured duration exceeded budget, independent of how many
of those ticks were logged versus suppressed — the status/test readout must
never lie about how bad things really are, only the JOURNAL gets quieter.

``ctx.emit`` pass-through
--------------------------
The engine looks for a ``.emit`` attribute on whatever ``tick_seam`` it was
given (see ``reachy.behavior.engine._drive``) to wire ``ctx.emit`` for every
driver on the wrapped seam. :class:`TickMetrics` proxies its own ``.emit`` to
the wrapped seam's ``.emit`` (falling through to a no-op when the wrapped seam
exposes none), so wrapping with metrics is transparent to every existing
consumer — nothing about the export feed or rule-fire events changes.

Composition (how ``cmd_engine_run`` would wire this — not yet wired; see
``docs`` / the task report for the exact snippet)
--------------------------------------------------------------------------
::

    from reachy.behavior.tick_metrics import TickMetrics, budget_from_hz

    tick_seam = TickBus(drivers=drivers, consumers=consumers)  # existing composition
    tick_seam = TickMetrics(tick_seam, budget_s=budget_from_hz(config.compose_hz))
    engine_run(transport, config, tick_seam=tick_seam, ...)
    ...
    tick_seam.close()  # shutdown: flush any overrun streak still open

Pure standard library (``time``/``logging`` via :mod:`reachy.senselog`) plus
in-package imports; nothing here touches ``reachy_mini`` or a network.
"""

from __future__ import annotations

import time
from typing import Callable

from reachy import senselog
from reachy.behavior.rule_engine import STAGE

#: The tick-metrics ``source=`` / ``event=`` tokens, kept distinct from
#: ``rule_engine``'s per-field/per-rule tokens since a tick-budget observation
#: has neither a sense field nor a rule id.
SOURCE = "tick"
EVENT_OVERRUN = "overrun"
#: The episode-closing summary's ``event=`` token (#121, porting #99's
#: ``_release_suppression`` line). Deliberately a distinct token from
#: :data:`EVENT_OVERRUN` (not just a suffix a caller could confuse via a loose
#: substring match) — see the module docstring's streak-summary bullet.
EVENT_OVERRUN_SUMMARY = "overrun-summary"

#: A tick this many times over budget always reports immediately, even
#: mid-streak — the spike-bypass rule that keeps the 425-1213 ms startup-stall
#: class (21x-61x over budget, see ``docs/verification/
#: 2026-07-20-retire-old-flow-baseline.md`` section 3) visible no matter what
#: streak it lands inside.
SPIKE_MULTIPLIER = 5.0

#: Consecutive in-budget ticks required before an overrun episode is declared
#: over.  One is not enough: measured on the live robot the loop alternates
#: over/under budget, so single-tick closing produced 3-4 tick episodes and
#: TWO log lines each (entry + summary) -- 578 summaries in 60 s, defeating the
#: whole point of #121.  At 50 Hz this is one second of calm.
DEFAULT_CALM_TICKS_TO_CLOSE = 50


def _noop_emit(_event: dict) -> None:
    """Default ``.emit`` when the wrapped seam exposes none."""


def budget_from_hz(compose_hz: float) -> float:
    """Derive a tick budget in seconds from the engine's ``compose_hz``.

    Mirrors ``engine._timing``'s own ``period = 1.0 / compose_hz`` derivation
    (duplicated, not imported, to keep this module's only in-package dependency
    ``rule_engine.STAGE`` — see the module docstring). Raises ``ValueError`` for
    a non-positive rate; there is no sensible budget for a stopped/inverted
    cadence.
    """
    if compose_hz <= 0:
        raise ValueError(f"compose_hz must be positive to derive a budget, got {compose_hz!r}")
    return 1.0 / compose_hz


class TickMetrics:
    """Wrap a ``tick_seam`` callable, timing + counting real-duration overruns.

    Usable directly as ``engine.run(tick_seam=TickMetrics(inner, budget_s=...))``
    — it is itself a one-argument callable (``__call__(ctx)``) and proxies
    ``.emit`` to the wrapped seam, so it drops in wherever a bare driver or a
    :class:`~reachy.behavior.rule_engine.TickBus` would go.

    Args:
        inner: the wrapped ``tick_seam`` callable (a ``TickBus``, a bare
            driver, or any ``ctx -> None`` callable) — invoked exactly once per
            call, timed end to end.
        budget_s: the per-tick wall-clock budget in seconds. Callers derive
            this from the engine's cadence via :func:`budget_from_hz`.
        duration_clock: an injectable ``() -> float`` real-duration probe,
            default :func:`time.perf_counter`. Deliberately NOT ``ctx.now`` —
            that is the engine's logical clock (see the module docstring).

    Attributes:
        overruns: the running count of ticks whose measured duration exceeded
            ``budget_s`` — readable for a status readout or a test assertion.
            Exact and unaffected by streak suppression (see the module
            docstring).
    """

    def __init__(
        self,
        inner: Callable[[object], None],
        *,
        budget_s: float,
        duration_clock: Callable[[], float] = time.perf_counter,
        calm_ticks_to_close: int = DEFAULT_CALM_TICKS_TO_CLOSE,
    ) -> None:
        self._inner = inner
        self.budget_s = budget_s
        self._duration_clock = duration_clock
        self.overruns = 0
        # -- open-episode state (#121, ported #99 shape) ------------------ #
        self._calm_ticks_to_close = max(1, int(calm_ticks_to_close))
        self._calm_ticks = 0
        self._streak_open = False
        self._streak_ticks = 0
        self._streak_sum_ms = 0.0
        self._streak_max_ms = 0.0
        self._streak_end_tick: object = None

    def __call__(self, ctx) -> None:
        """Invoke the wrapped seam once, timing it; log + count on overrun.

        The duration is measured (and an overrun logged) even if ``inner``
        raises — a tick that errors out slowly is still a tick that ran over
        budget — via ``try/finally``; the original exception always propagates
        unchanged (this wrapper adds no fault isolation of its own, matching
        every other seam wrapper in this codebase, see ``TickBus``'s docstring
        on where isolation *does* live).
        """
        start = self._duration_clock()
        try:
            self._inner(ctx)
        finally:
            elapsed = self._duration_clock() - start
            if elapsed > self.budget_s:
                self.overruns += 1
                self._calm_ticks = 0
                self._record_overrun(ctx, elapsed)
            else:
                # HYSTERESIS -- load-bearing, measured on the live robot.
                # Closing an episode on the FIRST in-budget tick makes the
                # suppression useless in the real regime: ticks alternate
                # over/under budget, so streaks are 3-4 ticks long and each
                # one costs an entry line PLUS a summary line -- 578 summaries
                # in 60 s on spark-f8a9, worse per streak than the single line
                # it replaced.  A streak is only really over once the loop has
                # stayed inside budget for a while.
                self._calm_ticks += 1
                if self._calm_ticks >= self._calm_ticks_to_close:
                    self._close_episode()

    # -- episode bookkeeping (#121) ---------------------------------------- #

    def _record_overrun(self, ctx, elapsed: float) -> None:
        """Fold one overrunning tick into the open episode (or open a new one).

        Always updates the streak's count/sum/max — ``.overruns`` and this
        bookkeeping never lie about the true extent of the streak, only the
        JOURNAL LINE for a continuing, non-spike tick is suppressed (the #99
        flood fix, see the module docstring).
        """
        tick = getattr(ctx, "tick", "?")
        elapsed_ms = elapsed * 1000.0
        budget_ms = self.budget_s * 1000.0
        is_spike = elapsed_ms > budget_ms * SPIKE_MULTIPLIER
        opening = not self._streak_open
        if opening:
            self._streak_open = True
            self._streak_ticks = 0
            self._streak_sum_ms = 0.0
            self._streak_max_ms = 0.0
        self._streak_ticks += 1
        self._streak_sum_ms += elapsed_ms
        self._streak_max_ms = max(self._streak_max_ms, elapsed_ms)
        self._streak_end_tick = tick
        if opening or is_spike:
            self._log_overrun(tick, elapsed_ms, budget_ms)

    def _close_episode(self) -> None:
        """Emit the closing summary for an open streak, then reset it.

        A no-op when no episode is open, so an ordinary in-budget tick stays
        exactly as silent as before this fix. Called both from the normal
        tick path (the first in-budget tick after a streak) and from
        :meth:`close` (the shutdown-flush path) — the two share one summary
        shape so a flushed streak reads identically to a naturally-closed one.
        """
        if not self._streak_open:
            return
        self._emit_summary()
        self._reset_streak()

    def _emit_summary(self) -> None:
        budget_ms = self.budget_s * 1000.0
        mean_ms = self._streak_sum_ms / self._streak_ticks
        senselog.stage(
            STAGE,
            SOURCE,
            EVENT_OVERRUN_SUMMARY,
            f"overrun streak ended tick={self._streak_end_tick} "
            f"count={self._streak_ticks} mean_ms={mean_ms:.2f} "
            f"max_ms={self._streak_max_ms:.2f} budget_ms={budget_ms:.2f}",
        )

    def _reset_streak(self) -> None:
        self._streak_open = False
        self._streak_ticks = 0
        self._streak_sum_ms = 0.0
        self._streak_max_ms = 0.0
        self._streak_end_tick = None

    def close(self) -> None:
        """Flush an overrun streak still open, e.g. when the runtime shuts down.

        Improves on ``rule_engine``'s #99 edge (a streak open at loop stop
        emits no summary) rather than replicating it: at a sustained high
        overrun rate a streak may never close on its own, so without this the
        whole run's tail could be invisible in the journal. Idempotent — a
        second call, or a call when no episode is open, is a safe no-op — so a
        caller may wire this into a teardown path that could run more than
        once (mirroring ``_RuntimeResources.close``'s own idempotency).
        """
        self._close_episode()

    def _log_overrun(self, tick, elapsed_ms: float, budget_ms: float) -> None:
        senselog.stage(
            STAGE,
            SOURCE,
            EVENT_OVERRUN,
            f"overrun tick={tick} duration_ms={elapsed_ms:.2f} budget_ms={budget_ms:.2f}",
        )

    @property
    def emit(self) -> Callable[[dict], None]:
        """Proxy to the wrapped seam's ``.emit`` (a no-op when it exposes none).

        The engine looks up ``tick_seam.emit`` once per run (see
        ``reachy.behavior.engine._drive``) to wire ``ctx.emit`` for every
        driver the wrapped seam fans out to; without this proxy, wrapping a
        ``TickBus`` in :class:`TickMetrics` would silently swallow every
        ``rule.fire`` / ``rule.suppress`` / export event.
        """
        candidate = getattr(self._inner, "emit", None)
        return candidate if callable(candidate) else _noop_emit
