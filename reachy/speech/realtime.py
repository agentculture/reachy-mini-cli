"""The lobes ``/v1/realtime`` session client — hearing with SERVER-side VAD.

This is the module that moves utterance endpointing OFF the robot. Where
:class:`reachy.speech.stt.Transcriber` needs the caller to decide where an
utterance starts and stops (a locally tuned energy threshold) and then POSTs
one finished clip, :class:`RealtimeTranscriber` opens ONE long-lived WebSocket
session to the lobes gateway, streams every mic chunk into it as a base64
``input_audio_buffer.append`` TEXT event, and lets the server's ``server_vad``
say where the sentence ended. What comes back is an already-endpointed
:class:`Utterance`.

**It behaves like** :mod:`reachy.speech.stt` **in the one way that matters: it
never raises into its caller.** A refused handshake, a dead gateway, a session
that closes mid-stream, a malformed event, a named server error — every one of
them resolves to a NAMED :func:`reachy.senselog.drop` and a scheduled
reconnect. The caller sees "no words this tick", exactly as it does when the
STT endpoint is down.

--------------------------------------------------------------------------
Public API (task t4 wires this into ``TranscriptSenseDriver``)
--------------------------------------------------------------------------
::

    client = RealtimeTranscriber(sample_rate=media.samplerate)   # config below
    client.start()                       # spawn the worker; do this at COMPOSITION
    client.submit_audio(chunk)           # tick thread: O(1), non-blocking, no raise
    utterance = client.take_utterance()  # tick thread: an Utterance, or None
    client.close()                       # idempotent; joins the worker

* :meth:`RealtimeTranscriber.submit_audio` accepts one mic chunk — a float32
  numpy array in ``[-1, 1]`` (1-D, or ``(N, C)``, coerced through
  :func:`reachy.robot.audio_shape.to_mono`), an ``int16`` array, or raw PCM16
  LE ``bytes``. It converts to PCM16 and does ONE ``put_nowait`` onto a bounded
  queue. Returns whether the chunk was accepted. ``None`` / an empty chunk /
  an unusable object is ``False`` with no log (absence of audio is not a
  fault); a genuinely full queue is ``False`` plus a LATCHED ``queue-full``
  drop. It never touches a socket, never blocks, and never raises.
* :meth:`RealtimeTranscriber.take_utterance` pops the oldest ready
  :class:`Utterance` or returns ``None``. Non-blocking, never raises. An
  optional ``on_utterance`` callback is an ADDITIONAL tap (fired on the worker
  thread, guarded); the queue is populated either way, so t4 can use whichever
  fits its tick.
* :class:`Utterance` is frozen: ``text`` (the transcript), ``t`` (the monotonic
  instant it arrived — what a self-mute window is compared against), plus the
  server's opaque ``item_id`` / ``session_id`` for correlation.
* Counters for a status readout or an assertion: ``submitted`` / ``sent`` /
  ``dropped`` / ``utterances`` / ``sessions`` / ``connect_failures`` /
  ``pongs_sent`` / ``ignored_events``; state via ``connected`` and
  ``session_down``.
* :meth:`RealtimeTranscriber.set_sample_rate` re-negotiates the session when
  the real mic rate turns out to differ from the constructed one (the rate
  rides the connect URL, so it can only change with a new session).

--------------------------------------------------------------------------
Threading: the tick thread never reaches a socket
--------------------------------------------------------------------------
ONE background worker thread owns the socket, the handshake, the reconnect
backoff and every frame in both directions. The tick thread only ever touches
two bounded :class:`queue.Queue` objects. This is the same split
:class:`reachy.behavior.speech_act.SpeechActuator` uses in the opposite
direction, and for the same reason: the engine holds a **20 ms budget at
50 Hz**, and a blocking call on that thread is the defect class that produced
the deployed box's measured 425-1213 ms startup overruns.

The worker never blocks on a single waitable it cannot leave: it polls the
socket with :func:`select.select` on a short timeout and drains the audio queue
between polls, so a chunk submitted on the tick reaches the wire within one
poll interval, and a silent server never starves the sender.

--------------------------------------------------------------------------
Keepalive: the server pings, we pong
--------------------------------------------------------------------------
The gateway is served by **uvicorn**, whose WebSocket implementation sends a
PING roughly every **20 s** and drops the connection if no PONG comes back
(``ws_ping_interval=20.0`` / ``ws_ping_timeout=20.0`` are its defaults). A
client that reads only TEXT frames therefore looks dead after ~40 s of quiet
and gets disconnected. Every inbound PING is answered with a PONG carrying the
same payload (RFC 6455 §5.5.3). This client never sends a PING of its own — a
session that has gone silent surfaces as a read failure and a reconnect, which
is the recovery path anyway.

--------------------------------------------------------------------------
Session-down is a LATCHED transition, not a per-chunk complaint
--------------------------------------------------------------------------
Audio arrives 50 times a second. A drop line per undeliverable chunk would put
50 lines/s in the journal — the #99 defect this repo has already paid for
once. So:

* Entering the down state logs exactly TWO lines: the CAUSE
  (``handshake-refused (HTTP 401)``, ``connect-failed (...)``,
  ``stream-closed (...)``) and the latched ``session-down`` state. Every
  further failed attempt while already down logs NOTHING.
* Reconnect is on the worker thread with bounded exponential backoff
  (``backoff_initial_s`` doubling to ``backoff_max_s``). A session that stayed
  up for ``stable_after_s`` resets the backoff, so a transient drop recovers
  immediately while a dead gateway is retried slowly.
* Recovery logs ONE ``session up`` line naming the URL.
* A full audio queue is latched the same way: one ``queue-full`` line per fill
  episode, cleared by the next successful put.

On (re)connect the standing audio backlog is dropped if it is older than
``stale_after_s`` — the same discipline
:class:`reachy.behavior.audio_pump.AudioPump` applies before going live, since
replaying seconds-old audio into a server-side VAD manufactures utterances
that were never spoken. Fresh chunks queued during the handshake survive.

--------------------------------------------------------------------------
Ears-only
--------------------------------------------------------------------------
This wire is a microphone, not a conversation: **``response.create`` is never
sent** (issue #115's non-goal), so the server never synthesizes anything. An
inbound ``response.*`` event — from a future server that volunteers one — is
ignored with a debug log and counted in ``ignored_events``. The robot's voice
lives in :mod:`reachy.behavior.speech_act`, and cognition in ``agent attach``.

--------------------------------------------------------------------------
Configuration
--------------------------------------------------------------------------
::

    url      explicit arg > REACHY_REALTIME_URL > REACHY_OPENAI_URL_BASE
             (http(s) mapped to ws(s) + /v1/realtime) > ws://localhost:8001/v1/realtime
    api key  explicit arg > REACHY_REALTIME_API_KEY > REACHY_OPENAI_API_KEY

Precedence is by PRESENCE, not truthiness: an explicitly EMPTY
``REACHY_REALTIME_API_KEY`` means "this gateway needs no auth", and must not
fall through to a stale ``REACHY_OPENAI_API_KEY`` — the same rule
:mod:`reachy.speech.llm` states for its own pair. That resolution is
duplicated here rather than imported, because importing the LLM client into a
module the behavior runtime will hold would drag a language model into the
runtime's import path (see ``tests/test_zero_llm_boundary.py``). Nothing in
this module reaches an LLM; the gateway route it speaks to is a VAD + STT.

An unusable URL scheme is a clean exit-1 :class:`~reachy.cli._errors.CliError`
raised at CONSTRUCTION — a typo is a startup error, never a mid-session
surprise, exactly as :func:`reachy.behavior.speech_act.resolve_playback_transport`
treats its own.

Standard library plus numpy and :mod:`reachy.speech.realtime_wire` — no
WebSocket dependency, no new package (``pyproject.toml`` is untouched).
"""

