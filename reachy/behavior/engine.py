"""The 50 Hz behavior engine — hold active behaviors, arbitrate, compose, stream.

The engine keeps a set of :class:`~reachy.behavior.model.Behavior` objects (in
admission order) and, every tick:

1. drops any that have expired;
2. :func:`~reachy.behavior.arbitration.arbitrate`-s a single owner per channel;
3. asks each owner for its contribution *once* and composes a **complete** pose
   (unclaimed channels fall to neutral, so the immediate target is never partial);
4. streams that pose to the robot via a :class:`~reachy.robot.transport.TargetSink`
   held open for the whole loop.

Between ticks it drains the command spool, so behaviors can be added and stopped
while it runs. ``feel-alive`` is seeded as a passive base layer (unless disabled),
so an idle robot keeps breathing and any channel no behavior claims stays alive.

The loop mirrors :func:`reachy.alive.run_loop`: injectable ``sleep`` / ``now`` /
``max_ticks`` for deterministic tests, SIGTERM/SIGINT graceful stop via
:mod:`reachy.looputil`, transient-error tolerance, and a settle-to-neutral on exit.
"""

from __future__ import annotations

import dataclasses
import time
from dataclasses import dataclass, field
from typing import Callable

from reachy import senselog
from reachy.behavior import control as control_mod
from reachy.behavior import library
from reachy.behavior.arbitration import admit, arbitrate
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head
from reachy.behavior.sense import EMPTY_SENSE, Sense
from reachy.cli._errors import CliError
from reachy.looputil import (
    DEFAULT_SLEEP_SLICE,
    install_stop_handlers,
    interruptible_sleep,
    restore_stop_handlers,
)
from reachy.robot.transport import TargetSink

# Base-layer behavior name + the param the CLI/config exposes (its liveliness).
BASE_LAYER_NAME = "feel-alive"

# The senselog coordinates of a base-layer re-seed (#183): one line per real
# re-seed, so an operator can tell "the base layer came back" from "the base
# layer never left" in the journal alone.
_BASE_STAGE = "engine"
_BASE_SOURCE = "base-layer"

#: The causes :meth:`Engine.state`'s ``base_layer.stopped_by`` reports.
STOPPED_BY_STOP = "stop"
STOPPED_BY_INHIBITION = "inhibition"


@dataclass
class EngineConfig:
    """Tunables for an engine run (connection flavor lives with the transport)."""

    compose_hz: float = 50.0
    base_layer: bool = True
    energy: float = 1.0
    max_errors: int = 5
    settle: bool = True


@dataclass
class ActiveBehavior:
    behavior: Behavior
    start_t: float
    is_base: bool = False


def _noop_emit(_event: dict) -> None:
    """Default ``TickContext.emit`` when the seam registers no event consumers."""


def _noop_ensure_base() -> str | None:
    """Default ``TickContext.ensure_base`` for a context built without an engine."""
    return None


