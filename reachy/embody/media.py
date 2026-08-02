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

* **robot** — source: the runtime's audio TEE — one self-describing JSON header
  line, then contiguous little-endian **float32** mono samples, over a unix
  ``SOCK_STREAM`` socket under ``state_dir()``. The writer is
  :mod:`reachy.behavior.audio_tee`, and this module **cites its constants**
  (see "One definition of the wire" below). Sink: the daemon's HTTP media route
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

One definition of the wire — cited, never re-derived
------------------------------------------------------
The reader and the writer are the two ends of ONE pipe, and they were first
built independently against two different descriptions of it: the writer's
header + float32, and a headerless int16 stream this module had inferred. That
does not fail loudly — a reader would have parsed the header's ASCII as audio
and then misread float32 as int16, so the layer would have appeared to *hear
noise* rather than to be broken, and neither side's tests could catch it
because no test connected them. Two things follow, and both are enforced:

* every wire fact comes from ``reachy.behavior.audio_tee`` by import —
  :data:`~reachy.behavior.audio_tee.SAMPLE_DTYPE`,
  :data:`~reachy.behavior.audio_tee.BYTES_PER_SAMPLE`,
  :data:`~reachy.behavior.audio_tee.WIRE_NAME`/``WIRE_VERSION``/``WIRE_FORMAT``,
  :data:`~reachy.behavior.audio_tee.HEADER_TERMINATOR` and the socket PATH
  (:func:`~reachy.behavior.audio_tee.socket_path`, so one
  ``REACHY_AUDIO_TEE_SOCKET`` moves both ends and neither can half-move);
* the two ends are connected in a real end-to-end test
  (``tests/test_embody_tee_integration.py``) rather than each being fed a
  payload it framed itself.

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
:func:`reachy.senselog.drop`) rather than raising. The pin lives where the other
engine pins live: ``pip install 'reachy-mini-cli[bench]'``. It is deliberately
an extra rather than a base dep because ``sounddevice`` binds PortAudio, a
system library absent on a bare box and in CI — and because the DEPLOYED path
never needs it: the robot profile hears through the runtime's tee socket and
speaks through the daemon's http route, both stdlib.

Import boundary
----------------
No ``reachy_mini`` import anywhere in this file (``tests/test_embody_media.py``
pins this with an AST scan, not merely a run-time probe — a lazy import buried
in a branch nobody exercises would not be caught by import-time inspection
alone). The only reachy imports are :mod:`reachy.behavior.audio_tee` (the wire
constants + the socket path — never ``reachy.daemon`` itself, which owns the
daemon's ``start``/``stop``; the tee module resolves the state dir on this
module's behalf, and ``tests/test_embody_redteam.py`` asserts no layer module
ever NAMES ``reachy.daemon``), :func:`reachy.speech.playback.play_audio` (the
sanctioned daemon-http sink, always called with ``transport="http"``),
:mod:`reachy.senselog` (named drops) and :mod:`reachy.cli._errors` (the shared
error contract for a genuinely bad profile string). No ``subprocess``, no
``os.system``, no shell of any kind — device I/O goes through ``socket`` (the
tee) or the lazily-imported ``sounddevice`` (the bench devices) only.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import time
import uuid
from pathlib import Path
from typing import Any, Callable

import numpy as np

from reachy.behavior.audio_tee import BYTES_PER_SAMPLE as TEE_BYTES_PER_SAMPLE
from reachy.behavior.audio_tee import DEFAULT_SOCKET_NAME as _WRITER_SOCKET_NAME
from reachy.behavior.audio_tee import HEADER_TERMINATOR as TEE_HEADER_TERMINATOR
from reachy.behavior.audio_tee import SAMPLE_DTYPE as TEE_SAMPLE_DTYPE
from reachy.behavior.audio_tee import WIRE_FORMAT as TEE_WIRE_FORMAT
from reachy.behavior.audio_tee import WIRE_NAME as TEE_WIRE_NAME
from reachy.behavior.audio_tee import WIRE_VERSION as TEE_WIRE_VERSION
from reachy.behavior.audio_tee import socket_path as tee_socket_path
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

