"""Sound-orienting in the symbolic runtime (task t8).

The old flow's two-tier ladder (``reachy.motion.listen.ListenProducer``) is the
DONOR. These tests pin three things:

1. **Admission** — the interim, corroborated gate refuses to steer on the
   measured at-rest signal (``speech_detected`` flickering true ~46 % of the
   time with the angle wandering the full range —
   ``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 2), and
   the gate is a plain injectable seam so task t9's latched-DoA guard drops in
   without touching the planner.
2. **Geometry** — the planner reproduces the donor's ladder NUMBER FOR NUMBER,
   asserted by driving the real ``ListenProducer`` alongside it.
3. **Arbitration** — orienting is an ordinary channel owner: a pat reaction and
   a sleep-class behavior preempt it exactly as they preempt anything else, and
   with no sound it ABSTAINS so ``feel-alive`` keeps the channels.
"""

from __future__ import annotations

import ast
import inspect
import math

import pytest

from reachy.behavior import library
from reachy.behavior.arbitration import admit, arbitrate
from reachy.behavior.engine import Engine
from reachy.behavior.model import Contribution, Lifetime, StopClass
from reachy.behavior.orient import (
    ANTENNA_LEAN_S,
    CorroboratedGate,
    LatchedDoaGuard,
    OrientParams,
    OrientTier,
    OrientToSound,
    make_orient_to_sound,
    plan_orient,
)
from reachy.behavior.sense import Sense, doa_angle_to_yaw
from reachy.motion.listen import ListenParams, ListenProducer

# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #

#: A reading loud enough to clear the ported ``sound_present`` floor.
LOUD = 0.2
#: A reading below it — the quiet room the baseline probe measured.
QUIET = 0.001


def _sense(*, angle=None, speech=False, rms=None, transcript=None) -> Sense:
    return Sense(doa_angle=angle, speech_detected=speech, rms=rms, transcript=transcript)


def _donor(**overrides) -> ListenProducer:
    """A ``ListenProducer`` in transcribe-style mode (the 3-tier ladder)."""
    return ListenProducer(ListenParams(turn_enabled=False, idle_energy=0.0, **overrides))


def _drive(fn, sense, *, ticks: int, dt: float = 0.02, t0: float = 0.0):
    """Run a contribution function for *ticks* ticks, returning every contribution."""
    return [fn(t0 + i * dt, {}, sense) for i in range(ticks)]


def _assert_same(target, action) -> None:
    """Assert a planned :class:`OrientTarget` matches a donor ``MotionAction``.

    The donor expresses "no head commitment" as ``action.head is None`` (its
    within-deadband antenna lean) and "nothing at all" as ``None``; the port
    expresses those the same two ways, so the comparison is total.
    """
    if action is None:
        assert target is None
        return
    assert target is not None
    head = None if action.head is None else action.head["yaw"]
    assert target.head_yaw == (None if head is None else pytest.approx(head))
    assert target.body_yaw == (None if action.body_yaw is None else pytest.approx(action.body_yaw))
    assert target.antennas == pytest.approx(action.antennas)
    assert target.duration == pytest.approx(action.duration)


# --------------------------------------------------------------------------- #
# 1. Admission — the interim corroborated gate                                #
# --------------------------------------------------------------------------- #


def test_gate_refuses_to_steer_on_the_measured_at_rest_signal() -> None:
    """The load-bearing constraint: 120 samples in a QUIET room read
    ``speech_detected`` true 46 % of the time, with 35 distinct angles spanning
    0.000-3.124 rad. Replayed through the gate, not one tick may reach a
    head-moving tier — there is no sound ENERGY to corroborate the flag.
    """
    gate = CorroboratedGate()
    params = OrientParams()
    rng_angles = [0.0, 1.082, 3.124, 1.047, 0.942, 1.117, 2.4, 0.3, 1.065, 1.9]
    tiers = set()
    for i in range(120):
        flicker = bool(i % 2)  # ~46-50 % true, as measured
        angle = rng_angles[i % len(rng_angles)]
        tiers.add(gate(_sense(angle=angle, speech=flicker, rms=QUIET), i * 0.5, params))
    assert tiers == {OrientTier.NONE}


def test_gate_refuses_a_wandering_angle_even_when_the_room_is_loud() -> None:
    """Sound energy alone is not a bearing. While the angle keeps jumping
    (the measured 0.000-3.124 rad wander) the gate may lean antennas but must
    never reach the head-moving SPEECH tier.
    """
    gate = CorroboratedGate()
    params = OrientParams()
    angles = [0.2, 2.9, 1.0, 3.0, 0.1, 1.6, 2.7, 0.4]
    tiers = [
        gate(_sense(angle=angles[i % len(angles)], speech=True, rms=LOUD), i * 0.5, params)
        for i in range(40)
    ]
    assert OrientTier.SPEECH not in tiers
    assert OrientTier.NOISE in tiers  # the antenna lean still reacts to live sound


def test_gate_reaches_the_speech_tier_once_the_bearing_holds_still() -> None:
    """A real speaker gives a STEADY bearing. Once the angle has dwelt inside
    the tolerance for ``dwell_s``, the head-moving tier opens.
    """
    gate = CorroboratedGate()
    params = OrientParams()
    seen = [gate(_sense(angle=0.5, speech=True, rms=LOUD), t, params) for t in (0.0, 0.2, 0.4)]
    assert seen[0] is OrientTier.NONE  # the NOISE attack not yet earned (t35's envelope)
    assert seen[1] is OrientTier.NOISE  # dwell not yet earned
    late = gate(_sense(angle=0.5, speech=True, rms=LOUD), params.dwell_s + 0.1, params)
    assert late is OrientTier.SPEECH


