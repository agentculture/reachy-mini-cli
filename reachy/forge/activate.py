"""Validator-gated AUTO-activation + hot registration of forged skills.

This is the runtime half of the forge that decides *when* a staged skill goes live
(:mod:`reachy.forge.lifecycle` only provides the move + event; :mod:`reachy.forge.client`
only dispatches + stages). The policy here is **validator-gated auto-activation, with no
human gate** (a confirmed product decision): a staged skill activates automatically the
moment it exists — but generated code is imported and registered ONLY after the AST
validator (:mod:`reachy.forge.validator`) has returned ``ok`` for its folder.

The four moving parts
---------------------
* :class:`ForgedSkillContext` — the restricted ``ctx`` a forged ``execute(params, ctx)``
  is handed. It exposes EXACTLY the sanctioned reaction seams the validator allow-lists
  (``speak`` / ``harmonics`` / ``express`` / ``state_get`` / ``state_update``) as thin,
  defensive delegations to the same injected callables the built-in agent tools use.
  Nothing else is reachable — no engine, no buffer, no transport.
* :func:`import_forged_execute` — imports a validated ``executor.py`` via
  ``importlib.util.spec_from_file_location`` and *never* registers it in
  :data:`sys.modules`, so one forged skill's module can never shadow or leak into
  another's (or the app's). Ported from ``reachy_nova.skills._import_forged_execute``.
* :func:`wrap_executor` — wraps the imported ``execute`` in a crash-catcher that returns
  an error *string* tool-result instead of raising, so a buggy forged skill can never
  kill the agent's tool loop.
* :class:`ForgeActivator` — the orchestrator. Its :meth:`~ForgeActivator.publish` is the
  ``PublishFn`` handed to :class:`~reachy.forge.client.ForgeClient`: it emits the
  ``[SENSE stage=forge]`` lifecycle line for every ``forge/*`` event AND, on
  ``forge/staged``, auto-activates. :meth:`~ForgeActivator.activate` moves staged→active,
  re-validates, imports (only after ``ok``), wraps, hot-registers into the LIVE registry
  via an injected ``register`` callback, and announces via an injected callable.
  :meth:`~ForgeActivator.reload_active` re-registers everything under ``active/`` at boot.

Restart-note semantics
----------------------
:class:`~reachy.speech.agent_turn.AgentTurnEngine` reads ``registry.tools()`` **fresh on
every round of every turn** — the tool schema is NOT snapshotted per session. So a tool
hot-registered into the live registry here is callable on the **next turn**; no restart is
needed and no deferred-until-restart line is emitted (contrast nova, whose Nova-Sonic
session pins its ``toolConfiguration`` and therefore needs a restart).

Import boundary
---------------
This module keeps forged concerns decoupled: it imports neither
:mod:`reachy.speech.events` (the announce sink is an injected callable) nor
:mod:`reachy.speech.tools` (the register callback + restricted ``ctx`` seams are injected
at composition). It depends only on its sibling forge modules + :mod:`reachy.senselog`.
"""

from __future__ import annotations

import importlib.util
import logging
import threading
import uuid
from collections.abc import Callable, MutableMapping
from pathlib import Path

from reachy import senselog
from reachy.forge import lifecycle
from reachy.forge.validator import validate as _default_validate

logger = logging.getLogger(__name__)

#: Default wall-clock bound (seconds) a forged ``execute`` gets before
#: :func:`wrap_executor` gives up on it and returns a timeout tool-result. Injectable
#: per-call (``wrap_executor(..., timeout=...)``) — see its docstring.
DEFAULT_EXECUTE_TIMEOUT = 10.0

#: ``register(name, description, parameters, handler)`` — the composition-provided hot
#: registration callback. Composition adapts it to ``ToolRegistry.register`` +
#: ``function_tool`` so this module never imports :mod:`reachy.speech.tools`.
RegisterFn = Callable[[str, str, dict, Callable[[dict], str]], None]
#: ``announce(cue_text)`` — feed one cognition cue. Composition wires it to the shared
#: ``EventBuffer`` (kept a plain callable so this module never imports the event bus).
AnnounceFn = Callable[[str], None]
#: ``validate(skill_dir) -> (ok, reasons)`` — the AST gate seam (default: the in-package
#: validator with its default sanctioned ``ctx`` surface).
ValidatorFn = Callable[[Path], "tuple[bool, list[str]]"]
#: ``import_execute(executor_path, name) -> execute | None`` — the importer seam.
ImporterFn = Callable[[Path, str], "Callable | None"]

