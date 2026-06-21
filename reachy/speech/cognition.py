"""The ``think`` cognition engine: accumulated sense events → spoken thought.

This module ties the Wave-1 speech primitives into one engine,
:class:`CognitionEngine`, that turns the robot's perceptions into speech. It
wires together four collaborators — the sense :class:`~reachy.speech.events.EventBuffer`
(input), the streaming LLM (:func:`~reachy.speech.llm.stream_sentences`), the TTS
synthesizer (:func:`~reachy.speech.tts.synthesize`), and audio playback
(:func:`~reachy.speech.playback.play_audio`) — with two precise behaviours.

Serialized cognition
---------------------
Exactly **one** LLM turn runs at a time. :meth:`CognitionEngine.run_turn` takes a
:class:`threading.Lock` for the whole turn, so a second concurrent call blocks
until the first completes (no overlapping thoughts). A turn *snapshots* the event
buffer at its very start — before any LLM work — so any sense cues that arrive
*during* a turn accumulate in the (now-empty) buffer and are consumed only by the
**next** turn. A thought is never interrupted or re-seeded mid-stream.

Parallel think ↔ speak
----------------------
Within a turn the LLM streams complete sentences early; each finished sentence is
synthesized and played **while later sentences are still being generated**. This
is a producer/consumer pipeline, not generate-all-then-speak: :meth:`run_turn`
runs a dedicated *speak worker* thread that drains a :class:`queue.Queue`,
synthesizing and playing each queued sentence, while the main thread keeps pulling
sentences off the LLM stream and enqueuing them. So the first sentence reaches the
speaker before the LLM has finished the turn. The worker is *joined* at the end of
the turn, so the next turn starts only once all speech for this turn has finished.

Input boundary (deliberate, narrow)
------------------------------------
The engine's **only** input is the event buffer's
:meth:`~reachy.speech.events.EventBuffer.snapshot`. There is intentionally **no**
STT / transcription path, **no** tool-use, and **no** barge-in / interrupt path.
``think`` speaks *about what the robot perceives*; it does not transcribe speech,
call tools, or react to being interrupted mid-thought. Keeping the input surface
to a single method is what makes the serialized-cognition guarantee meaningful.

Errors
------
A :class:`~reachy.cli._errors.CliError` raised by the LLM client (e.g. an
unreachable endpoint) is **not** swallowed — it propagates out of
:meth:`run_turn` (and :meth:`run`) so the CLI's top-level handler renders it under
the structured error contract.

TTS / playback errors follow the LLM rule **by default** (``audio_optional=False``):
the speak worker re-raises them on the turn thread once the stream finishes, so a
dead TTS surfaces as a clean exit-2 for ``say`` / standalone ``think run``. With
``audio_optional=True`` (the folded ``listen --live`` cognition) an audio-sink
failure instead degrades to "no speech" — logged once, the clip skipped, the turn
completing normally — and after a short run of consecutive failures the audio sink
latches off entirely. Cognition keeps thinking and every non-audio sink (expression
motion, the export feed) keeps receiving the thought, because those are driven on
the producer thread ahead of the speak worker.

Determinism
-----------
No wall-clock randomness and no hidden sleeps. The inter-turn pacing uses an
injectable ``sleep``; :meth:`run` takes a ``max_turns`` cap and an optional
``stop`` predicate, mirroring :func:`reachy.motion.server.run`'s ``max_ticks``
style, so tests run bounded and fully deterministic.
"""

from __future__ import annotations

import logging
import queue
import threading
import time
from collections.abc import Iterator
from typing import Callable, Protocol

from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech import llm as _llm
from reachy.speech import playback as _playback
from reachy.speech import tts as _tts
from reachy.speech.events import SenseCue
from reachy.speech.markers import MarkerEvent, MarkerParser, SpeechEvent