def test_gate_engages_immediately_on_an_addressed_transcript() -> None:
    """An utterance that cleared the engagement gate (t11's ``Sense.transcript``)
    is corroboration of the strongest kind — it needs no dwell and no loudness.
    """
    gate = CorroboratedGate()
    assert (
        gate(_sense(angle=0.5, transcript="hey reachy"), 0.0, OrientParams()) is OrientTier.ENGAGED
    )


def test_gate_never_engages_without_a_bearing_to_steer_toward() -> None:
    gate = CorroboratedGate()
    params = OrientParams()
    assert gate(_sense(transcript="hey reachy", rms=LOUD), 0.0, params) is OrientTier.NONE
    assert gate(_sense(speech=True, rms=LOUD), 0.0, params) is OrientTier.NONE


def test_gate_is_an_injectable_seam_the_latched_doa_guard_plugs_into() -> None:
    """t9 ports the latched-DoA guard. It must compose as a plain callable —
    the planner must not bake ``if sense.speech_detected`` into itself.
    """
    calls: list[tuple] = []

    def veto_gate(sense, now, params):  # the shape t9 implements
        calls.append((sense.doa_angle, now))
        return OrientTier.NONE

    fn = OrientToSound(gate=veto_gate)
    contribs = _drive(fn, _sense(angle=0.0, speech=True, rms=LOUD), ticks=10)
    assert calls  # the seam was consulted every tick
    assert all(c.head is None and c.body_yaw is None for c in contribs)


def test_a_raising_gate_leaves_the_robot_still_rather_than_killing_the_tick() -> None:
    def boom(sense, now, params):
        raise RuntimeError("gate on fire")

    fn = OrientToSound(gate=boom)
    contribs = _drive(fn, _sense(angle=0.0, speech=True, rms=LOUD), ticks=10)
    assert all(c.head is None and c.antennas is None for c in contribs)


def test_every_tier_transition_is_logged_once_and_never_per_tick(caplog) -> None:
    """The trace an operator greps to answer "did the gate open, and on what?"
    — bounded to genuine transitions, and a close is a named DROP, never a
    silent no-op."""
    fn = make_orient_to_sound()
    with caplog.at_level("INFO", logger="reachy.sense"):
        _drive(fn, _sense(angle=0.5, speech=True, rms=LOUD), ticks=50)  # NONE -> NOISE
        _drive(fn, _sense(rms=QUIET), ticks=50, t0=1.0)  # NOISE -> NONE
    lines = [r.getMessage() for r in caplog.records if "stage=orient" in r.getMessage()]
    # Four genuine transitions across 100 ticks: the ladder climbing once the
    # attack then the dwell are earned, demoting to the lean-only tier while the
    # release hold rides out the quiet, then closing when the hold expires.
    assert len(lines) == 4, lines
    assert "NONE->NOISE" in lines[0] and "bearing=0.500rad" in lines[0]
    assert "NOISE->SPEECH" in lines[1]
    assert "SPEECH->NOISE" in lines[2]  # the release hold keeps the lean, not the head tier
    assert "closed from=NOISE" in lines[3]


# --------------------------------------------------------------------------- #
# 2. Geometry — the planner against the donor, number for number              #
# --------------------------------------------------------------------------- #


def test_orient_params_do_not_drift_from_the_donor_tunables() -> None:
    """Every knob the ladder shares with ``ListenParams`` keeps the donor's
    value, so "observably equivalent" cannot rot silently.
    """
    donor = ListenParams()
    ours = OrientParams()
    shared = (
        "gain",
        "max_yaw",
        "deadband",
        "hold",
        "alert_speed",
        "relax_speed",
        "min_dur",
        "max_dur",
        "antenna_gain",
        "antenna_max",
        "body_yaw_max",
        "body_speed",
        "head_only_band",
        "speech_orient_gain",
        "speech_orient_max",
        "engaged_min_dur",
        "recenter_after",
    )
    for name in shared:
        assert getattr(ours, name) == getattr(donor, name), name


@pytest.mark.parametrize("angle", [0.0, 0.4, 0.9, 1.2, 1.5708, 1.9, 2.4, 3.1])
def test_engaged_tier_matches_the_donor_turn(angle: float) -> None:
    """The ENGAGED tier reproduces the donor's deliberate turn — head yaw, body
    yaw, antenna pair and duration — including the head->body escalation
    beyond ``head_only_band`` and the head re-centre onto the residual.
    """
    prod = _donor()
    action = prod._react_to_angle(angle, 0.0, triggered=False, live=True, speech=True, engaged=True)
    target = plan_orient(OrientTier.ENGAGED, angle, OrientParams(), head_yaw=0.0, body_yaw=0.0)
    _assert_same(target, action)


@pytest.mark.parametrize("angle", [0.0, 0.6, 1.2, 2.2, 3.0])
def test_speech_tier_matches_the_donor_bounded_head_only_nudge(angle: float) -> None:
    prod = _donor()
    action = prod._react_to_angle(
        angle, 0.0, triggered=False, live=True, speech=True, engaged=False
    )
    target = plan_orient(OrientTier.SPEECH, angle, OrientParams(), head_yaw=0.0, body_yaw=0.0)
    _assert_same(target, action)
    if target is not None:
        assert target.body_yaw is None  # the speech tier never escalates to the body
        if target.head_yaw is not None:
            assert abs(target.head_yaw) <= OrientParams().speech_orient_max


@pytest.mark.parametrize("angle", [0.0, 0.7, 1.5708, 2.3, 3.1])
def test_noise_tier_matches_the_donor_antenna_lean(angle: float) -> None:
    """Tier 1 survives: the NEAR-side antenna deflects toward the sound (the
    right joint's sign mirrored), the head is never driven.
    """
    prod = _donor()
    action = prod._react_to_angle(
        angle, 0.0, triggered=False, live=True, speech=False, engaged=False
    )
    target = plan_orient(OrientTier.NOISE, angle, OrientParams(), head_yaw=0.0, body_yaw=0.0)
    _assert_same(target, action)
    if target is not None:
        assert target.head_yaw is None and target.body_yaw is None
        assert target.duration == pytest.approx(ANTENNA_LEAN_S)


