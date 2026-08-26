"""``mute`` / ``unmute`` — the mind's timed quiet reaches the body's own voice.

The mind can already go quiet on its own; until t17 the BODY could not. A react
rule with a ``say`` never travels the ``speak`` library behavior — it goes
``rule_engine._speak`` -> the injected speech seam -> :meth:`SpeechActuator.say`
— so inhibiting ``speak`` silences only rules whose ``run`` IS ``speak``. The
gate therefore lives in the actuator, where EVERY say path passes through it,
and the two new intent kinds are the only thing that flips it.

What is pinned here:

* the two kinds return typed results (``ok`` + ``op`` + ``muted``), and are
  idempotent with a named ``note`` rather than an error;
* a muted actuator DROPS the utterance — no synthesis, no playback — counts it,
  and says so ONCE (a latched senselog line), with one ``count=N`` summary line
  when the voice comes back;
* driven THROUGH the rule engine's speech seam with a ``run = "nod"`` +
  ``say = "hi"`` rule: the nod still happens, the voice does not, and it comes
  back on ``unmute``.

Everything is in-process: an injected ``synthesize``/``play`` pair, an injected
clock, and a bounded ``join_idle`` — no audio device, no daemon, no network.
"""

from __future__ import annotations

import logging

import pytest

from reachy.behavior import control as control_mod
from reachy.behavior import library as behavior_library
from reachy.behavior import mute_intent as MI
from reachy.behavior import speech_act as SA
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import Sense
from reachy.cli._errors import CliError

SENSE_LOGGER = "reachy.sense"


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #


class _RecordingCtx:
    """The narrowest duck-typed tick context these tests need."""

    def __init__(self) -> None:
        self.now = 0.0
        self.tick = 0
        self.sense = Sense()
        self.admits: list = []
        self.events: list = []
        self.ownership: dict = {}

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set()


def _actuator(**kwargs) -> tuple[SA.SpeechActuator, list, list]:
    """A wired-for-test actuator plus the synth/play call logs."""
    synthesized: list[str] = []
    played: list[bytes] = []

    def synthesize(text: str) -> bytes:
        synthesized.append(text)
        return b"\x00\x01" * 8

    def play(pcm, *, samplerate: int) -> None:
        played.append(bytes(pcm))

    actuator = SA.SpeechActuator(synthesize=synthesize, samplerate=16000, play=play, **kwargs)
    return actuator, synthesized, played


def _registry(actuator) -> control_mod.KindRegistry:
    return MI.register_into(control_mod.KindRegistry(), actuator)


def _sense_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


def _muted_drop_lines(caplog) -> list[str]:
    return [line for line in _sense_lines(caplog) if f"reason={SA.REASON_MUTED}" in line]


# --------------------------------------------------------------------------- #
# Criterion 1a — the two kinds and their typed, idempotent results            #
# --------------------------------------------------------------------------- #


def test_mute_kind_returns_a_typed_result_and_silences_the_voice() -> None:
    actuator, _synth, _played = _actuator()
    registry = _registry(actuator)
    ctx = _RecordingCtx()

    result = registry.dispatch({"op": MI.MUTE, "cmd_id": "c1"}, ctx)

    assert result == {"ok": True, "op": MI.MUTE, "muted": True}
    assert actuator.voice_muted is True


def test_unmute_kind_returns_a_typed_result_and_restores_the_voice() -> None:
    actuator, _synth, _played = _actuator()
    registry = _registry(actuator)
    ctx = _RecordingCtx()
    registry.dispatch({"op": MI.MUTE, "cmd_id": "c1"}, ctx)

    result = registry.dispatch({"op": MI.UNMUTE, "cmd_id": "c2"}, ctx)

    assert result == {"ok": True, "op": MI.UNMUTE, "muted": False}
    assert actuator.voice_muted is False


def test_mute_is_idempotent_with_a_named_note() -> None:
    actuator, _synth, _played = _actuator()
    registry = _registry(actuator)
    ctx = _RecordingCtx()
    registry.dispatch({"op": MI.MUTE, "cmd_id": "c1"}, ctx)

    again = registry.dispatch({"op": MI.MUTE, "cmd_id": "c2"}, ctx)

    assert again == {"ok": True, "op": MI.MUTE, "muted": True, "note": MI.NOTE_ALREADY_MUTED}
    assert again["note"] == "already muted"


def test_unmute_on_an_unmuted_voice_is_idempotent_with_a_named_note() -> None:
    actuator, _synth, _played = _actuator()
    registry = _registry(actuator)
    ctx = _RecordingCtx()

    result = registry.dispatch({"op": MI.UNMUTE, "cmd_id": "c1"}, ctx)

    assert result == {"ok": True, "op": MI.UNMUTE, "muted": False, "note": MI.NOTE_NOT_MUTED}
    assert result["note"] == "not muted"


def test_an_unknown_field_is_refused_rather_than_silently_ignored() -> None:
    actuator, _synth, _played = _actuator()
    registry = _registry(actuator)

    result = registry.dispatch({"op": MI.MUTE, "cmd_id": "c1", "seconds": 30}, _RecordingCtx())

    assert result["ok"] is False
    assert "seconds" in str(result["error"])
    assert actuator.voice_muted is False


def test_the_handlers_raise_a_clierror_for_an_unknown_field_off_the_registry() -> None:
    actuator, _synth, _played = _actuator()
    mute, _unmute = MI.make_mute_handlers(actuator)

    with pytest.raises(CliError):
        mute({"op": MI.MUTE, "nope": 1}, _RecordingCtx())


