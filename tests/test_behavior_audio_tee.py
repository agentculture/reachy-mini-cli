"""Unit tests for :mod:`reachy.behavior.audio_tee` — the third pump consumer (t4).

The tee fans the ONE per-tick chunk ``_AudioTap`` already swapped off the
:class:`~reachy.behavior.audio_pump.AudioPump` out to a local unix socket, so an
external process (the embodiment layer) can hear the robot's mic without opening
a second SDK media session. Two defect classes bound the design, and every test
here pins one of them:

1. **A second ``take()`` halves everyone's audio.** ``AudioPump.take()`` is a
   CONSUMING latch swap; the tee must be a consumer of the one take, never a
   taker itself. Asserted structurally over this module's AST (it names neither
   ``take`` nor ``audio``) and behaviourally in the composition tests.
2. **Nothing on the 20 ms tick may block.** :meth:`AudioTee.offer` does no
   socket I/O at all — a bounded queue absorbs the chunk and a background worker
   owns every ``send``. A wedged consumer (a client that connects and never
   reads) must produce NAMED drops and leave the offering thread free; the test
   for that offers from a thread and fails on a join timeout, so a regression
   that reintroduces blocking is a clean failure rather than a hung suite.

These tests use REAL ``AF_UNIX`` sockets (loopback-free, no network), so the file
deliberately does NOT carry ``@pytest.mark.offline``: that lane monkeypatches
``socket.socket.connect`` to raise, which would break a unix-domain connect that
never touches a network at all.
"""

from __future__ import annotations

import ast
import inspect
import json
import socket
import textwrap
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from reachy.behavior import audio_tee as tee_mod
from reachy.behavior.audio_tee import AudioTee
from tests.conftest import WAIT_BUDGET_S

#: The shared contention budget (see :data:`tests.conftest.WAIT_BUDGET_S` for
#: why it is generous). Everything waited on here — an accept, a header, three
#: tiny chunks — lands within a couple of ``DEFAULT_BEAT_S`` sweeps on an idle
#: box, so this number only bounds scheduler starvation, never behaviour.
_TIMEOUT = WAIT_BUDGET_S


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _chunk(start: float, size: int = 8) -> np.ndarray:
    return np.arange(start, start + size, dtype=np.float32)


def _wait(predicate, *, timeout: float = _TIMEOUT, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _connect(path: Path, *, timeout: float = _TIMEOUT) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(timeout)
    sock.connect(str(path))
    return sock


def _read_exactly(sock: socket.socket, count: int) -> bytes:
    buf = bytearray()
    while len(buf) < count:
        piece = sock.recv(count - len(buf))
        if not piece:
            raise AssertionError(f"consumer socket closed after {len(buf)}/{count} bytes")
        buf.extend(piece)
    return bytes(buf)


def _read_header(sock: socket.socket) -> dict:
    """Read the newline-terminated JSON header the tee writes on accept."""
    buf = bytearray()
    while not buf.endswith(b"\n"):
        piece = sock.recv(1)
        if not piece:
            raise AssertionError("consumer socket closed before the header arrived")
        buf.extend(piece)
    return json.loads(bytes(buf).decode("utf-8"))


def _samples(payload: bytes) -> np.ndarray:
    return np.frombuffer(payload, dtype=tee_mod.SAMPLE_DTYPE)


def _drop_reasons(caplog) -> list[str]:
    return [
        record.getMessage() for record in caplog.records if "dropped reason=" in record.getMessage()
    ]


@pytest.fixture()
def tee_path(tmp_path) -> Path:
    return tmp_path / "audio_tee.sock"


# --------------------------------------------------------------------------- #
# 1. The stream a consumer receives: header, then contiguous mono float32      #
# --------------------------------------------------------------------------- #


def test_a_consumer_receives_the_header_then_contiguous_mono_float32(tee_path):
    """Criterion 1's wire half: chunks arrive contiguous, mono, in order.

    The header is self-describing (format + rate + channels) so the embodiment
    layer never has to guess the mic's rate — that guess is exactly what
    mis-times a server-side VAD.
    """
    tee = AudioTee(tee_path, samplerate_provider=lambda: 48000)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1), "the tee never accepted the consumer"
        header = _read_header(consumer)
        assert header["stream"] == tee_mod.WIRE_NAME
        assert header["version"] == tee_mod.WIRE_VERSION
        assert header["format"] == tee_mod.WIRE_FORMAT
        assert header["channels"] == 1
        assert header["samplerate"] == 48000

        chunks = [_chunk(0.0), _chunk(8.0), _chunk(16.0)]
        for chunk in chunks:
            tee.offer(chunk)
        expected = np.concatenate(chunks)
        payload = _read_exactly(consumer, expected.size * tee_mod.BYTES_PER_SAMPLE)
        np.testing.assert_allclose(_samples(payload), expected)
        assert tee.queued == len(chunks)
        assert tee.dropped == 0
        # The measurable seam an on-box run reads back (t15's tick-budget work).
        assert tee.sent_bytes >= len(payload)
        consumer.close()
    finally:
        tee.close()


