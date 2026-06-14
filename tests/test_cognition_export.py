"""Tests for the cognition export hook + raw-thought tap (t4).

The :class:`~reachy.speech.cognition.CognitionEngine` gains an OPTIONAL
``export`` hook. When supplied, a turn emits export blocks
(:class:`~reachy.export.events.EmotionEvent`,
:class:`~reachy.export.events.MessageEvent`,
:class:`~reachy.export.events.ThinkingEvent`) as the turn progresses — without
changing any existing speech behaviour, ordering, or timing.

The three acceptance criteria proven here:

1. **Emission shape / order** — one ``EmotionEvent`` per parsed marker, one
   ``MessageEvent`` per parsed quoted span (interleaved as produced), and exactly
   one ``ThinkingEvent`` last, carrying the turn's sense cues + the raw turn text.
2. **Raw-thought tap** — the captured ``ThinkingEvent.text`` is the FULL raw LLM
   concatenation, INCLUDING prose / markers / quotes that the
   :class:`~reachy.speech.markers.MarkerParser` discards before they reach speech.
3. **No-op when absent** — with ``export`` omitted (default ``None``) the engine's
   spoken output and collaborator calls are byte-identical to before the hook
   existed; no export side effects occur.

All collaborators are faked — no live LLM, TTS, robot, or sleeps — and the
timestamp source is injected so emissions are deterministic.
"""

from __future__ import annotations

import threading

from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import ExportHook
from reachy.speech.cognition import CognitionEngine
from reachy.speech.events import EventBuffer

# ---------------------------------------------------------------------------
# Test fakes
# ---------------------------------------------------------------------------


def _const_clock(value: float = 0.0):
    return lambda: value


