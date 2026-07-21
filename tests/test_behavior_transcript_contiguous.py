"""Contiguous capture — the root cause of issue #108 (task t38).

The runtime could not hear a spoken sentence. The cause was not the endpointer,
the engagement gate, the STT service or a sample-rate error: it was that
:mod:`reachy.behavior.transcript_sense` built an utterance out of **only the
chunks that individually cleared the RMS speech threshold**. A chunk below
threshold fell through to ``_maybe_submit_on_pause`` and was discarded — it
entered the pre-roll ring, but the ring was never consulted again until the next
rising edge. So what was POSTed to STT was::

    [<=2 s contiguous pre-roll] + [the loud frames butt-spliced, quiet frames excised]

Every unvoiced consonant, stop closure and inter-word gap *inside* the sentence
was cut out and the survivors glued edge to edge. Reproduced live against the
real Parakeet at ``localhost:9002`` with one synthesized phrase mixed to
realistic mic levels:

===========================================  =========  ======  =========================
scenario                                     threshold  kept    transcript
===========================================  =========  ======  =========================
contiguous (``reachy_nova``'s shape)         --         100 %   ``'Richie, are you there?'``
gated, background 0.020 (the live journal)   0.060      42 %    ``'Reaching there.'``
gated, background 0.034                      0.102      27 %    ``'Return.'``
gated, across the room, background 0.034     0.102      12 %    ``'Yeah.'``
===========================================  =========  ======  =========================

``reachy_nova``'s equivalent (``speech_events.py:263-265``) emits
``full_buffer[clip_offset:]`` — ONE contiguous slice — and uses its 0.02 RMS
threshold ONLY in ``_measure_onset`` (``:237-254``) to locate where to start
backtracking. Our 0.02 was cited from that constant but repurposed from a
**locator** into a **content filter**. That inversion was the bug.

These tests pin the fix, from failing-first, without a live STT anywhere: the
mic is a deterministic synthetic stream and the transcriber records the exact
buffer handed to it, so "contiguous" is checked by locating the submitted clip
*verbatim inside the source stream* rather than by ear.

* **The clip is a contiguous slice** (criterion 1) — it occurs verbatim in the
  source, exactly once, and it contains every sample between the first and the
  last speech chunk. Under the spliced behaviour it occurs nowhere.
* **The live scenario, reconstructed** (criterion 2) — speech-like audio with
  real inter-word dips *below* the resolved threshold, at the two measured
  background levels (0.020 and 0.034, i.e. thresholds 0.060 and 0.102 through
  the shipped ``speech_ratio`` of 3.0), is submitted byte-identical to its
  contiguous source span.
* **The ring survives a submit** (criterion 3) — two back-to-back utterances
  leave NO gap in the source: the second clip begins exactly where the first
  ended, so no audio is destroyed and none is transcribed twice.
* **``min_utterance_s`` is a wall-clock span** (criterion 4) — a real sentence
  whose *loud sample count* is below the floor but whose *span* clears it is
  captured, while a genuine blip is still dropped.
* **Self-mute still discards** (criterion 5) — and remains the only thing that
  clears the ring.
"""

from __future__ import annotations

import ast
import inspect
import logging
import re
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from reachy.behavior import transcript_sense
from reachy.behavior.sense import Sense
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning

RATE = 16000
CHUNK = 160  # 160 samples = 10 ms at 16 kHz — one tick's mic chunk
DT = 0.01
T0 = 100.0

#: An utterance that trips the engagement gate's NAME fast-path.
NAMED = "reachy can you look at me"

#: The two measured room backgrounds from the live journal (issue #108/#102).
NIGHT_BACKGROUND = 0.020
REPORTED_BACKGROUND = 0.034


# --------------------------------------------------------------------------- #
# A deterministic microphone: one known stream, handed out one tick at a time  #
# --------------------------------------------------------------------------- #


class _Stream:
    """A fake held media client replaying a KNOWN source array, chunk by chunk.

    The point of replaying a known array (rather than generating chunks on the
    fly) is that every assertion below can be phrased as "where does this clip
    occur in the source?" — which is exactly the question the defect answers
    with "nowhere".
    """

    def __init__(self, src: np.ndarray, *, chunk: int = CHUNK) -> None:
        self.src = np.asarray(src, dtype=np.float32)
        self.chunk = chunk
        self.samplerate = RATE
        self.channels = 1
        self.pos = 0

    def audio(self) -> np.ndarray | None:
        if self.pos >= self.src.size:
            return None
        out = self.src[self.pos : self.pos + self.chunk]
        self.pos += int(out.size)
        return out


