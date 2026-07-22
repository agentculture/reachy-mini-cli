"""Contiguous capture — issue #108's lesson, restated for the server-VAD path.

The runtime once could not hear a spoken sentence. The cause was not the
endpointer, the engagement gate, the STT service or a sample-rate error: it was
that :mod:`reachy.behavior.transcript_sense` built an utterance out of **only
the chunks that individually cleared an RMS speech threshold**. A chunk below
threshold was discarded, so what was POSTed to STT was::

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

The root cause was a **category error**: an energy predicate is a *locator* (it
says where to start looking), never a *content filter* (it may not say which
audio is worth keeping). The realtime arc (issue #115) deleted the machinery
that made the mistake possible — the ring, the threshold, the onset scan, the
silence-hold timer, the span floor — and moved endpointing to the server's
``server_vad``. That does NOT retire the lesson: the driver now decides which
audio reaches the session, and the same category error is one ``if`` away.

So these tests are re-scoped, not deleted. They no longer ask "is the SUBMITTED
CLIP a contiguous slice?" (there is no clip) but the property that outlived it:

* **Everything the microphone produced reaches the session, in order, exactly
  once** — the concatenation of every chunk handed to the client is the mic
  stream verbatim, at both measured room backgrounds, with the inter-word gaps
  that the spliced behaviour excised still in place.
* **An utterance boundary changes nothing about the stream** — two sentences
  endpointed mid-run leave no gap and no duplicate, because the driver no longer
  owns a buffer that a submit could destroy (the live journal's
  ``pre_roll=0.02s buffered=512`` on back-to-back utterances).
* **Self-mute is still the one deliberate exception**, and it withholds audio
  rather than filtering it: the robot's own voice never reaches the server's VAD.
* **Structurally, no energy predicate survives in the capture path** — the AST
  guard that used to pin "only self-mute clears the ring" now pins "nothing here
  decides what is speech", which is the same defect class one level up.

Deterministic and offline: the mic is a known synthetic stream and the session
client is a fake that records every buffer it was handed, so "contiguous" is
checked by locating the streamed audio *verbatim inside the source* rather than
by ear.
"""

from __future__ import annotations

import ast
import inspect
import logging
import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from reachy.behavior import transcript_sense
from reachy.behavior.sense import Sense
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning
from reachy.speech.realtime import Utterance

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
# A deterministic microphone + a recording session client                     #
# --------------------------------------------------------------------------- #


