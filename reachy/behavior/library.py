"""The built-in behavior library — named, parametric, pure motion generators.

Each :class:`LibraryEntry` is data (which channels it claims, its default
contention class, its natural lifetime, a typed parameter schema) plus a
``fn(t_local, params, sense) -> Contribution``. ``behavior list`` renders the
registry without instantiating anything; the engine calls :func:`build` to turn a
resolved (name, params, class, lifetime) into a live :class:`Behavior`.

Almost every generator is a *pure, continuous* function of behavior-local time —
smooth trig, no randomness, ignoring ``sense`` — because the engine streams
immediate ``set_target`` poses at 50 Hz with no daemon-side interpolation. (This
is why the idle ``feel-alive`` layer here is a fresh continuous formulation rather
than ``alive.next_pose``, which re-samples a random gaze target per call and is
built for the slower, ``goto``-interpolated demo-mode loop.)

The one exception is ``listen``: a *sensor-driven* entry (``wants_sense=True``)
whose ``make_fn`` builds a fresh stateful closure per behavior — it reads the
sound Direction of Arrival from ``sense`` and slews the head toward it, abstaining
(returning ``None`` channels, so ``feel-alive`` shows through) when there is no
sound to react to.

Units are the CLI's friendly ones: millimetres, degrees, seconds.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Callable

from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head
from reachy.behavior.sense import Sense, doa_angle_to_yaw
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
    # Most entries claim a fixed ``channels`` set; an entry whose claim depends on
    # its params (e.g. ``listen`` claims ``body_yaw`` only when ``body_gain>0``)
    # supplies ``channels_fn`` so a channel it will never drive does not make it an
    # eviction target on that channel.
    channels_fn: Callable[[dict], frozenset[str]] | None = field(
        default=None, compare=False, repr=False
    )

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

    def claimed_channels(self, params: dict[str, float]) -> frozenset[str]:
        """The channels this instance claims (dynamic when ``channels_fn`` is set)."""
        if self.channels_fn is not None:
            return self.channels_fn(params)
        return self.channels


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


def _feel_alive(t: float, p: dict, _sense: Sense) -> Contribution:
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


def _gaze_hold(t: float, p: dict, _sense: Sense) -> Contribution:
    """Hold a fixed head offset (the 'look up-and-aside, hold N seconds' case)."""
    return Contribution(head=_head(yaw=p["yaw"], pitch=p["pitch"], roll=p["roll"], z=p["z"]))


def _nod(t: float, p: dict, _sense: Sense) -> Contribution:
    return Contribution(head=_head(pitch=p["amp"] * _sin_at(t, p["period"])))


def _shake(t: float, p: dict, _sense: Sense) -> Contribution:
    return Contribution(head=_head(yaw=p["amp"] * _sin_at(t, p["period"])))


def _speak(t: float, p: dict, _sense: Sense) -> Contribution:
    """Speech-like head bob: a quick pitch oscillation with a smaller offset yaw."""
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


def _make_listen() -> ContribFn:
    """Build a fresh sound-orienting closure: slew head (and optionally body) toward DoA.

    Unlike the pure generators above this is *stateful* — it holds the current
    head/body yaw and the last tick time so it can rate-limit (ease, not snap) the
    slew toward the sound direction. When there is no usable reading — no mic,
    daemon error, or (with ``speech_only``) no speech — it **abstains** by returning
    ``None`` channels, so the passive ``feel-alive`` base layer keeps the head and
    body alive rather than freezing them at the last orientation.
    """
    state = {"yaw_head": 0.0, "yaw_body": 0.0, "last_t": None}

    def _listen(t: float, p: dict, sense: Sense) -> Contribution:
        speech_only = p["speech_only"] >= 0.5
        angle = sense.doa_angle
        signal = angle is not None and (not speech_only or sense.speech_detected)
        last = state["last_t"]
        dt = 0.0 if last is None else max(0.0, t - last)
        state["last_t"] = t
        # Exponential ease. dt==0 (first active tick) -> alpha 0, so it eases up
        # from where it is rather than snapping; smooth<=0 -> snap.
        alpha = 1.0 - math.exp(-dt / p["smooth"]) if p["smooth"] > 0 else 1.0
        if not signal:
            # Nothing to orient to: abstain (return None channels) so feel-alive
            # shows through. Ease the internal slew state back toward center so a
            # re-acquired sound starts near where the head actually is, not from a
            # stale target -> no jump on takeover.
            state["yaw_head"] += (0.0 - state["yaw_head"]) * alpha
            state["yaw_body"] += (0.0 - state["yaw_body"]) * alpha
            return Contribution(head=None, body_yaw=None)
        target_head = _clamp(doa_angle_to_yaw(angle, p["gain"]), p["max_yaw"])
        state["yaw_head"] += (target_head - state["yaw_head"]) * alpha
        body_yaw = None
        if p["body_gain"] > 0:
            target_body = _clamp(doa_angle_to_yaw(angle, p["body_gain"]), p["body_max"])
            state["yaw_body"] += (target_body - state["yaw_body"]) * alpha
            body_yaw = state["yaw_body"]
        return Contribution(head=_head(yaw=state["yaw_head"]), body_yaw=body_yaw)

    return _listen


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
    "listen": LibraryEntry(
        name="listen",
        summary="turn the head/body toward the direction of arrival of sound (DoA)",
        channels=frozenset({"head", "body_yaw"}),
        default_class=StopClass.STOPPABLE,
        looping=True,
        default_duration=None,
        params={
            "gain": Param(0.6, "x", "head-yaw gain per acoustic angle"),
            "max_yaw": Param(35.0, "deg", "max head yaw toward sound"),
            "smooth": Param(0.35, "s", "slew ease time constant (smaller = snappier)"),
            "speech_only": Param(0.0, "", "react only to speech (1) vs any sound (0)"),
            "body_gain": Param(0.0, "x", "body-yaw gain per acoustic angle (0 = head only)"),
            "body_max": Param(45.0, "deg", "max body yaw toward sound"),
        },
        make_fn=_make_listen,
        wants_sense=True,
        # Only contend for body_yaw when actually turning the body, so a head-only
        # listen is not an eviction target for a body-yaw 'stopping' behavior.
        channels_fn=lambda p: (
            frozenset({"head", "body_yaw"}) if p["body_gain"] > 0 else frozenset({"head"})
        ),
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
        channels=entry.claimed_channels(params),
        stop_class=stop_class,
        lifetime=lifetime,
        params=dict(params),
        fn=entry.build_fn(),
        wants_sense=entry.wants_sense,
    )
