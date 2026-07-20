"""Tests for :mod:`reachy.robot.state_reader` — the held, media-free state reader.

All tests use a *stubbed* ``reachy_mini`` — ``HeldStateReader._import`` is
monkeypatched to return a fake ``ReachyMini`` class (mirroring
``tests/test_sdk_transport.py``'s ``_patch_import`` seam), so no real hardware or
installed SDK is needed. A manually-advanced fake clock (mirroring
``tests/test_listen_direction_invariants.py``'s ``_FakeClock``) makes the
lazy-construction / retry-backoff behavior fully deterministic.
"""

from __future__ import annotations

import logging

import numpy as np
import pytest

from reachy.robot.state_reader import DEFAULT_RETRY_BACKOFF, HeldStateReader

_SENSE_LOGGER_NAME = "reachy.sense"


# ---------------------------------------------------------------------------
# Fake reachy_mini infrastructure
# ---------------------------------------------------------------------------


class _FakeMini:
    """Minimal stand-in for a ``ReachyMini(media_backend='no_media')`` instance."""

    def __init__(self, *, head_pose: "np.ndarray | None" = None, fail_reads: bool = False) -> None:
        self._head_pose = np.eye(4) if head_pose is None else head_pose
        self.fail_reads = fail_reads
        self.closed = False
        self.disconnected = False

    def get_current_head_pose(self):  # type: ignore[no-untyped-def]
        if self.fail_reads:
            raise RuntimeError("read failed")
        return self._head_pose

    def close(self) -> None:
        self.closed = True


class _FakeMiniNoClose:
    """A fake client exposing neither ``close`` nor ``disconnect`` (close() must tolerate it)."""

    def __init__(self) -> None:
        self._head_pose = np.eye(4)

    def get_current_head_pose(self):  # type: ignore[no-untyped-def]
        return self._head_pose


class _FakeMiniDisconnectOnly:
    """A fake client exposing ``disconnect`` but not ``close``."""

    def __init__(self) -> None:
        self._head_pose = np.eye(4)
        self.disconnected = False

    def get_current_head_pose(self):  # type: ignore[no-untyped-def]
        return self._head_pose

    def disconnect(self) -> None:
        self.disconnected = True


class _FakeMiniCls:
    """A fake ``ReachyMini`` class (callable that returns ``_FakeMini`` or raises).

    ``should_fail`` is a plain mutable attribute the test flips between calls to
    model "daemon down, then recovers" without needing a queue/side-effect list.
    """

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
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: fake_cls))


def _patch_import_absent(monkeypatch) -> None:
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: None))


# ---------------------------------------------------------------------------
# Off-thread warm-up — the tick-budget affordance (live evidence, t1 section 3)
# ---------------------------------------------------------------------------
#
# This class is the MEASURED cause of a reproducible tick-budget violation on
# the deployed box. Every start of ``reachy-runtime.service`` logs this pair,
# in this order, at tick ~447-453:
#
#     [SENSE stage=state source=head_pose event=…] connected (media_backend=no_media)
#     [SENSE stage=rule source=tick event=overrun] overrun tick=449
#         duration_ms=424.93 budget_ms=20.00
#
# Durations across restarts: 424.93 / 974.39 / 990.61 / 1102.92 / 1212.66 ms
# against a 20 ms budget — 21x to 61x over
# (``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).
# The cause is construct-on-first-read building the ``no_media`` client ON THE
# TICK THREAD, so the 50 Hz loop stalls for up to ~1.2 s.
#
# The fix mirrors :class:`reachy.robot.media_client.HeldMediaClient` exactly,
# giving a tick-thread caller two doors:
#   * :meth:`warm_up` — the owner constructs off-thread, before the loop starts.
#   * ``allow_inline_connect=False`` — closes the on-thread door entirely, which
#     ``warm_up()`` alone cannot do (a mid-run reconnect after a read fault
#     would otherwise construct inline, reproducing the stall mid-run).
#
# Both are ADDITIVE: the lazy default is unchanged, so every existing caller
# (notably ``_commands/behavior.py::_make_state_reader``) keeps working.


