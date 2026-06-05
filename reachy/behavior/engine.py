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

from reachy.behavior import control as control_mod
from reachy.behavior import library
from reachy.behavior.arbitration import admit, arbitrate
from reachy.behavior.model import Behavior, Lifetime, StopClass, neutral_head
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
        beh = library.build(
            BASE_LAYER_NAME,
            params,
            StopClass.PASSIVE,
            Lifetime(looping=True, duration=None),
            self._next_id(BASE_LAYER_NAME),
        )
        self.active.append(ActiveBehavior(beh, now, is_base=True))
        self._base_ids.add(beh.id)
        return beh.id

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
        """
        beh = library.build(name, params, stop_class, lifetime, self._next_id(name))
        if channels:
            beh = dataclasses.replace(beh, channels=frozenset(channels))
        result = admit(beh, self.behaviors())
        evicted_ids = {b.id for b in result.evicted}
        if evicted_ids:
            self.active = [ab for ab in self.active if ab.behavior.id not in evicted_ids]
        self.active.append(ActiveBehavior(beh, now))
        return {
            "ok": True,
            "op": "add",
            "id": beh.id,
            "name": name,
            "class": stop_class.value,
            "channels": sorted(beh.channels),
            "evicted": [b.id for b in result.evicted],
            "blocked": result.blocked,
        }

    def stop(self, target: str) -> dict:
        """Stop a behavior by id or name, or ``all`` (keeps the passive base layer)."""
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
        except Exception as err:  # noqa: BLE001 - defensive: isolate a bad command
            return {"ok": False, "op": op, "error": f"{type(err).__name__}: {err}"}

    # --- composition -----------------------------------------------------
    def compose_tick(self, now: float, sense: Sense = EMPTY_SENSE) -> dict:
        """Drop expired, arbitrate, and compose one complete pose. Mutates ``active``.

        Every live behavior is asked for its contribution once (not just owners) so
        abstention-aware :func:`arbitrate` can fall a channel through to the next
        claimant when its nominal owner returns ``None`` for it.
        """
        live: list[ActiveBehavior] = []
        expired: list[str] = []
        for ab in self.active:
            if ab.behavior.is_expired(now - ab.start_t):
                expired.append(ab.behavior.id)
            else:
                live.append(ab)
        self.active = live

        behaviors = [ab.behavior for ab in live]
        # Only sensor-driven behaviors are fed the live snapshot; everything else
        # gets EMPTY_SENSE, so a behavior can't accidentally become sensor-
        # dependent just because some other behavior is polling.
        contribs: dict[str, object] = {
            ab.behavior.id: ab.behavior.contribution(
                now - ab.start_t, sense if ab.behavior.wants_sense else EMPTY_SENSE
            )
            for ab in live
        }
        owners = arbitrate(behaviors, contribs)
        pose = _compose_pose(owners, contribs)
        ownership = {ch: (o.id if o is not None else None) for ch, o in owners.items()}
        self._last_ownership = ownership
        self._last_sense = sense
        return {"pose": pose, "ownership": ownership, "expired": expired}

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


def _read_sense(engine: Engine, sense, t: float) -> Sense:
    """Poll the sense source — but only while some behavior wants it (else EMPTY).

    Gating on ``wants_sense`` keeps an idle engine from touching the mic endpoint
    at all; the :class:`~reachy.behavior.sense.DoaPoller` itself throttles the rate.
    """
    if sense is None or not any(ab.behavior.wants_sense for ab in engine.active):
        return EMPTY_SENSE
    return sense(t)


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
) -> int:
    """The 50 Hz body: drain → compose → stream → publish, until stopped. Returns ticks."""
    ticks = 0
    consecutive = 0
    last_state_tick = -timing.heartbeat
    while not stop["flag"]:
        t = now()
        changed = _apply_commands(engine, control, t)
        tick = engine.compose_tick(t, _read_sense(engine, sense, t))
        changed = changed or bool(tick["expired"])
        consecutive = _stream_tick(sink, tick["pose"], consecutive, config.max_errors)
        ticks += 1
        if control is not None and (changed or ticks - last_state_tick >= timing.heartbeat):
            control.write_state(engine.state(t, config))
            last_state_tick = ticks
        if emit is not None:
            emit({"tick": ticks, "ownership": tick["ownership"]})
        if max_ticks is not None and ticks >= max_ticks:
            break
        interruptible_sleep(timing.period, stop, sleep, timing.slice_seconds)
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
) -> int:
    """Drive the robot from composed behaviors until stopped. Returns ticks run.

    Connectivity is validated by an opening neutral ``set_target`` (a dead daemon
    raises, so the loop exits cleanly before announcing a start). ``on_start`` runs
    only after that succeeds. The robot is eased to neutral on exit (best effort).

    ``sense`` is an optional ``(t) -> Sense`` source (e.g. a
    :class:`~reachy.behavior.sense.DoaPoller`); it is polled only while a
    sensor-driven behavior is active, and every behavior otherwise gets
    :data:`EMPTY_SENSE`.
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
