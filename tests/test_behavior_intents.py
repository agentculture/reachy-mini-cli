"""Intent tools through the act-in spool — control.py's registry + IntentDriver.

Pins the t7 acceptance criteria:

1. Each intent tool writes an atomic spool command (control.py idiom); the
   engine-side driver (:class:`~reachy.behavior.intents.IntentDriver`) applies
   it and ``state.json`` reflects the sustained intent.
2. An intent declared once is still being sustained many ticks later with no
   further agent calls, observable via ``behavior status --json``'s state file
   (the ``state.json`` the engine writes).

Also covers the extension mechanism this wave adds to
:mod:`reachy.behavior.control` (:class:`~reachy.behavior.control.KindRegistry`,
namespaced spools) independently of any intent semantics, and the one small
additive coordination method on :class:`~reachy.behavior.rule_engine.RuleEngine`
(:meth:`~reachy.behavior.rule_engine.RuleEngine.set_active_mode`) the
``set_mode`` kind calls through.

Deterministic throughout: a duck-typed recording ``ctx`` (mirroring
``tests/test_behavior_rule_engine.py``'s ``_RecordingCtx``) for the unit-level
driver tests, and the REAL engine loop (injected clock/sleep/max_ticks, a fake
in-memory streaming sink) for the criterion-2 integration test — no robot,
daemon, network, or LLM anywhere in this file.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field

import pytest

from reachy.behavior import control as control_mod
from reachy.behavior import engine as E
from reachy.behavior import library as behavior_library
from reachy.behavior.control import CommandSpool, KindRegistry
from reachy.behavior.engine import BASE_LAYER_NAME, Engine, EngineConfig
from reachy.behavior.intents import (
    DECLARE_GOAL,
    INTENT_NAMESPACE,
    RUN_BEHAVIOR,
    SET_INHIBITION,
    SET_MODE,
    IntentDriver,
)
from reachy.behavior.model import Lifetime
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import Mode, RulesConfig
from reachy.cli._errors import CliError

# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext (mirrors test_behavior_rule_engine.py's fixture)."""

    now: float = 0.0
    tick: int = 0
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    base_adds: list = field(default_factory=list)
    base_ensured: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def add_base(self) -> dict:
        """The engine's un-stop verb (an unbounded ``add`` of the base name)."""
        self.base_adds.append(self.tick)
        self._active.add(BASE_LAYER_NAME)
        return {"ok": True, "op": "add", "id": "base:1", "name": BASE_LAYER_NAME}

    def ensure_base(self) -> str | None:
        if BASE_LAYER_NAME in self._active:
            return None
        self.base_ensured.append(self.tick)
        self._active.add(BASE_LAYER_NAME)
        return "base:1"

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


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _submit(root, op, **fields):
    return control_mod.submit(op, namespace=INTENT_NAMESPACE, root=root, **fields)


# --------------------------------------------------------------------------- #
# control.py: KindRegistry — a domain-free kind -> handler mapping            #
# --------------------------------------------------------------------------- #


def test_kind_registry_dispatches_to_the_registered_handler() -> None:
    seen = []
    reg = KindRegistry()
    reg.register(
        "ping", lambda payload, ctx: seen.append((payload, ctx)) or {"ok": True, "pong": 1}
    )
    result = reg.dispatch({"op": "ping", "x": 1}, "some-ctx")
    assert result == {"ok": True, "pong": 1}
    assert seen == [({"op": "ping", "x": 1}, "some-ctx")]


def test_kind_registry_unknown_kind_is_a_clean_error_never_raises() -> None:
    reg = KindRegistry()
    result = reg.dispatch({"op": "nope"}, None)
    assert result["ok"] is False
    assert "nope" in result["error"]


def test_kind_registry_handler_cli_error_becomes_clean_outcome() -> None:
    reg = KindRegistry()

    def boom(_payload, _ctx):
        raise CliError(code=1, message="bad thing", remediation="do the other thing")

    reg.register("boom", boom)
    result = reg.dispatch({"op": "boom"}, None)
    assert result == {"ok": False, "op": "boom", "error": "bad thing (do the other thing)"}


def test_kind_registry_handler_generic_exception_is_isolated() -> None:
    reg = KindRegistry()

    def boom(_payload, _ctx):
        raise RuntimeError("kaboom")

    reg.register("boom", boom)
    result = reg.dispatch({"op": "boom"}, None)
    assert result["ok"] is False
    assert "kaboom" in result["error"]


def test_kind_registry_register_returns_self_for_chaining() -> None:
    reg = KindRegistry()
    out = reg.register("a", lambda p, c: {"ok": True}).register("b", lambda p, c: {"ok": True})
    assert out is reg
    assert reg.kinds() == ["a", "b"]


# --------------------------------------------------------------------------- #
# control.py: namespaced spools stay isolated from the base engine's spool    #
# --------------------------------------------------------------------------- #