def test_planner_holds_inside_the_deadband() -> None:
    """A source within ``deadband`` of the current heading does not re-commit the
    HEAD — the donor's anti-whip guard. It still leans the antennas, exactly as
    the donor fell through to its Tier-1 lean."""
    params = OrientParams()
    angle = math.pi / 2.0 - math.radians(20.0 / params.gain)  # desired ~ +20 deg
    committed = plan_orient(OrientTier.ENGAGED, angle, params, head_yaw=0.0, body_yaw=0.0)
    assert committed is not None and committed.head_yaw is not None
    held = plan_orient(OrientTier.ENGAGED, angle, params, head_yaw=20.0, body_yaw=0.0)
    assert held is not None and held.head_yaw is None
    donor = _donor()
    donor.committed = 20.0
    action = donor._react_to_angle(
        angle, 0.0, triggered=False, live=True, speech=True, engaged=True
    )
    _assert_same(held, action)


def test_planner_escalates_to_the_body_beyond_the_head_only_band() -> None:
    params = OrientParams()
    inside = plan_orient(OrientTier.ENGAGED, 0.9, params, head_yaw=0.0, body_yaw=0.0)
    assert inside is not None and inside.body_yaw is None
    outside = plan_orient(OrientTier.ENGAGED, 0.0, params, head_yaw=0.0, body_yaw=0.0)
    assert outside is not None and outside.body_yaw is not None
    raw = doa_angle_to_yaw(0.0, params.gain)
    assert outside.body_yaw == pytest.approx(min(abs(raw), params.body_yaw_max))
    # head takes the residual so head + body together face the source
    assert outside.head_yaw == pytest.approx(
        max(-params.max_yaw, min(params.max_yaw, raw - outside.body_yaw))
    )


def test_planner_never_exceeds_the_joint_clamps() -> None:
    params = OrientParams()
    for i in range(64):
        angle = math.pi * i / 63.0
        for tier in (OrientTier.NOISE, OrientTier.SPEECH, OrientTier.ENGAGED):
            target = plan_orient(tier, angle, params, head_yaw=0.0, body_yaw=0.0)
            if target is None:
                continue
            if target.head_yaw is not None:
                assert abs(target.head_yaw) <= params.max_yaw + 1e-9
            if target.body_yaw is not None:
                assert abs(target.body_yaw) <= params.body_yaw_max + 1e-9
            if target.antennas is not None:
                assert max(abs(v) for v in target.antennas) <= params.antenna_max + 1e-9


# --------------------------------------------------------------------------- #
# 3. The behavior — sustained, smooth, abstaining                             #
# --------------------------------------------------------------------------- #


def test_orienting_eases_to_the_target_instead_of_snapping() -> None:
    """The donor's turns were minjerk gotos with a >= 1.5 s duration floor. The
    runtime behavior must not step the commanded head in one tick.
    """
    fn = make_orient_to_sound()
    sense = _sense(angle=0.0, transcript="hey reachy", rms=LOUD)  # engaged, far off-axis
    contribs = _drive(fn, sense, ticks=120)
    yaws = [c.head["yaw"] for c in contribs if c.head is not None]
    assert yaws, "the engaged tier must drive the head"
    assert yaws[0] == pytest.approx(0.0, abs=1.0)  # starts from where it was
    assert max(abs(b - a) for a, b in zip(yaws, yaws[1:])) < 2.0  # no snap, per tick
    assert abs(yaws[-1]) > 5.0  # and it actually got somewhere


def test_orienting_holds_its_bearing_after_committing() -> None:
    """The ``hold`` window: after committing, a new bearing does not whip the
    head straight back (the donor's ``_hold_until``)."""
    params = OrientParams()
    fn = OrientToSound(params)
    engaged = _sense(angle=0.0, transcript="hey reachy", rms=LOUD)
    _drive(fn, engaged, ticks=200)  # commit + settle
    committed = fn.head_yaw
    # A brand new bearing on the far side, inside the hold window: ignored.
    other = _sense(angle=math.pi, transcript="over here", rms=LOUD)
    _drive(fn, other, ticks=5, t0=4.0)
    assert fn.target_head_yaw == pytest.approx(committed, abs=1e-6)


def test_orienting_abstains_with_no_sound_so_feel_alive_keeps_the_channels() -> None:
    """A sound-reactive behavior with no sound yields rather than freezing —
    the abstention contract ``arbitrate`` implements."""
    fn = make_orient_to_sound()
    contribs = _drive(fn, _sense(rms=QUIET), ticks=10)
    assert all(
        c.head is None and c.antennas is None and c.body_yaw is None and not c.done
        for c in contribs
    )


def test_orienting_drifts_home_then_abstains_when_the_sound_stops() -> None:
    """The donor's drift-home: after ``recenter_after`` seconds of silence the
    committed heading eases back to front, and only THEN does the behavior let
    go of the channel."""
    params = OrientParams(recenter_after=0.5)
    fn = OrientToSound(params)
    _drive(fn, _sense(angle=0.0, transcript="hey reachy", rms=LOUD), ticks=200)
    assert abs(fn.head_yaw) > 5.0
    quiet = _sense(rms=QUIET)
    contribs = _drive(fn, quiet, ticks=1000, t0=4.0)
    assert abs(fn.head_yaw) == pytest.approx(0.0, abs=0.5)
    assert contribs[-1].head is None  # released the channel once home
    assert not contribs[-1].done  # ... but the behavior itself is still alive


