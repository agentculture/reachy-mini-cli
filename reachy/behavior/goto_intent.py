"""The ``goto`` intent kind — a fail-closed :class:`~reachy.behavior.control.KindRegistry`
handler that turns an externally-submitted command into a :class:`
~reachy.behavior.goto_lane.GotoSpec` and hands it to an injected
:class:`~reachy.behavior.goto_lane.GotoLane`.

Why this module exists, and why it is strict
----------------------------------------------
:mod:`reachy.behavior.intents` documents (in its ``IntentDriver`` docstring) the
``registry`` extension point a *future* goto kind would use: build a
``KindRegistry``, register a handler for a new ``op``, hand it to whichever
driver(s) share it — never edit :mod:`reachy.behavior.control` or
:mod:`reachy.behavior.intents` to add one. This module is that goto kind, made
real.

The command that reaches a handler here did NOT come from a trusted, reviewed
call site. It came from :mod:`reachy.behavior.control`'s namespaced spool — a
plain directory of JSON files any agent, script, or bug can write into. A hard
scope-level finding (recorded when this task was framed) established there is
**no clamping anywhere on the streaming path**: :meth:`Engine.compose_tick`
(:mod:`reachy.behavior.engine`) composes each active behavior's contribution
*raw* and the transport unit-converts and POSTs it unchanged; only individual
library behaviors self-clamp their own amplitudes (see
:mod:`reachy.behavior.library`'s ``Param`` defaults, and its (currently
unused) ``_clamp`` helper — a goto has no such self-discipline, because a goto's
whole point is to *reach a caller-chosen target*, not oscillate around one.
:func:`~reachy.behavior.goto_lane.build_goto_behavior` will happily interpolate
toward whatever target a :class:`~reachy.behavior.goto_lane.GotoSpec` names — a
wild target from a buggy agent must never reach that interpolation, let alone
stream to the daemon.

So every validation in this module is **fail-closed**: an unknown field, an
out-of-range axis, a non-numeric value, or a runaway duration is *refused*
outright (never silently clamped to the nearest legal value — a silent clamp
would hide the bug that produced the wild value instead of surfacing it) and
:func:`~reachy.behavior.goto_lane.GotoLane.submit` is never called. Bounds are
module constants; each one is commented with the precedent it mirrors —
:mod:`reachy.behavior.library`'s per-behavior amplitude ``Param`` defaults (the
largest value any *shipped* behavior actually drives that channel to) and
:mod:`reachy.speech.expressions`' TOML catalog's own "AMPLITUDE GUIDE" comment
(the hand-tuned "typical safe range" per axis for a deliberate, targeted pose) —
taking the larger of the two per axis, so this gate never rejects a target a
shipped behavior already drives the robot to, while still refusing anything
beyond established precedent.

Contract with the (not-yet-built) composition layer
------------------------------------------------------
:func:`make_goto_handler` returns a ``handler(payload, ctx) -> dict`` shaped
EXACTLY like every handler :mod:`reachy.behavior.intents` registers today (see
its ``_apply_run_behavior`` / ``_apply_declare_goal`` / ...): ``payload`` is the
FULL drained command dict (``cmd_id`` / ``op`` / the kind's own fields — the
same object :class:`~reachy.behavior.control.KindRegistry.dispatch` hands every
handler, not just the kind-specific subset), ``ctx`` is accepted but unused (a
goto's admission goes through the injected :class:`GotoLane`, not
``ctx.admit``). A validation failure ``raise``\\ s :class:`
~reachy.cli._errors.CliError` — never returns a bespoke error shape — so
:meth:`KindRegistry.dispatch` catches it exactly the way it already catches
every other kind's :class:`CliError`, and :meth:`IntentDriver._observe`'s
``senselog``/``ctx.emit`` wiring (``intent.applied`` / ``intent.blocked``) keeps
working unchanged once this handler is registered at composition (a later
task — this module registers nothing itself).

Import boundary
-----------------
This module must never import :mod:`reachy.behavior.control` or
:mod:`reachy.behavior.intents` — registering the ``GOTO`` kind into a live
:class:`~reachy.behavior.control.KindRegistry` is composition's job, not this
leaf's. It DOES import :mod:`reachy.behavior.goto_lane` (for
:class:`~reachy.behavior.goto_lane.GotoSpec`, the value type it builds and
submits) and :mod:`reachy.cli._errors` (the shared error type every kind
handler in this codebase raises) — neither is control.py or intents.py.
``tests/test_behavior_goto_intent.py`` asserts the boundary holds.
"""

from __future__ import annotations

from typing import Callable

from reachy.behavior.goto_lane import GotoSpec
from reachy.cli._errors import EXIT_USER_ERROR, CliError

#: The command kind (``op`` field) this module's handler answers to — follows
#: :mod:`reachy.behavior.intents`'s bare-verb naming (``run_behavior``,
#: ``declare_goal``, ...).
GOTO = "goto"

