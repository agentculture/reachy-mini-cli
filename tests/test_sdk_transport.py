"""Tests for SdkTransport.doa() and SdkTransport.media_session().

All tests use a *stubbed* ``reachy_mini`` — ``SdkTransport._import`` is
monkeypatched to return a fake ``ReachyMini`` class, so no real hardware or
installed SDK is needed.  The live daemon owns the robot; importing the real
SDK in a test could conflict.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy.robot.sdk_transport import MediaSession, SdkTransport

# ---------------------------------------------------------------------------
# Fake reachy_mini infrastructure
# ---------------------------------------------------------------------------


class _FakeMedia:
    """Minimal stand-in for ``ReachyMini.media`` (the real 1.9.x ``MediaManager``).

    Mirrors the real surface the transport now uses: ``camera`` (a handle or
    ``None`` when no camera is initialised) and ``get_frame()`` (the BGR ndarray,
    or ``None`` when no frame is ready this instant — exactly what
    ``MediaManager.get_frame`` returns).
    """

    def __init__(
        self,
        *,
        doa_return=None,
        audio_return=None,
        samplerate=16000,
        channels=1,
        camera_available=True,
        frame=None,
    ) -> None:
        self._doa_return = doa_return
        self._audio_return = audio_return
        self._samplerate = samplerate
        self._channels = channels
        self.recording_started = False
        self.recording_stopped = False
        # A truthy sentinel stands in for the GStreamerCamera handle; ``None``
        # models "no camera initialised" (NO_MEDIA / no hardware).
        self.camera: object | None = object() if camera_available else None
        self._frame = frame

    def start_recording(self) -> None:
        self.recording_started = True

    def stop_recording(self) -> None:
        self.recording_stopped = True

    def get_DoA(self):  # noqa: N802 — matches SDK spelling
        return self._doa_return

    def get_audio_sample(self):
        return self._audio_return

    def get_input_audio_samplerate(self) -> int:
        return self._samplerate

    def get_input_channels(self) -> int:
        return self._channels

    def get_frame(self):
        """Return the frame, or ``None`` when no camera (matches MediaManager)."""
        if self.camera is None:
            return None
        return self._frame


class _FakeMini:
    """Minimal stand-in for a ``ReachyMini`` instance returned by context manager.

    Models the real 1.9.x media-ownership surface: ``media`` (the MediaManager),
    ``media_released`` / ``acquire_media()`` (daemon media hand-off; ``acquire``
    re-creates the manager, matching ``ReachyMini.acquire_media``). It exposes
    NEITHER the removed ``is_local_camera_available()`` method nor a
    ``media_manager.camera`` attribute — the guessed APIs the transport used to
    reference.
    """

    def __init__(
        self, *, head_pose=None, media_released=False, **media_kwargs
    ) -> None:  # type: ignore[no-untyped-def]
        self._media_kwargs = media_kwargs
        # Faithful to the SDK: while media is released the manager has no camera
        # (NO_MEDIA); acquire_media() re-creates it with the camera restored.
        if media_released:
            self.media = _FakeMedia(camera_available=False)
        else:
            self.media = _FakeMedia(**media_kwargs)
        self.gotos: list[dict] = []  # type: ignore[type-arg]
        self._head_pose = np.eye(4) if head_pose is None else head_pose
        self.media_released = media_released
        self.acquired = 0

    def acquire_media(self) -> None:
        self.acquired += 1
        self.media_released = False
        # The SDK re-creates the media manager on acquire; a stale cached handle
        # must be refreshed by the caller (the transport does).
        self.media = _FakeMedia(**self._media_kwargs)

    def get_current_head_pose(self):
        return self._head_pose

    def goto_target(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self.gotos.append(kwargs)

    def __enter__(self):
        return self

    def __exit__(self, *exc: object) -> bool:
        return False


class _FakeMiniCls:
    """A fake ``ReachyMini`` class (callable that returns ``_FakeMini``)."""

    def __init__(self, **kwargs) -> None:  # type: ignore[no-untyped-def]
        self._kwargs = kwargs
        self._instances: list[_FakeMini] = []

    def __call__(self):
        inst = _FakeMini(**self._kwargs)
        self._instances.append(inst)
        return inst

    @property
    def last(self) -> _FakeMini:
        return self._instances[-1]


def _patch_import(monkeypatch, fake_cls: _FakeMiniCls) -> None:
    """Make ``SdkTransport._import`` return the fake class (and a no-op pose factory)."""

    def _fake_import():
        return fake_cls, lambda **kw: kw  # create_head_pose stub

    monkeypatch.setattr(SdkTransport, "_import", staticmethod(_fake_import))


# ---------------------------------------------------------------------------
# doa()
# ---------------------------------------------------------------------------


def test_doa_maps_tuple_to_dict(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """doa() must map (angle, speech) tuple to the expected dict shape."""
    fake_cls = _FakeMiniCls(doa_return=(1.05, True))
    _patch_import(monkeypatch, fake_cls)

    result = SdkTransport().doa()

    assert result == {"angle": 1.05, "speech_detected": True}


def test_doa_maps_false_speech(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """doa() correctly carries speech_detected=False."""
    fake_cls = _FakeMiniCls(doa_return=(0.0, False))
    _patch_import(monkeypatch, fake_cls)

    result = SdkTransport().doa()

    assert result == {"angle": 0.0, "speech_detected": False}


def test_doa_none_returns_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """When get_DoA() returns None the transport must also return None."""
    fake_cls = _FakeMiniCls(doa_return=None)
    _patch_import(monkeypatch, fake_cls)

    result = SdkTransport().doa()

    assert result is None


# ---------------------------------------------------------------------------
# media_session()
# ---------------------------------------------------------------------------


def test_media_session_calls_start_and_stop(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session() must call start_recording on enter and stop_recording on exit."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session():
        # at this point recording should have started
        assert fake_cls.last.media.recording_started

    # after context exit recording should have stopped
    assert fake_cls.last.media.recording_stopped


def test_media_session_doa_passthrough(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().doa() returns the dict-shaped result from the SDK."""
    fake_cls = _FakeMiniCls(doa_return=(1.05, True))
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.doa()

    assert result == {"angle": 1.05, "speech_detected": True}


