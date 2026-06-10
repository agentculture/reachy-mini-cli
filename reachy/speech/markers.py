"""Streaming parser for the ``*marker* / "speech"`` LLM output convention.

The cognition LLM interleaves **expression markers** and **speech** in its
token stream using a lightweight convention:

- ``*…*`` — expression marker.  The content between the asterisks is typically
  a single emoji (``*🤔*``) or a short action word (``*thinking*``).  These
  drive a robot expression; they are **not** spoken aloud.
- ``"…"`` — speech segment.  Only the text inside double quotes is passed to
  TTS / spoken.
- Anything outside markers / quotes is silently ignored.

Example stream::

    *🤔* "I wonder what that sound was." *👂* "There it is again."

Parses to::

    [MarkerEvent(emoji='🤔'), SpeechEvent(text='I wonder what that sound was.'),
     MarkerEvent(emoji='👂'), SpeechEvent(text='There it is again.')]

Design notes
------------
The parser is a small char-by-char state machine so it handles **incremental
feeding** correctly: a marker or quoted span that arrives split across several
LLM token chunks is assembled correctly and the event is emitted the moment its
closing delimiter arrives.  This makes it a natural fit for the
:class:`~reachy.speech.cognition.CognitionEngine` sentence-streaming pipeline.

Rule for malformed / unclosed spans
-------------------------------------
An unclosed span (unterminated ``"`` or un-closed ``*…``) at the time
:meth:`MarkerParser.flush` is called is **dropped silently** — it yields no
event.  Events for fully-closed spans that arrived before the unclosed tail are
unaffected.  This keeps the robot safe: a half-formed LLM token at stream-end
is treated as noise rather than garbage speech or a ghost expression.

Public API
----------
:class:`MarkerEvent`
    Frozen dataclass — ``kind="marker"``, ``emoji: str``.
:class:`SpeechEvent`
    Frozen dataclass — ``kind="speech"``, ``text: str``.
:data:`Event`
    ``MarkerEvent | SpeechEvent`` union alias (for type annotations).
:class:`MarkerParser`
    Streaming parser — ``feed(chunk: str) -> list[Event]``,
    ``flush() -> list[Event]``.
:func:`parse`
    Convenience whole-string parser built on :class:`MarkerParser`.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal, Union

# ---------------------------------------------------------------------------
# Event types
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State machine constants
# ---------------------------------------------------------------------------

_STATE_IDLE = "idle"
_STATE_MARKER = "marker"
_STATE_SPEECH = "speech"


# ---------------------------------------------------------------------------
# Streaming parser
# ---------------------------------------------------------------------------


class MarkerParser:
    """Incremental streaming parser for the ``*marker* / "speech"`` convention.

    Feed LLM token chunks as they arrive; events are yielded as their closing
    delimiter is received so they compose cleanly with the sentence-streaming
    pipeline in :mod:`reachy.speech.cognition`.

    Usage::

        parser = MarkerParser()
        for chunk in llm_token_stream:
            for event in parser.feed(chunk):
                handle(event)
        for event in parser.flush():
            handle(event)

    The parser is **reusable** after :meth:`flush` — the internal buffer and
    state are reset, so the same instance can be used for successive turns.
    """

    def __init__(self) -> None:
        self._state: str = _STATE_IDLE
        self._buf: list[str] = []

    # ------------------------------------------------------------------
    # Public interface
    # ------------------------------------------------------------------

    def feed(self, chunk: str) -> list[Event]:
        """Feed the next chunk of text from the LLM stream.

        Returns a list of events that were **completed** by characters in this
        chunk.  The list is empty when no span was closed by this chunk.

        Parameters
        ----------
        chunk:
            One or more characters from the LLM output.  May be a single token,
            a partial token, or several tokens — the parser is agnostic.

        Returns
        -------
        list[Event]
            Zero or more :class:`MarkerEvent` / :class:`SpeechEvent` objects,
            in the order they were completed within the chunk.
        """
        events: list[Event] = []
        for ch in chunk:
            result = self._step(ch)
            if result is not None:
                events.append(result)
        return events

    def flush(self) -> list[Event]:
        """Signal end-of-stream and return any remaining events.

        An unclosed span (``*…`` or ``"…`` without a closing delimiter) is
        **dropped** — not yielded — per the module's malformed-input rule.
        The parser state is reset so the instance can be reused.

        Returns
        -------
        list[Event]
            Always an empty list under the current rule (unclosed spans are
            dropped).  Kept as a list for API symmetry with :meth:`feed`.
        """
        # Drop any partial accumulation — unclosed spans are discarded.
        self._state = _STATE_IDLE
        self._buf.clear()
        return []

    # ------------------------------------------------------------------
    # Internal state machine
    # ------------------------------------------------------------------

    def _step(self, ch: str) -> Event | None:
        """Process one character and return a completed event or None."""
        # Inside a span: a matching delimiter closes it, anything else is literal
        # content.  Both spans are symmetric, so they share one handler.
        if self._state == _STATE_MARKER:
            return self._step_span(ch, "*", MarkerEvent, "emoji")
        if self._state == _STATE_SPEECH:
            return self._step_span(ch, '"', SpeechEvent, "text")

        # Idle: a delimiter opens a span; everything else is silently ignored.
        if ch == "*":
            self._state = _STATE_MARKER
            self._buf.clear()
        elif ch == '"':
            self._state = _STATE_SPEECH
            self._buf.clear()
        return None

    def _step_span(
        self,
        ch: str,
        delimiter: str,
        event_cls: type[MarkerEvent] | type[SpeechEvent],
        field: str,
    ) -> Event | None:
        """Handle one character inside an open span.

        A ``delimiter`` closes the span and emits ``event_cls(**{field: content})``
        when the stripped content is non-empty; any other character is
        accumulated as literal content.
        """
        if ch != delimiter:
            self._buf.append(ch)
            return None
        content = "".join(self._buf).strip()
        self._state = _STATE_IDLE
        self._buf.clear()
        if content:
            return event_cls(**{field: content})
        return None


# ---------------------------------------------------------------------------
# Convenience whole-string parser
# ---------------------------------------------------------------------------


def parse(text: str) -> list[Event]:
    """Parse a complete LLM output string and return all events in order.

    This is a thin wrapper around :class:`MarkerParser` for the non-streaming
    (whole-string) case.  It is equivalent to::

        parser = MarkerParser()
        events = parser.feed(text)
        events += parser.flush()
        return events

    Parameters
    ----------
    text:
        The full LLM output string, possibly containing ``*marker*`` and
        ``"speech"`` spans interspersed with arbitrary prose.

    Returns
    -------
    list[Event]
        Ordered sequence of :class:`MarkerEvent` and :class:`SpeechEvent`
        objects.  Prose outside spans and unclosed spans are silently dropped.
    """
    parser = MarkerParser()
    events = parser.feed(text)
    events.extend(parser.flush())
    return events
