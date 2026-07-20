"""The built-in behavior library — named, parametric, pure motion generators.

Each :class:`LibraryEntry` is data (which channels it claims, its default
contention class, its natural lifetime, a typed parameter schema) plus a
``fn(t_local, params, sense) -> Contribution``. ``behavior list`` renders the
registry without instantiating anything; the engine calls :func:`build` to turn a
resolved (name, params, class, lifetime) into a live :class:`Behavior`.

Most generators are *pure, continuous* functions of behavior-local time — smooth
trig, ignoring ``sense`` — because the engine streams immediate ``set_target``
poses at 50 Hz with no daemon-side interpolation. Stateful presence generators
live in focused leaf modules and are minted per behavior through ``make_fn``.

The engine feeds a *sensor-driven* entry (one with ``wants_sense=True``, built
per-instance via ``make_fn`` so it may hold state) a live :class:`Sense` reading.
Pure entries continue to receive an empty snapshot.

Units are the CLI's friendly ones: millimetres, degrees, seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from reachy.behavior.feel_alive import make_feel_alive
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head
from reachy.behavior.orient import OrientParams, make_orient_to_sound
from reachy.behavior.pet_reaction import (
    DONE_GESTURE_S,
    MAX_CONTACT_S,
    NOMINAL_TICK_S,
    make_pet_reaction,
)
from reachy.behavior.sense import Sense
from reachy.cli._errors import EXIT_USER_ERROR, CliError

ContribFn = Callable[[float, dict, Sense], Contribution]


@dataclass(frozen=True)
class Param:
    """One tunable knob of a behavior: its default value, unit, and help text."""

    default: float
    unit: str
    help: str


@dataclass(frozen=True)
class LibraryEntry:
    """A named behavior template: metadata + a contribution function.

    A pure entry supplies ``fn`` directly. A *sensor-driven* entry supplies
    ``make_fn`` (a zero-arg factory) instead, so :func:`build` mints a fresh
    stateful closure per behavior, and sets ``wants_sense`` so the engine feeds it
    a live :class:`Sense` (pure entries always get :data:`EMPTY_SENSE`).
    """

    name: str
    summary: str
    channels: frozenset[str]
    default_class: StopClass
    looping: bool
    default_duration: float | None
    params: dict[str, Param]
    fn: ContribFn | None = field(default=None, compare=False, repr=False)
    make_fn: Callable[[], ContribFn] | None = field(default=None, compare=False, repr=False)
    wants_sense: bool = False

    def default_params(self) -> dict[str, float]:
        return {k: p.default for k, p in self.params.items()}

    def build_fn(self) -> ContribFn:
        """The contribution function for one behavior instance (fresh if stateful)."""
        if self.make_fn is not None:
            return self.make_fn()
        if self.fn is None:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"behavior {self.name!r} has neither fn nor make_fn",
                remediation="this is a library bug — report it",
            )
        return self.fn


# --------------------------------------------------------------------------- #
# Small math helpers                                                          #
# --------------------------------------------------------------------------- #


def _head(**offsets: float) -> dict[str, float]:
    """A full six-axis head offset dict, unspecified axes zeroed."""
    head = neutral_head()
    head.update(offsets)
    return head


def _smoothstep(x: float) -> float:
    """Clamp ``x`` to ``[0, 1]`` then ease (3x^2 - 2x^3) — a soft ramp."""
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def _sin_at(t: float, period: float) -> float:
    return math.sin(2.0 * math.pi * t / period) if period else 0.0


# --------------------------------------------------------------------------- #
# Contribution functions                                                      #
# --------------------------------------------------------------------------- #


def _gaze_hold(t: float, p: dict, _sense: Sense) -> Contribution:
    """Hold a fixed head offset (the 'look up-and-aside, hold N seconds' case)."""
    return Contribution(head=_head(yaw=p["yaw"], pitch=p["pitch"], roll=p["roll"], z=p["z"]))


def _nod(t: float, p: dict, _sense: Sense) -> Contribution:
    return Contribution(head=_head(pitch=p["amp"] * _sin_at(t, p["period"])))


def _shake(t: float, p: dict, _sense: Sense) -> Contribution:
    return Contribution(head=_head(yaw=p["amp"] * _sin_at(t, p["period"])))


def _speak(t: float, p: dict, _sense: Sense) -> Contribution:
    """Speech-like head bob: a quick pitch oscillation with a smaller offset yaw.

    MOTION ONLY, and deliberately still so — the mouth-movement analogue, not a
    voice. The audible half of speech is a react rule's ``say`` text (see
    :mod:`reachy.behavior.rules`), rendered off the tick thread by
    :class:`reachy.behavior.speech_act.SpeechActuator`. The two were reconciled
    rather than merged: a library entry is a PURE, stateless function of
    behavior-local time evaluated at 50 Hz, so giving this one a side effect
    would put synthesis on the tick thread — exactly the defect the actuator
    exists to avoid. Pair them in ONE rule (``run = "speak"`` + ``say = "..."``)
    to get a robot that both looks and sounds like it is talking.
    """
    ph = 2.0 * math.pi * t / p["period"] if p["period"] else 0.0
    return Contribution(
        head=_head(pitch=p["pitch"] * math.sin(ph), yaw=p["yaw"] * math.sin(ph * 1.7 + 0.5))
    )


def _thoughtful(t: float, p: dict, _sense: Sense) -> Contribution:
    """Ease into a tilted, gazing-aside hold (a 'thinking' gesture)."""
    k = _smoothstep(t / p["rise"]) if p["rise"] else 1.0
    return Contribution(head=_head(pitch=p["pitch"] * k, yaw=p["yaw"] * k, roll=p["roll"] * k))


def _antenna_sway(t: float, p: dict, _sense: Sense) -> Contribution:
    sway = p["amp"] * _sin_at(t, p["period"])
    return Contribution(antennas=(sway, -sway))


def _body_turn_hold(t: float, p: dict, _sense: Sense) -> Contribution:
    k = _smoothstep(t / p["rise"]) if p["rise"] else 1.0
    return Contribution(body_yaw=p["yaw"] * k)


def _clamp(value: float, limit: float) -> float:
    """Clamp ``value`` to the symmetric range ``[-limit, limit]``."""
    return max(-limit, min(limit, value))


# --------------------------------------------------------------------------- #
# The registry                                                                #
# --------------------------------------------------------------------------- #

_HEAD = frozenset({"head"})
_ANTENNAS = frozenset({"antennas"})
_BODY = frozenset({"body_yaw"})

#: Defaults for the ``orient-to-sound`` knobs come from ONE place —
#: :class:`reachy.behavior.orient.OrientParams`, which itself tracks the donor
#: ``ListenParams``. Restating a number here would let the catalog and the
#: ladder drift apart silently, so every entry below reads it instead.
_ORIENT_DEFAULTS = OrientParams()


def _ORIENT_PARAM(name: str, unit: str, help: str) -> Param:  # noqa: N802 - table-local helper
    """A ``Param`` whose default is read off :class:`OrientParams`, never retyped."""
    return Param(getattr(_ORIENT_DEFAULTS, name), unit, help)


# The reaction normally self-completes no later than its internal contact limit
# plus one done gesture. Keep a two-tick margin so the engine can observe
# ``done=True`` before this independent finite lifetime releases the channels.
_PET_REACTION_BACKSTOP_S = MAX_CONTACT_S + DONE_GESTURE_S + 2.0 * NOMINAL_TICK_S

LIBRARY: dict[str, LibraryEntry] = {
    "feel-alive": LibraryEntry(
        name="feel-alive",
        summary="gentle continuous idle motion (breathing, slow gaze wander, antenna sway)",
        channels=frozenset({"head", "antennas", "body_yaw"}),
        default_class=StopClass.PASSIVE,
        looping=True,
        default_duration=None,
        params={
            "energy": Param(1.0, "x", "liveliness multiplier scaling every amplitude"),
            "breathe_period": Param(5.0, "s", "breathing cycle length"),
            "breathe_z": Param(3.0, "mm", "vertical breathing amplitude"),
            "breathe_pitch": Param(2.0, "deg", "pitch breathing amplitude"),
            "gaze_yaw": Param(12.0, "deg", "horizontal gaze wander amplitude"),
            "gaze_pitch": Param(7.0, "deg", "vertical gaze wander amplitude"),
            "antenna": Param(12.0, "deg", "antenna sway amplitude"),
            "antenna_period": Param(6.0, "s", "antenna sway cycle length"),
            "body_yaw": Param(6.0, "deg", "slow body-yaw wander amplitude"),
        },
        make_fn=make_feel_alive,
    ),
    "pet-reaction": LibraryEntry(
        name="pet-reaction",
        summary="settle into sustained petting, then signal release or enough",
        channels=frozenset({"head", "antennas", "body_yaw"}),
        default_class=StopClass.STOPPABLE,
        looping=False,
        default_duration=_PET_REACTION_BACKSTOP_S,
        params={},
        make_fn=make_pet_reaction,
        wants_sense=True,
    ),
    "orient-to-sound": LibraryEntry(
        name="orient-to-sound",
        summary=(
            "turn toward live sound — antennas lean, then a bounded head nudge, then a "
            "deliberate head+body turn on an addressed utterance"
        ),
        channels=frozenset({"head", "antennas", "body_yaw"}),
        default_class=StopClass.STOPPABLE,
        # Looping with NO default duration: orienting is a STANDING goal (the
        # `declare_goal` surface, which is exempt from the bounded-lifetime
        # invariant on purpose). A react rule or a `run_behavior` intent must
        # therefore bound it explicitly, exactly like `nod` / `feel-alive`.
        looping=True,
        default_duration=None,
        params={
            "gain": _ORIENT_PARAM(
                "gain", "x", "scales the ~+-90 deg acoustic span onto a yaw target"
            ),
            "max_yaw": _ORIENT_PARAM("max_yaw", "deg", "head yaw clamp"),
            "deadband": _ORIENT_PARAM(
                "deadband", "deg", "ignore sound within this of the current heading"
            ),
            "hold": _ORIENT_PARAM(
                "hold", "s", "after committing a turn, hold before reconsidering"
            ),
            "alert_speed": _ORIENT_PARAM("alert_speed", "deg/s", "turning toward a new sound"),
            "relax_speed": _ORIENT_PARAM("relax_speed", "deg/s", "easing back toward centre"),
            "min_dur": _ORIENT_PARAM("min_dur", "s", "duration floor, so turns stay deliberate"),
            "max_dur": _ORIENT_PARAM("max_dur", "s", "duration ceiling for one turn"),
            "antenna_gain": _ORIENT_PARAM("antenna_gain", "x", "scales the antenna lean"),
            "antenna_max": _ORIENT_PARAM(
                "antenna_max", "deg", "maximum near-side antenna deflection"
            ),
            "body_yaw_max": _ORIENT_PARAM("body_yaw_max", "deg", "body yaw clamp"),
            "body_speed": _ORIENT_PARAM("body_speed", "deg/s", "body rotation speed"),
            "head_only_band": _ORIENT_PARAM(
                "head_only_band", "deg", "beyond this the body turn escalates"
            ),
            "speech_orient_gain": _ORIENT_PARAM(
                "speech_orient_gain", "x", "fraction of the target the speech tier uses"
            ),
            "speech_orient_max": _ORIENT_PARAM(
                "speech_orient_max", "deg", "cap on the speech-tier head-only nudge"
            ),
            "engaged_min_dur": _ORIENT_PARAM(
                "engaged_min_dur", "s", "duration floor for the deliberate engaged turn"
            ),
            "recenter_after": _ORIENT_PARAM(
                "recenter_after", "s", "silence grace before the heading drifts home"
            ),
            "rms_floor": _ORIENT_PARAM(
                "rms_floor", "rms", "loudness floor corroborating that sound is live"
            ),
            "dwell_s": _ORIENT_PARAM(
                "dwell_s", "s", "the bearing must hold this long before the head moves"
            ),
            "dwell_tol_rad": _ORIENT_PARAM(
                "dwell_tol_rad", "rad", "how far the bearing may move and still count as held"
            ),
        },
        make_fn=make_orient_to_sound,
        wants_sense=True,
    ),
    "gaze-hold": LibraryEntry(
        name="gaze-hold",
        summary="look to a fixed head offset and hold it",
        channels=_HEAD,
        default_class=StopClass.STOPPABLE,
        looping=False,
        default_duration=5.0,
        params={
            "yaw": Param(18.0, "deg", "horizontal look angle"),
            "pitch": Param(10.0, "deg", "vertical look angle"),
            "roll": Param(0.0, "deg", "head roll"),
            "z": Param(0.0, "mm", "head height offset"),
        },
        fn=_gaze_hold,
    ),
    "nod": LibraryEntry(
        name="nod",
        summary="nod the head (pitch oscillation), 'yes'",
        channels=_HEAD,
        default_class=StopClass.STOPPABLE,
        looping=True,
        default_duration=None,
        params={
            "amp": Param(12.0, "deg", "nod amplitude"),
            "period": Param(0.8, "s", "nod cycle length"),
        },
        fn=_nod,
    ),
    "shake": LibraryEntry(
        name="shake",
        summary="shake the head (yaw oscillation), 'no'",
        channels=_HEAD,
        default_class=StopClass.STOPPABLE,
        looping=True,
        default_duration=None,
        params={
            "amp": Param(15.0, "deg", "shake amplitude"),
            "period": Param(0.7, "s", "shake cycle length"),
        },
        fn=_shake,
    ),
    "speak": LibraryEntry(
        name="speak",
        summary=(
            "bob the head like speech — motion only, no sound; pair with a rule's "
            'say = "..." for the voice (for N seconds or until stopped)'
        ),
        channels=_HEAD,
        default_class=StopClass.STOPPABLE,
        looping=True,
        default_duration=None,
        params={
            "pitch": Param(5.0, "deg", "vertical bob amplitude"),
            "yaw": Param(3.0, "deg", "horizontal bob amplitude"),
            "period": Param(0.32, "s", "bob cycle length"),
        },
        fn=_speak,
    ),
    "thoughtful": LibraryEntry(
        name="thoughtful",
        summary="ease into a tilted, gazing-aside 'thinking' hold",
        channels=_HEAD,
        default_class=StopClass.STOPPABLE,
        looping=False,
        default_duration=3.0,
        params={
            "pitch": Param(8.0, "deg", "upward/forward tilt"),
            "yaw": Param(10.0, "deg", "gaze-aside angle"),
            "roll": Param(5.0, "deg", "head roll"),
            "rise": Param(0.6, "s", "ease-in time"),
        },
        fn=_thoughtful,
    ),
    "antenna-sway": LibraryEntry(
        name="antenna-sway",
        summary="sway the antennas (pass --class stopping --channels to also seize body yaw)",
        channels=_ANTENNAS,
        default_class=StopClass.STOPPABLE,
        looping=True,
        default_duration=None,
        params={
            "amp": Param(18.0, "deg", "sway amplitude"),
            "period": Param(3.0, "s", "sway cycle length"),
        },
        fn=_antenna_sway,
    ),
    "body-turn-hold": LibraryEntry(
        name="body-turn-hold",
        summary="turn the body to a yaw angle and hold it",
        channels=_BODY,
        default_class=StopClass.STOPPABLE,
        looping=False,
        default_duration=5.0,
        params={
            "yaw": Param(20.0, "deg", "body yaw angle (+ left / - right)"),
            "rise": Param(0.5, "s", "ease-in time"),
        },
        fn=_body_turn_hold,
    ),
}


# --------------------------------------------------------------------------- #
# Resolution helpers (used by the CLI to validate, and the engine to build)   #
# --------------------------------------------------------------------------- #


def get(name: str) -> LibraryEntry:
    """Look up a library entry, raising a clean user error for an unknown name."""
    entry = LIBRARY.get(name)
    if entry is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown behavior {name!r}",
            remediation=f"list them with 'reachy behavior list' (have: {', '.join(LIBRARY)})",
        )
    return entry


def resolve_params(entry: LibraryEntry, overrides: dict[str, str] | None) -> dict[str, float]:
    """Merge ``key=value`` string overrides onto the entry defaults, validating.

    Unknown keys and non-numeric values raise a :class:`CliError`, so a bad
    ``--set`` is reported at submit time rather than silently inside the engine.
    """
    params = entry.default_params()
    for key, raw in (overrides or {}).items():
        if key not in entry.params:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{entry.name}: unknown parameter {key!r}",
                remediation=f"valid params: {', '.join(entry.params) or '(none)'}",
            )
        try:
            params[key] = float(raw)
        except ValueError as err:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"{entry.name}: parameter {key} must be a number (got {raw!r})",
                remediation="pass a numeric value, e.g. --set amp=20",
            ) from err
    return params


def resolve_class(entry: LibraryEntry, name: str | None) -> StopClass:
    """The contention class to use: an explicit name, else the entry's default."""
    if name is None:
        return entry.default_class
    try:
        return StopClass(name)
    except ValueError as err:
        choices = ", ".join(c.value for c in StopClass)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown contention class {name!r}",
            remediation=f"use one of: {choices}",
        ) from err


