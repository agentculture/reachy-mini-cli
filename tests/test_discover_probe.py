"""Tests for reachy.discover.probe — the stdlib HTTP identity probe.

Acceptance criteria covered (one section each):

1. probe(host) against a fake daemon returning wireless_version=true yields a
   frozen UnitRecord carrying hardware_id, robot_name, model, wireless,
   version, wlan_ip, address.
2. A non-200, a connection refusal, a timeout, and valid JSON that is NOT a
   Reachy daemon each return None -- the caller never sees an exception.
3. The probe issues exactly ONE GET to /api/daemon/status and no other
   request, asserted against a recording stub -- proving it neither arms
   motors nor opens a media session.

Every test here monkeypatches ``urllib.request.urlopen`` directly (mirroring
``tests/test_stt.py``'s pattern for the sibling stdlib-urllib client) so the
suite never touches the real network.
"""

from __future__ import annotations

import json
import socket
import urllib.error
import urllib.request
from dataclasses import FrozenInstanceError

import pytest

from reachy.discover.probe import DEFAULT_PORT, STATUS_PATH, UnitRecord, probe

# Real response captured from the live robot (see the task brief) -- ground
# truth fixture for the exact payload shape this module must parse.
LIVE_DAEMON_STATUS = {
    "type": "daemon_status",
    "robot_name": "reachy_mini",
    "state": "running",
    "wireless_version": True,
    "desktop_app_daemon": False,
    "simulation_enabled": False,
    "mockup_sim_enabled": False,
    "no_media": False,
    "media_released": False,
    "camera_specs_name": "wireless",
    "backend_status": {"ready": False, "motor_control_mode": "disabled"},
    "error": None,
    "wlan_ip": "192.168.1.162",
    "version": "1.9.0",
    "hardware_id": "a89063c05ae79779",
}


class _FakeResp:
    """Minimal stand-in for the context manager urlopen() yields."""

    def __init__(self, *, status: int = 200, body: bytes = b"") -> None:
        self._status = status
        self._body = body

    status = property(lambda self: self._status)

    def getcode(self):
        return self._status

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _patch_urlopen(monkeypatch: pytest.MonkeyPatch, resp: _FakeResp) -> None:
    monkeypatch.setattr(urllib.request, "urlopen", lambda *a, **k: resp)


# ---------------------------------------------------------------------------
# Criterion 1 -- a wireless daemon's response parses into a frozen UnitRecord
# ---------------------------------------------------------------------------


class TestProbeParsesUnitRecord:
    def test_wireless_daemon_yields_expected_record(self, monkeypatch):
        body = json.dumps(LIVE_DAEMON_STATUS).encode()
        _patch_urlopen(monkeypatch, _FakeResp(body=body))

        result = probe("192.168.1.162")

        assert result == UnitRecord(
            hardware_id="a89063c05ae79779",
            robot_name="reachy_mini",
            model="Reachy Mini Wireless",
            wireless=True,
            version="1.9.0",
            wlan_ip="192.168.1.162",
            address="192.168.1.162",
        )

    def test_result_is_a_frozen_dataclass(self, monkeypatch):
        body = json.dumps(LIVE_DAEMON_STATUS).encode()
        _patch_urlopen(monkeypatch, _FakeResp(body=body))

        result = probe("192.168.1.162")

        with pytest.raises(FrozenInstanceError):
            result.hardware_id = "different"

    def test_lite_daemon_derives_lite_model(self, monkeypatch):
        payload = dict(LIVE_DAEMON_STATUS, wireless_version=False, hardware_id="deadbeef01")
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))

        result = probe("192.168.1.157")

        assert result.model == "Reachy Mini Lite"
        assert result.wireless is False

    def test_address_is_the_host_actually_probed_not_the_wlan_ip(self, monkeypatch):
        # wlan_ip in the payload can differ from the interface actually dialed
        # (e.g. reached over a bridge address while advertising its own
        # wireless address) -- `address` must record what WE dialed.
        payload = dict(LIVE_DAEMON_STATUS, wlan_ip="192.168.1.162")
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))

        result = probe("10.0.0.5")

        assert result.address == "10.0.0.5"
        assert result.wlan_ip == "192.168.1.162"

    def test_missing_wlan_ip_is_none_not_a_crash(self, monkeypatch):
        payload = {k: v for k, v in LIVE_DAEMON_STATUS.items() if k != "wlan_ip"}
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))

        result = probe("192.168.1.162")

        assert result is not None
        assert result.wlan_ip is None


# ---------------------------------------------------------------------------
# Criterion 2 -- every failure mode degrades to None, never an exception
# ---------------------------------------------------------------------------


