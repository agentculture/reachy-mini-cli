"""A forged skill's ONE actuator: an intent submitted through the shared admission path.

Why this file exists
--------------------
``reachy/forge/`` promises "the robot gains a new callable tool". In the OLD host
(``listen run --live --cognition agent``, deleted by this arc) the forged ctx bound
REAL seams — a live ``ExpressionProducer.express``, a real TTS/harmonic engine, real
playback — so a forged skill could actually move and speak. In the NEW host
(``agent attach``) those same seams are deliberately **publish-only** (``_silent_synth``
/ ``_no_play`` / ``_no_express``), because the external attach client never opens the
robot's SDK: the agent DECLARES INTENTS and the symbolic runtime executes them.

So three of forge's five ctx attributes became no-ops and a forged skill could publish
text and hold scratch state, and nothing else. This file pins the fix: ONE new sanctioned
ctx attribute, :meth:`~reachy.forge.activate.ForgedSkillContext.run_behavior`, which
submits a ``run_behavior`` intent through **exactly** the handler
:mod:`reachy.speech.intent_tools` already uses for the agent's own ``run_behavior`` tool.

Scope argument (asserted, not just asserted-in-prose)
-----------------------------------------------------
The intents spool carries four kinds. A forged skill gets exactly ONE of them:

* ``run_behavior``  — ADMITTED. A one-time, BOUNDED admission with a natural end. This
  is the movement primitive; it is what makes a forged skill genuinely useful.
* ``declare_goal``  — REFUSED. A STANDING, deliberately-indefinite admission that
  outlives the skill's ``execute`` and re-admits itself forever. Handing generated code
  the one sanctioned unbounded surface launders the bounded-lifetime invariant.
* ``set_inhibition`` — REFUSED. REPLACES the whole inhibited set with no natural expiry;
  generated code could mute the robot's reactive layer indefinitely.
* ``set_mode``      — REFUSED. A global rules-config swap affecting every rule-admitted
  behavior from that point on.

None of the three refused kinds is reachable: there is no ctx method for them and the AST
validator rejects the attribute by name (see the criterion-3 section below).

Everything here runs against an isolated ``tmp_path`` spool with the REAL engine-side
:class:`~reachy.behavior.intents.IntentDriver` draining it — no robot, no network, no LLM.
"""

from __future__ import annotations

import json
import textwrap
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reachy.behavior import library
from reachy.behavior.intents import INTENT_NAMESPACE, IntentDriver
from reachy.forge.activate import ForgedSkillContext, build_ctx_seams, import_forged_execute
from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS, validate
from reachy.speech.intent_tools import make_run_behavior_effector, register_intent_tools
from reachy.speech.tools import ToolRegistry

# A bounded library entry (looping=False, default_duration=5.0) — the shape a
# run_behavior admission is allowed to take.
BOUNDED = "gaze-hold"
# A looping-default entry with default_duration=None — the UNBOUNDED shape
# reachy.behavior.intents._validated_lifetime refuses on the shared admission path.
UNBOUNDED = "nod"


@dataclass
class _RecordingCtx:
    """A duck-typed TickContext (mirrors tests/test_speech_intent_tools.py)."""

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


def _effector(spool: Path):
    return make_run_behavior_effector(spool_dir=spool, await_timeout=0.0)


def _ctx(spool: Path) -> ForgedSkillContext:
    """The restricted ctx a forged execute() receives, wired like `agent attach` wires it."""
    return ForgedSkillContext(run_behavior=_effector(spool))


def _pending(spool: Path) -> list[dict]:
    """Every intent command currently sitting in the spool, in submission order."""
    d = spool / "behavior" / INTENT_NAMESPACE / "commands"
    if not d.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(d.iterdir()) if p.suffix == ".json"]


def _drain(spool: Path) -> _RecordingCtx:
    """Run the REAL engine-side driver over the spool for one tick; return its ctx."""
    driver = IntentDriver(root=spool)
    ctx = _RecordingCtx()
    driver.on_tick(ctx)
    return ctx


# --------------------------------------------------------------------------- #
# CRITERION 1 — a forged skill submits a validated intent; the runtime runs it #
# --------------------------------------------------------------------------- #


