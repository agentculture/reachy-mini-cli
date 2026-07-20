"""Tests for the runtime transcript sense (task t11).

:mod:`reachy.behavior.transcript_sense` ports ``listen``'s hearing-words
capability (``reachy/motion/listen_transcribe.py``) into the symbolic behavior
runtime. These tests pin, from failing-first, the three acceptance criteria of
the task plus the discipline the module inherits from its two precedents:

* **The tick reads a LATCHED value, never a blocking STT call.** The network
  round-trip (and the engagement classifier's, which is also a network call)
  happens on a background worker; the tick only peeks a latch. Proved two ways:
  a deliberately wedged transcriber cannot slow the tick down, and the fake
  records the thread it was called on — never the tick thread.
* **An unreachable STT leaves the field ``None`` and drops no ticks.** Driven in
  the ``offline`` lane against a REAL :class:`reachy.speech.stt.Transcriber`
  with sockets blocked, so the degradation is the shipped code path, not a fake.
* **The #54/#56 engagement gate still filters addressed-vs-ambient speech.** The
  gate is reused, not reimplemented: the name fast-path engages with ZERO
  classifier calls, an ambient utterance the classifier rejects never latches,
  and a raising classifier degrades to the word-count/conversation-window
  heuristic rather than stalling.
* The one-tick latch cadence of :class:`reachy.behavior.pat_sense.PatSenseDriver`
  (cleared before every tick, peeked non-destructively within a tick).

Deterministic apart from the deliberate thread handoff, which every test waits
on with a bounded poll. No robot, SDK, or daemon anywhere: the media client is a
fake handing out scripted mic chunks.
"""

from __future__ import annotations

import threading
import time
from types import SimpleNamespace

import numpy as np
import pytest

from reachy.behavior.sense import Sense, SenseProviders, read_perception
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning

RATE = 16000
CHUNK = 160  # 160 samples = 10 ms at 16 kHz — one tick's mic chunk
DT = 0.01
T0 = 100.0

#: An utterance that trips the engagement gate's NAME fast-path.
NAMED = "reachy can you look at me"
#: A coherent utterance that names nobody — ambient human-to-human speech.
AMBIENT = "did you see the game last night"


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


def _loud(n: int = CHUNK) -> np.ndarray:
    """A mic chunk comfortably above the energy VAD threshold."""
    return np.full(n, 0.5, dtype=np.float32)


def _quiet(n: int = CHUNK) -> np.ndarray:
    """A silent mic chunk."""
    return np.zeros(n, dtype=np.float32)


class _Media:
    """A fake ``HeldMediaClient``: hands out whatever ``next_chunk`` holds.

    Records the calling thread so a test can assert the mic is only ever read
    from the tick thread (the held client's documented thread contract).
    """

    def __init__(self, samplerate: int = RATE) -> None:
        self.samplerate = samplerate
        self.channels = 1
        self.next_chunk: np.ndarray | None = None
        self.calls = 0
        self.threads: set[int] = set()

    def audio(self):
        self.calls += 1
        self.threads.add(threading.get_ident())
        return self.next_chunk


class _Transcriber:
    """A fake :class:`~reachy.speech.stt.Transcriber` recording call thread + count."""

    def __init__(
        self,
        text: str | None = NAMED,
        *,
        gate: threading.Event | None = None,
        error: BaseException | None = None,
    ) -> None:
        self.text = text
        self.gate = gate
        self.error = error
        self.calls = 0
        self.threads: set[int] = set()

    def transcribe_once(self, audio):
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.gate is not None:
            self.gate.wait(10.0)
        if self.error is not None:
            raise self.error
        return self.text


