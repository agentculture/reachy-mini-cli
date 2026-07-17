"""Fold STT transcription into the ``listen`` motion loop and feed words to cognition.

``listen`` already owns the *one* in-process SDK media session and derives a
single per-tick :class:`~reachy.motion.sense_sample.SenseSample` (direction of
arrival, mic loudness, a speech flag, and — new — the raw mic ``audio`` chunk) to
drive its Tier-1 antenna lean and Tier-2 turn. :class:`TranscribeHook` rides that
*same* sample: it is a per-tick ``on_tick`` hook
(``(transport, queue, t, commanded_head) -> None``) that transcribes the sample's
nearby speech and feeds the recognised **words** into ``think``'s
:class:`~reachy.speech.events.EventBuffer` via
:meth:`~reachy.speech.events.EventBuffer.feed_transcript` — the *same* buffer the
:class:`~reachy.speech.cognition.CognitionEngine` consumes (the composition layer
wires one buffer into both). It is the live-loop glue between the loop's shared
per-tick audio and the shared :class:`~reachy.speech.stt.Transcriber`.

Why a folded hook rather than a second process / a second media session
----------------------------------------------------------------------
The robot has one single-consumer SDK media subsystem. A standalone transcription
process opening its *own* media session would contend with ``listen`` for that one
client and throttle to ~1 Hz (the same constraint that motivated folding ``pat``
in via :class:`~reachy.motion.listen_pat.PatHook`, #43, and ``think`` /
``sleep`` via :class:`~reachy.motion.listen_think.ThinkHook` /
:class:`~reachy.motion.listen_sleep.SleepHook`; see the single-SDK-owner model in
``CLAUDE.md``). So ``TranscribeHook`` opens **no** audio of its own — it never
imports or constructs a ``ReachyMini`` client and never calls ``media_session``.
Its only audio input is ``sample.audio`` from the injected
:data:`~reachy.motion.sense_sample.SampleProvider`, the raw mic chunk the loop has
already pulled this tick. When the provider returns ``None`` (no fresh sample) the
tick is a silent no-op.

Pre-roll ring buffer + measured onset (leading words are not lost)
------------------------------------------------------------------
The SDK speech flag is polled at ~5 Hz and lags the audio, so the leading words of
every utterance arrive on ticks where ``sample.speech`` is still False. To keep
them, the hook feeds ``sample.audio`` into a rolling ~``ring_seconds`` ring buffer
on **every** (non-muted) tick — *before* the speech-flag gate — trimmed by TOTAL
samples with a cheap per-tick append (the only concat is one snapshot on the rising
edge). On the flag's rising edge the utterance is seeded from the ring at the
**measured** onset (an RMS scan of the buffered audio in 10 ms windows, cited from
reachy_nova's ``SpeechEventDetector``) minus ``pre_roll`` (default 2.0 s, clamped to
the ring start). Subsequent speech ticks append their chunk; the ring is cleared
with each flushed / discarded utterance so stale audio never bleeds into the next
lead-in. This design fixes the "first words lost" bug where accumulation started
only once the flag flipped.

The transcribe gate (cheap-first, mute-aware)
---------------------------------------------
The self-mute window is checked *before* anything is captured: while ``t <
mute_until()`` the current utterance AND the ring are discarded, so while (and just
after) the robot speaks, its own voice through the shared USB audio device is never
buffered, never pre-rolled, and never transcribed. ``t`` (the tick's clock, exactly
as :mod:`reachy.motion.listen_sleep` uses it) is the current time; ``mute_until()``
returns the monotonic deadline the speak path stamps (default ``0.0`` = never
muted).

Endpointing then accumulates the whole utterance and transcribes it in **one**
:meth:`Transcriber.transcribe_once` POST on a ``silence_hold_s`` pause (or at
``max_utterance_s``), so the STT sees a full sentence, pre-roll included. Sub-
``min_utterance_s`` blips are dropped — the gate measures the *speech* samples only,
so the pre-roll lead-in can never pad a blip past the floor. A non-empty transcript
that clears the engagement gate is fed to the cognition buffer; a ``None`` / empty
transcript, or an un-addressed utterance, feeds nothing.

Each capture / onset, and each min-utterance / self-mute discard, emits one
parseable ``[SENSE]`` line via :mod:`reachy.senselog` (logger ``reachy.sense``).

Error isolation (a hook must never kill the loop)
-------------------------------------------------
Every step is guarded — a provider, transcriber, or feed fault is logged and
**swallowed**, so the tick degrades to "no words this tick" and never propagates
out of :meth:`__call__` (exactly like :class:`ThinkHook`; the
:class:`~reachy.motion.listen_hooks.HookChain` isolates hooks too, but the hook
defends itself). :meth:`close` exists and is safe + idempotent; this hook writes
**no** ``*_active.flag`` (transcription is not an idle-priority owner — it only
feeds words to cognition), so there is no flag to manage on the way out.

Pure standard library + numpy + the existing speech engine — no new runtime
dependency.
"""

