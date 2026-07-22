"""Tests for the runtime transcript sense (task t11, recaptured by t4).

:mod:`reachy.behavior.transcript_sense` gives the symbolic behavior runtime
words. Its ADMISSION half is unchanged since t11 — the #54/#56 layered
engagement gate — while its CAPTURE half moved to server-side VAD in the
realtime arc (issue #115): the driver streams every mic chunk into an injected
:class:`~reachy.speech.realtime.RealtimeTranscriber` session and takes back
already-endpointed utterances, instead of running a local energy VAD over a
pre-roll ring and POSTing finished clips to
:class:`reachy.speech.stt.Transcriber`.

These tests pin, from failing-first, the acceptance criteria of both tasks:

* **The tick reads a LATCHED value, never a blocking call.** The one network
  round-trip left in this module — the engagement classifier's — happens on a
  background worker; the tick only peeks a latch. Proved two ways: a
  deliberately wedged classifier cannot slow the tick down, and the fake records
  the thread it was called on — never the tick thread.
* **An unreachable session leaves the field ``None`` and drops no ticks.** Driven
  in the ``offline`` lane against a REAL
  :class:`~reachy.speech.realtime.RealtimeTranscriber` with sockets blocked, so
  the degradation is the shipped code path, not a fake. There is no local
  fallback endpointer by design (the arc's operator decision c17).
* **The #54/#56 engagement gate still filters addressed-vs-ambient speech**, and
  still receives exactly the utterance shape it always did. The gate is reused,
  not reimplemented: the name fast-path engages with ZERO classifier calls, an
  ambient utterance the classifier rejects never latches, and a raising
  classifier degrades to the word-count/conversation-window heuristic rather
  than stalling.
* **Self-mute is two guards now**: the tick feeds the session no audio while the
  robot speaks, AND a transcript that ARRIVES inside the mute window is
  discarded — because the server's VAD cannot know when the robot is speaking.
* The one-tick latch cadence of :class:`reachy.behavior.pat_sense.PatSenseDriver`
  (cleared before every tick, peeked non-destructively within a tick).

The wire itself is exercised elsewhere: ``tests/test_realtime_client.py`` drives
the session client over a loopback socket, and
``tests/test_behavior_transcript_realtime.py`` joins the two ends (structural
no-socket-on-the-tick guard + a real session end to end).

Deterministic apart from the deliberate thread handoff, which every test waits
on with a bounded poll. No robot, SDK, daemon or socket anywhere here: the media
client and the session client are both fakes.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np
import pytest

from reachy.behavior.sense import Sense, SenseProviders, read_perception
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning
from reachy.speech.realtime import Utterance

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


def _sound(n: int = CHUNK) -> np.ndarray:
    """A mic chunk carrying signal (content is irrelevant — nothing gates on it)."""
    return np.full(n, 0.5, dtype=np.float32)


def _quiet(n: int = CHUNK) -> np.ndarray:
    """A silent mic chunk. Still forwarded: the server decides what is speech."""
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


class _Realtime:
    """A fake :class:`~reachy.speech.realtime.RealtimeTranscriber`.

    Records every chunk it was handed (and the thread that handed it), and
    replays utterances a test queues with :meth:`emit` — the offline stand-in
    for the server's own VAD deciding a sentence ended.
    """

    def __init__(
        self,
        *,
        submit_error: BaseException | None = None,
        take_error: BaseException | None = None,
    ) -> None:
        self.chunks: list[np.ndarray] = []
        self.rates: list[int] = []
        self.threads: set[int] = set()
        self.submit_error = submit_error
        self.take_error = take_error
        self._ready: deque[Utterance] = deque()
        self._lock = threading.Lock()

    # --- the client's tick-thread surface ---------------------------------
    def submit_audio(self, audio) -> bool:
        self.threads.add(threading.get_ident())
        if self.submit_error is not None:
            raise self.submit_error
        self.chunks.append(np.asarray(audio, dtype=np.float32).copy())
        return True

    def take_utterance(self):
        if self.take_error is not None:
            raise self.take_error
        with self._lock:
            return self._ready.popleft() if self._ready else None

    def set_sample_rate(self, rate: int) -> None:
        self.rates.append(int(rate))

    # --- test-side scripting ----------------------------------------------
    def emit(self, text: str, t: float) -> None:
        """Queue one server-endpointed utterance, stamped as arriving at *t*."""
        with self._lock:
            self._ready.append(Utterance(text=text, t=t, item_id="item", session_id="sess"))

    @property
    def streamed(self) -> np.ndarray:
        """Everything submitted so far, concatenated in submission order."""
        if not self.chunks:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(self.chunks)


class _Classifier:
    """A fake :class:`~reachy.speech.engagement.EngagementClassifier`."""

    def __init__(
        self,
        verdict: bool = True,
        *,
        error: BaseException | None = None,
        gate: threading.Event | None = None,
    ) -> None:
        self.verdict = verdict
        self.error = error
        self.gate = gate
        self.calls = 0
        self.threads: set[int] = set()

    def judge(self, text, context):
        self.calls += 1
        self.threads.add(threading.get_ident())
        if self.gate is not None:
            self.gate.wait(10.0)
        if self.error is not None:
            raise self.error
        return self.verdict


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _tuning(**kw) -> TranscriptTuning:
    """Fast admission tuning (a 1 s conversation window keeps the tests short)."""
    base = dict(min_words=3, engage_window_s=1.0)
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


def _hear(driver, media: _Media, session: _Realtime, text: str, t: float, *, doa=None) -> float:
    """The server endpoints one utterance; drive the tick that collects it.

    The tick hands the words to the worker (a ``put_nowait``); the LATCH is
    written by whichever LATER tick drains the worker's ready queue, so callers
    wait on ``driver.transcripts`` and then drive one explicit tick to observe
    it — exactly the cadence the one-tick contract demands.
    """
    session.emit(text, t=t)
    media.next_chunk = _sound()
    driver(_ctx(t, doa))
    return t + DT


def _idle(driver, media: _Media, t: float, ticks: int = 5) -> float:
    """Drive *ticks* ticks with nothing endpointed (latch cleared each time)."""
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
# Criterion 1 — the tick reads a latched value, never a blocking call          #
# --------------------------------------------------------------------------- #


def test_a_wedged_engagement_gate_never_slows_the_tick() -> None:
    """The one remaining network leg, blocked mid-call, cannot stall the 50 Hz tick.

    The STT round-trip left this module with the realtime session; the
    engagement classifier's call did not. So it is the classifier that must
    never be reachable from the tick thread.
    """
    gate = threading.Event()
    classifier = _Classifier(verdict=True, gate=gate)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        # Open the conversation by name (fast path, no classifier call)...
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        # ...then a nameless follow-up inside the window reaches the wedged gate.
        t = _hear(driver, media, session, AMBIENT, t)
        assert _await(lambda: classifier.calls == 1)

        started = time.monotonic()
        t = _idle(driver, media, t, ticks=50)
        elapsed = time.monotonic() - started

        # 50 ticks against a wedged 10 s classifier call: an inline call would
        # take >= 10 s. The whole run must fit inside a small fraction of that.
        assert elapsed < 0.5
        assert driver.transcripts == 1  # the second turn is still stuck in the gate
        assert driver.ticks == media.calls  # and not one tick was dropped

        gate.set()
        assert _await(lambda: driver.transcripts == 2)
        driver(_ctx(t))
        assert driver.peek() == AMBIENT
    finally:
        gate.set()
        driver.close()


def test_the_engagement_gate_runs_off_the_tick_thread() -> None:
    """The classifier round-trip runs on the worker, never on the caller's thread."""
    classifier = _Classifier(verdict=True)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        _hear(driver, media, session, AMBIENT, t)
        assert _await(lambda: classifier.calls == 1)

        assert classifier.threads
        assert threading.get_ident() not in classifier.threads
        # The mic AND the session's non-blocking surface ARE the tick thread's.
        assert media.threads == {threading.get_ident()}
        assert session.threads == {threading.get_ident()}
    finally:
        driver.close()


