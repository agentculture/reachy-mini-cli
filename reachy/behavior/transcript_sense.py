"""Transcript sense for the 50 Hz behavior runtime — hearing WORDS, not just sound.

The symbolic runtime senses direction, loudness and touch, but until now it had
no path from the microphone to *what was actually said*: that capability lived
only in the retired ``listen --live`` loop
(``reachy.motion.listen_transcribe``, the donor for this module). This driver
ports it onto the runtime's one tick seam, so a data-only rule (``when
{field=transcript, op=is_true}``) and an externally attached agent can both
reason about speech.

It is built exactly like :class:`reachy.behavior.pat_sense.PatSenseDriver` — a
``TickBus`` driver that WRITES a one-tick latch at the end of every tick, plus a
zero-arg PEEK provider (:meth:`peek` / :meth:`as_provider`) that
:func:`reachy.behavior.sense.read_perception` folds into the next tick's
:class:`~reachy.behavior.sense.Sense`. Read that module's "r2 — cadence + one-tick
latch semantics" section: the discipline is identical here, including the
clear-BEFORE-process rule that holds on every path, so a transcript is delivered
to exactly ONE sense snapshot and multiple peeks within a tick agree.

--------------------------------------------------------------------------
Why a background worker (the load-bearing difference from ``PatSenseDriver``)
--------------------------------------------------------------------------
A pat is sensed with arithmetic; a transcript costs a **network round-trip** —
two, in fact, since the engagement classifier is also an HTTP call. Doing either
inline would blow the 20 ms tick budget by orders of magnitude. This is not
hypothetical: the deployed box already shows a reproducible startup overrun of
**424.93-1212.66 ms against a 20 ms budget**, caused by exactly this class of
on-thread blocking (a media client constructed on the tick thread —
``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).

So the work is split across two threads with a queue between them:

* **The tick thread** (:meth:`__call__`) does only cheap, bounded work: read one
  mic chunk from the injected held media client, push it into a rolling pre-roll
  ring, run an energy VAD, and — when an utterance endpoints — hand the finished
  buffer to the worker with a NON-BLOCKING ``put_nowait``. It also drains at most
  one ready transcript into the latch. No socket is ever touched here.
* **The worker thread** (:meth:`_worker_loop`, started lazily on the first
  submitted utterance) makes the single
  :meth:`~reachy.speech.stt.Transcriber.transcribe_once` POST, runs the
  engagement gate, fires ``on_engage``, and publishes an accepted transcript onto
  the ready queue.

Every queue is BOUNDED and every put is non-blocking: a wedged STT backs the
pending queue up, and further utterances are dropped with a logged reason rather
than growing memory or — far worse — blocking the tick. An unreachable STT
therefore leaves the field ``None`` and drops **no ticks**.

--------------------------------------------------------------------------
The engagement gate is reused, never reimplemented
--------------------------------------------------------------------------
Admission is :class:`reachy.speech.engagement.ConversationGate` — the #54/#56
layered gate plus the #105 conversation state, shared with (not duplicated from)
the donor:

1. **Name fast-path** — an utterance naming the robot (or a common STT
   mishearing of it) engages immediately, with ZERO classifier calls, and opens
   the conversation.
2. **Short-utterance rule** — a context-only engagement needs at least
   ``min_words`` words. A backchannel ("No.", "Okay.") carries no addressing
   signal; only a name can engage in that few words. Zero classifier calls.
3. **Warm-window rule** — a nameless utterance is only judged while a
   conversation is live (within ``engage_window_s`` of the last accepted turn).
   Past that, it is dropped with zero classifier calls and only a fresh name can
   reopen the conversation.
4. **Single-shot LLM classifier** — otherwise one "is this addressed to me,
   given the recent conversation?" call decides ENGAGE or DROP.
5. **DEGRADE** — a classifier that raises falls back to this module's own cheap
   heuristic (:meth:`_should_engage`: a clear sentence inside an open
   conversation window), so hearing never stalls on a dead endpoint; a heuristic
   accept is reported back to the gate so degraded turns keep it warm.

Rules 2 and 3 exist because the history this module used to keep by hand was a
one-way ratchet — only accepts entered it, so a single false accept made the
next accept likelier (issue #105; the measured trace is in the gate's own
docstring). Every outcome is NAMED, and the name is used verbatim as the
:func:`reachy.senselog.drop` reason, so the journal distinguishes
``not-addressed`` from ``not-addressed-short`` and ``not-addressed-cold``.

``REACHY_ENGAGE_HEURISTIC`` (read once at construction) forces the heuristic
throughout, and omitting the ``classifier`` argument does the same: in both
cases no gate is built at all, so no classifier call is reachable.

--------------------------------------------------------------------------
Deviations from the donor, and why
--------------------------------------------------------------------------
* **Voice activity is measured from the audio, not taken from the daemon's
  speech flag.** The donor gated capture on ``SenseSample.speech`` (the SDK's
  ~5 Hz speech flag). The runtime's equivalent, ``Sense.speech_detected``, was
  measured on the deployed box as **true 45.8% of the time in a quiet room with
  nobody speaking** (baseline section 2) — a coin-flip, useless as a capture
  gate. So this module runs an RMS energy VAD over the chunk it already holds,
  reusing the donor's own float-PCM silence threshold (the one its onset scan
  uses). The pre-roll ring exists regardless, because an energy VAD also misses
  the quiet leading phoneme of an utterance.
* **The audio source is the injected held media client**, not a per-tick
  ``SenseSample``. The driver calls ``media.audio()`` once per tick and NEVER
  constructs a client, opens a media session, or imports the SDK — the runtime's
  composition root owns exactly one client and injects it (see
  :mod:`reachy.robot.media_client`). Because a *first* read on a cold holder
  blocks for order-of-seconds, that owner is expected to
  :meth:`~reachy.robot.media_client.HeldMediaClient.warm_up` off-thread and pass
  ``allow_inline_connect=False``; this driver only ever reads.
* **The mic sample rate is resolved lazily, after a successful read.** Touching
  ``media.samplerate`` on a cold holder can trigger construction, so it is read
  only once ``audio()`` has already returned a chunk (proving the client is up).
  Until then no sample-count threshold is needed, because there is no audio.
* **``EventBuffer`` is gone.** The donor fed words into ``think``'s cognition
  buffer; here the accepted transcript becomes a latched perception field. Any
  cognition is external (``agent attach``), reading the same ``Sense``.

Every failure degrades to "no words" and never raises out of the driver or the
provider, mirroring :func:`reachy.behavior.sense._peek`.

Standard library plus numpy and the existing speech engine — no new dependency.
"""

