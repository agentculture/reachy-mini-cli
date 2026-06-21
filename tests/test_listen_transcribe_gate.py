"""Tests for the layered engagement gate wired into ``TranscribeHook`` (t6).

t5 built the engagement engine (``reachy.speech.engagement.decide_engagement`` +
``EngagementClassifier``).  t6 wires it into the transcribe hook behind an
injected ``classifier=`` seam, with two safety properties:

* an **escape hatch** — ``REACHY_ENGAGE_HEURISTIC=1`` (or any classifier left
  un-injected) forces the pure ``_should_engage`` heuristic and makes **zero**
  classifier calls; and
* a **graceful fallback** — a classifier that raises (``DEGRADE``) drops back to
  the heuristic so the hearing loop never stalls.

These tests drive the new ``_decide`` flow (and the full ``_flush`` path through
it) with fakes — no robot, no daemon, no network, no real STT, no real LLM.  The
per-utterance decision label is asserted via the module logger (caplog).

The shared fixture set (``tests/fixtures/engagement_transcripts.py``) is the same
one t3's characterization tests and t5's engine tests use, so the gate is proven
on identical transcripts:

* NAMED / MISHEARD → engage (name fast-path, no classifier call needed for the
  canonical names; the classifier catches the fuzzy mishearings that fall
  through ``is_name_match``),
* AMBIENT stays quiet under the LLM gate (the in-window flaw t3 pinned is fixed),
* ADDRESSED_FOLLOWUP engages via the classifier verdict,
* a forced-FAILING classifier keeps hearing (degrades to the heuristic).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence

import numpy as np
import pytest

from reachy.motion.listen_transcribe import TranscribeHook
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample
from reachy.speech.name_match import is_name_match
from tests.fixtures.engagement_transcripts import (
    ADDRESSED_FOLLOWUP,
    AMBIENT,
    MISHEARD_NAME,
    NAMED,
    TranscriptLine,
)

# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _RecordingBuffer:
    """A minimal :class:`EventBuffer` look-alike recording fed transcripts."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []
        self.directions: list[str | None] = []

    def feed_transcript(self, text: str, *, direction: str | None = None) -> None:
        self.transcripts.append(text)
        self.directions.append(direction)


class _FakeTranscriber:
    """Returns canned transcripts (one per ``transcribe_once`` call)."""

    def __init__(self, results: list | None = None) -> None:
        self.once_calls: list[np.ndarray] = []
        self._results = list(results or [])

    def transcribe_once(self, audio: np.ndarray):
        self.once_calls.append(audio)
        if self._results:
            return self._results.pop(0)
        return None


class _RecordingClassifier:
    """A fake classifier with a fixed verdict that records every ``judge`` call."""

    def __init__(self, verdict: bool):
        self._verdict = verdict
        self.calls: list[tuple[str, Sequence[str]]] = []

    def judge(self, text: str, context: Sequence[str]) -> bool:
        self.calls.append((text, tuple(context)))
        return self._verdict


class _CategoryClassifier:
    """A fake classifier whose verdict depends on the fixture category.

    ENGAGE (YES) for any ADDRESSED_FOLLOWUP or MISHEARD_NAME line that reaches it
    (the canonical NAMED lines never reach it — they short-circuit on the name
    fast-path); NO for everything else (AMBIENT).  Records its calls.
    """

    def __init__(self) -> None:
        self.calls: list[tuple[str, Sequence[str]]] = []
        yes = {ln.text for ln in (*ADDRESSED_FOLLOWUP, *MISHEARD_NAME)}
        self._yes = yes

    def judge(self, text: str, context: Sequence[str]) -> bool:
        self.calls.append((text, tuple(context)))
        return text in self._yes


class _RaisingClassifier:
    """A fake classifier that always raises — drives the DEGRADE → heuristic path."""

    def __init__(self, exc: BaseException | None = None) -> None:
        self.calls: list[tuple[str, Sequence[str]]] = []
        self._exc = exc or RuntimeError("classifier down")

    def judge(self, text: str, context: Sequence[str]) -> bool:
        self.calls.append((text, tuple(context)))
        raise self._exc


def _audio(n: int = 256) -> np.ndarray:
    return np.full(n, 0.05, dtype=np.float32)


