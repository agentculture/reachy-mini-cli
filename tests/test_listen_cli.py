"""Tests for the ``listen`` noun group and the ``reachy.motion.supervisor``.

No real robot, daemon, or background process is involved: the motion loop runs
against a fake transport, and the supervisor's subprocess (``subprocess.Popen``),
liveness (``os.kill`` / ``is_alive``), grace sleep, and HTTP health check are
monkeypatched. State is pinned to a tmp dir via ``REACHY_STATE_DIR``. (The motion
queue, executor, and listen producer are unit-tested in ``tests/test_motion.py``;
here we cover the CLI wiring and the process supervisor.)
"""

from __future__ import annotations

import json
import signal

import pytest

from reachy.cli import main
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
    tr = _FakeTransport(doa={"angle": 0.0, "speech_detected": False})  # sound on the left
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda args: tr)
    rc = main(["listen", "run", "--json", "--dwell", "0", "--deadband", "0", "--max-ticks", "5"])
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert any(e.get("action") for e in events)  # turned toward the sound
    assert any((e.get("yaw") or 0.0) > 0 for e in events)  # left -> +yaw


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
    rc = main(["listen", "start"])
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
