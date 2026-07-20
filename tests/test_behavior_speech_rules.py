"""Speech as a rules-reachable action — ``say`` on a react rule (task t6).

"The robot's behavior is rules and configuration" is the point of the arc, so
the speech actuator has to be reachable from ``rules.toml``. These tests pin
the shape it is reachable through: an optional, validated ``say`` STRING on a
react rule, dispatched to an injected speech seam when (and only when) the rule
actually fires.

They cover the schema gate (``reachy.behavior.rules``), the dispatch
(``reachy.behavior.rule_engine``), its survival across a live reload
(``reachy.behavior.reload_driver``), and the composition wiring
(``reachy.cli._commands.behavior``).
"""

from __future__ import annotations

import pytest

from reachy.behavior import rules as rules_mod
from reachy.behavior.reload_driver import ReloadDriver
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig, RulesLoader
from reachy.behavior.sense import Sense
from reachy.cli._errors import CliError


class _Ctx:
    """A minimal TickContext stand-in (mirrors tests/test_behavior_rule_engine.py)."""

    def __init__(self, *, sense=None, now=0.0, tick=1, active=()):
        self.sense = sense if sense is not None else Sense()
        self.now = now
        self.tick = tick
        self.admitted: list = []
        self.evicted: list = []
        self.events: list = []
        self._active = set(active)

    def emit(self, event):
        self.events.append(event)

    def admit(self, behavior):
        self.admitted.append(behavior)
        self._active.add(behavior.name)
        return {"ok": True}

    def evict(self, name):
        self.evicted.append(name)
        return {"ok": True}

    def active_names(self):
        return set(self._active)


def _speaking_rules(say="hello there", **extra):
    return {
        "react": [
            {
                "id": "greet",
                "when": {"field": "speech", "op": "is_true"},
                "run": "speak",
                "duration_s": 1.5,
                "say": say,
                **extra,
            }
        ]
    }


# --------------------------------------------------------------------------- #
# Schema — `say` is validated declarative data                                #
# --------------------------------------------------------------------------- #


def test_a_react_rule_may_carry_a_say_string():
    config = RulesConfig.from_dict(_speaking_rules())
    assert config.react[0].say == "hello there"
    assert config.react[0].behavior == "speak"


def test_say_defaults_to_none_when_absent():
    config = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "nod-at-sound",
                    "when": {"field": "speech", "op": "is_true"},
                    "run": "nod",
                    "duration_s": 1.0,
                }
            ]
        }
    )
    assert config.react[0].say is None


@pytest.mark.parametrize("bad", [123, 4.5, True, ["a"], {"a": 1}, "", "   "])
def test_a_non_string_or_blank_say_is_refused(bad):
    with pytest.raises(CliError) as excinfo:
        RulesConfig.from_dict(_speaking_rules(say=bad))
    assert "say" in excinfo.value.message


def test_an_overlong_say_is_refused_fail_closed():
    """Bounded like ``goto``'s ``MAX_DURATION_S`` — never clamped, always refused."""
    with pytest.raises(CliError) as excinfo:
        RulesConfig.from_dict(_speaking_rules(say="x" * (rules_mod.MAX_SAY_CHARS + 1)))
    assert str(rules_mod.MAX_SAY_CHARS) in excinfo.value.message


def test_an_inhibit_rule_may_not_carry_say():
    with pytest.raises(CliError) as excinfo:
        RulesConfig.from_dict(
            {
                "inhibit": [
                    {
                        "id": "hush",
                        "when": {"field": "speech", "op": "is_true"},
                        "disable": ["nod"],
                        "say": "no",
                    }
                ]
            }
        )
    assert "unexpected field" in excinfo.value.message


def test_say_survives_a_toml_round_trip_through_the_loader(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(
        "\n".join(
            [
                "[[react]]",
                'id = "greet"',
                'run = "speak"',
                "duration_s = 1.5",
                'say = "hello from the overlay"',
                "[react.when]",
                'field = "transcript"',
                'op = "is_true"',
            ]
        ),
        encoding="utf-8",
    )
    config = rules_mod.load_rules(path)
    assert config.react[0].say == "hello from the overlay"


# --------------------------------------------------------------------------- #
# Dispatch — a firing rule speaks; a suppressed one does not                  #
# --------------------------------------------------------------------------- #


def test_a_firing_react_rule_dispatches_its_say_to_the_speech_seam():
    spoken: list[str] = []
    engine = RuleEngine(RulesConfig.from_dict(_speaking_rules()), speech=spoken.append)
    engine(_Ctx(sense=Sense(speech_detected=True)))
    assert spoken == ["hello there"]


def test_the_fire_event_carries_the_spoken_text():
    ctx = _Ctx(sense=Sense(speech_detected=True))
    RuleEngine(RulesConfig.from_dict(_speaking_rules()), speech=lambda _t: None)(ctx)
    fires = [e for e in ctx.events if e["type"] == "rule.fire"]
    assert fires and fires[0]["say"] == "hello there"


def test_a_rule_without_say_never_touches_the_speech_seam():
    spoken: list[str] = []
    config = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "quiet",
                    "when": {"field": "speech", "op": "is_true"},
                    "run": "nod",
                    "duration_s": 1.0,
                }
            ]
        }
    )
    RuleEngine(config, speech=spoken.append)(_Ctx(sense=Sense(speech_detected=True)))
    assert spoken == []


