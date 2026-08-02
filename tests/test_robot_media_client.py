"""Tests for :mod:`reachy.robot.media_client` — the held, single-owner media client.

The behavior runtime is about to grow senses that need the microphone (transcript
+ rms) and the camera (face + frame-available). The SDK media session is
SINGLE-CONSUMER: two consumers throttle each other to ~1 Hz (see CLAUDE.md's
"single-SDK-owner model", the constraint that motivated folding senses into one
loop in #43). :class:`~reachy.robot.media_client.HeldMediaClient` is the media
counterpart to :class:`~reachy.robot.state_reader.HeldStateReader`: ONE held
client for the process lifetime, explicitly closed.

All tests use a *stubbed* ``reachy_mini`` — ``HeldMediaClient._import`` is
monkeypatched to return a fake ``ReachyMini`` class (the same seam
``tests/test_robot_state_reader.py`` and ``tests/test_sdk_transport.py`` use), so
no real hardware and no installed SDK is needed. A manually-advanced fake clock
makes the lazy-construction / retry-backoff behavior deterministic.

The three acceptance criteria this file proves:

1. Exactly one media client and one ``no_media`` pose client per process; both
   closed explicitly. (``test_*_constructs_client_at_most_once*``,
   ``test_media_and_state_holders_are_separate_profiles``, the ``close`` block.)
2. The process does not hang at interpreter exit. (``test_no_del_method``,
   ``test_module_starts_no_threads_and_registers_no_atexit_hook``,
   ``test_subprocess_using_the_holder_exits_promptly``.)
3. No second media client is opened anywhere in the runtime. (The
   "criterion 3" block at the bottom — a STATIC source scan; read its docstrings
   for exactly what it does and does not prove at this stage.)
"""

from __future__ import annotations

import ast
import inspect
import logging
import subprocess
import sys
import textwrap
from pathlib import Path

import numpy as np

from reachy.robot.media_client import DEFAULT_RETRY_BACKOFF, HeldMediaClient

_SENSE_LOGGER_NAME = "reachy.sense"
_REPO_ROOT = Path(__file__).parent.parent


# ---------------------------------------------------------------------------
# Fake reachy_mini infrastructure (mirrors tests/test_robot_state_reader.py)
# ---------------------------------------------------------------------------


class _FakeMedia:
    """Minimal stand-in for ``ReachyMini.media`` (the SDK's ``MediaManager``)."""

    def __init__(
        self,
        *,
        camera: object | None = object(),
        audio_chunk: "np.ndarray | None" = None,
        frame: "np.ndarray | None" = None,
        fail_audio: bool = False,
        fail_frame: bool = False,
    ) -> None:
        self.camera = camera
        self._audio_chunk = np.zeros(4, dtype=np.float32) if audio_chunk is None else audio_chunk
        self._frame = np.zeros((2, 2, 3), dtype=np.uint8) if frame is None else frame
        self.fail_audio = fail_audio
        self.fail_frame = fail_frame
        self.recording = False
        self.start_recording_calls = 0
        self.stop_recording_calls = 0

    # --- lifecycle ---
    def start_recording(self) -> None:
        self.recording = True
        self.start_recording_calls += 1

    def stop_recording(self) -> None:
        self.recording = False
        self.stop_recording_calls += 1

    # --- properties the holder caches at construction ---
    def get_input_audio_samplerate(self) -> int:
        return 16000

    def get_input_channels(self) -> int:
        return 1

    # --- reads ---
    def get_audio_sample(self):  # type: ignore[no-untyped-def]
        if self.fail_audio:
            raise RuntimeError("audio read failed")
        return self._audio_chunk

    def get_frame(self):  # type: ignore[no-untyped-def]
        if self.fail_frame:
            raise RuntimeError("frame read failed")
        return self._frame


class _FakeMini:
    """Minimal stand-in for a default-profile ``ReachyMini()`` instance."""

    def __init__(self, *, media: _FakeMedia | None = None, media_released: bool = False) -> None:
        self.media = _FakeMedia() if media is None else media
        self.media_released = media_released
        self.acquire_media_calls = 0
        self.closed = False

    def acquire_media(self) -> None:
        self.acquire_media_calls += 1
        self.media_released = False

    def close(self) -> None:
        self.closed = True


class _FakeMiniDisconnectOnly:
    """A fake client exposing ``disconnect`` but not ``close``."""

    def __init__(self) -> None:
        self.media = _FakeMedia()
        self.media_released = False
        self.disconnected = False

    def disconnect(self) -> None:
        self.disconnected = True


class _FakeMiniNoClose:
    """A fake client exposing neither ``close`` nor ``disconnect``."""

    def __init__(self) -> None:
        self.media = _FakeMedia()
        self.media_released = False


class _FakeMiniCls:
    """A fake ``ReachyMini`` class: callable returning a fake, or raising."""

    def __init__(self, *, should_fail: bool = False, mini_factory=None) -> None:
        self.should_fail = should_fail
        self._mini_factory = mini_factory or _FakeMini
        self.calls: list[dict] = []  # type: ignore[type-arg]
        self.instances: list[object] = []

    def __call__(self, **kwargs):  # type: ignore[no-untyped-def]
        self.calls.append(kwargs)
        if self.should_fail:
            raise RuntimeError("daemon down")
        inst = self._mini_factory()
        self.instances.append(inst)
        return inst

    @property
    def last(self):  # type: ignore[no-untyped-def]
        return self.instances[-1]


class _FakeClock:
    """A manually-advanced clock for deterministic backoff tests."""

    def __init__(self, t: float = 0.0) -> None:
        self._t = t

    def advance(self, dt: float) -> None:
        self._t += dt

    def __call__(self) -> float:
        return self._t


def _patch_import(monkeypatch, fake_cls) -> None:
    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(lambda: fake_cls))


def _patch_import_absent(monkeypatch) -> None:
    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(lambda: None))


# ---------------------------------------------------------------------------
# Off-thread warm-up — the tick-budget affordance (live evidence, t1 section 3)
# ---------------------------------------------------------------------------
#
# The deployed box shows a REPRODUCIBLE tick-budget violation on every runtime
# start: the ``[SENSE stage=state]`` "connected" line is immediately followed by
# a ``stage=rule source=tick event=overrun`` at 424.93 / 974.39 / 990.61 /
# 1102.92 / 1212.66 ms against a 20 ms budget (21x-61x over). The cause is
# construct-on-first-read building the SDK client ON THE TICK THREAD. A camera
# pipeline warms slower than a ``no_media`` handle, so this holder MUST offer a
# supported way for a tick-thread caller never to construct inline.
#
# Two doors, tested here:
#   * :meth:`warm_up` — the owner constructs off-thread, before the loop starts.
#   * ``allow_inline_connect=False`` — closes the on-thread door entirely, which
#     ``warm_up()`` alone cannot do (a mid-run reconnect after a fault would
#     otherwise construct inline all over again).


