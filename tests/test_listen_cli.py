"""Tests for the ``listen`` noun group and the ``reachy.motion.supervisor``.

No real robot, daemon, or background process is involved: the motion loop runs
against a fake transport, and the supervisor's subprocess (``subprocess.Popen``),
liveness (``os.kill`` / ``is_alive``), grace sleep, and HTTP health check are
monkeypatched. State is pinned to a tmp dir via ``REACHY_STATE_DIR``. (The motion
queue, executor, and listen producer are unit-tested in ``tests/test_motion.py``;
here we cover the CLI wiring and the process supervisor.)
"""

from __future__ import annotations

import argparse
import json
import signal

import pytest

from reachy.cli import _build_parser, main
from reachy.cli._commands.listen import _add_tuning_args, _params_from_args
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.motion import supervisor
from reachy.motion.listen import ListenParams


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


class _FakeTransport:
    """Records gotos; answers ``doa`` with a fixed reading (``None`` = no mic)."""

    name = "fake"

    def __init__(self, doa=None) -> None:
        self.gotos: list[dict] = []
        self._doa = doa

    def move_goto(self, **kwargs) -> object:  # noqa: ANN003 - test shim
        self.gotos.append(kwargs)
        return {"uuid": "x"}

    def doa(self, *, timeout=None) -> object:  # noqa: ANN001 - test shim
        return self._doa


# --- CLI: run -------------------------------------------------------------


def test_run_centers_then_settles_when_silent(monkeypatch, capsys) -> None:
    tr = _FakeTransport()  # no mic -> producer abstains, no look-at moves
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda args: tr)
    rc = main(["listen", "run", "--max-ticks", "3"])
    assert rc == 0
    # First goto is the preflight center; last is the settle-to-center.
    assert tr.gotos[0]["head"]["yaw"] == 0.0
    assert tr.gotos[0]["interpolation"] == "minjerk"
    assert tr.gotos[-1]["head"]["yaw"] == 0.0


def test_run_orients_toward_sound_json(monkeypatch, capsys) -> None:
    # t6: the head turn (Tier-2) is triggered by SPEECH or a loud snap — a bare latched
    # DoA angle never turns the head. So the live source here is a *speech* reading on the
    # left; the producer leans (Tier-1) then orients toward it (Tier-2).
    tr = _FakeTransport(doa={"angle": 0.0, "speech_detected": True})  # speech on the left
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda args: tr)
    # Two-tier listen: a Tier-1 antenna lean precedes the Tier-2 head turn, and each
    # move runs serially through the queue — so allow enough ticks for the head turn to
    # *dispatch* after the lean's interpolation completes.
    rc = main(["listen", "run", "--json", "--dwell", "0", "--deadband", "0", "--max-ticks", "20"])
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(e.get("action") for e in events)  # reacted to the sound
    # t6 triggers the head turn immediately on speech (no dwell wait), so with deadband 0
    # the off-axis speech orients the head (Tier-2: left -> +yaw). The Tier-1 antenna lean
    # is exercised in tests/test_motion.py, where a within-deadband / no-trigger sound leans.
    assert any((e.get("yaw") or 0.0) > 0 for e in events)  # Tier-2 turn: left -> +yaw


