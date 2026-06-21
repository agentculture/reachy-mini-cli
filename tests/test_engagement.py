"""Tests for the LLM engagement engine (``reachy.speech.engagement``).

The engagement engine decides whether a transcribed utterance is *addressed to
the robot* (engage) or is ambient human-to-human chatter (drop).  It is the
decision core of issue #55.  These tests drive it TDD-first and prove its
contract WITHOUT any real network:

*   A fuzzy name match (canonical name OR a common STT mishearing) short-circuits
    to ``ENGAGE`` with **zero** classifier calls.
*   Otherwise the single-shot LLM classifier is asked exactly once; a positive
    verdict → ``ENGAGE``, a negative verdict → ``DROP``.
*   A classifier that raises (timeout / network / parse) yields the sentinel
    ``DEGRADE`` — the caller's signal to fall back to a heuristic.

Every classifier is faked.  Fakes record their call count so the at-most-one /
zero-on-name-path guarantees are asserted directly, not inferred.
"""

from __future__ import annotations

import socket
import urllib.error
from collections.abc import Sequence

import pytest

from reachy.speech.engagement import (
    Decision,
    EngagementClassifier,
    decide_engagement,
)
from reachy.speech.name_match import is_name_match
from tests.fixtures.engagement_transcripts import (
    ADDRESSED_FOLLOWUP,
    AMBIENT,
    MISHEARD_NAME,
    NAMED,
    TranscriptLine,
)

# ---------------------------------------------------------------------------
# Fakes — record call counts so we can assert the at-most-one contract.
# ---------------------------------------------------------------------------


class _RecordingClassifier:
    """A fake classifier whose ``judge`` returns a fixed verdict and counts calls.

    Stands in for :class:`EngagementClassifier` so ``decide_engagement`` can be
    exercised without an LLM.  ``calls`` records every ``(text, context)`` pair
    so a test can assert *how many* classifier calls happened (zero on the
    name-match path, exactly one otherwise).
    """

    def __init__(self, verdict: bool):
        self._verdict = verdict
        self.calls: list[tuple[str, Sequence[str]]] = []

    def judge(self, text: str, context: Sequence[str]) -> bool:
        self.calls.append((text, tuple(context)))
        return self._verdict


class _RaisingClassifier:
    """A fake classifier whose ``judge`` always raises — drives the DEGRADE path."""

    def __init__(self, exc: BaseException):
        self._exc = exc
        self.calls: list[tuple[str, Sequence[str]]] = []

    def judge(self, text: str, context: Sequence[str]) -> bool:
        self.calls.append((text, tuple(context)))
        raise self._exc


class _RecordingComplete:
    """A fake ``complete_fn`` returning a canned string and recording its calls.

    Lets us build a real :class:`EngagementClassifier` (exercising its prompt
    assembly + response parsing) with no network.  ``messages`` captures the
    message list of each call so prompt-assembly assertions are possible.
    """

    def __init__(self, response: str):
        self._response = response
        self.messages: list[list[dict]] = []

    def __call__(self, messages, **kwargs):  # noqa: ANN001, ANN003
        self.messages.append(messages)
        return self._response


# ---------------------------------------------------------------------------
# Decision enum
# ---------------------------------------------------------------------------


def test_decision_has_three_members() -> None:
    """The result type is a 3-valued enum: ENGAGE / DROP / DEGRADE."""
    assert {m.name for m in Decision} == {"ENGAGE", "DROP", "DEGRADE"}


# ---------------------------------------------------------------------------
# 1. Name fast-path → ENGAGE with ZERO classifier calls (short-circuit)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", NAMED, ids=[ln.text[:40] for ln in NAMED])
def test_named_engages_without_classifier_call(line: TranscriptLine) -> None:
    """A canonical-name utterance engages on the fast-path; classifier untouched."""
    # The classifier is rigged to say NO — if it were ever consulted the result
    # would be DROP, so an ENGAGE here proves the name path short-circuited.
    classifier = _RecordingClassifier(verdict=False)
    decision = decide_engagement(line.text, [], classifier=classifier)
    assert decision is Decision.ENGAGE, f"NAMED line must engage: {line.text!r}"
    assert classifier.calls == [], "name match must NOT call the classifier"


