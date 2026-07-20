"""Fake-clock contract tests for the stateful dog-like pet reaction."""

from __future__ import annotations

import pytest

from reachy.behavior.engine import Engine
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass
from reachy.behavior.pet_reaction import (
    ANTENNA_LIMIT_DEG,
    ANTENNA_STEP_DEG,
    BODY_YAW_LIMIT_DEG,
    BODY_YAW_STEP_DEG,
    DONE_GESTURE_S,
    HEAD_PITCH_LIMIT_DEG,
    HEAD_ROTATION_STEP_DEG,
    HEAD_YAW_LIMIT_DEG,
    MAX_CONTACT_S,
    PAT_COOLDOWN_S,
    SENSE_LOSS_GRACE_S,
    make_pet_reaction,
)
from reachy.behavior.sense import PatState, Sense

DT = 0.02
ALL_CHANNELS = frozenset({"head", "antennas", "body_yaw"})


def _state(
    *,
    availability: str = "available",
    contact: bool = True,
    touch_type: str | None = "side_pat",
    yaw_deg: float | None = 3.0,
    phase: str = "receptive",
    last_press_at: float | None = 10.0,
) -> Sense:
    return Sense(
        pat_state=PatState(
            availability=availability,
            contact=contact,
            touch_type=touch_type,
            level="level1" if contact else None,
            yaw_deg=yaw_deg,
            phase=phase,
            phase_started_at=10.0,
            last_press_at=last_press_at,
        )
    )


def _run(reaction, sense: Sense, *, start: float = 0.0, ticks: int = 60):
    out = None
    for index in range(ticks):
        out = reaction(start + index * DT, {}, sense)
    assert out is not None
    return out


def _assert_complete_pose(out: Contribution) -> None:
    assert out.done is False
    assert set(out.head or {}) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert isinstance(out.antennas, tuple) and len(out.antennas) == 2
    assert isinstance(out.body_yaw, float)


def test_side_pat_seeks_the_hand_by_opposing_the_measured_deviation() -> None:
    """The lean presses BACK along the axis the hand pushed, not with it.

    ``yaw_deg`` is the signed actual-minus-commanded deviation, so a hand on the
    robot's right pushes the head left and arrives here with the left sign.
    Seeking that hand means turning right — opposite the deviation. Both axes
    still saturate at their clamps on a large deviation.
    """
    pushed_left = _run(make_pet_reaction(), _state(yaw_deg=100.0))
    pushed_right = _run(make_pet_reaction(), _state(yaw_deg=-100.0))

    _assert_complete_pose(pushed_left)
    _assert_complete_pose(pushed_right)
    assert pushed_left.head["yaw"] == pytest.approx(-HEAD_YAW_LIMIT_DEG)
    assert pushed_right.head["yaw"] == pytest.approx(HEAD_YAW_LIMIT_DEG)
    assert pushed_left.body_yaw == pytest.approx(-BODY_YAW_LIMIT_DEG)
    assert pushed_right.body_yaw == pytest.approx(BODY_YAW_LIMIT_DEG)


def test_scratch_is_a_distinct_pitch_pose_independent_of_yaw_side() -> None:
    scratch_left = _run(
        make_pet_reaction(),
        _state(touch_type="scratch", yaw_deg=9.0),
    )
    scratch_right = _run(
        make_pet_reaction(),
        _state(touch_type="scratch", yaw_deg=-9.0),
    )
    side = _run(make_pet_reaction(), _state(touch_type="side_pat", yaw_deg=9.0))

    assert scratch_left.head == scratch_right.head
    assert scratch_left.body_yaw == scratch_right.body_yaw == 0.0
    assert scratch_left.head["yaw"] == 0.0
    assert scratch_left.head["pitch"] != side.head["pitch"]


def test_contact_pose_becomes_bit_exactly_constant_for_sensing() -> None:
    reaction = make_pet_reaction()
    sense = _state(yaw_deg=4.0)
    outputs = [reaction(index * DT, {}, sense) for index in range(100)]

    for out in outputs:
        _assert_complete_pose(out)
    assert len({repr(out) for out in outputs[-30:]}) == 1


def test_worst_case_entry_settles_early_enough_for_one_second_reacquisition() -> None:
    reaction = make_pet_reaction()
    sense = _state(yaw_deg=100.0)
    outputs = [reaction(index * DT, {}, sense) for index in range(50)]
    stable_index = next(
        index
        for index, output in enumerate(outputs)
        if all(later == output for later in outputs[index:])
    )

    assert stable_index * DT + 0.5 <= SENSE_LOSS_GRACE_S


