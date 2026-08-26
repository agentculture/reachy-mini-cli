"""t1 — DoaPoller.age_s + the one-shot 'look-at-sound' behavior.

Covers the t1 acceptance criteria:

1. ``DoaPoller.age_s(now)`` — seconds since the last GOOD (angle-bearing) DoA
   reading, ``None`` when never read, driven by an injected clock.
2. ``run_behavior name='look-at-sound'``: refused (no reading / stale) with
   ``{"ok": False, "error": "no recent sound direction"}`` and nothing
   admitted; admitted as a bounded one-shot with a fresh reading, aiming yaw
   at ``doa_angle_to_yaw(angle)`` clamped to ``max_yaw``.
3. The name-collision guard: ``'look-at-sound'`` is a LIBRARY name, distinct
   from the react rule id ``'look-toward-sound'`` shipped in
   ``default_rules.toml``.

Deterministic throughout: injected clocks, a duck-typed recording ``ctx``
(mirrors ``tests/test_behavior_intents.py``'s ``_RecordingCtx``, extended with
a ``sense`` field this module's admission path reads) and the real
``IntentDriver`` — no robot, daemon, network, or LLM anywhere in this file.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import pytest

from reachy.behavior import control as control_mod
from reachy.behavior import gaze
from reachy.behavior import library as behavior_library
from reachy.behavior.intents import INTENT_NAMESPACE, RUN_BEHAVIOR, IntentDriver
from reachy.behavior.library import LIBRARY
from reachy.behavior.rules import load_shipped_rules
from reachy.behavior.sense import EMPTY_SENSE, DoaPoller, Sense, doa_angle_to_yaw
from reachy.cli._errors import CliError

# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext (mirrors test_behavior_intents.py's fixture),
    extended with ``sense`` — the field ``_apply_run_behavior`` reads for
    ``look-at-sound``'s admission check."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = field(default_factory=lambda: EMPTY_SENSE)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        self._active.add(behavior.name)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        was_active = name in self._active
        self._active.discard(name)
        return {"ok": True, "op": "stop", "target": name, "stopped": [name] if was_active else []}

    def active_names(self) -> set:
        return set(self._active)


def _submit(root, **fields):
    return control_mod.submit(RUN_BEHAVIOR, namespace=INTENT_NAMESPACE, root=root, **fields)


# --------------------------------------------------------------------------- #
# 1. DoaPoller.age_s                                                          #
# --------------------------------------------------------------------------- #


def test_age_s_is_none_before_any_good_reading() -> None:
    poller = DoaPoller(lambda: EMPTY_SENSE, period=0.2)
    assert poller.age_s(0.0) is None
    poller(0.0)
    assert poller.age_s(0.0) is None  # the one read had no usable angle


def test_age_s_tracks_time_since_the_last_good_reading() -> None:
    poller = DoaPoller(lambda: Sense(doa_angle=1.0), period=0.2)
    poller(10.0)  # first poll -> a good reading at t=10.0
    assert poller.age_s(10.0) == 0.0
    assert poller.age_s(10.5) == 0.5
    assert poller.age_s(13.0) == 3.0


def test_age_s_keeps_the_last_good_time_across_a_later_failure() -> None:
    calls = {"n": 0}

    def _read():
        calls["n"] += 1
        if calls["n"] == 1:
            return Sense(doa_angle=0.5)
        raise RuntimeError("mic dropped")

    poller = DoaPoller(_read, period=1.0)
    poller(0.0)  # good reading at t=0.0
    assert poller.age_s(0.0) == 0.0
    poller(1.0)  # throttle period elapsed -> re-read -> raises -> EMPTY_SENSE
    assert poller(1.0).doa_angle is None  # the returned snapshot IS empty now
    assert poller.age_s(1.0) == 1.0  # but age still counts from the t=0.0 good read


def test_call_stamps_doa_age_s_onto_the_returned_sense() -> None:
    poller = DoaPoller(lambda: Sense(doa_angle=0.2), period=0.2)
    s = poller(5.0)
    assert s.doa_angle == 0.2
    assert s.doa_age_s == 0.0
    s2 = poller(5.05)  # within the throttle period -> cached angle, age still ticks
    assert s2.doa_angle == 0.2
    assert s2.doa_age_s == pytest.approx(0.05)


def test_call_with_no_good_reading_ever_returns_empty_sense_identity() -> None:
    """A poll that never sees a usable angle must still return the exact
    EMPTY_SENSE singleton (never a copy) — the pre-existing failure-handling
    contract (see tests/test_behavior.py::test_doa_poller_swallows_every_error)
    must survive the age-stamping addition unchanged."""

    def _no_mic() -> Sense:
        raise RuntimeError("audio device not available")

    poller = DoaPoller(_no_mic, period=0.2)
    assert poller(0.0) is EMPTY_SENSE


# --------------------------------------------------------------------------- #
# 2. read_perception threads doa_age_s through                                #
# --------------------------------------------------------------------------- #


def test_read_perception_carries_doa_age_s_from_base() -> None:
    from reachy.behavior.sense import NO_PROVIDERS, read_perception

    base = Sense(doa_angle=0.1, doa_age_s=3.5)
    snap = read_perception(NO_PROVIDERS, base=base)
    assert snap.doa_age_s == 3.5


# --------------------------------------------------------------------------- #
# 3. plan_look_at_sound — pure refusal/clamp logic                            #
# --------------------------------------------------------------------------- #


def test_plan_refuses_with_no_reading() -> None:
    assert gaze.plan_look_at_sound(EMPTY_SENSE) is None


def test_plan_refuses_when_stale() -> None:
    sense = Sense(doa_angle=0.5, doa_age_s=8.1)
    assert gaze.plan_look_at_sound(sense, max_age_s=8.0) is None


def test_plan_refuses_when_age_unknown() -> None:
    # A usable angle but no age reading at all (e.g. hand-built Sense) refuses,
    # same as "no reading": there is nothing to certify freshness against.
    sense = Sense(doa_angle=0.5, doa_age_s=None)
    assert gaze.plan_look_at_sound(sense, max_age_s=8.0) is None


def test_plan_admits_a_fresh_reading_within_the_clamp() -> None:
    # A bearing close to front (pi/2) yields a small yaw, well inside the clamp.
    angle = math.pi / 2.0 - 0.2
    sense = Sense(doa_angle=angle, doa_age_s=1.0)
    yaw = gaze.plan_look_at_sound(sense, max_age_s=8.0, max_yaw=35.0)
    assert yaw is not None
    assert yaw == pytest.approx(doa_angle_to_yaw(angle, 1.0))
    assert abs(yaw) <= 35.0


def test_plan_clamps_an_extreme_bearing() -> None:
    sense = Sense(doa_angle=0.0, doa_age_s=0.0)  # far left -> big raw yaw
    yaw = gaze.plan_look_at_sound(sense, max_age_s=8.0, max_yaw=10.0)
    assert yaw == 10.0  # clamped, not the raw ~90 deg


def test_plan_at_exactly_max_age_s_is_still_fresh() -> None:
    sense = Sense(doa_angle=0.3, doa_age_s=8.0)
    assert gaze.plan_look_at_sound(sense, max_age_s=8.0) is not None


# --------------------------------------------------------------------------- #
# 4. run_behavior name='look-at-sound' — refusal + admission (t1 c2)         #
# --------------------------------------------------------------------------- #


def test_run_behavior_refuses_with_no_reading(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-sound", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=EMPTY_SENSE)

    driver.on_tick(ctx)

    assert ctx.admits == []  # nothing admitted
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert result["error"] == "no recent sound direction"


def test_run_behavior_refuses_when_stale(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-sound", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    stale = Sense(doa_angle=0.4, doa_age_s=9.0)  # older than the 8.0s default
    ctx = _RecordingCtx(now=1.0, tick=1, sense=stale)

    driver.on_tick(ctx)

    assert ctx.admits == []
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert result["error"] == "no recent sound direction"


def test_run_behavior_admits_a_one_shot_aiming_at_the_clamped_yaw(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-sound", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    fresh = Sense(doa_angle=math.pi / 4.0, doa_age_s=0.5)  # front-left, fresh
    ctx = _RecordingCtx(now=1.0, tick=1, sense=fresh)

    driver.on_tick(ctx)

    assert len(ctx.admits) == 1
    beh = ctx.admits[0]
    assert beh.name == "look-at-sound"
    assert beh.channels == frozenset({"head"})
    assert beh.lifetime.looping is False
    assert beh.lifetime.duration == gaze.DEFAULT_DURATION_S
    raw_yaw = doa_angle_to_yaw(math.pi / 4.0, 1.0)
    expected_yaw = max(-gaze.DEFAULT_MAX_YAW, min(gaze.DEFAULT_MAX_YAW, raw_yaw))
    assert beh.params["yaw"] == expected_yaw
    assert abs(beh.params["yaw"]) <= gaze.DEFAULT_MAX_YAW

    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True
    assert result["op"] == RUN_BEHAVIOR
    assert result["name"] == "look-at-sound"


def test_run_behavior_admits_with_a_custom_max_yaw_param(tmp_path) -> None:
    _submit(
        tmp_path,
        name="look-at-sound",
        params={"max_yaw": 5.0},
        lifetime=None,
    )
    driver = IntentDriver(root=tmp_path)
    # A bearing whose raw yaw is far larger than 5 deg.
    fresh = Sense(doa_angle=0.0, doa_age_s=0.0)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=fresh)

    driver.on_tick(ctx)

    assert len(ctx.admits) == 1
    assert ctx.admits[0].params["yaw"] == 5.0


def test_contribution_eases_then_holds_the_target_yaw() -> None:
    """Unit-level check of the pure contribution fn, independent of admission."""
    entry = LIBRARY[gaze.NAME]
    params = entry.default_params()
    params["yaw"] = 20.0
    params["ease_s"] = 0.5
    fn = entry.fn
    start = fn(0.0, params, EMPTY_SENSE)
    mid = fn(0.25, params, EMPTY_SENSE)
    held = fn(0.5, params, EMPTY_SENSE)
    later = fn(2.0, params, EMPTY_SENSE)  # past the ease -> still holds
    assert start.head["yaw"] == 0.0
    assert 0.0 < mid.head["yaw"] < 20.0
    assert held.head["yaw"] == 20.0
    assert later.head["yaw"] == 20.0


# --------------------------------------------------------------------------- #
# 5. Name-collision guard                                                     #
# --------------------------------------------------------------------------- #


def test_look_at_sound_is_a_library_entry() -> None:
    assert "look-at-sound" in LIBRARY
    assert LIBRARY["look-at-sound"] is behavior_library.LIBRARY["look-at-sound"]


def test_look_at_sound_is_not_a_shipped_rule_id() -> None:
    rule_ids = {r.id for r in load_shipped_rules().react}
    assert "look-at-sound" not in rule_ids
    # the pre-existing, differently-named rule that admits the SUSTAINED
    # sibling (orient-to-sound) is still there, unaffected:
    assert "look-toward-sound" in rule_ids


def test_look_at_sound_default_class_is_stoppable() -> None:
    from reachy.behavior.model import StopClass

    assert LIBRARY["look-at-sound"].default_class is StopClass.STOPPABLE


# --------------------------------------------------------------------------- #
# 6. plan_look_at_face — pure refusal/clamp logic (t3 c1)                     #
# --------------------------------------------------------------------------- #


def test_face_plan_refuses_with_no_bbox() -> None:
    assert gaze.plan_look_at_face(EMPTY_SENSE) is None


def test_face_plan_refuses_when_stale() -> None:
    sense = Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=1.6)
    assert gaze.plan_look_at_face(sense, max_age_s=1.5) is None


def test_face_plan_refuses_when_age_unknown() -> None:
    # A bbox with no age reading at all refuses, mirroring plan_look_at_sound's
    # "nothing to certify freshness against" stance for its sibling one-shot.
    sense = Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=None)
    assert gaze.plan_look_at_face(sense, max_age_s=1.5) is None


def test_face_plan_at_exactly_max_age_s_is_still_fresh() -> None:
    sense = Sense(face_bbox=(0.5, 0.5, 0.0, 0.0), face_age_s=1.5)
    assert gaze.plan_look_at_face(sense, max_age_s=1.5) is not None


def test_face_plan_centre_bbox_yields_no_offset() -> None:
    sense = Sense(face_bbox=(0.5, 0.5, 0.0, 0.0), face_age_s=0.0)
    yaw, pitch = gaze.plan_look_at_face(sense)
    assert yaw == pytest.approx(0.0)
    assert pitch == pytest.approx(0.0)


@pytest.mark.parametrize(
    "bbox, expected_yaw, expected_pitch",
    [
        # top-left corner (cx=0, cy=0): +yaw (left), +pitch (up), both clamped.
        ((0.0, 0.0, 0.0, 0.0), 20.0, 12.0),
        # top-right corner (cx=1, cy=0): -yaw (right), +pitch (up).
        ((1.0, 0.0, 0.0, 0.0), -20.0, 12.0),
        # bottom-left corner (cx=0, cy=1): +yaw (left), -pitch (down).
        ((0.0, 1.0, 0.0, 0.0), 20.0, -12.0),
        # bottom-right corner (cx=1, cy=1): -yaw (right), -pitch (down).
        ((1.0, 1.0, 0.0, 0.0), -20.0, -12.0),
    ],
)
def test_face_plan_aims_within_clamp_at_the_four_corners(
    bbox, expected_yaw, expected_pitch
) -> None:
    sense = Sense(face_bbox=bbox, face_age_s=0.0)
    yaw, pitch = gaze.plan_look_at_face(sense, max_yaw=20.0, max_pitch=12.0)
    assert yaw == pytest.approx(expected_yaw)
    assert pitch == pytest.approx(expected_pitch)
    assert abs(yaw) <= 20.0
    assert abs(pitch) <= 12.0


# --------------------------------------------------------------------------- #
# 7. run_behavior name='look-at-face' — refusal + admission (t3 c2)          #
# --------------------------------------------------------------------------- #


def test_face_run_behavior_refuses_with_no_bbox(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-face", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=EMPTY_SENSE)

    driver.on_tick(ctx)

    assert ctx.admits == []
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert result["error"] == "no face known"


def test_face_run_behavior_refuses_when_stale(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-face", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    stale = Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=2.0)  # > 1.5s default
    ctx = _RecordingCtx(now=1.0, tick=1, sense=stale)

    driver.on_tick(ctx)

    assert ctx.admits == []
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert result["error"] == "no face known"


def test_face_run_behavior_admits_a_one_shot_aiming_at_the_clamped_target(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-face", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    fresh = Sense(face_bbox=(0.0, 0.0, 0.0, 0.0), face_age_s=0.2)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=fresh)

    driver.on_tick(ctx)

    assert len(ctx.admits) == 1
    beh = ctx.admits[0]
    assert beh.name == "look-at-face"
    assert beh.channels == frozenset({"head"})
    assert beh.stop_class is not None
    from reachy.behavior.model import StopClass

    assert beh.stop_class is StopClass.STOPPABLE
    assert beh.lifetime.looping is False
    assert beh.lifetime.duration == gaze.DEFAULT_DURATION_S_FACE
    assert beh.params["yaw"] == pytest.approx(20.0)
    assert beh.params["pitch"] == pytest.approx(12.0)

    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True
    assert result["op"] == RUN_BEHAVIOR
    assert result["name"] == "look-at-face"


def test_face_run_behavior_admits_with_custom_max_yaw_and_max_pitch(tmp_path) -> None:
    _submit(
        tmp_path,
        name="look-at-face",
        params={"max_yaw": 5.0, "max_pitch": 3.0},
        lifetime=None,
    )
    driver = IntentDriver(root=tmp_path)
    fresh = Sense(face_bbox=(0.0, 0.0, 0.0, 0.0), face_age_s=0.0)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=fresh)

    driver.on_tick(ctx)

    assert len(ctx.admits) == 1
    assert ctx.admits[0].params["yaw"] == pytest.approx(5.0)
    assert ctx.admits[0].params["pitch"] == pytest.approx(3.0)


def test_face_contribution_eases_then_holds_the_target(tmp_path) -> None:
    """Unit-level check of the pure contribution fn, independent of admission."""
    entry = LIBRARY[gaze.NAME_FACE]
    params = entry.default_params()
    params["yaw"] = 20.0
    params["pitch"] = 12.0
    params["ease_s"] = 0.5
    fn = entry.fn
    start = fn(0.0, params, EMPTY_SENSE)
    mid = fn(0.25, params, EMPTY_SENSE)
    held = fn(0.5, params, EMPTY_SENSE)
    later = fn(2.0, params, EMPTY_SENSE)  # past the ease -> still holds
    assert start.head["yaw"] == 0.0
    assert start.head["pitch"] == 0.0
    assert 0.0 < mid.head["yaw"] < 20.0
    assert 0.0 < mid.head["pitch"] < 12.0
    assert held.head["yaw"] == pytest.approx(20.0)
    assert held.head["pitch"] == pytest.approx(12.0)
    assert later.head["yaw"] == pytest.approx(20.0)
    assert later.head["pitch"] == pytest.approx(12.0)


# --------------------------------------------------------------------------- #
# 8. Library / rule-id membership for both one-shots (t3 c3)                  #
# --------------------------------------------------------------------------- #


def test_look_at_face_is_a_library_entry() -> None:
    assert "look-at-face" in LIBRARY
    assert LIBRARY["look-at-face"] is behavior_library.LIBRARY["look-at-face"]


def test_look_at_face_is_not_a_shipped_rule_id() -> None:
    rule_ids = {r.id for r in load_shipped_rules().react}
    assert "look-at-face" not in rule_ids


def test_look_at_face_default_class_is_stoppable() -> None:
    from reachy.behavior.model import StopClass

    assert LIBRARY["look-at-face"].default_class is StopClass.STOPPABLE


def test_both_gaze_one_shot_names_present_in_library() -> None:
    assert {"look-at-sound", "look-at-face"} <= set(LIBRARY)


# --------------------------------------------------------------------------- #
# Domain validation: NaN / inf / negative overrides (PR #172 review)           #
# --------------------------------------------------------------------------- #


def _refusal(tmp_path, *, name: str, params: dict) -> dict:
    cmd_id = _submit(tmp_path, name=name, params=params, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=Sense(doa_angle=0.4, doa_age_s=9.0))
    driver.on_tick(ctx)
    assert ctx.admits == []
    return control_mod.await_result(cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2)


def test_a_nan_max_age_is_refused_rather_than_admitting_arbitrarily_stale_data(tmp_path) -> None:
    """`age > NaN` is False, so a NaN freshness limit certifies ANY reading fresh.

    stdlib `json` decodes a bare `NaN`, so this reaches the planners from the
    intents spool — the type check ("is it a number?") passes it through.
    """
    result = _refusal(tmp_path, name="look-at-sound", params={"max_age_s": float("nan")})

    assert result["ok"] is False
    assert "max_age_s" in result["error"]
    assert "finite" in result["error"]


def test_an_infinite_max_age_is_refused_too(tmp_path) -> None:
    result = _refusal(tmp_path, name="look-at-sound", params={"max_age_s": float("inf")})
    assert result["ok"] is False
    assert "max_age_s" in result["error"]


def test_a_negative_clamp_is_refused_rather_than_inverting_the_target(tmp_path) -> None:
    """`_clamp(v, -5)` returns `+5`: a negative clamp forces the WRONG angle."""
    result = _refusal(tmp_path, name="look-at-sound", params={"max_yaw": -5.0})

    assert result["ok"] is False
    assert "max_yaw" in result["error"]


def test_a_nan_clamp_never_reaches_the_head_contribution(tmp_path) -> None:
    result = _refusal(tmp_path, name="look-at-face", params={"max_pitch": float("nan")})
    assert result["ok"] is False
    assert "max_pitch" in result["error"]


def test_a_valid_override_is_still_accepted(tmp_path) -> None:
    cmd_id = _submit(tmp_path, name="look-at-sound", params={"max_age_s": 2.0}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1, sense=Sense(doa_angle=math.pi / 4.0, doa_age_s=0.5))

    driver.on_tick(ctx)

    assert len(ctx.admits) == 1
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True


def test_the_cli_set_path_refuses_a_non_finite_value_too() -> None:
    """`float("nan")` parses cleanly, so the string path needs the same guard."""
    entry = LIBRARY["look-at-sound"]

    with pytest.raises(CliError) as excinfo:
        behavior_library.resolve_params(entry, {"max_age_s": "nan"})

    assert "finite" in str(excinfo.value.message)


def test_every_gaze_and_lock_clamp_declares_its_domain() -> None:
    """The bounds live on the Param, so one validator serves every surface."""
    for name in ("look-at-sound", "look-at-face", "face-lock"):
        entry = LIBRARY[name]
        for key, param in entry.params.items():
            if key in {"pitch", "roll", "z"}:
                continue  # signed by nature: an offset, not a magnitude
            assert param.minimum == 0.0, f"{name}.{key} declares no lower bound"
