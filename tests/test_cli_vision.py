"""Tests for the ``vision`` noun group and the ``reachy.vision.supervisor``.

No real robot, daemon, camera, or background process is involved: the vision
loop runs against a fake transport (yielding synthetic frames), and the
supervisor's subprocess (``subprocess.Popen``), liveness (``os.kill`` /
``is_alive``), grace sleep, and HTTP health check are monkeypatched. State is
pinned to a tmp dir via ``REACHY_STATE_DIR``. (VisionProducer unit tests live in
``tests/test_vision_producer.py``; here we cover the CLI wiring and the process
supervisor.)
"""

from __future__ import annotations

import json
import signal

import numpy as np
import pytest

from reachy.cli import _build_parser, main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.vision import supervisor
from reachy.vision.producer import VisionParams


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# --- fake transports ------------------------------------------------------


class _FakeTransport:
    """Records gotos; returns a blank frame from get_frame; supports camera_specs."""

    name = "fake"

    def __init__(self, frame=None, specs=None, move_error=None) -> None:
        self.gotos: list[dict] = []
        self._frame = frame if frame is not None else np.zeros((64, 64, 3), dtype=np.uint8)
        self._specs = specs or {"width": 640, "height": 480, "name": "test-cam"}
        self._move_error = move_error

    def move_goto(self, **kwargs) -> object:  # noqa: ANN003 - test shim
        if self._move_error is not None:
            raise self._move_error
        self.gotos.append(kwargs)
        return {"uuid": "x"}

    def get_frame(self) -> object:
        return self._frame

    def camera_specs(self) -> object:
        return self._specs


class _NoFrameTransport(_FakeTransport):
    """Raises CliError(exit-2) from get_frame — simulates missing local camera."""

    def get_frame(self) -> object:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="camera frames are not available over the http transport",
            remediation=(
                "frames come only via the local SDK path: install the sdk extra "
                "(pip install 'reachy-mini-cli[sdk]', or '[daemon]') and run on the "
                "robot with --transport sdk"
            ),
        )


# --- CLI: overview --------------------------------------------------------


def test_vision_overview_text(capsys) -> None:
    assert main(["vision", "overview"]) == 0
    out = capsys.readouterr().out
    assert "# reachy-mini-cli vision" in out
    assert out.strip()