def test_warm_up_constructs_and_reports_success(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    reader = HeldStateReader(now=_FakeClock(0.0))

    assert reader.warm_up() is True
    assert len(fake_cls.calls) == 1
    assert reader.connected is True


def test_reads_after_warm_up_never_construct(monkeypatch) -> None:
    """THE contract: a warmed reader does zero construction on the tick thread."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    assert reader.warm_up() is True
    calls_after_warm_up = len(fake_cls.calls)

    for _ in range(50):
        assert reader.read() == (0.0, 0.0)

    assert len(fake_cls.calls) == calls_after_warm_up == 1


def test_warm_up_off_thread_means_the_tick_thread_never_constructs(monkeypatch) -> None:
    """Criterion 1, stated as the deployment actually runs it.

    The composition root warms the reader on a setup/background thread; the
    engine then reads on its one tick thread. This asserts the construction
    happened on the OTHER thread — i.e. the ~1.2 s ``no_media`` bring-up is
    charged to setup, never to a tick.
    """
    import threading

    construct_threads: list[int] = []

    class _ThreadRecordingCls(_FakeMiniCls):
        def __call__(self, **kwargs):  # type: ignore[no-untyped-def]
            construct_threads.append(threading.get_ident())
            return super().__call__(**kwargs)

    fake_cls = _ThreadRecordingCls()
    _patch_import(monkeypatch, fake_cls)
    # The door is closed too, so a mid-run drop can never reopen it inline.
    reader = HeldStateReader(now=_FakeClock(0.0), allow_inline_connect=False)

    warmer = threading.Thread(target=reader.warm_up)
    warmer.start()
    warmer.join()

    assert reader.connected is True
    assert construct_threads == [warmer.ident]

    tick_thread_ident = threading.get_ident()
    assert tick_thread_ident != warmer.ident
    for _ in range(100):
        assert reader.read() == (0.0, 0.0)

    # Exactly one construction, and it did not happen on this (tick) thread.
    assert len(construct_threads) == 1
    assert tick_thread_ident not in construct_threads


def test_warm_up_is_idempotent(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    for _ in range(5):
        assert reader.warm_up() is True

    assert len(fake_cls.calls) == 1


def test_warm_up_is_safe_and_false_when_sdk_absent(monkeypatch, caplog) -> None:
    """A bare box (no ``[sdk]`` extra) degrades: False, one warning, no storm."""
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=0.001)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(10):
            assert reader.warm_up() is False  # must not raise
            clock.advance(1.0)

    assert reader.connected is False
    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(sense_records) == 1


def test_warm_up_reports_failure_and_can_be_retried_after_backoff(monkeypatch) -> None:
    """An owner polling warm_up() off-thread recovers when the daemon comes up."""
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=5.0)

    assert reader.warm_up() is False
    assert reader.connected is False

    clock.advance(1.0)
    assert reader.warm_up() is False
    assert len(fake_cls.calls) == 1  # backoff still throttles the off-thread caller

    clock.advance(5.0)
    fake_cls.should_fail = False
    assert reader.warm_up() is True
    assert len(fake_cls.calls) == 2


def test_warm_up_after_close_returns_false_and_never_constructs(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    reader.close()

    assert reader.warm_up() is False
    assert len(fake_cls.calls) == 0


def test_connected_never_constructs(monkeypatch) -> None:
    """``connected`` is a pure predicate — a supervisor can poll it freely."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    for _ in range(10):
        assert reader.connected is False
    assert len(fake_cls.calls) == 0

    reader.warm_up()
    assert reader.connected is True

    reader.close()
    assert reader.connected is False


# --- allow_inline_connect=False: the on-thread door, closed -----------------


def test_inline_connect_disabled_reads_never_construct(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, allow_inline_connect=False)

    for _ in range(20):
        assert reader.read() is None
        clock.advance(10.0)

    assert len(fake_cls.calls) == 0


def test_inline_connect_disabled_works_normally_after_warm_up(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0), allow_inline_connect=False)

    assert reader.warm_up() is True

    for _ in range(10):
        assert reader.read() == (0.0, 0.0)

    assert len(fake_cls.calls) == 1