def test_read_doa_passes_timeout_to_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """read_doa(session, timeout=...) must work — MediaSession.doa accepts (ignores) timeout.

    Regression for Qodo PR #24 comment 3: the SDK listen loop polls DoA via
    ``read_doa(session, timeout=DOA_TIMEOUT)``, which calls ``session.doa(timeout=...)``.
    A missing ``timeout`` kwarg raised TypeError (swallowed by DoaPoller), so the SDK
    listen path never received any reading.
    """
    from reachy.behavior.sense import read_doa

    fake_cls = _FakeMiniCls(doa_return=(1.05, True))
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        # The exact call read_doa makes — must not raise.
        sense = read_doa(session, timeout=0.1)
        assert session.doa(timeout=0.1) == {"angle": 1.05, "speech_detected": True}

    assert sense.doa_angle == 1.05 and sense.speech_detected is True


def test_media_session_doa_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().doa() returns None when SDK returns None."""
    fake_cls = _FakeMiniCls(doa_return=None)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.doa()

    assert result is None


def test_media_session_get_audio_sample(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().get_audio_sample() passes through the fake ndarray."""
    chunk = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    fake_cls = _FakeMiniCls(audio_return=chunk)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.get_audio_sample()

    assert result is chunk


def test_media_session_get_audio_sample_none(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().get_audio_sample() returns None when SDK returns None."""
    fake_cls = _FakeMiniCls(audio_return=None)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.get_audio_sample()

    assert result is None


def test_media_session_samplerate(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().samplerate is read from the SDK."""
    fake_cls = _FakeMiniCls(samplerate=44100)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert session.samplerate == 44100


def test_media_session_channels(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """media_session().channels is read from the SDK."""
    fake_cls = _FakeMiniCls(channels=2)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert session.channels == 2


def test_media_session_stop_on_exception(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """stop_recording() is called even if the body of the with block raises."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    with pytest.raises(RuntimeError):
        with SdkTransport().media_session():
            raise RuntimeError("boom")

    assert fake_cls.last.media.recording_stopped


def test_media_session_yields_media_session(monkeypatch) -> None:  # type: ignore[no-untyped-def]
    """The object yielded by media_session() is a MediaSession instance."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert isinstance(session, MediaSession)


# ---------------------------------------------------------------------------
# MediaSession serves pose / move / frame through the ONE open client (issue #51)
# ---------------------------------------------------------------------------


def test_media_session_head_pose_uses_held_client(monkeypatch) -> None:
    """head_pose() reads the pose off the already-open client (identity -> (0, 0))."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert session.head_pose() == (0.0, 0.0)


def test_media_session_move_goto_streams_through_held_client(monkeypatch) -> None:
    """move_goto() drives goto_target on the held client (no new session)."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.move_goto(
            head={"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 5, "yaw": 0},
            duration=0.3,
            interpolation="minjerk",
        )

    assert result == {"status": "ok", "transport": "sdk", "action": "goto"}
    assert len(fake_cls.last.gotos) == 1
    assert fake_cls.last.gotos[0]["method"] == "minjerk"


def test_media_session_get_frame_uses_held_camera(monkeypatch) -> None:
    """get_frame() returns the held client's media.get_frame() (no new session)."""
    fake_cls = _FakeMiniCls(frame="FRAME")
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert session.get_frame() == "FRAME"


def test_media_session_get_frame_raises_when_no_camera(monkeypatch) -> None:
    """get_frame() raises a clean CliError(exit-2) when media.camera is None."""
    from reachy.cli._errors import EXIT_ENV_ERROR, CliError

    fake_cls = _FakeMiniCls(camera_available=False)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        with pytest.raises(CliError) as excinfo:
            session.get_frame()

    assert excinfo.value.code == EXIT_ENV_ERROR
    assert "camera" in excinfo.value.message.lower()


def test_media_session_get_frame_none_when_no_frame_ready(monkeypatch) -> None:
    """A camera present but no frame ready (media.get_frame() -> None) degrades to None.

    This is the documented "no frame this instant" path: the transport does NOT
    raise; it returns None and the caller (grabber / producer) skips the tick.
    """
    fake_cls = _FakeMiniCls(camera_available=True, frame=None)
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        assert session.get_frame() is None


def test_media_session_get_frame_acquires_released_media(monkeypatch) -> None:
    """When the daemon's media is released, get_frame() calls acquire_media() once.

    Honors ``acquire_media`` where the SDK requires it (real 1.9.x surface): a
    released media manager has no camera, so the transport re-acquires it, then
    refreshes its cached handle and serves the frame.
    """
    fake_cls = _FakeMiniCls(media_released=True, frame="FRAME")
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        result = session.get_frame()

    assert result == "FRAME"
    assert fake_cls.last.acquired == 1
    assert fake_cls.last.media_released is False


def test_per_tick_reads_open_exactly_one_client(monkeypatch) -> None:
    """Issue #51: many per-tick pose/move/frame reads must construct ONE client.

    The crash-loop was a fd leak from ``head_pose``/``move_goto``/``get_frame``
    each opening (and the SDK's ``GStreamerAudio`` teardown leaking) a fresh
    ``ReachyMini`` per call. Routing them through the open ``MediaSession`` means
    the whole loop builds exactly one client, no matter how many reads.
    """
    fake_cls = _FakeMiniCls(frame="FRAME")
    _patch_import(monkeypatch, fake_cls)

    with SdkTransport().media_session() as session:
        for _ in range(50):  # 50 ticks' worth of reads
            session.head_pose()
            session.get_frame()
            session.move_goto(duration=0.1, interpolation="minjerk")

    assert len(fake_cls._instances) == 1, (
        "per-tick reads must reuse the one open client, not open a new ReachyMini "
        f"per call; opened {len(fake_cls._instances)}"
    )
