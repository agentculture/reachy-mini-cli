"""Injectable audio source/sink for the embodiment layer — two profiles, one code path.

The layer needs audio in (ears) and audio out (mouth), and per the package's own
import boundary (``reachy/embody/__init__.py``) it may never construct a
``ReachyMini`` or open an SDK media session — that belongs to the runtime alone
(the single-SDK-owner model, ``CLAUDE.md``). So this module never touches
``reachy_mini``: it reads audio the runtime already captured, and it writes
audio through the daemon's HTTP route, or — on a bare dev box with no robot at
all — through ordinary dev-box devices.

Two profiles, selected by CONFIG/ENV ONLY (:func:`resolve_profile`,
:data:`ENV_PROFILE`), never by a code fork:

* **robot** — source: the runtime's audio TEE, a raw mono PCM16 stream over a
  unix ``SOCK_STREAM`` socket under ``state_dir()`` (the writer side is task
  t4's ``reachy/behavior/audio_tee.py``, built in the same wave; this module
  owns only the READER and depends on the documented wire, never on that
  module, so the two land independently). Sink: the daemon's HTTP media route
  via :func:`reachy.speech.playback.play_audio` with ``transport="http"``
  passed EXPLICITLY on every call — see "Why transport is hard-coded" below.
* **bench** — source: the dev-box microphone (a USB webcam mic, typically
  44.1/48 kHz). Sink: the monitor speakers. Acoustic echo cancellation runs on
  the OS audio server, not in this process — see "Bench AEC" below.

Both profiles are driven through exactly two classes, :class:`EmbodySource` and
:class:`EmbodySink` — literally the SAME classes regardless of profile, never a
subclass per profile and never an ``isinstance`` check anywhere in this module.
A profile only decides which small *backend* object gets injected (the
``read_native()``/``play()`` duck-typed contract below); the resample-to-a-
common-rate step, the PCM16 framing, and the failure handling live once, in the
wrapper, so the two profiles are provably the same code under test (see
``tests/test_embody_media.py``).

Backend contract (duck-typed, never checked with ``isinstance``)::

    class _SourceBackend:
        def read_native(self) -> tuple[np.ndarray, int] | None: ...  # (mono float32, its rate)
        def close(self) -> None: ...

    class _SinkBackend:
        def play(self, pcm16_bytes: bytes, *, samplerate: int) -> None: ...
        def close(self) -> None: ...

Why transport is hard-coded on the robot sink
----------------------------------------------
:func:`reachy.speech.playback.play_audio` defaults ``transport`` to ``"sdk"``
when neither the parameter nor ``REACHY_TRANSPORT`` says otherwise — the right
default for ``say``/the runtime's own voice, and the WRONG one here, because
the sdk path opens a second ``ReachyMini`` (``_open_sdk_media`` inside
``playback.py``), exactly the forbidden move this package's import boundary
exists to prevent. :class:`_RobotHttpSinkBackend` therefore names
``transport="http"`` as a literal keyword on every call — an operator's
``REACHY_TRANSPORT=sdk`` can never steer this module back onto the sdk path,
and ``reachy_mini`` never enters ``sys.modules`` because the sdk leg's lazy
import is simply never reached. ``tests/test_embody_media.py`` asserts both
halves of this.

Rate normalisation — resolved here, once (plan risk r7)
---------------------------------------------------------
:class:`EmbodySource` resamples every read to ONE configured
``target_sample_rate`` (:data:`DEFAULT_TARGET_SAMPLE_RATE`, 16000 Hz) using the
same linear-interpolation approach
:mod:`reachy.speech.playback`'s ``_resample_mono`` already uses for the sdk
speaker path — cited rather than imported, because that helper is that
module's own private implementation detail, and importing a leading-underscore
name across a package boundary would tie this module to playback.py's
internals rather than to its public ``play_audio`` contract. 16000 Hz is not a
guess: it is the runtime's own measured mic rate
(``docs/operating-reachy.md``'s "mic-rate line at boot" —
``[SENSE stage=warmup source=realtime event=setup] mic rate 16000 Hz`` on the
deployed box), so the robot profile's resample is a no-op by default and the
bench profile (a USB webcam at 44.1/48 kHz) is the one that actually converts.
Override via :data:`ENV_TARGET_SAMPLE_RATE` if a deployment negotiates a
different rate. The realtime wire itself does not *require* a client to
normalise — ``input_sample_rate`` rides the connect URL and the server
resamples from whatever it is told (``reachy/speech/realtime_wire.py``) — but
normalising HERE means every later consumer (the duplex client, task t9; the
composition root, task t11) reads one rate regardless of profile, instead of
special-casing bench vs robot.

Bench AEC — an OS-level module, not a Python DSP dependency
--------------------------------------------------------------
The design note this task owns says it plainly: prefer an OS-level echo-cancel
route over a Python DSP dependency. This box runs PipeWire with the
PulseAudio-compatible ``pactl`` shim (confirmed present:
``pactl``/``pw-cli``/``arecord``/``aplay`` all on PATH). The operator loads the
echo-cancel module ONCE, outside this process::

    pactl load-module module-echo-cancel aec_method=webrtc \\
        source_name=embody_echo_cancel_source sink_name=embody_echo_cancel_sink

That command creates a virtual sink/source PAIR: audio played to
``embody_echo_cancel_sink`` is both forwarded to the real speaker AND used as
the reference signal that gets subtracted from ``embody_echo_cancel_source``'s
captured mic input. **AEC therefore requires BOTH ends of this module to point
at that pair** — capture at the ``*_source`` name, playback at the ``*_sink``
name (:data:`ENV_BENCH_INPUT_DEVICE` / :data:`ENV_BENCH_OUTPUT_DEVICE`) — using
only one half gets you a mic with no cancellation and a speaker with no cross
effect. Three reasons this beats a Python AEC library: (1) it needs no new
dependency — the setup command is one ``pactl`` call, and this module still
only *lazily* imports the device-I/O binding (see below), never an AEC
algorithm; (2) it already has the reference signal wired by construction (the
sink IS the reference for the paired source), where a Python-side AEC would
need this module to feed back its own playback into a canceller by hand; (3)
it is the same class of fix the runtime itself relies on for its own hearing
(``reachy/robot/audio_shape.py``'s ``AEC_CHANNEL`` selects an already
hardware-AEC'd channel rather than cancelling in Python). Loading the module is
an operator/deployment step, not something this file does — a module missing
from `pactl load-module module-list` degrades to "the bench mic has no AEC",
never a crash.

Bench capture/playback binding — lazy import, no new base dependency
-----------------------------------------------------------------------
Device I/O needs SOME Python binding to PortAudio/ALSA; this module picks
``sounddevice`` (the same library ``harmonics-cli``'s OWN optional ``[audio]``
extra already depends on — see ``pyproject.toml``'s base-dependency comment —
so it is not a new *kind* of dependency for this dependency tree, just not yet
requested here). It is **lazily imported** inside
:func:`_import_sounddevice`, exactly like ``reachy/vision/face.py``'s
``_import_cv2`` / ``reachy/behavior/face_sense.py``'s ``[vision]`` degradation:
absent, it logs exactly ONE process-wide warning
(:data:`BENCH_AUDIO_EXTRA_ABSENT`) and the bench profile's source/sink degrade
to permanently quiet (reads return ``None``, plays are a named
:func:`reachy.senselog.drop`) rather than raising. **This file does not, and
must not, edit ``pyproject.toml``** — a future extra (a `[bench]` extra, or
widening ``harmonics-cli[audio]``'s own reach) is the natural home for a hard
``sounddevice`` pin; until one exists, an operator who wants the bench profile
installs it by hand (``pip install sounddevice``) same as any other optional
engine on a bare install.

Import boundary
----------------
No ``reachy_mini`` import anywhere in this file (``tests/test_embody_media.py``
pins this with an AST scan, not merely a run-time probe — a lazy import buried
in a branch nobody exercises would not be caught by import-time inspection
alone). The only reachy imports are :func:`reachy.behavior.control.behavior_dir`
(the tee socket's default location — never ``reachy.daemon`` itself, which owns
the daemon's ``start``/``stop``), :func:`reachy.speech.playback.play_audio` (the
sanctioned daemon-http sink, always called with ``transport="http"``),
:mod:`reachy.senselog` (named drops) and :mod:`reachy.cli._errors` (the shared
error contract for a genuinely bad profile string). No ``subprocess``, no
``os.system``, no shell of any kind — device I/O goes through ``socket`` (the
tee) or the lazily-imported ``sounddevice`` (the bench devices) only.
"""

