"""Event types for the ``*marker* / "speech"`` LLM output convention.

These are the two pure, frozen dataclasses (plus their union alias) that
:mod:`reachy.speech.markers`'s streaming :class:`~reachy.speech.markers.MarkerParser`
produces. They live in their own module — separate from the parser — so that
:mod:`reachy.motion.expression` (part of the ``apply_pose`` tool path that
survives the in-loop cognition engine's eventual removal) can depend on the
*shape* of a marker event without depending on the parser that produces it.
:mod:`reachy.speech.markers` imports these names back (a re-export, not a
redefinition — the class objects are identical) so every existing importer of
``reachy.speech.markers.MarkerEvent`` / ``Event`` keeps working unchanged.

Public API
----------
:class:`MarkerEvent`
    Frozen dataclass — ``kind="marker"``, ``emoji: str``.
:class:`SpeechEvent`
    Frozen dataclass — ``kind="speech"``, ``text: str``.
:data:`Event`
    ``MarkerEvent | SpeechEvent`` union alias (for type annotations).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union


@dataclass(frozen=True)
class MarkerEvent:
    """An expression-marker event emitted when a ``*…*`` span closes.

    Parameters
    ----------
    emoji:
        The trimmed content between the asterisks.  Typically a single emoji
        (``🤔``) or a short action word (``thinking``).
    """

    emoji: str
    kind: Literal["marker"] = "marker"


@dataclass(frozen=True)
class SpeechEvent:
    """A speech event emitted when a ``"…"`` span closes.

    Parameters
    ----------
    text:
        The trimmed text between the double-quote delimiters.
    """

    text: str
    kind: Literal["speech"] = "speech"


#: Union type alias for use in annotations.
Event = Union[MarkerEvent, SpeechEvent]
