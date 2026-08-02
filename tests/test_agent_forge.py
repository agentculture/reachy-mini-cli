"""The forge (runtime self-extension) re-homed onto ``reachy agent attach`` (task t17).

Why this file exists
--------------------
``reachy/forge/`` used to have exactly ONE composition site: ``listen run --live
--cognition agent``. This arc deletes that path, so the forge had to be re-composed onto
``agent attach`` — the surviving sanctioned external-cognition surface.

The four ``tests/test_forge_*.py`` suites exercise the forge package **in isolation**:
they would stay green over a permanently orphaned forge, which is precisely the failure
mode this file exists to prevent. Every test here therefore drives the **re-homed path**
— through :func:`reachy.cli._commands.agent.cmd_agent_attach` /
:func:`~reachy.cli._commands.agent._build_default_engine` — end to end.

Determinism
-----------
:meth:`reachy.forge.client.ForgeClient.dispatch` runs the round-trip on a daemon thread.
Rather than poll for a background effect, these tests inject a ``forge_client_factory``
that wraps a **real** ``ForgeClient`` (real fence parsing, real staging, real AST
validator, real activation, real hot-registration) and runs its worker body inline. Only
the coder-model HTTP call is faked (the injectable ``transport`` seam). The threading
itself is ForgeClient's own concern and is covered by ``tests/test_forge_client.py``.
"""

from __future__ import annotations

import argparse
import ast
import inspect
import textwrap

import pytest

from reachy.cli._commands import agent as agent_mod
from reachy.cli._commands.agent import cmd_agent_attach
from reachy.speech.llm import ToolCall, TurnResult

# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Root the forge staged/active trees under tmp_path (they hang off state_dir())."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _make_attach_args(**kw) -> argparse.Namespace:
    defaults = dict(
        json=False,
        feed="-",
        spool_dir=None,
        await_timeout=0.0,
        max_turns=None,
        max_events=None,
        export=None,
        export_blocks=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _sense_line(**fields) -> str:
    import json

    return json.dumps({"t": "sense", "ts": 1.0, "tick": 1, "doa": 0.0, "speech": True, **fields})


def _scripted_turn_fn(*results: TurnResult):
    """A fake ``turn_fn`` returning *results* in order, then empty turns.

    Also records the ``tools=`` list advertised on every call, so a test can assert what
    the model could see on turn N (the "callable on the NEXT turn" evidence).
    """
    state = {"n": 0}
    advertised: list[list[str]] = []

    def turn_fn(messages, *, tools=None, **kw):  # test double
        advertised.append([t["function"]["name"] for t in (tools or [])])
        i = state["n"]
        state["n"] += 1
        if i < len(results):
            return results[i]
        return TurnResult(content="", tool_calls=[])

    turn_fn.advertised = advertised  # type: ignore[attr-defined]
    return turn_fn


def _call(name: str, **arguments) -> ToolCall:
    import json

    return ToolCall(
        id=f"c-{name}",
        name=name,
        arguments=arguments,
        arguments_json=json.dumps(arguments),
    )


SKILL_MD = textwrap.dedent("""
    ---
    name: greet-back
    description: Greet a person who just said hello.
    ---
    Use when someone greets the robot.
    """).strip()

SAFE_EXECUTOR = textwrap.dedent("""
    def execute(params, ctx):
        ctx.speak("hello back")
        ctx.express("blush")
        ctx.state_update(greeted=True)
        return "greeted-ok"
    """).strip()

# `import os` is on the validator's forbidden list — the fail-closed gate must refuse it
# BEFORE the module is ever imported or executed.
UNSAFE_EXECUTOR = textwrap.dedent("""
    import os

    def execute(params, ctx):
        return os.popen("id").read()
    """).strip()


def _reply(skill_md: str, executor_py: str) -> dict:
    """A chat-completions response body carrying the two fences the forge prompt demands."""
    content = f"```SKILL.md\n{skill_md}\n```\n\n```executor.py\n{executor_py}\n```"
    return {"choices": [{"message": {"content": content}}]}


def _inline_forge_factory(reply: dict, *, seen: list | None = None):
    """A ``forge_client_factory`` building a REAL ForgeClient that dispatches inline.

    Only the HTTP leg is faked; fence parsing, staging, the AST validator, the
    ``forge/staged`` publish, auto-activation and hot-registration are all the real code
    paths the production wiring uses.
    """

    def transport(url, payload, headers, timeout):  # test double
        if seen is not None:
            seen.append(payload)
        return reply

    def factory(publish):
        from reachy.forge import ForgeClient
        from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS

        real = ForgeClient(
            publish=publish,
            allowed_ctx_attrs=set(DEFAULT_ALLOWED_CTX_ATTRS),
            transport=transport,
        )

        class _Inline:
            """Same ``dispatch(goal, improve=...)`` shape, run on the calling thread."""

            def dispatch(self, goal, context=None, improve=None):
                real._run(goal, context or {}, improve)
                return None

        return _Inline()

    return factory


def _engine_factory(turn_fn, tmp_path, *, forge_client_factory=None, captured=None):
    def factory(buffer, export):
        engine = agent_mod._build_default_engine(
            buffer,
            export,
            spool_dir=tmp_path,
            await_timeout=0.0,
            turn_fn=turn_fn,
            forge_client_factory=forge_client_factory,
        )
        if captured is not None:
            captured.append(engine)
        return engine

    return factory


def _tool_names(engine) -> list[str]:
    return [t["function"]["name"] for t in engine._registry.tools()]


# --------------------------------------------------------------------------- #
# The forge tool reaches the attach surface at all                            #
# --------------------------------------------------------------------------- #


def test_attach_engine_advertises_the_forge_tool_by_default(tmp_path):
    """Production wiring (no injection): `agent attach`'s registry carries `forge`."""
    engine = agent_mod._build_default_engine(
        object(), None, spool_dir=tmp_path, await_timeout=0.0, turn_fn=_scripted_turn_fn()
    )
    assert "forge" in _tool_names(engine)


def test_default_forge_client_factory_builds_a_real_forge_client():
    """The default (uninjected) factory is the real ForgeClient — not a stub."""
    from reachy.forge import ForgeClient

    client = agent_mod._default_forge_client_factory(lambda _t, _p: None)
    assert isinstance(client, ForgeClient)


# --------------------------------------------------------------------------- #
# CRITERION 1 — validated fail-closed, activated, callable on the NEXT turn    #
# --------------------------------------------------------------------------- #


def test_forged_skill_via_attach_is_validated_activated_and_callable_next_turn(tmp_path):
    """The whole re-homed loop, end to end, through `agent attach`:

    turn 1 the agent calls the `forge` tool with a goal → the coder reply is parsed,
    staged, run through the AST validator, auto-activated onto disk, and hot-registered
    into the LIVE registry → turn 2 the model is advertised the new tool AND calling it
    actually runs the forged `execute(params, ctx)` over the restricted ctx.
    """
    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("forge", goal="greet people back")]),
        TurnResult(content="", tool_calls=[_call("greet-back")]),
    )
    captured: list = []
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=2)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line(), _sense_line()],
        engine_factory=_engine_factory(
            turn_fn,
            tmp_path,
            forge_client_factory=_inline_forge_factory(_reply(SKILL_MD, SAFE_EXECUTOR)),
            captured=captured,
        ),
    )
    assert rc == 0
    engine = captured[0]

    # Activated: the skill moved staged/ -> active/ on disk.
    active = tmp_path / "forge" / "active" / "greet-back"
    assert (active / "executor.py").is_file(), "forged skill was never activated"
    assert not (tmp_path / "forge" / "staged" / "greet-back").exists()

    # Callable on the NEXT turn: advertised to the model on turn 2 but NOT on turn 1.
    assert "greet-back" not in turn_fn.advertised[0]
    assert "greet-back" in turn_fn.advertised[1], "forged tool must be callable next turn"

    # And actually dispatchable: the forged execute() really ran.
    result = engine._registry.dispatch("greet-back", "{}", "x")
    assert "greeted-ok" in str(result)