from __future__ import annotations

import logging
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.senselog import drop as senselog_drop
from reachy.senselog import stage as senselog_stage
from reachy.speech.playback import DEFAULT_BASE_URL, play_audio

logger = logging.getLogger(__name__)

_STAGE = "embody"
_SOURCE_ROBOT_SRC = "media-robot-source"
_SOURCE_ROBOT_SINK = "media-robot-sink"
_SOURCE_BENCH_SRC = "media-bench-source"
_SOURCE_BENCH_SINK = "media-bench-sink"

# ---------------------------------------------------------------------------
# Profile selection
# ---------------------------------------------------------------------------

PROFILE_ROBOT = "robot"
PROFILE_BENCH = "bench"
_PROFILES = (PROFILE_ROBOT, PROFILE_BENCH)
DEFAULT_PROFILE = PROFILE_ROBOT

ENV_PROFILE = "REACHY_EMBODY_MEDIA_PROFILE"
ENV_TARGET_SAMPLE_RATE = "REACHY_EMBODY_TARGET_SAMPLE_RATE"
ENV_TEE_SOCKET = "REACHY_EMBODY_TEE_SOCKET"
ENV_ROBOT_SAMPLE_RATE = "REACHY_EMBODY_ROBOT_SAMPLE_RATE"
ENV_BENCH_INPUT_DEVICE = "REACHY_EMBODY_BENCH_INPUT_DEVICE"
ENV_BENCH_OUTPUT_DEVICE = "REACHY_EMBODY_BENCH_OUTPUT_DEVICE"
ENV_BENCH_SAMPLE_RATE = "REACHY_EMBODY_BENCH_SAMPLE_RATE"
#: Base URL for the robot sink's daemon-http route. Reuses the site-wide
#: ``REACHY_BASE_URL`` convention (``reachy/behavior/speech_act.py``,
#: ``reachy/robot/transport.py``) rather than inventing an embody-specific
#: variable for the same daemon.
ENV_BASE_URL = "REACHY_BASE_URL"

