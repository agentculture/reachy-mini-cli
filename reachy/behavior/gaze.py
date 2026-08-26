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
