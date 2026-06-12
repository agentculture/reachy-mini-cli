"""Tests for ``reachy.sleep.supervisor`` — the sleep-noun background-process supervisor.

No real process is spawned: ``subprocess.Popen``, liveness (``is_alive``), grace
sleep, and OS signals are monkeypatched. State is pinned to a tmp dir via
``REACHY_STATE_DIR`` (mirrors ``tests/test_listen_cli.py`` / ``tests/test_think.py``).

Acceptance criteria verified here:
1. PID/log paths are ``sleep.pid`` / ``sleep.log`` and do NOT collide with
   ``listen.pid`` / ``think.pid``.
2. ``build_run_command`` produces ``python -m reachy sleep run …`` argv.
3. ``start`` / ``stop`` / ``restart`` / ``status`` behave correctly (idempotency,
   SIGTERM→SIGKILL, PID-reuse guard, stale-pid clearing).
"""

from __future__ import annotations

import signal

import pytest

from reachy.sleep import supervisor


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# ---------------------------------------------------------------------------
# Path-collision acceptance criteria
# ---------------------------------------------------------------------------


def test_pid_file_is_sleep_pid(tmp_path) -> None:
    """sleep.pid must not collide with listen.pid."""
    assert supervisor.pid_file() == tmp_path / "sleep.pid"


def test_log_file_is_sleep_log(tmp_path) -> None:
    """sleep.log must not collide with listen.log or think.log."""
    assert supervisor.log_file() == tmp_path / "sleep.log"


def test_pid_file_differs_from_listen(tmp_path) -> None:
    assert supervisor.pid_file() != tmp_path / "listen.pid"


def test_pid_file_differs_from_think(tmp_path) -> None:
    assert supervisor.pid_file() != tmp_path / "think.pid"


def test_log_file_differs_from_listen(tmp_path) -> None:
    assert supervisor.log_file() != tmp_path / "listen.log"


def test_log_file_differs_from_think(tmp_path) -> None:
    assert supervisor.log_file() != tmp_path / "think.log"


# ---------------------------------------------------------------------------
# build_run_command
# ---------------------------------------------------------------------------


def test_build_run_command_core_argv() -> None:
    """Must produce ``python -m reachy sleep run`` with transport/base-url/timeout."""
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=10.0,
    )
    assert cmd[1:5] == ["-m", "reachy", "sleep", "run"]
    assert "--transport" in cmd
    assert cmd[cmd.index("--transport") + 1] == "http"
    assert "--base-url" in cmd
    assert cmd[cmd.index("--base-url") + 1] == "http://localhost:8000"
    assert "--timeout" in cmd
    assert cmd[cmd.index("--timeout") + 1] == "10.0"


def test_build_run_command_sdk_transport() -> None:
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=5.0,
    )
    assert cmd[1:5] == ["-m", "reachy", "sleep", "run"]
    assert cmd[cmd.index("--transport") + 1] == "sdk"


def test_build_run_command_optional_ticks() -> None:
    """--ticks is forwarded when provided."""
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=5.0,
        ticks=100,
    )
    assert "--ticks" in cmd
    assert cmd[cmd.index("--ticks") + 1] == "100"


def test_build_run_command_no_optional_flags_when_none() -> None:
    """Optional flags absent when their values are None."""
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=5.0,
    )
    assert "--ticks" not in cmd
    assert "--idle-timeout" not in cmd


def test_build_run_command_idle_timeout() -> None:
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=5.0,
        idle_timeout=30.0,
    )
    assert "--idle-timeout" in cmd
    assert cmd[cmd.index("--idle-timeout") + 1] == "30.0"


def test_build_run_command_no_audio_wake_forwarded() -> None:
    """``--no-audio-wake`` appears in argv when ``no_audio_wake=True``."""
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=5.0,
        no_audio_wake=True,
    )
    assert "--no-audio-wake" in cmd


def test_build_run_command_no_audio_wake_absent_by_default() -> None:
    """``--no-audio-wake`` must NOT appear when not set (default ``False``)."""
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=5.0,
    )
    assert "--no-audio-wake" not in cmd


def test_build_run_command_no_audio_wake_false_also_absent() -> None:
    """Explicit ``no_audio_wake=False`` produces the same clean argv as omitting it."""
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=5.0,
        no_audio_wake=False,
    )
    assert "--no-audio-wake" not in cmd


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 9191

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


# ---------------------------------------------------------------------------
# start
# ---------------------------------------------------------------------------


def test_start_spawns_sleep_run(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    result = supervisor.start(transport="http")
    assert result["status"] == "started"
    assert result["pid"] == 9191
    assert (tmp_path / "sleep.pid").read_text().strip() == "9191"
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "sleep", "run"]
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_forwards_no_audio_wake(monkeypatch, tmp_path) -> None:
    """``start(no_audio_wake=True)`` spawns a command that contains ``--no-audio-wake``."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    supervisor.start(transport="sdk", no_audio_wake=True)
    assert "--no-audio-wake" in procs[0].cmd


def test_start_omits_no_audio_wake_by_default(monkeypatch, tmp_path) -> None:
    """``start()`` without ``no_audio_wake`` produces a command without the flag."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    supervisor.start(transport="sdk")
    assert "--no-audio-wake" not in procs[0].cmd


