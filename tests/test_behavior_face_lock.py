"""The ``lock_face`` / ``release_face`` intent kinds and the ``face-lock`` behavior.

Pins the t4 acceptance criteria:

1. ``lock_face`` with no (or a stale) ``Sense.face_bbox`` is a typed refusal
   (``no face known``) that admits NOTHING; with a face it admits ONE looping
   ``face-lock`` behavior that maps the bbox centre to a clamped yaw/pitch every
   tick, holds its last target while the bbox is momentarily absent, and adds
   ``orient-to-sound`` to the inhibited set (``feel-alive`` left it in #183 —
   the lock claims ``head`` + ``body_yaw`` and leaves the antennas to the base
   layer instead).
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
from pathlib import Path

import pytest

from reachy.behavior import face_lock as face_lock_mod
from reachy.behavior import intents as intents_mod
from reachy.behavior import library as behavior_library
from reachy.behavior.control import KindRegistry
from reachy.behavior.engine import BASE_LAYER_NAME, Engine, EngineConfig
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
from reachy.cli._errors import CliError

# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext carrying the ``sense`` snapshot this driver reads."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    #: The pose the engine streamed this tick (``TickContext.pose``) — the lock
    #: reads its ``body_yaw`` to hold the body where it already was.
    pose: dict | None = None
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

    # A CENTRED face asks for no correction, so the aim HOLDS where it is —
    # incremental, not absolute: "the face is dead centre" is not a request to
    # return to neutral, it is confirmation that the current angle is right.
    before = (fn.target_yaw, fn.target_pitch)
    heads = _run(fn, params, _face(cx=0.5, cy=0.5), 200, start=100.0)
    assert (fn.target_yaw, fn.target_pitch) == pytest.approx(before, abs=1e-6)
    assert heads[-1]["yaw"] == pytest.approx(before[0], abs=1e-6)
    assert heads[-1]["pitch"] == pytest.approx(before[1], abs=1e-6)

    # And from neutral, a centred face commands nothing at all.
    fresh = make_face_lock()
    heads = _run(fresh, params, _face(cx=0.5, cy=0.5), 200)
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
    — which INCLUDES every :data:`LOCK_INHIBITS` name the lock just added.
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
    registry.dispatch({"op": SET_INHIBITION, "behaviors": [*LOCK_INHIBITS, "speak"]}, ctx)
    assert frozenset(LOCK_INHIBITS) <= intents.inhibitions  # lock still holds its own
    assert intents.inhibitions == frozenset({"speak", *LOCK_INHIBITS})

    # end_silence: `speak` drops away again.
    registry.dispatch({"op": SET_INHIBITION, "behaviors": list(LOCK_INHIBITS)}, ctx)
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
    registry.dispatch({"op": SET_INHIBITION, "behaviors": [*LOCK_INHIBITS, "nod"]}, ctx)
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod"})
    assert result["inhibitions"] == ["nod"]


def test_a_replacement_reclaims_a_lock_name_the_operator_already_held() -> None:
    """Ownership is RECOMPUTED per replacement, not frozen at acquisition.

    ``orient-to-sound`` was operator-inhibited BEFORE the lock, so the lock never
    "added" it. A later replacement that drops it would, with an acquisition-time
    ownership set, leave the presence loop free to drag the head off the face
    while the lock is still held — the lock's core invariant broken. The lock
    reclaims it instead, and gives it back on release.
    """
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["orient-to-sound"]}, ctx)
    registry.dispatch({"op": LOCK_FACE}, ctx)

    registry.dispatch({"op": SET_INHIBITION, "behaviors": ["nod"]}, ctx)
    assert frozenset(LOCK_INHIBITS) <= intents.inhibitions
    assert intents.inhibitions == frozenset({"nod", *LOCK_INHIBITS})

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset({"nod"})
    assert result["inhibitions"] == ["nod"]


def test_with_one_lock_name_a_replacement_naming_it_is_always_the_echo() -> None:
    """Keeping SOME of the lock's names is a deliberate choice, not an echo.

    With a single-name :data:`LOCK_INHIBITS` (#183) there is no proper subset to
    keep, so any replacement naming it carries EVERY name the lock holds and is
    therefore the echo case: the lock keeps its own addition and takes it back
    on release. The partial-keep branch stays in the code because the set is
    "whatever arbitration cannot handle", not "one behavior" — a second name
    would make it reachable again.
    """
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)

    registry.dispatch({"op": SET_INHIBITION, "behaviors": list(LOCK_INHIBITS)}, ctx)
    assert intents.inhibitions == frozenset(LOCK_INHIBITS)

    result = registry.dispatch({"op": RELEASE_FACE}, ctx)
    assert intents.inhibitions == frozenset()
    assert result["inhibitions"] == []


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


@pytest.mark.parametrize("overrides", [{}, {"fov_h": 360.0, "fov_v": 360.0, "damping": 1.0}])
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


def test_the_face_lock_library_entry_is_sensor_driven_and_claims_head_and_body() -> None:
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    assert entry.wants_sense is True
    # `antennas` is deliberately absent: the base layer keeps it (#183).
    assert entry.channels == frozenset({"head", "body_yaw"})
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


# --------------------------------------------------------------------------- #
# 6. the INCREMENTAL aim (issue #181, spec c2/c3/c5, honesty h2/h3/h4)         #
# --------------------------------------------------------------------------- #
#
# The old aim was ABSOLUTE — `-(cx-0.5)*2*gain` — i.e. a proportional loop of
# gain `2*gain/FOV` (~0.31 on the Wireless camera), so the head settled a third
# of the way to the face and stayed there. These tests drive the gaze against a
# SIMULATED PINHOLE CAMERA that reacts to the commanded angle, which is the only
# way an open-loop settle and a closed-loop convergence can be told apart.


_HFOV = 87.0
_VFOV = 57.0


def _seen_bbox(
    bearing_yaw: float,
    bearing_pitch: float,
    yaw: float,
    pitch: float,
    *,
    size: float = 0.2,
) -> tuple[float, float, float, float]:
    """Where a face at ``bearing_*`` lands in a frame taken while commanding ``yaw``.

    A pinhole camera bolted to the head, small-angle-linearised across the frame
    (exact enough for a convergence property, and the same linearisation the
    lock's own inverse makes). The repo's signs: ``+yaw`` is LEFT, so a face to
    the left of the camera axis lands at a SMALLER x; ``+pitch`` is up, so a face
    above the axis lands at a SMALLER y.
    """
    cx = 0.5 - (bearing_yaw - yaw) / _HFOV
    cy = 0.5 - (bearing_pitch - pitch) / _VFOV
    return (cx - size / 2.0, cy - size / 2.0, size, size)


def _track(
    fn,
    params: dict,
    *,
    bearing_yaw: float,
    bearing_pitch: float = 0.0,
    detections: tuple[float, ...],
    latency_s: float = 0.0,
    duration_s: float,
    dt: float = 0.02,
) -> list[tuple[float, float, float]]:
    """Run the gaze against the simulated camera; return ``(t, yaw, pitch)`` each tick.

    A detection is PUBLISHED at each time in *detections* and republished (with a
    growing ``face_age_s``, exactly as :mod:`reachy.behavior.face_sense` holds a
    bbox for its TTL) on every tick until the next one. ``latency_s`` is the
    detection latency: the frame behind a detection published at ``t`` was
    captured at ``t - latency_s``, and ``face_age_s`` reports the age since
    CAPTURE, which is what the ring lookup keys on.
    """
    out: list[tuple[float, float, float]] = []
    published: tuple[float, tuple[float, float, float, float]] | None = None
    pending = list(detections)
    yaw, pitch = 0.0, 0.0
    capture_pose: dict[float, tuple[float, float]] = {}
    ticks = int(round(duration_s / dt)) + 1
    for i in range(ticks):
        t = i * dt
        # Remember the pose at every instant, so a detection can be built from
        # the pose that was actually commanded when its frame was captured.
        capture_pose[round(t, 6)] = (yaw, pitch)
        while pending and pending[0] <= t + 1e-9:
            publish_at = pending.pop(0)
            captured_at = publish_at - latency_s
            base = capture_pose.get(round(max(0.0, captured_at), 6), (yaw, pitch))
            published = (
                captured_at,
                _seen_bbox(bearing_yaw, bearing_pitch, base[0], base[1]),
            )
        if published is None:
            sense = EMPTY_SENSE
        else:
            captured_at, bbox = published
            sense = Sense(face_bbox=bbox, face_age_s=max(0.0, t - captured_at))
        head = fn(t, params, sense).head
        yaw, pitch = head["yaw"], head["pitch"]
        out.append((t, yaw, pitch))
    return out


def _lock_params(**overrides) -> dict:
    params = behavior_library.get(FACE_LOCK_BEHAVIOR).default_params()
    params.update(overrides)
    return params


def test_the_defaults_are_the_wireless_cameras_fov_and_the_shipped_damping() -> None:
    params = _lock_params()
    assert params["fov_h"] == 87.0
    assert params["fov_v"] == 57.0
    assert params["damping"] == 0.7


def test_the_aim_converges_on_a_face_30_deg_off_axis_without_overshooting() -> None:
    """h2: within 2 deg of the bearing after two detection cycles, no overshoot.

    Damping 0.7 leaves 30% of the error per detection, so a 30 deg bearing is
    9.0 deg out after ONE application, 2.7 after two and 0.81 after three. Two
    detection CYCLES of elapsed time (t=0, 1.0 and 2.0 s) is three applications
    and lands at 0.81 deg — comfortably inside the 2 deg the honesty condition
    names. The clamp is widened for the test: the shipped 20 deg envelope
    physically cannot reach a 30 deg bearing, and the property under test is the
    loop, not the envelope.
    """
    fn = make_face_lock()
    params = _lock_params(max_yaw=45.0, max_pitch=45.0)
    track = _track(fn, params, bearing_yaw=30.0, detections=(0.0, 1.0, 2.0), duration_s=2.5)

    final_yaw = track[-1][1]
    assert abs(30.0 - final_yaw) <= 2.0
    # No overshoot: the error keeps its sign for the whole run, so the head
    # never passes the face and comes back.
    assert all(30.0 - yaw > 0.0 for _t, yaw, _p in track)
    assert max(yaw for _t, yaw, _p in track) <= 30.0 + 1e-9
    # And it is genuinely converging, not settling at a fraction: the old
    # absolute map would have parked at 2*20/87 = 0.23 of the bearing.
    assert final_yaw > 0.9 * 30.0


def test_the_aim_converges_in_pitch_too() -> None:
    fn = make_face_lock()
    params = _lock_params(max_yaw=45.0, max_pitch=45.0)
    track = _track(
        fn,
        params,
        bearing_yaw=0.0,
        bearing_pitch=15.0,
        detections=(0.0, 1.0, 2.0),
        duration_s=2.5,
    )
    final_pitch = track[-1][2]
    assert abs(15.0 - final_pitch) <= 2.0
    assert all(15.0 - pitch > 0.0 for _t, _y, pitch in track)


def test_a_300_ms_detection_latency_still_converges_in_two_cycles() -> None:
    """h3/c42: the ring is indexed by CAPTURE time, so a slewing head is not double-counted.

    The slew is deliberately slow (20 deg/s) so the head is still MOVING when
    the next frame is captured — the only regime in which basing the increment
    on the current pose rather than the capture-time pose differs. It differs by
    overshooting: at t=1.0 s the naive base (the pose now, 20 deg) plus 0.7 of an
    error measured at 14 deg targets 31.2 deg, past a 30 deg face; the ring's
    capture-time base targets 25.2 deg and keeps closing from below.
    """
    fn = make_face_lock()
    params = _lock_params(max_yaw=45.0, max_pitch=45.0, slew=20.0)
    track = _track(
        fn,
        params,
        bearing_yaw=30.0,
        detections=(0.0, 1.0, 2.0),
        latency_s=0.3,
        duration_s=2.5,
    )

    assert abs(30.0 - track[-1][1]) <= 2.0
    assert all(30.0 - yaw > 0.0 for _t, yaw, _p in track)
    assert fn.target_yaw <= 30.0 + 1e-9


def test_a_republished_stale_reading_does_not_walk_the_target() -> None:
    """c2: one detection moves the target ONCE, however many ticks republish it.

    Between detections the producer holds the same bbox and its ``face_age_s``
    grows, so the derived capture time stands still. Re-applying the increment
    every tick would march the head off the face within a second.
    """
    fn = make_face_lock()
    params = _lock_params()
    bbox = _seen_bbox(15.0, 0.0, 0.0, 0.0)

    first = fn(0.0, params, Sense(face_bbox=bbox, face_age_s=0.0))
    target_after_one = fn.target_yaw
    assert target_after_one == pytest.approx(15.0 * 0.7, abs=1e-6)

    for i in range(1, 100):  # 2 s of republication at 50 Hz
        t = i * 0.02
        fn(t, params, Sense(face_bbox=bbox, face_age_s=t))
    assert fn.target_yaw == pytest.approx(target_after_one, abs=1e-9)
    assert first.head["yaw"] == pytest.approx(0.0, abs=1e-9)


def test_the_ring_falls_back_to_the_oldest_sample_it_still_holds() -> None:
    """An age older than the ring is answered honestly, never with the current pose."""
    fn = make_face_lock()
    params = _lock_params(max_yaw=45.0, max_age=60.0)
    # Drive the head somewhere with one detection, then hand it a reading whose
    # capture time predates every sample in the ring.
    _track(fn, params, bearing_yaw=20.0, detections=(0.0,), duration_s=1.0)
    moved = fn.yaw
    assert moved > 1.0

    oldest = fn._ring[0]
    fn(1.02, params, Sense(face_bbox=_seen_bbox(20.0, 0.0, 0.0, 0.0), face_age_s=50.0))
    # Based on the oldest remembered command (the start of the run), not on the
    # pose it happens to hold now.
    assert fn.target_yaw == pytest.approx(oldest[1] + 20.0 * 0.7, abs=1e-6)


def test_a_snapshot_with_no_age_at_all_falls_back_to_a_changed_bbox() -> None:
    """An older/partial provider still tracks — one application per new bbox."""

    class _NoAge:
        def __init__(self, bbox):
            self.face_bbox = bbox

    fn = make_face_lock()
    params = _lock_params(max_yaw=45.0)
    bbox = _seen_bbox(10.0, 0.0, 0.0, 0.0)
    for i in range(50):
        fn(i * 0.02, params, _NoAge(bbox))
    once = fn.target_yaw
    assert once == pytest.approx(10.0 * 0.7, abs=1e-6)

    moved = _seen_bbox(10.0, 0.0, 5.0, 0.0)
    fn(1.02, params, _NoAge(moved))
    assert fn.target_yaw != once


# --------------------------------------------------------------------------- #
# 7. the params are a validated DOMAIN on both override paths                 #
# --------------------------------------------------------------------------- #


def test_the_old_gain_params_are_gone_from_the_entry() -> None:
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    assert "yaw_gain" not in entry.params
    assert "pitch_gain" not in entry.params
    assert {"fov_h", "fov_v", "damping"} <= set(entry.params)


@pytest.mark.parametrize("key", ["fov_h", "fov_v", "damping"])
@pytest.mark.parametrize("bad", ["nan", "inf", "-1"])
def test_the_cli_set_path_refuses_a_non_finite_or_negative_value(key, bad) -> None:
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    with pytest.raises(CliError) as excinfo:
        behavior_library.resolve_params(entry, {key: bad})
    assert key in str(excinfo.value.message)


@pytest.mark.parametrize("key", ["fov_h", "fov_v", "damping"])
@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0])
def test_the_intent_params_path_refuses_a_non_finite_or_negative_value(key, bad) -> None:
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    with pytest.raises(CliError) as excinfo:
        intents_mod._validated_params(entry, {key: bad})
    assert key in str(excinfo.value.message)


@pytest.mark.parametrize("key", ["fov_h", "fov_v"])
def test_a_zero_field_of_view_is_refused_on_both_paths(key) -> None:
    """A zero FOV reads EVERY face as dead centre — a lock that can never aim."""
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    with pytest.raises(CliError):
        behavior_library.resolve_params(entry, {key: "0"})
    with pytest.raises(CliError):
        intents_mod._validated_params(entry, {key: 0.0})


def test_damping_above_one_is_refused() -> None:
    """Above 1.0 the loop over-corrects by construction: it would oscillate."""
    entry = behavior_library.get(FACE_LOCK_BEHAVIOR)
    with pytest.raises(CliError):
        intents_mod._validated_params(entry, {"damping": 1.5})
    # Zero damping is legal (a lock that holds still), and so is 1.0.
    assert intents_mod._validated_params(entry, {"damping": 0.0})["damping"] == 0.0
    assert intents_mod._validated_params(entry, {"damping": 1.0})["damping"] == 1.0


def test_a_lock_face_payload_naming_the_new_params_reaches_the_gaze() -> None:
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch(
        {"op": LOCK_FACE, "params": {"fov_h": 120.0, "fov_v": 70.0, "damping": 1.0}}, ctx
    )
    beh = ctx.admits[0]
    assert beh.params["fov_h"] == 120.0
    assert beh.params["damping"] == 1.0

    beh.fn(0.0, dict(beh.params), _face(cx=0.75, cy=0.5))
    # damping 1.0 through a 120 deg FOV: a face a quarter-frame right of centre
    # is 30 deg away, and the target aims all of it.
    assert beh.fn.target_yaw == pytest.approx(-min(30.0, beh.params["max_yaw"]), abs=1e-6)


def test_a_lock_face_payload_naming_a_dead_param_is_refused() -> None:
    """The registry turns the CliError into a typed refusal; nothing is admitted."""
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face())
    result = registry.dispatch({"op": LOCK_FACE, "params": {"yaw_gain": 30.0}}, ctx)
    assert result["ok"] is False
    assert "yaw_gain" in result["error"]
    assert ctx.admits == []


# --------------------------------------------------------------------------- #
# 8. the leaf's import boundary (h4)                                          #
# --------------------------------------------------------------------------- #


def test_face_lock_imports_no_transport_no_sdk_and_no_network() -> None:
    """h4: ``face_lock.py`` imports no transport and no ``reachy_mini``.

    The FOV defaults are CONSTANTS in this module precisely because reading them
    from ``GET /api/camera/specs`` here would give a leaf behavior a transport;
    composition is where a per-camera value would be resolved and injected.
    """
    tree = ast.parse(inspect.getsource(face_lock_mod))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            modules.add(node.module)
    forbidden = {"reachy_mini", "urllib", "urllib.request", "socket", "requests"}
    assert not (modules & forbidden)
    assert not any(m == "reachy.robot" or m.startswith("reachy.robot.") for m in modules)


def test_a_negative_damping_in_a_raw_params_dict_never_steers_away() -> None:
    """Validation is the gate; the leaf still refuses to invert its own loop."""
    fn = make_face_lock()
    params = _lock_params(damping=-1.0)
    fn(0.0, params, Sense(face_bbox=_seen_bbox(15.0, 0.0, 0.0, 0.0), face_age_s=0.0))
    assert fn.target_yaw == pytest.approx(0.0, abs=1e-9)


# --------------------------------------------------------------------------- #
# 9. #183 — the antennas keep swaying under a lock (c28, h19, c29, h20)       #
# --------------------------------------------------------------------------- #


@dataclass
class _EngineCtx:
    """A ``TickContext``-shaped ctx over a REAL :class:`Engine`.

    The lock's handlers only need ``sense`` / ``pose`` / ``admit`` / ``evict`` /
    ``now``, so this is the smallest honest bridge from the duck-typed ctx the
    rest of this file uses to the engine that actually arbitrates.
    """

    engine: Engine
    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    pose: dict | None = None
    events: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        return self.engine.admit_behavior(behavior, self.now)

    def evict(self, target: str) -> dict:
        return self.engine.evict(target)

    def active_names(self) -> set:
        return {ab.behavior.name for ab in self.engine.active}


def _owner_names(engine: Engine, tick: dict) -> dict[str, str | None]:
    """This tick's ``{channel: owning behavior NAME}`` (ids are per-run)."""
    by_id = {ab.behavior.id: ab.behavior.name for ab in engine.active}
    return {channel: by_id.get(owner) for channel, owner in tick["ownership"].items()}


def test_lock_inhibits_only_orient_to_sound() -> None:
    """c28: feel-alive left the set — the base layer is no longer evicted."""
    assert LOCK_INHIBITS == ("orient-to-sound",)


def test_the_gaze_contributes_a_constant_held_body_yaw() -> None:
    fn = make_face_lock()
    fn.hold_body_yaw(3.25)
    params = _lock_params()
    sense = _face(cx=0.7)
    values = [fn(i * 0.02, params, sense).body_yaw for i in range(50)]
    assert values == [pytest.approx(3.25)] * 50


def test_the_gaze_holds_zero_body_yaw_when_nothing_was_handed_to_it() -> None:
    fn = make_face_lock()
    assert fn(0.0, _lock_params(), _face()).body_yaw == pytest.approx(0.0)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), "x", None])
