"""Tests for the ``goto`` intent kind (:mod:`reachy.behavior.goto_intent`, task t6).

The command a handler here answers to arrives over an externally-writable spool
(:mod:`reachy.behavior.control`'s namespaced command dir) — any agent, script,
or bug can drop one. There is no clamping anywhere on the engine's streaming
path (:meth:`reachy.behavior.engine.Engine.compose_tick` composes contributions
raw; only individual :mod:`reachy.behavior.library` behaviors self-clamp their
own amplitudes), so this module's validation is the ONLY gate standing between
a malformed/wild goto target and the daemon. Every assertion below pins the
fail-closed contract: a bad payload is refused with a specific, named error and
:class:`GotoLane`'s ``submit`` is provably never called.

Deterministic throughout — a duck-typed recording fake lane (only ``submit`` is
needed, mirroring the module's own "GotoLane, duck-typed" contract) and a bare
sentinel ``ctx`` (the handler never touches it). No robot, daemon, network, or
LLM anywhere in this file.
"""

from __future__ import annotations

import ast
import inspect
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import pytest

import reachy.behavior.goto_intent as goto_intent_mod
from reachy.behavior.goto_intent import (
    ANTENNA_LIMIT_DEG,
    BODY_YAW_LIMIT_DEG,
    GOTO,
    HEAD_PITCH_LIMIT_DEG,
    HEAD_ROLL_LIMIT_DEG,
    HEAD_X_LIMIT_MM,
    HEAD_YAW_LIMIT_DEG,
    HEAD_Z_LIMIT_MM,
    MAX_DURATION_S,
    make_goto_handler,
)
from reachy.behavior.goto_lane import GotoSpec
from reachy.cli._errors import CliError

# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


class _RecordingLane:
    """A bare duck-typed ``GotoLane`` — only ``submit`` is exercised by the handler."""

    def __init__(self) -> None:
        self.submitted: list[GotoSpec] = []

    def submit(self, spec: GotoSpec) -> str:
        self.submitted.append(spec)
        return f"goto-{len(self.submitted)}"


_CTX = object()  # the handler never touches ctx; a plain sentinel is enough


def _command(op: str = GOTO, **fields: object) -> dict:
    """Build a full drained-command dict, mirroring control.submit's envelope shape."""
    return {"cmd_id": "cmd-1", "op": op, **fields}


# --------------------------------------------------------------------------- #
# Valid submission                                                            #
# --------------------------------------------------------------------------- #


def test_valid_payload_submits_a_matching_gotospec_and_returns_the_goto_id() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    result = handler(
        _command(
            head={"pitch": 5.0, "yaw": -3.0},
            antennas=[10.0, -10.0],
            body_yaw=4.0,
            duration=2.0,
            interpolation="ease",
            label="wave",
        ),
        _CTX,
    )

    assert len(lane.submitted) == 1
    spec = lane.submitted[0]
    assert spec.head == {"pitch": 5.0, "yaw": -3.0}
    assert spec.antennas == (10.0, -10.0)
    assert spec.body_yaw == 4.0
    assert spec.duration == 2.0
    assert spec.interpolation == "ease"
    assert spec.label == "wave"

    assert result["ok"] is True
    assert result["op"] == GOTO
    assert result["id"] == "goto-1"
    assert result["channels"] == sorted(spec.channels())
    assert result["duration"] == 2.0


def test_minimal_payload_uses_gotospec_defaults_for_omitted_fields() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    result = handler(_command(body_yaw=5.0), _CTX)

    spec = lane.submitted[0]
    assert spec.label == GotoSpec.label
    assert spec.duration == GotoSpec.duration
    assert spec.interpolation == GotoSpec.interpolation
    assert spec.head is None
    assert spec.antennas is None
    assert spec.body_yaw == 5.0
    assert result["ok"] is True
    assert result["id"] == "goto-1"


def test_head_only_target_is_a_valid_single_channel_goto() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    result = handler(_command(head={"yaw": 1.0}), _CTX)

    assert lane.submitted[0].channels() == frozenset({"head"})
    assert result["channels"] == ["head"]