def test_the_latch_is_one_tick_and_peeks_are_non_consuming() -> None:
    """Delivered exactly once, to exactly one tick — the ``PatSenseDriver`` cadence."""
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        t = _hear(driver, media, session, NAMED, T0)
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
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        providers = SenseProviders(transcript=driver.as_provider())
        assert read_perception(providers).transcript == NAMED
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Criterion 2 — an unreachable session leaves the field None and drops no ticks#
# --------------------------------------------------------------------------- #


@pytest.mark.offline
def test_an_unreachable_realtime_session_leaves_the_field_none_and_drops_no_ticks() -> None:
    """The REAL session client against blocked sockets: no words, no lost ticks.

    This is the shape of the no-fallback decision (c17): with the gateway down
    the runtime simply stops hearing — it does not quietly re-endpoint locally —
    and the client keeps retrying on its own worker thread while the tick loop
    runs untouched.
    """
    from reachy.speech.realtime import RealtimeTranscriber

    media = _Media()
    session = RealtimeTranscriber(
        sample_rate=RATE,
        url="ws://127.0.0.1:1/v1/realtime",
        backoff_initial_s=0.2,
        backoff_max_s=0.2,
    )
    driver = _driver(media, realtime=session)
    try:
        t = T0
        for _ in range(40):
            media.next_chunk = _sound()
            driver(_ctx(t))
            t += DT

        assert driver.peek() is None
        assert driver.transcripts == 0
        assert driver.streamed == 40  # the chunks were offered, not withheld
        assert driver.ticks == media.calls  # every tick ran; none dropped
        assert driver.ticks == 40
    finally:
        driver.close()
        session.close()


