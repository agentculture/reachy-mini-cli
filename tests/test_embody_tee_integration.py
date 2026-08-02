"""The two ends of the audio tee, connected — the test that should have existed.

Tasks t4 (``reachy/behavior/audio_tee.py``, the WRITER) and t6
(``reachy/embody/media.py``'s ``_RobotTeeSourceBackend``, the READER) built the
two halves of one unix-socket audio pipe independently, and each shipped green
against its OWN assumption about the wire:

* the writer wrote one newline-terminated JSON header line and then contiguous
  little-endian **float32** mono samples;
* the reader expected a **headerless** stream of little-endian **int16**, with
  the sample rate supplied out of band.

Deployed together that is not silence, which is what makes it dangerous: the
reader would have parsed the header's ASCII bytes as audio and then misread
float32 as int16, so the layer would have appeared to *hear noise* rather than
to be broken. Neither side's suite could catch it, because no test connected
them — every reader test fed it a payload the reader itself had framed.

So this module refuses to describe the wire at all. It starts the REAL
:class:`~reachy.behavior.audio_tee.AudioTee` on a REAL unix socket in a tmp dir,
points the REAL reader at it, and asserts on what comes out the far end. If the
two ends ever disagree again — about the header, the dtype, the sample size, or
the default socket path — a test here fails, not a robot.
"""

from __future__ import annotations

import ast
import json
import logging
import socket
import threading
import time
from pathlib import Path

import numpy as np
import pytest

from reachy.behavior import audio_tee
from reachy.embody import media
from tests.conftest import WAIT_BUDGET_S

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEDIA_PATH = _REPO_ROOT / "reachy" / "embody" / "media.py"

_TIMEOUT_S = WAIT_BUDGET_S


# --------------------------------------------------------------------------- #
# Harness                                                                     #
# --------------------------------------------------------------------------- #