def test_a_hostile_held_body_yaw_degrades_to_zero(bad) -> None:
    fn = make_face_lock()
    fn.hold_body_yaw(bad)
    assert fn(0.0, _lock_params(), _face()).body_yaw == pytest.approx(0.0)


def test_the_lock_holds_the_body_yaw_the_engine_streamed_on_the_previous_tick() -> None:
    """The held value is ``ctx.pose['body_yaw']`` — the tick BEFORE the lock took it."""
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face(cx=0.7), pose={"body_yaw": -4.5})
    registry.dispatch({"op": LOCK_FACE}, ctx)
    beh = ctx.admits[0]
    assert beh.channels == frozenset({"head", "body_yaw"})
    assert beh.fn(0.0, beh.params, ctx.sense).body_yaw == pytest.approx(-4.5)


def test_a_ctx_with_no_live_pose_holds_zero() -> None:
    """A lock taken before any tick streamed a pose holds the neutral body yaw."""
    _driver, registry = _wire()
    ctx = _RecordingCtx(sense=_face(cx=0.7), pose=None)
    registry.dispatch({"op": LOCK_FACE}, ctx)
    beh = ctx.admits[0]
    assert beh.fn(0.0, beh.params, ctx.sense).body_yaw == pytest.approx(0.0)


def test_a_lock_over_the_seeded_base_layer_takes_head_and_body_yaw_only() -> None:
    """h19: arbitration gives the antennas to feel-alive across lock, hold and release."""
    engine = Engine()
    engine.seed_base_layer(0.0, 1.0)
    intents = IntentDriver()
    driver = FaceLockDriver(
        inhibitions_getter=lambda: intents.inhibitions,
        inhibitions_setter=intents.set_inhibitions,
    )
    driver.register_into(intents.registry)
    intents.inhibition_observer = driver.notice_inhibition_replaced

    sense = _face(cx=0.7)
    before = engine.compose_tick(0.02, sense)
    assert set(_owner_names(engine, before).values()) == {BASE_LAYER_NAME}

    ctx = _EngineCtx(engine=engine, now=0.02, sense=sense, pose=before["pose"])
    result = intents.registry.dispatch({"op": LOCK_FACE}, ctx)
    assert result["ok"] is True
    assert result["inhibited"] == ["orient-to-sound"]

    for i in range(1, 26):  # lock and hold
        tick = engine.compose_tick(0.02 + i * 0.02, sense)
        owners = _owner_names(engine, tick)
        assert owners["head"] == FACE_LOCK_BEHAVIOR
        assert owners["body_yaw"] == FACE_LOCK_BEHAVIOR
        assert owners["antennas"] == BASE_LAYER_NAME
        # The base layer is never evicted by a lock.
        assert BASE_LAYER_NAME in {ab.behavior.name for ab in engine.active}

    held = engine.compose_tick(0.54, sense)["pose"]["body_yaw"]
    assert held == pytest.approx(before["pose"]["body_yaw"])

    intents.registry.dispatch({"op": RELEASE_FACE}, ctx)
    after = engine.compose_tick(0.56, sense)
    assert set(_owner_names(engine, after).values()) == {BASE_LAYER_NAME}
    # h20/#183: the lock case never stops the base layer, so it is still active.
    assert engine.state(0.56, EngineConfig())["base_layer"]["active"] is True


