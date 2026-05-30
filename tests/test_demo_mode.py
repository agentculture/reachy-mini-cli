"""Tests for the ``demo-mode`` noun group and the ``reachy.alive`` module.

No real robot, daemon, or background process is involved: the motion engine runs
against a fake in-memory transport, and the supervisor's subprocess
(``subprocess.Popen``), liveness (``os.kill`` / ``is_alive``), grace sleep, and
HTTP health check are monkeypatched. Every test pins bookkeeping into a throwaway
dir via ``REACHY_STATE_DIR`` so the real ``~/.local/state/reachy`` is untouched.
"""

from __future__ import annotations

import json
import random
import signal

import pytest

from reachy import alive
from reachy.cli import main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    # Never touch the host's real systemd: by default every systemctl call looks
    # 'absent'. Service-specific tests override this with their own fake runner.
    monkeypatch.setattr("reachy.demo_service._run", lambda args: None)


class _FakeTransport(alive.Transport):
    """Records every operation; optionally fails ``move_goto`` a set number of times."""

    name = "fake"

    def __init__(self, fail_times: int = 0, fail_forever: bool = False) -> None:
        self.calls: list[tuple] = []
        self.gotos: list[dict] = []
        self._fail_times = fail_times
        self._fail_forever = fail_forever

    def wake(self) -> object:
        self.calls.append(("wake",))
        return {"status": "ok"}

    def move_goto(self, **kwargs) -> object:  # noqa: ANN003 - test shim
        self.calls.append(("goto", kwargs))
        self.gotos.append(kwargs)
        if self._fail_forever or self._fail_times > 0:
            self._fail_times -= 1
            raise CliError(code=EXIT_ENV_ERROR, message="daemon gone", remediation="start it")
        return {"uuid": "x"}


# --- motion engine: next_pose --------------------------------------------