def _wait_for(predicate, *, timeout: float = _TIMEOUT_S) -> bool:
    """Poll *predicate* until true or *timeout*. Returns whether it came true."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _sense_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == "reachy.sense"]


class _LiveTee:
    """A started :class:`AudioTee` plus a connected reader backend, both real.

    The one ordering subtlety the pipe has: :meth:`AudioTee.offer` DISCARDS a
    chunk when nobody is attached (that is the documented ``no-consumer`` drop),
    so a test must not offer audio until the tee's worker has accepted the
    reader. :meth:`attach` waits for exactly that.
    """

    def __init__(self, tmp_path: Path, **backend_kwargs) -> None:
        self.path = tmp_path / "audio_tee.sock"
        self.samplerate: object = 16000
        self.tee = audio_tee.AudioTee(
            self.path,
            samplerate_provider=lambda: self.samplerate,
            enabled=True,
            beat_s=0.005,
        )
        self._backend_kwargs = backend_kwargs
        self.backend: media._RobotTeeSourceBackend | None = None

    def start(self, *, samplerate: object = 16000) -> "_LiveTee":
        self.samplerate = samplerate
        assert self.tee.start() is True, "the tee refused to bind in a tmp dir"
        return self

    def attach(self, **overrides) -> media._RobotTeeSourceBackend:
        kwargs = {
            "native_sample_rate": 16000,
            "connect_timeout": 1.0,
            "read_timeout": 0.02,
            **self._backend_kwargs,
            **overrides,
        }
        self.backend = media._RobotTeeSourceBackend(self.path, **kwargs)
        # The first read establishes the connection; the tee's worker accepts on
        # its next sweep and writes the header there.
        self.backend.read_native()
        assert _wait_for(lambda: self.tee.clients == 1), "the tee never accepted the reader"
        return self.backend

    def drain(self, expected: int, *, timeout: float = _TIMEOUT_S) -> np.ndarray:
        """Read until *expected* samples have arrived (or time out); concatenate."""
        assert self.backend is not None
        collected: list[np.ndarray] = []
        rates: list[int] = []
        deadline = time.monotonic() + timeout
        while sum(int(c.size) for c in collected) < expected and time.monotonic() < deadline:
            result = self.backend.read_native()
            if result is None:
                continue
            samples, rate = result
            rates.append(rate)
            collected.append(samples)
        self.rates = rates
        return np.concatenate(collected) if collected else np.empty(0, dtype=np.float32)

    def close(self) -> None:
        if self.backend is not None:
            self.backend.close()
        self.tee.close()


@pytest.fixture
def live(tmp_path):
    built: list[_LiveTee] = []

    def _make(**kwargs) -> _LiveTee:
        pipe = _LiveTee(tmp_path, **kwargs)
        built.append(pipe)
        return pipe

    yield _make
    for pipe in built:
        pipe.close()


def _serve_raw_once(path: Path, payload: bytes, *, keep_open_s: float = 1.0) -> threading.Thread:
    """A foreign server: binds *path*, accepts once, sends *payload* verbatim.

    Used for the headers the real writer can never produce — a headerless int16
    stream (the reader's ORIGINAL assumption), a truncated line, a foreign
    ``stream`` name, an unknown ``version``.
    """
    srv = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    srv.bind(str(path))
    srv.listen(1)

    def _run() -> None:
        conn, _ = srv.accept()
        try:
            conn.sendall(payload)
            time.sleep(keep_open_s)
        finally:
            conn.close()
            srv.close()

    thread = threading.Thread(target=_run, daemon=True)
    thread.start()
    return thread


# --------------------------------------------------------------------------- #
# The happy path — real writer to real reader                                 #
# --------------------------------------------------------------------------- #


def test_the_reader_receives_the_writers_chunks_exactly_in_order(live):
    """The whole point: known samples in one end, the SAME samples out the other.

    Exact equality (``array_equal``, not ``allclose``): the wire is float32 and
    the reader does no scaling, so any surviving int16 round-trip — or any
    off-by-one in the 4-bytes-per-sample arithmetic — changes these values.
    """
    pipe = live().start(samplerate=16000)
    pipe.attach()

    chunks = [
        np.linspace(-1.0, 1.0, 7, dtype=np.float32),
        np.array([0.25, -0.5, 0.125], dtype=np.float32),
        np.linspace(0.9, -0.9, 11, dtype=np.float32),
    ]
    for chunk in chunks:
        pipe.tee.offer(chunk)

    expected = np.concatenate(chunks)
    got = pipe.drain(expected.size)

    assert got.size == expected.size, f"expected {expected.size} samples, got {got.size}"
    assert np.array_equal(got, expected)
    assert got.dtype == np.float32


def test_the_reader_takes_the_native_rate_from_the_header_not_its_config(live):
    """The header is self-describing, so the configured rate is only a fallback.

    The backend is built claiming 48000 Hz and the writer announces 16000 Hz;
    the reader must report the writer's number, because a wrong rate mis-times
    every downstream decision (resampling here, server-side VAD later).
    """
    pipe = live().start(samplerate=16000)
    pipe.attach(native_sample_rate=48000)

    pipe.tee.offer(np.zeros(16, dtype=np.float32))
    pipe.drain(16)

    assert pipe.rates, "no chunk arrived at all"
    assert set(pipe.rates) == {16000}


def test_a_null_samplerate_header_falls_back_to_the_configured_rate(live, caplog):
    """A cold media holder cannot report a rate — the writer says ``null``, honestly.

    That is a legitimate header, not a fault, so the reader keeps reading and
    falls back to its configured rate — but it must SAY so, because every later
    consumer is now working off a guess.
    """
    pipe = live().start(samplerate=None)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        pipe.attach(native_sample_rate=22050)
        pipe.tee.offer(np.zeros(8, dtype=np.float32))
        pipe.drain(8)

    assert set(pipe.rates) == {22050}
    named = [line for line in _sense_lines(caplog) if media.TEE_RATE_UNKNOWN in line]
    assert len(named) == 1, f"expected exactly one named rate-unknown line, got {named}"
    assert "22050" in named[0], named[0]


def test_the_header_is_consumed_and_never_reaches_the_caller_as_audio(live):
    """The defect in its original form: the header's ASCII bytes are not samples.

    ``{"stream":"reachy-audio-tee",...}`` read as float32 lands in the 1e-2..1e34
    range; genuine samples here are all zero. So an all-zero result proves the
    header line was consumed as a header.
    """
    pipe = live().start(samplerate=16000)
    pipe.attach()

    pipe.tee.offer(np.zeros(32, dtype=np.float32))
    got = pipe.drain(32)

    assert got.size == 32
    assert np.array_equal(got, np.zeros(32, dtype=np.float32))


def test_the_embody_source_wrapper_resamples_from_the_header_rate(live):
    """End to end through the public wrapper, not just the backend.

    The writer announces 48000 Hz; :class:`EmbodySource` normalises to 16000 Hz.
    The 3:1 length ratio can only come from the HEADER's rate reaching the
    resample step.
    """
    pipe = live().start(samplerate=48000)
    backend = pipe.attach(native_sample_rate=16000)  # deliberately wrong config
    source = media.EmbodySource(backend, target_sample_rate=16000)

    pipe.tee.offer(np.linspace(-0.5, 0.5, 480, dtype=np.float32))

    collected: list[np.ndarray] = []
    deadline = time.monotonic() + _TIMEOUT_S
    while sum(int(c.size) for c in collected) < 150 and time.monotonic() < deadline:
        chunk = source.read()
        if chunk is not None and chunk.size:
            collected.append(chunk)
    got = np.concatenate(collected) if collected else np.empty(0, dtype=np.float32)

    assert 150 <= got.size <= 170, f"unexpected resampled length: {got.size}"


# --------------------------------------------------------------------------- #
# Chunk boundaries that do not align to sample boundaries                     #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("recv_bytes", [1, 3, 5, 7, 13])
def test_reads_that_land_mid_sample_never_desync_the_stream(live, recv_bytes):
    """``recv`` is a byte stream: it splits wherever it likes, header included.

    Every ``recv_bytes`` here is coprime-ish with the 4-byte sample size, so the
    reads land mid-sample repeatedly and the header itself arrives across many
    calls. A single byte kept or dropped in error shifts every sample after it,
    which exact equality on a strictly-increasing ramp detects immediately.
    """
    pipe = live().start(samplerate=16000)
    pipe.attach(recv_bytes=recv_bytes)

    expected = np.linspace(-1.0, 1.0, 37, dtype=np.float32)
    pipe.tee.offer(expected)

    got = pipe.drain(expected.size)

    assert got.size == expected.size, f"expected {expected.size} samples, got {got.size}"
    assert np.array_equal(got, expected)


def test_many_small_chunks_arrive_contiguous_across_split_reads(live):
    """Fan-out order plus partial reads: one long ramp, cut into 20 offers."""
    pipe = live().start(samplerate=16000)
    pipe.attach(recv_bytes=6)

    expected = np.linspace(-1.0, 1.0, 20 * 9, dtype=np.float32)
    for chunk in np.split(expected, 20):
        pipe.tee.offer(chunk)

    got = pipe.drain(expected.size)

    assert np.array_equal(got, expected)


# --------------------------------------------------------------------------- #
# A header this reader cannot understand: refuse, never guess                 #
# --------------------------------------------------------------------------- #


def _header(**overrides) -> bytes:
    payload = {
        "stream": audio_tee.WIRE_NAME,
        "version": audio_tee.WIRE_VERSION,
        "format": audio_tee.WIRE_FORMAT,
        "channels": 1,
        "samplerate": 16000,
    }
    payload.update(overrides)
    return json.dumps(payload).encode("utf-8") + audio_tee.HEADER_TERMINATOR


_FOREIGN_HEADERS = {
    # The reader's ORIGINAL assumption: a headerless int16 stream. Its bytes
    # contain newlines by chance, so "find a line, parse it" must still refuse.
    "headerless-int16": (np.arange(-2000, 2000, dtype="<i2").tobytes(), media.TEE_HEADER_INVALID),
    "not-json": (b"hello, is this the audio tee?\n" + b"\x00" * 64, media.TEE_HEADER_INVALID),
    "json-but-not-an-object": (b"[1, 2, 3]\n" + b"\x00" * 64, media.TEE_HEADER_INVALID),
    "foreign-stream": (_header(stream="somebody-elses-audio"), media.TEE_HEADER_FOREIGN),
    "unknown-version": (_header(version=audio_tee.WIRE_VERSION + 1), media.TEE_HEADER_FOREIGN),
    "wrong-format": (_header(format="s16le"), media.TEE_HEADER_FOREIGN),
    "stereo": (_header(channels=2), media.TEE_HEADER_FOREIGN),
}


@pytest.mark.parametrize("case", sorted(_FOREIGN_HEADERS))
def test_a_header_this_reader_cannot_understand_is_a_named_refusal(tmp_path, caplog, case):
    """Never "just start reading samples anyway" — the refusal is named and total."""
    payload, expected_reason = _FOREIGN_HEADERS[case]
    sock_path = tmp_path / "foreign.sock"
    _serve_raw_once(sock_path, payload, keep_open_s=1.0)

    backend = media._RobotTeeSourceBackend(
        sock_path, native_sample_rate=16000, connect_timeout=1.0, read_timeout=0.02
    )
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            deadline = time.monotonic() + 2.0
            results = []
            while time.monotonic() < deadline:
                results.append(backend.read_native())
                if any(expected_reason in line for line in _sense_lines(caplog)):
                    break
    finally:
        backend.close()

    assert all(r is None for r in results), "garbage was handed to the caller as audio"
    named = [line for line in _sense_lines(caplog) if expected_reason in line]
    assert named, f"no named {expected_reason} drop; saw {_sense_lines(caplog)}"
    assert named[0].startswith("[SENSE stage=embody source=media-robot-source")


def test_a_refused_header_disconnects_and_backs_off_rather_than_spinning(tmp_path, caplog):
    """A refusal must cost a disconnect + a backoff, not a reconnect storm."""
    sock_path = tmp_path / "foreign.sock"
    _serve_raw_once(sock_path, b"not a tee at all\n" + b"\x00" * 64, keep_open_s=1.0)

    backend = media._RobotTeeSourceBackend(
        sock_path,
        native_sample_rate=16000,
        connect_timeout=1.0,
        read_timeout=0.02,
        retry_backoff=30.0,
    )
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            for _ in range(50):
                assert backend.read_native() is None
    finally:
        backend.close()

    refusals = [line for line in _sense_lines(caplog) if media.TEE_HEADER_INVALID in line]
    assert len(refusals) == 1, f"expected one refusal inside the backoff, got {len(refusals)}"


def test_a_peer_that_closes_mid_header_is_the_ordinary_tee_closed_drop(tmp_path, caplog):
    """A truncated header is a departed writer, not a foreign one."""
    sock_path = tmp_path / "truncated.sock"
    _serve_raw_once(sock_path, b'{"stream":"reachy-audio', keep_open_s=0.0)

    backend = media._RobotTeeSourceBackend(
        sock_path, native_sample_rate=16000, connect_timeout=1.0, read_timeout=0.02
    )
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                assert backend.read_native() is None
                if any("tee-closed" in line for line in _sense_lines(caplog)):
                    break
    finally:
        backend.close()

    assert any("tee-closed" in line for line in _sense_lines(caplog)), _sense_lines(caplog)


def test_an_oversized_header_line_is_refused_rather_than_buffered_forever(tmp_path, caplog):
    """A peer that never sends a newline must not grow the reader's buffer."""
    sock_path = tmp_path / "endless.sock"
    _serve_raw_once(sock_path, b"x" * (media.MAX_HEADER_BYTES + 512), keep_open_s=1.0)

    backend = media._RobotTeeSourceBackend(
        sock_path, native_sample_rate=16000, connect_timeout=1.0, read_timeout=0.02
    )
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                assert backend.read_native() is None
                if any(media.TEE_HEADER_INVALID in line for line in _sense_lines(caplog)):
                    break
    finally:
        backend.close()

    assert any(media.TEE_HEADER_INVALID in line for line in _sense_lines(caplog))


def test_a_reconnect_reads_a_fresh_header(live):
    """Header state is per-CONNECTION: a restarted writer is read from scratch."""
    pipe = live().start(samplerate=16000)
    backend = pipe.attach()
    pipe.tee.offer(np.full(8, 0.5, dtype=np.float32))
    assert np.array_equal(pipe.drain(8), np.full(8, 0.5, dtype=np.float32))

    # The writer goes away and comes back announcing a different rate.
    pipe.tee.close()
    backend.close()
    pipe.tee = audio_tee.AudioTee(
        pipe.path, samplerate_provider=lambda: 48000, enabled=True, beat_s=0.005
    )
    assert pipe.tee.start() is True
    backend._next_attempt_t = None  # skip the backoff wait; the retry itself is tested above

    assert _wait_for(lambda: backend.read_native() is not None or pipe.tee.clients == 1)
    pipe.tee.offer(np.full(8, -0.25, dtype=np.float32))
    got = pipe.drain(8)

    assert np.array_equal(got, np.full(8, -0.25, dtype=np.float32))
    assert set(pipe.rates) == {48000}


# --------------------------------------------------------------------------- #
# One definition of the path, and one definition of the dtype                 #
# --------------------------------------------------------------------------- #


def test_both_ends_resolve_the_same_default_socket_path(tmp_path, monkeypatch):
    """The second half of the same defect: writer ``state_dir()/audio_tee.sock``
    vs reader ``state_dir()/behavior/audio_tee.sock`` — two files, no pipe."""
    monkeypatch.delenv(audio_tee.SOCKET_ENV, raising=False)  # set by the suite-wide guard
    monkeypatch.delenv(media.ENV_TEE_SOCKET, raising=False)
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))

    assert media._default_tee_socket_path() == audio_tee.socket_path()
    # Named explicitly, so a reader that quietly re-derives a ``behavior/``
    # subdirectory (the original defect) fails here rather than at a robot.
    assert media._default_tee_socket_path() == tmp_path / audio_tee.DEFAULT_SOCKET_NAME


