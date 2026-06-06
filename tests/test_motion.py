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
from reachy.motion.queue import ANTENNA_KEY, LOOK_KEY, MotionAction, MotionQueue
from reachy.motion.server import run


def _look(label: str, yaw: float) -> MotionAction:
    return MotionAction(label=label, head={"yaw": yaw}, duration=1.0, coalesce_key=LOOK_KEY)


def _antenna(label: str, right: float, left: float) -> MotionAction:
    return MotionAction(label=label, antennas=(right, left), duration=1.0, coalesce_key=ANTENNA_KEY)


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


def test_antenna_key_coalesces_independently() -> None:
    # antenna actions coalesce with each other
    q = MotionQueue()
    q.submit(_antenna("antenna-up", 10, 10))
    q.submit(_antenna("antenna-down", 0, 0))  # replaces the pending antenna
    assert len(q) == 1
    only = q.pop()
    assert only.label == "antenna-down"


def test_antenna_and_look_do_not_evict_each_other() -> None:
    # antenna and look are independent coalesce keys — a look doesn't evict an antenna
    # and vice-versa
    q = MotionQueue()
    q.submit(_look("look-left", 20))
    q.submit(_antenna("antenna-up", 10, 10))
    q.submit(_look("look-right", -20))  # replaces the pending look only
    pending_labels = [a.label for a in q.pending()]
    assert pending_labels == ["antenna-up", "look-right"]


# --------------------------------------------------------------------------- #
# listen producer                                                             #
# --------------------------------------------------------------------------- #


def test_producer_commits_only_after_dwell() -> None:
    prod = ListenProducer(ListenParams(deadband=10, dwell=0.5, gain=0.6, max_yaw=35))
    left = Sense(doa_angle=0.0)
    # Tier-1: while dwell is accumulating the producer now emits near-side antenna leans
    # instead of None — the head is not driven, only the left (near-side) antenna deflects.
    a0 = prod.update(0.0, left)  # candidate noted + first antenna lean
    assert a0 is not None and a0.head is None and a0.coalesce_key == ANTENNA_KEY
    assert a0.antennas is not None and a0.antennas[1] > 0 and a0.antennas[0] == 0.0  # left only
    a1 = prod.update(0.3, left)  # still under dwell — another antenna lean, no head turn
    assert a1 is not None and a1.head is None and a1.coalesce_key == ANTENNA_KEY
    a = prod.update(0.6, left)  # dwell elapsed -> head turn committed
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY


def test_producer_holds_within_deadband() -> None:
    prod = ListenProducer(ListenParams(deadband=20, dwell=0.0, gain=0.6, max_yaw=35))
    # Front sound (doa=pi/2) maps to desired≈0° — lean magnitude is 0, so None still.
    assert prod.update(0.0, Sense(doa_angle=math.pi / 2)) is None  # front -> ~0, no lean
    # doa=1.28 maps to ~10° head yaw, within the 20° deadband — no head turn.
    # Tier-1: a non-zero desired yaw now produces a near-side antenna lean instead of None.
    a = prod.update(0.1, Sense(doa_angle=1.28))
    assert a is not None and a.head is None and a.coalesce_key == ANTENNA_KEY
    assert a.antennas is not None and a.antennas[1] > 0 and a.antennas[0] == 0.0  # left near


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
# Tier-1 antenna lean                                                         #
# --------------------------------------------------------------------------- #


def test_tier1_antenna_lean_left() -> None:
    """Sound on the left (within deadband) → near-side (left) antenna leans; head is not driven."""
    # Large deadband so the sound never triggers a head turn; dwell>0 for extra safety.
    p = ListenParams(deadband=30, dwell=2.0, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa=0.0 → desired ≈ +35° (left), within deadband=30 is False here — so this is
    # actually the "outside deadband, dwell not met" branch.  Use a softer angle instead.
    # doa≈1.0 rad → desired ≈ degrees(pi/2-1.0)*0.6 ≈ 17.2*0.6 ≈ 10.3° — within 30° deadband.
    a = prod.update(0.0, Sense(doa_angle=1.0))
    assert a is not None, "expected antenna lean, got None"
    assert a.head is None, "Tier-1 must not drive the head"
    assert a.coalesce_key == ANTENNA_KEY
    assert a.antennas is not None
    right_a, left_a = a.antennas
    assert left_a > 0, "near-side (left) antenna must deflect toward the sound"
    assert right_a == 0.0, "far-side (right) antenna must stay neutral"
    assert left_a > right_a, "near magnitude must exceed far magnitude"


def test_tier1_antenna_lean_right() -> None:
    """Sound on the right (within deadband) → near-side (right) antenna leans; head not driven."""
    p = ListenParams(deadband=30, dwell=2.0, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa≈2.14 rad → desired ≈ degrees(pi/2-2.14)*0.6 ≈ -37.7*0.6 ≈ -10.3° (right side),
    # within 30° deadband, so no head turn.
    a = prod.update(0.0, Sense(doa_angle=2.14))
    assert a is not None, "expected antenna lean, got None"
    assert a.head is None, "Tier-1 must not drive the head"
    assert a.coalesce_key == ANTENNA_KEY
    assert a.antennas is not None
    right_a, left_a = a.antennas
    assert right_a > 0, "near-side (right) antenna must deflect toward the sound"
    assert left_a == 0.0, "far-side (left) antenna must stay neutral"
    assert right_a > left_a, "near magnitude must exceed far magnitude"


def test_tier1_antenna_lean_during_dwell_accumulation() -> None:
    """Sound outside deadband but dwell not yet met → antenna lean each tick, no head turn."""
    p = ListenParams(deadband=10, dwell=1.0, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa=0.0 → desired=35° (clamped), outside 10° deadband — dwell clock starts.
    a0 = prod.update(0.0, Sense(doa_angle=0.0))
    a1 = prod.update(0.5, Sense(doa_angle=0.0))  # still under 1.0s dwell
    for tick_result in (a0, a1):
        assert tick_result is not None, "expected antenna lean during dwell wait"
        assert tick_result.head is None
        assert tick_result.coalesce_key == ANTENNA_KEY
        assert tick_result.antennas is not None
        right_a, left_a = tick_result.antennas
        assert left_a > 0 and right_a == 0.0  # left near-side for positive desired yaw
    # After dwell elapses, the head-turn action is returned (not an antenna lean).
    head_action = prod.update(1.1, Sense(doa_angle=0.0))
    assert head_action is not None and head_action.head is not None
    assert head_action.coalesce_key == LOOK_KEY


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
