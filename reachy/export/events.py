"""Event model and JSONL serializer for the ``reachy-mini-cli`` export feed.

The export feed is a **newline-delimited JSON** (NDJSON) stream written to
stdout.  Each line is one self-contained JSON object carrying at minimum a
block-type discriminator (``t``) and a unix timestamp (``ts``), followed by
type-specific payload fields.

Three block types are defined:

- ``"thinking"`` — an internal reasoning turn, with sense cues and LLM text.
- ``"message"`` — a speech segment (text the robot says aloud).
- ``"emotion"`` — a body-expression trigger (emoji + optional pose snapshot).

See ``docs/export-schema.md`` for the full wire-format specification that
external consumers should implement against.

Public API
----------
:class:`EmotionEvent`
    Frozen dataclass — ``t = "emotion"``, fields: ``emoji``, ``pose``, ``ts``.
:class:`MessageEvent`
    Frozen dataclass — ``t = "message"``, fields: ``text``, ``ts``.
:class:`ThinkingEvent`
    Frozen dataclass — ``t = "thinking"``, fields: ``cues``, ``text``, ``ts``.
:data:`Event`
    ``EmotionEvent | MessageEvent | ThinkingEvent`` union alias.
:func:`to_jsonl`
    Serialize any :data:`Event` to a compact single-line JSON string (no
    trailing newline).  Uses stdlib ``json`` only; emoji are kept literal
    (``ensure_ascii=False``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import ClassVar, Union

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class EmotionEvent:
    """A body-expression event emitted when the robot adopts an emotional pose.

    Parameters
    ----------
    emoji:
        The emoji that triggered the expression (e.g. ``"🙂"``).
    pose:
        Optional snapshot of the 9-axis pose applied (head mm/deg, antenna deg,
        body_yaw deg).  ``None`` when no pose was resolved (unknown emoji).
    ts:
        Unix timestamp in fractional seconds.  Defaults to ``0.0``; callers
        should inject :func:`time.time` at creation time.
    """

    #: Block-type discriminator for this event class.
    t: ClassVar[str] = "emotion"

    emoji: str
    pose: dict | None = None
    ts: float = 0.0


@dataclass(frozen=True)
class MessageEvent:
    """A speech event emitted when the robot speaks a sentence aloud.

    Parameters
    ----------
    text:
        The text spoken (after TTS synthesis).
    ts:
        Unix timestamp in fractional seconds.  Defaults to ``0.0``.
    """

    #: Block-type discriminator for this event class.
    t: ClassVar[str] = "message"

    text: str
    ts: float = 0.0


@dataclass(frozen=True)
class ThinkingEvent:
    """An internal reasoning event emitted by the cognition loop.

    Parameters
    ----------
    cues:
        Sense cues that triggered this reasoning turn (e.g. ``["sound", "motion"]``).
        May be empty when the turn was timer-driven.
    text:
        The raw LLM output for this reasoning turn, including any ``*emoji*``
        and ``"speech"`` markers.
    ts:
        Unix timestamp in fractional seconds.  Defaults to ``0.0``.
    """

    #: Block-type discriminator for this event class.
    t: ClassVar[str] = "thinking"

    cues: list[str]
    text: str
    ts: float = 0.0


#: Union type alias for use in type annotations and ``isinstance`` checks.
Event = Union[EmotionEvent, MessageEvent, ThinkingEvent]


# ---------------------------------------------------------------------------
# JSONL serializer
# ---------------------------------------------------------------------------


def to_jsonl(event: Event) -> str:
    """Serialize an :data:`Event` to a compact single-line JSON string.

    The output layout always starts with ``t`` (block type) and ``ts``
    (timestamp) — so stream parsers can dispatch on block type before reading
    the rest — followed by the type-specific payload fields:

    - ``"emotion"`` → ``{t, ts, emoji, pose}``
    - ``"message"`` → ``{t, ts, text}``
    - ``"thinking"`` → ``{t, ts, cues, text}``

    The returned string contains **no trailing newline**.  To write an NDJSON
    stream, append ``"\\n"`` yourself::

        sys.stdout.write(to_jsonl(ev) + "\\n")
        sys.stdout.flush()

    Parameters
    ----------
    event:
        Any of :class:`EmotionEvent`, :class:`MessageEvent`,
        :class:`ThinkingEvent`.

    Returns
    -------
    str
        Compact JSON string (``ensure_ascii=False``, no spaces around
        separators).  Emoji and other non-ASCII characters are kept literal.
    """
    if isinstance(event, EmotionEvent):
        payload: dict = {
            "t": event.t,
            "ts": event.ts,
            "emoji": event.emoji,
            "pose": event.pose,
        }
    elif isinstance(event, MessageEvent):
        payload = {
            "t": event.t,
            "ts": event.ts,
            "text": event.text,
        }
    else:  # ThinkingEvent
        payload = {
            "t": event.t,
            "ts": event.ts,
            "cues": list(event.cues),
            "text": event.text,
        }
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))