def test_namespaced_spool_is_isolated_from_the_base_spool(tmp_path) -> None:
    """A command submitted to the base (unnamespaced) spool is invisible to an
    intents-namespaced CommandSpool, and vice versa — the hard requirement that
    lets intent kinds land without colliding with engine.py's own drain of the
    base commands_dir()."""
    base = CommandSpool(root=tmp_path)
    intents = CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)

    control_mod.submit("add", namespace="", root=tmp_path, name="nod")
    control_mod.submit(RUN_BEHAVIOR, namespace=INTENT_NAMESPACE, root=tmp_path, name="nod")

    base_cmds = base.drain()
    intent_cmds = intents.drain()
    assert [c["op"] for c in base_cmds] == ["add"]
    assert [c["op"] for c in intent_cmds] == [RUN_BEHAVIOR]


def test_default_namespace_paths_are_unchanged_from_before(tmp_path) -> None:
    """No-argument calls resolve to EXACTLY the pre-t7 paths (backward compat)."""
    root_default = control_mod.behavior_dir(tmp_path)
    assert control_mod.commands_dir(root=tmp_path) == root_default / "commands"
    assert control_mod.results_dir(root=tmp_path) == root_default / "results"
    assert control_mod.state_file(root=tmp_path) == root_default / "state.json"


def test_command_spool_read_state_mirrors_the_free_function(tmp_path) -> None:
    spool = CommandSpool(root=tmp_path)
    assert spool.read_state() is None
    spool.write_state({"active": []})
    assert spool.read_state() == {"active": []}
    assert control_mod.read_state(root=tmp_path) == {"active": []}


def test_root_override_avoids_the_env_var_path(tmp_path, monkeypatch) -> None:
    other = tmp_path / "elsewhere"
    other.mkdir()
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "not-used"))
    cmd_id = control_mod.submit("run_behavior", namespace=INTENT_NAMESPACE, root=other, name="nod")
    # Drains from `other`, not from the env-derived state dir.
    spool = CommandSpool(namespace=INTENT_NAMESPACE, root=other)
    drained = spool.drain()
    assert [c["cmd_id"] for c in drained] == [cmd_id]


# --------------------------------------------------------------------------- #
# Acceptance criterion 1 — atomic write + drain roundtrip                     #
# --------------------------------------------------------------------------- #


def test_run_behavior_roundtrip_admits_and_confirms(tmp_path) -> None:
    # gaze-hold is a BOUNDED-default entry (looping=False, duration=5.0), so this
    # exercises the plain roundtrip mechanism unaffected by the t5 unbounded-
    # lifetime refusal below (which only bites looping-default entries like nod).
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name="gaze-hold", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["gaze-hold"]
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True
    assert result["op"] == RUN_BEHAVIOR
    assert result["name"] == "gaze-hold"


def test_run_behavior_respects_explicit_lifetime(tmp_path) -> None:
    _submit(
        tmp_path,
        RUN_BEHAVIOR,
        name="gaze-hold",
        params={},
        lifetime={"looping": False, "duration": 2.5},
    )
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver.on_tick(ctx)
    beh = ctx.admits[0]
    assert beh.lifetime.looping is False
    assert beh.lifetime.duration == 2.5


def test_run_behavior_is_not_re_admitted_after_eviction(tmp_path) -> None:
    """run_behavior is a ONE-TIME admission — contrast with declare_goal."""
    # gaze-hold is a BOUNDED-default entry — see the roundtrip test above.
    _submit(tmp_path, RUN_BEHAVIOR, name="gaze-hold", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0)
    ctx.tick = 1
    driver.on_tick(ctx)
    assert len(ctx.admits) == 1

    ctx.evict("gaze-hold")  # something else stops it
    for tick in range(2, 6):
        ctx.tick = tick
        ctx.now = float(tick)
        driver.on_tick(ctx)
    assert len(ctx.admits) == 1  # never re-admitted