def test_forged_ctx_is_built_over_agent_attachs_own_publish_only_seams(tmp_path, monkeypatch):
    """The forged skill's ctx is built from the SAME publish-only seams `agent attach`'s
    built-in speak/harmonics/apply_pose tools use — the external client never opens the
    robot's SDK — and exposes exactly the validator's sanctioned surface."""
    import reachy.forge as forge_pkg
    from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS

    real_build = forge_pkg.build_ctx_seams
    seen: dict = {}

    def _spy(**kwargs):
        seen.update(kwargs)
        return real_build(**kwargs)

    monkeypatch.setattr(forge_pkg, "build_ctx_seams", _spy)

    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("forge", goal="greet people back")])
    )
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    cmd_agent_attach(
        args,
        lines=[_sense_line()],
        engine_factory=_engine_factory(
            turn_fn,
            tmp_path,
            forge_client_factory=_inline_forge_factory(_reply(SKILL_MD, SAFE_EXECUTOR)),
        ),
    )
    assert set(seen) == {"speak_engine", "harmonic_engine", "play", "express", "run_behavior"}
    # Publish-only: the voice seams synthesize nothing and playback is a no-op, exactly
    # like the built-in speak/harmonics tools on this noun.
    assert seen["speak_engine"].synthesize("hi") == b""
    assert seen["harmonic_engine"].synthesize("hi") == b""
    assert seen["play"](b"", samplerate=1) is None
    assert seen["express"]("blush") is None
    # ...and exactly ONE seam that is NOT inert: the intent effector — the sanctioned way
    # a forged skill still reaches the robot now that the publish-only seams cannot.
    # See tests/test_forge_intent_effector.py for its full contract.
    assert callable(seen["run_behavior"])

    ctx = real_build(**seen)
    public = {n for n in dir(ctx) if not n.startswith("_")}
    assert public == set(DEFAULT_ALLOWED_CTX_ATTRS)


MOVING_EXECUTOR = textwrap.dedent("""
    def execute(params, ctx):
        return ctx.run_behavior("gaze-hold", duration=2.0)
    """).strip()


def test_a_forged_skill_reaches_the_robot_through_the_intent_spool(tmp_path):
    """The t29 fix, end to end through the REAL host: a forged skill that calls
    ``ctx.run_behavior`` lands an atomic ``run_behavior`` command in the intents spool
    the running engine drains — so "the robot gains a new callable tool" is true again,
    without the external attach client ever opening the SDK."""
    import json as _json

    from reachy.behavior.intents import INTENT_NAMESPACE

    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("forge", goal="look at people")]),
        TurnResult(content="", tool_calls=[_call("greet-back")]),
    )
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=2)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line(), _sense_line()],
        engine_factory=_engine_factory(
            turn_fn,
            tmp_path,
            forge_client_factory=_inline_forge_factory(_reply(SKILL_MD, MOVING_EXECUTOR)),
        ),
    )
    assert rc == 0

    spool = tmp_path / "behavior" / INTENT_NAMESPACE / "commands"
    commands = [_json.loads(p.read_text()) for p in sorted(spool.iterdir())]
    assert commands, "the forged skill's ctx.run_behavior never reached the intent spool"
    cmd = commands[-1]
    assert cmd["op"] == "run_behavior"
    assert cmd["name"] == "gaze-hold"
    assert cmd["lifetime"] == {"looping": False, "duration": 2.0}


def test_forge_announces_the_new_skill_into_the_cue_buffer(tmp_path):
    """`feed_forge` is wired: activation announces back as a perception cue, so the
    agent learns on its next snapshot that it gained a skill."""
    buffer = agent_mod._RuntimeCueBuffer()
    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("forge", goal="greet people back")])
    )
    engine = agent_mod._build_default_engine(
        buffer,
        None,
        spool_dir=tmp_path,
        await_timeout=0.0,
        turn_fn=turn_fn,
        forge_client_factory=_inline_forge_factory(_reply(SKILL_MD, SAFE_EXECUTOR)),
    )
    buffer.feed_event({"t": "sense", "ts": 1.0, "doa": 0.0, "speech": True})
    engine.run_turn()
    assert any("learned a new skill: greet-back" in c.text for c in buffer.snapshot())


def test_runtime_cue_buffer_feed_forge_ignores_empty_text():
    buffer = agent_mod._RuntimeCueBuffer()
    buffer.feed_forge("")
    buffer.feed_forge("   ")
    buffer.feed_forge(None)
    assert buffer.snapshot() == []


# --------------------------------------------------------------------------- #
# Fail-closed: unsafe generated code never activates, cognition keeps running  #
# --------------------------------------------------------------------------- #


