"""Tests for the sense-stage log helper (reachy/senselog.py)."""

import logging
import re

import pytest

from reachy import senselog

LOGGER_NAME = "reachy.sense"

_LINE_RE = re.compile(
    r"^\[SENSE stage=(?P<stage>\S+) source=(?P<source>\S+) event=(?P<event>\S+)\] "
    r"(?P<detail>.*)$"
)


def test_logger_is_dedicated_and_named_reachy_sense():
    assert senselog.logger.name == LOGGER_NAME


def test_stage_emits_fixed_parseable_shape(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.stage("vad", "speech", "3f2a9c1e", "utterance detected")

    assert len(caplog.records) == 1
    record = caplog.records[0]
    message = record.getMessage()

    assert message == "[SENSE stage=vad source=speech event=3f2a9c1e] utterance detected"
    match = _LINE_RE.match(message)
    assert match is not None
    assert match.group("stage") == "vad"
    assert match.group("source") == "speech"
    assert match.group("event") == "3f2a9c1e"
    assert match.group("detail") == "utterance detected"


def test_stage_logs_at_info_level(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.stage("capture", "vision", "abc123", "frame captured")

    assert caplog.records[0].levelno == logging.INFO


def test_stage_uses_dedicated_logger_name(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.stage("inject", "touch", "ev1", "pat forwarded to cognition")

    assert caplog.records[0].name == LOGGER_NAME


def test_stage_does_not_emit_below_info(caplog):
    # A stage() call should never be silently swallowed by an INFO threshold
    # check elsewhere in the app; this locks the level choice explicitly.
    with caplog.at_level(logging.WARNING, logger=LOGGER_NAME):
        senselog.stage("vad", "speech", "ev2", "utterance detected")

    assert caplog.records == []


def test_drop_emits_fixed_parseable_shape_with_reason(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.drop("engagement", "speech", "ev3", "self-mute")

    assert len(caplog.records) == 1
    message = caplog.records[0].getMessage()

    match = _LINE_RE.match(message)
    assert match is not None
    assert match.group("stage") == "engagement"
    assert match.group("source") == "speech"
    assert match.group("event") == "ev3"
    # The reason must be greppable directly out of the detail text.
    assert "dropped reason=self-mute" in match.group("detail")


def test_drop_logs_at_info_level(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.drop("throttle", "vision", "ev4", "throttle")

    assert caplog.records[0].levelno == logging.INFO
    assert caplog.records[0].name == LOGGER_NAME


@pytest.mark.parametrize(
    "reason",
    ["self-mute", "throttle", "gate-reject", "cooldown"],
)
def test_drop_names_every_documented_reason(caplog, reason):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.drop("gate", "speech", "ev5", reason)

    message = caplog.records[0].getMessage()
    assert f"dropped reason={reason}" in message


def test_drop_message_grep_pattern_is_stable(caplog):
    # Lock the exact greppable substring shape callers/log-scrapers rely on.
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.drop("wake", "audio", "ev6", "cooldown")

    message = caplog.records[0].getMessage()
    assert re.search(r"dropped reason=cooldown\b", message)


def test_stage_and_drop_share_the_same_line_shape(caplog):
    with caplog.at_level(logging.INFO, logger=LOGGER_NAME):
        senselog.stage("vad", "speech", "ev7", "kept")
        senselog.drop("vad", "speech", "ev7", "gate-reject")

    kept, dropped = (r.getMessage() for r in caplog.records)
    assert _LINE_RE.match(kept) is not None
    assert _LINE_RE.match(dropped) is not None
