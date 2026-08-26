"""The ``lock_face`` / ``release_face`` intent kinds and the ``face-lock`` behavior.

Pins the t4 acceptance criteria:

1. ``lock_face`` with no (or a stale) ``Sense.face_bbox`` is a typed refusal
   (``no face known``) that admits NOTHING; with a face it admits ONE looping
   ``face-lock`` behavior that maps the bbox centre to a clamped yaw/pitch every
   tick, holds its last target while the bbox is momentarily absent, and adds
   ``feel-alive`` / ``orient-to-sound`` to the inhibited set.
2. Inhibition is LATER-WINS for the CALLER's names: a ``set_inhibition`` arriving
   while locked becomes the lock's new snapshot MINUS its own additions (which it
   re-asserts and still takes back on release), so a mind echoing back the set it
   just read never leaves the presence loop inhibited. With no intervening call,
   release restores the snapshot taken at lock time.
3. ``release_face`` is idempotent-safe when not locked, and when locked evicts
   the behavior, restores inhibitions per (2), and emits exactly ONE raw
   ``lock-released`` event through ``ctx.emit``.
4. The clamp is the behavior's OWN: a bbox pinned at the frame edge for 500
   ticks never commands beyond it, at any gain. Locking twice is a no-op.
5. Both kinds register into a :class:`~reachy.behavior.control.KindRegistry`
   exactly the way ``goto`` does, and the leaf module keeps ``goto_intent``'s
   import boundary (no ``control`` / ``intents`` import).

Deterministic throughout: a duck-typed recording ``ctx`` (mirroring
``tests/test_behavior_intents.py``'s ``_RecordingCtx``, plus the ``sense`` field
this consumer reads) and the REAL :class:`IntentDriver` for the inhibition
semantics — no robot, daemon, network, or LLM anywhere in this file.
"""

from __future__ import annotations

import ast
import inspect
from dataclasses import dataclass, field

import pytest

from reachy.behavior import face_lock as face_lock_mod
from reachy.behavior import library as behavior_library
from reachy.behavior.control import KindRegistry
from reachy.behavior.face_lock import (
    FACE_LOCK_BEHAVIOR,
    LOCK_FACE,
    LOCK_INHIBITS,
    LOCK_RELEASED_ACTION,
    MAX_FACE_AGE_S,
    RELEASE_FACE,
    FaceLockDriver,
    make_face_lock,
)
from reachy.behavior.intents import SET_INHIBITION, IntentDriver
from reachy.behavior.sense import EMPTY_SENSE, Sense

# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext carrying the ``sense`` snapshot this driver reads."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
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

    def evict(self, target: str) -> dict:
        self.evicts.append(target)
        self._active.discard(FACE_LOCK_BEHAVIOR)
        return {"ok": True, "op": "stop", "target": target}

    def active_names(self) -> set:
        return set(self._active)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


def _face(cx: float = 0.5, cy: float = 0.5, size: float = 0.2, age: float = 0.0) -> Sense:
    """A ``Sense`` whose single face is centred on ``(cx, cy)``."""
    return Sense(face_bbox=(cx - size / 2.0, cy - size / 2.0, size, size), face_age_s=age)


def _wire(intents: IntentDriver | None = None) -> tuple[FaceLockDriver, KindRegistry]:
    """Compose a driver + registry the way ``_compose_run_seam`` does."""
    if intents is None:
        intents = IntentDriver()
    driver = FaceLockDriver(
        inhibitions_getter=lambda: intents.inhibitions,
        inhibitions_setter=intents.set_inhibitions,
    )
    driver.register_into(intents.registry)
    intents.inhibition_observer = driver.notice_inhibition_replaced
    return driver, intents.registry


def _run(fn, params: dict, sense: Sense, ticks: int, *, start: float = 0.0, dt: float = 0.02):
    """Drive a contribution function for ``ticks`` ticks; return every head dict."""
    out = []
    for i in range(ticks):
        contribution = fn(start + i * dt, params, sense)
        out.append(contribution.head)
    return out


# --------------------------------------------------------------------------- #
# 1. lock_face — refusal, admission, tracking                                 #
# --------------------------------------------------------------------------- #


def test_lock_face_without_a_face_refuses_and_admits_nothing() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=EMPTY_SENSE)
    result = registry.dispatch({"op": LOCK_FACE}, ctx)
    assert result == {"ok": False, "op": LOCK_FACE, "error": "no face known"}
    assert ctx.admits == []


