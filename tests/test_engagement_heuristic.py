"""Characterization tests for today's ``_should_engage`` heuristic.

These tests PIN the CURRENT behavior of
:meth:`~reachy.motion.listen_transcribe.TranscribeHook._should_engage` exactly
as shipped (PR #54, v0.27.0) and demonstrate the flaw that the upcoming gate
change (t5/t6) is designed to fix.

Do NOT modify these to make them pass after the gate change — they are a
**baseline** that the new gate must beat on the ``ambient`` category.  The
contract:

*   ``test_heuristic_logic_matches_source`` — white-box: asserts the rule is
    exactly "whole-word name OR (coherent AND in-window)".
*   ``test_named_utterances_always_engage`` — verifies the happy path (every
    NAMED fixture engages, regardless of window state).
*   ``test_ambient_ignored_when_idle`` — verifies ambient lines are correctly
    rejected OUTSIDE the engage window.
*   ``test_ambient_flaw_inside_window`` — DEMONSTRATES THE BUG: an ambient
    coherent sentence is accepted by today's heuristic when the window is open,
    because the heuristic conflates "in-window" with "addressed to the robot".
*   ``test_misheard_name_does_not_engage`` — verifies STT phonetic corruptions
    of the robot's name do NOT trigger the name gate.
*   ``test_addressed_followup_engages_in_window`` — verifies that real
    follow-ups ARE accepted in-window (the behavior we want to PRESERVE).

All tests drive ``_should_engage`` directly — a pure method that takes only
``(text: str, t: float)`` — so there is no robot, daemon, network, or
threading involved.
"""

from __future__ import annotations

import re
from dataclasses import fields as _dc_fields

import pytest

