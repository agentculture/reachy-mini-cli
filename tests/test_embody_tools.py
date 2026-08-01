"""The embodiment layer's direct-operation action set (task t7).

Four action classes, five tools, and one containment claim: *every* tool wraps a
surface that already exists and already validates, so the layer adds reach
without adding a new way to reach. This module pins the mapping and the rule
authoring contract; ``tests/test_embody_redteam.py`` pins the refusals and the
no-shell property.

Nothing here needs a robot, an engine, a network, or an LLM: the intents spool
is a directory, the rules overlay is a file, and the voice is two injected
callables.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from reachy.behavior import control as control_mod
from reachy.behavior import rules as rules_mod
from reachy.behavior.goto_intent import GOTO
from reachy.behavior.intents import INTENT_NAMESPACE, RUN_BEHAVIOR
from reachy.embody import tools as embody_tools
from reachy.embody.tools import (
    ACTION_SET,
    CREATE_RULE,
    HARMONICS,
    RULE_ID_PREFIX,
    SANCTIONED_SURFACES,
    SPEAK,
    EmbodyToolRegistry,
    list_embody_rules,
)

# --------------------------------------------------------------------------- #
# Fixtures                                                                    #
# --------------------------------------------------------------------------- #

#: An operator-authored overlay with comments, odd spacing, and BOTH rule kinds
#: — the thing the layer must never rewrite. Byte-identity is only a meaningful
#: claim against a file a formatter would visibly change.
OPERATOR_OVERLAY = """\
# ------------------------------------------------------------------ #
# Ori's own rules. Hand-tuned. Do not reformat.                       #
# ------------------------------------------------------------------ #

[[react]]
id = "operator-greeting"
when = { field = "face", op = "is_true" }
run    = "nod"
duration_s = 2.0
cooldown_s = 7.5
say = "Hello there."

# a deliberately odd stanza: extra blank lines and aligned '='


[[inhibit]]
id      = "operator-quiet"
when    = { field = "rms", op = "gt", value = 0.9 }
disable = ["nod", "shake"]
"""


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Point every state-dir consumer (spool, overlay, reload spool) at tmp."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    return tmp_path


@pytest.fixture
def overlay(state_dir: Path) -> Path:
    """A populated, operator-authored overlay at its real resolved location."""
    path = rules_mod.overlay_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(OPERATOR_OVERLAY, encoding="utf-8")
    return path


class RecordingSeam:
    """A voice seam that records instead of speaking."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def __call__(self, text: str) -> str:
        self.calls.append(text)
        return "played"


class RecordingReload:
    """A reload seam that records instead of touching the reload spool."""

    def __init__(self, result: dict | None = None) -> None:
        self.calls: list[float] = []
        self._result = result

    def __call__(self, timeout: float) -> dict | None:
        self.calls.append(timeout)
        return self._result


def build_registry(**kwargs) -> EmbodyToolRegistry:
    kwargs.setdefault("speak", RecordingSeam())
    kwargs.setdefault("harmonics", RecordingSeam())
    kwargs.setdefault("reload_seam", RecordingReload())
    kwargs.setdefault("await_timeout", 0.0)
    return EmbodyToolRegistry(**kwargs)


def content(result: dict) -> dict:
    """The parsed tool-result content (every handler returns a JSON object)."""
    assert result["role"] == "tool"
    return json.loads(result["content"])


def spooled(root: Path) -> list[dict]:
    """Every command sitting in the intents spool, in submission order."""
    commands_dir = control_mod.commands_dir(INTENT_NAMESPACE, root=root)
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(commands_dir.iterdir())
        if p.suffix == ".json"
    ]


# --------------------------------------------------------------------------- #
# 1 — the action set is closed, and every member wraps a real surface         #
# --------------------------------------------------------------------------- #


def test_the_action_set_is_exactly_the_direct_operation_tools(state_dir: Path) -> None:
    """Pinned by EQUALITY, so a sixth tool and a deleted tool both fail loudly.

    The spec's action set is four CLASSES: move the head/antennas/body, make a
    sound, run a set of movements and sounds, create a new rule-triggered
    action. "Make a sound" is two tools for the same reason ``reachy.speech.tools``
    registers two — the agent picks TTS or the melodic voice per utterance,
    instead of the process picking one for its whole life.
    """
    registry = build_registry()
    assert registry.names() == list(ACTION_SET)
    assert set(ACTION_SET) == {GOTO, SPEAK, HARMONICS, RUN_BEHAVIOR, CREATE_RULE}


