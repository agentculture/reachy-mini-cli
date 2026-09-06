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
consuming reads) wired to shared per-tick sources, so multiple consumers reading
the same tick's sample never race or steal from one another. This module only
defines the snapshot shape and the provider contract; the composition root
(``_compose_run_seam`` in ``reachy.cli._commands.behavior``) is what supplies
the concrete callables — today for every optional field (see
:data:`_COMPOSED_PROVIDER_FIELDS`).

Stdlib only, and it imports neither the transport nor the model package (it
duck-types the transport's ``doa`` method, and every provider callable) so it
stays a dependency-free leaf.
"""

from __future__ import annotations

import math
import time
from dataclasses import dataclass, replace
from typing import Callable, Literal

# DoA angle is radians: 0 = left, pi/2 = front, pi = right (the daemon's
# convention). Poll a few Hz, not 50 — DoA updates slowly — and read it with a
# short timeout so a slow/hanging daemon can never stall the 50 Hz compose loop
# for long (a missed read just yields EMPTY_SENSE for that window).
DOA_POLL_PERIOD = 0.2  # seconds (5 Hz)
DOA_TIMEOUT = 0.1  # seconds


# Wire-friendly string literals keep this module stdlib-only while making the
# persistent pat contract explicit to the detector/driver, behavior, and export
# layers that meet here. Availability is deliberately tri-state: an available
# sample with ``contact=False`` is an observed release, while ``blocked`` and
# ``unavailable`` must never be mistaken for physical no-contact.
PatAvailability = Literal["available", "blocked", "unavailable"]
PatTouchType = Literal["scratch", "side_pat"]
PatLevel = Literal["level1", "level2"]
PatPhase = Literal[
    "idle",
    "receptive",
    "contentment",
    "warning",
    "released",
    "enough",
    "cooldown",
]


@dataclass(frozen=True)
class PatState:
    """Event-stable snapshot of one proprioceptive pat interaction.

    ``availability`` separates observed samples from commanded-motion blocking
    and reader failure. ``contact`` therefore only claims what the most recent
    meaningful state says; consumers must inspect availability before treating
    ``False`` as an observed release. ``yaw_deg`` is signed robot-frame yaw in
    degrees, never a guessed physical left/right label.

    ``blocked_reason`` (issue #168) names WHICH gate is closed while
    ``availability == "blocked"``, so a live measurement of "N/N samples
    blocked" can say which one. It is one of four wire strings —
    ``"stillness"`` (the commanded pose has not been quiet long enough,
    :func:`reachy.behavior.pat_sense.PatSenseDriver._stillness_open`),
    ``"ownership"`` (another behavior took the head mid-interaction,
    :meth:`~reachy.behavior.pat_sense.PatSenseDriver._ownership_changed`),
    ``"clock-gap"`` (the logical observation interval exceeded its bound,
    :meth:`~reachy.behavior.pat_sense.PatSenseDriver._observation_clock_gapped`),
    or ``"no-command"`` (this tick's commanded pose was missing or malformed)
    — and it is always ``None`` outside ``"blocked"``: ``None`` when
    ``availability == "available"`` (there is nothing to name), and ``None``
    when ``availability == "unavailable"`` (a missing reading is already
    unambiguous on its own; giving it a reason too would just be
    ``"unavailable"`` spelled twice). The :data:`PatAvailability` Literal
    itself stays byte-identical — this field is purely additive, so no
    existing consumer keyed on ``availability`` alone changes behavior.

    The two monotonic anchors let consumers derive recency from their own clock.
    No derived age or tick timestamp is stored, so equal interaction states stay
    equal across unchanged 50 Hz ticks and change-only exporters remain bounded.
    """

    availability: PatAvailability = "unavailable"
    contact: bool = False
    touch_type: PatTouchType | None = None
    level: PatLevel | None = None
    yaw_deg: float | None = None
    phase: PatPhase = "idle"
    phase_started_at: float | None = None
    last_press_at: float | None = None
    blocked_reason: str | None = None


#: Safe default for a missing, failed, or not-yet-wired pat-state provider.
#: This is intentionally not an available no-contact observation.
UNAVAILABLE_PAT_STATE = PatState()


@dataclass(frozen=True)
class Sense:
    """The latest sensor snapshot fed to every behavior each tick.

    ``doa_angle`` is the sound Direction of Arrival in radians (``0``=left,
    ``pi/2``=front, ``pi``=right), or ``None`` when there is no usable reading
    (no mic, daemon error, or no sound). ``speech_detected`` is the daemon's
    speech-vs-any-sound flag for the same reading. ``doa_age_s`` is how long
    ago (seconds) the last GOOD (angle-bearing) DoA reading landed — the
    :class:`DoaPoller`'s own :meth:`DoaPoller.age_s`, carried onto the snapshot
    it returns as ``base`` to :func:`read_perception` — or ``None`` when there
    has never been one. Deliberately Sense's OWN field, not a call-time
    lookup: a one-shot behavior like ``look-at-sound``
    (:mod:`reachy.behavior.gaze`) samples it once at admission and must see
    the SAME age a concurrent rule predicate would, not a second, later clock
    read.

    ``rms``, ``pat_event``, ``pat_state``, ``face``, ``frame_available``,
    ``transcript``, and ``self_moving`` extend the snapshot with the cues the
    retired ``listen`` loop carried on its folded hooks (``PatHook`` /
    ``VisionHook`` / ``FaceHook`` / ``TranscribeHook``), so a sensor-driven
    behavior reads them the same way it reads ``doa_angle``. Each has a "no reading"
    default so every existing bare or doa-only ``Sense(...)`` call site keeps
    constructing a valid, fully-populated snapshot with no code change:

    - ``rms`` — mic loudness for the tick (the same loudness cue
      ``reachy.motion.listen.ListenProducer``'s ``SnapDetector`` reads), or
      ``None`` when not sampled. Deliberately RAW: an honest measurement other
      consumers read, never the admission predicate (see ``rms_ratio``).
    - ``rms_ratio`` — the same tick's loudness over a rolling estimate of the
      room's own background (:class:`reachy.behavior.rms_background.RmsBackground`),
      or ``None`` when not sampled or the estimate is still cold. This is what
      sound ADMISSION keys on, because the measured mic background drifts ~25x
      across conditions one robot lives in within 24 h (issue #102) — so "loud"
      has to be a comparison, and a one-predicate rule cannot express a
      comparison, so it arrives already made.
    - ``pat_event`` — ``(touch_type, level)`` from a pat detection this tick
      (mirrors
      ``EventBuffer.feed_pat(kind, level)``'s argument shape), or ``None``
      when there was no pat this tick.
    - ``pat_state`` — the persistent, event-stable interaction snapshot. Its
      unavailable default is distinct from an observed no-contact sample.
    - ``face`` — the name of a recognised, named face this tick (mirrors
      ``EventBuffer.feed_face(name)``), or ``None`` (no match, an unnamed
      face, or not sampled).
    - ``face_bbox`` — WHERE the best face of the latest detection is:
      ``(x, y, w, h)``, normalised to the frame (``0..1``, origin top-left),
      or ``None`` when no face has been detected recently. Unlike ``face``
      this is a HELD level, not a one-tick event, and it is deliberately
      independent of recognition: an unknown face — a stranger looking at the
      robot — still has a position, so ``face_bbox`` can be set while ``face``
      is ``None``. Which face, when several are in frame, is decided by
      :func:`reachy.behavior.face_sense.select_face` (a recognised face first,
      biggest among equals).
    - ``face_age_s`` — how long ago (seconds) the ``face_bbox`` detection
      landed, or ``None`` when there is no position reading. A consumer that
      must not act on a stale position reads this rather than guessing the
      camera's cadence; the producer also expires the bbox on its own TTL.
    - ``frame_available`` — whether a camera frame was available to peek this
      tick. A signal only — never the frame itself, so this module never
      needs to name a frame's concrete type (numpy/cv2) and stays a
      dependency-free leaf. Defaults ``False``.
    - ``transcript`` — the WORDS of an addressed utterance heard this tick
      (mirroring the retiring loop's ``feed_transcript(text)``), or ``None``
      when nothing was heard or the utterance did not clear the engagement
      gate. Delivered to exactly ONE snapshot by
      :class:`reachy.behavior.transcript_sense.TranscriptSenseDriver`'s
      one-tick latch, the same cadence ``pat_event`` uses — so it is a cue
      ("this was just said"), never a standing value to poll.
    - ``self_moving`` — whether the engine is currently COMMANDING motion (any
      watched pose axis — head, ANTENNAS, body yaw — changed above eps within
      the release tail; see :class:`reachy.behavior.self_motion.SelfMotionDriver`).
      A CONDITION like ``frame_available`` (an always-populated boolean, never
      ``None``), defaulting ``False``. It is both a rule predicate in its own
      right and the latch behind the moving rms floor (#95).
    """

    doa_angle: float | None = None
    speech_detected: bool = False
    doa_age_s: float | None = None
    rms: float | None = None
    pat_event: tuple[str, str] | None = None
    face: str | None = None
    face_bbox: tuple[float, float, float, float] | None = None
    face_age_s: float | None = None
    frame_available: bool = False
    pat_state: PatState = UNAVAILABLE_PAT_STATE
    transcript: str | None = None
    self_moving: bool = False
    rms_ratio: float | None = None


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
        # The monotonic timestamp of the last read whose angle was USABLE (not
        # every poll — a failed or angle-less poll never advances this), so
        # `age_s` answers "how stale is the last good bearing" even across a
        # run of subsequent failures that overwrite `_last` with EMPTY_SENSE.
        self._last_good_t: float | None = None

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
            except Exception:
                self._last = EMPTY_SENSE
            else:
                if self._last.doa_angle is not None:
                    self._last_good_t = t
        age = self.age_s(t)
        if age is None:
            # No good reading yet: return `_last` UNCHANGED (never a `replace`
            # copy) so identity-sensitive callers (e.g. `poller(t) is
            # EMPTY_SENSE`) keep working — there is nothing to stamp.
            return self._last
        return replace(self._last, doa_age_s=age)

    def age_s(self, now: float) -> float | None:
        """Seconds since the last GOOD (angle-bearing) DoA reading, or ``None``.

        ``None`` means "never read a usable angle" — distinct from a very
        stale one. Uses the *caller's* ``now``, not the poller's own throttled
        clock, so a behavior can ask "how stale is this right now" between
        polls, not only at poll time.
        """
        if self._last_good_t is None:
            return None
        return now - self._last_good_t


#: A provider is a zero-arg callable returning its field's latest reading — a
#: PEEK of a shared per-tick source, never a consuming read. This is the same
#: "peek, not take" contract as
#: the retired ``VisionHook.latest_frame`` peek (a non-consuming
#: peek at the vision grabber's held frame) versus that hook's own consuming
#: ``take()`` — so two independent consumers of the SAME provider (e.g. a
#: behavior and a cognition sink) see the SAME tick's value, and a provider
#: never needs — or opens — a second media session or camera grabber of its
#: own; it just reads whatever the ONE upstream loop already captured this
#: tick. Providers are duck-typed exactly like :class:`DoaPoller`'s ``read``
#: callable: this module only names the shape and never imports a concrete
#: source (no ``reachy_mini``, no ``cv2``) — it stays a dependency-free leaf.
RmsProvider = Callable[[], float | None]
RmsRatioProvider = Callable[[], float | None]
PatEventProvider = Callable[[], tuple[str, str] | None]
PatStateProvider = Callable[[], PatState | None]
FaceProvider = Callable[[], str | None]
FaceBboxProvider = Callable[[], tuple[float, float, float, float] | None]
FaceAgeProvider = Callable[[], float | None]
FrameAvailableProvider = Callable[[], bool]
TranscriptProvider = Callable[[], str | None]
SelfMovingProvider = Callable[[], bool]


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
    face_bbox: FaceBboxProvider | None = None
    face_age_s: FaceAgeProvider | None = None
    frame_available: FrameAvailableProvider | None = None
    pat_state: PatStateProvider | None = None
    transcript: TranscriptProvider | None = None
    self_moving: SelfMovingProvider | None = None
    rms_ratio: RmsRatioProvider | None = None


#: Predicate-field names (the ``Predicate.field`` vocabulary validated against
#: ``reachy.behavior.rules.SENSE_FIELDS``) that the base DoA/speech leg feeds
#: UNCONDITIONALLY. Every composition builds a :class:`DoaPoller` regardless of
#: which optional :class:`SenseProviders` fields it wires (see
#: :func:`read_perception`'s ``base`` argument) — a mic-less box degrades the
#: *reading* to "no reading", never the *wiring* to "no provider" — so these
#: two never depend on any ``SenseProviders`` field being set.
_ALWAYS_FED_FIELDS: frozenset[str] = frozenset({"doa", "speech"})

#: Maps each optional :class:`SenseProviders` attribute to the predicate-field
#: name it feeds once a real callable is wired into it.
#:
#: NOT every provider appears here: ``face_bbox`` and ``face_age_s`` are
#: CONTINUOUS readings a motion behavior consumes off the :class:`Sense`
#: snapshot directly, not ``rules.toml`` predicate fields (they are absent from
#: :data:`reachy.behavior.rules.SENSE_FIELDS`, so no rule can key on them and
#: there is nothing for ``behavior rules check`` to warn about). A provider
#: belongs in this map only once a predicate field names it.
_PROVIDER_PREDICATE_FIELDS: dict[str, str] = {
    "rms": "rms",
    "rms_ratio": "rms_ratio",
    "pat_event": "pat",
    "face": "face",
    "frame_available": "frame_available",
    "transcript": "transcript",
    "self_moving": "self_moving",
}

#: Which of the optional :class:`SenseProviders` attributes the CURRENT engine
#: composition (``_compose_run_seam`` in ``reachy.cli._commands.behavior``)
#: actually wires a live provider for. Since t28 that is EVERY optional field:
#: ``pat_event``/``pat_state`` (the folded pat-sense driver), ``rms`` and
#: ``rms_ratio`` (two peeks of the ONE ``RmsSense`` over the shared per-tick mic
#: chunk — the raw loudness gated by the #95 moving floor, and its ratio over
#: the rolling background of #102), ``transcript`` (the
#: transcript driver's one-tick latch of an addressed utterance),
#: ``face``/``frame_available`` (the face driver's name latch and TTL-held
#: camera condition) and ``self_moving`` (the self-motion driver's held latch
#: over the commanded pose, #95).
#:
#: "Wired" is about the COMPOSITION, not the hardware: a box with no ``[sdk]``
#: extra, no camera or no reachable STT still has these providers wired and
#: simply reads "no reading" through them — the same distinction
#: :data:`_ALWAYS_FED_FIELDS` draws for a mic-less box's DoA leg. A rule keyed
#: on one of these is therefore live code, not a silent no-op, which is exactly
#: what this set is asked to declare.
#:
#: THIS SET IS THE ONE DECLARED SOURCE OF TRUTH ``behavior rules check``
#: (``reachy.cli._commands.behavior._unfed_field_warnings``) reads to warn on a
#: rule predicate keyed to a sense field nothing feeds. A predicate field can
#: be a valid, schema-accepted ``rules.toml`` value (see
#: ``reachy.behavior.rules.SENSE_FIELDS``) while still being unfed here — that
#: gap is exactly what the check surfaces. When a future composition wires a
#: new provider (e.g. rms/face/frame_available/transcript), add its predicate
#: field to :data:`FED_SENSE_FIELDS` (via this set, or directly) in the SAME
#: change that wires the provider — this is the only place that needs
#: updating; nothing else duplicates this list, so the check can never drift
#: from reality by forgetting a second copy.
_COMPOSED_PROVIDER_FIELDS: frozenset[str] = frozenset(
    {"pat_event", "rms", "rms_ratio", "face", "frame_available", "transcript", "self_moving"}
)

#: The full set of predicate fields (``Predicate.field`` values) the current
#: composition feeds a live reading for. A predicate keyed on any field
#: outside this set will validate cleanly (it is schema-valid) and then
#: silently never fire — see ``behavior rules check``'s unfed-field warning.
FED_SENSE_FIELDS: frozenset[str] = _ALWAYS_FED_FIELDS | frozenset(
    _PROVIDER_PREDICATE_FIELDS[name] for name in _COMPOSED_PROVIDER_FIELDS
)


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
    except Exception:
        return default


def _coerce_bbox(value) -> tuple[float, float, float, float] | None:
    """Accept only a 4-tuple of finite numbers -> ``(x, y, w, h)``; else ``None``.

    A malformed reading is a NON-READING, never a raise: the same degradation
    :func:`_peek` gives a provider that blows up. Strings are rejected on
    purpose — a 4-character string is iterable and would otherwise sneak
    through as a "box".
    """
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        x, y, w, h = value  # type: ignore[misc]
        box = (float(x), float(y), float(w), float(h))
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in box):
        return None
    return box


def _coerce_float(value) -> float | None:
    """Accept a finite number -> ``float``; anything else is a non-reading."""
    if value is None or isinstance(value, (str, bytes)):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if math.isfinite(out) else None


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
    pat_state = _peek(providers.pat_state, None)
    return Sense(
        doa_angle=base.doa_angle,
        speech_detected=base.speech_detected,
        doa_age_s=base.doa_age_s,
        rms=_peek(providers.rms, None),
        rms_ratio=_peek(providers.rms_ratio, None),
        pat_event=_peek(providers.pat_event, None),
        face=_peek(providers.face, None),
        face_bbox=_coerce_bbox(_peek(providers.face_bbox, None)),
        face_age_s=_coerce_float(_peek(providers.face_age_s, None)),
        frame_available=bool(_peek(providers.frame_available, False)),
        pat_state=pat_state if isinstance(pat_state, PatState) else UNAVAILABLE_PAT_STATE,
        transcript=_peek(providers.transcript, None),
        self_moving=bool(_peek(providers.self_moving, False)),
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