#: The JSON-schema ``parameters`` a forged tool advertises. A forged ``execute`` takes a
#: free-form ``params`` dict, so the schema is a permissive object (the model may pass any
#: keys the skill's SKILL.md documents).
DEFAULT_FORGED_PARAMS: dict = {"type": "object", "properties": {}}

#: The frontmatter key ``read_skill_description`` scans a SKILL.md for.
_DESC_PREFIX = "description:"


# ---------------------------------------------------------------------------
# The restricted ctx surface
# ---------------------------------------------------------------------------


class ForgedSkillContext:
    """The restricted ``ctx`` handed to a forged skill's ``execute(params, ctx)``.

    A forged skill is runtime-generated code that only ever passed *static* analysis
    (:mod:`reachy.forge.validator`), never a human review — so it NEVER receives an
    engine, a buffer, a transport, or any other live subsystem. It gets exactly the
    sanctioned reaction primitives the validator allow-lists
    (:data:`~reachy.forge.validator.DEFAULT_ALLOWED_CTX_ATTRS`): ``speak``, ``harmonics``,
    ``express``, ``state_get``, ``state_update`` — and nothing else is reachable.

    Each method is a thin, defensive delegation to the injected seam (the SAME callables
    the built-in agent tools use). When the seam is absent or raises, the method logs a
    warning and returns a bracketed status string instead of raising — a forged
    ``execute`` must never crash just because e.g. the TTS endpoint is down. Ported from
    ``reachy_nova.skills.ForgedSkillContext``, re-surfaced to this project's seams.
    """

    def __init__(
        self,
        *,
        speak: Callable[[str], object] | None = None,
        harmonics: Callable[[str], object] | None = None,
        express: Callable[[str], object] | None = None,
        state: MutableMapping | None = None,
    ) -> None:
        self._speak = speak
        self._harmonics = harmonics
        self._express = express
        self._state: MutableMapping = state if state is not None else {}

    def speak(self, text: str) -> str:
        """Speak *text* in Reachy's spoken (TTS) voice."""
        return self._delegate_voice(self._speak, "speak", text)

    def harmonics(self, text: str) -> str:
        """Render *text* as Reachy's harmonic (melodic) voice."""
        return self._delegate_voice(self._harmonics, "harmonics", text)

    def express(self, emoji: str) -> str:
        """Apply a catalog-emoji body expression."""
        seam = self._express
        if seam is None:
            logger.warning("ForgedSkillContext.express: no express seam available")
            return "[express unavailable]"
        try:
            seam(emoji)
            return f"[expressed {emoji}]"
        except Exception as err:  # noqa: BLE001 - a forged skill must never crash the loop
            logger.warning("ForgedSkillContext.express failed: %s", err)
            return f"[express error: {err}]"

    def state_get(self, key: str):
        """Read one field from the forged-skill scratch state (``None`` if unset)."""
        try:
            return self._state.get(key)
        except Exception as err:  # noqa: BLE001 - defensive
            logger.warning("ForgedSkillContext.state_get failed: %s", err)
            return None

    def state_update(self, **fields) -> str:
        """Write one or more fields onto the forged-skill scratch state."""
        try:
            self._state.update(fields)
            return "[state updated]"
        except Exception as err:  # noqa: BLE001 - defensive
            logger.warning("ForgedSkillContext.state_update failed: %s", err)
            return f"[state_update error: {err}]"

    def _delegate_voice(self, seam, label: str, text: str) -> str:
        if seam is None:
            logger.warning("ForgedSkillContext.%s: no %s seam available", label, label)
            return f"[{label} unavailable]"
        try:
            seam(text)
            return f"[{label}]"
        except Exception as err:  # noqa: BLE001 - a forged skill must never crash the loop
            logger.warning("ForgedSkillContext.%s failed: %s", label, err)
            return f"[{label} error: {err}]"


# ---------------------------------------------------------------------------
# Import + wrap (the ONLY place forged code is loaded — always after validation)
# ---------------------------------------------------------------------------