def _make_hook(**kwargs):
    """Build a hook driving ``_decide``/``_flush`` directly (provider returns None)."""
    buffer = kwargs.pop("buffer", None) or _RecordingBuffer()
    transcriber = kwargs.pop("transcriber", None) or _FakeTranscriber()
    kwargs.setdefault("min_utterance_s", 0.0)
    hook = TranscribeHook(lambda: None, buffer=buffer, transcriber=transcriber, **kwargs)
    return hook, buffer, transcriber


def _make_driven_hook(*, transcriber=None, buffer=None, **kwargs):
    """A hook reading a mutable holder so a test can feed speech then a pause."""
    kwargs.setdefault("min_utterance_s", 0.0)
    holder: dict = {"s": None}
    buffer = buffer or _RecordingBuffer()
    transcriber = transcriber or _FakeTranscriber()
    hook = TranscribeHook(lambda: holder["s"], buffer=buffer, transcriber=transcriber, **kwargs)
    return hook, buffer, transcriber, holder


def _utterance(hook, holder, *, t_speech=0.0, t_pause=1.0, doa=10.0, audio=None) -> None:
    """Drive ONE utterance: a speech tick, then a pause tick that flushes."""
    chunk = _audio() if audio is None else audio
    holder["s"] = SenseSample(rms=0.1, doa=doa, speech=True, ts=t_speech, audio=chunk)
    hook(object(), MotionQueue(), t_speech, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=doa, speech=False, ts=t_pause, audio=None)
    hook(object(), MotionQueue(), t_pause, {"pitch": 0.0, "yaw": 0.0})


def _stamp_window(hook: TranscribeHook, last_accepted_t: float) -> None:
    hook._engaged_until = last_accepted_t + hook._engage_window_s


# ---------------------------------------------------------------------------
# 1. Default (no classifier) == byte-identical heuristic, zero classifier calls
# ---------------------------------------------------------------------------


def test_no_classifier_is_pure_heuristic() -> None:
    """With no classifier injected, ``_decide`` is exactly ``_should_engage``."""
    hook, _buffer, _tr = _make_hook()
    _stamp_window(hook, last_accepted_t=0.0)  # window open until 20.0

    # Named — engage regardless of window.
    assert hook._decide("reachy what time is it", 25.0) is True
    # Short fragment — drop.
    assert hook._decide("uh yeah", 5.0) is False
    # Coherent in-window (the heuristic's known accept) — engage (pure heuristic).
    assert hook._decide("the weather looks nice today", 5.0) is True
    # Coherent out-of-window — drop.
    assert hook._decide("the weather looks nice today", 25.0) is False


def test_should_engage_unchanged_by_t6() -> None:
    """``_should_engage`` is PRESERVED verbatim — t3's pin must still hold here too."""
    hook, _buffer, _tr = _make_hook()
    _stamp_window(hook, last_accepted_t=0.0)
    assert hook._should_engage("reachy hello", 25.0) is True
    assert hook._should_engage("uh yeah", 5.0) is False
    assert hook._should_engage("the weather looks nice today", 5.0) is True
    assert hook._should_engage("the weather looks nice today", 25.0) is False


# ---------------------------------------------------------------------------
# 2. Escape hatch: REACHY_ENGAGE_HEURISTIC forces heuristic, NO classifier call
# ---------------------------------------------------------------------------


def test_escape_hatch_forces_heuristic_no_classifier_call(monkeypatch) -> None:
    """``REACHY_ENGAGE_HEURISTIC=1`` ignores the injected classifier entirely."""
    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", "1")
    classifier = _RecordingClassifier(verdict=False)  # would DROP everything
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)

    # Coherent in-window: the heuristic accepts; the classifier (which would say
    # NO) is NEVER consulted.
    assert hook._decide("the weather looks nice today", 5.0) is True
    assert classifier.calls == [], "escape hatch must make ZERO classifier calls"


def test_llm_gate_path_consults_classifier() -> None:
    """Without the escape hatch and WITH a classifier, the classifier decides."""
    classifier = _RecordingClassifier(verdict=False)  # NO → drop
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)

    # Same coherent in-window line the heuristic would accept — but the LLM gate
    # is in force and says NO, so it is DROPPED (the ambient flaw is fixed).
    assert hook._decide("the weather looks nice today", 5.0) is False
    assert len(classifier.calls) == 1, "the LLM-gate path must consult the classifier once"