def test_every_tool_names_an_existing_sanctioned_surface() -> None:
    """``SANCTIONED_SURFACES`` is the 1:1 claim, and it must RESOLVE.

    A tool that wraps nothing (or wraps a surface that has since been renamed)
    is exactly the failure this catches: every dotted name in the table is
    imported and looked up here, so the claim cannot rot into a comment.
    """
    import importlib

    assert set(SANCTIONED_SURFACES) == set(ACTION_SET)
    for tool_name, dotted_names in SANCTIONED_SURFACES.items():
        assert dotted_names, f"{tool_name} claims no surface"
        for dotted in dotted_names:
            module_name, _, attribute = dotted.rpartition(".")
            module = importlib.import_module(module_name)
            assert hasattr(module, attribute), f"{tool_name}: {dotted} does not exist"


def test_the_registry_refuses_a_tool_outside_the_set(state_dir: Path) -> None:
    registry = build_registry()
    result = registry.dispatch("shell", json.dumps({"command": "rm -rf /"}))
    body = content(result)
    assert body["ok"] is False
    assert body["refusal"] == embody_tools.REFUSAL_UNKNOWN_TOOL


def test_the_registry_is_closed_by_construction(state_dir: Path) -> None:
    """No ``register`` — the containment claim is structural, not a convention.

    ``reachy.speech.tools.ToolRegistry`` deliberately EXPOSES ``register`` so
    the forge can hot-add a generated skill. The layer must not have that door
    at all, so its registry's public surface is pinned by equality.
    """
    registry = build_registry()
    public = sorted(name for name in dir(registry) if not name.startswith("_"))
    assert public == ["dispatch", "names", "tools"]


def test_the_published_definitions_are_openai_function_tools(state_dir: Path) -> None:
    registry = build_registry()
    definitions = registry.tools()
    assert [d["function"]["name"] for d in definitions] == list(ACTION_SET)
    for definition in definitions:
        assert definition["type"] == "function"
        assert definition["function"]["description"].strip()
        assert definition["function"]["parameters"]["type"] == "object"


# --------------------------------------------------------------------------- #
# 2 — motion + behavior ride the intents spool the engine already drains      #
# --------------------------------------------------------------------------- #


def test_goto_writes_the_goto_kind_into_the_intents_spool(state_dir: Path) -> None:
    registry = build_registry(spool_root=state_dir)
    result = registry.dispatch(GOTO, json.dumps({"head": {"pitch": -8.0}, "duration": 1.5}))
    body = content(result)
    # No engine is draining, so the honest outcome is "submitted, unconfirmed".
    assert body["ok"] is None
    assert body["submitted"]

    commands = spooled(state_dir)
    assert len(commands) == 1
    assert commands[0]["op"] == GOTO
    assert commands[0]["head"] == {"pitch": -8.0}
    assert commands[0]["duration"] == 1.5


def test_run_behavior_writes_the_run_behavior_kind_into_the_intents_spool(
    state_dir: Path,
) -> None:
    registry = build_registry(spool_root=state_dir)
    result = registry.dispatch(RUN_BEHAVIOR, json.dumps({"name": "nod", "duration": 3.0}))
    assert content(result)["ok"] is None

    commands = spooled(state_dir)
    assert len(commands) == 1
    assert commands[0]["op"] == RUN_BEHAVIOR
    assert commands[0]["name"] == "nod"
    assert commands[0]["lifetime"]["duration"] == 3.0


def test_a_confirmed_engine_result_is_reported_verbatim(state_dir: Path, monkeypatch) -> None:
    """When an engine DOES answer, the layer reports its outcome, not its own."""
    registry = build_registry(spool_root=state_dir, await_timeout=1.0)
    seen: list[str] = []

    def fake_await(cmd_id, **_kwargs):
        seen.append(cmd_id)
        return {"ok": True, "op": GOTO, "id": "goto:1", "label": "goto"}

    monkeypatch.setattr(control_mod, "await_result", fake_await)
    body = content(registry.dispatch(GOTO, json.dumps({"body_yaw": 5.0})))

    assert seen
    assert body == {"ok": True, "op": GOTO, "id": "goto:1", "label": "goto"}


# --------------------------------------------------------------------------- #
# 3 — sound is two injected seams, never an audio import                      #
# --------------------------------------------------------------------------- #