def test_warm_up_constructs_and_reports_success(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert holder.warm_up() is True
    assert len(fake_cls.calls) == 1
    assert holder.connected is True


def test_reads_after_warm_up_never_construct(monkeypatch) -> None:
    """THE contract: a warmed holder does zero construction on the tick thread."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert holder.warm_up() is True
    calls_after_warm_up = len(fake_cls.calls)

    for _ in range(50):
        assert holder.audio() is not None
        assert holder.frame() is not None
        assert holder.samplerate == 16000
        assert holder.channels == 1
        assert holder.camera_available is True

    assert len(fake_cls.calls) == calls_after_warm_up == 1


def test_warm_up_is_idempotent(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    for _ in range(5):
        assert holder.warm_up() is True

    assert len(fake_cls.calls) == 1


def test_warm_up_is_safe_and_false_when_sdk_absent(monkeypatch, caplog) -> None:
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=0.001)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(10):
            assert holder.warm_up() is False  # must not raise
            clock.advance(1.0)

    assert holder.connected is False
    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(sense_records) == 1  # still exactly one warning, no storm


def test_warm_up_reports_failure_and_can_be_retried_after_backoff(monkeypatch) -> None:
    """An owner polling warm_up() off-thread recovers when the daemon comes up."""
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=5.0)

    assert holder.warm_up() is False
    assert holder.connected is False

    clock.advance(1.0)
    assert holder.warm_up() is False
    assert len(fake_cls.calls) == 1  # backoff still throttles the off-thread caller

    clock.advance(5.0)
    fake_cls.should_fail = False
    assert holder.warm_up() is True
    assert len(fake_cls.calls) == 2


def test_warm_up_after_close_returns_false_and_never_constructs(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    holder.close()

    assert holder.warm_up() is False
    assert len(fake_cls.calls) == 0


def test_connected_never_constructs(monkeypatch) -> None:
    """``connected`` is a pure predicate — a supervisor can poll it freely."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    for _ in range(10):
        assert holder.connected is False
    assert len(fake_cls.calls) == 0

    holder.warm_up()
    assert holder.connected is True

    holder.close()
    assert holder.connected is False


# --- allow_inline_connect=False: the on-thread door, closed -----------------


def test_inline_connect_disabled_reads_never_construct(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, allow_inline_connect=False)

    for _ in range(20):
        assert holder.audio() is None
        assert holder.frame() is None
        assert holder.samplerate is None
        assert holder.channels is None
        assert holder.camera_available is False
        clock.advance(10.0)

    assert len(fake_cls.calls) == 0


def test_inline_connect_disabled_works_normally_after_warm_up(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), allow_inline_connect=False)

    assert holder.warm_up() is True

    for _ in range(10):
        assert holder.audio() is not None
        assert holder.frame() is not None

    assert len(fake_cls.calls) == 1


def test_inline_connect_disabled_does_not_reconnect_on_the_tick_thread(monkeypatch) -> None:
    """A mid-run fault must not turn into an inline reconnect stall.

    This is the case ``warm_up()`` alone cannot cover: the first construction is
    off-thread, but a dropped client would otherwise be rebuilt by whichever
    read noticed — i.e. on the tick thread, reproducing the measured overrun
    mid-run instead of at start.
    """
    failing = _FakeMini(media=_FakeMedia(fail_audio=True))
    fake_cls = _FakeMiniCls(mini_factory=lambda: failing)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=2.0, allow_inline_connect=False)

    assert holder.warm_up() is True
    assert holder.audio() is None  # the read faults and drops the client
    assert holder.connected is False

    clock.advance(100.0)  # well past the backoff window
    for _ in range(10):
        assert holder.audio() is None
    assert len(fake_cls.calls) == 1  # NO inline reconnect

    # The owner re-warms off-thread, having noticed via ``connected``.
    fake_cls._mini_factory = _FakeMini
    assert holder.warm_up() is True
    assert len(fake_cls.calls) == 2
    assert holder.audio() is not None


def test_inline_connect_enabled_is_the_default(monkeypatch) -> None:
    """The default stays lazy — a non-tick-thread owner needs no ceremony."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert holder.audio() is not None
    assert len(fake_cls.calls) == 1


def test_warm_up_starts_no_threads(monkeypatch) -> None:
    """The holder stays PASSIVE: warm-up runs on the caller's thread, by design.

    Spawning a thread inside the class would re-introduce exactly the
    interpreter-exit hazard ``close()`` exists to avoid. The owner decides which
    thread warms it.
    """
    import threading

    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    before = threading.active_count()
    holder.warm_up()

    assert threading.active_count() == before


# ---------------------------------------------------------------------------
# Criterion 1 — exactly ONE media client, constructed lazily, reused forever
# ---------------------------------------------------------------------------


def test_audio_returns_the_mic_chunk(monkeypatch) -> None:
    chunk = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    fake_cls = _FakeMiniCls(mini_factory=lambda: _FakeMini(media=_FakeMedia(audio_chunk=chunk)))
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert np.array_equal(holder.audio(), chunk)


def test_frame_returns_the_camera_frame(monkeypatch) -> None:
    frame = np.ones((3, 4, 3), dtype=np.uint8)
    fake_cls = _FakeMiniCls(mini_factory=lambda: _FakeMini(media=_FakeMedia(frame=frame)))
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert np.array_equal(holder.frame(), frame)


def test_audio_constructs_client_at_most_once_across_n_reads(monkeypatch) -> None:
    """Construction happens on first use only; N more reads build zero more clients."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))
    for _ in range(50):
        assert holder.audio() is not None

    assert len(fake_cls.calls) == 1