def test_orienting_never_raises_on_a_hostile_sense_or_clock() -> None:
    fn = make_orient_to_sound()

    class Boom:
        @property
        def doa_angle(self):
            raise RuntimeError("sensor on fire")

    assert fn(0.0, {}, Boom()).head is None  # type: ignore[arg-type]
    assert fn(float("nan"), {}, _sense(rms=LOUD, angle=0.5)) is not None
    assert fn(-5.0, {}, _sense(rms=LOUD, angle=0.5)) is not None


def test_library_params_retune_the_ladder_without_code_changes() -> None:
    entry = library.get("orient-to-sound")
    defaults = entry.default_params()
    assert defaults["gain"] == OrientParams().gain
    assert defaults["max_yaw"] == OrientParams().max_yaw
    fn = entry.build_fn()
    tight = dict(defaults, max_yaw=25.0, head_only_band=90.0)  # head-only, tighter clamp
    contribs = [
        fn(i * 0.02, tight, _sense(angle=0.0, transcript="hi", rms=LOUD)) for i in range(300)
    ]
    yaws = [c.head["yaw"] for c in contribs if c.head is not None]
    assert yaws and max(abs(y) for y in yaws) == pytest.approx(25.0)
    assert all(c.body_yaw is None for c in contribs)  # the raised band suppressed escalation


def test_library_mints_a_fresh_instance_per_behavior() -> None:
    entry = library.get("orient-to-sound")
    assert entry.wants_sense is True
    assert entry.build_fn() is not entry.build_fn()


# --------------------------------------------------------------------------- #
# 4. Arbitration — orienting is an ordinary channel owner                     #
# --------------------------------------------------------------------------- #


def _orient_behavior(behavior_id: str = "orient-1") -> object:
    entry = library.get("orient-to-sound")
    return library.build(
        "orient-to-sound",
        entry.default_params(),
        entry.default_class,
        Lifetime(looping=True, duration=None),
        behavior_id,
    )


def _blocker(name: str, stop_class: StopClass, behavior_id: str, channels=None):
    entry = library.get(name)
    beh = library.build(
        name,
        entry.default_params(),
        stop_class,
        Lifetime(looping=True, duration=10.0),
        behavior_id,
    )
    if channels is not None:
        import dataclasses

        beh = dataclasses.replace(beh, channels=frozenset(channels))
    return beh


def test_a_pat_reaction_preempts_orienting_on_every_shared_channel() -> None:
    """``pet-reaction`` is admitted (by the pat rule) AFTER orienting and claims
    the same three channels; same class, higher recency -> it owns them all.
    """
    orient = _orient_behavior()
    pet = _blocker("pet-reaction", StopClass.STOPPABLE, "pet-1")
    owners = arbitrate([orient, pet])
    assert {ch: owners[ch].id for ch in owners} == {
        "head": "pet-1",
        "antennas": "pet-1",
        "body_yaw": "pet-1",
    }


def test_a_sleep_class_behavior_preempts_orienting_by_priority() -> None:
    """Sleep owns the head against everything below it. Whether it arrives as
    ``stopping`` (evicts on admit) or ``unstoppable`` (out-prioritised per
    tick), orienting yields — it is plain ``stoppable``.
    """
    orient = _orient_behavior()
    stopping = _blocker("gaze-hold", StopClass.STOPPING, "sleep-1", channels=["head"])
    result = admit(stopping, [orient])
    assert [b.id for b in result.evicted] == ["orient-1"]

    orient2 = _orient_behavior("orient-2")
    unstoppable = _blocker("gaze-hold", StopClass.UNSTOPPABLE, "sleep-2", channels=["head"])
    owners = arbitrate([unstoppable, orient2])
    assert owners["head"].id == "sleep-2"
    assert owners["body_yaw"].id == "orient-2"  # only the shared channel is taken


def test_orienting_takes_the_channel_back_once_the_preemptor_ends() -> None:
    orient = _orient_behavior()
    unstoppable = _blocker("gaze-hold", StopClass.UNSTOPPABLE, "sleep-2", channels=["head"])
    assert arbitrate([unstoppable, orient])["head"].id == "sleep-2"
    assert arbitrate([orient])["head"].id == "orient-1"


def test_orienting_loses_the_head_to_a_pat_reaction_in_a_live_engine() -> None:
    """End to end on the real engine: orienting owns the head while it is the
    only claimant, and the moment a pat reaction is admitted the composed pose
    is the reaction's, not the orient target's.
    """
    engine = Engine()
    engine.seed_base_layer(now=0.0, energy=1.0)
    engine.admit_behavior(_orient_behavior(), now=0.0)
    loud = _sense(angle=0.0, transcript="hey reachy", rms=LOUD)
    for i in range(200):
        tick = engine.compose_tick(i * 0.02, sense=loud)
    assert tick["ownership"]["head"] == "orient-1"
    engine.admit_behavior(_blocker("pet-reaction", StopClass.STOPPABLE, "pet-1"), now=4.0)
    ownership = engine.compose_tick(4.02, sense=loud)["ownership"]
    assert ownership["head"] == "pet-1"
    assert ownership["antennas"] == "pet-1"
    assert ownership["body_yaw"] == "pet-1"


def test_orienting_yields_the_head_to_feel_alive_in_a_quiet_room() -> None:
    """The whole point of the abstention contract, on the real engine: with no
    sound the base layer keeps driving the head."""
    engine = Engine()
    engine.seed_base_layer(now=0.0, energy=1.0)
    engine.admit_behavior(_orient_behavior(), now=0.0)
    quiet = _sense(rms=QUIET, speech=True, angle=1.08)  # the measured at-rest signal
    for i in range(50):
        ownership = engine.compose_tick(i * 0.02, sense=quiet)["ownership"]
        assert ownership["head"] == "feel-alive-1"


# --------------------------------------------------------------------------- #
# 5. Boundaries                                                               #
# --------------------------------------------------------------------------- #


