"""Tests for :mod:`reachy.embody.cues` — runtime events -> perception cues.

Two acceptance criteria drive this file's shape:

1. A table test maps EVERY runtime line type (``rule`` fire/suppress the
   headline, ``sense``, ``intent``, ``motion``) to its cue text; an unknown or
   malformed line is skipped with a named drop.
2. An absent bus-subscribe capability degrades to one named drop and the
   feed-tail fallback, with no live network access — proven against a fake
   subscriber (the happy/failure paths) and against the REAL installed
   ``events-cli`` package (the canary documenting today's publish-only gap).
"""

from __future__ import annotations

import io
import itertools
import socket

import pytest

from reachy.embody import cues
from tests.fake_bus_subscriber import FakeBusSubscriber

# --------------------------------------------------------------------------- #
# 1. The cue-mapping table — every runtime line type -> its cue text          #
# --------------------------------------------------------------------------- #

_RULE_FIRE_WITH_BEHAVIOR = {
    "t": "rule",
    "action": "fire",
    "rule": "hear",
    "behavior": "nod",
    "disable": [],
}
_RULE_FIRE_WITH_DISABLE = {
    "t": "rule",
    "action": "fire",
    "rule": "calm",
    "behavior": None,
    "disable": ["nod", "shake"],
}
_RULE_FIRE_BARE = {
    "t": "rule",
    "action": "fire",
    "rule": "ping",
    "behavior": None,
    "disable": [],
}
_RULE_SUPPRESS = {"t": "rule", "action": "suppress", "rule": "hear"}
_RULE_UNKNOWN_ACTION = {"t": "rule", "action": "frobnicate", "rule": "hear"}

_SENSE_SPEECH_AHEAD = {
    "t": "sense",
    "doa": 1.5707963267948966,  # pi/2 -> "ahead"
    "speech": True,
    "rms": 0.01,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_SPEECH_NO_DOA = {
    "t": "sense",
    "doa": None,
    "speech": True,
    "rms": None,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_LOUD_LEFT = {
    "t": "sense",
    "doa": 0.0,
    "speech": False,
    "rms": 0.05,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_LOUD_NO_DOA = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": 0.05,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_QUIET = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": 0.001,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_PAT_SCRATCH_L1 = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": None,
    "pat": ["scratch", "level1"],
    "face": None,
    "frame_available": False,
}
_SENSE_PAT_SIDE_L2 = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": None,
    "pat": ["side_pat", "level2"],
    "face": None,
    "frame_available": False,
}
_SENSE_FACE = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": None,
    "pat": None,
    "face": "Ori",
    "frame_available": False,
}
_SENSE_FRAME_AVAILABLE = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": None,
    "pat": None,
    "face": None,
    "frame_available": True,
}
_SENSE_NOTHING = {
    "t": "sense",
    "doa": None,
    "speech": False,
    "rms": None,
    "pat": None,
    "face": None,
    "frame_available": False,
}
_SENSE_EVERYTHING = {
    "t": "sense",
    "doa": 1.5707963267948966,
    "speech": True,
    "rms": 0.5,
    "pat": ["scratch", "level2"],
    "face": "Ori",
    "frame_available": True,
}

_INTENT_DECLARE_NAMED = {"t": "intent", "action": "declare", "name": "stay-alert"}
_INTENT_DECLARE_UNNAMED = {"t": "intent", "action": "declare", "name": ""}
_INTENT_UPDATE_NAMED = {"t": "intent", "action": "update", "name": "stay-alert"}
_INTENT_CLEAR = {"t": "intent", "action": "clear", "name": "stay-alert"}
_INTENT_APPLIED = {"t": "intent", "action": "applied", "name": "run_behavior"}
_INTENT_BLOCKED = {"t": "intent", "action": "blocked", "name": "run_behavior"}

_MOTION_ADMIT = {"t": "motion", "action": "admit", "behavior": "nod"}
_MOTION_EVICT = {"t": "motion", "action": "evict", "behavior": "nod"}
_MOTION_ADMIT_NO_LABEL = {"t": "motion", "action": "admit", "behavior": None}
_MOTION_GOTO = {"t": "motion", "action": "goto", "behavior": None}

