"""Rule evaluation on the engine tick — the react / inhibit / mode consumer.

This module compiles a validated :class:`reachy.behavior.rules.RulesConfig` onto
the engine's per-tick seam (:class:`reachy.behavior.engine.TickContext`, handed
to ``engine.run(tick_seam=...)``). It is a PURE CONSUMER of that seam: nothing
here is imported by :mod:`reachy.behavior.engine` — the engine only ever calls
the opaque ``tick_seam`` callable it was handed — so the rules layer, the goto
lane (a later task), and the export feed (a later task) all ride the ONE seam
without ever editing ``engine.py`` again.

Seam contract (so a goto / export rider can ride from this docstring alone)
---------------------------------------------------------------------------
``engine.run(..., tick_seam=seam)`` invokes ``seam(ctx)`` exactly once per tick,
after that tick's pose has streamed, with a fresh
:class:`~reachy.behavior.engine.TickContext` exposing:

* ``ctx.now`` / ``ctx.tick`` — the engine's injected monotonic clock reading and
  the 1-based tick counter (deterministic under the engine's ``now`` /
  ``max_ticks`` seams).
* ``ctx.sense`` — this tick's :class:`~reachy.behavior.sense.Sense` snapshot,
  read UNGATED while a ``tick_seam`` is installed (so a rule sees perception
  every tick, not only while a ``wants_sense`` behavior is active).
* ``ctx.ownership`` — ``{channel: owner_id | None}`` resolved this tick.
* ``ctx.emit(event: dict)`` — publish a structured event; it fans out to whatever
  event consumers the ``tick_seam`` registered (a no-op when the seam exposes no
  ``.emit``). The shapes this module emits are :data:`EVENT_FIRE` /
  :data:`EVENT_SUPPRESS` (see :meth:`RuleEngine._emit_fire` /
  ``_emit_suppress`` for the exact keys).
* ``ctx.admit(behavior) -> dict`` — put a :func:`reachy.behavior.library.build`
  result onto the engine's active set (the REACT action).
* ``ctx.evict(name) -> dict`` — stop every active behavior with that library name
  (the INHIBIT action).
* ``ctx.active_names() -> set[str]`` — the library names currently active.

:class:`TickBus` is the reusable, fault-isolating composition of the seam: a list
of per-tick DRIVERS (this :class:`RuleEngine` is one; a goto lane is another) plus
a list of event CONSUMERS (an export sink is one). Compose one with
:func:`compose_rule_seam` and pass it as ``engine.run(tick_seam=...)``.

Timing semantics (asserted exactly by the tests)
------------------------------------------------
* ``cooldown_s`` — the minimum seconds between two fires of the same rule, on the
  injected ``ctx.now`` clock.
* ``hysteresis`` — a re-arm guard on top of cooldown: after a rule fires, its
  predicate must evaluate ``False`` *continuously* for at least ``hysteresis``
  seconds before it may fire again. ``hysteresis == 0`` means cooldown alone
  governs.
* ``absent_for`` — the predicate is true once the named field has been
  absent/``None`` continuously for at least ``value`` seconds, tracked against
  ``ctx.now`` via a per-field last-seen clock.

Observability
-------------
Every decision that fires, inhibits, or suppresses a rule emits one
``[SENSE stage=rule source=<field> event=<rule id>] ...`` line via
:mod:`reachy.senselog` (``stage`` for a fire naming the reason, ``drop`` for a
suppression naming the reason). A tick where nothing matches logs nothing — no
per-tick no-match noise — so a log capture reconstructs every non-trivial
decision and a silent side effect is impossible.

Pure standard library (``logging`` only) plus in-package imports; nothing here
touches a network, an LLM, or ``reachy_mini``.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, replace

from reachy import senselog
from reachy.behavior import library as behavior_library
from reachy.behavior.model import Lifetime
from reachy.behavior.rules import SENSE_FIELDS, Predicate, Rule, RulesConfig
from reachy.behavior.sense import Sense
from reachy.cli._errors import EXIT_USER_ERROR, CliError

logger = logging.getLogger(__name__)

STAGE = "rule"

#: Structured event ``type`` values published through ``ctx.emit`` so a downstream
#: consumer (e.g. an export feed) can subscribe to the full decision stream.
EVENT_FIRE = "rule.fire"
EVENT_SUPPRESS = "rule.suppress"

#: Suppression / outcome reasons — the ``reason=`` token in every ``[SENSE
#: stage=rule ...]`` line and the ``reason`` key on every emitted event.
REASON_FIRED = "fired"
REASON_COOLDOWN = "cooldown"
REASON_REARMING = "rearming"
REASON_INHIBITED = "inhibited"
REASON_ALREADY_ACTIVE = "already-active"


# --------------------------------------------------------------------------- #
# Predicate evaluation over a Sense snapshot                                  #
# --------------------------------------------------------------------------- #


def _field_present(sense: Sense, field: str) -> bool:
    """Whether the cue *field* has a reading this tick (the ``is_true`` sense).

    Presence is "the cue fired": ``speech`` and ``frame_available`` use their
    boolean flags directly (they are CONDITIONS — always-populated booleans, never
    ``None``); every other field is present when its snapshot attribute is not
    ``None``.
    """
    if field == "speech":
        return bool(sense.speech_detected)
    if field == "frame_available":
        return bool(sense.frame_available)
    if field == "doa":
        return sense.doa_angle is not None
    if field == "rms":
        return sense.rms is not None
    if field == "pat":
        return sense.pat_event is not None
    if field == "face":
        return sense.face is not None
    if field == "transcript":
        return sense.transcript is not None
    return False


def _field_value(sense: Sense, field: str):
    """The comparable value of *field* for the ordered / equality comparators."""
    return {
        "doa": sense.doa_angle,
        "speech": sense.speech_detected,
        "rms": sense.rms,
        "pat": sense.pat_event,
        "face": sense.face,
        "frame_available": sense.frame_available,
        "transcript": sense.transcript,
    }.get(field)


def _compare(op: str, left, right) -> bool:
    """A total, never-raising comparison — a type mismatch is simply ``False``."""
    try:
        if op == "gt":
            return left > right
        if op == "lt":
            return left < right
        if op == "ge":
            return left >= right
        if op == "le":
            return left <= right
        if op == "eq":
            return left == right
        if op == "ne":
            return left != right
    except TypeError:
        return False
    return False


# --------------------------------------------------------------------------- #
# Per-rule timing state                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _RuleState:
    """The cooldown + hysteresis bookkeeping for one rule."""

    last_fire_t: float | None = None
    false_since: float | None = None
    armed: bool = True


# --------------------------------------------------------------------------- #
# The rules evaluator — a per-tick seam driver                                #
# --------------------------------------------------------------------------- #


class RuleEngine:
    """Evaluate a :class:`RulesConfig` against each tick's perception.

    Instantiated once with a validated config, then passed as (or composed into)
    the engine's ``tick_seam``. On every tick it:

    1. updates per-field last-seen clocks (for ``absent_for``);
    2. fires the inhibit rules that match (evicting their named behaviors),
       cooldown / hysteresis gated;
    3. fires the react rules that match (admitting their library behavior),
       suppressing any whose behavior a currently-matching inhibit rule blocks,
       or that is cooling down, re-arming, or already active.

    Every fire and every suppression is logged and emitted; a no-match tick is
    silent. It is defensive: a per-rule failure is isolated and logged, never
    propagated into the engine loop.
    """

    def __init__(self, config: RulesConfig, *, id_prefix: str = "rule", lib=None) -> None:
        self._config = config
        self._prefix = id_prefix
        self._lib = lib if lib is not None else behavior_library
        self._state: dict[str, _RuleState] = {
            rule.id: _RuleState() for rule in (*config.react, *config.inhibit)
        }
        self._last_present: dict[str, float] = {}
        self._started = False
        self._seq = 0

    # -- seam entry points ------------------------------------------------- #

    def __call__(self, ctx) -> None:
        """Usable directly as ``engine.run(tick_seam=rule_engine)``."""
        self.on_tick(ctx)

    def on_tick(self, ctx) -> None:
        """Evaluate every rule against ``ctx.sense`` for the tick at ``ctx.now``."""
        sense = ctx.sense
        now = ctx.now
        self._track_presence(sense, now)
        inhibited = self._current_inhibited(sense, now)
        self._run_inhibit(ctx, sense, now)
        self._run_react(ctx, sense, now, inhibited)

    # -- runtime mode coordination (additive, t7) --------------------------- #

    def set_active_mode(self, name: str | None) -> None:
        """Swap the live config's active mode at runtime.

        Purely additive: no other method's behavior changes, and every existing
        read of ``self._config`` (:meth:`_active_mode_params`, :meth:`_build`)
        keeps working unmodified since it only ever reads attributes off
        whatever :class:`RulesConfig` is currently assigned. This is the
        coordination point the ``set_mode`` intent
        (:mod:`reachy.behavior.intents`) calls through an injected callback —
        this module has no knowledge of intents, spools, or agent tools.

        ``name`` must name an existing mode in the current config, or be
        ``None`` to clear any active mode. Raises :class:`CliError` (never a
        bare ``KeyError``) for an unknown name, mirroring every other
        validation in this codebase.
        """
        if name is not None and name not in self._config.modes:
            valid = ", ".join(sorted(self._config.modes)) or "(none configured)"
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown mode {name!r}",
                remediation=f"use one of: {valid}",
            )
        self._config = replace(self._config, active_mode=name)

    # -- presence tracking (absent_for) ------------------------------------ #

    def _track_presence(self, sense: Sense, now: float) -> None:
        if not self._started:
            # Seed every field's last-seen to the first tick so ``absent_for``
            # measures elapsed absence from engine start, not from -inf.
            for field in SENSE_FIELDS:
                self._last_present[field] = now
            self._started = True
        for field in SENSE_FIELDS:
            if _field_present(sense, field):
                self._last_present[field] = now

    def _absent_duration(self, field: str, now: float) -> float:
        return now - self._last_present.get(field, now)

    # -- predicate evaluation --------------------------------------------- #

    def _eval(self, pred: Predicate, sense: Sense, now: float) -> bool:
        op = pred.op
        field = pred.field
        if op == "absent_for":
            return self._absent_duration(field, now) >= pred.value
        if op == "is_true":
            return _field_present(sense, field)
        if op == "is_false":
            return not _field_present(sense, field)
        value = _field_value(sense, field)
        if value is None:
            return False  # no reading -> an ordered/equality predicate cannot hold
        return _compare(op, value, pred.value)

    # -- cooldown / hysteresis arming ------------------------------------- #

    def _step_arming(self, rule: Rule, matched: bool, now: float) -> None:
        """Advance the re-arm state given this tick's predicate result."""
        state = self._state[rule.id]
        if matched:
            state.false_since = None  # continuity of False is broken
            return
        if state.false_since is None:
            state.false_since = now
        if not state.armed and (now - state.false_since) >= rule.hysteresis:
            state.armed = True

    def _fire_reason(self, rule: Rule, now: float) -> str:
        """Whether *rule* may fire now, or why it is suppressed."""
        state = self._state[rule.id]
        if not state.armed:
            return REASON_REARMING
        if state.last_fire_t is not None and (now - state.last_fire_t) < rule.cooldown_s:
            return REASON_COOLDOWN
        return REASON_FIRED

    def _mark_fired(self, rule: Rule, now: float) -> None:
        state = self._state[rule.id]
        state.last_fire_t = now
        if rule.hysteresis > 0:
            # Must now see hysteresis seconds of continuous False before re-firing.
            state.armed = False
            state.false_since = None

    # -- inhibit rules ----------------------------------------------------- #

    def _current_inhibited(self, sense: Sense, now: float) -> set[str]:
        """The behavior names blocked by any inhibit rule matching THIS tick.

        Independent of cooldown: while an inhibit predicate holds, its behaviors
        are blocked from admission (a continuous effect), even between the
        cooldown-gated eviction fires.
        """
        blocked: set[str] = set()
        for rule in self._config.inhibit:
            try:
                if self._eval(rule.when, sense, now):
                    blocked |= set(rule.disable)
            except Exception:  # noqa: BLE001 - a bad rule never breaks the tick
                logger.exception("inhibit rule %r failed to evaluate", rule.id)
        return blocked

    def _run_inhibit(self, ctx, sense: Sense, now: float) -> None:
        for rule in self._config.inhibit:
            try:
                self._fire_inhibit_rule(ctx, rule, sense, now)
            except Exception:  # noqa: BLE001 - isolate a single bad rule
                logger.exception("inhibit rule %r failed", rule.id)

    def _fire_inhibit_rule(self, ctx, rule: Rule, sense: Sense, now: float) -> None:
        matched = self._eval(rule.when, sense, now)
        self._step_arming(rule, matched, now)
        if not matched:
            return
        reason = self._fire_reason(rule, now)
        if reason != REASON_FIRED:
            self._emit_suppress(ctx, rule, reason)
            return
        for name in sorted(rule.disable):
            ctx.evict(name)
        self._mark_fired(rule, now)
        self._emit_fire(ctx, rule, disabled=sorted(rule.disable))

    # -- react rules ------------------------------------------------------- #

    def _run_react(self, ctx, sense: Sense, now: float, inhibited: set[str]) -> None:
        active = set(ctx.active_names())
        for rule in self._config.react:
            try:
                if self._fire_react_rule(ctx, rule, sense, now, inhibited, active):
                    active.add(rule.behavior)
            except Exception:  # noqa: BLE001 - isolate a single bad rule
                logger.exception("react rule %r failed", rule.id)

    def _fire_react_rule(self, ctx, rule, sense, now, inhibited, active) -> bool:
        """Evaluate one react rule; return True iff it admitted a behavior."""
        matched = self._eval(rule.when, sense, now)
        self._step_arming(rule, matched, now)
        if not matched:
            return False
        if rule.behavior in inhibited:
            self._emit_suppress(ctx, rule, REASON_INHIBITED)
            return False
        reason = self._fire_reason(rule, now)
        if reason != REASON_FIRED:
            self._emit_suppress(ctx, rule, reason)
            return False
        if rule.behavior in active:
            self._emit_suppress(ctx, rule, REASON_ALREADY_ACTIVE)
            return False
        ctx.admit(self._build(rule))
        self._mark_fired(rule, now)
        self._emit_fire(ctx, rule, run=rule.behavior)
        return True

    def _build(self, rule: Rule):
        """Realise a react rule via the vetted library.build path (mode-swapped).

        The admitted :class:`~reachy.behavior.model.Lifetime` uses
        ``rule.duration_s`` as its ``duration`` when the rule carries one,
        overriding the target entry's own default; otherwise it falls back to
        the library defaults — which :meth:`reachy.behavior.rules.RulesConfig.from_dict`
        now guarantees are bounded for any react rule's target (a looping entry
        with no ``default_duration`` cannot pass validation without a
        ``duration_s`` of its own), so this is never an unbounded admission.
        """
        entry = self._lib.LIBRARY[rule.behavior]
        params = entry.default_params()
        for key, value in self._active_mode_params().items():
            if key in entry.params:  # a mode param only swaps a real behavior knob
                params[key] = value
        params.update(rule.params)  # rule overrides win over the active mode
        self._seq += 1
        behavior_id = f"{self._prefix}:{rule.id}:{self._seq}"
        if rule.duration_s is not None:
            lifetime = Lifetime(looping=entry.looping, duration=rule.duration_s)
        else:
            lifetime = Lifetime(looping=entry.looping, duration=entry.default_duration)
        return self._lib.build(rule.behavior, params, entry.default_class, lifetime, behavior_id)

    def _active_mode_params(self) -> dict[str, float]:
        name = self._config.active_mode
        if name is None:
            return {}
        mode = self._config.modes.get(name)
        return dict(mode.params) if mode is not None else {}

    # -- observability ----------------------------------------------------- #

    def _emit_fire(self, ctx, rule: Rule, *, run: str | None = None, disabled=None) -> None:
        detail = f"fired kind={rule.kind}"
        if run is not None:
            detail += f" run={run}"
        if disabled is not None:
            detail += f" disable={disabled}"
        senselog.stage(STAGE, rule.when.field, rule.id, detail)
        ctx.emit(
            {
                "type": EVENT_FIRE,
                "rule": rule.id,
                "kind": rule.kind,
                "field": rule.when.field,
                "op": rule.when.op,
                "behavior": run,
                "disable": disabled or [],
                "reason": REASON_FIRED,
                "ts": ctx.now,
                "tick": ctx.tick,
            }
        )

    def _emit_suppress(self, ctx, rule: Rule, reason: str) -> None:
        senselog.drop(STAGE, rule.when.field, rule.id, reason)
        ctx.emit(
            {
                "type": EVENT_SUPPRESS,
                "rule": rule.id,
                "kind": rule.kind,
                "field": rule.when.field,
                "op": rule.when.op,
                "reason": reason,
                "ts": ctx.now,
                "tick": ctx.tick,
            }
        )


