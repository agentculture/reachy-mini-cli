"""Tests for the robot noun groups (device, app, move) and the transport layer.

No daemon and no ``reachy_mini`` SDK are needed: the http transport's network
call (``urllib.request.urlopen``) is monkeypatched with a fake response, and the
sdk-transport paths exercised here don't trigger the optional import.
"""

from __future__ import annotations

import email.message
import io
import json
import math
import urllib.error

import pytest

from reachy.cli import main


class _FakeResp:
    """Minimal stand-in for the urlopen context-manager response."""

    def __init__(self, payload: object, status: int = 200) -> None:
        self._raw = b"" if payload is None else json.dumps(payload).encode("utf-8")
        self.status = status

    def read(self) -> bytes:
        return self._raw

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


def _patch_urlopen(monkeypatch, payload, recorder=None, status=200):
    """Patch urllib so the http transport returns ``payload`` and records the request."""

    def _fake(req, timeout=None):  # noqa: ANN001 - test shim
        if recorder is not None:
            recorder["method"] = req.get_method()
            recorder["url"] = req.full_url
            recorder["data"] = json.loads(req.data) if req.data else None
            recorder["timeout"] = timeout
        return _FakeResp(payload, status)

    monkeypatch.setattr("urllib.request.urlopen", _fake)


# --- device ---------------------------------------------------------------


def test_device_status_text(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"state": "RUNNING", "version": "1.2.3"}, rec)
    rc = main(["device", "status"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "state: RUNNING" in out
    assert "version: 1.2.3" in out
    assert rec["method"] == "GET"
    assert rec["url"].endswith("/api/daemon/status")


def test_device_status_json(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_urlopen(monkeypatch, {"state": "RUNNING", "version": "1.2.3"})
    rc = main(["device", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload == {"state": "RUNNING", "version": "1.2.3"}


def test_device_state_uses_state_endpoint(monkeypatch) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"body_yaw": 0.0}, rec)
    assert main(["device", "state"]) == 0
    assert rec["url"].endswith("/api/state/full")


# --- DoA (sound direction-of-arrival, read by the behavior engine) --------


def test_doa_endpoint_and_per_call_timeout(monkeypatch) -> None:
    from reachy.robot.http_transport import HttpTransport

    rec: dict = {}
    _patch_urlopen(monkeypatch, {"angle": 1.2, "speech_detected": True}, rec)
    transport = HttpTransport(base_url="http://localhost:8000", timeout=10.0)
    out = transport.doa(timeout=0.1)
    assert out == {"angle": 1.2, "speech_detected": True}
    assert rec["method"] == "GET"
    assert rec["url"].endswith("/api/state/doa")
    assert rec["timeout"] == 0.1  # per-call override, not the transport's 10s default


def test_doa_null_body_is_none(monkeypatch) -> None:
    from reachy.robot.http_transport import HttpTransport

    _patch_urlopen(monkeypatch, None)  # a no-mic unit answers with a null/empty body
    assert HttpTransport().doa() is None


def test_doa_http_500_is_env_error(monkeypatch) -> None:
    from reachy.cli._errors import EXIT_ENV_ERROR, CliError
    from reachy.robot.http_transport import HttpTransport

    def _boom(req, timeout=None):  # noqa: ANN001 - test shim
        raise urllib.error.HTTPError(req.full_url, 500, "err", email.message.Message(), None)

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    with pytest.raises(CliError) as exc:
        HttpTransport().doa()
    assert exc.value.code == EXIT_ENV_ERROR


# --- app ------------------------------------------------------------------


def test_app_list_text(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, [{"name": "demo", "installed": True}], rec)
    rc = main(["app", "list"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "- demo" in out
    assert "installed: True" in out
    assert rec["url"].endswith("/api/apps/list-available")


def test_app_start_posts_url_encoded_name(monkeypatch) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"name": "my app", "state": "starting"}, rec)
    assert main(["app", "start", "my app"]) == 0
    assert rec["method"] == "POST"
    assert rec["url"].endswith("/api/apps/start-app/my%20app")


def test_app_status_empty_text(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    _patch_urlopen(monkeypatch, None)
    assert main(["app", "status"]) == 0
    assert "(no app running)" in capsys.readouterr().out


# --- move -----------------------------------------------------------------


def test_move_goto_converts_units(monkeypatch) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"uuid": "abc"}, rec)
    rc = main(["move", "goto", "--z", "10", "--pitch", "-5", "--duration", "2"])
    assert rc == 0
    assert rec["method"] == "POST"
    assert rec["url"].endswith("/api/move/goto")
    body = rec["data"]
    assert body["duration"] == 2.0
    assert body["interpolation"] == "minjerk"
    # mm -> metres, degrees -> radians.
    assert body["head_pose"]["z"] == pytest.approx(0.01)
    assert body["head_pose"]["pitch"] == pytest.approx(math.radians(-5))
    assert body["head_pose"]["x"] == 0.0
    assert "antennas" not in body
    assert "body_yaw" not in body


def test_move_goto_maps_ease_to_daemon_name(monkeypatch) -> None:
    # The CLI's friendly curve "ease" must reach the daemon as "ease_in_out"
    # (its InterpolationTechnique name); the others pass through unchanged.
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"uuid": "abc"}, rec)
    assert main(["move", "goto", "--yaw", "10", "--duration", "1", "--interpolation", "ease"]) == 0
    assert rec["data"]["interpolation"] == "ease_in_out"


