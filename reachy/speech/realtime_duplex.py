"""The embodiment layer's ONE lobes ``/v1/realtime`` session: ears **and** mouth.

This is the duplex peer of :mod:`reachy.speech.realtime`. That module is the
symbolic runtime's hearing session — ears-only, deliberately never arming a
reply (issue #115's non-goal). This one is what the embodiment layer talks
through: the same wire, the same server-side VAD, the same never-raise
discipline, plus the half the runtime refuses — it ARMS the session with one
``response.create`` and plays the server's ``response.audio.delta`` stream out
through the robot's speaker.

One process holds exactly ONE of these. The t1 probe
(``docs/evidence/2026-08-01-probe-concurrent-realtime-sessions.md``) is why it
is allowed to exist beside the runtime's own session at all: one
transcription-only session and one armed conversational session coexisted
against the deployed gateway for ~85 s with zero cross-talk and zero errors.
That verdict has stated bounds — it did NOT test two *armed* sessions
(``TTS_VOICE_CONCURRENCY`` defaults to 1 upstream), so this client must remain
the only armed session on the box.

--------------------------------------------------------------------------
The two injected seams — write these down, the composition root builds them
--------------------------------------------------------------------------
This module sits BELOW the embodiment layer and imports nothing from
``reachy.embody`` (that package is composed on top of it, task t11). Audio in
and audio out are therefore plain injected callables, whose contracts are
exactly the shapes ``reachy/embody/media.py`` already produces::

    read_audio: Callable[[], np.ndarray | None]      # = EmbodySource.read
    play:       Callable[[bytes], None]              # = EmbodySink.play
                # called as play(pcm16_bytes, samplerate=<int>)

**``read_audio`` — the ears.** Returns ONE chunk of **mono float32 samples in
[-1, 1]** at the constructor's ``sample_rate``, or ``None`` for "nothing this
call" (not connected yet, a transient hiccup, genuine silence — never a
fault). It is called ONLY on this client's session worker thread, never on a
caller's, so it may block briefly (the tee reader's 50 ms socket timeout, a
PortAudio read); it must not block for seconds, or the server's keepalive PING
goes unanswered. It is total: a raise is caught, named, latched and survived.
An ``int16`` array or raw PCM16 ``bytes`` are also accepted (taken verbatim),
and an ``(N, C)`` array is channel-SELECTED, never flattened — see the audio
format contract below.

**``play`` — the mouth.** Called as ``play(pcm16_bytes, samplerate=...)`` with
raw **little-endian PCM16 mono bytes** at :data:`DEFAULT_OUTPUT_SAMPLE_RATE`
(24000 Hz — the gateway's TTS rate, measured in the t1 probe: 4800-byte deltas
= 2400 samples each, matching lobes-cli's ``TTS_SAMPLE_RATE``). It is called
ONLY on a dedicated playback thread, never on the session worker, because the
robot sink's daemon-HTTP route is an upload-then-play round trip lasting
seconds — charging that to the socket pump would stop the ears, starve the
keepalive, and get the session dropped. A raise, or a wedged sink, is a named
drop; it never touches the session. ``play=None`` is legal (the layer becomes
mute) and says so once, by name.

**Audio format, stated unambiguously.** In: mono float32 in [-1, 1] (or int16,
or PCM16 bytes). On the wire: PCM16 mono **little-endian**, base64, inside an
``input_audio_buffer.append`` JSON TEXT event — never a binary frame. Out:
PCM16 mono little-endian bytes at :data:`DEFAULT_OUTPUT_SAMPLE_RATE`. The
float→PCM16 coercion is :func:`reachy.speech.realtime._to_pcm16`, imported
rather than re-derived, so the documented ``(N, 2)`` hazard (a bare
``reshape(-1)`` interleaves both channels into one double-length stream a WAV
header then mislabels) stays fixed in exactly one place.

--------------------------------------------------------------------------
UNGATED, by construction (spec claim c4)
--------------------------------------------------------------------------
The runtime's :class:`~reachy.behavior.transcript_sense.TranscriptSenseDriver`
runs every heard utterance through :mod:`reachy.speech.engagement` +
:mod:`reachy.speech.name_match` — the addressed-vs-ambient admission gate. The
layer does **not**: it hears all speech, including the utterances that gate
drops. That is not a configuration choice here, it is structural — nothing in
this module's import closure reaches either gate, and
``tests/test_realtime_duplex.py`` asserts it three ways (direct imports, the
transitive closure, and ``sys.modules`` after a fresh import).

--------------------------------------------------------------------------
The send surface is CLOSED (honesty condition h13)
--------------------------------------------------------------------------
Exactly three things ever leave this client:

1. **session config** — which rides the connect URL's query params
   (``?input_sample_rate=``), not a frame at all;
2. ``input_audio_buffer.append`` — one JSON TEXT frame per mic chunk;
3. ``response.create`` — the arming frame, once per session.

Plus protocol-level PONG and CLOSE frames, which are RFC 6455 mechanics rather
than session events. **No tool call ever travels over this socket.** Tool use
rides the HTTP ``/v1/chat/completions`` lane beside it (the layer's turn
engine, task t10); if lobes later ships socket tool-calls, adopting them is a
new arc. Both halves are pinned by AST scan over this file, so a third sender
added later under any name fails the suite immediately.

--------------------------------------------------------------------------
Arming: once per session, on ``session.created``
--------------------------------------------------------------------------
The server starts DISARMED and answers nothing until it sees one
``response.create`` (lobes-cli's ``lobes/realtime/_conversation.py``); arming
is session-level and idempotent, and one arm covers every later turn (the t1
probe armed once and received two complete response lifecycles). This client
therefore arms when ``session.created`` arrives — after the server has
confirmed the session exists, which is the only ordering the probe actually
verified — and re-arms on every new session. :meth:`RealtimeDuplexSession.arm`
lets a caller request another one; it costs nothing if the session is already
armed. ``arm_on_connect=False`` degrades this client to the runtime's own
ears-only shape.

--------------------------------------------------------------------------
The mute seam: present, and OFF by default (the AEC decision)
--------------------------------------------------------------------------
``mute_during_playback`` defaults to **False**, and that default IS a product
decision: Reachy has hardware AEC against its own speakers
(``reachy/robot/audio_shape.py``'s ``AEC_CHANNEL``), so the layer keeps
hearing while it speaks — which is what makes barge-in possible at all, and
what lets the server's VAD cut a reply short. Do not "fix" this to True
because the runtime's :class:`~reachy.behavior.speech_act.SpeechActuator`
self-mutes: that path plays through a route the runtime's own AEC reference
does not cover, and this one is being validated live in task t15. If live AEC
proves insufficient, flipping this ONE flag withholds every chunk captured
while the mouth is busy — a named, latched ``self-mute`` drop per episode, and
hearing resumes by itself when playback ends. Configuration, not a code change.

--------------------------------------------------------------------------
Threading: the caller's thread never touches a socket
--------------------------------------------------------------------------
TWO background threads. The **session worker** owns the socket, the handshake,
the reconnect backoff, ``read_audio``, and every frame in both directions; the
**mouth** owns ``play``. A caller only ever touches bounded queues and plain
counters: :meth:`start` returns immediately (the blocking connect happens on
the worker), :meth:`arm`, :meth:`take_utterance`, :meth:`take_response` and
:meth:`close` are all O(1) and non-raising. This is the same split
:class:`reachy.speech.realtime.RealtimeTranscriber` and
:class:`reachy.behavior.speech_act.SpeechActuator` use, for the same reason.

--------------------------------------------------------------------------
Every failure is a NAMED drop, latched, with a backoff reconnect
--------------------------------------------------------------------------
A refused handshake, an unreachable gateway, a mid-stream close, a malformed
event, a named server error, an unreadable audio delta, a raising source or
sink: each resolves to one :func:`reachy.senselog.drop` naming its reason, and
the session state itself latches — entering the down state logs the CAUSE and
``session-down`` exactly once, however many reconnect attempts fail after it
(the #99 journal-flood discipline). A down session means the layer is simply
deaf and mute, quietly.

One reason is this module's own, and it is a DIAGNOSIS rather than an outage:
:data:`REASON_LANE_UNAVAILABLE` for an HTTP **404** on the handshake. The
gateway serves ``/v1/realtime`` only while its ``stt`` role is feasible, and
answers 404 ``role_infeasible`` otherwise (lobes-cli's browser harness,
``site/src/scripts/realtime-connection.ts``, warns *"stt lane declared off —
/v1/realtime will 404 role_infeasible"* after reading ``GET /v1/capabilities``'
``stt.feasible``). Reported as a generic refusal that reads as a flaky
gateway; named, it tells the operator to switch the lane on. The client keeps
reconnecting on the same backoff regardless — the lane can come up under us —
and the latch keeps it to one line.

Standard library plus numpy, :mod:`reachy.speech.realtime_wire` and a handful
of cited pieces of :mod:`reachy.speech.realtime` — no WebSocket dependency, no
new package (``pyproject.toml`` is untouched by this module).
"""

