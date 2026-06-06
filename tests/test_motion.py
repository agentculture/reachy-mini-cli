"""Tests for the serial motion subsystem (queue + executor + listen producer).

Pure / injectable: the queue is plain data, the executor takes an injected clock, sleep,
and a fake transport, and the listen producer is a pure decision function fed synthetic
``Sense`` values — so no robot, daemon, or wall-clock is involved.
"""

from __future__ import annotations

import math

from reachy.behavior.sense import Sense
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.queue import LOOK_KEY, MotionAction, MotionQueue
from reachy.motion.server import run


def _look(label: str, yaw: float) -> MotionAction:
    return MotionAction(label=label, head={"yaw": yaw}, duration=1.0, coalesce_key=LOOK_KEY)


# --------------------------------------------------------------------------- #
# queue                                                                       #
# --------------------------------------------------------------------------- #


def test_queue_fifo_for_noncoalescing() -> None:
    q = MotionQueue()
    q.submit(MotionAction(label="nod"))
    q.submit(MotionAction(label="wake"))
    assert [a.label for a in q.pending()] == ["nod", "wake"]
    assert q.pop().label == "nod"
    assert q.pop().label == "wake"
    assert q.pop() is None


def test_queue_coalesces_pending_same_key() -> None:
    q = MotionQueue()
    q.submit(_look("look-left", 20))
    q.submit(_look("look-right", -20))  # replaces the pending look
    assert len(q) == 1
    only = q.pop()
    assert only.label == "look-right" and only.head["yaw"] == -20


def test_queue_coalescing_keeps_other_kinds() -> None:
    q = MotionQueue()
    q.submit(MotionAction(label="nod"))  # coalesce_key None -> never replaced
    q.submit(_look("look-1", 10))
    q.submit(_look("look-2", 30))  # replaces look-1 only
    assert [a.label for a in q.pending()] == ["nod", "look-2"]


def test_queue_recoalesces_after_pop() -> None:
    # a look that already started (popped) does not block a fresh look from queuing
    q = MotionQueue()
    q.submit(_look("look-1", 10))
    started = q.pop()  # executor takes it; no longer pending
    q.submit(_look("look-2", 30))
    assert started.label == "look-1"
    assert [a.label for a in q.pending()] == ["look-2"]


# --------------------------------------------------------------------------- #
# listen producer                                                             #
# --------------------------------------------------------------------------- #


def test_producer_commits_only_after_dwell() -> None:
    prod = ListenProducer(ListenParams(deadband=10, dwell=0.5, gain=0.6, max_yaw=35))
    left = Sense(doa_angle=0.0)
    assert prod.update(0.0, left) is None  # candidate noted
    assert prod.update(0.3, left) is None  # still under dwell
    a = prod.update(0.6, left)  # dwell elapsed -> turn
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY


def test_producer_holds_within_deadband() -> None:
    prod = ListenProducer(ListenParams(deadband=20, dwell=0.0, gain=0.6, max_yaw=35))
    assert prod.update(0.0, Sense(doa_angle=math.pi / 2)) is None  # front -> ~0, held
    # a sound mapping to ~10deg head-yaw is within the 20deg deadband -> no turn
    assert prod.update(0.1, Sense(doa_angle=1.28)) is None


def test_producer_relax_is_gentler_than_alert() -> None:
    p = ListenParams(alert_speed=30, relax_speed=10, min_dur=0.5, max_dur=5.0)
    prod = ListenProducer(p)
    alert = prod._move_to(30.0, 0.0)  # turn out to +30 (away from center)
    relax = prod._move_to(0.0, 1.0)  # ease back to 0 (toward center)
    assert relax.duration > alert.duration  # easing back is slower than turning toward