def test_state_json_reflects_the_run_behavior_goal_is_none(tmp_path) -> None:
    # This submission is refused (nod is a looping-default entry with no lifetime
    # payload — see the t5 section below), but the assertion here is about the
    # "intents" state VIEW, which `_publish_state` writes independently of any
    # given tick's command outcomes — so it holds whether or not the command was
    # admitted.
    _submit(tmp_path, RUN_BEHAVIOR, name="nod", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    state = control_mod.read_state(root=tmp_path)
    assert state["intents"] == {"goal": None, "inhibitions": [], "mode": None}


# --------------------------------------------------------------------------- #
# run_behavior — bounded-lifetime refusal (t5)                                #
# --------------------------------------------------------------------------- #
#
# Background: a react rule admitting the looping `nod` behavior with library
# defaults held the head channel FOREVER (a live incident on the rules-engine
# surface, fixed by a sibling task). This closes the same defect on the
# intent-spool `run_behavior` surface: `_validated_lifetime` now REFUSES
# whenever the RESULTING lifetime is unbounded (looping=True, duration=None),
# regardless of whether that shape came from a missing lifetime payload on a
# looping-default library entry, or an explicit `{"looping": true}` with no
# duration.

_LOOPING_DEFAULT_ENTRIES = sorted(
    name
    for name, entry in behavior_library.LIBRARY.items()
    if entry.looping and entry.default_duration is None
)


def test_looping_default_entries_fixture_matches_the_library() -> None:
    """Pins the exact set of entries this refusal protects (nod/shake/speak/
    antenna-sway/feel-alive, plus orient-to-sound) so a library edit that
    silently changes this set is caught here rather than by a confusing failure
    in the tests below.

    ``orient-to-sound`` (task t8) joined deliberately: sound-orienting is a
    STANDING goal, so its library defaults are looping-with-no-duration and the
    one-time ``run_behavior`` surface must bound it explicitly. The standing,
    indefinite ``declare_goal`` surface — exempt by design — is the documented
    way to sustain it.

    ``face-lock`` (task t4) joined for the same reason: a face lock is a
    STANDING intent ended by ``release_face``, so its library defaults are
    looping-with-no-duration and the one-time ``run_behavior`` surface must
    bound it explicitly. ``lock_face`` — a dedicated kind that OWNS the lock
    state — is the exempt standing surface, exactly as ``declare_goal`` is for
    ``orient-to-sound``.
    """
    assert _LOOPING_DEFAULT_ENTRIES == [
        "antenna-sway",
        "face-lock",
        "feel-alive",
        "nod",
        "orient-to-sound",
        "shake",
        "speak",
    ]


def test_run_behavior_refuses_a_looping_default_entry_with_no_lifetime_payload(
    tmp_path,
) -> None:
    """The core t5 case: `run_behavior` naming `nod` with NO lifetime payload at
    all would silently inherit nod's own (looping=True, duration=None) library
    defaults — an unbounded admission that holds the head channel forever. It
    must be refused, not admitted."""
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name="nod", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert ctx.admits == []  # never reaches ctx.admit
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "nod" in result["error"]
    assert "duration" in result["error"].lower()  # names the remedy


def test_run_behavior_refusal_emits_intent_blocked_not_applied(tmp_path) -> None:
    _submit(tmp_path, RUN_BEHAVIOR, name="nod", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver.on_tick(ctx)

    blocked = [e for e in ctx.events if e["type"] == "intent.blocked"]
    applied = [e for e in ctx.events if e["type"] == "intent.applied"]
    assert blocked and blocked[0]["kind"] == RUN_BEHAVIOR
    assert applied == []


@pytest.mark.parametrize("name", [n for n in _LOOPING_DEFAULT_ENTRIES if n != BASE_LAYER_NAME])
def test_run_behavior_refuses_every_looping_default_entry_with_no_lifetime(tmp_path, name) -> None:
    """Every looping-default library entry — not just nod — is caught.

    ``feel-alive`` is the ONE carve-out (t11, #183 decision c38): an unbounded
    ``run_behavior`` of the base-layer name is the un-stop verb, not an
    unbounded admission. See the base-layer section at the end of this file.
    """
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name=name, params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))

    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert name in result["error"]


def test_run_behavior_admits_a_looping_default_entry_when_given_a_duration(
    tmp_path,
) -> None:
    """The same `nod` payload, but WITH `lifetime={"duration": 5}`, admits — the
    resulting Lifetime(looping=True, duration=5.0) reaches ctx.admit."""
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name="nod", params={}, lifetime={"duration": 5})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["nod"]
    beh = ctx.admits[0]
    assert beh.lifetime == Lifetime(looping=True, duration=5.0)
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True
    assert result["name"] == "nod"


def test_run_behavior_refuses_explicit_looping_true_with_no_duration(tmp_path) -> None:
    """Even on a BOUNDED-default entry (gaze-hold, default_duration=5.0), an
    EXPLICIT override that resolves to unbounded (`looping: true`, `duration:
    null`) is refused — the refusal is about the RESULTING shape, not the
    entry's own defaults. (An explicit `duration: null` is distinct from
    OMITTING `duration` — the latter would fall back to gaze-hold's own bounded
    default and admit cleanly; see the byte-identical test below.)"""
    cmd_id = _submit(
        tmp_path,
        RUN_BEHAVIOR,
        name="gaze-hold",
        params={},
        lifetime={"looping": True, "duration": None},
    )
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert ctx.admits == []
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "gaze-hold" in result["error"]


def test_run_behavior_still_admits_bounded_looping_true_with_duration(tmp_path) -> None:
    """looping=True WITH a positive duration is bounded and admitted, exactly as
    before — the refusal only fires when duration is None."""
    cmd_id = _submit(
        tmp_path,
        RUN_BEHAVIOR,
        name="shake",
        params={},
        lifetime={"looping": True, "duration": 3.0},
    )
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["shake"]
    assert ctx.admits[0].lifetime == Lifetime(looping=True, duration=3.0)
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True


