"""The speech ACTUATOR for the 50 Hz behavior runtime — the robot's voice.

Until this module the symbolic runtime could hear (``transcript``), feel
(``pat``), see (``face``) and move — but it could not make a sound. Every noise
the robot produced came out of the retiring ``listen --live`` / ``think``
process, and the behavior library's ``speak`` entry is a head BOB, not speech.
This is the missing leg: a text-in, audio-out actuator a rule can reach.

--------------------------------------------------------------------------
Why a background worker (non-negotiable)
--------------------------------------------------------------------------
Speech is the first genuinely BLOCKING side effect in the engine loop.
Synthesis takes tens of milliseconds at best; playback reaches a device or the
daemon. The engine holds a **20 ms budget at 50 Hz**, and this exact defect
class — a slow call made on the tick thread — is what produced the deployed
robot's reproducible **425-1213 ms** startup overruns (a media client
constructed inline; see
``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3). It was
fixed by moving the blocking work off the tick thread, and it must not be
reintroduced here.

So the split is the same one :class:`reachy.behavior.transcript_sense.TranscriptSenseDriver`
uses for its STT round-trip, in the opposite direction:

* **The tick thread** only ever calls :meth:`SpeechActuator.say`, which
  validates the text, checks a mute latch, and does ONE non-blocking
  ``put_nowait`` onto a BOUNDED queue. It never synthesizes, never opens a
  socket, never waits on a lock held across I/O, and never blocks — a full
  queue is a named DROP, not backpressure onto the loop.
* **The worker thread** (:meth:`_worker_loop`) synthesizes and plays.

Because the queue is bounded and every put is non-blocking, a wedged TTS or a
dead speaker backs the queue up and further utterances are dropped with a
logged reason — the tick never notices.

--------------------------------------------------------------------------
The default voice is OFFLINE (a product decision, not a fallback)
--------------------------------------------------------------------------
:data:`RUNTIME_DEFAULT_VOICE_ENGINE` is ``"harmonic"`` —
:mod:`reachy.speech.harmonic`'s in-process note-melody synth, backed by
``harmonics-cli`` (a BASE dependency with zero transitive runtime deps). It
needs no network hop and no speech model, so:

* a robot with nothing reachable — fresh box, dead LAN, TTS container down —
  still has a voice, and
* the "wedged TTS" degradation below is mostly moot on the shipped path.

This deliberately DIFFERS from :func:`reachy.speech.voice.resolve_voice_engine`'s
own ``"tts"`` default, which serves the retiring ``say``/``think`` nouns. The
runtime resolves through :func:`resolve_runtime_voice_engine` instead;
``REACHY_VOICE_ENGINE=tts`` selects the external Chatterbox backend as the
configurable ALTERNATIVE. TTS is never required for the default install.

--------------------------------------------------------------------------
Playback is INJECTED, and plays through the ONE HELD media client
--------------------------------------------------------------------------
:mod:`reachy.speech.playback` offers two transports and this module hard-wires
neither — ``play`` is a plain injected callable, and the default binding picks
its transport from :data:`SPEECH_TRANSPORT_ENV` / ``REACHY_TRANSPORT`` /
:data:`RUNTIME_DEFAULT_TRANSPORT`.

The shipped default is ``"sdk"``, pushing PCM through the media session the
runtime **already owns** — :class:`reachy.robot.media_client.HeldMediaClient`,
resolved through the injected ``media_session_provider``. That is the whole
point of the seam: without a provider the ``sdk`` path would call
``playback._open_sdk_media()`` and open a **SECOND** ``ReachyMini``, which the
single-SDK-owner model forbids. Speech is therefore a fan-OUT leg of the one
client, exactly as ``rms`` and the transcript sense are fan-IN legs of it.

Two facts make this the right default, and neither is the retired #94 premise:

1. **Issue #94 is CLOSED.** A media-profile client is not "unconstructable" on
   the deployed robot — the runtime warms that very client on every boot
   (measured 2026-07-23: ``HeldMediaClient`` up in 1032 ms, 9/10 camera frames
   and live mic audio). The earlier ``"http"`` default was justified by that
   stale premise; it no longer holds.
2. **The push tolerates the speech worker thread** (live probe, spark-f8a9,
   2026-07-24, deviation d2): pushing a clip from a worker thread while a
   reader thread concurrently drained ``client.audio()`` gave 198 clean reads,
   ZERO read errors and no reader stall — the SDK's input-read and output-push
   paths do not contend. ``push_audio_sample`` buffers and returns in ~8 ms for
   a 5.76 s clip, so it never blocks. This is why the hand-off is a direct call
   rather than a pump-style output seam mirroring
   :class:`reachy.behavior.audio_pump.AudioPump`.

Using the held client removes the voice's dependency on **daemon media state**
entirely, which is the durable fix beneath #122's quick ``http`` re-enable.

``http`` remains one variable away (``REACHY_SPEECH_TRANSPORT=http``) and is
also the automatic fallback when the provider yields no session — a holder that
has not warmed yet, or a box with no ``[sdk]`` extra at all. Falling back there
rather than to ``_open_sdk_media()`` is deliberate: opening a second client is
the defect being removed, and the daemon route reaches the SAME physical
speaker. The two routes were never in contention, because the daemon and the
runtime hold ``/dev/snd/pcmC2D0p`` *simultaneously* through the
``reachymini_audio_sink`` ALSA plugin device (``~/.asoundrc``) — the
single-SDK-owner model constrains the *media session*, not the *ALSA sink*
(verified 2026-07-23, and the reason the ``http`` route works at all while the
runtime holds media). It also needs no extra: pure ``urllib``, so a bare
``pip install reachy-mini-cli`` box still has a voice — the same property that
made the harmonic voice the default.

--------------------------------------------------------------------------
Self-mute
--------------------------------------------------------------------------
:meth:`mute_until` publishes the monotonic instant the robot's own voice stops
occupying the room (clip duration + :data:`DEFAULT_MUTE_MARGIN_S`). It is
shaped exactly for :class:`reachy.behavior.transcript_sense.TranscriptSenseDriver`'s
``mute_until`` seam, so the runtime cannot transcribe itself and talk back to
its own voice — the feedback loop the retiring loop's ``play_audio`` wrapper
also had to close.

It is a SECOND line of defence, not the only one. Whenever the voice plays
through the robot's own speaker — which both routes above do — the hardware
already cancels it: the mic read selects the AEC channel
(:data:`reachy.robot.audio_shape.AEC_CHANNEL`), so the robot's own output is
subtracted from what it hears and it structurally cannot transcribe itself.
Self-mute is kept anyway because it costs nothing, is transport-agnostic, and
does not depend on AEC being correctly configured on a given box.

Standard library plus the existing speech engine — no new dependency, and
nothing here imports ``reachy_mini``.
"""