def test_a_suppressed_rule_does_not_speak():
    """Cooldown-suppressed: the robot must not repeat itself every tick."""
    spoken: list[str] = []
    engine = RuleEngine(
        RulesConfig.from_dict(_speaking_rules(cooldown_s=10.0)), speech=spoken.append
    )
    for tick, now in enumerate([0.0, 0.02, 0.04], start=1):
        # `active_names` stays empty so `already-active` never masks cooldown.
        engine(_Ctx(sense=Sense(speech_detected=True), now=now, tick=tick))
    assert spoken == ["hello there"]


def test_an_inhibited_rule_does_not_speak():
    spoken: list[str] = []
    data = _speaking_rules()
    data["inhibit"] = [
        {"id": "hush", "when": {"field": "speech", "op": "is_true"}, "disable": ["speak"]}
    ]
    RuleEngine(RulesConfig.from_dict(data), speech=spoken.append)(
        _Ctx(sense=Sense(speech_detected=True))
    )
    assert spoken == []


def test_a_say_rule_with_no_speech_seam_wired_drops_with_a_named_reason(caplog):
    """Never a silent no-op — senselog's "a drop always names its reason"."""
    ctx = _Ctx(sense=Sense(speech_detected=True))
    with caplog.at_level("INFO", logger="reachy.sense"):
        RuleEngine(RulesConfig.from_dict(_speaking_rules()))(ctx)
    assert "reason=no-speech-actuator" in caplog.text
    assert ctx.admitted, "the motion half must still be admitted"


def test_a_raising_speech_seam_never_breaks_the_tick(caplog):
    def boom(_text):
        raise RuntimeError("speech exploded")

    ctx = _Ctx(sense=Sense(speech_detected=True))
    with caplog.at_level("INFO", logger="reachy.sense"):
        RuleEngine(RulesConfig.from_dict(_speaking_rules()), speech=boom)(ctx)
    assert ctx.admitted, "the rule's behavior is still admitted"
    assert "reason=speech-dispatch-failed" in caplog.text


# --------------------------------------------------------------------------- #
# The speech seam survives a live rules reload                                #
# --------------------------------------------------------------------------- #


def test_reload_driver_carries_the_speech_seam_into_a_rebuilt_rule_engine(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    path = tmp_path / "rules.toml"
    path.write_text("", encoding="utf-8")
    loader = RulesLoader(path)
    loader.reload()

    spoken: list[str] = []
    driver = ReloadDriver(loader, speech=spoken.append)

    path.write_text(
        "\n".join(
            [
                "[[react]]",
                'id = "greet"',
                'run = "speak"',
                "duration_s = 1.5",
                'say = "reloaded voice"',
                "[react.when]",
                'field = "speech"',
                'op = "is_true"',
            ]
        ),
        encoding="utf-8",
    )
    assert loader.reload() is not None
    driver._engine = driver._engine  # sanity: the driver owns its engine
    driver._apply({}, 0.0)  # force the rebuild path
    driver(_Ctx(sense=Sense(speech_detected=True)))
    assert spoken == ["reloaded voice"]


# --------------------------------------------------------------------------- #
# The library's head-bob `speak` entry is the VISUAL half of speech           #
# --------------------------------------------------------------------------- #


def test_the_speak_library_entry_is_still_pure_motion_and_says_so():
    """It bobs the head; it makes no sound. Its summary must not promise a voice.

    Reconciling the two: ``speak`` stays the mouth-movement analogue (pure,
    stateless, 50 Hz motion), and a rule's ``say`` supplies the audio. Pairing
    them in one rule is what "the robot talks" looks like.
    """
    from reachy.behavior import library

    entry = library.LIBRARY["speak"]
    summary = entry.summary.lower()
    assert "head" in summary
    assert "say" in summary  # points the operator at the audible half
    contribution = entry.build_fn()(0.1, entry.default_params(), Sense())
    assert contribution.head is not None
    assert contribution.antennas is None


# --------------------------------------------------------------------------- #
# `behavior rules list` shows the words — an operator must be able to read     #
# what their robot is about to say without opening the TOML                    #
# --------------------------------------------------------------------------- #


def test_rules_list_json_reports_the_say_text(tmp_path, monkeypatch, capsys):
    import json

    from reachy.cli import main

    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    path = tmp_path / "behavior" / "rules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[[react]]",
                'id = "greet"',
                'run = "speak"',
                "duration_s = 1.5",
                'say = "hello there"',
                "[react.when]",
                'field = "speech"',
                'op = "is_true"',
            ]
        ),
        encoding="utf-8",
    )

    assert main(["behavior", "rules", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    react = payload["react"][0]
    assert react["say"] == "hello there"
    assert react["duration_s"] == 1.5