def test_run_behavior_bounded_entries_stay_byte_identical_with_no_lifetime_payload(
    tmp_path,
) -> None:
    """Bounded-default entries (gaze-hold, thoughtful) admit on a bare payload
    exactly as before this change — the refusal never touches them."""
    for name in ("gaze-hold", "thoughtful"):
        cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name=name, params={}, lifetime=None)
        driver = IntentDriver(root=tmp_path)
        ctx = _RecordingCtx(now=1.0, tick=1)
        driver.on_tick(ctx)

        assert [b.name for b in ctx.admits] == [name]
        result = control_mod.await_result(
            cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
        )
        assert result["ok"] is True


# --------------------------------------------------------------------------- #
# declare_goal — standing admission, sustained + re-admitted                  #
# --------------------------------------------------------------------------- #


def test_declare_goal_admits_and_state_json_reflects_it(tmp_path) -> None:
    cmd_id = _submit(tmp_path, DECLARE_GOAL, goal="nod", params={"amp": 20.0})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["nod"]
    assert ctx.admits[0].lifetime.looping is True
    assert ctx.admits[0].lifetime.duration is None
    assert ctx.admits[0].params["amp"] == 20.0

    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result == {
        "ok": True,
        "op": DECLARE_GOAL,
        "goal": "nod",
        "params": {**_nod_defaults(), "amp": 20.0},
    }

    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["goal"]["name"] == "nod"
    assert state["intents"]["goal"]["params"]["amp"] == 20.0


def _nod_defaults() -> dict:
    from reachy.behavior import library

    return library.LIBRARY["nod"].default_params()


def test_declare_goal_is_sustained_many_ticks_later_with_no_further_calls(tmp_path) -> None:
    """Acceptance criterion 2 (unit-level): declared ONCE, still sustained tick 50."""
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=0)

    ctx.tick = 1
    driver.on_tick(ctx)  # the one and only spool command drains here
    assert "nod" in ctx.active_names()

    for tick in range(2, 51):
        ctx.tick = tick
        ctx.now = float(tick)
        driver.on_tick(ctx)  # NO further spool submissions

    assert "nod" in ctx.active_names()
    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["goal"]["name"] == "nod"


def test_declare_goal_re_admits_when_something_else_evicts_it(tmp_path) -> None:
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)
    assert len(ctx.admits) == 1

    ctx.evict("nod")  # some external force (a rule, a stop-all, ...) evicts it
    assert "nod" not in ctx.active_names()

    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)  # no new spool command — the driver notices and re-admits

    assert len(ctx.admits) == 2
    assert "nod" in ctx.active_names()


def test_declare_goal_replacement_evicts_the_previous_goal(tmp_path) -> None:
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)
    assert "nod" in ctx.active_names()

    _submit(tmp_path, DECLARE_GOAL, goal="shake", params={})
    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)

    assert "nod" not in ctx.active_names()
    assert "shake" in ctx.active_names()


def test_declare_goal_clear_evicts_and_stops_sustaining(tmp_path) -> None:
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)
    assert "nod" in ctx.active_names()

    _submit(tmp_path, DECLARE_GOAL, goal=None)
    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)
    assert "nod" not in ctx.active_names()
    assert driver.goal is None

    for tick in range(3, 10):
        ctx.tick = tick
        ctx.now = float(tick)
        driver.on_tick(ctx)
    assert "nod" not in ctx.active_names()  # never comes back

    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["goal"] is None


def test_declare_goal_unknown_behavior_is_a_named_error(tmp_path) -> None:
    cmd_id = _submit(tmp_path, DECLARE_GOAL, goal="not-a-real-behavior", params={})
    driver = IntentDriver(root=tmp_path)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "not-a-real-behavior" in result["error"]


# --------------------------------------------------------------------------- #
# set_inhibition — blocks admission until cleared                             #
# --------------------------------------------------------------------------- #


def test_set_inhibition_blocks_a_currently_active_behavior(tmp_path) -> None:
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)
    assert "nod" in ctx.active_names()

    _submit(tmp_path, SET_INHIBITION, behaviors=["nod"])
    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)
    assert "nod" not in ctx.active_names()  # evicted this same tick

    # Goal is still recorded, but withheld — many further ticks, still blocked.
    for tick in range(3, 10):
        ctx.tick = tick
        ctx.now = float(tick)
        driver.on_tick(ctx)
    assert "nod" not in ctx.active_names()
    assert driver.goal is not None and driver.goal["name"] == "nod"


def test_set_inhibition_clearing_lets_the_goal_resume(tmp_path) -> None:
    _submit(tmp_path, DECLARE_GOAL, goal="nod", params={})
    _submit(tmp_path, SET_INHIBITION, behaviors=["nod"])
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)  # drains both commands this tick
    assert "nod" not in ctx.active_names()

    _submit(tmp_path, SET_INHIBITION, behaviors=[])  # clear
    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)

    assert "nod" in ctx.active_names()
    assert driver.inhibitions == frozenset()