def test_receptive_contentment_and_warning_have_stable_antenna_meanings() -> None:
    reaction = make_pet_reaction()
    receptive = _run(reaction, _state(phase="receptive"), ticks=60)
    contentment = _run(
        reaction,
        _state(phase="contentment"),
        start=1.2,
        ticks=30,
    )
    warning = _run(
        reaction,
        _state(phase="warning"),
        start=1.8,
        ticks=30,
    )

    assert len({receptive.antennas, contentment.antennas, warning.antennas}) == 3
    assert receptive.head == contentment.head == warning.head
    assert receptive.body_yaw == contentment.body_yaw == warning.body_yaw


def test_yaw_deadband_and_first_credible_direction_latch_prevent_chatter() -> None:
    reaction = make_pet_reaction()
    t = 0.0
    for yaw in (0.2, -0.3, 0.4, -0.1):
        for _ in range(4):
            out = reaction(t, {}, _state(yaw_deg=yaw))
            t += DT
    assert out.head["yaw"] == 0.0

    # First credible deviation latches the direction. It is a push to the left,
    # so the hand is on the right and the seeking lean turns right (negative).
    for _ in range(50):
        out = reaction(t, {}, _state(yaw_deg=4.0))
        t += DT
    assert out.head["yaw"] < 0.0

    # A later opposite-signed deviation must NOT flip the lean — the latch is
    # what stops the head chattering between sides mid-pat.
    for _ in range(50):
        out = reaction(t, {}, _state(yaw_deg=-10.0))
        t += DT
    assert out.head["yaw"] < 0.0


def test_observed_release_starts_done_gesture_within_release_budget() -> None:
    reaction = make_pet_reaction()
    contact = _state(last_press_at=10.0)
    before = _run(reaction, contact, ticks=35)
    released_at = 0.7
    released = _state(
        contact=False,
        touch_type=None,
        yaw_deg=None,
        phase="released",
        last_press_at=10.0,
    )

    first_release_tick = reaction(released_at, {}, released)

    assert released_at < 1.0
    assert reaction.finish_reason == "observed_release"
    assert reaction.finish_started_at == released_at
    assert first_release_tick != before
    done = _run_until_done(reaction, released, start=released_at + DT)
    assert done <= released_at + DONE_GESTURE_S + 2 * DT


def test_enough_latches_one_gesture_across_persistent_cooldown() -> None:
    assert PAT_COOLDOWN_S == 5.0
    reaction = make_pet_reaction()
    _run(reaction, _state(phase="contentment"), ticks=30)
    enough_at = 0.6
    reaction(enough_at, {}, _state(phase="enough"))
    started = reaction.finish_started_at

    t = enough_at + DT
    while t <= enough_at + DONE_GESTURE_S + 2 * DT:
        out = reaction(t, {}, _state(contact=False, phase="cooldown", touch_type=None))
        assert reaction.finish_reason == "enough"
        assert reaction.finish_started_at == started
        if out.done:
            break
        t += DT

    assert out.done is True
    assert reaction.finish_count == 1


def test_done_gesture_coordinates_head_body_wiggle_and_antenna_reorientation() -> None:
    reaction = make_pet_reaction()
    contact = _state(yaw_deg=3.0)
    start = _run(reaction, contact, ticks=60)
    enough_at = 1.2
    samples = [reaction(enough_at, {}, _state(phase="enough"))]
    for index in range(1, 60):
        out = reaction(
            enough_at + index * DT,
            {},
            _state(contact=False, touch_type=None, phase="cooldown"),
        )
        if out.done:
            break
        samples.append(out)

    assert max(out.head["yaw"] for out in samples) - min(out.head["yaw"] for out in samples) > 1.0
    assert max(out.body_yaw for out in samples) - min(out.body_yaw for out in samples) > 1.0
    assert samples[-1].antennas != start.antennas


def test_admission_during_cooldown_completes_without_replaying_gesture() -> None:
    reaction = make_pet_reaction()

    out = reaction(0.0, {}, _state(contact=False, phase="cooldown", touch_type=None))

    assert out == Contribution(done=True)
    assert reaction.finish_count == 0


def test_blocked_or_unavailable_never_escalates_and_uses_bounded_grace() -> None:
    reaction = make_pet_reaction()
    _run(reaction, _state(phase="receptive"), ticks=10)
    blocked = _state(availability="blocked", phase="enough")

    reaction(0.2, {}, blocked)
    before_grace = reaction(0.2 + SENSE_LOSS_GRACE_S - DT, {}, blocked)
    assert before_grace.done is False
    assert reaction.phase == "receptive"
    assert reaction.finish_reason is None

    reaction(0.2 + SENSE_LOSS_GRACE_S, {}, blocked)
    assert reaction.finish_reason == "sensing_lost"
    assert reaction.finish_reason != "observed_release"
    done = _run_until_done(
        reaction,
        _state(availability="unavailable", phase="released", contact=False),
        start=0.2 + SENSE_LOSS_GRACE_S + DT,
    )
    assert done <= 0.2 + SENSE_LOSS_GRACE_S + DONE_GESTURE_S + 2 * DT


