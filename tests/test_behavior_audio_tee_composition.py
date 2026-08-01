"""The audio tee, composed into the runtime seam (task t4).

``tests/test_behavior_audio_tee.py`` pins the tee itself. This file pins the
wiring, and pins it where it can actually go wrong — the three acceptance
criteria of t4:

1. **The tee is the THIRD consumer of the ONE take.**
   :meth:`~reachy.behavior.audio_pump.AudioPump.take` is a CONSUMING latch swap;
   ``_AudioTap`` calls it once at the top of the tick and fans that chunk out. A
   second take would hand each consumer half the audio — the documented defect
   class. Proved twice over: a call-counting fake pump driven through the REAL
   composed ``sense_reader`` (one take per tick, and the bytes that reach a real
   unix-socket consumer are the pump's chunks concatenated, in order), and
   structurally over ``_compose_run_seam``'s AST, because the defect it prevents
   is a refactor that "just reads the audio where it needs it".
2. **A wedged or absent consumer never reaches the tick.** Covered end to end in
   the unit file; here the composition half — with nothing attached, the tee
   queues nothing at all.
3. **With no consumer the runtime behaves as it did before the tee.** The runtime
   feed of an engine run on an injected clock is compared against the same run
   with the tee switched off; the socket lives under the state dir and is gone
   after shutdown.

Real ``AF_UNIX`` sockets again (no network), so no ``@pytest.mark.offline`` — the
lane's ``socket.connect`` guard would break a unix-domain connect.
"""

from __future__ import annotations

import ast
import json
import socket
import time
from pathlib import Path

import numpy as np
import pytest

from reachy.behavior import audio_tee as tee_mod
from reachy.behavior.engine import EngineConfig
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_mod

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BEHAVIOR_MODULE = _REPO_ROOT / "reachy" / "cli" / "_commands" / "behavior.py"
_TIMEOUT = 5.0


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (a mic-less box)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    def streaming(self):
        from contextlib import nullcontext

        return nullcontext(self.sink)

    def doa(self, timeout=None):
        return None


class _FakeMedia:
    """The held media client stand-in: warms, reports a rate, hands out nothing.

    ``audio()`` returns ``None`` on purpose — in this file the audio comes from
    the fake PUMP, which is the only thing allowed to read a mic (#100).
    """

    samplerate = 16000
    channels = 1
    camera_available = False

    def __init__(self) -> None:
        self.connected = False
        self.closed = False

    def warm_up(self) -> bool:
        self.connected = True
        return True

    def audio(self):
        return None

    def frame(self):
        return None

    def close(self) -> None:
        self.closed = True


class _CountingPump:
    """An ``AudioPump`` stand-in that counts CONSUMING takes.

    One scripted chunk per take, then ``None`` — so "how many chunks reached the
    consumer" and "how many takes happened" are directly comparable.
    """

    def __init__(self, chunks) -> None:
        self.script = list(chunks)
        self.takes = 0
        self.started = False
        self.closed = False

    def start(self) -> None:
        self.started = True

    def close(self) -> None:
        self.closed = True

    def take(self):
        self.takes += 1
        if not self.script:
            return None
        return self.script.pop(0)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _wait(predicate, *, timeout: float = _TIMEOUT, interval: float = 0.002) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


def _connect(path: Path) -> socket.socket:
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(_TIMEOUT)
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
    buf = bytearray()
    while not buf.endswith(b"\n"):
        piece = sock.recv(1)
        if not piece:
            raise AssertionError("consumer socket closed before the header arrived")
        buf.extend(piece)
    return json.loads(bytes(buf).decode("utf-8"))


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — this scan is blind")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Every call to a bare name or an attribute ending in *name*."""
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            found.append(child)
    return found


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setenv(tee_mod.SOCKET_ENV, str(tmp_path / "audio_tee.sock"))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")  # no held pose reader to fake
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: _FakeMedia())
    return tmp_path


# --------------------------------------------------------------------------- #
# 1. The tee receives the ONE take, contiguous and in order                   #
# --------------------------------------------------------------------------- #