def test_a_multichannel_read_is_coerced_to_mono_at_the_boundary(tee_path):
    """``to_mono`` at the audio boundary, never a bare ``reshape(-1)``.

    Flattening an ``(N, 2)`` read interleaves both channels into one
    double-length stream that the header then mislabels — the closed portability
    hazard :mod:`reachy.robot.audio_shape` exists to state once. The tee is a
    wire boundary, so it coerces defensively even though today's pump already
    hands it 1-D audio (a pass-through with no copy).
    """
    tee = AudioTee(tee_path)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        _read_header(consumer)

        stereo = np.stack([np.arange(4, dtype=np.float32), np.full(4, 9.0, np.float32)], axis=1)
        tee.offer(stereo)
        payload = _read_exactly(consumer, 4 * tee_mod.BYTES_PER_SAMPLE)
        np.testing.assert_allclose(_samples(payload), np.arange(4, dtype=np.float32))
        consumer.close()
    finally:
        tee.close()


def test_two_consumers_receive_the_same_stream(tee_path):
    """The fan-out is per consumer: neither steals the other's audio."""
    tee = AudioTee(tee_path)
    assert tee.start() is True
    try:
        first = _connect(tee_path)
        second = _connect(tee_path)
        assert _wait(lambda: tee.clients == 2), f"only {tee.clients} consumer(s) accepted"
        _read_header(first)
        _read_header(second)

        chunk = _chunk(1.0)
        tee.offer(chunk)
        for consumer in (first, second):
            payload = _read_exactly(consumer, chunk.size * tee_mod.BYTES_PER_SAMPLE)
            np.testing.assert_allclose(_samples(payload), chunk)
            consumer.close()
    finally:
        tee.close()


def test_a_departing_consumer_is_reaped_and_the_tee_survives(tee_path, caplog):
    """A consumer that hangs up is named once; the tee keeps running."""
    caplog.set_level("INFO", logger="reachy.sense")
    tee = AudioTee(tee_path)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        _read_header(consumer)
        consumer.close()
        assert _wait(lambda: tee.clients == 0), "the departed consumer was never reaped"

        tee.offer(_chunk(0.0))  # must not raise, must not reconnect anything
        assert tee.queued == 0
        assert _wait(
            lambda: any(tee_mod.REASON_NO_CONSUMER in line for line in _drop_reasons(caplog))
        ), "audio discarded for want of a consumer was never NAMED"
    finally:
        tee.close()


# --------------------------------------------------------------------------- #
# 2. Nothing on the tick thread blocks                                        #
# --------------------------------------------------------------------------- #