from __future__ import annotations

import collections
import logging
import math
import os
import re
import uuid
from dataclasses import dataclass
from typing import Callable

import numpy as np

from reachy import senselog
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SampleProvider, SenseSample
from reachy.speech.engagement import Decision, decide_engagement
from reachy.speech.events import EventBuffer, _doa_direction
from reachy.speech.name_match import is_name_match
from reachy.speech.stt import Transcriber

#: Truthy strings recognised by the ``REACHY_ENGAGE_HEURISTIC`` escape hatch.
_TRUTHY = frozenset({"1", "true", "yes", "on", "y", "t"})

#: How many recent accepted utterances to pass to the classifier as conversation
#: context.  A sane default; the exact count is a parked follow-up (issue #55).
_HISTORY_MAXLEN = 6

#: Words counted for the coherence gate (letters + intra-word apostrophes).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Pre-roll ring + measured onset defaults (cited from reachy_nova's
#: ``speech_events.SpeechEventDetector``). The mic audio here is float32 PCM
#: already normalised to [-1, 1] (``get_audio_sample`` yields floats, and the RMS
#: loudness detector reads them directly), so nova's 0.02 float-PCM silence
#: threshold applies verbatim — no int16 rescale.
_ONSET_WINDOW_SECONDS = 0.01  # 10 ms analysis window for the RMS onset scan
_DEFAULT_SILENCE_THRESHOLD = 0.02  # RMS over a 10 ms window (float PCM)
_DEFAULT_RING_SECONDS = 10.0  # rolling pre-roll buffer horizon
_DEFAULT_PRE_ROLL_SECONDS = 2.0  # lead-in kept before the measured onset

logger = logging.getLogger(__name__)


def _env_truthy(value: str | None) -> bool:
    """Return ``True`` for the usual truthy env strings; ``False`` for unset/"0"/""."""
    if value is None:
        return False
    return value.strip().lower() in _TRUTHY