def test_the_reader_follows_the_writers_socket_env_override(tmp_path, monkeypatch):
    """One override moves BOTH ends — a bench run cannot half-move the pipe."""
    monkeypatch.delenv(media.ENV_TEE_SOCKET, raising=False)
    monkeypatch.setenv(audio_tee.SOCKET_ENV, str(tmp_path / "elsewhere.sock"))

    assert media._default_tee_socket_path() == tmp_path / "elsewhere.sock"


def test_build_media_points_the_robot_source_at_the_writers_path(tmp_path, monkeypatch):
    monkeypatch.delenv(media.ENV_TEE_SOCKET, raising=False)
    monkeypatch.setenv(audio_tee.SOCKET_ENV, str(tmp_path / "built.sock"))

    built = media.build_media(profile="robot", base_url="http://127.0.0.1:1")
    try:
        assert built.source._backend._socket_path == tmp_path / "built.sock"
    finally:
        built.close()


def test_the_embody_specific_override_still_wins_over_the_writers_default(tmp_path, monkeypatch):
    monkeypatch.setenv(audio_tee.SOCKET_ENV, str(tmp_path / "writer.sock"))
    monkeypatch.setenv(media.ENV_TEE_SOCKET, str(tmp_path / "reader.sock"))

    built = media.build_media(profile="robot", base_url="http://127.0.0.1:1")
    try:
        assert built.source._backend._socket_path == tmp_path / "reader.sock"
    finally:
        built.close()