#: FALLBACK native rate for the robot source, used only when the tee's header
#: announces ``samplerate: null`` (a cold media holder that cannot report one
#: yet). The header is authoritative whenever it carries a rate — see
#: :class:`_RobotTeeSourceBackend`. 16000 Hz is the runtime's measured mic rate
#: (see the module docstring) and also the default normalisation target, so the
#: robot profile resamples nothing.
DEFAULT_ROBOT_SAMPLE_RATE = 16000
DEFAULT_TARGET_SAMPLE_RATE = 16000
#: A typical USB webcam mic's native rate, used only when the bench device
#: itself does not report one and no override is configured.
DEFAULT_BENCH_SAMPLE_RATE_FALLBACK = 48000
#: ~21 ms at 48 kHz / ~64 ms at 16 kHz — small enough that a duplex loop
#: polling this source stays responsive, large enough to amortise per-call
#: overhead in the PortAudio binding.
DEFAULT_BENCH_BLOCKSIZE = 1024

#: The tee socket's filename — CITED from the writer, never spelled again here.
#: Kept as a public name because it is part of this module's own surface; the
#: default PATH comes from the writer's resolver (:func:`_default_tee_socket_path`).
DEFAULT_TEE_SOCKET_NAME = _WRITER_SOCKET_NAME

DEFAULT_HTTP_TIMEOUT = 10.0

_ROBOT_RECV_BYTES = 4096
_ROBOT_CONNECT_TIMEOUT = 0.5
_ROBOT_READ_TIMEOUT = 0.05
_ROBOT_RETRY_BACKOFF = 2.0

#: Wire versions this reader knows how to consume. A tuple, not a set: a
#: version field of ``[1]`` (or any other unhashable JSON value) must be
#: REFUSED, not raise ``TypeError`` inside the membership test.
UNDERSTOOD_WIRE_VERSIONS = (TEE_WIRE_VERSION,)

#: Cap on the header line. The writer's header is well under 200 bytes, so this
#: is not a limit a real tee can hit — it exists so a peer that never sends a
#: newline is REFUSED rather than buffered without bound.
MAX_HEADER_BYTES = 4096

