"""Tests for :mod:`reachy.runtime_cues` — the shared runtime-event -> cue vocabulary.

This module was extracted to remove a SonarCloud-flagged duplication between
``reachy.cli._commands.agent``'s ``_CUE_MAPPERS`` (``agent attach``) and
``reachy.embody.cues``'s ``CUE_MAPPERS`` (``agent embody``) on PR #140. Both
consumers already exercise this module indirectly (``tests/test_agent.py``,
``tests/test_embody_cues.py``); this file pins the shared module's own
contract directly, independent of either caller, so a future change to one
caller's test suite can never silently drop coverage of the shared functions.
"""

from __future__ import annotations

import pytest

from reachy import runtime_cues

# --------------------------------------------------------------------------- #
# direction_word                                                              #
# --------------------------------------------------------------------------- #


def test_direction_word_bands():
    import math

    assert runtime_cues.direction_word(0.0) == "left"
    assert runtime_cues.direction_word(math.pi / 2.0) == "ahead"
    assert runtime_cues.direction_word(math.pi) == "right"


def test_direction_word_none_for_missing_or_unparseable():
    assert runtime_cues.direction_word(None) is None
    assert runtime_cues.direction_word("not a number") is None
    assert runtime_cues.direction_word(object()) is None


# --------------------------------------------------------------------------- #
# is_number                                                                   #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, True),
        (0.5, True),
        (-3, True),
        (True, False),  # bool is an int subtype but must not count as a number
        (False, False),
        (None, False),
        ("0.5", False),
    ],
)
def test_is_number(value, expected):
    assert runtime_cues.is_number(value) is expected


# --------------------------------------------------------------------------- #
# sense_cues — the shared core (speech/loud-sound, pat, face; NOT             #
# frame_available, which is reachy.embody.cues's own extension)              #
# --------------------------------------------------------------------------- #


def test_sense_cues_speech_with_and_without_direction():
    assert runtime_cues.sense_cues({"doa": 0.0, "speech": True}) == ["speech from the left"]
    assert runtime_cues.sense_cues({"doa": None, "speech": True}) == ["speech nearby"]


def test_sense_cues_loud_sound_with_and_without_direction():
    assert runtime_cues.sense_cues({"doa": 3.14159265, "speech": False, "rms": 0.5}) == [
        "loud sound right"
    ]
    assert runtime_cues.sense_cues({"doa": None, "speech": False, "rms": 0.5}) == [
        "loud sound nearby"
    ]


def test_sense_cues_quiet_yields_no_speech_or_loudness_cue():
    assert runtime_cues.sense_cues({"doa": None, "speech": False, "rms": 0.001}) == []


def test_sense_cues_pat_phrasing():
    assert runtime_cues.sense_cues({"pat": ["scratch", "level1"]}) == [
        "felt a gentle scratch on the head"
    ]
    assert runtime_cues.sense_cues({"pat": ["side_pat", "level2"]}) == [
        "felt a firm sideways nudge on the head"
    ]
    assert runtime_cues.sense_cues({"pat": None}) == []
    assert runtime_cues.sense_cues({"pat": ["unknown_kind", "level1"]}) == []


def test_sense_cues_face():
    assert runtime_cues.sense_cues({"face": "Ada"}) == ["saw Ada"]
    assert runtime_cues.sense_cues({"face": "  "}) == []
    assert runtime_cues.sense_cues({"face": None}) == []


def test_sense_cues_never_produces_a_frame_available_cue():
    """The shared core stops at face — frame_available is cues.py's own extension."""
    assert runtime_cues.sense_cues({"frame_available": True}) == []


def test_sense_cues_everything_at_once_in_order():
    event = {
        "doa": 1.5707963267948966,
        "speech": True,
        "rms": 0.5,
        "pat": ["scratch", "level2"],
        "face": "Ori",
    }
    assert runtime_cues.sense_cues(event) == [
        "speech from the ahead",
        "felt a firm scratch on the head",
        "saw Ori",
    ]


# --------------------------------------------------------------------------- #
# rule_cues                                                                   #
# --------------------------------------------------------------------------- #


def test_rule_cues_fire_with_behavior():
    event = {"action": "fire", "rule": "hear", "behavior": "nod", "disable": []}
    assert runtime_cues.rule_cues(event) == ["a behavior rule fired (hear): now doing nod"]


def test_rule_cues_fire_with_disable():
    event = {"action": "fire", "rule": "calm", "behavior": None, "disable": ["nod", "shake"]}
    assert runtime_cues.rule_cues(event) == ["a behavior rule fired (calm): stopping nod, shake"]


def test_rule_cues_fire_bare_is_never_empty():
    event = {"action": "fire", "rule": "ping", "behavior": None, "disable": []}
    assert runtime_cues.rule_cues(event) == ["a behavior rule fired (ping)"]


def test_rule_cues_suppress():
    assert runtime_cues.rule_cues({"action": "suppress", "rule": "hear"}) == [
        "a behavior rule held off (hear)"
    ]


def test_rule_cues_unknown_action_yields_no_cue():
    assert runtime_cues.rule_cues({"action": "frobnicate", "rule": "hear"}) == []


# --------------------------------------------------------------------------- #
# intent_cues                                                                 #
# --------------------------------------------------------------------------- #


def test_intent_cues_declare_and_update():
    assert runtime_cues.intent_cues({"action": "declare", "name": "stay-alert"}) == [
        "a standing intent was set: stay-alert"
    ]
    assert runtime_cues.intent_cues({"action": "update", "name": "stay-alert"}) == [
        "a standing intent was updated: stay-alert"
    ]


def test_intent_cues_declare_unnamed():
    assert runtime_cues.intent_cues({"action": "declare", "name": ""}) == [
        "a standing intent was set"
    ]


def test_intent_cues_clear():
    assert runtime_cues.intent_cues({"action": "clear"}) == ["a standing intent was cleared"]


def test_intent_cues_status_actions_yield_no_cue():
    assert runtime_cues.intent_cues({"action": "applied", "name": "run_behavior"}) == []
    assert runtime_cues.intent_cues({"action": "blocked", "name": "run_behavior"}) == []


# --------------------------------------------------------------------------- #
# motion_cues                                                                 #
# --------------------------------------------------------------------------- #


def test_motion_cues_admit_and_evict():
    assert runtime_cues.motion_cues({"action": "admit", "behavior": "nod"}) == [
        "started moving: nod"
    ]
    assert runtime_cues.motion_cues({"action": "evict", "behavior": "nod"}) == [
        "stopped moving: nod"
    ]


def test_motion_cues_admit_with_no_label_falls_back():
    assert runtime_cues.motion_cues({"action": "admit", "behavior": None}) == [
        "started moving: a body behavior"
    ]


def test_motion_cues_goto_is_not_surfaced():
    assert runtime_cues.motion_cues({"action": "goto", "behavior": None}) == []


# --------------------------------------------------------------------------- #
# parse_runtime_line                                                          #
# --------------------------------------------------------------------------- #


def test_parse_runtime_line_parses_a_valid_json_object():
    assert runtime_cues.parse_runtime_line('{"t":"rule","action":"fire","rule":"hear"}') == {
        "t": "rule",
        "action": "fire",
        "rule": "hear",
    }


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_parse_runtime_line_is_none_for_blank_lines(line):
    assert runtime_cues.parse_runtime_line(line) is None


@pytest.mark.parametrize("line", ["not json", "[1,2,3]", '"just a string"', "42"])
def test_parse_runtime_line_is_none_for_non_object_json(line):
    assert runtime_cues.parse_runtime_line(line) is None
