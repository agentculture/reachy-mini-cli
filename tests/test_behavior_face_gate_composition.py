"""Composition tests for the September-6 arc's wiring (task t12).

Every piece below was built and unit-tested on its own branch; this file pins
that ``_compose_run_seam`` actually WIRES them — the built-but-unwired failure
class this repo has met four times with a green suite:

* #179 — the face driver's still-only gate rides its OWN slew-speed self-motion
  latch beside the mic's fine one, and its ``lock_held`` peek reaches the lock driver.
* #176 — the face driver's ``on_stale`` route is the held media client's own
  ``drop``; a camera that goes silent is handed back from the TICK thread and
  the keeper's unchanged ``connected == False`` poll re-warms it, with no
  process restart. A client that never had a camera is never dropped.
* #176 — the availability rider's liveness provider is the face driver's
  last-usable-frame timestamp, on the face driver's own clock.

The boundary pins (``_COMPOSED_PROVIDER_FIELDS`` untouched, the zero-LLM and
red-team suites unmodified) are asserted where they live; here we only pin
that no NEW rule-visible sense field appeared.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from reachy.behavior import face_sense
from reachy.behavior.engine import EngineConfig
from reachy.behavior.face_lock import FaceLockDriver
from reachy.behavior.face_sense import FaceSenseDriver
from reachy.behavior.self_motion import SelfMotionDriver
from reachy.behavior.sense import _COMPOSED_PROVIDER_FIELDS
from reachy.behavior.sense_availability import SenseAvailabilityDriver
from reachy.cli._commands import behavior as behavior_mod

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def send(self, *_a, **_k):  # pragma: no cover - never exercised here
        return None


class _QuietTransport:
    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


class _FakeMedia:
    """A held media client stand-in with the #176 lifecycle verbs.

    ``frames`` is a queue of frames ``frame()`` hands out while ``streaming`` is
    True; once exhausted (or streaming is switched off) it answers ``None``
    while still claiming ``connected`` and ``camera_available`` — the exact
    silent-death shape #138 measured live and #176 recurred.
    """

    samplerate = 16000
    channels = 1

    def __init__(self, *, camera_available: bool = True) -> None:
        self.connected = False
        self.closed = False
        self.camera_available = camera_available
        self.streaming = True
        self.drops: list[str] = []
        self.warm_ups = 0

    def warm_up(self) -> bool:
        self.connected = True
        self.warm_ups += 1
        return True

    def drop(self, reason: str) -> bool:
        if not self.connected:
            return False
        self.connected = False
        self.drops.append(reason)
        return True

    def audio(self):
        return None

    def frame(self):
        if not (self.connected and self.streaming and self.camera_available):
            return None
        import numpy as np

        return np.zeros((8, 8, 3), dtype=np.uint8)

    def close(self) -> None:
        self.closed = True


class _Ctx:
    """The face driver reads ``now``; the self-motion driver reads ``pose``."""

    def __init__(self, now: float, pose: dict | None = None) -> None:
        self.now = now
        self.pose = pose


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


@pytest.fixture
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setattr(behavior_mod, "get_transport", lambda args: _QuietTransport())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _drivers_of(tick_seam) -> list:
    seam = tick_seam
    for _ in range(4):
        drivers = getattr(seam, "_drivers", None)
        if drivers is not None:
            return list(drivers)
        inner = getattr(seam, "_inner", None) or getattr(seam, "_seam", None)
        if inner is None:
            break
        seam = inner
    raise AssertionError(f"no driver list found behind {type(tick_seam).__name__}")


def _only(drivers, cls):
    found = [d for d in drivers if isinstance(d, cls)]
    assert len(found) == 1, f"expected exactly one {cls.__name__}, got {len(found)}"
    return found[0]


@contextlib.contextmanager
def _composed(monkeypatch, media: _FakeMedia | None = None):
    media = media if media is not None else _FakeMedia()
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: media)
    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), EngineConfig(compose_hz=50, base_layer=True, settle=False), None, None
    )
    try:
        yield media, _drivers_of(tick_seam), resources
    finally:
        resources.close()


# --------------------------------------------------------------------------- #
# 1. #179 — the gate is wired to its own slew-speed latch and to the lock       #
# --------------------------------------------------------------------------- #


def test_the_face_gate_has_its_own_slew_speed_latch_beside_the_mics_fine_one(
    _isolated, monkeypatch
):
    """Live on the Wireless the gate first shared the mic's 1.75 deg/s latch and
    the base layer's ~2.7 deg/s wander starved detection (no face ever known).
    The face gate now rides a SECOND latch at slew speed; the mic keeps its own."""
    with _composed(monkeypatch) as (_media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        latches = [d for d in drivers if isinstance(d, SelfMotionDriver)]
        assert len(latches) == 2, "expected the mic's latch AND the face gate's"
        assert face._moving is not None, "the still-only gate was built but never wired (#179)"
        gate_latch = face._moving.__self__
        assert gate_latch in latches
        fine = [d for d in latches if d is not gate_latch][0]
        assert gate_latch._eps_deg == pytest.approx(
            face_sense.DEFAULT_GATE_EPS_DEG_S / 50.0
        )  # 20 deg/s at 50 Hz = 0.4 deg/tick
        assert fine._eps_deg < gate_latch._eps_deg / 5


def test_the_base_layers_slow_wander_never_closes_detection(_isolated, monkeypatch):
    """feel-alive's gaze wander peaks ~2.7 deg/s (0.054 deg/tick at 50 Hz); the
    face gate must stay CLOSED (detection running) through it."""
    with _composed(monkeypatch) as (_media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        gate_latch = face._moving.__self__
        neutral = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

        def pose(yaw: float) -> dict:
            head = dict(neutral)
            head["yaw"] = yaw
            return {"head": head, "antennas": (0.0, 0.0), "body_yaw": 0.0}

        for i in range(0, 200):  # 4 s of a 3 deg/s wander
            t = i * 0.02
            gate_latch(_Ctx(t, pose(0.06 * i)))
            assert face._due_interval(t) == face._detect_interval, f"gate opened at tick {i}"


def test_a_commanded_slew_closes_detection_and_stillness_reopens_it(_isolated, monkeypatch):
    """Drive the composed self-motion latch through a slew and read the gate."""
    with _composed(monkeypatch) as (_media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        motion = face._moving.__self__  # the gate's own slew-speed latch
        neutral = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

        def pose(yaw: float) -> dict:
            head = dict(neutral)
            head["yaw"] = yaw
            return {"head": head, "antennas": (0.0, 0.0), "body_yaw": 0.0}

        t = 0.0
        motion(_Ctx(t, pose(0.0)))
        assert face._due_interval(t) == face._detect_interval  # still: free-running
        for i in range(1, 26):  # a 0.5 s slew at 50 Hz, 2 deg per tick
            t = i * 0.02
            motion(_Ctx(t, pose(2.0 * i)))
            assert face._due_interval(t) is None, "detection ran during a slew (#179)"
        # Hold still: inside the settle it is still closed, past it, open.
        for i in range(26, 26 + int(face._still_settle_s / 0.02) + 40):
            t = i * 0.02
            motion(_Ctx(t, pose(50.0)))
        assert face._due_interval(t) == face._detect_interval


def test_the_lock_held_peek_reaches_the_composed_lock_driver(_isolated, monkeypatch):
    with _composed(monkeypatch) as (_media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        lock = _only(drivers, FaceLockDriver)
        assert face._lock_held is not None, "the held-lock cadence was built but never wired"
        assert face._lock_held() is False
        monkeypatch.setattr(type(lock), "locked", property(lambda self: True))
        assert face._lock_held() is True


# --------------------------------------------------------------------------- #
# 2. #176 — a silent camera is handed back from the tick thread, re-warmed     #
# --------------------------------------------------------------------------- #


def test_a_silent_camera_is_dropped_from_the_tick_thread_and_the_keeper_rewarms_it(
    _isolated, monkeypatch, caplog
):
    caplog.set_level("INFO", logger="reachy.sense")
    with _composed(monkeypatch) as (media, drivers, res):
        face = _only(drivers, FaceSenseDriver)
        assert face._on_stale is not None, "the stale route was built but never wired (#176)"
        assert media.connected, "composition warms the held client"
        face._stream_stale_s = 0.05  # the 10 s window, shortened for the test
        face._frame_interval_s = 0.0
        # Frames flow: the driver records a usable frame.
        face(_Ctx(0.0))
        assert face.peek_last_frame_at() is not None
        # Then the pipeline dies silently: still connected, still camera_available.
        media.streaming = False
        deadline = time.monotonic() + 2.0
        while media.connected and time.monotonic() < deadline:
            face(_Ctx(1.0))
            time.sleep(0.01)
        assert media.drops == [face_sense.REASON_STREAM_ENDED], "drop never reached the client"
        assert not media.connected
        assert "camera-stream-ended" in caplog.text
        # The keeper's UNCHANGED poll re-warms a disconnected holder.
        res.keeper.poll_once()
        assert media.connected and media.warm_ups >= 2
        # Frames flow again through the same held client — no process restart.
        media.streaming = True
        face(_Ctx(2.0))
        assert face.peek_frame_available() is True


def test_a_robot_with_no_camera_is_never_dropped(_isolated, monkeypatch):
    media = _FakeMedia(camera_available=False)
    with _composed(monkeypatch, media) as (media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        face._stream_stale_s = 0.0
        for i in range(20):
            face(_Ctx(float(i)))
        assert media.drops == []
        assert media.connected


# --------------------------------------------------------------------------- #
# 3. #176 — liveness on the senses block comes from the face driver            #
# --------------------------------------------------------------------------- #


def test_the_availability_rider_reads_the_face_drivers_last_frame_on_its_clock(
    _isolated, monkeypatch
):
    with _composed(monkeypatch) as (_media, drivers, _res):
        face = _only(drivers, FaceSenseDriver)
        avail = _only(drivers, SenseAvailabilityDriver)
        assert avail._frame_liveness is not None, "liveness was built but never wired (#176)"
        assert avail._frame_liveness.__self__ is face
        assert avail._clock is face.clock


# --------------------------------------------------------------------------- #
# 4. Boundary — no new rule-visible sense field                                #
# --------------------------------------------------------------------------- #


def test_no_new_rule_visible_sense_field_appeared():
    assert "live" not in _COMPOSED_PROVIDER_FIELDS
    assert "last_frame_at" not in _COMPOSED_PROVIDER_FIELDS
    assert "self_moving" in _COMPOSED_PROVIDER_FIELDS  # the latch the gate reads, unchanged