@pytest.mark.parametrize("truthy", ["1", "true", "TRUE", "yes", "on"], ids=lambda v: f"env={v!r}")
def test_escape_hatch_truthy_strings(monkeypatch, truthy: str) -> None:
    """Common truthy strings all force the heuristic."""
    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", truthy)
    classifier = _RecordingClassifier(verdict=False)
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)
    assert hook._decide("the weather looks nice today", 5.0) is True
    assert classifier.calls == []


@pytest.mark.parametrize("falsey", ["0", "", "false", "no"], ids=lambda v: f"env={v!r}")
def test_escape_hatch_falsey_strings_use_classifier(monkeypatch, falsey: str) -> None:
    """Falsey / unset values leave the LLM gate active (classifier is consulted)."""
    monkeypatch.setenv("REACHY_ENGAGE_HEURISTIC", falsey)
    classifier = _RecordingClassifier(verdict=False)
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)
    assert hook._decide("the weather looks nice today", 5.0) is False
    assert len(classifier.calls) == 1


# ---------------------------------------------------------------------------
# 3. DEGRADE → heuristic fallback (the loop never stalls)
# ---------------------------------------------------------------------------


def test_raising_classifier_degrades_to_heuristic() -> None:
    """A classifier that raises maps to DEGRADE → ``_should_engage`` fallback."""
    classifier = _RaisingClassifier()
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)

    # Coherent in-window: classifier raises → DEGRADE → heuristic engages.
    assert hook._decide("the weather looks nice today", 5.0) is True
    assert len(classifier.calls) == 1, "the classifier was attempted before degrading"

    # Coherent out-of-window: classifier raises → DEGRADE → heuristic drops.
    assert hook._decide("the weather looks nice today", 25.0) is False


def test_raising_classifier_keeps_loop_alive_through_flush() -> None:
    """A raising classifier never escapes ``_flush`` — the utterance is processed."""
    transcriber = _FakeTranscriber(results=["the weather looks nice today"])
    classifier = _RaisingClassifier()
    hook, buffer, _tr, holder = _make_driven_hook(transcriber=transcriber, classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)  # window open → heuristic will engage
    # No exception should escape; the heuristic fallback engages the coherent line.
    _utterance(hook, holder, t_speech=0.5, t_pause=1.5)
    assert buffer.transcripts == ["the weather looks nice today"]
    assert hook.transcripts == 1
    assert len(classifier.calls) == 1


# ---------------------------------------------------------------------------
# 4. Fixture-driven proof over the shared transcript set
# ---------------------------------------------------------------------------

# Partition MISHEARD by what the fuzzy matcher catches vs. what falls to the LLM.
_MISHEARD_FUZZY = [ln for ln in MISHEARD_NAME if is_name_match(ln.text, ("reachy", "robot"))]
_MISHEARD_LLM = [ln for ln in MISHEARD_NAME if not is_name_match(ln.text, ("reachy", "robot"))]


@pytest.mark.parametrize("line", NAMED, ids=[ln.text[:40] for ln in NAMED])
def test_named_engage_via_name_fastpath_no_classifier(line: TranscriptLine) -> None:
    """NAMED lines engage on the name fast-path with NO classifier call."""
    classifier = _RecordingClassifier(verdict=False)  # would drop if consulted
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    assert hook._decide(line.text, 0.0) is True
    assert classifier.calls == [], f"NAMED must not reach the classifier: {line.text!r}"


@pytest.mark.parametrize("line", _MISHEARD_FUZZY, ids=[ln.text[:40] for ln in _MISHEARD_FUZZY])
def test_misheard_caught_by_fuzzy_match_no_classifier(line: TranscriptLine) -> None:
    """MISHEARD lines the fuzzy matcher catches engage with NO classifier call."""
    classifier = _RecordingClassifier(verdict=False)
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    assert hook._decide(line.text, 0.0) is True
    assert classifier.calls == [], f"fuzzy-caught misheard must not reach classifier: {line.text!r}"


@pytest.mark.parametrize("line", _MISHEARD_LLM, ids=[ln.text[:40] for ln in _MISHEARD_LLM])
def test_misheard_falling_through_engages_via_classifier(line: TranscriptLine) -> None:
    """MISHEARD lines that fall through fuzzy-match engage via a YES classifier verdict."""
    classifier = _CategoryClassifier()  # says YES for misheard fixtures
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    assert hook._decide(line.text, 0.0) is True
    assert (
        len(classifier.calls) == 1
    ), f"a fall-through misheard must consult the classifier: {line.text!r}"