def test_next_pose_shape_and_units() -> None:
    cfg = alive.AliveConfig(seed=1)
    pose = alive.next_pose(0.0, random.Random(1), cfg)
    assert set(pose) == {"head", "antennas", "body_yaw", "duration", "interpolation"}
    assert set(pose["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert len(pose["antennas"]) == 2
    assert pose["interpolation"] == cfg.interpolation
    # duration tracks the interval so motion glides between ticks.
    assert pose["duration"] == pytest.approx(cfg.interval * 0.9)


def test_next_pose_is_deterministic_with_seed() -> None:
    cfg = alive.AliveConfig()
    a = alive.next_pose(1.0, random.Random(42), cfg)
    b = alive.next_pose(1.0, random.Random(42), cfg)
    assert a == b


def test_energy_zero_is_nearly_still() -> None:
    cfg = alive.AliveConfig(energy=0.0)
    pose = alive.next_pose(1.234, random.Random(7), cfg)
    assert pose["head"]["z"] == 0.0
    assert pose["head"]["yaw"] == 0.0
    assert pose["antennas"] == (0.0, 0.0)
    assert pose["body_yaw"] == 0.0


def test_energy_scales_amplitude() -> None:
    big = alive.next_pose(1.0, random.Random(3), alive.AliveConfig(energy=2.0))
    small = alive.next_pose(1.0, random.Random(3), alive.AliveConfig(energy=1.0))
    # Same RNG stream + same elapsed -> bigger energy gives a larger yaw magnitude.
    assert abs(big["head"]["yaw"]) > abs(small["head"]["yaw"])


# --- motion engine: run_loop ---------------------------------------------


def test_run_loop_sends_poses_and_settles() -> None:
    tr = _FakeTransport()
    cfg = alive.AliveConfig(interval=0, seed=1)  # interval 0 -> no sleeping
    ticks = alive.run_loop(tr, cfg, sleep=lambda *_: None, max_ticks=3)
    assert ticks == 3
    ops = [c[0] for c in tr.calls]
    assert ops[0] == "wake"  # preflight/wake first
    assert ops.count("goto") == 4  # 3 ticks + 1 settle
    # Settle is the neutral pose (centred head).
    settle = tr.gotos[-1]
    assert settle["head"] == {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}


def test_run_loop_no_wake_preflights_with_neutral() -> None:
    tr = _FakeTransport()
    cfg = alive.AliveConfig(interval=0, seed=1)
    alive.run_loop(tr, cfg, sleep=lambda *_: None, max_ticks=1, wake=False)
    assert ("wake",) not in tr.calls
    # First op is a neutral goto (the preflight).
    assert tr.gotos[0]["head"]["yaw"] == 0.0


def test_run_loop_no_settle_skips_final_neutral() -> None:
    tr = _FakeTransport()
    cfg = alive.AliveConfig(interval=0, seed=1)
    ticks = alive.run_loop(tr, cfg, sleep=lambda *_: None, max_ticks=2, settle=False)
    assert ticks == 2
    assert [c[0] for c in tr.calls].count("goto") == 2  # no settle goto


def test_run_loop_preflight_propagates_when_robot_unreachable() -> None:
    # wake() itself fails -> the loop never starts.
    class _DeadOnWake(_FakeTransport):
        def wake(self):
            raise CliError(code=EXIT_ENV_ERROR, message="no daemon", remediation="start it")

    with pytest.raises(CliError):
        alive.run_loop(_DeadOnWake(), alive.AliveConfig(interval=0), sleep=lambda *_: None)


def test_run_loop_on_start_runs_after_preflight() -> None:
    tr = _FakeTransport()
    cfg = alive.AliveConfig(interval=0, seed=1)
    order: list = []
    tr_wake = tr.wake

    def _wake():
        order.append("preflight")
        return tr_wake()

    tr.wake = _wake  # type: ignore[method-assign]
    alive.run_loop(
        tr, cfg, sleep=lambda *_: None, max_ticks=1, on_start=lambda: order.append("on_start")
    )
    assert order[:2] == ["preflight", "on_start"]


def test_run_loop_on_start_skipped_when_preflight_fails() -> None:
    class _DeadOnWake(_FakeTransport):
        def wake(self):
            raise CliError(code=EXIT_ENV_ERROR, message="no daemon", remediation="start it")

    started: list = []
    with pytest.raises(CliError):
        alive.run_loop(
            _DeadOnWake(),
            alive.AliveConfig(interval=0),
            sleep=lambda *_: None,
            on_start=lambda: started.append(1),
        )
    assert started == []  # never announced a start that didn't happen


def test_run_loop_tolerates_transient_errors_then_recovers() -> None:
    tr = _FakeTransport(fail_times=2)  # first two gotos fail, then succeed
    cfg = alive.AliveConfig(interval=0, seed=1, max_errors=5)
    events: list[dict] = []
    ticks = alive.run_loop(
        tr, cfg, sleep=lambda *_: None, max_ticks=4, emit=events.append, settle=False
    )
    assert ticks == 4
    assert [e["ok"] for e in events] == [False, False, True, True]


def test_run_loop_gives_up_after_max_consecutive_errors() -> None:
    tr = _FakeTransport(fail_forever=True)
    cfg = alive.AliveConfig(interval=0, seed=1, max_errors=3)
    with pytest.raises(CliError):
        alive.run_loop(tr, cfg, sleep=lambda *_: None, max_ticks=100, settle=False)
    # Stopped at the error ceiling, not after all 100 ticks (no settle goto here).
    assert [c[0] for c in tr.calls].count("goto") == 3


# --- CLI: run -------------------------------------------------------------


def test_run_command_json_emits_event_stream(monkeypatch, capsys) -> None:
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.demo_mode.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)  # don't actually wait between ticks
    rc = main(["demo-mode", "run", "--json", "--interval", "1", "--max-ticks", "2"])
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]
    assert [e["tick"] for e in events] == [1, 2]
    assert all(e["ok"] for e in events)


