"""Foreground-verb arbitration against the live behavior engine (task t5).

The ``*_active.flag`` files (``pat_active`` / ``sleep_active`` / ``think_active``)
were a *soft* cross-process guard for the shared head, read only by the ``listen``
idle layer. When the ``listen`` noun retires, those readers go with it and the
surviving writers in ``pat run`` / ``sleep run`` signal into the void — a
foreground verb and the ``reachy-runtime.service`` behavior engine would then
drive the head with no mutual awareness at all.

This suite pins the replacement: a foreground ``pat run`` / ``sleep run``
**refuses to start** while a behavior engine heartbeat is live, rather than
yielding to it. See :mod:`reachy.behavior.liveness` for the decision and its
rationale.

The freshness mechanism is the one this repo already chose for exactly this
hazard on the probe path (``behavior.py``'s ``_refuse_bad_probe_request`` /
``_probe_engine_is_fresh``): the engine's own ``state.json`` heartbeat, which is
self-expiring, rather than a flag file, which is not.
"""

from __future__ import annotations

import argparse
import json

import pytest

from reachy.behavior import control, liveness
from reachy.cli._errors import EXIT_USER_ERROR, CliError


@pytest.fixture(autouse=True)
def _isolated_state(tmp_path, monkeypatch):
    """Point every state-dir consumer at a scratch dir, never the real one."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    return tmp_path


def _publish_heartbeat(updated: float) -> None:
    """Write a state.json whose ``updated`` stamp is exactly *updated*."""
    control.state_file().write_text(json.dumps({"updated": updated}), encoding="utf-8")


# --------------------------------------------------------------------------- #
# engine_is_live — the freshness read                                          #
# --------------------------------------------------------------------------- #


def test_no_state_file_is_not_live():
    """A box that has never run an engine must not lock the operator out."""
    assert liveness.engine_is_live() is False


def test_fresh_heartbeat_is_live(monkeypatch):
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 1000.0)
    _publish_heartbeat(999.5)
    assert liveness.engine_is_live() is True


def test_stale_heartbeat_is_not_live(monkeypatch):
    """A SIGKILLed engine's leftover stamp expires — the flag-file defect, fixed.

    This is the whole reason the heartbeat beats a flag file: a flag left on
    disk by a killed writer is load-bearing forever, a heartbeat is not.
    """
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 1000.0)
    _publish_heartbeat(1000.0 - liveness.ENGINE_HEARTBEAT_TTL_S - 0.5)
    assert liveness.engine_is_live() is False


def test_marginally_future_heartbeat_is_live(monkeypatch):
    """Engine.state() rounds ``updated`` up to ms, so a live engine can read ahead."""
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 1000.0)
    _publish_heartbeat(1000.0005)
    assert liveness.engine_is_live() is True


def test_far_future_heartbeat_is_not_live(monkeypatch):
    """A pre-reboot stamp (monotonic reset) is stale, not live — never a lockout."""
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 10.0)
    _publish_heartbeat(999999.0)
    assert liveness.engine_is_live() is False


@pytest.mark.parametrize("bad", ["soon", None, True, False, float("nan"), float("inf")])
def test_unusable_heartbeat_stamp_is_not_live(bad):
    """A non-finite / non-numeric / bool stamp proves nothing — do not refuse on it."""
    control.state_file().write_text(json.dumps({"updated": bad}), encoding="utf-8")
    assert liveness.engine_is_live() is False


def test_corrupt_state_file_is_not_live():
    control.state_file().write_text("{not json", encoding="utf-8")
    assert liveness.engine_is_live() is False


# --------------------------------------------------------------------------- #
# refuse_if_engine_live — the guard                                            #
# --------------------------------------------------------------------------- #


def test_guard_is_a_no_op_when_no_engine_is_live():
    assert liveness.refuse_if_engine_live("pat run") is None


def test_guard_raises_a_clean_user_error_naming_the_fix(monkeypatch):
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 1000.0)
    _publish_heartbeat(1000.0)

    with pytest.raises(CliError) as excinfo:
        liveness.refuse_if_engine_live("pat run")

    err = excinfo.value
    assert err.code == EXIT_USER_ERROR
    assert "pat run" in err.message
    # The remediation must name a real way out, not just describe the problem.
    assert "behavior engine stop" in err.remediation
    assert liveness.RUNTIME_UNIT in err.remediation


# --------------------------------------------------------------------------- #
# The foreground verbs actually consult the guard                              #
# --------------------------------------------------------------------------- #


def _exploding_transport(*_args, **_kwargs):
    raise AssertionError("the guard must refuse BEFORE any transport is constructed")


@pytest.mark.parametrize(
    ("module_path", "cmd_name", "verb"),
    [
        ("reachy.cli._commands.pat", "cmd_pat_run", "pat run"),
        ("reachy.cli._commands.sleep", "cmd_sleep_run", "sleep run"),
    ],
)
def test_foreground_run_refuses_beside_a_live_engine(module_path, cmd_name, verb, monkeypatch):
    """Criterion 1: a foreground verb and the runtime cannot silently fight.

    The refusal must also precede transport construction, so the verb never even
    opens the single-consumer SDK media session it would have contended for.
    """
    import importlib

    module = importlib.import_module(module_path)
    monkeypatch.setattr(module, "get_transport", _exploding_transport)
    monkeypatch.setattr(liveness.time, "monotonic", lambda: 1000.0)
    _publish_heartbeat(1000.0)

    with pytest.raises(CliError) as excinfo:
        getattr(module, cmd_name)(argparse.Namespace(json=False, ticks=1))

    assert excinfo.value.code == EXIT_USER_ERROR
    assert verb in excinfo.value.message


@pytest.mark.parametrize(
    ("module_path", "cmd_name"),
    [
        ("reachy.cli._commands.pat", "cmd_pat_run"),
        ("reachy.cli._commands.sleep", "cmd_sleep_run"),
    ],
)
def test_foreground_run_proceeds_with_no_live_engine(module_path, cmd_name, monkeypatch):
    """With no engine heartbeat the guard is inert — normal operation is untouched."""
    import importlib

    module = importlib.import_module(module_path)

    sentinel = RuntimeError("reached transport construction")

    def _marker(*_args, **_kwargs):
        raise sentinel

    monkeypatch.setattr(module, "get_transport", _marker)

    # No heartbeat published: the guard must fall through to the real body,
    # which fails at our injected transport marker rather than at a refusal.
    with pytest.raises(RuntimeError) as excinfo:
        getattr(module, cmd_name)(argparse.Namespace(json=False, ticks=1))
    assert excinfo.value is sentinel


# --------------------------------------------------------------------------- #
# One policy, not two                                                          #
# --------------------------------------------------------------------------- #


def test_probe_refusal_shares_the_one_freshness_read(monkeypatch):
    """``behavior``'s probe refusal must delegate here, never fork the logic.

    Two independently-drifting definitions of "an engine is live" in one repo is
    exactly the defect this task exists to avoid.
    """
    from reachy.cli._commands import behavior as behavior_cmd

    calls: list[bool] = []

    def _fake_engine_is_live() -> bool:
        calls.append(True)
        return False

    monkeypatch.setattr(liveness, "engine_is_live", _fake_engine_is_live)
    assert behavior_cmd._probe_engine_is_fresh() is False
    assert calls, "the probe path did not route through reachy.behavior.liveness"
