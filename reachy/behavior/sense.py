"""Live sensor input for the behavior engine — sound Direction of Arrival (DoA).

Behaviors are otherwise *pure* functions of behavior-local time; this module is
the one live-input seam. A :class:`Sense` is the latest sensor snapshot the engine
hands every behavior each tick (today: just sound direction). The
:class:`DoaPoller` reads the daemon's ``/api/state/doa`` route at a *low* rate (a
few Hz — DoA updates slowly) and tolerates the unit having no working mic, where
the route answers ``500`` or JSON ``null``: any failure simply caches
:data:`EMPTY_SENSE`, so a sound-reactive behavior reads "no reading" and yields
rather than crashing.

Stdlib only, and it imports neither the transport nor the model package (it
duck-types the transport's ``doa`` method) so it stays a dependency-free leaf.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Callable

# DoA angle is radians: 0 = left, pi/2 = front, pi = right (the daemon's
# convention). Poll a few Hz, not 50 — DoA updates slowly — and read it with a
# short timeout so a slow/hanging daemon can never stall the 50 Hz compose loop
# for long (a missed read just yields EMPTY_SENSE for that window).
DOA_POLL_PERIOD = 0.2  # seconds (5 Hz)
DOA_TIMEOUT = 0.1  # seconds


@dataclass(frozen=True)
class Sense:
    """The latest sensor snapshot fed to every behavior each tick.

    ``doa_angle`` is the sound Direction of Arrival in radians (``0``=left,
    ``pi/2``=front, ``pi``=right), or ``None`` when there is no usable reading
    (no mic, daemon error, or no sound). ``speech_detected`` is the daemon's
    speech-vs-any-sound flag for the same reading.
    """

    doa_angle: float | None = None
    speech_detected: bool = False


# The "no reading" snapshot — what behaviors get when nothing senses, the poll
# fails, or the unit has no mic. A sound-reactive behavior treats it as "yield".
EMPTY_SENSE = Sense()


def read_doa(transport, *, timeout: float = DOA_TIMEOUT) -> Sense:
    """Read one DoA snapshot from a transport. May raise the transport's error.

    Maps the daemon's ``{angle, speech_detected}`` (or a ``null`` body, which the
    HTTP transport surfaces as ``None``) onto a :class:`Sense`. A missing or
    ``null`` ``angle`` becomes ``doa_angle=None`` so callers degrade gracefully.
    The :class:`DoaPoller` is what swallows transport failures; this helper just
    does the shape-mapping.
    """
    result = transport.doa(timeout=timeout)
    if not isinstance(result, dict):
        return EMPTY_SENSE
    angle = result.get("angle")
    return Sense(
        doa_angle=float(angle) if angle is not None else None,
        speech_detected=bool(result.get("speech_detected", False)),
    )


class DoaPoller:
    """Throttle a DoA reader to a low rate and tolerate every failure.

    Callable as ``poller(t) -> Sense`` where ``t`` is the engine's (injectable)
    monotonic clock, so throttling is deterministic in tests. At most one read per
    ``period`` seconds; between reads the last snapshot is returned. Any exception
    from ``read`` (a dead mic's ``500``, an unreachable daemon, an unsupported
    transport) caches :data:`EMPTY_SENSE` — the loop never sees the error.
    """

    def __init__(
        self,
        read: Callable[[], Sense],
        *,
        period: float = DOA_POLL_PERIOD,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._read = read
        self._period = period
        self._now = now
        self._last: Sense = EMPTY_SENSE
        self._next_t: float | None = None

    def __call__(self, t: float | None = None) -> Sense:
        """Return the latest snapshot, reading afresh at most once per ``period``."""
        if t is None:
            t = self._now()
        if self._next_t is None or t >= self._next_t:
            self._next_t = t + self._period
            # Any failure (no mic, a 500, an unsupported transport) means "no
            # reading" — it must never crash the 50 Hz loop.
            try:
                self._last = self._read()
            except Exception:  # noqa: BLE001
                self._last = EMPTY_SENSE
        return self._last


def doa_angle_to_yaw(angle: float, gain: float) -> float:
    """Map a DoA angle (radians) to a head/body yaw target in degrees.

    The daemon's angle runs ``0``=left .. ``pi/2``=front .. ``pi``=right, while
    yaw is degrees with ``+``=left (matches ``body-turn-hold``). So front maps to
    ``0`` and the sign is ``degrees(pi/2 - angle)`` — sound on the left yields a
    positive (leftward) yaw. ``gain`` scales the ~±90° acoustic span before the
    caller clamps to the joint's range.
    """
    return math.degrees(math.pi / 2.0 - angle) * gain