# The MISHEARD_NAME fixtures split into two groups by what the t2 fuzzy matcher
# (with its first-letter "initial guard") actually accepts:
#   * fuzzy-caught — "richie"/"reachie"/"robbot": is_name_match → True, so they
#     engage on the fast-path with ZERO classifier calls (the c15 success cases);
#   * fuzzy-missed — "peachy" (initial-guard: starts 'p'), "ricky"/"robbie"
#     (score below 0.50): is_name_match → False, so they correctly fall THROUGH
#     to the classifier (exactly one call).  decide_engagement must not invent a
#     name match the matcher doesn't make.
_FUZZY_CAUGHT = [ln for ln in MISHEARD_NAME if is_name_match(ln.text)]
_FUZZY_MISSED = [ln for ln in MISHEARD_NAME if not is_name_match(ln.text)]


def test_misheard_partition_is_nonempty() -> None:
    """Sanity: the fixture set exercises BOTH the caught and missed branches."""
    assert _FUZZY_CAUGHT, "expected some misheard names the fuzzy matcher catches"
    assert _FUZZY_MISSED, "expected some misheard names the fuzzy matcher misses"


@pytest.mark.parametrize("line", _FUZZY_CAUGHT, ids=[ln.text[:40] for ln in _FUZZY_CAUGHT])
def test_misheard_name_caught_engages_via_fuzzy_path(line: TranscriptLine) -> None:
    """STT mishearings the fuzzy matcher catches engage with zero classifier calls.

    These are the c15 success-signal cases — the fuzzy ``is_name_match`` accepts
    "richie"/"reachie"/"robbot", so they short-circuit on the name fast-path.
    """
    classifier = _RecordingClassifier(verdict=False)
    decision = decide_engagement(line.text, [], classifier=classifier)
    assert decision is Decision.ENGAGE, f"misheard name must engage: {line.text!r}"
    assert classifier.calls == [], "fuzzy name match must NOT call the classifier"


@pytest.mark.parametrize("line", _FUZZY_MISSED, ids=[ln.text[:40] for ln in _FUZZY_MISSED])
def test_misheard_name_missed_falls_through_to_classifier(line: TranscriptLine) -> None:
    """Mishearings the fuzzy matcher does NOT catch fall through to the classifier.

    "peachy" (blocked by the initial guard), "ricky"/"robbie" (score < 0.50) are
    not name matches, so the layered gate correctly defers to the classifier —
    exactly one call — rather than fabricating a name match.  Here the (faked)
    classifier judges them addressed, so they engage; the point under test is the
    single fall-through call, not the verdict.
    """
    classifier = _RecordingClassifier(verdict=True)
    decision = decide_engagement(line.text, [], classifier=classifier)
    assert decision is Decision.ENGAGE
    assert len(classifier.calls) == 1, "a fuzzy-missed name must consult the classifier once"


# ---------------------------------------------------------------------------
# 2. No name → exactly ONE classifier call; verdict decides ENGAGE / DROP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line", ADDRESSED_FOLLOWUP, ids=[ln.text[:40] for ln in ADDRESSED_FOLLOWUP]
)
def test_addressed_followup_engages_on_yes(line: TranscriptLine) -> None:
    """A nameless follow-up engages iff the classifier says YES — one call."""
    classifier = _RecordingClassifier(verdict=True)
    context = ["reachy what time is it"]  # a prior named turn
    decision = decide_engagement(line.text, context, classifier=classifier)
    assert decision is Decision.ENGAGE, f"YES verdict must engage: {line.text!r}"
    assert len(classifier.calls) == 1, "exactly one classifier call expected"
    # The classifier sees the new utterance and the recent context.
    seen_text, seen_ctx = classifier.calls[0]
    assert seen_text == line.text
    assert seen_ctx == tuple(context)


