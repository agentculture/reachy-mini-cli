"""Tests for the serial motion subsystem (queue + executor + listen producer).

Pure / injectable: the queue is plain data, the executor takes an injected clock, sleep,
and a fake transport, and the listen producer is a pure decision function fed synthetic
``Sense`` values — so no robot, daemon, or wall-clock is involved.
"""

from __future__ import annotations

import contextlib
import math

import numpy as np
import pytest

from reachy.behavior.sense import EMPTY_SENSE, Sense
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.queue import ANTENNA_KEY, IDLE_KEY, LOOK_KEY, MotionAction, MotionQueue
from reachy.motion.server import LoopHooks, run


@pytest.fixture(autouse=True)
def _isolate_state_dir(monkeypatch, tmp_path):
    """Pin the ``*_active`` flag state dir to a throwaway dir.

    ``ListenProducer.update`` consults ``cognition_signal`` / ``pat_signal`` /
    ``sleep_signal`` (``state_dir()/<x>_active.flag``) to decide whether to drop
    the idle wander into focused / yield mode. Without this, these pure producer
    tests read the *real* state dir — and on a box where the ``listen --live``
    service is running, those flags toggle under the test's feet and flip the idle
    assertions (``a is None``) intermittently under ``pytest -n auto``. A clean tmp
    dir has no flags, so the producer runs in its normal idle mode deterministically.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


def _look(label: str, yaw: float) -> MotionAction:
    return MotionAction(label=label, head={"yaw": yaw}, duration=1.0, coalesce_key=LOOK_KEY)


def _antenna(label: str, right: float, left: float) -> MotionAction:
    return MotionAction(label=label, antennas=(right, left), duration=1.0, coalesce_key=ANTENNA_KEY)


def _idle_action(label: str) -> MotionAction:
    return MotionAction(
        label=label, head={"yaw": 0.0}, antennas=(1.0, -1.0), duration=2.0, coalesce_key=IDLE_KEY
    )


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


def test_antenna_does_not_evict_a_pending_look() -> None:
    # A Tier-1 antenna lean must never drop a queued Tier-2 turn (one-way supersede).
    q = MotionQueue()
    q.submit(_look("look-left", 20))
    q.submit(_antenna("antenna-up", 10, 10))  # coexists — does not evict the look
    assert [a.label for a in q.pending()] == ["look-left", "antenna-up"]


def test_look_supersedes_a_pending_antenna_lean() -> None:
    # A committed head/body turn supersedes a pending subtle antenna lean so the
    # "turn to see" is never delayed behind one (Qodo PR #24, comment 4).
    q = MotionQueue()
    q.submit(_antenna("antenna-up", 10, 10))
    q.submit(_look("look-right", -20))  # evicts the pending antenna lean
    pending_labels = [a.label for a in q.pending()]
    assert pending_labels == ["look-right"]


def test_look_supersedes_a_pending_idle_pose() -> None:
    # A real "turn to see" must never wait behind a background idle pose.
    q = MotionQueue()
    q.submit(_idle_action("idle-1"))
    q.submit(_look("look-left", 20))  # evicts the pending idle pose
    assert [a.label for a in q.pending()] == ["look-left"]


def test_antenna_lean_supersedes_a_pending_idle_pose() -> None:
    # A live-sound Tier-1 lean preempts background idle motion (sound wins over idle).
    q = MotionQueue()
    q.submit(_idle_action("idle-1"))
    q.submit(_antenna("antenna-up", 10, 0))  # evicts the pending idle pose
    assert [a.label for a in q.pending()] == ["antenna-up"]


def test_idle_coalesces_with_itself_but_never_evicts_a_turn() -> None:
    # Idle replaces only a previous idle pose; it never evicts a queued turn or lean.
    q = MotionQueue()
    q.submit(_look("look-left", 20))
    q.submit(_idle_action("idle-1"))  # coexists — idle never evicts a turn
    q.submit(_idle_action("idle-2"))  # latest idle replaces the previous idle only
    assert [a.label for a in q.pending()] == ["look-left", "idle-2"]


# --------------------------------------------------------------------------- #
# listen producer                                                             #
# --------------------------------------------------------------------------- #


def test_producer_commits_on_speech_off_axis() -> None:
    # Speech off-axis commits exactly one head turn, then holds (no second commit).
    # idle_energy=0 isolates the reactive path (else idle motion fires during the hold).
    prod = ListenProducer(ListenParams(deadband=10, hold=3.0, gain=0.6, max_yaw=35, idle_energy=0))
    spoke = Sense(doa_angle=0.0, speech_detected=True)  # doa=0 → desired +35°, off-axis
    a = prod.update(0.0, spoke, sound_present=True)  # speech off-axis -> head turn
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY
    # During the hold window a second speech event does not re-commit.
    assert prod.update(0.5, spoke, sound_present=True) is None
    assert prod.update(1.0, spoke, sound_present=True) is None


def test_producer_commits_on_snap_off_axis() -> None:
    # A loud snap off-axis commits exactly one head turn, even with no speech.
    # idle_energy=0 isolates the reactive path (else idle motion fires during the hold).
    prod = ListenProducer(ListenParams(deadband=10, hold=3.0, gain=0.6, max_yaw=35, idle_energy=0))
    s = Sense(doa_angle=0.0, speech_detected=False)
    a = prod.update(0.0, s, snap=True, sound_present=True)  # snap off-axis -> head turn
    assert a is not None and a.head["yaw"] > 0 and a.coalesce_key == LOOK_KEY
    # Hold window suppresses a second commit even on another snap.
    assert prod.update(0.5, s, snap=True, sound_present=True) is None


def test_latched_angle_never_turns_head() -> None:
    # A constant/latched angle with no speech, no snap, no live sound must NOT turn the
    # head toward it (the latched-DoA guard). idle_energy=0 isolates the reaction logic
    # from the always-alive idle layer (which would otherwise wander the head on silence).
    prod = ListenProducer(
        ListenParams(deadband=10, hold=0.0, recenter_after=1.0, gain=0.6, max_yaw=35, idle_energy=0)
    )
    latched = Sense(doa_angle=0.0, speech_detected=False)  # off-axis but frozen/silent
    for i in range(30):  # 30 ticks of a bare latched angle, no liveness
        a = prod.update(i * 0.1, latched, snap=False, sound_present=False)
        assert a is None, "a silent latched angle must produce no action with idle disabled"
    assert prod.committed == 0.0, "head never turned toward the latched angle"


def test_producer_no_head_turn_within_deadband() -> None:
    # Speech within the deadband leans (Tier-1) but does not turn the head.
    # idle_energy=0 isolates the reaction logic from the always-alive idle layer.
    prod = ListenProducer(ListenParams(deadband=20, gain=0.6, max_yaw=35, idle_energy=0))
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


def test_producer_drifts_home_after_silence() -> None:
    # After a turn, the always-alive idle layer slowly drifts the committed heading back
    # toward front during silence (a gentle drift, not the old hard snap).
    prod = ListenProducer(
        ListenParams(
            deadband=10,
            hold=0.0,
            recenter_after=1.0,
            gain=0.6,
            min_dur=0.0,
            drift_speed=4.0,
            alert_speed=1000.0,
            body_speed=1000.0,  # near-instant escalation so hold clears immediately
        )
    )
    # Speech off-axis commits the turn (latched angle alone never would).
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed != 0.0 or prod.body != 0.0

    # Within the silence grace, idle keeps the heading where it is (stays rotated).
    first = prod.update(0.5, EMPTY_SENSE, sound_present=False)
    assert first is not None and first.coalesce_key == IDLE_KEY
    offset_before = abs(prod.committed) + abs(prod.body)

    # Past the grace, successive idle poses (paced ≥ interval apart) ease the head + body
    # toward 0 monotonically, without overshoot, eventually reaching center.
    seen = []
    for t in (3.1, 5.7, 8.3, 10.9, 13.5, 16.1):
        prod.update(t, EMPTY_SENSE, sound_present=False)
        seen.append(abs(prod.committed) + abs(prod.body))
    assert seen[0] < offset_before, "drift should reduce the offset"
    assert all(b <= a + 1e-9 for a, b in zip(seen, seen[1:])), "drift is monotone toward 0"
    assert prod.committed == 0.0 and prod.body == 0.0, "eventually fully home"


def test_producer_holds_at_target_after_turn() -> None:
    # turn readily on speech, but stay committed for `hold` seconds before reconsidering.
    # idle_energy=0 isolates the reactive path (else idle motion fires during the hold).
    p = ListenParams(
        deadband=10,
        hold=3.0,
        gain=0.6,
        max_yaw=35,
        alert_speed=30,
        min_dur=0.5,
        body_speed=1000.0,  # near-instant escalation so hold duration is driven by alert_speed
        idle_energy=0,
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
    # The right antenna's joint sign is mirrored from the left, so leaning it toward a
    # right-side sound uses a NEGATIVE value (a positive value would tilt it the wrong way).
    assert right_a < 0, "near-side (right) antenna must deflect toward the sound (mirrored sign)"
    assert left_a == 0.0, "far-side (left) antenna must stay neutral"
    assert abs(right_a) > abs(left_a), "near magnitude must exceed far magnitude"


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


def test_no_antenna_lean_without_live_sound() -> None:
    """No live sound → never a Tier-1 antenna lean (ANTENNA_KEY); only idle motion may fire."""
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
    # First commit a turn via speech so there is a non-zero heading to drift from.
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed != 0.0 or prod.body != 0.0
    # Now the angle latches but sound goes silent: the near-side LEAN must never fire on
    # silence (the latched-DoA guard). Background idle poses (IDLE_KEY) are allowed.
    latched = Sense(doa_angle=0.0, speech_detected=False)
    for t in (0.5, 3.1, 5.7, 8.3):
        a = prod.update(t, latched, sound_present=False)
        if a is not None:
            assert a.coalesce_key != ANTENNA_KEY, "silence must not produce an antenna lean"


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


def test_hold_window_suppresses_reactive_but_keeps_idle_alive() -> None:
    """During the post-turn hold window: no reactive re-commit/lean, but idle keeps alive.

    The hold must stop the head whipping to a new sound, yet the always-alive idle layer
    keeps running so the robot never freezes — and the committed heading stays fixed.
    """
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
    assert a is not None and a.coalesce_key == LOOK_KEY  # committed the turn
    committed = prod.committed
    # During hold, even with live sound, no reactive turn/lean fires — only idle may.
    for ti in (0.5, 1.0, 1.5, 2.0, 2.5):
        result = prod.update(ti, s, sound_present=True)
        if result is not None:
            assert result.coalesce_key == IDLE_KEY, f"only idle may fire during hold at t={ti}"
        assert prod.committed == committed, "the committed heading stays fixed during hold"


def test_drift_home_returns_head_and_body_to_center() -> None:
    """After silence, the slow drift eventually eases BOTH head and body back to center."""
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
        drift_speed=4.0,
    )
    prod = ListenProducer(p)
    # Speech off-axis → escalate so both head and body are off-center.
    s = Sense(doa_angle=0.0, speech_detected=True)
    prod.update(0.0, s, sound_present=True)
    assert prod.body != 0.0, "body should be non-zero after escalation"

    # Drive enough silent idle emissions (paced ≥ interval apart, past the grace).
    last = None
    for i in range(12):
        last = prod.update(3.0 + i * 2.6, EMPTY_SENSE, sound_present=False)
    assert prod.committed == 0.0, "head fully drifted home"
    assert prod.body == 0.0, "body fully drifted home"
    # The final idle pose wanders around the now-centred heading (an IDLE_KEY action).
    assert last is not None and last.coalesce_key == IDLE_KEY


# --------------------------------------------------------------------------- #
# always-alive idle layer                                                     #
# --------------------------------------------------------------------------- #


def test_idle_disabled_holds_still_on_silence() -> None:
    """idle_energy=0 restores the old behaviour: silence produces no action."""
    prod = ListenProducer(ListenParams(idle_energy=0))
    assert prod.update(0.0, EMPTY_SENSE, sound_present=False) is None
    assert prod.update(10.0, EMPTY_SENSE, sound_present=False) is None


def test_idle_emits_alive_pose_around_committed_heading() -> None:
    """With a committed heading, idle wanders AROUND it (stays rotated) under IDLE_KEY."""
    p = ListenParams(
        idle_energy=1.0,
        hold=0.0,
        recenter_after=100.0,  # long grace → no drift within the test window
        gain=0.6,
        max_yaw=35,
        min_dur=0.0,
        alert_speed=1000.0,
        head_only_band=90.0,  # head-only turn (no body)
    )
    prod = ListenProducer(p)
    # Commit a head-only turn to the left.
    prod.update(0.0, Sense(doa_angle=1.0, speech_detected=True), sound_present=True)
    committed = prod.committed
    assert committed > 0.0
    # The first idle pose (within the long grace → no drift) wanders near the committed yaw.
    a = prod.update(0.5, EMPTY_SENSE, sound_present=False)
    assert a is not None and a.coalesce_key == IDLE_KEY
    assert a.head is not None
    # Head yaw is offset by the committed heading (not centred on 0); wander stays bounded.
    assert abs(a.head["yaw"] - committed) <= 20.0
    # committed is unchanged (no drift within the grace) — the robot stays rotated.
    assert prod.committed == committed


def test_idle_is_paced_to_interval() -> None:
    """Idle emits at most one pose per AliveConfig.interval; intermediate ticks return None."""
    prod = ListenProducer(ListenParams(idle_energy=1.0))
    first = prod.update(0.0, EMPTY_SENSE, sound_present=False)
    assert first is not None and first.coalesce_key == IDLE_KEY
    # Within the interval (2.5 s) the next ticks are paced out.
    assert prod.update(0.5, EMPTY_SENSE, sound_present=False) is None
    assert prod.update(1.0, EMPTY_SENSE, sound_present=False) is None
    # Past the interval, another idle pose emits.
    assert prod.update(2.6, EMPTY_SENSE, sound_present=False) is not None


def test_idle_head_yaw_clamped_to_max_yaw() -> None:
    """The composed idle head yaw never exceeds the head's safe range even around a max turn."""
    p = ListenParams(
        idle_energy=1.0,
        hold=0.0,
        recenter_after=100.0,
        gain=0.6,
        max_yaw=35,
        min_dur=0.0,
        alert_speed=1000.0,
        head_only_band=90.0,
    )
    prod = ListenProducer(p)
    # Turn to the head-only max so wander could otherwise push past the limit.
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed == 35.0
    for i in range(8):
        a = prod.update(0.5 + i * 2.6, EMPTY_SENSE, sound_present=False)
        if a is not None and a.head is not None:
            assert -35.0 - 1e-9 <= a.head["yaw"] <= 35.0 + 1e-9


