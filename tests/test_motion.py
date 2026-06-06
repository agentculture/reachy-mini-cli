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


def test_producer_commits_on_speech_off_axis() -> None:
    # Speech off-axis commits exactly one head turn, then holds (no second commit).
    prod = ListenProducer(ListenParams(deadband=10, hold=3.0, gain=0.6, max_yaw=35))
    spoke = Sense(doa_angle=0.0, speech_detected=True)  # doa=0 → desired +35°, off-axis
    a = prod.update(0.0, spoke, sound_present=True)  # speech off-axis -> head turn
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY
    # During the hold window a second speech event does not re-commit.
    assert prod.update(0.5, spoke, sound_present=True) is None
    assert prod.update(1.0, spoke, sound_present=True) is None


def test_producer_commits_on_snap_off_axis() -> None:
    # A loud snap off-axis commits exactly one head turn, even with no speech.
    prod = ListenProducer(ListenParams(deadband=10, hold=3.0, gain=0.6, max_yaw=35))
    s = Sense(doa_angle=0.0, speech_detected=False)
    a = prod.update(0.0, s, snap=True, sound_present=True)  # snap off-axis -> head turn
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY
    # Hold window suppresses a second commit even on another snap.
    assert prod.update(0.5, s, snap=True, sound_present=True) is None


def test_latched_angle_never_turns_head() -> None:
    # A constant/latched angle with no speech, no snap, no live sound must NOT turn the
    # head at all (the latched-DoA guard) — and it must recenter after silence.
    prod = ListenProducer(
        ListenParams(deadband=10, hold=0.0, recenter_after=1.0, gain=0.6, max_yaw=35)
    )
    latched = Sense(doa_angle=0.0, speech_detected=False)  # off-axis but frozen/silent
    turns = 0
    for i in range(30):  # 30 ticks of a bare latched angle, no liveness
        a = prod.update(i * 0.1, latched, snap=False, sound_present=False)
        if a is not None and a.head is not None:
            # the only head action permitted is the eventual recenter to 0°
            assert a.head["yaw"] == 0.0
        elif a is not None and a.head is None:
            turns += 1  # would be an antenna lean — also not allowed on silence
    assert turns == 0, "no antenna lean and no off-axis head turn on a silent latched angle"
    assert prod.committed == 0.0, "head recentered after recenter_after of silence"


def test_producer_no_head_turn_within_deadband() -> None:
    # Speech within the deadband leans (Tier-1) but does not turn the head.
    prod = ListenProducer(ListenParams(deadband=20, gain=0.6, max_yaw=35))
    # Front sound (doa=pi/2) maps to desired≈0° — lean magnitude is 0, so None still.
    assert prod.update(0.0, Sense(doa_angle=math.pi / 2), sound_present=True) is None
    # doa=1.28 maps to ~10° head yaw, within the 20° deadband — no head turn even on speech.
    a = prod.update(0.1, Sense(doa_angle=1.28, speech_detected=True), sound_present=True)
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
            hold=0.0,
            recenter_after=1.0,
            gain=0.6,
            min_dur=0.0,
            alert_speed=1000.0,
            body_speed=1000.0,  # near-instant escalation so hold clears immediately
        )  # near-instant move so hold clears
    )
    # Speech off-axis commits the turn (latched angle alone never would).
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed != 0.0
    from reachy.behavior.sense import EMPTY_SENSE

    # Silence clock is keyed on liveness, not on the (still-latched) angle.
    assert prod.update(0.5, EMPTY_SENSE, sound_present=False) is None  # within grace, holds
    back = prod.update(1.1, EMPTY_SENSE, sound_present=False)  # past recenter_after -> center
    assert back is not None and back.head["yaw"] == 0.0


def test_producer_holds_at_target_after_turn() -> None:
    # turn readily on speech, but stay committed for `hold` seconds before reconsidering
    p = ListenParams(
        deadband=10,
        hold=3.0,
        gain=0.6,
        max_yaw=35,
        alert_speed=30,
        min_dur=0.5,
        body_speed=1000.0,  # near-instant escalation so hold duration is driven by alert_speed
    )
    prod = ListenProducer(p)
    left = Sense(doa_angle=0.0, speech_detected=True)
    right = Sense(doa_angle=math.pi, speech_detected=True)
    assert prod.update(0.1, left, sound_present=True) is not None  # commit left on speech
    # a strong opposite sound during the hold window is ignored
    assert prod.update(2.0, right, sound_present=True) is None  # still holding left
    # once the hold elapses a fresh speech event may turn again
    b = prod.update(5.2, right, sound_present=True)
    assert b is not None and b.head["yaw"] < 0  # now turns to the right