@dataclass(frozen=True)
class TranscribeTuning:
    """Grouped numeric knobs tuning HOW :class:`TranscribeHook` endpoints + gates.

    Split out of :meth:`TranscribeHook.__init__` (SonarCloud S107 — too many
    parameters) so the constructor's SEAM parameters (transcriber, buffer,
    classifier, clocks, callbacks — the idiomatic injectables this codebase
    favours) stay individual, while the pure-number tuning cluster travels as
    one value object. Every field keeps its previously shipped default, so a
    bare ``TranscribeTuning()`` reproduces today's behaviour byte-identically.

    Endpointing (whole-utterance accumulation):

    * ``silence_hold_s`` — pause length that ends an utterance and triggers the
      single :meth:`~reachy.speech.stt.Transcriber.transcribe_once` POST.
    * ``max_utterance_s`` — hard cap that force-flushes a very long monologue.
    * ``min_utterance_s`` — floor below which a blip is dropped, never sent to
      STT (measured over *speech* samples only, pre-roll excluded).

    Pre-roll ring buffer + measured onset (cited from reachy_nova's
    ``SpeechEventDetector``):

    * ``ring_seconds`` — horizon of the rolling pre-flag audio buffer.
    * ``pre_roll_s`` — lead-in kept before the measured onset.
    * ``onset_threshold`` — RMS level (float PCM) an analysis window must clear
      to count as the onset.
    * ``onset_window_s`` — width of each onset-scan analysis window.

    Engagement gate:

    * ``min_words`` — word-count floor for the "clear sentence" heuristic.
    * ``engage_window_s`` — how long a conversation stays "open" after an
      ENGAGE decision, for the coherent-follow-up heuristic branch.
    """

    silence_hold_s: float = 0.7
    max_utterance_s: float = 15.0
    min_utterance_s: float = 0.3
    ring_seconds: float = _DEFAULT_RING_SECONDS
    pre_roll_s: float = _DEFAULT_PRE_ROLL_SECONDS
    onset_threshold: float = _DEFAULT_SILENCE_THRESHOLD
    onset_window_s: float = _ONSET_WINDOW_SECONDS
    min_words: int = 3
    engage_window_s: float = 20.0