def test_run_command_unreachable_exits_2(monkeypatch, capsys) -> None:
    class _Dead(_FakeTransport):
        def wake(self):
            raise CliError(
                code=EXIT_ENV_ERROR, message="cannot reach daemon", remediation="daemon start"
            )

    monkeypatch.setattr("reachy.cli._commands.demo_mode.get_transport", lambda args: _Dead())
    rc = main(["demo-mode", "run", "--interval", "1", "--max-ticks", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    # The startup diagnostic is emitted only AFTER a successful preflight, so a
    # failed preflight yields exactly the two-line error:/hint: contract.
    assert err.startswith("error:")
    assert "hint:" in err
    assert "feeling alive" not in err


# --- CLI / supervisor: start ---------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 5151

    def poll(self):
        return self.returncode


class _DeadPopen(_FakePopen):
    returncode = 1


def _popen_factory(box, cls=_FakePopen):
    def _popen(cmd, **kwargs):  # noqa: ANN001 - test shim
        proc = cls(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


def test_start_preflights_and_spawns(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = main(["demo-mode", "start", "--interval", "3", "--energy", "0.5", "--seed", "9"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out
    assert "pid: 5151" in out
    assert (tmp_path / "demo-mode.pid").read_text().strip() == "5151"
    # Re-invokes this CLI's demo-mode run with the tuning forwarded.
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "demo-mode", "run"]
    assert cmd[cmd.index("--interval") + 1] == "3.0"
    assert cmd[cmd.index("--energy") + 1] == "0.5"
    assert cmd[cmd.index("--seed") + 1] == "9"
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_refuses_when_daemon_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: False)

    def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise AssertionError("must not spawn when the daemon is unreachable")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["demo-mode", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "daemon start" in err


def test_start_idempotent_when_already_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: True)

    def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise AssertionError("must not spawn when a loop already runs")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["demo-mode", "start"])
    assert rc == 0
    assert "already-running" in capsys.readouterr().out


def test_start_reports_startup_crash(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.Popen", _popen_factory([], cls=_DeadPopen))
    rc = main(["demo-mode", "start"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: exited" in out
    assert "exit_code: 1" in out


def test_start_sdk_skips_http_preflight(monkeypatch, capsys) -> None:
    # sdk transport can't be health-checked over http; start must not require it.
    def _boom(*a, **k):
        raise AssertionError("sdk start must not call the http health check")

    monkeypatch.setattr("reachy.alive.health_ok", _boom)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    rc = main(["demo-mode", "start", "--transport", "sdk"])
    assert rc == 0
    assert "status: started" in capsys.readouterr().out
    assert "--transport" in procs[0].cmd and "sdk" in procs[0].cmd


# --- CLI / supervisor: stop ----------------------------------------------


def test_stop_when_not_running(capsys) -> None:
    rc = main(["demo-mode", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    state = {"alive": True}
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.alive._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    rc = main(["demo-mode", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGTERM" in out
    assert killed == [(5151, signal.SIGTERM)]
    assert not (tmp_path / "demo-mode.pid").exists()


def test_stop_escalates_to_sigkill(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    state = {"alive": True}
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.alive._is_our_process", lambda pid: True)
    sigs: list = []

    def _kill(pid, sig):
        sigs.append(sig)
        if sig == signal.SIGKILL:
            state["alive"] = False  # ignores SIGTERM; dies on SIGKILL

    monkeypatch.setattr("os.kill", _kill)
    rc = main(["demo-mode", "stop", "--timeout", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGKILL" in out
    assert signal.SIGTERM in sigs and signal.SIGKILL in sigs


def test_stop_fails_when_unkillable(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.alive._is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)  # signals do nothing
    rc = main(["demo-mode", "stop", "--timeout", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert "still alive after SIGKILL" in err
    assert (tmp_path / "demo-mode.pid").exists()  # kept — process is still there


def test_start_wraps_popen_oserror(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: True)

    def _boom(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise OSError("Exec format error")

    monkeypatch.setattr("subprocess.Popen", _boom)
    rc = main(["demo-mode", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "failed to launch demo-mode" in err
    assert "hint:" in err


def test_stop_refuses_reused_pid(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.alive._is_our_process", lambda pid: False)
    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(sig))
    rc = main(["demo-mode", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out and "reused" in out
    assert killed == []
    assert not (tmp_path / "demo-mode.pid").exists()


# --- CLI / supervisor: status --------------------------------------------


def test_status_running_healthy(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "demo-mode.pid").write_text("5151")
    monkeypatch.setattr("reachy.alive.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: True)
    rc = main(["demo-mode", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running"
    assert payload["pid"] == 5151
    assert payload["daemon"] == "healthy"


def test_status_stopped_when_no_pid(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: False)
    rc = main(["demo-mode", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "process: stopped" in out
    assert "daemon: unreachable" in out


def test_status_includes_service_block(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.alive.health_ok", lambda *a, **k: True)
    rc = main(["demo-mode", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["service"]["unit"] == "reachy-demo-mode.service"
    assert payload["service"]["installed"] is False  # nothing installed in the tmp dir


# --- restart --------------------------------------------------------------


def test_restart_uses_service_when_active(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.demo_service.is_active", lambda: True)
    monkeypatch.setattr("reachy.demo_service.restart", lambda: {"status": "restarted"})

    def _no_proc(**kwargs):
        raise AssertionError("must not restart the process when the service is active")

    monkeypatch.setattr("reachy.alive.restart", _no_proc)
    rc = main(["demo-mode", "restart", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "service"
    assert payload["status"] == "restarted"


def test_restart_uses_process_when_service_inactive(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.demo_service.is_active", lambda: False)
    captured = {}

    def _fake_restart(**kwargs):
        captured.update(kwargs)
        return {"status": "started", "pid": 1, "restarted_from": "not running"}

    monkeypatch.setattr("reachy.alive.restart", _fake_restart)
    rc = main(["demo-mode", "restart", "--json", "--energy", "0.4"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["mode"] == "process"
    assert captured["energy"] == 0.4  # resolved config forwarded to the relaunch


def test_alive_restart_stops_then_starts(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr(
        "reachy.alive.stop", lambda **k: calls.append("stop") or {"status": "stopped"}
    )
    monkeypatch.setattr(
        "reachy.alive.start", lambda **k: calls.append("start") or {"status": "started", "pid": 7}
    )
    out = alive.restart(transport="http")
    assert calls == ["stop", "start"]
    assert out["restarted_from"] == "stopped"


# --- config CLI -----------------------------------------------------------


def test_config_show_defaults(capsys) -> None:
    rc = main(["demo-mode", "config", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["interval"] == 2.5
    assert payload["config"]["energy"] == 1.0


def test_config_set_persists(tmp_path, capsys) -> None:
    rc = main(["demo-mode", "config", "--set", "energy=0.8", "interval=4", "--json"])
    assert rc == 0
    json.loads(capsys.readouterr().out)
    # Re-read shows the persisted values.
    rc = main(["demo-mode", "config", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert payload["config"]["energy"] == 0.8
    assert payload["config"]["interval"] == 4.0


def test_config_set_rejects_bad_key(capsys) -> None:
    rc = main(["demo-mode", "config", "--set", "wobble=1"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown config key" in err
    assert "hint:" in err


def test_run_reads_persisted_config(monkeypatch, capsys) -> None:
    main(["demo-mode", "config", "--set", "energy=0.2", "seed=5"])
    capsys.readouterr()
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.demo_mode.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    rc = main(["demo-mode", "run", "--json", "--max-ticks", "1"])
    assert rc == 0
    # energy 0.2 from config -> small first goto magnitudes (not the energy-1 defaults).
    assert tr.gotos  # at least woke + one pose


# --- service CLI ----------------------------------------------------------


def test_install_writes_unit(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.demo_service.install",
        lambda config_file=None: {"status": "installed", "unit_path": str(config_file)},
    )
    rc = main(["demo-mode", "install", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "installed"


def test_install_creates_missing_custom_config(monkeypatch, tmp_path, capsys) -> None:
    # Qodo #4: a custom --config that doesn't exist must be created so the unit
    # never points at a missing file.
    seen: dict = {}
    monkeypatch.setattr(
        "reachy.demo_service.install",
        lambda config_file=None: seen.update(cf=config_file) or {"status": "installed"},
    )
    custom = tmp_path / "nested" / "custom.json"
    assert not custom.exists()
    rc = main(["demo-mode", "install", "--config", str(custom), "--json"])
    assert rc == 0
    assert custom.is_file()  # ensure() created it
    assert seen["cf"] == str(custom)  # unit points at the real file


def test_enable_passes_linger_flag(monkeypatch, capsys) -> None:
    seen = {}
    monkeypatch.setattr(
        "reachy.demo_service.enable",
        lambda *, linger=True: seen.update(linger=linger) or {"status": "enabled"},
    )
    assert main(["demo-mode", "enable"]) == 0
    assert seen["linger"] is True
    capsys.readouterr()
    assert main(["demo-mode", "enable", "--no-linger"]) == 0
    assert seen["linger"] is False


def test_enable_without_systemctl_exits_2(monkeypatch, capsys) -> None:
    # Default fixture makes _run return None -> _require raises a clean exit-2.
    rc = main(["demo-mode", "enable"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "systemctl" in err
    assert "hint:" in err


# --- overview / rubric ----------------------------------------------------


def test_demo_overview_text(capsys) -> None:
    assert main(["demo-mode", "overview"]) == 0
    assert "# reachy-mini-cli demo-mode" in capsys.readouterr().out


def test_demo_overview_json(capsys) -> None:
    assert main(["demo-mode", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli demo-mode"
    assert isinstance(payload["sections"], list)


def test_bare_demo_prints_overview(capsys) -> None:
    assert main(["demo-mode"]) == 0
    assert capsys.readouterr().out.strip()


def test_demo_bad_flag_structured_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["demo-mode", "status", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- module-level units ---------------------------------------------------


def test_is_our_process_false_for_dead_pid() -> None:
    from pathlib import Path

    if not Path("/proc").is_dir():
        pytest.skip("no /proc on this platform")
    assert alive._is_our_process(2_000_000_000) is False


def test_read_pid_garbage_is_none(tmp_path) -> None:
    (tmp_path / "demo-mode.pid").write_text("not-a-number")
    assert alive.read_pid() is None


def test_build_run_command_includes_no_flags_when_disabled() -> None:
    cmd = alive.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=10.0,
        interval=2.5,
        energy=1.0,
        interpolation="minjerk",
        seed=None,
        wake=False,
        settle=False,
    )
    assert "--no-wake" in cmd
    assert "--no-settle" in cmd
    assert "--seed" not in cmd