class _Stream:
    """A fake held media client replaying a KNOWN source array, chunk by chunk.

    The point of replaying a known array (rather than generating chunks on the
    fly) is that every assertion below can be phrased as "where does this audio
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


class _Session:
    """A fake session client keeping every buffer it was handed, in order."""

    def __init__(self) -> None:
        self.chunks: list[np.ndarray] = []
        self._ready: list[Utterance] = []
        self._lock = threading.Lock()

    def submit_audio(self, audio) -> bool:
        with self._lock:
            self.chunks.append(np.asarray(audio, dtype=np.float32).copy())
        return True

    def take_utterance(self):
        with self._lock:
            return self._ready.pop(0) if self._ready else None

    def set_sample_rate(self, rate: int) -> None:
        return None

    def emit(self, text: str, t: float) -> None:
        with self._lock:
            self._ready.append(Utterance(text=text, t=t))

    @property
    def streamed(self) -> np.ndarray:
        with self._lock:
            if not self.chunks:
                return np.zeros(0, dtype=np.float32)
            return np.concatenate(self.chunks)


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
    gaps carry the room background ONLY, so they sit below what any plausible
    capture threshold would be — which is precisely the audio the spliced
    behaviour excised.
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
    base = dict(min_words=3, engage_window_s=1.0)
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


def _stream_ticks(src: np.ndarray) -> int:
    return int(src.size // CHUNK) + 8  # every chunk, plus a few dry ticks


# --------------------------------------------------------------------------- #
# 1 — everything the microphone produced reaches the session                  #
# --------------------------------------------------------------------------- #


def test_the_microphone_stream_reaches_the_session_contiguous_and_whole() -> None:
    """The defect in one assertion, at the new boundary.

    The spliced buffer — the loud frames butt-joined — occurs NOWHERE in the
    stream the mic produced, because the inter-word gaps it excised are still
    there in the source. What the driver hands the session must therefore be the
    source itself: same samples, same order, nothing dropped for being quiet.
    """
    src, spans = _source(words=4, word_ticks=4, gap_ticks=2, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    session = _Session()
    driver = TranscriptSenseDriver(media=stream, realtime=session, tuning=_tuning())
    try:
        _drive(driver, _stream_ticks(src))
        streamed = session.streamed
        assert streamed.size == src.size
        assert np.array_equal(streamed, src)
        # ...which necessarily contains the whole speech span, gaps included.
        core = src[spans[0][0] : spans[-1][1]]
        assert len(_locate(streamed, core)) == 1
    finally:
        driver.close()


def test_every_quiet_frame_between_the_words_still_reaches_the_session() -> None:
    """Stated as a count: nothing is filtered out for being below a threshold.

    Under the splice what reached the transcriber was strictly SHORTER than the
    span it claimed to represent — 42 % of it at the live background, 12 % across
    a room — which is why long sentences shattered while short interjections
    survived. Here the quiet frames are counted explicitly.
    """
    src, spans = _source(words=5, word_ticks=3, gap_ticks=3, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    session = _Session()
    driver = TranscriptSenseDriver(media=stream, realtime=session, tuning=_tuning())
    try:
        _drive(driver, _stream_ticks(src))
        streamed = session.streamed
        span = spans[-1][1] - spans[0][0]
        loud_only = sum(end - begin for begin, end in spans)
        assert loud_only < span  # the fixture really does have quiet gaps inside
        assert streamed.size >= span
        for index in range(len(spans) - 1):
            gap = src[spans[index][1] : spans[index + 1][0]]
            assert _locate(streamed, gap), "a quiet inter-word frame was filtered out"
    finally:
        driver.close()


@pytest.mark.parametrize("background", [NIGHT_BACKGROUND, REPORTED_BACKGROUND])
def test_the_stream_is_byte_identical_at_both_measured_backgrounds(background: float) -> None:
    """The room's level cannot change what is captured any more.

    The old capture gate was ``max(0.02, 3.0 x background)`` — 0.060 at the
    measured night level and 0.102 at the reported one, both ABOVE the
    inter-word dips, which is exactly how the live failure happened. With
    endpointing upstream there is no threshold left to move: the two runs stream
    byte-identical audio, and the only thing the room changes is what the SERVER
    decides was speech.
    """
    src, _ = _source(words=4, word_ticks=4, gap_ticks=2, background=background)
    stream = _Stream(src)
    session = _Session()
    driver = TranscriptSenseDriver(media=stream, realtime=session, tuning=_tuning())
    try:
        _drive(driver, _stream_ticks(src))
        assert np.array_equal(session.streamed, src)
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# 2 — an utterance boundary changes nothing about the stream                  #
# --------------------------------------------------------------------------- #


def test_back_to_back_utterances_leave_no_gap_and_no_duplicate() -> None:
    """Two sentences endpointed mid-run cost the stream nothing.

    The live journal showed ``pre_roll=0.02s buffered=512`` on consecutive
    utterances — the runtime destroying its own pre-roll on every emit. The
    driver no longer holds a buffer that an emit could destroy, so the property
    is now structural: whatever the server endpoints, the audio it is fed stays
    the microphone's own stream, once.
    """
    src, spans = _source(
        words=2,
        word_ticks=4,
        gap_ticks=10,
        background=NIGHT_BACKGROUND,
    )
    stream = _Stream(src)
    session = _Session()
    driver = TranscriptSenseDriver(media=stream, realtime=session, tuning=_tuning())
    try:
        # The server endpoints the first sentence part-way through the run and
        # the second at the end — the ticks where the old code cleared its ring.
        first_emit = spans[0][1] // CHUNK + 2
        t = T0
        for index in range(_stream_ticks(src)):
            if index == first_emit:
                session.emit(NAMED, t=t)
            if index == _stream_ticks(src) - 2:
                session.emit(NAMED, t=t)
            driver(_ctx(t))
            t += DT
        assert _await(lambda: driver.judged == 2)
        assert np.array_equal(session.streamed, src)
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# 3 — self-mute withholds audio; it does not filter it                        #
# --------------------------------------------------------------------------- #


def test_self_muted_audio_never_reaches_the_session() -> None:
    """The robot's own voice must never reach the server's VAD.

    This is the ONE deliberate exception to "everything reaches the session",
    and it is a withholding, not a filter: a contiguous prefix is missing (the
    mute window) and everything after it is the mic stream verbatim.
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
    session = _Session()
    driver = TranscriptSenseDriver(
        media=stream,
        realtime=session,
        # ...and the mute window covers the whole of the first "word".
        mute_until=lambda: T0 + mute_ticks * DT,
        tuning=_tuning(),
    )
    try:
        _drive(driver, _stream_ticks(src))
        streamed = session.streamed
        offsets = _locate(src, streamed)
        assert len(offsets) == 1
        # Not one sample from inside the mute window was handed over...
        assert offsets[0] >= mute_ticks * CHUNK
        assert offsets[0] >= spans[0][1]
        # ...and everything after it was, unbroken to the end of the stream.
        assert offsets[0] + streamed.size == src.size
    finally:
        driver.close()