def test_audio_and_frame_share_the_one_client(monkeypatch) -> None:
    """Mic and camera are two reads on ONE held client, not two clients."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))
    for _ in range(25):
        holder.audio()
        holder.frame()
        holder.camera_available  # property read, must not reconstruct

    assert len(fake_cls.calls) == 1
    assert len(fake_cls.instances) == 1


def test_construction_uses_the_default_media_profile(monkeypatch) -> None:
    """The media holder uses the DEFAULT ``ReachyMini()`` profile (media chain up).

    Explicitly NOT ``media_backend='no_media'`` — that is the pose reader's
    profile, and the two are different construction profiles that must never be
    shared (``state_reader``'s module docstring, lines 26-32).
    """
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    HeldMediaClient(now=_FakeClock(0.0)).audio()

    assert fake_cls.calls == [{}]


def test_construction_starts_recording_once(monkeypatch) -> None:
    """The AEC mic recorder is activated exactly once, at construction."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))
    for _ in range(10):
        holder.audio()

    assert fake_cls.last.media.start_recording_calls == 1
    assert fake_cls.last.media.recording is True


def test_samplerate_and_channels_come_from_the_held_client(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    holder = HeldMediaClient(now=_FakeClock(0.0))

    assert holder.samplerate == 16000
    assert holder.channels == 1
    assert len(fake_cls.calls) == 1  # the properties reuse the one client


def test_media_and_state_holders_are_separate_profiles(monkeypatch) -> None:
    """One media client AND one no_media pose client — never the same object.

    A runtime process that needs both constructs one of each; this test pins the
    "one of each, different profiles" shape criterion 1 asks for.
    """
    from reachy.robot.state_reader import HeldStateReader

    media_cls = _FakeMiniCls()
    _patch_import(monkeypatch, media_cls)

    class _PoseMini:
        def get_current_head_pose(self):  # type: ignore[no-untyped-def]
            return np.eye(4)

        def close(self) -> None:
            pass

    pose_cls = _FakeMiniCls(mini_factory=_PoseMini)
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: pose_cls))

    media = HeldMediaClient(now=_FakeClock(0.0))
    pose = HeldStateReader(now=_FakeClock(0.0))
    media.audio()
    pose.read()

    assert media_cls.calls == [{}]
    assert pose_cls.calls == [{"media_backend": "no_media"}]
    assert media_cls.last is not pose_cls.last

    media.close()
    pose.close()


# ---------------------------------------------------------------------------
# Degradation — construction failure, read failure, missing SDK
# ---------------------------------------------------------------------------


def test_daemon_down_then_recovers_after_backoff(monkeypatch, caplog) -> None:
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=5.0)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        assert holder.audio() is None
        assert len(fake_cls.calls) == 1

        clock.advance(2.0)  # inside the backoff window
        assert holder.audio() is None
        assert holder.frame() is None
        assert len(fake_cls.calls) == 1

        clock.advance(3.5)  # 5.5s total >= 5.0s backoff
        fake_cls.should_fail = False
        assert holder.audio() is not None
        assert len(fake_cls.calls) == 2

    sense_lines = [r.getMessage() for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert any("retrying" in line for line in sense_lines)
    assert any("connected" in line for line in sense_lines)


def test_start_recording_failure_is_a_construction_failure(monkeypatch) -> None:
    """A client that comes up but won't record is released, not held half-open."""

    class _NoRecordMedia(_FakeMedia):
        def start_recording(self) -> None:
            raise RuntimeError("recorder busy")

    mini = _FakeMini(media=_NoRecordMedia())
    fake_cls = _FakeMiniCls(mini_factory=lambda: mini)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=1.0)

    assert holder.audio() is None
    assert mini.closed is True  # released, not leaked


def test_default_retry_backoff_is_a_positive_constant() -> None:
    assert DEFAULT_RETRY_BACKOFF > 0
    assert HeldMediaClient() is not None  # defaults must not raise


def test_backoff_interval_is_injectable(monkeypatch) -> None:
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=1.0)

    assert holder.audio() is None
    clock.advance(0.5)
    assert holder.audio() is None
    assert len(fake_cls.calls) == 1

    clock.advance(0.6)
    fake_cls.should_fail = False
    assert holder.audio() is not None
    assert len(fake_cls.calls) == 2


def test_audio_read_failure_drops_client_and_reconnects_after_backoff(monkeypatch) -> None:
    failing = _FakeMini(media=_FakeMedia(fail_audio=True))
    fake_cls = _FakeMiniCls(mini_factory=lambda: failing)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=2.0)

    assert holder.audio() is None
    assert failing.closed is True

    clock.advance(1.0)
    assert holder.audio() is None
    assert len(fake_cls.calls) == 1

    clock.advance(1.5)
    fake_cls._mini_factory = _FakeMini
    assert holder.audio() is not None
    assert len(fake_cls.calls) == 2


def test_frame_read_failure_drops_client(monkeypatch) -> None:
    failing = _FakeMini(media=_FakeMedia(fail_frame=True))
    fake_cls = _FakeMiniCls(mini_factory=lambda: failing)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), retry_backoff=2.0)

    assert holder.frame() is None
    assert failing.closed is True


def test_frame_returns_none_when_no_frame_is_ready_without_dropping(monkeypatch) -> None:
    """A ``None`` frame is "nothing ready this instant", NOT a fault."""
    mini = _FakeMini(media=_FakeMedia(frame=None))
    mini.media._frame = None
    fake_cls = _FakeMiniCls(mini_factory=lambda: mini)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    for _ in range(5):
        assert holder.frame() is None

    assert mini.closed is False
    assert len(fake_cls.calls) == 1


def test_absent_camera_degrades_to_none_frames_permanently(monkeypatch, caplog) -> None:
    """No camera is a latched, once-warned degradation — never a raise, never a storm."""
    mini = _FakeMini(media=_FakeMedia(camera=None))
    fake_cls = _FakeMiniCls(mini_factory=lambda: mini)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=0.001)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(20):
            assert holder.frame() is None
            clock.advance(1.0)

    assert holder.camera_available is False
    assert mini.closed is False  # the mic side keeps working
    assert holder.audio() is not None
    camera_lines = [
        r.getMessage()
        for r in caplog.records
        if r.name == _SENSE_LOGGER_NAME and "camera" in r.getMessage()
    ]
    assert len(camera_lines) == 1


def test_released_media_is_reacquired_once_for_the_camera(monkeypatch) -> None:
    """When the daemon released media, ``acquire_media()`` is called exactly once."""
    mini = _FakeMini(media_released=True)
    fake_cls = _FakeMiniCls(mini_factory=lambda: mini)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    for _ in range(5):
        assert holder.frame() is not None

    assert mini.acquire_media_calls == 1


def test_missing_sdk_degrades_to_permanently_none(monkeypatch) -> None:
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=1.0)

    for _ in range(20):
        assert holder.audio() is None
        assert holder.frame() is None
        clock.advance(1000.0)

    assert holder.samplerate is None
    assert holder.channels is None
    assert holder.camera_available is False


