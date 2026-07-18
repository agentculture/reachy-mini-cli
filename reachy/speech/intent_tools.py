"""Agent tools for sustained body intents — through the same spool the engine drains.

Four tools — ``run_behavior`` / ``declare_goal`` / ``set_mode`` / ``set_inhibition``
— let the tool-use agent (:mod:`reachy.speech.agent_turn`) drive standing body
intents on the 50 Hz behavior engine (:mod:`reachy.behavior.engine`). Unlike
``apply_pose`` (:mod:`reachy.speech.tools`, a one-shot expression enqueue on the
serial ``MotionQueue``), a ``declare_goal``/``set_inhibition``/``set_mode`` call
PERSISTS: once declared, :class:`reachy.behavior.intents.IntentDriver` keeps it
sustained tick after tick with no further agent call. See
:mod:`reachy.behavior.intents` for the engine-side half and the exact command
payload shapes each tool submits.

Follows :mod:`reachy.speech.tools`'s pattern exactly: one JSON-schema
:class:`~reachy.speech.tools.Tool` per capability (built via
:func:`~reachy.speech.tools.function_tool`), an ``enum``-constrained parameter
that is validated BEFORE anything is submitted (mirroring ``apply_pose``'s
catalog check) so an unknown behavior/mode name is rejected with an error
tool-result naming the valid keys — never silently dropped or forwarded to the
engine to fail asynchronously.

Composed into a SEPARATE module, never into ``tools.py`` itself — a later
wave's agent-client task calls :func:`register_intent_tools` onto the live
:class:`~reachy.speech.tools.ToolRegistry` alongside the built-in tools. This
module imports :mod:`reachy.speech.tools` only for its public ``Tool`` /
``function_tool`` / ``Handler`` / ``ToolRegistry`` shapes (the same public
surface any external caller would use) — it does not reach into ``tools.py``
internals, and ``tools.py`` never imports this module (so the boundary holds in
both directions, exactly like ``say`` not importing ``think``'s LLM pieces).

This module never imports :mod:`reachy.behavior.engine` or
:mod:`reachy.behavior.rule_engine` — only :mod:`reachy.behavior.control` (the
spool primitives) and :mod:`reachy.behavior.library` (to advertise + validate
behavior names) — matching the codebase's "the CLI/tool layer never imports the
engine" boundary already established by ``reachy/cli/_commands/behavior.py``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Iterable, Mapping

from reachy.behavior import control as control_mod
from reachy.behavior import library
from reachy.behavior.intents import (
    DECLARE_GOAL,
    INTENT_NAMESPACE,
    RUN_BEHAVIOR,
    SET_INHIBITION,
    SET_MODE,
)
from reachy.speech.tools import Handler, Tool, ToolRegistry, function_tool

#: Default seconds a tool call waits for the engine-side driver to confirm
#: before degrading to a "submitted, unconfirmed" result (mirrors the CLI's
#: ``--await-timeout`` on ``behavior run``/``behavior stop``).
DEFAULT_AWAIT_TIMEOUT = 1.0


# ---------------------------------------------------------------------------
# Submission helper — shared by all four handlers
# ---------------------------------------------------------------------------


def _submit_and_await(op: str, spool_dir: Path | None, timeout: float, **fields: object) -> dict:
    """Atomically submit an intent command, then wait up to *timeout* for its result.

    Uses the SAME namespaced spool (:mod:`reachy.behavior.control`,
    ``namespace=INTENT_NAMESPACE``) :class:`~reachy.behavior.intents.IntentDriver`
    drains. When no engine process is draining it (or it hasn't gotten to it
    yet), this degrades to a ``{"ok": None, "submitted": ...}`` result rather
    than blocking the turn indefinitely — the command is still on disk, so a
    later-started engine will still apply it.
    """
    cmd_id = control_mod.submit(op, namespace=INTENT_NAMESPACE, root=spool_dir, **fields)
    result = control_mod.await_result(
        cmd_id, namespace=INTENT_NAMESPACE, root=spool_dir, timeout=timeout
    )
    if result is None:
        return {
            "ok": None,
            "submitted": cmd_id,
            "note": "engine did not confirm in time — is the behavior engine running?",
        }
    return result


def _validate_params(raw: object, *, entry) -> dict[str, float]:
    """Validate a JSON-schema ``params`` object against *entry*'s known params.

    Mirrors ``apply_pose``'s pre-flight validation: rejected BEFORE the spool
    write, with the valid keys named in the error, exactly like an unknown
    catalog emoji.
    """
    if raw is None:
        return {}
    if not isinstance(raw, Mapping):
        raise ValueError("'params' must be an object")
    bad = sorted(set(raw) - set(entry.params))
    if bad:
        valid = ", ".join(sorted(entry.params)) or "(none)"
        raise ValueError(f"unknown param(s) {bad} for {entry.name!r}; valid params: {valid}")
    out: dict[str, float] = {}
    for key, value in raw.items():
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"param {key!r} must be a number (got {value!r})")
        out[key] = float(value)
    return out


def _require_known_name(name: object, *, catalog: Mapping, what: str) -> str:
    if not isinstance(name, str) or name not in catalog:
        valid = ", ".join(sorted(catalog))
        raise ValueError(f"unknown {what} {name!r}; valid: {valid}")
    return name


# ---------------------------------------------------------------------------
# Handler factories
# ---------------------------------------------------------------------------


def _make_run_behavior_handler(spool_dir: Path | None, timeout: float, catalog: Mapping) -> Handler:
    def handler(arguments: dict) -> str:
        name = _require_known_name(arguments.get("name"), catalog=catalog, what="behavior")
        entry = catalog[name]
        params = _validate_params(arguments.get("params"), entry=entry)
        duration = arguments.get("duration")
        if duration is not None and (
            isinstance(duration, bool) or not isinstance(duration, (int, float))
        ):
            raise ValueError("'duration' must be a number")
        loop = arguments.get("loop")
        lifetime = {
            "looping": bool(loop) if loop is not None else entry.looping,
            "duration": float(duration) if duration is not None else entry.default_duration,
        }
        result = _submit_and_await(
            RUN_BEHAVIOR, spool_dir, timeout, name=name, params=params, lifetime=lifetime
        )
        return json.dumps(result)

    return handler


def _make_declare_goal_handler(spool_dir: Path | None, timeout: float, catalog: Mapping) -> Handler:
    def handler(arguments: dict) -> str:
        goal = arguments.get("goal")
        if goal is not None:
            goal = _require_known_name(goal, catalog=catalog, what="behavior")
            params = _validate_params(arguments.get("params"), entry=catalog[goal])
        else:
            params = {}
        result = _submit_and_await(DECLARE_GOAL, spool_dir, timeout, goal=goal, params=params)
        return json.dumps(result)

    return handler


def _make_set_mode_handler(
    spool_dir: Path | None, timeout: float, modes: frozenset[str]
) -> Handler:
    def handler(arguments: dict) -> str:
        mode = arguments.get("mode")
        if mode is not None:
            if not isinstance(mode, str) or (modes and mode not in modes):
                valid = ", ".join(sorted(modes)) or "(no modes configured)"
                raise ValueError(f"unknown mode {mode!r}; valid: {valid}")
        result = _submit_and_await(SET_MODE, spool_dir, timeout, mode=mode)
        return json.dumps(result)

    return handler


def _make_set_inhibition_handler(
    spool_dir: Path | None, timeout: float, catalog: Mapping
) -> Handler:
    def handler(arguments: dict) -> str:
        raw = arguments.get("behaviors")
        if not isinstance(raw, list):
            raise ValueError("'behaviors' must be a list of behavior names (empty list clears)")
        bad = sorted({b for b in raw if not isinstance(b, str) or b not in catalog})
        if bad:
            valid = ", ".join(sorted(catalog))
            raise ValueError(f"unknown behavior(s) {bad}; valid: {valid}")
        result = _submit_and_await(SET_INHIBITION, spool_dir, timeout, behaviors=list(raw))
        return json.dumps(result)

    return handler


# ---------------------------------------------------------------------------
# Tool definitions
# ---------------------------------------------------------------------------


def _run_behavior_tool(spool_dir: Path | None, timeout: float, catalog: Mapping) -> Tool:
    keys = sorted(catalog)
    return function_tool(
        name=RUN_BEHAVIOR,
        description=(
            "Run a named body behavior once, for its own lifetime (one-shot for a "
            "duration, or looping until stopped). This is a ONE-TIME admission — "
            "unlike declare_goal, the engine does NOT re-admit it if something "
            "else evicts it. Use declare_goal for a standing intent that should "
            "persist across turns."
        ),
        parameters={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "enum": keys,
                    "description": "The behavior to run. Must be one of 'enum'.",
                },
                "params": {
                    "type": "object",
                    "description": "Numeric parameter overrides for the behavior.",
                },
                "duration": {
                    "type": "number",
                    "description": "Seconds to run; omit for the behavior's own default.",
                },
                "loop": {
                    "type": "boolean",
                    "description": "Loop until stopped instead of running once; "
                    "omit for the behavior's own default.",
                },
            },
            "required": ["name"],
        },
        handler=_make_run_behavior_handler(spool_dir, timeout, catalog),
    )


def _declare_goal_tool(spool_dir: Path | None, timeout: float, catalog: Mapping) -> Tool:
    keys = sorted(catalog)
    return function_tool(
        name=DECLARE_GOAL,
        description=(
            "Declare a STANDING body-behavior goal: the engine keeps it admitted "
            "indefinitely, tick after tick, automatically re-admitting it if "
            "anything evicts it — no further tool call needed to sustain it. "
            "Persists until replaced by a new declare_goal call or cleared. "
            "Omit 'goal' (or pass no arguments) to CLEAR the current standing goal."
        ),
        parameters={
            "type": "object",
            "properties": {
                "goal": {
                    "type": "string",
                    "enum": keys,
                    "description": "The behavior to sustain. Must be one of 'enum'. "
                    "Omit to clear the current standing goal.",
                },
                "params": {
                    "type": "object",
                    "description": "Numeric parameter overrides for the behavior.",
                },
            },
            "required": [],
        },
        handler=_make_declare_goal_handler(spool_dir, timeout, catalog),
    )


def _set_mode_tool(spool_dir: Path | None, timeout: float, modes: Iterable[str]) -> Tool:
    keys = sorted(modes)
    properties: dict = {
        "mode": {
            "type": "string",
            "description": "The rules mode to activate. Omit to clear the override.",
        }
    }
    if keys:
        properties["mode"]["enum"] = keys
    return function_tool(
        name=SET_MODE,
        description=(
            "Swap the active behavior-rules mode. The new mode's parameters "
            "apply to every rule-admitted behavior from this point on. Omit "
            "'mode' to clear the active-mode override."
        ),
        parameters={"type": "object", "properties": properties, "required": []},
        handler=_make_set_mode_handler(spool_dir, timeout, frozenset(keys)),
    )


def _set_inhibition_tool(spool_dir: Path | None, timeout: float, catalog: Mapping) -> Tool:
    keys = sorted(catalog)
    return function_tool(
        name=SET_INHIBITION,
        description=(
            "Block a set of named behaviors from admission until cleared. "
            "REPLACES the current inhibited set (not additive) — pass an empty "
            "list to clear all inhibitions."
        ),
        parameters={
            "type": "object",
            "properties": {
                "behaviors": {
                    "type": "array",
                    "items": {"type": "string", "enum": keys},
                    "description": "Behavior names to inhibit; [] clears all inhibitions.",
                }
            },
            "required": ["behaviors"],
        },
        handler=_make_set_inhibition_handler(spool_dir, timeout, catalog),
    )


# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------


def register_intent_tools(
    registry: ToolRegistry,
    *,
    spool_dir: Path | None = None,
    await_timeout: float = DEFAULT_AWAIT_TIMEOUT,
    catalog: Mapping | None = None,
    modes: Iterable[str] = (),
) -> None:
    """Register the four intent tools onto *registry*.

    ``spool_dir`` overrides the state-dir root the intents spool writes under
    (default: :func:`reachy.daemon.state_dir`'s normal resolution) — mainly for
    test isolation, or a caller that wants an isolated spool, without mutating
    ``REACHY_STATE_DIR``. ``catalog`` overrides the advertised + validated
    behavior-name set (default: the full :data:`reachy.behavior.library.LIBRARY`).
    ``modes`` is the known mode-name set ``set_mode`` advertises/validates
    against (default: none known — the tool still submits; the engine-side
    :class:`~reachy.behavior.intents.IntentDriver` validates against its own
    ``known_modes`` seam if one is wired there).

    Mirrors :class:`~reachy.speech.tools.ToolRegistry`'s own construction
    pattern: each tool is one :func:`~reachy.speech.tools.function_tool` call,
    registered via the registry's public :meth:`~reachy.speech.tools.ToolRegistry.register`
    — this module never reaches into ``ToolRegistry`` internals.
    """
    lib = catalog if catalog is not None else library.LIBRARY
    for tool in (
        _run_behavior_tool(spool_dir, await_timeout, lib),
        _declare_goal_tool(spool_dir, await_timeout, lib),
        _set_mode_tool(spool_dir, await_timeout, modes),
        _set_inhibition_tool(spool_dir, await_timeout, lib),
    ):
        registry.register(tool)