def test_move_goto_antennas_only(monkeypatch) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"uuid": "abc"}, rec)
    assert main(["move", "goto", "--antennas", "30", "20", "--duration", "1"]) == 0
    body = rec["data"]
    assert "head_pose" not in body
    assert body["antennas"] == pytest.approx([math.radians(30), math.radians(20)])


def test_move_wake_posts(monkeypatch) -> None:
    rec: dict = {}
    _patch_urlopen(monkeypatch, {"uuid": "abc"}, rec)
    assert main(["move", "wake"]) == 0
    assert rec["method"] == "POST"
    assert rec["url"].endswith("/api/move/play/wake_up")


# --- error contract -------------------------------------------------------


def test_daemon_unreachable_exit_2(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(req, timeout=None):  # noqa: ANN001 - test shim
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    rc = main(["device", "status"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "reachy daemon start" in err


def test_daemon_unreachable_json(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _boom(req, timeout=None):  # noqa: ANN001 - test shim
        raise urllib.error.URLError(ConnectionRefusedError("refused"))

    monkeypatch.setattr("urllib.request.urlopen", _boom)
    rc = main(["device", "status", "--json"])
    assert rc == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 2
    assert payload["remediation"]


def test_http_4xx_is_user_error(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    def _raise_400(req, timeout=None):  # noqa: ANN001 - test shim
        fp = io.BytesIO(json.dumps({"detail": "no such app"}).encode("utf-8"))
        raise urllib.error.HTTPError(req.full_url, 400, "Bad Request", email.message.Message(), fp)

    monkeypatch.setattr("urllib.request.urlopen", _raise_400)
    rc = main(["app", "start", "ghost"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "400" in err
    assert "no such app" in err


def test_sdk_transport_daemon_op_unsupported(
    capsys: pytest.CaptureFixture[str],
) -> None:
    # device status is daemon-side; the sdk flavor reports it unsupported
    # without needing the optional reachy_mini import.
    rc = main(["device", "status", "--transport", "sdk"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "not supported on the 'sdk' transport" in err
    assert "hint:" in err


def test_bad_base_url_is_user_error(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["device", "status", "--base-url", "ftp://nope"])
    assert rc == 1
    assert "http" in capsys.readouterr().err


def test_invalid_transport_env_fails_loud(monkeypatch, capsys: pytest.CaptureFixture[str]) -> None:
    # argparse choices don't validate the env-var default; get_transport must.
    monkeypatch.setenv("REACHY_TRANSPORT", "bogus")
    rc = main(["device", "status"])
    assert rc == 1
    err = capsys.readouterr().err
    assert "unknown transport" in err
    assert "hint:" in err


# --- overviews ------------------------------------------------------------


@pytest.mark.parametrize("noun", ["device", "app", "move"])
def test_noun_overview_text(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun, "overview"])
    assert rc == 0
    assert f"# reachy-mini-cli {noun}" in capsys.readouterr().out


@pytest.mark.parametrize("noun", ["device", "app", "move"])
def test_noun_overview_json(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun, "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == f"reachy-mini-cli {noun}"
    assert isinstance(payload["sections"], list)


@pytest.mark.parametrize("noun", ["device", "app", "move"])
def test_bare_noun_prints_overview(noun: str, capsys: pytest.CaptureFixture[str]) -> None:
    rc = main([noun])
    assert rc == 0
    assert capsys.readouterr().out.strip()


@pytest.mark.parametrize("noun", ["device", "app", "move"])
def test_noun_subverb_bad_flag_structured_error(
    noun: str, capsys: pytest.CaptureFixture[str]
) -> None:
    with pytest.raises(SystemExit) as exc:
        main([noun, "overview", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