def test_a_raising_session_client_degrades_and_the_tick_survives() -> None:
    """A duck-typed client that explodes costs words, never ticks."""
    media = _Media()
    session = _Realtime(
        submit_error=RuntimeError("submit exploded"),
        take_error=RuntimeError("take exploded"),
    )
    driver = _driver(media, realtime=session)
    try:
        t = T0
        for _ in range(10):
            media.next_chunk = _sound()
            driver(_ctx(t))
            t += DT
        assert driver.peek() is None
        assert driver.ticks == 10
        assert driver.streamed == 0  # every submit raised

        # It recovers the moment the client does.
        session.submit_error = None
        session.take_error = None
        t = _hear(driver, media, session, NAMED, t)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        driver.close()


def test_an_empty_transcript_latches_nothing() -> None:
    """The client already drops empty transcripts; a duck-typed one might not."""
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        t = _hear(driver, media, session, "   ", T0)
        t = _idle(driver, media, t, ticks=10)
        assert driver.peek() is None
        assert driver.submitted == 0
        assert driver.transcripts == 0
    finally:
        driver.close()


def test_no_session_wired_hears_nothing_and_says_so_once(caplog) -> None:
    """``realtime=None`` is a quiet sense, not a silent one (and not a crash).

    This is the state a bare box or a half-composed runtime is in: the mic is
    still read every tick (a co-riding loudness provider needs that sample), the
    transcript field stays ``None``, and the journal carries exactly ONE named
    line rather than one per chunk at 50 Hz (the #99 flood discipline).
    """
    import logging

    media = _Media()
    driver = _driver(media)
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            t = T0
            for _ in range(20):
                media.next_chunk = _sound()
                driver(_ctx(t))
                t += DT
        assert driver.peek() is None
        assert driver.ticks == media.calls == 20
        lines = [m for m in caplog.messages if "reason=no-realtime-session" in m]
        assert len(lines) == 1
    finally:
        driver.close()


def test_a_media_client_with_no_audio_never_raises() -> None:
    """A cold / disconnected held client returns ``None`` — the tick just idles."""
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        t = T0
        for _ in range(20):
            media.next_chunk = None
            driver(_ctx(t))
            t += DT
        assert driver.peek() is None
        assert driver.ticks == 20
        assert session.chunks == []
    finally:
        driver.close()


def test_a_raising_media_client_degrades_to_no_audio() -> None:
    class _Boom:
        samplerate = RATE

        def audio(self):
            raise RuntimeError("media exploded")

    driver = _driver(_Boom(), realtime=_Realtime())
    try:
        for i in range(5):
            driver(_ctx(T0 + i * DT))
        assert driver.peek() is None
        assert driver.ticks == 5
    finally:
        driver.close()