@dataclass
class TickContext:
    """The per-tick seam contract handed to ``engine.run(tick_seam=...)``.

    The engine builds one fresh ``TickContext`` each tick — *after* that tick's
    pose has streamed — and invokes ``tick_seam(ctx)`` exactly once with it. It
    is the single, generous integration seam every per-tick rider shares (the
    rules evaluator, the goto lane, the export feed); a rider reads perception,
    admits/evicts behaviors, and publishes events through it without the engine
    importing the rider. Fields:

    * ``now`` / ``tick`` — the engine's injected monotonic clock reading for this
      tick and the 1-based tick counter (both deterministic under the engine's
      ``now`` / ``max_ticks`` seams).
    * ``sense`` — this tick's :class:`~reachy.behavior.sense.Sense` snapshot,
      read UNGATED whenever a ``tick_seam`` is installed (so a rider sees
      perception every tick, not only while a ``wants_sense`` behavior is
      active). :data:`~reachy.behavior.sense.EMPTY_SENSE` when no ``sense``
      source was supplied.
    * ``pose`` — the complete pose THIS tick streamed to the robot, populated
      AFTER streaming: ``{"head": {...6 axes...}, "antennas": (right, left),
      "body_yaw": float}`` — the exact dict :meth:`Engine.compose_tick` composed
      and :func:`_send_target` already sent to the transport this tick (same
      object, not a copy). A rider that needs "where is the robot right now"
      (e.g. a goto's start-pose continuity, see
      :mod:`reachy.behavior.pose_feed`) reads it here instead of re-deriving
      ownership/contributions itself.
    * ``ownership`` — the ``{channel: owner_id | None}`` resolved this tick.
    * ``emit`` — ``emit(event: dict) -> None``: publish a structured event; it
      fans out to whatever event consumers the ``tick_seam`` registered (a
      no-op, :func:`_noop_emit`, when the seam exposes no ``.emit``).
    * ``admit`` — ``admit(behavior) -> dict``: put a
      :func:`reachy.behavior.library.build` result onto the engine's active set
      (see :meth:`Engine.admit_behavior`).
    * ``evict`` — ``evict(name_or_id) -> dict``: stop every active behavior
      matching that library name or id (see :meth:`Engine.stop`).
    * ``active_names`` — ``active_names() -> set[str]``: the library names of the
      behaviors currently active.
    * ``ensure_base`` — ``ensure_base() -> str | None``: re-seed the passive
      ``feel-alive`` base layer if no base behavior is active, returning the new
      id (``None`` when one already is — the call is idempotent). The seam a
      rider uses on the edge where an inhibition naming the base layer CLEARS
      (see :meth:`Engine.ensure_base`); the engine itself never calls it.

    The ``tick_seam`` is invoked directly and is responsible for its own error
    isolation (the engine adds none), so one rider raising never silently eats
    another; :class:`reachy.behavior.rule_engine.TickBus` is the reusable
    fault-isolating composition of drivers + event consumers.
    """

    now: float
    tick: int
    sense: Sense
    pose: dict
    ownership: dict
    emit: Callable[[dict], None]
    admit: Callable[[Behavior], dict]
    evict: Callable[[str], dict]
    active_names: Callable[[], set[str]]
    ensure_base: Callable[[], str | None] = _noop_ensure_base