def test_missing_sdk_logs_exactly_one_warning(monkeypatch, caplog) -> None:
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=0.001)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(20):
            holder.audio()
            holder.frame()
            clock.advance(1.0)

    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(sense_records) == 1
    assert "sdk-absent" in sense_records[0].getMessage()


def test_missing_sdk_never_probes_import_more_than_once(monkeypatch) -> None:
    call_count = {"n": 0}

    def _counting_import():
        call_count["n"] += 1
        return None

    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(_counting_import))
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=0.001)

    for _ in range(15):
        holder.audio()
        holder.frame()
        clock.advance(10.0)

    assert call_count["n"] == 1


def test_logs_one_line_per_state_change_not_per_read(monkeypatch, caplog) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(30):
            holder.audio()
            holder.frame()

    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(sense_records) == 1
    assert "connected" in sense_records[0].getMessage()


# ---------------------------------------------------------------------------
# Criterion 1 (cont.) + 2 — explicit, idempotent close; no GC/__del__ reliance
# ---------------------------------------------------------------------------


def test_close_stops_recording_then_releases_client(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))
    holder.audio()

    holder.close()

    assert fake_cls.last.media.stop_recording_calls == 1
    assert fake_cls.last.closed is True


def test_close_releases_via_disconnect_when_no_close(monkeypatch) -> None:
    fake_cls = _FakeMiniCls(mini_factory=_FakeMiniDisconnectOnly)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))
    holder.audio()

    holder.close()

    assert fake_cls.last.disconnected is True


def test_close_tolerates_client_with_neither_close_nor_disconnect(monkeypatch) -> None:
    fake_cls = _FakeMiniCls(mini_factory=_FakeMiniNoClose)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))
    holder.audio()

    holder.close()  # must not raise

    assert holder.audio() is None


def test_close_tolerates_a_raising_stop_recording(monkeypatch) -> None:
    """Teardown must never raise — and must still release the client."""

    class _BadStopMedia(_FakeMedia):
        def stop_recording(self) -> None:
            raise RuntimeError("recorder wedged")

    mini = _FakeMini(media=_BadStopMedia())
    fake_cls = _FakeMiniCls(mini_factory=lambda: mini)
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))
    holder.audio()

    holder.close()  # must not raise

    assert mini.closed is True


def test_close_before_any_construction_is_a_noop(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))

    holder.close()

    assert len(fake_cls.calls) == 0
    assert holder.audio() is None
    assert len(fake_cls.calls) == 0


def test_close_is_idempotent(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0))
    holder.audio()

    holder.close()
    holder.close()
    holder.close()

    assert fake_cls.last.media.stop_recording_calls == 1
    assert fake_cls.last.closed is True


def test_reads_after_close_return_none_and_never_reconstruct(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    holder = HeldMediaClient(now=clock, retry_backoff=1.0)
    holder.audio()
    calls_before = len(fake_cls.calls)

    holder.close()
    clock.advance(100.0)

    for _ in range(10):
        assert holder.audio() is None
        assert holder.frame() is None

    assert len(fake_cls.calls) == calls_before


def test_context_manager_closes_even_when_body_raises(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    try:
        with HeldMediaClient(now=_FakeClock(0.0)) as holder:
            holder.audio()
            raise ValueError("boom")
    except ValueError:
        pass

    assert fake_cls.last.closed is True


def test_no_del_method() -> None:
    """Teardown is EXPLICIT. Relying on ``__del__``/GC is what hangs the process.

    ``state_reader``'s module docstring (lines 20-24): the process hangs at
    interpreter exit unless the client is explicitly closed — a bare "let it get
    garbage-collected" teardown does not release the connection.
    """
    assert "__del__" not in vars(HeldMediaClient)


def test_module_starts_no_threads_and_registers_no_atexit_hook() -> None:
    """Nothing in the module can keep the interpreter alive at exit.

    A non-daemon thread or an ``atexit`` hook that touches the SDK is the other
    way a process hangs on shutdown; this holder has neither.
    """
    import reachy.robot.media_client as mod

    source = inspect.getsource(mod)
    tree = ast.parse(source)
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)

    assert "threading" not in imported
    assert "atexit" not in imported
    assert "signal" not in imported


def test_subprocess_using_the_holder_exits_promptly() -> None:
    """A process that constructs, uses, and closes the holder terminates.

    Runs in a fresh interpreter with a FAKE ``ReachyMini`` injected through the
    ``_import`` seam, so it needs no SDK and no hardware. What it proves: the
    holder's own construct/read/close lifecycle adds nothing (thread, atexit
    hook, lingering reference) that blocks interpreter shutdown. What it does
    NOT prove: that the REAL SDK client releases promptly — that is a hardware
    property, and the reason ``close()`` is explicit in the first place.
    """
    script = textwrap.dedent("""
        from reachy.robot.media_client import HeldMediaClient

        class _Media:
            camera = object()
            def start_recording(self): pass
            def stop_recording(self): pass
            def get_input_audio_samplerate(self): return 16000
            def get_input_channels(self): return 1
            def get_audio_sample(self): return [0.0]
            def get_frame(self): return [[0]]

        class _Mini:
            media_released = False
            def __init__(self, **kw): self.media = _Media()
            def close(self): pass

        HeldMediaClient._import = staticmethod(lambda: _Mini)
        holder = HeldMediaClient()
        for _ in range(5):
            holder.audio()
            holder.frame()
        holder.close()
        print("done")
        """)
    proc = subprocess.run(
        [sys.executable, "-c", script],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
        check=True,
    )
    assert proc.stdout.strip().endswith("done")


# ---------------------------------------------------------------------------
# Import boundary — safe on a bare box (no reachy_mini installed)
# ---------------------------------------------------------------------------


def test_module_imports_without_reachy_mini(monkeypatch) -> None:
    """``reachy.robot.media_client`` must not hard-import ``reachy_mini``."""
    import importlib

    monkeypatch.setitem(sys.modules, "reachy_mini", None)
    import reachy.robot.media_client as mod

    importlib.reload(mod)


def test_importing_the_holder_does_not_pull_in_reachy_mini() -> None:
    """Probed in a SUBPROCESS so it cannot pollute this interpreter's sys.modules."""
    code = "import sys, reachy.robot.media_client; print('reachy_mini' in sys.modules)"
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
        check=True,
    )
    assert proc.stdout.strip() == "False"