class _Classifier:
    """A fake :class:`~reachy.speech.engagement.EngagementClassifier`."""

    def __init__(self, verdict: bool = True, *, error: BaseException | None = None) -> None:
        self.verdict = verdict
        self.error = error
        self.calls = 0

    def judge(self, text, context):
        self.calls += 1
        if self.error is not None:
            raise self.error
        return self.verdict


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _tuning(**kw) -> TranscriptTuning:
    """Fast tuning so an utterance endpoints within a handful of 10 ms ticks."""
    base = dict(
        silence_hold_s=0.03,
        max_utterance_s=1.0,
        min_utterance_s=0.02,
        ring_seconds=0.5,
        pre_roll_s=0.05,
        min_words=3,
        engage_window_s=1.0,
    )
    base.update(kw)
    return TranscriptTuning(**base)


def _ctx(now: float, doa: float | None = None):
    """A minimal ``TickContext``-shaped fake: only the fields the driver reads."""
    return SimpleNamespace(
        now=now,
        tick=int(now * 100),
        sense=Sense(doa_angle=doa),
    )


def _driver(media: _Media, **kw) -> TranscriptSenseDriver:
    kw.setdefault("tuning", _tuning())
    return TranscriptSenseDriver(media=media, **kw)


def _speak(driver, media: _Media, t: float, *, chunks: int = 6, doa: float | None = None) -> float:
    """Drive ``chunks`` loud ticks then enough quiet ticks to endpoint the utterance.

    Stops the moment the utterance reaches the worker (``submitted`` advances).
    That closes a genuine race rather than papering over one: the one-tick latch
    is written by whichever tick drains the ready queue, so ANY tick driven here
    after the handoff can adopt the transcript — and the tick after that clears
    it again. Callers wait for the worker and then drive one explicit tick to
    observe the latch, so if this helper kept ticking past the handoff, a worker
    that finished early (a scheduling accident, far likelier under parallel load)
    could consume the transcript inside the helper and leave the caller peeking
    at an already-cleared latch. With the shipped test tuning submission lands on
    the 3rd of 5 quiet ticks, leaving exactly two such ticks — the shape of the
    ~6%, load-only flake seen in
    ``test_the_direction_of_the_utterance_is_latched_alongside_the_words``.

    A sub-floor blip never submits, so those tests still get all five quiet ticks.
    """
    for _ in range(chunks):
        media.next_chunk = _loud()
        driver(_ctx(t, doa))
        t += DT
    submitted_before = driver.submitted
    for _ in range(5):
        media.next_chunk = _quiet()
        driver(_ctx(t, doa))
        t += DT
        if driver.submitted > submitted_before:
            break
    return t


def _idle(driver, media: _Media, t: float, ticks: int = 5) -> float:
    """Drive ``ticks`` silent ticks (nothing captured, latch cleared each time)."""
    for _ in range(ticks):
        media.next_chunk = _quiet()
        driver(_ctx(t))
        t += DT
    return t


def _await(predicate, timeout: float = 5.0) -> bool:
    """Poll *predicate* until true or *timeout* elapses (the worker handoff wait)."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


# --------------------------------------------------------------------------- #
# Criterion 1 — the tick reads a latched value, never a blocking STT call      #
# --------------------------------------------------------------------------- #


def test_a_wedged_stt_never_slows_the_tick() -> None:
    """A transcriber blocked mid-request cannot stall the 50 Hz tick."""
    gate = threading.Event()
    transcriber = _Transcriber(gate=gate)
    media = _Media()
    driver = _driver(media, transcriber=transcriber)
    try:
        t = _speak(driver, media, T0)
        # The worker has picked the utterance up and is now wedged inside STT.
        assert _await(lambda: transcriber.calls == 1)

        started = time.monotonic()
        t = _idle(driver, media, t, ticks=50)
        elapsed = time.monotonic() - started

        # 50 ticks against a wedged 10 s STT call: an inline call would take
        # >= 10 s. The whole run must fit inside a small fraction of that.
        assert elapsed < 0.5
        assert driver.peek() is None  # nothing latched while the STT is blocked
        assert driver.ticks == media.calls  # and not one tick was dropped

        gate.set()
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        gate.set()
        driver.close()


def test_the_stt_call_happens_off_the_tick_thread() -> None:
    """The network round-trip runs on the worker, never on the caller's thread."""
    transcriber = _Transcriber()
    media = _Media()
    driver = _driver(media, transcriber=transcriber)
    try:
        _speak(driver, media, T0)
        assert _await(lambda: transcriber.calls == 1)
        assert transcriber.threads
        assert threading.get_ident() not in transcriber.threads
        # The mic itself IS read on the tick thread (the held client's contract).
        assert media.threads == {threading.get_ident()}
    finally:
        driver.close()


