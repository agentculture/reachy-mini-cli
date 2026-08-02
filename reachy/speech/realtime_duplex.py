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
= 2400 samples each, matching lobes-cli's ``TTS_SAMPLE_RATE``). One call is
ONE CHUNK of a reply, not a whole reply (see the next section). It is called
ONLY on a dedicated playback thread, never on the session worker, because the
robot sink's daemon-HTTP route is an upload-then-play round trip lasting
seconds — charging that to the socket pump would stop the ears, starve the
keepalive, and get the session dropped. A raise, or a wedged sink, is a named
drop; it never touches the session. ``play=None`` is legal (the layer becomes
mute) and says so once, by name.

--------------------------------------------------------------------------
Playback is CHUNKED, so it is CANCELLABLE (issue #151 item 3, spec c12)
--------------------------------------------------------------------------
This client used to accumulate every ``response.audio.delta`` and hand the
whole reply to ``play`` once, on ``response.done``. That made interruption
impossible in the one case that matters: the robot's sink has no stop handle
and no "what is left to play" — on the robot profile ``play`` is a daemon-HTTP
upload-then-play round trip, and once the daemon owns the clip the room is
going to hear all of it. So a human interjecting over an ALREADY-AUDIBLE reply
could not cut it; the live journal shows exactly that, an interruption landing
as ``response interrupted chars=0 audio=0B`` while the speaker talked on.

The fix needs nothing new from the daemon or the gateway. A reply is played as
SEVERAL chunks with the queue between them, so **"stop talking" becomes "do
not send the next chunk"** — an impossible problem (cancel audio in flight)
turned into a trivial one (empty a queue). Concretely:

* ``_accumulate_audio`` flushes a chunk group the moment a reply has
  :attr:`Limits.playback_chunk_bytes` of audio pending, so speech starts while
  the reply is still arriving; ``_finish_response`` flushes the remainder;
* :meth:`RealtimeDuplexSession.cancel_playback` (any thread, O(1), never
  raises) bumps a GENERATION counter and drains the mouth queue. Every queued
  chunk is skipped, every chunk of that reply still to ARRIVE is refused, and
  the mouth re-checks the generation between chunks — so the cut lands within
  ONE chunk boundary. A chunk already inside ``play`` cannot be recalled; that
  is the whole cost, and it is why the chunk size is a bound rather than a
  constant.
* ``response.interrupted`` (the server's own barge-in) takes the same path.

**Sizing is a genuine tradeoff, and the numbers here are a starting point.**
Bigger chunks mean fewer daemon round trips and a smaller chance of an audible
seam, but a LONGER cut latency (the room keeps hearing up to one chunk after
the human speaks). Smaller chunks cut faster and start sooner, but pay a
round trip per chunk and can leave an audible gap if that round trip exceeds
the chunk's own duration. The shipped pair —
:data:`DEFAULT_PLAYBACK_CHUNK_BYTES` (1.0 s) after a smaller
:data:`DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES` (0.4 s, so the robot starts
speaking sooner) — is chosen against the spec's "stops within roughly one
chunk" and the operator's accepted "minor gap", NOT against a measurement:
the per-chunk daemon ``/media/play`` round trip is unmeasured (plan task t1
measures it, and may retune both values). Both are :class:`Limits` fields, so
retuning is one number in one place and every test injects its own.

**What the room actually heard is MEASURED, not assumed.**
:class:`PlaybackProgress` (via :meth:`RealtimeDuplexSession.playback_progress`,
and returned by :meth:`~RealtimeDuplexSession.cancel_playback`) reports, per
reply, the bytes the sink CONFIRMED (returned from), the bytes still inside a
``play`` call at the moment of the cut, and the bytes skipped. That is the
authority for the said/unsaid split one level up (task t7): exact to the chunk
boundary, estimated only INSIDE the boundary chunk. A failed ``play`` is
counted as skipped, never as played — the room heard nothing.

The reply RECORD is unchanged by any of this: :class:`Response` still carries
every byte the server sent, because what the server said and what the room
heard are two different facts and conflating them is the defect t7 exists to
prevent. One consequence to carry forward: a reply is published on
``response.done``, which since t6 is no longer the same instant as "the last
chunk left the speaker". So ``on_response`` means *the server finished this
reply*, not *the room heard all of it* — the second question is
:meth:`~RealtimeDuplexSession.playback_progress`'s, and reconciling the two
into a said/unsaid record is task t7's, not this module's.

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
new arc. Both halves are pinned by AST scan over this file **and over**
:mod:`reachy.speech.realtime` — the PONG and CLOSE frames are emitted by the
shared session mechanics that live there, so a scan of this file alone would
be blind to half of what actually leaves. A third sender added later under any
name, in either file, fails the suite immediately.

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
import socket
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass, field
from typing import Any, Callable, Protocol

import numpy as np

from reachy.speech import realtime_wire as wire

# ---------------------------------------------------------------------------
# Cited from the ears-only session client, never re-derived.
#
# These two modules are the two ends of ONE wire, and this arc has already paid
# once (the t4/t6 audio-tee integration) for two agents independently deriving
# one protocol: it does not fail loudly, it produces plausible garbage. So
# everything both sessions do IDENTICALLY keeps ONE owner —
# reachy.speech.realtime, see its "Shared session mechanics" section — and is
# imported here rather than copied: the endpoint/key precedence, the connect-URL
# builder, the utterance record, the named reason strings, the buffered frame
# reader, the float->PCM16 coercion, the handshake, the frame pump, the up/down
# latch and the backoff arithmetic. Most are private names in that module;
# importing a leading-underscore name from a SIBLING module of the same package
# is the lesser evil against a second copy that can drift, and it is deliberate:
# none of them is worth a public API change to a module the runtime holds live.
#
# What this module does NOT inherit is policy — its reconnect loop, its 404
# lane diagnosis, its pull-source staleness drain, its latched overflow
# reporting and its ARMING are all its own, and the shared code has no notion
# of any of them. In particular nothing imported here can build a
# `response.create`: arming lives in `_send_pending_arm` below and nowhere else.
# ---------------------------------------------------------------------------
from reachy.speech.realtime import (
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
    _Handshake,
    _server_error,
    _session_created_line,
    _SessionLost,
    _SessionObservables,
    _SessionState,
    _to_pcm16,
    _utterance_from,
    _vad_stage_line,
    _ws_connect,
    _ws_pump_frames,
    _ws_release,
    _ws_run_session,
    _ws_send,
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
    "DEFAULT_PLAYBACK_CHUNK_BYTES",
    "DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES",
    "Limits",
    "PlaybackProgress",
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
    "REASON_PLAYBACK_CANCELLED",
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
#: A barge-in cut the reply short: whatever had not reached the speaker yet is
#: skipped, and the line says how much was heard before the cut.
REASON_RESPONSE_INTERRUPTED = "response-interrupted"
#: A reply arrived faster than the caller drained it (oldest evicted).
REASON_RESPONSE_QUEUE_FULL = "response-queue-full"
#: There is audio to speak and no ``play`` sink was injected.
REASON_NO_PLAYBACK_SINK = "no-playback-sink"
#: A caller cut the mouth off (:meth:`RealtimeDuplexSession.cancel_playback`)
#: and audio the server had already produced is deliberately never spoken.
REASON_PLAYBACK_CANCELLED = "playback-cancelled"
#: The mouth queue is full: the REST of that reply is refused rather than one
#: chunk being dropped out of its middle (a truncation is honest, a hole is a
#: defect), so speech stops early instead of skipping.
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

#: One second of PCM16 at 24 kHz: the unit the mouth speaks, and the unit a
#: cancel skips. It is therefore the WORST-CASE cut latency an interjection
#: pays — the room keeps hearing at most this much after the human speaks —
#: traded against a daemon ``/media/play`` round trip per chunk and the seam
#: between chunks. Unmeasured on the deployed stack (plan task t1 measures the
#: per-chunk round trip and may retune this); see the module docstring's
#: chunked-playback section for the full tradeoff.
DEFAULT_PLAYBACK_CHUNK_BYTES = DEFAULT_OUTPUT_SAMPLE_RATE * 2
#: The FIRST chunk of a reply is smaller, so the robot starts speaking sooner
#: (time-to-first-audio is a full chunk otherwise). Smaller than the steady
#: chunk, and still long enough that the next chunk's round trip has somewhere
#: to hide. 0.4 s.
DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES = DEFAULT_OUTPUT_SAMPLE_RATE * 2 * 2 // 5

#: One reply's accumulated audio cap: 60 s of PCM16 at 24 kHz. A runaway
#: server cannot make this process grow without bound.
DEFAULT_MAX_RESPONSE_BYTES = DEFAULT_OUTPUT_SAMPLE_RATE * 2 * 60

#: The mouth queue, in CHUNKS. Deep enough to hold one capped reply
#: (``DEFAULT_MAX_RESPONSE_BYTES`` / ``DEFAULT_PLAYBACK_CHUNK_BYTES`` = 60)
#: plus slack, because the gateway streams a reply's deltas far faster than
#: the speaker plays them: a shallow queue would refuse the tail of every long
#: answer. It was depth 2 while a whole reply was ONE item.
DEFAULT_PLAYBACK_MAXSIZE = 64

#: How many chunks the connect-time backlog drain may discard (~1.3 s at 20 ms
#: chunks). It stops early the moment the source says "nothing ready", so a
#: source with no backlog loses nothing.
DEFAULT_STALE_DRAIN_MAX_CHUNKS = 64
#: Chunks sent per pump iteration, so the send side cannot starve the read side.
_MAX_CHUNKS_PER_PUMP = 8

DEFAULT_CONNECT_TIMEOUT_S = 5.0
DEFAULT_FRAME_TIMEOUT_S = 5.0
DEFAULT_POLL_INTERVAL_S = 0.01
DEFAULT_BACKOFF_INITIAL_S = 0.5
DEFAULT_BACKOFF_MAX_S = 30.0
DEFAULT_STABLE_AFTER_S = 10.0
DEFAULT_JOIN_TIMEOUT_S = 2.0
#: How long the mouth thread parks between queue polls (bounds close()).
_PLAYBACK_POLL_S = 0.05

# --------------------------------------------------------------------------- #
# Bounds, grouped into one frozen home (issue #141, python:S107)              #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Limits:
    """:class:`RealtimeDuplexSession`'s numeric bounds, out of the constructor's kwargs.

    Every field here was a bare keyword parameter on
    :class:`RealtimeDuplexSession` before this task; the constructor's OTHER
    parameters are injected SEAMS (``read_audio``, ``play``, ``on_utterance``,
    ``on_response``, ``clock``) or per-deployment identity/configuration
    (``sample_rate``, ``output_sample_rate``, ``mute_during_playback``,
    ``url``, ``api_key``, ``arm_on_connect``) — none of those moved. This
    class does not re-explain each bound: the reasoning behind every default
    lives with its ``DEFAULT_*`` constant above (the one documented home this
    module already keeps), and every field here simply carries that same
    constant forward unchanged, so the refactor cannot silently change a
    number while moving it.

    The two playback CHUNK bounds arrived later (task t6) and were never bare
    parameters, but they follow the same rule for the same reason: they are the
    numbers a measured per-chunk daemon round trip will retune, and a bound
    with two homes is a bound that drifts.
    """

    #: Bounded queue depths — see :data:`DEFAULT_UTTERANCE_MAXSIZE` and
    #: neighbours: the oldest entry is evicted on overflow, never the newest.
    utterance_maxsize: int = DEFAULT_UTTERANCE_MAXSIZE
    response_maxsize: int = DEFAULT_RESPONSE_MAXSIZE
    playback_maxsize: int = DEFAULT_PLAYBACK_MAXSIZE
    #: How much audio one ``play`` call speaks — hence the worst-case cut
    #: latency of an interjection. See :data:`DEFAULT_PLAYBACK_CHUNK_BYTES`.
    playback_chunk_bytes: int = DEFAULT_PLAYBACK_CHUNK_BYTES
    #: The first chunk of each reply, smaller so speech starts sooner.
    playback_first_chunk_bytes: int = DEFAULT_PLAYBACK_FIRST_CHUNK_BYTES
    #: Cap on ONE reply's accumulated audio.
    max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES
    #: Connect-time backlog drain bound.
    stale_drain_max_chunks: int = DEFAULT_STALE_DRAIN_MAX_CHUNKS
    #: Socket budgets.
    connect_timeout_s: float = DEFAULT_CONNECT_TIMEOUT_S
    frame_timeout_s: float = DEFAULT_FRAME_TIMEOUT_S
    poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    #: Reconnect policy.
    backoff_initial_s: float = DEFAULT_BACKOFF_INITIAL_S
    backoff_max_s: float = DEFAULT_BACKOFF_MAX_S
    stable_after_s: float = DEFAULT_STABLE_AFTER_S
    #: Bounded thread join at :meth:`RealtimeDuplexSession.close`.
    join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S


def _even(value: object) -> int:
    """At least one PCM16 sample, and a whole number of them."""
    return max(2, int(value) & ~1)


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


@dataclass(frozen=True)
class PlaybackProgress:
    """What the ROOM heard of one reply — measured at the sink, not assumed.

    Returned by :meth:`RealtimeDuplexSession.playback_progress` and by
    :meth:`RealtimeDuplexSession.cancel_playback`, which is what makes a cut
    *reportable*: the caller that stopped the robot mid-sentence learns, in the
    same call, how much of the sentence the room actually got.

    The split between *played_bytes* and *in_flight_bytes* is the honest one
    and it is deliberate: a chunk inside ``play`` cannot be recalled and cannot
    be confirmed either, so it is reported as its own quantity rather than
    guessed into one bucket or the other. The said/unsaid text split one level
    up (task t7) is therefore EXACT to the chunk boundary and estimated only
    inside the boundary chunk.

    Attributes:
        response_id: the reply this describes, or ``None``.
        queued_bytes: audio handed to the mouth for this reply.
        played_bytes: audio the sink RETURNED from — heard, as far as anything
            in this process can know. A failed ``play`` is never counted here.
        in_flight_bytes: the boundary chunk: inside ``play`` right now,
            neither confirmed nor recallable.
        skipped_bytes: audio queued and then skipped by a cancel, or dropped by
            a failed ``play`` — produced by the server, never heard.
        cancelled: whether a cut reached this reply.
    """

    response_id: str | None
    queued_bytes: int
    played_bytes: int
    in_flight_bytes: int
    skipped_bytes: int
    cancelled: bool


@dataclass
class _PlaybackLedger:
    """The mutable half of :class:`PlaybackProgress`, one per reply."""

    response_id: str | None
    queued_bytes: int = 0
    played_bytes: int = 0
    in_flight_bytes: int = 0
    skipped_bytes: int = 0
    cancelled: bool = False

    def snapshot(self) -> PlaybackProgress:
        return PlaybackProgress(
            response_id=self.response_id,
            queued_bytes=self.queued_bytes,
            played_bytes=self.played_bytes,
            in_flight_bytes=self.in_flight_bytes,
            skipped_bytes=self.skipped_bytes,
            cancelled=self.cancelled,
        )


@dataclass(frozen=True)
class _PlaybackChunk:
    """One unit of speech on the mouth queue, stamped with its cut generation.

    The stamp is what makes a cancel work without reaching into the queue's
    internals or racing the mouth: bumping the session's generation makes every
    chunk carrying an older one stale, wherever it happens to be — still
    queued, or already dequeued and one instruction away from ``play``.
    """

    response_id: str | None
    generation: int
    pcm: bytes


@dataclass
class _PendingResponse:
    """One in-flight reply being accumulated across ``response.*`` events.

    *audio* is the whole reply (the record); *flushed* is how much of it has
    been handed to the mouth, so the two never disagree and no second buffer
    can drift from the first.
    """

    response_id: str | None
    item_id: str | None = None
    text: str = ""
    audio: bytearray = field(default_factory=bytearray)
    overflowed: bool = False
    #: The cut generation this reply was created under. A cancel bumps the
    #: session's, which is what stops chunks that have not ARRIVED yet.
    generation: int = 0
    #: Bytes of *audio* already handed to the mouth.
    flushed: int = 0
    #: Chunks handed over so far (the first one has its own size).
    chunks: int = 0
    #: The mouth queue refused a chunk, so the rest of this reply is refused
    #: too — a truncation, never a hole.
    truncated: bool = False


class RealtimeDuplexSession(_SessionObservables):
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
        limits: the session's numeric bounds — the three queue depths, the
            reply-audio cap, the connect-time stale-drain bound, the socket
            timeouts and the reconnect/backoff policy — grouped into one
            frozen :class:`Limits` (issue #141/``python:S107``). Every field
            keeps the exact default it had as a bare parameter; see
            :class:`Limits` for what each one bounds.
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
        limits: Limits | None = None,
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
        self._limits = limits if limits is not None else Limits()
        self._max_response_bytes = max(0, int(self._limits.max_response_bytes))
        # Chunk sizes are forced EVEN: a chunk boundary inside a PCM16 sample
        # would split it across two ``play`` calls and click.
        self._chunk_bytes = _even(self._limits.playback_chunk_bytes)
        self._first_chunk_bytes = _even(self._limits.playback_first_chunk_bytes)
        self._stale_drain_max_chunks = max(0, int(self._limits.stale_drain_max_chunks))
        self._connect_timeout_s = max(0.1, float(self._limits.connect_timeout_s))
        self._frame_timeout_s = max(0.1, float(self._limits.frame_timeout_s))
        self._poll_interval_s = max(0.001, float(self._limits.poll_interval_s))
        self._stable_after_s = max(0.0, float(self._limits.stable_after_s))
        self._join_timeout_s = max(0.0, float(self._limits.join_timeout_s))
        self._on_utterance = on_utterance
        self._on_response = on_response
        self._clock = clock

        self._utterances: queue.Queue = queue.Queue(
            maxsize=max(1, int(self._limits.utterance_maxsize))
        )
        self._responses: queue.Queue = queue.Queue(
            maxsize=max(1, int(self._limits.response_maxsize))
        )
        self._playback: queue.Queue = queue.Queue(
            maxsize=max(1, int(self._limits.playback_maxsize))
        )

        self.worker: threading.Thread | None = None
        self.mouth: threading.Thread | None = None
        self._start_lock = threading.Lock()
        self._sock_lock = threading.Lock()
        self._closed = False
        #: The session lifecycle: identity, the up/down latch, its counters,
        #: the wake signal and the reconnect backoff (SHARED).
        self._state = _SessionState(
            STAGE,
            SOURCE,
            is_closed=lambda: self._closed,
            backoff_initial_s=self._limits.backoff_initial_s,
            backoff_max_s=self._limits.backoff_max_s,
        )

        # --- worker-thread state ------------------------------------------- #
        self._sock: socket.socket | None = None
        self._reader: _FrameReader | None = None
        self._connected_at = 0.0
        self._session_id: str | None = None
        self._pending: dict[str, _PendingResponse] = {}
        self._utterance_seq = 0
        self._source_failed_logged = False
        #: Overflow reasons already reported for the CURRENT episode. `_offer`
        #: adds on the first eviction and clears on the first clean enqueue, so
        #: a persistently-full sink costs ONE line per episode rather than one
        #: per chunk — the same discipline the rest of this module's failures
        #: already follow, and the defect class the runtime's tick-overrun
        #: summary exists to avoid (69,696 lines measured, once).
        self._overflow_logged: set[str] = set()

        # --- the mouth's shared state (worker + playback thread) ------------ #
        #: Guards the cut generation, the per-reply ledgers and the id of the
        #: reply the mouth is on. Held for a handful of integer updates and
        #: never across ``play`` or a queue wait, so it cannot serialise
        #: anything that matters.
        self._playback_lock = threading.Lock()
        #: Bumped by every cut. A chunk (or a reply) stamped with an older one
        #: is skipped — see :class:`_PlaybackChunk`.
        self._generation = 0
        self._ledgers: "OrderedDict[str, _PlaybackLedger]" = OrderedDict()
        self._current_response_id: str | None = None

        # --- cross-thread flags (single writer each; plain reads are atomic) - #
        self._arm_pending = False
        self._speaking = False
        self._muted_logged = False
        self._no_sink_logged = False
        self._playback_full_logged = False
        self._playback_failed_logged = False
        self._lane_unavailable = False

        self.chunks_sent = 0
        self.bytes_sent = 0
        self.utterances = 0
        self.responses = 0
        self.response_audio_bytes = 0
        self.arms_sent = 0
        self.ignored_events = 0
        self.muted_chunks = 0
        self.stale_chunks_discarded = 0
        #: Chunks handed to the mouth, chunks the sink returned from, and the
        #: bytes behind the second figure. ``played`` counts CHUNKS since t6
        #: (it counted whole replies while a reply was one ``play`` call).
        self.chunks_queued = 0
        self.played = 0
        self.played_bytes = 0
        #: Chunks a cut skipped, and the audio behind them — produced by the
        #: server, deliberately never spoken.
        self.chunks_cancelled = 0
        self.cancelled_bytes = 0
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
        self._state.wake()
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
        self._state.wake()

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

    def cancel_playback(self) -> PlaybackProgress:
        """Stop talking NOW, and report how much of the reply the room got.

        The interjection primitive (issue #151 item 3). Skips every chunk still
        queued AND every chunk of the cut reply still to arrive, so the cut
        lands within one chunk boundary — the chunk already inside ``play``
        finishes, because a daemon-HTTP upload-then-play has no stop handle.
        Anything the server produced and the room never heard is a NAMED drop.

        Safe from any thread, before :meth:`start` and after :meth:`close`,
        O(1)-ish (a bounded queue drain and a few counters), and never raises.
        A cancel with nothing to cut is silent — it withheld nothing.

        The returned :class:`PlaybackProgress` describes the reply that was
        cut, measured at the sink; :meth:`playback_progress` re-reads it later.
        """
        cut = self._skip_remaining()
        if cut.skipped_bytes:
            self._state.drop(
                REASON_PLAYBACK_CANCELLED,
                f"cut after {cut.played_bytes}B spoken; {cut.skipped_bytes}B not spoken",
            )
        elif cut.in_flight_bytes:
            # Nothing withheld: the cut landed on the last chunk, which the
            # speaker is already committed to. Still worth one line — it is the
            # moment the robot was told to stop.
            self._state.note(
                f"playback cut after {cut.played_bytes}B spoken (last chunk in flight)"
            )
        return cut

    def playback_progress(self, response_id: str | None = None) -> PlaybackProgress | None:
        """What the room heard of *response_id* (default: the reply being spoken).

        ``None`` when this session has no record of that reply at all — every
        reply it has SEEN has a progress record, zeroed until the mouth touches
        it, so a zeroed record and ``None`` are different answers.
        """
        with self._playback_lock:
            key = response_id if response_id is not None else self._current_response_id
            ledger = self._ledgers.get(key or "")
            return ledger.snapshot() if ledger is not None else None

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
        _ws_run_session(
            state=self._state,
            is_connected=lambda: self._sock is not None,
            connect=self._connect,
            pump_once=self._pump_once,
            teardown=self._teardown_socket,
        )
        self._teardown_socket(graceful=True)

    def _pump_once(self, attempts: int) -> int:
        """This session's half of the shared loop: pump until the session ends.

        Every end is a fault here — unlike the ears-only client, which can end
        a session INTENTIONALLY to re-negotiate its sample rate. A session that
        had been up for ``stable_after_s`` still restarts the backoff from zero
        rather than compounding across an outage that already recovered once.
        """
        try:
            self._pump()
        except _SessionLost as lost:
            stable = (self._clock() - self._connected_at) >= self._stable_after_s
            self._teardown_socket()
            self._state.mark_down(lost.reason, lost.detail)
            return 0 if stable else attempts + 1
        except Exception:  # the worker must outlive any fault
            logger.warning("duplex: session pump raised", exc_info=True)
            self._teardown_socket()
            self._state.mark_down(REASON_STREAM_CLOSED, "unexpected pump failure")
            return attempts + 1
        return attempts

    # --- connect ------------------------------------------------------- #

    def _connect(self) -> bool:
        """One connect + handshake attempt. Returns success; never raises.

        The wire half is :func:`~reachy.speech.realtime._ws_connect` (SHARED).
        What stays here is this session's own: the 404 lane DIAGNOSIS, the
        per-session reply bookkeeping, and a pull-source backlog drain.
        """
        event = self._state.next_event()
        url = self.connect_url
        handshake = _ws_connect(
            url,
            api_key=self._api_key,
            connect_timeout_s=self._connect_timeout_s,
            frame_timeout_s=self._frame_timeout_s,
        )
        if not handshake.ok:
            return self._note_refusal(handshake)

        with self._sock_lock:
            self._sock = handshake.sock
        self._reader = handshake.reader
        self._connected_at = self._clock()
        self._lane_unavailable = False
        self._pending.clear()
        # A reply whose session died mid-sentence is dead with it: speaking its
        # queued tail seconds later, into a new session, is the outbound twin of
        # the stale-audio hazard `_drain_stale_source` handles below.
        stale = self._skip_remaining()
        if stale.skipped_bytes:
            self._state.drop(
                REASON_PLAYBACK_CANCELLED,
                f"{stale.skipped_bytes}B from the previous session are not spoken",
            )
        self._drain_stale_source()
        self._state.mark_up(event, url)
        return True

    def _note_refusal(self, handshake: _Handshake) -> bool:
        """Name a failed handshake — an HTTP 404 is its own DIAGNOSIS.

        Everything else is reported exactly as the shared connect named it:
        something answered and said no (or nothing answered at all), and
        retrying is the right move. A 404 says the route is not served because
        the gateway's ``stt`` role is infeasible — the fix is operator
        configuration, so the log has to say so rather than read as a flaky
        gateway. Retrying stays right either way (the lane can be switched on
        while we run), and the latch keeps this to one line.
        """
        if handshake.status == 404:
            self._lane_unavailable = True
            return self._state.note_connect_failure(
                REASON_LANE_UNAVAILABLE,
                "HTTP 404 - the gateway's stt lane is likely declared off; "
                "check GET /v1/capabilities for stt.feasible",
            )
        return self._state.note_connect_failure(handshake.reason, handshake.detail)

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
        _ws_send(self._sock, wire.OPCODE_TEXT, wire.build_response_create_event().encode("utf-8"))
        self.arms_sent += 1
        self._state.note("armed (response.create)")

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
                    self._state.drop(REASON_SELF_MUTE)
                continue
            self._muted_logged = False
            pcm = _to_pcm16(chunk)
            if not pcm:
                continue
            _ws_send(self._sock, wire.OPCODE_TEXT, wire.build_append_event(pcm).encode("utf-8"))
            self.chunks_sent += 1
            self.bytes_sent += len(pcm)

    def _read_source(self) -> Any:
        """One guarded ``read_audio()`` call. Never raises; a fault is latched."""
        try:
            chunk = self._read_audio()
        except Exception:  # a broken source must not end the session
            if not self._source_failed_logged:
                self._source_failed_logged = True
                self._state.drop(REASON_SOURCE_FAILED, "read_audio raised")
                logger.warning("duplex: read_audio raised", exc_info=True)
            return None
        self._source_failed_logged = False
        return chunk

    def _read_frames(self) -> None:
        _ws_pump_frames(
            socket_of=lambda: self._sock,
            reader=self._reader,
            poll_interval_s=self._poll_interval_s,
            is_closed=lambda: self._closed,
            on_event=self._dispatch_event,
            state=self._state,
        )

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
            self._state.note(f"response started id={pending.response_id}")
        elif kind == RESPONSE_DONE:
            self._finish_response(event, interrupted=False)
        elif kind == RESPONSE_INTERRUPTED:
            self._finish_response(event, interrupted=True)
        elif kind == SESSION_CREATED:
            self._on_session_created(event)
        elif kind in (SPEECH_STARTED, SPEECH_STOPPED):
            self._state.note(_vad_stage_line(kind, event))
        elif kind == ERROR_EVENT:
            reason, detail = _server_error(event)
            self._state.drop(reason, detail)
        else:
            self.ignored_events += 1
            logger.debug("duplex: unhandled event type %r", kind)

    def _on_session_created(self, event: dict) -> None:
        self._session_id = _as_str(event.get("session_id"))
        self._state.note(_session_created_line(event))
        if self._arm_on_connect:
            self._arm_pending = True

    def _publish_utterance(self, event: dict) -> None:
        """Publish one heard utterance. **No engagement gate** (spec claim c4)."""
        utterance = _utterance_from(event, t=self._clock(), session_id=self._session_id)
        if utterance is None:
            self._state.drop(REASON_EMPTY_TRANSCRIPT, "transcription.completed with no text")
            return
        self._utterance_seq += 1
        if not self._offer(self._utterances, utterance, REASON_UTTERANCE_QUEUE_FULL):
            return
        self.utterances += 1
        self._state.note(
            f"utterance chars={len(utterance.text)}", event=f"utt{self._utterance_seq}"
        )
        self._tap(self._on_utterance, utterance, "on_utterance")

    def _pending_for(self, event: dict) -> _PendingResponse:
        """The in-flight reply this event belongs to, created on first sight.

        Keyed by ``response_id`` and tolerant of its absence: a delta that
        arrives before (or without) ``response.created`` still lands in the
        right place rather than being dropped for a missing envelope field.

        The reply is stamped with the CURRENT cut generation here, once: a
        cancel bumps the session's, and from that moment every chunk this reply
        would still have produced is refused. A reply created AFTER the cut
        gets the new generation and speaks normally — cutting a sentence must
        not mute the answer to the interruption.
        """
        response_id = _as_str(event.get("response_id"))
        pending = self._pending.get(response_id or "")
        if pending is None:
            with self._playback_lock:
                generation = self._generation
                self._ledger_for(response_id)
            pending = _PendingResponse(response_id=response_id, generation=generation)
            self._pending[response_id or ""] = pending
        if pending.item_id is None:
            pending.item_id = _as_str(event.get("item_id"))
        return pending

    def _accumulate_audio(self, event: dict) -> None:
        """Append one base64 PCM16 delta to its reply, bounded — and SPEAK it.

        The delta is appended to the reply's record and then whatever complete
        chunk groups that made available are handed straight to the mouth, so
        the robot starts talking while the reply is still arriving. This is the
        half of chunked playback that makes a cut possible at all: by the time
        ``response.done`` lands, most of a long answer is already spoken or
        queued, and a cut has something to skip.
        """
        pending = self._pending_for(event)
        raw = event.get("delta")
        if not isinstance(raw, str) or not raw:
            self._state.drop(REASON_MALFORMED_AUDIO_DELTA, "delta missing or not a string")
            return
        try:
            pcm = base64.b64decode(raw, validate=True)
        except (ValueError, TypeError):
            self._state.drop(REASON_MALFORMED_AUDIO_DELTA, f"{len(raw)} base64 chars")
            return
        if len(pending.audio) + len(pcm) > self._max_response_bytes:
            if not pending.overflowed:
                pending.overflowed = True
                self._state.drop(REASON_RESPONSE_TOO_LONG, f"over {self._max_response_bytes} bytes")
            return
        pending.audio.extend(pcm)
        self._flush_chunks(pending, final=False)

    def _finish_response(self, event: dict, *, interrupted: bool) -> None:
        """Complete one reply: publish it, and finish (or cut) speaking it."""
        pending = self._pending_for(event)
        self._pending.pop(pending.response_id or "", None)
        if not interrupted:
            # The tail: whatever is left below one chunk group.
            self._flush_chunks(pending, final=True)
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
        self._state.note(
            f"response {'interrupted' if interrupted else 'done'} "
            f"id={pending.response_id} chars={len(pending.text)} audio={len(audio)}B"
        )
        if interrupted:
            # A barge-in means the human started talking again, so everything
            # not yet in the speaker is skipped — the queued chunks AND the
            # tail this reply never flushed. What the room already heard is
            # measured and named; the record still carries the whole reply.
            cut = self._skip_remaining(pending.response_id or "")
            self._state.drop(
                REASON_RESPONSE_INTERRUPTED,
                f"cut after {cut.played_bytes}B spoken; "
                f"{max(0, len(audio) - cut.played_bytes)}B not spoken",
            )
        self._tap(self._on_response, response, "on_response")

    # --- feeding the mouth ---------------------------------------------- #

    def _flush_chunks(self, pending: _PendingResponse, *, final: bool) -> None:
        """Hand every complete chunk group of *pending* to the mouth.

        With ``final=True`` (``response.done``) the remainder goes too, however
        short — chunking must never eat a reply's tail.

        Runs on the session worker and stays O(1)-ish per delta: a slice, a
        ``put_nowait`` and a couple of counters. No socket, no encoding, no
        blocking call — the pump is what keeps the ears open.
        """
        while not pending.truncated:
            available = len(pending.audio) - pending.flushed
            if available <= 0:
                return
            target = self._first_chunk_bytes if pending.chunks == 0 else self._chunk_bytes
            if available < target:
                if not final:
                    return
                target = available
            chunk = bytes(pending.audio[pending.flushed : pending.flushed + target])
            if not self._offer_chunk(pending, chunk):
                return
            pending.flushed += target
            pending.chunks += 1

    def _offer_chunk(self, pending: _PendingResponse, chunk: bytes) -> bool:
        """Queue one chunk. Returns whether feeding this reply may continue."""
        if self._play is None:
            if not self._no_sink_logged:
                self._no_sink_logged = True
                self._state.drop(REASON_NO_PLAYBACK_SINK, f"{len(chunk)} bytes not spoken")
            return False
        with self._playback_lock:
            if pending.generation != self._generation:
                return False  # this reply was cut; its remainder is not spoken
            item = _PlaybackChunk(pending.response_id, self._generation, chunk)
        try:
            self._playback.put_nowait(item)
        except queue.Full:
            # Refuse the REST of this reply rather than dropping one chunk out
            # of its middle: speech that stops early is honest, speech with a
            # hole in it is a defect.
            pending.truncated = True
            if not self._playback_full_logged:
                self._playback_full_logged = True
                self._state.drop(
                    REASON_PLAYBACK_QUEUE_FULL,
                    f"the mouth is {self._playback.maxsize} chunks behind; "
                    f"the rest of this reply is not spoken",
                )
            return False
        self._playback_full_logged = False
        self.chunks_queued += 1
        with self._playback_lock:
            self._ledger_for(pending.response_id).queued_bytes += len(chunk)
        return True

    # --- cutting the mouth off ------------------------------------------ #

    def _skip_remaining(self, key: str | None = None) -> PlaybackProgress:
        """Bump the cut generation and drain the mouth queue. Never raises.

        Everything drained is counted as SKIPPED against its reply, and a chunk
        the mouth had already dequeued is skipped too — it re-checks the
        generation immediately before ``play``. Only a chunk already inside
        ``play`` survives a cut, which is exactly the one-chunk boundary the
        chunk size buys.

        *key* names the reply to REPORT on when the caller already knows it
        (``response.interrupted`` does). Left out — an operator-driven cut,
        which knows only "stop talking" — the reply reported is the next one
        that would have been spoken, or the one in the speaker if the queue was
        already empty.
        """
        with self._playback_lock:
            self._generation += 1
        drained: list[_PlaybackChunk] = []
        while True:
            try:
                drained.append(self._playback.get_nowait())
            except queue.Empty:
                break
        with self._playback_lock:
            for item in drained:
                ledger = self._ledger_for(item.response_id)
                ledger.skipped_bytes += len(item.pcm)
                ledger.cancelled = True
                self.chunks_cancelled += 1
                self.cancelled_bytes += len(item.pcm)
            if key is None:
                key = drained[0].response_id if drained else self._current_response_id
            cut = self._ledger_for(key or None)
            cut.cancelled = True
            return cut.snapshot()

    def _ledger_for(self, response_id: str | None) -> _PlaybackLedger:
        """This reply's measurement record. Caller holds ``_playback_lock``.

        Bounded like every other per-reply structure here: the oldest record
        goes when a long-lived session has seen more replies than the response
        queue is deep. A cut is read immediately by whoever made it, so the
        history a caller can still ask about is deliberately short.
        """
        key = response_id or ""
        ledger = self._ledgers.get(key)
        if ledger is None:
            ledger = _PlaybackLedger(response_id=response_id)
            self._ledgers[key] = ledger
            while len(self._ledgers) > max(1, int(self._limits.response_maxsize)):
                self._ledgers.popitem(last=False)
        return ledger

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
                item = self._playback.get(timeout=_PLAYBACK_POLL_S)
            except queue.Empty:
                continue
            if self._begin_chunk(item):
                self._speak(item)

    def _begin_chunk(self, item: _PlaybackChunk) -> bool:
        """Adopt one chunk, unless a cut overtook it between queue and speaker."""
        with self._playback_lock:
            ledger = self._ledger_for(item.response_id)
            if item.generation != self._generation:
                ledger.skipped_bytes += len(item.pcm)
                ledger.cancelled = True
                self.chunks_cancelled += 1
                self.cancelled_bytes += len(item.pcm)
                return False
            self._current_response_id = item.response_id
            ledger.in_flight_bytes = len(item.pcm)
        return True

    def _speak(self, item: _PlaybackChunk) -> None:
        play = self._play
        if play is None:  # pragma: no cover - never enqueued without a sink
            return
        self._speaking = True
        spoken = False
        try:
            play(item.pcm, samplerate=self._output_sample_rate)
        except Exception:  # a dead speaker must not end the session
            self.playback_failures += 1
            if not self._playback_failed_logged:
                self._playback_failed_logged = True
                self._state.drop(REASON_PLAYBACK_FAILED, f"{len(item.pcm)} bytes")
                logger.warning("duplex: playback sink raised", exc_info=True)
        else:
            spoken = True
            self.played += 1
            self.played_bytes += len(item.pcm)
            self._playback_failed_logged = False
        finally:
            self._speaking = False
            with self._playback_lock:
                ledger = self._ledger_for(item.response_id)
                ledger.in_flight_bytes = 0
                # Confirmed by the sink, or not heard at all: a failed play is
                # never counted as spoken.
                if spoken:
                    ledger.played_bytes += len(item.pcm)
                else:
                    ledger.skipped_bytes += len(item.pcm)

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
                self._state.drop(reason, "oldest evicted (latched until this sink drains)")
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
        except Exception:  # a tap must not kill the session
            logger.warning("duplex: %s callback raised", name, exc_info=True)

    def _teardown_socket(self, *, graceful: bool = False) -> None:
        """Release the session socket. Idempotent, never raises."""
        with self._sock_lock:
            sock = self._sock
            self._sock = None
        self._reader = None
        self._state.mark_released()
        _ws_release(sock, graceful=graceful)


def build(**kwargs: Any) -> RealtimeDuplexSession:
    """Convenience constructor mirroring the other engines' factory style."""
    return RealtimeDuplexSession(**kwargs)