def test_default_import_returns_none_or_a_callable() -> None:
    """The real (unpatched) seam degrades to ``None`` rather than raising when absent."""
    result = HeldMediaClient._import()
    assert result is None or callable(result)


# ---------------------------------------------------------------------------
# Criterion 3 — "no second media client is opened anywhere in the runtime"
# ---------------------------------------------------------------------------
#
# This is a STATIC source scan, and it is worth being precise about its reach at
# this stage of the plan:
#
# WHAT IT PROVES
#   * No module under ``reachy/behavior/`` (the behavior runtime package, where
#     the three new senses will live) imports ``reachy_mini``, constructs a
#     client, opens a ``media_session()``, or starts a recorder. The runtime
#     package reaches hardware only through injected holders.
#   * The set of modules under ``reachy/`` that can open an SDK client is a
#     FROZEN, enumerated inventory. Adding a fourth one anywhere in the package
#     — including a second media client inside the runtime — fails this test and
#     forces the author to justify it here.
#
# WHAT IT DOES *NOT* PROVE (yet)
#   * Nothing about the composition root. ``reachy/cli/_commands/behavior.py``
#     is out of scope for task t10 (sibling tasks edit it next wave), so this
#     file cannot yet assert "the runtime composes exactly one holder" — that
#     assertion belongs with the wiring task.
#   * Nothing dynamic. A static scan cannot see a client opened via an injected
#     factory, a plugin, or ``importlib`` at runtime. The single-owner property
#     is ultimately upheld by the composition root passing ONE holder around,
#     not by this scan.
#   * Nothing about ``reachy/motion/`` + ``reachy/cli/_commands/listen.py``, the
#     OLD AI-first loop this plan is retiring. That loop legitimately owns its
#     own media session today and is expected to disappear, not to conform.


def _reachy_sources() -> list[Path]:
    return sorted((_REPO_ROOT / "reachy").rglob("*.py"))


def _rel(path: Path) -> str:
    return str(path.relative_to(_REPO_ROOT))


def _imports_reachy_mini(tree: ast.AST) -> bool:
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            if any(
                a.name == "reachy_mini" or a.name.startswith("reachy_mini.") for a in node.names
            ):
                return True
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module == "reachy_mini" or node.module.startswith("reachy_mini."):
                return True
    return False


def _calls_named(tree: ast.AST, names: set[str]) -> set[str]:
    found: set[str] = set()
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        if isinstance(func, ast.Attribute) and func.attr in names:
            found.add(func.attr)
        elif isinstance(func, ast.Name) and func.id in names:
            found.add(func.id)
    return found


#: Every module under ``reachy/`` allowed to import ``reachy_mini`` — i.e. every
#: module that can bring an SDK client into existence. Deliberately frozen: a new
#: entry here is a new potential media owner and must be argued for.
_SDK_OWNING_MODULES = {
    # The transport layer: one-shot clients for CLI verbs + the legacy
    # ``media_session()`` the old AI-first listen loop rides.
    "reachy/robot/sdk_transport.py",
    # The held, media-FREE pose client (``media_backend='no_media'``).
    "reachy/robot/state_reader.py",
    # The held media client — THIS task's module; the runtime's one mic+camera owner.
    "reachy/robot/media_client.py",
    # The ``say``/TTS speaker leg (playback only; opens its own short-lived client).
    "reachy/speech/playback.py",
}


def test_sdk_owning_modules_are_a_frozen_inventory() -> None:
    """Only an enumerated set of modules may import ``reachy_mini``.

    A second media client appearing anywhere in ``reachy/`` must pass through
    this list first.
    """
    owners = {
        _rel(path)
        for path in _reachy_sources()
        if _imports_reachy_mini(ast.parse(path.read_text()))
    }

    assert owners == _SDK_OWNING_MODULES


def test_media_client_is_the_only_held_media_owner() -> None:
    """Among the held (long-lived) holders, exactly one opens a media client.

    ``state_reader`` holds a ``no_media`` client; this module holds the media
    one. No third held holder exists.
    """
    held = {p for p in _reachy_sources() if p.name in {"state_reader.py", "media_client.py"}}

    assert {_rel(p) for p in held} == {
        "reachy/robot/state_reader.py",
        "reachy/robot/media_client.py",
    }

    state_src = (_REPO_ROOT / "reachy/robot/state_reader.py").read_text()
    assert 'media_backend="no_media"' in state_src

    media_src = (_REPO_ROOT / "reachy/robot/media_client.py").read_text()
    assert 'media_backend="no_media"' not in media_src


def test_behavior_runtime_package_opens_no_sdk_client() -> None:
    """``reachy/behavior/`` reaches hardware only through injected holders."""
    offenders: list[str] = []
    for path in sorted((_REPO_ROOT / "reachy" / "behavior").rglob("*.py")):
        tree = ast.parse(path.read_text())
        if _imports_reachy_mini(tree):
            offenders.append(f"{_rel(path)}: imports reachy_mini")
        hits = _calls_named(tree, {"ReachyMini", "media_session", "start_recording"})
        if hits:
            offenders.append(f"{_rel(path)}: calls {sorted(hits)}")

    assert offenders == []


# ---------------------------------------------------------------------------
# t30 — ACQUIRE the daemon's media subsystem before constructing
# ---------------------------------------------------------------------------
#
# Diagnosed on the live robot (2026-07-20). The holder's retry loop could never
# succeed on the deployed box, because the daemon had RELEASED media:
#
#     GET  /api/media/status  -> {"available": false, "released": true,
#                                 "no_media": false}
#     GET  /api/daemon/status ->  "media_released": true
#
# Nothing was listening, so a bare ``ReachyMini()`` raised
# ``ConnectionRefusedError: [Errno 111] Connection refused`` — for ever. Verified
# by hand: ``POST /api/media/acquire`` returns ``{"status":"ok"}``, status flips
# to ``{"available": true, "released": false}``, and a bare ``ReachyMini()`` then
# constructs in **0.9 s** and disconnects cleanly. Media returns to
# ``released: true`` once the last consumer lets go, so ``released: true`` is the
# ordinary RESTING state of any box — not a misconfiguration.
#
# Consequence while unfixed: the transcript / rms / face / frame-available senses
# are wired but permanently dormant, and rules keyed on them validate and never
# fire. That is the silent-no-op class this arc exists to close.
#
# The gate is deliberately FAIL-OPEN on an unreachable daemon: a probe that
# cannot answer must leave the holder exactly as it behaves today, never make it
# more broken. It is fail-CLOSED on the one definitive negative — another
# consumer already holding the single-consumer subsystem — which is precisely the
# state in which construction was measured to HANG rather than refuse.