from __future__ import annotations

import logging
import math
import os
import queue
import re
import threading
import time
import uuid
from dataclasses import dataclass
from typing import Any, Callable

import numpy as np

from reachy import senselog
from reachy.speech.engagement import ConversationGate, Decision
from reachy.speech.events import _doa_direction
from reachy.speech.stt import Transcriber

logger = logging.getLogger(__name__)

_STAGE_CAPTURE = "capture"
_STAGE_TRANSCRIPT = "transcript"
_SOURCE = "speech"

#: Truthy strings recognised by the ``REACHY_ENGAGE_HEURISTIC`` escape hatch.
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

#: Words counted by the coherence heuristic (letters + intra-word apostrophes).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Fallback mic rate used only until a real one can be read off the held client.
_FALLBACK_RATE = 16000

#: RMS level (float PCM, over one analysis window) a chunk must clear to count as
#: speech. Cited verbatim from the donor's onset threshold, itself cited from
#: ``reachy_nova``'s speech event detector: the mic hands out float32 PCM already
#: normalised to [-1, 1], so no int16 rescale applies.
#:
#: Since #102 this is a FLOOR under a relative threshold, not the threshold
#: itself — see :data:`DEFAULT_SPEECH_RATIO` and :meth:`TranscriptSenseDriver.
#: _speech_threshold`. Live-verified why: with the measured night background at
#: p50 0.034 this absolute gate is permanently open, and the journal fills with
#: ``utterance start`` -> ``dropped reason=stt-empty`` — the runtime recording
#: silence and POSTing it to the STT. Kept as the floor so a QUIET room's
#: capture behaviour is byte-identical to what shipped.
DEFAULT_SPEECH_RMS = 0.02

#: How many times the room's rolling background a chunk must stand to count as
#: speech, when a background estimate is wired and warm. Deliberately LOOSER
#: than orienting's :data:`reachy.behavior.rms_background.DEFAULT_RATIO` (5x):
#: capture and orienting have asymmetric costs. A missed utterance is
#: unrecoverable — the words are gone — while a wasted capture costs one STT
#: POST that returns empty, so hearing should start on less evidence than
#: turning the head does. 3x still clears the measured still-room spread, whose
#: samples top out at ~2.5x their own median in every measured condition, so an
#: empty room cannot start an utterance.
DEFAULT_SPEECH_RATIO = 3.0

#: Endpointing + pre-roll defaults, all carried over from the donor unchanged.
DEFAULT_SILENCE_HOLD_S = 0.7
DEFAULT_MAX_UTTERANCE_S = 15.0
DEFAULT_MIN_UTTERANCE_S = 0.3
DEFAULT_RING_SECONDS = 10.0
DEFAULT_PRE_ROLL_S = 2.0
DEFAULT_ONSET_WINDOW_S = 0.01
DEFAULT_MIN_WORDS = 3
DEFAULT_ENGAGE_WINDOW_S = 20.0

#: Canonical names the robot answers to (the donor's default).
DEFAULT_NAMES: tuple[str, ...] = ("reachy", "robot")

