"""Tests for reachy.embody.media — injectable audio source/sink, two profiles.

Task t6 of the ``embodiment-layer`` plan (docs/plans/2026-08-01-embodiment-layer.md).
Written test-first against the task's own acceptance contract:

  AC1 — profile selection is config/env only, and both profiles run through the
        SAME wrapper classes (``EmbodySource``/``EmbodySink``) against fakes —
        no ``isinstance`` fork anywhere in the implementation (machine-checked
        via an AST scan, not just a behavioural probe).
  AC2 — the robot sink calls ``play_audio`` with ``transport="http"`` EXPLICITLY,
        on every call, regardless of ``REACHY_TRANSPORT`` — so the sdk fallback
        (which would open a second ``ReachyMini``) is unreachable, and
        ``reachy_mini`` never enters ``sys.modules``.
  AC3 — no new base dependency: ``pyproject.toml``'s base deps are pinned by
        equality, and the bench profile's ``sounddevice`` binding is a lazy
        import that degrades gracefully (one warning, then permanently quiet)
        when the package is absent — which it is on a bare install and in CI.

All tests use fakes/real local (unix-domain / loopback-refused) sockets; no
real robot, daemon, or audio hardware is needed — see the module's own
docstring for what is therefore UNVERIFIED without live hardware (bench mic
capture against a real device, and OS-level AEC via a real
``pactl load-module module-echo-cancel``).
"""

from __future__ import annotations

import ast
import logging
import socket
import subprocess
import sys
import threading
import time
import tomllib
from pathlib import Path

import numpy as np
import pytest

from reachy.cli._errors import CliError
from reachy.embody import media

_REPO_ROOT = Path(__file__).resolve().parent.parent
_MEDIA_PATH = _REPO_ROOT / "reachy" / "embody" / "media.py"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pcm16_bytes(values: list[int]) -> bytes:
    return np.array(values, dtype="<i2").tobytes()


def _tee_stream(samples: list[float], *, samplerate: int | None = 16000) -> bytes:
    """One tee wire image: the writer's own header line, then float32 samples.

    Built with :func:`reachy.behavior.audio_tee.header_bytes` rather than a
    hand-written JSON literal — a test that re-derives the wire is exactly how
    the reader and the writer drifted apart in the first place. The pipe itself
    is exercised end to end in ``tests/test_embody_tee_integration.py``; these
    tests only need a byte image of it.
    """
    from reachy.behavior.audio_tee import SAMPLE_DTYPE, header_bytes

    return header_bytes(samplerate) + np.array(samples, dtype=SAMPLE_DTYPE).tobytes()


def _sense_lines(caplog, *, logger_name: str = "reachy.sense") -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == logger_name]