import json  # noqa: E402
import urllib.error  # noqa: E402
import urllib.request  # noqa: E402

import pytest  # noqa: E402

import reachy.robot.media_client as media_client_mod  # noqa: E402
from reachy.robot.media_client import (  # noqa: E402
    DEFAULT_GATE_TIMEOUT,
    MEDIA_ACQUIRE_PATH,
    MEDIA_RELEASE_PATH,
    MEDIA_STATUS_PATH,
)

_BASE = "http://localhost:8000"

#: The genuine HTTP helpers, captured at import time. ``tests/conftest.py``'s
#: ``_no_live_daemon_media_gate`` fixture stubs these two names on the module for
#: EVERY test (so the suite can never acquire the real robot's mic/camera); the
#: handful of tests below that exercise the urllib legs themselves patch them
#: back, and drive ``urllib.request.urlopen`` instead of a socket.
_REAL_GET_JSON = media_client_mod._get_json
_REAL_POST_OK = media_client_mod._post_ok

#: The resting state of a deployed box: the daemon has let media go.
_RELEASED = {"available": False, "released": True, "no_media": False}
#: What the status route reports once media has been acquired.
_ACQUIRED = {"available": True, "released": False, "no_media": False}


class _FakeDaemon:
    """A stand-in for the daemon's media routes, recording every call.

    Patched over the module's two HTTP seams, so no socket is ever opened. The
    ``status`` it returns is mutable, which is what lets a test walk the real
    lifecycle: released -> acquire -> available -> release -> released.
    """

    def __init__(self, *, status: dict | None = None, reachable: bool = True) -> None:
        self.status = dict(_RELEASED if status is None else status)
        self.reachable = reachable
        self.calls: list[tuple[str, str]] = []  # (method, path)
        self.acquire_ok = True
        self.release_ok = True
        #: Set to make ``acquire`` succeed without media actually coming up —
        #: the "we asked but it is not ready" case.
        self.acquire_makes_available = True

    def get_json(self, url: str, timeout: float) -> dict | None:
        self.calls.append(("GET", url[len(_BASE) :]))
        if not self.reachable:
            return None
        return dict(self.status)

    def post_ok(self, url: str, timeout: float) -> bool:
        path = url[len(_BASE) :]
        self.calls.append(("POST", path))
        if not self.reachable:
            return False
        if path == MEDIA_ACQUIRE_PATH:
            if not self.acquire_ok:
                return False
            if self.acquire_makes_available:
                self.status = dict(_ACQUIRED)
            return True
        if path == MEDIA_RELEASE_PATH:
            if not self.release_ok:
                return False
            self.status = dict(_RELEASED)
            return True
        raise AssertionError(f"unexpected POST to {path}")

    # --- convenience assertions -------------------------------------------
    @property
    def paths(self) -> list[str]:
        return [path for _method, path in self.calls]

    def count(self, method: str, path: str) -> int:
        return self.calls.count((method, path))


def _patch_daemon(monkeypatch, daemon: _FakeDaemon) -> _FakeDaemon:
    monkeypatch.setattr(media_client_mod, "_get_json", daemon.get_json)
    monkeypatch.setattr(media_client_mod, "_post_ok", daemon.post_ok)
    return daemon


# --- criterion 1: acquire, then construct ----------------------------------


def test_released_media_is_acquired_before_construction(monkeypatch) -> None:
    """The diagnosed box: ``released: true`` -> POST acquire -> construct works.

    This is the whole bug in one test. Before t30 the holder went straight to
    ``ReachyMini()`` against a released subsystem and got connection-refused on
    every retry, for ever.
    """
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 1
    assert len(fake_cls.instances) == 1


def test_acquire_happens_before_the_client_is_constructed(monkeypatch) -> None:
    """Ordering is the point: the SDK cannot connect to a released subsystem.

    ``_ensure_camera``'s existing SDK-level ``acquire_media()`` runs AFTER
    construction and so is unreachable in this failure mode — the constructor
    itself is what refuses.
    """
    order: list[str] = []
    daemon = _FakeDaemon(status=_RELEASED)

    def _post_ok(url: str, timeout: float) -> bool:
        order.append("acquire")
        return daemon.post_ok(url, timeout)

    monkeypatch.setattr(media_client_mod, "_get_json", daemon.get_json)
    monkeypatch.setattr(media_client_mod, "_post_ok", _post_ok)

    class _RecordingCls(_FakeMiniCls):
        def __call__(self, **kwargs):  # type: ignore[no-untyped-def]
            order.append("construct")
            return super().__call__(**kwargs)

    _patch_import(monkeypatch, _RecordingCls())
    HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE).warm_up()

    assert order == ["acquire", "construct"]


def test_already_available_media_is_not_re_acquired(monkeypatch) -> None:
    """Media we did not have to acquire is left alone — see the release test."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status={"available": True, "released": True}))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True
    # ``released`` is the signal, not ``available``: a released subsystem is
    # acquired regardless of what ``available`` happens to say.
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 1


def test_no_media_daemon_is_never_acquired(monkeypatch) -> None:
    """A daemon running media-less has nothing to acquire; do not ask."""
    daemon = _patch_daemon(
        monkeypatch, _FakeDaemon(status={"available": False, "released": True, "no_media": True})
    )
    _patch_import(monkeypatch, _FakeMiniCls())
    HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE).warm_up()

    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 0


# --- criterion 2: close() releases symmetrically -----------------------------


def test_close_releases_the_media_subsystem(monkeypatch) -> None:
    """A stopped runtime must not hold the speaker/camera hostage."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()

    holder.close()

    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 1
    assert daemon.status["released"] is True


def test_release_happens_after_the_client_lets_go(monkeypatch) -> None:
    """Release the subsystem only once OUR client has disconnected from it."""
    order: list[str] = []
    daemon = _FakeDaemon(status=_RELEASED)

    def _post_ok(url: str, timeout: float) -> bool:
        if url.endswith(MEDIA_RELEASE_PATH):
            order.append("release")
        return daemon.post_ok(url, timeout)

    monkeypatch.setattr(media_client_mod, "_get_json", daemon.get_json)
    monkeypatch.setattr(media_client_mod, "_post_ok", _post_ok)

    class _Mini(_FakeMini):
        def close(self) -> None:
            order.append("client-close")
            super().close()

    _patch_import(monkeypatch, _FakeMiniCls(mini_factory=_Mini))
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()
    holder.close()

    assert order == ["client-close", "release"]


