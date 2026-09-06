"""Sustained agent intents on the engine tick — the standing-goal / inhibition /
mode-override consumer.

Where :mod:`reachy.behavior.rule_engine` compiles a *file* of react/inhibit rules
onto the engine's per-tick seam, this module lets a *live agent* push the same
four kinds of standing decision through the command spool
(:mod:`reachy.behavior.control`) instead of a rules TOML: run a behavior once,
declare a standing goal the engine keeps re-admitting forever, swap the active
rules mode, or inhibit a set of behaviors from admission. All four persist —
declared once, they stay in effect tick after tick with no further agent call —
which is the point: an agent turn is a single tool call, but "keep doing this"
needs something that outlives the turn.

:class:`IntentDriver` is a :class:`~reachy.behavior.engine.TickContext` seam
driver (usable directly as ``engine.run(tick_seam=...)``, or composed alongside
:class:`~reachy.behavior.rule_engine.RuleEngine` via
``compose_rule_seam(config, drivers=(IntentDriver(...),))``). It is a PURE
CONSUMER of the seam — nothing here is imported by :mod:`reachy.behavior.engine`
or :mod:`reachy.behavior.rule_engine` (except one small, additive,
justified-in-review coordination method, :meth:`RuleEngine.set_active_mode`, that
``set_mode`` calls through an injected callback — this module still never
imports ``rule_engine`` itself).

Two spools, one state file
---------------------------
Intent COMMANDS use their OWN namespaced spool
(:func:`reachy.behavior.control.commands_dir` with ``namespace="intents"``, the
default :data:`INTENT_NAMESPACE`) — a *separate* inbox from the base engine's
unnamespaced ``behavior/commands/``, which the engine's own ``_apply_commands``
drain (in :mod:`reachy.behavior.engine`) already fully consumes each tick before
a ``tick_seam`` ever runs. Landing intent commands in that SAME directory would
mean the engine silently rejects them ("unknown op") before this driver ever saw
them — a hard single-reader conflict, not a design choice. So
:func:`reachy.speech.intent_tools.register_intent_tools` submits into the
namespaced spool, and :meth:`IntentDriver.on_tick` drains that SAME namespaced
spool every tick.

The sustained-intent VIEW, though, is written into the SAME ``state.json`` the
base engine writes (:func:`reachy.behavior.control.state_file` with
``namespace=""``) — additively, under a new top-level ``"intents"`` key,
alongside the existing ``active``/``ownership``/``doa`` keys ``behavior status``
already reads. Since :mod:`reachy.behavior.engine` cannot be edited (it owns the
periodic ``control.write_state(engine.state(...))`` heartbeat write and this
module must never touch it), :meth:`IntentDriver.on_tick` re-reads the current
``state.json``, merges the intents view in, and re-writes it EVERY tick — the
merge self-heals within a fraction of a tick on the rare tick where the engine's
own heartbeat write also fires (it always writes the un-augmented base shape,
since it has no idea this module exists); the very next tick this driver's
unconditional write restores the ``"intents"`` key. For a bounded, deterministic
test run this is exact, not probabilistic: the engine's heartbeat only fires on
tick 1 and then every ``compose_hz/2`` ticks after (see
:func:`reachy.behavior.engine._timing`), so any run whose final tick does not
land on one of those ticks ends with this driver's merged content as the last
word.

Standing goal semantics
------------------------
* ``run_behavior`` — a ONE-TIME admission using the given (or the library
  entry's default) lifetime. The driver never re-admits it — it is "sustained
  per its lifetime" only, exactly like a plain ``behavior run``. The RESULTING
  lifetime must be BOUNDED: ``looping=True`` with ``duration=None`` — whether
  from an explicit payload or silently from a looping-default library entry's
  own defaults (``nod``, ``shake``, ``speak``, ``antenna-sway``) — is refused
  with a ``CliError`` naming the entry and the fix (pass an explicit
  ``duration``, or choose a bounded behavior), so a one-time admission can never
  hold its channel(s) forever with no standing-intent record to undo it. The ONE
  carve-out is ``feel-alive``: unbounded means the base layer's UN-STOP verb,
  routed to ``ctx.add_base`` (see :func:`_validated_lifetime` and
  :meth:`IntentDriver._un_stop_base`).
* ``declare_goal`` — a STANDING admission: the driver remembers the (name,
  params) pair and, every tick, re-admits it (``looping=True, duration=None`` —
  indefinite) whenever it is missing from ``ctx.active_names()`` and not
  currently inhibited. It persists until replaced by a new ``declare_goal`` or
  cleared (``goal`` omitted/``None`` clears it). Declaring a goal while its name
  is inhibited is NOT rejected — the intent is recorded but admission is
  withheld until the inhibition clears, at which point the very next tick
  admits it with no further agent call.
* ``set_inhibition`` — REPLACES the full inhibited set (an empty list clears
  it). Every tick, any inhibited behavior found in ``ctx.active_names()`` (from
  any source — a rule, a prior intent, ...) is evicted immediately. And on the
  tick where the BASE LAYER's name leaves that set, the driver calls
  ``ctx.ensure_base()`` exactly once (#183): eviction by inhibition is the one
  removal a clearing edge is expected to undo. See
  :meth:`IntentDriver._check_base_layer_edge`.
* ``set_mode`` — calls an injected ``mode_setter(name)`` callback (typically
  :meth:`reachy.behavior.rule_engine.RuleEngine.set_active_mode`); with no
  callback wired the driver still records the mode for ``state.json`` but has
  nothing live to coordinate with.

Observability: every applied/blocked command emits a ``ctx.emit`` event
(``intent.applied`` / ``intent.blocked``) for the export feed, and a
``[SENSE stage=intent ...]`` line via :mod:`reachy.senselog`, mirroring
:mod:`reachy.behavior.rule_engine`'s own observability contract.

The one import of :mod:`reachy.behavior.engine` is the NAME constant
``BASE_LAYER_NAME`` — the base layer's identity is the engine's, and a second
spelling of ``"feel-alive"`` here would be a second thing to drift. The
dependency runs one way still (the engine imports nothing from this module).

Pure standard library plus in-package imports; nothing here touches a network,
an LLM, or ``reachy_mini``.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Callable, Iterable

from reachy import senselog
from reachy.behavior import control as control_mod
from reachy.behavior import gaze
from reachy.behavior import library as behavior_library
from reachy.behavior.engine import BASE_LAYER_NAME
from reachy.behavior.model import Lifetime
from reachy.cli._errors import EXIT_USER_ERROR, CliError

logger = logging.getLogger(__name__)

STAGE = "intent"

#: The namespaced spool intent commands land in (see the module docstring for
#: why this cannot be the base engine's unnamespaced ``behavior/commands/``).
INTENT_NAMESPACE = "intents"

#: Command kinds (the ``op`` field every submitted intent command carries).
RUN_BEHAVIOR = "run_behavior"
DECLARE_GOAL = "declare_goal"
SET_MODE = "set_mode"
SET_INHIBITION = "set_inhibition"

#: Structured event ``type`` values published through ``ctx.emit``.
EVENT_APPLIED = "intent.applied"
EVENT_BLOCKED = "intent.blocked"

_ID_PREFIX = "intent"


# --------------------------------------------------------------------------- #
# Small validation helpers (shared by the four kind handlers)                 #
# --------------------------------------------------------------------------- #


def _plan_look_at_sound(params: dict, sense) -> dict[str, float] | None:
    """Adapt :func:`reachy.behavior.gaze.plan_look_at_sound` to the
    ``_GAZE_PLANNERS`` shape: baked-param-dict-or-``None``."""
    yaw = gaze.plan_look_at_sound(
        sense,
        max_age_s=params.get("max_age_s", gaze.DEFAULT_MAX_AGE_S),
        max_yaw=params.get("max_yaw", gaze.DEFAULT_MAX_YAW),
    )
    if yaw is None:
        return None
    return {"yaw": yaw}


def _plan_look_at_face(params: dict, sense) -> dict[str, float] | None:
    """Adapt :func:`reachy.behavior.gaze.plan_look_at_face` to the
    ``_GAZE_PLANNERS`` shape: baked-param-dict-or-``None``."""
    target = gaze.plan_look_at_face(
        sense,
        max_age_s=params.get("max_age_s", gaze.DEFAULT_MAX_AGE_S_FACE),
        max_yaw=params.get("max_yaw", gaze.DEFAULT_MAX_YAW_FACE),
        max_pitch=params.get("max_pitch", gaze.DEFAULT_MAX_PITCH_FACE),
    )
    if target is None:
        return None
    yaw, pitch = target
    return {"yaw": yaw, "pitch": pitch}


#: The name-keyed branch :meth:`IntentDriver._apply_run_behavior` consults for
#: every sensor-driven ONE-SHOT gaze behavior (``look-at-sound``,
#: ``look-at-face``): name -> ``(planner, refusal_reason)``, where *planner*
#: takes the tick's validated params and the live
#: :class:`~reachy.behavior.sense.Sense` and returns either a dict of params to
#: bake onto the admitted behavior (its clamped aim), or ``None`` to refuse.
#: Generalised from t1's single ``look-at-sound``-only branch so a second
#: sensor-driven one-shot (t3's ``look-at-face``) shares the same admission
#: path instead of growing a second copy of it.
_GAZE_PLANNERS: dict[str, tuple[Callable[[dict, object], dict | None], str]] = {
    gaze.NAME: (_plan_look_at_sound, gaze.NO_RECENT_SOUND),
    gaze.NAME_FACE: (_plan_look_at_face, gaze.NO_FACE_KNOWN),
}


def _validated_params(entry, overrides: dict | None) -> dict[str, float]:
    """Merge numeric *overrides* onto *entry*'s defaults, rejecting anything bad.

    Mirrors :func:`reachy.behavior.library.resolve_params`, but *overrides* here
    are already-numeric (JSON-decoded), not CLI ``key=value`` strings.
    """
    params = entry.default_params()
    for key, value in (overrides or {}).items():
        if key not in entry.params:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{entry.name}: unknown parameter {key!r}",
                remediation=f"valid params: {', '.join(sorted(entry.params)) or '(none)'}",
            )
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{entry.name}: parameter {key} must be a number (got {value!r})",
            )
        params[key] = behavior_library.validate_param_value(entry, key, float(value))
    return params


def _validated_lifetime(entry, raw: dict | None) -> Lifetime:
    """Build the ``run_behavior`` lifetime, refusing an UNBOUNDED result.

    ``looping=True`` with ``duration=None`` never expires on its own — the same
    shape :meth:`IntentDriver._admit_goal` uses ON PURPOSE for a standing
    ``declare_goal`` (see the module docstring's "Standing goal semantics").
    But a *one-time* ``run_behavior`` admission has no standing-intent record to
    replace/clear it later, so that same shape would hold the entry's channel(s)
    FOREVER with no way for the agent to reason about when it ends — and it can
    arrive from nothing more than a missing lifetime payload on a looping-default
    library entry (``nod``, ``shake``, ``speak``, ``antenna-sway``, ``feel-alive``
    — see :mod:`reachy.behavior.library`). So the unbounded shape is refused
    REGARDLESS of whether it came from an explicit payload (``{"looping": true}``
    with no ``duration``) or silently from the entry's own defaults (no lifetime
    payload at all) — the *resulting* shape is what is checked, not its origin.
    A bounded result — ``looping=False`` with a positive duration, or
    ``looping=True`` WITH a positive duration — is admitted exactly as before.

    **One carve-out: the base layer name** (#183 decision c38). An UNBOUNDED
    ``run_behavior`` of :data:`~reachy.behavior.engine.BASE_LAYER_NAME` is not an
    unbounded admission at all — it is the UN-STOP verb, the spool twin of
    ``behavior run feel-alive`` with no lifetime flag, and the engine routes that
    exact shape to its base re-seed path (:meth:`reachy.behavior.engine.Engine.add`).
    Refusing it would leave a by-name ``stop feel-alive`` with no way back short
    of an engine restart. Every other looping-default entry (``nod`` / ``shake``
    / ``speak`` / ``antenna-sway`` / ``orient-to-sound`` / ``face-lock``) stays
    refused, and ``feel-alive`` WITH a duration is an ordinary bounded run.
    """
    raw = raw or {}
    looping = raw.get("looping", entry.looping)
    duration = raw.get("duration", entry.default_duration)
    lifetime = Lifetime(looping=bool(looping), duration=duration)
    problems = lifetime.errors()
    if problems:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{entry.name}: invalid lifetime: {'; '.join(problems)}",
            remediation="duration must be > 0",
        )
    if lifetime.looping and lifetime.duration is None and entry.name == BASE_LAYER_NAME:
        return lifetime  # the un-stop verb, not an unbounded admission
    if lifetime.looping and lifetime.duration is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=(
                f"{entry.name}: run_behavior refuses an unbounded lifetime "
                "(looping with no duration would hold its channel forever)"
            ),
            remediation=(
                'pass lifetime {"duration": <seconds>} to bound it, ' "or choose a bounded behavior"
            ),
        )
    return lifetime


# --------------------------------------------------------------------------- #
# The intent driver — a per-tick seam driver                                  #
# --------------------------------------------------------------------------- #


def _is_base_un_stop(name: str, lifetime: Lifetime) -> bool:
    """Whether this ``run_behavior`` is the base layer's un-stop verb.

    The same predicate :meth:`reachy.behavior.engine.Engine._is_base_re_seed`
    applies engine-side; stated here too because this module must decide which
    seam to reach BEFORE building a behavior, and the two surfaces
    (``behavior run feel-alive`` and a spool ``run_behavior``) must agree.
    """
    return name == BASE_LAYER_NAME and lifetime.looping and lifetime.duration is None


class IntentDriver:
    """Drain the intents spool and sustain standing goals/inhibitions/mode.

    Usable directly as ``engine.run(tick_seam=...)``, or as one driver in a
    :class:`~reachy.behavior.rule_engine.TickBus` (e.g.
    ``compose_rule_seam(config, drivers=(IntentDriver(),))``).

    Parameters
    ----------
    intent_control:
        The namespaced :class:`~reachy.behavior.control.CommandSpool` this
        driver drains commands from. Defaults to a fresh
        ``CommandSpool(namespace=namespace, root=root)``.
    main_control:
        The UNNAMESPACED :class:`~reachy.behavior.control.CommandSpool` whose
        ``state.json`` this driver additively merges the intents view into —
        the SAME spool the base engine writes to. Defaults to a fresh
        ``CommandSpool(root=root)``.
    namespace / root:
        Only used to build the DEFAULT ``intent_control`` (namespace) and
        default ``intent_control``/``main_control`` (root) when those are not
        given explicitly. ``root`` overrides ``state_dir()`` for both spools —
        mainly for test isolation without mutating ``REACHY_STATE_DIR``.
    lib:
        The behavior library module (default: :mod:`reachy.behavior.library`) —
        injectable for tests, mirroring :class:`~reachy.behavior.rule_engine.RuleEngine`.
    mode_setter:
        ``mode_setter(name: str | None) -> None``, called on a ``set_mode``
        command (typically :meth:`reachy.behavior.rule_engine.RuleEngine.set_active_mode`).
        ``None`` (the default) means no live rules engine is coordinated — the
        mode is still recorded for ``state.json``.
    known_modes:
        ``known_modes() -> Iterable[str]``, an optional validator for
        ``set_mode``'s ``mode`` name (mirrors the library-name validation on the
        other three kinds). ``None`` (the default) skips validation — any mode
        name is accepted (useful when no rules config is wired at all).
    registry:
        An existing :class:`~reachy.behavior.control.KindRegistry` to use
        instead of building a fresh one with the four default handlers
        registered — the extension point a future command kind (e.g. a goto
        lane) uses to add itself without ever editing
        :mod:`reachy.behavior.control` again: build one ``KindRegistry``,
        register every kind's handler into it (this module's ``_register_defaults``
        is the pattern to copy), and hand it to whichever driver(s) share it.
    """

    def __init__(
        self,
        *,
        intent_control: control_mod.CommandSpool | None = None,
        main_control: control_mod.CommandSpool | None = None,
        namespace: str = INTENT_NAMESPACE,
        root: Path | None = None,
        lib=None,
        mode_setter: Callable[[str | None], None] | None = None,
        known_modes: Callable[[], Iterable[str]] | None = None,
        registry: control_mod.KindRegistry | None = None,
        inhibition_observer: Callable[[frozenset[str]], None] | None = None,
    ) -> None:
        self._spool = intent_control or control_mod.CommandSpool(namespace=namespace, root=root)
        self._main = main_control or control_mod.CommandSpool(root=root)
        self._lib = lib if lib is not None else behavior_library
        self._mode_setter = mode_setter
        self._known_modes = known_modes
        #: Fired with the NEW set whenever ``set_inhibition`` replaces the whole
        #: inhibited set. A plain public attribute so composition can wire it
        #: after both parties exist (see
        #: :meth:`reachy.behavior.face_lock.FaceLockDriver.notice_inhibition_replaced`,
        #: the later-wins seam whose whole job is to hear this).
        self.inhibition_observer = inhibition_observer
        self._goal: dict | None = None  # {"name": str, "params": dict}
        self._inhibitions: frozenset[str] = frozenset()
        #: The inhibited set as of the END of the previous tick — the only state
        #: the base-layer EDGE needs. Comparing sets per tick (rather than
        #: hooking one command handler) means EVERY path that drops the base
        #: name counts: a spool ``set_inhibition`` with an empty list, one with a
        #: list that simply omits it, and the in-process
        #: :meth:`set_inhibitions` a lock's release calls.
        self._previous_inhibitions: frozenset[str] = frozenset()
        self._mode: str | None = None
        self._seq = 0
        self.registry = registry
        if self.registry is None:
            self.registry = control_mod.KindRegistry()
            self._register_defaults()
        # Deliberately NO reset()-on-construct here (unlike engine.run's own
        # control.reset() on the MAIN spool): a command can legitimately land
        # in the intents spool in the narrow window between an engine process
        # starting and this driver being constructed (or between two engine
        # restarts), and silently discarding it would be a surprising, hard to
        # debug race. A truly stale command from a long-dead prior process is
        # just drained and applied on the first tick like any other — harmless,
        # since every kind handler is idempotent-safe (re-admitting an already
        # -active behavior, re-declaring the same goal, ... all no-op cleanly).

    def _register_defaults(self) -> None:
        self.registry.register(RUN_BEHAVIOR, self._apply_run_behavior)
        self.registry.register(DECLARE_GOAL, self._apply_declare_goal)
        self.registry.register(SET_MODE, self._apply_set_mode)
        self.registry.register(SET_INHIBITION, self._apply_set_inhibition)

    # -- read-only introspection (tests + state view) ----------------------- #

    @property
    def goal(self) -> dict | None:
        """The current standing goal ``{"name": ..., "params": ...}``, or ``None``."""
        return dict(self._goal) if self._goal is not None else None

    @property
    def inhibitions(self) -> frozenset[str]:
        """The current inhibited behavior names."""
        return self._inhibitions

    def set_inhibitions(self, names: Iterable[str]) -> frozenset[str]:
        """Replace the inhibited set IN PROCESS, without a spool command.

        The same primitive ``set_inhibition`` applies (a full replacement),
        exposed for an in-process consumer that owns part of the set for the
        duration of some state — today only
        :class:`reachy.behavior.face_lock.FaceLockDriver`, which adds
        ``feel-alive`` / ``orient-to-sound`` while a face lock is held and
        removes exactly those again on release.

        Deliberately does NOT fire :attr:`inhibition_observer`: that seam means
        "a caller replaced the whole set", and an in-process owner adjusting its
        own contribution is not that.
        """
        self._inhibitions = frozenset(names)
        return self._inhibitions

    @property
    def mode(self) -> str | None:
        """The last mode ``set_mode`` recorded (whether or not a setter was wired)."""
        return self._mode

    # -- seam entry point ---------------------------------------------------- #

    def __call__(self, ctx) -> None:
        """Usable directly as ``engine.run(tick_seam=intent_driver)``."""
        self.on_tick(ctx)

    def on_tick(self, ctx) -> None:
        """Drain intent commands, enforce inhibitions, sustain the goal, publish state."""
        self._drain(ctx)
        self._enforce_inhibitions(ctx)
        self._sustain_goal(ctx)
        self._publish_state(ctx)

    # -- drain + dispatch ----------------------------------------------------- #

    def _drain(self, ctx) -> None:
        for cmd in self._spool.drain():
            result = self.registry.dispatch(cmd, ctx)
            self._spool.write_result(cmd.get("cmd_id"), result)
            self._observe(ctx, cmd, result)

    def _observe(self, ctx, cmd: dict, result: dict) -> None:
        kind = cmd.get("op")
        cmd_id = cmd.get("cmd_id")
        ok = bool(result.get("ok"))
        if ok:
            senselog.stage(STAGE, str(kind), str(cmd_id), f"applied result={result}")
            self._emit(ctx, EVENT_APPLIED, kind=kind, cmd_id=cmd_id, result=result)
        else:
            reason = str(result.get("error", "rejected"))
            senselog.drop(STAGE, str(kind), str(cmd_id), reason)
            self._emit(ctx, EVENT_BLOCKED, kind=kind, cmd_id=cmd_id, reason=reason)

    def _emit(self, ctx, event_type: str, **fields: object) -> None:
        try:
            ctx.emit({"type": event_type, "ts": ctx.now, "tick": ctx.tick, **fields})
        except Exception:  # a raising consumer must never break a tick
            logger.exception("intent emit failed")

    # -- kind handlers (registered into self.registry) ------------------------ #

    def _apply_run_behavior(self, payload: dict, ctx) -> dict:
        name = payload.get("name")
        entry = self._lib.get(name)  # raises CliError naming the problem for an unknown name
        behavior_library.refuse_if_lifecycle_owned(name, surface=RUN_BEHAVIOR)
        if name in self._inhibitions:
            return {
                "ok": False,
                "op": RUN_BEHAVIOR,
                "name": name,
                "error": f"{name!r} is inhibited",
            }
        params = _validated_params(entry, payload.get("params"))
        planner_entry = _GAZE_PLANNERS.get(name)
        if planner_entry is not None:
            planner, refusal_reason = planner_entry
            baked = planner(params, ctx.sense)
            if baked is None:
                senselog.drop(STAGE, RUN_BEHAVIOR, name, refusal_reason)
                return {
                    "ok": False,
                    "op": RUN_BEHAVIOR,
                    "name": name,
                    "error": refusal_reason,
                }
            params.update(baked)
        lifetime = _validated_lifetime(entry, payload.get("lifetime"))
        if _is_base_un_stop(name, lifetime):
            return self._un_stop_base(ctx)
        self._seq += 1
        behavior_id = f"{_ID_PREFIX}:run:{name}:{self._seq}"
        beh = self._lib.build(name, params, entry.default_class, lifetime, behavior_id)
        result = ctx.admit(beh)
        result["op"] = RUN_BEHAVIOR
        return result

    def _apply_declare_goal(self, payload: dict, ctx) -> dict:
        name = payload.get("goal")
        if name is None:
            previous = self._goal
            self._goal = None
            if previous is not None:
                ctx.evict(previous["name"])
            return {"ok": True, "op": DECLARE_GOAL, "goal": None, "cleared": previous}
        entry = self._lib.get(name)  # raises CliError naming the problem for an unknown name
        behavior_library.refuse_if_lifecycle_owned(name, surface=DECLARE_GOAL)
        params = _validated_params(entry, payload.get("params"))
        previous = self._goal
        self._goal = {"name": name, "params": params}
        if previous is not None and (previous["name"] != name or previous["params"] != params):
            # A different goal, or the same one re-declared with new params: evict
            # whatever is currently running so `_sustain_goal` re-admits it fresh
            # (below, same tick) with the new params rather than leaving the old
            # instance's params in place.
            ctx.evict(previous["name"])
        return {"ok": True, "op": DECLARE_GOAL, "goal": name, "params": params}

    def _apply_set_mode(self, payload: dict, ctx) -> dict:  # ctx unused
        mode = payload.get("mode")
        if mode is not None and self._known_modes is not None:
            valid = set(self._known_modes())
            if mode not in valid:
                raise CliError(
                    code=EXIT_USER_ERROR,
                    message=f"unknown mode {mode!r}",
                    remediation=f"use one of: {', '.join(sorted(valid)) or '(none configured)'}",
                )
        if self._mode_setter is not None:
            self._mode_setter(mode)
        self._mode = mode
        return {"ok": True, "op": SET_MODE, "mode": mode}

    def _apply_set_inhibition(self, payload: dict, ctx) -> dict:  # ctx unused
        raw = payload.get("behaviors")
        if not isinstance(raw, list):
            raise CliError(
                code=EXIT_USER_ERROR,
                message="set_inhibition requires a 'behaviors' list (empty to clear)",
            )
        names: set[str] = set()
        for item in raw:
            self._lib.get(item)  # raises CliError naming the problem for an unknown name
            names.add(item)
        self._inhibitions = frozenset(names)
        if self.inhibition_observer is not None:
            # LATER-WINS: an in-process owner of part of the set (the face lock)
            # hears that a caller has just replaced the whole thing, and adopts
            # it as its new snapshot rather than restoring a stale one on
            # release. It keeps only the names IT added (stripping them from
            # that snapshot and re-asserting them), because a caller echoing
            # back the set it just read cannot tell those apart from its own.
            self.inhibition_observer(self._inhibitions)
        return {"ok": True, "op": SET_INHIBITION, "behaviors": sorted(self._inhibitions)}

    # -- continuous enforcement (every tick, no spool command needed) -------- #

    def _enforce_inhibitions(self, ctx) -> None:
        self._check_base_layer_edge(ctx)
        if not self._inhibitions:
            return
        active = ctx.active_names()
        for name in sorted(self._inhibitions):
            if name in active:
                ctx.evict(name)
                senselog.drop(STAGE, "set_inhibition", name, "inhibited")
                self._emit(
                    ctx, EVENT_BLOCKED, kind=SET_INHIBITION, behavior=name, reason="inhibited"
                )

    def _check_base_layer_edge(self, ctx) -> None:
        """Re-seed the base layer on the tick an inhibition naming it CLEARS.

        The #183 bug is eviction BY INHIBITION: ``_enforce_inhibitions`` evicts
        ``feel-alive`` for as long as it is named, and before this nothing put it
        back — the robot stayed still for the life of the process. So the edge,
        and ONLY the edge, re-seeds: while the name is still in the set the
        eviction below is doing its job, and a by-name ``behavior stop
        feel-alive`` is intentional stillness the engine's own
        :meth:`reachy.behavior.engine.Engine.ensure_base` refuses to undo (this
        driver deliberately does not second-guess that refusal — the engine owns
        the removal cause).
        """
        previous, self._previous_inhibitions = self._previous_inhibitions, self._inhibitions
        if BASE_LAYER_NAME not in previous or BASE_LAYER_NAME in self._inhibitions:
            return
        ensure = getattr(ctx, "ensure_base", None)
        if ensure is None:
            return
        base_id = ensure()
        if base_id is None:
            return  # already active, or held still by a by-name stop
        senselog.stage(STAGE, SET_INHIBITION, BASE_LAYER_NAME, "re-seeded (inhibition cleared)")
        self._emit(
            ctx,
            EVENT_APPLIED,
            kind=SET_INHIBITION,
            behavior=BASE_LAYER_NAME,
            reason="re-seed",
            id=base_id,
        )

    def _un_stop_base(self, ctx) -> dict:
        """Route an unbounded ``run_behavior feel-alive`` to the engine's base add.

        NOT ``ctx.admit``: that would put an ordinary ``feel-alive`` copy BESIDE
        the base layer (``is_base=False``, absent from the engine's ``_base_ids``,
        so ``stop all`` would sweep it away) — a robot that looks alive until the
        next stop-all and a base layer still reported stopped. ``ctx.add_base``
        is the engine's own un-stop verb, the same path
        ``behavior run feel-alive`` takes. A context predating that seam refuses
        fail-closed rather than falling back to a plain admission.
        """
        add_base = getattr(ctx, "add_base", None)
        if add_base is None:
            return {
                "ok": False,
                "op": RUN_BEHAVIOR,
                "name": BASE_LAYER_NAME,
                "error": (
                    f"{BASE_LAYER_NAME}: this engine exposes no base-layer seam "
                    "(no ctx.add_base), so the un-stop cannot be applied"
                ),
            }
        result = dict(add_base())
        result["op"] = RUN_BEHAVIOR
        result.setdefault("name", BASE_LAYER_NAME)
        return result

    def _sustain_goal(self, ctx) -> None:
        if self._goal is None:
            return
        name = self._goal["name"]
        if name in self._inhibitions:
            return  # recorded, but withheld until the inhibition clears
        if name in ctx.active_names():
            return  # already sustained
        result = self._admit_goal(ctx)
        if result is not None:
            senselog.stage(STAGE, DECLARE_GOAL, name, "re-admitted (sustained goal)")
            self._emit(ctx, EVENT_APPLIED, kind=DECLARE_GOAL, goal=name, reason="re-admit")

    def _admit_goal(self, ctx) -> dict | None:
        if self._goal is None:
            return None
        name = self._goal["name"]
        entry = self._lib.get(name)
        lifetime = Lifetime(looping=True, duration=None)  # indefinite: "until replaced/cleared"
        self._seq += 1
        behavior_id = f"{_ID_PREFIX}:goal:{name}:{self._seq}"
        beh = self._lib.build(
            name, dict(self._goal["params"]), entry.default_class, lifetime, behavior_id
        )
        return ctx.admit(beh)

    # -- state.json (additive merge into the SAME file the engine writes) ---- #

    def _intents_view(self) -> dict:
        return {
            "goal": self.goal,
            "inhibitions": sorted(self._inhibitions),
            "mode": self._mode,
        }

    def _publish_state(self, ctx) -> None:  # ctx unused (kept for symmetry)
        current = self._main.read_state() or {}
        merged = dict(current)
        merged["intents"] = self._intents_view()
        self._main.write_state(merged)
