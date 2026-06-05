"""The built-in behavior library — named, parametric, pure motion generators.

Each :class:`LibraryEntry` is data (which channels it claims, its default
contention class, its natural lifetime, a typed parameter schema) plus a pure
``fn(t_local, params) -> Contribution``. ``behavior list`` renders the registry
without instantiating anything; the engine calls :func:`build` to turn a
resolved (name, params, class, lifetime) into a live :class:`Behavior`.

Every generator is a *continuous* function of behavior-local time — smooth trig,
no randomness — because the engine streams immediate ``set_target`` poses at
50 Hz with no daemon-side interpolation. (This is why the idle ``feel-alive``
layer here is a fresh continuous formulation rather than ``alive.next_pose``,
which re-samples a random gaze target per call and is built for the slower,
``goto``-interpolated demo-mode loop.)

Units are the CLI's friendly ones: millimetres, degrees, seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head
from reachy.cli._errors import EXIT_USER_ERROR, CliError

ContribFn = Callable[[float, dict], Contribution]


@dataclass(frozen=True)
class Param:
    """One tunable knob of a behavior: its default value, unit, and help text."""

    default: float
    unit: str
    help: str


@dataclass(frozen=True)
class LibraryEntry:
    """A named behavior template: metadata + a pure contribution function."""

    name: str
    summary: str
    channels: frozenset[str]
    default_class: StopClass
    looping: bool
    default_duration: float | None
    params: dict[str, Param]
    fn: ContribFn = field(compare=False, repr=False)

    def default_params(self) -> dict[str, float]:
        return {k: p.default for k, p in self.params.items()}


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


def _feel_alive(t: float, p: dict) -> Contribution:
    """Continuous idle motion: breathing + slow organic gaze wander + antenna sway."""
    e = p["energy"]
    phase = 2.0 * math.pi * t / p["breathe_period"] if p["breathe_period"] else 0.0
    z = p["breathe_z"] * e * math.sin(phase)
    breathe_pitch = p["breathe_pitch"] * e * math.sin(phase)
    # Sum two slow incommensurate sines -> a smooth, non-repeating wander.
    yaw = e * p["gaze_yaw"] * (0.6 * math.sin(0.13 * t) + 0.4 * math.sin(0.37 * t + 1.3))
    gaze_pitch = e * p["gaze_pitch"] * (0.6 * math.sin(0.11 * t + 0.7) + 0.4 * math.sin(0.29 * t))
    sway = p["antenna"] * e * _sin_at(t, p["antenna_period"])
    body_yaw = e * p["body_yaw"] * math.sin(0.07 * t + 0.5)
    return Contribution(
        head=_head(z=z, pitch=breathe_pitch + gaze_pitch, yaw=yaw),
        antennas=(sway, -sway),
        body_yaw=body_yaw,
    )


def _gaze_hold(t: float, p: dict) -> Contribution:
    """Hold a fixed head offset (the 'look up-and-aside, hold N seconds' case)."""
    return Contribution(head=_head(yaw=p["yaw"], pitch=p["pitch"], roll=p["roll"], z=p["z"]))


def _nod(t: float, p: dict) -> Contribution:
    return Contribution(head=_head(pitch=p["amp"] * _sin_at(t, p["period"])))


def _shake(t: float, p: dict) -> Contribution:
    return Contribution(head=_head(yaw=p["amp"] * _sin_at(t, p["period"])))


def _speak(t: float, p: dict) -> Contribution:
    """Speech-like head bob: a quick pitch oscillation with a smaller offset yaw."""
    ph = 2.0 * math.pi * t / p["period"] if p["period"] else 0.0
    return Contribution(
        head=_head(pitch=p["pitch"] * math.sin(ph), yaw=p["yaw"] * math.sin(ph * 1.7 + 0.5))
    )


def _thoughtful(t: float, p: dict) -> Contribution:
    """Ease into a tilted, gazing-aside hold (a 'thinking' gesture)."""
    k = _smoothstep(t / p["rise"]) if p["rise"] else 1.0
    return Contribution(head=_head(pitch=p["pitch"] * k, yaw=p["yaw"] * k, roll=p["roll"] * k))


def _antenna_sway(t: float, p: dict) -> Contribution:
    sway = p["amp"] * _sin_at(t, p["period"])
    return Contribution(antennas=(sway, -sway))


def _body_turn_hold(t: float, p: dict) -> Contribution:
    k = _smoothstep(t / p["rise"]) if p["rise"] else 1.0
    return Contribution(body_yaw=p["yaw"] * k)


# --------------------------------------------------------------------------- #
# The registry                                                                #
# --------------------------------------------------------------------------- #

_HEAD = frozenset({"head"})
_ANTENNAS = frozenset({"antennas"})
_BODY = frozenset({"body_yaw"})

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
        fn=_feel_alive,
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
        summary="bob the head like speech (for N seconds or until stopped)",
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
        fn=entry.fn,
    )
