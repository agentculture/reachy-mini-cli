"""The pure behavior data model — channels, contention classes, lifetimes, poses.

No I/O, no transport, no ``reachy_mini``: every type here is a plain value object
so the arbitration core and the library are trivially unit-testable. A
:class:`Behavior` pairs a small immutable spec (which channels it claims, how it
contends, how long it lives) with a contribution function — its desired
per-channel offsets as a function of behavior-local time and the latest
:class:`~reachy.behavior.sense.Sense`.

Most behaviors are *pure* — given the same behavior-local time they return the
same offsets, ignoring ``sense`` — so motion is smooth and reproducible. A
sensor-driven behavior (one whose library entry sets ``wants_sense``) is the
exception: it reads ``sense`` and may hold internal slew state, so it is *not*
reproducible from time alone.

Units match the rest of the CLI (``move goto`` / ``alive``): millimetres for head
translation, degrees for rotation and antennas/body yaw. The engine converts to
the daemon's metres/radians at the transport boundary, exactly once.
"""

from __future__ import annotations

import enum
from dataclasses import dataclass, field
from typing import Callable

from reachy.behavior.sense import EMPTY_SENSE, Sense

# The three arbitration units. A *channel* is a group of DOF claimed and resolved
# atomically; they mirror the daemon's three independent target fields
# (target_head_pose / target_antennas / target_body_yaw). This tuple is the single
# source of truth — arbitration and composition iterate it, never the literals, so
# a future split (e.g. head orientation vs. translation) stays local to this file.
CHANNELS = ("head", "antennas", "body_yaw")

_HEAD_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")


def neutral_head() -> dict[str, float]:
    """A fresh centred head offset (all six axes zero)."""
    return dict.fromkeys(_HEAD_KEYS, 0.0)


class StopClass(enum.Enum):
    """How a behavior contends for the channels it claims.

    Ordered by ``priority``: a higher-priority behavior owns a contested channel
    at tick time, and (on admit) decides whether it can evict or is rejected.
    """

    PASSIVE = "passive"
    STOPPABLE = "stoppable"
    UNSTOPPABLE = "unstoppable"
    STOPPING = "stopping"

    @property
    def priority(self) -> int:
        return _PRIORITY[self]


# UNSTOPPABLE and STOPPING both "hold" a channel against newcomers; UNSTOPPABLE
# ranks highest so it also wins a same-tick contest. STOPPABLE drives but yields;
# PASSIVE only fills a channel nobody else wants.
_PRIORITY: dict[StopClass, int] = {
    StopClass.PASSIVE: 0,
    StopClass.STOPPABLE: 1,
    StopClass.STOPPING: 2,
    StopClass.UNSTOPPABLE: 3,
}

# Classes that block a channel against a newly-admitted behavior (cannot be
# evicted by an add). STOPPABLE is intentionally absent — it is the polite default.
BLOCKING_CLASSES = frozenset({StopClass.UNSTOPPABLE, StopClass.STOPPING})


@dataclass(frozen=True)
class Lifetime:
    """How long a behavior runs.

    * one-shot (``looping=False``) — runs once for ``duration`` seconds then
      expires (``duration`` is required, > 0);
    * looping (``looping=True``) — repeats until ``duration`` seconds elapse, or
      forever (``duration=None``) until explicitly stopped or evicted.
    """

    looping: bool = False
    duration: float | None = None

    def errors(self) -> list[str]:
        """Human-readable validity problems (empty == valid)."""
        problems: list[str] = []
        if self.duration is not None and self.duration <= 0:
            problems.append("duration must be > 0")
        if not self.looping and self.duration is None:
            problems.append("a one-shot behavior needs a duration")
        return problems


@dataclass
class Contribution:
    """A behavior's desired per-channel offsets at one instant.

    A channel left ``None`` is one this behavior does not drive this tick. ``head``
    is the six-key offset dict, ``antennas`` a ``(right, left)`` degree pair,
    ``body_yaw`` a scalar in degrees — all friendly units.
    """

    head: dict[str, float] | None = None
    antennas: tuple[float, float] | None = None
    body_yaw: float | None = None

    def channel(self, name: str):
        """The value for ``name`` (``head`` / ``antennas`` / ``body_yaw``)."""
        return getattr(self, name)


@dataclass
class Behavior:
    """A live behavior: an immutable spec plus a contribution function.

    ``fn`` maps ``(t_local, params, sense) -> Contribution``. For a pure behavior
    it ignores ``sense`` and returns the same offsets for the same behavior-local
    time, so motion is smooth and reproducible regardless of when it was admitted.
    A sensor-driven behavior (``wants_sense=True``) reads ``sense`` and may hold
    internal state, so the engine only feeds it a live :class:`Sense` (otherwise
    every behavior gets :data:`EMPTY_SENSE`). ``id`` is assigned by the engine
    (e.g. ``"speak-3"``); ``name`` is the library entry it came from.

    A contribution that leaves a *claimed* channel ``None`` **abstains** from it
    this tick: the engine then resolves that channel to the next-priority claimant
    (a sound-reactive behavior with no sound yields the head back to ``feel-alive``
    rather than freezing it).
    """

    id: str
    name: str
    channels: frozenset[str]
    stop_class: StopClass
    lifetime: Lifetime
    params: dict[str, float]
    fn: Callable[[float, dict, Sense], Contribution] = field(repr=False, compare=False)
    wants_sense: bool = False

    def contribution(self, t_local: float, sense: Sense = EMPTY_SENSE) -> Contribution:
        """The behavior's desired offsets at ``t_local`` seconds since it started."""
        return self.fn(t_local, self.params, sense)

    def is_expired(self, t_local: float) -> bool:
        """True once a finite lifetime has elapsed (looping-forever never expires)."""
        d = self.lifetime.duration
        return d is not None and t_local >= d