def test_run_behavior_is_rejected_while_inhibited(tmp_path) -> None:
    _submit(tmp_path, SET_INHIBITION, behaviors=["nod"])
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=0.0, tick=1)
    driver.on_tick(ctx)  # applies the inhibition

    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name="nod", params={}, lifetime=None)
    ctx.tick = 2
    ctx.now = 2.0
    driver.on_tick(ctx)

    assert ctx.admits == []
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "inhibited" in result["error"]


def test_set_inhibition_unknown_behavior_is_a_named_error(tmp_path) -> None:
    cmd_id = _submit(tmp_path, SET_INHIBITION, behaviors=["not-a-real-behavior"])
    driver = IntentDriver(root=tmp_path)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "not-a-real-behavior" in result["error"]


def test_set_inhibition_requires_a_list(tmp_path) -> None:
    cmd_id = _submit(tmp_path, SET_INHIBITION, behaviors="nod")  # not a list
    driver = IntentDriver(root=tmp_path)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False


# --------------------------------------------------------------------------- #
# set_mode — coordinates with a live RuleEngine via an injected callback      #
# --------------------------------------------------------------------------- #


def test_set_mode_swaps_the_live_rule_engine_active_mode(tmp_path) -> None:
    cfg = RulesConfig(
        modes={"calm": Mode("calm", {}), "excited": Mode("excited", {"amp": 30.0})},
        active_mode="calm",
    )
    rule_engine = RuleEngine(cfg)
    driver = IntentDriver(
        root=tmp_path,
        mode_setter=rule_engine.set_active_mode,
        known_modes=lambda: cfg.modes.keys(),
    )
    _submit(tmp_path, SET_MODE, mode="excited")
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))

    assert rule_engine._config.active_mode == "excited"
    assert driver.mode == "excited"


def test_set_mode_unknown_mode_is_rejected_and_never_calls_the_setter(tmp_path) -> None:
    calls = []
    driver = IntentDriver(
        root=tmp_path,
        mode_setter=calls.append,
        known_modes=lambda: {"calm", "excited"},
    )
    cmd_id = _submit(tmp_path, SET_MODE, mode="furious")
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))

    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert "furious" in result["error"]
    assert calls == []


def test_set_mode_clear_passes_none_to_the_setter(tmp_path) -> None:
    calls = []
    driver = IntentDriver(root=tmp_path, mode_setter=calls.append)
    _submit(tmp_path, SET_MODE, mode=None)
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    assert calls == [None]
    assert driver.mode is None


def test_set_mode_without_a_wired_setter_still_records_the_mode(tmp_path) -> None:
    driver = IntentDriver(root=tmp_path)  # no mode_setter/known_modes wired
    _submit(tmp_path, SET_MODE, mode="whatever")
    driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    assert driver.mode == "whatever"
    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["mode"] == "whatever"


# --------------------------------------------------------------------------- #
# Events + observability                                                      #
# --------------------------------------------------------------------------- #