def test_inline_connect_disabled_does_not_reconnect_on_the_tick_thread(monkeypatch) -> None:
    """A mid-run fault must not turn into an inline reconnect stall.

    This is the case ``warm_up()`` alone cannot cover: the first construction is
    off-thread, but a dropped client would otherwise be rebuilt by whichever
    read noticed — i.e. on the tick thread, reproducing the measured overrun
    mid-run instead of at start.
    """
    fake_cls = _FakeMiniCls(mini_factory=lambda: _FakeMini(fail_reads=True))
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=2.0, allow_inline_connect=False)

    assert reader.warm_up() is True
    assert reader.read() is None  # the read faults and drops the client
    assert reader.connected is False

    clock.advance(100.0)  # well past the backoff window
    for _ in range(10):
        assert reader.read() is None
    assert len(fake_cls.calls) == 1  # NO inline reconnect

    # The owner re-warms off-thread, having noticed via ``connected``.
    fake_cls._mini_factory = _FakeMini
    assert reader.warm_up() is True
    assert len(fake_cls.calls) == 2
    assert reader.read() == (0.0, 0.0)


def test_inline_connect_enabled_is_the_default(monkeypatch) -> None:
    """The lazy default is unchanged — every existing caller keeps working."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    reader = HeldStateReader(now=_FakeClock(0.0))

    assert reader.read() == (0.0, 0.0)
    assert len(fake_cls.calls) == 1


def test_warm_up_starts_no_threads(monkeypatch) -> None:
    """The reader stays PASSIVE: warm-up runs on the caller's thread, by design.

    Spawning a thread inside the class would re-introduce exactly the
    interpreter-exit hazard ``close()`` exists to avoid. The owner decides which
    thread warms it.
    """
    import threading

    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    before = threading.active_count()
    reader.warm_up()

    assert threading.active_count() == before


def test_warm_up_holds_exactly_one_client_for_the_process_lifetime(monkeypatch) -> None:
    """The load-bearing contract survives the new API: ONE ``no_media`` client."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0), allow_inline_connect=False)

    reader.warm_up()
    for _ in range(200):
        reader.read()
    reader.warm_up()

    assert len(fake_cls.instances) == 1
    assert fake_cls.calls == [{"media_backend": "no_media"}]

    # ...and close() still explicitly releases it (the interpreter-exit hazard).
    reader.close()
    assert fake_cls.instances[0].closed is True


# ---------------------------------------------------------------------------
# Lazy construction — at most one construction across many reads
# ---------------------------------------------------------------------------


def test_read_returns_pitch_yaw_tuple(monkeypatch) -> None:
    """A successful read returns (pitch_deg, yaw_deg); identity pose -> (0.0, 0.0)."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    reader = HeldStateReader(now=_FakeClock(0.0))

    assert reader.read() == (0.0, 0.0)


def test_read_constructs_client_at_most_once_across_n_reads(monkeypatch) -> None:
    """Construction happens on first read only; N more reads build zero more clients."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    reader = HeldStateReader(now=_FakeClock(0.0))
    for _ in range(25):
        assert reader.read() == (0.0, 0.0)

    assert len(fake_cls.calls) == 1