from __future__ import annotations

import base64
import logging
import queue
import select
import socket
import ssl
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol
from urllib.parse import urlsplit

import numpy as np

from reachy import senselog
from reachy.speech import realtime_wire as wire

# ---------------------------------------------------------------------------
# Cited from the ears-only session client, never re-derived.
#
# These two modules are the two ends of ONE wire, and this arc has already paid
# once (the t4/t6 audio-tee integration) for two agents independently deriving
# one protocol: it does not fail loudly, it produces plausible garbage. So the
# endpoint/key precedence, the connect-URL builder, the utterance record, the
# named reason strings, the buffered frame reader and the float->PCM16
# coercion all keep ONE owner — reachy.speech.realtime — and are imported here
# rather than copied. Four of them are private names in that module; importing
# a leading-underscore name from a SIBLING module of the same package is the
# lesser evil against a second copy that can drift, and it is deliberate: none
# of them is worth a public API change to a module the runtime holds live.
# ---------------------------------------------------------------------------
from reachy.speech.realtime import (
    _ERROR_REASONS,
    ERROR_EVENT,
    OPENAI_API_KEY_ENV,
    OPENAI_URL_BASE_ENV,
    REALTIME_API_KEY_ENV,
    REALTIME_URL_ENV,
    REASON_CONNECT_FAILED,
    REASON_EMPTY_TRANSCRIPT,
    REASON_HANDSHAKE_REFUSED,
    REASON_MALFORMED_EVENT,
    REASON_SERVER_ERROR,
    REASON_SESSION_DOWN,
    REASON_STREAM_CLOSED,
    REASON_STT_FORWARD_FAILED,
    REASON_UTTERANCE_QUEUE_FULL,
    REASON_VAD_UNAVAILABLE,
    SESSION_CREATED,
    SPEECH_STARTED,
    SPEECH_STOPPED,
    TRANSCRIPTION_COMPLETED,
    Utterance,
    _as_str,
    _FrameReader,
    _to_pcm16,
    connect_url,
    resolve_realtime_api_key,
    resolve_realtime_base_url,
)