def test_applied_command_emits_intent_applied_event(tmp_path) -> None:
    # gaze-hold is a BOUNDED-default entry — see the roundtrip test above.
    _submit(tmp_path, RUN_BEHAVIOR, name="gaze-hold", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver.on_tick(ctx)
    applied = [e for e in ctx.events if e["type"] == "intent.applied"]
    assert applied and applied[0]["kind"] == RUN_BEHAVIOR


def test_rejected_command_emits_intent_blocked_event(tmp_path) -> None:
    _submit(tmp_path, RUN_BEHAVIOR, name="not-a-real-behavior", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver.on_tick(ctx)
    blocked = [e for e in ctx.events if e["type"] == "intent.blocked"]
    assert blocked and blocked[0]["kind"] == RUN_BEHAVIOR


def test_sense_log_lines_emitted_for_apply_and_drop(caplog) -> None:
    import logging

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver = IntentDriver()
        _submit(None, RUN_BEHAVIOR, name="not-a-real-behavior", params={}, lifetime=None)
        driver.on_tick(_RecordingCtx(now=1.0, tick=1))
    lines = [r.getMessage() for r in caplog.records if r.name == "reachy.sense"]
    assert any("stage=intent" in ln and "dropped" in ln for ln in lines)


def test_driver_is_directly_usable_as_a_bare_tick_seam(tmp_path) -> None:
    """IntentDriver.__call__ makes it usable as engine.run(tick_seam=driver)."""
    # gaze-hold is a BOUNDED-default entry — see the roundtrip test above.
    _submit(tmp_path, RUN_BEHAVIOR, name="gaze-hold", params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver(ctx)  # not .on_tick(ctx)
    assert [b.name for b in ctx.admits] == ["gaze-hold"]


# --------------------------------------------------------------------------- #
# Acceptance criterion 2 — real engine loop, many ticks, no further calls     #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.poses = []
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


def test_declared_goal_is_sustained_across_a_real_bounded_engine_run() -> None:
    """The full round trip: a tool-style submit BEFORE the loop starts, then a
    real engine.run() for many ticks with NO further submissions — 'behavior
    status --json's state file (the state.json the engine writes)' shows the
    goal both admitted (active) and recorded (intents), at the very end."""
    control_mod.submit(DECLARE_GOAL, namespace=INTENT_NAMESPACE, goal="nod", params={})

    eng = Engine()
    driver = IntentDriver()
    main_spool = CommandSpool()
    tr = _FakeTransport()

    # 30 ticks stays well clear of the base engine's own heartbeat re-publish
    # (tick 1, then every compose_hz/2 == 25 ticks after) so the driver's own
    # unconditional per-tick merge-write is the LAST word in state.json.
    ticks = E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=30,
        engine=eng,
        control=main_spool,
        tick_seam=driver,
    )
    assert ticks == 30

    active_names = {ab.behavior.name for ab in eng.active}
    assert "nod" in active_names

    state = control_mod.read_state()
    assert "nod" in {a["name"] for a in state["active"]}
    assert state["intents"]["goal"]["name"] == "nod"


def test_run_behavior_expires_naturally_and_is_never_resurrected() -> None:
    """Contrast case: a one-shot run_behavior expires on its own lifetime and the
    driver does NOT bring it back (unlike declare_goal)."""
    control_mod.submit(
        RUN_BEHAVIOR,
        namespace=INTENT_NAMESPACE,
        name="gaze-hold",
        params={},
        lifetime={"looping": False, "duration": 0.05},
    )

    eng = Engine()
    driver = IntentDriver()
    tr = _FakeTransport()

    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=20,  # 20 * 0.02s = 0.4s, well past the 0.05s duration
        engine=eng,
        control=CommandSpool(),
        tick_seam=driver,
    )

    assert "gaze-hold" not in {ab.behavior.name for ab in eng.active}


def test_goal_re_admission_survives_an_external_eviction_over_a_real_run() -> None:
    """A sibling driver simulates something else (e.g. a rule) evicting the goal
    behavior mid-run; IntentDriver notices on its very next tick and re-admits
    it with NO further agent call — over a real, bounded engine.run()."""
    control_mod.submit(DECLARE_GOAL, namespace=INTENT_NAMESPACE, goal="nod", params={})

    class _EvictOnce:
        def __init__(self):
            self.done = False

        def __call__(self, ctx):
            if ctx.tick == 5 and not self.done:
                ctx.evict("nod")
                self.done = True

    class _Fan:
        def __init__(self, drivers):
            self._drivers = drivers

        def __call__(self, ctx):
            for d in self._drivers:
                d(ctx)

    eng = Engine()
    driver = IntentDriver()
    seam = _Fan([_EvictOnce(), driver])
    tr = _FakeTransport()

    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=15,
        engine=eng,
        control=CommandSpool(),
        tick_seam=seam,
    )

    assert "nod" in {ab.behavior.name for ab in eng.active}


# --------------------------------------------------------------------------- #
# enroll — the face-naming kind (issue #166)                                  #
# --------------------------------------------------------------------------- #


def test_enroll_binds_a_name_through_the_injected_seam(tmp_path) -> None:
    seen: list[str] = []

    def seam(name: str) -> dict:
        seen.append(name)
        return {"ok": True, "id": "abc123", "name": name}

    driver = IntentDriver(root=tmp_path, enroll_face=seam)
    result = driver.registry.dispatch({"op": "enroll", "name": "  Ori  ", "cmd_id": "x"}, None)
    assert result == {"ok": True, "id": "abc123", "name": "Ori", "op": "enroll"}
    assert seen == ["Ori"]


def test_enroll_without_a_seam_is_the_vision_unavailable_refusal(tmp_path) -> None:
    driver = IntentDriver(root=tmp_path)
    result = driver.registry.dispatch({"op": "enroll", "name": "Ori", "cmd_id": "x"}, None)
    assert result == {"ok": False, "op": "enroll", "error": "vision-unavailable"}


def test_enroll_refuses_a_bad_name_before_touching_the_seam(tmp_path) -> None:
    seen: list[str] = []
    driver = IntentDriver(root=tmp_path, enroll_face=lambda n: seen.append(n))
    for bad in (
        {"op": "enroll", "cmd_id": "x"},
        {"op": "enroll", "name": "   ", "cmd_id": "x"},
        {"op": "enroll", "name": 7, "cmd_id": "x"},
        {"op": "enroll", "name": "n" * 65, "cmd_id": "x"},
    ):
        result = driver.registry.dispatch(bad, None)
        assert result["ok"] is False
        assert "name" in result["error"]
    assert seen == []