#: NAMED refusal reasons for the tee's header, following this module's
#: ``reason (detail)`` convention. Split two ways because the fixes differ: an
#: INVALID header means whatever is on that socket is not this protocol at all
#: (unparseable, not an object, or no line inside the cap); a FOREIGN one is
#: well-formed but announces a stream / version / format / channel count this
#: reader cannot consume. Either way the reader disconnects and backs off — it
#: never falls through to reading the bytes as samples.
TEE_HEADER_INVALID = "tee-header-invalid"
TEE_HEADER_FOREIGN = "tee-header-foreign"
#: The header's ``samplerate`` was ``null`` (or unusable) — a LEGITIMATE header
#: the writer emits when a cold media holder cannot report a rate yet. The
#: reader keeps reading against its configured fallback, and says so, because
#: every later consumer is now working off a configured guess rather than the
#: mic's real rate.
TEE_RATE_UNKNOWN = "tee-rate-unknown"

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
    """Reads the runtime's audio tee: one JSON header line, then float32 mono.

    The wire is :mod:`reachy.behavior.audio_tee`'s, and this class does not
    restate it — it imports it (see the module docstring's "One definition of
    the wire"). A ``SOCK_STREAM`` unix socket carrying::

        {"stream":"reachy-audio-tee","version":1,"format":"f32le",
         "channels":1,"samplerate":16000}\\n
        <little-endian float32 samples, contiguous, in production order>

    Three consequences, each load-bearing:

    * **The header is read first, in full, and validated.** It is the one thing
      a hearer cannot guess, so an absent, truncated, unparseable or foreign
      header is a NAMED refusal (:data:`TEE_HEADER_INVALID` /
      :data:`TEE_HEADER_FOREIGN`) plus a disconnect and a backoff retry —
      never "start reading samples anyway", which is precisely how ASCII header
      bytes would arrive at a caller as audio.
    * **The rate comes off the wire.** ``samplerate`` in the header wins over
      the configured ``native_sample_rate``, which survives only as the
      fallback for the header's legitimate ``null`` (a cold media holder that
      cannot report one yet) — announced with :data:`TEE_RATE_UNKNOWN`, never
      silently assumed.
    * **A read may land mid-sample.** A sample is
      :data:`~reachy.behavior.audio_tee.BYTES_PER_SAMPLE` (4) bytes, not 2, so
      the remainder is buffered and prefixed onto the next read. An off-by-one
      here shifts every sample after it — the whole stream, silently — which is
      why ``tests/test_embody_tee_integration.py`` drives this against real
      ``recv`` sizes that are coprime with the sample size.

    Connection is lazy and backed off exactly like
    ``reachy.robot.media_client.HeldMediaClient``: a socket that does not exist
    yet (the tee not started, or absent entirely on a bare box) is the ORDINARY
    resting state, reported once via a latched drop, never a raised exception
    and never a retry storm.

    :param native_sample_rate: the FALLBACK rate, used only for a header that
        announces ``samplerate: null``.
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
        #: Per-CONNECTION header state — reset on every connect, because a
        #: restarted writer may legitimately announce a different rate.
        self._header_buf = b""
        self._header_seen = False
        self._stream_rate = self._native_sample_rate
        #: Last reported header refusal, so a peer that is permanently not a
        #: tee costs ONE line rather than one per backoff period. Cleared by a
        #: header that parses, so a NEW fault is always reported.
        self._header_fault: str | None = None

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
        self._reset_stream_state()
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

    def _reset_stream_state(self) -> None:
        """Forget everything about the previous connection's byte stream."""
        self._pending = b""
        self._header_buf = b""
        self._header_seen = False
        self._stream_rate = self._native_sample_rate

    def read_native(self) -> tuple[np.ndarray, int] | None:
        """One chunk of mono float32 samples plus the stream's rate, or ``None``.

        ``None`` is every "nothing this call" case — not connected, nothing
        ready, header still arriving, or a refusal that has already been named.
        Never raises.
        """
        if not self._ensure_connected():
            return None
        sock = self._sock
        if sock is None:  # unreachable: _ensure_connected returned True
            return None
        try:
            data = sock.recv(self._recv_bytes)
        except (TimeoutError, socket.timeout):
            return None  # nothing ready this poll — ordinary, not a drop
        except OSError as err:
            self._drop(f"read-failed ({err})")
            return None
        if not data:
            self._drop("tee-closed")
            return None
        if not self._header_seen:
            data = self._consume_header(data)
            if data is None:
                return None
        return self._decode(data)

    # -- the header ----------------------------------------------------

    def _consume_header(self, data: bytes) -> bytes | None:
        """Take the header line off the front; return the SAMPLE bytes after it.

        ``None`` means no samples this call: the line is still arriving (a
        ``recv`` may split it anywhere, header included) or it was refused.
        """
        buffered = self._header_buf + data
        line, terminator, rest = buffered.partition(TEE_HEADER_TERMINATOR)
        if not terminator:
            if len(buffered) > MAX_HEADER_BYTES:
                self._refuse_header(
                    TEE_HEADER_INVALID, f"no header line in the first {len(buffered)} bytes"
                )
                return None
            self._header_buf = buffered
            return None
        self._header_buf = b""
        refusal = self._accept_header(line)
        if refusal is not None:
            self._refuse_header(*refusal)
            return None
        return rest

    def _accept_header(self, line: bytes) -> tuple[str, str] | None:
        """Validate one header line. ``None`` accepts it; else (reason, detail).

        Fail-closed on every field the wire declares, because each one silently
        changes how the bytes after it must be read.
        """
        try:
            header = json.loads(line.decode("utf-8"))
        except (UnicodeDecodeError, ValueError):
            return (TEE_HEADER_INVALID, "the first line is not JSON")
        # ``type(...) is`` rather than ``isinstance``: this module is pinned
        # isinstance-free (tests/test_embody_media.py), and an exact-type check
        # is the stricter reading anyway.
        if type(header) is not dict:
            return (TEE_HEADER_INVALID, f"the header is {type(header).__name__}, not an object")
        stream = header.get("stream")
        if stream != TEE_WIRE_NAME:
            return (TEE_HEADER_FOREIGN, f"stream={stream!r} (expected {TEE_WIRE_NAME!r})")
        version = header.get("version")
        if version not in UNDERSTOOD_WIRE_VERSIONS:
            return (
                TEE_HEADER_FOREIGN,
                f"version={version!r} (understood: {list(UNDERSTOOD_WIRE_VERSIONS)})",
            )
        wire_format = header.get("format")
        if wire_format != TEE_WIRE_FORMAT:
            return (TEE_HEADER_FOREIGN, f"format={wire_format!r} (expected {TEE_WIRE_FORMAT!r})")
        channels = header.get("channels", 1)
        if channels != 1:
            return (TEE_HEADER_FOREIGN, f"channels={channels!r} (this reader is mono)")
        self._header_seen = True
        self._header_fault = None
        self._stream_rate = self._resolve_stream_rate(header.get("samplerate"))
        senselog_stage(
            _STAGE,
            _SOURCE_ROBOT_SRC,
            uuid.uuid4().hex[:8],
            f"tee header accepted (format={wire_format} rate={self._stream_rate} Hz)",
        )
        return None

    def _resolve_stream_rate(self, announced: object) -> int:
        """The header's rate, or the configured fallback — announced either way."""
        try:
            rate = int(announced) if announced is not None else None
        except (TypeError, ValueError):
            rate = None
        if rate is not None and rate > 0:
            return rate
        senselog_drop(
            _STAGE,
            _SOURCE_ROBOT_SRC,
            uuid.uuid4().hex[:8],
            f"{TEE_RATE_UNKNOWN} (header samplerate={announced!r}; "
            f"using the configured {self._native_sample_rate} Hz)",
        )
        return self._native_sample_rate

    def _refuse_header(self, reason: str, detail: str) -> None:
        """Name it, disconnect, back off — never consume the bytes as audio."""
        report = self._header_fault != reason
        self._close_socket()
        self._reset_stream_state()
        self._header_fault = reason
        self._next_attempt_t = self._now() + self._retry_backoff
        if report:
            senselog_drop(_STAGE, _SOURCE_ROBOT_SRC, uuid.uuid4().hex[:8], f"{reason} ({detail})")

    # -- the samples ---------------------------------------------------

    def _decode(self, data: bytes) -> tuple[np.ndarray, int] | None:
        """Whole float32 samples only; the trailing part-sample waits for more.

        The wire is already mono float32, so there is no conversion here at
        all — an int16 round-trip would only lose precision.
        """
        buffered = self._pending + data
        usable = len(buffered) - (len(buffered) % TEE_BYTES_PER_SAMPLE)
        self._pending = buffered[usable:]
        if usable == 0:
            return None
        # ``astype`` copies, so the result owns writable memory rather than
        # aliasing the read-only buffer ``frombuffer`` returns, and it converts
        # the wire's explicit little-endian dtype to the machine's own.
        samples = np.frombuffer(buffered[:usable], dtype=TEE_SAMPLE_DTYPE).astype(np.float32)
        return samples, self._stream_rate

    def _drop(self, reason: str) -> None:
        self._close_socket()
        self._reset_stream_state()
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
            "(pip install 'reachy-mini-cli[bench]'; the robot profile needs none of this)"
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
    """Exactly where the WRITER binds — :func:`reachy.behavior.audio_tee.socket_path`.

    Not a path this module derives in parallel. It used to be
    ``state_dir()/behavior/audio_tee.sock`` while the writer bound
    ``state_dir()/audio_tee.sock``: two files, no pipe, and nothing that could
    notice — the reader simply never found a socket and reported its ordinary
    ``tee-unavailable`` resting state forever. Calling the writer's own resolver
    also means one ``REACHY_AUDIO_TEE_SOCKET`` moves BOTH ends together, so a
    bench run (or the suite's own per-worker guard) cannot half-move the pipe.

    Reached through :mod:`reachy.behavior.audio_tee` rather than
    ``reachy.daemon.state_dir`` on purpose: ``reachy.daemon`` also owns
    ``start``/``stop`` for the daemon OS process, and no module in this package
    may hold a reference to that — a layer that can stop the daemon has a blast
    radius wider than its sanctioned action set (see
    ``tests/test_embody_redteam.py``). The tee module resolves the state dir on
    this module's behalf.

    :data:`ENV_TEE_SOCKET` remains an embody-specific override for the unusual
    case of a reader pointed somewhere else deliberately.
    """
    return tee_socket_path()


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
