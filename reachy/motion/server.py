"""The motion executor: one loop that drains a producer into the queue and runs it.

Each tick it asks the producer for an action (submitting any to the queue, with
coalescing), then — only when not already mid-move — pops the next action and issues it as
a single interpolated ``goto``, marking itself busy until that move finishes. Because a new
move is never started while one is running, interpolated moves can never overlap or reset
each other; a faster-moving producer just coalesces its pending action so the next move
goes to the latest intent. Injectable ``now`` / ``sleep`` / ``sense`` and ``max_ticks``
make it deterministic in tests; the real run installs SIGTERM/SIGINT handlers and tolerates
transient transport errors like the behavior engine.
"""

from __future__ import annotations

import time
from typing import Callable

from reachy.behavior.sense import EMPTY_SENSE
from reachy.cli._errors import CliError
from reachy.looputil import install_stop_handlers, interruptible_sleep, restore_stop_handlers
from reachy.motion.queue import MotionQueue

# Extra hold after a move completes before the next may start — a beat between gestures.
SETTLE = 0.2
DEFAULT_TICK = 0.05  # 20 Hz producer/poll cadence (the DoA itself updates slowly)


def run(
    transport,
    producer,
    *,
    sense: Callable | None = None,
    queue: MotionQueue | None = None,
    now=time.monotonic,
    sleep=time.sleep,
    tick: float = DEFAULT_TICK,
    settle: float = SETTLE,
    max_ticks: int | None = None,
    max_errors: int = 5,
    on_action: Callable | None = None,
    stop: dict | None = None,
) -> int:
    """Drive the robot from ``producer`` actions until stopped. Returns ticks run.

    ``producer.update(t, sense) -> MotionAction | None`` decides what to do each tick;
    ``sense`` is an optional ``(t) -> Sense`` source (e.g. a ``DoaPoller``). Moves are run
    one at a time via ``transport.move_goto`` — never overlapping.
    """
    q = queue if queue is not None else MotionQueue()
    own_stop = stop is None
    stop = stop if stop is not None else {"flag": False}
    handlers = install_stop_handlers(stop) if own_stop else None
    busy_until = 0.0
    ticks = 0
    consecutive = 0
    try:
        while not stop["flag"]:
            t = now()
            s = sense(t) if sense is not None else EMPTY_SENSE
            action = producer.update(t, s)
            if action is not None:
                q.submit(action)
            if t >= busy_until and len(q):
                nxt = q.pop()
                try:
                    transport.move_goto(
                        head=nxt.head,
                        antennas=nxt.antennas,
                        body_yaw=nxt.body_yaw,
                        duration=nxt.duration,
                        interpolation=nxt.interpolation,
                    )
                    consecutive = 0
                    busy_until = t + nxt.duration + settle
                    if on_action is not None:
                        on_action(nxt)
                except CliError:
                    consecutive += 1
                    if consecutive >= max_errors:
                        raise
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            interruptible_sleep(tick, stop, sleep, tick)
    finally:
        if handlers is not None:
            restore_stop_handlers(handlers)
    return ticks