def test_unsafe_forged_code_is_refused_and_never_becomes_callable(tmp_path):
    """The AST gate refuses `import os` BEFORE the module is imported: nothing lands in
    active/, nothing is registered, and the attach loop keeps thinking."""
    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("forge", goal="run a shell command")]),
        TurnResult(content="", tool_calls=[_call("declare_goal", goal="nod")]),
    )
    captured: list = []
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=2)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line(), _sense_line()],
        engine_factory=_engine_factory(
            turn_fn,
            tmp_path,
            forge_client_factory=_inline_forge_factory(_reply(SKILL_MD, UNSAFE_EXECUTOR)),
            captured=captured,
        ),
    )
    assert rc == 0
    assert not (tmp_path / "forge" / "active" / "greet-back").exists()
    assert "greet-back" not in _tool_names(captured[0])
    # Quarantined, not silently dropped.
    assert (tmp_path / "forge" / "staged" / ".rejected" / "greet-back").exists()
    # Cognition kept running: the second turn still reached the intent tools.
    assert "declare_goal" in turn_fn.advertised[1]


def test_a_broken_forge_stack_disables_only_the_forge_tool(tmp_path, monkeypatch):
    """A forge stack that cannot be composed must not take cognition down with it."""
    monkeypatch.setattr(agent_mod, "_forge_stack_available", lambda: False)
    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("declare_goal", goal="nod")])
    )
    captured: list = []
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line()],
        engine_factory=_engine_factory(turn_fn, tmp_path, captured=captured),
    )
    assert rc == 0
    names = _tool_names(captured[0])
    assert "forge" not in names
    assert "declare_goal" in names, "cognition must keep running without the forge"


def test_a_raising_forge_activation_disables_only_the_forge_tool(tmp_path, monkeypatch):
    """Same guarantee when the forge stack imports but activation itself blows up."""

    def _boom(*_a, **_kw):
        raise RuntimeError("forge subsystem is broken")

    monkeypatch.setattr(agent_mod, "_activate_forge", _boom)
    turn_fn = _scripted_turn_fn(
        TurnResult(content="", tool_calls=[_call("declare_goal", goal="nod")])
    )
    captured: list = []
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line()],
        engine_factory=_engine_factory(turn_fn, tmp_path, captured=captured),
    )
    assert rc == 0
    assert "declare_goal" in _tool_names(captured[0])


# --------------------------------------------------------------------------- #
# Boot reload — a previously forged skill is live before the first turn        #
# --------------------------------------------------------------------------- #


def test_boot_reload_registers_a_preexisting_active_skill_before_the_first_turn(tmp_path):
    """`active/<name>` survives a restart: attach re-registers it at composition, so it
    is advertised on the very FIRST turn."""
    skill_dir = tmp_path / "forge" / "active" / "greet-back"
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(SKILL_MD)
    (skill_dir / "executor.py").write_text(SAFE_EXECUTOR)

    turn_fn = _scripted_turn_fn(TurnResult(content="", tool_calls=[_call("greet-back")]))
    captured: list = []
    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    rc = cmd_agent_attach(
        args,
        lines=[_sense_line()],
        engine_factory=_engine_factory(turn_fn, tmp_path, captured=captured),
    )
    assert rc == 0
    assert "greet-back" in turn_fn.advertised[0], "a boot-reloaded skill must be live at turn 1"
    assert "greeted-ok" in str(captured[0]._registry.dispatch("greet-back", "{}", "x"))


# --------------------------------------------------------------------------- #
# The import boundary (asserted, not assumed)                                 #
# --------------------------------------------------------------------------- #


def _imported_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_the_engine_modules_still_never_import_forge():
    """Re-homing must not have leaked `reachy.forge` into the engine modules: the
    dispatch seam and the register/announce callbacks stay plain INJECTED callables."""
    import reachy.speech.agent_turn as agent_turn_mod
    import reachy.speech.tools as tools_mod

    for module in (tools_mod, agent_turn_mod):
        for name in _imported_names(module):
            assert "reachy.forge" not in name, f"{module.__name__} must not import forge ({name!r})"


def test_agent_module_imports_forge_lazily_never_at_module_scope():
    """agent.py is the composition site, so it MAY import forge — but only inside the
    composition functions, so a missing/broken forge can never break the noun's import."""
    tree = ast.parse(inspect.getsource(agent_mod))
    for node in tree.body:  # module scope only
        if isinstance(node, ast.ImportFrom) and node.module:
            assert "reachy.forge" not in node.module
        elif isinstance(node, ast.Import):
            for alias in node.names:
                assert "reachy.forge" not in alias.name


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