#: The runtime's measured mic rate (see the module docstring) — also the
#: default normalisation target, so the robot profile resamples nothing.
DEFAULT_ROBOT_SAMPLE_RATE = 16000
DEFAULT_TARGET_SAMPLE_RATE = 16000
#: A typical USB webcam mic's native rate, used only when the bench device
#: itself does not report one and no override is configured.
DEFAULT_BENCH_SAMPLE_RATE_FALLBACK = 48000
#: ~21 ms at 48 kHz / ~64 ms at 16 kHz — small enough that a duplex loop
#: polling this source stays responsive, large enough to amortise per-call
#: overhead in the PortAudio binding.
DEFAULT_BENCH_BLOCKSIZE = 1024

DEFAULT_TEE_SOCKET_NAME = "audio_tee.sock"
DEFAULT_HTTP_TIMEOUT = 10.0

_ROBOT_RECV_BYTES = 4096
_ROBOT_CONNECT_TIMEOUT = 0.5
_ROBOT_READ_TIMEOUT = 0.05
_ROBOT_RETRY_BACKOFF = 2.0

#: NAMED reasons a bench backend is unavailable, mirroring
#: ``reachy/behavior/face_sense.py``'s ``VISION_EXTRA_ABSENT`` /
#: ``VISION_STACK_UNAVAILABLE`` split: a missing package is a different fault,
#: with a different fix, from an installed package that fails to open a device.
BENCH_AUDIO_EXTRA_ABSENT = "bench-audio-extra-absent"
BENCH_AUDIO_STACK_UNAVAILABLE = "bench-audio-stack-unavailable"

#: Process-wide latch for the missing-``sounddevice`` warning — module-level
#: because the package's absence is a property of the process, not of any one
#: backend instance (see ``face_sense._VISION_WARNED`` for the same pattern).
_BENCH_WARNED = False

#: Sentinel distinguishing "no override passed" (read the env) from an
#: explicit ``None`` (use the system default device — a legitimate value for
#: ``sounddevice``).
_UNSET = object()