def test_construction_passes_no_media_backend(monkeypatch) -> None:
    """Construction is ``ReachyMini(media_backend='no_media')`` — the held, audio-free profile."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)

    HeldStateReader(now=_FakeClock(0.0)).read()

    assert fake_cls.calls == [{"media_backend": "no_media"}]


# ---------------------------------------------------------------------------
# Daemon-down at start: fail -> None during backoff -> clock advances -> success
# ---------------------------------------------------------------------------


def test_daemon_down_then_recovers_after_backoff(monkeypatch, caplog) -> None:
    """Construction fails, reads degrade to None during backoff, retry succeeds after."""
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=5.0)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        # First read: construction attempted and fails -> None, one construct call.
        assert reader.read() is None
        assert len(fake_cls.calls) == 1

        # Still within the backoff window: no further construction attempts.
        clock.advance(1.0)
        assert reader.read() is None
        assert len(fake_cls.calls) == 1

        clock.advance(1.0)
        assert reader.read() is None
        assert len(fake_cls.calls) == 1

        # Backoff elapses; the daemon has come up. Next read retries and succeeds.
        clock.advance(3.5)  # total elapsed = 5.5s >= 5.0s backoff
        fake_cls.should_fail = False
        assert reader.read() == (0.0, 0.0)
        assert len(fake_cls.calls) == 2

        # Readings keep flowing without any further construction.
        for _ in range(5):
            assert reader.read() == (0.0, 0.0)
        assert len(fake_cls.calls) == 2

    sense_lines = [r.getMessage() for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert any("retrying" in line for line in sense_lines)
    assert any("connected" in line for line in sense_lines)


def test_backoff_interval_is_injectable(monkeypatch) -> None:
    """A custom retry_backoff governs when the next construction attempt is allowed."""
    fake_cls = _FakeMiniCls(should_fail=True)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=1.0)

    assert reader.read() is None
    assert len(fake_cls.calls) == 1

    clock.advance(0.5)
    assert reader.read() is None
    assert len(fake_cls.calls) == 1  # still within the 1.0s backoff

    clock.advance(0.6)  # total 1.1s elapsed
    fake_cls.should_fail = False
    assert reader.read() == (0.0, 0.0)
    assert len(fake_cls.calls) == 2


def test_default_retry_backoff_is_a_positive_constant() -> None:
    """DEFAULT_RETRY_BACKOFF exists and is used when retry_backoff is not passed."""
    assert DEFAULT_RETRY_BACKOFF > 0
    reader = HeldStateReader()
    assert reader is not None  # constructing with defaults must not raise


# ---------------------------------------------------------------------------
# A read failure on an already-connected client -> None, reconnect after backoff
# ---------------------------------------------------------------------------


def test_read_failure_on_connected_client_degrades_to_none_then_reconnects(
    monkeypatch,
) -> None:
    """A read that raises on an already-open client drops it and retries later."""
    failing_mini = _FakeMini(fail_reads=True)
    fake_cls = _FakeMiniCls(mini_factory=lambda: failing_mini)
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=2.0)

    # First read constructs the client, but the pose read itself raises.
    assert reader.read() is None
    assert len(fake_cls.calls) == 1
    assert failing_mini.closed is True  # the dead client is released

    # Still within backoff: no reconstruction.
    clock.advance(1.0)
    assert reader.read() is None
    assert len(fake_cls.calls) == 1

    # Backoff elapses; a fresh (working) client is built.
    clock.advance(1.5)
    fake_cls._mini_factory = _FakeMini  # next client works
    assert reader.read() == (0.0, 0.0)
    assert len(fake_cls.calls) == 2


def test_logs_one_line_per_state_change_not_per_read(monkeypatch, caplog) -> None:
    """Repeated successful reads on an already-connected client log nothing further."""
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(30):
            reader.read()

    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    # Exactly one line: the initial "connected" transition. No per-read spam.
    assert len(sense_records) == 1
    assert "connected" in sense_records[0].getMessage()


# ---------------------------------------------------------------------------
# close() — idempotent, releases the client, stays closed
# ---------------------------------------------------------------------------


def test_close_releases_client_via_close_method(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))
    reader.read()  # construct

    reader.close()

    assert fake_cls.last.closed is True


def test_close_releases_client_via_disconnect_when_no_close(monkeypatch) -> None:
    fake_cls = _FakeMiniCls(mini_factory=_FakeMiniDisconnectOnly)
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))
    reader.read()

    reader.close()

    assert fake_cls.last.disconnected is True


def test_close_tolerates_client_with_neither_close_nor_disconnect(monkeypatch) -> None:
    fake_cls = _FakeMiniCls(mini_factory=_FakeMiniNoClose)
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))
    reader.read()

    reader.close()  # must not raise

    assert reader.read() is None


def test_close_before_any_construction_is_a_noop(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    reader.close()  # must not raise, never constructed

    assert len(fake_cls.calls) == 0
    assert reader.read() is None
    assert len(fake_cls.calls) == 0  # closed reader never reconstructs


def test_close_is_idempotent(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))
    reader.read()

    reader.close()
    reader.close()  # second call must not raise or double-release
    reader.close()

    assert fake_cls.last.closed is True


def test_read_after_close_returns_none_and_never_reconstructs(monkeypatch) -> None:
    fake_cls = _FakeMiniCls()
    _patch_import(monkeypatch, fake_cls)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=1.0)
    reader.read()
    calls_before_close = len(fake_cls.calls)

    reader.close()
    clock.advance(100.0)  # well past any backoff

    for _ in range(10):
        assert reader.read() is None

    assert len(fake_cls.calls) == calls_before_close


# ---------------------------------------------------------------------------
# Missing SDK — permanently-None reader, one warning, no retry storm
# ---------------------------------------------------------------------------


def test_missing_sdk_degrades_to_permanently_none(monkeypatch) -> None:
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=1.0)

    assert reader.read() is None
    clock.advance(1000.0)  # far past any conceivable backoff
    for _ in range(20):
        assert reader.read() is None


def test_missing_sdk_logs_exactly_one_warning(monkeypatch, caplog) -> None:
    _patch_import_absent(monkeypatch)
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=0.001)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        for _ in range(20):
            reader.read()
            clock.advance(1.0)  # would clear backoff every time if it retried

    sense_records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(sense_records) == 1
    assert (
        "sdk-absent" in sense_records[0].getMessage()
        or "sdk" in sense_records[0].getMessage().lower()
    )


def test_missing_sdk_never_calls_import_more_than_once(monkeypatch) -> None:
    """No retry storm: once the SDK is known absent, ``_import`` isn't re-probed."""
    call_count = {"n": 0}

    def _counting_import():
        call_count["n"] += 1
        return None

    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(_counting_import))
    clock = _FakeClock(0.0)
    reader = HeldStateReader(now=clock, retry_backoff=0.001)

    for _ in range(15):
        reader.read()
        clock.advance(10.0)

    assert call_count["n"] == 1


