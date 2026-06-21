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
from typing import Callable

from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SampleProvider, SenseSample
from reachy.speech.events import EventBuffer
from reachy.speech.stt import Transcriber

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
        mute_until: Callable[[], float] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._provider = sample_provider
        self._buffer = buffer
        self._transcriber = transcriber if transcriber is not None else Transcriber()
        self._mute_until = mute_until if mute_until is not None else (lambda: 0.0)
        if clock is not None:
            self._clock = clock
        else:
            import time

            self._clock = time.monotonic

        #: Count of ticks that yielded a non-empty transcript (diagnostics / tests).
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
        """Transcribe the sample's audio when eligible and feed any recognised words.

        Eligibility (cheap-first): the sample must carry speech AND a raw audio
        chunk, and the tick must be outside the self-mute window. The self-mute
        check happens **before** :meth:`Transcriber.transcribe` is called, so no STT
        POST ever happens while the robot is muted (it must not transcribe its own
        voice). A non-empty transcript is fed via
        :meth:`~reachy.speech.events.EventBuffer.feed_transcript`; a ``None`` /
        empty transcript feeds nothing.
        """
        if not sample.speech or sample.audio is None:
            return
        if t < self._mute_until():
            # Inside the self-mute window — drop the audio BEFORE transcription so
            # the robot never POSTs (and so never transcribes) its own speech.
            return
        text = self._transcriber.transcribe(sample.audio)  # type: ignore[attr-defined]
        if not text:
            return
        self._buffer.feed_transcript(text)
        self.transcripts += 1

    def close(self) -> None:
        """No-op cleanup, present for the hook contract (safe + idempotent).

        ``TranscribeHook`` holds no flag and owns no background worker — it only
        feeds words into the shared cognition buffer per tick — so there is nothing
        to tear down. The method exists so the ``listen`` loop can call ``close()``
        on every hook uniformly in its ``finally``.
        """
        return None