def test_with_no_consumer_the_offer_is_a_no_op_named_once(tee_path, caplog):
    """Criterion 3's local half: nothing connected means no work and no flood.

    The no-consumer state is NAMED (never a silent no-op) but reported ONCE per
    episode, following :class:`~reachy.behavior.audio_pump.AudioPump`'s
    client-lost discipline — at 50 Hz a per-tick line would be the whole journal.
    The line comes from the WORKER: the tick thread does not even log.
    """
    caplog.set_level("INFO", logger="reachy.sense")
    tee = AudioTee(tee_path)
    assert tee.start() is True
    try:
        for _ in range(200):
            tee.offer(_chunk(0.0))
        assert tee.queued == 0, "chunks were buffered with nobody listening"
        assert tee.offers == 200

        def _named() -> list[str]:
            return [line for line in _drop_reasons(caplog) if tee_mod.REASON_NO_CONSUMER in line]

        assert _wait(lambda: len(_named()) == 1), "the no-consumer episode was never named"
        time.sleep(4 * tee_mod.DEFAULT_BEAT_S)  # several more worker sweeps
        assert len(_named()) == 1, f"expected ONE no-consumer line, got {len(_named())}"
    finally:
        tee.close()


def test_a_wedged_consumer_never_blocks_the_offering_thread(tee_path, caplog):
    """Criterion 2: a consumer that never reads costs the offering thread nothing.

    The consumer connects and then reads NOTHING, so the kernel send buffer fills
    (pinned small via the ``sndbuf`` test seam) and every later send would block a
    naive writer. The offers run on their own thread and the assertion is a JOIN
    with a timeout: a regression that puts socket I/O back on the offering path
    fails here in bounded time instead of hanging the suite.

    The shared queue is deliberately roomy and the consumer's outbox tight, so
    the drop lands where this test is aiming — on the CONSUMER bound (a slow
    reader), not on the shared one (a starved worker, covered by the
    ``_ChunkQueue`` unit tests).
    """
    caplog.set_level("INFO", logger="reachy.sense")
    tee = AudioTee(tee_path, max_chunks=1024, max_client_chunks=4, sndbuf=2048)
    assert tee.start() is True
    consumer = None
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)

        done = threading.Event()
        payload = np.zeros(1024, dtype=np.float32)

        def _offer_many() -> None:
            for _ in range(300):
                tee.offer(payload)
            done.set()

        worker = threading.Thread(target=_offer_many, daemon=True)
        worker.start()
        assert done.wait(_TIMEOUT), (
            "offer() blocked — a wedged consumer backpressured the offering "
            "thread, which on the runtime is the 20 ms tick"
        )
        assert _wait(lambda: tee.dropped > 0), "a wedged consumer never produced a drop"
        assert any(
            tee_mod.REASON_CONSUMER_SLOW in line for line in _drop_reasons(caplog)
        ), f"the drop was not NAMED consumer-slow: {_drop_reasons(caplog)}"
        assert tee.clients == 1, "a slow consumer was disconnected rather than dropped"
    finally:
        if consumer is not None:
            consumer.close()
        tee.close()


def test_offer_performs_no_socket_io_at_all(tee_path):
    """The structural half of criterion 2, read off :meth:`AudioTee.offer`'s AST.

    A behavioural test can only observe that today's offer did not block; this
    one states WHY it cannot: the method names no socket call. Same idiom as
    ``tests/test_zero_llm_boundary.py`` — the defect it prevents is a refactor
    that "just writes the chunk where it already is".
    """
    tree = ast.parse(textwrap.dedent(inspect.getsource(AudioTee.offer)))
    forbidden = {"send", "sendall", "sendto", "recv", "accept", "connect", "select", "flush"}
    named = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert not (named & forbidden), f"offer() performs socket I/O: {sorted(named & forbidden)}"