def test_the_tee_receives_the_very_stream_the_tap_fanned_out(_isolated, monkeypatch):
    """Criterion 1: one take per tick, and the tee gets exactly that audio.

    The fake pump counts takes and the consumer is a real socket, so this is the
    whole path — ``sense_reader`` -> ``_AudioTap.pull`` -> the tee's fan-out ->
    the wire — with nothing stubbed in between.
    """
    chunks = [np.arange(i * 8, i * 8 + 8, dtype=np.float32) for i in range(5)]
    pump = _CountingPump(chunks)
    monkeypatch.setattr(behavior_mod, "AudioPump", lambda media: pump)

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    sense_reader, _metrics, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None
    )
    consumer = None
    try:
        tee = resources.tee
        assert tee is not None and tee.active, "the runtime composed no live audio tee"
        consumer = _connect(tee.path)
        assert _wait(lambda: tee.clients == 1), "the tee never accepted the consumer"
        header = _read_header(consumer)
        assert header["samplerate"] == 16000, "the tee announced the wrong mic rate"

        for tick in range(len(chunks)):
            sense_reader(float(tick) * 0.02)

        assert pump.takes == len(chunks), (
            f"{pump.takes} takes for {len(chunks)} ticks — the tick consumed the "
            "pump's latch more than once, which halves every consumer's audio"
        )
        expected = np.concatenate(chunks)
        payload = _read_exactly(consumer, expected.size * tee_mod.BYTES_PER_SAMPLE)
        got = np.frombuffer(payload, dtype=tee_mod.SAMPLE_DTYPE)
        np.testing.assert_allclose(got, expected)
    finally:
        if consumer is not None:
            consumer.close()
        resources.close()


def test_the_seam_takes_the_pump_only_through_the_tap(_isolated):
    """The structural half of criterion 1, read off ``_compose_run_seam``'s AST.

    The seam composes the pump and the tap and then never takes again: every
    consumer — the rms providers, the transcript driver and now the tee — reads
    the tick's chunk through the tap's non-consuming peek. A ``.take()`` inside
    the seam (or inside ``sense_reader``) would be a second consuming swap.
    """
    seam = _function(_module_tree(_BEHAVIOR_MODULE), "_compose_run_seam")
    assert not _calls(seam, "take"), (
        "_compose_run_seam calls .take() directly — the ONE take belongs to "
        "_AudioTap.pull; a second one hands each consumer half the audio"
    )
    assert len(_calls(seam, "_AudioTap")) == 1, "expected exactly one _AudioTap in the seam"


def test_the_tee_is_a_sink_on_the_tap_not_a_reader_of_its_own(_isolated):
    """The tee is fed BY the one swap; it never asks anyone for audio.

    Pinned as: the seam registers ``tee.offer`` as a sink on the very object
    ``sense_reader`` pulls, and the sense reader itself contains no audio read
    of its own beyond that one ``pull``. Push rather than peek is what makes the
    single-take property structural — a sink cannot be wired to anything else.
    """
    tree = _module_tree(_BEHAVIOR_MODULE)
    seam = _function(tree, "_compose_run_seam")
    registrations = _calls(seam, "add_sink")
    assert (
        len(registrations) == 1
    ), f"expected exactly one audio sink registration, got {len(registrations)}"
    call = registrations[0]
    assert isinstance(call.func, ast.Attribute) and isinstance(call.func.value, ast.Name)
    tap_name = call.func.value.id
    (arg,) = call.args
    assert (
        isinstance(arg, ast.Attribute) and arg.attr == "offer"
    ), "the registered sink is not the tee's offer()"

    reader = _function(tree, "sense_reader")
    pulls = _calls(reader, "pull")
    tapped = {
        pull.func.value.id
        for pull in pulls
        if isinstance(pull.func, ast.Attribute) and isinstance(pull.func.value, ast.Name)
    }
    assert tap_name in tapped, (
        f"the tee is a sink on {tap_name} but the tick pulls {sorted(tapped)} — "
        "the tee must be fed by the SAME latch the tick swapped"
    )
    assert not _calls(reader, "audio"), (
        "the sense reader reads audio directly again — every consumer is fed by "
        "the one pull, so a read here is a second view of a consuming swap"
    )


# --------------------------------------------------------------------------- #
# 2. Nothing attached: no work, and no change to the runtime                  #
# --------------------------------------------------------------------------- #


def test_with_no_consumer_the_tee_queues_nothing(_isolated, monkeypatch):
    """Criterion 3's cheap half: an unattached tee costs the tick a flag store."""
    chunks = [np.ones(8, dtype=np.float32) for _ in range(4)]
    monkeypatch.setattr(behavior_mod, "AudioPump", lambda media: _CountingPump(chunks))

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    sense_reader, _metrics, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None
    )
    try:
        for tick in range(len(chunks)):
            sense_reader(float(tick) * 0.02)
        tee = resources.tee
        assert tee.offers == len(chunks)
        assert tee.queued == 0, "the tee buffered audio with nobody listening"
        assert tee.dropped == 0
    finally:
        resources.close()


class _StepClock:
    """A deterministic monotonic clock advancing ``dt`` seconds per call."""

    def __init__(self, dt: float = 0.02) -> None:
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


