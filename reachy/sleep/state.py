"""Pure sleep-state machine driven by an injected monotonic idle clock.

The machine models graduated sleepiness: a robot that has been undisturbed for
long enough transitions ALERT → DROWSY → ASLEEP.  Any stimulating event
(speech, touch, vision motion, …) calls :meth:`SleepStateMachine.reset` to
snap the machine back to ALERT and zero the idle clock.

This module is **pure** — stdlib only (``enum``, ``dataclasses``); no robot,
transport, or threading imports.  The current time is always *injected* via the
``now=`` parameter so the machine is fully testable with a fake clock.

Typical usage::

    m = SleepStateMachine()
    m.update(now=time.monotonic())   # call each tick
    if m.state is SleepState.ASLEEP:
        ...  # enter low-power pose
    m.reset(now=time.monotonic())    # call on any stimulating event
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum, auto


class SleepState(Enum):
    """Graduated wakefulness states."""

    ALERT = auto()
    DROWSY = auto()
    ASLEEP = auto()


@dataclass
class SleepStateMachine:
    """Idle-timer state machine with three wakefulness levels.

    Parameters
    ----------
    drowsy_after:
        Seconds of uninterrupted idle time before transitioning ALERT → DROWSY.
        Default ≈ 75 s — a comfortable "starting to nod off" feel.
    asleep_after:
        Seconds of uninterrupted idle time before transitioning DROWSY → ASLEEP
        (and directly from ALERT if ``drowsy_after`` is exceeded in one jump).
        Default ≈ 150 s.  Must be ≥ ``drowsy_after``.

    The ``now=`` parameter on every mutating method must be a monotonically
    non-decreasing float (e.g. ``time.monotonic()``).  Backwards ticks are
    clamped to zero elapsed time — state never regresses.

    Read-only snapshot attributes:

    * :attr:`state` — current :class:`SleepState`.
    * :attr:`idle_seconds` — seconds elapsed since the last :meth:`reset` (or
      the very first :meth:`update` call if :meth:`reset` was never called).
    """

    drowsy_after: float = 75.0
    asleep_after: float = 150.0

    # --- internal bookkeeping (not part of the public API) ---
    _state: SleepState = field(default=SleepState.ALERT, init=False, repr=False)
    _idle_start: float | None = field(default=None, init=False, repr=False)
    _last_now: float | None = field(default=None, init=False, repr=False)

    # ------------------------------------------------------------------
    # Public read-only snapshot
    # ------------------------------------------------------------------

    @property
    def state(self) -> SleepState:
        """Current wakefulness state."""
        return self._state

    @property
    def idle_seconds(self) -> float:
        """Seconds of uninterrupted idle time since the last reset (or first update)."""
        if self._idle_start is None:
            return 0.0
        if self._last_now is None:
            return 0.0
        return max(0.0, self._last_now - self._idle_start)

    # ------------------------------------------------------------------
    # Mutating methods
    # ------------------------------------------------------------------

    def update(self, *, now: float) -> SleepState:
        """Advance the state machine to time *now*.

        Call this on every loop tick.  Returns the (possibly updated) state.

        Parameters
        ----------
        now:
            Current monotonic time in seconds (injected by the caller — never
            read from ``time.monotonic()`` internally).
        """
        # Anchor the idle clock on the very first call.
        if self._idle_start is None:
            self._idle_start = now

        # Clamp backwards ticks: don't let a stale now regress idle_seconds.
        if self._last_now is not None and now < self._last_now:
            now = self._last_now
        self._last_now = now

        elapsed = max(0.0, now - self._idle_start)

        if elapsed >= self.asleep_after:
            self._state = SleepState.ASLEEP
        elif elapsed >= self.drowsy_after:
            self._state = SleepState.DROWSY
        else:
            self._state = SleepState.ALERT

        return self._state

    def reset(self, *, now: float) -> SleepState:
        """Stimulation signal: zero the idle clock and return to ALERT.

        Call this whenever an event that indicates wakefulness occurs (speech
        detected, touch, vision motion, …).

        Parameters
        ----------
        now:
            Current monotonic time in seconds.

        Returns
        -------
        SleepState
            Always :attr:`SleepState.ALERT` after a reset.
        """
        # Clamp backwards ticks, exactly as update() does — a stale now must not
        # plant the idle clock in the past (the class contract documents this).
        if self._last_now is not None and now < self._last_now:
            now = self._last_now
        self._idle_start = now
        self._last_now = now
        self._state = SleepState.ALERT
        return self._state