from reachy.motion.listen_transcribe import TranscribeHook, TranscribeTuning
from tests.fixtures.engagement_transcripts import (
    ADDRESSED_FOLLOWUP,
    AMBIENT,
    MISHEARD_NAME,
    NAMED,
    TranscriptLine,
    by_category,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

#: Module-level word regex — must match the one in listen_transcribe (copied
#  here so the characterization test can verify the word-count independently).
_WORD_RE = re.compile(r"[A-Za-z]+(?:'[A-Za-z]+)?")

#: Default constructor values (mirrors TranscribeHook.__init__ defaults).
_DEFAULT_NAMES = ("reachy", "robot")
_DEFAULT_MIN_WORDS = 3
_DEFAULT_ENGAGE_WINDOW = 20.0

#: Field names of TranscribeTuning — used to split tuning kwargs from seam kwargs
#: at the test helper below (S107 split: the constructor now takes one grouped
#: ``tuning=`` object instead of nine individual numeric parameters).
_TUNING_FIELDS = {f.name for f in _dc_fields(TranscribeTuning)}


def _pop_tuning(kwargs: dict) -> TranscribeTuning:
    """Pop any TranscribeTuning-field keys out of *kwargs* and build a TranscribeTuning."""
    return TranscribeTuning(**{k: kwargs.pop(k) for k in list(kwargs) if k in _TUNING_FIELDS})


class _FakeBuffer:
    """Minimal EventBuffer stand-in (we never call feed_transcript in these tests)."""

    def feed_transcript(self, text: str, *, direction: str | None = None) -> None:  # noqa: ARG002
        pass


def _make_hook(**kwargs) -> TranscribeHook:
    """Build a TranscribeHook suitable for driving ``_should_engage`` directly.

    Injects a trivially-failing transcriber (we never drive a full tick here —
    we only call ``_should_engage`` directly on the constructed object).
    """

    class _NullTranscriber:
        def transcribe_once(self, audio):  # noqa: ARG002
            return None

    kwargs.setdefault("min_utterance_s", 0.0)
    tuning = _pop_tuning(kwargs)
    return TranscribeHook(
        lambda: None,
        buffer=_FakeBuffer(),
        transcriber=_NullTranscriber(),
        tuning=tuning,
        **kwargs,
    )


def _stamp_window(hook: TranscribeHook, last_accepted_t: float) -> None:
    """Simulate a previous accepted utterance at *last_accepted_t*.

    Directly stamps ``_engaged_until`` the same way ``_flush`` does so that
    follow-up / ambient tests can exercise the in-window branch without
    running a full utterance through the loop.
    """
    hook._engaged_until = last_accepted_t + hook._engage_window_s


# ---------------------------------------------------------------------------
# 1. White-box: pin the exact rule as shipped
# ---------------------------------------------------------------------------


def test_heuristic_logic_matches_source() -> None:
    """Pin TODAY's exact ``_should_engage`` rule.

    The rule (from ``reachy/motion/listen_transcribe.py`` ~lines 293-309):

        words = _WORD_RE.findall(text.lower())
        if any(name in words for name in self._names):
            return True
        coherent = len(words) >= self._min_words
        return coherent and t < self._engaged_until

    We verify this *exactly* by probing four representative points:
    1. Named (regardless of window)   → True.
    2. Short fragment (no name)       → False (even in-window).
    3. Coherent + in-window (no name) → True  (the ambient-flaw case).
    4. Coherent + out-of-window       → False.
    """
    hook = _make_hook()  # defaults: names=("reachy","robot"), min_words=3, window=20s

    # Stamp the window so tests 1 and 3 can probe the in-window branch.
    _stamp_window(hook, last_accepted_t=0.0)  # window open until t=20.0
    now_in_window = 5.0
    now_out_of_window = 25.0

    # 1. Named — always True.
    assert (
        hook._should_engage("reachy what time is it", now_out_of_window) is True
    ), "a named utterance must engage regardless of window state"

    # 2. Short fragment, no name, in-window.
    assert (
        hook._should_engage("uh yeah", now_in_window) is False
    ), "a short fragment (< min_words) must not engage even in-window"

    # 3. Coherent + in-window, no name → True today (this is the flaw).
    assert (
        hook._should_engage("the weather looks nice today", now_in_window) is True
    ), "today's heuristic accepts any coherent in-window utterance — even ambient"

    # 4. Coherent + out-of-window → False.
    assert (
        hook._should_engage("the weather looks nice today", now_out_of_window) is False
    ), "a coherent utterance outside the window is correctly rejected"


def test_name_match_is_whole_word() -> None:
    """Verify the name gate is whole-word, not substring.

    'robotic' contains 'robot' but must NOT match (the word list after
    ``_WORD_RE.findall`` is ['robotic'] — 'robot' is not an element).
    """
    hook = _make_hook()

    assert (
        hook._should_engage("the robotic arm is moving", 0.0) is False
    ), "'robotic' contains 'robot' as a substring — must not engage when idle"
    assert (
        hook._should_engage("robots are fascinating machines", 0.0) is False
    ), "'robots' is not the canonical whole-word 'robot' — must not engage when idle"
    assert (
        hook._should_engage("robot please turn around", 0.0) is True
    ), "the whole word 'robot' must engage"


# ---------------------------------------------------------------------------
# 2. Named fixtures: always engage (whole-word name gate, idle baseline)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", NAMED, ids=[ln.text[:40] for ln in NAMED])
def test_named_utterances_always_engage(line: TranscriptLine) -> None:
    """Every NAMED fixture must engage, even with no prior conversation (t=0)."""
    hook = _make_hook()  # _engaged_until = 0.0 → window closed
    assert (
        hook._should_engage(line.text, 0.0) is True
    ), f"NAMED utterance should always engage; got False for: {line.text!r}"


# ---------------------------------------------------------------------------
# 3. Ambient ignored when idle (out-of-window) — this part is CORRECT today
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", AMBIENT, ids=[ln.text[:40] for ln in AMBIENT])
def test_ambient_ignored_when_idle(line: TranscriptLine) -> None:
    """Every AMBIENT fixture is correctly ignored when there is no open window.

    This half of the ambient behavior is already correct — the flaw only
    manifests INSIDE the engage window (see ``test_ambient_flaw_inside_window``).
    """
    hook = _make_hook()  # _engaged_until = 0.0 → window closed
    assert (
        hook._should_engage(line.text, 1.0) is False
    ), f"ambient utterance outside window must be rejected; got True for: {line.text!r}"


# ---------------------------------------------------------------------------
# 4. THE FLAW: ambient coherent lines are accepted inside the window
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [ln for ln in AMBIENT if len(_WORD_RE.findall(ln.text)) >= _DEFAULT_MIN_WORDS],
    ids=[ln.text[:40] for ln in AMBIENT if len(_WORD_RE.findall(ln.text)) >= _DEFAULT_MIN_WORDS],
)
def test_ambient_flaw_inside_window(line: TranscriptLine) -> None:
    """Demonstrate today's heuristic flaw: ambient chatter engages inside the window.

    After the robot responds to "reachy hello" (window opens), a nearby
    human-to-human conversation like "the weather looks nice today" will be
    passed to cognition because the current heuristic only checks:

        coherent (>= 3 words) AND t < _engaged_until

    It does NOT check whether the utterance is actually addressed to the robot.
    This is the specific behavior the upcoming engagement-gate (t5/t6) will fix.

    This test is INTENTIONALLY expected to pass (i.e., the flaw is confirmed to
    exist).  Do NOT change it to ``is False`` — that belongs in the t6 test
    that verifies the new gate.
    """
    hook = _make_hook()
    _stamp_window(hook, last_accepted_t=0.0)  # simulate a prior exchange at t=0
    t_ambient = 5.0  # 5 s later, well inside the 20 s window

    result = hook._should_engage(line.text, t_ambient)
    assert result is True, (
        f"BUG CONFIRMED: ambient line accepted in-window for: {line.text!r}\n"
        "This is the flaw the new gate must fix."
    )


