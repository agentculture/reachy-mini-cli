"""CLI wiring for ``agent embody start/stop/restart/status`` (task t12).

``tests/test_embody_supervisor.py`` pins the supervisor module's own
behaviour (spawn/signal/pid-file mechanics, the h7/h16/h26 proofs);
``tests/test_agent_embody.py`` pins the composition root the foreground
``agent embody`` verb wires. This module is the missing middle layer: the
argparse plumbing from ``reachy.cli.main([...])`` down to
``reachy.embody.supervisor``, exercised exactly like every sibling
``test_*_cli.py`` (``sleep``, ``vision``) drives its own supervisor's CLI
surface — ``subprocess.Popen`` / ``is_alive`` mocked, nothing real spawned.

Pins, explicitly:

* **h17** — one command each way: ``agent embody start`` and
  ``agent embody stop`` are each a SINGLE CLI invocation that does the whole
  job (idempotent start, escalating stop).
* Bare ``agent embody`` is UNCHANGED by this task (a regression guard: t11
  pinned ``args.func is cmd_agent_embody`` / ``args.agent_command ==
  "embody"`` for the bare form in ``tests/test_agent_embody.py`` — this
  module additionally proves ``embody start`` / ``stop`` / ``restart`` /
  ``status`` route to the NEW functions without disturbing that).
* Flags plumb end-to-end: ``embody start --media-profile bench`` reaches
  ``build_run_command`` and the spawned argv, not just the CLI's own
  ``args`` namespace.
"""

from __future__ import annotations

import pytest

import reachy.cli._commands.agent as agent_mod
from reachy.cli import _build_parser, main


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# ---------------------------------------------------------------------------
# Fakes (mirrors tests/test_sleep_cli.py's spawn spy)
# ---------------------------------------------------------------------------


class _SpyPopen:
    returncode = None
    pid = 4242

    def __init__(self, cmd, **kwargs) -> None:  # test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs

    def poll(self):
        return self.returncode