def test_speak_and_harmonics_call_only_their_injected_seams(state_dir: Path) -> None:
    speak, harmonics = RecordingSeam(), RecordingSeam()
    registry = build_registry(speak=speak, harmonics=harmonics)

    assert content(registry.dispatch(SPEAK, json.dumps({"text": "hello"})))["ok"] is True
    assert content(registry.dispatch(HARMONICS, json.dumps({"text": "la la"})))["ok"] is True

    assert speak.calls == ["hello"]
    assert harmonics.calls == ["la la"]


def test_a_missing_voice_seam_is_a_named_refusal_not_a_crash(state_dir: Path) -> None:
    registry = EmbodyToolRegistry(speak=None, harmonics=None, reload_seam=RecordingReload())
    body = content(registry.dispatch(SPEAK, json.dumps({"text": "hello"})))
    assert body["ok"] is False
    assert body["refusal"] == embody_tools.REFUSAL_NO_VOICE


# --------------------------------------------------------------------------- #
# 4 — rule authoring: prefixed, atomic, and never over the operator's file    #
# --------------------------------------------------------------------------- #


def a_rule(rule_id: str = "embody-wave-back", **overrides) -> dict:
    payload = {
        "id": rule_id,
        "when": {"field": "face", "op": "is_true"},
        "run": "gaze-hold",
        "cooldown_s": 6.0,
    }
    payload.update(overrides)
    return payload


def test_rule_authoring_requires_the_embody_prefix(overlay: Path) -> None:
    before = overlay.read_bytes()
    registry = build_registry()
    body = content(registry.dispatch(CREATE_RULE, json.dumps(a_rule("greet-the-operator"))))
    assert body["ok"] is False
    assert body["refusal"] == embody_tools.REFUSAL_RULE_NAMESPACE
    assert RULE_ID_PREFIX in body["error"]
    assert overlay.read_bytes() == before


def test_rule_authoring_writes_temp_then_rename(overlay: Path, monkeypatch) -> None:
    """Atomic by construction: one ``os.replace`` from a sibling temp file."""
    renames: list[tuple[str, str]] = []
    real_replace = os.replace

    def spy(src, dst):
        renames.append((str(src), str(dst)))
        real_replace(src, dst)

    monkeypatch.setattr(embody_tools.os, "replace", spy)

    registry = build_registry()
    assert content(registry.dispatch(CREATE_RULE, json.dumps(a_rule())))["ok"] is True

    assert len(renames) == 1
    src, dst = renames[0]
    assert dst == str(overlay)
    assert src != dst
    assert Path(src).parent == overlay.parent  # same dir => same fs => atomic
    assert sorted(p.name for p in overlay.parent.iterdir()) == [overlay.name]


def test_operator_rules_stay_byte_identical_across_a_write_sequence(overlay: Path) -> None:
    """The load-bearing claim: the layer only ever appends to its own block.

    Three writes plus one refusal, including replacing a rule the layer wrote
    earlier — the sequence most likely to reformat a file — then the operator's
    region is compared BYTE for byte, and the operator's rules are compared as
    validated dataclasses.
    """
    original_text = overlay.read_text(encoding="utf-8")
    original_config = rules_mod.load_rules(overlay, include_shipped=False)
    operator_rules = {
        rule.id: rule
        for rule in (*original_config.react, *original_config.inhibit)
        if not rule.id.startswith(RULE_ID_PREFIX)
    }
    assert set(operator_rules) == {"operator-greeting", "operator-quiet"}

    registry = build_registry()
    for payload in (
        a_rule("embody-wave-back"),
        a_rule("embody-lean-in", run="thoughtful"),
        a_rule("embody-wave-back", run="body-turn-hold", cooldown_s=9.0),  # replace
    ):
        assert content(registry.dispatch(CREATE_RULE, json.dumps(payload)))["ok"] is True
    # ... and one refused write, which must be just as inert.
    assert (
        content(registry.dispatch(CREATE_RULE, json.dumps(a_rule("embody-bad", run="nope"))))["ok"]
        is False
    )

    final_text = overlay.read_text(encoding="utf-8")
    head = final_text.split(embody_tools.MANAGED_BEGIN)[0]
    assert head.startswith(original_text)
    assert head.rstrip("\n") == original_text.rstrip("\n")

    final_config = rules_mod.load_rules(overlay, include_shipped=False)
    final_by_id = {rule.id: rule for rule in (*final_config.react, *final_config.inhibit)}
    for rule_id, rule in operator_rules.items():
        assert final_by_id[rule_id] == rule