def test_the_latch_is_one_tick_and_peeks_are_non_consuming() -> None:
    """Delivered exactly once, to exactly one tick — the ``PatSenseDriver`` cadence."""
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.transcripts == 1)

        driver(_ctx(t))
        t += DT
        assert driver.peek() == NAMED
        assert driver.peek() == NAMED  # peeking twice in a tick is identical

        driver(_ctx(t))
        assert driver.peek() is None  # cleared before the next tick's processing
    finally:
        driver.close()


def test_the_provider_feeds_read_perception() -> None:
    """``as_provider()`` drops straight into ``SenseProviders(transcript=...)``."""
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        providers = SenseProviders(transcript=driver.as_provider())
        assert read_perception(providers).transcript == NAMED
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 2 — an unreachable STT leaves the field None and drops no ticks    #
# --------------------------------------------------------------------------- #


@pytest.mark.offline
def test_an_unreachable_stt_leaves_the_field_none_and_drops_no_ticks() -> None:
    """The REAL transcriber against a blocked socket: no words, no lost ticks."""
    from reachy.speech.stt import Transcriber

    media = _Media()
    driver = _driver(media, transcriber=Transcriber(sample_rate=RATE))
    try:
        t = T0
        for _ in range(4):
            t = _speak(driver, media, t)
        assert _await(lambda: driver.submitted == 4)
        t = _idle(driver, media, t, ticks=20)

        assert driver.peek() is None
        assert driver.transcripts == 0
        assert driver.ticks == media.calls  # every tick ran; none dropped
        # 6 loud + 3 quiet ticks per utterance: `_speak` stops at the handoff
        # (the 3rd quiet tick is the one that endpoints and submits).
        assert driver.ticks == 4 * 9 + 20
    finally:
        driver.close()


def test_a_raising_transcriber_degrades_and_the_worker_survives() -> None:
    """One STT fault is "no words this utterance", not a dead worker."""
    transcriber = _Transcriber(error=RuntimeError("stt exploded"))
    media = _Media()
    driver = _driver(media, transcriber=transcriber)
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: transcriber.calls == 1)
        t = _idle(driver, media, t)
        assert driver.peek() is None
        assert driver.transcripts == 0

        # The worker is still alive: the next utterance transcribes normally.
        transcriber.error = None
        t = _speak(driver, media, t)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        driver.close()


def test_an_empty_transcript_latches_nothing() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(text=""))
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.submitted == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.peek() is None
        assert driver.transcripts == 0
    finally:
        driver.close()


def test_a_media_client_with_no_audio_never_raises() -> None:
    """A cold / disconnected held client returns ``None`` — the tick just idles."""
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        t = T0
        for _ in range(20):
            media.next_chunk = None
            driver(_ctx(t))
            t += DT
        assert driver.peek() is None
        assert driver.ticks == 20
    finally:
        driver.close()


def test_a_raising_media_client_degrades_to_no_audio() -> None:
    class _Boom:
        samplerate = RATE

        def audio(self):
            raise RuntimeError("media exploded")

    driver = _driver(_Boom(), transcriber=_Transcriber())
    try:
        for i in range(5):
            driver(_ctx(T0 + i * DT))
        assert driver.peek() is None
        assert driver.ticks == 5
    finally:
        driver.close()


def test_a_malformed_ctx_never_raises() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        media.next_chunk = _loud()
        driver(object())  # no ``now``, no ``sense``
        driver(SimpleNamespace(now="not-a-number", sense=None))
        assert driver.peek() is None
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 3 — the #54/#56 engagement gate still filters                     #
# --------------------------------------------------------------------------- #