class _Recorder:
    """A fake ``Transcriber`` keeping a copy of every buffer it was handed."""

    def __init__(self, text: str = NAMED) -> None:
        self.text = text
        self.clips: list[np.ndarray] = []
        self.lock = threading.Lock()

    def transcribe_once(self, audio) -> str:
        with self.lock:
            self.clips.append(np.asarray(audio, dtype=np.float32).copy())
        return self.text

    def clip(self, index: int) -> np.ndarray:
        with self.lock:
            return self.clips[index]

    @property
    def count(self) -> int:
        with self.lock:
            return len(self.clips)


# --------------------------------------------------------------------------- #
# Source construction + location helpers                                      #
# --------------------------------------------------------------------------- #


def _source(
    *,
    words: int,
    word_ticks: int,
    gap_ticks: int,
    background: float,
    level: float = 0.25,
    lead_ticks: int = 8,
    tail_ticks: int = 8,
    seed: int = 0,
) -> tuple[np.ndarray, list[tuple[int, int]]]:
    """A speech-like mic stream: quiet lead-in, words, sub-threshold gaps, tail.

    Returns the stream and the ``(start, end)`` sample span of each word. The
    gaps carry the room background ONLY, so they sit below the resolved capture
    threshold — which is precisely the audio the spliced behaviour excised.
    """
    rng = np.random.RandomState(seed)
    parts: list[np.ndarray] = []
    spans: list[tuple[int, int]] = []
    total = 0

    def quiet(ticks: int) -> None:
        nonlocal total
        block = (rng.randn(ticks * CHUNK) * background).astype(np.float32)
        parts.append(block)
        total += int(block.size)

    def word(ticks: int) -> None:
        nonlocal total
        n = ticks * CHUNK
        t = np.arange(total, total + n, dtype=np.float64) / RATE
        block = (level * np.sin(2 * np.pi * 180.0 * t) + rng.randn(n) * background).astype(
            np.float32
        )
        spans.append((total, total + n))
        parts.append(block)
        total += n

    quiet(lead_ticks)
    for index in range(words):
        word(word_ticks)
        if index < words - 1:
            quiet(gap_ticks)
    quiet(tail_ticks)
    return np.concatenate(parts), spans


def _locate(haystack: np.ndarray, needle: np.ndarray) -> list[int]:
    """Every offset at which *needle* occurs verbatim and contiguously in *haystack*."""
    n = int(needle.size)
    if n == 0 or n > haystack.size:
        return []
    return [
        i for i in range(int(haystack.size) - n + 1) if np.array_equal(haystack[i : i + n], needle)
    ]


def _tuning(**kw) -> TranscriptTuning:
    base = dict(
        speech_rms=0.02,
        speech_ratio=3.0,
        silence_hold_s=0.05,
        max_utterance_s=1.0,
        min_utterance_s=0.02,
        ring_seconds=2.0,
        pre_roll_s=0.05,
        min_words=3,
        engage_window_s=1.0,
    )
    base.update(kw)
    return TranscriptTuning(**base)


def _ctx(now: float):
    return SimpleNamespace(now=now, tick=int(now * 100), sense=Sense(doa_angle=None))


def _drive(driver, ticks: int, t0: float = T0) -> float:
    t = t0
    for _ in range(ticks):
        driver(_ctx(t))
        t += DT
    return t