def test_orient_module_imports_no_motion_transport_or_sdk() -> None:
    """``reachy.behavior`` stays a dependency-free leaf: the port CITES the
    donor's maths, it does not import the retiring loop."""
    from reachy.behavior import orient as orient_mod

    tree = ast.parse(inspect.getsource(orient_mod))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    forbidden = ("reachy_mini", "reachy.motion.listen", "reachy.motion.queue", "reachy.robot")
    for name in modules:
        assert not any(name.startswith(bad) for bad in forbidden), name


def test_orienting_is_bounded_on_the_rule_and_intent_admission_surfaces() -> None:
    """``orient-to-sound`` is a looping-default entry with no default duration,
    so both bounded-lifetime gates apply: a react rule needs ``duration_s`` and
    ``run_behavior`` needs an explicit duration. The standing, indefinite
    ``declare_goal`` surface stays the way to sustain it — this IS the goal.
    """
    entry = library.get("orient-to-sound")
    assert entry.looping is True
    assert entry.default_duration is None

    from reachy.behavior.intents import _validated_lifetime
    from reachy.cli._errors import CliError

    with pytest.raises(CliError):
        _validated_lifetime(entry, None)
    assert _validated_lifetime(entry, {"duration": 4.0}).duration == 4.0


def test_the_ladder_is_ordered_and_the_null_tier_plans_nothing() -> None:
    assert OrientTier.NONE.rank < OrientTier.NOISE.rank < OrientTier.SPEECH.rank
    assert OrientTier.SPEECH.rank < OrientTier.ENGAGED.rank
    params = OrientParams()
    assert plan_orient(OrientTier.NONE, 0.0, params, head_yaw=0.0, body_yaw=0.0) is None
    # A front-facing source needs no lean at all (the donor's own guard).
    front = math.pi / 2.0
    assert plan_orient(OrientTier.NOISE, front, params, head_yaw=0.0, body_yaw=0.0) is None


def test_a_degenerate_speed_still_yields_a_positive_duration() -> None:
    """The donor's ``_clamp_dur`` contract: a zero/NaN speed collapses to the
    floor rather than producing a zero-length (or backwards) move."""
    params = OrientParams(alert_speed=0.0, body_speed=0.0)
    target = plan_orient(OrientTier.ENGAGED, 0.0, params, head_yaw=0.0, body_yaw=0.0)
    assert target is not None and target.duration >= params.min_dur


def test_a_malformed_params_dict_falls_back_per_key_instead_of_crashing() -> None:
    """A rule can only supply numbers, but the behavior is defensive anyway: a
    junk or missing value uses the default for THAT key, never aborts the tick."""
    fn = OrientToSound()
    junk = {"gain": "not-a-number", "max_yaw": None, "deadband": 4.0}
    contribs = [fn(i * 0.02, junk, _sense(angle=0.0, transcript="hi", rms=LOUD)) for i in range(50)]
    assert any(c.head is not None for c in contribs)


def test_a_non_tier_verdict_or_a_broken_clock_is_treated_as_no_reading() -> None:
    fn = OrientToSound(gate=lambda sense, now, params: "very loud")  # not an OrientTier
    assert fn(0.0, {}, _sense(angle=0.0, rms=LOUD)).head is None
    other = OrientToSound()
    assert other(object(), {}, _sense(angle=0.0, rms=LOUD)).head is None  # type: ignore[arg-type]
    assert other.body_yaw == 0.0


def test_a_tier_without_a_bearing_commits_nothing() -> None:
    """A gate may open on evidence that carries no angle; the planner is then
    given nothing to steer toward and the behavior must simply not move."""
    fn = OrientToSound(gate=lambda sense, now, params: OrientTier.ENGAGED)
    contribs = _drive(fn, _sense(rms=LOUD), ticks=20)
    assert all(c.head is None and c.antennas is None for c in contribs)


def test_orienting_contributes_a_complete_head_offset() -> None:
    """A composed head contribution must carry all six axes (the engine
    composes a COMPLETE pose; a partial dict would be a silent hole)."""
    fn = make_orient_to_sound()
    contribs = _drive(fn, _sense(angle=0.0, transcript="hi", rms=LOUD), ticks=50)
    heads = [c.head for c in contribs if c.head is not None]
    assert heads
    assert all(set(h) == {"x", "y", "z", "roll", "pitch", "yaw"} for h in heads)
    assert all(isinstance(c, Contribution) for c in contribs)


# --------------------------------------------------------------------------- #
# 4. The latched-DoA guard (task t9)                                          #
# --------------------------------------------------------------------------- #
#
# t8's `CorroboratedGate` already carries the DONOR's actual latched-angle
# defence: `live = sound_present if sound_present is not None else (angle is not
# None)` in `ListenProducer.update` is exactly "prefer live mic energy over the
# latched angle", and `sound_present` is literally `rms > SnapDetector.min_rms`
# (`_audio` in `reachy/cli/_commands/listen.py`). The gate's `rms >= rms_floor`
# conjunct IS that check.
#
# What t8 could NOT close, and named in its own docstring, is the case its dwell
# conjunct actively REWARDS: a bearing that never changes is maximally "steady",
# so a wedged DoA feed inside a loud room votes YES on both conjuncts and parks
# the robot in a stuck stare. `rms` (the held media client's mic) and `doa_angle`
# (the daemon's HTTP route) are independent sources, so one can wedge while the
# other stays live. The guard below removes that perverse incentive.


def _cycle(fn, angles, *, speech=True, rms=LOUD, dt=0.02, t0=0.0):
    """Drive a gate over a sequence of bearings, returning each verdict."""
    return [
        fn(_sense(angle=a, speech=speech, rms=rms), t0 + i * dt, OrientParams())
        for i, a in enumerate(angles)
    ]