def test_lock_face_with_a_stale_face_refuses_and_admits_nothing() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face(age=MAX_FACE_AGE_S + 0.1))
    result = registry.dispatch({"op": LOCK_FACE}, ctx)
    assert result == {"ok": False, "op": LOCK_FACE, "error": "no face known"}
    assert ctx.admits == []


def test_lock_face_admits_one_looping_face_lock_behavior() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face(cx=0.7))
    result = registry.dispatch({"op": LOCK_FACE}, ctx)
    assert result["ok"] is True
    assert result["op"] == LOCK_FACE
    assert result["locked"] is True
    assert len(ctx.admits) == 1
    beh = ctx.admits[0]
    assert beh.name == FACE_LOCK_BEHAVIOR
    assert beh.lifetime.looping is True
    assert beh.lifetime.duration is None
    assert beh.wants_sense is True


def test_the_locked_behavior_maps_the_bbox_centre_to_yaw_and_pitch_every_tick() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face(cx=0.5, cy=0.5))
    registry.dispatch({"op": LOCK_FACE}, ctx)
    fn = ctx.admits[0].fn
    params = dict(ctx.admits[0].params)

    # A face to the robot's RIGHT (large x) turns the head right: yaw is
    # negative, since `sense.doa_angle_to_yaw`'s convention is +yaw = left.
    _run(fn, params, _face(cx=0.9, cy=0.5), 200)
    assert fn.target_yaw < 0.0
    assert fn.yaw == pytest.approx(fn.target_yaw, abs=1e-6)

    # A face HIGH in the frame (small y) tilts the head up: +pitch is up.
    _run(fn, params, _face(cx=0.5, cy=0.1), 200)
    assert fn.target_pitch > 0.0
    assert fn.pitch == pytest.approx(fn.target_pitch, abs=1e-6)

    # A centred face commands neither.
    heads = _run(fn, params, _face(cx=0.5, cy=0.5), 200)
    assert heads[-1]["yaw"] == pytest.approx(0.0, abs=1e-6)
    assert heads[-1]["pitch"] == pytest.approx(0.0, abs=1e-6)


def test_the_locked_behavior_holds_its_last_target_when_the_bbox_vanishes() -> None:
    fn = make_face_lock()
    params = behavior_library.get(FACE_LOCK_BEHAVIOR).default_params()
    _run(fn, params, _face(cx=0.9, cy=0.2), 200)
    held = (fn.target_yaw, fn.target_pitch)
    assert held[0] != 0.0
    assert held[1] != 0.0

    heads = _run(fn, params, EMPTY_SENSE, 200, start=4.0)
    assert (fn.target_yaw, fn.target_pitch) == held
    assert heads[-1]["yaw"] == pytest.approx(held[0], abs=1e-6)
    assert heads[-1]["pitch"] == pytest.approx(held[1], abs=1e-6)

    # A bbox that is merely STALE is held the same way — never released here.
    heads = _run(fn, params, _face(cx=0.1, age=MAX_FACE_AGE_S + 1.0), 200, start=8.0)
    assert (fn.target_yaw, fn.target_pitch) == held


def test_lock_adds_feel_alive_and_orient_to_sound_to_the_inhibited_set() -> None:
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})


# --------------------------------------------------------------------------- #
# 2. later-wins inhibition                                                    #
# --------------------------------------------------------------------------- #


def test_release_restores_the_snapshot_when_nothing_intervened() -> None:
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod", "shake"]}, ctx)
    snapshot = intents.inhibitions
    registry.dispatch({"op": LOCK_FACE}, ctx)
    registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == snapshot == frozenset({"nod", "shake"})


def test_a_set_inhibition_while_locked_wins_over_the_locks_own_additions() -> None:
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod"]}, ctx)  # set A
    registry.dispatch({"op": LOCK_FACE}, ctx)
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["shake"]}, ctx)  # set B
    registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"shake"})  # B, not A


def test_a_name_the_lock_did_not_add_survives_release() -> None:
    """The lock removes only what IT added — never a caller's own entry."""
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["feel-alive"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)
    registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"feel-alive"})