# ---------------------------------------------------------------------------
# 5. Misheard name does NOT engage (name gate is exact, not fuzzy — today)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", MISHEARD_NAME, ids=[ln.text[:40] for ln in MISHEARD_NAME])
def test_misheard_name_does_not_engage_when_idle(line: TranscriptLine) -> None:
    """Phonetic corruptions of the robot name do NOT trigger today's name gate.

    The name check is ``name in words`` (exact whole-word match on the lowercased
    canonical names list).  'richie', 'reachie', 'peachy', etc. are not in that
    list, so a misheard utterance is treated like any other text — only the
    word-count + window branch can accept it.

    Checked here in the idle (out-of-window) state, so even the coherence branch
    cannot save them.
    """
    hook = _make_hook()  # _engaged_until = 0.0 → window closed
    assert (
        hook._should_engage(line.text, 1.0) is False
    ), f"misheard name must not engage when idle; got True for: {line.text!r}"


# ---------------------------------------------------------------------------
# 6. Addressed follow-ups engage in-window (behavior to PRESERVE)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "line",
    [ln for ln in ADDRESSED_FOLLOWUP if len(_WORD_RE.findall(ln.text)) >= _DEFAULT_MIN_WORDS],
    ids=[
        ln.text[:40]
        for ln in ADDRESSED_FOLLOWUP
        if len(_WORD_RE.findall(ln.text)) >= _DEFAULT_MIN_WORDS
    ],
)
def test_addressed_followup_engages_in_window(line: TranscriptLine) -> None:
    """Real follow-ups (coherent, no name) are accepted inside the window.

    This is the INTENDED behavior that the new gate must continue to support.
    After the robot is addressed by name it opens a conversation window; clear
    follow-up sentences within that window should still engage.

    Today's heuristic passes this test; the new gate must not regress it.
    """
    hook = _make_hook()
    _stamp_window(hook, last_accepted_t=0.0)  # window open until t=20.0
    t_followup = 8.0  # 8 s later, inside the window

    assert (
        hook._should_engage(line.text, t_followup) is True
    ), f"addressed follow-up in-window must engage; got False for: {line.text!r}"


# ---------------------------------------------------------------------------
# 7. Fixtures module sanity — the imported fixture set is coherent
# ---------------------------------------------------------------------------


def test_fixture_set_has_all_categories() -> None:
    """The fixture module exports at least one line per required category."""
    assert len(by_category("ambient")) >= 5, "need at least 5 ambient lines"
    assert len(by_category("named")) >= 4, "need at least 4 named lines"
    assert len(by_category("addressed_followup")) >= 4, "need at least 4 follow-up lines"
    assert len(by_category("misheard_name")) >= 4, "need at least 4 misheard lines"


def test_named_fixtures_contain_canonical_name() -> None:
    """Every NAMED fixture must include at least one canonical name as a whole word."""
    names = set(_DEFAULT_NAMES)
    for line in NAMED:
        words = set(_WORD_RE.findall(line.text.lower()))
        assert words & names, f"NAMED fixture has no canonical name: {line.text!r}"


def test_ambient_fixtures_contain_no_canonical_name() -> None:
    """No AMBIENT fixture should contain a canonical robot name (defeats the purpose)."""
    names = set(_DEFAULT_NAMES)
    for line in AMBIENT:
        words = set(_WORD_RE.findall(line.text.lower()))
        assert not (
            words & names
        ), f"AMBIENT fixture contains a robot name (wrong category): {line.text!r}"


def test_misheard_name_fixtures_contain_no_canonical_name() -> None:
    """No MISHEARD_NAME fixture should contain the real canonical name."""
    names = set(_DEFAULT_NAMES)
    for line in MISHEARD_NAME:
        words = set(_WORD_RE.findall(line.text.lower()))
        assert not (
            words & names
        ), f"MISHEARD_NAME fixture contains a real canonical name: {line.text!r}"