from __future__ import annotations

import logging
import os
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

from reachy import senselog
from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.speech.harmonic import HARMONIC_SAMPLE_RATE
from reachy.speech.voice import VOICE_ENGINE_ENV, VoiceEngine, resolve_voice_engine

logger = logging.getLogger(__name__)

#: ``[SENSE stage=speech source=say event=<n>]`` — this module's log identity.
STAGE = "speech"
SOURCE = "say"

#: The runtime's default voice: in-process, offline, no network round-trip.
#: Deliberately NOT :data:`reachy.speech.voice.DEFAULT_ENGINE` (``"tts"``).
RUNTIME_DEFAULT_VOICE_ENGINE = "harmonic"

#: Env var selecting the playback transport for the behavior runtime
#: specifically; falls back to the general ``REACHY_TRANSPORT``.
SPEECH_TRANSPORT_ENV = "REACHY_SPEECH_TRANSPORT"
#: The shipped playback transport — see the module docstring for why.
RUNTIME_DEFAULT_TRANSPORT = "sdk"
_VALID_TRANSPORTS = ("sdk", "http")

#: Daemon base URL for the ``http`` playback transport.
BASE_URL_ENV = "REACHY_BASE_URL"
DEFAULT_BASE_URL = "http://localhost:8000"

#: Queue depth. Small on purpose: an utterance that has been waiting behind two
#: others is stale by the time it would play, so dropping beats buffering.
DEFAULT_QUEUE_MAXSIZE = 2
#: Consecutive render failures before the audio sink latches off.
DEFAULT_FAILURE_LATCH = 3
#: How long a latched-off sink stays off before trying again. Time-bounded (not
#: permanent, unlike the cognition engine's latch) because a boot-persistent
#: robot must recover from a daemon restart without a human restarting it.
DEFAULT_RETRY_AFTER_S = 30.0
#: Padding added to a clip's own duration when stamping the self-mute window.
DEFAULT_MUTE_MARGIN_S = 0.5
#: Bounded join at shutdown — teardown must never hang the process.
DEFAULT_JOIN_TIMEOUT_S = 2.0