@pytest.mark.parametrize("line", AMBIENT, ids=[ln.text[:40] for ln in AMBIENT])
def test_ambient_drops_on_no(line: TranscriptLine) -> None:
    """Ambient chatter is dropped when the classifier says NO — one call.

    This is the flaw the gate fixes: a coherent ambient line ("the weather looks
    nice today") that today's window heuristic would ACCEPT is correctly DROPPED
    because it is not addressed to the robot.
    """
    classifier = _RecordingClassifier(verdict=False)
    context = ["reachy hello"]  # window is open, yet ambient must still drop
    decision = decide_engagement(line.text, context, classifier=classifier)
    assert decision is Decision.DROP, f"NO verdict must drop: {line.text!r}"
    assert len(classifier.calls) == 1, "exactly one classifier call expected"


def test_at_most_one_classifier_call_on_drop() -> None:
    """The DROP path consults the classifier at most once (no retries)."""
    classifier = _RecordingClassifier(verdict=False)
    decide_engagement("could you help me with this spreadsheet", [], classifier=classifier)
    assert len(classifier.calls) == 1


# ---------------------------------------------------------------------------
# 3. Classifier raises / times out → DEGRADE (one attempt, then sentinel)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "exc",
    [
        urllib.error.URLError("connection refused"),
        socket.timeout("timed out"),
        TimeoutError("timed out"),
        OSError("network down"),
        ValueError("unparseable response"),
    ],
    ids=["urlerror", "sockettimeout", "timeouterror", "oserror", "valueerror"],
)
def test_classifier_failure_yields_degrade(exc: BaseException) -> None:
    """Any classifier failure (network / timeout / parse) → DEGRADE sentinel."""
    classifier = _RaisingClassifier(exc)
    decision = decide_engagement("tell me about quantum physics", [], classifier=classifier)
    assert decision is Decision.DEGRADE
    assert len(classifier.calls) == 1, "exactly one (failing) classifier attempt"


def test_degrade_does_not_run_a_heuristic() -> None:
    """DEGRADE is a pure sentinel — decide_engagement runs no fallback itself.

    Even for a clearly-coherent, in-conversation utterance (which a heuristic
    *would* accept), a raising classifier yields DEGRADE, never ENGAGE/DROP —
    proving the fallback is the caller's job, not this function's.
    """
    classifier = _RaisingClassifier(socket.timeout("timed out"))
    decision = decide_engagement(
        "what do you think about that",
        ["reachy tell me a joke"],  # window open, coherent follow-up
        classifier=classifier,
    )
    assert decision is Decision.DEGRADE


# ---------------------------------------------------------------------------
# 4. Tunable params: custom names / threshold still short-circuit
# ---------------------------------------------------------------------------


def test_custom_names_short_circuit() -> None:
    """A custom name list drives the fast-path too (zero classifier calls)."""
    classifier = _RecordingClassifier(verdict=False)
    decision = decide_engagement(
        "marvin are you listening",
        [],
        classifier=classifier,
        names=("marvin",),
    )
    assert decision is Decision.ENGAGE
    assert classifier.calls == []


def test_empty_context_is_accepted() -> None:
    """An empty context is valid input to the classifier path."""
    classifier = _RecordingClassifier(verdict=True)
    decision = decide_engagement("can you help me", [], classifier=classifier)
    assert decision is Decision.ENGAGE
    assert classifier.calls[0][1] == ()


# ---------------------------------------------------------------------------
# EngagementClassifier — prompt assembly + response parsing (faked complete_fn)
# ---------------------------------------------------------------------------


def test_classifier_parses_yes_as_true() -> None:
    """A response starting with YES (any case / punctuation) → True."""
    for resp in ("YES", "yes", "Yes.", "  YES, it is.\n", "yes — addressed"):
        complete = _RecordingComplete(resp)
        clf = EngagementClassifier(complete_fn=complete)
        assert clf.judge("can you help", []) is True, f"{resp!r} should parse True"


