"""The motion queue and the action it carries.

A :class:`MotionAction` is a pure description of one interpolated move (a target pose in
the CLI's friendly units + a duration + an interpolation mode). :class:`MotionQueue` holds
the *pending* actions (the one currently executing lives in the executor, not the queue),
and applies the coalescing rule on submit: a new action whose ``coalesce_key`` is not
``None`` evicts any pending action sharing that key, so a fast-moving reactive producer
(e.g. ``listen`` re-targeting as a sound moves) never builds a stale backlog — only the
latest intent for that key remains. One-shot gestures use ``coalesce_key=None`` and queue
strictly in order.

Pure data + a list; no I/O, no clock — so it is trivially unit-testable.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# A reactive producer re-targeting the head shares this key so only the latest look
# survives in the queue; one-shot gestures pass coalesce_key=None to queue in order.
LOOK_KEY = "look"
# A reactive producer re-targeting the antennas shares this key so only the latest antenna
# action survives in the queue, independently of LOOK_KEY.
ANTENNA_KEY = "antenna"
# The always-alive idle layer re-targets a gentle breathing/gaze pose under this key so only
# the latest idle pose survives; any real reaction (turn or lean) supersedes it, so live
# sound always wins over background idle motion.
IDLE_KEY = "idle"

# A committed head/body move (LOOK_KEY) supersedes any pending subtle antenna lean
# (ANTENNA_KEY) *and* any pending idle pose (IDLE_KEY) — a deliberate "turn to see" must
# never wait behind background motion. A Tier-1 lean (ANTENNA_KEY) likewise supersedes a
# pending idle pose so live sound preempts idle. The relations are one-way: a lean never
# evicts a queued turn, and idle never evicts a turn or a lean. (A turn already folds the
# antenna pose into its own action, so the antenna still moves with the head.)
_SUPERSEDES: dict[str, frozenset[str]] = {
    LOOK_KEY: frozenset({ANTENNA_KEY, IDLE_KEY}),
    ANTENNA_KEY: frozenset({IDLE_KEY}),
}


@dataclass(frozen=True)
class MotionAction:
    """One interpolated move: a target pose (friendly units) + how to get there.

    ``head`` is the six-axis offset dict (mm / degrees), ``antennas`` a ``(right, left)``
    degree pair, ``body_yaw`` a scalar in degrees — any left ``None`` is not driven. The
    executor hands these straight to ``transport.move_goto``. ``coalesce_key`` groups
    actions a newer submission may replace while still pending (``None`` = never replaced).
    ``label`` is for status/logs only.
    """

    label: str
    head: dict[str, float] | None = None
    antennas: tuple[float, float] | None = None
    body_yaw: float | None = None
    duration: float = 1.0
    interpolation: str = "minjerk"
    coalesce_key: str | None = None


@dataclass
class MotionQueue:
    """A FIFO of pending :class:`MotionAction`\\ s with coalescing on submit."""

    _pending: list[MotionAction] = field(default_factory=list)

    def submit(self, action: MotionAction) -> None:
        """Enqueue ``action``; if it coalesces, drop any pending action it replaces.

        A keyed action evicts pending actions sharing its key, plus any keys it
        *supersedes* (see :data:`_SUPERSEDES` — a turn evicts a pending lean). The
        currently-executing action is owned by the executor (not here), so it always
        finishes — coalescing only ever replaces moves that have not started yet.
        """
        key = action.coalesce_key
        if key is not None:
            evict = {key} | _SUPERSEDES.get(key, frozenset())
            self._pending = [a for a in self._pending if a.coalesce_key not in evict]
        self._pending.append(action)

    def peek(self) -> MotionAction | None:
        """Return the next pending action without removing it (``None`` if empty).

        The executor peeks, issues the move, and only :meth:`pop`\\ s once the daemon
        accepts it — so a move that fails to send is retried, never silently dropped.
        """
        return self._pending[0] if self._pending else None

    def pop(self) -> MotionAction | None:
        """Remove and return the next pending action, or ``None`` if the queue is empty."""
        return self._pending.pop(0) if self._pending else None

    def pending(self) -> list[MotionAction]:
        """A snapshot of the pending actions, oldest-first (for status)."""
        return list(self._pending)

    def __len__(self) -> int:
        return len(self._pending)