def test_available_contact_has_finite_safety_backstop() -> None:
    reaction = make_pet_reaction()
    sense = _state(phase="receptive")
    reaction(0.0, {}, sense)

    reaction(MAX_CONTACT_S, {}, sense)

    assert reaction.finish_reason == "safety_backstop"
    done = _run_until_done(reaction, sense, start=MAX_CONTACT_S + DT)
    assert done <= MAX_CONTACT_S + DONE_GESTURE_S + 2 * DT


def test_every_active_tick_is_complete_bounded_and_slew_limited() -> None:
    reaction = make_pet_reaction()
    outputs: list[Contribution] = []
    for index in range(45):
        outputs.append(reaction(index * DT, {}, _state(yaw_deg=100.0)))
    enough_at = 45 * DT
    for index in range(80):
        out = reaction(
            enough_at + index * DT,
            {},
            _state(phase="enough" if index == 0 else "cooldown", contact=index == 0),
        )
        if out.done:
            break
        outputs.append(out)

    for out in outputs:
        _assert_complete_pose(out)
        assert abs(out.head["pitch"]) <= HEAD_PITCH_LIMIT_DEG
        assert abs(out.head["yaw"]) <= HEAD_YAW_LIMIT_DEG
        assert abs(out.body_yaw) <= BODY_YAW_LIMIT_DEG
        assert max(abs(value) for value in out.antennas) <= ANTENNA_LIMIT_DEG
    for previous, current in zip(outputs, outputs[1:], strict=False):
        for axis in ("roll", "pitch", "yaw"):
            assert abs(current.head[axis] - previous.head[axis]) <= HEAD_ROTATION_STEP_DEG
        assert abs(current.body_yaw - previous.body_yaw) <= BODY_YAW_STEP_DEG
        for before, after in zip(previous.antennas, current.antennas, strict=True):
            assert abs(after - before) <= ANTENNA_STEP_DEG


def test_release_completion_and_explicit_stop_free_all_channels_to_base() -> None:
    engine = Engine()
    base_id = engine.seed_base_layer(now=0.0, energy=0.0)
    reaction = make_pet_reaction()
    behavior = Behavior(
        id="pet-reaction-1",
        name="pet-reaction",
        channels=ALL_CHANNELS,
        stop_class=StopClass.STOPPABLE,
        lifetime=Lifetime(looping=True, duration=None),
        params={},
        fn=reaction,
        wants_sense=True,
    )
    engine.admit_behavior(behavior, now=0.0)
    engine.compose_tick(0.1, _state())
    stopped = engine.stop("pet-reaction-1")
    stopped_tick = engine.compose_tick(0.12, _state())

    assert stopped["stopped"] == ["pet-reaction-1"]
    assert set(stopped_tick["ownership"].values()) == {base_id}

    second = make_pet_reaction()
    engine.admit_behavior(
        Behavior(
            id="pet-reaction-2",
            name="pet-reaction",
            channels=ALL_CHANNELS,
            stop_class=StopClass.STOPPABLE,
            lifetime=Lifetime(looping=True, duration=None),
            params={},
            fn=second,
            wants_sense=True,
        ),
        now=1.0,
    )
    completed_tick = None
    for index in range(100):
        now = 1.0 + index * DT
        sense = _state() if index < 5 else _state(contact=False, phase="released")
        tick = engine.compose_tick(now, sense)
        if tick["completed"]:
            completed_tick = tick
            break

    assert completed_tick is not None
    assert completed_tick["completed"] == ["pet-reaction-2"]
    assert set(completed_tick["ownership"].values()) == {base_id}


def test_bad_pat_snapshot_becomes_bounded_fault_completion_not_exception() -> None:
    reaction = make_pet_reaction()
    malformed = Sense(pat_state=object())  # type: ignore[arg-type]

    first = reaction(0.0, {}, malformed)

    _assert_complete_pose(first)
    assert reaction.finish_reason == "fault"
    done = _run_until_done(reaction, malformed, start=DT)
    assert done <= DONE_GESTURE_S + 2 * DT


def _run_until_done(reaction, sense: Sense, *, start: float) -> float:
    for index in range(200):
        t = start + index * DT
        if reaction(t, {}, sense).done:
            return t
    raise AssertionError("pet reaction did not complete within the bounded test window")