@dataclass
class Engine:
    """The active-behavior set and the per-tick composition."""

    active: list[ActiveBehavior] = field(default_factory=list)
    _seq: int = 0
    _base_ids: set[str] = field(default_factory=set)
    # The most recent tick's resolved (abstention-aware) ownership and sense
    # snapshot, surfaced by ``state()`` so ``behavior status`` reports who *is*
    # driving each channel (not just who nominally claims it) and the last DoA.
    _last_ownership: dict | None = None
    _last_sense: Sense = EMPTY_SENSE
    # Base-layer lifecycle bookkeeping (#183). ``_base_seeded`` latches on the
    # first seed (so "never seeded" and "seeded then stopped" are different
    # states); ``_base_energy`` is the energy the FIRST seed used, so every
    # re-seed restores the liveliness the run was configured with rather than
    # the library default; ``_base_stopped_by`` records the CAUSE of the last
    # removal — an operator (or the peer harness) must be able to tell
    # intentional stillness from an inhibition-driven eviction.
    _base_seeded: bool = False
    _base_energy: float = 1.0
    _base_stopped_by: str | None = None

    # --- mutation --------------------------------------------------------
    def _next_id(self, name: str) -> str:
        self._seq += 1
        return f"{name}-{self._seq}"

    def behaviors(self) -> list[Behavior]:
        """The live behaviors, oldest-first (admission order)."""
        return [ab.behavior for ab in self.active]

    def seed_base_layer(self, now: float, energy: float) -> str:
        """Add the passive ``feel-alive`` base layer; record it so 'stop all' keeps it."""
        entry = library.get(BASE_LAYER_NAME)
        params = entry.default_params()
        params["energy"] = energy
        return self._seed_base(now, params)

    def _seed_base(self, now: float, params: dict[str, float]) -> str:
        """Build + append an ``is_base`` ``feel-alive``; the ONE path that makes a base id.

        Records the id in ``_base_ids`` (so ``stop all`` keeps it), latches
        ``_base_seeded``, remembers the seeding energy for later re-seeds, and
        clears the removal cause — a base layer that is back was not stopped.
        """
        beh = library.build(
            BASE_LAYER_NAME,
            dict(params),
            StopClass.PASSIVE,
            Lifetime(looping=True, duration=None),
            self._next_id(BASE_LAYER_NAME),
        )
        self.active.append(ActiveBehavior(beh, now, is_base=True))
        self._base_ids.add(beh.id)
        if not self._base_seeded:
            self._base_energy = float(beh.params.get("energy", self._base_energy))
        self._base_seeded = True
        self._base_stopped_by = None
        return beh.id

    def base_active(self) -> bool:
        """Whether an ``is_base`` behavior is on the active set right now."""
        return any(ab.is_base for ab in self.active)

    def ensure_base(self, now: float) -> str | None:
        """Re-seed the base layer unless one is already active. Returns the new id or ``None``.

        The engine side of #183: ``seed_base_layer`` runs once at start, so a
        behavior evicted by an inhibition naming ``feel-alive`` used to be gone
        for the life of the process. A rider calls this (via
        ``TickContext.ensure_base``) on the edge where such an inhibition
        clears. Idempotent by construction — while a base id is active this is a
        silent no-op, so it can never put a SECOND ``feel-alive`` on the active
        set — and every real re-seed emits exactly one senselog line, since a
        base layer that comes back without a trace is indistinguishable from one
        that never left.
        """
        if self.base_active():
            return None
        entry = library.get(BASE_LAYER_NAME)
        params = entry.default_params()
        params["energy"] = self._base_energy
        was = self._base_stopped_by
        base_id = self._seed_base(now, params)
        senselog.stage(
            _BASE_STAGE,
            _BASE_SOURCE,
            "re-seed",
            f"re-seeded {BASE_LAYER_NAME} id={base_id} energy={self._base_energy} "
            f"after={was or 'never-active'}",
        )
        return base_id

    def add(
        self,
        name: str,
        params: dict[str, float],
        stop_class: StopClass,
        lifetime: Lifetime,
        now: float,
        channels: list[str] | None = None,
    ) -> dict:
        """Admit a new behavior, evicting what a ``stopping`` add stops. Returns the outcome.

        ``channels`` overrides which channels the behavior claims (e.g. an
        ``antenna-sway`` set to also seize ``body_yaw``); ``None`` keeps the
        library entry's channels.

        **The un-stop carve-out (#183).** An UNBOUNDED add of
        :data:`BASE_LAYER_NAME` (``looping=True`` with no duration — what
        ``behavior run feel-alive`` with no lifetime flag, and the equivalent
        spool ``run_behavior``, produce) re-seeds the base layer PROPER rather
        than admitting a plain copy beside it: ``is_base=True``, the id recorded
        in ``_base_ids`` so ``stop all`` keeps it, and the removal cause
        cleared. It is the only verb that can undo a by-name ``stop
        feel-alive``. An add of the same name WITH a duration (or with
        ``looping=False``) is an ordinary bounded behavior, unchanged.
        """
        if self._is_base_re_seed(name, lifetime):
            return self._add_base(params, now, channels)
        beh = library.build(name, params, stop_class, lifetime, self._next_id(name))
        if channels:
            beh = dataclasses.replace(beh, channels=frozenset(channels))
        return self.admit_behavior(beh, now)

    @staticmethod
    def _is_base_re_seed(name: str, lifetime: Lifetime) -> bool:
        """Whether this add is the un-stop verb (an unbounded add of the base name)."""
        return name == BASE_LAYER_NAME and lifetime.looping and lifetime.duration is None

    def _add_base(self, params: dict[str, float], now: float, channels: list[str] | None) -> dict:
        """Re-seed the base layer from an unbounded ``add``; a no-op while one is active.

        ``channels`` AND ``params`` are deliberately ignored: the base layer's
        claim and tuning are the engine's own (the library entry's defaults at
        the energy the run was configured with), and a re-seed that claimed or
        tuned something else would not be the base layer any more. This matters
        because the CLI's ``behavior run`` fills EVERY library default into the
        payload, so honouring ``params`` would reset a runtime started with
        ``--energy 0.4`` back to ``energy=1.0`` on every un-stop.
        """
        for active in self.active:
            if active.is_base:
                outcome = self._outcome(active.behavior, [], {})
                outcome["note"] = f"{BASE_LAYER_NAME} base layer already active"
                return outcome
        entry = library.get(BASE_LAYER_NAME)
        merged = entry.default_params()
        merged["energy"] = self._base_energy
        base_id = self._seed_base(now, merged)
        beh = self.active[-1].behavior
        senselog.stage(
            _BASE_STAGE,
            _BASE_SOURCE,
            "re-seed",
            f"re-seeded {BASE_LAYER_NAME} id={base_id} via add",
        )
        return self._outcome(beh, [], {})

    @staticmethod
    def _outcome(beh: Behavior, evicted: list, blocked: dict) -> dict:
        """The shared ``add``/``admit_behavior`` outcome shape."""
        return {
            "ok": True,
            "op": "add",
            "id": beh.id,
            "name": beh.name,
            "class": beh.stop_class.value,
            "channels": sorted(beh.channels),
            "evicted": [b.id for b in evicted],
            "blocked": blocked,
        }

    def admit_behavior(self, beh: Behavior, now: float) -> dict:
        """Admit an already-built :class:`Behavior` onto the active set.

        The shared admit -> evict -> append path behind :meth:`add`, and also the
        per-tick seam's react entry point (``TickContext.admit``): a rules/goto
        consumer hands a :func:`reachy.behavior.library.build` result straight
        onto the active set without re-deriving eviction. Returns the same
        outcome dict as :meth:`add`.
        """
        result = admit(beh, self.behaviors())
        evicted_ids = {b.id for b in result.evicted}
        if evicted_ids:
            self.active = [ab for ab in self.active if ab.behavior.id not in evicted_ids]
        self.active.append(ActiveBehavior(beh, now))
        return self._outcome(beh, result.evicted, result.blocked)

    def stop(self, target: str) -> dict:
        """Stop a behavior by id or name, or ``all`` (keeps the passive base layer).

        A stop that removes the base layer records ``"stop"`` as its cause —
        intentional stillness, held until an unbounded ``add`` of
        :data:`BASE_LAYER_NAME` or a restart. :meth:`evict` is the same removal
        attributed to an inhibition instead.
        """
        return self._remove(target, STOPPED_BY_STOP)

    def evict(self, target: str) -> dict:
        """``stop`` attributed to an inhibition — the call ``TickContext.evict`` binds to.

        Identical removal semantics and identical outcome dict; the only
        difference is the cause recorded when the removed behavior was the base
        layer, so ``state()`` can tell an inhibition-driven eviction (which a
        rider un-does on the clearing edge via :meth:`ensure_base`) from an
        operator's deliberate ``stop feel-alive``.
        """
        return self._remove(target, STOPPED_BY_INHIBITION)

    def _remove(self, target: str, cause: str) -> dict:
        before = {ab.behavior.id for ab in self.active}
        if target == "all":
            keep = self._base_ids
            removed = [ab for ab in self.active if ab.behavior.id not in keep]
            self.active = [ab for ab in self.active if ab.behavior.id in keep]
        else:
            removed = [
                ab for ab in self.active if ab.behavior.id == target or ab.behavior.name == target
            ]
            removed_ids = {ab.behavior.id for ab in removed}
            self.active = [ab for ab in self.active if ab.behavior.id not in removed_ids]
        stopped = [ab.behavior.id for ab in removed]
        if any(ab.is_base for ab in removed):
            self._base_stopped_by = cause
        return {
            "ok": True,
            "op": "stop",
            "target": target,
            "stopped": stopped,
            "count": len(stopped),
            "unknown": bool(not stopped and target != "all" and target not in before),
        }

    def apply(self, cmd: dict, now: float) -> dict:
        """Apply one spool command defensively — a bad command never kills the loop."""
        op = cmd.get("op")
        try:
            if op == "add":
                lifetime = Lifetime(**cmd.get("lifetime", {}))
                return self.add(
                    cmd["name"],
                    dict(cmd.get("params", {})),
                    StopClass(cmd["class"]),
                    lifetime,
                    now,
                    channels=cmd.get("channels"),
                )
            if op == "stop":
                return self.stop(str(cmd.get("target", "all")))
            if op == "list":
                return {"ok": True, "op": "list"}
            return {"ok": False, "error": f"unknown op {op!r}"}
        except CliError as err:
            return {"ok": False, "op": op, "error": err.message}
        except Exception as err:  # defensive: isolate a bad command
            return {"ok": False, "op": op, "error": f"{type(err).__name__}: {err}"}

    # --- composition -----------------------------------------------------
    def compose_tick(self, now: float, sense: Sense = EMPTY_SENSE) -> dict:
        """Drop expired/completed, arbitrate, and compose a pose. Mutates ``active``.

        Every live behavior is asked for its contribution once (not just owners) so
        abstention-aware :func:`arbitrate` can fall a channel through to the next
        claimant when its nominal owner returns ``None`` for it. Contributions that
        explicitly set ``done`` are removed before arbitration, releasing every
        claimed channel in the same tick; their ids are reported in ``completed``.
        """
        live: list[ActiveBehavior] = []
        expired: list[str] = []
        for ab in self.active:
            if ab.behavior.is_expired(now - ab.start_t):
                expired.append(ab.behavior.id)
            else:
                live.append(ab)
        self.active = live

        # Only sensor-driven behaviors are fed the live snapshot; everything else
        # gets EMPTY_SENSE, so a behavior can't accidentally become sensor-
        # dependent just because some other behavior is polling.
        contribs: dict[str, Contribution] = {
            ab.behavior.id: ab.behavior.contribution(
                now - ab.start_t, sense if ab.behavior.wants_sense else EMPTY_SENSE
            )
            for ab in live
        }
        # `getattr(..., "done", False)` rather than `contribs[id].done`: arbitrate()
        # already treats a missing or malformed contribution as an abstention, and a
        # boot-persistent presence loop must degrade the same way here — one behavior
        # breaking its return contract cannot be allowed to kill the tick.
        completed = [
            ab.behavior.id for ab in live if getattr(contribs.get(ab.behavior.id), "done", False)
        ]
        if completed:
            completed_ids = set(completed)
            live = [ab for ab in live if ab.behavior.id not in completed_ids]
            self.active = live
            contribs = {
                behavior_id: contribution
                for behavior_id, contribution in contribs.items()
                if behavior_id not in completed_ids
            }

        behaviors = [ab.behavior for ab in live]
        owners = arbitrate(behaviors, contribs)
        pose = _compose_pose(owners, contribs)
        ownership = {ch: (o.id if o is not None else None) for ch, o in owners.items()}
        self._last_ownership = ownership
        self._last_sense = sense
        return {
            "pose": pose,
            "ownership": ownership,
            "expired": expired,
            "completed": completed,
        }

    # --- snapshot --------------------------------------------------------
    def state(self, now: float, config: EngineConfig) -> dict:
        """A JSON snapshot for ``behavior status`` (active set + channel ownership + DoA)."""
        if self._last_ownership is not None:
            ownership = self._last_ownership
        else:
            owners = arbitrate([ab.behavior for ab in self.active])
            ownership = {ch: (o.id if o is not None else None) for ch, o in owners.items()}
        active = []
        for ab in self.active:
            t_local = now - ab.start_t
            dur = ab.behavior.lifetime.duration
            active.append(
                {
                    "id": ab.behavior.id,
                    "name": ab.behavior.name,
                    "class": ab.behavior.stop_class.value,
                    "channels": sorted(ab.behavior.channels),
                    "looping": ab.behavior.lifetime.looping,
                    "t_local": round(t_local, 2),
                    "remaining": None if dur is None else round(max(0.0, dur - t_local), 2),
                    "base": ab.is_base,
                }
            )
        return {
            "updated": round(now, 3),
            "compose_hz": config.compose_hz,
            "active": active,
            "ownership": ownership,
            "doa": {
                "angle": self._last_sense.doa_angle,
                "speech_detected": self._last_sense.speech_detected,
            },
            # Additive (#183): an operator and the peer harness can tell a
            # deliberately still robot from an inhibited one, and both from an
            # engine that never seeded a base layer at all.
            "base_layer": {
                "seeded": self._base_seeded,
                "active": self.base_active(),
                "stopped_by": self._base_stopped_by,
            },
        }