def test_classifier_parses_no_as_false() -> None:
    """A response NOT starting with YES → False."""
    for resp in ("NO", "no", "No, that's ambient.", "I don't think so", ""):
        complete = _RecordingComplete(resp)
        clf = EngagementClassifier(complete_fn=complete)
        assert clf.judge("the weather is nice", []) is False, f"{resp!r} should parse False"


def test_classifier_builds_system_and_user_messages() -> None:
    """``judge`` assembles a system + user message list for ``complete_fn``."""
    complete = _RecordingComplete("YES")
    clf = EngagementClassifier(complete_fn=complete)
    clf.judge("can you say that again", ["reachy hello", "hi there"])

    assert len(complete.messages) == 1
    messages = complete.messages[0]
    roles = [m["role"] for m in messages]
    assert roles[0] == "system"
    assert "user" in roles
    # System prompt encodes the addressed-vs-helpable distinction + YES/NO contract.
    system = messages[0]["content"]
    assert "Reachy" in system
    assert "YES" in system and "NO" in system
    # The user message carries the new utterance and the recent context.
    user = next(m["content"] for m in messages if m["role"] == "user")
    assert "can you say that again" in user
    assert "reachy hello" in user and "hi there" in user


def test_classifier_handles_empty_context() -> None:
    """An empty context still produces a valid call and parses normally."""
    complete = _RecordingComplete("NO")
    clf = EngagementClassifier(complete_fn=complete)
    assert clf.judge("did you see the game", []) is False
    assert len(complete.messages) == 1


def test_classifier_failure_propagates_from_judge() -> None:
    """If ``complete_fn`` raises, ``judge`` lets it propagate (caller wraps it).

    ``decide_engagement`` is what turns this into DEGRADE; ``judge`` itself does
    not swallow — mirroring ``llm.complete``'s own "raise, don't swallow" policy.
    """

    def boom(messages, **kwargs):  # noqa: ANN001, ANN003
        raise socket.timeout("timed out")

    clf = EngagementClassifier(complete_fn=boom)
    with pytest.raises((socket.timeout, OSError, TimeoutError)):
        clf.judge("anything", [])


def test_classifier_threads_tunables_to_complete_fn() -> None:
    """model / base_url / api_key / timeout are forwarded to ``complete_fn``."""
    captured: dict = {}

    def capture(messages, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return "YES"

    clf = EngagementClassifier(
        complete_fn=capture,
        model="m1",
        base_url="http://host:9",
        api_key="k1",
        timeout=3.0,
    )
    clf.judge("hi", [])
    assert captured["model"] == "m1"
    assert captured["base_url"] == "http://host:9"
    assert captured["api_key"] == "k1"
    assert captured["timeout"] == 3.0


def test_classifier_default_timeout_is_bounded() -> None:
    """The classifier uses a tight, bounded default timeout (<= 10 s)."""
    captured: dict = {}

    def capture(messages, **kwargs):  # noqa: ANN001, ANN003
        captured.update(kwargs)
        return "YES"

    clf = EngagementClassifier(complete_fn=capture)
    clf.judge("hi", [])
    assert captured["timeout"] is not None
    assert captured["timeout"] <= 10.0


def test_real_classifier_drops_ambient_engages_addressed() -> None:
    """End-to-end through EngagementClassifier with faked complete_fn.

    Proves decide_engagement composes with a *real* EngagementClassifier (not
    just the recording fake): a YES-returning endpoint engages an addressed
    follow-up; a NO-returning endpoint drops ambient chatter.
    """
    engaging = EngagementClassifier(complete_fn=_RecordingComplete("YES"))
    assert (
        decide_engagement("what do you think about that", ["reachy hi"], classifier=engaging)
        is Decision.ENGAGE
    )

    dropping = EngagementClassifier(complete_fn=_RecordingComplete("NO"))
    assert (
        decide_engagement("did you see the game last night", ["reachy hi"], classifier=dropping)
        is Decision.DROP
    )
