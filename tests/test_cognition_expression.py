"""Tests for the cognition loop's marker→expression + quoted-speech integration (t7).

The LLM stream now interleaves ``*emoji*`` expression markers and ``"quoted"``
speech (the marker convention, see :mod:`reachy.speech.markers`). The cognition
loop must:

* route the LLM stream through the streaming :class:`~reachy.speech.markers.MarkerParser`,
* synthesize / play **only** the quoted speech (``SpeechEvent.text``) — NEVER the
  emoji or any out-of-span prose,
* invoke the injected ``express`` callback **once per marker**, in stream order
  relative to the speech.

These tests fake every collaborator (stream / synth / play / express) — no live
LLM, TTS, robot, or sleeps. The final test is the required INTEGRATION test that
proves expression moves sourced from think reach a serial motion queue, using a
real :class:`~reachy.motion.expression.ExpressionProducer` over a fake transport
queue, with a fake express callback wired exactly as t8 will wire it.
"""

from __future__ import annotations

import threading

from reachy.speech.cognition import CognitionEngine
from reachy.speech.events import EventBuffer


def _const_clock(value: float = 0.0):
    return lambda: value


class _Recorder:
    """Thread-safe recorder of synth / play / express calls in arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []
        self.expressed: list[str] = []
        self.events: list[tuple[str, str]] = []  # ("speak"|"express", payload), in order

    def synth(self, text: str, **_kw) -> bytes:
        with self._lock:
            self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        with self._lock:
            text = pcm.decode("utf-8").removeprefix("pcm:")
            self.played_texts.append(text)
            self.events.append(("speak", text))

    def express(self, emoji: str) -> None:
        with self._lock:
            self.expressed.append(emoji)
            self.events.append(("express", emoji))


def _buf_with_cue() -> EventBuffer:
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)
    return buf


# ---------------------------------------------------------------------------
# Criterion 1 — parse markers, speak ONLY quoted text, fire express per marker
# ---------------------------------------------------------------------------


def test_only_quoted_text_is_synthesized_and_played():
    """TTS receives ONLY the quoted speech — never the emoji nor out-of-span prose."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        # Markers, quoted speech, and stray prose interleaved.
        yield '*🤔* "I wonder what that was." '
        yield 'noise *👂* "There it is again."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    spoke = engine.run_turn()

    assert spoke is True
    # Only the quoted spans were spoken — no emoji, no "noise".
    assert rec.synth_texts == ["I wonder what that was.", "There it is again."]
    assert rec.played_texts == ["I wonder what that was.", "There it is again."]
    assert "🤔" not in rec.synth_texts and "👂" not in rec.synth_texts


def test_one_express_call_per_marker_in_order():
    """The express callback fires exactly once per MarkerEvent, in stream order."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield '*🤔* "hmm." *👂* "ah." *😊*'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert rec.expressed == ["🤔", "👂", "😊"]


def test_marker_and_speech_interleave_in_stream_order():
    """Markers fire in the position they appear relative to speech."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield '*🤔* "one." *👂* "two."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert rec.events == [
        ("express", "🤔"),
        ("speak", "one."),
        ("express", "👂"),
        ("speak", "two."),
    ]


def test_express_is_optional_speech_still_works():
    """With no express callback, quoted speech is still spoken and markers are dropped."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield '*🤔* "still talking." *👂*'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        # express omitted — defaults to None (no-op)
        system_prompt="SYS",
    )
    engine.run_turn()

    assert rec.played_texts == ["still talking."]
    assert rec.expressed == []  # recorder never called


def test_marker_split_across_chunks_still_fires_once():
    """A marker split across stream chunks is assembled and fires exactly once."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        # The marker *🤔* is split across three chunks; speech split too.
        yield "*"
        yield "🤔"
        yield '* "split '
        yield 'speech."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert rec.expressed == ["🤔"]
    assert rec.played_texts == ["split speech."]


def test_unclosed_span_at_end_is_dropped():
    """An unterminated quote/marker at end-of-turn is dropped (flush rule)."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield '"closed." *🤔* "never closed'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert rec.played_texts == ["closed."]
    assert rec.expressed == ["🤔"]  # the closed marker fired; the open quote dropped


# ---------------------------------------------------------------------------
# Criterion 2 — INTEGRATION: expression moves reach the serial motion queue
# ---------------------------------------------------------------------------


def test_integration_markers_reach_the_serial_motion_queue():
    """Wire a real ExpressionProducer over a real MotionQueue, as t8 will.

    Proves end-to-end that (a) only quoted text was synthesized/played, and
    (b) one expression move per marker reached the serial MotionQueue, in order.

    A real ``listen``/``think`` executor drains the queue one move at a time, so
    each enqueued expression is observed before the next is submitted. We model
    that here with a recording executor that pops after every submit — without it,
    the two EXPRESSION_KEY moves would coalesce to the latest (by design), which is
    a property of the queue, not of the marker→express wiring under test.
    """
    from reachy.motion.expression import ExpressionProducer
    from reachy.motion.queue import MotionQueue

    queue = MotionQueue()
    producer = ExpressionProducer(queue=queue)

    synth_texts: list[str] = []
    played_texts: list[str] = []
    drained: list[str] = []

    def synth(text: str, **_kw) -> bytes:
        synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(pcm: bytes, **_kw) -> None:
        played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))

    def express(emoji: str) -> None:
        # exactly how t8 wires it: a callback into the real producer ...
        producer.express(emoji)
        # ... and the serial executor immediately drains the move it just enqueued.
        action = queue.pop()
        if action is not None:
            drained.append(action.label)

    def fake_stream(messages, **_kw):
        yield '*🤔* "I hear something." '
        yield '*👀* "Over there."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=synth,
        play_audio=play,
        express=express,
        system_prompt="SYS",
    )
    engine.run_turn()

    # (a) only the quoted text was spoken.
    assert synth_texts == ["I hear something.", "Over there."]
    assert played_texts == ["I hear something.", "Over there."]

    # (b) one move per marker reached the serial queue, in order.
    assert drained == ["express 🤔", "express 👀"]
