"""The ``name_mentioned`` one-tick latch, and the LIVE names behind it (#177).

:class:`~reachy.behavior.transcript_sense.TranscriptSenseDriver` already latches
WHAT was heard. These tests pin the second half the configurable-names arc
needs: WHY it was admitted, and against WHICH names.

* **The names are a PROVIDER, resolved per utterance.** A driver built with
  ``names_provider=`` never snapshots the names, so an operator renaming the
  robot while the runtime is up is obeyed by the very next utterance — nothing
  is rebuilt, and the conversation state the gate holds is not lost. A driver
  built WITHOUT one behaves exactly as it always did (pinned by the whole of
  ``tests/test_behavior_transcript_sense.py``, and again here).
* **``name_mentioned`` is a separate one-tick latch on the same cadence as the
  transcript.** ``True`` for the single tick that adopts a by-name admission,
  ``False`` for the tick that adopts a CONTEXT admission (which still sets
  ``transcript``) and for every tick after. The distinction is not derivable
  from the text downstream: the gate's fuzzy matcher admits STT mishearings a
  plain substring check would miss, and only the gate knows which rule fired.

"nova" is used throughout as a CONFIGURED name. It is not a shipped name and
must never become one: the robot learns it from configuration, never from a
literal in ``reachy/``.

No robot, SDK, daemon or socket: the media client, the session client and the
classifier are all fakes, and every thread handoff is waited on with a bounded
poll.
"""

from __future__ import annotations

import threading
import time
from collections import deque
from types import SimpleNamespace

import numpy as np

from reachy.behavior.sense import Sense
from reachy.behavior.transcript_sense import TranscriptSenseDriver, TranscriptTuning
from reachy.speech.name_match import SHIPPED_NAMES
from reachy.speech.realtime import Utterance

RATE = 16000
CHUNK = 160
DT = 0.01
T0 = 100.0

#: The shipped pair plus one operator-configured name.
CONFIGURED_NAMES: tuple[str, ...] = SHIPPED_NAMES + ("nova",)

#: Names the robot answers to only if it was CONFIGURED to.
BY_CONFIGURED_NAME = "nova come here"
#: A coherent utterance naming nobody — admissible on context alone.
NAMELESS = "did you see the game last night"


class _Media:
    """A fake ``HeldMediaClient`` handing out one fixed chunk."""

    samplerate = RATE
    channels = 1

    def __init__(self) -> None:
        self.next_chunk: np.ndarray | None = np.full(CHUNK, 0.5, dtype=np.float32)

    def audio(self):
        return self.next_chunk


class _Realtime:
    """A fake session client replaying utterances a test queues."""

    def __init__(self) -> None:
        self._ready: deque[Utterance] = deque()
        self._lock = threading.Lock()

    def submit_audio(self, audio) -> bool:  # noqa: ARG002 - the fake keeps nothing
        return True

    def take_utterance(self):
        with self._lock:
            return self._ready.popleft() if self._ready else None

    def set_sample_rate(self, rate: int) -> None:
        pass

    def emit(self, text: str, t: float) -> None:
        with self._lock:
            self._ready.append(Utterance(text=text, t=t, item_id="item", session_id="sess"))


class _Classifier:
    """A fake engagement classifier with a fixed verdict."""

    def __init__(self, verdict: bool = True) -> None:
        self.verdict = verdict
        self.calls = 0

    def judge(self, text, context) -> bool:  # noqa: ARG002 - verdict is fixed
        self.calls += 1
        return self.verdict


def _ctx(now: float):
    return SimpleNamespace(now=now, tick=int(now * 100), sense=Sense())