_TABLE = [
    pytest.param(
        _RULE_FIRE_WITH_BEHAVIOR,
        ["a behavior rule fired (hear): now doing nod"],
        id="rule-fire-behavior",
    ),
    pytest.param(
        _RULE_FIRE_WITH_DISABLE,
        ["a behavior rule fired (calm): stopping nod, shake"],
        id="rule-fire-disable",
    ),
    pytest.param(_RULE_FIRE_BARE, ["a behavior rule fired (ping)"], id="rule-fire-bare"),
    pytest.param(_RULE_SUPPRESS, ["a behavior rule held off (hear)"], id="rule-suppress"),
    pytest.param(_RULE_UNKNOWN_ACTION, [], id="rule-unknown-action"),
    pytest.param(_SENSE_SPEECH_AHEAD, ["speech from the ahead"], id="sense-speech-ahead"),
    pytest.param(_SENSE_SPEECH_NO_DOA, ["speech nearby"], id="sense-speech-no-doa"),
    pytest.param(_SENSE_LOUD_LEFT, ["loud sound left"], id="sense-loud-left"),
    pytest.param(_SENSE_LOUD_NO_DOA, ["loud sound nearby"], id="sense-loud-no-doa"),
    pytest.param(_SENSE_QUIET, [], id="sense-quiet-no-cue"),
    pytest.param(
        _SENSE_PAT_SCRATCH_L1, ["felt a gentle scratch on the head"], id="sense-pat-scratch-l1"
    ),
    pytest.param(
        _SENSE_PAT_SIDE_L2, ["felt a firm sideways nudge on the head"], id="sense-pat-side-l2"
    ),
    pytest.param(_SENSE_FACE, ["saw Ori"], id="sense-face"),
    pytest.param(
        _SENSE_FRAME_AVAILABLE, ["a camera frame is available"], id="sense-frame-available"
    ),
    pytest.param(_SENSE_NOTHING, [], id="sense-nothing"),
    pytest.param(
        _SENSE_EVERYTHING,
        [
            "speech from the ahead",
            "felt a firm scratch on the head",
            "saw Ori",
            "a camera frame is available",
        ],
        id="sense-everything-at-once",
    ),
    pytest.param(
        _INTENT_DECLARE_NAMED, ["a standing intent was set: stay-alert"], id="intent-declare"
    ),
    pytest.param(
        _INTENT_DECLARE_UNNAMED, ["a standing intent was set"], id="intent-declare-unnamed"
    ),
    pytest.param(
        _INTENT_UPDATE_NAMED, ["a standing intent was updated: stay-alert"], id="intent-update"
    ),
    pytest.param(_INTENT_CLEAR, ["a standing intent was cleared"], id="intent-clear"),
    pytest.param(_INTENT_APPLIED, [], id="intent-applied-status-no-cue"),
    pytest.param(_INTENT_BLOCKED, [], id="intent-blocked-status-no-cue"),
    pytest.param(_MOTION_ADMIT, ["started moving: nod"], id="motion-admit"),
    pytest.param(_MOTION_EVICT, ["stopped moving: nod"], id="motion-evict"),
    pytest.param(
        _MOTION_ADMIT_NO_LABEL, ["started moving: a body behavior"], id="motion-admit-no-label"
    ),
    pytest.param(_MOTION_GOTO, [], id="motion-goto-not-surfaced"),
]


@pytest.mark.parametrize(("event", "expected"), _TABLE)
def test_every_runtime_line_type_maps_to_its_cue_text(event, expected):
    assert cues.cues_for_runtime_event(event) == expected


def test_rule_fires_are_the_headline_react_in_voice_input():
    """A rule fire is never silently dropped — it is always a non-empty cue."""
    for event in (_RULE_FIRE_WITH_BEHAVIOR, _RULE_FIRE_WITH_DISABLE, _RULE_FIRE_BARE):
        assert cues.cues_for_runtime_event(event), event


# --------------------------------------------------------------------------- #
# Unknown / malformed lines are skipped with a NAMED drop, never silently     #
# --------------------------------------------------------------------------- #


def test_an_unknown_line_type_is_skipped_with_a_named_drop(caplog):
    with caplog.at_level("INFO"):
        result = cues.cues_for_runtime_event({"t": "cognition", "ts": 1.0})
    assert result == []
    assert "dropped reason=unknown-line-type" in caplog.text
    assert "t='cognition'" in caplog.text


def test_a_line_with_no_t_field_is_skipped_with_a_named_drop(caplog):
    with caplog.at_level("INFO"):
        result = cues.cues_for_runtime_event({"ts": 1.0})
    assert result == []
    assert "dropped reason=unknown-line-type" in caplog.text


@pytest.mark.parametrize("bad", [None, "a string", 42, ["not", "a", "dict"]])
def test_a_non_dict_event_is_skipped_with_a_named_drop(bad, caplog):
    with caplog.at_level("INFO"):
        result = cues.cues_for_runtime_event(bad)
    assert result == []
    assert "dropped reason=malformed-line" in caplog.text