def _await(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _speech_ticks(src: np.ndarray) -> int:
    return int(src.size // CHUNK) + 8  # every chunk, plus room to endpoint


# --------------------------------------------------------------------------- #
# Criterion 1 — the submitted audio is a contiguous slice                     #
# --------------------------------------------------------------------------- #


def test_the_submitted_clip_is_one_contiguous_slice_of_the_microphone_stream() -> None:
    """No frame inside the utterance span is dropped for being quiet.

    This is the defect in one assertion. The spliced buffer — pre-roll plus the
    loud frames butt-joined — occurs NOWHERE in the stream the mic produced,
    because the inter-word gaps it excised are still there in the source.
    """
    src, spans = _source(words=4, word_ticks=4, gap_ticks=2, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 1)
        clip = recorder.clip(0)

        offsets = _locate(src, clip)
        assert offsets, "the submitted clip is not a contiguous slice of the mic stream"
        assert len(offsets) == 1
        start = offsets[0]

        # It carries the WHOLE speech span, inner silence included...
        core = src[spans[0][0] : spans[-1][1]]
        assert _locate(clip, core), "audio between the words was excised from the clip"
        # ...plus measured pre-roll ahead of the first word, and no truncation.
        assert start < spans[0][0]
        assert start + clip.size >= spans[-1][1]
    finally:
        driver.close()


def test_every_quiet_frame_inside_the_utterance_reaches_the_transcriber() -> None:
    """Stated as a count: the clip is at least as long as the span it covers.

    Under the splice the clip was strictly SHORTER than the span it claimed to
    represent — 42 % of it at the live background, 12 % across a room — which is
    why long sentences shattered while short interjections survived.
    """
    src, spans = _source(words=5, word_ticks=3, gap_ticks=3, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 1)
        clip = recorder.clip(0)
        span = spans[-1][1] - spans[0][0]
        loud_only = sum(end - begin for begin, end in spans)
        assert loud_only < span  # the fixture really does have quiet gaps inside
        assert clip.size >= span
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 2 — the live scenario, reconstructed at both measured backgrounds #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize("background", [NIGHT_BACKGROUND, REPORTED_BACKGROUND])
def test_the_live_scenario_submits_a_byte_identical_contiguous_span(background: float) -> None:
    """The reproduced failure, without a live STT: the two measured room levels.

    ``speech_ratio`` of 3.0 puts the capture threshold at 0.060 and 0.102
    respectively — above the inter-word dips, exactly as on the box. The
    submitted buffer must nevertheless be byte-identical to a contiguous span of
    what the microphone produced.
    """
    src, spans = _source(words=4, word_ticks=4, gap_ticks=2, background=background)
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: background,
        tuning=_tuning(),
    )
    try:
        # The fixture is only meaningful if the gates really do close mid-sentence.
        threshold = driver._speech_threshold()
        assert threshold == pytest.approx(max(0.02, 3.0 * background))
        gap = src[spans[0][1] : spans[1][0]]
        assert float(np.sqrt(np.mean(np.square(gap)))) < threshold

        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 1)
        clip = recorder.clip(0)

        offsets = _locate(src, clip)
        assert len(offsets) == 1
        start = offsets[0]
        assert np.array_equal(clip, src[start : start + clip.size])
        assert start + clip.size >= spans[-1][1]
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 3 — the ring survives a submit                                    #
# --------------------------------------------------------------------------- #


def test_back_to_back_utterances_leave_no_gap_in_the_captured_stream() -> None:
    """The ring is not destroyed on emit, so nothing between two sentences is lost.

    The live journal showed ``pre_roll=0.02s buffered=512`` on consecutive
    utterances — the runtime destroying its own pre-roll. With the ring retained
    and only the ALREADY-SUBMITTED audio marked consumed, the second clip starts
    exactly where the first ended: no gap, and no sample transcribed twice.
    """
    src, _ = _source(
        words=2,
        word_ticks=4,
        gap_ticks=10,  # longer than silence_hold_s: utterance 1 endpoints here
        background=NIGHT_BACKGROUND,
    )
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 2)
        first, second = recorder.clip(0), recorder.clip(1)

        starts_first = _locate(src, first)
        starts_second = _locate(src, second)
        assert len(starts_first) == 1 and len(starts_second) == 1
        assert starts_second[0] == starts_first[0] + first.size
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 4 — min_utterance_s is a wall-clock span                          #
# --------------------------------------------------------------------------- #