#: The module's own surface, plus the names it deliberately RE-EXPORTS from
#: :mod:`reachy.speech.realtime` so a caller (or a test) can reach the whole
#: duplex vocabulary through one import without either module owning two copies
#: of a string. Declaring them here is also what says "these imports are a
#: re-export, not a leftover".
__all__ = [
    "DEFAULT_OUTPUT_SAMPLE_RATE",
    "PlaySink",
    "ReadAudio",
    "RealtimeDuplexSession",
    "Response",
    "Utterance",
    "build",
    "connect_url",
    "resolve_realtime_api_key",
    "resolve_realtime_base_url",
    # configuration env names (owned by reachy.speech.realtime)
    "OPENAI_API_KEY_ENV",
    "OPENAI_URL_BASE_ENV",
    "REALTIME_API_KEY_ENV",
    "REALTIME_URL_ENV",
    # named drop reasons — this module's own, and the shared ones
    "REASON_CONNECT_FAILED",
    "REASON_EMPTY_TRANSCRIPT",
    "REASON_HANDSHAKE_REFUSED",
    "REASON_LANE_UNAVAILABLE",
    "REASON_MALFORMED_AUDIO_DELTA",
    "REASON_MALFORMED_EVENT",
    "REASON_NO_PLAYBACK_SINK",
    "REASON_PLAYBACK_FAILED",
    "REASON_PLAYBACK_QUEUE_FULL",
    "REASON_RESPONSE_INTERRUPTED",
    "REASON_RESPONSE_QUEUE_FULL",
    "REASON_RESPONSE_TOO_LONG",
    "REASON_SELF_MUTE",
    "REASON_SERVER_ERROR",
    "REASON_SESSION_DOWN",
    "REASON_SOURCE_FAILED",
    "REASON_STREAM_CLOSED",
    "REASON_STT_FORWARD_FAILED",
    "REASON_UTTERANCE_QUEUE_FULL",
    "REASON_VAD_UNAVAILABLE",
]

logger = logging.getLogger(__name__)

#: ``[SENSE stage=duplex source=embody event=<id>]`` — this module's identity,
#: deliberately distinct from the runtime session's ``stage=realtime
#: source=speech`` so one journal can be split by which ear heard what.
STAGE = "duplex"
SOURCE = "embody"

#: Thread names, exported so a test (and a stack dump) can name them.
WORKER_THREAD_NAME = "realtime-duplex-session"
PLAYBACK_THREAD_NAME = "realtime-duplex-mouth"

# --------------------------------------------------------------------------- #
# The response.* half of the wire (the runtime session ignores all of these)   #
# --------------------------------------------------------------------------- #

RESPONSE_CREATED = "response.created"
RESPONSE_TEXT_DONE = "response.text.done"
RESPONSE_AUDIO_DELTA = "response.audio.delta"
RESPONSE_DONE = "response.done"
RESPONSE_INTERRUPTED = "response.interrupted"

# --------------------------------------------------------------------------- #
# Named drop reasons that are this module's own (the rest are cited above)     #
# --------------------------------------------------------------------------- #

#: HTTP 404 on the handshake: the gateway's ``stt`` lane is declared off, so
#: ``/v1/realtime`` is not served at all. An operator fix, not an outage.
REASON_LANE_UNAVAILABLE = "realtime-lane-unavailable"
#: A ``response.audio.delta`` whose ``delta`` field is not decodable base64.
REASON_MALFORMED_AUDIO_DELTA = "malformed-audio-delta"
#: One reply's audio exceeded ``max_response_bytes``; the tail is discarded.
REASON_RESPONSE_TOO_LONG = "response-too-long"
#: A barge-in cut the reply short, so it is never spoken (see the handler).
REASON_RESPONSE_INTERRUPTED = "response-interrupted"
#: A reply arrived faster than the caller drained it (oldest evicted).
REASON_RESPONSE_QUEUE_FULL = "response-queue-full"
#: There is audio to speak and no ``play`` sink was injected.
REASON_NO_PLAYBACK_SINK = "no-playback-sink"
#: The mouth is still busy with an earlier reply.
REASON_PLAYBACK_QUEUE_FULL = "playback-queue-full"
#: The injected ``play`` raised, or the speaker is gone.
REASON_PLAYBACK_FAILED = "playback-failed"
#: ``read_audio`` raised. Latched: a broken source costs one line, not one per read.
REASON_SOURCE_FAILED = "audio-source-failed"
#: ``mute_during_playback`` is on and the mouth is busy — the AEC fallback.
REASON_SELF_MUTE = "self-mute"

# --------------------------------------------------------------------------- #
# Defaults                                                                    #
# --------------------------------------------------------------------------- #

#: The gateway's TTS output rate. Measured on the deployed stack in the t1
#: probe (4800-byte deltas = 2400 PCM16 samples each; 68 deltas = 6.80 s of
#: audio) and matching lobes-cli's own ``TTS_SAMPLE_RATE``. It is NOT announced
#: on the wire — ``session.created``'s config describes the INPUT — so it is a
#: constant here, overridable per deployment.
DEFAULT_OUTPUT_SAMPLE_RATE = 24000

#: Inbound queue depths. Both small: a stale utterance or reply is worth less
#: than a fresh one, so the OLDEST is evicted on overflow.
DEFAULT_UTTERANCE_MAXSIZE = 8
DEFAULT_RESPONSE_MAXSIZE = 8
#: The mouth queue. Depth 2 (the ``SpeechActuator`` figure): one playing, one
#: waiting; a third means the layer is talking far faster than it can speak.
DEFAULT_PLAYBACK_MAXSIZE = 2