def resolve_lifetime(
    entry: LibraryEntry, *, once: bool, loop: bool, duration: float | None
) -> Lifetime:
    """Build the lifetime from ``--once``/``--loop``/``--duration`` and entry defaults."""
    if once and loop:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="--once and --loop are mutually exclusive",
            remediation="pass at most one",
        )
    if once:
        looping = False
    elif loop:
        looping = True
    else:
        looping = entry.looping
    dur = duration if duration is not None else entry.default_duration
    if not looping and dur is None:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{entry.name}: a one-shot run needs a duration",
            remediation="pass --duration SECONDS (or --loop to run until stopped)",
        )
    lifetime = Lifetime(looping=looping, duration=dur)
    problems = lifetime.errors()
    if problems:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{entry.name}: invalid lifetime: {'; '.join(problems)}",
            remediation="duration must be > 0",
        )
    return lifetime


def build(
    name: str,
    params: dict[str, float],
    stop_class: StopClass,
    lifetime: Lifetime,
    behavior_id: str,
) -> Behavior:
    """Construct a live :class:`Behavior` from a resolved spec (engine-side)."""
    entry = get(name)
    return Behavior(
        id=behavior_id,
        name=name,
        channels=entry.channels,
        stop_class=stop_class,
        lifetime=lifetime,
        params=dict(params),
        fn=entry.build_fn(),
        wants_sense=entry.wants_sense,
    )