def test_min_utterance_is_measured_as_a_span_not_as_a_count_of_loud_samples() -> None:
    """Otherwise the same defect reappears as a length test.

    This sentence spends more time between its words than inside them: its loud
    sample count is below the floor while its wall-clock span clears it. Judged
    on loud samples it is discarded as a blip; judged on span — what a listener
    would call its length — it is a sentence.
    """
    src, spans = _source(
        words=4,
        word_ticks=1,
        gap_ticks=1,
        background=NIGHT_BACKGROUND,
        lead_ticks=6,
    )
    loud = sum(end - begin for begin, end in spans)
    span = spans[-1][1] - spans[0][0]
    floor = int(0.05 * RATE)
    assert loud < floor < span  # the fixture straddles the floor, on purpose

    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(min_utterance_s=0.05),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 1)
    finally:
        driver.close()


def test_a_genuine_blip_shorter_than_the_span_floor_is_still_dropped() -> None:
    """The floor still does its job: one 10 ms tick of sound is not a sentence."""
    src, _ = _source(
        words=1,
        word_ticks=1,
        gap_ticks=0,
        background=NIGHT_BACKGROUND,
        lead_ticks=6,
        tail_ticks=10,
    )
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(min_utterance_s=0.05),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert not _await(lambda: recorder.count > 0, timeout=0.5)
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 5 — self-mute still discards, and is the only thing clearing the ring
# --------------------------------------------------------------------------- #


def test_self_muted_audio_never_reaches_the_transcriber_nor_the_next_pre_roll() -> None:
    """The robot's own voice is discarded AND cannot bleed into the next clip.

    Self-mute is the one path that still wipes the ring — otherwise the retained
    buffer would pre-roll the robot's own speech into the next thing it hears.
    """
    mute_ticks = 10
    src, spans = _source(
        words=2,
        word_ticks=4,
        gap_ticks=10,
        background=NIGHT_BACKGROUND,
        lead_ticks=0,  # the robot's own voice starts immediately...
        seed=3,
    )
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        # ...and the mute window covers the whole of the first "word".
        mute_until=lambda: T0 + mute_ticks * DT,
        tuning=_tuning(),
    )
    try:
        _drive(driver, _speech_ticks(src))
        assert _await(lambda: recorder.count == 1)
        clip = recorder.clip(0)
        offsets = _locate(src, clip)
        assert len(offsets) == 1
        # Not one sample from inside the mute window is in the emitted clip.
        assert offsets[0] >= mute_ticks * CHUNK
        assert offsets[0] >= spans[0][1]
    finally:
        driver.close()


def test_discarding_the_ring_is_reachable_from_exactly_one_place() -> None:
    """Structural guard: self-mute is the ONLY caller that wipes the buffer.

    The functional tests above prove no gap appears between two utterances
    *today*; this one pins the mechanism, because the tempting fix for any
    future "stale audio" bug is to clear the ring somewhere else — which is
    precisely how the pre-roll was being destroyed before #108.
    """
    source = inspect.getsource(transcript_sense)
    tree = ast.parse(source)

    def _targets(node):
        if isinstance(node, ast.Assign):
            return node.targets
        if isinstance(node, ast.AnnAssign):
            return [node.target]
        return []

    clears = [
        node
        for node in ast.walk(tree)
        if any(
            isinstance(target, ast.Attribute) and target.attr == "_ring"
            for target in _targets(node)
        )
    ]
    # One in __init__ (the annotated declaration), one in _discard_ring. No more.
    assert len(clears) == 2

    callers = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "_discard_ring"
    ]
    assert len(callers) == 1


def test_the_utterance_end_line_reports_the_span_and_the_clip(caplog) -> None:
    """The journal must show a clip at least as long as the span it covers.

    That line is how an operator confirms the fix on a real robot without
    reading transcripts: a ``clip`` shorter than its ``span`` means audio is
    being dropped inside the utterance again.
    """
    src, _ = _source(words=4, word_ticks=4, gap_ticks=2, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    recorder = _Recorder()
    driver = TranscriptSenseDriver(
        media=stream,
        transcriber=recorder,
        background=lambda: NIGHT_BACKGROUND,
        tuning=_tuning(),
    )
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            _drive(driver, _speech_ticks(src))
            assert _await(lambda: recorder.count == 1)
        ends = [m for m in caplog.messages if "utterance end" in m]
        assert len(ends) == 1
        span = float(re.search(r"span=([\d.]+)s", ends[0]).group(1))
        clip = float(re.search(r"clip=([\d.]+)s", ends[0]).group(1))
        assert clip >= span
        assert "contiguous" in ends[0]
    finally:
        driver.close()