# --------------------------------------------------------------------------- #
# Tier-1 antenna lean                                                         #
# --------------------------------------------------------------------------- #


def test_tier1_antenna_lean_left() -> None:
    """Live sound on the left (within deadband) → near-side (left) antenna leans; head unmoved."""
    # Large deadband so the sound never triggers a head turn even if it were speech.
    p = ListenParams(deadband=30, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa≈1.0 rad → desired ≈ degrees(pi/2-1.0)*0.6 ≈ 17.2*0.6 ≈ 10.3° — within 30° deadband.
    a = prod.update(0.0, Sense(doa_angle=1.0), sound_present=True)
    assert a is not None, "expected antenna lean, got None"
    assert a.head is None, "Tier-1 must not drive the head"
    assert a.coalesce_key == ANTENNA_KEY
    assert a.antennas is not None
    right_a, left_a = a.antennas
    assert left_a > 0, "near-side (left) antenna must deflect toward the sound"
    assert right_a == 0.0, "far-side (right) antenna must stay neutral"
    assert left_a > right_a, "near magnitude must exceed far magnitude"


def test_tier1_antenna_lean_right() -> None:
    """Live sound on the right (within deadband) → near-side (right) antenna leans; head unmoved."""
    p = ListenParams(deadband=30, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa≈2.14 rad → desired ≈ degrees(pi/2-2.14)*0.6 ≈ -37.7*0.6 ≈ -10.3° (right side),
    # within 30° deadband, so no head turn.
    a = prod.update(0.0, Sense(doa_angle=2.14), sound_present=True)
    assert a is not None, "expected antenna lean, got None"
    assert a.head is None, "Tier-1 must not drive the head"
    assert a.coalesce_key == ANTENNA_KEY
    assert a.antennas is not None
    right_a, left_a = a.antennas
    assert right_a > 0, "near-side (right) antenna must deflect toward the sound"
    assert left_a == 0.0, "far-side (left) antenna must stay neutral"
    assert right_a > left_a, "near magnitude must exceed far magnitude"


def test_tier1_lean_on_sound_present_without_speech_or_snap() -> None:
    """Live sound (sound_present) off-axis, but no speech/snap → antenna lean only, no head turn."""
    p = ListenParams(deadband=10, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # doa=0.0 → desired=35° (clamped), well outside 10° deadband — but with no speech and
    # no snap the head must NOT turn; only the near-side antenna leans.
    for ti in (0.0, 0.5, 1.5):
        a = prod.update(ti, Sense(doa_angle=0.0, speech_detected=False), sound_present=True)
        assert a is not None and a.head is None, "live sound w/o speech/snap → lean only"
        assert a.coalesce_key == ANTENNA_KEY
        assert a.antennas is not None
        right_a, left_a = a.antennas
        assert left_a > 0 and right_a == 0.0  # left near-side for positive desired yaw
    assert prod.committed == 0.0, "no head turn was committed"


def test_tier1_no_lean_without_live_sound_then_recenters() -> None:
    """No live sound → no antenna lean (even on a latched angle); head eventually recenters."""
    p = ListenParams(
        deadband=10,
        hold=0.0,
        recenter_after=1.0,
        gain=0.6,
        max_yaw=35,
        min_dur=0.0,
        alert_speed=1000.0,  # near-instant move so the hold window clears immediately
        body_speed=1000.0,  # near-instant escalation so hold clears immediately
    )
    prod = ListenProducer(p)
    # First commit a turn via speech so there is something to recenter from.
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed != 0.0
    # Now the angle latches but sound goes silent — no lean while waiting to recenter.
    latched = Sense(doa_angle=0.0, speech_detected=False)
    assert prod.update(0.5, latched, sound_present=False) is None  # within grace, no lean
    back = prod.update(1.6, latched, sound_present=False)  # past recenter_after -> center once
    assert back is not None and back.head["yaw"] == 0.0


def test_remote_profile_falls_back_to_latched_angle_for_liveness() -> None:
    """sound_present=None (HTTP/remote) → ``live`` falls back to ``doa_angle is not None``."""
    p = ListenParams(deadband=10, gain=0.6, max_yaw=35, antenna_max=18.0)
    prod = ListenProducer(p)
    # No audio path: a present angle is the best-effort liveness signal, so Tier-1 leans.
    a = prod.update(0.0, Sense(doa_angle=0.0), sound_present=None)
    assert a is not None and a.head is None and a.coalesce_key == ANTENNA_KEY
    # But still no head turn without speech/snap.
    assert prod.committed == 0.0


# --------------------------------------------------------------------------- #
# t7: antenna fold + head→body escalation                                    #
# --------------------------------------------------------------------------- #


def test_near_off_axis_speech_head_only_antennas_folded() -> None:
    """Near off-axis (within head_only_band) → head-only turn; antenna folded into the action."""
    # head_only_band=60 ensures raw_desired stays below band for a moderate doa angle.
    p = ListenParams(
        deadband=10,
        gain=0.6,
        max_yaw=35,
        antenna_max=18.0,
        head_only_band=60.0,  # wide band → head-only path
    )
    prod = ListenProducer(p)
    # doa=1.0 → raw ~17.2°, within head_only_band=60 → head-only turn.
    s = Sense(doa_angle=1.0, speech_detected=True)
    a = prod.update(0.0, s, sound_present=True)
    assert a is not None, "expected a head turn"
    assert a.coalesce_key == LOOK_KEY
    assert a.head is not None and a.head["yaw"] > 0, "head should turn toward the sound"
    # body_yaw should be absent (None) — no body movement for head-only path.
    assert a.body_yaw is None, "head-only turn must not move the body"
    # Antenna should be folded into this same action (near-side non-zero).
    assert a.antennas is not None, "antenna pose must be folded into the committing turn"
    right_a, left_a = a.antennas
    # Sound on the left (positive yaw) → left antenna near-side.
    assert left_a > 0, "near-side (left) antenna must deflect toward the sound"
    assert right_a == 0.0, "far-side (right) antenna must stay neutral"
    # Body yaw state is unchanged.
    assert prod.body == 0.0


def test_far_off_axis_speech_body_escalation() -> None:
    """Far off-axis (beyond head_only_band) → combined body+head action with antennas folded."""
    # Use narrow head_only_band so doa=0.0 (raw=54° at gain=0.6) triggers escalation.
    p = ListenParams(
        deadband=10,
        gain=0.6,
        max_yaw=35,
        antenna_max=18.0,
        head_only_band=30.0,  # raw=54 > 30 → escalate
        body_yaw_max=45.0,
        body_speed=1000.0,  # fast so test is not about timing
        min_dur=0.0,
    )
    prod = ListenProducer(p)
    s = Sense(doa_angle=0.0, speech_detected=True)
    a = prod.update(0.0, s, snap=False, sound_present=True)
    assert a is not None, "expected an escalation action"
    assert a.coalesce_key == LOOK_KEY
    # body_yaw must be non-zero toward the source (positive for left-side source).
    assert a.body_yaw is not None and a.body_yaw > 0, "body must rotate toward the source"
    # head yaw must be less extreme than the raw desired angle (54°), re-centered.
    assert a.head is not None
    raw_desired = 54.0  # doa_angle_to_yaw(0.0, 0.6)
    assert abs(a.head["yaw"]) < abs(raw_desired), "head should be more centred than raw desired"
    # Antennas must be folded in.
    assert a.antennas is not None, "antenna must be folded into escalation action"
    # body and committed state updated.
    assert prod.body > 0
    assert prod.committed == a.head["yaw"]


def test_hold_window_returns_none_no_stale_antenna() -> None:
    """During the post-turn hold window, update() returns None (no stray antenna action)."""
    p = ListenParams(
        deadband=10,
        hold=3.0,
        gain=0.6,
        max_yaw=35,
        head_only_band=60.0,  # head-only path
    )
    prod = ListenProducer(p)
    s = Sense(doa_angle=1.0, speech_detected=True)
    a = prod.update(0.0, s, sound_present=True)
    assert a is not None  # committed the turn
    # During hold, even with live sound, no further action is returned.
    for ti in (0.5, 1.0, 1.5, 2.0, 2.5):
        result = prod.update(ti, s, sound_present=True)
        assert result is None, f"expected None during hold at t={ti}, got {result}"


def test_recenter_returns_head_and_body_to_center() -> None:
    """After silence, both head and body return to center (body_yaw=0 in recenter action)."""
    p = ListenParams(
        deadband=10,
        hold=0.0,
        recenter_after=1.0,
        gain=0.6,
        max_yaw=35,
        min_dur=0.0,
        head_only_band=30.0,  # escalation path
        body_yaw_max=45.0,
        body_speed=1000.0,  # near-instant
        alert_speed=1000.0,
    )
    prod = ListenProducer(p)
    # Speech off-axis → escalate so both head and body are off-center.
    s = Sense(doa_angle=0.0, speech_detected=True)
    prod.update(0.0, s, sound_present=True)
    assert prod.body != 0.0, "body should be non-zero after escalation"
    from reachy.behavior.sense import EMPTY_SENSE

    # After silence, recenter action should bring both head and body to 0.
    back = prod.update(1.1, EMPTY_SENSE, sound_present=False)
    assert back is not None and back.head["yaw"] == 0.0, "head must recenter"
    assert back.body_yaw == 0.0, "body must also be returned to center in the recenter action"


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