# --------------------------------------------------------------------------- #
# TickBus — the reusable seam composition                                     #
# --------------------------------------------------------------------------- #


class TickBus:
    """Fan the engine's ONE ``tick_seam`` out to many riders, fault-isolated.

    Holds two lists:

    * ``drivers`` — per-tick callables invoked as ``driver(ctx)`` each tick (the
      :class:`RuleEngine` is one; a goto lane is another);
    * ``consumers`` — event callables invoked as ``consumer(event)`` whenever any
      driver calls ``ctx.emit`` (an export sink is one).

    The engine wires ``ctx.emit`` to :meth:`emit`, so a driver publishes through
    the same ``ctx`` it was handed and every consumer receives it. Each driver and
    each consumer is wrapped so one raising rider never breaks another or the
    engine loop.
    """

    def __init__(self, drivers=None, consumers=None) -> None:
        self._drivers = list(drivers or [])
        self._consumers = list(consumers or [])

    def add_driver(self, driver):
        """Register a per-tick driver; returns it for chaining."""
        self._drivers.append(driver)
        return driver

    def add_consumer(self, consumer):
        """Register an event consumer; returns it for chaining."""
        self._consumers.append(consumer)
        return consumer

    def __call__(self, ctx) -> None:
        for driver in self._drivers:
            try:
                driver(ctx)
            except Exception:  # noqa: BLE001 - one rider never breaks the loop
                logger.exception("tick driver %r raised", driver)

    def emit(self, event: dict) -> None:
        for consumer in self._consumers:
            try:
                consumer(event)
            except Exception:  # noqa: BLE001 - one consumer never breaks the fan-out
                logger.exception("event consumer %r raised", consumer)


def compose_rule_seam(config: RulesConfig, *, drivers=(), consumers=(), id_prefix="rule", lib=None):
    """Build a :class:`TickBus` seam that drives *config*'s rules plus any riders.

    The returned bus has a :class:`RuleEngine` driver first, then any extra
    *drivers* (e.g. a goto lane), and fans emitted events out to *consumers*
    (e.g. an export sink). Pass it as ``engine.run(tick_seam=...)``.
    """
    bus = TickBus(consumers=consumers)
    bus.add_driver(RuleEngine(config, id_prefix=id_prefix, lib=lib))
    for driver in drivers:
        bus.add_driver(driver)
    return bus
