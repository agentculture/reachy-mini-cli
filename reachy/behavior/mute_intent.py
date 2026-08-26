"""The ``mute`` / ``unmute`` command kinds — the mind's quiet, reaching the body.

The mind can already choose to go quiet. Until t17 the BODY could not: a react
rule with a ``say`` fires its own voice through
:meth:`reachy.behavior.speech_act.SpeechActuator.say` whatever the mind is
doing, so a "be quiet for ten minutes" decision left the robot still
announcing every pat and every face it saw.

Two kinds close that, and only two: ``mute`` and ``unmute``. They are
deliberately NOT a duration ("mute for 30 s"): a timer inside the runtime would
be a second, drifting copy of the mind's own quiet window, and the moment the
two disagree the robot is either mute with nobody left to un-mute it or talking
through a quiet it was told to keep. The mind owns the clock; the body owns
only the gate.

Why not ``set_inhibition("speak")``
-----------------------------------
Because ``say`` does not go through the ``speak`` library behavior at all. A
firing rule takes ``rule_engine.RuleEngine._speak`` -> the injected speech seam
-> ``SpeechActuator.say``; the library's ``speak`` behavior is only what a rule
whose ``run`` IS ``speak`` admits. Inhibiting it would silence that one rule
shape and nothing else. The gate therefore lives in the ACTUATOR (see
:meth:`~reachy.behavior.speech_act.SpeechActuator.mute`), which every say path
without exception passes through, and this module is only the two commands that
flip it.

Import boundary
---------------
Mirrors :mod:`reachy.behavior.goto_intent`: this module must never import
:mod:`reachy.behavior.control` or :mod:`reachy.behavior.intents` — registering a
kind into a live registry is composition's job, not this leaf's. It imports the
shared CLI error type and nothing else from the runtime.
"""

from __future__ import annotations

from typing import Callable

from reachy.cli._errors import EXIT_USER_ERROR, CliError

#: The two command kinds (``op`` fields) this module answers to — bare verbs,
#: like :mod:`reachy.behavior.intents`'s ``run_behavior`` / ``declare_goal``.
MUTE = "mute"
UNMUTE = "unmute"

#: The ``note`` an idempotent re-issue carries. A repeat is ``ok: True`` with a
#: note, never an error: a mind that lost track of the gate and re-asserts the
#: state it wants is behaving correctly, and answering it with a failure would
#: teach it to stop asserting.
NOTE_ALREADY_MUTED = "already muted"
NOTE_NOT_MUTED = "not muted"

#: Fields every drained command carries regardless of kind (the spool envelope —
#: see :func:`reachy.behavior.control.submit`). Neither kind takes a payload of
#: its own, so these are the ONLY fields allowed.
_ENVELOPE_FIELDS = frozenset({"cmd_id", "op"})


def _reject_unknown_fields(kind: str, payload: dict) -> None:
    """Refuse a payload field this kind does not understand.

    Fail-closed on purpose, matching ``goto_intent``'s whitelist: a mind that
    submits ``{"op": "mute", "seconds": 30}`` has a real expectation about what
    happens, and silently ignoring the field would mute the robot FOREVER while
    the mind believed it had asked for half a minute.
    """
    unknown = sorted(set(payload) - _ENVELOPE_FIELDS)
    if unknown:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"{kind}: unknown field(s) {unknown} (this kind takes no payload)",
            remediation=(
                f"submit a bare {kind!r} command; the mind owns the quiet window's clock, "
                "so the runtime takes no duration"
            ),
        )


def make_mute_handlers(actuator) -> tuple[Callable[[dict, object], dict], ...]:
    """Build the ``(mute, unmute)`` handlers closing over *actuator*.

    *actuator* is duck-typed — it needs only ``mute()``, ``unmute()`` and the
    ``voice_muted`` property, exactly
    :class:`~reachy.behavior.speech_act.SpeechActuator`'s t17 surface, so a test
    can inject a bare recording fake with no audio machinery at all.

    Both results are typed the way every other kind's is (``ok`` + ``op``, plus
    the state that resulted), so a caller never has to infer the outcome from
    the absence of an error.
    """

    def mute(payload: dict, _ctx: object) -> dict:
        _reject_unknown_fields(MUTE, payload)
        changed = actuator.mute()
        result: dict = {"ok": True, "op": MUTE, "muted": True}
        if not changed:
            result["note"] = NOTE_ALREADY_MUTED
        return result

    def unmute(payload: dict, _ctx: object) -> dict:
        _reject_unknown_fields(UNMUTE, payload)
        changed = actuator.unmute()
        result: dict = {"ok": True, "op": UNMUTE, "muted": False}
        if not changed:
            result["note"] = NOTE_NOT_MUTED
        return result

    return mute, unmute


def register_into(registry, actuator):
    """Register both kinds into *registry* (duck-typed ``KindRegistry``).

    The one wiring step composition takes, mirroring
    :meth:`reachy.behavior.face_lock.FaceLockDriver.register_into` — the
    registry is returned so the call can be chained.
    """
    mute, unmute = make_mute_handlers(actuator)
    registry.register(MUTE, mute)
    registry.register(UNMUTE, unmute)
    return registry


__all__ = [
    "MUTE",
    "NOTE_ALREADY_MUTED",
    "NOTE_NOT_MUTED",
    "UNMUTE",
    "make_mute_handlers",
    "register_into",
]
