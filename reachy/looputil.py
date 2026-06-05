"""Shared primitives for signal-stoppable, fixed-rate loops.

Extracted from :mod:`reachy.alive` so both the demo-mode "feel alive" loop and
the behavior engine share one definition of "flip a stop flag on SIGTERM/SIGINT"
and "sleep an inter-tick gap in slices small enough that a stop signal lands
fast". Pure standard library — no third-party runtime dependency.

The ``stop`` argument is a one-key dict ``{"flag": bool}`` so the signal handler
(which cannot return a value) can mutate it in place and the loop can poll it.
"""

from __future__ import annotations

import signal

# Default granularity for slicing an inter-tick sleep. A slower loop (demo-mode,
# interval ~2.5 s) slices at this; a 50 Hz loop passes a finer slice equal to its
# own short period so it does not overshoot a whole tick on every iteration.
DEFAULT_SLEEP_SLICE = 0.25


def install_stop_handlers(stop: dict):
    """Install SIGTERM/SIGINT handlers that set ``stop['flag']``; return the olds.

    ``signal.signal`` only works in the main thread; under a test runner / worker
    thread it raises ``ValueError`` — in that case we run without graceful stop
    and return ``None`` (which :func:`restore_stop_handlers` treats as a no-op).
    """

    def _handler(_signum, _frame):
        stop["flag"] = True

    try:
        return (
            signal.signal(signal.SIGTERM, _handler),
            signal.signal(signal.SIGINT, _handler),
        )
    except ValueError:
        return None


def restore_stop_handlers(handlers) -> None:
    """Restore the handlers returned by :func:`install_stop_handlers` (None = no-op)."""
    if handlers is not None:
        signal.signal(signal.SIGTERM, handlers[0])
        signal.signal(signal.SIGINT, handlers[1])


def interruptible_sleep(
    seconds: float, stop: dict, sleep, slice_seconds: float = DEFAULT_SLEEP_SLICE
) -> None:
    """Sleep up to ``seconds`` in ``slice_seconds`` steps, waking early if stopped.

    ``sleep`` is injected (``time.sleep`` in production, a no-op in tests). Each
    slice is clamped to the time still remaining, so the total never overshoots
    ``seconds`` even when it is not an exact multiple of the slice (e.g. a 0.3 s
    gap with a 0.25 s slice sleeps 0.25 + 0.05, not 0.5).
    """
    if seconds <= 0:
        return
    step = slice_seconds if slice_seconds > 0 else seconds
    slept = 0.0
    while slept < seconds and not stop["flag"]:
        chunk = min(step, seconds - slept)  # clamp the last slice; never oversleep
        sleep(chunk)
        slept += chunk
