"""Tests for validator-gated auto-activation + hot registration (:mod:`reachy.forge.activate`).

Covers task t13:

* a restricted ``ForgedSkillContext`` exposing EXACTLY the five sanctioned seams
  (``speak`` / ``harmonics`` / ``express`` / ``state_get`` / ``state_update``) as thin
  defensive delegations — nothing else reachable;
* :func:`import_forged_execute` importing a validated ``executor.py`` via
  ``spec_from_file_location`` WITHOUT registering it in ``sys.modules``;
* the crash-catching handler wrapper;
* :class:`ForgeActivator` — auto-activation on a ``forge/staged`` event (no human gate),
  moving staged→active, importing ONLY after validation ok, hot-registering into a live
  registry via an injected register callback, announcing via an injected callable, and
  the ``[SENSE stage=forge]`` lifecycle lines; and
* on-startup ``reload_active`` re-registration.
"""

from __future__ import annotations

import logging
import sys
import textwrap
import threading
import time

import pytest

from reachy.forge import activate as act
from reachy.forge import lifecycle

VALID_EXECUTOR = textwrap.dedent("""
    def execute(params, ctx):
        ctx.speak("hello from the forge")
        return "greeted"
    """).strip()

VALID_SKILL_MD = textwrap.dedent("""
    ---
    name: wave-hello
    description: Wave hello to a nearby person.
    ---
    Use when someone new appears and a greeting fits.
    """).strip()


# ---------------------------------------------------------------------------
# Helpers / fakes
# ---------------------------------------------------------------------------


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, event_type, payload):
        self.events.append((event_type, payload))


class _Register:
    """A fake registry callback: records (name, description, parameters, handler)."""

    def __init__(self):
        self.calls = []

    def __call__(self, name, description, parameters, handler):
        self.calls.append((name, description, parameters, handler))

    @property
    def names(self):
        return [c[0] for c in self.calls]

    def handler_for(self, name):
        for n, _d, _p, h in self.calls:
            if n == name:
                return h
        raise KeyError(name)


def _write_skill(root, name, *, executor=VALID_EXECUTOR, skill_md=VALID_SKILL_MD):
    skill_dir = root / name
    skill_dir.mkdir(parents=True, exist_ok=True)
    (skill_dir / "SKILL.md").write_text(skill_md)
    (skill_dir / "executor.py").write_text(executor)
    return skill_dir


@pytest.fixture
def roots(tmp_path):
    return tmp_path / "staged", tmp_path / "active"


# ---------------------------------------------------------------------------
# ForgedSkillContext — the restricted ctx surface
# ---------------------------------------------------------------------------


def test_ctx_public_surface_is_exactly_the_five_sanctioned_seams():
    ctx = act.ForgedSkillContext()
    public = {a for a in dir(ctx) if not a.startswith("_")}
    assert public == {"speak", "harmonics", "express", "state_get", "state_update"}
    # the validator's default allow-list and the ctx surface must agree
    from reachy.forge.validator import DEFAULT_ALLOWED_CTX_ATTRS

    assert public == set(DEFAULT_ALLOWED_CTX_ATTRS)


def test_ctx_speak_and_harmonics_delegate_to_injected_seams():
    spoken, sung = [], []
    ctx = act.ForgedSkillContext(speak=spoken.append, harmonics=sung.append)
    ctx.speak("hi")
    ctx.harmonics("la la")
    assert spoken == ["hi"]
    assert sung == ["la la"]


def test_ctx_express_delegates_to_injected_seam():
    posed = []
    ctx = act.ForgedSkillContext(express=posed.append)
    ctx.express("🙂")
    assert posed == ["🙂"]


def test_ctx_state_get_update_use_the_injected_state_mapping():
    state = {}
    ctx = act.ForgedSkillContext(state=state)
    assert ctx.state_get("mood") is None
    ctx.state_update(mood="curious", energy=3)
    assert state == {"mood": "curious", "energy": 3}
    assert ctx.state_get("mood") == "curious"


def test_ctx_methods_are_defensive_when_a_seam_is_absent_or_raises():
    # Absent seams: a status string, never a raise.
    ctx = act.ForgedSkillContext()
    assert isinstance(ctx.speak("x"), str)
    assert isinstance(ctx.harmonics("x"), str)
    assert isinstance(ctx.express("🙂"), str)
    assert ctx.state_get("nope") is None
    assert isinstance(ctx.state_update(a=1), str)

    # A raising seam is caught, not propagated.
    def _boom(_):
        raise RuntimeError("device down")

    ctx2 = act.ForgedSkillContext(speak=_boom, express=_boom)
    assert isinstance(ctx2.speak("x"), str)  # no raise
    assert isinstance(ctx2.express("🙂"), str)