def _compose_pose(owners: dict, contribs: dict) -> dict:
    """Assemble a complete immediate target from each channel's owner (else neutral)."""
    head = neutral_head()
    antennas: tuple[float, float] = (0.0, 0.0)
    body_yaw = 0.0
    owner = owners["head"]
    if owner is not None and contribs[owner.id].head is not None:
        head = dict(contribs[owner.id].head)
    owner = owners["antennas"]
    if owner is not None and contribs[owner.id].antennas is not None:
        antennas = contribs[owner.id].antennas
    owner = owners["body_yaw"]
    if owner is not None and contribs[owner.id].body_yaw is not None:
        body_yaw = contribs[owner.id].body_yaw
    return {"head": head, "antennas": antennas, "body_yaw": body_yaw}


# --------------------------------------------------------------------------- #
# The loop                                                                    #
# --------------------------------------------------------------------------- #

_NEUTRAL_POSE = {"head": neutral_head(), "antennas": (0.0, 0.0), "body_yaw": 0.0}


def _send_target(sink: TargetSink, pose: dict) -> object:
    return sink.set_target(head=pose["head"], antennas=pose["antennas"], body_yaw=pose["body_yaw"])


def _stream_tick(sink: TargetSink, pose: dict, consecutive: int, max_errors: int) -> int:
    """Stream one pose; return the running consecutive-error count (raises at the ceiling)."""
    try:
        _send_target(sink, pose)
    except CliError:
        consecutive += 1
        if consecutive >= max_errors:
            raise
        return consecutive
    return 0