def test_a_malformed_ctx_never_raises() -> None:
    media = _Media()
    driver = _driver(media, realtime=_Realtime())
    try:
        media.next_chunk = _sound()
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
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
        assert classifier.calls == 0
    finally:
        driver.close()


def test_ambient_speech_from_cold_never_reaches_the_classifier() -> None:
    """No conversation open -> ambient chatter is dropped with ZERO LLM calls (#105).

    This is the measured failure mode: 45 minutes of human-to-human speech with
    the robot's name never spoken. With no conversation for it to continue,
    nothing the classifier could say is allowed to start one, so it is not asked.
    """
    classifier = _Classifier(verdict=True)  # maximally credulous — must not be consulted
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, AMBIENT, T0)
        assert _await(lambda: driver.judged == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.peek() is None
        assert driver.transcripts == 0
        assert classifier.calls == 0
    finally:
        driver.close()


def test_ambient_speech_the_classifier_rejects_never_reaches_the_latch() -> None:
    """Inside an open conversation, chatter still costs exactly one LLM call to drop.

    The warm window admits the utterance to the classifier; the classifier is
    what says no. The 199 correct drops measured live are this path.
    """
    classifier = _Classifier(verdict=False)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        # Open the conversation by name (fast path, zero classifier calls).
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        assert classifier.calls == 0

        t = _hear(driver, media, session, AMBIENT, t)
        assert _await(lambda: classifier.calls == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.peek() is None
        assert driver.transcripts == 1  # still just the named turn
    finally:
        driver.close()


def test_addressed_speech_the_classifier_accepts_reaches_the_latch() -> None:
    """A nameless follow-up inside an open conversation engages on the classifier's YES."""
    classifier = _Classifier(verdict=True)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)

        t = _hear(driver, media, session, AMBIENT, t)
        assert _await(lambda: driver.transcripts == 2)
        driver(_ctx(t))
        assert driver.peek() == AMBIENT
        assert classifier.calls == 1
    finally:
        driver.close()


def test_a_raising_classifier_degrades_to_the_heuristic() -> None:
    """DEGRADE keeps hearing: the word-count + conversation-window rule takes over."""
    classifier = _Classifier(error=RuntimeError("llm down"))
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        # No conversation open yet -> dropped before the classifier is reached.
        t = _hear(driver, media, session, AMBIENT, T0)
        assert _await(lambda: driver.judged == 1)
        t = _idle(driver, media, t, ticks=10)
        assert driver.transcripts == 0

        # Name the robot: engages on the fast path and opens the window.
        t = _hear(driver, media, session, NAMED, t)
        assert _await(lambda: driver.transcripts == 1)

        # A coherent follow-up inside the window reaches the (dead) classifier,
        # degrades, and the heuristic accepts it — hearing never stalls.
        t = _hear(driver, media, session, AMBIENT, t)
        assert _await(lambda: driver.transcripts == 2)
        assert classifier.calls == 1
        driver(_ctx(t))
        assert driver.peek() == AMBIENT
    finally:
        driver.close()


def test_with_no_classifier_injected_the_gate_is_the_pure_heuristic() -> None:
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        t = _hear(driver, media, session, AMBIENT, T0)
        assert _await(lambda: driver.judged == 1)
        _idle(driver, media, t, ticks=10)
        assert driver.transcripts == 0  # unnamed, no open conversation -> dropped
    finally:
        driver.close()


def test_on_engage_fires_once_per_engaged_utterance_and_never_on_a_drop() -> None:
    fires: list[int] = []
    classifier = _Classifier(verdict=False)
    media = _Media()
    session = _Realtime()
    driver = _driver(
        media,
        realtime=session,
        classifier=classifier,
        on_engage=lambda: fires.append(1),
    )
    try:
        t = _hear(driver, media, session, AMBIENT, T0)
        assert _await(lambda: driver.judged == 1)
        t = _idle(driver, media, t, ticks=10)
        assert fires == []  # dropped: no turn signal

        t = _hear(driver, media, session, NAMED, t)
        assert _await(lambda: driver.transcripts == 1)
        assert fires == [1]
    finally:
        driver.close()


def test_a_false_accept_does_not_hold_the_driver_gate_open() -> None:
    """THE #105 REGRESSION, end-to-end through the driver.

    One accepted turn opens the conversation; then the short backchannels that
    were measured engaging on the deployed box arrive one after another. The
    classifier is maximally credulous (YES to everything), so if any of them
    engages it is the gate's structure that failed, not the model.

    Under the old ratchet each accept re-seeded a six-turn "mid-conversation"
    context and the run stayed open indefinitely. Here only the opening turn
    ever latches.
    """
    classifier = _Classifier(verdict=True)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)

        for index, text in enumerate(["No.", "Okay.", "Right.", "Yeah.", "Hold up."], start=2):
            t = _hear(driver, media, session, text, t)
            assert _await(lambda: driver.judged == index)
            t = _idle(driver, media, t, ticks=10)

        assert driver.transcripts == 1, "a short backchannel rode the open conversation"
        assert classifier.calls == 0, "a backchannel must not even reach the classifier"
    finally:
        driver.close()