def test_idle_disabled_still_recenters_after_turn() -> None:
    """idle_energy=0 keeps the old hard recenter: a committed turn snaps home after silence.

    Regression guard: disabling the idle layer must NOT also disable homing — otherwise the
    robot would stay rotated forever once it had turned.
    """
    p = ListenParams(
        deadband=10,
        hold=0.0,
        recenter_after=1.0,
        gain=0.6,
        min_dur=0.0,
        alert_speed=1000.0,
        body_speed=1000.0,
        idle_energy=0,
    )
    prod = ListenProducer(p)
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    assert prod.committed != 0.0 or prod.body != 0.0
    # Within the silence grace it holds; past it, a single hard recenter to front fires.
    assert prod.update(0.5, EMPTY_SENSE, sound_present=False) is None
    back = prod.update(1.1, EMPTY_SENSE, sound_present=False)
    assert back is not None and back.head["yaw"] == 0.0 and back.body_yaw == 0.0


def test_negative_drift_speed_does_not_diverge() -> None:
    """A negative --drift-speed must never push the heading away from 0 (step clamped to >=0)."""
    p = ListenParams(
        deadband=10,
        hold=0.0,
        recenter_after=1.0,
        gain=0.6,
        min_dur=0.0,
        alert_speed=1000.0,
        body_speed=1000.0,
        drift_speed=-10.0,
    )
    prod = ListenProducer(p)
    prod.update(0.0, Sense(doa_angle=0.0, speech_detected=True), sound_present=True)
    start = abs(prod.committed) + abs(prod.body)
    for t in (3.1, 5.7, 8.3, 10.9):
        prod.update(t, EMPTY_SENSE, sound_present=False)
    assert abs(prod.committed) + abs(prod.body) <= start + 1e-9, "negative drift must not diverge"


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

    def update(self, t, sense, **_kwargs):
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

    def update(self, t, sense, **_kwargs):
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