def test_orient_to_sound_stays_inhibited_so_it_never_owns_the_head() -> None:
    """c29: same class as the lock, so only the inhibition keeps it off the face."""
    intents = IntentDriver()
    _driver, registry = _wire(intents)
    ctx = _RecordingCtx(sense=_face())
    registry.dispatch({"op": LOCK_FACE}, ctx)
    assert "orient-to-sound" in intents.inhibitions
    refused = registry.dispatch(
        {"op": "run_behavior", "name": "orient-to-sound", "duration_s": 1.0}, ctx
    )
    assert refused["ok"] is False
    assert "inhibited" in refused["error"]
    assert [beh.name for beh in ctx.admits] == [FACE_LOCK_BEHAVIOR]


def test_claude_md_names_one_inhibited_behavior() -> None:
    """The doc sentence the pair used to live in (spec target h19)."""
    text = (Path(__file__).resolve().parents[1] / "CLAUDE.md").read_text(encoding="utf-8")
    assert "(`feel-alive`, `orient-to-sound` — the two behaviors that would drag" not in text
    assert "the antennas keep swaying" in text


def test_fov_params_refuse_an_impossible_camera_angle():
    """PR #187 review: a FOV above 180 deg would scale the aim past any real lens."""
    from reachy.behavior import library as lib

    entry = lib.get("face-lock")
    for name in ("fov_h", "fov_v"):
        assert entry.params[name].maximum == 180.0
        with pytest.raises(Exception):
            lib.validate_param_value(entry, name, 181.0)