# ---------------------------------------------------------------------------
# import_forged_execute — spec_from_file_location, NOT sys.modules
# ---------------------------------------------------------------------------


def test_import_forged_execute_returns_execute_without_touching_sys_modules(roots):
    staging, _active = roots
    skill_dir = _write_skill(staging, "wave-hello")
    before = set(sys.modules)

    fn = act.import_forged_execute(skill_dir / "executor.py", "wave-hello")
    assert callable(fn)

    # The forged module name is NEVER registered in sys.modules.
    assert not (
        set(sys.modules) - before
    ), "importing a forged executor must not leak into sys.modules"
    assert "_forged_skill_wave_hello" not in sys.modules


def test_import_forged_execute_none_when_no_execute(roots):
    staging, _active = roots
    skill_dir = _write_skill(staging, "no-exec", executor="X = 1\n")
    assert act.import_forged_execute(skill_dir / "executor.py", "no-exec") is None


# ---------------------------------------------------------------------------
# wrap_executor — crash-catching handler
# ---------------------------------------------------------------------------


def test_wrap_executor_calls_execute_with_params_and_ctx():
    seen = {}

    def execute(params, ctx):
        seen["params"] = params
        seen["ctx"] = ctx
        return "ran"

    ctx = object()
    handler = act.wrap_executor(execute, ctx, "demo")
    out = handler({"speed": 2})
    assert seen["params"] == {"speed": 2}
    assert seen["ctx"] is ctx
    assert "ran" in out


def test_wrap_executor_catches_a_raising_execute_and_returns_error_string():
    def execute(params, ctx):
        raise ValueError("kaboom")

    handler = act.wrap_executor(execute, object(), "demo")
    out = handler({})  # must NOT raise
    assert isinstance(out, str)
    assert "demo" in out and "kaboom" in out


# ---------------------------------------------------------------------------
# wrap_executor — bounded timeout (Qodo finding: a forged execute() runs
# synchronously with no timeout; ``time`` is an ALLOWED import, so
# ``time.sleep(1e9)`` or ``while True`` wedges the caller's turn loop forever).
# ---------------------------------------------------------------------------


def test_wrap_executor_returns_within_bound_while_executor_is_still_blocked():
    started = threading.Event()
    blocker = threading.Event()

    def execute(params, ctx):
        started.set()
        blocker.wait()  # never set by the test — simulates a runaway forged skill
        return "should never surface"

    handler = act.wrap_executor(execute, object(), "wedged", timeout=0.2)

    t0 = time.monotonic()
    out = handler({})
    elapsed = time.monotonic() - t0

    assert started.wait(2.0), "the executor never even started"
    assert elapsed < 1.5, f"handler blocked for {elapsed}s — timeout was not enforced"
    assert isinstance(out, str)
    assert "timed out" in out
    assert "0.2" in out

    blocker.set()  # release the leaked daemon thread so it can exit cleanly


def test_wrap_executor_timeout_logs_a_senselog_drop(caplog):
    blocker = threading.Event()

    def execute(params, ctx):
        blocker.wait()

    handler = act.wrap_executor(execute, object(), "wedged", timeout=0.1)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        handler({})

    assert "dropped reason=skill-timeout" in caplog.text
    blocker.set()


def test_wrap_executor_timeout_is_injectable_and_default_is_generous():
    """A fast executor completes normally under the (generous) default timeout."""

    def execute(params, ctx):
        return "fast"

    handler = act.wrap_executor(execute, object(), "quick")
    out = handler({})
    assert "fast" in out


# ---------------------------------------------------------------------------
# ForgeActivator.activate — validator-gated AUTO-activation + hot registration
# ---------------------------------------------------------------------------


def test_activate_moves_staged_to_active_registers_and_announces(roots):
    staging, active = roots
    _write_skill(staging, "wave-hello")
    register = _Register()
    announced = []
    publish = _Recorder()

    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        announce=announced.append,
        staging_root=staging,
        active_root=active,
    )
    ok = activator.activate("wave-hello", publish=publish)

    assert ok is True
    # moved staged -> active
    assert not (staging / "wave-hello").exists()
    assert (active / "wave-hello" / "executor.py").exists()
    # hot-registered exactly one tool named for the skill, with a real description
    assert register.names == ["wave-hello"]
    _n, description, params, _h = register.calls[0]
    assert "Wave hello" in description
    assert params["type"] == "object"
    # announced "learned a new skill: <name>"
    assert any("wave-hello" in a and "learned" in a for a in announced)
    # forge/activated emitted through publish
    assert (
        "forge/activated",
        {"name": "wave-hello", "path": str(active / "wave-hello")},
    ) in publish.events