# Default system prompt — terse, first-person, present-tense, spoken aloud.
DEFAULT_SYSTEM_PROMPT = (
    "You are Reachy Mini, a small expressive desk robot, thinking out loud. "
    "You are given a short list of things you just perceived through your "
    "microphone and camera. React in ONE or TWO short spoken sentences, in the "
    "first person, present tense, as if musing to yourself. Do not narrate the "
    "raw sensor readings; respond to them naturally. No markdown, no emoji, no "
    "lists."
)

logger = logging.getLogger(__name__)

# Default minimum gap between turns in the run() loop (seconds).
DEFAULT_TURN_INTERVAL = 1.0

# Consecutive audio-sink failures (in audio_optional mode) before the engine
# latches the audio sink off — see CognitionEngine._note_audio_failure. A small
# streak (not 1) tolerates a single transient blip without muting the session.
DEFAULT_AUDIO_MUTE_THRESHOLD = 2


# ---------------------------------------------------------------------------
# Collaborator protocols (documentation; engine accepts any matching callable)
# ---------------------------------------------------------------------------


class _StreamSentences(Protocol):
    def __call__(self, messages: list[dict], **kwargs) -> Iterator[str]: ...


class _Synthesize(Protocol):
    def __call__(self, text: str, **kwargs) -> bytes: ...


class _PlayAudio(Protocol):
    def __call__(self, pcm_bytes: bytes, **kwargs) -> None: ...


class _Express(Protocol):
    def __call__(self, emoji: str) -> None: ...


class _BufferLike(Protocol):
    def snapshot(self) -> list[SenseCue]: ...


# Sentinel pushed onto the speak queue to tell the worker the stream is done.
_DONE = object()


# ---------------------------------------------------------------------------
# Prompt construction
# ---------------------------------------------------------------------------


def build_messages(system_prompt: str, cues: list[SenseCue]) -> list[dict]:
    """Build an OpenAI chat-format message list from a system prompt + cues.

    The cues become a single user message that lists what the robot perceived,
    oldest first. Returns ``[{role: system, ...}, {role: user, ...}]``.
    """
    lines = [f"- {cue.text}" for cue in cues]
    user = "I just perceived:\n" + "\n".join(lines)
    return [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user},
    ]


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------