def test_no_energy_predicate_survives_in_the_capture_path() -> None:
    """Structural guard, re-scoped from "only self-mute clears the ring".

    The functional tests above prove no audio is filtered *today*; this one pins
    the mechanism, because the tempting fix for any future "we are streaming
    silence" bug is to re-add a threshold here — which is precisely the category
    error that produced #108. Endpointing lives on the server; this module holds
    no notion of what counts as speech.
    """
    source = inspect.getsource(transcript_sense)
    tree = ast.parse(source)

    banned = {
        "_is_speech",
        "_speech_threshold",
        "_measure_onset",
        "_push_ring",
        "_concat_ring",
        "_discard_ring",
        "_clip_from",
        "speech_rms",
        "speech_ratio",
        "silence_hold_s",
        "min_utterance_s",
        "pre_roll_s",
        "ring_seconds",
    }
    seen = {
        node.attr if isinstance(node, ast.Attribute) else node.id
        for node in ast.walk(tree)
        if isinstance(node, (ast.Attribute, ast.Name))
    } | {node.name for node in ast.walk(tree) if isinstance(node, (ast.FunctionDef, ast.ClassDef))}
    assert not (seen & banned), (
        "an energy/endpointing predicate reappeared in the capture path: "
        f"{sorted(seen & banned)}. The server's VAD decides where an utterance "
        "starts and stops (issue #115); a local threshold here is issue #108 "
        "returning as a 'small optimisation'."
    )
    # And it imports no loudness helper it could build one out of. (The prose
    # above still NAMES the retired knobs, on purpose — the AST is what is
    # checked, so the module can keep explaining what it no longer does.)
    imported = {
        node.module for node in ast.walk(tree) if isinstance(node, ast.ImportFrom) and node.module
    } | {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, ast.Import)
        for alias in node.names
    }
    assert not [name for name in imported if "rms" in name], sorted(imported)


# --------------------------------------------------------------------------- #
# 4 — the journal still names what was heard                                  #
# --------------------------------------------------------------------------- #


def test_the_journal_names_every_arrived_utterance(caplog) -> None:
    """An operator confirms hearing on a real robot by reading the journal.

    The old ``utterance end span=... clip=... contiguous`` line was the client's
    own report of what it had cut; the server cuts now (and says so on the
    ``realtime`` stage), so this module reports what ARRIVED. One line per
    utterance, at the capture stage, carrying its size.
    """
    src, _ = _source(words=4, word_ticks=4, gap_ticks=2, background=NIGHT_BACKGROUND)
    stream = _Stream(src)
    session = _Session()
    driver = TranscriptSenseDriver(media=stream, realtime=session, tuning=_tuning())
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            session.emit(NAMED, t=T0)
            _drive(driver, 4)
            # ``judged`` is the barrier, not ``transcripts``: the counter is
            # bumped inside the handler, the journal line after it.
            assert _await(lambda: driver.judged == 1)
            assert driver.transcripts == 1
        arrived = [m for m in caplog.messages if "utterance chars=" in m]
        assert len(arrived) == 1
        assert f"chars={len(NAMED)}" in arrived[0]
        assert "stage=capture" in arrived[0]
        heard = [m for m in caplog.messages if "heard " in m]
        assert len(heard) == 1
        assert "stage=transcript" in heard[0]
    finally:
        driver.close()
