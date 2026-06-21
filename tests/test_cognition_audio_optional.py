"""Tests for ``CognitionEngine(audio_optional=...)`` — TTS is a degradable sink.

A wedged TTS endpoint used to crash the cognition worker (a synth ``CliError``
propagated out of ``run_turn`` and stopped the folded ``listen --live`` thinking
loop entirely). ``audio_optional=True`` makes audio one *optional* output: a
synth/playback failure degrades to "no speech" — the turn completes, cognition
keeps running, and every non-audio sink (expression motion + the export feed) still
receives the thought. After ``audio_mute_threshold`` consecutive failures the audio
sink latches off so a hard-down TTS does not throttle every turn by the synth
timeout.

The default (``audio_optional=False``) keeps the strict fail-fast contract proven
in ``test_think.py`` (an unreachable TTS → exit 2), so it is re-asserted here too.
"""

from __future__ import annotations

import threading

import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.export.events import MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech.cognition import CognitionEngine
from reachy.speech.events import EventBuffer


def _const_clock(value: float = 0.0):
    return lambda: value


def _buf_with_cue() -> EventBuffer:
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)
    return buf


def _refill(buf: EventBuffer) -> None:
    """Re-arm the (snapshot-drained) buffer with a cue so the next turn speaks."""
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)


class _CountingFailSynth:
    """A synth that always raises, counting how many times it was actually called."""

    def __init__(self) -> None:
        self.calls = 0

    def __call__(self, text: str, **_kw) -> bytes:
        self.calls += 1
        raise CliError(code=EXIT_ENV_ERROR, message="TTS unreachable", remediation="start it")


def _one_quote_stream(messages, **_kw):
    yield '"Hello there."'


# ---------------------------------------------------------------------------
# Strict default — unchanged fail-fast contract
# ---------------------------------------------------------------------------


def test_strict_default_propagates_tts_error():
    """With audio_optional unset, a synth failure still propagates out of run_turn."""
    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=_one_quote_stream,
        synthesize=_CountingFailSynth(),
        play_audio=lambda *a, **k: None,
    )
    with pytest.raises(CliError):
        engine.run_turn()


# ---------------------------------------------------------------------------
# audio_optional — absorb the failure, keep thinking, keep exporting
# ---------------------------------------------------------------------------


def test_audio_optional_absorbs_failure_and_turn_completes():
    """A synth failure under audio_optional does not raise; the turn reports spoken."""
    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=_one_quote_stream,
        synthesize=_CountingFailSynth(),
        play_audio=lambda *a, **k: None,
        audio_optional=True,
    )
    assert engine.run_turn() is True  # no raise


def test_audio_optional_still_exports_message_and_thinking():
    """Thoughts reach the export sink even when audio is dead — they are decoupled."""
    exported: list[object] = []
    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=_one_quote_stream,
        synthesize=_CountingFailSynth(),
        play_audio=lambda *a, **k: None,
        export=ExportHook(emit=exported.append, time_fn=lambda: 1.0),
        audio_optional=True,
    )
    engine.run_turn()

    assert any(isinstance(ev, MessageEvent) and ev.text == "Hello there." for ev in exported)
    assert any(isinstance(ev, ThinkingEvent) for ev in exported)


def test_audio_optional_still_fires_expressions_after_a_failed_clip():
    """An express marker after a failed speak clip still fires (the loop is not aborted)."""
    expressed: list[str] = []
    lock = threading.Lock()

    def _express(emoji: str) -> None:
        with lock:
            expressed.append(emoji)

    def _stream(messages, **_kw):
        yield '"first" *🎉*'  # a speak item (synth fails) followed by an express item

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=_stream,
        synthesize=_CountingFailSynth(),
        play_audio=lambda *a, **k: None,
        express=_express,
        audio_optional=True,
    )
    engine.run_turn()

    assert expressed == ["🎉"]


# ---------------------------------------------------------------------------
# Latch — stop hammering a hard-down TTS after a short streak
# ---------------------------------------------------------------------------


def test_audio_latches_off_after_threshold_consecutive_failures():
    """Once muted, no further synth is attempted — cognition runs at full speed."""
    synth = _CountingFailSynth()
    buf = _buf_with_cue()
    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=_one_quote_stream,
        synthesize=synth,
        play_audio=lambda *a, **k: None,
        audio_optional=True,
    )
    # DEFAULT_AUDIO_MUTE_THRESHOLD == 2:
    # Turn 1: synth tried + fails (streak 1). Turn 2: tried + fails (streak 2 → mute).
    # Turn 3+: muted, synth never called again.
    for _ in range(4):
        engine.run_turn()
        _refill(buf)

    assert synth.calls == 2  # capped at the mute threshold