def import_forged_execute(executor_path: Path, name: str) -> Callable | None:
    """Import a forged ``executor.py`` and return its ``execute(params, ctx)`` function.

    SECURITY CONTRACT: call this ONLY after the validator has returned ``ok=True`` for the
    containing folder. Uses ``importlib.util.spec_from_file_location`` (not a plain
    ``import``) and deliberately does NOT register the module in :data:`sys.modules`, so
    each forged skill stays isolated from the next and from the app.

    Returns ``None`` (never raises for a missing/non-callable ``execute``) so the caller
    treats "no usable ``execute``" like any other discovery failure — skip it, log, move on.
    ``exec_module`` itself may raise; the caller wraps this in try/except.
    """
    module_name = f"_forged_skill_{name.replace('-', '_')}"
    spec = importlib.util.spec_from_file_location(module_name, executor_path)
    if spec is None or spec.loader is None:
        return None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)  # may raise — the caller isolates it
    execute_fn = getattr(module, "execute", None)
    if not callable(execute_fn):
        return None
    return execute_fn


def wrap_executor(
    execute_fn: Callable,
    ctx: object,
    name: str,
    *,
    timeout: float = DEFAULT_EXECUTE_TIMEOUT,
) -> Callable[[dict], str]:
    """Wrap a forged ``execute(params, ctx)`` in a crash-catching, timeout-bounded
    tool handler.

    The returned handler matches :data:`reachy.speech.tools.Handler` (``(arguments) ->
    content str``): it calls ``execute_fn(arguments, ctx)`` and returns its result
    stringified, or — if ``execute`` raises — a bracketed error string. It NEVER raises,
    so a buggy forged skill degrades to an error tool-result instead of killing the loop.

    RELIABILITY CONTRACT: forged code only ever passed *static* analysis
    (:mod:`reachy.forge.validator`), never a human review, and ``time`` is an ALLOWED
    import — so a runaway ``execute`` (``time.sleep(1e9)``, ``while True: pass``) must
    never wedge the caller's cognition turn loop forever. ``execute_fn`` therefore runs on
    a **daemon** worker thread with a bounded ``timeout`` (default
    :data:`DEFAULT_EXECUTE_TIMEOUT`, injectable per-call for tests/tuning): if the thread
    hasn't finished within ``timeout`` seconds, the handler returns immediately with an
    error tool-result and logs loudly (a warning plus ``senselog.drop
    reason=skill-timeout``). The leaked, still-running worker thread is a daemon, so it
    never blocks process shutdown; any late result or exception it eventually produces is
    simply discarded.
    """

    def handler(arguments: dict) -> str:
        outcome: dict = {}

        def _run() -> None:
            try:
                outcome["result"] = execute_fn(arguments or {}, ctx)
            except Exception as err:  # noqa: BLE001 - forged code must never crash the loop
                outcome["error"] = err

        worker = threading.Thread(target=_run, name=f"forge-exec-{name}", daemon=True)
        worker.start()
        worker.join(timeout)

        if worker.is_alive():
            logger.warning("forged skill %r timed out after %ss", name, timeout)
            event_id = uuid.uuid4().hex[:8]
            senselog.drop("forge", name, event_id, "skill-timeout")
            return f"[Skill error: timed out after {timeout:g}s]"

        if "error" in outcome:
            err = outcome["error"]
            logger.warning("forged skill %r raised: %s", name, err)
            return f"[forged skill {name!r} error: {err}]"

        result = outcome.get("result")
        if result is None:
            return f"[forged skill {name!r} ran]"
        return str(result)

    return handler


# ---------------------------------------------------------------------------
# SKILL.md parsing (stdlib scan — no YAML dependency, mirrors whoami)
# ---------------------------------------------------------------------------


def read_skill_description(skill_md_path: Path, name: str) -> str:
    """Best-effort read of the ``description:`` frontmatter field, with a fallback.

    A plain, backtracking-free line scan (rather than an anchored ``\\s*(.+)$``
    regex, which SonarCloud flagged for super-linear worst-case behavior on
    untrusted input — S8786) for the first line starting with ``description:``.
    """
    try:
        text = skill_md_path.read_text()
    except OSError:
        return f"A forged skill: {name}."
    for line in text.splitlines():
        if line.startswith(_DESC_PREFIX):
            desc = line[len(_DESC_PREFIX) :].strip().strip("\"'")
            if desc:
                return desc
            break
    return f"A forged skill: {name}."


