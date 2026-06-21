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

The transcribe gate (cheap-first, mute-aware)
---------------------------------------------
A tick transcribes **only** when all three hold:

* ``sample.speech`` is True (there is speech to recognise this tick), and
* ``sample.audio is not None`` (there is a raw chunk to send), and
* the tick is **outside the self-mute window** — i.e. ``t >= mute_until()``.

The self-mute gate is checked *before* :meth:`Transcriber.transcribe` is ever
called, so while (and just after) the robot speaks, its own voice through the
shared USB audio device is dropped on the floor and **no STT POST happens** — the
robot never transcribes itself. ``t`` (the tick's clock, exactly as
:mod:`reachy.motion.listen_sleep` uses it) is the current time; ``mute_until()``
returns the monotonic deadline the speak path stamps (default ``0.0`` = never
muted). The cheap boolean checks come first so an ineligible tick costs nothing.

When eligible, the chunk is handed to :meth:`Transcriber.transcribe`, which itself
accumulates a rolling window, throttles its POSTs, and never raises — returning a
non-empty transcript string or ``None``. A non-empty transcript is fed to the
cognition buffer; a ``None`` / empty transcript feeds nothing.

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

import logging
import math
import re
from typing import Callable

import numpy as np

from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SampleProvider, SenseSample
from reachy.speech.events import EventBuffer, _doa_direction
from reachy.speech.stt import Transcriber

#: Words counted for the coherence gate (letters + intra-word apostrophes).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

logger = logging.getLogger(__name__)


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
    mute_until:
        Zero-arg callable returning the monotonic deadline (seconds) until which the
        robot is self-muted — while ``t < mute_until()`` the tick discards the audio
        **before** transcription (no STT POST). Defaults to ``lambda: 0.0`` (never
        muted). Wire it to the speak path's mute window so the robot never
        transcribes its own voice.
    clock:
        Injectable ``() -> float`` (unused by the core logic today — the tick's
        ``t`` is the time used for the mute gate, mirroring
        :mod:`reachy.motion.listen_sleep`; reserved for future deterministic
        stamping). Defaults to :func:`time.monotonic`.
    """

    def __init__(
        self,
        sample_provider: SampleProvider,
        *,
        buffer: EventBuffer,
        transcriber: object | None = None,
        sample_rate: int | None = None,
        mute_until: Callable[[], float] | None = None,
        clock: Callable[[], float] | None = None,
        silence_hold_s: float = 0.7,
        max_utterance_s: float = 15.0,
        min_utterance_s: float = 0.3,
        min_words: int = 3,
        engage_window_s: float = 20.0,
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
        if clock is not None:
            self._clock = clock
        else:
            import time

            self._clock = time.monotonic

        # --- Endpointing: accumulate a whole utterance, transcribe on a pause. ---
        self._rate = int(sample_rate) if sample_rate else 16000
        self._silence_hold_s = float(silence_hold_s)
        self._max_utterance_s = float(max_utterance_s)
        self._min_utt_samples = int(max(0.0, min_utterance_s) * self._rate)
        #: Chunks of the current utterance (cleared on flush / mute / reset).
        self._utt: list[np.ndarray] = []
        self._utt_samples = 0
        self._utt_started_t: float | None = None
        self._last_speech_t: float | None = None
        self._utt_direction: str | None = None

        # --- Engagement gate: only respond to clear sentences that are addressed
        #     to the robot (its name) or continue an ongoing conversation. ---
        self._min_words = int(min_words)
        self._engage_window_s = float(engage_window_s)
        self._names = tuple(n.lower() for n in names)
        self._engaged_until = 0.0

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
        """Accumulate a whole utterance, then transcribe + gate it on a pause.

        Endpointing: while the sample carries speech (and we are outside the
        self-mute window) the raw chunk is appended to the current utterance. When
        speech stops for ``silence_hold_s`` — or the utterance grows past
        ``max_utterance_s`` — the *whole* buffer is transcribed in one POST (so the
        STT sees a full sentence, not a 1.5 s slice) and run through the engagement
        gate before being fed to cognition.

        The self-mute window is checked first: while (and just after) the robot
        speaks, the current utterance is discarded and nothing is accumulated — the
        robot must never transcribe its own voice.
        """
        if t < self._mute_until():
            # Robot is speaking — drop any partial utterance; never capture its voice.
            self._reset_utt()
            return

        if sample.speech and sample.audio is not None:
            chunk = np.asarray(sample.audio, dtype=np.float32).reshape(-1)
            if self._utt_samples == 0:
                self._utt_started_t = t
                self._utt_direction = self._direction_of(sample)
            self._utt.append(chunk)
            self._utt_samples += len(chunk)
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
        samples, direction = self._utt_samples, self._utt_direction
        utt = self._utt
        self._reset_utt()
        if samples < self._min_utt_samples or not utt:
            return  # too short to be a real utterance (a blip, not speech)
        audio = np.concatenate(utt)
        text = self._transcriber.transcribe_once(audio)  # type: ignore[attr-defined]
        if not text:
            return
        if not self._should_engage(text, t):
            # A coherent-enough utterance, but not addressed to the robot and not part
            # of an ongoing conversation — ignore it (ambient speech / noise).
            logger.debug("[transcribe] ignoring un-addressed utterance: %r", text)
            return
        self._buffer.feed_transcript(text, direction=direction)
        self._engaged_until = t + self._engage_window_s
        self.transcripts += 1

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

    def _reset_utt(self) -> None:
        """Clear the current utterance accumulator."""
        self._utt = []
        self._utt_samples = 0
        self._utt_started_t = None
        self._last_speech_t = None
        self._utt_direction = None

    def close(self) -> None:
        """No-op cleanup, present for the hook contract (safe + idempotent).

        ``TranscribeHook`` holds no flag and owns no background worker — it only
        feeds words into the shared cognition buffer per tick — so there is nothing
        to tear down. The method exists so the ``listen`` loop can call ``close()``
        on every hook uniformly in its ``finally``.
        """
        return None
