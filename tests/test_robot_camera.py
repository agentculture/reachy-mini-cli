"""Tests for camera access in the transport layer (vision feature, t1).

No daemon and no ``reachy_mini`` SDK are needed:

* the http transport's network call (``urllib.request.urlopen``) is
  monkeypatched with a fake response (camera *metadata* only);
* the sdk transport's lazy ``_import_camera`` seam is monkeypatched to return a
  FAKE camera yielding a known frame, so the optional ``reachy_mini`` package is
  never imported.
"""

from __future__ import annotations

import json

import numpy as np
import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.robot.http_transport import HttpTransport
from reachy.robot.sdk_transport import SdkTransport

# ---------------------------------------------------------------------------
# HttpTransport.camera_specs() — GET /api/camera/specs
# ---------------------------------------------------------------------------


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
        return _FakeResp(payload, status)

    monkeypatch.setattr("urllib.request.urlopen", _fake)


def test_camera_specs_parses_response(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """camera_specs() GETs /api/camera/specs and returns the parsed metadata dict."""
    specs = {
        "name": "reachy-mini-cam",
        "width": 1280,
        "height": 720,
        "intrinsics": [[700.0, 0.0, 640.0], [0.0, 700.0, 360.0], [0.0, 0.0, 1.0]],
    }
    rec: dict[str, object] = {}
    _patch_urlopen(monkeypatch, specs, rec)

    result = HttpTransport(base_url="http://localhost:8000").camera_specs()

    assert result == specs
    assert rec["method"] == "GET"
    assert rec["url"] == "http://localhost:8000/api/camera/specs"


# ---------------------------------------------------------------------------
# HttpTransport.get_frame() — unsupported over HTTP
# ---------------------------------------------------------------------------


def test_http_get_frame_is_env_error_with_extra_hint() -> None:
    """HTTP cannot serve frames: get_frame() raises CliError(code=2) pointing at the extra."""
    with pytest.raises(CliError) as excinfo:
        HttpTransport().get_frame()

    err = excinfo.value
    assert err.code == EXIT_ENV_ERROR
    # The remediation must steer the operator at the local SDK/daemon profile.
    hint = err.remediation.lower()
    assert "sdk" in hint or "daemon" in hint
    assert "[sdk]" in err.remediation or "[daemon]" in err.remediation


# ---------------------------------------------------------------------------
# SdkTransport.get_frame() — local camera path, with an injectable fake
# ---------------------------------------------------------------------------


class _FakeCamera:
    """Minimal stand-in for the SDK's local camera handle."""

    def __init__(self, frame) -> None:  # type: ignore[no-untyped-def]
        self._frame = frame

    def get_frame(self):
        return self._frame


def _patch_camera(monkeypatch, *, frame=None, available=True) -> None:
    """Make ``SdkTransport._import_camera`` return a fake (available, camera) pair.

    Mirrors the ``_patch_import`` seam in test_sdk_transport.py — the FAKE is
    injected so the real (uninstalled) ``reachy_mini`` is never imported.
    """

    def _fake_import_camera():
        return available, _FakeCamera(frame)

    monkeypatch.setattr(SdkTransport, "_import_camera", staticmethod(_fake_import_camera))


def test_sdk_get_frame_returns_ndarray(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """get_frame() returns the camera's ndarray frame unchanged (expected H x W x 3)."""
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    frame[0, 0] = (12, 34, 56)  # a known pixel so we can assert identity, not just shape
    _patch_camera(monkeypatch, frame=frame, available=True)

    result = SdkTransport().get_frame()

    assert isinstance(result, np.ndarray)
    assert result.shape == (720, 1280, 3)
    assert result.dtype == np.uint8
    assert tuple(result[0, 0]) == (12, 34, 56)


def test_sdk_get_frame_unavailable_is_env_error(monkeypatch) -> None:  # type: ignore
    """When the local camera is unavailable, get_frame() raises CliError(code=2)."""
    _patch_camera(monkeypatch, frame=None, available=False)

    with pytest.raises(CliError) as excinfo:
        SdkTransport().get_frame()

    err = excinfo.value
    assert err.code == EXIT_ENV_ERROR
    assert "camera" in err.message.lower()