def test_forged_skill_submits_a_run_behavior_intent_the_runtime_executes(tmp_path):
    """End to end over the REAL forge machinery: a generated ``executor.py`` clears the
    AST validator, is imported by :func:`import_forged_execute`, and its ``execute`` runs
    over the restricted ctx -> intents spool -> ``IntentDriver.on_tick`` -> ``ctx.admit``.
    The robot actually moves, through the runtime, without the skill touching the SDK."""
    skill_dir = _staged(
        tmp_path,
        """
        def execute(params, ctx):
            return ctx.run_behavior("gaze-hold", duration=2.0)
        """,
    )
    ok, reasons = validate(skill_dir)
    assert ok, reasons  # the gate ALLOWS the effector — that is the point of t29

    execute = import_forged_execute(skill_dir / "executor.py", "mover")
    result = execute({}, _ctx(tmp_path))

    # No engine was draining at submit time, so the tool path's honest degraded result.
    assert json.loads(result)["ok"] is None

    ctx = _drain(tmp_path)
    assert [b.name for b in ctx.admits] == [BOUNDED], "the runtime never admitted the intent"
    admitted = ctx.admits[0]
    assert admitted.lifetime.looping is False
    assert admitted.lifetime.duration == 2.0


def test_the_effector_writes_the_same_spool_command_as_the_agents_own_tool(tmp_path):
    """SAME admission path, proven on the wire: the command the forged effector writes is
    byte-identical (modulo the random cmd_id) to the one the registered `run_behavior`
    tool writes for the same call."""
    registry = ToolRegistry()
    register_intent_tools(registry, spool_dir=tmp_path, await_timeout=0.0)
    registry.dispatch("run_behavior", json.dumps({"name": BOUNDED, "duration": 2.0}), "c1")
    _ctx(tmp_path).run_behavior(BOUNDED, duration=2.0)

    via_tool, via_forge = _pending(tmp_path)
    via_tool.pop("cmd_id")
    via_forge.pop("cmd_id")
    assert via_forge == via_tool


def test_the_effector_reuses_intent_tools_own_handler_factory(monkeypatch, tmp_path):
    """Not a parallel implementation: the effector is built from the SAME private handler
    factory `_run_behavior_tool` uses, so the two can never drift apart."""
    import reachy.speech.intent_tools as mod

    calls: list = []
    real = mod._make_run_behavior_handler

    def _spy(*a, **kw):
        calls.append((a, kw))
        return real(*a, **kw)

    monkeypatch.setattr(mod, "_make_run_behavior_handler", _spy)
    mod.make_run_behavior_effector(spool_dir=tmp_path, await_timeout=0.0)
    assert calls, "make_run_behavior_effector must build its handler via _make_run_behavior_handler"


def test_params_and_unknown_names_are_validated_before_the_spool_write(tmp_path):
    """Fail-closed, goto_intent-style: an unknown behavior / unknown param / non-numeric
    value is REFUSED with the valid keys named — never clamped, never written."""
    ctx = _ctx(tmp_path)

    out = ctx.run_behavior("definitely-not-a-behavior")
    assert out.startswith("[run_behavior refused:")
    assert BOUNDED in out, "the refusal must name the valid keys"

    out = ctx.run_behavior(BOUNDED, params={"not_a_param": 1.0})
    assert out.startswith("[run_behavior refused:")

    out = ctx.run_behavior(BOUNDED, params={"hold": "not-a-number"})
    assert out.startswith("[run_behavior refused:")

    out = ctx.run_behavior(BOUNDED, duration="soon")
    assert out.startswith("[run_behavior refused:")

    assert _pending(tmp_path) == [], "a refused call must never reach the spool"


def test_an_absent_effector_seam_degrades_instead_of_raising():
    """A ctx built with no run_behavior seam (a bare box, a partial composition) reports
    unavailable rather than crashing the forged skill."""
    assert ForgedSkillContext().run_behavior(BOUNDED) == "[run_behavior unavailable]"