# ---------------------------------------------------------------------------
# The activator
# ---------------------------------------------------------------------------


class ForgeActivator:
    """Auto-activates staged forged skills and hot-registers them into the live registry.

    Parameters
    ----------
    register:
        The hot-registration callback ``register(name, description, parameters, handler)``.
        Composition adapts it to ``ToolRegistry.register`` + ``function_tool`` (so this
        module never imports :mod:`reachy.speech.tools`).
    ctx:
        The restricted execution context handed to every forged ``execute`` (typically a
        :class:`ForgedSkillContext`).
    announce:
        Optional ``announce(cue_text)`` — fed ``"learned a new skill: <name>"`` on each
        successful activation. Composition wires it to the shared cognition
        ``EventBuffer``; ``None`` skips the announcement.
    validator / importer:
        Injectable seams (defaults: the in-package AST validator with its default
        sanctioned ``ctx`` surface, and :func:`import_forged_execute`).
    staging_root / active_root:
        The staged/active roots (defaults: the state-dir roots from
        :mod:`reachy.forge.lifecycle`).
    """

    def __init__(
        self,
        *,
        register: RegisterFn,
        ctx: object,
        announce: AnnounceFn | None = None,
        validator: ValidatorFn | None = None,
        importer: ImporterFn | None = None,
        staging_root: Path | None = None,
        active_root: Path | None = None,
    ) -> None:
        self._register = register
        self._ctx = ctx
        self._announce = announce
        self._validator = validator or _default_validate
        self._importer = importer or import_forged_execute
        self._staging_root = staging_root
        self._active_root = active_root

    # -- the PublishFn handed to ForgeClient ---------------------------------

    def publish(self, event_type: str, payload: dict) -> None:
        """The ``PublishFn`` for :class:`~reachy.forge.client.ForgeClient`.

        Emits the ``[SENSE stage=forge]`` lifecycle line for every ``forge/*`` event and,
        on ``forge/staged``, AUTO-activates (validator-gated, no human gate). Never raises
        — a fault in senselog or activation is logged, not propagated onto the forge
        dispatch thread.
        """
        try:
            _senselog_event(event_type, payload)
        except Exception as err:  # noqa: BLE001 - observability must never break dispatch
            logger.warning("forge senselog failed for %s: %s", event_type, err)
        if event_type == lifecycle.EVENT_STAGED:
            name = payload.get("name")
            if name:
                try:
                    self.activate(name)
                except Exception as err:  # noqa: BLE001 - activation must never crash dispatch
                    logger.warning("forge auto-activation failed for %s: %s", name, err)

    # -- activation ----------------------------------------------------------

    def activate(self, name: str, *, publish: lifecycle.PublishFn | None = None) -> bool:
        """Move ``staged/<name>`` → ``active/<name>``, hot-register it, and announce it.

        Returns ``True`` only when the skill moved, re-validated, imported, and registered
        cleanly. A missing staged folder, a failed move, a validation rejection, an import
        failure, or a registration failure each returns ``False`` (logged) — never raises.
        ``publish`` overrides the event sink used for the ``forge/activated`` emission
        (defaults to :meth:`publish`, so senselog fires); tests inject a recorder.
        """
        pub = publish if publish is not None else self.publish
        dst = lifecycle.activate(
            pub, name, staging_root=self._staging_root, active_root=self._active_root
        )
        if dst is None:
            return False
        if not self._register_dir(name, dst):
            return False
        if self._announce is not None:
            try:
                self._announce(f"learned a new skill: {name}")
            except Exception as err:  # noqa: BLE001 - announcing must never break activation
                logger.warning("forge announce failed for %s: %s", name, err)
        return True

    # -- boot reload ---------------------------------------------------------

    def reload_active(self) -> list[str]:
        """Re-register every forged skill under ``active/`` — the boot path.

        Idempotent (re-registration replaces cleanly) and defensive: a folder that is not
        a forged skill (no ``SKILL.md``/``executor.py``, or a hidden dir like
        ``.rejected``) is skipped, and a per-folder failure never stops the rest. Returns
        the names actually re-registered. Mirrors nova's ``discover_runtime``.
        """
        root = (
            self._active_root if self._active_root is not None else lifecycle.default_active_root()
        )
        registered: list[str] = []
        if not root.is_dir():
            return registered
        for skill_dir in sorted(p for p in root.iterdir() if p.is_dir()):
            if skill_dir.name.startswith("."):
                continue  # hidden bookkeeping dir, never a skill
            if (
                not (skill_dir / lifecycle.SKILL_FILENAME).is_file()
                or not (skill_dir / lifecycle.EXECUTOR_FILENAME).is_file()
            ):
                continue  # not a forged-skill folder
            if self._register_dir(skill_dir.name, skill_dir):
                registered.append(skill_dir.name)
        return registered

    # -- the one place import + register happens -----------------------------

    def _register_dir(self, name: str, skill_dir: Path) -> bool:
        """Validate → import (only after ok) → wrap → hot-register. Never raises."""
        try:
            ok, reasons = self._validator(skill_dir)
        except Exception as err:  # noqa: BLE001 - a raising validator fails closed
            logger.warning("forge activate: validator raised for %s: %s", name, err)
            return False
        if not ok:
            logger.warning("forge activate: %s failed validation: %s", name, reasons)
            return False
        # Import happens ONLY after validation returned ok — the security invariant.
        assert ok, "forged code must never be imported before validation passes"

        try:
            execute_fn = self._importer(skill_dir / lifecycle.EXECUTOR_FILENAME, name)
        except Exception as err:  # noqa: BLE001 - a raising import skips just this skill
            logger.warning("forge activate: importing %s failed: %s", name, err)
            return False
        if execute_fn is None:
            logger.warning("forge activate: %s has no usable execute(params, ctx)", name)
            return False

        description = read_skill_description(skill_dir / lifecycle.SKILL_FILENAME, name)
        handler = wrap_executor(execute_fn, self._ctx, name)
        try:
            self._register(name, description, dict(DEFAULT_FORGED_PARAMS), handler)
        except Exception as err:  # noqa: BLE001 - a broken register callback isolates
            logger.warning("forge activate: registering %s failed: %s", name, err)
            return False
        logger.info("forge activate: hot-registered forged skill %r", name)
        return True