def _run_feed(monkeypatch, chunks) -> list[dict]:
    """Drive the REAL engine over the composed seam; return its raw event feed.

    An injected step clock rather than a wall clock, deliberately: several
    runtime fields (``self_moving`` chief among them) change at a clock-derived
    moment, so two real-time runs legitimately publish their snapshots on
    different ticks. That is the runtime being timing-dependent, not the tee
    changing it — and comparing two wall-clock runs would be exactly the flaky
    threshold this repo refuses to write.
    """
    from reachy.behavior import control
    from reachy.behavior import engine as engine_mod

    monkeypatch.setattr(behavior_mod, "AudioPump", lambda media: _CountingPump(list(chunks)))
    events: list[dict] = []
    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        transport, config, None, events.append
    )
    try:
        engine_mod.run(
            transport,
            config,
            sleep=lambda *_: None,
            now=_StepClock(dt=0.02),
            max_ticks=12,
            control=control.CommandSpool(),
            sense=sense_reader,
            tick_seam=tick_seam,
        )
    finally:
        resources.close()
    return events


def test_an_unattached_tee_leaves_the_runtime_feed_unchanged(_isolated, monkeypatch):
    """Criterion 3: with the layer absent the runtime behaves as it did before.

    The same engine run, once with the tee composed (nothing attached) and once
    with it switched off entirely, produces the same feed — the tee is an
    ADDITIVE export leg, never a change to what the runtime decides.
    """
    chunks = [np.full(8, 0.1, dtype=np.float32) for _ in range(12)]

    monkeypatch.setenv(tee_mod.ENABLED_ENV, "0")
    without = _run_feed(monkeypatch, chunks)

    monkeypatch.delenv(tee_mod.ENABLED_ENV, raising=False)
    with_tee = _run_feed(monkeypatch, chunks)

    assert with_tee == without, "composing the audio tee changed the runtime feed"
    assert without, "the comparison is blind — the engine published nothing at all"


# --------------------------------------------------------------------------- #
# 3. The socket: under the state dir, gone at shutdown                        #
# --------------------------------------------------------------------------- #


def test_the_socket_lives_under_the_state_dir_and_is_removed_on_shutdown(monkeypatch, tmp_path):
    """Where a consumer looks for it, and nothing left behind afterwards."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(tee_mod.SOCKET_ENV, raising=False)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: _FakeMedia())

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    _sense_reader, _metrics, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), config, None, None
    )
    expected = tmp_path / tee_mod.DEFAULT_SOCKET_NAME
    try:
        assert resources.tee.path == expected
        assert expected.exists(), "the runtime bound no socket under the state dir"
    finally:
        resources.close()
    assert not expected.exists(), "the tee socket outlived the runtime"


def test_the_cli_run_composes_a_live_tee_and_cleans_up_after_itself(monkeypatch, tmp_path):
    """The whole CLI path, not just the seam: ``cmd_engine_run`` opens and releases it.

    ``offers`` only counts while the tee is ACTIVE, so a non-zero count after the
    run is proof the socket was live and the tick was feeding it — and the
    missing file afterwards is proof ``cmd_engine_run``'s teardown reached it.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv(tee_mod.SOCKET_ENV, raising=False)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: _FakeMedia())
    monkeypatch.setattr(behavior_mod, "get_transport", lambda args: _QuietTransport())
    monkeypatch.setattr(
        behavior_mod,
        "AudioPump",
        lambda media: _CountingPump([np.ones(8, dtype=np.float32) for _ in range(8)]),
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)

    built: list = []
    real_factory = behavior_mod._make_audio_tee

    def _spy(provider):
        tee = real_factory(provider)
        built.append(tee)
        return tee

    monkeypatch.setattr(behavior_mod, "_make_audio_tee", _spy)

    assert main(["behavior", "engine", "run", "--max-ticks", "6", "--json"]) == 0

    (tee,) = built
    assert tee.path == tmp_path / tee_mod.DEFAULT_SOCKET_NAME
    assert tee.offers > 0, "the tee was composed but the tick never fed it"
    assert not tee.active, "the tee outlived the run"
    assert not tee.path.exists(), "the tee socket outlived the run"


def test_the_tee_is_released_even_when_composition_fails(_isolated, monkeypatch):
    """A raise part-way through composition must not strand the tee's thread.

    Same discipline as the two held SDK clients: ``_compose_run_seam`` releases
    everything it opened before re-raising, or a failed start leaves a live
    socket nobody owns and a thread nobody joins.
    """

    class _Boom(RuntimeError):
        pass

    def _explode(*_a, **_k):
        raise _Boom("composition exploded after the tee was opened")

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", _explode)
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    with pytest.raises(_Boom):
        behavior_mod._compose_run_seam(_QuietTransport(), config, None, None)
    assert not (_isolated / "audio_tee.sock").exists(), "a failed composition stranded the socket"