_CITED_FROM_THE_WRITER = {
    "BYTES_PER_SAMPLE",
    "DEFAULT_SOCKET_NAME",
    "HEADER_TERMINATOR",
    "SAMPLE_DTYPE",
    "WIRE_FORMAT",
    "WIRE_NAME",
    "WIRE_VERSION",
    "socket_path",
}


def test_the_reader_cites_the_writers_wire_constants_rather_than_re_deriving_them():
    """The fix's whole point, machine-checked: ONE definition of the wire.

    A re-derived ``"<f4"`` literal in the reader is exactly how the two ends
    drifted in the first place, so the citation is asserted structurally rather
    than left to review.
    """
    tree = ast.parse(_MEDIA_PATH.read_text(encoding="utf-8"))
    cited = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.ImportFrom) and node.module == "reachy.behavior.audio_tee"
        for alias in node.names
    }
    missing = _CITED_FROM_THE_WRITER - cited
    assert not missing, f"the reader re-derives instead of citing: {sorted(missing)}"


def test_the_robot_source_keeps_no_int16_round_trip():
    """float32 in, float32 out — converting to int16 would be lossy for nothing."""
    tree = ast.parse(_MEDIA_PATH.read_text(encoding="utf-8"))
    backend = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.ClassDef) and node.name == "_RobotTeeSourceBackend"
    )
    literals = {
        node.value
        for node in ast.walk(backend)
        if isinstance(node, ast.Constant) and isinstance(node.value, (str, int, float))
    }
    assert "<i2" not in literals, "the int16 wire assumption survives in the reader"
    assert 32768 not in literals, "a PCM16 rescale survives"
    assert 32768.0 not in literals, "a PCM16 rescale survives"


def test_the_wire_constants_still_say_float32():
    """Guard the guard: these tests are only meaningful while the wire is f32le."""
    assert audio_tee.WIRE_FORMAT == "f32le"
    assert audio_tee.SAMPLE_DTYPE == "<f4"
    assert audio_tee.BYTES_PER_SAMPLE == 4
    assert np.dtype(audio_tee.SAMPLE_DTYPE).itemsize == audio_tee.BYTES_PER_SAMPLE
    assert audio_tee.HEADER_TERMINATOR == b"\n"