@dataclass
class _Timing:
    """Derived per-run cadence: loop period, sleep slice, and state-publish heartbeat."""

    period: float
    slice_seconds: float
    heartbeat: int


def _timing(config: EngineConfig) -> _Timing:
    period = 1.0 / config.compose_hz if config.compose_hz > 0 else 0.0
    slice_seconds = min(period, DEFAULT_SLEEP_SLICE) if period > 0 else DEFAULT_SLEEP_SLICE
    heartbeat = max(1, int(round(config.compose_hz / 2.0)))
    return _Timing(period, slice_seconds, heartbeat)


def _apply_commands(engine: Engine, control: "control_mod.CommandSpool | None", now: float) -> bool:
    """Drain + apply pending spool commands; return whether the active set changed."""
    if control is None:
        return False
    changed = False
    for cmd in control.drain():
        control.write_result(cmd.get("cmd_id"), engine.apply(cmd, now))
        changed = True
    return changed


def _read_sense(engine: Engine, sense, t: float, *, force: bool = False) -> Sense:
    """Poll the sense source — but only while some behavior wants it (else EMPTY).

    Gating on ``wants_sense`` keeps an idle engine from touching the mic endpoint
    at all; the :class:`~reachy.behavior.sense.DoaPoller` itself throttles the rate.
    ``force`` overrides the gate: when a ``tick_seam`` is installed the seam
    consumes perception every tick, so the read happens regardless of whether any
    active behavior wants it (a rule can then react to sound even before it has
    admitted a sensor-driven behavior).
    """
    if sense is None or not (force or any(ab.behavior.wants_sense for ab in engine.active)):
        return EMPTY_SENSE
    return sense(t)