from __future__ import annotations

import logging
import os
import queue
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

import numpy as np

from reachy import senselog
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.robot.audio_shape import AEC_CHANNEL, to_mono
from reachy.speech import realtime_wire as wire

logger = logging.getLogger(__name__)

#: ``[SENSE stage=realtime source=speech event=<id>]`` — this module's identity.
STAGE = "realtime"
SOURCE = "speech"

# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #

#: Explicit realtime overrides (win over the shared gateway pair below).
REALTIME_URL_ENV = "REACHY_REALTIME_URL"
REALTIME_API_KEY_ENV = "REACHY_REALTIME_API_KEY"
#: The gateway pair the rest of the repo already configures.
OPENAI_URL_BASE_ENV = "REACHY_OPENAI_URL_BASE"
OPENAI_API_KEY_ENV = "REACHY_OPENAI_API_KEY"

#: The lobes gateway on the same box (cortex/senses/realtime all share :8001).
DEFAULT_GATEWAY_URL = "http://localhost:8001"

#: The one event type this client consumes as WORDS.
TRANSCRIPTION_COMPLETED = "conversation.item.input_audio_transcription.completed"
SESSION_CREATED = "session.created"
SPEECH_STARTED = "input_audio_buffer.speech_started"
SPEECH_STOPPED = "input_audio_buffer.speech_stopped"
ERROR_EVENT = "error"

# --------------------------------------------------------------------------- #
# Named drop reasons — every failure is one of these, never a silent no-op     #
# --------------------------------------------------------------------------- #

#: The LATCHED state: there is no session, so audio is going nowhere.
REASON_SESSION_DOWN = "session-down"
#: The gateway answered the handshake with something other than 101.
REASON_HANDSHAKE_REFUSED = "handshake-refused"
#: The TCP/TLS connect itself failed (nothing listening, DNS, timeout).
REASON_CONNECT_FAILED = "connect-failed"
#: An established session ended (CLOSE frame, EOF, or a send/read failure).
REASON_STREAM_CLOSED = "stream-closed"
#: The tick thread offered a chunk the worker has not drained yet.
REASON_QUEUE_FULL = "queue-full"
#: A transcript arrived faster than the caller drained it (oldest evicted).
REASON_UTTERANCE_QUEUE_FULL = "utterance-queue-full"
#: A TEXT frame that is not a decodable event object.
REASON_MALFORMED_EVENT = "malformed-event"
#: The server said ``server_vad`` is unavailable — no endpointing upstream.
REASON_VAD_UNAVAILABLE = "vad-unavailable"
#: The server could not forward a committed turn to STT.
REASON_STT_FORWARD_FAILED = "stt-forward-failed"
#: Any other named ``error`` event.
REASON_SERVER_ERROR = "server-error"
#: A ``transcription.completed`` carrying no usable text.
REASON_EMPTY_TRANSCRIPT = "empty-transcript"