def _serve_unix_socket_once(
    path: Path, payload: bytes, *, keep_open_s: float = 1.0
) -> threading.Thread:
    """Bind+listen on a unix socket, accept one connection, send *payload*.

    A real local socket pair (no fakes needed): the OS gives us genuine
    ``SOCK_STREAM`` semantics — no message boundaries, exactly the wire
    :class:`reachy.embody.media._RobotTeeSourceBackend` is written against.
    The connection is kept open for *keep_open_s* after sending so a caller can
    also exercise the "connected but nothing new yet" (timeout, not a drop)
    path before the server thread exits and closes it.
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


class _FakeInputStream:
    """Stand-in for ``sounddevice.InputStream`` — a fixed data buffer, read in slices."""

    def __init__(
        self,
        *,
        device,
        channels,
        samplerate,
        dtype,
        blocksize,
        data: np.ndarray | None = None,
        raise_on_open: bool = False,
    ) -> None:
        if raise_on_open:
            raise RuntimeError("no such input device")
        self.device = device
        self.channels = channels
        self.samplerate = samplerate
        self.blocksize = blocksize
        self.started = False
        self.closed = False
        self._data = data if data is not None else np.zeros((blocksize, channels), dtype=np.float32)

    def start(self) -> None:
        self.started = True

    def read(self, frames: int):
        return self._data[:frames], False

    def stop(self) -> None:
        pass

    def close(self) -> None:
        self.closed = True


def _make_fake_sounddevice(
    *,
    input_stream_cls=_FakeInputStream,
    input_data: np.ndarray | None = None,
    raise_on_play: bool = False,
):
    """A minimal stand-in ``sounddevice`` module object.

    Only the surface :mod:`reachy.embody.media` actually calls: ``InputStream``
    (a class) and ``play`` (a function). Injected via
    ``monkeypatch.setattr(media, "_import_sounddevice", lambda: fake)``.
    """
    play_calls: list[dict] = []

    class _Bound(input_stream_cls):
        def __init__(self, **kwargs):
            super().__init__(data=input_data, **kwargs)

    def _play(data, samplerate=None, device=None):
        if raise_on_play:
            raise RuntimeError("no such output device")
        play_calls.append({"data": data, "samplerate": samplerate, "device": device})

    fake = type("_FakeSoundDevice", (), {})()
    fake.InputStream = _Bound
    fake.play = _play
    fake.play_calls = play_calls
    return fake


# ---------------------------------------------------------------------------
# Static/AST boundary checks — the acceptance contract, machine-checked
# ---------------------------------------------------------------------------


def _parse_media_module() -> ast.Module:
    return ast.parse(_MEDIA_PATH.read_text(encoding="utf-8"), filename=str(_MEDIA_PATH))


def _imported_module_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
    return names


def test_media_module_never_imports_reachy_mini_anywhere():
    """h14's letter, pinned locally: no ``import reachy_mini`` in this file at all.

    Not merely "not at module scope" — an AST walk covers a lazy import buried
    inside any function body too, unlike a runtime ``sys.modules`` probe.
    """
    imported = _imported_module_names(_parse_media_module())
    offenders = {n for n in imported if n == "reachy_mini" or n.startswith("reachy_mini.")}
    assert not offenders, f"reachy_mini imported: {offenders}"


def test_media_module_never_calls_isinstance():
    """AC1's letter: the two profiles run through the SAME wrapper classes —
    there is nothing here for an ``isinstance`` fork to dispatch on, and this
    pins that structurally rather than trusting a passing behavioural test."""
    tree = _parse_media_module()
    called_names = {
        node.func.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
    }
    assert "isinstance" not in called_names


def test_media_module_never_shells_out():
    """No subprocess, no os.system/popen — device I/O is socket or sounddevice only."""
    tree = _parse_media_module()
    imported = _imported_module_names(tree)
    assert "subprocess" not in imported
    shell_attrs = {"system", "popen", "spawnl", "spawnv", "spawnve"}
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            assert node.func.attr not in shell_attrs, f"unexpected shell-out: {node.func.attr}"


def test_pyproject_base_dependencies_are_unchanged():
    """AC3: this task may not add a base runtime dependency, machine-checked.

    Pinned by equality (the same style ``test_zero_llm_boundary.py`` uses for
    its own hard sets) so drift in either direction fails loudly.
    """
    data = tomllib.loads((_REPO_ROOT / "pyproject.toml").read_text())
    assert data["project"]["dependencies"] == [
        "numpy>=1.24",
        "harmonics-cli>=0.8",
        "events-cli>=0.9",
    ]
    optional = data["project"]["optional-dependencies"]
    assert set(optional.keys()) == {"daemon", "sdk", "cpu", "gpu", "vision"}
    assert "sounddevice" not in repr(
        optional
    ), "bench capture must stay a lazy import, not an extra pin"


# ---------------------------------------------------------------------------
# resolve_profile — config/env only
# ---------------------------------------------------------------------------


def test_resolve_profile_defaults_to_robot_with_no_config():
    assert media.resolve_profile() == media.PROFILE_ROBOT


def test_resolve_profile_reads_env_when_arg_is_absent(monkeypatch):
    monkeypatch.setenv(media.ENV_PROFILE, "bench")
    assert media.resolve_profile() == media.PROFILE_BENCH


def test_resolve_profile_explicit_arg_wins_over_env(monkeypatch):
    monkeypatch.setenv(media.ENV_PROFILE, "bench")
    assert media.resolve_profile("robot") == media.PROFILE_ROBOT


def test_resolve_profile_rejects_an_unknown_value():
    with pytest.raises(CliError):
        media.resolve_profile("space-station")


# ---------------------------------------------------------------------------
# AC1 — both profiles run the SAME wrapper classes against fakes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("profile", [media.PROFILE_ROBOT, media.PROFILE_BENCH])
def test_build_media_returns_the_same_wrapper_types_for_both_profiles(
    profile, tmp_path, monkeypatch
):
    """Parametrized over BOTH profiles: identical calling code drives either one.

    Only the fixture setup below differs per profile (a real unix socket vs. a
    fake sounddevice) — the code under test (``EmbodySource``/``EmbodySink``,
    literally the same classes either way) does not branch at all.
    """
    if profile == media.PROFILE_ROBOT:
        sock_path = tmp_path / "tee.sock"
        _serve_unix_socket_once(sock_path, _tee_stream([0.1, -0.2, 0.3]), keep_open_s=0.3)
        play_calls: list[dict] = []
        monkeypatch.setattr(media, "play_audio", lambda pcm, **kw: play_calls.append(kw))
        built = media.build_media(
            profile=profile,
            tee_socket=sock_path,
            robot_sample_rate=16000,
            target_sample_rate=16000,
            base_url="http://127.0.0.1:1",
        )
    else:
        fake_sd = _make_fake_sounddevice(input_data=np.zeros((4096, 1), dtype=np.float32))
        monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)
        built = media.build_media(
            profile=profile, target_sample_rate=16000, bench_sample_rate=16000
        )

    assert type(built.source) is media.EmbodySource
    assert type(built.sink) is media.EmbodySink
    assert built.profile == profile

    # The SAME two calls, regardless of which profile built these objects.
    result = built.source.read()
    assert result is None or isinstance(result, np.ndarray)
    built.sink.play(_pcm16_bytes([1, 2, 3, 4]), samplerate=16000)

    built.close()


# ---------------------------------------------------------------------------
# Robot source — reads the tee unix socket
# ---------------------------------------------------------------------------


def test_robot_source_reconstructs_contiguous_samples_across_short_reads(tmp_path):
    """Chunks arrive contiguous, mono, in order — even split mid-sample by the OS.

    ``recv_bytes`` is set deliberately small (and not a multiple of the wire's
    4-byte sample) so neither the header nor the 20-byte payload can arrive in
    one ``recv()`` call, exercising the pending-byte buffer that survives a read
    landing mid-sample.
    """
    sock_path = tmp_path / "tee.sock"
    original = [0.1, -0.2, 0.3, -0.4, 0.5]
    _serve_unix_socket_once(sock_path, _tee_stream(original), keep_open_s=0.3)

    backend = media._RobotTeeSourceBackend(
        sock_path,
        native_sample_rate=16000,
        recv_bytes=5,
        connect_timeout=1.0,
        read_timeout=0.05,
    )
    try:
        collected: list[np.ndarray] = []
        deadline = time.monotonic() + 2.0
        while sum(int(c.size) for c in collected) < len(original) and time.monotonic() < deadline:
            result = backend.read_native()
            if result is not None:
                samples, rate = result
                assert rate == 16000
                collected.append(samples)
        got = np.concatenate(collected)
    finally:
        backend.close()

    assert got.size == len(original), f"expected {len(original)} samples, got {got.size}"
    assert np.array_equal(got, np.array(original, dtype=np.float32))


def test_robot_source_degrades_when_the_tee_socket_is_absent(tmp_path, caplog):
    """A missing tee (t4 not started, or absent entirely) is ORDINARY, not fatal."""
    backend = media._RobotTeeSourceBackend(tmp_path / "nobody-home.sock", native_sample_rate=16000)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert backend.read_native() is None
        assert backend.read_native() is None  # still within backoff — no new attempt

    drop_lines = [line for line in _sense_lines(caplog) if "tee-unavailable" in line]
    assert len(drop_lines) == 1, f"expected exactly one latched drop, got {drop_lines}"
    assert drop_lines[0].startswith("[SENSE stage=embody source=media-robot-source")


def test_embody_source_resamples_robot_reads_when_target_differs(tmp_path):
    """Even the robot profile goes through the shared resample step (a no-op by
    default only because native == target; this proves it is not special-cased
    out of the code path)."""
    sock_path = tmp_path / "tee.sock"
    _serve_unix_socket_once(sock_path, _tee_stream([0.03] * 48, samplerate=48000), keep_open_s=0.3)
    backend = media._RobotTeeSourceBackend(sock_path, native_sample_rate=48000, connect_timeout=1.0)
    source = media.EmbodySource(backend, target_sample_rate=16000)
    try:
        deadline = time.monotonic() + 2.0
        collected: list[np.ndarray] = []
        while sum(int(c.size) for c in collected) < 10 and time.monotonic() < deadline:
            chunk = source.read()
            if chunk is not None and chunk.size:
                collected.append(chunk)
        got = np.concatenate(collected)
    finally:
        source.close()
    # 48 samples at 48000 Hz resampled to 16000 Hz -> ~16 samples.
    assert 12 <= got.size <= 20, f"unexpected resampled length: {got.size}"


# ---------------------------------------------------------------------------
# Robot sink — AC2: transport="http" is explicit and unshakeable
# ---------------------------------------------------------------------------


def test_robot_sink_always_passes_transport_http_explicitly(monkeypatch):
    calls: list[dict] = []
    monkeypatch.setattr(media, "play_audio", lambda pcm, **kw: calls.append(kw))
    backend = media._RobotHttpSinkBackend(base_url="http://127.0.0.1:1")

    backend.play(_pcm16_bytes([1, 2, 3]), samplerate=16000)

    assert len(calls) == 1
    assert calls[0]["transport"] == "http"


def test_robot_sink_ignores_reachy_transport_env_entirely(monkeypatch):
    """The sdk fallback is unreachable: REACHY_TRANSPORT can never steer this sink."""
    calls: list[dict] = []
    monkeypatch.setattr(media, "play_audio", lambda pcm, **kw: calls.append(kw))
    monkeypatch.setenv("REACHY_TRANSPORT", "sdk")
    backend = media._RobotHttpSinkBackend(base_url="http://127.0.0.1:1")

    backend.play(_pcm16_bytes([1, 2, 3]), samplerate=16000)

    assert calls[0]["transport"] == "http"


def test_robot_sink_never_imports_reachy_mini_even_on_a_real_playback_failure():
    """The strongest form of AC2: drive the REAL play_audio (unpatched) against
    an unreachable daemon and confirm reachy_mini never enters sys.modules —
    the sdk leg's lazy import is provably never reached.

    Probed in a SUBPROCESS, following the precedent
    ``test_importing_the_holder_does_not_pull_in_reachy_mini`` set in
    tests/test_robot_media_client.py. ``sys.modules`` is interpreter-global and
    several sibling modules legitimately stub ``reachy_mini`` into it while they
    run; an in-process assertion therefore tests the whole worker's history
    rather than this backend, and fails intermittently under ``pytest -n auto``
    depending on which tests xdist happened to schedule ahead of it here. (This
    is the repo's own recorded lesson from the sleep-mode arc: probe the import
    boundary in a subprocess, never in the shared interpreter.) A fresh
    interpreter makes the claim both order-independent and strictly stronger —
    nothing but this code path has run in it.
    """
    code = (
        "import sys\n"
        "from reachy.embody import media\n"
        "backend = media._RobotHttpSinkBackend(base_url='http://127.0.0.1:1', timeout=0.2)\n"
        "backend.play(b'\\x01\\x00\\x02\\x00', samplerate=16000)\n"
        "print('reachy_mini' in sys.modules)\n"
    )
    proc = subprocess.run(
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        cwd=str(_REPO_ROOT),
        timeout=60,
        check=True,
    )
    # `check=True` already proves `play` did not raise against a dead endpoint.
    assert proc.stdout.strip() == "False"


def test_robot_sink_degrades_a_playback_failure_to_a_named_drop(caplog):
    backend = media._RobotHttpSinkBackend(base_url="http://127.0.0.1:1", timeout=0.2)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        backend.play(_pcm16_bytes([1, 2, 3, 4]), samplerate=16000)

    drop_lines = [line for line in _sense_lines(caplog) if "playback-failed" in line]
    assert len(drop_lines) == 1
    assert drop_lines[0].startswith("[SENSE stage=embody source=media-robot-sink")


def test_embody_sink_skips_an_empty_payload_without_touching_the_backend():
    calls = []

    class _Backend:
        def play(self, pcm, *, samplerate):
            calls.append((pcm, samplerate))

        def close(self):
            pass

    sink = media.EmbodySink(_Backend())
    sink.play(b"", samplerate=16000)

    assert calls == []


# ---------------------------------------------------------------------------
# Bench source — dev-box mic via lazily-imported sounddevice
# ---------------------------------------------------------------------------


def test_bench_source_degrades_when_sounddevice_is_absent(monkeypatch, caplog):
    monkeypatch.setattr(media, "_import_sounddevice", lambda: None)
    backend = media._BenchMicSourceBackend(device=None, samplerate=48000)

    with caplog.at_level(logging.WARNING, logger="reachy.embody.media"):
        assert backend.read_native() is None
        assert backend.read_native() is None  # latched: no second warning

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1, f"expected exactly one process-wide warning, got {len(warnings)}"


def test_bench_source_degrades_when_the_device_open_fails(monkeypatch):
    fake_sd = _make_fake_sounddevice(input_stream_cls=_FakeInputStream)

    def _raising_input_stream(**kwargs):
        raise RuntimeError("device busy")

    fake_sd.InputStream = _raising_input_stream
    monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)
    backend = media._BenchMicSourceBackend(device=None, samplerate=48000)

    assert backend.read_native() is None


def test_bench_source_resamples_to_the_target_rate(monkeypatch):
    """AC/r7: a 48 kHz webcam mic normalises to the configured target rate."""
    blocksize = 480  # 10 ms @ 48 kHz
    data = np.linspace(-0.5, 0.5, blocksize, dtype=np.float32).reshape(blocksize, 1)
    fake_sd = _make_fake_sounddevice(input_data=data)
    monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)

    backend = media._BenchMicSourceBackend(device=None, samplerate=48000, blocksize=blocksize)
    source = media.EmbodySource(backend, target_sample_rate=16000)

    chunk = source.read()

    assert chunk is not None
    # 480 samples @ 48000 Hz -> 160 samples @ 16000 Hz.
    assert chunk.shape[0] == 160
    assert chunk.dtype == np.float32
    assert np.all(np.abs(chunk) <= 1.0)


def test_bench_source_uses_the_configured_input_device(monkeypatch):
    seen_devices = []

    class _RecordingInputStream(_FakeInputStream):
        def __init__(self, **kwargs):
            seen_devices.append(kwargs.get("device"))
            super().__init__(**kwargs)

    fake_sd = _make_fake_sounddevice(input_stream_cls=_RecordingInputStream)
    monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)

    backend = media._BenchMicSourceBackend(device="embody_echo_cancel_source", samplerate=48000)
    backend.read_native()

    assert seen_devices == ["embody_echo_cancel_source"]


# ---------------------------------------------------------------------------
# Bench sink — monitor speakers via lazily-imported sounddevice
# ---------------------------------------------------------------------------


def test_bench_sink_degrades_when_sounddevice_is_absent(monkeypatch, caplog):
    monkeypatch.setattr(media, "_import_sounddevice", lambda: None)
    backend = media._BenchSpeakerSinkBackend(device=None)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        backend.play(_pcm16_bytes([1, 2, 3]), samplerate=16000)

    drop_lines = [line for line in _sense_lines(caplog) if media.BENCH_AUDIO_EXTRA_ABSENT in line]
    assert len(drop_lines) == 1


def test_bench_sink_forwards_pcm_and_device_to_sounddevice_play(monkeypatch):
    fake_sd = _make_fake_sounddevice()
    monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)
    backend = media._BenchSpeakerSinkBackend(device="embody_echo_cancel_sink")

    backend.play(_pcm16_bytes([10, -10, 20]), samplerate=16000)

    assert len(fake_sd.play_calls) == 1
    call = fake_sd.play_calls[0]
    assert call["samplerate"] == 16000
    assert call["device"] == "embody_echo_cancel_sink"
    assert np.array_equal(call["data"], np.array([10, -10, 20], dtype="<i2"))


def test_bench_sink_degrades_a_playback_failure_to_a_named_drop(monkeypatch, caplog):
    fake_sd = _make_fake_sounddevice(raise_on_play=True)
    monkeypatch.setattr(media, "_import_sounddevice", lambda: fake_sd)
    backend = media._BenchSpeakerSinkBackend(device=None)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        backend.play(_pcm16_bytes([1, 2, 3]), samplerate=16000)

    drop_lines = [line for line in _sense_lines(caplog) if "playback-failed" in line]
    assert len(drop_lines) == 1
    assert drop_lines[0].startswith("[SENSE stage=embody source=media-bench-sink")


# ---------------------------------------------------------------------------
# EmbodyMedia lifecycle
# ---------------------------------------------------------------------------


def test_embody_media_close_is_idempotent(tmp_path):
    built = media.build_media(
        profile="robot",
        tee_socket=tmp_path / "nope.sock",
        base_url="http://127.0.0.1:1",
    )
    built.close()
    built.close()  # must not raise


def test_embody_media_context_manager_closes_on_exit(tmp_path):
    with media.build_media(
        profile="robot", tee_socket=tmp_path / "nope.sock", base_url="http://127.0.0.1:1"
    ) as built:
        assert built.source is not None
    # A second explicit close (post-context) must still be safe.
    built.close()


# ---------------------------------------------------------------------------
# _resample_mono unit tests
# ---------------------------------------------------------------------------


def test_resample_mono_is_a_noop_when_rates_match():
    samples = np.array([0.1, -0.2, 0.3], dtype=np.float32)
    out = media._resample_mono(samples, 16000, 16000)
    assert out is samples


def test_resample_mono_scales_length_proportionally():
    samples = np.linspace(-1.0, 1.0, 480, dtype=np.float32)
    out = media._resample_mono(samples, 48000, 16000)
    assert out.shape[0] == 160
    assert out.dtype == np.float32


def test_resample_mono_is_a_noop_on_empty_input():
    out = media._resample_mono(np.empty(0, dtype=np.float32), 48000, 16000)
    assert out.size == 0