def resolve_profile(profile: str | None = None) -> str:
    """Return the effective profile: explicit arg > :data:`ENV_PROFILE` > default.

    Fails CLOSED on an unrecognised value — a typo'd profile must never
    silently fall back to a different one, since robot vs bench picks between a
    real socket/daemon and a dev-box microphone/speaker.
    """
    resolved = profile or os.environ.get(ENV_PROFILE) or DEFAULT_PROFILE
    if resolved not in _PROFILES:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown embody media profile {resolved!r}",
            remediation=f"set profile to one of {_PROFILES!r}, or {ENV_PROFILE} in the environment",
        )
    return resolved


def _env_int(name: str) -> int | None:
    """Parse an optional integer env var; ``None`` when unset or unparsable."""
    raw = os.environ.get(name)
    if not raw:
        return None
    try:
        return int(raw)
    except ValueError:
        logger.warning("embody: ignoring non-integer %s=%r", name, raw)
        return None


def _resolve_device(raw: str | None) -> Any:
    """``sounddevice`` accepts an index (int) or a name (str); env vars are strings."""
    if raw is None or raw == "":
        return None
    try:
        return int(raw)
    except ValueError:
        return raw


# ---------------------------------------------------------------------------
# Resample — cites reachy.speech.playback's approach, does not import it
# ---------------------------------------------------------------------------


def _resample_mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linearly resample mono float32 *samples* from *src_rate* to *dst_rate* Hz.

    Same approach as ``reachy.speech.playback._resample_mono`` (linear
    interpolation via ``numpy.interp`` — adequate for speech, adds no
    dependency beyond the numpy base dep already in the tree). A no-op when
    the rates already match or the input is empty, so the robot profile's
    default configuration (native == target == 16000 Hz) never touches the
    array.
    """
    if src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate or samples.size == 0:
        return samples
    n_dst = max(1, int(round(samples.size * dst_rate / src_rate)))
    src_index = np.linspace(0.0, samples.size - 1, num=n_dst)
    return np.interp(src_index, np.arange(samples.size), samples).astype(np.float32)


# ---------------------------------------------------------------------------
# The two profile-agnostic wrapper classes
# ---------------------------------------------------------------------------


class EmbodySource:
    """One mono float32 audio-in channel, normalised to ``target_sample_rate``.

    Literally the same class for both profiles — see the module docstring's
    backend contract. Never raises: a backend's ``read_native()`` failure is
    already ``None`` by the time it reaches here.
    """

    def __init__(self, backend: Any, *, target_sample_rate: int) -> None:
        self._backend = backend
        self._target_sample_rate = int(target_sample_rate)

    def read(self) -> np.ndarray | None:
        """Return one chunk of mono float32 samples in ``[-1, 1]``, or ``None``.

        ``None`` covers every "nothing this call" case: the backend is not
        connected/opened yet, a transient read hiccup, or genuinely no data
        ready — the caller treats it exactly like every other sense read in
        this codebase (silence, not a fault).
        """
        result = self._backend.read_native()
        if result is None:
            return None
        samples, native_rate = result
        samples = np.asarray(samples, dtype=np.float32).reshape(-1)
        if samples.size == 0:
            return samples
        return _resample_mono(samples, int(native_rate), self._target_sample_rate)

    @property
    def sample_rate(self) -> int:
        """The rate every :meth:`read` result is normalised to."""
        return self._target_sample_rate

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> "EmbodySource":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EmbodySink:
    """One audio-out channel, playing raw PCM16 mono LE bytes.

    Literally the same class for both profiles. ``play`` never raises — every
    backend failure resolves to a named :func:`reachy.senselog.drop`.
    """

    def __init__(self, backend: Any) -> None:
        self._backend = backend

    def play(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        """Play *pcm16_bytes* (raw int16 mono LE) at *samplerate* Hz.

        An empty payload is a harmless no-op, not a fault — mirrors
        ``play_audio``'s own empty-sample early return on the sdk leg.
        """
        if not pcm16_bytes:
            return
        self._backend.play(pcm16_bytes, samplerate=int(samplerate))

    def close(self) -> None:
        self._backend.close()

    def __enter__(self) -> "EmbodySink":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


class EmbodyMedia:
    """The (source, sink) pair :func:`build_media` returns for one profile."""

    def __init__(self, *, profile: str, source: EmbodySource, sink: EmbodySink) -> None:
        self.profile = profile
        self.source = source
        self.sink = sink

    def close(self) -> None:
        """Close both channels. Idempotent-safe: each backend's own close is."""
        self.source.close()
        self.sink.close()

    def __enter__(self) -> "EmbodyMedia":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