# ---------------------------------------------------------------------------
# Module-level helpers
# ---------------------------------------------------------------------------


def _senselog_event(event_type: str, payload: dict) -> None:
    """Emit one ``[SENSE stage=forge]`` line for a ``forge/*`` lifecycle event."""
    transition = event_type.split("/", 1)[-1]  # staged | activated | rejected
    name = payload.get("name") or "<unnamed>"
    event_id = uuid.uuid4().hex[:8]
    if transition == "rejected":
        senselog.drop("forge", name, event_id, payload.get("reason", "rejected"))
    else:
        senselog.stage("forge", name, event_id, transition)


def build_ctx_seams(
    *,
    speak_engine,
    harmonic_engine,
    play,
    express,
) -> ForgedSkillContext:
    """Build a :class:`ForgedSkillContext` over the SAME seams the built-in tools use.

    ``speak_engine`` / ``harmonic_engine`` are :class:`reachy.speech.voice.VoiceEngine`
    objects and ``play`` is the ``play(pcm, *, samplerate=...)`` seam — the exact trio the
    ``speak`` / ``harmonics`` tools synthesize + play through — so a forged skill's
    ``ctx.speak`` / ``ctx.harmonics`` render identically. ``express`` is the
    ``ExpressionProducer.express`` seam (as the ``apply_pose`` tool uses). A fresh scratch
    dict backs ``state_get`` / ``state_update``. Kept here (not in composition) so the
    voice-seam wiring stays with the ctx it feeds, but it takes only plain objects — this
    module still imports neither voice nor motion.
    """

    def _speak(text: str) -> None:
        play(speak_engine.synthesize(text), samplerate=speak_engine.samplerate)

    def _harmonics(text: str) -> None:
        play(harmonic_engine.synthesize(text), samplerate=harmonic_engine.samplerate)

    return ForgedSkillContext(speak=_speak, harmonics=_harmonics, express=express, state={})


__all__ = [
    "ForgedSkillContext",
    "ForgeActivator",
    "import_forged_execute",
    "wrap_executor",
    "read_skill_description",
    "build_ctx_seams",
    "DEFAULT_FORGED_PARAMS",
    "DEFAULT_EXECUTE_TIMEOUT",
    "RegisterFn",
    "AnnounceFn",
    "ValidatorFn",
    "ImporterFn",
]