def test_the_conversation_closes_once_it_goes_quiet() -> None:
    """Past the engage window a nameless utterance drops with no classifier call.

    The window is what stops a leak amplifying: once closed, only a fresh name
    can reopen the conversation, so no run of credulous verdicts can keep it
    alive on its own.
    """
    classifier = _Classifier(verdict=True)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)

        # Jump far past the 1.0 s test engage window.
        t = _hear(driver, media, session, AMBIENT, t + 60.0)
        assert _await(lambda: driver.judged == 2)
        _idle(driver, media, t, ticks=10)
        assert driver.transcripts == 1
        assert classifier.calls == 0
    finally:
        driver.close()


def test_a_raising_on_engage_never_blocks_the_words() -> None:
    def _boom() -> None:
        raise RuntimeError("motion seam exploded")

    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, on_engage=_boom)
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        driver.close()


def test_the_escape_hatch_builds_no_gate_at_all(monkeypatch) -> None:
    """``REACHY_ENGAGE_HEURISTIC=1`` means no classifier is BUILT, not just unused.

    The stronger structural guarantee behind the escape hatch: with it set there
    is no gate object, so there is no code path on which a classifier call could
    happen, whatever an injected classifier would have said.
    """
    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", "1")
    classifier = _Classifier(verdict=True)
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, classifier=classifier)
    try:
        assert driver._gate is None
        t = _hear(driver, media, session, AMBIENT, T0)
        assert _await(lambda: driver.judged == 1)
        _idle(driver, media, t, ticks=10)
        assert classifier.calls == 0
    finally:
        driver.close()


def test_no_classifier_injected_builds_no_gate() -> None:
    """Omitting the classifier is the same guarantee as the escape hatch."""
    driver = _driver(_Media(), realtime=_Realtime())
    try:
        assert driver._gate is None
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Capture — what the tick does with audio and with arrived words               #
# --------------------------------------------------------------------------- #


def test_the_self_mute_window_feeds_the_session_no_audio(caplog) -> None:
    """While the robot speaks, its own voice never reaches the server's VAD.

    Latched, not per-chunk: audio arrives 50 times a second, so a drop line per
    withheld chunk is the #99 journal flood, not observability.
    """
    import logging

    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, mute_until=lambda: T0 + 0.1)
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            t = T0
            for _ in range(10):  # the whole mute window
                media.next_chunk = _sound()
                driver(_ctx(t))
                t += DT
            assert session.chunks == []
            assert driver.streamed == 0
            assert driver.ticks == media.calls  # muted ticks are not dropped ticks

            # Past the window the stream resumes, with no second announcement.
            for _ in range(5):
                media.next_chunk = _sound()
                driver(_ctx(t))
                t += DT
        assert driver.streamed == 5
        muted = [m for m in caplog.messages if "reason=self-mute" in m]
        assert len(muted) == 1
    finally:
        driver.close()