def test_vision_overview_json(capsys) -> None:
    assert main(["vision", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli vision"
    assert payload["sections"]


def test_bare_vision_prints_overview(capsys) -> None:
    assert main(["vision"]) == 0
    assert capsys.readouterr().out.strip()


def test_vision_help_exits_zero(capsys) -> None:
    """``reachy vision --help`` exits 0 and shows usage."""
    with pytest.raises(SystemExit) as exc:
        main(["vision", "--help"])
    assert exc.value.code == 0


# --- CLI: run -------------------------------------------------------------


def test_run_centers_then_settles(monkeypatch, capsys) -> None:
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    rc = main(["vision", "run", "--max-ticks", "3"])
    assert rc == 0
    # First goto is the preflight center; last is the settle-to-center.
    assert tr.gotos[0]["head"]["yaw"] == 0.0
    assert tr.gotos[0]["interpolation"] == "minjerk"
    assert tr.gotos[-1]["head"]["yaw"] == 0.0


def test_run_installs_stop_handlers_and_settles_on_interrupt(monkeypatch) -> None:
    """`vision run` installs SIGTERM/SIGINT handlers and still eases the head back
    to center if the loop is interrupted — so `vision stop`/Ctrl-C never leave the
    head off-center (the supervisor's "eases back to center" contract)."""
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    seen: dict = {}
    monkeypatch.setattr(
        "reachy.cli._commands.vision.install_stop_handlers",
        lambda stop: seen.__setitem__("stop", stop),  # returns None -> restore is a no-op
    )

    def _boom(self, **kwargs):  # noqa: ANN003 - test shim
        raise RuntimeError("interrupted mid-loop")

    monkeypatch.setattr("reachy.vision.producer.VisionProducer.run", _boom)
    main(["vision", "run", "--max-ticks", "1"])
    # handlers were installed against a real stop flag ...
    assert isinstance(seen.get("stop"), dict) and "flag" in seen["stop"]
    # ... and the settle-to-center still ran despite the interruption (preflight + settle).
    assert len(tr.gotos) >= 2
    assert tr.gotos[-1]["head"]["yaw"] == 0.0


def test_run_json_exits_zero_with_blank_frames(monkeypatch, capsys) -> None:
    # A blank (all-zero) frame stream produces no actions (motion centred, deadband holds),
    # but the loop must exit 0 and emit valid JSON for each event (if any).
    tr = _FakeTransport()  # returns a blank frame every tick
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    rc = main(["vision", "run", "--json", "--max-ticks", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    for line in out.splitlines():
        if line.strip():
            json.loads(line)  # every stdout line must be valid JSON


def test_run_emits_action_on_offcenter_motion(monkeypatch, capsys) -> None:
    # Alternating between a dark frame and a right-half-bright frame produces
    # off-centre motion that triggers a head turn.  We inject a fast 'now' into
    # the producer so the busy_until guard expires quickly without real sleeping.
    frame_a = np.zeros((64, 64, 3), dtype=np.uint8)
    frame_b = np.zeros((64, 64, 3), dtype=np.uint8)
    frame_b[:, 32:, :] = 200  # right half bright → direction ~+0.53

    call_count = {"n": 0}

    class _AltTransport(_FakeTransport):
        def get_frame(self):
            f = frame_a if call_count["n"] % 2 == 0 else frame_b
            call_count["n"] += 1
            return f

    tr = _AltTransport()

    # Patch VisionProducer.run to inject a fast 'now' (advances 10 s per tick)
    # so busy_until and hold_until never block the action dispatch.
    import reachy.vision.producer as _vp_mod

    _orig_run = _vp_mod.VisionProducer.run

    def _fast_run(self, *, max_ticks=None, on_action=None, **kwargs):
        tick_t = [0.0]

        def _fast_now():
            tick_t[0] += 10.0
            return tick_t[0]

        return _orig_run(
            self,
            max_ticks=max_ticks,
            on_action=on_action,
            now=_fast_now,
            sleep=lambda _: None,
            **{k: v for k, v in kwargs.items() if k not in ("now", "sleep")},
        )

    monkeypatch.setattr(_vp_mod.VisionProducer, "run", _fast_run)
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    rc = main(
        [
            "vision",
            "run",
            "--json",
            "--deadband",
            "0",
            "--hold",
            "0",
            "--motion-threshold",
            "0",
            "--max-ticks",
            "10",
        ]
    )
    assert rc == 0
    lines = [ln for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    events = [json.loads(ln) for ln in lines]
    assert any(e.get("action") for e in events)


def test_run_no_camera_exits_2(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.vision.get_transport", lambda args: _NoFrameTransport()
    )
    rc = main(["vision", "run", "--max-ticks", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    # The preflight center move succeeds (diagnostic printed), then the loop fails on
    # get_frame — so the error contract lines appear in stderr (after the diagnostic).
    assert "error:" in err
    assert "hint:" in err


def test_run_unreachable_daemon_exits_2(monkeypatch, capsys) -> None:
    """A dead daemon (move_goto raises exit-2 CliError) exits 2 cleanly."""

    class _Dead(_FakeTransport):
        def move_goto(self, **kwargs):
            raise CliError(
                code=EXIT_ENV_ERROR, message="cannot reach daemon", remediation="daemon start"
            )

    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: _Dead())
    rc = main(["vision", "run", "--max-ticks", "1"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- CLI: specs -----------------------------------------------------------


def test_specs_json(monkeypatch, capsys) -> None:
    specs = {"width": 1280, "height": 720, "name": "reachy-cam"}
    tr = _FakeTransport(specs=specs)
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    rc = main(["vision", "specs", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["width"] == 1280
    assert payload["name"] == "reachy-cam"


def test_specs_text(monkeypatch, capsys) -> None:
    specs = {"width": 640, "height": 480, "name": "test-cam"}
    tr = _FakeTransport(specs=specs)
    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: tr)
    rc = main(["vision", "specs"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "640" in out or "test-cam" in out


def test_specs_no_camera_exits_2(monkeypatch, capsys) -> None:
    """Accessing specs over a transport that raises CliError exit-2 surfaces cleanly."""

    class _NoCam(_FakeTransport):
        def camera_specs(self) -> object:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="camera specs unavailable",
                remediation="check the daemon is running",
            )

    monkeypatch.setattr("reachy.cli._commands.vision.get_transport", lambda args: _NoCam())
    rc = main(["vision", "specs"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err


# --- CLI: status ----------------------------------------------------------


def test_status_running_healthy(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "vision.pid").write_text("5050")
    monkeypatch.setattr("reachy.vision.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("reachy.vision.supervisor.health_ok", lambda *a, **k: True)
    rc = main(["vision", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running" and payload["pid"] == 5050
    assert payload["daemon"] == "healthy"


def test_status_stopped_when_no_pid(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.vision.supervisor.health_ok", lambda *a, **k: False)
    rc = main(["vision", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "process: stopped" in out and "daemon: unreachable" in out


def test_status_json_stopped(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.vision.supervisor.health_ok", lambda *a, **k: False)
    rc = main(["vision", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "stopped"


# --- CLI / supervisor: start ----------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 5151

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
    monkeypatch.setattr("reachy.vision.supervisor.health_ok", lambda *a, **k: True)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = main(["vision", "start", "--transport", "http", "--hold", "2", "--speed", "12"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out and "pid: 5151" in out
    assert (tmp_path / "vision.pid").read_text().strip() == "5151"
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "vision", "run"]
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_sdk_skips_http_preflight(monkeypatch, capsys) -> None:
    def _boom(*a, **k):
        raise AssertionError("sdk start must not call the http health check")

    monkeypatch.setattr("reachy.vision.supervisor.health_ok", _boom)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    rc = main(["vision", "start", "--transport", "sdk"])
    assert rc == 0
    assert "status: started" in capsys.readouterr().out
    assert "--transport" in procs[0].cmd and "sdk" in procs[0].cmd


def test_start_idempotent_when_already_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "vision.pid").write_text("5151")
    monkeypatch.setattr("reachy.vision.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["vision", "start"])
    assert rc == 0
    assert "already-running" in capsys.readouterr().out


def test_start_refuses_when_daemon_unreachable(monkeypatch, capsys) -> None:
    monkeypatch.setattr("reachy.vision.supervisor.health_ok", lambda *a, **k: False)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = main(["vision", "start", "--transport", "http"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "daemon start" in err


# --- CLI / supervisor: stop -----------------------------------------------


def test_stop_when_not_running(capsys) -> None:
    rc = main(["vision", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "vision.pid").write_text("5050")
    state = {"alive": True}
    monkeypatch.setattr("reachy.vision.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.vision.supervisor._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    rc = main(["vision", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGTERM" in out
    assert killed == [(5050, signal.SIGTERM)]
    assert not (tmp_path / "vision.pid").exists()


# --- supervisor: build_run_command ----------------------------------------


def test_build_run_command_serializes_params() -> None:
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=10.0,
        params=VisionParams(hold=2.0, speed=12.0, motion_threshold=0.05),
    )
    assert cmd[1:5] == ["-m", "reachy", "vision", "run"]
    assert cmd[cmd.index("--hold") + 1] == "2.0"
    assert cmd[cmd.index("--speed") + 1] == "12.0"
    assert cmd[cmd.index("--motion-threshold") + 1] == "0.05"


def test_build_run_command_default_transport_is_sdk() -> None:
    cmd = supervisor.build_run_command(
        transport="sdk",
        base_url="http://localhost:8000",
        timeout=10.0,
        params=VisionParams(),
    )
    assert "--transport" in cmd
    assert cmd[cmd.index("--transport") + 1] == "sdk"


# --- SDK-first default transport ------------------------------------------


def test_vision_run_defaults_to_sdk(monkeypatch) -> None:
    """``reachy vision run`` with no --transport and no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["vision", "run"])
    assert args.transport == "sdk"


def test_vision_run_transport_flag_overrides(monkeypatch) -> None:
    """``--transport http`` still selects http regardless of the SDK default."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["vision", "run", "--transport", "http"])
    assert args.transport == "http"


def test_vision_start_defaults_to_sdk(monkeypatch) -> None:
    """``reachy vision start`` with no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["vision", "start"])
    assert args.transport == "sdk"


def test_vision_restart_defaults_to_sdk(monkeypatch) -> None:
    """``reachy vision restart`` with no env → transport=sdk."""
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    parser = _build_parser()
    args = parser.parse_args(["vision", "restart"])
    assert args.transport == "sdk"


# --- tuning flags parse correctly -----------------------------------------


def test_tuning_defaults_unchanged_when_unset() -> None:
    """Unset flags keep the VisionParams dataclass defaults."""
    import argparse

    from reachy.cli._commands.vision import _add_tuning_args, _params_from_args

    p = argparse.ArgumentParser()
    _add_tuning_args(p)
    args = p.parse_args([])
    params = _params_from_args(args)
    d = VisionParams()
    assert params.gain == d.gain
    assert params.max_yaw == d.max_yaw
    assert params.deadband == d.deadband
    assert params.hold == d.hold
    assert params.speed == d.speed
    assert params.motion_threshold == d.motion_threshold


def test_tuning_flags_parsed_correctly() -> None:
    """All tuning flags map to the right VisionParams fields."""
    import argparse

    from reachy.cli._commands.vision import _add_tuning_args, _params_from_args

    p = argparse.ArgumentParser()
    _add_tuning_args(p)
    args = p.parse_args(
        [
            "--gain",
            "0.8",
            "--max-yaw",
            "40",
            "--deadband",
            "5",
            "--hold",
            "2",
            "--speed",
            "20",
            "--motion-threshold",
            "0.05",
        ]
    )
    params = _params_from_args(args)
    assert params.gain == 0.8
    assert params.max_yaw == 40.0
    assert params.deadband == 5.0
    assert params.hold == 2.0
    assert params.speed == 20.0
    assert params.motion_threshold == 0.05
