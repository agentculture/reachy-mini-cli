"""Gaze behaviors — a one-shot reflex that snaps the head toward live sound.

:mod:`reachy.behavior.orient` already turns a live DoA bearing into a
SUSTAINED gaze goal (``orient-to-sound``, admitted as a standing
``declare_goal``). This module is the smaller, DIFFERENT thing: a single quick
glance, admitted once via ``run_behavior`` and gone in ``default_duration``
seconds — "look at whatever made that sound just now", not "keep watching
sound forever". It shares :func:`reachy.behavior.sense.doa_angle_to_yaw` and
``orient``'s own ``max_yaw`` clamp with the sustained behavior, but keeps its
own tiny geometry (ease-then-hold, no ladder, no dwell/latch gating) because a
one-shot reflex has none of ``orient``'s escalation state to carry.

Named ``look-at-sound`` — NOT ``look-toward-sound`` — on purpose: a react rule
id ``look-toward-sound`` already exists in ``default_rules.toml`` (it admits
``orient-to-sound``), and a same-named BEHAVIOR would only be confusingly
close, never actually colliding (rule ids and library names are different
namespaces) — the distinct name removes the ambiguity outright.

Refusal happens at ADMISSION time, in
:meth:`reachy.behavior.intents.IntentDriver._apply_run_behavior`, which is why
this module exposes a pure planning function (:func:`plan_look_at_sound`) the
driver calls with that tick's live :class:`~reachy.behavior.sense.Sense`
rather than folding the check into the (necessarily pure, time-only)
contribution function itself.

StopClass — why ``STOPPABLE``, not ``STOPPING``
=================================================
The one-shot must be able to take the head from an active ``feel-alive``
(``PASSIVE``) and from an active ``orient-to-sound`` (``STOPPABLE``).
Against ``PASSIVE`` this is automatic: arbitration
(:func:`reachy.behavior.arbitration.arbitrate`) gives a channel to the
highest-``priority`` claimant every tick, and ``PASSIVE`` is the lowest
priority there is — any non-passive newcomer wins the channel without needing
to evict anything.

Against ``orient-to-sound`` (also ``STOPPABLE``) the two are priority-tied, so
arbitration falls to its documented tie-break — "most recently admitted"
wins (:func:`arbitrate`'s ``max(..., key=lambda ib: (priority, index))``,
where a newer behavior has the larger index). Since ``look-at-sound`` is by
definition the just-admitted behavior, it wins the head immediately WITHOUT
evicting ``orient-to-sound`` — which is the point: ``orient-to-sound`` stays
active underneath, unaware and un-evicted, and reclaims the head on its own
the moment the one-shot's short lifetime ends (its contribution abstains,
:class:`~reachy.behavior.model.Contribution`'s ``done=True``, and it drops
out of the active set). Reaching for ``StopClass.STOPPING`` instead would
EVICT ``orient-to-sound`` outright (:func:`reachy.behavior.arbitration.admit`
only removes ``STOPPABLE`` incumbents for a ``STOPPING`` newcomer) — killing
the standing goal for one quick glance, which a one-shot reflex has no
business doing. ``STOPPABLE`` is also the same class ``gaze-hold`` and
``pet-reaction`` already use for exactly this "brief, polite, self-ending"
shape (see :mod:`reachy.behavior.library`).
"""

from __future__ import annotations

import math

from reachy.behavior import face_lock as face_lock_mod
from reachy.behavior.model import Contribution, neutral_head
from reachy.behavior.orient import OrientParams
from reachy.behavior.sense import Sense, doa_angle_to_yaw

#: The library name. Distinct from the react rule id ``look-toward-sound``
#: (``reachy/behavior/default_rules.toml``) — see the module docstring.
NAME = "look-at-sound"

#: A DoA reading older than this (or altogether absent) refuses admission.
DEFAULT_MAX_AGE_S = 8.0

#: The one-shot's total lifetime: ease onto the bearing, hold, then end.
DEFAULT_DURATION_S = 2.0

#: How long the ease-in ramp takes; the remainder of the duration holds still.
DEFAULT_EASE_S = 0.6