def test_a_transcript_arriving_inside_the_self_mute_window_is_discarded(caplog) -> None:
    """The server's VAD cannot know the robot was talking — this can.

    ``Utterance.t`` is the monotonic instant the transcript ARRIVED, stamped by
    the session client off the same clock ``mute_until`` speaks. A transcript
    that lands mid-clip is (probably) the robot hearing itself, and is refused
    before it can reach the gate. Both clocks are injected here, so the test is
    deterministic rather than timing-dependent.
    """
    import logging

    media = _Media()
    session = _Realtime()
    mute_until = T0 + 0.5
    driver = _driver(media, realtime=session, mute_until=lambda: mute_until)
    try:
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            # Arrives INSIDE the window (t < mute_until) -> discarded...
            session.emit(NAMED, t=T0 + 0.2)
            media.next_chunk = _quiet()
            driver(_ctx(T0))
            assert not _await(lambda: driver.submitted > 0, timeout=0.2)
            assert driver.transcripts == 0

            # ...while the identical words arriving after it are heard.
            session.emit(NAMED, t=mute_until + 0.01)
            driver(_ctx(T0 + DT))
            assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(T0 + 2 * DT))
        assert driver.peek() == NAMED
        # Two guards, two distinguishable lines: the stream-level (latched)
        # suppression, and this arrival's own per-utterance refusal.
        muted = [m for m in caplog.messages if "reason=self-mute" in m]
        arrivals = [m for m in muted if "event=stream" not in m]
        assert len(arrivals) == 1, "the discarded arrival must name the reason exactly once"
    finally:
        driver.close()


def test_the_real_mic_rate_is_pushed_into_the_session_config() -> None:
    """The session resamples from the rate it is told, so it must be the mic's.

    Resolved only AFTER a successful read (touching ``samplerate`` on a cold
    holder can trigger the holder's blocking construction), and pushed once —
    ``set_sample_rate`` is a no-op when it already matches.
    """
    media = _Media(samplerate=48000)
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        media.next_chunk = None
        driver(_ctx(T0))
        assert session.rates == [], "the rate was read before any audio proved the mic is up"

        for i in range(5):
            media.next_chunk = _sound()
            driver(_ctx(T0 + (i + 1) * DT))
        assert session.rates == [48000]
    finally:
        driver.close()


def test_a_pump_concatenated_multi_chunk_tick_is_forwarded_verbatim() -> None:
    """#100: the audio feed is the pump's per-tick latch — ONE array concatenating
    every chunk produced since the last tick. The driver forwards whatever it is
    handed, unsliced and unresampled, so a concatenation reaches the session as
    exactly the samples the microphone produced."""
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        chunk = np.concatenate([_sound(), _quiet(), _sound(), _quiet()])  # 640 samples
        media.next_chunk = chunk
        driver(_ctx(T0))
        assert len(session.chunks) == 1
        assert np.array_equal(session.chunks[0], chunk)
    finally:
        driver.close()


def test_the_direction_of_the_utterance_is_latched_alongside_the_words() -> None:
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    try:
        # DoA 0 rad is the daemon's "left".
        t = _hear(driver, media, session, NAMED, T0, doa=0.0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
        assert driver.peek_direction() == "left"
        driver(_ctx(t + DT))
        assert driver.peek_direction() is None  # latched one tick, like the words
    finally:
        driver.close()


def test_a_burst_of_utterances_is_drained_over_ticks_not_in_one_loop() -> None:
    """One tick pops a bounded number of ready utterances; the rest wait a tick.

    The bound is what stops a reconnect that delivers a backlog from turning one
    tick into an unbounded loop — the same reason every queue here has a size.
    """
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session, max_takes_per_tick=2)
    try:
        for index in range(4):
            session.emit(f"reachy number {index}", t=T0)
        media.next_chunk = _quiet()
        driver(_ctx(T0))
        assert driver.submitted == 2
        driver(_ctx(T0 + DT))
        assert driver.submitted == 4
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Lifecycle + boundaries                                                      #
# --------------------------------------------------------------------------- #


def test_close_is_idempotent_and_stops_the_worker() -> None:
    media = _Media()
    session = _Realtime()
    driver = _driver(media, realtime=session)
    _hear(driver, media, session, NAMED, T0)
    assert _await(lambda: driver.transcripts == 1)
    before = set(threading.enumerate())
    driver.close()
    driver.close()  # idempotent
    assert _await(lambda: not any(th.is_alive() and th not in before for th in [driver.worker]))
    # A tick after close is a safe no-op.
    driver(_ctx(T0 + 1.0))
    assert driver.peek() is None