def _resolve_seam_emit(tick_seam) -> Callable[[dict], None]:
    """The seam's ``emit`` fan-out, resolved once per run (no-op without one)."""
    candidate = getattr(tick_seam, "emit", None) if tick_seam is not None else None
    return candidate if callable(candidate) else _noop_emit


def _invoke_seam(tick_seam, seam_emit, engine: Engine, t: float, ticks: int, snapshot, tick):
    """Call the tick seam with this tick's :class:`TickContext`."""
    tick_seam(
        TickContext(
            now=t,
            tick=ticks,
            sense=snapshot,
            pose=tick["pose"],
            ownership=tick["ownership"],
            emit=seam_emit,
            admit=lambda beh, _t=t: engine.admit_behavior(beh, _t),
            evict=engine.evict,
            active_names=lambda: {ab.behavior.name for ab in engine.active},
            ensure_base=lambda _t=t: engine.ensure_base(_t),
        )
    )


def _drive(
    engine: Engine,
    sink: TargetSink,
    config: EngineConfig,
    *,
    control,
    emit,
    stop: dict,
    now,
    sleep,
    max_ticks: int | None,
    timing: _Timing,
    sense=None,
    tick_seam=None,
) -> int:
    """The 50 Hz body: drain → compose → stream → publish, until stopped. Returns ticks.

    Cadence is deadline-based (#97). The loop used to sleep the FULL period after
    each tick's work, so cadence = work + period — measured on the deployed robot
    as a mean 43.17 ms tick (23.16 Hz against the 50 Hz target: ~20.7 ms work +
    20 ms sleep reproduces the number exactly). Now each tick sleeps only the time
    remaining to an absolute per-tick deadline (established from the first clock
    read, advanced one period per tick), so the work is absorbed into the gap and
    the achieved cadence equals the period. A tick that overruns its budget sleeps
    zero — and once the loop is more than one full period behind, the deadline is
    RESET to "now" rather than running back-to-back catch-up ticks (a burst would
    violate the one-move-at-a-time motion discipline downstream). ``emit`` carries
    the per-tick timing seam: additive ``work_s`` (measured work duration) and
    ``sleep_s`` (the sleep actually requested, clamped ≥ 0) keys, the numbers an
    on-box profile run reads to apportion the work. Overrun *observability* is
    unchanged — :mod:`reachy.behavior.tick_metrics` still measures the seam's own
    duration against the budget on its own clock, independent of this scheduling.
    """
    ticks = 0
    consecutive = 0
    last_state_tick = -timing.heartbeat
    seam_emit = _resolve_seam_emit(tick_seam)
    deadline: float | None = None  # established from the first tick's clock read
    while not stop["flag"]:
        t = now()
        if deadline is None:
            deadline = t
        changed = _apply_commands(engine, control, t)
        snapshot = _read_sense(engine, sense, t, force=tick_seam is not None)
        tick = engine.compose_tick(t, snapshot)
        changed = changed or bool(tick["expired"] or tick["completed"])
        consecutive = _stream_tick(sink, tick["pose"], consecutive, config.max_errors)
        ticks += 1
        # Publish the engine's state snapshot BEFORE the seam runs: a seam rider
        # that augments state.json (the IntentDriver's "intents" view) is then the
        # tick's FINAL writer, so its additions are never clobbered by this write.
        if control is not None and (changed or ticks - last_state_tick >= timing.heartbeat):
            control.write_state(engine.state(t, config))
            last_state_tick = ticks
        if tick_seam is not None:
            _invoke_seam(tick_seam, seam_emit, engine, t, ticks, snapshot, tick)
        # Second clock read: the tick's work ends here. emit's own cost lands after
        # it, but the ABSOLUTE deadline arithmetic self-corrects on the next tick.
        work_end = now()
        remaining = deadline + timing.period - work_end
        sleep_s = remaining if remaining > 0.0 else 0.0
        if emit is not None:
            emit(
                {
                    "tick": ticks,
                    "ownership": tick["ownership"],
                    "work_s": work_end - t,
                    "sleep_s": sleep_s,
                }
            )
        if max_ticks is not None and ticks >= max_ticks:
            break
        if remaining <= -timing.period:
            deadline = work_end  # >1 period behind: restart the schedule, never burst
        else:
            deadline += timing.period
        if sleep_s > 0.0:
            interruptible_sleep(sleep_s, stop, sleep, timing.slice_seconds)
    return ticks