#: Bound on utterances awaiting transcription. Small on purpose: if the STT is
#: wedged, queueing more is pointless — the words are already stale by the time
#: they would be transcribed — and an unbounded queue is a memory leak.
DEFAULT_PENDING_MAXSIZE = 4

#: Bound on transcripts awaiting a tick to latch them. At 50 Hz the tick drains
#: one every 20 ms, so this only ever fills if the engine has stopped ticking.
DEFAULT_READY_MAXSIZE = 8

#: How long :meth:`close` waits for the worker to finish its current request.
DEFAULT_JOIN_TIMEOUT_S = 2.0

#: Sentinel pushed onto the pending queue to stop the worker.
_STOP = object()


def _env_truthy(value: str | None) -> bool:
    """Return ``True`` for the usual truthy env strings; ``False`` for unset/"0"/""."""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TranscriptTuning:
    """Grouped numeric knobs tuning HOW :class:`TranscriptSenseDriver` endpoints.

    Split out of the constructor (as the donor's ``TranscribeTuning`` is) so the
    SEAM parameters — media client, transcriber, classifier, callbacks — stay
    individual while the pure-number cluster travels as one value object. Every
    field keeps the donor's shipped default, so a bare ``TranscriptTuning()``
    reproduces the retiring loop's capture behaviour.

    Endpointing (whole-utterance accumulation, one POST per utterance):

    * ``speech_rms`` — absolute FLOOR under the speech threshold (the energy VAD
      that replaces the donor's unreliable daemon speech flag).
    * ``speech_ratio`` — times the room's rolling background a chunk must stand
      to count as speech. The effective threshold is
      ``max(speech_rms, speech_ratio * background)``, so a quiet room behaves
      exactly as the donor did and a loud one stops capturing its own hiss
      (#102). With no background seam wired, ``speech_rms`` alone applies.
    * ``silence_hold_s`` — pause length that ends an utterance and submits it.
    * ``max_utterance_s`` — hard cap that force-submits a long monologue.
    * ``min_utterance_s`` — floor below which a blip is dropped, never sent to
      STT (measured over *speech* samples only, so pre-roll cannot pad it).

    Pre-roll ring buffer + measured onset:

    * ``ring_seconds`` — horizon of the rolling pre-speech audio buffer.
    * ``pre_roll_s`` — lead-in kept before the measured onset.
    * ``onset_window_s`` — width of each RMS onset-scan analysis window.

    Engagement heuristic (the DEGRADE / no-classifier path only):

    * ``min_words`` — word-count floor for the "clear sentence" rule.
    * ``engage_window_s`` — how long a conversation stays open after an ENGAGE.
    """

    speech_rms: float = DEFAULT_SPEECH_RMS
    speech_ratio: float = DEFAULT_SPEECH_RATIO
    silence_hold_s: float = DEFAULT_SILENCE_HOLD_S
    max_utterance_s: float = DEFAULT_MAX_UTTERANCE_S
    min_utterance_s: float = DEFAULT_MIN_UTTERANCE_S
    ring_seconds: float = DEFAULT_RING_SECONDS
    pre_roll_s: float = DEFAULT_PRE_ROLL_S
    onset_window_s: float = DEFAULT_ONSET_WINDOW_S
    min_words: int = DEFAULT_MIN_WORDS
    engage_window_s: float = DEFAULT_ENGAGE_WINDOW_S


@dataclass(frozen=True)
class _Utterance:
    """One endpointed utterance handed across the queue to the worker."""

    audio: Any
    direction: str | None
    t: float
    event_id: str