#: Server ``error.code`` -> this module's kebab-case drop reason.
_ERROR_REASONS = {
    "vad_unavailable": REASON_VAD_UNAVAILABLE,
    "stt_forward_failed": REASON_STT_FORWARD_FAILED,
}

# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #

#: Outbound audio queue depth. ~64 x 20 ms chunks ~ 1.3 s at the runtime's tick
#: rate: deep enough to ride out a scheduling hiccup, shallow enough that a
#: wedged session cannot hoard memory or seconds of stale sound.
DEFAULT_AUDIO_MAXSIZE = 64
#: Inbound utterance queue depth. A transcript is worthless once stale, so this
#: is small and the OLDEST is evicted on overflow.
DEFAULT_UTTERANCE_MAXSIZE = 8
#: TCP connect + handshake-response budget.
DEFAULT_CONNECT_TIMEOUT_S = 5.0
#: How long a half-arrived frame may stall before the session is declared lost.
DEFAULT_FRAME_TIMEOUT_S = 5.0
#: Socket-level timeout: the granularity at which the worker re-checks close().
_SOCKET_TIMEOUT_S = 0.2
#: How long the worker waits on a quiet socket before draining audio again.
DEFAULT_POLL_INTERVAL_S = 0.01
#: Reconnect backoff bounds (doubling).
DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
#: A session that lasted this long counts as healthy; its loss resets backoff.
DEFAULT_STABLE_AFTER_S = 10.0
#: Queued audio older than this is discarded when a session comes up.
DEFAULT_STALE_AFTER_S = 2.0
#: Bounded worker join at close() — teardown must never hang the process.
DEFAULT_JOIN_TIMEOUT_S = 2.0

#: Bounds on one pump iteration, so neither direction can starve the other.
_MAX_SENDS_PER_PUMP = 32
_MAX_FRAMES_PER_PUMP = 32
#: A handshake response head larger than this is a broken peer, not a gateway.
_MAX_HEAD_BYTES = 64 * 1024


# --------------------------------------------------------------------------- #
# Resolution helpers (the stt.py resolver style)                              #
# --------------------------------------------------------------------------- #


def _env_present(name: str) -> str | None:
    """Return the env value if the name is SET (even to ""), else ``None``."""
    return os.environ[name] if name in os.environ else None


def resolve_realtime_base_url(url: str | None = None) -> str:
    """The ws(s) realtime endpoint, WITHOUT the ``input_sample_rate`` query.

    Explicit *url* > ``REACHY_REALTIME_URL`` > ``REACHY_OPENAI_URL_BASE`` >
    :data:`DEFAULT_GATEWAY_URL`. An ``http(s)`` value is read as a GATEWAY BASE
    and mapped onto the route by
    :func:`reachy.speech.realtime_wire.derive_realtime_ws_url` (so the scheme +
    path mapping has one owner); a ``ws(s)`` value is taken verbatim, path and
    all. The sample rate is deliberately NOT baked in here — it rides
    :func:`connect_url`, so :meth:`RealtimeTranscriber.set_sample_rate` can
    change it without re-resolving configuration.

    Raises a clean exit-1 :class:`CliError` for any other scheme.
    """
    raw = url or _env_present(REALTIME_URL_ENV) or _env_present(OPENAI_URL_BASE_ENV)
    raw = (raw or DEFAULT_GATEWAY_URL).strip()
    scheme = urlsplit(raw).scheme
    if scheme in ("ws", "wss"):
        # Taken verbatim, minus a trailing slash — an empty path falls back to
        # the route in connect_url(), and "…/v1/realtime/" is the same route.
        return raw.rstrip("/")
    if scheme in ("http", "https"):
        # Cite the wire helper for the mapping, then drop its placeholder query.
        derived = urlsplit(wire.derive_realtime_ws_url(raw, 0))
        return urlunsplit((derived.scheme, derived.netloc, derived.path, "", ""))
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"unusable realtime endpoint {raw!r}",
        remediation=(
            f"set {REALTIME_URL_ENV} to a ws:// or wss:// URL, or "
            f"{OPENAI_URL_BASE_ENV} to the gateway's http(s) base URL"
        ),
    )


def resolve_realtime_api_key(api_key: str | None = None) -> str | None:
    """The bearer key: explicit > ``REACHY_REALTIME_API_KEY`` > ``REACHY_OPENAI_API_KEY``.

    Precedence is by PRESENCE (see the module docstring): a set-but-empty
    realtime key means "no auth" and stops the fallback, rather than silently
    sending the gateway key. Returns ``None`` when no key applies, in which
    case no ``Authorization`` header is sent at all — matching the gateway's
    documented unauthenticated default.
    """
    if api_key is not None:
        return api_key or None
    for name in (REALTIME_API_KEY_ENV, OPENAI_API_KEY_ENV):
        value = _env_present(name)
        if value is not None:
            return value or None
    return None


