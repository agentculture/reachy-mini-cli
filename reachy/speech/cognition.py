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
A :class:`~reachy.cli._errors.CliError` raised by the LLM or TTS clients (e.g. an
unreachable endpoint) is **not** swallowed — it propagates out of
:meth:`run_turn` (and :meth:`run`) so the CLI's top-level handler renders it under
the structured error contract. The speak worker re-raises any such error on the
turn thread once the stream finishes.

Determinism
-----------
No wall-clock randomness and no hidden sleeps. The inter-turn pacing uses an
injectable ``sleep``; :meth:`run` takes a ``max_turns`` cap and an optional
``stop`` predicate, mirroring :func:`reachy.motion.server.run`'s ``max_ticks``
style, so tests run bounded and fully deterministic.
"""

from __future__ import annotations

import queue
import threading
from collections.abc import Iterator
from typing import Callable, Protocol

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

# Default minimum gap between turns in the run() loop (seconds).
DEFAULT_TURN_INTERVAL = 1.0


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
    """

    def __init__(
        self,
        *,
        buffer: _BufferLike,
        stream_sentences: _StreamSentences | None = None,
        synthesize: _Synthesize | None = None,
        play_audio: _PlayAudio | None = None,
        express: _Express | None = None,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        llm_kwargs: dict | None = None,
        tts_kwargs: dict | None = None,
        playback_kwargs: dict | None = None,
        sleep: Callable[[float], None] | None = None,
        turn_interval: float = DEFAULT_TURN_INTERVAL,
    ) -> None:
        self._buffer = buffer
        self._stream_sentences = stream_sentences or _llm.stream_sentences
        self._synthesize = synthesize or _tts.synthesize
        self._play_audio = play_audio or _playback.play_audio
        # Optional motion seam: fired once per expression marker, in stream order.
        # None → a no-op (markers parsed out of the speech, emoji simply not driven).
        self._express = express
        self._system_prompt = system_prompt
        self._llm_kwargs = dict(llm_kwargs or {})
        self._tts_kwargs = dict(tts_kwargs or {})
        self._playback_kwargs = dict(playback_kwargs or {})
        if sleep is None:
            import time

            sleep = time.sleep
        self._sleep = sleep
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
            Propagated unchanged from the LLM / TTS / playback collaborators
            (e.g. an unreachable endpoint). Never swallowed.
        """
        with self._turn_lock:
            cues = self._buffer.snapshot()
            if not cues:
                return False
            messages = build_messages(self._system_prompt, cues)
            self._stream_and_speak(messages)
            return True

    def _stream_and_speak(self, messages: list[dict]) -> None:
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
        try:
            chunks = self._stream_sentences(messages, **self._llm_kwargs)
            for item in _iter_work_items(chunks):
                speak_q.put(item)
        finally:
            # Always signal end-of-stream and join, so the worker terminates even
            # if the producer raised (e.g. LLM CliError mid-stream).
            speak_q.put(_DONE)
            worker.join()
        if worker_error:
            raise worker_error[0]

    def _speak_worker(self, speak_q: queue.Queue, error_out: list) -> None:
        """Drain the work queue in order: speak quoted text, fire expressions.

        Runs on its own thread so synth + playback of spoken item N overlap the
        producer's generation of item N+1. Each item is a ``(kind, payload)`` pair:
        ``("speak", text)`` is synthesized + played (empty synth output is skipped);
        ``("express", emoji)`` invokes :attr:`_express` (a no-op when it is ``None``).
        Stops on the :data:`_DONE` sentinel. A raised exception is stashed in
        ``error_out`` for the turn thread to re-raise (it cannot escape a worker
        thread on its own).
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
                    continue
                pcm = self._synthesize(payload, **self._tts_kwargs)
                if pcm:
                    self._play_audio(pcm, **self._playback_kwargs)
        except Exception as exc:  # noqa: BLE001 — re-raised on the turn thread
            error_out.append(exc)
            # Drain any remaining items so a blocked producer's put() unblocks and
            # the sentinel is consumed; we are abandoning playback for this turn.
            _drain(speak_q)

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
            Stop after this many *spoken* turns (no-op idle turns don't count).
            ``None`` runs until ``stop`` fires.
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
            The number of turns that actually spoke.
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
