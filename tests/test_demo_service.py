"""Tests for ``reachy.demo_service`` — systemd --user unit gen + management.

No real ``systemctl`` runs: ``reachy.demo_service._run`` is replaced with a
recorder, and the loginctl call (``subprocess.run``) is stubbed. The unit dir is
isolated via ``XDG_CONFIG_HOME``.
"""

from __future__ import annotations

import sys
import types

import pytest

from reachy import demo_service as svc
from reachy.cli._errors import CliError


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def _ok(stdout: str = "", code: int = 0):
    return types.SimpleNamespace(returncode=code, stdout=stdout, stderr="")


def _recorder(box, result_for=None):
    def _run(args):
        box.append(list(args))
        return result_for(args) if result_for else _ok()

    return _run


# --- pure unit text -------------------------------------------------------


def test_unit_text_shape() -> None:
    text = svc.unit_text("/tmp/demo.json")
    assert "[Service]" in text
    assert "Restart=on-failure" in text
    assert "-m reachy demo-mode run --config" in text
    assert "WantedBy=default.target" in text


def test_exec_start_uses_running_interpreter() -> None:
    line = svc.exec_start("/tmp/demo.json")
    assert sys.executable in line
    assert line.endswith('--config "/tmp/demo.json"')


def test_unit_arg_escapes_percent_and_quotes() -> None:
    assert svc._unit_arg("a b") == '"a b"'
    assert svc._unit_arg("100%done") == '"100%%done"'


# --- install / uninstall --------------------------------------------------


def test_install_writes_unit_and_reloads(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("reachy.demo_service._run", _recorder(calls))
    data = svc.install("/tmp/demo.json")
    assert data["status"] == "installed"
    assert svc.unit_path().is_file()
    assert "-m reachy demo-mode run" in svc.unit_path().read_text()
    assert ["--user", "daemon-reload"] in calls


def test_install_without_systemctl_raises(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", lambda args: None)
    with pytest.raises(CliError):
        svc.install("/tmp/demo.json")


def test_uninstall_removes_unit(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", _recorder([]))
    svc.install("/tmp/demo.json")
    data = svc.uninstall()
    assert data["status"] == "uninstalled"
    assert not svc.unit_path().is_file()


def test_uninstall_not_installed(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", _recorder([]))
    data = svc.uninstall()
    assert data["status"] == "not-installed"


# --- enable / disable / restart -------------------------------------------


def test_enable_enables_and_lingers(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("reachy.demo_service._run", _recorder(calls))
    monkeypatch.setattr("shutil.which", lambda name: f"/usr/bin/{name}")
    linger_calls: list = []
    monkeypatch.setattr("subprocess.run", lambda cmd, **k: linger_calls.append(cmd) or _ok())
    data = svc.enable(linger=True)
    assert data["status"] == "enabled"
    assert data["linger"] is True
    assert ["--user", "enable", "--now", svc.UNIT_NAME] in calls
    assert any("enable-linger" in c for c in linger_calls)


def test_enable_no_linger_skips_loginctl(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", _recorder([]))

    def _boom(cmd, **k):
        raise AssertionError("must not call loginctl when linger is off")

    monkeypatch.setattr("subprocess.run", _boom)
    data = svc.enable(linger=False)
    assert data["linger"] is False


def test_enable_failure_raises(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", lambda args: _ok(stdout="boom", code=1))
    with pytest.raises(CliError):
        svc.enable()


def test_disable_and_restart(monkeypatch) -> None:
    calls: list = []
    monkeypatch.setattr("reachy.demo_service._run", _recorder(calls))
    assert svc.disable()["status"] == "disabled"
    assert svc.restart()["status"] == "restarted"
    assert ["--user", "disable", "--now", svc.UNIT_NAME] in calls
    assert ["--user", "restart", svc.UNIT_NAME] in calls


# --- status / is_active ---------------------------------------------------


def test_status_reports_state(monkeypatch) -> None:
    def _result(args):
        if "is-active" in args:
            return _ok(stdout="active")
        if "is-enabled" in args:
            return _ok(stdout="enabled")
        return _ok()

    monkeypatch.setattr("reachy.demo_service._run", _recorder([], result_for=_result))
    svc.unit_path().parent.mkdir(parents=True, exist_ok=True)
    svc.unit_path().write_text("x", encoding="utf-8")
    data = svc.status()
    assert data["active"] == "active"
    assert data["enabled"] == "enabled"
    assert data["installed"] is True


def test_status_unknown_without_systemctl(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", lambda args: None)
    data = svc.status()
    assert data["active"] == "unknown"
    assert data["enabled"] == "unknown"


def test_is_active_true_only_on_active(monkeypatch) -> None:
    monkeypatch.setattr("reachy.demo_service._run", lambda args: _ok(stdout="active"))
    assert svc.is_active() is True
    monkeypatch.setattr("reachy.demo_service._run", lambda args: _ok(stdout="inactive", code=3))
    assert svc.is_active() is False
    monkeypatch.setattr("reachy.demo_service._run", lambda args: None)
    assert svc.is_active() is False