# --------------------------------------------------------------------------- #
# t8: audio= kwarg wired into producer.update()                               #
# --------------------------------------------------------------------------- #


class _RecordingProducer:
    """Records every (snap, sound_present) pair it is called with; never produces a move."""

    def __init__(self):
        self.calls: list[tuple[bool, object]] = []

    def update(self, t, sense, *, snap: bool = False, sound_present=None, **_):
        self.calls.append((snap, sound_present))
        return None


def test_run_forwards_audio_kwargs_to_producer() -> None:
    """run(audio=...) must pass snap+sound_present from the audio source to producer.update()."""
    # Script: first call returns (False, False), second (True, True), rest (False, False).
    script = [(False, False), (True, True)]
    call_count = [0]

    def _audio(_t):
        i = call_count[0]
        call_count[0] += 1
        return script[i] if i < len(script) else (False, False)

    tr = _RecTransport()
    prod = _RecordingProducer()
    run(
        tr,
        prod,
        hooks=LoopHooks(audio=_audio),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        max_ticks=3,
    )
    assert len(prod.calls) == 3
    assert prod.calls[0] == (False, False)
    assert prod.calls[1] == (True, True)
    assert prod.calls[2] == (False, False)  # past script → default


def test_run_no_audio_passes_false_none_to_producer() -> None:
    """When audio=None (HTTP profile), producer.update() receives snap=False, sound_present=None."""
    tr = _RecTransport()
    prod = _RecordingProducer()
    run(
        tr,
        prod,
        hooks=LoopHooks(audio=None),
        now=_Clock(0.05),
        sleep=lambda *_: None,
        tick=0.05,
        max_ticks=4,
    )
    assert len(prod.calls) == 4
    assert all(c == (False, None) for c in prod.calls)