def _spy_popen(box):
    def _popen(cmd, **kwargs):  # test shim
        proc = _SpyPopen(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


# ---------------------------------------------------------------------------
# Regression: bare `agent embody` is unchanged by this task
# ---------------------------------------------------------------------------


def test_bare_agent_embody_still_routes_to_the_foreground_command() -> None:
    """t11's own pin, restated here as a regression guard for this task's edit."""
    args = _build_parser().parse_args(["agent", "embody", "--max-turns=1"])
    assert args.func is agent_mod.cmd_agent_embody
    assert args.agent_command == "embody"


def test_agent_embody_start_stop_restart_status_are_registered() -> None:
    parser = _build_parser()
    start_args = parser.parse_args(["agent", "embody", "start"])
    assert start_args.func is agent_mod.cmd_agent_embody_start
    assert start_args.agent_command == "embody"
    assert start_args.embody_command == "start"

    stop_args = parser.parse_args(["agent", "embody", "stop"])
    assert stop_args.func is agent_mod.cmd_agent_embody_stop
    assert stop_args.embody_command == "stop"

    restart_args = parser.parse_args(["agent", "embody", "restart"])
    assert restart_args.func is agent_mod.cmd_agent_embody_restart
    assert restart_args.embody_command == "restart"

    status_args = parser.parse_args(["agent", "embody", "status"])
    assert status_args.func is agent_mod.cmd_agent_embody_status
    assert status_args.embody_command == "status"


# ---------------------------------------------------------------------------
# h17 — one command each way
# ---------------------------------------------------------------------------


def test_h17_start_is_one_command(monkeypatch, tmp_path) -> None:
    """A single ``agent embody start`` invocation spawns the layer detached."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _spy_popen(procs))

    rc = main(["agent", "embody", "start"])
    assert rc == 0
    assert len(procs) == 1
    assert (tmp_path / "embody.pid").read_text().strip() == "4242"
    assert procs[0].cmd[1:5] == ["-m", "reachy", "agent", "embody"]


def test_h17_stop_is_one_command(monkeypatch, tmp_path) -> None:
    """A single ``agent embody stop`` invocation ends the tracked layer."""
    from reachy.embody import supervisor as embody_supervisor

    (tmp_path / "embody.pid").write_text("4242")
    monkeypatch.setattr(embody_supervisor, "is_alive", lambda pid: True)
    monkeypatch.setattr(embody_supervisor, "_is_our_process", lambda pid: True)
    killed: list = []
    monkeypatch.setattr("os.kill", lambda pid, sig: killed.append((pid, sig)))
    monkeypatch.setattr(embody_supervisor, "_wait_gone", lambda pid, timeout: True)

    rc = main(["agent", "embody", "stop"])
    assert rc == 0
    assert killed, "stop must have signalled the tracked pid"
    assert not (tmp_path / "embody.pid").exists()


def test_h17_start_then_stop_json_round_trip(monkeypatch, tmp_path, capsys) -> None:
    """The full one-command-each-way loop, asserting the --json result shape."""
    from reachy.embody import supervisor as embody_supervisor

    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _spy_popen(procs))

    rc = main(["agent", "embody", "start", "--json"])
    assert rc == 0
    import json

    started = json.loads(capsys.readouterr().out)
    assert started["status"] == "started"
    assert started["pid"] == 4242

    monkeypatch.setattr(embody_supervisor, "is_alive", lambda pid: True)
    monkeypatch.setattr(embody_supervisor, "_is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)
    monkeypatch.setattr(embody_supervisor, "_wait_gone", lambda pid, timeout: True)

    rc = main(["agent", "embody", "stop", "--json"])
    assert rc == 0
    stopped = json.loads(capsys.readouterr().out)
    assert stopped["status"] == "stopped"
    assert stopped["pid"] == 4242


# ---------------------------------------------------------------------------
# Flags plumb end-to-end into the spawned argv
# ---------------------------------------------------------------------------


def test_start_forwards_media_profile_end_to_end(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _spy_popen(procs))

    rc = main(["agent", "embody", "start", "--media-profile", "bench"])
    assert rc == 0
    assert "--media-profile" in procs[0].cmd
    assert procs[0].cmd[procs[0].cmd.index("--media-profile") + 1] == "bench"


def test_start_forwards_feed_and_turn_interval_end_to_end(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _spy_popen(procs))

    rc = main(["agent", "embody", "start", "--feed", "/tmp/runtime.feed", "--turn-interval", "1.5"])
    assert rc == 0
    cmd = procs[0].cmd
    assert cmd[cmd.index("--feed") + 1] == "/tmp/runtime.feed"
    assert cmd[cmd.index("--turn-interval") + 1] == "1.5"


def test_restart_forwards_mute_during_playback_end_to_end(monkeypatch, tmp_path) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _spy_popen(procs))

    rc = main(["agent", "embody", "restart", "--mute-during-playback"])
    assert rc == 0
    assert "--mute-during-playback" in procs[0].cmd


def test_stop_timeout_flag_reaches_the_supervisor(monkeypatch, tmp_path) -> None:
    from reachy.embody import supervisor as embody_supervisor

    (tmp_path / "embody.pid").write_text("4242")
    monkeypatch.setattr(embody_supervisor, "is_alive", lambda pid: True)
    monkeypatch.setattr(embody_supervisor, "_is_our_process", lambda pid: True)
    monkeypatch.setattr("os.kill", lambda pid, sig: None)

    captured: list[float] = []

    def _spy_wait_gone(pid, timeout):
        captured.append(timeout)
        return True

    monkeypatch.setattr(embody_supervisor, "_wait_gone", _spy_wait_gone)

    rc = main(["agent", "embody", "stop", "--timeout", "3.5"])
    assert rc == 0
    assert captured[0] == 3.5


# ---------------------------------------------------------------------------
# status
# ---------------------------------------------------------------------------


def test_status_reports_stopped_with_no_pid(tmp_path, capsys) -> None:
    import json

    rc = main(["agent", "embody", "status", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["process"] == "stopped"
    assert data["pid"] is None
    assert "embody.log" in data["log"]


def test_status_reports_running(monkeypatch, tmp_path, capsys) -> None:
    import json

    from reachy.embody import supervisor as embody_supervisor

    (tmp_path / "embody.pid").write_text("4242")
    monkeypatch.setattr(embody_supervisor, "is_alive", lambda pid: True)

    rc = main(["agent", "embody", "status", "--json"])
    assert rc == 0
    data = json.loads(capsys.readouterr().out)
    assert data["process"] == "running"
    assert data["pid"] == 4242


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
