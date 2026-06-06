"""Tests for the ``daemon`` noun group and the daemon process module.

No real ``reachy-mini-daemon`` is spawned and no daemon is contacted: the
subprocess (``subprocess.Popen``), process-liveness (``os.kill`` / ``is_alive``)
and HTTP health check (``health_ok`` / ``urllib``) are all monkeypatched. Every
test runs against an isolated state dir via ``REACHY_STATE_DIR`` so the real
``~/.local/state/reachy`` is never touched.
"""

from __future__ import annotations

import json
import signal
import urllib.error

import pytest

from reachy import daemon
from reachy.cli import main
from reachy.cli._errors import CliError


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    """Pin daemon bookkeeping into a throwaway dir and clear ambient env."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_DAEMON_CMD", raising=False)
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)


class _FakePopen:
    """Stand-in for ``subprocess.Popen``: alive (``poll() is None``) by default."""

    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 4242

    def poll(self):
        return self.returncode


class _DeadPopen(_FakePopen):
    """A process that has already exited with a non-zero code."""

    returncode = 1


def _popen_factory(box, cls=_FakePopen):
    def _popen(cmd, **kwargs):  # noqa: ANN001 - test shim
        proc = cls(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


# --- start ----------------------------------------------------------------


def test_start_no_wait_spawns_and_records_pid(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "fake-daemon")
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = main(["daemon", "start", "--no-wait"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out
    assert "pid: 4242" in out
    assert (tmp_path / "daemon.pid").read_text().strip() == "4242"
    # Detached, output redirected, stdin closed.
    assert procs[0].cmd == ["fake-daemon"]
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_waits_until_healthy(monkeypatch, capsys) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "fake-daemon")
    calls = {"n": 0}

    def _health(*a, **k):
        calls["n"] += 1
        return calls["n"] > 1  # pre-spawn foreign check False, then healthy

    monkeypatch.setattr("reachy.daemon.health_ok", _health)
    monkeypatch.setattr("subprocess.Popen", _popen_factory([]))

    rc = main(["daemon", "start"])
    assert rc == 0
    assert "healthy: True" in capsys.readouterr().out


def test_start_idempotent_when_tracked_alive(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: True)

    def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise AssertionError("must not spawn when a daemon already runs")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)

    rc = main(["daemon", "start"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already-running" in out
    assert "pid: 4242" in out


def test_start_detects_foreign_daemon(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: True)

    def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise AssertionError("must not spawn when a foreign daemon answers")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)

    rc = main(["daemon", "start"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already-running" in out
    assert "not started by this CLI" in out


def test_start_missing_binary_exit_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    monkeypatch.setattr("shutil.which", lambda name: None)

    rc = main(["daemon", "start"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "reachy-cli[daemon]" in err


def test_start_forwards_extra_args_after_dashdash(monkeypatch) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "fake-daemon")
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = main(["daemon", "start", "--no-wait", "--", "--sim", "--fastapi-port", "9000"])
    assert rc == 0
    assert procs[0].cmd == ["fake-daemon", "--sim", "--fastapi-port", "9000"]


def test_start_reports_exit_on_startup_crash(monkeypatch, capsys) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "fake-daemon")
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    monkeypatch.setattr("subprocess.Popen", _popen_factory([], cls=_DeadPopen))

    rc = main(["daemon", "start", "--wait-timeout", "0.05"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: exited" in out
    assert "exit_code: 1" in out


def test_start_idempotent_path_honours_wait(monkeypatch, tmp_path, capsys) -> None:
    # Tracked daemon is up but not answering yet; --wait should poll there too.
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    calls = {"n": 0}

    def _health(*a, **k):
        calls["n"] += 1
        return calls["n"] > 1  # first check not-ready, then healthy

    monkeypatch.setattr("reachy.daemon.health_ok", _health)

    def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise AssertionError("must not spawn for an already-tracked daemon")

    monkeypatch.setattr("subprocess.Popen", _no_spawn)

    rc = main(["daemon", "start"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "already-running" in out
    assert "healthy: True" in out


def test_start_wraps_popen_oserror(monkeypatch, capsys) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "fake-daemon")
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)

    def _boom(cmd, **kwargs):  # noqa: ANN001 - test shim
        raise OSError("Exec format error")

    monkeypatch.setattr("subprocess.Popen", _boom)

    rc = main(["daemon", "start", "--no-wait"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "failed to launch the daemon" in err
    assert "hint:" in err


# --- stop -----------------------------------------------------------------


def test_stop_when_not_running(capsys) -> None:
    rc = main(["daemon", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_stop_clears_stale_pid(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: False)

    rc = main(["daemon", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out
    assert not (tmp_path / "daemon.pid").exists()


def test_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    state = {"alive": True}
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.daemon._is_our_daemon", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False  # dies on SIGTERM

    monkeypatch.setattr("os.kill", _kill)

    rc = main(["daemon", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out
    assert "SIGTERM" in out
    assert killed == [(4242, signal.SIGTERM)]
    assert not (tmp_path / "daemon.pid").exists()


def test_stop_escalates_to_sigkill_then_dies(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    state = {"alive": True}
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.daemon._is_our_daemon", lambda pid: True)
    sigs: list = []

    def _kill(pid, sig):
        sigs.append(sig)
        if sig == signal.SIGKILL:
            state["alive"] = False  # SIGTERM ignored; SIGKILL lands

    monkeypatch.setattr("os.kill", _kill)

    rc = main(["daemon", "stop", "--timeout", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out
    assert "SIGKILL" in out
    assert signal.SIGTERM in sigs and signal.SIGKILL in sigs
    assert not (tmp_path / "daemon.pid").exists()


def test_stop_refuses_reused_pid(monkeypatch, tmp_path, capsys) -> None:
    # is_alive is True but the pid was recycled by an unrelated process.
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon._is_our_daemon", lambda pid: False)
    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append(sig))

    rc = main(["daemon", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "not running" in out
    assert "reused" in out
    assert killed == []  # never signalled the recycled pid
    assert not (tmp_path / "daemon.pid").exists()


def test_stop_fails_when_unkillable(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon._is_our_daemon", lambda pid: True)
    monkeypatch.setattr("reachy.daemon._wait_gone", lambda pid, timeout: False)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)  # signals do nothing

    rc = main(["daemon", "stop", "--timeout", "0"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "still alive after SIGKILL" in err
    assert (tmp_path / "daemon.pid").exists()  # kept — process is still there


# --- status ---------------------------------------------------------------


def test_status_running_healthy(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: True)

    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running"
    assert payload["pid"] == 4242
    assert payload["http"] == "healthy"


def test_status_stale_pid(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: False)
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)

    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "stale"
    assert payload["http"] == "unreachable"


def test_status_stopped_when_no_pid(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    rc = main(["daemon", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "process: stopped" in out


# --- overview / rubric ----------------------------------------------------


def test_daemon_overview_text(capsys) -> None:
    assert main(["daemon", "overview"]) == 0
    assert "# reachy-mini-cli daemon" in capsys.readouterr().out


def test_daemon_overview_json(capsys) -> None:
    assert main(["daemon", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli daemon"
    assert isinstance(payload["sections"], list)


def test_bare_daemon_prints_overview(capsys) -> None:
    assert main(["daemon"]) == 0
    assert capsys.readouterr().out.strip()


def test_daemon_bad_flag_structured_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["daemon", "status", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- module-level units ---------------------------------------------------


def test_health_ok_true_on_2xx(monkeypatch) -> None:
    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def getcode(self):
            return 200

    monkeypatch.setattr("urllib.request.urlopen", lambda req, timeout=None: _Resp())
    assert daemon.health_ok("http://localhost:8000", 1.0) is True


def test_health_ok_false_on_error(monkeypatch) -> None:
    def _boom(req, timeout=None):  # noqa: ANN001 - test shim
        raise urllib.error.URLError("refused")

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    assert daemon.health_ok("http://localhost:8000", 1.0) is False


def test_health_ok_false_on_bad_scheme() -> None:
    assert daemon.health_ok("ftp://nope", 1.0) is False


def test_resolve_daemon_cmd_override_resolves_program(monkeypatch) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "mybin --x")
    monkeypatch.setattr("shutil.which", lambda n: "/usr/bin/mybin" if n == "mybin" else None)
    assert daemon.resolve_daemon_cmd() == ["/usr/bin/mybin", "--x"]


def test_resolve_daemon_cmd_empty_override_errors(monkeypatch) -> None:
    monkeypatch.setenv("REACHY_DAEMON_CMD", "   ")
    with pytest.raises(CliError):
        daemon.resolve_daemon_cmd()


def test_read_pid_garbage_is_none(tmp_path) -> None:
    (tmp_path / "daemon.pid").write_text("not-a-number")
    assert daemon.read_pid() is None


def test_is_our_daemon_false_for_dead_pid() -> None:
    from pathlib import Path

    if not Path("/proc").is_dir():
        pytest.skip("no /proc on this platform")
    # A pid that cannot exist -> /proc read fails -> not our daemon.
    assert daemon._is_our_daemon(2_000_000_000) is False


# --- is_robot_live liveness probe ------------------------------------------


def test_is_robot_live_true_when_http_healthy(monkeypatch) -> None:
    """is_robot_live returns True immediately when the HTTP health endpoint answers."""
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: True)
    assert daemon.is_robot_live() is True


def test_is_robot_live_false_when_http_unreachable(monkeypatch) -> None:
    """is_robot_live returns False when the health endpoint does not answer."""
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)
    assert daemon.is_robot_live() is False


def test_is_robot_live_reflects_restart(monkeypatch, tmp_path) -> None:
    """Simulate a daemon restart: down then up.

    A stale PID file is present throughout.  The probe must NOT trust the cached
    PID state — each call re-checks the HTTP health endpoint independently.
    """
    # Write a stale PID file (daemon is down, but file still exists).
    (tmp_path / "daemon.pid").write_text("4242")
    # is_alive claims the pid is gone (restart tore down the old process).
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: False)

    call_count = {"n": 0}

    def _health(*a, **k):
        call_count["n"] += 1
        # First call: daemon is still down (just restarting).
        # Second call: daemon is back up.
        return call_count["n"] > 1

    monkeypatch.setattr("reachy.daemon.health_ok", _health)

    # First probe: must report down even though PID file exists.
    assert daemon.is_robot_live() is False

    # Second probe: must report live — fresh HTTP check, not a cached/PID result.
    assert daemon.is_robot_live() is True

    # Exactly two health probes were made (one per is_robot_live call).
    assert call_count["n"] == 2


def test_is_robot_live_no_stale_cache(monkeypatch) -> None:
    """is_robot_live never caches: alternating up/down is reflected correctly."""
    states = [True, False, True]
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: states.pop(0))
    assert daemon.is_robot_live() is True
    assert daemon.is_robot_live() is False
    assert daemon.is_robot_live() is True


def test_is_robot_live_ignores_pid_file(monkeypatch, tmp_path) -> None:
    """A stale PID file must not cause is_robot_live to report True when HTTP is down."""
    (tmp_path / "daemon.pid").write_text("9999")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)  # pid "alive"
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)  # but HTTP down

    # The robot is NOT actually reachable — HTTP says no.
    assert daemon.is_robot_live() is False


def test_is_robot_live_custom_url_and_timeout(monkeypatch) -> None:
    """is_robot_live forwards base_url and timeout to health_ok."""
    captured: dict = {}

    def _health(base_url, timeout):
        captured["base_url"] = base_url
        captured["timeout"] = timeout
        return True

    monkeypatch.setattr("reachy.daemon.health_ok", _health)
    result = daemon.is_robot_live(base_url="http://robot.local:8080", timeout=2.5)
    assert result is True
    assert captured["base_url"] == "http://robot.local:8080"
    assert captured["timeout"] == 2.5


def test_status_includes_live_field(monkeypatch, tmp_path, capsys) -> None:
    """daemon.status result now includes a 'live' key from is_robot_live."""
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: True)

    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "live" in payload
    assert payload["live"] is True


def test_status_live_false_when_http_down(monkeypatch, tmp_path, capsys) -> None:
    """live=False in status when HTTP health is not reachable."""
    (tmp_path / "daemon.pid").write_text("4242")
    monkeypatch.setattr("reachy.daemon.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.daemon.health_ok", lambda *a, **k: False)

    rc = main(["daemon", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["live"] is False