class TranscriptSenseDriver:
    """A ``TickBus`` driver latching heard WORDS as a one-tick perception cue.

    Construct one with the runtime's single held media client, register
    :meth:`__call__` on the engine's ``tick_seam``, wire
    ``SenseProviders(transcript=driver.as_provider())``, and call :meth:`close`
    at shutdown (it stops the worker thread; it does NOT close the media client,
    which the composition root owns).

    Parameters
    ----------
    media:
        The process's ONE held media client, duck-typed: an ``audio()`` returning
        a float32 mic chunk (or ``None``) plus a ``samplerate`` property. Injected,
        never constructed — this module opens no media session and imports no SDK.
        Because the holder's FIRST read can block for order-of-seconds, the owner
        should warm it up off-thread and construct it with
        ``allow_inline_connect=False``; this driver only reads.
    transcriber:
        The :class:`~reachy.speech.stt.Transcriber` doing the work (duck-typed on
        ``transcribe_once``). Defaults to a real one, built lazily once the mic's
        true sample rate is known so the WAV header matches the audio — a wrong
        rate makes the STT mis-decode and return nothing. Construction performs no
        network I/O.
    classifier:
        Optional :class:`~reachy.speech.engagement.EngagementClassifier`-like
        object (``judge(text, context) -> bool``) used by the layered gate.
        ``None`` (the default) keeps the gate on the pure heuristic with no
        classifier call ever made.
    on_engage:
        Optional zero-arg callback fired EXACTLY ONCE per ENGAGE decision, never
        on a drop. Wire it to whatever should orient toward the speaker. It runs
        on the WORKER thread and is guarded: a fault is logged and swallowed, and
        never stops the words reaching the latch.
    mute_until:
        Zero-arg callable returning the monotonic deadline until which the robot
        is self-muted. While ``now < mute_until()`` the tick discards the chunk
        AND the pre-roll ring, so the robot never transcribes its own voice.
        Defaults to "never muted".
    tuning:
        A :class:`TranscriptTuning`; see its docstring.
    names:
        Canonical names for the gate's fast path.
    clock:
        Monotonic clock used only when ``ctx.now`` is unusable. Injectable.
    """

    def __init__(
        self,
        *,
        media: Any,
        transcriber: Any | None = None,
        classifier: Any | None = None,
        on_engage: Callable[[], None] | None = None,
        mute_until: Callable[[], float] | None = None,
        background: Callable[[], float | None] | None = None,
        tuning: TranscriptTuning = TranscriptTuning(),
        names: tuple[str, ...] = DEFAULT_NAMES,
        clock: Callable[[], float] = time.monotonic,
        pending_maxsize: int = DEFAULT_PENDING_MAXSIZE,
        ready_maxsize: int = DEFAULT_READY_MAXSIZE,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    ) -> None:
        self._media = media
        self._transcriber = transcriber
        self._on_engage = on_engage
        self._mute_until = mute_until if mute_until is not None else (lambda: 0.0)
        #: Non-consuming peek at the room's rolling background level (#102),
        #: wired at composition to the SAME
        #: :class:`reachy.behavior.rms_background.RmsBackground` the orienting
        #: gate reads — one estimator, two consumers with two different
        #: thresholds, never two estimators that could disagree about the room.
        #: ``None`` (or a ``None`` reading, i.e. a cold estimate) falls back to
        #: the absolute floor, which is exactly the pre-#102 behaviour.
        self._background = background
        self._tuning = tuning
        self._names = tuple(name.lower() for name in names)
        self._clock = clock
        self._join_timeout_s = max(0.0, float(join_timeout_s))

        # --- rate-dependent sizes, resolved lazily after the first real read ---
        self._rate: int | None = None
        self._min_utt_samples = 0
        self._ring_max = 0
        self._pre_roll_samples = 0
        self._onset_window = 1

        # --- tick-thread capture state (touched by NO other thread) -----------
        self._utt: list[np.ndarray] = []
        self._utt_samples = 0
        self._utt_speech_samples = 0
        self._utt_started_t: float | None = None
        self._last_speech_t: float | None = None
        self._utt_direction: str | None = None
        self._event_id: str | None = None
        self._ring: list[np.ndarray] = []
        self._ring_samples = 0
        self._ring_total = 0

        # --- the one-tick latch (written by the tick thread only) -------------
        self._latch: str | None = None
        self._latch_direction: str | None = None

        # --- the handoff ------------------------------------------------------
        self._pending: queue.Queue = queue.Queue(maxsize=max(1, int(pending_maxsize)))
        self._ready: queue.Queue = queue.Queue(maxsize=max(1, int(ready_maxsize)))
        #: The background worker, started lazily on the first submitted utterance
        #: so a runtime that never hears speech never spawns a thread.
        self.worker: threading.Thread | None = None
        self._closed = False

        # --- worker-thread gate state (touched by NO other thread) ------------
        self._force_heuristic = _env_truthy(os.environ.get("REACHY_ENGAGE_HEURISTIC"))
        #: The stateful engagement gate (#105), or ``None`` when the pure
        #: heuristic is in force — the escape hatch, or no classifier injected.
        #: ``None`` means no classifier is ever built OR called, which is exactly
        #: what ``REACHY_ENGAGE_HEURISTIC=1`` promises.
        #:
        #: Its two thresholds are the tuning's own ``engage_window_s`` /
        #: ``min_words``, so the classifier path and the DEGRADE heuristic path
        #: share ONE definition of "the conversation is still going" and ONE
        #: word floor, rather than disagreeing about the same room.
        self._gate: ConversationGate | None = (
            None
            if (self._force_heuristic or classifier is None)
            else ConversationGate(
                classifier=classifier,
                names=self._names,
                warm_window_s=tuning.engage_window_s,
                min_context_words=tuning.min_words,
            )
        )
        self._engaged_until = 0.0

        #: Diagnostics / tests: ticks processed, utterances submitted to the
        #: worker, transcripts that cleared the gate and reached the ready queue.
        self.ticks = 0
        self.submitted = 0
        self.transcripts = 0

    # ------------------------------------------------------------------
    # TickBus driver entry point (TICK THREAD)
    # ------------------------------------------------------------------

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """One tick: clear the latch, capture audio, and maybe adopt a ready transcript.

        Never raises and never blocks on I/O. The latch is cleared first — a
        plain assignment that cannot fail, preserving the one-tick contract on
        every path — then the body runs under a broad guard so a misbehaving
        media client degrades to "no words this tick".
        """
        # Clear-before-process (see PatSenseDriver's r2): a transcript latched
        # last tick has already been read by this tick's start-of-tick sense.
        self._latch = None
        self._latch_direction = None
        self.ticks += 1
        if self._closed:
            return
        try:
            self._process(ctx)
        # A sense tap must never crash the loop.
        except Exception:  # noqa: BLE001
            logger.warning("TranscriptSenseDriver tick raised; transcript dropped", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """The capture body, split out so :meth:`__call__` stays a thin guard."""
        now = self._now(ctx)
        self._adopt_ready()

        # Read the mic EVERY tick, even while muted or mid-drop, so the held
        # client's buffer keeps draining and a co-riding loudness provider sees
        # the same sample. Never a second reader, never a second session.
        chunk = self._read_audio()
        if chunk is None:
            # No audio this tick: it still counts as silence for endpointing, so
            # an utterance that ends with the mic going quiet still submits.
            self._maybe_submit_on_pause(now)
            return

        if now < self._mute_until():
            # The robot is speaking: discard the partial utterance AND the ring,
            # so its own voice is never pre-rolled nor transcribed.
            if self._utt:
                senselog.drop(_STAGE_CAPTURE, _SOURCE, self._event_id or "?", "self-mute")
            self._reset_utt()
            return

        self._push_ring(chunk)
        if self._is_speech(chunk):
            self._on_speech_tick(ctx, chunk, now)
            return
        self._maybe_submit_on_pause(now)

    def _on_speech_tick(self, ctx, chunk: np.ndarray, now: float) -> None:  # type: ignore[no-untyped-def]  # noqa: E501
        """Accumulate one speech chunk, seeding the utterance on the rising edge."""
        if self._utt_samples == 0:
            # Rising edge: seed from the ring at the measured onset minus the
            # pre-roll. The triggering chunk is already in the ring (pushed
            # above), so it must NOT be appended again.
            self._begin_utterance(ctx, now, int(chunk.size))
        else:
            self._utt.append(chunk)
            self._utt_samples += int(chunk.size)
            self._utt_speech_samples += int(chunk.size)
        self._last_speech_t = now
        started = self._utt_started_t
        if started is not None and (now - started) >= self._tuning.max_utterance_s:
            self._submit(now)  # cap a very long monologue

    def _maybe_submit_on_pause(self, now: float) -> None:
        """End the utterance once speech has been absent for ``silence_hold_s``."""
        if (
            self._utt
            and self._last_speech_t is not None
            and (now - self._last_speech_t) >= self._tuning.silence_hold_s
        ):
            self._submit(now)

    # ------------------------------------------------------------------
    # The handoff — tick side (NON-BLOCKING BY CONSTRUCTION)
    # ------------------------------------------------------------------

    def _submit(self, now: float) -> None:
        """Hand the finished utterance to the worker; never blocks, never raises.

        This is the whole point of the module: the tick's involvement with a
        transcript ENDS here, at a ``put_nowait``. Everything downstream — the
        STT POST, the engagement classifier's call — happens on the worker.
        """
        speech_samples = self._utt_speech_samples
        direction = self._utt_direction
        utt = self._utt
        event_id = self._event_id or "?"
        self._reset_utt()

        if not utt or speech_samples < self._min_utt_samples:
            # A blip, not speech. The gate measures SPEECH samples only, so the
            # pre-roll lead-in can never pad a blip past the floor.
            senselog.drop(_STAGE_CAPTURE, _SOURCE, event_id, "min-utterance")
            return

        audio = utt[0] if len(utt) == 1 else np.concatenate(utt)
        self._ensure_worker()
        try:
            self._pending.put_nowait(_Utterance(audio, direction, now, event_id))
        except queue.Full:
            # A wedged/slow STT has backed the queue up. Dropping is correct:
            # blocking would blow the tick budget, and the words are stale.
            senselog.drop(_STAGE_CAPTURE, _SOURCE, event_id, "stt-backlog")
            return
        self.submitted += 1

    def _adopt_ready(self) -> None:
        """Latch at most one transcript the worker has finished. Never blocks."""
        try:
            text, direction = self._ready.get_nowait()
        except queue.Empty:
            return
        self._latch = text
        self._latch_direction = direction

    def _ensure_worker(self) -> None:
        """Start the background worker on first use (tick thread only, no race)."""
        if self.worker is not None or self._closed:
            return
        self.worker = threading.Thread(
            target=self._worker_loop,
            name="reachy-transcript-sense",
            daemon=True,
        )
        self.worker.start()

    # ------------------------------------------------------------------
    # The worker (BACKGROUND THREAD) — every network call lives here
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Drain submitted utterances: transcribe, gate, publish. Never raises out."""
        while True:
            job = self._pending.get()
            if job is _STOP:
                return
            try:
                self._handle(job)
            # A worker fault must cost one utterance, never the worker.
            except Exception:  # noqa: BLE001
                logger.warning("TranscriptSenseDriver worker degraded", exc_info=True)

    def _handle(self, job: _Utterance) -> None:
        """Transcribe one utterance, run the engagement gate, publish if admitted."""
        transcriber = self._transcriber
        if transcriber is None:
            return
        text = transcriber.transcribe_once(job.audio)
        if not text:
            senselog.drop(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, "stt-empty")
            return
        engaged, label = self._decide(text, job.t)
        if not engaged:
            # Not addressed to the robot: ambient speech, a backchannel too short
            # to carry addressing signal, or no conversation open to continue.
            # The gate's own label is the reason, so the journal distinguishes
            # the three rather than collapsing them into one word.
            senselog.drop(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, label)
            return
        self._notify_engaged()
        try:
            self._ready.put_nowait((text, job.direction))
        except queue.Full:
            # The engine has stopped draining. Drop the OLDEST so the latch
            # always carries the freshest words rather than a stale backlog.
            self._drop_oldest_ready()
            try:
                self._ready.put_nowait((text, job.direction))
            except queue.Full:
                senselog.drop(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, "latch-backlog")
                return
        self._engaged_until = job.t + self._tuning.engage_window_s
        self.transcripts += 1
        senselog.stage(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, f'heard "{text[:60]}"')

    def _drop_oldest_ready(self) -> None:
        try:
            self._ready.get_nowait()
        except queue.Empty:
            return

    def _notify_engaged(self) -> None:
        """Fire ``on_engage`` once for an ENGAGE decision (guarded, worker thread)."""
        if self._on_engage is None:
            return
        try:
            self._on_engage()
        except Exception:  # noqa: BLE001 — a turn-signal fault must not lose the words
            logger.warning("TranscriptSenseDriver on_engage raised; ignoring", exc_info=True)

    # ------------------------------------------------------------------
    # The engagement gate (WORKER THREAD) — reused, not reimplemented
    # ------------------------------------------------------------------

    def _decide(self, text: str, t: float) -> tuple[bool, str]:
        """Layered engagement decision — the #54/#56 gate, now conversation-aware.

        Two paths, chosen once at construction:

        * **Pure heuristic** — the ``REACHY_ENGAGE_HEURISTIC`` escape hatch, or
          no classifier injected. Byte-identical to what shipped, and no
          classifier is ever built or called.
        * **The gate** — :class:`~reachy.speech.engagement.ConversationGate`,
          which runs the name fast-path, the short-utterance rule and the
          warm-window rule before spending at most one classifier call, and owns
          the conversation history that used to be a one-way ratchet here
          (issue #105; the argument is in that module's docstring). A DEGRADE
          still falls back to :meth:`_should_engage` so the hearing loop never
          stalls on a dead endpoint — and a heuristic accept is reported back to
          the gate, so a run of degraded turns cannot strand it cold.

        Returns the decision and its LABEL. The label is the caller's
        ``senselog.drop`` reason, so a drop always says which rule dropped it.
        """
        gate = self._gate
        if gate is None:
            engaged = self._should_engage(text, t)
            label = "engaged-heuristic" if engaged else "dropped-heuristic"
        else:
            verdict = gate.decide(text, t)
            if verdict.decision is Decision.DEGRADE:
                engaged = self._should_engage(text, t)
                label = "degrade->heuristic"
                if engaged:
                    gate.note_engaged(text, t)
            else:
                engaged = verdict.decision is Decision.ENGAGE
                label = verdict.label

        logger.info('engagement: %s :: "%s"', label, text[:40])
        return engaged, label

    def _should_engage(self, text: str, t: float) -> bool:
        """The cheap fallback rule: named, or a clear sentence in an open window.

        The name match is WHOLE-WORD, not a substring, so "robotic"/"robots" do
        not falsely trigger on the name "robot".
        """
        words = _WORD_RE.findall(text.lower())
        if any(name in words for name in self._names):
            return True
        coherent = len(words) >= self._tuning.min_words
        return coherent and t < self._engaged_until

    # ------------------------------------------------------------------
    # Capture internals (TICK THREAD)
    # ------------------------------------------------------------------

    def _read_audio(self) -> np.ndarray | None:
        """One mic chunk off the injected held client, degrading every failure.

        Returns ``None`` for "no audio this tick" — a cold/disconnected holder, a
        read that raised, or a genuinely empty chunk. The first successful read
        is also where the real mic sample rate is resolved: querying it earlier
        could trigger the holder's blocking construction on this very thread.
        """
        try:
            raw = self._media.audio()
        # A raising media client degrades, never propagates.
        except Exception:  # noqa: BLE001
            logger.debug("TranscriptSenseDriver media read raised; no audio", exc_info=True)
            return None
        if raw is None:
            return None
        try:
            chunk = np.asarray(raw, dtype=np.float32).reshape(-1)
        except (TypeError, ValueError):
            return None
        if chunk.size == 0:
            return None
        if self._rate is None:
            self._resolve_rate()
        return chunk

    def _resolve_rate(self) -> None:
        """Adopt the mic's real rate and size every rate-dependent threshold.

        Called only after a successful :meth:`_read_audio`, which proves the
        holder is connected — so this property read is free rather than a
        blocking connect.
        """
        rate: Any = None
        try:
            rate = self._media.samplerate
        except Exception:  # noqa: BLE001
            rate = None
        try:
            self._rate = int(rate) if rate else _FALLBACK_RATE
        except (TypeError, ValueError):
            self._rate = _FALLBACK_RATE
        tuning = self._tuning
        self._min_utt_samples = int(max(0.0, tuning.min_utterance_s) * self._rate)
        self._ring_max = int(max(0.0, tuning.ring_seconds) * self._rate)
        self._pre_roll_samples = int(max(0.0, tuning.pre_roll_s) * self._rate)
        self._onset_window = max(1, int(tuning.onset_window_s * self._rate))
        if self._transcriber is None:
            # Built with the REAL rate so the WAV header matches the audio; a
            # wrong rate makes the STT mis-decode and return nothing. No I/O.
            self._transcriber = Transcriber(sample_rate=self._rate)

    def _speech_threshold(self) -> float:
        """The rms a chunk must clear to count as speech, for THIS room (#102).

        ``max(speech_rms, speech_ratio * background)``: the shipped absolute
        value becomes a FLOOR under a relative threshold, which is the shape
        :class:`reachy.motion.snap.SnapDetector` always had (``min_rms`` inside
        a ratio test) and the shape the orienting gate now uses too. So capture
        can never become LESS sensitive than what shipped, and can no longer sit
        permanently open in a room whose background has drifted above it.

        Never raises: a missing, raising or cold background peek falls back to
        the floor — the pre-#102 behaviour — because a sense tap must not be
        able to crash the 20 ms tick it runs on.
        """
        floor = float(self._tuning.speech_rms)
        if self._background is None:
            return floor
        try:
            level = self._background()
        except Exception:  # noqa: BLE001 — a peek failure means "no estimate"
            return floor
        if level is None or not isinstance(level, (int, float)):
            return floor
        level = float(level)
        if not math.isfinite(level) or level < 0.0:
            return floor
        return max(floor, level * float(self._tuning.speech_ratio))

    def _is_speech(self, chunk: np.ndarray) -> bool:
        """Energy VAD over the chunk (see the module docstring's deviation note)."""
        return self._rms(chunk) >= self._speech_threshold()

    @staticmethod
    def _rms(chunk: np.ndarray) -> float:
        if chunk.size == 0:
            return 0.0
        return float(np.sqrt(np.mean(np.square(chunk))))

    def _begin_utterance(self, ctx, now: float, speech_samples: int) -> None:  # type: ignore[no-untyped-def]  # noqa: E501
        """Seed a new utterance with measured-onset pre-roll from the ring buffer.

        Called on the VAD's rising edge. The triggering chunk is already in the
        ring, so the seeded slice includes it. The onset is MEASURED (an RMS scan
        of the buffered audio) and the utterance starts at ``onset - pre_roll``
        clamped to the ring start, so a quiet leading phoneme is kept.
        """
        self._utt_started_t = now
        self._utt_direction = self._direction_of(ctx)
        self._utt_speech_samples = int(speech_samples)
        self._event_id = uuid.uuid4().hex[:8]

        snapshot = self._concat_ring()  # one concat, rising edge only
        buffer_start = self._ring_total - self._ring_samples
        onset_offset = self._measure_onset(snapshot)
        onset_absolute = buffer_start + onset_offset
        clip_start = max(buffer_start, onset_absolute - self._pre_roll_samples)
        preroll = snapshot[clip_start - buffer_start :]
        self._utt = [preroll] if preroll.size else []
        self._utt_samples = int(preroll.size)

        rate = self._rate or _FALLBACK_RATE
        senselog.stage(
            _STAGE_CAPTURE,
            _SOURCE,
            self._event_id,
            f"utterance start pre_roll={(onset_absolute - clip_start) / rate:.2f}s "
            f"buffered={self._ring_samples}",
        )

    def _push_ring(self, chunk: np.ndarray) -> None:
        """Append a chunk to the rolling pre-roll ring (cheap; trimmed by samples)."""
        self._ring.append(chunk)
        self._ring_samples += int(chunk.size)
        self._ring_total += int(chunk.size)
        while len(self._ring) > 1 and self._ring_samples - self._ring[0].size >= self._ring_max:
            self._ring_samples -= int(self._ring.pop(0).size)

    def _concat_ring(self) -> np.ndarray:
        """Concatenate the ring into one float32 snapshot (rising edge only)."""
        if not self._ring:
            return np.zeros(0, dtype=np.float32)
        if len(self._ring) == 1:
            return self._ring[0]
        return np.concatenate(self._ring)

    def _measure_onset(self, snapshot: np.ndarray) -> int:
        """First window offset whose RMS clears the speech threshold, else 0.

        A MEASUREMENT over the buffered audio, not an assumed fixed offset, so
        the emitted clip's lead-in tracks where energy actually rises. Falls back
        to the ring start when nothing clears the threshold.
        """
        win = self._onset_window
        threshold = self._speech_threshold()
        for start in range(0, int(snapshot.size), win):
            window = snapshot[start : start + win]
            if window.size and self._rms(window) >= threshold:
                return start
        return 0

    def _reset_utt(self) -> None:
        """Clear the utterance accumulator AND the pre-roll ring.

        The ring goes with it so the next utterance's onset scan only sees audio
        captured after this one ended (or after the robot stopped speaking) — no
        bleed-through of previous words, or of the robot's own voice.
        """
        self._utt = []
        self._utt_samples = 0
        self._utt_speech_samples = 0
        self._utt_started_t = None
        self._last_speech_t = None
        self._utt_direction = None
        self._event_id = None
        self._ring = []
        self._ring_samples = 0
        self._ring_total = 0

    # ------------------------------------------------------------------
    # Provider seam
    # ------------------------------------------------------------------

    def peek(self) -> str | None:
        """The current latch — a non-consuming PEEK, safe to call many times a tick.

        Directly usable as ``SenseProviders(transcript=driver.peek)``. Returns the
        transcript adopted by the most recent :meth:`__call__`, or ``None``.
        Never raises, never blocks.
        """
        return self._latch

    def as_provider(self) -> Callable[[], str | None]:
        """The zero-arg ``transcript`` provider callable (an alias for :meth:`peek`).

        Mirrors :meth:`reachy.behavior.pat_sense.PatSenseDriver.as_provider` so
        composition reads symmetrically across the sense providers.
        """
        return self.peek

    def peek_direction(self) -> str | None:
        """The direction word the latched utterance came from, or ``None``.

        Latched and cleared on the same one-tick cadence as :meth:`peek`, so it
        always describes the transcript peeked in the same tick. Preserves the
        donor's ``feed_transcript(text, direction=...)`` information, which the
        plain string field cannot carry.
        """
        return self._latch_direction

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the worker thread. Idempotent, never raises.

        Does NOT close the media client: the composition root owns exactly one
        and other senses read it too. After ``close()`` every tick is a no-op
        that still clears the latch, so a late tick is always safe.
        """
        if self._closed:
            return
        self._closed = True
        worker = self.worker
        if worker is None:
            return
        try:
            self._pending.put_nowait(_STOP)
        except queue.Full:
            # Make room: a wedged queue must not stop us signalling the worker.
            try:
                self._pending.get_nowait()
                self._pending.put_nowait(_STOP)
            except (queue.Empty, queue.Full):
                logger.debug("TranscriptSenseDriver: could not enqueue worker stop")
        worker.join(timeout=self._join_timeout_s)

    def __enter__(self) -> "TranscriptSenseDriver":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # Defensive readers of the (duck-typed) TickContext
    # ------------------------------------------------------------------

    def _now(self, ctx) -> float:  # type: ignore[no-untyped-def]
        """This tick's clock reading — ``ctx.now`` when usable, else the fallback.

        Unlike :class:`~reachy.behavior.pat_sense.PatSenseDriver` this driver
        needs a real number (endpointing is entirely time-based), so a missing or
        non-finite ``ctx.now`` falls back to the injected clock rather than
        degrading to ``None``.
        """
        now = getattr(ctx, "now", None)
        if isinstance(now, (int, float)) and not isinstance(now, bool):
            value = float(now)
            if math.isfinite(value):
                return value
        return float(self._clock())

    @staticmethod
    def _direction_of(ctx) -> str | None:  # type: ignore[no-untyped-def]
        """Direction word for this tick's DoA reading (radians → label), or ``None``."""
        sense = getattr(ctx, "sense", None)
        angle = getattr(sense, "doa_angle", None)
        if not isinstance(angle, (int, float)) or isinstance(angle, bool):
            return None
        try:
            return _doa_direction(float(angle))
        except Exception:  # noqa: BLE001 — a bad angle must never drop the words
            return None