#: Reuse `orient.py`'s own head-yaw clamp so a quick glance and the sustained
#: gaze goal never disagree about how far the head is allowed to turn.
DEFAULT_MAX_YAW = OrientParams().max_yaw

#: The one refusal reason `run_behavior` returns for `look-at-sound` — a
#: missing reading and a stale one collapse to the SAME wire string, because a
#: caller cannot act differently on the distinction (both mean "there is
#: nothing live enough to look at").
NO_RECENT_SOUND = "no recent sound direction"


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


#: The library name for the face one-shot. Distinct from ``face-lock``
#: (:mod:`reachy.behavior.face_lock`), the LOOPING behavior ``lock_face``
#: admits — this is the same "one quick glance, then gone" shape as
#: ``look-at-sound``, aimed at a face instead of a sound.
NAME_FACE = "look-at-face"

#: A face reading older than this (or altogether absent) refuses admission.
#: Equal to :data:`reachy.behavior.face_lock.MAX_FACE_AGE_S` on purpose — both
#: modules agree with the SAME producer TTL (``reachy.behavior.face_sense``).
DEFAULT_MAX_AGE_S_FACE = face_lock_mod.MAX_FACE_AGE_S

#: Reuse `face_lock.py`'s own head clamps so a quick glance at a face and the
#: continuous face-lock never disagree about how far the head is allowed to
#: turn/tilt.
DEFAULT_MAX_YAW_FACE = face_lock_mod.MAX_YAW_DEG
DEFAULT_MAX_PITCH_FACE = face_lock_mod.MAX_PITCH_DEG

#: The one-shot's total lifetime: ease onto the bbox-centre target, hold, end.
DEFAULT_DURATION_S_FACE = 2.0

#: The refusal reason for ``look-at-face`` — the SAME wire string
#: :mod:`reachy.behavior.face_lock` uses for ``lock_face``'s own refusal
#: (both mean "there is no fresh face to aim at"), even though the two
#: refusal paths are otherwise independent (this module never imports
#: face_lock's driver, only its pure clamp/gain constants above).
NO_FACE_KNOWN = "no face known"


def _bbox_centre(bbox: object) -> tuple[float, float] | None:
    """The ``(cx, cy)`` centre of a normalised ``(x, y, w, h)`` bbox, or ``None``.

    A local copy of :mod:`reachy.behavior.face_lock`'s private helper of the
    same name — lifted rather than imported because that helper is private to
    its module (not in ``face_lock.__all__``) and this module must not reach
    into a sibling task's private symbol. Kept in sync by both modules sharing
    the same bbox contract (:attr:`reachy.behavior.sense.Sense.face_bbox`).
    """
    if not isinstance(bbox, (tuple, list)) or len(bbox) != 4:
        return None
    try:
        x, y, w, h = (float(v) for v in bbox)
    except (TypeError, ValueError):
        return None
    if not all(math.isfinite(v) for v in (x, y, w, h)):
        return None
    return (x + w / 2.0, y + h / 2.0)


def plan_look_at_face(
    sense: Sense,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S_FACE,
    max_yaw: float = DEFAULT_MAX_YAW_FACE,
    max_pitch: float = DEFAULT_MAX_PITCH_FACE,
) -> tuple[float, float] | None:
    """The clamped ``(yaw, pitch)`` head target (degrees) for a fresh face bbox.

    ``None`` is the refusal signal — no bbox (``sense.face_bbox is None``), or
    an age that cannot certify freshness: absent (``sense.face_age_s is
    None``, i.e. never polled — mirrors :func:`plan_look_at_sound`'s stance on
    a missing DoA age) or older than ``max_age_s``. A fresh bbox maps through
    the SAME bbox-centre -> yaw/pitch convention as
    :class:`reachy.behavior.face_lock.FaceLockGaze` (``+yaw`` left, ``+pitch``
    up; gain equal to the clamp, so the mapping is linear across the frame and
    only saturates at the edge) and clamps to ``max_yaw``/``max_pitch``.
    """
    centre = _bbox_centre(sense.face_bbox)
    if centre is None:
        return None
    if sense.face_age_s is None or sense.face_age_s > max_age_s:
        return None
    cx, cy = centre
    yaw = _clamp(-(cx - 0.5) * 2.0 * face_lock_mod.YAW_GAIN_DEG, max_yaw)
    pitch = _clamp(-(cy - 0.5) * 2.0 * face_lock_mod.PITCH_GAIN_DEG, max_pitch)
    return (yaw, pitch)


