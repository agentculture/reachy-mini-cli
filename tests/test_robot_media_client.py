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
        holder.camera_available  # noqa: B018 — property read, must not reconstruct

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