def test_producer_recenters_after_silence() -> None:
    prod = ListenProducer(
        ListenParams(
            deadband=10,
            dwell=0.0,
            hold=0.0,
            recenter_after=1.0,
            gain=0.6,
            min_dur=0.0,
            alert_speed=1000.0,
        )  # near-instant move so hold clears
    )
    prod.update(0.0, Sense(doa_angle=0.0))
    prod.update(0.02, Sense(doa_angle=0.0))  # commit off-center
    assert prod.committed != 0.0
    from reachy.behavior.sense import EMPTY_SENSE

    assert prod.update(0.5, EMPTY_SENSE) is None  # within grace, holds
    back = prod.update(1.1, EMPTY_SENSE)  # silence past recenter_after -> ease to center
    assert back is not None and back.head["yaw"] == 0.0


def test_producer_holds_at_target_after_turn() -> None:
    # turn readily (dwell 0), but stay committed for `hold` seconds before reconsidering
    p = ListenParams(
        deadband=10, dwell=0.0, hold=3.0, gain=0.6, max_yaw=35, alert_speed=30, min_dur=0.5
    )
    prod = ListenProducer(p)
    prod.update(0.0, Sense(doa_angle=0.0))
    assert prod.update(0.1, Sense(doa_angle=0.0)) is not None  # commit left
    # a strong opposite sound during the hold window is ignored
    prod.update(0.2, Sense(doa_angle=math.pi))
    assert prod.update(2.0, Sense(doa_angle=math.pi)) is None  # still holding left
    # once the hold elapses it may turn again
    prod.update(5.0, Sense(doa_angle=math.pi))
    b = prod.update(5.2, Sense(doa_angle=math.pi))
    assert b is not None and b.head["yaw"] < 0  # now turns to the right


# --------------------------------------------------------------------------- #
# executor (serial, no overlap)                                               #
# --------------------------------------------------------------------------- #


class _Clock:
    def __init__(self, dt=0.05):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


class _RecTransport:
    name = "rec"

    def __init__(self):
        self.gotos: list[float] = []

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(duration)
        return {"uuid": "x"}


class _AlwaysLook:
    """A producer that wants to look somewhere every single tick."""

    def update(self, t, sense):
        return MotionAction(label="look", head={"yaw": 20.0}, duration=1.0, coalesce_key=LOOK_KEY)


def test_server_runs_moves_serially_without_overlap() -> None:
    tr = _RecTransport()
    # 60 ticks * 0.05s = 3.0s; each move is 1.0s + 0.2s settle (~1.2s apart). Despite the
    # producer wanting to move every tick, serialization yields only a couple of moves.
    run(
        tr,
        _AlwaysLook(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=60,
    )
    assert 2 <= len(tr.gotos) <= 4  # NOT ~60 — no overlap, one move at a time


def test_queue_peek_does_not_remove() -> None:
    q = MotionQueue()
    q.submit(MotionAction(label="nod"))
    assert q.peek().label == "nod"
    assert len(q) == 1  # still pending — peek doesn't consume
    assert q.pop().label == "nod" and len(q) == 0
    assert q.peek() is None  # empty


class _OnceMove:
    """A producer that emits exactly one (non-coalescing) move, then nothing."""

    def __init__(self):
        self.done = False

    def update(self, t, sense):
        if self.done:
            return None
        self.done = True
        return MotionAction(label="once", head={"yaw": 10.0}, duration=1.0)


class _FlakyTransport:
    name = "flaky"

    def __init__(self, fail_times: int):
        self.gotos: list[float] = []
        self._fail = fail_times

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        if self._fail > 0:
            self._fail -= 1
            raise CliError(code=EXIT_ENV_ERROR, message="daemon hiccup", remediation="retry")
        self.gotos.append(duration)
        return {"uuid": "x"}


def test_server_retries_a_failed_move_instead_of_dropping_it() -> None:
    # The single queued move fails to send on its first attempt; the executor must
    # keep it pending and land it on a later tick, not pop-and-lose it.
    tr = _FlakyTransport(fail_times=1)
    run(
        tr,
        _OnceMove(),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        settle=0.2,
        max_ticks=5,
    )
    assert tr.gotos == [1.0]  # the move eventually landed (was not dropped on the failure)