def test_the_name_fast_path_engages_with_zero_classifier_calls() -> None:
    """An utterance naming the robot is addressed by definition — no LLM call."""
    classifier = _Classifier(verdict=False)  # would DROP if it were ever consulted
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(text=NAMED), classifier=classifier)
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
        assert classifier.calls == 0
    finally:
        driver.close()


def test_ambient_speech_the_classifier_rejects_never_reaches_the_latch() -> None:
    """Human-to-human chatter is dropped, at the cost of exactly one LLM call."""
    classifier = _Classifier(verdict=False)
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(text=AMBIENT), classifier=classifier)
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: classifier.calls == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.peek() is None
        assert driver.transcripts == 0
    finally:
        driver.close()


def test_addressed_speech_the_classifier_accepts_reaches_the_latch() -> None:
    classifier = _Classifier(verdict=True)
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(text=AMBIENT), classifier=classifier)
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == AMBIENT
        assert classifier.calls == 1
    finally:
        driver.close()


def test_a_raising_classifier_degrades_to_the_heuristic() -> None:
    """DEGRADE keeps hearing: the word-count + conversation-window rule takes over."""
    classifier = _Classifier(error=RuntimeError("llm down"))
    transcriber = _Transcriber(text=AMBIENT)
    media = _Media()
    driver = _driver(media, transcriber=transcriber, classifier=classifier)
    try:
        # No conversation open yet -> the heuristic drops a coherent-but-unnamed line.
        t = _speak(driver, media, T0)
        assert _await(lambda: classifier.calls == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.transcripts == 0

        # Name the robot: engages on the fast path and opens the window.
        transcriber.text = NAMED
        t = _speak(driver, media, t)
        assert _await(lambda: driver.transcripts == 1)
        t = _idle(driver, media, t)

        # A coherent follow-up inside the window now engages via the heuristic.
        transcriber.text = AMBIENT
        t = _speak(driver, media, t)
        assert _await(lambda: driver.transcripts == 2)
        driver(_ctx(t))
        assert driver.peek() == AMBIENT
    finally:
        driver.close()


def test_with_no_classifier_injected_the_gate_is_the_pure_heuristic() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(text=AMBIENT))
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.submitted == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.transcripts == 0  # unnamed, no open conversation -> dropped
    finally:
        driver.close()


def test_on_engage_fires_once_per_engaged_utterance_and_never_on_a_drop() -> None:
    fires: list[int] = []
    classifier = _Classifier(verdict=False)
    transcriber = _Transcriber(text=AMBIENT)
    media = _Media()
    driver = _driver(
        media,
        transcriber=transcriber,
        classifier=classifier,
        on_engage=lambda: fires.append(1),
    )
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: classifier.calls == 1)
        t = _idle(driver, media, t, ticks=10)
        assert fires == []  # dropped: no turn signal

        transcriber.text = NAMED
        t = _speak(driver, media, t)
        assert _await(lambda: driver.transcripts == 1)
        assert fires == [1]
    finally:
        driver.close()