class CognitionEngine:
    """Turns accumulated sense cues into spoken thought, one serialized turn at a time.

    Parameters
    ----------
    buffer:
        The sense-event source. Its :meth:`~EventBuffer.snapshot` is the engine's
        **only** input — see the module docstring's input-boundary note.
    stream_sentences:
        Callable ``(messages, **kw) -> Iterator[str]`` yielding complete
        sentences early. Defaults to :func:`reachy.speech.llm.stream_sentences`.
    synthesize:
        Callable ``(text, **kw) -> bytes`` returning PCM16. Defaults to
        :func:`reachy.speech.tts.synthesize`.
    play_audio:
        Callable ``(pcm_bytes, **kw) -> None`` playing PCM. Defaults to
        :func:`reachy.speech.playback.play_audio`.
    express:
        Optional callable ``(emoji: str) -> None`` invoked **once per expression
        marker** the LLM emits (``*🤔*``), in stream order relative to the spoken
        text. Defaults to ``None`` (a no-op — markers are parsed and the emoji is
        simply not driven). This is the motion seam: ``think``'s CLI passes
        ``lambda emoji: expression_producer.express(emoji)`` so each marker enqueues
        one calm gesture on the serial motion queue. The engine deliberately does
        **not** import :mod:`reachy.motion` — the producer is injected through this
        callback, preserving the say/think and speech/motion boundaries.
    export:
        Optional :class:`~reachy.export.exporter.ExportHook` — the **export hook**,
        bundling ``emit`` (the sink, e.g. ``JsonlExporter.emit``, which never
        raises), ``pose_resolver`` (emoji → 9-axis pose dict, or ``None`` for an
        unknown emoji; fills :attr:`EmotionEvent.pose`), and ``time_fn`` (the
        wall-clock source used to stamp each event's ``ts`` — tests inject a
        constant for determinism). When given, each turn emits export blocks as it
        runs: one :class:`~reachy.export.events.EmotionEvent` per expression marker,
        one :class:`~reachy.export.events.MessageEvent` per quoted speech span (both
        interleaved in stream order, alongside the existing speech/motion side
        effects), and exactly one :class:`~reachy.export.events.ThinkingEvent` at
        the end of the turn carrying the snapshot's sense cues and the **raw**
        concatenated LLM text for the turn (see the raw-thought tap below). Defaults
        to ``None`` — when absent the engine's output, control flow, and timing are
        *byte-identical* to a build without this hook: no events are built and the
        export branch is never entered.
    system_prompt:
        The system message prepended to every turn's prompt.
    llm_kwargs / tts_kwargs / playback_kwargs:
        Optional keyword dicts forwarded to the respective collaborator on each
        call (e.g. ``base_url``, ``model``, ``transport``).
    sleep:
        Injectable ``(seconds) -> None`` used for inter-turn pacing in
        :meth:`run`. Defaults to :func:`time.sleep`; tests inject a no-op.
    turn_interval:
        Minimum gap (seconds) between turns in :meth:`run`.

    All collaborators are injectable so tests can substitute deterministic fakes;
    each defaults to the real Wave-1 module function.

    Raw-thought tap
    ---------------
    When ``export`` is set the engine accumulates the **raw** LLM text for the turn
    — every chunk pulled off the stream, *before* the
    :class:`~reachy.speech.markers.MarkerParser` discards prose outside ``*…*`` /
    ``"…"`` spans. The tap sits at the point chunks are fed to the parser
    (:meth:`_stream_and_speak`), so the resulting :class:`ThinkingEvent.text` is the
    full concatenation (including un-spoken prose, the literal ``*emoji*`` markers,
    the quote delimiters, and any unclosed span dropped at flush) — not merely the
    spoken text. The tap is a pure side observation; it never alters what is fed to
    the parser, so speech behaviour is unchanged.
    """

    def __init__(
        self,
        *,
        buffer: _BufferLike,
        stream_sentences: _StreamSentences | None = None,
        synthesize: _Synthesize | None = None,
        play_audio: _PlayAudio | None = None,
        express: _Express | None = None,
        export: ExportHook | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        llm_kwargs: dict | None = None,
        tts_kwargs: dict | None = None,
        playback_kwargs: dict | None = None,
        sleep: Callable[[float], None] | None = None,
        turn_interval: float = DEFAULT_TURN_INTERVAL,
        audio_optional: bool = False,
    ) -> None:
        self._buffer = buffer
        self._stream_sentences = stream_sentences or _llm.stream_sentences
        self._synthesize = synthesize or _tts.synthesize
        self._play_audio = play_audio or _playback.play_audio
        # Audio-optional mode: when True a synth/playback failure degrades to
        # "no speech" instead of aborting the turn (and killing cognition). The
        # thought still flows to every other sink — expression motion + the export
        # feed are produced on the producer thread, ahead of and independent of the
        # speak worker — so a screen/log consumer is unaffected by a dead TTS. After
        # ``DEFAULT_AUDIO_MUTE_THRESHOLD`` consecutive failures the audio sink latches
        # off (no further synth attempts) so a hard-down TTS never throttles cognition
        # to one turn per request-timeout. Default False keeps the strict, fail-fast
        # contract (an unreachable TTS raises CliError → exit 2) for `say`/standalone
        # `think run`; the folded `listen --live` cognition opts in. The threshold is a
        # module constant (not a ctor arg) — internal tuning, rarely overridden; tests
        # set ``_audio_mute_threshold`` directly when they need a different value.
        self._audio_optional = audio_optional
        self._audio_mute_threshold = DEFAULT_AUDIO_MUTE_THRESHOLD
        self._audio_muted = False
        self._audio_fail_streak = 0
        # Optional motion seam: fired once per expression marker, in stream order.
        # None → a no-op (markers parsed out of the speech, emoji simply not driven).
        self._express = express
        # Optional export hook (bundles emit + pose_resolver + time_fn). None →
        # the export path is never entered, so behaviour is byte-identical to a
        # build without the hook.
        self._export = export
        self._system_prompt = system_prompt
        self._llm_kwargs = dict(llm_kwargs or {})
        self._tts_kwargs = dict(tts_kwargs or {})
        self._playback_kwargs = dict(playback_kwargs or {})
        self._sleep = sleep if sleep is not None else time.sleep
        self._turn_interval = turn_interval

        # The cognition lock guarantees exactly one turn runs at a time: a
        # concurrent run_turn() blocks here until the in-flight turn releases it.
        self._turn_lock = threading.Lock()

    # ------------------------------------------------------------------
    # One serialized turn
    # ------------------------------------------------------------------

    def run_turn(self) -> bool:
        """Execute exactly one serialized cognition turn.

        Snapshots the event buffer (atomically clearing it), and — if it held any
        cues — builds the prompt, streams sentences from the LLM, and pipelines
        each through synth + playback in parallel with later sentence generation.

        Holds :attr:`_turn_lock` for the whole turn, so no other turn can run
        concurrently. Cues fed into the buffer during this turn are *not* seen
        here (the snapshot was taken first); they are consumed by the next turn.

        Returns
        -------
        bool
            ``True`` if there were cues and the engine spoke a turn; ``False`` if
            the buffer was empty (a no-op turn — no LLM call, no audio).

        Raises
        ------
        reachy.cli._errors.CliError
            Propagated unchanged from the LLM collaborator (e.g. an unreachable
            endpoint). TTS / playback errors propagate the same way **unless**
            ``audio_optional`` is set, in which case they are absorbed by the speak
            worker (logged once, speech skipped) and the turn completes normally.
        """
        with self._turn_lock:
            cues = self._buffer.snapshot()
            if not cues:
                return False
            messages = build_messages(self._system_prompt, cues)
            self._stream_and_speak(messages, cues)
            return True

    def _stream_and_speak(self, messages: list[dict], cues: list[SenseCue]) -> None:
        """Producer/consumer pipeline: parse markers, speak quoted text, drive expressions.

        The main thread is the *producer*: it pulls chunks off the LLM stream,
        feeds each through a streaming :class:`~reachy.speech.markers.MarkerParser`
        (so a marker / quoted span split across chunks is assembled correctly), and
        enqueues one ordered *work item* per parsed event — ``("speak", text)`` for a
        :class:`SpeechEvent`, ``("express", emoji)`` for a :class:`MarkerEvent`. A
        dedicated *speak worker* thread is the *consumer*: it synthesizes + plays each
        spoken item and fires :attr:`_express` for each expression item, in queue
        (i.e. stream) order. Because the worker runs concurrently with the producer,
        the first quoted sentence reaches the speaker before the turn's stream ends —
        the think↔speak overlap is preserved for the spoken text.

        Only the **quoted** text is ever synthesized; the emoji / markers are never
        spoken. Markers fire in the exact position they appear relative to speech.

        Legacy / un-marked prose (a stream with no ``*…*`` or ``"…"`` spans, e.g. the
        plain-sentence fakes in the older tests) is spoken verbatim — see
        :func:`_iter_work_items`.

        Export hook (only when :attr:`_export` is set)
        ----------------------------------------------
        ``cues`` is the turn's buffer snapshot; it is used only to build the
        turn-end :class:`ThinkingEvent`. The producer's chunk source is wrapped by
        :func:`_tap_raw` so the **raw** LLM text is accumulated *before* the parser
        discards out-of-span prose — that accumulation becomes ``ThinkingEvent.text``.
        As each work item is produced (in stream order, on this producer thread) the
        corresponding :class:`EmotionEvent` / :class:`MessageEvent` is exported, so
        emotion/message ordering follows the stream exactly and never depends on the
        worker's playback timing. The :class:`ThinkingEvent` is exported once, after
        the producer loop drains. Exports are pure side observations — they do not
        touch the work queue, so speech behaviour, ordering, and the think↔speak
        overlap are unchanged. When :attr:`_export` is ``None`` none of this runs.

        Any exception from the worker (synth/playback ``CliError``) is captured and
        re-raised on this thread after the worker is joined, so it propagates out of
        :meth:`run_turn` under the error contract.
        """
        speak_q: queue.Queue = queue.Queue()
        worker_error: list[BaseException] = []
        worker = threading.Thread(
            target=self._speak_worker,
            args=(speak_q, worker_error),
            name="reachy-think-speak",
            daemon=True,
        )
        worker.start()
        # Raw-thought accumulator: filled by _tap_raw only when exporting.
        raw_parts: list[str] = []
        try:
            chunks = self._stream_sentences(messages, **self._llm_kwargs)
            if self._export is not None:
                chunks = _tap_raw(chunks, raw_parts)
            for item in _iter_work_items(chunks):
                speak_q.put(item)
                if self._export is not None:
                    self._emit_for_item(item)
        finally:
            # Always signal end-of-stream and join, so the worker terminates even
            # if the producer raised (e.g. LLM CliError mid-stream).
            speak_q.put(_DONE)
            worker.join()
        if worker_error:
            raise worker_error[0]
        # Turn-end thinking block: the full raw stream + this turn's sense cues.
        if self._export is not None:
            self._export.emit(
                ThinkingEvent(
                    cues=[cue.text for cue in cues],
                    text="".join(raw_parts),
                    ts=self._export.time_fn(),
                )
            )

    def _emit_for_item(self, item: tuple[str, str]) -> None:
        """Export the block for one work item, in producer (stream) order.

        ``("express", emoji)`` → :class:`EmotionEvent` (pose via the hook's
        ``pose_resolver`` when set); ``("speak", text)`` → :class:`MessageEvent`.
        Only called when :attr:`_export` is set; the hook's ``emit`` is the
        caller's sink (the real :class:`JsonlExporter.emit` never raises).
        """
        hook = self._export  # never None: only called from the guarded export branch
        kind, payload = item
        if kind == "express":
            pose = hook.pose_resolver(payload) if hook.pose_resolver is not None else None
            hook.emit(EmotionEvent(emoji=payload, pose=pose, ts=hook.time_fn()))
        else:  # "speak"
            hook.emit(MessageEvent(text=payload, ts=hook.time_fn()))

    def _speak_worker(self, speak_q: queue.Queue, error_out: list) -> None:
        """Drain the work queue in order: speak quoted text, fire expressions.

        Runs on its own thread so synth + playback of spoken item N overlap the
        producer's generation of item N+1. Each item is a ``(kind, payload)`` pair:
        ``("speak", text)`` is synthesized + played (empty synth output is skipped);
        ``("express", emoji)`` invokes :attr:`_express` (a no-op when it is ``None``).
        Stops on the :data:`_DONE` sentinel. A raised exception is stashed in
        ``error_out`` for the turn thread to re-raise (it cannot escape a worker
        thread on its own).

        Audio-optional mode (:attr:`_audio_optional`): a synth/playback failure on a
        spoken item is logged once and the clip skipped, rather than aborting the
        turn — so a dead TTS no longer kills cognition. After
        :attr:`_audio_mute_threshold` consecutive failures the audio sink latches off
        for the rest of the engine's life (:attr:`_audio_muted`), so a hard-down TTS
        does not throttle every turn by the synth timeout. Expression items and the
        export feed (driven on the producer thread) are unaffected either way.
        """
        try:
            while True:
                item = speak_q.get()
                if item is _DONE:
                    return
                kind, payload = item
                if kind == "express":
                    if self._express is not None:
                        self._express(payload)
                elif not self._audio_muted:
                    self._speak_clip(payload)
        except Exception as exc:  # noqa: BLE001 — re-raised on the turn thread
            error_out.append(exc)
            # Drain any remaining items so a blocked producer's put() unblocks and
            # the sentinel is consumed; we are abandoning playback for this turn.
            _drain(speak_q)

    def _speak_clip(self, payload: str) -> None:
        """Synthesize + play one spoken clip (empty synth output is skipped).

        Strict mode (``audio_optional`` False) lets a synth/playback exception
        propagate to :meth:`_speak_worker`, which stashes it for the turn thread to
        re-raise. In audio-optional mode the failure is absorbed via
        :meth:`_note_audio_failure` (logged once, the clip skipped) so the turn
        completes and cognition keeps running.
        """
        try:
            pcm = self._synthesize(payload, **self._tts_kwargs)
            if pcm:
                self._play_audio(pcm, **self._playback_kwargs)
            self._audio_fail_streak = 0
        except Exception:  # noqa: BLE001
            # Strict mode re-raises (the worker stashes it for the turn thread);
            # audio_optional absorbs it so the turn completes and cognition continues.
            if not self._audio_optional:
                raise
            self._note_audio_failure()

    def _note_audio_failure(self) -> None:
        """Record one audio-sink failure in audio-optional mode (log once, maybe latch).

        Logs on the first failure of a streak; once
        :attr:`_audio_mute_threshold` consecutive failures accumulate, latches the
        audio sink off (:attr:`_audio_muted`) so no further synth is attempted —
        cognition keeps thinking at full speed and feeds every non-audio sink.
        """
        self._audio_fail_streak += 1
        if self._audio_fail_streak == 1:
            logger.warning(
                "cognition audio sink failed; continuing without speech (audio is optional)",
                exc_info=True,
            )
        if not self._audio_muted and self._audio_fail_streak >= self._audio_mute_threshold:
            self._audio_muted = True
            logger.warning(
                "cognition audio muted after %d consecutive failures; thoughts continue "
                "(expression + export sinks unaffected)",
                self._audio_fail_streak,
            )

    # ------------------------------------------------------------------
    # The thin loop
    # ------------------------------------------------------------------

    def run(
        self,
        *,
        max_turns: int | None = None,
        stop: Callable[[], bool] | None = None,
        before_turn: Callable[[], None] | None = None,
    ) -> int:
        """Repeatedly run turns while cues exist, until stopped. Returns turns run.

        Mirrors :func:`reachy.motion.server.run`'s bounded-loop style: a turn runs
        only when the buffer holds unprocessed cues *and* the engine is idle (turns
        never overlap — :meth:`run_turn` serializes them). Between turns the loop
        sleeps ``turn_interval`` via the injectable ``sleep``.

        Parameters
        ----------
        max_turns:
            Stop after this many turns that *ran* — i.e. turns where cues existed
            and a cognition turn was produced (LLM output + expression markers +
            export blocks); no-op idle turns (empty buffer) don't count. ``None``
            runs until ``stop`` fires. Note: in ``audio_optional`` mode a counted
            turn may complete with **no audio** once the audio sink has latched off
            — it still produced a cognition turn, so it counts. (The folded
            ``listen --live`` loop drives ``run`` with ``stop`` and no ``max_turns``,
            so muted turns never cause premature termination there.)
        stop:
            Optional zero-arg predicate; the loop exits when it returns truthy
            (checked before each turn). Keeps the loop testable / bounded.
        before_turn:
            Optional zero-arg hook called at the top of each iteration — e.g. to
            pump fresh sense readings into the buffer in a test, or to poll a
            sensor source in production. The engine itself reads input *only* from
            the buffer; this hook is how callers feed it.

        Returns
        -------
        int
            The number of turns that ran (produced a cognition turn). In strict
            mode every such turn also spoke; in ``audio_optional`` mode a muted turn
            is counted even though it played no audio.
        """
        spoken = 0
        first = True
        while True:
            if stop is not None and stop():
                break
            if max_turns is not None and spoken >= max_turns:
                break
            if before_turn is not None:
                before_turn()
            if not first:
                self._sleep(self._turn_interval)
            first = False
            if self.run_turn():
                spoken += 1
            elif before_turn is None and stop is None and max_turns is not None:
                # No producer hook and nothing left to consume: an empty buffer
                # will never refill on its own, so stop spinning rather than busy
                # loop until max_turns. (With a producer/stop predicate the caller
                # controls termination.)
                break
        return spoken