_BYTES_PER_SAMPLE = 2  # PCM16 mono, the contract every synthesize() here returns

REASON_CLOSED = "closed"
REASON_EMPTY = "empty-text"
REASON_TOO_LONG = "too-long"
REASON_QUEUE_FULL = "queue-full"
REASON_LATCHED = "sink-latched-off"
REASON_SYNTHESIZE = "synthesize-failed"
REASON_PLAYBACK = "playback-failed"
REASON_NO_AUDIO = "no-audio"


# --------------------------------------------------------------------------- #
# Resolution helpers                                                          #
# --------------------------------------------------------------------------- #


def resolve_runtime_voice_engine(name: str | None = None) -> VoiceEngine:
    """The runtime's voice: explicit *name* > ``REACHY_VOICE_ENGINE`` > harmonic.

    A thin re-defaulting of :func:`reachy.speech.voice.resolve_voice_engine` —
    the registry, the ``VoiceEngine`` shape and the unknown-name error are all
    reused unchanged; only the fallback differs (harmonic here, TTS there). An
    unknown name is the same clean exit-1 :class:`CliError`.
    """
    resolved = name or os.environ.get(VOICE_ENGINE_ENV) or RUNTIME_DEFAULT_VOICE_ENGINE
    return resolve_voice_engine(resolved)


def resolve_playback_transport(name: str | None = None) -> str:
    """The playback transport: explicit *name* > :data:`SPEECH_TRANSPORT_ENV` >
    ``REACHY_TRANSPORT`` > :data:`RUNTIME_DEFAULT_TRANSPORT`.

    The speech-specific variable wins over the general one so an operator can
    keep the engine on ``--transport sdk`` while routing only AUDIO through the
    daemon — the escape hatch if the held client's session is ever unavailable
    on a given box. (This used to read "while issue #94 is open"; #94 is closed
    and the held-client route is now the default — see the module docstring.)

    Raises a clean exit-1 :class:`CliError` for anything else — resolved at
    SETUP so a typo is a startup error, never a mid-utterance surprise.
    """
    resolved = (
        name
        or os.environ.get(SPEECH_TRANSPORT_ENV)
        or os.environ.get("REACHY_TRANSPORT")
        or RUNTIME_DEFAULT_TRANSPORT
    )
    if resolved not in _VALID_TRANSPORTS:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown speech playback transport {resolved!r}",
            remediation=f"set {SPEECH_TRANSPORT_ENV} to one of: {', '.join(_VALID_TRANSPORTS)}",
        )
    return resolved


def make_default_play(
    *,
    transport: str | None = None,
    base_url: str | None = None,
    media_session_provider: Callable[[], Any] | None = None,
) -> Callable[..., None]:
    """Build the default playback callable — ``play(pcm, *, samplerate)``.

    Binds :func:`reachy.speech.playback.play_audio` to a transport resolved
    once, here, at setup. ``reachy.speech.playback`` is imported lazily so a
    module-level import of this file never drags the audio stack in.

    Args:
        media_session_provider: a zero-arg callable returning the runtime's ONE
            held media session (or ``None``). Resolved at **play time**, on the
            worker thread — never captured here — because the composition root
            builds this actuator BEFORE the media client exists, and because a
            mid-run reconnect swaps the session underneath us. See the module
            docstring for why the ``sdk`` route must use the held client rather
            than let ``playback._open_sdk_media()`` open a second one.

    The ``sdk`` route degrades in ONE direction only: no session means the
    daemon ``http`` route, never a second SDK client. A provider that raises is
    treated identically — a broken holder is a degradation, not a lost
    utterance.
    """
    resolved_transport = resolve_playback_transport(transport)
    resolved_base = base_url or os.environ.get(BASE_URL_ENV) or DEFAULT_BASE_URL
    announced_fallback = False

    def _held_session() -> Any | None:
        if media_session_provider is None:
            return None
        try:
            return media_session_provider()
        except Exception as err:  # noqa: BLE001 — a broken holder must not lose the clip
            logger.warning("speech: held media session provider raised (%s); using http", err)
            return None

    def _play(pcm: bytes, *, samplerate: int) -> None:
        nonlocal announced_fallback
        from reachy.speech.playback import play_audio

        if resolved_transport != "sdk":
            play_audio(
                pcm,
                samplerate=samplerate,
                transport=resolved_transport,
                base_url=resolved_base,
            )
            return

        session = _held_session()
        if session is None:
            # No held session: the holder has not warmed yet, it dropped, or
            # this box has no `[sdk]` extra at all. Fall back to the daemon —
            # the SAME physical speaker (shared ALSA sink) — rather than open a
            # second media client, which is the defect this seam removes.
            if not announced_fallback:
                announced_fallback = True
                logger.info(
                    "speech: no held media session yet; playing through the daemon http "
                    "route at %s (no second SDK client is ever opened)",
                    resolved_base,
                )
            play_audio(pcm, samplerate=samplerate, transport="http", base_url=resolved_base)
            return

        play_audio(
            pcm,
            samplerate=samplerate,
            transport="sdk",
            base_url=resolved_base,
            media_session=session,
        )

    return _play