# --------------------------------------------------------------------------- #
# At least one channel required                                              #
# --------------------------------------------------------------------------- #


def test_a_goto_with_no_channel_target_is_refused_and_never_submitted() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(duration=1.0), _CTX)

    assert "at least one channel" in exc.value.message
    assert lane.submitted == []


# --------------------------------------------------------------------------- #
# Out-of-range targets — refused, named, never submitted                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "head_payload, bad_axis, limit",
    [
        ({"x": HEAD_X_LIMIT_MM + 0.1}, "head.x", HEAD_X_LIMIT_MM),
        ({"z": -(HEAD_Z_LIMIT_MM + 1.0)}, "head.z", HEAD_Z_LIMIT_MM),
        ({"roll": HEAD_ROLL_LIMIT_DEG + 5.0}, "head.roll", HEAD_ROLL_LIMIT_DEG),
        ({"pitch": -(HEAD_PITCH_LIMIT_DEG + 5.0)}, "head.pitch", HEAD_PITCH_LIMIT_DEG),
        ({"yaw": HEAD_YAW_LIMIT_DEG + 100.0}, "head.yaw", HEAD_YAW_LIMIT_DEG),
    ],
)
def test_out_of_range_head_axis_is_refused_naming_axis_and_limit(
    head_payload: dict, bad_axis: str, limit: float
) -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(head=head_payload), _CTX)

    assert bad_axis in exc.value.message
    assert str(limit) in exc.value.message
    assert "out of range" in exc.value.message
    assert lane.submitted == []


def test_out_of_range_antenna_is_refused_naming_axis_and_limit() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(antennas=[ANTENNA_LIMIT_DEG + 1.0, 0.0]), _CTX)

    assert "antennas.right" in exc.value.message
    assert str(ANTENNA_LIMIT_DEG) in exc.value.message
    assert lane.submitted == []


def test_out_of_range_body_yaw_is_refused_naming_axis_and_limit() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=BODY_YAW_LIMIT_DEG + 0.5), _CTX)

    assert "body_yaw" in exc.value.message
    assert str(BODY_YAW_LIMIT_DEG) in exc.value.message
    assert lane.submitted == []


def test_a_wild_target_from_a_buggy_agent_is_never_submitted() -> None:
    """The core safety property this module exists for: a huge, obviously-wild
    target on ANY axis is refused before ``lane.submit`` is ever reached."""
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError):
        handler(_command(head={"yaw": 99999.0}, duration=1.0), _CTX)

    assert lane.submitted == []


# --------------------------------------------------------------------------- #
# Unknown fields refused                                                     #
# --------------------------------------------------------------------------- #


def test_unknown_top_level_field_is_refused_and_never_submitted() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=1.0, speed=99.0), _CTX)

    assert "speed" in exc.value.message
    assert lane.submitted == []


def test_envelope_fields_cmd_id_and_op_are_not_treated_as_unknown() -> None:
    """The full drained command carries cmd_id/op alongside the GotoSpec fields —
    those must never be rejected as 'unknown'."""
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    result = handler(_command(body_yaw=1.0), _CTX)

    assert result["ok"] is True


def test_unknown_head_axis_is_refused_and_never_submitted() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(head={"elbow": 1.0}), _CTX)

    assert "elbow" in exc.value.message
    assert lane.submitted == []


# --------------------------------------------------------------------------- #
# Duration bounds                                                            #
# --------------------------------------------------------------------------- #


def test_non_positive_duration_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=1.0, duration=0.0), _CTX)

    assert "duration" in exc.value.message
    assert lane.submitted == []


def test_negative_duration_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError):
        handler(_command(body_yaw=1.0, duration=-2.0), _CTX)

    assert lane.submitted == []


def test_absurd_duration_beyond_ten_seconds_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=1.0, duration=MAX_DURATION_S + 0.1), _CTX)

    assert "duration" in exc.value.message
    assert str(MAX_DURATION_S) in exc.value.message
    assert lane.submitted == []


