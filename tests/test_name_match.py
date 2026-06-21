"""Tests for reachy.speech.name_match — fuzzy robot-name detector.

Acceptance criteria:
  1. is_name_match() returns True for exact "reachy"/"robot" AND close
     mishearings ("reachie", "richy"/"richie" at the tuned threshold), and
     False for "reach", "rich", "preachy" and unrelated words.
  2. Pure stdlib — no numpy, no new runtime dependency.
  3. A table-driven test pins every accept/reject case above.
"""

from __future__ import annotations

import pytest

from reachy.speech.name_match import DEFAULT_THRESHOLD, is_name_match

# ---------------------------------------------------------------------------
# Acceptance criterion 1 — required accept / reject table
# ---------------------------------------------------------------------------

# Each entry: (utterance, expected_result, reason)
_REQUIRED_TABLE: list[tuple[str, bool, str]] = [
    # --- must accept ---
    ("reachy", True, "exact name match"),
    ("robot", True, "exact generic label match"),
    ("reachie", True, "common STT mishearing of 'reachy'"),
    ("richy", True, "STT mishearing: vowel swap"),
    ("richie", True, "phonetic mishearing: 'richie' ≈ 'reachy' at threshold 0.50"),
    # --- must reject ---
    ("reach", False, "strict prefix of 'reachy' — truncation, not mishearing"),
    ("rich", False, "too different from any name (score 0.40 < threshold 0.50)"),
    ("preachy", False, "'reachy' is a substring of 'preachy' — superstring guard"),
    ("hello", False, "completely unrelated word"),
]


@pytest.mark.parametrize(
    "text,expected,reason", _REQUIRED_TABLE, ids=[r[0] for r in _REQUIRED_TABLE]
)
def test_required_accept_reject_table(text: str, expected: bool, reason: str) -> None:
    """Pin every required accept/reject case from the task spec."""
    result = is_name_match(text)
    assert result == expected, f"is_name_match({text!r}) = {result}, want {expected}: {reason}"


# ---------------------------------------------------------------------------
# Additional reject cases — superstring / morphological extensions
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["robots", "robotics", "robotic"])
def test_robot_extensions_rejected(text: str) -> None:
    """'robot' is a substring of 'robots'/'robotics' — superstring guard applies."""
    assert (
        is_name_match(text) is False
    ), f"'{text}' contains 'robot' as a substring; superstring guard must reject it"


# ---------------------------------------------------------------------------
# Sentence-level tests — name embedded in natural speech
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("hey reachy what time is it", True),
        ("hey richie how are you", True),
        ("reachie turn around please", True),
        ("okay robot can you move", True),
        ("i need to reach the shelf", False),
        # "speech" ties "richie" on the raw score but starts with 's', so the
        # initial guard rejects it — critical, since this is a hearing feature.
        ("let me give a speech about this", False),
        # "preachy" — caught by the superstring guard.
        ("this is preachy nonsense", False),
        ("the robotics competition starts tomorrow", False),
    ],
)
def test_sentence_cases(text: str, expected: bool) -> None:
    """Name match works on full utterances, not just isolated words."""
    result = is_name_match(text)
    assert result == expected, f"is_name_match({text!r}) = {result}, want {expected}"


# ---------------------------------------------------------------------------
# Additional mishearing variants — coverage for plausible STT errors
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["reachee", "reachi", "richey", "reechy", "rachy"])
def test_additional_mishearings_accepted(text: str) -> None:
    """Other plausible STT mishearings of 'reachy' are accepted."""
    assert is_name_match(text) is True, f"'{text}' should be accepted as a mishearing of 'reachy'"


@pytest.mark.parametrize("text", ["robo", "roboto", "wreachy"])
def test_non_mishearings_rejected(text: str) -> None:
    """Truncations and unrelated look-alikes are rejected."""
    assert is_name_match(text) is False, f"'{text}' should be rejected (truncation / unrelated)"


# ---------------------------------------------------------------------------
# Case-insensitivity
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", ["Reachy", "REACHY", "Robot", "ROBOT", "Richie", "RICHIE"])
def test_case_insensitive(text: str) -> None:
    """Matching is case-insensitive."""
    assert is_name_match(text) is True, f"'{text}' should match regardless of case"


# ---------------------------------------------------------------------------
# Custom names parameter
# ---------------------------------------------------------------------------


def test_custom_names_accepted() -> None:
    """Caller can supply custom names to match against."""
    assert is_name_match("nova", names=("nova",)) is True


def test_custom_names_rejects_defaults() -> None:
    """When custom names are supplied, default names no longer match."""
    assert is_name_match("reachy", names=("nova",)) is False


# ---------------------------------------------------------------------------
# Custom threshold parameter
# ---------------------------------------------------------------------------


def test_high_threshold_rejects_richie() -> None:
    """At threshold=0.60, 'richie' (score 0.50) is rejected."""
    assert is_name_match("richie", threshold=0.60) is False


def test_low_threshold_accepts_more() -> None:
    """At threshold=0.30, even 'rich' (score 0.40) is accepted."""
    assert is_name_match("rich", threshold=0.30) is True


@pytest.mark.parametrize("text", ["speech", "each", "beach", "preach", "leech"])
def test_initial_guard_rejects_same_score_collisions(text: str) -> None:
    """Same-length words that tie 'richie' on the raw similarity score but start
    with a different letter are rejected by the initial guard.

    "speech" scores 0.500 against "reachy" — identical to the required "richie"
    accept — but begins with 's', not 'r'.  An STT mishearing of "reachy" keeps
    the leading phoneme, so the initial guard separates the genuine mishearings
    ("richie"/"reachie") from these homophone collisions.  This matters most for
    "speech", which is ubiquitous in a hearing/transcription feature.
    """
    assert is_name_match(text) is False, f"'{text}' should be rejected by the initial guard"


def test_exact_match_always_passes_regardless_of_threshold() -> None:
    """An exact name match always returns True, even at threshold=1.0."""
    assert is_name_match("reachy", threshold=1.0) is True
    assert is_name_match("robot", threshold=1.0) is True


# ---------------------------------------------------------------------------
# Empty / degenerate inputs
# ---------------------------------------------------------------------------


def test_empty_string() -> None:
    """Empty input returns False without error."""
    assert is_name_match("") is False


def test_no_alphabetic_words() -> None:
    """Input with only digits/punctuation returns False without error."""
    assert is_name_match("123 !!! 456") is False


def test_whitespace_only() -> None:
    """Whitespace-only input returns False without error."""
    assert is_name_match("   ") is False


# ---------------------------------------------------------------------------
# API surface
# ---------------------------------------------------------------------------


def test_default_threshold_exported() -> None:
    """DEFAULT_THRESHOLD is a float exported from the module."""
    assert isinstance(DEFAULT_THRESHOLD, float)
    assert DEFAULT_THRESHOLD > 0.0
    assert DEFAULT_THRESHOLD < 1.0


def test_module_has_docstring() -> None:
    """reachy.speech.name_match has a module-level docstring."""
    import reachy.speech.name_match as mod

    assert mod.__doc__, "name_match.py must have a module-level docstring"


def test_module_exports() -> None:
    """The module exposes is_name_match and DEFAULT_THRESHOLD."""
    import reachy.speech.name_match as mod

    assert hasattr(mod, "is_name_match")
    assert hasattr(mod, "DEFAULT_THRESHOLD")
    assert callable(mod.is_name_match)