# --------------------------------------------------------------------------- #
# Payload shape — the GotoSpec-shaped whitelist                               #
# --------------------------------------------------------------------------- #

#: The head axes a goto may target, in :func:`reachy.behavior.model.neutral_head`'s
#: order — mirrors (not imports) :mod:`reachy.behavior.goto_lane`'s private
#: ``_HEAD_KEYS`` so this module has no dependency on that name staying stable.
HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

#: Top-level fields a goto command may carry — exactly :class:`GotoSpec`'s own
#: fields (``label`` / ``head`` / ``antennas`` / ``body_yaw`` / ``duration`` /
#: ``interpolation``). Nothing else is invented here.
_ALLOWED_FIELDS = frozenset({"label", "head", "antennas", "body_yaw", "duration", "interpolation"})

#: Fields every drained command carries regardless of kind (the spool envelope —
#: see :func:`reachy.behavior.control.submit`), never part of the GotoSpec
#: whitelist but never rejected as "unknown" either.
_ENVELOPE_FIELDS = frozenset({"cmd_id", "op"})

# --------------------------------------------------------------------------- #
# Boundary constants — cited precedent per axis                               #
# --------------------------------------------------------------------------- #

#: A goto's duration must be > 0 and at most this many seconds. There is no
#: "amplitude" precedent for time, so this is a plain, deliberately small,
#: sanity ceiling: long enough for any deliberate expressive move, short enough
#: that a runaway/duplicated goto command can never park the robot mid-motion
#: for an unbounded time.
MAX_DURATION_S = 10.0

#: Head translation (mm). ``reachy.speech.expressions`` TOML's AMPLITUDE GUIDE:
#: "head_x / head_y: ±5 mm". :mod:`reachy.behavior.library` drives no head_x/
#: head_y offset at all (no precedent to raise it), so the guide's figure wins.
HEAD_X_LIMIT_MM = 5.0
HEAD_Y_LIMIT_MM = 5.0

#: Head height (mm). max(library's ``feel-alive`` ``breathe_z`` default = 3.0,
#: expressions.toml guide "head_z: ±8 mm") = 8.0.
HEAD_Z_LIMIT_MM = 8.0

#: Head roll (deg). max(library's ``thoughtful`` ``roll`` default = 5.0,
#: expressions.toml guide "head_roll: ±10 deg") = 10.0.
HEAD_ROLL_LIMIT_DEG = 10.0

#: Head pitch (deg). max(library's ``nod`` ``amp`` default = 12.0,
#: expressions.toml guide "head_pitch: ±12 deg") = 12.0 (an exact match).
HEAD_PITCH_LIMIT_DEG = 12.0

#: Head yaw (deg). max(library's ``gaze-hold`` ``yaw`` default = 18.0,
#: expressions.toml guide "head_yaw: ±20 deg") = 20.0.
HEAD_YAW_LIMIT_DEG = 20.0

#: Antennas, each side (deg). max(library's ``antenna-sway`` ``amp`` default =
#: 18.0, expressions.toml guide "antennas: ±45 deg") = 45.0 — also comfortably
#: covers the highest antenna value any shipped expression entry actually uses
#: (the 🎉 entry's ±40 deg).
ANTENNA_LIMIT_DEG = 45.0

#: Body yaw (deg). max(library's ``body-turn-hold`` ``yaw`` default = 20.0,
#: expressions.toml guide "body_yaw: ±15 deg") = 20.0 — the library's own
#: shipped behavior is the binding precedent here, not the (lower) guide.
BODY_YAW_LIMIT_DEG = 20.0

#: axis -> (limit, unit label for error messages), keyed by :data:`HEAD_AXES`.
_HEAD_LIMITS: dict[str, tuple[float, str]] = {
    "x": (HEAD_X_LIMIT_MM, "mm"),
    "y": (HEAD_Y_LIMIT_MM, "mm"),
    "z": (HEAD_Z_LIMIT_MM, "mm"),
    "roll": (HEAD_ROLL_LIMIT_DEG, "deg"),
    "pitch": (HEAD_PITCH_LIMIT_DEG, "deg"),
    "yaw": (HEAD_YAW_LIMIT_DEG, "deg"),
}


# --------------------------------------------------------------------------- #
# Validation helpers — every one raises CliError, never clamps                #
# --------------------------------------------------------------------------- #


def _reject(message: str, remediation: str = "") -> None:
    raise CliError(code=EXIT_USER_ERROR, message=f"goto: {message}", remediation=remediation)