def test_duration_exactly_at_the_ceiling_is_accepted() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    result = handler(_command(body_yaw=1.0, duration=MAX_DURATION_S), _CTX)

    assert result["ok"] is True
    assert lane.submitted[0].duration == MAX_DURATION_S


# --------------------------------------------------------------------------- #
# Non-numeric / bool values refused                                          #
# --------------------------------------------------------------------------- #


def test_bool_value_for_body_yaw_is_refused_not_treated_as_zero_or_one() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=True), _CTX)

    assert "body_yaw" in exc.value.message
    assert lane.submitted == []


def test_bool_value_for_duration_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError):
        handler(_command(body_yaw=1.0, duration=True), _CTX)

    assert lane.submitted == []


def test_string_value_for_head_axis_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(head={"yaw": "12"}), _CTX)

    assert "head.yaw" in exc.value.message
    assert lane.submitted == []


def test_non_list_antennas_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(antennas="not-a-pair"), _CTX)

    assert "antennas" in exc.value.message
    assert lane.submitted == []


def test_wrong_length_antennas_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(antennas=[1.0, 2.0, 3.0]), _CTX)

    assert "antennas" in exc.value.message
    assert lane.submitted == []


def test_non_dict_head_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(head=[1.0, 2.0]), _CTX)

    assert "head" in exc.value.message
    assert lane.submitted == []


def test_non_string_interpolation_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=1.0, interpolation=7), _CTX)

    assert "interpolation" in exc.value.message
    assert lane.submitted == []


def test_non_string_label_is_refused() -> None:
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(body_yaw=1.0, label=7), _CTX)

    assert "label" in exc.value.message
    assert lane.submitted == []


# --------------------------------------------------------------------------- #
# Handler is registerable-shaped: raises CliError the way KindRegistry expects #
# --------------------------------------------------------------------------- #


def test_handler_raises_cli_error_matching_kind_registry_catch_contract() -> None:
    """Mirrors reachy.behavior.control.KindRegistry.dispatch's own test coverage
    (test_behavior_intents.py's test_kind_registry_handler_cli_error_becomes_clean_outcome):
    a validation failure must be a CliError with .message/.remediation, not a bespoke
    exception or a plain dict, so a future KindRegistry.dispatch(cmd, ctx) call (wired
    at composition, outside this module) catches it identically to every other kind."""
    lane = _RecordingLane()
    handler = make_goto_handler(lane)

    with pytest.raises(CliError) as exc:
        handler(_command(duration=1.0), _CTX)

    assert isinstance(exc.value, CliError)
    assert exc.value.message
    assert lane.submitted == []


# --------------------------------------------------------------------------- #
# Import boundary — no reachy.behavior.control / reachy.behavior.intents       #
# --------------------------------------------------------------------------- #


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


def test_goto_intent_module_does_not_import_control_or_intents() -> None:
    for name in _imported_modules(goto_intent_mod):
        assert "behavior.control" not in name, f"goto_intent.py must not import control ({name!r})"
        assert "behavior.intents" not in name, f"goto_intent.py must not import intents ({name!r})"
    assert "control" not in goto_intent_mod.__dict__
    assert "control_mod" not in goto_intent_mod.__dict__
    assert "intents" not in goto_intent_mod.__dict__


def test_importing_goto_intent_does_not_pull_control_or_intents_into_sys_modules() -> None:
    """A fresh interpreter importing reachy.behavior.goto_intent must not transitively
    import reachy.behavior.control or reachy.behavior.intents — registration of the
    GOTO kind into a live KindRegistry is composition's job (a later task), never this
    leaf module's."""
    code = (
        "import sys, reachy.behavior.goto_intent;"
        "assert 'reachy.behavior.control' not in sys.modules, 'control leaked';"
        "assert 'reachy.behavior.intents' not in sys.modules, 'intents leaked';"
        "print('ok')"
    )
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