def test_the_tee_never_takes_from_the_pump_and_never_reads_the_mic():
    """Criterion 1's structural half: the tee is a CONSUMER of the one take.

    ``AudioPump.take()`` is a consuming swap — a second caller would hand each
    consumer half the audio, the documented defect class this whole seam exists
    to avoid. The tee is handed chunks; it names neither ``take`` nor ``audio``
    anywhere, so it cannot become a second reader by accident.
    """
    source = Path(inspect.getfile(tee_mod)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    called = {
        node.func.attr
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "take" not in called, "the tee calls .take() — that is a SECOND consuming pump read"
    assert "audio" not in called, "the tee reads audio itself — it must be HANDED the tick's chunk"
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert "reachy_mini" not in imported, "the tee must never import the SDK"
    assert not {name for name in imported if name.startswith("reachy.speech")}


# --------------------------------------------------------------------------- #
# 3. The bounded queue: drop-oldest, counted, named per episode               #
# --------------------------------------------------------------------------- #


def test_the_bounded_queue_drops_the_oldest_chunk():
    """Freshness wins, exactly as :class:`AudioPump`'s pending buffer decides."""
    q = tee_mod._ChunkQueue(3)
    for value in (b"a", b"b", b"c"):
        assert q.push(value) == 0
    assert q.push(b"d") == 1
    assert list(q) == [b"b", b"c", b"d"]


def test_the_bounded_queue_never_drops_a_partially_sent_head():
    """A stream socket can accept a PARTIAL write, so the head may be in flight.

    Dropping it would splice the consumer's float32 frame mid-sample and
    misalign every sample after it. Overflow therefore drops the oldest chunk
    that is NOT in flight; when the head is all there is, the NEW chunk goes
    instead. Either way a drop is always a whole number of samples.
    """
    q = tee_mod._ChunkQueue(2)
    q.push(b"head")
    q.push(b"next")
    assert q.push(b"new", protect_head=True) == 1
    assert list(q) == [b"head", b"new"]

    single = tee_mod._ChunkQueue(1)
    single.push(b"head")
    assert single.push(b"new", protect_head=True) == 1
    assert list(single) == [b"head"], "the in-flight head was dropped"


# --------------------------------------------------------------------------- #
# 4. Lifecycle: the socket file, and never clobbering a live one              #
# --------------------------------------------------------------------------- #


def test_start_creates_the_socket_and_close_removes_it(tee_path):
    tee = AudioTee(tee_path)
    assert tee.start() is True
    assert tee_path.exists(), "the tee did not create its socket"
    tee.close()
    assert not tee_path.exists(), "the socket outlived the runtime"
    tee.close()  # idempotent


def test_close_on_a_never_started_tee_is_safe(tee_path):
    AudioTee(tee_path).close()
    assert not tee_path.exists()


def test_offer_before_start_is_a_no_op(tee_path):
    tee = AudioTee(tee_path)
    tee.offer(_chunk(0.0))
    assert tee.queued == 0
    tee.close()


def test_a_stale_socket_file_is_replaced(tee_path):
    """A crashed runtime leaves an unlinked socket file; the next start reclaims it."""
    stale = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    stale.bind(str(tee_path))
    stale.close()  # bound, never listening: connects are refused
    assert tee_path.exists()

    tee = AudioTee(tee_path)
    try:
        assert tee.start() is True
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        consumer.close()
    finally:
        tee.close()


def test_a_live_listener_on_the_path_is_never_clobbered(tee_path, caplog):
    """Another live tee owns the path: refuse it, named, rather than unlink it.

    Unlinking a socket somebody is serving on is silent theft — the incumbent
    keeps its fd and its consumers keep their connections, but nothing can ever
    reach it again. The honest outcome is that the SECOND tee disables itself.
    """
    caplog.set_level("INFO", logger="reachy.sense")
    incumbent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    incumbent.bind(str(tee_path))
    incumbent.listen(4)
    tee = AudioTee(tee_path)
    try:
        assert tee.start() is False
        assert tee.active is False
        assert tee_path.exists(), "the live socket was unlinked"
        assert any(tee_mod.REASON_SOCKET_IN_USE in line for line in _drop_reasons(caplog))
        # The incumbent still serves: a consumer reaches IT, not the refused tee.
        consumer = _connect(tee_path)
        served, _addr = incumbent.accept()
        served.close()
        consumer.close()
        tee.offer(_chunk(0.0))  # inert, never raises
        assert tee.queued == 0
    finally:
        tee.close()
        incumbent.close()
        tee_path.unlink(missing_ok=True)


def test_close_does_not_remove_a_socket_it_did_not_create(tee_path):
    """The refused tee's close() must leave the incumbent's socket file alone."""
    incumbent = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    incumbent.bind(str(tee_path))
    incumbent.listen(4)
    try:
        tee = AudioTee(tee_path)
        assert tee.start() is False
        tee.close()
        assert tee_path.exists(), "closing a refused tee removed the live socket"
    finally:
        incumbent.close()
        tee_path.unlink(missing_ok=True)


def test_an_unusable_path_degrades_to_a_named_drop(tmp_path, caplog):
    """A bind that cannot succeed disables the tee — it never kills the runtime."""
    caplog.set_level("INFO", logger="reachy.sense")
    tee = AudioTee(tmp_path / "no-such-dir" / "audio_tee.sock")
    try:
        assert tee.start() is False
        assert tee.active is False
        assert any(tee_mod.REASON_BIND_FAILED in line for line in _drop_reasons(caplog))
        tee.offer(_chunk(0.0))
        assert tee.queued == 0
    finally:
        tee.close()


# --------------------------------------------------------------------------- #
# 5. Configuration: the kill switch and the socket path                       #
# --------------------------------------------------------------------------- #


def test_disabled_by_env_binds_nothing(tee_path, monkeypatch, caplog):
    caplog.set_level("INFO", logger="reachy.sense")
    monkeypatch.setenv(tee_mod.ENABLED_ENV, "0")
    tee = AudioTee(tee_path)
    try:
        assert tee.start() is False
        assert not tee_path.exists()
        assert any(tee_mod.REASON_DISABLED in line for line in _drop_reasons(caplog))
    finally:
        tee.close()


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, True, id="absent"),
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="blank"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("OFF", False, id="OFF-case-insensitive"),
        pytest.param("1", True, id="one"),
        pytest.param("yes", True, id="yes"),
        pytest.param("banana", True, id="arbitrary"),
    ],
)
def test_enabled_env_parsing(monkeypatch, raw, expected):
    """Same four-way reading ``REACHY_PAT_SENSE`` uses: an explicitly EMPTY value
    means "unset this", not "turn it on"."""
    if raw is None:
        monkeypatch.delenv(tee_mod.ENABLED_ENV, raising=False)
    else:
        monkeypatch.setenv(tee_mod.ENABLED_ENV, raw)
    assert tee_mod.tee_enabled() is expected