def _as_number(value: object, label: str) -> float:
    """Coerce ``value`` to ``float``, refusing bools and any non-numeric type."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        _reject(f"{label} must be a number (got {value!r})")
    return float(value)  # type: ignore[arg-type]  # _reject always raises above


def _check_range(value: float, limit: float, label: str, unit: str) -> None:
    if value < -limit or value > limit:
        _reject(
            f"{label} out of range: {value!r} (allowed [-{limit}, {limit}] {unit})",
            remediation=f"submit a {label} within ±{limit} {unit}",
        )


def _reject_unknown_fields(payload: dict) -> None:
    unknown = sorted(set(payload) - _ALLOWED_FIELDS - _ENVELOPE_FIELDS)
    if unknown:
        _reject(
            f"unknown field(s) {unknown} (allowed: {sorted(_ALLOWED_FIELDS)})",
            remediation="drop the unknown field(s) and resubmit",
        )


def _validate_head(raw: object) -> dict[str, float] | None:
    if raw is None:
        return None
    if not isinstance(raw, dict):
        _reject(f"'head' must be an object of axis:value pairs (got {raw!r})")
    unknown_axes = sorted(set(raw) - set(HEAD_AXES))  # type: ignore[arg-type]
    if unknown_axes:
        _reject(
            f"unknown head axis/axes {unknown_axes} (allowed: {list(HEAD_AXES)})",
            remediation="use only the six head axes: x, y, z, roll, pitch, yaw",
        )
    result: dict[str, float] = {}
    for axis, raw_value in raw.items():  # type: ignore[union-attr]
        label = f"head.{axis}"
        value = _as_number(raw_value, label)
        limit, unit = _HEAD_LIMITS[axis]
        _check_range(value, limit, label, unit)
        result[axis] = value
    return result


def _validate_antennas(raw: object) -> tuple[float, float] | None:
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, (list, tuple)) or len(raw) != 2:
        _reject(f"'antennas' must be a [right, left] pair (got {raw!r})")
    right = _as_number(raw[0], "antennas.right")  # type: ignore[index]
    left = _as_number(raw[1], "antennas.left")  # type: ignore[index]
    _check_range(right, ANTENNA_LIMIT_DEG, "antennas.right", "deg")
    _check_range(left, ANTENNA_LIMIT_DEG, "antennas.left", "deg")
    return (right, left)


def _validate_body_yaw(raw: object) -> float | None:
    if raw is None:
        return None
    value = _as_number(raw, "body_yaw")
    _check_range(value, BODY_YAW_LIMIT_DEG, "body_yaw", "deg")
    return value


def _validate_duration(raw: object) -> float:
    if raw is None:
        return GotoSpec.duration  # dataclass field default (1.0) when omitted
    value = _as_number(raw, "duration")
    if value <= 0:
        _reject(f"duration must be > 0 (got {value!r})")
    if value > MAX_DURATION_S:
        _reject(
            f"duration out of range: {value!r} (allowed (0, {MAX_DURATION_S}] s)",
            remediation=f"submit a duration of at most {MAX_DURATION_S} seconds",
        )
    return value


def _validate_interpolation(raw: object) -> str:
    if raw is None:
        return GotoSpec.interpolation  # dataclass field default ("minjerk")
    if not isinstance(raw, str):
        _reject(f"'interpolation' must be a string (got {raw!r})")
    return raw  # type: ignore[return-value]  # unknown modes fall back inside goto_lane


def _validate_label(raw: object) -> str:
    if raw is None:
        return GotoSpec.label  # dataclass field default ("goto")
    if not isinstance(raw, str):
        _reject(f"'label' must be a string (got {raw!r})")
    return raw  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# The handler factory                                                        #
# --------------------------------------------------------------------------- #


def make_goto_handler(lane) -> Callable[[dict, object], dict]:
    """Build a ``handler(payload, ctx) -> dict`` closing over *lane*.

    *lane* is duck-typed — it needs only ``submit(spec) -> str``, exactly
    :class:`~reachy.behavior.goto_lane.GotoLane` (or its ``GotoLaneAdapter``
    facade)'s surface, so tests inject a bare recording fake with no other
    engine machinery.

    Every payload field is validated (see the module docstring) BEFORE
    ``lane.submit`` is ever called — a rejected payload never reaches the lane,
    so an out-of-range or malformed goto is provably never queued, let alone
    streamed.
    """

    def handler(payload: dict, _ctx: object) -> dict:
        _reject_unknown_fields(payload)
        head = _validate_head(payload.get("head"))
        antennas = _validate_antennas(payload.get("antennas"))
        body_yaw = _validate_body_yaw(payload.get("body_yaw"))
        if head is None and antennas is None and body_yaw is None:
            _reject(
                "a goto must name at least one channel target (head / antennas / body_yaw)",
                remediation="include at least one of head/antennas/body_yaw",
            )
        duration = _validate_duration(payload.get("duration"))
        interpolation = _validate_interpolation(payload.get("interpolation"))
        label = _validate_label(payload.get("label"))
        spec = GotoSpec(
            label=label,
            head=head,
            antennas=antennas,
            body_yaw=body_yaw,
            duration=duration,
            interpolation=interpolation,
        )
        goto_id = lane.submit(spec)
        return {
            "ok": True,
            "op": GOTO,
            "id": goto_id,
            "label": spec.label,
            "channels": sorted(spec.channels()),
            "duration": spec.duration,
        }

    return handler
