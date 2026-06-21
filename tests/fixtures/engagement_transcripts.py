"""Labelled transcript fixtures for engagement-gate characterization tests.

These are shared across the t3 baseline / pin tests and the t6 integration
tests (which import this module to exercise the new gate against the same
labelled set).  Every line is realistic — something an STT backend might
actually return from a nearby conversation.

Categories
----------
``ambient``
    Human-to-human speech that has nothing to do with the robot.  The heuristic
    should *not* engage on these; when it does (inside the conversation window)
    that is the flaw we are pinning.

``named``
    Utterances that include the robot's canonical name ("reachy" or "robot") as a
    **whole word**.  The heuristic must always engage on these regardless of
    window state.

``addressed_followup``
    Coherent sentences clearly aimed at the robot but WITHOUT the robot's name
    (natural follow-ups inside a conversation).  The new gate should accept these
    only when context confirms they are addressed to the robot; the old heuristic
    accepts them purely on word-count + window membership.

``misheard_name``
    Utterances where STT got the robot's name wrong ("richie", "reachie", etc.).
    The heuristic should NOT engage on these (the name match is whole-word and
    only canonical names qualify); the new gate may use fuzzy matching.

Each fixture is a list of ``TranscriptLine`` instances so callers can iterate,
filter by category, and read structured metadata in a single import.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

Category = Literal["ambient", "named", "addressed_followup", "misheard_name"]


@dataclass(frozen=True)
class TranscriptLine:
    """One labelled STT transcript line.

    Attributes
    ----------
    text:
        The raw transcript string exactly as an STT backend would return it.
    category:
        Semantic label (see module docstring).
    note:
        Optional human-readable explanation (useful for failing-test messages).
    """

    text: str
    category: Category
    note: str = field(default="", compare=False)


# ---------------------------------------------------------------------------
# ambient — human-to-human, not addressed to the robot
# ---------------------------------------------------------------------------

AMBIENT: list[TranscriptLine] = [
    TranscriptLine(
        "the weather looks nice today",
        "ambient",
        note="casual observation between humans, 5 words",
    ),
    TranscriptLine(
        "did you see the game last night",
        "ambient",
        note="7-word sports question between humans",
    ),
    TranscriptLine(
        "i think we should grab coffee later",
        "ambient",
        note="social plan between humans, 7 words",
    ),
    TranscriptLine(
        "what time does the meeting start",
        "ambient",
        note="office chatter, 6 words, no robot name",
    ),
    TranscriptLine(
        "have you tried the new restaurant on fifth",
        "ambient",
        note="8-word food recommendation between humans",
    ),
    TranscriptLine(
        "let me check my calendar real quick",
        "ambient",
        note="self-directed comment, 7 words",
    ),
    TranscriptLine(
        "she said the project is due friday",
        "ambient",
        note="third-person office gossip, 7 words",
    ),
]

# ---------------------------------------------------------------------------
# named — contains "reachy" or "robot" as a whole word
# ---------------------------------------------------------------------------

NAMED: list[TranscriptLine] = [
    TranscriptLine(
        "reachy what time is it",
        "named",
        note="direct address using the primary name",
    ),
    TranscriptLine(
        "hey reachy can you hear me",
        "named",
        note="greeting + question to the robot",
    ),
    TranscriptLine(
        "robot please turn to face me",
        "named",
        note="command using the alternate name",
    ),
    TranscriptLine(
        "reachy tell me a joke",
        "named",
        note="entertainment request by name",
    ),
    TranscriptLine(
        "is reachy working properly today",
        "named",
        note="third-person check — still contains the name",
    ),
    TranscriptLine(
        "i asked robot to move but it did not",
        "named",
        note="complaint containing the name mid-sentence",
    ),
]

# ---------------------------------------------------------------------------
# addressed_followup — coherent sentences aimed at the robot, no canonical name
# ---------------------------------------------------------------------------

ADDRESSED_FOLLOWUP: list[TranscriptLine] = [
    TranscriptLine(
        "what do you think about that",
        "addressed_followup",
        note="6-word follow-up question after a named turn",
    ),
    TranscriptLine(
        "can you say that again please",
        "addressed_followup",
        note="clarification request in context",
    ),
    TranscriptLine(
        "how does that make you feel",
        "addressed_followup",
        note="empathy follow-up, clearly to the robot",
    ),
    TranscriptLine(
        "do you know any other jokes",
        "addressed_followup",
        note="entertainment continuation",
    ),
    TranscriptLine(
        "tell me more about that",
        "addressed_followup",
        note="open-ended follow-up, 5 words",
    ),
    TranscriptLine(
        "what was the last thing you said",
        "addressed_followup",
        note="memory/recall question to the robot",
    ),
]

# ---------------------------------------------------------------------------
# misheard_name — STT phonetic corruption of the robot's name
# ---------------------------------------------------------------------------

MISHEARD_NAME: list[TranscriptLine] = [
    TranscriptLine(
        "richie what time is it",
        "misheard_name",
        note="'reachy' → 'richie', a common phonetic slip",
    ),
    TranscriptLine(
        "reachie can you hear me",
        "misheard_name",
        note="'reachy' → 'reachie', extra vowel from STT",
    ),
    TranscriptLine(
        "peachy turn around",
        "misheard_name",
        note="'reachy' → 'peachy', rhyming mis-decode",
    ),
    TranscriptLine(
        "ricky please move forward",
        "misheard_name",
        note="'reachy' → 'ricky', consonant swap",
    ),
    TranscriptLine(
        "robbot stop moving",
        "misheard_name",
        note="'robot' → 'robbot', doubled consonant",
    ),
    TranscriptLine(
        "robbie what are you doing",
        "misheard_name",
        note="'robot' → 'robbie', human-name confusion",
    ),
]

# ---------------------------------------------------------------------------
# Convenience: all lines in one flat list
# ---------------------------------------------------------------------------

ALL: list[TranscriptLine] = [
    *AMBIENT,
    *NAMED,
    *ADDRESSED_FOLLOWUP,
    *MISHEARD_NAME,
]


def by_category(category: Category) -> list[TranscriptLine]:
    """Return all fixture lines matching *category*."""
    return [line for line in ALL if line.category == category]
