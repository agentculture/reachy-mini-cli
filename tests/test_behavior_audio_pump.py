"""Unit tests for :mod:`reachy.behavior.audio_pump` — the background mic pump (#100).

The live-verified defect: the SDK's audio appsink is ``drop=True,
max-buffers=500`` — a FIFO of up to ~10 s of audio built for a consumer that
drains at production rate. The behavior engine pulled ONE chunk per tick
(23-43 Hz achieved), slower than production, so every read was SECONDS stale:
rule fires landed the instant the #95 moving-floor gate closed (the mic was
replaying the robot's own past motion), STT transcribed a past word in a silent
room, and ``utterance start`` fired continuously in silence. Additionally the
SDK's ``get_sample`` blocks up to 20 ms when the queue is empty — audio I/O on
the tick thread (#97's residual).

:class:`~reachy.behavior.audio_pump.AudioPump` owns ALL audio acquisition on a
background daemon thread; the tick thread only ever swaps out the pending
buffer (:meth:`take`). These tests pin the four acceptance criteria:

1. a standing backlog is discarded — a consumer polling at ANY rate never reads
   the backlog once the pump is live;
2. ``take()`` is a pure latch swap (no call into the media source), and every
   produced chunk reaches the taker exactly once, in order;
3. a down (``None``-returning) source degrades cleanly — one named [SENSE]
   transition line, no spin (reads are beat-paced);
4. ``close()`` joins the thread with a bounded timeout and is idempotent.

No robot, no SDK, no network: the media source is a scripted fake.
"""

from __future__ import annotations

import collections
import inspect
import logging
import threading
import time

import numpy as np
import pytest

from reachy.behavior import audio_pump as audio_pump_mod
from reachy.behavior.audio_pump import AudioPump

pytestmark = pytest.mark.offline


def _chunk(value: float, size: int = 4) -> np.ndarray:
    return np.full(size, float(value), dtype=np.float32)


class _ScriptedSource:
    """A held-media-client stand-in: ``audio()`` pops a scripted queue.

    Returns ``None`` when the queue is empty — exactly the real
    ``HeldMediaClient.audio()`` contract ("no audio right now" and "client
    down" are both ``None``; the free ``connected`` predicate disambiguates).
    Thread-safe: the pump reads from its own thread while the test feeds.
    """

    def __init__(self, items=(), *, connected: bool = True) -> None:
        self._lock = threading.Lock()
        self._items: collections.deque = collections.deque(items)
        self.connected = connected
        self.calls = 0
        self.call_threads: list[int] = []

    def feed(self, *chunks) -> None:
        with self._lock:
            self._items.extend(chunks)

    def audio(self):
        with self._lock:
            self.calls += 1
            self.call_threads.append(threading.get_ident())
            if not self._items:
                return None
            return self._items.popleft()


def _wait(predicate, *, timeout: float = 5.0, interval: float = 0.001) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return
        time.sleep(interval)
    raise AssertionError("condition never became true within the deadline")


def _drain_takes(pump: AudioPump, *, samples: int, timeout: float = 5.0) -> list[np.ndarray]:
    """Poll ``take()`` until *samples* total samples have been collected."""
    got: list[np.ndarray] = []
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        out = pump.take()
        if out is not None:
            got.append(out)
        if sum(int(g.size) for g in got) >= samples:
            return got
        time.sleep(0.001)
    raise AssertionError(f"collected {sum(int(g.size) for g in got)} of {samples} samples")


def _sense_lines(caplog) -> list[str]:
    return [record.getMessage() for record in caplog.records if record.name == "reachy.sense"]


# --------------------------------------------------------------------------- #
# 1. A standing backlog is discarded; take() reflects only LIVE chunks         #
# --------------------------------------------------------------------------- #


def test_a_standing_backlog_is_discarded_and_take_returns_only_live_audio(caplog):
    """Criterion 1: against a source pre-loaded with a 500-chunk stale backlog,
    the pump drains and DISCARDS the backlog, then latches only chunks produced
    after it went live — a consumer polling at any rate never reads the backlog.
    """
    stale = [_chunk(-1.0) for _ in range(500)]
    source = _ScriptedSource(stale)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        pump = AudioPump(source, beat_s=0.001)
        pump.start()
        try:
            _wait(lambda: pump.live)
            live_chunks = [_chunk(i) for i in range(5)]
            source.feed(*live_chunks)
            got = _drain_takes(pump, samples=20)
        finally:
            pump.close()

    heard = np.concatenate(got)
    assert not np.any(heard == -1.0), "a stale backlog chunk reached the consumer"
    assert np.array_equal(heard, np.concatenate(live_chunks)), "live audio arrived out of order"
    assert pump.drained == 500

    live_lines = [line for line in _sense_lines(caplog) if "event=live" in line]
    assert len(live_lines) == 1, f"expected ONE live transition line, got {live_lines}"
    assert live_lines[0].startswith("[SENSE stage=audio source=pump")
    assert "500" in live_lines[0], "the live line must name how many stale chunks were discarded"