# --------------------------------------------------------------------------- #
# t8: fake-SDK-transport end-to-end                                           #
# --------------------------------------------------------------------------- #


class _FakeMediaSession:
    """Mimics sdk_transport.MediaSession: loud audio + off-axis speech DoA."""

    def __init__(self, loud_rms: float = 0.5):
        self._loud_rms = loud_rms
        # Build a history of quiet samples first so the SnapDetector has a baseline,
        # then a loud spike is a genuine snap (ratio-5 gate fires once baseline exists).
        self._call_count = 0

    def doa(self, *, timeout=None):  # noqa: ARG002 — timeout unused in fake
        # Speech off-axis to the left (angle=0 rad → left, speech=True).
        return {"angle": 0.0, "speech_detected": True}

    def get_audio_sample(self):
        self._call_count += 1
        # First 10 calls: quiet baseline (rms ≈ 0.001); thereafter: loud spike.
        if self._call_count <= 10:
            return np.full(512, 0.001, dtype=np.float32)
        return np.full(512, self._loud_rms, dtype=np.float32)


class _FakeSdkTransport:
    """A transport that exposes media_session() (SDK profile) and records gotos."""

    name = "sdk-fake"

    def __init__(self):
        self.gotos: list[dict] = []
        self._session = _FakeMediaSession()

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        self.gotos.append(
            {"head": head, "antennas": antennas, "body_yaw": body_yaw, "duration": duration}
        )
        return {"uuid": "x"}

    @contextlib.contextmanager
    def media_session(self):
        yield self._session