def test_enroll_turns_a_raising_seam_into_a_typed_refusal(tmp_path) -> None:
    def seam(name: str) -> dict:
        raise RuntimeError("store on fire")

    driver = IntentDriver(root=tmp_path, enroll_face=seam)
    result = driver.registry.dispatch({"op": "enroll", "name": "Ori", "cmd_id": "x"}, None)
    assert result["ok"] is False
    assert result["error"].startswith("enroll-failed")


# t11 (#183): the base layer's inhibition EDGE, and the un-stop carve-out      #
#                                                                             #
# The bug is eviction BY INHIBITION: `_enforce_inhibitions` evicts `feel-alive`
# while it is named, and nothing used to bring it back. So the driver tracks
# the inhibited set tick over tick and re-seeds on the edge where the base name
# LEAVES it — and only then. A by-name `stop feel-alive` is intentional
# stillness: it holds until an unbounded `run_behavior feel-alive` (the un-stop
# verb, carved out of the unbounded-lifetime refusal) or an engine restart.
# --------------------------------------------------------------------------- #

SENSE_LOGGER = "reachy.sense"


class _Fan:
    """Compose sibling seam drivers, in order, the way `_compose_run_seam` does."""

    def __init__(self, *drivers):
        self._drivers = drivers

    def __call__(self, ctx):
        for driver in self._drivers:
            driver(ctx)


class _Script:
    """Run `actions[tick](ctx)` on the matching tick — a scripted sibling driver."""

    def __init__(self, actions):
        self._actions = actions

    def __call__(self, ctx):
        action = self._actions.get(ctx.tick)
        if action is not None:
            action(ctx)


def _run_engine(engine, seam, ticks=12, sense=None):
    return E.run(
        _FakeTransport(),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=ticks,
        engine=engine,
        control=CommandSpool(),
        tick_seam=seam,
        sense=sense,
    )


def _sense_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


def _re_seed_lines(caplog):
    return [line for line in _sense_lines(caplog) if "re-seeded (inhibition cleared)" in line]


def _base_actives(engine):
    return [ab for ab in engine.active if ab.is_base]


def _base_block(engine):
    return engine.state(1.0, EngineConfig())["base_layer"]


def _set_inhibition(*names):
    return lambda _ctx: control_mod.submit(
        SET_INHIBITION, namespace=INTENT_NAMESPACE, behaviors=list(names)
    )


def test_the_base_layer_returns_on_the_tick_an_inhibition_naming_it_clears(caplog) -> None:
    """Acceptance (a): set_inhibition(['feel-alive']) then set_inhibition([])
    leaves feel-alive active with is_base=True, `stop all` keeps it, and the
    re-seed names itself exactly once."""
    control_mod.submit(SET_INHIBITION, namespace=INTENT_NAMESPACE, behaviors=[BASE_LAYER_NAME])
    engine = Engine()
    driver = IntentDriver()
    script = _Script({6: _set_inhibition()})

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _run_engine(engine, _Fan(script, driver))

    base = _base_actives(engine)
    assert [ab.behavior.name for ab in base] == [BASE_LAYER_NAME]
    assert _base_block(engine) == {"seeded": True, "active": True, "stopped_by": None}

    engine.stop("all")  # the re-seeded id is a base id, so `stop all` keeps it
    assert [ab.behavior.name for ab in engine.active] == [BASE_LAYER_NAME]

    lines = _re_seed_lines(caplog)
    assert len(lines) == 1
    assert "stage=intent" in lines[0]
    assert "source=set_inhibition" in lines[0]
    assert f"event={BASE_LAYER_NAME}" in lines[0]


def test_the_edge_re_seed_emits_one_intent_applied_event() -> None:
    """The event half of the edge, mirroring `_sustain_goal`'s re-admit emit."""
    events: list = []

    ctx = _RecordingCtx(now=1.0, tick=1)
    ctx.events = events
    driver = IntentDriver()
    driver.set_inhibitions([BASE_LAYER_NAME])
    driver.on_tick(ctx)  # tick 1: inhibited (nothing active to evict)
    driver.set_inhibitions([])
    ctx.tick = 2
    driver.on_tick(ctx)  # tick 2: the clearing edge

    applied = [
        e for e in events if e["type"] == "intent.applied" and e.get("behavior") == BASE_LAYER_NAME
    ]
    assert len(applied) == 1
    assert applied[0]["kind"] == SET_INHIBITION
    assert ctx.base_ensured == [2]


def test_a_by_name_stop_of_the_base_layer_holds_through_an_inhibition_cycle() -> None:
    """Acceptance (b): intentional stillness is never undone by the edge — a
    `stop feel-alive`, then an inhibition naming it, then a clear, leaves the
    base layer stopped and still attributed to the stop."""
    engine = Engine()
    driver = IntentDriver()
    script = _Script(
        {
            2: lambda _ctx: engine.stop(BASE_LAYER_NAME),
            3: _set_inhibition(BASE_LAYER_NAME),
            8: _set_inhibition(),
        }
    )

    _run_engine(engine, _Fan(script, driver), ticks=14)

    assert _base_actives(engine) == []
    assert _base_block(engine) == {"seeded": True, "active": False, "stopped_by": "stop"}