# --------------------------------------------------------------------------- #
# Criterion 1b — a muted actuator drops, counts, and says so ONCE             #
# --------------------------------------------------------------------------- #


def test_a_muted_actuator_drops_the_utterance_without_touching_audio(caplog) -> None:
    actuator, synthesized, played = _actuator()
    actuator.mute()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        assert actuator.say("hello") is False
        actuator.join_idle(timeout=0.5)

    assert synthesized == []
    assert played == []
    assert actuator.submitted == 0
    assert actuator.spoken == 0
    assert actuator.muted_drops == 1
    assert actuator.dropped == 1


def test_the_muted_drop_is_latched_to_exactly_one_line(caplog) -> None:
    actuator, _synth, _played = _actuator()
    actuator.mute()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        for _ in range(5):
            actuator.say("hello")

    assert actuator.muted_drops == 5
    assert len(_muted_drop_lines(caplog)) == 1


def test_unmute_summarises_what_the_quiet_cost(caplog) -> None:
    actuator, _synth, _played = _actuator()
    actuator.mute()
    for _ in range(3):
        actuator.say("hello")

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        actuator.unmute()

    summaries = [line for line in _sense_lines(caplog) if "count=3" in line]
    assert len(summaries) == 1
    assert "unmute" in summaries[0]


def test_an_unmute_that_dropped_nothing_writes_no_summary(caplog) -> None:
    actuator, _synth, _played = _actuator()
    actuator.mute()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        actuator.unmute()

    assert [line for line in _sense_lines(caplog) if "count=" in line] == []


def test_a_second_mute_re_arms_the_latch(caplog) -> None:
    actuator, _synth, _played = _actuator()

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        actuator.mute()
        actuator.say("one")
        actuator.unmute()
        actuator.mute()
        actuator.say("two")

    assert len(_muted_drop_lines(caplog)) == 2


def test_an_unmuted_actuator_still_speaks() -> None:
    actuator, synthesized, played = _actuator()
    actuator.mute()
    actuator.say("dropped")
    actuator.unmute()

    assert actuator.say("spoken") is True
    assert actuator.join_idle(timeout=2.0) is True
    actuator.close()
    assert synthesized == ["spoken"]
    assert len(played) == 1


# --------------------------------------------------------------------------- #
# Criterion 1c — through the rule engine's speech seam (run="nod", say="hi")  #
# --------------------------------------------------------------------------- #


def _nod_and_say_rules() -> RulesConfig:
    entry = behavior_library.LIBRARY.get("nod")
    rule: dict = {
        "id": "greet",
        "when": {"field": "speech", "op": "is_true"},
        "run": "nod",
        "say": "hi",
        "cooldown_s": 0.0,
        "hysteresis": 0.0,
    }
    if entry is not None and entry.looping and entry.default_duration is None:
        rule["duration_s"] = 60.0
    return RulesConfig.from_dict({"react": [rule]})


def test_a_say_rule_is_silent_while_muted_and_audible_after_unmute() -> None:
    actuator, synthesized, played = _actuator()
    engine = RuleEngine(_nod_and_say_rules())
    engine.set_speech(actuator.say)

    ctx = _RecordingCtx()
    ctx.sense = Sense(speech_detected=True)

    # Muted: the MOTION half still fires (the reaction's reliable part), the
    # voice does not — which is the whole point of gating the actuator rather
    # than inhibiting a library behavior.
    actuator.mute()
    ctx.now, ctx.tick = 1.0, 1
    engine.on_tick(ctx)
    actuator.join_idle(timeout=0.5)
    assert [b.name for b in ctx.admits] == ["nod"]
    assert synthesized == []
    assert played == []
    assert actuator.muted_drops == 1

    # Unmuted: the same rule, the same seam, and now the robot talks.
    actuator.unmute()
    ctx.now, ctx.tick = 2.0, 2
    engine.on_tick(ctx)
    assert actuator.join_idle(timeout=2.0) is True
    actuator.close()
    assert synthesized == ["hi"]
    assert len(played) == 1


# --------------------------------------------------------------------------- #
# Criterion 3 — registered at composition, exactly the way ``goto`` is        #
# --------------------------------------------------------------------------- #


def test_the_composition_site_registers_both_kinds_into_the_one_registry() -> None:
    """A source-level check, like the face lock's sibling: wiring IS the feature.

    A handler nobody registered is a kind that answers ``unknown kind``, and
    booting the real CLI to discover that would prove it far more slowly and no
    more precisely.
    """
    import inspect  # noqa: PLC0415

    from reachy.cli._commands import behavior as behavior_cmd  # noqa: PLC0415

    source = inspect.getsource(behavior_cmd._compose_run_seam)
    assert "register_mute_kinds(intent_driver.registry, speech)" in source
    # The SAME registry the goto kind joins — five-plus kinds, one registry.
    goto_at = source.index("registry.register(GOTO")
    mute_at = source.index("register_mute_kinds(")
    assert mute_at > goto_at, "the quiet kinds belong beside goto, in the same registry"


def test_the_gate_is_the_actuators_not_a_wrapper_the_rules_layer_can_miss() -> None:
    """``rules_driver.set_speech`` is handed the ACTUATOR's own bound method.

    If composition ever wrapped ``say`` to add the gate, any other holder of the
    unwrapped bound method (the agent seam, a future consumer) would talk
    straight through a mute. Pinning the seam here keeps the gate where every
    path already passes.
    """
    import inspect  # noqa: PLC0415

    from reachy.cli._commands import behavior as behavior_cmd  # noqa: PLC0415

    source = inspect.getsource(behavior_cmd._compose_run_seam)
    assert "rules_driver.set_speech(speech.say)" in source