def test_restart_forwards_no_audio_wake(monkeypatch, tmp_path) -> None:
    """``restart(no_audio_wake=True)`` passes the flag through to the spawned command."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    result = supervisor.restart(transport="sdk", no_audio_wake=True)
    assert result["status"] == "started"
    assert "--no-audio-wake" in procs[0].cmd


def test_start_idempotent_when_already_running(monkeypatch, tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("9191")
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    result = supervisor.start()
    assert result["status"] == "already-running"
    assert result["pid"] == 9191


def test_start_replaces_stale_pid_and_spawns(monkeypatch, tmp_path) -> None:
    """A stale pid (dead process) is cleared and a new process is spawned."""
    (tmp_path / "sleep.pid").write_text("9191")
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: False)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    result = supervisor.start(transport="http")
    assert result["status"] == "started"
    assert len(procs) == 1


def test_start_reports_exited_when_process_dies_in_grace_window(monkeypatch, tmp_path) -> None:
    """If the spawned process exits during the grace window, status is 'exited'."""

    class _ExitedPopen(_FakePopen):
        returncode = 1

    def _popen_exited(cmd, **kwargs):
        return _ExitedPopen(cmd, **kwargs)

    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setattr("subprocess.Popen", _popen_exited)
    result = supervisor.start(transport="http")
    assert result["status"] == "exited"
    assert result["exit_code"] == 1
    # The pid file must NOT linger after a failed start — otherwise status/stop
    # would report a stale pid (regression: pid was written unconditionally).
    assert not (tmp_path / "sleep.pid").exists()
    assert supervisor.read_pid() is None


# ---------------------------------------------------------------------------
# stop
# ---------------------------------------------------------------------------


def test_stop_when_not_running_returns_not_running() -> None:
    result = supervisor.stop()
    assert result["status"] == "not running"
    assert "no tracked sleep pid" in result["note"]


def test_stop_clears_stale_pid(tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("9191")
    # is_alive is NOT monkeypatched → falls through to real os.kill(9191, 0)
    # which should fail (pid not ours) — but we mock is_alive for cleanliness
    import reachy.sleep.supervisor as sup

    original = sup.is_alive

    def fake_alive(pid):
        return False

    sup.is_alive = fake_alive
    try:
        result = supervisor.stop()
    finally:
        sup.is_alive = original
    assert result["status"] == "not running"
    assert not (tmp_path / "sleep.pid").exists()


def test_stop_sigterm(monkeypatch, tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("9191")
    state = {"alive": True}
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.sleep.supervisor._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    result = supervisor.stop()
    assert result["status"] == "stopped"
    assert result["signal"] == "SIGTERM"
    assert killed == [(9191, signal.SIGTERM)]
    assert not (tmp_path / "sleep.pid").exists()


def test_stop_sigkill_when_sigterm_ignored(monkeypatch, tmp_path) -> None:
    """If process survives SIGTERM (timeout), SIGKILL is sent."""
    (tmp_path / "sleep.pid").write_text("9191")
    killed: list = []
    wait_calls = {"n": 0}

    # First _wait_gone call (after SIGTERM) returns False (timeout, still alive).
    # Second _wait_gone call (after SIGKILL) returns True (process gone).
    def _fake_wait_gone(pid, timeout):
        wait_calls["n"] += 1
        return wait_calls["n"] >= 2

    monkeypatch.setattr("reachy.sleep.supervisor._wait_gone", _fake_wait_gone)
    monkeypatch.setattr("reachy.sleep.supervisor._is_our_process", lambda pid: True)
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
    result = supervisor.stop()
    assert result["status"] == "stopped"
    assert result["signal"] == "SIGKILL"
    assert any(sig == signal.SIGKILL for _, sig in killed)


def test_stop_pid_reuse_guard(monkeypatch, tmp_path) -> None:
    """If tracked pid is no longer our process, stop must NOT signal it."""
    (tmp_path / "sleep.pid").write_text("9191")
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.sleep.supervisor._is_our_process", lambda pid: False)
    monkeypatch.setattr("os.kill", _no_spawn)  # must not be called
    result = supervisor.stop()
    assert result["status"] == "not running"
    assert "reused" in result["note"]
    assert not (tmp_path / "sleep.pid").exists()


# ---------------------------------------------------------------------------
# restart
# ---------------------------------------------------------------------------


def test_restart_stops_then_starts(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    # No prior pid -> stop is a no-op, then start spawns.
    result = supervisor.restart(transport="http")
    assert result["status"] == "started"
    assert "restarted_from" in result
    assert procs[0].cmd[1:5] == ["-m", "reachy", "sleep", "run"]


def test_restart_reports_prior_stop_status(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    result = supervisor.restart()
    # No prior pid -> stop returned "not running"
    assert result["restarted_from"] == "not running"


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_stopped_when_no_pid(tmp_path) -> None:
    result = supervisor.status()
    assert result["process"] == "stopped"
    assert result["pid"] is None
    assert "sleep.log" in result["log"]


def test_status_running(monkeypatch, tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("9191")
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: True)
    result = supervisor.status()
    assert result["process"] == "running"
    assert result["pid"] == 9191
    assert "sleep.log" in result["log"]


def test_status_stale(monkeypatch, tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("9191")
    monkeypatch.setattr("reachy.sleep.supervisor.is_alive", lambda pid: False)
    result = supervisor.status()
    assert result["process"] == "stale"
    assert result["pid"] == 9191


def test_status_includes_log_path(tmp_path) -> None:
    result = supervisor.status()
    assert result["log"] == str(tmp_path / "sleep.log")


# ---------------------------------------------------------------------------
# read_pid edge cases
# ---------------------------------------------------------------------------


def test_read_pid_absent_returns_none() -> None:
    assert supervisor.read_pid() is None


def test_read_pid_bad_content_returns_none(tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("not-a-number")
    assert supervisor.read_pid() is None


def test_read_pid_valid(tmp_path) -> None:
    (tmp_path / "sleep.pid").write_text("12345\n")
    assert supervisor.read_pid() == 12345