def test_sdk_transport_audio_drives_snap_turn(monkeypatch) -> None:
    """Fake-SDK transport: loud audio + off-axis speech → Tier-2 head (or body) turn dispatched.

    Drive via cmd_listen_run with an injected fake SDK transport.  To avoid
    real-clock timing issues the test patches time.sleep to a no-op and uses
    a fast enough move duration (speed=1000 deg/s, min_dur via speed) so that
    busy_until clears within the first handful of ticks.
    """
    import argparse

    from reachy.cli._commands.listen import cmd_listen_run

    monkeypatch.setattr("time.sleep", lambda *_: None)

    tr = _FakeSdkTransport()

    # Build a minimal args namespace (same fields as the real CLI).
    args = argparse.Namespace(
        json=False,
        gain=0.6,
        max_yaw=35.0,
        deadband=0.0,  # zero deadband so even small off-axis angles trigger
        dwell=0.0,
        hold=0.0,
        speed=1000.0,  # very fast so move duration is tiny; busy_until clears quickly
        recenter_after=60.0,
        speech_only=False,
        max_ticks=30,  # enough ticks for at least one Tier-2 dispatch
    )

    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _: tr)

    rc = cmd_listen_run(args)

    assert rc == 0
    # The first goto is the preflight center; subsequent ones should include a
    # head turn driven by the speech+snap path (Tier-2: yaw != 0 or body_yaw != None).
    non_center = [
        g
        for g in tr.gotos
        if (g.get("head") or {}).get("yaw", 0.0) != 0.0 or g.get("body_yaw") is not None
    ]
    assert non_center, (
        f"expected at least one off-center head/body move from snap/speech path; "
        f"gotos={tr.gotos}"
    )