def test_a_recognised_type_producing_zero_cues_is_not_logged_as_a_drop(caplog):
    """intent.applied / motion.goto / an unrecognised rule action are normal, not errors."""
    with caplog.at_level("INFO"):
        cues.cues_for_runtime_event(_INTENT_APPLIED)
        cues.cues_for_runtime_event(_MOTION_GOTO)
        cues.cues_for_runtime_event(_RULE_UNKNOWN_ACTION)
    assert "dropped" not in caplog.text


# --------------------------------------------------------------------------- #
# parse_runtime_line                                                          #
# --------------------------------------------------------------------------- #


def test_parse_runtime_line_parses_a_valid_json_object():
    assert cues.parse_runtime_line('{"t":"rule","action":"fire","rule":"hear"}') == {
        "t": "rule",
        "action": "fire",
        "rule": "hear",
    }


@pytest.mark.parametrize("line", ["", "   ", "\n"])
def test_parse_runtime_line_is_none_for_blank_lines(line):
    assert cues.parse_runtime_line(line) is None


@pytest.mark.parametrize("line", ["not json", "[1,2,3]", '"just a string"', "42"])
def test_parse_runtime_line_is_none_for_non_object_json(line):
    assert cues.parse_runtime_line(line) is None


def test_cues_for_line_composes_parse_and_map():
    line = '{"t":"rule","action":"suppress","rule":"hear"}\n'
    assert cues.cues_for_line(line) == ["a behavior rule held off (hear)"]


def test_cues_for_line_is_empty_for_a_blank_line():
    assert cues.cues_for_line("\n") == []


# --------------------------------------------------------------------------- #
# 2. Intake: bus-subscribe primary, feed-tail fallback                       #
# --------------------------------------------------------------------------- #


def test_resolve_bus_subscriber_without_a_factory_is_none():
    """No factory injected -> no bus-subscribe capability -> None, always."""
    assert cues.resolve_bus_subscriber() is None


def test_resolve_bus_subscriber_with_a_factory_returns_what_it_builds():
    fake = FakeBusSubscriber()
    assert cues.resolve_bus_subscriber(factory=lambda: fake) is fake


def test_no_bus_subscriber_falls_back_to_the_feed_with_one_named_drop(caplog):
    feed_text = '{"t":"rule","action":"fire","rule":"hear","behavior":"nod","disable":[]}\n'
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(stdin=io.StringIO(feed_text))
        collected = list(lines)
    assert collected == [feed_text]
    assert "dropped reason=no-bus-subscriber" in caplog.text


def test_a_connected_bus_subscriber_is_used_and_the_feed_is_never_touched(caplog):
    fake = FakeBusSubscriber(autoconnect=True)
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(
            feed="/nonexistent/path/should/never/be/opened.jsonl",
            subscriber_factory=lambda: fake,
        )
    assert fake.connect_calls == 1
    assert fake.subscribed_topic_filters == [cues.DEFAULT_TOPIC_FILTER]
    assert "subscribed" in caplog.text

    fake.push("reachy/events/rule/fire", '{"t":"rule","action":"fire","rule":"hear"}')
    fake.push("reachy/events/rule/suppress", '{"t":"rule","action":"suppress","rule":"hear"}')
    collected = list(itertools.islice(lines, 2))
    assert collected == [
        '{"t":"rule","action":"fire","rule":"hear"}',
        '{"t":"rule","action":"suppress","rule":"hear"}',
    ]


def test_bus_connect_failure_falls_back_with_a_named_drop(caplog):
    fake = FakeBusSubscriber(raise_on_connect=RuntimeError("no route to broker"))
    feed_text = '{"t":"motion","action":"admit","behavior":"nod"}\n'
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(
            stdin=io.StringIO(feed_text), subscriber_factory=lambda: fake
        )
        collected = list(lines)
    assert collected == [feed_text]
    assert "dropped reason=bus-connect-failed" in caplog.text


def test_bus_never_gets_a_session_falls_back_with_a_named_drop(caplog):
    """connect() raises nothing but no session results — the honest broker-not-up-yet case."""
    fake = FakeBusSubscriber(autoconnect=False)
    feed_text = '{"t":"motion","action":"evict","behavior":"nod"}\n'
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(
            stdin=io.StringIO(feed_text), subscriber_factory=lambda: fake
        )
        collected = list(lines)
    assert collected == [feed_text]
    assert "dropped reason=bus-broker-unreachable" in caplog.text
    assert fake.connect_calls == 1