def test_a_raising_on_engage_never_blocks_the_words() -> None:
    def _boom() -> None:
        raise RuntimeError("motion seam exploded")

    media = _Media()
    driver = _driver(media, transcriber=_Transcriber(), on_engage=_boom)
    try:
        t = _speak(driver, media, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Capture gates ported from the donor                                         #
# --------------------------------------------------------------------------- #


def test_the_self_mute_window_discards_audio_before_any_stt_call() -> None:
    """While the robot speaks, its own voice is never buffered nor transcribed."""
    transcriber = _Transcriber()
    media = _Media()
    driver = _driver(media, transcriber=transcriber, mute_until=lambda: T0 + 100.0)
    try:
        t = _speak(driver, media, T0)
        t = _idle(driver, media, t, ticks=10)
        assert transcriber.calls == 0
        assert driver.submitted == 0
        assert driver.ticks == media.calls  # muted ticks are not dropped ticks
    finally:
        driver.close()


def test_a_blip_shorter_than_min_utterance_is_dropped() -> None:
    transcriber = _Transcriber()
    media = _Media()
    driver = _driver(media, transcriber=transcriber)
    try:
        t = _speak(driver, media, T0, chunks=1)  # 160 samples < the 320-sample floor
        t = _idle(driver, media, t, ticks=10)
        assert transcriber.calls == 0
        assert driver.submitted == 0
    finally:
        driver.close()


def test_the_pre_roll_ring_keeps_audio_from_before_the_speech_onset() -> None:
    """The whole utterance reaches STT, lead-in included, in a single call."""
    seen: list[int] = []

    class _Sizing(_Transcriber):
        def transcribe_once(self, audio):
            seen.append(int(np.asarray(audio).size))
            return super().transcribe_once(audio)

    media = _Media()
    driver = _driver(media, transcriber=_Sizing())
    try:
        # Six quiet ticks first: they land in the ring, before any speech flag.
        t = _idle(driver, media, T0, ticks=6)
        t = _speak(driver, media, t, chunks=6)
        assert _await(lambda: len(seen) == 1)
        # 6 loud chunks = 960 samples; the pre-roll (0.05 s = 800 samples,
        # clamped to what the ring holds) must add lead-in on top of them.
        assert seen[0] > 6 * CHUNK
    finally:
        driver.close()


def test_a_long_monologue_is_force_flushed_at_the_cap() -> None:
    transcriber = _Transcriber()
    media = _Media()
    driver = _driver(media, transcriber=transcriber, tuning=_tuning(max_utterance_s=0.05))
    try:
        t = T0
        for _ in range(30):  # 30 unbroken loud ticks = 0.3 s of speech
            media.next_chunk = _loud()
            driver(_ctx(t))
            t += DT
        assert _await(lambda: driver.submitted >= 1)
    finally:
        driver.close()


def test_the_direction_of_the_utterance_is_latched_alongside_the_words() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        # DoA 0 rad is the daemon's "left".
        t = _speak(driver, media, T0, doa=0.0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
        assert driver.peek_direction() == "left"
        driver(_ctx(t + DT))
        assert driver.peek_direction() is None  # latched one tick, like the words
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Lifecycle + boundaries                                                      #
# --------------------------------------------------------------------------- #


def test_close_is_idempotent_and_stops_the_worker() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    _speak(driver, media, T0)
    assert _await(lambda: driver.transcripts == 1)
    before = set(threading.enumerate())
    driver.close()
    driver.close()  # idempotent
    assert _await(lambda: not any(th.is_alive() and th not in before for th in [driver.worker]))
    # A tick after close is a safe no-op.
    driver(_ctx(T0 + 1.0))
    assert driver.peek() is None


def test_a_driver_that_never_hears_speech_starts_no_worker_thread() -> None:
    media = _Media()
    driver = _driver(media, transcriber=_Transcriber())
    try:
        _idle(driver, media, T0, ticks=20)
        assert driver.worker is None
    finally:
        driver.close()


def test_the_module_imports_no_sdk_and_constructs_no_media_client() -> None:
    """The driver takes its media source injected — it never builds one."""
    import inspect

    from reachy.behavior import transcript_sense

    source = inspect.getsource(transcript_sense)
    assert "reachy_mini" not in source
    assert "media_session" not in source
    assert "HeldMediaClient(" not in source


def test_transcript_is_a_rule_testable_sense_field() -> None:
    from reachy.behavior.rule_engine import _field_present, _field_value
    from reachy.behavior.rules import SENSE_FIELDS, RulesConfig

    assert "transcript" in SENSE_FIELDS
    assert _field_present(Sense(transcript="reachy hello"), "transcript") is True
    assert _field_present(Sense(), "transcript") is False
    assert _field_value(Sense(transcript="reachy hello"), "transcript") == "reachy hello"

    config = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "heard-words",
                    "when": {"field": "transcript", "op": "is_true"},
                    "run": "thoughtful",
                    "duration_s": 2.0,
                }
            ]
        }
    )
    assert config.react[0].when.field == "transcript"