def test_a_replacement_never_turns_the_locks_own_additions_into_operator_held() -> None:
    """The live 2026-08-26 inertness bug: stay_silent/end_silence around a lock.

    The mind cannot tell the lock's own additions apart from its own, so its
    ``stay_silent`` merges ``speak`` into whatever ``state.json`` currently lists
    — which INCLUDES ``feel-alive`` / ``orient-to-sound`` the lock just added.
    A naive later-wins read of that replacement adopts the lock's own additions
    as operator-held, and release then leaves the presence loop inhibited: an
    inert robot. The new snapshot is therefore ``new_set - added``.
    """
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)  # snapshot A = {}
    assert intents.inhibitions == frozenset(LOCK_INHIBITS)

    # stay_silent: the mind echoes back what it read, plus `speak`.
    registry.dispatch(
        {"op": SET_INHIBITION, "behaviors": ["feel-alive", "orient-to-sound", "speak"]}, ctx
    )
    assert frozenset(LOCK_INHIBITS) <= intents.inhibitions  # lock still holds its own
    assert intents.inhibitions == frozenset({"speak", *LOCK_INHIBITS})

    # end_silence: `speak` drops away again.
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["feel-alive", "orient-to-sound"]}, ctx)
    assert frozenset(LOCK_INHIBITS) <= intents.inhibitions

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset()
    assert result["inhibitions"] == []


def test_an_operator_name_beside_the_locks_additions_still_survives_release() -> None:
    """Stripping the lock's additions from a replacement keeps the rest intact."""
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)  # snapshot A = {}
    registry.dispatch(
        {"op": SET_INHIBITION, "behaviors": ["feel-alive", "orient-to-sound", "nod"]}, ctx
    )
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod"})
    assert result["inhibitions"] == ["nod"]


def test_a_replacement_reclaims_a_lock_name_the_operator_already_held() -> None:
    """Ownership is RECOMPUTED per replacement, not frozen at acquisition.

    ``feel-alive`` was operator-inhibited BEFORE the lock, so the lock never
    "added" it. A later replacement that drops it would, with an acquisition-time
    ownership set, leave the presence loop free to drag the head off the face
    while the lock is still held — the lock's core invariant broken. The lock
    reclaims it instead, and gives it back on release.
    """
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["feel-alive"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)

    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod"]}, ctx)
    assert frozenset(LOCK_INHIBITS) <= intents.inhibitions
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod"})
    assert result["inhibitions"] == ["nod"]


def test_a_replacement_keeping_only_some_lock_names_hands_those_to_the_caller() -> None:
    """Keeping SOME of the lock's names is a deliberate choice, not an echo."""
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)

    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["feel-alive"]}, ctx)
    assert intents.inhibitions == frozenset(LOCK_INHIBITS)

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"feel-alive"})
    assert result["inhibitions"] == ["feel-alive"]


def test_the_live_set_stays_lock_complete_across_successive_replacements() -> None:
    """Every replacement while locked leaves the live set ``new | LOCK_INHIBITS``."""
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["orient-to-sound"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)

    for names in (["nod"], ["speak", "feel-alive"], []):
        registry.dispatch({"op": SET_INHIBITION, "behaviors": list(names)}, ctx)
        assert frozenset(LOCK_INHIBITS) <= intents.inhibitions
        assert intents.inhibitions == frozenset(names) | frozenset(LOCK_INHIBITS)

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset()
    assert result["inhibitions"] == []


# --------------------------------------------------------------------------- #
# 3. release_face                                                             #
# --------------------------------------------------------------------------- #


def test_release_face_when_not_locked_is_a_clean_no_op() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx()
    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert result == {
        "ok": True,
        "op": RELEASE_FACE,
        "released": False,
        "note": "not locked",
    }
    assert ctx.evicts == []
    assert ctx.events == []


def test_release_face_evicts_the_behavior_and_emits_one_lock_released_event() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)
    behavior_id = ctx.admits[0].id

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert result["ok"] is True
    assert result["op"] == RELEASE_FACE
    assert result["released"] is True
    assert ctx.evicts == [behavior_id]
    assert FACE_LOCK_BEHAVIOR not in ctx.active_names()

    assert len(ctx.events) == 1
    event = ctx.events[0]
    assert event["type"].split(".", 1)[1] == LOCK_RELEASED_ACTION
    assert event["behavior"] == FACE_LOCK_BEHAVIOR
    assert event["ts"] == ctx.now
    assert event["tick"] == ctx.tick