def plan_look_at_sound(
    sense: Sense,
    *,
    max_age_s: float = DEFAULT_MAX_AGE_S,
    max_yaw: float = DEFAULT_MAX_YAW,
) -> float | None:
    """The clamped head-yaw target (degrees) for a fresh reading, else ``None``.

    ``None`` is the refusal signal — no usable bearing (``sense.doa_angle is
    None``) or one older than ``max_age_s`` (``sense.doa_age_s is None``, i.e.
    never polled, counts as unusable too). A fresh bearing maps through
    :func:`~reachy.behavior.sense.doa_angle_to_yaw` at unit gain (a one-shot
    glance does not need `orient`'s escalating gain) and clamps to
    ``max_yaw``, mirroring `orient.py`'s own clamp.
    """
    if sense.doa_angle is None:
        return None
    if sense.doa_age_s is None or sense.doa_age_s > max_age_s:
        return None
    return _clamp(doa_angle_to_yaw(sense.doa_angle, 1.0), max_yaw)


def _smoothstep(x: float) -> float:
    """Clamp ``x`` to ``[0, 1]`` then ease (3x^2 - 2x^3) — a soft ramp.

    A small local copy of `library.py`'s own helper (private there, and this
    module must not import a private name from a module that in turn imports
    this one — see the library entry wiring in `library.py`).
    """
    x = max(0.0, min(1.0, x))
    return x * x * (3.0 - 2.0 * x)


def look_at_sound_fn(t: float, p: dict, _sense: Sense) -> Contribution:
    """The library ``fn``: ease onto ``p["yaw"]`` over ``p["ease_s"]``, then hold.

    ``yaw`` is baked into *p* once at admission (by
    :meth:`reachy.behavior.intents.IntentDriver._apply_run_behavior`, from
    :func:`plan_look_at_sound`'s clamped result) — this function stays PURE
    and time-only like every other library entry (``_gaze_hold``, ``_nod``,
    ...): it never re-reads ``sense``, so a one-shot glance holds its target
    steady for its whole lifetime even if the live bearing keeps moving.
    """
    ease_s = p.get("ease_s", DEFAULT_EASE_S)
    frac = 1.0 if ease_s <= 0 else min(1.0, t / ease_s)
    head = neutral_head()
    head["yaw"] = p.get("yaw", 0.0) * _smoothstep(frac)
    head["pitch"] = p.get("pitch", 0.0)
    head["roll"] = p.get("roll", 0.0)
    head["z"] = p.get("z", 0.0)
    return Contribution(head=head)


def look_at_face_fn(t: float, p: dict, _sense: Sense) -> Contribution:
    """The library ``fn``: ease onto ``p["yaw"]``/``p["pitch"]`` over
    ``p["ease_s"]``, then hold.

    Both ``yaw`` and ``pitch`` are baked into *p* once at admission (by
    :meth:`reachy.behavior.intents.IntentDriver._apply_run_behavior`, from
    :func:`plan_look_at_face`'s clamped result) — unlike ``look_at_sound_fn``
    (which only eases yaw; DoA carries no pitch), a face bbox centre supplies
    BOTH axes, so both ease together. This function stays PURE and time-only:
    it never re-reads ``sense``, so a one-shot glance holds its target steady
    for its whole lifetime even if the live bbox keeps moving.
    """
    ease_s = p.get("ease_s", DEFAULT_EASE_S)
    frac = 1.0 if ease_s <= 0 else min(1.0, t / ease_s)
    eased = _smoothstep(frac)
    head = neutral_head()
    head["yaw"] = p.get("yaw", 0.0) * eased
    head["pitch"] = p.get("pitch", 0.0) * eased
    head["roll"] = p.get("roll", 0.0)
    head["z"] = p.get("z", 0.0)
    return Contribution(head=head)