def test_an_empty_source_goes_live_immediately_with_nothing_discarded():
    """No backlog is the common case: the first empty read IS the live signal."""
    source = _ScriptedSource()
    pump = AudioPump(source, beat_s=0.001)
    pump.start()
    try:
        _wait(lambda: pump.live)
        assert pump.drained == 0
        source.feed(_chunk(7.0))
        got = _drain_takes(pump, samples=4)
        assert np.array_equal(np.concatenate(got), _chunk(7.0))
    finally:
        pump.close()


# --------------------------------------------------------------------------- #
# 2. take() is a latch swap: no source I/O, exactly-once delivery, in order    #
# --------------------------------------------------------------------------- #


def test_take_never_calls_into_the_media_source():
    """Criterion 2a: the tick-side read is a latch swap — a ``take()`` performs
    zero calls into the media source, before start, while running, and after
    close (the counting fake is the proof)."""
    source = _ScriptedSource()
    pump = AudioPump(source, beat_s=0.001)

    # Never started: nothing may reach for the source.
    for _ in range(10):
        assert pump.take() is None
    assert source.calls == 0

    pump.start()
    _wait(lambda: source.calls >= 1)
    pump.close()

    before = source.calls
    for _ in range(10):
        pump.take()
    assert source.calls == before, "take() called into the media source"


def test_every_chunk_reaches_the_taker_exactly_once_in_order():
    """Criterion 2b: across many interleaved takes, every produced chunk is
    delivered exactly once — no loss, no duplication, order preserved."""
    source = _ScriptedSource()
    pump = AudioPump(source, beat_s=0.0005)
    pump.start()
    got: list[np.ndarray] = []
    fed: list[np.ndarray] = []
    try:
        _wait(lambda: pump.live)
        for i in range(30):
            chunk = _chunk(i, size=3)
            fed.append(chunk)
            source.feed(chunk)
            out = pump.take()  # interleave takes at an arbitrary consumer rate
            if out is not None:
                got.append(out)
            time.sleep(0.001)
        got.extend(_drain_takes(pump, samples=90 - sum(int(g.size) for g in got)))
    finally:
        pump.close()

    assert np.array_equal(np.concatenate(got), np.concatenate(fed))


def test_take_concatenates_pending_chunks_into_one_array():
    """Multiple chunks pending across one consumer gap come back as ONE float32
    array — the shape the rms provider and the transcript driver both consume."""
    source = _ScriptedSource()
    pump = AudioPump(source, beat_s=0.001)
    pump.start()
    try:
        _wait(lambda: pump.live)
        source.feed(_chunk(1.0), _chunk(2.0), _chunk(3.0))
        _wait(lambda: source.calls >= 4 and pump.pending >= 3)
        out = pump.take()
    finally:
        pump.close()

    assert isinstance(out, np.ndarray)
    assert out.dtype == np.float32
    assert np.array_equal(out, np.concatenate([_chunk(1.0), _chunk(2.0), _chunk(3.0)]))


# --------------------------------------------------------------------------- #
# 3. A down source degrades cleanly: one [SENSE] line, beat-paced, no spin     #
# --------------------------------------------------------------------------- #


def test_a_down_source_degrades_to_none_without_spinning(caplog):
    """Criterion 3: a ``None``-returning, disconnected source yields ``take() ==
    None``, exactly one named client-lost transition line, and a BEAT-PACED read
    loop — every empty read is followed by one beat, never a hot spin."""
    source = _ScriptedSource(connected=False)
    sleeps: list[float] = []
    enough = threading.Event()

    def counting_sleep(seconds: float) -> None:
        sleeps.append(seconds)
        if len(sleeps) >= 20:
            enough.set()
        time.sleep(0.0002)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        pump = AudioPump(source, beat_s=0.02, sleep=counting_sleep)
        pump.start()
        assert enough.wait(5.0), "the pump never reached 20 beats"
        pump.close()

    assert pump.take() is None
    # 1:1 read/beat pairing (± the iterations in flight around close): bounded.
    assert source.calls <= len(sleeps) + 2, f"{source.calls} reads vs {len(sleeps)} beats"

    lost = [line for line in _sense_lines(caplog) if "event=client-lost" in line]
    assert len(lost) == 1, f"expected ONE client-lost line, got {lost}"
    assert lost[0].startswith("[SENSE stage=audio source=pump")


def test_a_raising_source_degrades_like_a_silent_one():
    """The held client never raises by contract, but the pump must not trust
    that: a raising ``audio()`` is 'no audio', the loop survives."""

    class _Hostile:
        connected = True

        def __init__(self) -> None:
            self.calls = 0

        def audio(self):
            self.calls += 1
            raise RuntimeError("read exploded")

    source = _Hostile()
    pump = AudioPump(source, beat_s=0.0005)
    pump.start()
    try:
        _wait(lambda: source.calls >= 3)
        assert pump.take() is None
    finally:
        pump.close()


