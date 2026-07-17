"""Tests for the think cognition engine (:mod:`reachy.speech.cognition`).

All collaborators are faked — no live LLM, TTS, or robot, and no sleeps. Ordering
guarantees are proven with :class:`threading.Event` gates so the suite is fully
deterministic regardless of scheduler timing.

The three acceptance criteria:

1. **Serialized cognition** — exactly one LLM turn runs at a time; cues that
   arrive *during* a turn are consumed only by the *next* turn's prompt.
2. **Parallel think↔speak** — early sentences reach playback while later
   sentences are still being generated (producer/consumer pipeline).
3. **Cue-only input** — the engine consumes ONLY the event buffer for input;
   it never reaches for STT/transcription/tool-use/barge-in.
"""

from __future__ import annotations

import logging
import threading

import pytest

from reachy.speech.cognition import CognitionEngine
from reachy.speech.events import EventBuffer

# ---------------------------------------------------------------------------
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------

_SENSE_LOGGER_NAME = "reachy.sense"


def _sense_records(caplog) -> list:
    return [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]


# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


class _Recorder:
    """Thread-safe recorder of synth/play calls in arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.synth_texts: list[str] = []
        self.played: list[bytes] = []
        self.played_texts: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        with self._lock:
            self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        with self._lock:
            self.played.append(pcm)
            self.played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))


def _const_clock(value: float = 0.0):
    return lambda: value


# ---------------------------------------------------------------------------
# Basic single-turn behaviour
# ---------------------------------------------------------------------------


def test_run_turn_no_cues_is_a_noop():
    """With an empty buffer, a turn neither calls the LLM nor speaks."""
    rec = _Recorder()
    calls: list = []

    def fake_stream(messages, **_kw):
        calls.append(messages)
        yield "should not happen."

    engine = CognitionEngine(
        buffer=EventBuffer(clock=_const_clock()),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        system_prompt="SYS",
    )
    spoke = engine.run_turn()

    assert spoke is False
    assert calls == []
    assert rec.synth_texts == []
    assert rec.played == []


def test_run_turn_pipes_each_sentence_through_synth_and_play():
    """A turn with cues streams sentences, synthesizing and playing each one."""
    rec = _Recorder()
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)  # "speech from the left"

    seen_messages: list = []

    def fake_stream(messages, **_kw):
        seen_messages.append(messages)
        yield "Hello there."
        yield "How are you?"

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        system_prompt="SYS",
    )
    spoke = engine.run_turn()

    assert spoke is True
    # System prompt is first; the cue text reaches the user message.
    assert seen_messages[0][0] == {"role": "system", "content": "SYS"}
    assert seen_messages[0][-1]["role"] == "user"
    assert "speech from the left" in seen_messages[0][-1]["content"]
    # Both sentences synthesized and played, in order.
    assert rec.synth_texts == ["Hello there.", "How are you?"]
    assert rec.played_texts == ["Hello there.", "How are you?"]


def test_empty_synth_output_is_not_played():
    """A sentence that synthesizes to empty bytes is skipped at playback."""
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    def fake_stream(messages, **_kw):
        yield "real."
        yield "empty."

    def synth(text: str, **_kw) -> bytes:
        return b"" if text == "empty." else b"pcm"

    played: list[bytes] = []
    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=synth,
        play_audio=lambda pcm, **_kw: played.append(pcm),
        system_prompt="SYS",
    )
    engine.run_turn()
    assert played == [b"pcm"]


# ---------------------------------------------------------------------------
# Criterion 2 — parallel think <-> speak pipeline
# ---------------------------------------------------------------------------


def test_first_sentence_plays_before_last_is_yielded():
    """Early audio reaches playback WHILE later sentences are still generating.

    The fake LLM yields the first sentence, then blocks on a gate before
    yielding the last. The play() of the first sentence opens the gate. If the
    pipeline were generate-all-then-speak, the first sentence could not have
    been played before the last was yielded and the gate would deadlock.
    """
    rec = _Recorder()
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    first_played = threading.Event()
    last_about_to_yield = threading.Event()

    def fake_stream(messages, **_kw):
        yield "First sentence."
        # Generation of the LAST sentence is gated on the first being played.
        last_about_to_yield.set()
        assert first_played.wait(timeout=5.0), "first sentence never played"
        yield "Last sentence."

    def play(pcm: bytes, **_kw) -> None:
        rec.play(pcm)
        if pcm.decode("utf-8").removeprefix("pcm:") == "First sentence.":
            first_played.set()

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=play,
        system_prompt="SYS",
    )
    engine.run_turn()

    # The generator reached the gate (proving it yielded sentence 1 first), the
    # first sentence was played to open it, and only then was the last yielded.
    assert last_about_to_yield.is_set()
    assert first_played.is_set()
    assert rec.played_texts == ["First sentence.", "Last sentence."]


# ---------------------------------------------------------------------------
# Criterion 1 — serialized cognition; mid-turn cues land in the NEXT turn
# ---------------------------------------------------------------------------


def test_cues_arriving_during_a_turn_are_consumed_by_the_next_turn():
    """A turn snapshots the buffer at the start; cues fed mid-turn go to the next.

    The fake LLM blocks mid-turn (after yielding its only sentence) on a gate.
    While it is blocked we feed a NEW cue into the buffer, then release the gate.
    We assert the new cue is absent from this turn's prompt and present in the
    next turn's prompt.
    """
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)  # turn-1 cue: left

    prompts: list[str] = []
    release_turn = threading.Event()
    turn_blocked = threading.Event()

    def fake_stream(messages, **_kw):
        prompts.append(messages[-1]["content"])
        yield "Acknowledged."
        # Block the turn open so we can inject a cue concurrently.
        turn_blocked.set()
        assert release_turn.wait(timeout=5.0), "turn never released"

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
    )

    t1 = threading.Thread(target=engine.run_turn)
    t1.start()
    assert turn_blocked.wait(timeout=5.0), "turn-1 never reached its block"

    # Feed a NEW cue WHILE turn 1 is mid-stream.
    buf.feed_doa(angle_rad=3.1, rms=0.1, is_speech=True)  # turn-2 cue: right

    # Let turn 1 finish.
    release_turn.set()
    t1.join(timeout=5.0)
    assert not t1.is_alive()

    # Turn 2 consumes only the cue that arrived during turn 1.
    release_turn.clear()
    turn_blocked.clear()
    release_turn.set()  # turn 2 should not block before us; pre-arm the gate
    engine.run_turn()

    assert "speech from the left" in prompts[0]
    assert "speech from the right" not in prompts[0]
    assert "speech from the right" in prompts[1]


def test_run_turn_holds_a_lock_so_only_one_turn_runs_at_a_time():
    """Two concurrent run_turn() calls cannot overlap — the second blocks.

    Turn A blocks inside its stream. While A is blocked, turn B is started on
    another thread; we assert B has NOT started its stream until A is released.
    """
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    a_streaming = threading.Event()
    release_a = threading.Event()
    b_streaming = threading.Event()
    which: list[str] = []

    def fake_stream(messages, **_kw):
        # First caller to enter is "A"; it blocks. The second is "B".
        if not a_streaming.is_set():
            which.append("A")
            a_streaming.set()
            assert release_a.wait(timeout=5.0)
        else:
            which.append("B")
            b_streaming.set()
        yield "ok."
        # keep a cue for B to consume
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
    )

    ta = threading.Thread(target=engine.run_turn)
    ta.start()
    assert a_streaming.wait(timeout=5.0)

    tb = threading.Thread(target=engine.run_turn)
    tb.start()

    # B must not have begun streaming while A holds the cognition lock.
    assert not b_streaming.wait(timeout=0.2), "second turn overlapped the first"

    release_a.set()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)
    assert not ta.is_alive() and not tb.is_alive()
    assert which == ["A", "B"]


# ---------------------------------------------------------------------------
# Criterion 3 — cue-only input boundary
# ---------------------------------------------------------------------------


def test_engine_only_reads_input_from_the_event_buffer():
    """The engine's only input source is the EventBuffer.snapshot().

    We wrap the buffer so every read is recorded, and assert the engine pulls
    input exclusively from snapshot() — there is no STT, tool-use, or barge-in
    channel the engine could consult.
    """
    real = EventBuffer(clock=_const_clock())
    real.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    reads: list[str] = []

    class _SpyBuffer:
        def snapshot(self):
            reads.append("snapshot")
            return real.snapshot()

        def __getattr__(self, name):  # any other access is forbidden input
            reads.append("OTHER:" + name)
            return getattr(real, name)

    def fake_stream(messages, **_kw):
        yield "fine."

    engine = CognitionEngine(
        buffer=_SpyBuffer(),
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert reads == ["snapshot"]


def test_engine_has_no_stt_or_tooluse_attributes():
    """Static boundary check: the engine exposes no transcription/tool surface."""
    engine = CognitionEngine(
        buffer=EventBuffer(clock=_const_clock()),
        stream_sentences=lambda m, **_kw: iter(()),
        synthesize=lambda t, **_kw: b"",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
    )
    forbidden = ("transcribe", "stt", "listen", "tools", "tool_use", "barge_in", "interrupt")
    for name in forbidden:
        assert not hasattr(engine, name), f"engine unexpectedly exposes {name!r}"


# ---------------------------------------------------------------------------
# run() loop — bounded, testable
# ---------------------------------------------------------------------------


def test_run_loop_runs_turns_while_cues_exist_and_stops_at_max_turns():
    """run(max_turns=N) runs at most N turns and stops."""
    buf = EventBuffer(clock=_const_clock())
    turns: list = []

    def fake_stream(messages, **_kw):
        turns.append(messages)
        yield "tick."

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
        sleep=lambda _s: None,
    )

    # A producer that always has a fresh cue to consume.
    def feeder():
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    ran = engine.run(max_turns=3, before_turn=feeder)
    assert ran == 3
    assert len(turns) == 3


def test_run_loop_stops_on_predicate():
    """A stop predicate short-circuits the loop before max_turns."""
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    count = {"n": 0}

    def fake_stream(messages, **_kw):
        count["n"] += 1
        yield "x."

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
        sleep=lambda _s: None,
    )

    def feeder():
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    def should_stop() -> bool:
        return count["n"] >= 2

    ran = engine.run(max_turns=10, stop=should_stop, before_turn=feeder)
    assert ran == 2


def test_run_loop_idle_when_no_cues_does_not_call_llm():
    """An empty buffer means no turn runs even though the loop spins."""
    buf = EventBuffer(clock=_const_clock())
    called: list = []

    def fake_stream(messages, **_kw):
        called.append(messages)
        yield "y."

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
        sleep=lambda _s: None,
    )
    ran = engine.run(max_turns=3)
    assert ran == 0
    assert called == []


def test_llm_error_propagates_out_of_run_turn():
    """A CliError from the LLM (unreachable endpoint) is not swallowed."""
    from reachy.cli._errors import EXIT_ENV_ERROR, CliError

    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    def fake_stream(messages, **_kw):
        raise CliError(code=EXIT_ENV_ERROR, message="boom", remediation="fix it")
        yield  # pragma: no cover

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        system_prompt="SYS",
    )
    with pytest.raises(CliError):
        engine.run_turn()


# ---------------------------------------------------------------------------
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------


def test_run_turn_logs_a_sense_turn_line_with_cue_count(caplog):
    """A turn that fires logs exactly one [SENSE stage=turn] line naming the cue count."""
    rec = _Recorder()
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    def fake_stream(messages, **_kw):
        yield '"hi"'

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
    )

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        assert engine.run_turn() is True

    records = _sense_records(caplog)
    turn_records = [r for r in records if "stage=turn" in r.getMessage()]
    assert len(turn_records) == 1
    assert "cue_count=1" in turn_records[0].getMessage()


def test_run_turn_no_cues_logs_no_sense_turn_line(caplog):
    """An empty-buffer no-op turn never fires the stage=turn line."""
    rec = _Recorder()
    engine = CognitionEngine(
        buffer=EventBuffer(clock=_const_clock()),
        stream_sentences=lambda m, **_kw: iter(()),
        synthesize=rec.synth,
        play_audio=rec.play,
    )

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        assert engine.run_turn() is False

    assert _sense_records(caplog) == []