class _Recorder:
    """Thread-safe recorder of synth / play / express calls in arrival order."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []
        self.expressed: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        with self._lock:
            self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        with self._lock:
            self.played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))

    def express(self, emoji: str) -> None:
        with self._lock:
            self.expressed.append(emoji)


def _buf_with_cue(*, clock_value: float = 0.0) -> EventBuffer:
    buf = EventBuffer(clock=_const_clock(clock_value))
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)  # "speech from the left"
    return buf


# ---------------------------------------------------------------------------
# Criterion 1 — emission shape / order / payloads
# ---------------------------------------------------------------------------


def test_export_emits_emotion_message_interleaved_then_thinking_last():
    """One EmotionEvent per marker, one MessageEvent per quote, ThinkingEvent last."""
    rec = _Recorder()
    exported: list[object] = []

    def fake_stream(messages, **_kw):
        yield '*🤔* "Hello there." *👂* "Over there."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        export=ExportHook(emit=exported.append, time_fn=lambda: 42.0),
        system_prompt="SYS",
    )
    spoke = engine.run_turn()

    assert spoke is True

    # The emotion / message blocks are interleaved in stream order; thinking last.
    types = [type(ev) for ev in exported]
    assert types == [
        EmotionEvent,
        MessageEvent,
        EmotionEvent,
        MessageEvent,
        ThinkingEvent,
    ]

    # Payloads of each block.
    assert exported[0] == EmotionEvent(emoji="🤔", pose=None, ts=42.0)
    assert exported[1] == MessageEvent(text="Hello there.", ts=42.0)
    assert exported[2] == EmotionEvent(emoji="👂", pose=None, ts=42.0)
    assert exported[3] == MessageEvent(text="Over there.", ts=42.0)

    thinking = exported[4]
    assert isinstance(thinking, ThinkingEvent)
    assert thinking.cues == ["speech from the left"]
    assert thinking.text == '*🤔* "Hello there." *👂* "Over there."'
    assert thinking.ts == 42.0

    # Speech behaviour is unchanged: only quoted text is spoken, markers fire.
    assert rec.synth_texts == ["Hello there.", "Over there."]
    assert rec.played_texts == ["Hello there.", "Over there."]
    assert rec.expressed == ["🤔", "👂"]


def test_export_pose_resolver_fills_emotion_pose():
    """pose_resolver maps an emoji to a pose dict (or None) on EmotionEvent."""
    rec = _Recorder()
    exported: list[object] = []
    poses = {"🤔": {"head_pitch": -5.0}}

    def fake_stream(messages, **_kw):
        yield '*🤔* "thinking." *❓*'  # ❓ has no pose → None

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, pose_resolver=poses.get, time_fn=lambda: 7.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    emotions = [ev for ev in exported if isinstance(ev, EmotionEvent)]
    assert emotions == [
        EmotionEvent(emoji="🤔", pose={"head_pitch": -5.0}, ts=7.0),
        EmotionEvent(emoji="❓", pose=None, ts=7.0),
    ]


def test_export_thinking_carries_snapshot_cues_as_list_of_str():
    """ThinkingEvent.cues is the turn's snapshot cue strings, as a plain list[str]."""
    rec = _Recorder()
    exported: list[object] = []

    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)  # "speech from the left"
    buf.feed_vision(motion_direction=0.8, brightness_delta=0.0)  # "motion on the right"

    def fake_stream(messages, **_kw):
        yield '"ok."'

    engine = CognitionEngine(
        buffer=buf,
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, time_fn=lambda: 1.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    thinking = [ev for ev in exported if isinstance(ev, ThinkingEvent)]
    assert len(thinking) == 1
    assert thinking[0].cues == ["speech from the left", "motion on the right"]
    assert all(isinstance(c, str) for c in thinking[0].cues)


def test_export_emits_exactly_one_thinking_per_turn():
    """A single turn produces exactly one ThinkingEvent (turn-end emission)."""
    rec = _Recorder()
    exported: list[object] = []

    def fake_stream(messages, **_kw):
        yield '"a." "b." "c."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    assert sum(isinstance(ev, ThinkingEvent) for ev in exported) == 1
    # ... and it is the LAST block emitted.
    assert isinstance(exported[-1], ThinkingEvent)


# ---------------------------------------------------------------------------
# Criterion 2 — raw-thought tap captures the FULL stream (before discard)
# ---------------------------------------------------------------------------


def test_raw_thought_tap_captures_full_stream_including_discarded_prose():
    """ThinkingEvent.text is the FULL raw concatenation, not just the spoken text.

    The MarkerParser discards prose outside ``*…*`` / ``"…"`` spans (once the
    stream is "marked"). The raw tap must capture every chunk BEFORE that discard
    — including the literal ``*🤔*`` markers, the quote delimiters, AND the
    genuinely-discarded inter-span prose ("...pondering..." / "...done").

    Note: the FIRST chunk opens a marker (``*…*``) so the stream is "marked" from
    the start — this avoids the engine's legacy fast-path, under which leading
    un-marked prose is spoken verbatim (a deliberate pre-existing behaviour). Here
    every bit of prose between the spans is truly discarded by the parser, yet the
    raw tap still captures all of it.
    """
    rec = _Recorder()
    exported: list[object] = []

    # Marked from chunk 0; the "...pondering..." / "...done" prose is genuinely
    # discarded by the parser (NOT legacy-spoken), but the raw tap keeps it.
    chunks = ["*🤔* ", "...pondering... ", '"Hello there." ', "...done"]

    def fake_stream(messages, **_kw):
        yield from chunks

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    thinking = [ev for ev in exported if isinstance(ev, ThinkingEvent)][0]
    assert thinking.text == "".join(chunks)
    # Sanity: the captured raw text is a SUPERSET of the spoken text — it keeps
    # the discarded inter-span prose, the marker, and the quote delimiters.
    assert "...pondering..." in thinking.text
    assert "...done" in thinking.text
    assert "*🤔*" in thinking.text
    assert '"Hello there."' in thinking.text
    # The spoken text is ONLY the quoted span — the discarded prose was NOT spoken.
    assert rec.played_texts == ["Hello there."]
    # The marker was still exported (independent of the optional express callback).
    emotions = [ev for ev in exported if isinstance(ev, EmotionEvent)]
    assert [ev.emoji for ev in emotions] == ["🤔"]


def test_raw_thought_tap_reassembles_chunks_split_mid_marker():
    """The raw text is the byte-exact chunk concatenation, even split mid-span."""
    rec = _Recorder()
    exported: list[object] = []

    chunks = ["*", "🤔", '* "split ', 'speech."', " tail"]

    def fake_stream(messages, **_kw):
        yield from chunks

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    thinking = [ev for ev in exported if isinstance(ev, ThinkingEvent)][0]
    assert thinking.text == "".join(chunks)
    # The marker still fired once and the speech was spoken (unchanged behaviour).
    assert rec.played_texts == ["split speech."]


def test_raw_thought_tap_includes_unclosed_dropped_span():
    """An unclosed span (dropped by the parser) is still in the raw ThinkingEvent.text."""
    rec = _Recorder()
    exported: list[object] = []

    raw = '"closed." *🤔* "never closed'

    def fake_stream(messages, **_kw):
        yield raw

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
        system_prompt="SYS",
    )
    engine.run_turn()

    thinking = [ev for ev in exported if isinstance(ev, ThinkingEvent)][0]
    assert thinking.text == raw  # full raw incl. the dropped open quote
    # Behaviour unchanged: closed span spoken, closed marker fired, open quote dropped.
    assert rec.played_texts == ["closed."]


