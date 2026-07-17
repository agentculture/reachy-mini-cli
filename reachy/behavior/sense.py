"""Live sensor input for the behavior engine — sound Direction of Arrival (DoA).

Behaviors are otherwise *pure* functions of behavior-local time; this module is
the one live-input seam. A :class:`Sense` is the latest sensor snapshot the engine
hands every behavior each tick (today: sound direction plus loudness/pat/face/
frame-availability cues). The :class:`DoaPoller` reads the daemon's
``/api/state/doa`` route at a *low* rate (a few Hz — DoA updates slowly) and
tolerates the unit having no working mic, where the route answers ``500`` or JSON
``null``: any failure simply caches :data:`EMPTY_SENSE`, so a sound-reactive
behavior reads "no reading" and yields rather than crashing.

:class:`SenseProviders` + :func:`read_perception` are the seam for the other
cues: a small, duck-typed bundle of injected zero-arg PEEK callables (never
consuming reads) that a future engine composition can wire to the same shared
per-tick sources the folded ``listen`` hooks already use (``PatHook``,
``VisionHook``, ``FaceHook`` — see ``reachy/motion/listen_*.py``), so multiple
consumers reading the same tick's sample never race or steal from one another.
Not wired into :mod:`reachy.behavior.engine` yet — this module only defines the
snapshot shape and the provider contract.

Stdlib only, and it imports neither the transport nor the model package (it
duck-types the transport's ``doa`` method, and every provider callable) so it
stays a dependency-free leaf.
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

    ``rms``, ``pat_event``, ``face``, and ``frame_available`` extend the
    snapshot with the folded-hook cues (mirroring ``listen``'s ``PatHook`` /
    ``VisionHook`` / ``FaceHook``) so a future sensor-driven behavior can read
    them the same way it reads ``doa_angle`` today. Each has a "no reading"
    default so every existing bare or doa-only ``Sense(...)`` call site keeps
    constructing a valid, fully-populated snapshot with no code change:

    - ``rms`` — mic loudness for the tick (the same loudness cue
      ``reachy.motion.listen.ListenProducer``'s ``SnapDetector`` reads), or
      ``None`` when not sampled.
    - ``pat_event`` — ``(touch_type, level)`` from a folded ``PatHook``
      detection this tick (mirrors
      ``EventBuffer.feed_pat(kind, level)``'s argument shape), or ``None``
      when there was no pat this tick.
    - ``face`` — the name of a recognised, named face this tick (mirrors
      ``EventBuffer.feed_face(name)``), or ``None`` (no match, an unnamed
      face, or not sampled).
    - ``frame_available`` — whether a camera frame was available to peek this
      tick. A signal only — never the frame itself, so this module never
      needs to name a frame's concrete type (numpy/cv2) and stays a
      dependency-free leaf. Defaults ``False``.
    """

    doa_angle: float | None = None
    speech_detected: bool = False
    rms: float | None = None
    pat_event: tuple[str, str] | None = None
    face: str | None = None
    frame_available: bool = False


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


#: A provider is a zero-arg callable returning its field's latest reading — a
#: PEEK of a shared per-tick source, never a consuming read. This is the same
#: "peek, not take" contract as
#: ``reachy.motion.listen_vision.VisionHook.latest_frame`` (a non-consuming
#: peek at the vision grabber's held frame) versus that hook's own consuming
#: ``take()`` — so two independent consumers of the SAME provider (e.g. a
#: behavior and a cognition sink) see the SAME tick's value, and a provider
#: never needs — or opens — a second media session or camera grabber of its
#: own; it just reads whatever the ONE upstream loop already captured this
#: tick. Providers are duck-typed exactly like :class:`DoaPoller`'s ``read``
#: callable: this module only names the shape and never imports a concrete
#: source (no ``reachy_mini``, no ``cv2``) — it stays a dependency-free leaf.
RmsProvider = Callable[[], float | None]
PatEventProvider = Callable[[], tuple[str, str] | None]
FaceProvider = Callable[[], str | None]
FrameAvailableProvider = Callable[[], bool]


@dataclass(frozen=True)
class SenseProviders:
    """Bundle of injected, non-consuming perception taps.

    Each field is an optional zero-arg provider callable (see the
    ``*Provider`` type aliases above) that the engine composition layer
    supplies once per process — a real provider peeks a shared per-tick
    holder/sample (mirroring how ``VisionHook.latest_frame`` peeks its
    grabber's frame holder rather than consuming it), so multiple consumers
    reading the SAME tick's sample never race or steal from one another.
    ``None`` in any field means "no provider wired"; :func:`read_perception`
    fills that field with :class:`Sense`'s own default. Frozen and stdlib-only
    — this module never imports the concrete sources (``reachy_mini``,
    ``cv2``, ...) that back real providers, keeping it a dependency-free leaf.
    """

    rms: RmsProvider | None = None
    pat_event: PatEventProvider | None = None
    face: FaceProvider | None = None
    frame_available: FrameAvailableProvider | None = None


#: A :class:`SenseProviders` with every field unset — "no providers wired".
#: Plays the same role for providers that :data:`EMPTY_SENSE` plays for
#: readings: ``read_perception(NO_PROVIDERS)`` always returns a snapshot with
#: every extension field at its safe default.
NO_PROVIDERS = SenseProviders()


def _peek(provider, default):
    """Call *provider* if present, tolerating any failure -> *default*.

    Mirrors :class:`DoaPoller`'s failure handling: a raising or missing
    provider must never crash the compose loop — it just means "no reading"
    for that field this tick.
    """
    if provider is None:
        return default
    try:
        return provider()
    except Exception:  # noqa: BLE001
        return default


def read_perception(
    providers: SenseProviders = NO_PROVIDERS,
    *,
    base: Sense = EMPTY_SENSE,
) -> Sense:
    """Compose a full :class:`Sense` snapshot from *base* plus *providers*.

    *base* supplies ``doa_angle``/``speech_detected`` (typically a
    :class:`DoaPoller`'s latest reading, or :data:`EMPTY_SENSE` when there is
    none); each configured provider in *providers* is peeked — never consumed
    — for the remaining fields. A missing or raising provider degrades to
    that field's safe default (mirroring :class:`DoaPoller`'s own failure
    handling), so a partially-wired provider set can never crash the tick.
    Calling this more than once for the same tick (e.g. once per consumer)
    reads each provider again; a well-behaved ("peek") provider returns the
    identical value every time.
    """
    return Sense(
        doa_angle=base.doa_angle,
        speech_detected=base.speech_detected,
        rms=_peek(providers.rms, None),
        pat_event=_peek(providers.pat_event, None),
        face=_peek(providers.face, None),
        frame_available=bool(_peek(providers.frame_available, False)),
    )


def doa_angle_to_yaw(angle: float, gain: float) -> float:
    """Map a DoA angle (radians) to a head/body yaw target in degrees.

    The daemon's angle runs ``0``=left .. ``pi/2``=front .. ``pi``=right, while
    yaw is degrees with ``+``=left (matches ``body-turn-hold``). So front maps to
    ``0`` and the sign is ``degrees(pi/2 - angle)`` — sound on the left yields a
    positive (leftward) yaw. ``gain`` scales the ~±90° acoustic span before the
    caller clamps to the joint's range.
    """
    return math.degrees(math.pi / 2.0 - angle) * gain