@pytest.mark.parametrize("line", AMBIENT, ids=[ln.text[:40] for ln in AMBIENT])
def test_ambient_stays_quiet_under_llm_gate(line: TranscriptLine) -> None:
    """AMBIENT lines are DROPPED under the LLM gate — even inside the window.

    This is the flaw t3 pinned (`test_ambient_flaw_inside_window`) now FIXED: a
    coherent in-window ambient line that the heuristic would accept is dropped
    because the classifier judges it not-addressed.
    """
    classifier = _CategoryClassifier()  # says NO for ambient fixtures
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)  # window open: heuristic WOULD accept
    assert (
        hook._decide(line.text, 5.0) is False
    ), f"ambient line must be dropped under the LLM gate: {line.text!r}"


@pytest.mark.parametrize(
    "line", ADDRESSED_FOLLOWUP, ids=[ln.text[:40] for ln in ADDRESSED_FOLLOWUP]
)
def test_addressed_followup_engages_via_classifier(line: TranscriptLine) -> None:
    """ADDRESSED_FOLLOWUP lines engage via a positive classifier verdict."""
    classifier = _CategoryClassifier()  # says YES for follow-ups
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    assert hook._decide(line.text, 0.0) is True
    assert len(classifier.calls) == 1, f"a follow-up must consult the classifier: {line.text!r}"


@pytest.mark.parametrize("line", AMBIENT, ids=[ln.text[:40] for ln in AMBIENT])
def test_failing_classifier_keeps_hearing_over_fixtures(line: TranscriptLine) -> None:
    """A forced-FAILING classifier degrades to the heuristic for every fixture.

    Out-of-window (no open conversation) the heuristic drops everything, so the
    loop stays quiet AND never raises — the degrade path is exercised on every
    ambient line without an exception escaping.
    """
    classifier = _RaisingClassifier()
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    # Out of window → heuristic drops; the point is the call does not raise.
    assert hook._decide(line.text, 100.0) is False
    assert len(classifier.calls) == 1


# ---------------------------------------------------------------------------
# 5. Observability — the per-utterance decision is logged with a label
# ---------------------------------------------------------------------------


def test_decision_label_name(caplog) -> None:
    classifier = _RecordingClassifier(verdict=True)
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    with caplog.at_level(logging.INFO, logger="reachy.motion.listen_transcribe"):
        assert hook._decide("reachy what time is it", 0.0) is True
    assert any("name" in rec.getMessage() for rec in caplog.records), caplog.text


def test_decision_label_context(caplog) -> None:
    classifier = _RecordingClassifier(verdict=True)  # YES, no name → "context"
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    with caplog.at_level(logging.INFO, logger="reachy.motion.listen_transcribe"):
        assert hook._decide("what do you think about that", 0.0) is True
    assert any("context" in rec.getMessage() for rec in caplog.records), caplog.text


def test_decision_label_dropped(caplog) -> None:
    classifier = _RecordingClassifier(verdict=False)  # NO → dropped
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    with caplog.at_level(logging.INFO, logger="reachy.motion.listen_transcribe"):
        assert hook._decide("the weather looks nice today", 0.0) is False
    assert any("dropped" in rec.getMessage() for rec in caplog.records), caplog.text


def test_decision_label_degrade(caplog) -> None:
    classifier = _RaisingClassifier()
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    _stamp_window(hook, last_accepted_t=0.0)
    with caplog.at_level(logging.INFO, logger="reachy.motion.listen_transcribe"):
        assert hook._decide("the weather looks nice today", 5.0) is True
    assert any("degrade" in rec.getMessage().lower() for rec in caplog.records), caplog.text


# ---------------------------------------------------------------------------
# 6. History context accumulates and is passed to the classifier
# ---------------------------------------------------------------------------


def test_history_passed_as_context_and_grows_on_engage() -> None:
    """Accepted utterances accumulate and are passed as ``context`` to the classifier."""
    classifier = _RecordingClassifier(verdict=True)  # everything engages
    hook, _buffer, _tr = _make_hook(classifier=classifier)

    # First non-name utterance: empty context, then it is appended to history.
    assert hook._decide("tell me more about that", 0.0) is True
    assert classifier.calls[0][1] == (), "first decision sees empty context"

    # Second: the first accepted utterance is now in the context.
    assert hook._decide("how does that make you feel", 1.0) is True
    assert classifier.calls[1][1] == ("tell me more about that",)