def _await(predicate, timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return False


def _driver(**kw) -> TranscriptSenseDriver:
    kw.setdefault("tuning", TranscriptTuning(min_words=3, engage_window_s=5.0))
    kw.setdefault("media", _Media())
    return TranscriptSenseDriver(**kw)


def _hear(driver, session: _Realtime, text: str, t: float) -> float:
    """The server endpoints one utterance; drive the tick that collects it."""
    session.emit(text, t=t)
    driver(_ctx(t))
    return t + DT


def _adopt(driver, t: float, expected: int) -> float:
    """Wait for the worker, then drive the ONE tick that adopts the transcript."""
    assert _await(lambda: driver.transcripts == expected)
    driver(_ctx(t))
    return t + DT


# --------------------------------------------------------------------------- #
# The names provider reaches the gate, live                                    #
# --------------------------------------------------------------------------- #


def test_a_configured_name_engages_and_latches_name_mentioned() -> None:
    """With a provider naming "nova", "nova come here" engages BY NAME."""
    session = _Realtime()
    driver = _driver(
        realtime=session,
        classifier=_Classifier(verdict=False),
        names_provider=lambda: CONFIGURED_NAMES,
    )
    try:
        t = _hear(driver, session, BY_CONFIGURED_NAME, T0)
        t = _adopt(driver, t, expected=1)
        assert driver.peek() == BY_CONFIGURED_NAME
        assert driver.peek_name_mentioned() is True

        # ...and it is a ONE-TICK latch: the very next tick clears it.
        driver(_ctx(t))
        assert driver.peek() is None
        assert driver.peek_name_mentioned() is False
    finally:
        driver.close()


def test_a_provider_swap_takes_effect_on_the_next_utterance() -> None:
    """Nothing is rebuilt: the second utterance is judged against the new names."""
    configured: list[tuple[str, ...]] = [SHIPPED_NAMES]
    session = _Realtime()
    driver = _driver(
        realtime=session,
        classifier=_Classifier(verdict=False),
        names_provider=lambda: configured[0],
    )
    try:
        # Cold gate, name not configured yet: dropped, nothing latches.
        t = _hear(driver, session, BY_CONFIGURED_NAME, T0)
        assert _await(lambda: driver.judged == 1)
        driver(_ctx(t))
        t += DT
        assert driver.peek() is None
        assert driver.transcripts == 0

        configured[0] = CONFIGURED_NAMES
        t = _hear(driver, session, BY_CONFIGURED_NAME, t)
        t = _adopt(driver, t, expected=1)
        assert driver.peek() == BY_CONFIGURED_NAME
        assert driver.peek_name_mentioned() is True
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# name_mentioned is about the RULE that admitted, not about the text           #
# --------------------------------------------------------------------------- #


def test_a_context_admission_sets_transcript_but_not_name_mentioned() -> None:
    """A nameless follow-up is heard, but the robot was not ADDRESSED by name."""
    session = _Realtime()
    classifier = _Classifier(verdict=True)
    driver = _driver(
        realtime=session,
        classifier=classifier,
        names_provider=lambda: CONFIGURED_NAMES,
    )
    try:
        # A name opens the conversation (zero classifier calls)...
        t = _hear(driver, session, BY_CONFIGURED_NAME, T0)
        t = _adopt(driver, t, expected=1)
        assert driver.peek_name_mentioned() is True
        assert classifier.calls == 0

        # ...then a nameless utterance is admitted on CONTEXT.
        t = _hear(driver, session, NAMELESS, t)
        t = _adopt(driver, t, expected=2)
        assert driver.peek() == NAMELESS
        assert driver.peek_name_mentioned() is False
        assert classifier.calls == 1
    finally:
        driver.close()


def test_the_heuristic_path_still_reports_a_name() -> None:
    """With no classifier at all, a named utterance still latches name_mentioned.

    The pure-heuristic path (``REACHY_ENGAGE_HEURISTIC``, or no classifier
    injected) builds no gate — so if it did not report WHY it engaged, a robot
    whose gateway is down would stop noticing it had been called.
    """
    session = _Realtime()
    driver = _driver(realtime=session, names_provider=lambda: CONFIGURED_NAMES)
    try:
        t = _hear(driver, session, BY_CONFIGURED_NAME, T0)
        _adopt(driver, t, expected=1)
        assert driver.peek_name_mentioned() is True
    finally:
        driver.close()


def test_a_driver_with_no_provider_behaves_exactly_as_before() -> None:
    """The shipped names still work, and an unconfigured name still does not."""
    session = _Realtime()
    driver = _driver(realtime=session)
    try:
        t = _hear(driver, session, "reachy can you look at me", T0)
        t = _adopt(driver, t, expected=1)
        assert driver.peek_name_mentioned() is True

        t = _hear(driver, session, BY_CONFIGURED_NAME, t)
        assert _await(lambda: driver.judged == 2)
        driver(_ctx(t))
        # "nova" was never configured: admitted on CONTEXT (the window the first
        # utterance opened), so heard — but not by name.
        assert driver.peek_name_mentioned() is False
    finally:
        driver.close()


def test_the_provider_seam_is_the_zero_arg_callable_composition_wires() -> None:
    """``as_name_mentioned_provider`` is the peek, exactly like ``as_provider``."""
    driver = _driver()
    try:
        provider = driver.as_name_mentioned_provider()
        assert provider == driver.peek_name_mentioned
        assert provider() is False
    finally:
        driver.close()
