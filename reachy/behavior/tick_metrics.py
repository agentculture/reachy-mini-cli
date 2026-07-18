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

Emitted line
------------
On overrun (measured duration ``> budget_s``) this module emits exactly one
``[SENSE stage=rule source=tick event=overrun]`` line via
:mod:`reachy.senselog`, naming the measured and budgeted duration in
milliseconds — consistent with how :mod:`reachy.behavior.rule_engine` names its
own lines (``stage="rule"``; here ``source="tick"``/``event="overrun"`` since
there is no sense field or rule id for a tick-budget observation). A tick
within budget logs nothing — same "silent unless notable" convention as
:mod:`reachy.behavior.rule_engine`'s no-match ticks. The running overrun count
is kept on ``.overruns`` for a status readout or a test assertion.

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
    """

    def __init__(
        self,
        inner: Callable[[object], None],
        *,
        budget_s: float,
        duration_clock: Callable[[], float] = time.perf_counter,
    ) -> None:
        self._inner = inner
        self.budget_s = budget_s
        self._duration_clock = duration_clock
        self.overruns = 0

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
                self._log_overrun(ctx, elapsed)

    def _log_overrun(self, ctx, elapsed: float) -> None:
        tick = getattr(ctx, "tick", "?")
        elapsed_ms = elapsed * 1000.0
        budget_ms = self.budget_s * 1000.0
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