def test_run_unreachable_exits_2(monkeypatch, capsys) -> None:
    class _Dead(_FakeTransport):
        def move_goto(self, **kwargs):
            raise CliError(
                code=EXIT_ENV_ERROR, message="cannot reach daemon", remediation="daemon start"
            )

    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda args: _Dead())
    rc = main(["listen", "run", "--max-ticks", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    # The startup diagnostic prints only after a successful preflight, so a failed
    # preflight yields exactly the two-line error:/hint: contract.
    assert err.startswith("error:")
    assert "hint:" in err
    assert "orienting to sound" not in err


# --- CLI / supervisor: start ---------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 4242

    def poll(self):
        return self.returncode


def _popen_factory(box):
    def _popen(cmd, **kwargs):  # noqa: ANN001 - test shim
        proc = _FakePopen(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
    raise AssertionError("must not spawn a process here")


def test_start_preflights_and_spawns(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("reachy.motion.supervisor.health_ok", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = main(["listen", "start", "--dwell", "2", "--speed", "12", "--speech-only"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out and "pid: 4242" in out
    assert (tmp_path / "listen.pid").read_text().strip() == "4242"
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "listen", "run"]
    assert cmd[cmd.index("--dwell") + 1] == "2.0"
    assert cmd[cmd.index("--speed") + 1] == "12.0"
    assert "--speech-only" in cmd
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_refuses_when_daemon_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.motion.supervisor.health_ok", lambda *a, **k: False)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    # Use --transport http explicitly: the health-check preflight is http-only.
    rc = main(["listen", "start", "--transport", "http"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "daemon start" in err


def test_start_idempotent_when_already_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "listen.pid").write_text("4242")
    monkeypatch.setattr("reachy.motion.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["listen", "start"])
    assert rc == 0
    assert "already-running" in capsys.readouterr().out


def test_start_sdk_skips_http_preflight(monkeypatch, capsys) -> None:
    def _boom(*a, **k):
        raise AssertionError("sdk start must not call the http health check")

    monkeypatch.setattr("reachy.motion.supervisor.health_ok", _boom)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    rc = main(["listen", "start", "--transport", "sdk"])
    assert rc == 0
    assert "status: started" in capsys.readouterr().out
    assert "--transport" in procs[0].cmd and "sdk" in procs[0].cmd


# --- CLI / supervisor: stop ----------------------------------------------


def test_stop_when_not_running(capsys) -> None:
    rc = main(["listen", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "listen.pid").write_text("4242")
    state = {"alive": True}
    monkeypatch.setattr("reachy.motion.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.motion.supervisor._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    rc = main(["listen", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGTERM" in out
    assert killed == [(4242, signal.SIGTERM)]
    assert not (tmp_path / "listen.pid").exists()


# --- CLI / supervisor: status --------------------------------------------


def test_status_running_healthy(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "listen.pid").write_text("4242")
    monkeypatch.setattr("reachy.motion.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.motion.supervisor.health_ok", lambda *a, **k: True)
    rc = main(["listen", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running" and payload["pid"] == 4242
    assert payload["daemon"] == "healthy"


def test_status_stopped_when_no_pid(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.motion.supervisor.health_ok", lambda *a, **k: False)
    rc = main(["listen", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "process: stopped" in out and "daemon: unreachable" in out


# --- supervisor units + overview -----------------------------------------


def test_build_run_command_serializes_params() -> None:
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=10.0,
        params=ListenParams(
            dwell=2.0, hold=4.0, alert_speed=12.0, relax_speed=12.0, speech_only=True
        ),
    )
    assert cmd[1:5] == ["-m", "reachy", "listen", "run"]
    assert cmd[cmd.index("--dwell") + 1] == "2.0"
    assert cmd[cmd.index("--hold") + 1] == "4.0"
    assert cmd[cmd.index("--speed") + 1] == "12.0"
    assert "--speech-only" in cmd
    # The always-alive idle knobs must reach the background process too.
    assert cmd[cmd.index("--idle-energy") + 1] == "1.0"
    assert cmd[cmd.index("--drift-speed") + 1] == "4.0"


def test_build_run_command_omits_speech_only_by_default() -> None:
    cmd = supervisor.build_run_command(
        transport="http", base_url="x", timeout=1.0, params=ListenParams()
    )
    assert "--speech-only" not in cmd


def test_listen_overview_text(capsys) -> None:
    assert main(["listen", "overview"]) == 0
    assert "# reachy-mini-cli listen" in capsys.readouterr().out


def test_bare_listen_prints_overview(capsys) -> None:
    assert main(["listen"]) == 0
    assert capsys.readouterr().out.strip()


# --- SDK-first default transport -----------------------------------------


def test_listen_run_defaults_to_sdk(monkeypatch) -> None:
    """``reachy listen run`` with no --transport and no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["listen", "run"])
    assert args.transport == "sdk"


def test_listen_run_transport_flag_overrides(monkeypatch) -> None:
    """``--transport http`` still selects http regardless of the SDK default."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["listen", "run", "--transport", "http"])
    assert args.transport == "http"


def test_listen_run_env_overrides_sdk_default(monkeypatch) -> None:
    """REACHY_TRANSPORT=http overrides the SDK default (env is respected)."""
    monkeypatch.setenv("REACHY_TRANSPORT", "http")
    # Re-import to pick up the env var that is read at registration time.
    import importlib

    import reachy.cli._commands.listen as _listen_mod

    importlib.reload(_listen_mod)
    import reachy.cli as _cli_mod

    importlib.reload(_cli_mod)
    from reachy.cli import _build_parser as _bp

    parser = _bp()
    args = parser.parse_args(["listen", "run"])
    assert args.transport == "http"
    # Reload back to sdk default for other tests.
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    importlib.reload(_listen_mod)
    importlib.reload(_cli_mod)


def test_listen_start_defaults_to_sdk(monkeypatch) -> None:
    """``reachy listen start`` with no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["listen", "start"])
    assert args.transport == "sdk"


def test_listen_restart_defaults_to_sdk(monkeypatch) -> None:
    """``reachy listen restart`` with no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["listen", "restart"])
    assert args.transport == "sdk"


# --- New tuning flags parse + thread into ListenParams -------------------


def _parse_tuning(argv: list[str]) -> argparse.Namespace:
    """Helper: build a minimal parser with just the tuning args and parse *argv*."""
    p = argparse.ArgumentParser()
    _add_tuning_args(p)
    return p.parse_args(argv)


def test_antenna_max_flag() -> None:
    args = _parse_tuning(["--antenna-max", "25"])
    params = _params_from_args(args)
    assert params.antenna_max == 25.0


def test_body_yaw_max_flag() -> None:
    args = _parse_tuning(["--body-yaw-max", "60"])
    params = _params_from_args(args)
    assert params.body_yaw_max == 60.0


def test_head_only_band_flag() -> None:
    args = _parse_tuning(["--head-only-band", "20"])
    params = _params_from_args(args)
    assert params.head_only_band == 20.0


def test_antenna_gain_flag() -> None:
    args = _parse_tuning(["--antenna-gain", "0.5"])
    params = _params_from_args(args)
    assert params.antenna_gain == 0.5


def test_body_speed_flag() -> None:
    args = _parse_tuning(["--body-speed", "8"])
    params = _params_from_args(args)
    assert params.body_speed == 8.0


def test_idle_energy_flag() -> None:
    args = _parse_tuning(["--idle-energy", "0"])
    params = _params_from_args(args)
    assert params.idle_energy == 0.0


def test_drift_speed_flag() -> None:
    args = _parse_tuning(["--drift-speed", "6"])
    params = _params_from_args(args)
    assert params.drift_speed == 6.0


def test_new_tuning_defaults_unchanged_when_unset() -> None:
    """Unset new flags keep the ListenParams dataclass defaults."""
    d = ListenParams()
    args = _parse_tuning([])
    params = _params_from_args(args)
    assert params.antenna_gain == d.antenna_gain
    assert params.antenna_max == d.antenna_max
    assert params.body_yaw_max == d.body_yaw_max
    assert params.body_speed == d.body_speed
    assert params.head_only_band == d.head_only_band
    assert params.idle_energy == d.idle_energy
    assert params.drift_speed == d.drift_speed


def test_combined_new_tuning_flags() -> None:
    """All new flags together map to the right ListenParams fields."""
    args = _parse_tuning(["--antenna-max", "25", "--body-yaw-max", "60", "--head-only-band", "20"])
    params = _params_from_args(args)
    assert params.antenna_max == 25.0
    assert params.body_yaw_max == 60.0
    assert params.head_only_band == 20.0


def test_existing_flags_still_parse() -> None:
    """Legacy flags (--gain, --max-yaw, --deadband, --dwell, --hold, --speed,
    --recenter-after, --speech-only) continue to work alongside the new ones."""
    args = _parse_tuning(
        [
            "--gain",
            "0.8",
            "--max-yaw",
            "40",
            "--deadband",
            "10",
            "--dwell",
            "2",
            "--hold",
            "5",
            "--speed",
            "20",
            "--recenter-after",
            "6",
            "--speech-only",
        ]
    )
    params = _params_from_args(args)
    assert params.gain == 0.8
    assert params.max_yaw == 40.0
    assert params.deadband == 10.0
    assert params.dwell == 2.0
    assert params.hold == 5.0
    assert params.alert_speed == 20.0
    assert params.relax_speed == 20.0
    assert params.recenter_after == 6.0
    assert params.speech_only is True


def test_snap_ratio_and_floor_flags_parse() -> None:
    """--snap-ratio and --snap-floor are accepted by the parser."""
    args = _parse_tuning(["--snap-ratio", "7.0", "--snap-floor", "0.05"])
    assert args.snap_ratio == 7.0
    assert args.snap_floor == 0.05