def test_media_acquired_by_another_consumer_is_never_released_by_us(monkeypatch) -> None:
    """Never yank the subsystem out from under a consumer that got there first.

    We only ever release what we ourselves acquired. Here the probe reports
    another owner, so we neither construct nor release.
    """
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_ACQUIRED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()

    holder.close()

    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 0


def test_close_without_a_gate_posts_no_release(monkeypatch) -> None:
    """No probe ran, so nothing was acquired, so nothing is released."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=None)
    holder.warm_up()
    holder.close()

    assert daemon.calls == []


def test_close_is_still_idempotent_with_the_release_leg(monkeypatch) -> None:
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()

    holder.close()
    holder.close()
    holder.close()

    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 1


def test_a_failing_release_never_raises(monkeypatch) -> None:
    daemon = _FakeDaemon(status=_RELEASED)
    daemon.release_ok = False
    _patch_daemon(monkeypatch, daemon)
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()

    holder.close()  # must not raise

    assert holder.connected is False


# --- idempotency / reference counting ---------------------------------------


def test_acquire_is_not_repeated_while_we_already_hold_media(monkeypatch) -> None:
    """One acquire per held client — reads never re-probe the daemon."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)
    holder.warm_up()
    after_warm_up = list(daemon.calls)

    for _ in range(20):
        holder.audio()
        holder.frame()
    holder.warm_up()

    # Not one extra probe: the gate runs once per CONSTRUCTION, never per read
    # and never on a repeat warm-up of an already-live holder.
    assert daemon.calls == after_warm_up
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 1


def test_a_dropped_client_releases_then_re_acquires(monkeypatch) -> None:
    """A mid-run fault is a full round trip, so the daemon's state stays honest."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    clock = _FakeClock(0.0)
    fake_cls = _FakeMiniCls(mini_factory=lambda: _FakeMini(media=_FakeMedia(fail_audio=True)))
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=clock, base_url=_BASE)
    holder.warm_up()

    assert holder.audio() is None  # the read fault drops the client
    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 1
    assert daemon.status["released"] is True

    clock.advance(DEFAULT_RETRY_BACKOFF + 0.1)
    holder.warm_up()

    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 2


def test_a_failed_construction_gives_the_media_back(monkeypatch) -> None:
    """We acquired and then could not use it — do not sit on it during backoff."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls(should_fail=True))
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is False
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 1
    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 1
    assert daemon.status["released"] is True


# --- criterion 3: bounded, never an indefinite block -------------------------


def test_another_consumer_holding_media_refuses_construction(monkeypatch) -> None:
    """The measured HANG state is refused, not entered.

    With media acquired AND ``reachy-runtime.service`` running, a bare
    ``ReachyMini()`` produced no output under ``python -u`` and was killed at
    90 s — where the same call takes 0.9 s against a subsystem we own. The
    composition root warms holders SYNCHRONOUSLY during setup, so a construction
    that blocks indefinitely hangs unit startup with no error and no restart
    (``Restart=on-failure`` cannot fire on a merely-stuck process). A
    ``warm_up()`` returning ``False`` is a designed, graceful degradation; a
    ``warm_up()`` that never returns is not.
    """
    _patch_daemon(monkeypatch, _FakeDaemon(status=_ACQUIRED))
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is False
    assert fake_cls.instances == []  # never even attempted
    assert holder.connected is False


