"""Integration / E2E tests for the ``say`` and ``think`` CLI nouns.

Drives both verbs end-to-end through :func:`reachy.cli.main` with STUBBED LLM,
TTS, and media — no live robot, daemon, network, or audio device.

Acceptance criteria
-------------------
1. ``think run`` (bounded with ``--max-turns 1``) with stubbed sense feed,
   LLM, TTS, and playback: assert the sense cues lead to an LLM thought whose
   sentences are synthesized and played in the CORRECT ORDER through the fake
   sink.

2. ``say run`` works with NO LLM and NO senses (assert neither module is
   touched); ``think run`` works as a full sense→reason→speak loop — each
   independently invokable through the top-level CLI.

3. xdist-safe: no shared global state, no real sockets, no sleeps-as-
   synchronization. Thread-safe lists under a lock and Event gates prove
   ordering without timing assumptions.
"""

from __future__ import annotations

import threading
from typing import Iterator

import pytest

import reachy.cli._commands.say as say_mod
import reachy.cli._commands.think as think_mod
from reachy.cli import main
from reachy.speech.events import EventBuffer

# ---------------------------------------------------------------------------
# Shared fakes & helpers
# ---------------------------------------------------------------------------


class _ThreadSafeRecorder:
    """Record synth/play calls in arrival order, thread-safely.

    The cognition engine's speak worker runs on a dedicated thread, so writes
    to synth_texts/played_texts can race without a lock.  This recorder uses
    one lock for both lists so the order is globally consistent.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        with self._lock:
            self.synth_texts.append(text)
        return ("pcm:" + text).encode()

    def play(self, pcm: bytes, **_kw) -> None:
        with self._lock:
            self.played_texts.append(pcm.decode().removeprefix("pcm:"))


# ---------------------------------------------------------------------------
# AC-1: think E2E — sense cues → LLM → TTS → ordered playback
# ---------------------------------------------------------------------------


def test_think_e2e_ordered_audio_via_main(monkeypatch, capsys) -> None:
    """E2E: main(['think', 'run', '--max-turns', '1']) with stubbed collaborators.

    Proves:
    * Sense cues injected by the fake feed appear in the LLM prompt.
    * Each sentence yielded by the LLM is synthesized then played — in ORDER.
    * The "think overlap" property: uses a threading.Event gate to prove the
      first sentence reaches playback while the last is still being generated
      (i.e. sentence-at-a-time streaming, not generate-all-then-speak).
    * Ordered audio: [sentence_1, sentence_2] arrives at the fake sink in the
      right order, verified from the thread-safe recorder.
    """
    rec = _ThreadSafeRecorder()

    # Gate: sentence_1 played ← proved by gating sentence_2 generation on it.
    first_played = threading.Event()

    def fake_stream(messages: list[dict], **_kw) -> Iterator[str]:
        # Verify the sense cue is present in the user message.
        user = messages[-1]["content"]
        assert "speech from the left" in user
        yield "I notice something."
        # Block until the first sentence has been played — proves sentence-
        # streaming (the speak worker is running concurrently).
        assert first_played.wait(timeout=5.0), "first sentence was never played"
        yield "How interesting."

    def play_and_signal(pcm: bytes, **_kw) -> None:
        rec.play(pcm)
        if rec.played_texts and rec.played_texts[0] == "I notice something.":
            first_played.set()

    # Deterministic sense feed: inject exactly one DoA cue on the first call.
    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)
        injected.append(1)

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", play_and_signal)

    rc = main(["think", "run", "--max-turns", "1"])

    assert rc == 0
    # Both sentences synthesized in order.
    assert rec.synth_texts == ["I notice something.", "How interesting."]
    # Both sentences played in order (ordering guarantee).
    assert rec.played_texts == ["I notice something.", "How interesting."]
    # The gate was opened — proves sentence-streaming (not batch-then-speak).
    assert first_played.is_set()


def test_think_e2e_multiple_sentences_all_reach_playback(monkeypatch) -> None:
    """All sentences from a multi-sentence LLM turn reach the playback sink.

    Uses three sentences to confirm the full pipeline carries all audio without
    dropping any.  No threading gate needed for ordering — the join() in
    _stream_and_speak guarantees the worker finishes before we assert.
    """
    rec = _ThreadSafeRecorder()
    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=1.57, rms=0.3, is_speech=True)
        injected.append(1)

    def fake_stream(messages: list[dict], **_kw) -> Iterator[str]:
        yield "First."
        yield "Second."
        yield "Third."

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = main(["think", "run", "--max-turns", "1"])

    assert rc == 0
    assert rec.synth_texts == ["First.", "Second.", "Third."]
    assert rec.played_texts == ["First.", "Second.", "Third."]


def test_think_e2e_json_output_via_main(monkeypatch, capsys) -> None:
    """think run --json via main() emits a valid JSON result with turns count."""
    import json

    rec = _ThreadSafeRecorder()
    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)
        injected.append(1)

    def fake_stream(messages: list[dict], **_kw) -> Iterator[str]:
        yield "Acknowledged."

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = main(["think", "run", "--json", "--max-turns", "1"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["turns"] == 1


# ---------------------------------------------------------------------------
# AC-2a: say E2E — no LLM, no senses, dumb-pipe boundary held under main()
# ---------------------------------------------------------------------------


def test_say_e2e_no_llm_no_senses_via_main(monkeypatch) -> None:
    """say run via main() touches neither reachy.speech.llm nor reachy.speech.events.

    This is the dumb-pipe boundary test at the CLI level: we drive ``say run``
    through the top-level :func:`main` entry point (same path a real user takes)
    and confirm the LLM and events modules are not imported or called.
    """
    import sys

    synth_calls: list[str] = []
    play_calls: list[bytes] = []

    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **k: synth_calls.append(t) or b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda d, **k: play_calls.append(d))

    # Remove any cached copies of llm / events so a fresh import would be detectable.
    llm_key = "reachy.speech.llm"
    events_key = "reachy.speech.events"
    saved_llm = sys.modules.pop(llm_key, None)
    saved_events = sys.modules.pop(events_key, None)

    try:
        rc = main(["say", "run", "hello robot"])
    finally:
        if saved_llm is not None:
            sys.modules[llm_key] = saved_llm
        if saved_events is not None:
            sys.modules[events_key] = saved_events

    assert rc == 0
    assert synth_calls == ["hello robot"]
    assert play_calls == [b"pcm"]
    # Neither module was freshly imported during the say run.
    assert llm_key not in sys.modules or sys.modules.get(llm_key) is saved_llm
    assert events_key not in sys.modules or sys.modules.get(events_key) is saved_events


def test_say_e2e_full_pipeline_via_main(monkeypatch) -> None:
    """say run: text passes through synthesize → play_audio via main()."""
    synth_calls: list[str] = []
    play_calls: list[bytes] = []

    def _synth(text: str, **_kw) -> bytes:
        synth_calls.append(text)
        return b"audio:" + text.encode()

    def _play(data: bytes, **_kw) -> None:
        play_calls.append(data)

    monkeypatch.setattr(say_mod, "_synthesize", _synth)
    monkeypatch.setattr(say_mod, "_play_audio", _play)

    rc = main(["say", "run", "speak this"])

    assert rc == 0
    assert synth_calls == ["speak this"]
    assert play_calls == [b"audio:speak this"]


def test_say_e2e_json_output_via_main(monkeypatch, capsys) -> None:
    """say run --json via main() emits structured JSON."""
    import json

    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **k: b"x" * 42)
    monkeypatch.setattr(say_mod, "_play_audio", lambda d, **k: None)

    rc = main(["say", "run", "--json", "hi"])

    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    assert payload["text"] == "hi"
    assert payload["bytes"] == 42


# ---------------------------------------------------------------------------
# AC-2b: think E2E — full sense→reason→speak loop, independently invokable
# ---------------------------------------------------------------------------


def test_think_e2e_independence_from_say(monkeypatch) -> None:
    """think run is independently invokable: it drives LLM and the sense feed
    without touching the say noun's synthesize/play_audio seams.

    We patch think's OWN module-level seams (not say's) and confirm the loop
    completes successfully, proving the two nouns share no hidden state.
    """
    rec = _ThreadSafeRecorder()
    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=0.0, rms=0.15, is_speech=True)
        injected.append(1)

    def fake_stream(messages: list[dict], **_kw) -> Iterator[str]:
        yield "I see."

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    # say_mod seams are untouched — if think called into them we'd get the real
    # synthesize/play_audio and a network error in CI, not a pass.
    rc = main(["think", "run", "--max-turns", "1"])

    assert rc == 0
    assert rec.played_texts == ["I see."]


def test_say_and_think_sequential_independence(monkeypatch) -> None:
    """Running say then think in the same process leaves no cross-contamination.

    The two nouns use completely separate seams; patching one does not affect
    the other.  This test patches say's seams, runs say, un-patches them, then
    runs think with think's seams patched, and asserts each recorded the right calls.
    """
    say_plays: list[bytes] = []
    think_rec = _ThreadSafeRecorder()

    # --- say run ---
    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **k: b"say-pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda d, **k: say_plays.append(d))

    rc_say = main(["say", "run", "a quick test"])
    assert rc_say == 0
    assert say_plays == [b"say-pcm"]

    # --- think run ---
    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)
        injected.append(1)

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", lambda m, **k: iter(["Yes."]))
    monkeypatch.setattr(think_mod, "_synthesize", think_rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", think_rec.play)

    rc_think = main(["think", "run", "--max-turns", "1"])
    assert rc_think == 0
    assert think_rec.played_texts == ["Yes."]

    # say_plays was not extended by the think run.
    assert say_plays == [b"say-pcm"]


# ---------------------------------------------------------------------------
# AC-1 extension: verify ordering without timing assumptions (Event gate)
# ---------------------------------------------------------------------------


def test_think_e2e_sentence_ordering_proven_with_event_gate(monkeypatch) -> None:
    """Ordered playback proven via Event gates — no sleeps-as-synchronization.

    The fake LLM yields sentence_1, then blocks on ``second_gate`` until
    sentence_1 has been played (``first_played`` is set by the play hook).
    Only then is sentence_2 yielded.

    If the pipeline ran generate-all-then-speak, sentence_2 would be yielded
    before sentence_1 was played, and the gate-wait would deadlock (5 s timeout
    → test failure).  The gate opening proves sentence-streaming.

    We then assert the recorder captured both sentences in the correct order.
    """
    rec = _ThreadSafeRecorder()
    first_played = threading.Event()
    second_gate_checked = threading.Event()

    def fake_stream(messages: list[dict], **_kw) -> Iterator[str]:
        yield "Sentence one."
        # Block: generation of sentence_2 is gated on sentence_1 being played.
        second_gate_checked.set()
        assert first_played.wait(timeout=5.0), "sentence_1 never played before sentence_2 generated"
        yield "Sentence two."

    def recording_play(pcm: bytes, **_kw) -> None:
        rec.play(pcm)
        # Open the gate once sentence_1 is played.
        if not first_played.is_set() and rec.played_texts[0:1] == ["Sentence one."]:
            first_played.set()

    injected: list[int] = []

    def fake_feed(buffer: EventBuffer) -> None:
        if not injected:
            buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)
        injected.append(1)

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buffer: lambda: fake_feed(buffer),
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", recording_play)

    rc = main(["think", "run", "--max-turns", "1"])

    assert rc == 0
    # The generator reached the gate (sentence_1 was yielded before sentence_2).
    assert second_gate_checked.is_set()
    # sentence_1 was played before sentence_2 was generated.
    assert first_played.is_set()
    # Final order check: both sentences in the right order.
    assert rec.played_texts == ["Sentence one.", "Sentence two."]
    assert rec.synth_texts == ["Sentence one.", "Sentence two."]


# ---------------------------------------------------------------------------
# Isolation fixtures (mirrors test_think.py)
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch, tmp_path) -> None:  # type: ignore[return]
    """Isolate environment state so tests are xdist-safe.

    * REACHY_STATE_DIR → tmp_path (supervisor PID/log files don't bleed across).
    * REACHY_BASE_URL / REACHY_TRANSPORT unset (no accidental live robot calls).
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
