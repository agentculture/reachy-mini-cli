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


# ---------------------------------------------------------------------------
# Real-world collisions — a GROWING list, one entry per defect actually observed
# ---------------------------------------------------------------------------
#
# This is a RECURRING defect class, not a one-off (issue #104).  Each entry below
# is a word that reached the name fast-path on a deployed robot, or a word from
# the same structural family found while fixing one that did.  The name path
# engages with ZERO classifier calls, so there is no second opinion to catch a
# false positive here — every one of these is an unprompted chirp in a
# conversation the robot was never part of.
#
# Add to this table whenever a new collision is observed live.  Each entry
# records the score it achieved against the name it collided with, so the next
# person can see exactly how close to the line it was.
_COLLISION_TABLE: list[tuple[str, str, str]] = [
    # (word, colliding name, why the pre-#104 guards let it through)
    # --- observed live on the deployed box, 2026-07-21 (issue #104) ---
    ("really", "reachy", "score 0.667: same length, shared 'rea' prefix, shared initial 'r'"),
    ("reality", "reachy", "score 0.527: shared 'rea' prefix, shared initial 'r'"),
    # --- same structural family, found by sweeping common words while fixing ---
    ("ready", "reachy", "score 0.606"),
    ("reader", "reachy", "score 0.500"),
    ("reason", "reachy", "score 0.500"),
    ("recent", "reachy", "score 0.500"),
    ("record", "reachy", "score 0.500"),
    ("rachel", "reachy", "score 0.667 — a human name, not the robot's"),
    ("room", "robot", "score 0.533"),
    ("robe", "robot", "score 0.533"),
    ("route", "robot", "score 0.600"),
    ("root", "robot", "score 0.711"),
    ("robust", "robot", "score 0.606"),
    # --- n-family: "nova" joins the canonical names (issue #25) ---
    ("now", "nova", "phonetic code N000 (no consonant survives) != nova's N100"),
    ("no", "nova", "too short (below the four-letter fuzzy floor) and N000 != N100"),
    ("know", "nova", "silent 'k' — starts with 'k', not 'n' — initial guard rejects it"),
    ("nah", "nova", "phonetic code N000 != nova's N100"),
    ("not", "nova", "phonetic code N300 != nova's N100"),
    ("novel", "nova", "phonetic code N140 != nova's N100"),
    ("november", "nova", "phonetic code N151 != nova's N100"),
    ("nowhere", "nova", "phonetic code N600 != nova's N100"),
    ("nothing", "nova", "phonetic code N352 != nova's N100"),
    ("never", "nova", "phonetic code N160 != nova's N100"),
]


@pytest.mark.parametrize(
    "word,name,why", _COLLISION_TABLE, ids=[row[0] for row in _COLLISION_TABLE]
)
def test_real_world_collisions_are_rejected(word: str, name: str, why: str) -> None:
    """Common English words must never take the zero-classifier name fast-path.

    Every word here clears the combined similarity threshold against *name* and
    survives the prefix / superstring / initial-letter guards — they are all
    'r'-initial words that share letters with "reachy"/"robot".  What separates
    them from a genuine STT mishearing is the CONSONANT SKELETON: "richie",
    "reachie" and "richy" all keep "reachy"'s r-ch, while "really" (r-l),
    "reason" (r-s-n) and "root" (r-t) do not.  That is what the phonetic guard
    tests.
    """
    assert is_name_match(word) is False, f"{word!r} must not name-match {name!r} ({why})"


@pytest.mark.parametrize(
    "text",
    [
        "That was really good.",
        "in reality it never happened",
        "are you ready to go",
        "let me give a speech about this",
        "i will be there in a room upstairs",
        "we took the scenic route home",
    ],
)
def test_real_world_collisions_in_sentences(text: str) -> None:
    """The collisions above stay rejected inside natural sentences.

    ``"That was really good."`` is the exact utterance that fired
    ``greet-when-addressed`` on the deployed robot with nobody addressing it.
    """
    assert is_name_match(text) is False, f"{text!r} must not engage the name fast-path"


def test_exact_match_always_passes_regardless_of_threshold() -> None:
    """An exact name match always returns True, even at threshold=1.0."""
    assert is_name_match("reachy", threshold=1.0) is True
    assert is_name_match("robot", threshold=1.0) is True
    assert is_name_match("nova", threshold=1.0) is True


# ---------------------------------------------------------------------------
# n-family (issue #25) — "nova" as a canonical name
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text",
    [
        "nova",
        "NOVA",
        "Nova, come here",
        "hey nova",
        "nova what time is it",
        "nova's over here",  # plausible STT slip: name + possessive/contraction
    ],
)
def test_nova_accepted(text: str) -> None:
    """ "nova" and plausible variants match by default (issue #25)."""
    assert is_name_match(text) is True, f"{text!r} should match the default name 'nova'"


@pytest.mark.parametrize(
    "text",
    [
        "now",
        "no",
        "know",
        "nah",
        "not",
        "novel",
        "November",
        "nowhere",
        "not now",
        "nothing",
        "never",
    ],
)
def test_nova_collisions_rejected(text: str) -> None:
    """Common n-initial English words must not false-trigger on "nova" (issue #25)."""
    assert is_name_match(text) is False, f"{text!r} must not name-match 'nova'"


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
