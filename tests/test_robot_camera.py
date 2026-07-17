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
import sys
import types

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


class _FakeMediaHandle:
    """Stand-in for the real 1.9.x ``ReachyMini.media`` (a ``MediaManager``).

    Exposes only the surface the transport now uses: ``camera`` (a handle or
    ``None``) and ``get_frame()`` (the ndarray, or ``None`` when no frame is ready
    — matching ``MediaManager.get_frame``).
    """

    def __init__(self, *, frame=None, available=True) -> None:  # type: ignore[no-untyped-def]
        self.camera: object | None = object() if available else None
        self._frame = frame

    def get_frame(self):
        if self.camera is None:
            return None
        return self._frame


def _patch_camera(monkeypatch, *, frame=None, available=True) -> None:
    """Make ``SdkTransport._import_camera`` return a fake ``(mini, media)`` pair.

    Mirrors the ``_patch_import`` seam in test_sdk_transport.py — the FAKE is
    injected so the real (uninstalled) ``reachy_mini`` is never imported. The
    returned ``media`` is a stand-in for ``ReachyMini.media`` (the MediaManager),
    exposing only the real surface: ``media.camera`` + ``media.get_frame()``.
    """
    media = _FakeMediaHandle(frame=frame, available=available)
    mini = object()  # only held to keep the client alive during the read

    def _fake_import_camera():
        return mini, media

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
    """When media.camera is None, get_frame() raises CliError(code=2)."""
    _patch_camera(monkeypatch, frame=None, available=False)

    with pytest.raises(CliError) as excinfo:
        SdkTransport().get_frame()

    err = excinfo.value
    assert err.code == EXIT_ENV_ERROR
    assert "camera" in err.message.lower()


def test_sdk_get_frame_none_when_no_frame_ready(monkeypatch) -> None:  # type: ignore
    """A camera present but no frame ready (media.get_frame() -> None) returns None.

    The documented degrade path: the transport does not raise; the caller skips.
    """
    _patch_camera(monkeypatch, frame=None, available=True)

    assert SdkTransport().get_frame() is None


def test_import_camera_uses_real_surface_and_acquires(monkeypatch) -> None:  # type: ignore
    """The REAL ``_import_camera`` body: imports ReachyMini, acquires released media.

    Exercises the actual seam (not a monkeypatched replacement) with a FAKE
    ``reachy_mini.ReachyMini`` swapped in via ``monkeypatch.setattr`` — safe, no
    hardware, since the fake is never a real robot client. Proves it returns
    ``(mini, media)`` and honors ``acquire_media`` when ``media_released`` is set.
    """

    class _FakeMediaMgr:
        def __init__(self) -> None:
            self.camera = object()

        def get_frame(self):
            return "FRAME"

    class _FakeReachyMini:
        def __init__(self) -> None:
            self.media_released = True
            self.acquired = 0
            self.media = _FakeMediaMgr()

        def acquire_media(self) -> None:
            self.acquired += 1
            self.media_released = False

    # setitem (not setattr on the real module) so this passes on a bare
    # ``uv sync`` too — CI has no [sdk] extra, importing reachy_mini would fail.
    fake_module = types.SimpleNamespace(ReachyMini=_FakeReachyMini)
    monkeypatch.setitem(sys.modules, "reachy_mini", fake_module)

    mini, media = SdkTransport._import_camera()

    assert isinstance(mini, _FakeReachyMini)
    assert mini.acquired == 1  # released media was re-acquired
    assert media is mini.media
    assert media.get_frame() == "FRAME"


def test_no_removed_camera_api_referenced_in_shipping_code() -> None:
    """The removed guessed API must not be *used* in shipping code.

    ``is_local_camera_available`` is NOT a ``ReachyMini`` method — the transport
    used to guess it existed. Scans every ``*.py`` under the shipping trees
    (``reachy/`` production package + ``scripts/``) so a regression that
    reintroduces the guessed name fails loudly. Test files are intentionally out
    of scope: they *describe* the removed API (fakes, assertion messages, this
    very check) without ever calling it.
    """
    import pathlib

    # Assemble the needle from parts so this guard file is not itself a match.
    needle = "is_local_camera" + "_available"
    repo_root = pathlib.Path(__file__).resolve().parents[1]
    offenders = []
    for base in ("reachy", "scripts"):
        base_dir = repo_root / base
        if not base_dir.exists():
            continue
        for path in base_dir.rglob("*.py"):
            if "__pycache__" in path.parts:
                continue
            if needle in path.read_text(encoding="utf-8"):
                offenders.append(str(path.relative_to(repo_root)))

    assert not offenders, f"removed API {needle!r} still referenced in shipping code: {offenders}"