def test_a_frozen_bearing_is_refused_even_though_energy_and_dwell_both_pass() -> None:
    """The gap t8 named. A bit-identical angle is maximally steady, so dwell
    votes YES *because* of the fault; the guard must veto it anyway."""
    params = OrientParams()
    inner = CorroboratedGate()
    guarded = LatchedDoaGuard(CorroboratedGate())
    sense = _sense(angle=1.082, speech=True, rms=LOUD)
    ticks = int((params.latch_after_s + 1.0) / 0.02)

    inner_verdicts = [inner(sense, i * 0.02, params) for i in range(ticks)]
    guarded_verdicts = [guarded(sense, i * 0.02, params) for i in range(ticks)]

    assert OrientTier.SPEECH in inner_verdicts, "precondition: the inner gate admits"
    assert guarded_verdicts[-1] is OrientTier.NONE
    assert all(v is OrientTier.NONE for v in guarded_verdicts[-10:])


def test_the_guard_never_weakens_the_inner_gate() -> None:
    """Wrapping must be a pure narrowing: whatever the inner gate refuses stays
    refused, and no tier is ever raised."""
    params = OrientParams()
    inner = CorroboratedGate()
    guarded = LatchedDoaGuard(CorroboratedGate())
    angles = [0.3 + 0.05 * i for i in range(200)]  # never latches
    for i, angle in enumerate(angles):
        sense = _sense(angle=angle, speech=True, rms=QUIET)  # quiet: inner says NONE
        assert inner(sense, i * 0.02, params) is OrientTier.NONE
        assert guarded(sense, i * 0.02, params) is OrientTier.NONE


def test_a_steady_bearing_that_still_jitters_is_not_latched() -> None:
    """Steady is not frozen — the distinction the whole guard turns on. A real
    talker holds a bearing within the dwell tolerance while the mic array's own
    noise keeps the exact value moving, so admission must survive."""
    guarded = LatchedDoaGuard(CorroboratedGate())
    # ±0.005 rad of jitter: far inside dwell_tol_rad (0.12), never bit-identical.
    angles = [1.05 + 0.005 * ((i % 3) - 1) for i in range(int(30.0 / 0.02))]
    verdicts = _cycle(guarded, angles)
    assert verdicts[-1] is OrientTier.SPEECH
    assert all(v is not OrientTier.NONE for v in verdicts[-100:])