def test_close_does_not_close_the_injected_session_client() -> None:
    """The composition root owns the session; a sense driver must not shut it down.

    Same rule as the held media client: one owner, many readers. Closing it here
    would take hearing away from a runtime that is merely rebuilding one sense.
    """
    closed: list[int] = []

    class _Closable(_Realtime):
        def close(self) -> None:
            closed.append(1)

    media = _Media()
    driver = _driver(media, realtime=_Closable())
    driver.close()
    assert closed == []


def test_a_driver_that_never_hears_words_starts_no_worker_thread() -> None:
    media = _Media()
    driver = _driver(media, realtime=_Realtime())
    try:
        _idle(driver, media, T0, ticks=20)
        assert driver.worker is None
    finally:
        driver.close()


def test_the_module_imports_no_sdk_and_constructs_no_media_client() -> None:
    """The driver takes both its sources injected — it never builds either."""
    import inspect

    from reachy.behavior import transcript_sense

    source = inspect.getsource(transcript_sense)
    assert "reachy_mini" not in source
    assert "media_session" not in source
    assert "HeldMediaClient(" not in source
    assert "RealtimeTranscriber(" not in source


def test_the_retired_background_seam_is_still_accepted_and_ignored() -> None:
    """A transitional kindness to the composition root, and nothing more.

    ``background`` fed the local energy VAD's room-relative threshold (#102).
    The server's VAD endpoints now, so nothing consumes it — but the runtime's
    composition still passes it until plan task t5 rewrites that call, and a
    ``TypeError`` there would take the whole runtime down. It is accepted,
    ignored, and goes away with t5.
    """
    media = _Media()
    session = _Realtime()
    driver = TranscriptSenseDriver(
        media=media,
        realtime=session,
        background=lambda: 0.034,
        tuning=_tuning(),
    )
    try:
        t = _hear(driver, media, session, NAMED, T0)
        assert _await(lambda: driver.transcripts == 1)
        driver(_ctx(t))
        assert driver.peek() == NAMED
    finally:
        driver.close()


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


def test_the_transcript_rule_schema_is_untouched_by_the_realtime_arc() -> None:
    """Where the words come from is not the rule schema's business (arc decision q2).

    A rule reacts to THAT something was said; reacting to WHAT was said stays
    the attached agent's job. So this arc changes nothing about how a rules file
    may key on ``transcript`` — the three ops the runtime actually supports load
    exactly as before, and the field is still listed as a CORROBORATING signal
    (it is an utterance that cleared the layered engagement gate, whoever
    endpointed it).

    Honest caveat, recorded rather than asserted away: ``rules.py`` does NOT
    *refuse* a content op today — ``{field=transcript, op=eq, value="hi"}`` is
    schema-valid and compares strings at evaluation time, because ``COMPARATORS``
    is global rather than per-field. The spec's claim that "the only legal ops
    over it are boolean" describes the shipped rules and the supported usage, not
    a validator refusal. Tightening that is a rule-schema change, which this arc
    is explicitly not allowed to make — so this test pins the status quo in BOTH
    directions: if a later change adds the refusal, this test fails and says so
    here rather than letting the schema drift silently.
    """
    from reachy.behavior.rules import COMPARATORS, CORROBORATING_SENSE_FIELDS, RulesConfig

    def _load(when: dict):
        return RulesConfig.from_dict(
            {"react": [{"id": "r", "when": when, "run": "thoughtful", "duration_s": 2.0}]}
        )

    for when in (
        {"field": "transcript", "op": "is_true"},
        {"field": "transcript", "op": "is_false"},
        {"field": "transcript", "op": "absent_for", "value": 5.0},
    ):
        assert _load(when).react[0].when.field == "transcript"

    assert "transcript" in CORROBORATING_SENSE_FIELDS
    assert {"is_true", "is_false", "absent_for", "eq", "ne", "lt", "gt", "le", "ge"} == set(
        COMPARATORS
    ), "the comparator set moved — the realtime arc must not touch the rule schema"
    # The documented gap, pinned so it cannot change unnoticed in either direction.
    assert _load({"field": "transcript", "op": "eq", "value": "hi"}).react[0].when.op == "eq"