def connect_url(base_url: str, sample_rate: int) -> str:
    """Add (or replace) ``input_sample_rate`` on *base_url*.

    Session config rides the connect URL on this wire — there is no follow-up
    ``session.update`` message — so the rate the microphone actually reports is
    carried here, never hard-coded and never resampled client-side.
    """
    parts = urlsplit(base_url)
    params = [
        (name, value)
        for name, value in parse_qsl(parts.query, keep_blank_values=True)
        if name != "input_sample_rate"
    ]
    params.append(("input_sample_rate", str(int(sample_rate))))
    path = parts.path or wire.REALTIME_PATH
    return urlunsplit((parts.scheme, parts.netloc, path, urlencode(params), ""))


# --------------------------------------------------------------------------- #
# The utterance record                                                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Utterance:
    """One server-endpointed utterance.

    Attributes:
        text: the transcript, guaranteed non-empty (an empty one is dropped).
        t: monotonic instant the transcript ARRIVED — what a self-mute window
            is compared against, so the caller never needs its own clock.
        item_id: the server's opaque conversation-item id, or ``None``.
        session_id: the server's opaque session id, or ``None``.
    """

    text: str
    t: float
    item_id: str | None = None
    session_id: str | None = None


class _SessionLost(Exception):
    """Internal: this session ended. Never escapes the worker thread."""

    def __init__(self, reason: str, detail: str = "", *, intentional: bool = False) -> None:
        super().__init__(f"{reason} {detail}".strip())
        self.reason = reason
        self.detail = detail
        self.intentional = intentional


class _FrameReader:
    """A byte-buffered reader over one connected socket.

    Serves both the HTTP handshake head (:meth:`read_until`) and the frame
    stream (:meth:`recv_exact`, the exact shape
    :func:`reachy.speech.realtime_wire.read_frame` takes) from ONE buffer, so a
    server that pipelines ``session.created`` into the same TCP segment as its
    101 response never loses those bytes.
    """

    def __init__(self, sock: socket.socket, frame_timeout_s: float) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._frame_timeout_s = frame_timeout_s

    def has_pending(self) -> bool:
        """Bytes already buffered here or inside a TLS record (select can't see either)."""
        if self._buf:
            return True
        pending = getattr(self._sock, "pending", None)
        try:
            return bool(pending()) if callable(pending) else False
        except OSError:  # pragma: no cover - defensive
            return False

    def read_until(self, marker: bytes, deadline: float) -> bytes | None:
        while marker not in self._buf:
            if time.monotonic() > deadline or len(self._buf) > _MAX_HEAD_BYTES:
                return None
            try:
                chunk = self._sock.recv(4096)
            except socket.timeout:
                continue
            except OSError:
                return None
            if not chunk:
                return None
            self._buf.extend(chunk)
        index = self._buf.index(marker) + len(marker)
        head = bytes(self._buf[:index])
        del self._buf[:index]
        return head

    def recv_exact(self, n: int) -> bytes:
        """Exactly *n* bytes, or fewer at EOF / once the frame budget expires."""
        deadline = time.monotonic() + self._frame_timeout_s
        while len(self._buf) < n:
            if time.monotonic() > deadline:
                break
            try:
                chunk = self._sock.recv(max(4096, n))
            except socket.timeout:
                continue
            except OSError:
                break
            if not chunk:
                break
            self._buf.extend(chunk)
        take = min(n, len(self._buf))
        data = bytes(self._buf[:take])
        del self._buf[:take]
        return data