def _tap_raw(chunks: Iterator[str], sink: list[str]) -> Iterator[str]:
    """Pass-through generator that appends each raw chunk to ``sink`` before yielding.

    This is the **raw-thought tap**: it sits between the LLM stream and the
    :class:`~reachy.speech.markers.MarkerParser` so the full, un-discarded stream is
    captured for the turn's :class:`ThinkingEvent`. It yields each chunk *verbatim*
    (including empty chunks) so the parser sees an identical stream to the un-tapped
    case — the tap never alters what is parsed or spoken. ``sink`` accumulates the
    chunk strings; ``"".join(sink)`` is the byte-exact raw turn text once the stream
    is exhausted.
    """
    for chunk in chunks:
        sink.append(chunk)
        yield chunk


def _event_to_item(event) -> tuple[str, str]:
    """Map a parsed marker/speech event onto a worker work item."""
    if isinstance(event, MarkerEvent):
        return ("express", event.emoji)
    assert isinstance(event, SpeechEvent)  # nosec B101 — exhaustive over the Event union
    return ("speak", event.text)


def _iter_work_items(chunks: Iterator[str]):
    """Turn the raw LLM chunk stream into an ordered stream of worker work items.

    Each yielded item is ``("speak", text)`` (quoted speech to synthesize + play) or
    ``("express", emoji)`` (an expression marker to drive), in the order the spans
    close in the stream. Chunks are fed **incrementally** through a single
    :class:`~reachy.speech.markers.MarkerParser`, so a marker / quoted span split
    across chunks is assembled correctly and the item is yielded the moment the span
    closes — this preserves the producer/consumer overlap for the spoken text.

    Backward compatibility: a stream that uses **no** marker convention at all (no
    ``*`` / ``"`` — the plain-sentence fakes in the older cognition tests, and any
    LLM that ignores the convention) is spoken verbatim. A chunk is treated as such
    legacy prose only while the parser is idle (not mid-span) and the chunk carries
    no delimiter, so a real marked stream is never mis-spoken.
    """
    parser = MarkerParser()
    marked = False  # latches True on the first marker/quote delimiter of the turn
    for chunk in chunks:
        if not chunk:
            continue
        if not marked and "*" not in chunk and '"' not in chunk:
            # Legacy fast-path: no delimiter has appeared yet this turn and this
            # chunk carries none either → it is plain prose; speak it verbatim. This
            # preserves the pre-marker contract (and its streaming overlap) for any
            # stream that ignores the marker convention. A marked stream's first
            # relevant chunk *does* carry a delimiter, so it never lands here.
            yield ("speak", chunk)
            continue
        marked = True  # from the first delimiter on, the parser owns the stream
        for event in parser.feed(chunk):
            yield _event_to_item(event)
    for event in parser.flush():  # always [] today (unclosed spans dropped)
        yield _event_to_item(event)


def _drain(q: queue.Queue) -> None:
    """Best-effort drain of a queue (used to unblock a producer on worker error)."""
    try:
        while True:
            q.get_nowait()
    except queue.Empty:
        return