def test_a_client_lost_mid_run_is_re_drained_on_return(caplog):
    """A reconnect gets a fresh drain: chunks that piled up while nobody was
    pumping are stale, so resumption re-runs the drain-then-live sequence rather
    than handing the consumer a burst of old audio."""
    source = _ScriptedSource()
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        pump = AudioPump(source, beat_s=0.001)
        pump.start()
        try:
            _wait(lambda: pump.live)
            source.connected = False
            _wait(lambda: not pump.live)
            # The client comes back with a small standing backlog.
            source.connected = True
            source.feed(_chunk(-2.0), _chunk(-2.0))
            _wait(lambda: pump.live)
            source.feed(_chunk(9.0))
            got = _drain_takes(pump, samples=4)
        finally:
            pump.close()

    heard = np.concatenate(got)
    assert not np.any(heard == -2.0), "post-reconnect stale audio reached the consumer"
    assert np.array_equal(heard, _chunk(9.0))
    lines = _sense_lines(caplog)
    assert sum("event=client-lost" in line for line in lines) == 1
    assert sum("event=live" in line for line in lines) == 2, "resumption must re-announce live"


# --------------------------------------------------------------------------- #
# 4. close() joins with a bounded timeout and is idempotent                    #
# --------------------------------------------------------------------------- #


def test_close_joins_the_thread_and_is_idempotent():
    source = _ScriptedSource()
    pump = AudioPump(source, beat_s=0.001)
    pump.start()
    _wait(lambda: source.calls >= 1)
    thread = pump._thread
    assert thread is not None and thread.is_alive()

    pump.close()
    assert not thread.is_alive(), "close() did not join the pump thread"
    assert pump.closed
    pump.close()  # idempotent

    before = source.calls
    time.sleep(0.01)
    assert source.calls == before, "the pump kept reading after close()"


def test_close_on_a_never_started_pump_is_safe():
    pump = AudioPump(_ScriptedSource())
    pump.close()
    pump.close()
    assert pump.take() is None


def test_start_after_close_is_refused_and_start_is_idempotent(caplog):
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        source = _ScriptedSource()
        pump = AudioPump(source, beat_s=0.001)
        pump.start()
        pump.start()  # a second start must not spawn a second thread
        _wait(lambda: source.calls >= 1)
        pump.close()
        pump.start()  # closed: refused
        assert pump._thread is None

    started = [line for line in _sense_lines(caplog) if "event=started" in line]
    assert len(started) == 1, f"expected ONE started line, got {started}"


# --------------------------------------------------------------------------- #
# Overflow: bounded buffer drops the OLDEST and counts per episode             #
# --------------------------------------------------------------------------- #


def test_overflow_drops_the_oldest_and_logs_one_episode_line(caplog):
    """A full pending buffer drops the OLDEST chunk (freshness wins) and the
    drops are reported ONCE per episode with a count — never per chunk."""
    source = _ScriptedSource()
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        pump = AudioPump(source, max_chunks=4, beat_s=0.001)
        pump.start()
        try:
            _wait(lambda: pump.live)
            source.feed(*[_chunk(i, size=2) for i in range(10)])
            _wait(lambda: pump.dropped == 6)
            out = pump.take()
        finally:
            pump.close()

    # The newest 4 chunks survived, in order.
    assert np.array_equal(
        out, np.concatenate([_chunk(6.0, 2), _chunk(7.0, 2), _chunk(8.0, 2), _chunk(9.0, 2)])
    )
    drop_lines = [line for line in _sense_lines(caplog) if "deque-overflow" in line]
    assert len(drop_lines) == 1, f"expected ONE per-episode drop line, got {drop_lines}"
    assert "dropped reason=" in drop_lines[0] and "count=6" in drop_lines[0]


def test_degenerate_chunks_are_skipped_not_latched():
    """Empty and uncoercible reads are 'no audio', never latched garbage."""
    source = _ScriptedSource([np.zeros(0, dtype=np.float32), object()])
    pump = AudioPump(source, beat_s=0.001)
    pump.start()
    try:
        _wait(lambda: pump.live)
        source.feed(_chunk(5.0))
        got = _drain_takes(pump, samples=4)
        assert np.array_equal(np.concatenate(got), _chunk(5.0))
    finally:
        pump.close()


# --------------------------------------------------------------------------- #
# Import hygiene                                                              #
# --------------------------------------------------------------------------- #


def test_module_is_import_safe_without_the_sdk():
    """The pump only calls the injected source: no SDK import, ever (the module
    docstring may NAME the SDK — it cites the appsink numbers — but no import
    statement may touch it)."""
    src = inspect.getsource(audio_pump_mod)
    assert "import reachy_mini" not in src
    assert "from reachy_mini" not in src