# ---------------------------------------------------------------------------
# Robot profile — tee socket source, daemon-http sink
# ---------------------------------------------------------------------------


class _RobotTeeSourceBackend:
    """Reads raw mono PCM16 chunks off the runtime's tee unix socket.

    Wire format this reader is written against (the shared contract, not
    something this module infers from the writer): a ``SOCK_STREAM`` unix
    domain socket carrying a headerless stream of little-endian int16 mono
    samples — the tee-fanned chunk bytes back to back, no length prefix, no
    per-chunk framing, no in-band sample-rate. Two things follow directly:

    * A single ``recv()`` may land mid-sample (an odd trailing byte), so it is
      buffered and prefixed onto the next read rather than dropped — the
      float32 conversion below never desyncs from the byte stream.
    * The sample rate is NOT on the wire, so it is a configuration constant
      this module owns (:data:`DEFAULT_ROBOT_SAMPLE_RATE`, overridable via
      :data:`ENV_ROBOT_SAMPLE_RATE` if a deployment's tee writer captures at a
      different rate than the documented default).

    Connection is lazy and backed off exactly like
    ``reachy.robot.media_client.HeldMediaClient``: a socket that does not exist
    yet (the tee not started, or absent entirely on a bare box) is the ORDINARY
    resting state, reported once via a latched drop, never a raised exception
    and never a retry storm.
    """

    def __init__(
        self,
        socket_path: Path,
        *,
        native_sample_rate: int,
        recv_bytes: int = _ROBOT_RECV_BYTES,
        connect_timeout: float = _ROBOT_CONNECT_TIMEOUT,
        read_timeout: float = _ROBOT_READ_TIMEOUT,
        retry_backoff: float = _ROBOT_RETRY_BACKOFF,
        now: Callable[[], float] | None = None,
    ) -> None:
        self._socket_path = socket_path
        self._native_sample_rate = int(native_sample_rate)
        self._recv_bytes = int(recv_bytes)
        self._connect_timeout = connect_timeout
        self._read_timeout = read_timeout
        self._retry_backoff = retry_backoff
        self._now = now or time.monotonic
        self._sock: socket.socket | None = None
        self._pending = b""
        self._next_attempt_t: float | None = None
        self._reported_down = False

    def _ensure_connected(self) -> bool:
        if self._sock is not None:
            return True
        now = self._now()
        if self._next_attempt_t is not None and now < self._next_attempt_t:
            return False
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(self._connect_timeout)
        try:
            sock.connect(str(self._socket_path))
        except OSError as err:
            sock.close()
            self._next_attempt_t = now + self._retry_backoff
            if not self._reported_down:
                self._reported_down = True
                senselog_drop(
                    _STAGE,
                    _SOURCE_ROBOT_SRC,
                    uuid.uuid4().hex[:8],
                    f"tee-unavailable ({err})",
                )
            return False
        sock.settimeout(self._read_timeout)
        self._sock = sock
        self._pending = b""
        if self._reported_down:
            senselog_stage(
                _STAGE,
                _SOURCE_ROBOT_SRC,
                uuid.uuid4().hex[:8],
                "connected to runtime tee socket (recovered)",
            )
        else:
            senselog_stage(
                _STAGE, _SOURCE_ROBOT_SRC, uuid.uuid4().hex[:8], "connected to runtime tee socket"
            )
        self._reported_down = False
        return True

    def read_native(self) -> tuple[np.ndarray, int] | None:
        if not self._ensure_connected():
            return None
        assert self._sock is not None  # narrowed by _ensure_connected
        try:
            data = self._sock.recv(self._recv_bytes)
        except (TimeoutError, socket.timeout):
            return None  # nothing ready this poll — ordinary, not a drop
        except OSError as err:
            self._drop(f"read-failed ({err})")
            return None
        if not data:
            self._drop("tee-closed")
            return None
        data = self._pending + data
        usable = len(data) - (len(data) % 2)
        self._pending = data[usable:]
        if usable == 0:
            return None
        pcm16 = np.frombuffer(data[:usable], dtype="<i2")
        samples = (pcm16.astype(np.float32) / 32768.0).copy()
        return samples, self._native_sample_rate

    def _drop(self, reason: str) -> None:
        self._close_socket()
        self._next_attempt_t = self._now() + self._retry_backoff
        senselog_drop(_STAGE, _SOURCE_ROBOT_SRC, uuid.uuid4().hex[:8], reason)

    def _close_socket(self) -> None:
        if self._sock is not None:
            try:
                self._sock.close()
            except OSError:
                pass
            self._sock = None

    def close(self) -> None:
        self._close_socket()