#: One reply's accumulated audio cap: 60 s of PCM16 at 24 kHz. A runaway
#: server cannot make this process grow without bound.
DEFAULT_MAX_RESPONSE_BYTES = DEFAULT_OUTPUT_SAMPLE_RATE * 2 * 60

#: How many chunks the connect-time backlog drain may discard (~1.3 s at 20 ms
#: chunks). It stops early the moment the source says "nothing ready", so a
#: source with no backlog loses nothing.
DEFAULT_STALE_DRAIN_MAX_CHUNKS = 64
#: Chunks sent per pump iteration, so the send side cannot starve the read side.
_MAX_CHUNKS_PER_PUMP = 8
_MAX_FRAMES_PER_PUMP = 32

DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_FRAME_TIMEOUT_S = 5.0
DEFAULT_POLL_INTERVAL_S = 0.01
DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
DEFAULT_STABLE_AFTER_S = 10.0
DEFAULT_JOIN_TIMEOUT_S = 2.0
#: Socket-level timeout: the granularity at which the worker re-checks close().
_SOCKET_TIMEOUT_S = 0.2
#: How long the mouth thread parks between queue polls (bounds close()).
_PLAYBACK_POLL_S = 0.05


class PlaySink(Protocol):
    """The ``play`` seam: raw PCM16 mono LE bytes at an explicit rate.

    Structurally identical to ``reachy.embody.media.EmbodySink.play``, which is
    what the composition root passes. Declared as a Protocol rather than a bare
    ``Callable`` so the keyword-only ``samplerate`` is part of the written
    contract instead of a convention.
    """

    def __call__(self, pcm16_bytes: bytes, *, samplerate: int) -> None: ...  # pragma: no cover


#: The ``read_audio`` seam: one mono float32 chunk in [-1, 1], or ``None``.
ReadAudio = Callable[[], "np.ndarray | None"]


@dataclass(frozen=True)
class Response:
    """One complete spoken reply from the server.

    Attributes:
        response_id: the server's opaque reply id, or ``None``.
        text: the reply's full text (``response.text.done``), ``""`` if none.
        audio: the reassembled PCM16 mono LE audio, contiguous and in order.
        samplerate: the rate *audio* is at (:data:`DEFAULT_OUTPUT_SAMPLE_RATE`).
        t: the monotonic instant the reply completed.
        interrupted: a barge-in cut it short — it was NOT played (see below).
        item_id: the transcribed turn this reply answers, when the server said.
        session_id: the server's opaque session id, or ``None``.
    """

    response_id: str | None
    text: str
    audio: bytes
    samplerate: int
    t: float
    interrupted: bool
    item_id: str | None = None
    session_id: str | None = None


@dataclass
class _PendingResponse:
    """One in-flight reply being accumulated across ``response.*`` events."""

    response_id: str | None
    item_id: str | None = None
    text: str = ""
    audio: bytearray = field(default_factory=bytearray)
    overflowed: bool = False


class _SessionLost(Exception):
    """Internal: this session ended. Never escapes the worker thread."""

    def __init__(self, reason: str, detail: str = "") -> None:
        super().__init__(f"{reason} {detail}".strip())
        self.reason = reason
        self.detail = detail


