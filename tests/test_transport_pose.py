"""Tests for transport head-pose readback (``head_pose()``).

Covers the three transport flavors plus the pure-numpy rotation-matrix → euler
extraction helper that ``sdk_transport`` uses to normalize the SDK's 4×4
homogeneous head-pose matrix into ``(pitch_deg, yaw_deg)``.

The euler convention mirrors reachy_nova's ``detect_pat`` (which uses
``scipy.spatial.transform.Rotation.from_matrix(...).as_euler("xyz", degrees=True)``
and reads ``pitch = euler[1]`` / ``yaw = euler[2]``). scipy is NOT a dependency
here, so the extraction is reimplemented in pure numpy and validated against
matrices constructed by hand with numpy.
"""

from __future__ import annotations

import math

import numpy as np
import pytest

from reachy.cli._errors import CliError
from reachy.robot.http_transport import HttpTransport
from reachy.robot.sdk_transport import SdkTransport, _euler_pitch_yaw
from reachy.robot.transport import Transport


# --- rotation builders (intrinsic axis rotations) ------------------------
def _rx(a: float) -> np.ndarray:
    c, s = math.cos(a), math.sin(a)
    return np.array([[1, 0, 0], [0, c, -s], [0, s, c]], dtype=float)


def _ry(b: float) -> np.ndarray:
    c, s = math.cos(b), math.sin(b)
    return np.array([[c, 0, s], [0, 1, 0], [-s, 0, c]], dtype=float)


def _rz(g: float) -> np.ndarray:
    c, s = math.cos(g), math.sin(g)
    return np.array([[c, -s, 0], [s, c, 0], [0, 0, 1]], dtype=float)


def _homogeneous(rot: np.ndarray) -> np.ndarray:
    """Embed a 3×3 rotation into a 4×4 homogeneous transform (translation 0)."""
    mat = np.eye(4, dtype=float)
    mat[:3, :3] = rot
    return mat


def _intrinsic_xyz(roll_deg: float, pitch_deg: float, yaw_deg: float) -> np.ndarray:
    """Build R = Rx(roll) @ Ry(pitch) @ Rz(yaw) — scipy's intrinsic "xyz"."""
    return _rx(math.radians(roll_deg)) @ _ry(math.radians(pitch_deg)) @ _rz(math.radians(yaw_deg))


# --- the pure-numpy euler helper -----------------------------------------
def test_euler_pure_pitch() -> None:
    rot = _intrinsic_xyz(0.0, 10.0, 0.0)
    pitch, yaw = _euler_pitch_yaw(_homogeneous(rot))
    assert pitch == pytest.approx(10.0, abs=1e-6)
    assert yaw == pytest.approx(0.0, abs=1e-6)


def test_euler_pure_yaw() -> None:
    rot = _intrinsic_xyz(0.0, 0.0, 15.0)
    pitch, yaw = _euler_pitch_yaw(_homogeneous(rot))
    assert pitch == pytest.approx(0.0, abs=1e-6)
    assert yaw == pytest.approx(15.0, abs=1e-6)


def test_euler_mixed_matches_xyz_convention() -> None:
    # roll/pitch/yaw all non-zero: pitch must come from index 1, yaw from
    # index 2 of the intrinsic-xyz euler decomposition (nova's reading).
    rot = _intrinsic_xyz(5.0, 10.0, 15.0)
    pitch, yaw = _euler_pitch_yaw(_homogeneous(rot))
    assert pitch == pytest.approx(10.0, abs=1e-6)
    assert yaw == pytest.approx(15.0, abs=1e-6)


def test_euler_negative_angles() -> None:
    rot = _intrinsic_xyz(-7.0, -20.0, -30.0)
    pitch, yaw = _euler_pitch_yaw(_homogeneous(rot))
    assert pitch == pytest.approx(-20.0, abs=1e-6)
    assert yaw == pytest.approx(-30.0, abs=1e-6)


def test_euler_accepts_3x3() -> None:
    # The helper should accept a bare 3×3 rotation as well as a 4×4 transform.
    rot = _intrinsic_xyz(0.0, 12.0, -8.0)
    pitch, yaw = _euler_pitch_yaw(rot)
    assert pitch == pytest.approx(12.0, abs=1e-6)
    assert yaw == pytest.approx(-8.0, abs=1e-6)


def test_euler_returns_plain_floats() -> None:
    pitch, yaw = _euler_pitch_yaw(_homogeneous(_intrinsic_xyz(0.0, 3.0, 4.0)))
    assert type(pitch) is float
    assert type(yaw) is float


# --- base transport ------------------------------------------------------
def test_base_head_pose_unsupported() -> None:
    with pytest.raises(CliError) as excinfo:
        Transport().head_pose()
    assert "not supported" in excinfo.value.message


# --- http transport ------------------------------------------------------
def test_http_head_pose_unsupported() -> None:
    transport = HttpTransport(base_url="http://localhost:8000")
    with pytest.raises(CliError) as excinfo:
        transport.head_pose()
    # exit-2 environment error, never a traceback.
    assert isinstance(excinfo.value, CliError)


# --- sdk transport (stubbed, no reachy_mini installed) -------------------
class _FakeMini:
    """Minimal stand-in for ``ReachyMini`` as a context manager."""

    def __init__(self, pose: np.ndarray) -> None:
        self._pose = pose

    def __enter__(self) -> "_FakeMini":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def get_current_head_pose(self) -> np.ndarray:
        return self._pose


def test_sdk_head_pose_maps_matrix_to_pitch_yaw(monkeypatch: pytest.MonkeyPatch) -> None:
    pose = _homogeneous(_intrinsic_xyz(2.0, 18.0, -25.0))

    def fake_import():  # type: ignore[no-untyped-def]
        return (lambda: _FakeMini(pose)), None

    monkeypatch.setattr(SdkTransport, "_import", staticmethod(fake_import))

    pitch, yaw = SdkTransport().head_pose()
    assert pitch == pytest.approx(18.0, abs=1e-6)
    assert yaw == pytest.approx(-25.0, abs=1e-6)
    assert type(pitch) is float
    assert type(yaw) is float
