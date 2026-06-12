"""Tests for the ``reachy sleep`` noun (t8): run / start / stop / restart /
status / demo / overview.

Covers the acceptance criteria:

1. ``sleep demo --json`` (injected synthetic sense + a fake clock, no robot)
   walks ALERT -> DROWSY -> ASLEEP then back to ALERT on a wake event, observable
   in the structured output (c1/h14).
2. ``sleep status --json`` reports the process/daemon health; an arc unit test
   drives the full ALERT -> DROWSY -> ASLEEP -> wake using a fake clock + injected
   sense with zero robot and zero real wall-clock wait (c7/h15).
3. The noun follows the listen|think|pat scaffold (run / start / stop / restart /
   status / demo / overview, every verb ``--json``, the CliError two-line
   contract).

Tests drive the CLI through ``reachy.cli.main([...])`` exactly like the other
``test_cli_*`` / ``test_*_cli`` suites, monkeypatching ``get_transport`` (or the
SDK import) so no real robot / ``[sdk]`` extra is required. ``demo`` needs no
transport at all.
"""

from __future__ import annotations

import json

import pytest

from reachy.cli import main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.sleep.state import SleepState

# --- overview -------------------------------------------------------------


def test_sleep_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "demo" in out
    assert "overview" in out


def test_sleep_overview_json_lists_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert "sleep run" in blob
    assert "sleep demo" in blob
    assert "sleep overview" in blob


def test_sleep_bare_shows_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep"])
    assert rc == 0
    assert "Verbs" in capsys.readouterr().out


def test_sleep_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sleep", "--help"])
    assert exc.value.code == 0


# --- demo (no robot) ------------------------------------------------------


def test_sleep_demo_json_walks_full_arc(capsys: pytest.CaptureFixture[str]) -> None:
    """``sleep demo --json`` walks ALERT -> DROWSY -> ASLEEP then back to ALERT
    on a wake event, all observable in the structured output. No robot, no SDK."""
    rc = main(["sleep", "demo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    states = payload["states"]
    assert isinstance(states, list) and len(states) >= 4
    # The arc must contain each level in order, and end back at ALERT after wake.
    assert states[0] == "ALERT"
    assert "DROWSY" in states
    assert "ASLEEP" in states
    # ASLEEP must come before the final ALERT (the wake).
    assert states.index("ASLEEP") < (len(states) - 1)
    assert states[-1] == "ALERT"
    # The wake event is reported explicitly.
    assert payload["woke"] is True


def test_sleep_demo_text_no_robot_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "demo"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_sleep_demo_does_not_need_reachy_mini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the noun + running demo must not require the reachy_mini SDK."""
    import builtins

    real_import = builtins.__import__

    def _guard(name: str, *args, **kwargs):
        if name == "reachy_mini" or name.startswith("reachy_mini."):
            raise ImportError("reachy_mini is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    rc = main(["sleep", "demo", "--json"])
    assert rc == 0


# --- status ---------------------------------------------------------------


def test_sleep_status_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``sleep status --json`` reports a process state + the cross-process sleep
    state; ``idle_seconds`` is ``null`` (the live timer is not readable across
    processes)."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    rc = main(["sleep", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Process health (delegated to the supervisor).
    assert payload["process"] in {"running", "stopped", "stale"}
    # Cross-process sleep state (from the flag) is reported.
    assert payload["state"] in {"ALERT", "DROWSY", "ASLEEP"}
    # idle_seconds is present but null — the live timer lives in the loop process.
    assert "idle_seconds" in payload
    assert payload["idle_seconds"] is None


def test_sleep_status_reports_asleep_when_flag_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """When the sleep-active flag is present, status reports the ASLEEP state."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from reachy.motion import sleep_signal

    sleep_signal.write()
    try:
        rc = main(["sleep", "status", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "ASLEEP"
    finally:
        sleep_signal.clear()


# --- run: missing-SDK path ------------------------------------------------


class _NoSdkTransport:
    """An sdk-flavor transport whose media_session raises the missing-[sdk] error."""

    name = "sdk"

    def media_session(self) -> object:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the reachy_mini SDK is not installed",
            remediation="install the sdk extra: pip install 'reachy-mini-cli[sdk]'",
        )

    def move_goto(self, **kwargs: object) -> None:  # pragma: no cover - guard
        return None


def test_sleep_run_sdk_missing_exits_2_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "1"])
    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_sleep_run_sdk_missing_exits_2_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "1", "--json"])
    assert rc == EXIT_ENV_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == EXIT_ENV_ERROR
    assert "message" in payload and "remediation" in payload


def test_sleep_unknown_verb_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sleep", "bogus-verb"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


# --- run: bounded loop with a fake transport ------------------------------


class _SilentSession:
    """A media session (context manager) that yields no DoA and no audio — so the
    sleep loop senses 'nothing' every tick and the idle clock runs uninterrupted."""

    def __enter__(self) -> "_SilentSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def doa(self, *, timeout: float | None = None) -> object:
        return None  # no reading → EMPTY_SENSE

    def get_audio_sample(self) -> object:
        return None


class _FakeSdkTransport:
    """An sdk-flavor transport with a silent media session and a goto sink."""

    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []

    def media_session(self) -> _SilentSession:
        return _SilentSession()

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_sleep_run_bounded_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    fake = _FakeSdkTransport()
    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: fake)
    monkeypatch.setattr(sleep_mod.time, "sleep", lambda *_a, **_k: None)
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "5", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    last = [line for line in out.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["status"] == "ok"
    assert payload["ticks"] == 5


# --- arc unit test (fake clock + injected sense, zero robot/wall-clock) ----


def test_sleep_arc_alert_drowsy_asleep_wake_no_wallclock() -> None:
    """Drive the decay->sleep->wake arc with an injected fake clock + injected
    synthetic sense feed and a bounded tick count, asserting the observed state
    sequence — with zero robot and zero real wall-clock wait."""
    from reachy.cli._commands.sleep import run_sleep_arc
    from reachy.motion.queue import MotionQueue

    # Fake clock: jumps forward by a big step each tick so a tiny idle-timeout
    # drives ALERT -> DROWSY -> ASLEEP across just a few ticks.
    clock_state = {"t": 0.0}

    def fake_now() -> float:
        return clock_state["t"]

    # A sense feed: returns 'no stimulus' until the final tick, then a wake event.
    from reachy.behavior.sense import EMPTY_SENSE, Sense

    senses = [EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, Sense(speech_detected=True)]
    feed_state = {"i": 0}

    def fake_sense() -> Sense:
        s = senses[min(feed_state["i"], len(senses) - 1)]
        return s

    # Advance the clock between ticks via on_tick: 10 s per tick.
    def advance() -> None:
        feed_state["i"] += 1
        clock_state["t"] += 10.0

    queue = MotionQueue()
    result = run_sleep_arc(
        queue=queue,
        now=fake_now,
        sense=fake_sense,
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,  # drowsy_after=7.5, asleep_after=15 → asleep by tick ~2-3
    )
    states = result["states"]
    assert states[0] == SleepState.ALERT.name
    assert SleepState.DROWSY.name in states
    assert SleepState.ASLEEP.name in states
    # The final tick had a speech stimulus → wake → back to ALERT.
    assert states[-1] == SleepState.ALERT.name
    assert result["woke"] is True