# ---------------------------------------------------------------------------
# Import boundary: the module must import cleanly without reachy_mini installed
# ---------------------------------------------------------------------------


def test_module_imports_without_reachy_mini(monkeypatch) -> None:
    """reachy.robot.state_reader must not hard-import reachy_mini at module load."""
    import sys

    monkeypatch.setitem(sys.modules, "reachy_mini", None)  # simulate absence on import
    import importlib

    import reachy.robot.state_reader as mod

    importlib.reload(mod)  # must not raise even with reachy_mini "absent"


def test_default_import_returns_none_without_reachy_mini_installed() -> None:
    """The real (unpatched) _import degrades to None rather than raising, when absent.

    This only asserts the *shape* of the seam (no CliError, tolerant of ImportError);
    it does not assert whether reachy_mini happens to be installed in this test env.
    """
    result = HeldStateReader._import()
    assert result is None or callable(result)


# ---------------------------------------------------------------------------
# Reused helper: _euler_pitch_yaw is imported, not copied
# ---------------------------------------------------------------------------


def test_reuses_euler_pitch_yaw_from_sdk_transport(monkeypatch) -> None:
    """A non-trivial rotation decodes identically to sdk_transport's own helper."""
    from reachy.robot.sdk_transport import _euler_pitch_yaw

    pose = np.eye(4)
    pose[0, 2] = 0.5  # inject a non-zero pitch component
    pose[0, 1] = -0.2

    fake_cls = _FakeMiniCls(mini_factory=lambda: _FakeMini(head_pose=pose))
    _patch_import(monkeypatch, fake_cls)
    reader = HeldStateReader(now=_FakeClock(0.0))

    assert reader.read() == pytest.approx(_euler_pitch_yaw(pose))
