"""Agent tools for sustained intents (:mod:`reachy.speech.intent_tools`).

Pins the tool-layer half of the t7 acceptance criteria: the four tools —
``run_behavior`` / ``declare_goal`` / ``set_mode`` / ``set_inhibition`` —
declare an OpenAI function-tool schema (mirroring ``apply_pose``'s
enum-constrained, pre-validated pattern from :mod:`reachy.speech.tools`), reject
an unknown behavior/mode name with an error tool-result naming the valid keys
BEFORE ever touching the spool, and produce a valid atomic spool command that
:class:`reachy.behavior.intents.IntentDriver` can actually drain and apply —
proving the tool -> spool -> engine-side driver round trip end to end.

Everything is exercised through fakes / an isolated ``tmp_path`` spool
directory — no robot, no network, no audio device, no LLM.
"""

from __future__ import annotations

import ast
import inspect
import json
from dataclasses import dataclass, field

import pytest

import reachy.speech.intent_tools as intent_tools_mod
from reachy.behavior import control as control_mod
from reachy.behavior import library
from reachy.behavior.intents import INTENT_NAMESPACE, IntentDriver
from reachy.speech.intent_tools import register_intent_tools
from reachy.speech.tools import ToolRegistry

# A small, focused catalog for tests that don't need the full library — keeps
# enum assertions short and readable (mirrors test_speech_tools.py's Catalog()
# use for apply_pose).
_SMALL_CATALOG = {"nod": library.LIBRARY["nod"], "shake": library.LIBRARY["shake"]}


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext (mirrors tests/test_behavior_intents.py)."""

    now: float = 0.0
    tick: int = 0
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
        self._active.discard(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set(self._active)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


# --------------------------------------------------------------------------- #
# Registration + schema shape                                                 #
# --------------------------------------------------------------------------- #


def test_register_intent_tools_adds_the_four_tools(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path)
    names = reg.names()
    assert "run_behavior" in names
    assert "declare_goal" in names
    assert "set_mode" in names
    assert "set_inhibition" in names
    # Alongside the built-ins, not replacing them.
    assert "speak" in names and "apply_pose" in names


def test_run_behavior_and_declare_goal_advertise_the_catalog_as_enum(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    by_name = {t["function"]["name"]: t["function"] for t in reg.tools()}
    assert by_name["run_behavior"]["parameters"]["properties"]["name"]["enum"] == ["nod", "shake"]
    assert by_name["declare_goal"]["parameters"]["properties"]["goal"]["enum"] == ["nod", "shake"]
    assert by_name["run_behavior"]["parameters"]["required"] == ["name"]
    assert by_name["declare_goal"]["parameters"]["required"] == []  # omitting 'goal' clears


def test_set_inhibition_advertises_the_catalog_in_the_array_items(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    by_name = {t["function"]["name"]: t["function"] for t in reg.tools()}
    props = by_name["set_inhibition"]["parameters"]["properties"]
    assert props["behaviors"]["items"]["enum"] == ["nod", "shake"]
    assert by_name["set_inhibition"]["parameters"]["required"] == ["behaviors"]


def test_set_mode_with_no_known_modes_has_no_enum(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path)
    by_name = {t["function"]["name"]: t["function"] for t in reg.tools()}
    assert "enum" not in by_name["set_mode"]["parameters"]["properties"]["mode"]


def test_set_mode_with_known_modes_publishes_the_sorted_enum(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, modes=["excited", "calm"])
    by_name = {t["function"]["name"]: t["function"] for t in reg.tools()}
    assert by_name["set_mode"]["parameters"]["properties"]["mode"]["enum"] == ["calm", "excited"]


# --------------------------------------------------------------------------- #
# Validation BEFORE the spool write (mirrors apply_pose)                      #
# --------------------------------------------------------------------------- #


def test_run_behavior_unknown_name_is_rejected_and_never_touches_the_spool(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    result = reg.dispatch(
        "run_behavior", json.dumps({"name": "not-a-real-behavior"}), tool_call_id="a"
    )

    assert result["role"] == "tool"
    payload = json.loads(result["content"])
    assert "error" in payload
    for key in _SMALL_CATALOG:
        assert key in payload["error"]

    # Nothing was ever written to the spool for a rejected call.
    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    assert spool.drain() == []


def test_declare_goal_unknown_goal_is_rejected_and_never_touches_the_spool(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    result = reg.dispatch("declare_goal", json.dumps({"goal": "nope"}), tool_call_id="b")
    payload = json.loads(result["content"])
    assert "error" in payload
    for key in _SMALL_CATALOG:
        assert key in payload["error"]
    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    assert spool.drain() == []


def test_set_mode_unknown_mode_is_rejected_when_modes_are_known(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, modes=["calm", "excited"])
    result = reg.dispatch("set_mode", json.dumps({"mode": "furious"}), tool_call_id="c")
    payload = json.loads(result["content"])
    assert "error" in payload
    assert "calm" in payload["error"] and "excited" in payload["error"]
    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    assert spool.drain() == []


def test_set_mode_any_mode_accepted_when_no_modes_are_known(tmp_path) -> None:
    """With no known_modes wired, the tool submits and lets the engine-side
    driver be the source of truth (mirrors apply_pose degrading cleanly with
    no seam — here the seam is 'no known catalog', not 'no producer')."""
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, await_timeout=0.05)
    result = reg.dispatch("set_mode", json.dumps({"mode": "anything"}), tool_call_id="d")
    payload = json.loads(result["content"])
    assert "error" not in payload
    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    assert [c["op"] for c in spool.drain()] == ["set_mode"]


def test_set_inhibition_unknown_name_is_rejected(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    result = reg.dispatch("set_inhibition", json.dumps({"behaviors": ["nope"]}), tool_call_id="e")
    payload = json.loads(result["content"])
    assert "error" in payload
    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    assert spool.drain() == []


def test_set_inhibition_missing_behaviors_is_a_malformed_argument_error(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    result = reg.dispatch("set_inhibition", json.dumps({}), tool_call_id="f")
    payload = json.loads(result["content"])
    assert "error" in payload


def test_run_behavior_unknown_param_key_is_rejected(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG)
    result = reg.dispatch(
        "run_behavior", json.dumps({"name": "nod", "params": {"bogus": 1.0}}), tool_call_id="g"
    )
    payload = json.loads(result["content"])
    assert "error" in payload
    assert "bogus" in payload["error"]


# --------------------------------------------------------------------------- #
# Valid calls write a valid atomic spool command                              #
# --------------------------------------------------------------------------- #


def test_run_behavior_valid_call_writes_a_drainable_spool_command(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG, await_timeout=0.05)
    result = reg.dispatch(
        "run_behavior", json.dumps({"name": "nod", "params": {"amp": 5.0}}), tool_call_id="h"
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is None  # no engine draining concurrently -> unconfirmed
    assert "submitted" in payload

    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    drained = spool.drain()
    assert len(drained) == 1
    cmd = drained[0]
    assert cmd["op"] == "run_behavior"
    assert cmd["name"] == "nod"
    assert cmd["params"] == {"amp": 5.0}
    assert cmd["cmd_id"] == payload["submitted"]


def test_declare_goal_omitted_goal_submits_a_clear_command(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG, await_timeout=0.05)
    reg.dispatch("declare_goal", json.dumps({}), tool_call_id="i")

    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    drained = spool.drain()
    assert len(drained) == 1
    assert drained[0]["op"] == "declare_goal"
    assert drained[0]["goal"] is None


def test_set_inhibition_empty_list_submits_a_clear_command(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG, await_timeout=0.05)
    reg.dispatch("set_inhibition", json.dumps({"behaviors": []}), tool_call_id="j")

    spool = control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path)
    drained = spool.drain()
    assert drained[0]["op"] == "set_inhibition"
    assert drained[0]["behaviors"] == []


def test_spool_dir_isolates_two_registrations_from_each_other(tmp_path) -> None:
    a_dir = tmp_path / "a"
    b_dir = tmp_path / "b"
    reg_a = ToolRegistry()
    reg_b = ToolRegistry()
    register_intent_tools(reg_a, spool_dir=a_dir, catalog=_SMALL_CATALOG, await_timeout=0.05)
    register_intent_tools(reg_b, spool_dir=b_dir, catalog=_SMALL_CATALOG, await_timeout=0.05)

    reg_a.dispatch("run_behavior", json.dumps({"name": "nod"}), tool_call_id="k")

    assert control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=a_dir).drain() != []
    assert control_mod.CommandSpool(namespace=INTENT_NAMESPACE, root=b_dir).drain() == []


# --------------------------------------------------------------------------- #
# Full round trip: tool submit -> IntentDriver drains + applies               #
# --------------------------------------------------------------------------- #


def test_full_round_trip_tool_submit_then_driver_applies(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG, await_timeout=0.05)

    result = reg.dispatch(
        "declare_goal", json.dumps({"goal": "nod", "params": {}}), tool_call_id="m"
    )
    payload = json.loads(result["content"])
    assert payload["ok"] is None  # not yet applied — no engine was draining

    driver = IntentDriver(root=tmp_path)
    ctx = _RecordingCtx(now=1.0, tick=1)
    driver.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["nod"]
    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["goal"]["name"] == "nod"

    # Submit + immediately drain manually to simulate the engine loop answering
    # within the await window (the tool's own polling just re-reads the result
    # file, so draining once before the timeout expires is enough).
    cmd_id = control_mod.submit(
        "run_behavior",
        namespace=INTENT_NAMESPACE,
        root=tmp_path,
        name="shake",
        params={},
        lifetime=None,
    )
    driver.on_tick(ctx)
    confirmed = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=tmp_path, timeout=0.2
    )
    assert confirmed["ok"] is True
    assert confirmed["name"] == "shake"


# --------------------------------------------------------------------------- #
# Import boundary                                                             #
# --------------------------------------------------------------------------- #


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_intent_tools_module_does_not_import_engine_or_rule_engine() -> None:
    """intent_tools.py talks only to the spool (control.py) + the library — never
    the engine or the rules evaluator, mirroring tools.py's own boundary discipline."""
    for name in _imported_modules(intent_tools_mod):
        assert "behavior.engine" not in name, f"must not import the engine ({name!r})"
        assert "behavior.rule_engine" not in name, f"must not import rule_engine ({name!r})"


def test_intent_tools_module_never_edits_tools_py() -> None:
    """Composition-only: intent_tools.py imports tools.py's PUBLIC surface, and
    tools.py has no knowledge of intent_tools.py at all (checked from the other
    side by test_speech_tools.py's own boundary tests staying green)."""
    import reachy.speech.tools as tools_mod

    for name in _imported_modules(tools_mod):
        assert "intent_tools" not in name


def test_dispatch_returns_openai_tool_result_message_shape(tmp_path) -> None:
    reg = ToolRegistry()
    register_intent_tools(reg, spool_dir=tmp_path, catalog=_SMALL_CATALOG, await_timeout=0.05)
    result = reg.dispatch("run_behavior", json.dumps({"name": "nod"}), tool_call_id="zz")
    assert result["role"] == "tool"
    assert result["tool_call_id"] == "zz"
    assert isinstance(result["content"], str)
    json.loads(result["content"])  # must be valid JSON