class TestProbeNeverRaises:
    def test_non_200_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResp(status=404, body=b"not found"))
        assert probe("127.0.0.1") is None

    def test_server_error_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResp(status=503, body=b"unavailable"))
        assert probe("127.0.0.1") is None

    def test_connection_refused_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise urllib.error.URLError(ConnectionRefusedError(111, "Connection refused"))

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert probe("127.0.0.1") is None

    def test_timeout_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise socket.timeout("timed out")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert probe("127.0.0.1") is None

    def test_dns_failure_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise urllib.error.URLError(socket.gaierror("Name or service not known"))

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert probe("nonexistent.invalid") is None

    def test_http_error_returns_none(self, monkeypatch):
        def _boom(*a, **k):
            raise urllib.error.HTTPError("url", 500, "Internal Server Error", {}, None)

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert probe("127.0.0.1") is None

    def test_valid_json_that_is_not_a_reachy_daemon_returns_none(self, monkeypatch):
        body = json.dumps({"hello": "world", "status": "ok"}).encode()
        _patch_urlopen(monkeypatch, _FakeResp(body=body))
        assert probe("127.0.0.1") is None

    def test_json_missing_hardware_id_returns_none(self, monkeypatch):
        payload = {k: v for k, v in LIVE_DAEMON_STATUS.items() if k != "hardware_id"}
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))
        assert probe("127.0.0.1") is None

    def test_json_missing_robot_name_returns_none(self, monkeypatch):
        payload = {k: v for k, v in LIVE_DAEMON_STATUS.items() if k != "robot_name"}
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))
        assert probe("127.0.0.1") is None

    def test_json_missing_version_returns_none(self, monkeypatch):
        payload = {k: v for k, v in LIVE_DAEMON_STATUS.items() if k != "version"}
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))
        assert probe("127.0.0.1") is None

    def test_wireless_version_missing_returns_none(self, monkeypatch):
        payload = {k: v for k, v in LIVE_DAEMON_STATUS.items() if k != "wireless_version"}
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))
        assert probe("127.0.0.1") is None

    def test_wireless_version_wrong_type_returns_none(self, monkeypatch):
        payload = dict(LIVE_DAEMON_STATUS, wireless_version="true")
        _patch_urlopen(monkeypatch, _FakeResp(body=json.dumps(payload).encode()))
        assert probe("127.0.0.1") is None

    def test_non_json_body_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResp(body=b"not json at all"))
        assert probe("127.0.0.1") is None

    def test_empty_body_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResp(body=b""))
        assert probe("127.0.0.1") is None

    def test_non_dict_json_returns_none(self, monkeypatch):
        _patch_urlopen(monkeypatch, _FakeResp(body=b"[1, 2, 3]"))
        assert probe("127.0.0.1") is None

    def test_unexpected_exception_still_returns_none(self, monkeypatch):
        """Belt-and-braces: even a totally unanticipated failure never raises."""

        def _boom(*a, **k):
            raise RuntimeError("something the urllib layer never documented")

        monkeypatch.setattr(urllib.request, "urlopen", _boom)
        assert probe("127.0.0.1") is None


# ---------------------------------------------------------------------------
# Criterion 3 -- exactly one GET to /api/daemon/status, nothing else
# ---------------------------------------------------------------------------


class TestProbeIssuesExactlyOneRequest:
    def test_single_get_to_the_status_path(self, monkeypatch):
        calls = []

        def _recording_urlopen(req, *a, **k):
            calls.append((req.get_method(), req.full_url))
            return _FakeResp(body=json.dumps(LIVE_DAEMON_STATUS).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _recording_urlopen)

        result = probe("192.168.1.162")

        assert result is not None
        assert calls == [("GET", f"http://192.168.1.162:{DEFAULT_PORT}{STATUS_PATH}")]

    def test_custom_port_is_honoured_in_the_single_request(self, monkeypatch):
        calls = []

        def _recording_urlopen(req, *a, **k):
            calls.append(req.full_url)
            return _FakeResp(body=json.dumps(LIVE_DAEMON_STATUS).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _recording_urlopen)

        probe("192.168.1.162", port=8123)

        assert calls == [f"http://192.168.1.162:8123{STATUS_PATH}"]

    def test_a_raising_stub_is_still_called_exactly_once(self, monkeypatch):
        calls = []

        def _recording_and_failing(req, *a, **k):
            calls.append(req.full_url)
            raise urllib.error.URLError("refused")

        monkeypatch.setattr(urllib.request, "urlopen", _recording_and_failing)

        assert probe("127.0.0.1") is None
        assert len(calls) == 1

    def test_no_request_reaches_any_other_path(self, monkeypatch):
        """The probe must never touch a move/motor/media route -- only status."""
        calls = []

        def _recording_urlopen(req, *a, **k):
            calls.append(req.full_url)
            return _FakeResp(body=json.dumps(LIVE_DAEMON_STATUS).encode())

        monkeypatch.setattr(urllib.request, "urlopen", _recording_urlopen)

        probe("192.168.1.162")

        assert all(STATUS_PATH in url for url in calls)
        assert not any("/api/move" in url or "/api/media" in url for url in calls)


# ---------------------------------------------------------------------------
# Module surface
# ---------------------------------------------------------------------------


def test_default_port_matches_the_daemon_rest_port():
    assert DEFAULT_PORT == 8000


def test_status_path_matches_http_transport():
    # The probe must speak the exact same route HttpTransport.daemon_status()
    # does -- this is a presence/identity check over that route, not a
    # competing implementation of it.
    assert STATUS_PATH == "/api/daemon/status"