def test_bus_subscribe_failure_falls_back_with_a_named_drop(caplog):
    fake = FakeBusSubscriber(raise_on_subscribe=RuntimeError("bad topic filter"))
    feed_text = '{"t":"intent","action":"clear","name":"x"}\n'
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(
            stdin=io.StringIO(feed_text), subscriber_factory=lambda: fake
        )
        collected = list(lines)
    assert collected == [feed_text]
    assert "dropped reason=bus-subscribe-failed" in caplog.text


def test_an_incompatible_subscriber_falls_back_with_a_named_drop(caplog):
    class _Incomplete:
        connected = False

        def connect(self) -> None:
            return None

    feed_text = '{"t":"sense","doa":null,"speech":false,"rms":null,"pat":null,"face":null}\n'
    with caplog.at_level("INFO"):
        lines = cues.open_runtime_lines(
            stdin=io.StringIO(feed_text), subscriber_factory=lambda: _Incomplete()
        )
        collected = list(lines)
    assert collected == [feed_text]
    assert "dropped reason=bus-subscriber-incompatible" in caplog.text
    assert "missing=" in caplog.text


def test_missing_subscriber_members_reports_exactly_whats_absent():
    class _Bare:
        pass

    missing = cues.missing_subscriber_members(_Bare())
    assert set(missing) == {"connected", "connect", "disconnect", "subscribe"}
    assert cues.missing_subscriber_members(FakeBusSubscriber()) == ()


# --------------------------------------------------------------------------- #
# Feed-tail semantics (mirrors agent attach's --feed contract)                #
# --------------------------------------------------------------------------- #


def test_feed_dash_reads_from_injected_stdin():
    lines = list(cues.open_runtime_lines(feed="-", stdin=io.StringIO("a\nb\n")))
    assert lines == ["a\n", "b\n"]


def test_feed_path_reads_a_real_file(tmp_path):
    feed_file = tmp_path / "runtime.jsonl"
    feed_file.write_text('{"t":"rule","action":"suppress","rule":"hear"}\n', encoding="utf-8")
    lines = list(cues.open_runtime_lines(feed=str(feed_file)))
    assert lines == ['{"t":"rule","action":"suppress","rule":"hear"}\n']


def test_a_missing_feed_path_raises_rather_than_silently_yielding_nothing():
    with pytest.raises(OSError):
        list(cues.open_runtime_lines(feed="/nonexistent/definitely/not/here.jsonl"))


# --------------------------------------------------------------------------- #
# Test-safety: the default (uninjected) path never touches a live socket      #
# --------------------------------------------------------------------------- #


def test_the_default_intake_never_opens_a_live_socket(monkeypatch):
    def _deny(*_a, **_kw):
        raise AssertionError("cues.py's default intake must never touch a live socket")

    monkeypatch.setattr(socket.socket, "connect", _deny)
    monkeypatch.setattr(socket, "create_connection", _deny)
    lines = cues.open_runtime_lines(stdin=io.StringIO("{}\n"))
    assert list(lines) == ["{}\n"]


# --------------------------------------------------------------------------- #
# The reported gap: today's events-cli client is publish-only (canary)        #
# --------------------------------------------------------------------------- #

real_events_cli = pytest.importorskip("events_cli")


def test_the_installed_events_cli_client_has_no_subscribe_capability():
    """Documents the gap this task reports rather than patches.

    events-cli>=0.9's ``EventClient`` — the only class the package
    re-exports — has no ``subscribe``/``on_message`` surface anywhere (Python
    API or CLI). If this test starts failing, events-cli has grown subscribe
    support and :func:`reachy.embody.cues.resolve_bus_subscriber` should be
    extended to bind it, the same way
    :mod:`reachy.export.events_client` binds the publish leg.
    """
    assert not hasattr(real_events_cli.EventClient, "subscribe")
    assert not hasattr(real_events_cli.EventClient, "on_message")


def test_cues_module_never_imports_events_cli_or_a_raw_mqtt_library():
    """The adapter discipline this task follows: no vendor shape to bind yet.

    Unlike :mod:`reachy.export.events_client` (the ONE module allowed to name
    ``events_cli`` for the publish leg), this module has nothing to adapt —
    see its docstring — so it must never IMPORT the vendor package (or any raw
    MQTT library) itself. An AST check, not a substring search, because the
    module docstring legitimately DISCUSSES ``events_cli`` in prose.
    """
    import ast
    import inspect

    tree = ast.parse(inspect.getsource(cues))
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_names.add(node.module.split(".")[0])

    forbidden = {"events_cli", "paho"}
    assert imported_names.isdisjoint(forbidden), imported_names & forbidden
