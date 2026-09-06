"""Transcript sense for the 50 Hz behavior runtime — hearing WORDS, not just sound.

The symbolic runtime senses direction, loudness and touch; this driver is the
path from the microphone to *what was actually said*, so a data-only rule
(``when {field=transcript, op=is_true}``) and an externally attached agent can
both reason about speech.

It is built exactly like :class:`reachy.behavior.pat_sense.PatSenseDriver` — a
``TickBus`` driver that WRITES a one-tick latch at the end of every tick, plus a
zero-arg PEEK provider (:meth:`peek` / :meth:`as_provider`) that
:func:`reachy.behavior.sense.read_perception` folds into the next tick's
:class:`~reachy.behavior.sense.Sense`. Read that module's "r2 — cadence + one-tick
latch semantics" section: the discipline is identical here, including the
clear-BEFORE-process rule that holds on every path, so a transcript is delivered
to exactly ONE sense snapshot and multiple peeks within a tick agree.

--------------------------------------------------------------------------
Endpointing lives on the SERVER now (the realtime arc, issue #115)
--------------------------------------------------------------------------
This driver used to decide *itself* where an utterance started and stopped: an
energy VAD over a rolling pre-roll ring, a silence-hold timer, a monologue cap,
a measured onset, a minimum-span floor — and then one
``POST /v1/audio/transcriptions`` per finished clip. All of that is gone. The
capture half is now:

    tick: chunk = media.audio() -> realtime.submit_audio(chunk)
    tick: utterance = realtime.take_utterance()  (or None)

:class:`reachy.speech.realtime.RealtimeTranscriber` holds ONE long-lived
WebSocket session to the lobes ``/v1/realtime`` route, streams every mic chunk
into it, and lets the server's ``server_vad`` say where the sentence ended. What
comes back is an already-endpointed :class:`~reachy.speech.realtime.Utterance`.
The client is **injected**, never constructed here (see the ``realtime``
parameter): this module opens no socket, imports no wire primitive, and does not
own the session's lifecycle — the composition root starts and closes it, exactly
as it does the held media client.

**There is no fallback** (the arc's confirmed operator decision c17). When the
session is down, hearing goes quiet and the client reconnects on its own
schedule with its own latched ``session-down`` drop; nothing here re-endpoints
locally. Keeping the old energy VAD as a standby would mean two capture paths
whose disagreements only ever show up in the field.

What #108 taught SURVIVES its machinery, and it is the reason this driver
forwards audio rather than choosing it: an energy predicate is a **locator**,
never a content filter. The old code appended only chunks that individually
cleared the threshold, so every stop closure and inter-word gap *inside* a
sentence was excised and the survivors glued edge to edge — live, that turned
``'Richie, are you there?'`` into ``'Reaching there.'``, then ``'Return.'``,
then ``'Yeah.'`` as the room got louder. Nothing here may reacquire the habit:
**every sample the mic hands over goes to the session, in order, exactly once**,
and the only audio deliberately withheld is the robot's own voice (below).

--------------------------------------------------------------------------
Why a background worker (the load-bearing difference from ``PatSenseDriver``)
--------------------------------------------------------------------------
A pat is sensed with arithmetic. The STT round-trip left this module with the
session client, but the **engagement classifier is still a network call**, and
doing it inline would blow the 20 ms tick budget by orders of magnitude. This is
not hypothetical: the deployed box already shows a reproducible startup overrun
of **424.93-1212.66 ms against a 20 ms budget** caused by exactly this class of
on-thread blocking (a media client constructed on the tick thread —
``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).

So the work is still split across two threads with a queue between them:

* **The tick thread** (:meth:`__call__`) does only cheap, bounded work: read one
  mic chunk from the injected held media client, hand it to the session client's
  O(1) ``submit_audio``, pop at most a few ready utterances with the equally
  O(1) ``take_utterance``, and forward each to the worker with a NON-BLOCKING
  ``put_nowait``. It also drains at most one admitted transcript into the latch.
  **No socket is ever touched here** — the session client's own worker thread
  owns the wire, and ``tests/test_behavior_transcript_realtime.py`` asserts that
  structurally, over the AST, in both modules.
* **The worker thread** (:meth:`_worker_loop`, started lazily on the first
  utterance) runs the engagement gate, fires ``on_engage``, and publishes an
  accepted transcript onto the ready queue.

Every queue is BOUNDED and every put is non-blocking: a wedged classifier backs
the pending queue up, and further utterances are dropped with a logged reason
rather than growing memory or — far worse — blocking the tick. An unreachable
gateway therefore leaves the field ``None`` and drops **no ticks**.

--------------------------------------------------------------------------
The engagement gate is reused, never reimplemented
--------------------------------------------------------------------------
Admission is unchanged by the realtime arc: it is
:class:`reachy.speech.engagement.ConversationGate` — the #54/#56 layered gate
plus the #105 conversation state — receiving exactly the utterance shape it
always did (a string plus the instant it was heard):

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
Self-mute moved to ARRIVAL, and it is now two guards, not one
--------------------------------------------------------------------------
The mic and the speaker share a room, so without a self-mute the runtime
transcribes its own voice, the transcript fires a rule, the rule speaks, and the
robot talks to itself forever. :class:`reachy.behavior.speech_act.SpeechActuator`
publishes the monotonic window its clip occupies; ``mute_until`` closes the loop.

With server-side VAD that check cannot live where the audio is captured alone,
because **the server's VAD cannot know when the robot is speaking**: an
utterance it already committed can be transcribed and delivered while the
speaker is mid-sentence. So:

* **Outbound** — while ``now < mute_until()`` the tick does not feed the session
  (the robot's voice never reaches the server's VAD at all). Audio arrives 50
  times a second, so that drop is LATCHED: one ``self-mute`` line per mute
  episode, not fifty per second (the #99 journal-flood discipline the session
  client applies to its own down state).
* **Inbound** — a transcript whose ``Utterance.t`` (the monotonic instant it
  ARRIVED, stamped by the session client) falls inside the mute window is
  discarded with the same named ``self-mute`` reason before it can reach the
  gate. That instant is on the same clock ``mute_until`` returns, which is what
  makes the comparison meaningful without this module keeping a clock of its own.

The inbound guard is deliberately blunt: a transcript of the HUMAN that happens
to land mid-clip is discarded too. That is the safe direction — a lost turn
costs one repetition, while a self-heard turn costs an unbounded feedback loop.

--------------------------------------------------------------------------
Deviations from the donor, and why
--------------------------------------------------------------------------
* **The audio source is the injected held media client**, not a per-tick
  ``SenseSample``. The driver calls ``media.audio()`` once per tick and NEVER
  constructs a client, opens a media session, or imports the SDK — the runtime's
  composition root owns exactly one client and injects it (see
  :mod:`reachy.robot.media_client`). In the composed runtime that injection is
  the ``_AudioTap`` fan-out, so this is not a second consuming read (#100).
* **The mic sample rate is resolved lazily, after a successful read**, and then
  pushed into the session (``set_sample_rate``) — touching ``media.samplerate``
  on a cold holder can trigger construction, so it is read only once ``audio()``
  has already returned a chunk (proving the client is up). The rate rides the
  session's connect URL and the server resamples from it, so a wrong rate
  mis-times every VAD decision; it is never hard-coded here.
* **``EventBuffer`` is gone.** The donor fed words into ``think``'s cognition
  buffer; here the accepted transcript becomes a latched perception field. Any
  cognition is external (``agent attach``), reading the same ``Sense``.

Every failure degrades to "no words" and never raises out of the driver or the
provider, mirroring :func:`reachy.behavior.sense._peek`.

Standard library plus numpy and the existing speech engines — no new dependency.
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
from reachy.robot.audio_shape import to_mono
from reachy.speech.engagement import DEFAULT_NAMES as _SHIPPED_NAMES
from reachy.speech.engagement import (
    LABEL_NAME,
    ConversationGate,
    Decision,
    NamesLike,
    resolve_names,
)
from reachy.speech.realtime import Utterance

logger = logging.getLogger(__name__)

_STAGE_CAPTURE = "capture"
_STAGE_TRANSCRIPT = "transcript"
_SOURCE = "speech"

#: Event id used for the LATCHED, stream-level capture lines (a continuous
#: stream has no per-event identity; a per-chunk uuid would be noise).
_STREAM_EVENT = "stream"

#: Named drop reasons this module owns. The session client's own reasons
#: (``session-down``, ``connect-failed``, ``queue-full``, ...) are emitted by
#: :mod:`reachy.speech.realtime` and never duplicated here.
REASON_SELF_MUTE = "self-mute"
REASON_NO_SESSION = "no-realtime-session"
REASON_EMPTY_TRANSCRIPT = "empty-transcript"
REASON_GATE_BACKLOG = "gate-backlog"
REASON_LATCH_BACKLOG = "latch-backlog"

#: Truthy strings recognised by the ``REACHY_ENGAGE_HEURISTIC`` escape hatch.
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

#: Words counted by the coherence heuristic (letters + intra-word apostrophes).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Fallback mic rate used only until a real one can be read off the held client.
_FALLBACK_RATE = 16000

#: Engagement defaults (the only tuning left — endpointing is the server's).
DEFAULT_MIN_WORDS = 3
DEFAULT_ENGAGE_WINDOW_S = 20.0

#: Canonical names the robot answers to — an ALIAS of the ONE place the shipped
#: pair is spelled (:data:`reachy.speech.name_match.SHIPPED_NAMES`, re-exported
#: as :data:`reachy.speech.engagement.DEFAULT_NAMES`). Reached through the
#: engagement module on purpose: ``reachy.speech.engagement`` is already on the
#: zero-LLM boundary's allow-list for this package, and a direct
#: ``reachy.speech.name_match`` edge would be a NEW one for a constant.
#:
#: A runtime whose names are CONFIGURABLE injects ``names_provider=`` instead;
#: this constant is only what a driver falls back to when nobody configured
#: anything.
DEFAULT_NAMES: tuple[str, ...] = _SHIPPED_NAMES

#: Bound on utterances awaiting the engagement gate. Small on purpose: if the
#: classifier is wedged, queueing more is pointless — the words are already
#: stale by the time they would be judged — and an unbounded queue is a leak.
DEFAULT_PENDING_MAXSIZE = 4

#: Bound on transcripts awaiting a tick to latch them. At 50 Hz the tick drains
#: one every 20 ms, so this only ever fills if the engine has stopped ticking.
DEFAULT_READY_MAXSIZE = 8

#: How many ready utterances one tick may pop off the session client. Bounded so
#: a burst can never turn one tick into an unbounded loop; at conversational
#: rates the queue holds at most one.
DEFAULT_MAX_TAKES_PER_TICK = 4

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
    """Grouped numeric knobs tuning HOW :class:`TranscriptSenseDriver` admits.

    Split out of the constructor so the SEAM parameters — media client, session
    client, classifier, callbacks — stay individual while the pure-number
    cluster travels as one value object.

    Both surviving fields belong to the **engagement heuristic** (the DEGRADE /
    no-classifier path) and are also the two thresholds handed to the stateful
    :class:`~reachy.speech.engagement.ConversationGate`, so the classifier path
    and the fallback share ONE definition of each:

    * ``min_words`` — word-count floor for the "clear sentence" rule.
    * ``engage_window_s`` — how long a conversation stays open after an ENGAGE.

    The endpointing cluster this class used to carry (``speech_rms`` /
    ``speech_ratio`` / ``silence_hold_s`` / ``max_utterance_s`` /
    ``min_utterance_s`` / ``ring_seconds`` / ``pre_roll_s`` / ``onset_window_s``)
    is GONE, not defaulted: the lobes ``/v1/realtime`` session decides where an
    utterance starts and stops (see the module docstring). A box that wants to
    tune endpointing tunes the server's ``turn_detection`` config, not this.
    """

    min_words: int = DEFAULT_MIN_WORDS
    engage_window_s: float = DEFAULT_ENGAGE_WINDOW_S


@dataclass(frozen=True)
class _Heard:
    """One arrived transcript on its way to the engagement gate (worker-bound)."""

    text: str
    direction: str | None
    t: float
    event_id: str


class TranscriptSenseDriver:
    """A ``TickBus`` driver latching heard WORDS as a one-tick perception cue.

    Construct one with the runtime's single held media client and its single
    realtime session client, register :meth:`__call__` on the engine's
    ``tick_seam``, wire ``SenseProviders(transcript=driver.as_provider())``, and
    call :meth:`close` at shutdown (it stops the worker thread; it does NOT close
    the media client OR the session client, both of which the composition root
    owns).

    Parameters
    ----------
    media:
        The process's ONE held media client, duck-typed: an ``audio()`` returning
        a float32 mic chunk (or ``None``) plus a ``samplerate`` property. Injected,
        never constructed — this module opens no media session and imports no SDK.
        Because the holder's FIRST read can block for order-of-seconds, the owner
        should warm it up off-thread and construct it with
        ``allow_inline_connect=False``; this driver only reads.
    realtime:
        The :class:`~reachy.speech.realtime.RealtimeTranscriber` session client,
        duck-typed on ``submit_audio(chunk)`` / ``take_utterance()`` (plus an
        optional ``set_sample_rate``). Injected and NOT owned: the composition
        root constructs it, calls ``start()`` before the first tick (a connect is
        blocking work that belongs to setup), and closes it at shutdown.
        ``None`` — the default — means the runtime has no hearing session wired:
        the mic is still read (so a co-riding loudness provider sees the same
        sample) and the transcript field stays permanently ``None``, announced
        ONCE as a named ``no-realtime-session`` drop rather than silently.
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
        is self-muted. While ``now < mute_until()`` the tick feeds the session no
        audio, and any transcript that ARRIVES before that deadline is discarded
        — see the module docstring's self-mute section for why it takes both.
        Defaults to "never muted".
    tuning:
        A :class:`TranscriptTuning`; see its docstring.
    names:
        Canonical names for the gate's fast path — a FIXED tuple, for a driver
        whose names never change. Defaults to :data:`DEFAULT_NAMES`.
    names_provider:
        A zero-arg callable returning the names the robot answers to RIGHT NOW.
        It WINS over ``names`` when given, and is resolved per utterance (never
        snapshotted at construction), so an operator renaming the robot while
        the runtime is up is obeyed by the very next utterance with nothing
        rebuilt — neither this driver nor the gate it owns. A driver built
        without one behaves exactly as before.
    clock:
        Monotonic clock used only when ``ctx.now`` is unusable. Injectable.
    """

    def __init__(
        self,
        *,
        media: Any,
        realtime: Any | None = None,
        classifier: Any | None = None,
        on_engage: Callable[[], None] | None = None,
        mute_until: Callable[[], float] | None = None,
        tuning: TranscriptTuning = TranscriptTuning(),
        names: tuple[str, ...] = DEFAULT_NAMES,
        names_provider: Callable[[], Any] | None = None,
        clock: Callable[[], float] = time.monotonic,
        pending_maxsize: int = DEFAULT_PENDING_MAXSIZE,
        ready_maxsize: int = DEFAULT_READY_MAXSIZE,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        max_takes_per_tick: int = DEFAULT_MAX_TAKES_PER_TICK,
    ) -> None:
        self._media = media
        self._realtime = realtime
        self._on_engage = on_engage
        self._mute_until = mute_until if mute_until is not None else (lambda: 0.0)
        self._tuning = tuning
        #: The names SOURCE — a provider when one was injected, else the fixed
        #: tuple. Held unresolved so a provider stays LIVE.
        self._names_source: NamesLike = (
            names_provider if names_provider is not None else tuple(names)
        )
        self._clock = clock
        self._join_timeout_s = max(0.0, float(join_timeout_s))
        self._max_takes_per_tick = max(1, int(max_takes_per_tick))

        # --- tick-thread capture state (touched by NO other thread) -----------
        #: The mic's real rate, resolved lazily after the first successful read
        #: and then pushed into the session config.
        self._rate: int | None = None
        #: One-line-per-episode latches, so a per-chunk condition cannot flood
        #: the journal (#99): the robot speaking, and no session wired at all.
        self._muted_logged = False
        self._no_session_logged = False

        # --- the one-tick latch (written by the tick thread only) -------------
        self._latch: str | None = None
        self._latch_direction: str | None = None
        #: Whether the transcript latched THIS tick was admitted BY NAME. A
        #: separate one-tick latch on the same cadence, not a property of the
        #: text: "reachy, stop" and a context-admitted follow-up both set
        #: ``transcript``, and only the first is the robot being ADDRESSED.
        self._name_mentioned = False

        # --- the handoff ------------------------------------------------------
        self._pending: queue.Queue = queue.Queue(maxsize=max(1, int(pending_maxsize)))
        self._ready: queue.Queue = queue.Queue(maxsize=max(1, int(ready_maxsize)))
        #: The background worker, started lazily on the first arrived utterance
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
                # The SOURCE, not a snapshot: the gate resolves it per decision.
                names=self._names_source,
                warm_window_s=tuning.engage_window_s,
                min_context_words=tuning.min_words,
            )
        )
        self._engaged_until = 0.0

        #: Diagnostics / tests: ticks processed, mic chunks handed to the
        #: session, utterances submitted to the worker, utterances the worker has
        #: finished judging (whatever the verdict), and transcripts that cleared
        #: the gate and reached the ready queue.
        #:
        #: ``judged`` is the WORKER-side barrier: ``submitted`` says the tick
        #: handed the words over, and a test that queues the next utterance on
        #: that alone can overrun the bounded pending queue before the gate has
        #: drained it — which is a real (and correctly named) ``gate-backlog``
        #: drop, but a confusing way to fail a test about something else.
        self.ticks = 0
        self.streamed = 0
        self.submitted = 0
        self.judged = 0
        self.transcripts = 0

    # ------------------------------------------------------------------
    # TickBus driver entry point (TICK THREAD)
    # ------------------------------------------------------------------

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """One tick: clear the latch, stream audio, and maybe adopt a ready transcript.

        Never raises and never blocks on I/O. The latch is cleared first — a
        plain assignment that cannot fail, preserving the one-tick contract on
        every path — then the body runs under a broad guard so a misbehaving
        media or session client degrades to "no words this tick".
        """
        # Clear-before-process (see PatSenseDriver's r2): a transcript latched
        # last tick has already been read by this tick's start-of-tick sense.
        self._latch = None
        self._latch_direction = None
        self._name_mentioned = False
        self.ticks += 1
        if self._closed:
            return
        try:
            self._process(ctx)
        # A sense tap must never crash the loop.
        except Exception:
            logger.warning("TranscriptSenseDriver tick raised; transcript dropped", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """The capture body, split out so :meth:`__call__` stays a thin guard.

        Three bounded steps, in this order: latch what the worker finished, feed
        the session this tick's audio, then collect whatever the server has
        endpointed since the last tick.
        """
        now = self._now(ctx)
        self._adopt_ready()

        # Read the mic EVERY tick, even while muted or with no session, so the
        # held client's buffer keeps draining and a co-riding loudness provider
        # sees the same sample. Never a second reader, never a second session.
        chunk = self._read_audio()
        muted = now < self._mute_until()
        if chunk is not None:
            self._stream(chunk, muted=muted)
        self._take_utterances(ctx, now)

    # ------------------------------------------------------------------
    # Capture — outbound (TICK THREAD, O(1) BY CONSTRUCTION)
    # ------------------------------------------------------------------

    def _stream(self, chunk: np.ndarray, *, muted: bool) -> None:
        """Hand one mic chunk to the session client. Never blocks, never raises.

        This is the whole outbound path: no VAD, no ring, no threshold, no
        decision about which audio is worth keeping (see the module docstring's
        #108 note). ``submit_audio`` is one bounded ``put_nowait`` on the
        client's queue; its worker thread owns the socket.
        """
        if muted:
            # The robot is speaking: its own voice must never reach the server's
            # VAD. Latched — audio arrives 50x/s and one line per chunk is the
            # #99 journal flood.
            if not self._muted_logged:
                self._muted_logged = True
                senselog.drop(_STAGE_CAPTURE, _SOURCE, _STREAM_EVENT, REASON_SELF_MUTE)
            return
        self._muted_logged = False

        client = self._realtime
        if client is None:
            if not self._no_session_logged:
                self._no_session_logged = True
                senselog.drop(_STAGE_CAPTURE, _SOURCE, _STREAM_EVENT, REASON_NO_SESSION)
            return
        try:
            client.submit_audio(chunk)
        # A duck-typed client that raises costs one chunk, never the tick.
        except Exception:
            logger.debug("TranscriptSenseDriver: submit_audio raised; chunk dropped", exc_info=True)
            return
        self.streamed += 1

    def _read_audio(self) -> np.ndarray | None:
        """One mic chunk off the injected held client, degrading every failure.

        Returns ``None`` for "no audio this tick" — a cold/disconnected holder, a
        read that raised, an unusable shape, or a genuinely empty chunk. The
        first successful read is also where the real mic sample rate is
        resolved: querying it earlier could trigger the holder's blocking
        construction on this very thread.

        Multi-channel reads have a channel SELECTED
        (:func:`reachy.robot.audio_shape.to_mono`), never flattened: flattening
        an ``(N, C)`` chunk interleaves its channels into one double-length
        stream, and the session config declares one channel at one rate.
        """
        try:
            raw = self._media.audio()
        # A raising media client degrades, never propagates.
        except Exception:
            logger.debug("TranscriptSenseDriver media read raised; no audio", exc_info=True)
            return None
        chunk = to_mono(raw)
        if chunk is None or chunk.size == 0:
            return None
        if self._rate is None:
            self._resolve_rate()
        return chunk

    def _resolve_rate(self) -> None:
        """Adopt the mic's real rate and carry it into the session config.

        Called only after a successful :meth:`_read_audio`, which proves the
        holder is connected — so this property read is free rather than a
        blocking connect. The rate rides the session's connect URL and the
        server resamples from it, so a rate that turns out to differ from the
        one composition guessed is worth one clean, intentional reconnect
        (:meth:`~reachy.speech.realtime.RealtimeTranscriber.set_sample_rate` is
        a no-op when it already matches).
        """
        rate: Any = None
        try:
            rate = self._media.samplerate
        except Exception:
            rate = None
        try:
            self._rate = int(rate) if rate else _FALLBACK_RATE
        except (TypeError, ValueError):
            self._rate = _FALLBACK_RATE
        setter = getattr(self._realtime, "set_sample_rate", None)
        if not callable(setter):
            return
        try:
            setter(self._rate)
        except Exception:  # a rate push must never cost a tick
            logger.debug("TranscriptSenseDriver: set_sample_rate raised", exc_info=True)

    # ------------------------------------------------------------------
    # Capture — inbound (TICK THREAD, NON-BLOCKING BY CONSTRUCTION)
    # ------------------------------------------------------------------

    def _take_utterances(self, ctx, now: float) -> None:  # type: ignore[no-untyped-def]
        """Pop what the server endpointed since the last tick; bounded, never blocks."""
        take = getattr(self._realtime, "take_utterance", None)
        if not callable(take):
            return
        for _ in range(self._max_takes_per_tick):
            try:
                utterance = take()
            # A duck-typed client that raises costs this tick's words, not the tick.
            except Exception:
                logger.debug("TranscriptSenseDriver: take_utterance raised", exc_info=True)
                return
            if utterance is None:
                return
            self._heard(ctx, utterance, now)

    def _heard(self, ctx, utterance: Utterance, now: float) -> None:  # type: ignore[no-untyped-def]
        """Admit one arrived utterance to the worker; the tick's part ENDS here.

        Two cheap guards run on this thread because both are O(1) and both are
        about *this instant*: an empty transcript is not words, and a transcript
        that arrived while the robot was speaking is (probably) the robot. The
        engagement gate — the only remaining network call — is the worker's.
        """
        event_id = uuid.uuid4().hex[:8]
        text = getattr(utterance, "text", None)
        if not isinstance(text, str) or not text.strip():
            senselog.drop(_STAGE_CAPTURE, _SOURCE, event_id, REASON_EMPTY_TRANSCRIPT)
            return

        t = self._arrival(utterance, now)
        if t < self._mute_until():
            # The server's VAD cannot know the robot was talking; this can.
            senselog.drop(_STAGE_CAPTURE, _SOURCE, event_id, REASON_SELF_MUTE)
            return

        self._ensure_worker()
        try:
            self._pending.put_nowait(_Heard(text, self._direction_of(ctx), t, event_id))
        except queue.Full:
            # A wedged/slow classifier has backed the queue up. Dropping is
            # correct: blocking would blow the tick budget, and the words are
            # stale by the time a wedged gate would judge them.
            senselog.drop(_STAGE_CAPTURE, _SOURCE, event_id, REASON_GATE_BACKLOG)
            return
        self.submitted += 1
        senselog.stage(
            _STAGE_CAPTURE, _SOURCE, event_id, f"utterance chars={len(text)} (server vad)"
        )

    def _arrival(self, utterance: Utterance, now: float) -> float:
        """The instant *utterance* arrived — its own stamp when usable, else *now*.

        :attr:`reachy.speech.realtime.Utterance.t` is stamped by the session
        client off the same monotonic clock ``mute_until`` speaks, which is what
        makes the self-mute comparison meaningful across two threads without a
        clock of our own. A duck-typed client that omits it falls back to this
        tick's time rather than losing the words.
        """
        stamp = getattr(utterance, "t", None)
        if isinstance(stamp, (int, float)) and not isinstance(stamp, bool):
            value = float(stamp)
            if math.isfinite(value):
                return value
        return now

    def _adopt_ready(self) -> None:
        """Latch at most one transcript the worker has finished. Never blocks."""
        try:
            text, direction, by_name = self._ready.get_nowait()
        except queue.Empty:
            return
        self._latch = text
        self._latch_direction = direction
        self._name_mentioned = bool(by_name)

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
    # The worker (BACKGROUND THREAD) — the one remaining network call
    # ------------------------------------------------------------------

    def _worker_loop(self) -> None:
        """Drain arrived utterances: gate, publish. Never raises out."""
        while True:
            job = self._pending.get()
            if job is _STOP:
                return
            try:
                self._handle(job)
            # A worker fault must cost one utterance, never the worker.
            except Exception:
                logger.warning("TranscriptSenseDriver worker degraded", exc_info=True)
            self.judged += 1

    def _handle(self, job: _Heard) -> None:
        """Run the engagement gate over one utterance; publish it if admitted."""
        engaged, label, by_name = self._decide(job.text, job.t)
        if not engaged:
            # Not addressed to the robot: ambient speech, a backchannel too short
            # to carry addressing signal, or no conversation open to continue.
            # The gate's own label is the reason, so the journal distinguishes
            # the three rather than collapsing them into one word.
            senselog.drop(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, label)
            return
        self._notify_engaged()
        item = (job.text, job.direction, by_name)
        try:
            self._ready.put_nowait(item)
        except queue.Full:
            # The engine has stopped draining. Drop the OLDEST so the latch
            # always carries the freshest words rather than a stale backlog.
            self._drop_oldest_ready()
            try:
                self._ready.put_nowait(item)
            except queue.Full:
                senselog.drop(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, REASON_LATCH_BACKLOG)
                return
        self._engaged_until = job.t + self._tuning.engage_window_s
        self.transcripts += 1
        senselog.stage(_STAGE_TRANSCRIPT, _SOURCE, job.event_id, f'heard "{job.text[:60]}"')

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
        except Exception:  # a turn-signal fault must not lose the words
            logger.warning("TranscriptSenseDriver on_engage raised; ignoring", exc_info=True)

    # ------------------------------------------------------------------
    # The engagement gate (WORKER THREAD) — reused, not reimplemented
    # ------------------------------------------------------------------

    def _names(self) -> tuple[str, ...]:
        """The names to judge THIS utterance against, resolved now and lowered.

        Lowered because :meth:`_should_engage` compares against lowercased
        words; :func:`~reachy.speech.name_match.is_name_match` (the gate's path)
        lowercases for itself.
        """
        return tuple(name.lower() for name in resolve_names(self._names_source))

    def _decide(self, text: str, t: float) -> tuple[bool, str, bool]:
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

        Returns the decision, its LABEL, and whether the admission was BY NAME.
        The label is the caller's ``senselog.drop`` reason, so a drop always
        says which rule dropped it. The by-name flag is what the
        ``name_mentioned`` latch carries: it is available on EVERY path,
        including the two heuristic ones, because "the robot was addressed by
        name" must not silently become false the moment the classifier is down.
        """
        gate = self._gate
        if gate is None:
            engaged, by_name = self._should_engage(text, t)
            label = "engaged-heuristic" if engaged else "dropped-heuristic"
        else:
            verdict = gate.decide(text, t)
            if verdict.decision is Decision.DEGRADE:
                engaged, by_name = self._should_engage(text, t)
                label = "degrade->heuristic"
                if engaged:
                    gate.note_engaged(text, t)
            else:
                engaged = verdict.decision is Decision.ENGAGE
                label = verdict.label
                # The gate NAMES its own reason; ``name`` is the fast path.
                by_name = engaged and label == LABEL_NAME

        logger.info('engagement: %s :: "%s"', label, text[:40])
        return engaged, label, bool(by_name)

    def _should_engage(self, text: str, t: float) -> tuple[bool, bool]:
        """The cheap fallback rule: named, or a clear sentence in an open window.

        The name match is WHOLE-WORD, not a substring, so "robotic"/"robots" do
        not falsely trigger on the name "robot".

        Returns ``(engaged, by_name)``. The second value is the same
        distinction the gate makes with its ``name`` label — WHY this engaged —
        so the caller can latch ``name_mentioned`` without re-deriving it (and
        without the two paths disagreeing about what a name is).
        """
        words = _WORD_RE.findall(text.lower())
        if any(name in words for name in self._names()):
            return True, True
        coherent = len(words) >= self._tuning.min_words
        return (coherent and t < self._engaged_until), False

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

    def peek_name_mentioned(self) -> bool:
        """Whether the transcript latched this tick NAMED the robot.

        A one-tick latch on exactly the cadence of :meth:`peek` — ``True`` for
        the single tick that adopts a by-name admission, ``False`` on every
        other tick, including the tick that adopts a CONTEXT admission (that
        one still sets ``transcript``). Never raises, never blocks, and safe to
        call repeatedly within a tick.
        """
        return self._name_mentioned

    def as_name_mentioned_provider(self) -> Callable[[], bool]:
        """The zero-arg ``name_mentioned`` provider (an alias for :meth:`peek_name_mentioned`)."""
        return self.peek_name_mentioned

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop the worker thread. Idempotent, never raises.

        Does NOT close the media client or the realtime session client: the
        composition root owns one of each, other senses read the media client
        too, and the session outlives any single sense. After ``close()`` every
        tick is a no-op that still clears the latch, so a late tick is safe.
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
        needs a real number (the self-mute window is time-based), so a missing or
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
            # Imported HERE, not at module scope: this is one small bearing
            # formatter, but ``reachy.speech.events`` carries the cognition
            # EventBuffer with it. Because ``_build_parser()`` reaches this
            # module, a top-level import put the event bus in the import path of
            # every ``reachy`` invocation — which `say`'s dumb-pipe boundary test
            # forbids outright. Found by t24's import-boundary suite.
            from reachy.speech.events import _doa_direction

            return _doa_direction(float(angle))
        except Exception:  # a bad angle must never drop the words
            return None