def test_a_raising_effector_seam_never_escapes_into_forged_code(tmp_path):
    def _boom(*_a, **_kw):
        raise RuntimeError("spool is on fire")

    out = ForgedSkillContext(run_behavior=_boom).run_behavior(BOUNDED)
    assert out.startswith("[run_behavior refused:")
    assert "spool is on fire" in out


# --------------------------------------------------------------------------- #
# CRITERION 2 — the ctx surface stays FAIL-CLOSED and moves only deliberately  #
# --------------------------------------------------------------------------- #


def test_the_ctx_surface_is_exactly_the_validators_allow_list():
    """The load-bearing gate, locked in BOTH directions: a new reachable ctx attribute
    that the validator does not allow-list, OR an allow-listed name with no ctx method
    behind it, fails here. Changing the surface must therefore be a deliberate edit to
    DEFAULT_ALLOWED_CTX_ATTRS *and* to this expectation, never an accident."""
    public = {name for name in dir(ForgedSkillContext) if not name.startswith("_")}
    assert public == set(DEFAULT_ALLOWED_CTX_ATTRS)


def test_the_sanctioned_surface_is_exactly_these_six_names():
    """The literal, reviewable surface — spelled out so a diff to it is visible in review."""
    assert set(DEFAULT_ALLOWED_CTX_ATTRS) == {
        "speak",
        "harmonics",
        "express",
        "state_get",
        "state_update",
        "run_behavior",
    }


def test_an_instance_exposes_no_extra_public_attribute(tmp_path):
    """Instance state (the injected seams, the scratch dict) stays private — an instance's
    reachable surface is the class surface, nothing more."""
    ctx = build_ctx_seams(
        speak_engine=_InertVoice(),
        harmonic_engine=_InertVoice(),
        play=lambda *_a, **_kw: None,
        express=lambda _e: None,
        run_behavior=_effector(tmp_path),
    )
    assert {n for n in dir(ctx) if not n.startswith("_")} == set(DEFAULT_ALLOWED_CTX_ATTRS)
    assert all(n.startswith("_") for n in vars(ctx)), "instance state must not be public"


class _InertVoice:
    """A publish-only VoiceEngine stand-in (the shape build_ctx_seams needs)."""

    name = "inert"
    samplerate = 16000

    def synthesize(self, _text: str) -> bytes:
        return b""


# --------------------------------------------------------------------------- #
# CRITERION 3 — no SDK, no direct motion, no bypass of the admission path      #
# --------------------------------------------------------------------------- #


def _staged(tmp_path: Path, source: str) -> Path:
    skill_dir = tmp_path / "skill"
    skill_dir.mkdir(exist_ok=True)
    (skill_dir / "SKILL.md").write_text("---\nname: probe\ndescription: probe\n---\nbody\n")
    (skill_dir / "executor.py").write_text(textwrap.dedent(source).strip() + "\n")
    return skill_dir


@pytest.mark.parametrize(
    "body",
    [
        "import reachy_mini\n\ndef execute(params, ctx):\n    return reachy_mini.ReachyMini()",
        "from reachy.motion.queue import MotionQueue\n\n"
        "def execute(params, ctx):\n    return MotionQueue()",
        "from reachy.behavior import control\n\n"
        "def execute(params, ctx):\n    return control.submit('run_behavior')",
    ],
)
def test_forged_code_cannot_reach_the_sdk_the_motion_queue_or_the_raw_spool(tmp_path, body):
    """The AST gate refuses the import BEFORE the module is ever imported or run — a
    forged skill has no path to the robot except ctx.run_behavior."""
    ok, reasons = validate(_staged(tmp_path, body))
    assert not ok
    assert any("not allowed" in r for r in reasons), reasons


@pytest.mark.parametrize("attr", ["declare_goal", "set_mode", "set_inhibition"])
def test_the_other_three_intent_kinds_are_unreachable_from_forged_code(tmp_path, attr):
    """A forged skill gets ONE intent kind. The standing/global/indefinite kinds are
    neither on the ctx nor allow-listed, so the gate refuses them by name."""
    assert not hasattr(ForgedSkillContext, attr)
    assert attr not in DEFAULT_ALLOWED_CTX_ATTRS
    ok, reasons = validate(
        _staged(tmp_path, f"def execute(params, ctx):\n    return ctx.{attr}('x')")
    )
    assert not ok
    assert any(f"ctx.{attr}" in r for r in reasons), reasons