def test_activate_registered_handler_invokes_the_forged_execute_with_ctx(roots):
    staging, active = roots
    executor = textwrap.dedent("""
        def execute(params, ctx):
            ctx.speak(params["word"])
            return "spoke"
        """).strip()
    _write_skill(staging, "echo", executor=executor)
    register = _Register()
    spoken = []
    ctx = act.ForgedSkillContext(speak=spoken.append)

    activator = act.ForgeActivator(
        register=register, ctx=ctx, staging_root=staging, active_root=active
    )
    assert activator.activate("echo") is True

    handler = register.handler_for("echo")
    out = handler({"word": "hi there"})
    assert spoken == ["hi there"]  # the forged skill ran against the injected ctx
    assert isinstance(out, str) and "spoke" in out


def test_activate_is_fail_closed_import_only_after_validation_ok(roots):
    """A rejecting validator means the importer is NEVER called and nothing registers."""
    staging, active = roots
    _write_skill(staging, "sketchy")
    register = _Register()
    imported = []

    def _spy_importer(path, name):
        imported.append((path, name))
        return lambda params, ctx: "should-never-run"

    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        validator=lambda _dir: (False, ["nope, forbidden construct"]),
        importer=_spy_importer,
        staging_root=staging,
        active_root=active,
    )
    ok = activator.activate("sketchy")

    assert ok is False
    assert imported == [], "the importer must never run when validation fails"
    assert register.calls == [], "nothing may be registered when validation fails"


def test_activate_missing_staged_dir_is_a_clean_false(roots):
    staging, active = roots
    register = _Register()
    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )
    assert activator.activate("ghost") is False
    assert register.calls == []


# ---------------------------------------------------------------------------
# ForgeActivator.publish — the PublishFn: senselog + auto-activate on staged
# ---------------------------------------------------------------------------


def test_publish_on_staged_auto_activates_without_a_human_gate(roots):
    staging, active = roots
    _write_skill(staging, "wave-hello")
    register = _Register()
    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )

    # A forge/staged event alone triggers activation — the validator is the only gate.
    activator.publish(
        lifecycle.EVENT_STAGED, {"name": "wave-hello", "path": str(staging / "wave-hello")}
    )

    assert register.names == ["wave-hello"]
    assert (active / "wave-hello").exists()


def test_publish_emits_senselog_lines_for_each_lifecycle_transition(roots, caplog):
    staging, active = roots
    _write_skill(staging, "wave-hello")
    activator = act.ForgeActivator(
        register=_Register(),
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        activator.publish(
            lifecycle.EVENT_STAGED, {"name": "wave-hello", "path": str(staging / "wave-hello")}
        )
        activator.publish(lifecycle.EVENT_REJECTED, {"name": "bad", "reason": "forbidden import"})

    text = caplog.text
    assert "stage=forge" in text
    assert "staged" in text  # the staged transition line
    assert "activated" in text  # activation fires its own line through publish
    assert "dropped reason=forbidden import" in text  # rejected → a drop line


# ---------------------------------------------------------------------------
# reload_active — on-startup idempotent re-registration
# ---------------------------------------------------------------------------


def test_reload_active_reregisters_every_active_skill(roots):
    staging, active = roots
    _write_skill(active, "wave-hello")
    _write_skill(active, "spin-around")
    register = _Register()

    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )
    registered = activator.reload_active()

    assert sorted(registered) == ["spin-around", "wave-hello"]
    assert sorted(register.names) == ["spin-around", "wave-hello"]


def test_reload_active_is_idempotent_and_skips_non_skill_dirs(roots):
    staging, active = roots
    _write_skill(active, "wave-hello")
    (active / ".rejected").mkdir(parents=True, exist_ok=True)  # not a skill folder
    (active / "empty").mkdir(parents=True, exist_ok=True)  # no SKILL.md/executor.py
    register = _Register()
    activator = act.ForgeActivator(
        register=register,
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )

    first = activator.reload_active()
    second = activator.reload_active()
    assert first == ["wave-hello"]
    assert second == ["wave-hello"]  # idempotent — re-registration re-runs cleanly


def test_reload_active_missing_root_is_empty(roots):
    staging, active = roots  # active never created
    activator = act.ForgeActivator(
        register=_Register(),
        ctx=act.ForgedSkillContext(),
        staging_root=staging,
        active_root=active,
    )
    assert activator.reload_active() == []


# ---------------------------------------------------------------------------
# Import boundary — activate must not import the event bus (announce is a callable)
# ---------------------------------------------------------------------------


def test_activate_module_does_not_import_events():
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(act))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    for name in names:
        assert (
            "speech.events" not in name
        ), f"forge.activate must not import the event bus ({name!r})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
