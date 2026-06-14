"""The motion executor: one loop that drains a producer into the queue and runs it.

Each tick it asks the producer for an action (submitting any to the queue, with
coalescing), then — only when not already mid-move — issues the next action as a single
interpolated ``goto``, marking itself busy until that move finishes. The action is removed
from the queue only once the daemon accepts the move, so a transient send failure leaves it
pending to retry rather than dropping it. Because a new move is never started while one is
running, interpolated moves can never overlap or reset each other; a faster-moving producer
just coalesces its pending action so the next move goes to the latest intent. Injectable
``now`` / ``sleep`` / ``sense`` and ``max_ticks``
make it deterministic in tests; the real run installs SIGTERM/SIGINT handlers and tolerates
transient transport errors like the behavior engine.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable

from reachy.behavior.sense import EMPTY_SENSE
from reachy.cli._errors import CliError
from reachy.looputil import install_stop_handlers, interruptible_sleep, restore_stop_handlers
from reachy.motion.queue import MotionQueue

# Extra hold after a move completes before the next may start — a beat between gestures.
SETTLE = 0.2
DEFAULT_TICK = 0.05  # 20 Hz producer/poll cadence (the DoA itself updates slowly)


@dataclass(frozen=True)
class LoopHooks:
    """The four optional per-tick callbacks the loop fans out to, bundled.

    Bundling them keeps :func:`run` / :func:`_drive` under SonarCloud's 13-param
    ceiling (S107) without flattening the per-tick contract. Every field defaults
    to ``None`` so ``LoopHooks()`` is a no-op bundle every existing caller can
    pass (or omit — both ``run`` and ``_drive`` default to it).

    Attributes
    ----------
    sense:
        Optional ``(t) -> Sense`` source (e.g. a ``DoaPoller``); when ``None`` the
        loop feeds the producer :data:`~reachy.behavior.sense.EMPTY_SENSE`.
    audio:
        Optional ``(t) -> (snap: bool, sound_present: bool | None)`` source; when
        provided, its values are forwarded to the producer each tick.
    on_action:
        Optional ``(action) -> None`` callback fired when a queued move is accepted
        by the transport (after it is popped).
    on_tick:
        Optional ``(transport, queue, t, commanded_head) -> None`` hook invoked
        once per tick *before* the producer is consulted. ``commanded_head`` is a
        ``{"pitch": float, "yaw": float}`` snapshot of the last head pose the loop
        dispatched (neutral ``{"pitch": 0.0, "yaw": 0.0}`` before the first move) —
        so a second, queue-driven behaviour folded into the loop (e.g. ``listen``'s
        head-pat detector) can compare the read-back against what the loop actually
        commanded, not a fixed baseline.
    """

    sense: Callable | None = None
    audio: Callable | None = None
    on_action: Callable | None = None
    on_tick: Callable | None = None


def _dispatch_next(transport, q: MotionQueue, t: float, settle: float, st: "_DriveState") -> float:
    """Issue the next queued move; record its head pose; return the new ``busy_until``.

    Peeks the queue and only removes the action via :meth:`~MotionQueue.pop_if`
    after ``move_goto`` is accepted, so a move that fails to send is left pending
    and retried next tick rather than silently dropped (a :class:`CliError`
    propagates to the caller, which counts it toward the error ceiling).
    ``pop_if`` (not a bare ``pop``) removes the action only if it is still the
    head — so a gesture a concurrent producer thread coalesced in mid-dispatch is
    never popped in its place (see :meth:`MotionQueue.pop_if`).

    On acceptance the dispatched action's head pitch/yaw are stamped onto
    ``st.commanded_head`` (axes the action omits default to ``0.0``) so the
    ``on_tick`` hook can be handed the pose the loop actually commanded.
    """
    nxt = q.peek()
    if nxt is None:  # emptied by another thread between the len() check and here
        return t
    transport.move_goto(
        head=nxt.head,
        antennas=nxt.antennas,
        body_yaw=nxt.body_yaw,
        duration=nxt.duration,
        interpolation=nxt.interpolation,
    )
    q.pop_if(nxt)  # accepted — remove it, unless a newer gesture took the head
    head = nxt.head or {}
    st.commanded_head = {"pitch": float(head.get("pitch", 0.0)), "yaw": float(head.get("yaw", 0.0))}
    if st.on_action is not None:
        st.on_action(nxt)
    return t + nxt.duration + settle


@dataclass
class _DriveState:
    """Mutable per-run bookkeeping for the loop (kept out of :func:`_drive`'s body)."""

    on_action: Callable | None = None
    busy_until: float = 0.0
    consecutive: int = 0
    ticks: int = 0
    #: Last head pose the loop dispatched; neutral until the first accepted move.
    commanded_head: dict[str, float] = field(default_factory=lambda: {"pitch": 0.0, "yaw": 0.0})


def _service_queue(transport, q, t, st: _DriveState, *, settle, max_errors) -> None:
    """If idle and something is queued, run the next move; count/raise on errors."""
    if t < st.busy_until or not len(q):
        return
    try:
        st.busy_until = _dispatch_next(transport, q, t, settle, st)
        st.consecutive = 0
    except CliError:
        st.consecutive += 1
        if st.consecutive >= max_errors:
            raise


def _drive(
    transport,
    producer,
    q,
    *,
    hooks: LoopHooks = LoopHooks(),
    now,
    sleep,
    tick,
    settle,
    max_ticks,
    max_errors,
    stop,
) -> int:
    """The serial body: drain the producer into the queue, run one move at a time."""
    st = _DriveState(on_action=hooks.on_action)
    while not stop["flag"]:
        t = now()
        if hooks.on_tick is not None:
            hooks.on_tick(transport, q, t, st.commanded_head)
        snap, sp = hooks.audio(t) if hooks.audio is not None else (False, None)
        sense_val = hooks.sense(t) if hooks.sense is not None else EMPTY_SENSE
        action = producer.update(t, sense_val, snap=snap, sound_present=sp)
        if action is not None:
            q.submit(action)
        _service_queue(transport, q, t, st, settle=settle, max_errors=max_errors)
        st.ticks += 1
        if max_ticks is not None and st.ticks >= max_ticks:
            break
        interruptible_sleep(tick, stop, sleep, tick)
    return st.ticks


def run(
    transport,
    producer,
    *,
    hooks: LoopHooks = LoopHooks(),
    queue: MotionQueue | None = None,
    now=time.monotonic,
    sleep=time.sleep,
    tick: float = DEFAULT_TICK,
    settle: float = SETTLE,
    max_ticks: int | None = None,
    max_errors: int = 5,
    stop: dict | None = None,
) -> int:
    """Drive the robot from ``producer`` actions until stopped. Returns ticks run.

    ``producer.update(t, sense, snap=..., sound_present=...) -> MotionAction | None`` decides
    what to do each tick. The four optional per-tick callbacks are bundled in
    ``hooks`` (a :class:`LoopHooks`; default ``LoopHooks()`` is a no-op so every
    existing caller is unaffected):

    * ``hooks.sense`` — an optional ``(t) -> Sense`` source (e.g. a ``DoaPoller``);
    * ``hooks.audio`` — an optional ``(t) -> (snap: bool, sound_present: bool | None)``
      source — when provided, its values are forwarded to the producer each tick so
      it can use real mic loudness rather than the degraded ``sound_present=None`` fallback;
    * ``hooks.on_action`` — fired with each accepted move;
    * ``hooks.on_tick`` — an optional ``(transport, queue, t, commanded_head) -> None``
      hook invoked once per tick *before* the producer is consulted, with the live
      transport, the working :class:`~reachy.motion.queue.MotionQueue`, the current
      time, and the ``{"pitch": float, "yaw": float}`` head pose the loop last
      dispatched. It lets a caller fold a second, queue-driven behaviour into the
      same loop that owns the single SDK client — e.g. ``listen`` reads the head
      pose back through it and compares it to ``commanded_head`` to detect a head
      pat (deviation = actual − commanded), so ``listen``'s own commanded turns
      never read as a press.

    Moves are run one at a time via ``transport.move_goto`` — never overlapping.
    """
    q = queue if queue is not None else MotionQueue()
    own_stop = stop is None
    stop = stop if stop is not None else {"flag": False}
    handlers = install_stop_handlers(stop) if own_stop else None
    try:
        return _drive(
            transport,
            producer,
            q,
            hooks=hooks,
            now=now,
            sleep=sleep,
            tick=tick,
            settle=settle,
            max_ticks=max_ticks,
            max_errors=max_errors,
            stop=stop,
        )
    finally:
        if handlers is not None:
            restore_stop_handlers(handlers)