def test_rewriting_the_same_rule_is_a_fixed_point(overlay: Path) -> None:
    """Byte-identity is not enough on its own: the file must not GROW either.

    The first cut of the managed block carried its own leading and trailing
    newline, so every write added one more blank line — invisible in a
    ``startswith`` check, unbounded in a file the robot re-reads forever. This
    pins the reassembly as a fixed point.
    """
    registry = build_registry()
    payload = json.dumps(a_rule())
    assert content(registry.dispatch(CREATE_RULE, payload))["ok"] is True
    once = overlay.read_bytes()
    for _ in range(3):
        assert content(registry.dispatch(CREATE_RULE, payload))["ok"] is True
    assert overlay.read_bytes() == once


def test_an_authored_rule_survives_a_round_trip_through_the_rules_loader(overlay: Path) -> None:
    """What the layer writes is what the engine reads — quoting included."""
    registry = build_registry()
    payload = a_rule(say='he said "hello" \\ then left', duration_s=4.0)
    assert content(registry.dispatch(CREATE_RULE, json.dumps(payload)))["ok"] is True

    written = {r.id: r for r in rules_mod.load_rules(overlay, include_shipped=False).react}
    rule = written["embody-wave-back"]
    assert rule.say == 'he said "hello" \\ then left'
    assert rule.behavior == "gaze-hold"
    assert rule.duration_s == 4.0
    assert rule.cooldown_s == 6.0


def test_layer_rules_are_enumerable_and_removable_by_prefix(overlay: Path) -> None:
    registry = build_registry()
    for rule_id in ("embody-wave-back", "embody-lean-in"):
        assert content(registry.dispatch(CREATE_RULE, json.dumps(a_rule(rule_id))))["ok"] is True

    assert list_embody_rules(overlay) == ("embody-lean-in", "embody-wave-back")

    # Removable as a set: dropping the managed block leaves the operator's file.
    text = overlay.read_text(encoding="utf-8")
    overlay.write_text(text.split(embody_tools.MANAGED_BEGIN)[0], encoding="utf-8")
    assert list_embody_rules(overlay) == ()
    assert {r.id for r in rules_mod.load_rules(overlay, include_shipped=False).react} == {
        "operator-greeting"
    }


def test_layer_rules_persist_after_the_layer_stops(overlay: Path) -> None:
    """A confirmed product decision: the robot keeps what it was taught.

    A fresh registry (i.e. a later layer process) reads back exactly what an
    earlier one wrote — there is no deletion-on-exit hook anywhere in the
    module, which is what the second half asserts.
    """
    assert content(build_registry().dispatch(CREATE_RULE, json.dumps(a_rule())))["ok"] is True
    assert list_embody_rules(overlay) == ("embody-wave-back",)
    assert list_embody_rules(overlay) == ("embody-wave-back",)  # a later process
    for forbidden in ("cleanup", "remove_rule", "delete_rule", "purge"):
        assert not hasattr(embody_tools, forbidden)


def test_rule_authoring_submits_a_reload_so_the_change_goes_live(overlay: Path) -> None:
    reload_seam = RecordingReload(result={"ok": True, "react": 3})
    registry = build_registry(reload_seam=reload_seam)
    body = content(registry.dispatch(CREATE_RULE, json.dumps(a_rule())))
    assert body["ok"] is True
    assert body["reload"] == {"ok": True, "react": 3}
    assert len(reload_seam.calls) == 1


def test_the_default_reload_seam_writes_the_real_reload_spool(overlay: Path) -> None:
    """No injected seam: the write lands in the same spool ``behavior reload`` uses."""
    registry = EmbodyToolRegistry(speak=RecordingSeam(), await_timeout=0.0)
    assert content(registry.dispatch(CREATE_RULE, json.dumps(a_rule())))["ok"] is True

    commands = sorted((overlay.parent / "reload" / "commands").iterdir())
    assert len(commands) == 1
    assert "cmd_id" in json.loads(commands[0].read_text(encoding="utf-8"))


def test_authoring_onto_a_missing_overlay_creates_it(state_dir: Path) -> None:
    path = rules_mod.overlay_rules_path()
    assert not path.exists()
    registry = build_registry()
    assert content(registry.dispatch(CREATE_RULE, json.dumps(a_rule())))["ok"] is True
    assert list_embody_rules(path) == ("embody-wave-back",)