def test_an_unbounded_spool_run_behavior_of_the_base_name_is_the_un_stop_verb() -> None:
    """Acceptance (c): the carve-out reaches the engine's base re-seed path, so
    a by-name stop is undone with is_base=True and the cause cleared."""
    engine = Engine()
    driver = IntentDriver()
    script = _Script(
        {
            2: lambda _ctx: engine.stop(BASE_LAYER_NAME),
            3: lambda _ctx: control_mod.submit(
                RUN_BEHAVIOR,
                namespace=INTENT_NAMESPACE,
                name=BASE_LAYER_NAME,
                params={},
                lifetime=None,
            ),
        }
    )

    _run_engine(engine, _Fan(script, driver), ticks=10)

    assert len(_base_actives(engine)) == 1
    assert _base_block(engine) == {"seeded": True, "active": True, "stopped_by": None}


def test_the_base_carve_out_routes_through_add_base_never_a_plain_admit(tmp_path) -> None:
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name=BASE_LAYER_NAME, params={}, lifetime=None)
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert ctx.admits == []  # never an ordinary admission beside the base layer
    assert ctx.base_adds == [1]
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is True
    assert result["op"] == RUN_BEHAVIOR
    assert result["name"] == BASE_LAYER_NAME


def test_a_bounded_run_behavior_of_the_base_name_stays_an_ordinary_admission(tmp_path) -> None:
    """Only the UNBOUNDED shape is the un-stop verb; with a duration it is a
    plain bounded behavior, exactly as before."""
    _submit(tmp_path, RUN_BEHAVIOR, name=BASE_LAYER_NAME, params={}, lifetime={"duration": 2})
    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)

    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == [BASE_LAYER_NAME]
    assert ctx.admits[0].lifetime == Lifetime(looping=True, duration=2.0)
    assert ctx.base_adds == []


def test_a_context_without_the_base_seam_refuses_the_un_stop_rather_than_admitting(
    tmp_path,
) -> None:
    """Fail-closed: with no `add_base` capability on the context the carve-out is
    a NAMED refusal, never a plain unbounded admission slipping through."""

    class _NoBaseSeamCtx:
        """A context predating the seam: admit/evict/emit only."""

        now = 1.0
        tick = 1

        def __init__(self):
            self.admits: list = []

        def emit(self, event: dict) -> None:
            pass

        def admit(self, behavior) -> dict:
            self.admits.append(behavior)
            return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

        def evict(self, target: str) -> dict:
            return {"ok": True, "op": "stop", "target": target}

        def active_names(self) -> set:
            return set()

    ctx = _NoBaseSeamCtx()
    cmd_id = _submit(tmp_path, RUN_BEHAVIOR, name=BASE_LAYER_NAME, params={}, lifetime=None)
    IntentDriver(root=tmp_path).on_tick(ctx)
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert result["ok"] is False
    assert BASE_LAYER_NAME in result["error"]
    assert ctx.admits == []


def test_a_lock_cycle_leaves_the_base_layer_active_at_the_end() -> None:
    """Acceptance (d): a real FaceLockDriver lock/release cycle — with a mind's
    own set_inhibition naming the base layer in the middle — ends with the base
    layer active and unattributed.

    Deliberately driven by `set_inhibition` rather than by `LOCK_INHIBITS`'
    contents: today the lock inhibits `feel-alive` (so the base returns on the
    release edge), and once task t10 lands it will not (so the base is never
    evicted at all, and the mind's own clear is the edge). Both readings satisfy
    this assertion.
    """
    from reachy.behavior.face_lock import LOCK_FACE, RELEASE_FACE, FaceLockDriver
    from reachy.behavior.sense import Sense

    engine = Engine()
    intents = IntentDriver()
    lock = FaceLockDriver(
        inhibitions_getter=lambda: intents.inhibitions,
        inhibitions_setter=intents.set_inhibitions,
    )
    lock.register_into(intents.registry)
    intents.inhibition_observer = lock.notice_inhibition_replaced

    def _submit_kind(kind):
        return lambda _ctx: control_mod.submit(kind, namespace=INTENT_NAMESPACE)

    script = _Script(
        {
            2: _submit_kind(LOCK_FACE),
            5: _set_inhibition(BASE_LAYER_NAME),
            9: _set_inhibition(),
            12: _submit_kind(RELEASE_FACE),
        }
    )
    sense = lambda _t: Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=0.0)  # noqa: E731

    _run_engine(engine, _Fan(script, intents, lock), ticks=18, sense=sense)

    assert len(_base_actives(engine)) == 1
    assert _base_block(engine) == {"seeded": True, "active": True, "stopped_by": None}