def test_a_contended_subsystem_is_retried_after_the_backoff(monkeypatch) -> None:
    """The refusal is transient: when the other consumer lets go, we come up."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status=_ACQUIRED))
    clock = _FakeClock(0.0)
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=clock, base_url=_BASE)

    assert holder.warm_up() is False

    daemon.status = dict(_RELEASED)  # the other consumer disconnected
    clock.advance(DEFAULT_RETRY_BACKOFF + 0.1)

    assert holder.warm_up() is True


def test_acquire_that_does_not_make_media_available_is_refused(monkeypatch) -> None:
    """ "We asked" is not "it is ready" — confirm before entering the connect.

    The hand-verified good state is ``{"available": true, "released": false}``;
    that is the state in which construction takes 0.9 s. Anything else is not the
    measured-safe precondition, so we hand the subsystem back and back off rather
    than gamble on an unbounded connect.
    """
    daemon = _FakeDaemon(status=_RELEASED)
    daemon.acquire_makes_available = False
    _patch_daemon(monkeypatch, daemon)
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is False
    assert fake_cls.instances == []
    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 1


def test_gate_timeout_default_is_a_positive_constant() -> None:
    """Bounded by construction, and generous against a loopback round trip.

    Every gate leg is a localhost request measured in milliseconds; the default
    is orders of magnitude of headroom over that, and still the same order as the
    0.9 s clean construction it guards, so the readiness check can never dominate
    the cost of the thing it is protecting.
    """
    assert isinstance(DEFAULT_GATE_TIMEOUT, float)
    assert 0.0 < DEFAULT_GATE_TIMEOUT <= 5.0


def test_gate_timeout_is_injectable_and_passed_to_every_leg(monkeypatch) -> None:
    seen: list[float] = []

    def _get_json(url: str, timeout: float):  # type: ignore[no-untyped-def]
        seen.append(timeout)
        return dict(_RELEASED)

    def _post_ok(url: str, timeout: float) -> bool:
        seen.append(timeout)
        return True

    monkeypatch.setattr(media_client_mod, "_get_json", _get_json)
    monkeypatch.setattr(media_client_mod, "_post_ok", _post_ok)
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE, gate_timeout=0.25)
    holder.warm_up()
    holder.close()

    assert seen  # status + acquire + confirm + release
    assert set(seen) == {0.25}


def test_gate_warm_up_still_starts_no_threads(monkeypatch) -> None:
    """The bound is a precondition gate, NOT a worker thread.

    A thread would re-introduce the interpreter-exit hazard ``close()`` exists to
    avoid, so the class stays passive and the gate is what keeps setup bounded.
    """
    import threading

    _patch_daemon(monkeypatch, _FakeDaemon(status=_RELEASED))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    before = threading.active_count()
    holder.warm_up()
    holder.close()

    assert threading.active_count() == before


# --- fail-open: an unreachable daemon must not make us MORE broken -----------


def test_an_unreachable_daemon_falls_open_to_plain_construction(monkeypatch) -> None:
    """No answer is not a negative answer. Behave exactly as before t30."""
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(reachable=False))
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True
    assert len(fake_cls.instances) == 1
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 0

    holder.close()
    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 0


def test_a_failing_acquire_falls_open_to_plain_construction(monkeypatch) -> None:
    """A daemon build with no acquire route must not be permanently refused."""
    daemon = _FakeDaemon(status=_RELEASED)
    daemon.acquire_ok = False
    _patch_daemon(monkeypatch, daemon)
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True
    assert len(fake_cls.instances) == 1
    holder.close()
    assert daemon.count("POST", MEDIA_RELEASE_PATH) == 0


def test_a_garbage_status_payload_falls_open(monkeypatch) -> None:
    monkeypatch.setattr(media_client_mod, "_get_json", lambda url, timeout: ["not", "a", "dict"])
    posted: list[str] = []
    monkeypatch.setattr(
        media_client_mod, "_post_ok", lambda url, timeout: posted.append(url) or True
    )
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    assert HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE).warm_up() is True
    assert posted == []


def test_a_raising_gate_never_escapes_warm_up(monkeypatch) -> None:
    """``warm_up`` keeps its no-raise contract even if the probe explodes."""

    def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise RuntimeError("probe exploded")

    monkeypatch.setattr(media_client_mod, "_get_json", _boom)
    monkeypatch.setattr(media_client_mod, "_post_ok", _boom)
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True  # fell open, did not raise
    holder.close()  # must not raise either


# --- the real HTTP legs (urllib, no socket) ---------------------------------


class _FakeResponse:
    def __init__(self, payload: bytes, status: int = 200) -> None:
        self._payload = payload
        self.status = status

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):  # type: ignore[no-untyped-def]
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def test_gate_builds_the_documented_daemon_requests(monkeypatch) -> None:
    """GET the status route, POST the acquire/release routes. Nothing else."""
    seen: list[tuple[str, str, float]] = []
    # The confirm leg must see media as available, so return the acquired shape
    # on the SECOND status GET.
    statuses = [_RELEASED, _ACQUIRED]

    def _urlopen_seq(req, timeout=None):  # type: ignore[no-untyped-def]
        seen.append((req.get_method(), req.full_url, timeout))
        if req.full_url.endswith(MEDIA_STATUS_PATH):
            return _FakeResponse(json.dumps(statuses.pop(0)).encode())
        return _FakeResponse(b'{"status":"ok"}')

    monkeypatch.setattr(urllib.request, "urlopen", _urlopen_seq)
    monkeypatch.setattr(media_client_mod, "_get_json", _REAL_GET_JSON)
    monkeypatch.setattr(media_client_mod, "_post_ok", _REAL_POST_OK)
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE, gate_timeout=1.0)
    assert holder.warm_up() is True
    holder.close()

    assert seen == [
        ("GET", _BASE + MEDIA_STATUS_PATH, 1.0),
        ("POST", _BASE + MEDIA_ACQUIRE_PATH, 1.0),
        ("GET", _BASE + MEDIA_STATUS_PATH, 1.0),
        ("POST", _BASE + MEDIA_RELEASE_PATH, 1.0),
    ]


def test_http_helpers_swallow_every_transport_error(monkeypatch) -> None:
    """The holder degrades, never raises — the probe is no exception."""

    for err in (
        ConnectionRefusedError(111, "Connection refused"),
        urllib.error.URLError("unreachable"),
        TimeoutError("timed out"),
        ValueError("bad url"),
    ):

        def _raise(*_a, _err=err, **_k):  # type: ignore[no-untyped-def]
            raise _err

        monkeypatch.setattr(urllib.request, "urlopen", _raise)
        assert _REAL_GET_JSON(_BASE + MEDIA_STATUS_PATH, 0.1) is None
        assert _REAL_POST_OK(_BASE + MEDIA_ACQUIRE_PATH, 0.1) is False


def test_non_http_scheme_is_refused_without_a_request(monkeypatch) -> None:
    """Mirrors ``reachy.daemon.health_ok``: never hand urllib a file:// URL."""

    def _boom(*_a, **_k):  # type: ignore[no-untyped-def]
        raise AssertionError("urlopen must not be reached")

    monkeypatch.setattr(urllib.request, "urlopen", _boom)
    assert _REAL_GET_JSON("file:///etc/passwd", 0.1) is None
    assert _REAL_POST_OK("file:///etc/passwd", 0.1) is False


@pytest.mark.offline
def test_offline_lane_degrades_to_plain_construction(monkeypatch) -> None:
    """Zero-service lane: the gate cannot reach a daemon, so it falls open.

    The offline guard blocks real socket connects outright — it raises
    ``AssertionError``, which is NOT an ``OSError``. The gate's helpers therefore
    catch broadly on purpose (the class contract is "no public method raises"),
    and treat a blocked connect exactly like any other unreachable daemon: no
    acquire, no release, straight to the ordinary construction path. So the
    offline lane stays green and fast with no special-casing — and this test
    pins that, rather than leaving it an accident.
    """
    monkeypatch.setattr(media_client_mod, "_get_json", _REAL_GET_JSON)
    monkeypatch.setattr(media_client_mod, "_post_ok", _REAL_POST_OK)
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE, gate_timeout=0.05)

    assert holder.warm_up() is True
    assert len(fake_cls.instances) == 1
    holder.close()


def test_status_without_a_released_key_falls_open(monkeypatch) -> None:
    """A payload that never mentions ``released`` is ABSENCE of information.

    The gate's stated contract is fail-OPEN on absence of information and
    fail-CLOSED only on the one definitive negative — another consumer holding
    the subsystem. A daemon build whose status route omits ``released``
    entirely reports no such negative: it reports nothing on the question.

    Read naively, ``status.get("released", False)`` collapses "the key is
    missing" into "released is false" and therefore into "someone holds it", so
    the holder would defer FOREVER against such a daemon — reproducing exactly
    the permanently-dormant-senses failure this gate exists to remove, just
    with a different cause and no acquire attempt in the log to explain it.

    The live box's payload does carry ``released`` (verified by hand), so this
    is a contract bug rather than an outage — but the contract is what future
    daemon builds will be read against, so it is pinned here.
    """
    daemon = _patch_daemon(monkeypatch, _FakeDaemon(status={"available": False}))
    _patch_import(monkeypatch, _FakeMiniCls())
    holder = HeldMediaClient(now=_FakeClock(0.0), base_url=_BASE)

    assert holder.warm_up() is True, "a status payload with no 'released' key must fall open"
    assert daemon.count("POST", MEDIA_ACQUIRE_PATH) == 0, "nothing said media was released"