class _RobotHttpSinkBackend:
    """Plays PCM through the daemon's HTTP media route — ``transport="http"`` always.

    See the module docstring's "Why transport is hard-coded" section: this is
    the one line in the whole layer that must never read
    ``REACHY_TRANSPORT``, because the sdk fallback opens a second
    ``ReachyMini``.
    """

    def __init__(self, *, base_url: str, timeout: float = DEFAULT_HTTP_TIMEOUT) -> None:
        self._base_url = base_url
        self._timeout = timeout

    def play(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        try:
            play_audio(
                pcm16_bytes,
                samplerate=samplerate,
                transport="http",
                base_url=self._base_url,
                timeout=self._timeout,
            )
        except CliError as err:
            senselog_drop(
                _STAGE, _SOURCE_ROBOT_SINK, uuid.uuid4().hex[:8], f"playback-failed ({err.message})"
            )
        except Exception as err:  # noqa: BLE001 — a sink must never raise into its caller
            senselog_drop(
                _STAGE, _SOURCE_ROBOT_SINK, uuid.uuid4().hex[:8], f"playback-failed ({err})"
            )

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Bench profile — dev-box mic source, monitor-speaker sink (sounddevice, lazy)
# ---------------------------------------------------------------------------


def _import_sounddevice() -> Any | None:
    """Lazily import ``sounddevice``; ``None`` (never raises) when absent.

    A ``@staticmethod``-shaped free function so a test can inject a fake via
    ``monkeypatch.setattr(media, "_import_sounddevice", ...)`` without
    installing the real package — the same seam
    ``reachy.robot.media_client.HeldMediaClient._import`` and
    ``reachy.vision.face._import_cv2`` use for their own lazy engines.
    """
    try:
        import sounddevice as sd
    except ImportError:
        return None
    return sd


def _warn_bench_audio_once(reason: str) -> None:
    """Log the missing/broken bench-audio fact exactly once per process."""
    global _BENCH_WARNED  # noqa: PLW0603 — one process-wide warning, by design
    if _BENCH_WARNED:
        return
    _BENCH_WARNED = True
    if reason == BENCH_AUDIO_EXTRA_ABSENT:
        logger.warning(
            "embody: bench audio needs the lazily-imported 'sounddevice' package "
            "(PortAudio bindings); not installed — bench mic/speaker stay unavailable "
            "(pip install sounddevice; see reachy/embody/media.py's module docstring)"
        )
    else:
        logger.warning(
            "embody: bench audio device unavailable (sounddevice installed, device open failed); "
            "bench mic/speaker stay unavailable"
        )


class _BenchMicSourceBackend:
    """Captures from a dev-box microphone via ``sounddevice`` (PortAudio), lazily.

    Opens on first :meth:`read_native`, not at construction — mirrors every
    other lazy engine in this repo (``HeldMediaClient``, ``FaceSenseDriver``):
    construction never blocks or fails just because the caller built the
    object before checking whether a mic exists. Point ``device`` at the
    OS-level echo-cancel module's ``*_source`` name for AEC (see the module
    docstring's "Bench AEC" section) — this class does no cancellation itself.
    """

    def __init__(
        self,
        *,
        device: Any,
        samplerate: int | None,
        blocksize: int = DEFAULT_BENCH_BLOCKSIZE,
        channels: int = 1,
        import_sounddevice: Callable[[], Any | None] | None = None,
    ) -> None:
        self._device = device
        self._requested_samplerate = samplerate
        self._blocksize = blocksize
        self._channels = channels
        # None by default and resolved at CALL time (never a bound default
        # argument value) — a default argument would capture the module-level
        # function object at class-definition time and silently ignore a
        # test's ``monkeypatch.setattr(media, "_import_sounddevice", ...)``,
        # the same trap CLAUDE.md documents for ``EngagementClassifier``'s
        # ``complete_fn=None``. Mirrors ``face_sense.vision_unavailable_reason``.
        self._import_sounddevice = import_sounddevice
        self._stream: Any | None = None
        self._resolved_samplerate: int | None = None
        self._unavailable_reason: str | None = None

    def _ensure_stream(self) -> Any | None:
        if self._stream is not None or self._unavailable_reason is not None:
            return self._stream
        probe = self._import_sounddevice or _import_sounddevice
        sd = probe()
        if sd is None:
            self._unavailable_reason = BENCH_AUDIO_EXTRA_ABSENT
            _warn_bench_audio_once(self._unavailable_reason)
            senselog_drop(_STAGE, _SOURCE_BENCH_SRC, uuid.uuid4().hex[:8], self._unavailable_reason)
            return None
        rate = self._requested_samplerate or DEFAULT_BENCH_SAMPLE_RATE_FALLBACK
        try:
            stream = sd.InputStream(
                device=self._device,
                channels=self._channels,
                samplerate=rate,
                dtype="float32",
                blocksize=self._blocksize,
            )
            stream.start()
        except Exception as err:  # noqa: BLE001 — a device open fault must degrade, never raise
            self._unavailable_reason = BENCH_AUDIO_STACK_UNAVAILABLE
            _warn_bench_audio_once(self._unavailable_reason)
            senselog_drop(
                _STAGE,
                _SOURCE_BENCH_SRC,
                uuid.uuid4().hex[:8],
                f"{self._unavailable_reason} ({err})",
            )
            return None
        self._stream = stream
        self._resolved_samplerate = rate
        senselog_stage(
            _STAGE, _SOURCE_BENCH_SRC, uuid.uuid4().hex[:8], f"bench mic open at {rate} Hz"
        )
        return stream

    def read_native(self) -> tuple[np.ndarray, int] | None:
        stream = self._ensure_stream()
        if stream is None:
            return None
        try:
            data, _overflowed = stream.read(self._blocksize)
        except Exception as err:  # noqa: BLE001 — a read fault must degrade, never raise
            senselog_drop(_STAGE, _SOURCE_BENCH_SRC, uuid.uuid4().hex[:8], f"read-failed ({err})")
            return None
        samples = np.asarray(data, dtype=np.float32).reshape(-1)
        rate = self._resolved_samplerate or self._requested_samplerate
        return samples, int(rate or DEFAULT_BENCH_SAMPLE_RATE_FALLBACK)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:  # noqa: BLE001 — teardown must never raise
                logger.warning("embody: bench mic stream close raised", exc_info=True)
            self._stream = None


class _BenchSpeakerSinkBackend:
    """Plays PCM through a dev-box speaker via ``sounddevice`` (PortAudio), lazily.

    Point ``device`` at the OS-level echo-cancel module's ``*_sink`` name for
    AEC (see the module docstring). No stream is held open between calls —
    ``sounddevice.play`` opens, plays and lets the driver reclaim the device,
    which matches the daemon-http sink's own fire-and-forget shape.
    """

    def __init__(
        self,
        *,
        device: Any,
        import_sounddevice: Callable[[], Any | None] | None = None,
    ) -> None:
        self._device = device
        # See ``_BenchMicSourceBackend.__init__`` — resolved at call time, not
        # bound as a default argument value.
        self._import_sounddevice = import_sounddevice

    def play(self, pcm16_bytes: bytes, *, samplerate: int) -> None:
        probe = self._import_sounddevice or _import_sounddevice
        sd = probe()
        if sd is None:
            _warn_bench_audio_once(BENCH_AUDIO_EXTRA_ABSENT)
            senselog_drop(
                _STAGE, _SOURCE_BENCH_SINK, uuid.uuid4().hex[:8], BENCH_AUDIO_EXTRA_ABSENT
            )
            return
        samples = np.frombuffer(pcm16_bytes, dtype="<i2")
        try:
            sd.play(samples, samplerate=samplerate, device=self._device)
        except Exception as err:  # noqa: BLE001 — a sink must never raise into its caller
            senselog_drop(
                _STAGE, _SOURCE_BENCH_SINK, uuid.uuid4().hex[:8], f"playback-failed ({err})"
            )

    def close(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def _default_tee_socket_path() -> Path:
    """``state_dir()/behavior/audio_tee.sock`` — the documented convention.

    ``behavior/`` mirrors where the runtime already keeps its other cross-
    process artefacts (``rules.toml``, the intents spool). The exact name is a
    convention pending task t4's writer, not a hard contract this reader
    enforces — override with :data:`ENV_TEE_SOCKET` if the writer lands at a
    different path.

    Resolved through :func:`reachy.behavior.control.behavior_dir` rather than
    ``reachy.daemon.state_dir`` on purpose: ``reachy.daemon`` also owns
    ``start``/``stop`` for the daemon OS process, and no module in this package
    may hold a reference to that — a layer that can stop the daemon has a
    blast radius wider than its sanctioned action set. The spool loader is the
    sanctioned route to the same path (see ``tests/test_embody_redteam.py``).
    """
    from reachy.behavior.control import behavior_dir

    return behavior_dir() / DEFAULT_TEE_SOCKET_NAME


def build_media(
    profile: str | None = None,
    *,
    target_sample_rate: int | None = None,
    base_url: str | None = None,
    http_timeout: float = DEFAULT_HTTP_TIMEOUT,
    tee_socket: Path | str | None = None,
    robot_sample_rate: int | None = None,
    bench_input_device: Any = _UNSET,
    bench_output_device: Any = _UNSET,
    bench_sample_rate: int | None = None,
    bench_blocksize: int = DEFAULT_BENCH_BLOCKSIZE,
) -> EmbodyMedia:
    """Build the (source, sink) pair for *profile* — config/env selects, never a fork.

    Every keyword is an override; leaving it out reads the matching env var,
    then a documented default (see the module-level ``ENV_*``/``DEFAULT_*``
    constants). ``bench_input_device``/``bench_output_device`` default to the
    sentinel :data:`_UNSET` rather than ``None`` because ``None`` is itself a
    legitimate ``sounddevice`` value ("use the system default device").
    """
    resolved_profile = resolve_profile(profile)
    resolved_target = (
        target_sample_rate or _env_int(ENV_TARGET_SAMPLE_RATE) or DEFAULT_TARGET_SAMPLE_RATE
    )

    if resolved_profile == PROFILE_ROBOT:
        socket_path = Path(tee_socket) if tee_socket is not None else None
        if socket_path is None:
            env_socket = os.environ.get(ENV_TEE_SOCKET)
            socket_path = Path(env_socket) if env_socket else _default_tee_socket_path()
        native_rate = (
            robot_sample_rate or _env_int(ENV_ROBOT_SAMPLE_RATE) or DEFAULT_ROBOT_SAMPLE_RATE
        )
        source_backend: Any = _RobotTeeSourceBackend(socket_path, native_sample_rate=native_rate)
        resolved_base_url = base_url or os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL)
        sink_backend: Any = _RobotHttpSinkBackend(base_url=resolved_base_url, timeout=http_timeout)
    else:  # PROFILE_BENCH — the only other member of _PROFILES
        in_device = (
            bench_input_device
            if bench_input_device is not _UNSET
            else _resolve_device(os.environ.get(ENV_BENCH_INPUT_DEVICE))
        )
        out_device = (
            bench_output_device
            if bench_output_device is not _UNSET
            else _resolve_device(os.environ.get(ENV_BENCH_OUTPUT_DEVICE))
        )
        native_bench_rate = bench_sample_rate or _env_int(ENV_BENCH_SAMPLE_RATE)
        source_backend = _BenchMicSourceBackend(
            device=in_device, samplerate=native_bench_rate, blocksize=bench_blocksize
        )
        sink_backend = _BenchSpeakerSinkBackend(device=out_device)

    source = EmbodySource(source_backend, target_sample_rate=resolved_target)
    sink = EmbodySink(sink_backend)
    return EmbodyMedia(profile=resolved_profile, source=source, sink=sink)