# --------------------------------------------------------------------------- #
# The actuator                                                                #
# --------------------------------------------------------------------------- #


class SpeechActuator:
    """Text in on the tick thread, audio out on a background worker.

    Args:
        voice: an explicit :class:`~reachy.speech.voice.VoiceEngine`. Omit to
            resolve one through :func:`resolve_runtime_voice_engine` (harmonic
            by default).
        synthesize: inject a bare ``synthesize(text) -> bytes`` callable
            instead of a whole engine (tests, and any caller that already has
            one). Paired with *samplerate*.
        samplerate: the rate *synthesize*'s PCM16 is rendered at. Only read
            alongside *synthesize*; a *voice* carries its own.
        play: ``play(pcm, *, samplerate) -> None``. Defaults to
            :func:`make_default_play`. INJECTED on purpose — see the module
            docstring on why neither transport is hard-wired.
        media_session_provider: zero-arg callable yielding the runtime's ONE
            held media session, used only when *play* is left to the default.
            The ``sdk`` route plays through it instead of opening a second SDK
            client; ``None`` (or a provider returning ``None``) falls back to
            the daemon ``http`` route. Resolved per utterance on the worker
            thread — see :func:`make_default_play`.
        base_url: daemon base URL for the ``http`` route, when *play* is left
            to the default. Defaults to ``REACHY_BASE_URL`` /
            :data:`DEFAULT_BASE_URL`.
        queue_maxsize: bounded hand-off depth (:data:`DEFAULT_QUEUE_MAXSIZE`).
        failure_latch: consecutive render failures before the sink latches off.
        retry_after_s: how long a latched-off sink stays off.
        mute_margin_s: padding on the published self-mute window.
        clock: injectable ``() -> float`` monotonic clock (mute + latch timing).
        join_timeout_s: bounded worker join at :meth:`close`.

    Attributes:
        submitted / spoken / dropped / failures: counters for a status readout
            or a test assertion.
        worker: the background thread, or ``None`` before :meth:`start`.
    """

    def __init__(
        self,
        *,
        voice: VoiceEngine | None = None,
        synthesize: Callable[..., bytes] | None = None,
        samplerate: int | None = None,
        play: Callable[..., None] | None = None,
        media_session_provider: Callable[[], Any] | None = None,
        base_url: str | None = None,
        queue_maxsize: int = DEFAULT_QUEUE_MAXSIZE,
        failure_latch: int = DEFAULT_FAILURE_LATCH,
        retry_after_s: float = DEFAULT_RETRY_AFTER_S,
        mute_margin_s: float = DEFAULT_MUTE_MARGIN_S,
        clock: Callable[[], float] = time.monotonic,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    ) -> None:
        if synthesize is not None:
            self.voice = VoiceEngine(
                name="injected",
                synthesize=synthesize,
                samplerate=int(samplerate or HARMONIC_SAMPLE_RATE),
            )
        else:
            self.voice = voice if voice is not None else resolve_runtime_voice_engine()
        self._play = (
            play
            if play is not None
            else make_default_play(
                base_url=base_url,
                media_session_provider=media_session_provider,
            )
        )
        self._clock = clock
        self._failure_latch = max(1, int(failure_latch))
        self._retry_after_s = max(0.0, float(retry_after_s))
        self._mute_margin_s = max(0.0, float(mute_margin_s))
        self._join_timeout_s = max(0.0, float(join_timeout_s))

        self._pending: queue.Queue = queue.Queue(maxsize=max(1, int(queue_maxsize)))
        self.worker: threading.Thread | None = None
        self._closed = False
        self._start_lock = threading.Lock()

        # Written by the worker, read by the tick thread. Plain float/int
        # assignment is atomic under CPython, and each has ONE writer.
        self._mute_until = 0.0
        self._latched_until = 0.0
        self._consecutive = 0

        # In-flight accounting, so `join_idle` can wait for a quiet actuator.
        self._idle = threading.Condition()
        self._inflight = 0
        self._seq = 0

        self.submitted = 0
        self.spoken = 0
        self.dropped = 0
        self.failures = 0

    # ------------------------------------------------------------------ #
    # Lifecycle                                                          #
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Spawn the worker. Idempotent; a no-op once closed.

        Call this at COMPOSITION time. :meth:`say` calls it too (so a
        stand-alone caller needs no ceremony), but spawning a thread is the one
        piece of non-O(1) work in the hand-off, and setup has no tick budget to
        protect — so the runtime pays for it before the first tick.
        """
        with self._start_lock:
            if self._closed or self.worker is not None:
                return
            self.worker = threading.Thread(
                target=self._worker_loop, name="behavior-speech", daemon=True
            )
            self.worker.start()

    def close(self) -> None:
        """Stop the worker with a bounded join. Idempotent, never raises."""
        with self._start_lock:
            if self._closed:
                return
            self._closed = True
            worker = self.worker
        if worker is not None:
            try:
                self._pending.put_nowait(None)
            except queue.Full:
                # Make room: a wedged queue must not stop us signalling the
                # worker. Dropping one queued utterance at shutdown is strictly
                # better than a worker that never learns to stop — the bounded
                # join() below would merely stop WAITING for it, leaving
                # synthesis and playback running through teardown.
                # Same pattern as TranscriptSenseDriver.close().
                try:
                    self._pending.get_nowait()
                    self._pending.put_nowait(None)
                except (queue.Empty, queue.Full):  # pragma: no cover - defensive
                    logger.debug("speech: could not enqueue worker stop")
            worker.join(timeout=self._join_timeout_s)

    def __enter__(self) -> "SpeechActuator":
        self.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------ #
    # Tick-thread entry point                                            #
    # ------------------------------------------------------------------ #

    def say(self, text: str) -> bool:
        """Queue *text* to be spoken. **O(1), non-blocking, never raises.**

        This is the ONLY method the tick thread calls. Returns whether the
        utterance was accepted; every rejection is a named
        :func:`reachy.senselog.drop`, never a silent no-op.
        """
        event = self._next_event()
        if self._closed:
            return self._drop(event, REASON_CLOSED)
        if not isinstance(text, str) or not text.strip():
            return self._drop(event, REASON_EMPTY)
        if len(text) > MAX_SAY_CHARS:
            return self._drop(event, REASON_TOO_LONG)
        if self.muted:
            return self._drop(event, REASON_LATCHED)

        self.start()
        with self._idle:
            self._inflight += 1
        try:
            # (event, text), not bare text: the worker logs this utterance's
            # outcome under the SAME id say() already used for its drops, so
            # an accept and its spoke/failure line can be correlated. It also
            # makes _next_event() single-threaded (tick thread only).
            self._pending.put_nowait((event, text))
        except queue.Full:
            with self._idle:
                self._inflight -= 1
                self._idle.notify_all()
            return self._drop(event, REASON_QUEUE_FULL)
        self.submitted += 1
        return True

    def mute_until(self) -> float:
        """The monotonic instant the robot's own voice stops occupying the room.

        The shape :class:`reachy.behavior.transcript_sense.TranscriptSenseDriver`'s
        ``mute_until`` seam expects — a free read, safe from the tick thread.
        """
        return self._mute_until

    @property
    def muted(self) -> bool:
        """Whether the audio sink is currently latched off after repeated faults."""
        return self._clock() < self._latched_until

    @property
    def speaking(self) -> bool:
        """Whether the robot's own voice is currently sounding."""
        return self._clock() < self._mute_until

    def join_idle(self, timeout: float = 5.0) -> bool:
        """Block until nothing is queued or rendering. A determinism seam.

        Test-facing (and usable by a caller that wants to flush before exit);
        the tick thread never calls it.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        with self._idle:
            while self._inflight > 0:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._idle.wait(remaining)
        return True

    # ------------------------------------------------------------------ #
    # Worker thread                                                      #
    # ------------------------------------------------------------------ #

    def _worker_loop(self) -> None:
        while True:
            item = self._pending.get()
            if item is None:
                return
            try:
                self._render(*item)
            except Exception:  # noqa: BLE001 — the worker must outlive any utterance
                logger.warning("speech: rendering an utterance raised", exc_info=True)
            finally:
                with self._idle:
                    self._inflight -= 1
                    self._idle.notify_all()

    def _render(self, event: str, text: str) -> None:
        """Synthesize then play ONE utterance. Every failure degrades to silence.

        *event* is allocated by :meth:`say` on the tick thread and carried
        through the queue, so every line this utterance produces — accept,
        drop, failure, ``spoke`` — shares one id.
        """
        if self.muted:
            self._drop(event, REASON_LATCHED)
            return
        try:
            pcm = self.voice.synthesize(text)
        except Exception as err:  # noqa: BLE001 — an unreachable TTS is silence
            self._note_failure(event, REASON_SYNTHESIZE, err)
            return
        if not pcm:
            # Not a fault: the harmonic backend renders nothing for text with no
            # pronounceable content. Named, so it is never a silent no-op.
            senselog.drop(STAGE, SOURCE, event, REASON_NO_AUDIO)
            self._consecutive = 0
            return

        duration_s = len(pcm) / (_BYTES_PER_SAMPLE * max(1, self.voice.samplerate))
        # Stamp BEFORE playback: the daemon's http route returns as soon as it has
        # started the clip, so the window has to be open before the sound is.
        self._stamp_mute(duration_s)
        try:
            self._play(pcm, samplerate=self.voice.samplerate)
        except Exception as err:  # noqa: BLE001 — a dead speaker is silence
            self._note_failure(event, REASON_PLAYBACK, err)
            return
        self._stamp_mute(duration_s)
        self._consecutive = 0
        self.spoken += 1
        senselog.stage(
            STAGE,
            SOURCE,
            event,
            f"spoke voice={self.voice.name} chars={len(text)} duration_s={duration_s:.2f}",
        )

    def _note_failure(self, event: str, reason: str, err: BaseException) -> None:
        """Record a render failure, latching the sink off after enough of them."""
        self.failures += 1
        self._consecutive += 1
        senselog.drop(STAGE, SOURCE, event, f"{reason} ({type(err).__name__}: {err})")
        if self._consecutive >= self._failure_latch:
            self._latched_until = self._clock() + self._retry_after_s
            self._consecutive = 0
            logger.warning(
                "speech: audio sink latched off for %.0fs after %d consecutive failures",
                self._retry_after_s,
                self._failure_latch,
            )
            senselog.drop(
                STAGE,
                SOURCE,
                event,
                f"{REASON_LATCHED} retry_after_s={self._retry_after_s:.0f}",
            )

    def _stamp_mute(self, duration_s: float) -> None:
        self._mute_until = max(self._mute_until, self._clock() + duration_s + self._mute_margin_s)

    # ------------------------------------------------------------------ #
    # Small helpers                                                      #
    # ------------------------------------------------------------------ #

    def _next_event(self) -> str:
        """Allocate an utterance id. **Tick thread only** — see :meth:`say`.

        Single-threaded by contract rather than by lock: ``say()`` is the only
        caller, and it hands the id to the worker through the queue. When the
        worker minted its own, an utterance's accept and its outcome carried
        DIFFERENT ids (the live journal showed ``spoke`` on even ids only,
        drops on odd), so the two could never be correlated.
        """
        self._seq += 1
        return f"utt{self._seq}"

    def _drop(self, event: str, reason: str) -> bool:
        self.dropped += 1
        senselog.drop(STAGE, SOURCE, event, reason)
        return False


def build(**kwargs: Any) -> SpeechActuator:
    """Convenience constructor mirroring the other producers' factory style."""
    return SpeechActuator(**kwargs)