@pytest.mark.parametrize("attr", ["_run_behavior", "_state", "_speak"])
def test_forged_code_cannot_reach_the_private_seams_behind_the_ctx(tmp_path, attr):
    ok, reasons = validate(_staged(tmp_path, f"def execute(params, ctx):\n    return ctx.{attr}"))
    assert not ok
    assert any(f"ctx.{attr}" in r for r in reasons), reasons


def test_the_bounded_lifetime_invariant_still_refuses_an_unbounded_forged_admission(tmp_path):
    """The incident guard holds on this surface too: `nod` is a looping-default entry with
    no default duration, so the resulting lifetime is looping+None — the shared
    admission path (intents._validated_lifetime) REFUSES it, exactly as it refuses the
    agent's own run_behavior tool. Nothing is admitted; the channel is never held."""
    ctx = _ctx(tmp_path)
    assert library.LIBRARY[UNBOUNDED].looping is True
    assert library.LIBRARY[UNBOUNDED].default_duration is None

    ctx.run_behavior(UNBOUNDED)  # submitted; the ENGINE is the authority that refuses
    tick = _drain(tmp_path)
    assert tick.admits == [], "an unbounded forged admission must never reach the robot"
    blocked = [e for e in tick.events if e["type"] == "intent.blocked"]
    assert blocked and "unbounded lifetime" in blocked[0]["reason"]


def test_the_same_behavior_bounded_is_admitted(tmp_path):
    """The refusal above is about the unbounded SHAPE, not about the behavior: the same
    entry with an explicit duration is admitted normally."""
    _ctx(tmp_path).run_behavior(UNBOUNDED, duration=1.5)
    tick = _drain(tmp_path)
    assert [b.name for b in tick.admits] == [UNBOUNDED]
    assert tick.admits[0].lifetime.duration == 1.5


def test_the_effector_exposes_no_loop_argument(tmp_path):
    """`loop` is the one run_behavior argument that can BUILD the unbounded shape on
    purpose. The effector simply does not accept it (the agent's own tool does), so a
    forged skill cannot even ask — narrowest surface, not merely a rejected value."""
    import inspect

    from reachy.forge.activate import wrap_executor

    sig = inspect.signature(_effector(tmp_path))
    assert list(sig.parameters) == ["name", "params", "duration"]
    assert "loop" not in inspect.signature(ForgedSkillContext.run_behavior).parameters

    # Forged code that asks for it anyway gets a TypeError — which wrap_executor turns
    # into an error tool-result, so the attempt neither reaches the spool nor crashes
    # the agent's tool loop.
    ctx = ForgedSkillContext(run_behavior=_effector(tmp_path))

    def _execute(_params, c):
        return c.run_behavior(BOUNDED, loop=True)

    out = wrap_executor(_execute, ctx, "loop-probe", timeout=5.0)({})
    assert "unexpected keyword argument 'loop'" in out
    assert _pending(tmp_path) == []


def test_only_run_behavior_commands_can_ever_land_in_the_spool_from_a_forged_skill(tmp_path):
    """Whatever a forged skill does with its one actuator, every command it can produce
    carries op=run_behavior — the three other kinds have no reachable producer."""
    ctx = _ctx(tmp_path)
    for name in (BOUNDED, UNBOUNDED, "thoughtful"):
        ctx.run_behavior(name, duration=1.0)
    assert {cmd["op"] for cmd in _pending(tmp_path)} == {"run_behavior"}


def test_the_forge_package_still_never_imports_the_intent_stack():
    """The effector is an INJECTED callable: reachy.forge stays decoupled from the spool,
    the behavior library and the tool layer, exactly as its docstring promises."""
    import ast
    import inspect

    import reachy.forge.activate as activate_mod

    tree = ast.parse(inspect.getsource(activate_mod))
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
        elif isinstance(node, ast.Import):
            imported.update(a.name for a in node.names)
    for banned in ("reachy.speech", "reachy.behavior"):
        assert not any(name.startswith(banned) for name in imported), imported


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