def test_history_not_grown_on_drop() -> None:
    """A DROP decision must NOT append to the conversation history."""
    classifier = _RecordingClassifier(verdict=False)
    hook, _buffer, _tr = _make_hook(classifier=classifier)
    assert hook._decide("the weather looks nice today", 0.0) is False
    assert hook._decide("did you see the game last night", 1.0) is False
    assert classifier.calls[1][1] == (), "a dropped utterance must not enter history"


# ---------------------------------------------------------------------------
# 7. on_engage seam (t7): the gate's ENGAGE decision fires the motion-ladder
#    signal exactly once per engage — and NEVER on a drop/degrade-to-drop.
# ---------------------------------------------------------------------------


def test_on_engage_fires_once_on_named_engage() -> None:
    """An addressed (named) utterance fires ``on_engage`` exactly once, via ``_flush``."""
    fired = {"n": 0}
    transcriber = _FakeTranscriber(results=["reachy what time is it"])  # names the robot
    hook, buffer, _tr, holder = _make_driven_hook(
        transcriber=transcriber, on_engage=lambda: fired.__setitem__("n", fired["n"] + 1)
    )
    _utterance(hook, holder, t_speech=0.5, t_pause=1.5)
    assert buffer.transcripts == ["reachy what time is it"], "the named utterance must engage"
    assert fired["n"] == 1, "on_engage must fire exactly once when the gate engages"


def test_on_engage_does_not_fire_on_drop() -> None:
    """A dropped (ambient, un-addressed) utterance must NOT fire ``on_engage``."""
    fired = {"n": 0}
    # Coherent line, but the LLM gate says NO (ambient) → dropped → no turn.
    classifier = _RecordingClassifier(verdict=False)
    transcriber = _FakeTranscriber(results=["the weather looks nice today"])
    hook, buffer, _tr, holder = _make_driven_hook(
        transcriber=transcriber,
        classifier=classifier,
        on_engage=lambda: fired.__setitem__("n", fired["n"] + 1),
    )
    _utterance(hook, holder, t_speech=0.5, t_pause=1.5)
    assert buffer.transcripts == [], "an ambient utterance must be dropped (no cue fed)"
    assert fired["n"] == 0, "a dropped utterance must NEVER latch an engaged turn (no barge-in)"


def test_on_engage_does_not_fire_on_degrade_to_drop() -> None:
    """A DEGRADE that the heuristic then DROPS must NOT fire ``on_engage``.

    A raising classifier degrades to the heuristic; out of the conversation window
    a coherent-but-unnamed line is dropped — so no engaged turn fires.
    """
    fired = {"n": 0}
    classifier = _RaisingClassifier()  # → DEGRADE → heuristic
    transcriber = _FakeTranscriber(results=["the weather looks nice today"])
    hook, buffer, _tr, holder = _make_driven_hook(
        transcriber=transcriber,
        classifier=classifier,
        on_engage=lambda: fired.__setitem__("n", fired["n"] + 1),
    )
    # Out of window (no open conversation) → heuristic drops the unnamed coherent line.
    _utterance(hook, holder, t_speech=100.0, t_pause=101.0)
    assert buffer.transcripts == [], "degrade-to-drop must feed nothing"
    assert fired["n"] == 0, "a degrade-to-drop must not latch an engaged turn"


def test_on_engage_fault_never_kills_the_flush() -> None:
    """A raising ``on_engage`` is swallowed — the words still reach cognition."""

    def _boom() -> None:
        raise RuntimeError("set_engaged blew up")

    transcriber = _FakeTranscriber(results=["reachy hello there"])  # named → engage
    hook, buffer, _tr, holder = _make_driven_hook(transcriber=transcriber, on_engage=_boom)
    # The raising callback must not escape _flush; the transcript still gets fed.
    _utterance(hook, holder, t_speech=0.5, t_pause=1.5)
    assert buffer.transcripts == ["reachy hello there"], "a callback fault must not block the words"
    assert hook.transcripts == 1


def test_no_on_engage_is_a_noop_default() -> None:
    """The default (no ``on_engage``) engages normally with no callback — byte-identical."""
    transcriber = _FakeTranscriber(results=["reachy what time is it"])
    hook, buffer, _tr, holder = _make_driven_hook(transcriber=transcriber)
    _utterance(hook, holder, t_speech=0.5, t_pause=1.5)
    assert buffer.transcripts == ["reachy what time is it"]
    assert hook.transcripts == 1