def test_socket_path_defaults_under_the_state_dir(monkeypatch, tmp_path):
    monkeypatch.delenv(tee_mod.SOCKET_ENV, raising=False)
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    assert tee_mod.socket_path() == tmp_path / tee_mod.DEFAULT_SOCKET_NAME

    monkeypatch.setenv(tee_mod.SOCKET_ENV, str(tmp_path / "elsewhere.sock"))
    assert tee_mod.socket_path() == tmp_path / "elsewhere.sock"


def test_the_header_reports_an_unknown_rate_as_null(tee_path):
    """A cold media holder cannot report a rate; say so rather than guess one."""
    tee = AudioTee(tee_path, samplerate_provider=lambda: None)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        assert _read_header(consumer)["samplerate"] is None
        consumer.close()
    finally:
        tee.close()


def test_a_raising_samplerate_probe_is_a_null_rate_not_a_crash(tee_path):
    def _boom():
        raise RuntimeError("cold holder")

    tee = AudioTee(tee_path, samplerate_provider=_boom)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        assert _read_header(consumer)["samplerate"] is None
        consumer.close()
    finally:
        tee.close()


def test_degenerate_chunks_are_never_queued(tee_path):
    """``None``/empty/unusable reads are "no audio this tick", not a wire event."""
    tee = AudioTee(tee_path)
    assert tee.start() is True
    try:
        consumer = _connect(tee_path)
        assert _wait(lambda: tee.clients == 1)
        _read_header(consumer)
        for degenerate in (None, np.zeros(0, dtype=np.float32), object(), np.zeros((2, 2, 2))):
            tee.offer(degenerate)
        assert tee.queued == 0
        consumer.close()
    finally:
        tee.close()


def test_module_is_import_safe_without_the_sdk():
    """The tee composes on a bare box: stdlib + numpy + the shared shape helper."""
    import importlib

    module = importlib.import_module("reachy.behavior.audio_tee")
    assert module.AudioTee is AudioTee