class TranscribeHook:
    """A per-tick ``on_tick`` hook transcribing the shared sample into cognition words.

    Construct one with the loop's :data:`SampleProvider` and the cognition
    :class:`~reachy.speech.events.EventBuffer` to feed (the composition layer wires
    the *same* buffer into both the :class:`~reachy.speech.cognition.CognitionEngine`
    and this hook, so words fed here are consumed by that engine). Pass
    :meth:`__call__` as ``on_tick=`` to :func:`reachy.motion.server.run` (usually
    inside a :class:`~reachy.motion.listen_hooks.HookChain`), and call :meth:`close`
    in the loop's ``finally`` (it is a safe no-op).

    Parameters
    ----------
    sample_provider:
        Zero-arg callable returning the loop's latest
        :class:`~reachy.motion.sense_sample.SenseSample`, or ``None`` for "no fresh
        sample this tick" (then the tick is a silent no-op). This is the hook's
        **only** audio input — it never opens a media session, and it transcribes
        ``sample.audio`` (the raw mic chunk the loop already pulled) rather than
        reading audio itself.
    buffer:
        The :class:`~reachy.speech.events.EventBuffer` recognised words are fed into
        via :meth:`~reachy.speech.events.EventBuffer.feed_transcript`. In production
        the composition layer passes the *same* buffer the cognition engine
        consumes.
    transcriber:
        The :class:`~reachy.speech.stt.Transcriber` that turns a mic chunk into a
        transcript string (it accumulates a rolling window, throttles its POSTs,
        and never raises). Defaults to a real :class:`Transcriber` (constructed with
        no network I/O); tests inject a fake recording its calls.
    classifier:
        Optional :class:`~reachy.speech.engagement.EngagementClassifier`-like
        object (anything with ``judge(text, context) -> bool``) used by the
        layered engagement gate (:meth:`_decide` →
        :func:`~reachy.speech.engagement.decide_engagement`). Default ``None``
        keeps the gate byte-identical to the pure :meth:`_should_engage`
        heuristic (no classifier call); inject one to let the LLM judge
        addressed-vs-ambient. The ``REACHY_ENGAGE_HEURISTIC`` env flag (truthy)
        forces the heuristic even when a classifier is injected, and a classifier
        that raises degrades back to the heuristic so the loop never stalls.
    on_engage:
        Optional zero-arg callback fired **exactly once per ENGAGE decision** —
        when an utterance clears the engagement gate (:meth:`_decide` returns
        True) and is about to be fed to cognition. It is **not** fired on a
        drop or a degrade-to-drop, so ambient / un-addressed speech never
        triggers it. The composition layer wires this to
        :meth:`~reachy.motion.listen.ListenProducer.set_engaged` so an addressed
        utterance latches exactly one deliberate head/body turn toward the
        speaker's DoA on the next tick (the engaged signal of the motion
        ladder). The callback is invoked inside a ``try/except`` — a callback
        fault is logged and swallowed so it can never kill the loop or block the
        words from reaching cognition. Default ``None`` is a no-op (no turn), so
        a build that does not inject it is byte-identical to today.
    mute_until:
        Zero-arg callable returning the monotonic deadline (seconds) until which the
        robot is self-muted — while ``t < mute_until()`` the tick discards the audio
        **before** transcription (no STT POST). Defaults to ``lambda: 0.0`` (never
        muted). Wire it to the speak path's mute window so the robot never
        transcribes its own voice. The tick's own ``t`` is the clock used for the
        mute gate (mirroring :mod:`reachy.motion.listen_sleep`), so the hook needs
        no separate clock seam.
    tuning:
        A :class:`TranscribeTuning` bundling the endpointing / pre-roll / gate
        numeric knobs (``silence_hold_s``, ``max_utterance_s``, ``min_utterance_s``,
        ``ring_seconds``, ``pre_roll_s``, ``onset_threshold``, ``onset_window_s``,
        ``min_words``, ``engage_window_s``). Defaults to ``TranscribeTuning()`` —
        today's shipped values, byte-identical when omitted. See that class's
        docstring for what each field controls.
    """

    def __init__(
        self,
        sample_provider: SampleProvider,
        *,
        buffer: EventBuffer,
        transcriber: object | None = None,
        classifier: object | None = None,
        on_engage: Callable[[], None] | None = None,
        sample_rate: int | None = None,
        mute_until: Callable[[], float] | None = None,
        tuning: TranscribeTuning = TranscribeTuning(),
        names: tuple[str, ...] = ("reachy", "robot"),
    ) -> None:
        self._provider = sample_provider
        self._buffer = buffer
        # Build the default Transcriber with the REAL mic sample rate when known so
        # the WAV header sent to STT matches the audio (a wrong rate makes the STT
        # mis-decode and return nothing — the bug live-testing surfaced). An explicit
        # transcriber (tests) wins; else honour sample_rate; else the 16 kHz default.
        if transcriber is not None:
            self._transcriber = transcriber
        elif sample_rate:
            self._transcriber = Transcriber(sample_rate=sample_rate)
        else:
            self._transcriber = Transcriber()
        self._mute_until = mute_until if mute_until is not None else (lambda: 0.0)

        # --- Endpointing: accumulate a whole utterance, transcribe on a pause. ---
        self._rate = int(sample_rate) if sample_rate else 16000
        self._silence_hold_s = float(tuning.silence_hold_s)
        self._max_utterance_s = float(tuning.max_utterance_s)
        self._min_utt_samples = int(max(0.0, tuning.min_utterance_s) * self._rate)
        #: Chunks of the current utterance (cleared on flush / mute / reset).
        self._utt: list[np.ndarray] = []
        self._utt_samples = 0
        #: Samples that arrived on speech-flagged ticks only (EXCLUDES pre-roll) —
        #: the quantity the min-utterance gate measures, so a pre-roll lead-in never
        #: pads a blip past the floor (the gate's original semantics are preserved).
        self._utt_speech_samples = 0
        self._utt_started_t: float | None = None
        self._last_speech_t: float | None = None
        self._utt_direction: str | None = None
        #: Short id shared by this utterance's capture / onset / drop [SENSE] lines.
        self._event_id: str | None = None

        # --- Pre-roll ring buffer + measured onset (cited from reachy_nova's
        #     speech_events.SpeechEventDetector). A rolling ~ring_seconds buffer is
        #     fed EVERY tick from the raw chunk, BEFORE the (lagging, ~5 Hz) speech
        #     flag. On the flag's rising edge the onset is MEASURED (an RMS scan of
        #     the buffered audio in onset_window_s windows) and the utterance is
        #     seeded from onset - pre_roll (clamped to the ring start), so the
        #     leading words the flag missed are kept. Trimmed by TOTAL samples with a
        #     cheap per-tick append — the only concat is one snapshot on the rising
        #     edge, never per tick. ---
        self._ring_max = int(max(0.0, tuning.ring_seconds) * self._rate)
        self._pre_roll_samples = int(max(0.0, tuning.pre_roll_s) * self._rate)
        self._onset_threshold = float(tuning.onset_threshold)
        self._onset_window = max(1, int(tuning.onset_window_s * self._rate))
        self._ring: list[np.ndarray] = []
        self._ring_samples = 0
        self._ring_total = 0

        # --- Engagement gate: only respond to clear sentences that are addressed
        #     to the robot (its name) or continue an ongoing conversation. ---
        self._min_words = int(tuning.min_words)
        self._engage_window_s = float(tuning.engage_window_s)
        self._names = tuple(n.lower() for n in names)
        self._engaged_until = 0.0

        # --- Layered engagement decision (t6): delegate to the t5 engine when a
        #     classifier is injected, else stay byte-identical to the pure
        #     heuristic.  The escape hatch is read ONCE here so flipping it
        #     mid-run never matters. ---
        self._classifier = classifier
        self._on_engage = on_engage
        self._force_heuristic = _env_truthy(os.environ.get("REACHY_ENGAGE_HEURISTIC"))
        #: Recent accepted utterances, oldest-first, handed to the classifier as
        #: conversation context (only appended on an ENGAGE decision).
        self._history: collections.deque[str] = collections.deque(maxlen=_HISTORY_MAXLEN)

        #: Count of utterances fed to cognition (diagnostics / tests).
        self.transcripts = 0
        #: Count of samples seen (diagnostics / tests).
        self.events = 0

    # ------------------------------------------------------------------
    # Per-tick entry point
    # ------------------------------------------------------------------

    def __call__(
        self,
        transport: object,
        queue: MotionQueue,
        t: float,
        commanded_head: dict[str, float] | None = None,
    ) -> None:
        """One tick: transcribe the shared sample's audio + feed any words.

        Reads the loop's latest sample via the provider; a ``None`` sample is a
        silent no-op. Otherwise, when the sample carries speech AND a raw audio
        chunk AND the tick is outside the self-mute window
        (``t >= mute_until()``), the chunk is handed to the
        :class:`~reachy.speech.stt.Transcriber`; a non-empty transcript is fed to
        the cognition :class:`~reachy.speech.events.EventBuffer`.

        ``transport`` / ``queue`` / ``commanded_head`` are part of the shared
        ``on_tick`` contract but unused: ``TranscribeHook`` drives no motion and
        reads no audio off the transport (its audio is ``sample.audio``). Every
        step is guarded — a provider, transcriber, or feed fault is logged and
        swallowed so a transient fault degrades to "no words this tick" and never
        kills the loop.
        """
        try:
            sample = self._provider()
        except Exception:  # noqa: BLE001
            logger.warning("TranscribeHook sample provider raised; skipping tick", exc_info=True)
            return
        if sample is None:
            return
        self.events += 1
        try:
            self._maybe_transcribe(sample, t)
        except Exception:  # noqa: BLE001
            logger.warning("TranscribeHook tick degraded (transcribe/feed fault)", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _maybe_transcribe(self, sample: SenseSample, t: float) -> None:
        """Feed the pre-roll ring, then accumulate + transcribe + gate an utterance.

        Pre-roll ring: the raw chunk is pushed to a rolling ~ring_seconds buffer on
        EVERY (non-muted) tick — BEFORE the lagging speech-flag gate — so the leading
        words that arrive while ``sample.speech`` is still False are still buffered
        when the flag rises.

        Endpointing: on the speech-flag rising edge the utterance is seeded from the
        ring at the *measured* onset minus ``pre_roll`` (see :meth:`_begin_utterance`);
        subsequent speech ticks append their chunk. When speech stops for
        ``silence_hold_s`` — or the utterance grows past ``max_utterance_s`` — the
        *whole* buffer is transcribed in one POST (so the STT sees a full sentence,
        pre-roll included) and run through the engagement gate before being fed to
        cognition.

        The self-mute window is checked first: while (and just after) the robot
        speaks, the current utterance AND the pre-roll ring are discarded and nothing
        is captured — the robot must never transcribe (or pre-roll) its own voice.
        """
        if t < self._mute_until():
            # Robot is speaking — drop any partial utterance + the pre-roll ring; the
            # robot must never transcribe (or pre-roll) its own voice.
            if self._utt:
                senselog.drop("capture", "speech", self._event_id or "?", "self-mute")
            self._reset_utt()
            return

        # Feed the rolling pre-roll ring EVERY tick from the raw chunk, BEFORE the
        # (lagging) speech-flag gate below — so leading words captured before the flag
        # flips are still buffered when the rising edge measures onset.
        if sample.audio is not None:
            self._push_ring(sample.audio)

        if sample.speech and sample.audio is not None:
            chunk = np.asarray(sample.audio, dtype=np.float32).reshape(-1)
            if self._utt_samples == 0:
                # Rising edge: seed the utterance with the measured-onset pre-roll
                # (which already includes this chunk — pushed to the ring above).
                self._begin_utterance(sample, t, int(chunk.size))
            else:
                self._utt.append(chunk)
                self._utt_samples += int(chunk.size)
                self._utt_speech_samples += int(chunk.size)
            self._last_speech_t = t
            started = self._utt_started_t
            if started is not None and (t - started) >= self._max_utterance_s:
                self._flush(t)  # cap a very long monologue
            return

        # Non-speech tick: a long-enough pause ends the utterance → transcribe it.
        if (
            self._utt
            and self._last_speech_t is not None
            and (t - self._last_speech_t) >= self._silence_hold_s
        ):
            self._flush(t)

    def _flush(self, t: float) -> None:
        """Transcribe the buffered utterance, gate it, and feed it if it qualifies."""
        speech_samples, direction = self._utt_speech_samples, self._utt_direction
        utt = self._utt
        event_id = self._event_id or "?"
        self._reset_utt()
        if speech_samples < self._min_utt_samples or not utt:
            # Too short to be a real utterance (a blip, not speech). The gate measures
            # SPEECH samples, so the pre-roll lead-in can never pad a blip past it.
            senselog.drop("capture", "speech", event_id, "min-utterance")
            return
        audio = np.concatenate(utt)
        text = self._transcriber.transcribe_once(audio)  # type: ignore[attr-defined]
        if not text:
            return
        if not self._decide(text, t):
            # A coherent-enough utterance, but not addressed to the robot and not part
            # of an ongoing conversation — ignore it (ambient speech / noise). No
            # engaged turn fires for dropped / degrade-to-dropped utterances.
            return
        # The gate ENGAGED: signal the motion ladder to turn toward the speaker (a
        # deliberate one-shot turn on the next tick) BEFORE feeding cognition. The
        # callback (wired to ListenProducer.set_engaged) is guarded so a fault can
        # neither kill the loop nor stop the words reaching cognition.
        self._notify_engaged()
        self._buffer.feed_transcript(text, direction=direction)
        self._engaged_until = t + self._engage_window_s
        self.transcripts += 1

    def _notify_engaged(self) -> None:
        """Fire the ``on_engage`` callback once for an ENGAGE decision (guarded).

        Called from :meth:`_flush` only when :meth:`_decide` returned True — i.e.
        exactly once per addressed/named utterance, never on a drop or a
        degrade-to-drop. ``on_engage`` is wired by the composition layer to
        :meth:`~reachy.motion.listen.ListenProducer.set_engaged` (latch one
        deliberate turn toward the DoA). A callback fault is logged and swallowed:
        a raising motion seam must never kill the hearing loop or block the words.
        """
        if self._on_engage is None:
            return
        try:
            self._on_engage()
        except Exception:  # noqa: BLE001 — a turn-signal fault must not kill the loop
            logger.warning("TranscribeHook on_engage callback raised; ignoring", exc_info=True)

    def _decide(self, text: str, t: float) -> bool:
        """Layered engagement decision: delegate to the t5 engine, or the heuristic.

        Two paths, chosen once per utterance:

        * **Heuristic path** — when the escape hatch ``REACHY_ENGAGE_HEURISTIC``
          is set (read once at construction) OR no classifier was injected, the
          decision is exactly :meth:`_should_engage` (byte-identical to today's
          shipped behaviour, and zero classifier calls).
        * **LLM-gate path** — otherwise :func:`~reachy.speech.engagement.decide_engagement`
          decides against the recent conversation ``context``:

          - :data:`~reachy.speech.engagement.Decision.ENGAGE` → engage (label
            ``"name"`` if the utterance fuzzy-matches the robot's name, else
            ``"context"``);
          - :data:`~reachy.speech.engagement.Decision.DROP` → drop (label
            ``"dropped"``);
          - :data:`~reachy.speech.engagement.Decision.DEGRADE` (classifier
            unavailable) → fall back to :meth:`_should_engage` so the hearing
            loop **never stalls** (label ``"degrade->heuristic"``).

        The per-utterance outcome is logged (the observability label) and, on an
        ENGAGE, the utterance is appended to the conversation ``_history`` so it
        becomes context for the next decision.
        """
        if self._force_heuristic or self._classifier is None:
            engaged = self._should_engage(text, t)
            label = "engaged-heuristic" if engaged else "dropped-heuristic"
        else:
            decision = decide_engagement(
                text, list(self._history), classifier=self._classifier, names=self._names
            )
            if decision is Decision.ENGAGE:
                engaged = True
                label = "name" if is_name_match(text, self._names) else "context"
            elif decision is Decision.DROP:
                engaged = False
                label = "dropped"
            else:  # Decision.DEGRADE — classifier unavailable, keep hearing.
                engaged = self._should_engage(text, t)
                label = "degrade->heuristic"

        logger.info('engagement: %s :: "%s"', label, text[:40])
        if engaged:
            self._history.append(text)
        return engaged

    def _should_engage(self, text: str, t: float) -> bool:
        """Decide whether *text* should drive cognition.

        Engage when the utterance names the robot (``"reachy"`` / ``"robot"``), OR
        it is a clear sentence (``>= min_words`` words) arriving while a conversation
        is still ongoing (within ``engage_window_s`` of the last exchange). Short
        fragments and ambient speech that isn't addressed to the robot are ignored —
        "don't reply to unintelligible sound, just clear, coherent sentences".

        The name match is **whole-word**, not a substring, so "robotic"/"robots" do
        not falsely trigger on the name "robot".
        """
        words = _WORD_RE.findall(text.lower())
        if any(name in words for name in self._names):
            return True
        coherent = len(words) >= self._min_words
        return coherent and t < self._engaged_until

    def _direction_of(self, sample: SenseSample) -> str | None:
        """Direction word the utterance came from (DoA in degrees → label), or None."""
        if sample.doa is None:
            return None
        try:
            return _doa_direction(math.radians(sample.doa))
        except Exception:  # noqa: BLE001 — a bad angle must never drop the words
            return None

    def _begin_utterance(self, sample: SenseSample, t: float, speech_samples: int) -> None:
        """Seed a new utterance with measured-onset pre-roll from the ring buffer.

        Called on the speech-flag rising edge. The triggering chunk is already in the
        ring (pushed at the top of the tick), so the seeded pre-roll slice includes it
        — the caller must NOT append it again. The onset is *measured* (an RMS scan of
        the buffered audio, :meth:`_measure_onset`), and the utterance starts at
        ``onset - pre_roll`` clamped to the ring start, so the leading words the
        lagging speech flag missed are kept. Emits the ``capture`` + ``onset`` [SENSE]
        lines for this utterance.

        ``speech_samples`` is the triggering chunk's length — the *speech* audio the
        min-utterance gate counts (pre-roll excluded).
        """
        self._utt_started_t = t
        self._utt_direction = self._direction_of(sample)
        self._utt_speech_samples = int(speech_samples)
        self._event_id = uuid.uuid4().hex[:8]

        snapshot = self._concat_ring()  # one concat, rising edge only (never per tick)
        buffer_start = self._ring_total - self._ring_samples
        onset_offset = self._measure_onset(snapshot)
        onset_absolute = buffer_start + onset_offset
        clip_start = max(buffer_start, onset_absolute - self._pre_roll_samples)
        clip_offset = clip_start - buffer_start
        preroll = snapshot[clip_offset:]
        self._utt = [preroll] if preroll.size else []
        self._utt_samples = int(preroll.size)

        pre_roll_s = (onset_absolute - clip_start) / self._rate
        senselog.stage(
            "capture",
            "speech",
            self._event_id,
            f"utterance start pre_roll={pre_roll_s:.2f}s buffered={self._ring_samples}",
        )
        senselog.stage(
            "onset",
            "speech",
            self._event_id,
            f"offset={onset_offset} samples ({onset_offset / self._rate:.3f}s)",
        )

    def _push_ring(self, audio: np.ndarray) -> None:
        """Append a mic chunk to the rolling pre-roll ring (cheap; trimmed by samples).

        A per-tick append with no concat; the oldest chunk is dropped once the buffer
        would exceed ``ring_seconds`` of TOTAL samples (keeping at least one chunk),
        mirroring reachy_nova's ``SpeechEventDetector._push``.
        """
        chunk = np.asarray(audio, dtype=np.float32).reshape(-1)
        if chunk.size == 0:
            return
        self._ring.append(chunk)
        self._ring_samples += int(chunk.size)
        self._ring_total += int(chunk.size)
        while len(self._ring) > 1 and self._ring_samples - self._ring[0].size >= self._ring_max:
            self._ring_samples -= int(self._ring.pop(0).size)

    def _concat_ring(self) -> np.ndarray:
        """Concatenate the ring's chunks into one float32 snapshot (rising edge only)."""
        if not self._ring:
            return np.zeros(0, dtype=np.float32)
        if len(self._ring) == 1:
            return self._ring[0]
        return np.concatenate(self._ring)

    def _measure_onset(self, snapshot: np.ndarray) -> int:
        """First ``onset_window`` offset whose RMS clears the silence threshold, else 0.

        A MEASUREMENT over the buffered audio (cited from reachy_nova's
        ``SpeechEventDetector._measure_onset``) — not an assumed fixed offset — so the
        emitted clip's lead-in tracks where energy actually rises. Falls back to 0 (the
        ring start) when nothing clears the threshold, so pre-roll still applies
        conservatively.
        """
        win = self._onset_window
        n = int(snapshot.size)
        for start in range(0, n, win):
            window = snapshot[start : start + win]
            if window.size == 0:
                continue
            rms = float(np.sqrt(np.mean(np.square(window))))
            if rms >= self._onset_threshold:
                return start
        return 0

    def _reset_utt(self) -> None:
        """Clear the current utterance accumulator AND the pre-roll ring buffer.

        The ring is cleared with the utterance so the next utterance's measured-onset
        pre-roll only scans audio captured *after* this one ended (or after the robot
        stopped speaking) — a previous utterance's (or the robot's own) words never
        bleed into the next utterance's lead-in.
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

    def close(self) -> None:
        """No-op cleanup, present for the hook contract (safe + idempotent).

        ``TranscribeHook`` holds no flag and owns no background worker — it only
        feeds words into the shared cognition buffer per tick — so there is nothing
        to tear down. The method exists so the ``listen`` loop can call ``close()``
        on every hook uniformly in its ``finally``.
        """
        return None