def _to_pcm16(audio: Any) -> bytes:
    """Coerce one mic chunk to little-endian PCM16 mono bytes; ``b""`` if unusable.

    Total by construction — a ``None`` read, an empty chunk, a string, a random
    object or a rank a microphone cannot produce all return ``b""`` rather than
    raising, because this runs on the caller's tick thread. Raw ``bytes`` are
    taken as already-encoded PCM16; an integer array is written verbatim; a
    float array goes through :func:`reachy.robot.audio_shape.to_mono` (channel
    selection, NEVER a bare ``reshape(-1)``, which interleaves) and is scaled
    the same way :meth:`reachy.speech.stt.Transcriber._encode_audio` does.
    """
    if audio is None:
        return b""
    if isinstance(audio, (bytes, bytearray, memoryview)):
        return bytes(audio)
    try:
        if isinstance(audio, np.ndarray) and np.issubdtype(audio.dtype, np.integer):
            array = audio
            if array.ndim == 2 and array.shape[1] > 0:
                array = array[:, min(AEC_CHANNEL, array.shape[1] - 1)]
            if array.ndim != 1 or array.size == 0:
                return b""
            return np.asarray(array).astype("<i2").tobytes()
        chunk = to_mono(audio)
        if chunk is None or chunk.size == 0:
            return b""
        return (np.clip(chunk, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
    except Exception:  # noqa: BLE001 - the tick thread must never see a raise
        logger.debug("realtime: unusable audio chunk of type %s", type(audio).__name__)
        return b""


# --------------------------------------------------------------------------- #
# The client                                                                  #
# --------------------------------------------------------------------------- #


class RealtimeTranscriber:
    """A worker-owned ``/v1/realtime`` session: audio in, endpointed words out.

    See the module docstring for the API contract, the threading split, the
    latched session-down discipline and the configuration precedence.

    Args:
        sample_rate: the mic's REAL sample rate, carried into the connect URL's
            ``input_sample_rate``. Required and never defaulted, because the
            server resamples from whatever it is told — a hard-coded 16000
            against a 48 kHz mic mis-times every VAD decision. Use
            :meth:`set_sample_rate` if the true rate is only learned later.
        url: explicit endpoint (see :func:`resolve_realtime_base_url`).
        api_key: explicit bearer key (see :func:`resolve_realtime_api_key`).
        audio_maxsize: bounded outbound queue depth.
        utterance_maxsize: bounded inbound queue depth (oldest evicted).
        connect_timeout_s: TCP connect + handshake-response budget.
        frame_timeout_s: how long a half-arrived frame may stall.
        poll_interval_s: worker poll granularity on a quiet socket.
        backoff_initial_s / backoff_max_s: reconnect backoff bounds.
        stable_after_s: session lifetime that counts as healthy (resets backoff).
        stale_after_s: queued audio older than this is dropped at connect.
        join_timeout_s: bounded worker join at :meth:`close`.
        on_utterance: optional ``(Utterance) -> None`` tap, fired on the WORKER
            thread and guarded — a raising callback is logged and swallowed.
        clock: injectable monotonic clock (utterance stamps + stability).

    Attributes:
        worker: the background thread, or ``None`` before :meth:`start`.
    """

    def __init__(
        self,
        *,
        sample_rate: int,
        url: str | None = None,
        api_key: str | None = None,
        audio_maxsize: int = DEFAULT_AUDIO_MAXSIZE,
        utterance_maxsize: int = DEFAULT_UTTERANCE_MAXSIZE,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        stable_after_s: float = DEFAULT_STABLE_AFTER_S,
        stale_after_s: float = DEFAULT_STALE_AFTER_S,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        on_utterance: Callable[[Utterance], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = resolve_realtime_base_url(url)
        self._api_key = resolve_realtime_api_key(api_key)
        self._sample_rate = max(1, int(sample_rate))
        self._connect_timeout_s = max(0.1, float(connect_timeout_s))
        self._frame_timeout_s = max(0.1, float(frame_timeout_s))
        self._poll_interval_s = max(0.001, float(poll_interval_s))
        self._backoff_initial_s = max(0.0, float(backoff_initial_s))
        self._backoff_max_s = max(self._backoff_initial_s, float(backoff_max_s))
        self._stable_after_s = max(0.0, float(stable_after_s))
        self._stale_after_s = max(0.0, float(stale_after_s))
        self._join_timeout_s = max(0.0, float(join_timeout_s))
        self._on_utterance = on_utterance
        self._clock = clock

        self._audio: queue.Queue = queue.Queue(maxsize=max(1, int(audio_maxsize)))
        self._ready: queue.Queue = queue.Queue(maxsize=max(1, int(utterance_maxsize)))

        self.worker: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._sock_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False

        # --- worker-thread state ------------------------------------------- #
        self._sock: socket.socket | None = None
        self._reader: _FrameReader | None = None
        self._connected_at = 0.0
        self._session_id: str | None = None
        self._reconfigure = False
        self._session_seq = 0
        self._utterance_seq = 0

        # --- cross-thread flags (single writer each; plain reads are atomic) - #
        self._up = False
        self._down = True
        self._down_logged = False
        self._session_event = "sess0"
        self._queue_full_logged = False

        self.submitted = 0
        self.sent = 0
        self.dropped = 0
        self.utterances = 0
        self.sessions = 0
        self.connect_failures = 0
        self.pongs_sent = 0
        self.ignored_events = 0
        #: Size in bytes of the last chunk accepted — proof that a stereo chunk
        #: was channel-SELECTED (N samples) and not interleaved (2N).
        self.last_chunk_bytes = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Spawn the session worker. Idempotent; a no-op once closed.

        Call this at COMPOSITION time: the first connect blocks for as long as
        a TCP handshake takes, and setup has no tick budget to protect.
        :meth:`submit_audio` calls it too, so a stand-alone caller needs no
        ceremony.
        """
        with self._start_lock:
            if self._closed or self.worker is not None:
                return
            self.worker = threading.Thread(target=self._run, name="realtime-session", daemon=True)
            self.worker.start()

    def close(self) -> None:
        """Stop the worker and release the socket. Idempotent, never raises."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            worker = self.worker
        self._wake.set()
        # Shut the socket DOWN (not closed) from here: it unblocks a worker
        # parked in recv/sendall without ever releasing the fd number out from
        # under it, so there is no window in which the worker could touch a
        # reused descriptor. The worker's own teardown does the close.
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        if worker is not None:
            worker.join(timeout=self._join_timeout_s)
        self._teardown_socket()

    def __enter__(self) -> "RealtimeTranscriber":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Tick-thread surface                                                #
    # ------------------------------------------------------------------ #

    def submit_audio(self, audio: Any) -> bool:
        """Queue one mic chunk for the session. **O(1), non-blocking, never raises.**

        Returns whether the chunk was accepted. See the module docstring for
        the accepted shapes and for why an absent chunk is a quiet ``False``
        while a full queue is a latched named drop.
        """
        if self._closed:
            return False
        pcm = _to_pcm16(audio)
        if not pcm:
            return False
        self.start()
        try:
            self._audio.put_nowait((self._clock(), pcm))
        except queue.Full:
            self.dropped += 1
            if not self._queue_full_logged:
                self._queue_full_logged = True
                senselog.drop(STAGE, SOURCE, self._session_event, REASON_QUEUE_FULL)
            return False
        self._queue_full_logged = False
        self.submitted += 1
        self.last_chunk_bytes = len(pcm)
        return True

    def take_utterance(self) -> Utterance | None:
        """Pop the oldest ready :class:`Utterance`, or ``None``. Never raises."""
        try:
            return self._ready.get_nowait()
        except queue.Empty:
            return None

    def set_sample_rate(self, sample_rate: int) -> None:
        """Re-negotiate the session at a new mic rate. Safe from any thread.

        The rate rides the connect URL, so changing it means a new session:
        this marks the current one for an immediate, INTENTIONAL reconnect
        (no session-down drop, no backoff — nothing failed).
        """
        rate = max(1, int(sample_rate))
        if rate == self._sample_rate:
            return
        self._sample_rate = rate
        self._reconfigure = True
        self._wake.set()

    @property
    def sample_rate(self) -> int:
        """The rate carried into the session config."""
        return self._sample_rate

    @property
    def connect_url(self) -> str:
        """The full ws(s) URL this client connects to, sample rate included."""
        return connect_url(self.url, self._sample_rate)

    @property
    def connected(self) -> bool:
        """Whether a session is established right now."""
        return self._up

    @property
    def session_down(self) -> bool:
        """Whether the client is in the LATCHED down state (no session)."""
        return self._down

    # ------------------------------------------------------------------ #
    # Worker thread                                                      #
    # ------------------------------------------------------------------ #

    def _run(self) -> None:
        """Own the socket for the life of the client. Never lets an error out."""
        attempts = 0
        while not self._closed:
            if self._sock is None:
                if attempts and not self._sleep(self._backoff_for(attempts)):
                    break
                attempts = 0 if self._attempt_connect() else attempts + 1
                continue
            try:
                self._pump()
            except _SessionLost as lost:
                stable = (self._clock() - self._connected_at) >= self._stable_after_s
                self._teardown_socket(graceful=lost.intentional)
                if lost.intentional:
                    attempts = 0
                else:
                    self._enter_down(lost.reason, lost.detail)
                    attempts = 0 if stable else attempts + 1
            except Exception:  # noqa: BLE001 - the worker must outlive any fault
                logger.warning("realtime: session pump raised", exc_info=True)
                self._teardown_socket()
                self._enter_down(REASON_STREAM_CLOSED, "unexpected pump failure")
                attempts += 1
        self._teardown_socket(graceful=True)

    def _attempt_connect(self) -> bool:
        """:meth:`_connect` under a total guard — the worker must outlive any fault.

        :meth:`_connect` handles the failure types a socket really raises
        (``OSError``/``ValueError``), but it is the ONE call in :meth:`_run` that
        used to sit outside a ``try``: anything else escaping it killed the
        session worker outright and silently, so the client stopped reconnecting
        forever while still reporting a latched ``session-down``. Same posture as
        the pump's guard below.
        """
        try:
            return self._connect()
        except Exception:  # noqa: BLE001 - a connect fault costs one attempt, not the worker
            logger.warning("realtime: connect raised", exc_info=True)
            self._teardown_socket()
            self.connect_failures += 1
            self._enter_down(REASON_CONNECT_FAILED, "unexpected connect failure")
            return False

    def _backoff_for(self, attempts: int) -> float:
        delay = self._backoff_initial_s * (2 ** max(0, attempts - 1))
        return min(self._backoff_max_s, delay)

    def _sleep(self, delay: float) -> bool:
        """Interruptible wait. Returns ``False`` when the client is closing."""
        self._wake.wait(delay)
        if self._closed:
            return False
        self._wake.clear()
        return True

    # --- connect ------------------------------------------------------- #

    def _connect(self) -> bool:
        """One connect + handshake attempt. Returns success; never raises."""
        event = self._next_session_event()
        url = self.connect_url
        parts = urlsplit(url)
        host = parts.hostname or "localhost"
        port = parts.port or (443 if parts.scheme == "wss" else 80)
        path = parts.path or wire.REALTIME_PATH
        if parts.query:
            path = f"{path}?{parts.query}"
        key = wire.make_sec_websocket_key()
        headers = {"Authorization": f"Bearer {self._api_key}"} if self._api_key else None

        sock: socket.socket | None = None
        try:
            sock = socket.create_connection((host, port), timeout=self._connect_timeout_s)
            if parts.scheme == "wss":
                context = ssl.create_default_context()
                sock = context.wrap_socket(sock, server_hostname=host)
            sock.settimeout(_SOCKET_TIMEOUT_S)
            sock.sendall(wire.build_handshake_request(parts.netloc, path, key, headers))
            reader = _FrameReader(sock, self._frame_timeout_s)
            head = reader.read_until(b"\r\n\r\n", time.monotonic() + self._connect_timeout_s)
        except (OSError, ValueError) as err:
            self._close_socket(sock)
            return self._note_connect_failure(REASON_CONNECT_FAILED, f"{type(err).__name__}: {err}")
        if head is None:
            self._close_socket(sock)
            return self._note_connect_failure(REASON_CONNECT_FAILED, "no handshake response")

        status, response_headers = wire.parse_response_head(head)
        if status != 101:
            self._close_socket(sock)
            return self._note_connect_failure(REASON_HANDSHAKE_REFUSED, f"HTTP {status}")
        if not wire.verify_accept_key(key, response_headers.get("sec-websocket-accept", "")):
            self._close_socket(sock)
            return self._note_connect_failure(REASON_HANDSHAKE_REFUSED, "bad Sec-WebSocket-Accept")

        with self._sock_lock:
            self._sock = sock
        self._reader = reader
        self._connected_at = self._clock()
        self._reconfigure = False
        self._discard_stale_audio()
        self._mark_up(event, url)
        return True

    def _note_connect_failure(self, reason: str, detail: str) -> bool:
        self.connect_failures += 1
        self._enter_down(reason, detail)
        return False

    def _mark_up(self, event: str, url: str) -> None:
        recovered = self._down_logged
        self._up = True
        self._down = False
        self._down_logged = False
        self._session_event = event
        self.sessions += 1
        senselog.stage(
            STAGE,
            SOURCE,
            event,
            f"session up url={url}{' (recovered)' if recovered else ''}",
        )

    def _enter_down(self, reason: str, detail: str = "") -> None:
        """Latch the down state, logging the cause + the state EXACTLY ONCE.

        Every subsequent failure while already down is silent — a retry loop
        that logs per attempt (or worse, per audio chunk) is the #99 journal
        flood this discipline exists to prevent.

        A session torn down by :meth:`close` is silent too. ``close()`` shuts
        the socket down under the worker on purpose (that is how a parked
        ``recv`` is unblocked), so the worker's last act is always a read
        failure — reporting it would put a session-down drop in the journal on
        every clean shutdown, which is noise, not news.
        """
        self._up = False
        self._down = True
        if self._closed or self._down_logged:
            return
        self._down_logged = True
        senselog.drop(
            STAGE, SOURCE, self._session_event, f"{reason} ({detail})" if detail else reason
        )
        senselog.drop(STAGE, SOURCE, self._session_event, REASON_SESSION_DOWN)

    def _discard_stale_audio(self) -> None:
        """Drop queued chunks older than ``stale_after_s`` before going live."""
        now = self._clock()
        kept: list[tuple[float, bytes]] = []
        while True:
            try:
                item = self._audio.get_nowait()
            except queue.Empty:
                break
            if (now - item[0]) <= self._stale_after_s:
                kept.append(item)
        for item in kept:
            try:
                self._audio.put_nowait(item)
            except queue.Full:  # pragma: no cover - defensive
                break

    # --- pump ---------------------------------------------------------- #

    def _pump(self) -> None:
        """One send/receive iteration. Raises :class:`_SessionLost` on any fault."""
        if self._reconfigure:
            self._reconfigure = False
            raise _SessionLost(REASON_STREAM_CLOSED, "sample rate changed", intentional=True)
        self._drain_audio()
        self._read_frames()

    def _drain_audio(self) -> None:
        for _ in range(_MAX_SENDS_PER_PUMP):
            try:
                _stamped, pcm = self._audio.get_nowait()
            except queue.Empty:
                return
            self._send(wire.OPCODE_TEXT, wire.build_append_event(pcm).encode("utf-8"))
            self.sent += 1

    def _send(self, opcode: int, payload: bytes) -> None:
        sock = self._sock
        if sock is None:
            raise _SessionLost(REASON_STREAM_CLOSED, "socket released")
        try:
            sock.sendall(wire.build_frame(opcode, payload, mask=True))
        except OSError as err:
            raise _SessionLost(REASON_STREAM_CLOSED, f"send failed: {err}") from err

    def _read_frames(self) -> None:
        reader = self._reader
        if reader is None:  # pragma: no cover - defensive
            raise _SessionLost(REASON_STREAM_CLOSED, "reader released")
        for index in range(_MAX_FRAMES_PER_PUMP):
            if self._closed or not self._readable(reader, first=index == 0):
                return
            try:
                _fin, opcode, payload = wire.read_frame(reader.recv_exact)
            except wire.FrameReadError as err:
                raise _SessionLost(REASON_STREAM_CLOSED, str(err)) from err
            self._handle_frame(opcode, payload)

    def _readable(self, reader: _FrameReader, *, first: bool) -> bool:
        if reader.has_pending():
            return True
        sock = self._sock
        if sock is None:
            raise _SessionLost(REASON_STREAM_CLOSED, "socket released")
        try:
            ready, _, _ = select.select([sock], [], [], self._poll_interval_s if first else 0.0)
        except (OSError, ValueError) as err:
            raise _SessionLost(REASON_STREAM_CLOSED, f"select failed: {err}") from err
        return bool(ready)

    def _handle_frame(self, opcode: int, payload: bytes) -> None:
        if opcode == wire.OPCODE_TEXT:
            event = wire.decode_event(payload)
            if event is None:
                self._drop(REASON_MALFORMED_EVENT, f"{len(payload)} bytes")
                return
            self._dispatch_event(event)
        elif opcode == wire.OPCODE_PING:
            # uvicorn pings roughly every 20 s and disconnects without a pong.
            self._send(wire.OPCODE_PONG, payload)
            self.pongs_sent += 1
        elif opcode == wire.OPCODE_PONG:
            logger.debug("realtime: pong received")
        elif opcode == wire.OPCODE_CLOSE:
            raise _SessionLost(REASON_STREAM_CLOSED, "server closed the session")
        else:
            logger.debug("realtime: ignoring unexpected opcode 0x%x", opcode)

    def _dispatch_event(self, event: dict) -> None:
        """Branch on one decoded event. Ears-only: ``response.*`` is ignored."""
        kind = event.get("type")
        if kind == TRANSCRIPTION_COMPLETED:
            self._publish(event)
        elif kind == SESSION_CREATED:
            self._session_id = _as_str(event.get("session_id"))
            config = event.get("config") if isinstance(event.get("config"), dict) else {}
            senselog.stage(
                STAGE,
                SOURCE,
                self._session_event,
                f"session.created rate={config.get('input_sample_rate')} "
                f"vad={config.get('turn_detection')}",
            )
        elif kind == SPEECH_STARTED:
            senselog.stage(STAGE, SOURCE, self._session_event, "speech started (server vad)")
        elif kind == SPEECH_STOPPED:
            senselog.stage(
                STAGE,
                SOURCE,
                self._session_event,
                f"speech stopped (server vad) reason={event.get('reason')}",
            )
        elif kind == ERROR_EVENT:
            code = _as_str(event.get("code")) or "unknown"
            reason = _ERROR_REASONS.get(code, REASON_SERVER_ERROR)
            self._drop(reason, f"code={code} {_as_str(event.get('message')) or ''}".strip())
        elif isinstance(kind, str) and kind.startswith("response."):
            # Ears-only: nothing here ever asked for a response.
            self.ignored_events += 1
            logger.debug("realtime: ignoring ears-only violation from the server: %s", kind)
        else:
            self.ignored_events += 1
            logger.debug("realtime: unhandled event type %r", kind)

    def _publish(self, event: dict) -> None:
        text = _as_str(event.get("text")) or _as_str(event.get("transcript"))
        if not text or not text.strip():
            self._drop(REASON_EMPTY_TRANSCRIPT, "transcription.completed with no text")
            return
        self._utterance_seq += 1
        tag = f"utt{self._utterance_seq}"
        utterance = Utterance(
            text=text,
            t=self._clock(),
            item_id=_as_str(event.get("item_id")),
            session_id=_as_str(event.get("session_id")) or self._session_id,
        )
        try:
            self._ready.put_nowait(utterance)
        except queue.Full:
            # A stale transcript is worth less than a fresh one: evict the
            # oldest rather than refusing the newest.
            self._drop(REASON_UTTERANCE_QUEUE_FULL, "oldest transcript evicted")
            try:
                self._ready.get_nowait()
                self._ready.put_nowait(utterance)
            except (queue.Empty, queue.Full):  # pragma: no cover - defensive
                logger.debug("realtime: could not enqueue utterance %s", tag)
                return
        self.utterances += 1
        senselog.stage(STAGE, SOURCE, tag, f"utterance chars={len(text)}")
        if self._on_utterance is not None:
            try:
                self._on_utterance(utterance)
            except Exception:  # noqa: BLE001 - a tap must not kill the session
                logger.warning("realtime: on_utterance callback raised", exc_info=True)

    # --- teardown ------------------------------------------------------ #

    def _teardown_socket(self, *, graceful: bool = False) -> None:
        """Release the session socket. Idempotent, never raises."""
        with self._sock_lock:
            sock = self._sock
            self._sock = None
        self._reader = None
        self._up = False
        if sock is None:
            return
        if graceful:
            try:
                sock.sendall(wire.build_frame(wire.OPCODE_CLOSE, b"\x03\xe8", mask=True))
            except OSError:
                pass
        self._close_socket(sock)

    @staticmethod
    def _close_socket(sock: socket.socket | None) -> None:
        if sock is None:
            return
        try:
            sock.close()
        except OSError:  # pragma: no cover - defensive
            pass

    # --- small helpers -------------------------------------------------- #

    def _next_session_event(self) -> str:
        self._session_seq += 1
        return f"sess{self._session_seq}"

    def _drop(self, reason: str, detail: str = "") -> None:
        senselog.drop(
            STAGE, SOURCE, self._session_event, f"{reason} ({detail})" if detail else reason
        )


def _as_str(value: Any) -> str | None:
    """Return *value* when it is a non-empty string, else ``None``."""
    return value if isinstance(value, str) and value else None


def build(**kwargs: Any) -> RealtimeTranscriber:
    """Convenience constructor mirroring the other engines' factory style."""
    return RealtimeTranscriber(**kwargs)