def test_release_then_lock_again_works() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)
    registry.dispatch({"op": RELEASE_FACE}, ctx)
    result = registry.dispatch({"op": LOCK_FACE}, ctx)
    assert result["locked"] is True
    assert "note" not in result
    assert len(ctx.admits) == 2


# --------------------------------------------------------------------------- #
# 4. the clamp, and locking twice                                             #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("overrides", [{}, {"yaw_gain": 400.0, "pitch_gain": 400.0}])
@pytest.mark.parametrize("corner", [(0.0, 0.0), (1.0, 0.0), (0.0, 1.0), (1.0, 1.0)])
def test_a_face_at_the_frame_edge_never_commands_beyond_the_clamp(overrides, corner) -> None:
    fn = make_face_lock()
    params = behavior_library.get(FACE_LOCK_BEHAVIOR).default_params()
    params.update(overrides)
    cx, cy = corner
    heads = _run(fn, params, _face(cx=cx, cy=cy, size=0.05), 500)
    assert len(heads) == 500
    for head in heads:
        assert abs(head["yaw"]) <= params["max_yaw"] + 1e-9
        assert abs(head["pitch"]) <= params["max_pitch"] + 1e-9
    # The clamp is the binding constraint at a corner, not an unreached ceiling.
    assert abs(heads[-1]["yaw"]) == pytest.approx(params["max_yaw"], abs=1e-6)
    assert abs(heads[-1]["pitch"]) == pytest.approx(params["max_pitch"], abs=1e-6)


def test_the_clamp_defaults_to_the_goto_envelope() -> None:
    params = behavior_library.get(FACE_LOCK_BEHAVIOR).default_params()
    assert params["max_yaw"] == 20.0
    assert params["max_pitch"] == 12.0


def test_the_clamp_is_overridable_per_lock() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE, "params": {"max_yaw": 5.0, "max_pitch": 3.0}}, ctx)
    beh = ctx.admits[0]
    assert beh.params["max_yaw"] == 5.0
    heads = _run(beh.fn, dict(beh.params), _face(cx=1.0, cy=1.0, size=0.05), 500)
    for head in heads:
        assert abs(head["yaw"]) <= 5.0 + 1e-9
        assert abs(head["pitch"]) <= 3.0 + 1e-9


def test_an_unknown_lock_param_is_refused_fail_closed() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    result = registry.dispatch({"op": LOCK_FACE, "params": {"nope": 1.0}}, ctx)
    assert result["ok"] is False
    assert result["op"] == LOCK_FACE
    assert "nope" in result["error"]
    assert ctx.admits == []


def test_locking_twice_neither_duplicates_the_behavior_nor_re_snapshots() -> None:
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    second = registry.dispatch({"op": LOCK_FACE}, ctx)
    assert second == {"ok": True, "op": LOCK_FACE, "locked": True, "note": "already locked"}
    assert len(ctx.admits) == 1
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod"})


# --------------------------------------------------------------------------- #
# 5. registration + import boundary                                           #
# --------------------------------------------------------------------------- #


def test_both_kinds_register_into_a_kind_registry_like_goto() -> None:
    intents = IntentDriver()
    _wire(intents)
    assert LOCK_FACE in intents.registry.kinds()
    assert RELEASE_FACE in intents.registry.kinds()
    # The four intent defaults are untouched by the addition.
    assert {"run_behavior", "declare_goal", "set_mode", SET_INHIBITION} <= set(
        intents.registry.kinds()
    )


def test_the_face_lock_library_entry_is_sensor_driven_and_head_only() -> None:
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    assert entry.wants_sense is True
    assert entry.channels == frozenset({"head"})
    assert entry.looping is True
    assert entry.default_duration is None
    assert entry.make_fn is not None


def test_face_lock_keeps_goto_intents_import_boundary() -> None:
    """The leaf must not import ``control`` or ``intents`` — composition wires it."""
    tree = ast.parse(inspect.getsource(face_lock_mod))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.col_offset == 0:
            modules.add(node.module)
    assert "reachy.behavior.control" not in modules
    assert "reachy.behavior.intents" not in modules


def test_the_runtime_seam_wires_both_kinds() -> None:
    from reachy.cli._commands import behavior as behavior_cmd

    source = inspect.getsource(behavior_cmd)
    assert "FaceLockDriver" in source
    assert "inhibition_observer" in source