class RealtimeDuplexSession:
    """One armed ``/v1/realtime`` session: audio in, words + spoken replies out.

    See the module docstring for the seam contracts, the closed send surface,
    the ungated-by-construction property, the mute seam and the threading
    split.

    Args:
        read_audio: the ears seam — ``() -> mono float32 chunk | None``.
        sample_rate: the rate *read_audio* produces, carried into the connect
            URL's ``input_sample_rate``. Required and never defaulted: the
            server resamples from whatever it is told, so a wrong value
            mis-times every VAD decision. There is no ``set_sample_rate`` here
            (unlike the runtime's session, which learns its mic rate late) —
            the layer's source normalises to a configured rate it knows at
            construction (``reachy.embody.media.EmbodySource.sample_rate``).
        play: the mouth seam — ``play(pcm16_bytes, samplerate=...)``. ``None``
            means the layer is mute, said once by name.
        output_sample_rate: the rate handed to *play*.
        mute_during_playback: the AEC fallback. **OFF by default** — see the
            module docstring; flipping it is configuration, not code.
        url / api_key: explicit endpoint + bearer, else the shared
            ``REACHY_REALTIME_*`` / ``REACHY_OPENAI_*`` precedence.
        arm_on_connect: send ``response.create`` on ``session.created``.
        utterance_maxsize / response_maxsize / playback_maxsize: bounded queues.
        max_response_bytes: cap on ONE reply's accumulated audio.
        stale_drain_max_chunks: connect-time backlog drain bound.
        connect_timeout_s / frame_timeout_s / poll_interval_s: socket budgets.
        backoff_initial_s / backoff_max_s / stable_after_s: reconnect policy.
        join_timeout_s: bounded thread join at :meth:`close`.
        on_utterance / on_response: optional taps, fired on the WORKER thread
            and guarded — a raising callback is logged and swallowed.
        clock: injectable monotonic clock.

    Attributes:
        worker: the session thread, or ``None`` before :meth:`start`.
        mouth: the playback thread, or ``None`` before :meth:`start`.
    """

    def __init__(
        self,
        *,
        read_audio: ReadAudio,
        sample_rate: int,
        play: PlaySink | None = None,
        output_sample_rate: int = DEFAULT_OUTPUT_SAMPLE_RATE,
        mute_during_playback: bool = False,
        url: str | None = None,
        api_key: str | None = None,
        arm_on_connect: bool = True,
        utterance_maxsize: int = DEFAULT_UTTERANCE_MAXSIZE,
        response_maxsize: int = DEFAULT_RESPONSE_MAXSIZE,
        playback_maxsize: int = DEFAULT_PLAYBACK_MAXSIZE,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        stale_drain_max_chunks: int = DEFAULT_STALE_DRAIN_MAX_CHUNKS,
        connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S,
        frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S,
        poll_interval_s: float = DEFAULT_POLL_INTERVAL_S,
        backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S,
        backoff_max_s: float = DEFAULT_BACKOFF_MAX_S,
        stable_after_s: float = DEFAULT_STABLE_AFTER_S,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        on_utterance: Callable[[Utterance], None] | None = None,
        on_response: Callable[[Response], None] | None = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.url = resolve_realtime_base_url(url)
        self._api_key = resolve_realtime_api_key(api_key)
        self._read_audio = read_audio
        self._play = play
        self._sample_rate = max(1, int(sample_rate))
        self._output_sample_rate = max(1, int(output_sample_rate))
        self._mute_during_playback = bool(mute_during_playback)
        self._arm_on_connect = bool(arm_on_connect)
        self._max_response_bytes = max(0, int(max_response_bytes))
        self._stale_drain_max_chunks = max(0, int(stale_drain_max_chunks))
        self._connect_timeout_s = max(0.1, float(connect_timeout_s))
        self._frame_timeout_s = max(0.1, float(frame_timeout_s))
        self._poll_interval_s = max(0.001, float(poll_interval_s))
        self._backoff_initial_s = max(0.0, float(backoff_initial_s))
        self._backoff_max_s = max(self._backoff_initial_s, float(backoff_max_s))
        self._stable_after_s = max(0.0, float(stable_after_s))
        self._join_timeout_s = max(0.0, float(join_timeout_s))
        self._on_utterance = on_utterance
        self._on_response = on_response
        self._clock = clock

        self._utterances: queue.Queue = queue.Queue(maxsize=max(1, int(utterance_maxsize)))
        self._responses: queue.Queue = queue.Queue(maxsize=max(1, int(response_maxsize)))
        self._playback: queue.Queue = queue.Queue(maxsize=max(1, int(playback_maxsize)))

        self.worker: threading.Thread | None = None
        self.mouth: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._sock_lock = threading.Lock()
        self._wake = threading.Event()
        self._closed = False

        # --- worker-thread state ------------------------------------------- #
        self._sock: socket.socket | None = None
        self._reader: _FrameReader | None = None
        self._connected_at = 0.0
        self._session_id: str | None = None
        self._pending: dict[str, _PendingResponse] = {}
        self._session_seq = 0
        self._utterance_seq = 0
        self._source_failed_logged = False
        #: Overflow reasons already reported for the CURRENT episode. `_offer`
        #: adds on the first eviction and clears on the first clean enqueue, so
        #: a persistently-full sink costs ONE line per episode rather than one
        #: per chunk — the same discipline the rest of this module's failures
        #: already follow, and the defect class the runtime's tick-overrun
        #: summary exists to avoid (69,696 lines measured, once).
        self._overflow_logged: set[str] = set()

        # --- cross-thread flags (single writer each; plain reads are atomic) - #
        self._arm_pending = False
        self._up = False
        self._down = True
        self._down_logged = False
        self._session_event = "sess0"
        self._speaking = False
        self._muted_logged = False
        self._no_sink_logged = False
        self._playback_full_logged = False
        self._playback_failed_logged = False
        self._lane_unavailable = False

        self.sessions = 0
        self.connect_failures = 0
        self.chunks_sent = 0
        self.bytes_sent = 0
        self.utterances = 0
        self.responses = 0
        self.response_audio_bytes = 0
        self.arms_sent = 0
        self.pongs_sent = 0
        self.ignored_events = 0
        self.muted_chunks = 0
        self.stale_chunks_discarded = 0
        self.played = 0
        self.playback_failures = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Spawn the session worker and the mouth. Idempotent; no-op once closed.

        Returns IMMEDIATELY: the blocking connect happens on the worker, so a
        composition root never waits on a gateway that is not up yet.
        """
        with self._start_lock:
            if self._closed or self.worker is not None:
                return
            self.worker = threading.Thread(target=self._run, name=WORKER_THREAD_NAME, daemon=True)
            self.mouth = threading.Thread(
                target=self._playback_loop, name=PLAYBACK_THREAD_NAME, daemon=True
            )
            self.worker.start()
            self.mouth.start()

    def close(self) -> None:
        """Stop both threads and release the socket. Idempotent, never raises."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            worker, mouth = self.worker, self.mouth
        self._wake.set()
        # Shut the socket DOWN (not closed) from here: it unblocks a worker
        # parked in recv/sendall without releasing the fd number out from under
        # it. The worker's own teardown does the close.
        with self._sock_lock:
            if self._sock is not None:
                try:
                    self._sock.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
        for thread in (worker, mouth):
            if thread is not None:
                thread.join(timeout=self._join_timeout_s)
        self._teardown_socket()

    def __enter__(self) -> "RealtimeDuplexSession":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Caller-thread surface — all O(1), all non-raising                  #
    # ------------------------------------------------------------------ #

    def arm(self) -> None:
        """Request one more ``response.create``. Safe from any thread, never raises.

        Arming is idempotent server-side, so this is always safe; the worker
        sends it on its next pump. Sessions arm themselves on
        ``session.created`` unless ``arm_on_connect=False``.
        """
        self._arm_pending = True
        self._wake.set()

    def take_utterance(self) -> Utterance | None:
        """Pop the oldest heard utterance, or ``None``. **Ungated** — see c4."""
        try:
            return self._utterances.get_nowait()
        except queue.Empty:
            return None

    def take_response(self) -> Response | None:
        """Pop the oldest completed reply, or ``None``."""
        try:
            return self._responses.get_nowait()
        except queue.Empty:
            return None

    @property
    def sample_rate(self) -> int:
        """The rate carried into the session config."""
        return self._sample_rate

    @property
    def output_sample_rate(self) -> int:
        """The rate handed to the ``play`` seam."""
        return self._output_sample_rate

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

    @property
    def lane_unavailable(self) -> bool:
        """Whether the last refusal was a 404 (the gateway's stt lane is off)."""
        return self._lane_unavailable

    @property
    def speaking(self) -> bool:
        """Whether the mouth is busy right now (what the mute seam keys on)."""
        return self._speaking

    @property
    def muted(self) -> bool:
        """Whether audio is being withheld this instant (always ``False`` by default)."""
        return self._mute_during_playback and (self._speaking or not self._playback.empty())

    # ------------------------------------------------------------------ #
    # Session worker                                                     #
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
            attempts = self._pump_once(attempts)
        self._teardown_socket(graceful=True)

    def _pump_once(self, attempts: int) -> int:
        try:
            self._pump()
        except _SessionLost as lost:
            stable = (self._clock() - self._connected_at) >= self._stable_after_s
            self._teardown_socket()
            self._enter_down(lost.reason, lost.detail)
            return 0 if stable else attempts + 1
        except Exception:  # noqa: BLE001 - the worker must outlive any fault
            logger.warning("duplex: session pump raised", exc_info=True)
            self._teardown_socket()
            self._enter_down(REASON_STREAM_CLOSED, "unexpected pump failure")
            return attempts + 1
        return attempts

    def _attempt_connect(self) -> bool:
        """:meth:`_connect` under a total guard — a fault costs one attempt, not the worker."""
        try:
            return self._connect()
        except Exception:  # noqa: BLE001
            logger.warning("duplex: connect raised", exc_info=True)
            self._teardown_socket()
            self.connect_failures += 1
            self._enter_down(REASON_CONNECT_FAILED, "unexpected connect failure")
            return False

    def _backoff_for(self, attempts: int) -> float:
        return min(self._backoff_max_s, self._backoff_initial_s * (2 ** max(0, attempts - 1)))

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
            return self._note_refusal(status)
        if not wire.verify_accept_key(key, response_headers.get("sec-websocket-accept", "")):
            self._close_socket(sock)
            return self._note_connect_failure(REASON_HANDSHAKE_REFUSED, "bad Sec-WebSocket-Accept")

        with self._sock_lock:
            self._sock = sock
        self._reader = reader
        self._connected_at = self._clock()
        self._lane_unavailable = False
        self._pending.clear()
        self._drain_stale_source()
        self._mark_up(event, url)
        return True

    def _note_refusal(self, status: int) -> bool:
        """Name a non-101 handshake response — 404 is its own DIAGNOSIS.

        Every other status is the generic refusal: something answered and said
        no, and retrying is the right move. A 404 says the route is not served
        because the gateway's ``stt`` role is infeasible — the fix is operator
        configuration, so the log has to say so rather than read as a flaky
        gateway. Retrying stays right either way (the lane can be switched on
        while we run), and the latch keeps this to one line.
        """
        if status == 404:
            self._lane_unavailable = True
            return self._note_connect_failure(
                REASON_LANE_UNAVAILABLE,
                "HTTP 404 - the gateway's stt lane is likely declared off; "
                "check GET /v1/capabilities for stt.feasible",
            )
        return self._note_connect_failure(REASON_HANDSHAKE_REFUSED, f"HTTP {status}")

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
            STAGE, SOURCE, event, f"session up url={url}{' (recovered)' if recovered else ''}"
        )

    def _enter_down(self, reason: str, detail: str = "") -> None:
        """Latch the down state, logging the CAUSE and the state EXACTLY ONCE.

        Every later failure while already down is silent — a retry loop that
        logs per attempt is the #99 journal flood this exists to prevent. A
        session torn down by :meth:`close` is silent too: ``close()`` shuts the
        socket down under the worker on purpose, so the worker's last act is
        always a read failure, and reporting it would put a drop in the journal
        on every clean shutdown.
        """
        self._up = False
        self._down = True
        if self._closed or self._down_logged:
            return
        self._down_logged = True
        self._drop(reason, detail)
        senselog.drop(STAGE, SOURCE, self._session_event, REASON_SESSION_DOWN)

    def _drain_stale_source(self) -> None:
        """Discard whatever the source has ALREADY buffered, before going live.

        Replaying seconds-old audio into a server-side VAD manufactures
        utterances nobody spoke — the same discipline
        :class:`reachy.speech.realtime.RealtimeTranscriber` applies to its
        queue and :class:`reachy.behavior.audio_pump.AudioPump` applies to the
        SDK appsink, adapted to a PULL source: drain only what is ready NOW
        (the first ``None`` ends it), so a source with no backlog loses nothing
        and a source holding one loses exactly the stale part.
        """
        for _ in range(self._stale_drain_max_chunks):
            chunk = self._read_source()
            if chunk is None:
                return
            self.stale_chunks_discarded += 1

    # --- pump ---------------------------------------------------------- #

    def _pump(self) -> None:
        """One send/receive iteration. Raises :class:`_SessionLost` on any fault."""
        self._send_pending_arm()
        self._pump_audio()
        self._read_frames()

    def _send_pending_arm(self) -> None:
        if not self._arm_pending:
            return
        self._arm_pending = False
        self._send(wire.OPCODE_TEXT, wire.build_response_create_event().encode("utf-8"))
        self.arms_sent += 1
        senselog.stage(STAGE, SOURCE, self._session_event, "armed (response.create)")

    def _pump_audio(self) -> None:
        """Pull from the source and append to the session. Bounded per iteration."""
        for _ in range(_MAX_CHUNKS_PER_PUMP):
            chunk = self._read_source()
            if chunk is None:
                return
            if self.muted:
                # Keep DRAINING while muted (so the source cannot back up), but
                # nothing reaches the wire. One line per mute episode.
                self.muted_chunks += 1
                if not self._muted_logged:
                    self._muted_logged = True
                    senselog.drop(STAGE, SOURCE, self._session_event, REASON_SELF_MUTE)
                continue
            self._muted_logged = False
            pcm = _to_pcm16(chunk)
            if not pcm:
                continue
            self._send(wire.OPCODE_TEXT, wire.build_append_event(pcm).encode("utf-8"))
            self.chunks_sent += 1
            self.bytes_sent += len(pcm)

    def _read_source(self) -> Any:
        """One guarded ``read_audio()`` call. Never raises; a fault is latched."""
        try:
            chunk = self._read_audio()
        except Exception:  # noqa: BLE001 - a broken source must not end the session
            if not self._source_failed_logged:
                self._source_failed_logged = True
                self._drop(REASON_SOURCE_FAILED, "read_audio raised")
                logger.warning("duplex: read_audio raised", exc_info=True)
            return None
        self._source_failed_logged = False
        return chunk

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
            logger.debug("duplex: pong received")
        elif opcode == wire.OPCODE_CLOSE:
            raise _SessionLost(REASON_STREAM_CLOSED, "server closed the session")
        else:
            logger.debug("duplex: ignoring unexpected opcode 0x%x", opcode)

    # --- events -------------------------------------------------------- #

    def _dispatch_event(self, event: dict) -> None:
        """Branch on one decoded event. Both halves of the duplex live here."""
        kind = event.get("type")
        if kind == TRANSCRIPTION_COMPLETED:
            self._publish_utterance(event)
        elif kind == RESPONSE_AUDIO_DELTA:
            self._accumulate_audio(event)
        elif kind == RESPONSE_TEXT_DONE:
            pending = self._pending_for(event)
            pending.text = _as_str(event.get("text")) or pending.text
        elif kind == RESPONSE_CREATED:
            pending = self._pending_for(event)
            senselog.stage(
                STAGE, SOURCE, self._session_event, f"response started id={pending.response_id}"
            )
        elif kind == RESPONSE_DONE:
            self._finish_response(event, interrupted=False)
        elif kind == RESPONSE_INTERRUPTED:
            self._finish_response(event, interrupted=True)
        elif kind == SESSION_CREATED:
            self._on_session_created(event)
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
        else:
            self.ignored_events += 1
            logger.debug("duplex: unhandled event type %r", kind)

    def _on_session_created(self, event: dict) -> None:
        self._session_id = _as_str(event.get("session_id"))
        config = event.get("config") if isinstance(event.get("config"), dict) else {}
        senselog.stage(
            STAGE,
            SOURCE,
            self._session_event,
            f"session.created rate={config.get('input_sample_rate')} "
            f"vad={config.get('turn_detection')}",
        )
        if self._arm_on_connect:
            self._arm_pending = True

    def _publish_utterance(self, event: dict) -> None:
        """Publish one heard utterance. **No engagement gate** (spec claim c4)."""
        text = _as_str(event.get("text")) or _as_str(event.get("transcript"))
        if not text or not text.strip():
            self._drop(REASON_EMPTY_TRANSCRIPT, "transcription.completed with no text")
            return
        self._utterance_seq += 1
        utterance = Utterance(
            text=text,
            t=self._clock(),
            item_id=_as_str(event.get("item_id")),
            session_id=_as_str(event.get("session_id")) or self._session_id,
        )
        if not self._offer(self._utterances, utterance, REASON_UTTERANCE_QUEUE_FULL):
            return
        self.utterances += 1
        senselog.stage(STAGE, SOURCE, f"utt{self._utterance_seq}", f"utterance chars={len(text)}")
        self._tap(self._on_utterance, utterance, "on_utterance")

    def _pending_for(self, event: dict) -> _PendingResponse:
        """The in-flight reply this event belongs to, created on first sight.

        Keyed by ``response_id`` and tolerant of its absence: a delta that
        arrives before (or without) ``response.created`` still lands in the
        right place rather than being dropped for a missing envelope field.
        """
        response_id = _as_str(event.get("response_id"))
        pending = self._pending.get(response_id or "")
        if pending is None:
            pending = _PendingResponse(response_id=response_id)
            self._pending[response_id or ""] = pending
        if pending.item_id is None:
            pending.item_id = _as_str(event.get("item_id"))
        return pending

    def _accumulate_audio(self, event: dict) -> None:
        """Append one base64 PCM16 delta to its reply, bounded."""
        pending = self._pending_for(event)
        raw = event.get("delta")
        if not isinstance(raw, str) or not raw:
            self._drop(REASON_MALFORMED_AUDIO_DELTA, "delta missing or not a string")
            return
        try:
            pcm = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            self._drop(REASON_MALFORMED_AUDIO_DELTA, f"{len(raw)} base64 chars")
            return
        if len(pending.audio) + len(pcm) > self._max_response_bytes:
            if not pending.overflowed:
                pending.overflowed = True
                self._drop(REASON_RESPONSE_TOO_LONG, f"over {self._max_response_bytes} bytes")
            return
        pending.audio.extend(pcm)

    def _finish_response(self, event: dict, *, interrupted: bool) -> None:
        """Complete one reply: publish it, and speak it unless it was cut short."""
        pending = self._pending_for(event)
        self._pending.pop(pending.response_id or "", None)
        audio = bytes(pending.audio)
        response = Response(
            response_id=pending.response_id,
            text=pending.text,
            audio=audio,
            samplerate=self._output_sample_rate,
            t=self._clock(),
            interrupted=interrupted,
            item_id=pending.item_id,
            session_id=self._session_id,
        )
        if not self._offer(self._responses, response, REASON_RESPONSE_QUEUE_FULL):
            return
        self.responses += 1
        self.response_audio_bytes += len(audio)
        senselog.stage(
            STAGE,
            SOURCE,
            self._session_event,
            f"response {'interrupted' if interrupted else 'done'} "
            f"id={pending.response_id} chars={len(pending.text)} audio={len(audio)}B",
        )
        if interrupted:
            # A barge-in means the human started talking again. Speaking the
            # truncated reply now would talk over them, so it is deliberately
            # never played — the record still carries the audio and says why.
            self._drop(REASON_RESPONSE_INTERRUPTED, "truncated reply is not spoken")
        elif audio:
            self._enqueue_playback(audio)
        self._tap(self._on_response, response, "on_response")

    def _enqueue_playback(self, audio: bytes) -> None:
        """Hand one finished reply to the mouth. O(1); never blocks the session."""
        if self._play is None:
            if not self._no_sink_logged:
                self._no_sink_logged = True
                self._drop(REASON_NO_PLAYBACK_SINK, f"{len(audio)} bytes not spoken")
            return
        try:
            self._playback.put_nowait(audio)
        except queue.Full:
            if not self._playback_full_logged:
                self._playback_full_logged = True
                self._drop(REASON_PLAYBACK_QUEUE_FULL, f"{len(audio)} bytes not spoken")
            return
        self._playback_full_logged = False

    # --- the mouth ------------------------------------------------------ #

    def _playback_loop(self) -> None:
        """Own the ``play`` seam. A blocking sink can never reach the session.

        Polls rather than blocking forever on the queue, so :meth:`close` needs
        no sentinel value to wake it: the loop notices ``_closed`` within one
        poll interval. (A sink already inside ``play`` finishes on its own or is
        left to the bounded join — it is a daemon thread either way.)
        """
        while not self._closed:
            try:
                audio = self._playback.get(timeout=_PLAYBACK_POLL_S)
            except queue.Empty:
                continue
            self._speak(audio)

    def _speak(self, audio: bytes) -> None:
        play = self._play
        if play is None:  # pragma: no cover - never enqueued without a sink
            return
        self._speaking = True
        try:
            play(audio, samplerate=self._output_sample_rate)
        except Exception:  # noqa: BLE001 - a dead speaker must not end the session
            self.playback_failures += 1
            if not self._playback_failed_logged:
                self._playback_failed_logged = True
                self._drop(REASON_PLAYBACK_FAILED, f"{len(audio)} bytes")
                logger.warning("duplex: playback sink raised", exc_info=True)
        else:
            self.played += 1
            self._playback_failed_logged = False
        finally:
            self._speaking = False

    # --- small helpers -------------------------------------------------- #

    def _offer(self, sink: queue.Queue, item: Any, reason: str) -> bool:
        """Enqueue *item*, evicting the OLDEST on overflow (stale is worth less).

        The overflow report is LATCHED per episode. A consumer that stops
        draining does not fail once — it fails on every single chunk, and an
        unlatched line there would bury the journal exactly as the runtime's
        tick overruns once did. The latch clears on the first clean enqueue, so
        a sink that recovers and fails again is reported again.
        """
        try:
            sink.put_nowait(item)
            self._overflow_logged.discard(reason)
            return True
        except queue.Full:
            if reason not in self._overflow_logged:
                self._overflow_logged.add(reason)
                self._drop(reason, "oldest evicted (latched until this sink drains)")
        try:
            sink.get_nowait()
            sink.put_nowait(item)
            return True
        except (queue.Empty, queue.Full):  # pragma: no cover - defensive
            return False

    def _tap(self, callback: Callable[[Any], None] | None, item: Any, name: str) -> None:
        if callback is None:
            return
        try:
            callback(item)
        except Exception:  # noqa: BLE001 - a tap must not kill the session
            logger.warning("duplex: %s callback raised", name, exc_info=True)

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

    def _next_session_event(self) -> str:
        self._session_seq += 1
        return f"sess{self._session_seq}"

    def _drop(self, reason: str, detail: str = "") -> None:
        senselog.drop(
            STAGE, SOURCE, self._session_event, f"{reason} ({detail})" if detail else reason
        )


def build(**kwargs: Any) -> RealtimeDuplexSession:
    """Convenience constructor mirroring the other engines' factory style."""
    return RealtimeDuplexSession(**kwargs)