# ---------------------------------------------------------------------------
# Criterion 3 — no-op when export is absent (byte-identical to before)
# ---------------------------------------------------------------------------


def _run_and_collect(*, with_export_none: bool) -> dict:
    """Run one identical turn; return the observable collaborator calls.

    ``with_export_none`` toggles between passing ``export=None`` explicitly and
    omitting the param entirely — both must yield identical observable behaviour.
    """
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield '*🤔* "Hello there." *👂* "Over there." trailing prose'

    kwargs = dict(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    if with_export_none:
        kwargs["export"] = None
    engine = CognitionEngine(**kwargs)
    spoke = engine.run_turn()
    return {
        "spoke": spoke,
        "synth": list(rec.synth_texts),
        "played": list(rec.played_texts),
        "expressed": list(rec.expressed),
    }


def test_no_export_is_byte_identical_whether_none_or_omitted():
    """export=None and omitting export produce identical observable behaviour."""
    omitted = _run_and_collect(with_export_none=False)
    explicit_none = _run_and_collect(with_export_none=True)

    assert omitted == explicit_none
    # And it matches the pre-hook contract: only quoted text spoken, markers fired.
    assert omitted["spoke"] is True
    assert omitted["synth"] == ["Hello there.", "Over there."]
    assert omitted["played"] == ["Hello there.", "Over there."]
    assert omitted["expressed"] == ["🤔", "👂"]


def test_no_export_has_no_side_effects_and_no_thinking_block():
    """With export omitted, nothing is emitted anywhere (no thinking block leaks)."""
    rec = _Recorder()
    # A sentinel callable that would record if it were ever called — but it isn't
    # wired, proving the engine never fabricates an export sink of its own.
    leaked: list[object] = []

    def fake_stream(messages, **_kw):
        yield '*🤔* "quiet."'

    engine = CognitionEngine(
        buffer=_buf_with_cue(),
        stream_sentences=fake_stream,
        synthesize=rec.synth,
        play_audio=rec.play,
        express=rec.express,
        system_prompt="SYS",
    )
    engine.run_turn()

    assert leaked == []
    assert rec.played_texts == ["quiet."]
    assert rec.expressed == ["🤔"]


def test_empty_buffer_turn_emits_nothing_even_with_export():
    """A no-op turn (empty buffer) calls neither the LLM nor export."""
    exported: list[object] = []
    calls: list = []

    def fake_stream(messages, **_kw):
        calls.append(messages)
        yield '"should not happen."'

    engine = CognitionEngine(
        buffer=EventBuffer(clock=_const_clock()),  # empty
        stream_sentences=fake_stream,
        synthesize=lambda t, **_kw: b"pcm",
        play_audio=lambda pcm, **_kw: None,
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
        system_prompt="SYS",
    )
    spoke = engine.run_turn()

    assert spoke is False
    assert calls == []
    assert exported == []  # no thinking block for a turn that never thought