def run(
    transport,
    config: EngineConfig,
    *,
    sleep=time.sleep,
    now=time.monotonic,
    on_start: Callable[[], None] | None = None,
    emit: Callable[[dict], None] | None = None,
    max_ticks: int | None = None,
    control: control_mod.CommandSpool | None = None,
    engine: Engine | None = None,
    sense=None,
    tick_seam=None,
) -> int:
    """Drive the robot from composed behaviors until stopped. Returns ticks run.

    Connectivity is validated by an opening neutral ``set_target`` (a dead daemon
    raises, so the loop exits cleanly before announcing a start). ``on_start`` runs
    only after that succeeds. The robot is eased to neutral on exit (best effort).

    ``sense`` is an optional ``(t) -> Sense`` source (e.g. a
    :class:`~reachy.behavior.sense.DoaPoller`); it is polled only while a
    sensor-driven behavior is active (or every tick once a ``tick_seam`` is
    installed), and every behavior otherwise gets :data:`EMPTY_SENSE`.

    ``tick_seam`` is the single per-tick integration seam: an optional callable
    invoked once per tick as ``tick_seam(ctx)`` with a fresh :class:`TickContext`
    (see its docstring for the full contract). It is how the rules evaluator, the
    goto lane, and the export feed ride the loop without the engine importing any
    of them — compose the seam at the call site (e.g.
    :func:`reachy.behavior.rule_engine.compose_rule_seam`). If the seam exposes an
    ``emit(event)`` method it becomes ``ctx.emit``'s fan-out target. The seam is
    invoked directly and owns its own error isolation.
    """
    engine = engine if engine is not None else Engine()
    if control is not None:
        control.reset()
    stop = {"flag": False}
    handlers = install_stop_handlers(stop)
    timing = _timing(config)
    try:
        with transport.streaming() as sink:
            try:
                _send_target(sink, _NEUTRAL_POSE)  # preflight: validates the transport
                start_t = now()
                if config.base_layer:
                    engine.seed_base_layer(start_t, config.energy)
                if on_start is not None:
                    on_start()
                return _drive(
                    engine,
                    sink,
                    config,
                    control=control,
                    emit=emit,
                    stop=stop,
                    now=now,
                    sleep=sleep,
                    max_ticks=max_ticks,
                    timing=timing,
                    sense=sense,
                    tick_seam=tick_seam,
                )
            finally:
                if config.settle:
                    _settle(sink)
    finally:
        restore_stop_handlers(handlers)


def _settle(sink: TargetSink) -> None:
    """Best-effort ease to neutral on stop (a dead transport can't be settled)."""
    try:
        _send_target(sink, _NEUTRAL_POSE)
    except CliError:
        pass