def test_the_measured_at_rest_trace_never_latches_this_guard() -> None:
    """The honesty test, and the reason this guard is small.

    The measured at-rest feed (120 samples / 60 s, quiet room —
    ``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 2)
    showed **35 distinct angles**: the exact value changes constantly, so on the
    daemon build we can actually observe, this guard NEVER fires. It defends a
    wedged feed, which is a state this build does not currently produce. Said
    plainly here rather than hidden, so nobody mistakes it for the thing that
    keeps a quiet room still — ``rms`` is what does that.
    """
    guarded = LatchedDoaGuard(CorroboratedGate())
    # 35 distinct values spanning the measured 0.000-3.124 rad range, cycled at
    # the DoA poll rate (5 Hz) across a multi-minute window.
    distinct = [3.124 * i / 34.0 for i in range(35)]
    angles = [distinct[(i // 10) % 35] for i in range(int(180.0 / 0.02))]
    verdicts = _cycle(guarded, angles)
    # The first call abstains while the NOISE attack is earned (t35's envelope);
    # from then on the guard never vetoes this trace.
    assert all(v is not OrientTier.NONE for v in verdicts[1:] if v is not None)
    assert not guarded.latched


def test_a_missing_bearing_clears_the_latch_rather_than_holding_it() -> None:
    params = OrientParams()
    guarded = LatchedDoaGuard(CorroboratedGate())
    sense = _sense(angle=1.082, speech=True, rms=LOUD)
    for i in range(int((params.latch_after_s + 1.0) / 0.02)):
        guarded(sense, i * 0.02, params)
    assert guarded.latched
    guarded(_sense(angle=None, rms=LOUD), 100.0, params)
    assert not guarded.latched


def test_a_raising_inner_gate_leaves_the_robot_still_rather_than_killing_the_tick() -> None:
    def boom(sense, now, params):
        raise RuntimeError("inner gate on fire")

    guarded = LatchedDoaGuard(boom)
    verdicts = _cycle(guarded, [0.1 * i for i in range(20)])
    assert all(v is OrientTier.NONE for v in verdicts)


def test_a_non_tier_inner_verdict_is_treated_as_no_reading() -> None:
    guarded = LatchedDoaGuard(lambda sense, now, params: "SPEECH")
    assert guarded(_sense(angle=0.5, rms=LOUD), 0.0, OrientParams()) is OrientTier.NONE


def test_the_latch_is_logged_on_entry_and_release_and_never_per_tick(caplog) -> None:
    """A 50 Hz guard that logged every latched tick would bury the journal, and
    a latch that logged nothing would be the silent no-op the senselog
    discipline exists to prevent. Exactly one line per EDGE."""
    params = OrientParams()
    guarded = LatchedDoaGuard(CorroboratedGate())
    sense = _sense(angle=1.082, speech=True, rms=LOUD)
    with caplog.at_level("INFO", logger="reachy.sense"):
        for i in range(int((params.latch_after_s + 4.0) / 0.02)):
            guarded(sense, i * 0.02, params)
        latched_lines = [r for r in caplog.records if "latched-doa" in r.getMessage()]
        assert len(latched_lines) == 1
        assert "reason=latched-doa" in latched_lines[0].getMessage()
        assert "stage=orient" in latched_lines[0].getMessage()
        caplog.clear()
        guarded(_sense(angle=2.0, speech=True, rms=LOUD), 100.0, params)
    released = [r for r in caplog.records if "latched-doa" in r.getMessage()]
    assert len(released) == 1
    assert "released" in released[0].getMessage()


def test_the_shipped_gate_is_the_guard_wrapping_the_corroborated_gate() -> None:
    """The guard must actually SHIP — a defence wired into nothing is a note."""
    fn = make_orient_to_sound()
    gate = fn._gate  # noqa: SLF001 - pinning the composed default is the point
    assert isinstance(gate, LatchedDoaGuard)
    assert isinstance(gate.inner, CorroboratedGate)


def test_a_frozen_bearing_never_reaches_the_head_through_the_shipped_behavior() -> None:
    """End to end: the wedged-feed stuck-stare the guard exists to prevent."""
    fn = make_orient_to_sound()
    sense = _sense(angle=0.0, speech=True, rms=LOUD)
    contribs = [fn(i * 0.02, {}, sense) for i in range(int(40.0 / 0.02))]
    assert all(c.head is None for c in contribs[-200:])


# --------------------------------------------------------------------------- #
# 6. The NOISE attack/release envelope (task t35)                             #
# --------------------------------------------------------------------------- #
#
# Live-verified defect (journal, 2026-07-21 01:25): a transient train (keyboard
# clicks) made the NOISE tier flap at tick rate — 22 `NONE->NOISE` opens in
# 1.3 s, one open/close per ~22 ms tick, with the #95 moving-floor gate CLOSED
# throughout. The rms reading genuinely alternates loud/quiet per tick on
# clicky sound, and the bare per-tick predicate (`doa finite AND rms >=
# rms_floor`) had zero temporal smoothing — SPEECH has an angular dwell, NOISE
# had nothing. The envelope under test adds the missing attack (consecutive
# loud calls before the tier opens) and release (continuous quiet before it
# closes) timing.

#: The tick period the 50 Hz engine calls the gate at (the measured burst's
#: ~22 ms open/close cadence).
TICK_S = 0.02


def test_the_envelope_knobs_ship_with_the_defect_derived_defaults() -> None:
    params = OrientParams()
    assert params.noise_attack_ticks == 2
    assert params.noise_release_s == pytest.approx(0.7)


def test_a_single_one_tick_click_never_opens_the_noise_tier() -> None:
    """One loud tick followed by quiet — the sharpest keyboard click — must
    never open the tier at all: the attack needs consecutive loud calls."""
    gate = CorroboratedGate()
    params = OrientParams()
    verdicts = [gate(_sense(angle=0.5, rms=LOUD), 0.0, params)]
    for i in range(1, 50):
        verdicts.append(gate(_sense(rms=QUIET), i * TICK_S, params))
    assert set(verdicts) == {OrientTier.NONE}


def test_sustained_sound_opens_noise_on_the_attack_tick_and_not_before() -> None:
    gate = CorroboratedGate()
    params = OrientParams()
    verdicts = [gate(_sense(angle=0.5, rms=LOUD), i * TICK_S, params) for i in range(10)]
    n = params.noise_attack_ticks
    assert all(v is OrientTier.NONE for v in verdicts[: n - 1])
    assert all(v is OrientTier.NOISE for v in verdicts[n - 1 :])


def test_a_per_tick_alternating_rms_train_opens_noise_once_and_holds() -> None:
    """The measured burst, replayed: an rms train alternating loud/quiet per
    tick opens NOISE at most ONCE and holds it through every sub-release gap —
    against the live robot's 22 opens in the same 1.3 s."""
    gate = CorroboratedGate()
    params = OrientParams()
    # Two loud ticks light the attack, then the per-tick alternation the click
    # train produced, for the rest of the measured 1.3 s burst.
    train = [LOUD, LOUD] + [QUIET if i % 2 else LOUD for i in range(63)]
    verdicts = [gate(_sense(angle=0.5, rms=rms), i * TICK_S, params) for i, rms in enumerate(train)]
    opens = sum(
        1
        for prev, cur in zip([OrientTier.NONE, *verdicts], verdicts)
        if prev is OrientTier.NONE and cur is not OrientTier.NONE
    )
    assert opens == 1
    first_open = next(i for i, v in enumerate(verdicts) if v is not OrientTier.NONE)
    assert all(v is OrientTier.NOISE for v in verdicts[first_open:])


def test_the_release_hold_closes_exactly_once_after_continuous_quiet() -> None:
    """After the sound ends the tier closes exactly once, ``noise_release_s``
    of CONTINUOUS quiet later — never on the first quiet tick."""
    gate = CorroboratedGate()
    params = OrientParams()
    for i in range(10):
        assert gate(_sense(angle=0.5, rms=LOUD), i * TICK_S, params) in (
            OrientTier.NONE,
            OrientTier.NOISE,
        )
    quiet_start = 10 * TICK_S
    times = [quiet_start + j * TICK_S for j in range(100)]
    verdicts = [gate(_sense(rms=QUIET), t, params) for t in times]
    closes = sum(
        1
        for prev, cur in zip(verdicts, verdicts[1:])
        if prev is not OrientTier.NONE and cur is OrientTier.NONE
    )
    assert closes == 1
    closed_at = next(t for t, v in zip(times, verdicts) if v is OrientTier.NONE)
    assert closed_at - quiet_start >= params.noise_release_s - 1e-9
    assert closed_at - quiet_start < params.noise_release_s + 3 * TICK_S
    first_none = verdicts.index(OrientTier.NONE)
    assert all(v is OrientTier.NOISE for v in verdicts[:first_none])
    assert all(v is OrientTier.NONE for v in verdicts[first_none:])


def test_a_fresh_loud_reading_during_the_hold_resets_the_quiet_timer() -> None:
    gate = CorroboratedGate()
    params = OrientParams()
    now = 0.0
    for i in range(5):
        now = i * TICK_S
        gate(_sense(angle=0.5, rms=LOUD), now, params)  # opens on the attack tick
    base = now
    for j in range(1, 30):  # 0.58 s of quiet — inside the release window
        assert gate(_sense(rms=QUIET), base + j * TICK_S, params) is OrientTier.NOISE
    now = base + 30 * TICK_S
    # One fresh loud tick: still NOISE, and the quiet timer starts over (the
    # bearing may update exactly as it does today).
    assert gate(_sense(angle=0.6, rms=LOUD), now, params) is OrientTier.NOISE
    quiet_start = now + TICK_S
    for k in range(34):  # another 0.66 s of quiet: inside a FRESH window
        assert gate(_sense(rms=QUIET), quiet_start + k * TICK_S, params) is OrientTier.NOISE
    late = quiet_start + params.noise_release_s + TICK_S
    assert gate(_sense(rms=QUIET), late, params) is OrientTier.NONE


def test_the_release_hold_reports_noise_not_the_higher_tier_it_fell_from() -> None:
    """A quiet gap has no meaningful bearing, so the hold keeps only the
    lean-only tier — a SPEECH nudge must not survive on silence."""
    gate = CorroboratedGate()
    params = OrientParams()
    verdict = OrientTier.NONE
    t = 0.0
    for i in range(50):
        t = i * TICK_S
        verdict = gate(_sense(angle=0.5, speech=True, rms=LOUD), t, params)
    assert verdict is OrientTier.SPEECH
    assert gate(_sense(rms=QUIET), t + TICK_S, params) is OrientTier.NOISE


def test_engagement_stays_immediate_even_from_a_cold_envelope() -> None:
    """The transcript fast-path takes no attack debounce: the very first call
    ENGAGEs, and a quiet tick right after rides the release hold."""
    gate = CorroboratedGate()
    params = OrientParams()
    assert gate(_sense(angle=0.5, transcript="hey reachy"), 0.0, params) is OrientTier.ENGAGED
    assert gate(_sense(rms=QUIET), TICK_S, params) is OrientTier.NOISE


def test_speech_escalation_is_unchanged_while_the_envelope_holds() -> None:
    """The envelope governs only NOISE open/close timing: a bearing that earns
    its dwell under sustained speech still escalates, hold or no hold."""
    gate = CorroboratedGate()
    params = OrientParams()
    for i in range(5):
        gate(_sense(angle=0.5, rms=LOUD), i * TICK_S, params)  # open the envelope
    base = 5 * TICK_S
    for j in range(10):  # a sub-release quiet gap, riding the hold
        assert gate(_sense(rms=QUIET), base + j * TICK_S, params) is OrientTier.NOISE
    t0 = base + 10 * TICK_S
    ticks = int(params.dwell_s / TICK_S) + 2
    verdicts = [
        gate(_sense(angle=1.0, speech=True, rms=LOUD), t0 + k * TICK_S, params)
        for k in range(ticks)
    ]
    assert verdicts[0] is OrientTier.NOISE  # held open — no re-attack after a gap
    assert verdicts[-1] is OrientTier.SPEECH  # the dwell evaluates exactly as today


def test_the_moving_floor_gated_zero_rms_rides_the_release_hold() -> None:
    """#95's moving-floor gate reports rms 0.0 while the robot's own lean runs.
    That is simply "quiet" riding the release hold, so the tier no longer
    collapses ~20 ms after its own lean starts — one lean per sound episode,
    held up to the release window."""
    gate = CorroboratedGate()
    params = OrientParams()
    for i in range(3):
        gate(_sense(angle=0.5, rms=LOUD), i * TICK_S, params)  # open
    verdict = gate(_sense(angle=0.5, rms=0.0), 3 * TICK_S, params)
    assert verdict is OrientTier.NOISE


def test_tier_transitions_drop_from_per_tick_to_per_episode_on_a_click_train() -> None:
    """The journal defect measured ~2 transitions per TICK (22 opens plus their
    closes in 1.3 s). Through the SHIPPED gate stack the same train yields
    exactly one open and one close — ~2 transitions per EPISODE — asserted via
    the verdict-transition count, not log scraping."""
    gate = LatchedDoaGuard(CorroboratedGate())
    params = OrientParams()
    train = [LOUD, LOUD] + [QUIET if i % 2 else LOUD for i in range(63)]
    train += [QUIET] * 60  # the episode ends: > noise_release_s of continuous quiet
    # The bearing wanders a little tick to tick (as the real feed does), so the
    # latched-DoA guard never reads it as frozen.
    verdicts = [
        gate(_sense(angle=0.5 + 0.001 * (i % 5), rms=rms), i * TICK_S, params)
        for i, rms in enumerate(train)
    ]
    transitions = sum(1 for a, b in zip(verdicts, verdicts[1:]) if a is not b)
    assert transitions == 2  # NONE->NOISE once, NOISE->NONE once
    assert verdicts[0] is OrientTier.NONE
    assert verdicts[-1] is OrientTier.NONE


def test_a_hostile_snapshot_mid_hold_fails_closed_and_resets_the_envelope() -> None:
    """The gate's never-raise contract extends to the envelope: an unreadable
    sense drops the hold outright rather than steering on stale state."""

    class Boom:
        doa_angle = property(lambda self: (_ for _ in ()).throw(RuntimeError("on fire")))
        transcript = None
        rms = None
        speech_detected = False

    gate = CorroboratedGate()
    params = OrientParams()
    for i in range(5):
        gate(_sense(angle=0.5, rms=LOUD), i * TICK_S, params)  # open
    assert gate(Boom(), 5 * TICK_S, params) is OrientTier.NONE  # type: ignore[arg-type]
    # ... and the hold did not survive the fault: the next quiet tick is NONE.
    assert gate(_sense(rms=QUIET), 6 * TICK_S, params) is OrientTier.NONE
