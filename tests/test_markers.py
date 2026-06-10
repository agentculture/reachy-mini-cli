"""Tests for the expression marker / speech parser (``reachy.speech.markers``).

Three acceptance criteria:

1. **Ordered event sequence** — a mixed stream is split into an ordered sequence
   of :class:`MarkerEvent` (from ``*…*``) and :class:`SpeechEvent` (from
   ``"…"``).  Text outside markers / quotes is dropped.  Order is preserved.

2. **Graceful malformed input** — a lone ``*``, unterminated ``"`` or ``*`` at
   end-of-stream does NOT crash.  Defined rule: an unclosed span at flush() is
   **dropped** (not yielded as a partial event).  Pairs that are complete up to
   the unclosed span are still emitted correctly.

3. **Streaming-friendly** — the parser supports incremental feeding of chunks
   (even one character at a time) and yields events as they complete.  A marker
   or quoted span that spans multiple chunks is assembled correctly.
"""

from __future__ import annotations

import pytest

from reachy.speech.markers import Event, MarkerEvent, MarkerParser, SpeechEvent, parse

# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _feed_all(parser: MarkerParser, text: str, chunk_size: int = 1) -> list[Event]:
    """Feed *text* in chunks of *chunk_size*, collect all events."""
    events: list[Event] = []
    for i in range(0, len(text), chunk_size):
        events.extend(parser.feed(text[i : i + chunk_size]))
    events.extend(parser.flush())
    return events


# ---------------------------------------------------------------------------
# Acceptance criterion 1 — ordered event sequence
# ---------------------------------------------------------------------------


class TestOrderedEvents:
    def test_simple_example_from_spec(self):
        """The spec's canonical example must parse to the exact ordered sequence."""
        stream = '*🤔* "I wonder what that sound was." *👂* "There it is again."'
        events = parse(stream)
        assert events == [
            MarkerEvent(emoji="🤔"),
            SpeechEvent(text="I wonder what that sound was."),
            MarkerEvent(emoji="👂"),
            SpeechEvent(text="There it is again."),
        ]

    def test_only_markers(self):
        events = parse("*😊* *😮* *🤖*")
        assert events == [
            MarkerEvent(emoji="😊"),
            MarkerEvent(emoji="😮"),
            MarkerEvent(emoji="🤖"),
        ]

    def test_only_speech(self):
        events = parse('"Hello." "World."')
        assert events == [
            SpeechEvent(text="Hello."),
            SpeechEvent(text="World."),
        ]

    def test_text_outside_markers_and_quotes_is_dropped(self):
        """Prose outside markers / quotes contributes no events."""
        events = parse('noise *😊* more noise "spoken part" trailing noise')
        assert events == [
            MarkerEvent(emoji="😊"),
            SpeechEvent(text="spoken part"),
        ]

    def test_empty_stream_yields_no_events(self):
        assert parse("") == []

    def test_event_type_shapes(self):
        """MarkerEvent and SpeechEvent expose the right fields and are frozen."""
        m = MarkerEvent(emoji="🤔")
        s = SpeechEvent(text="hi")
        assert m.emoji == "🤔"
        assert s.text == "hi"
        # Frozen dataclasses must raise on mutation.
        with pytest.raises((AttributeError, TypeError)):
            m.emoji = "x"  # type: ignore[misc]
        with pytest.raises((AttributeError, TypeError)):
            s.text = "y"  # type: ignore[misc]

    def test_whitespace_trimmed_inside_spans(self):
        """Leading/trailing whitespace inside spans is stripped."""
        events = parse('*  🤔  * "  hello  "')
        assert events == [
            MarkerEvent(emoji="🤔"),
            SpeechEvent(text="hello"),
        ]

    def test_empty_marker_is_dropped(self):
        """A ``**`` with nothing (or only whitespace) inside yields no event."""
        events = parse("** something **  * ")
        # No valid events: both markers are empty / malformed
        assert all(not isinstance(e, MarkerEvent) for e in events)

    def test_empty_speech_is_dropped(self):
        """A ``""`` with nothing (or only whitespace) inside yields no event."""
        events = parse('"" "  "')
        assert events == []

    def test_action_word_marker(self):
        """Markers can contain short action words, not just emoji."""
        events = parse('*thinking* "Out loud."')
        assert events == [
            MarkerEvent(emoji="thinking"),
            SpeechEvent(text="Out loud."),
        ]

    def test_event_equality(self):
        assert MarkerEvent(emoji="🤔") == MarkerEvent(emoji="🤔")
        assert SpeechEvent(text="hi") == SpeechEvent(text="hi")
        assert MarkerEvent(emoji="🤔") != SpeechEvent(text="🤔")

    def test_event_kind_field(self):
        """Both event types expose a ``kind`` discriminator."""
        assert MarkerEvent(emoji="x").kind == "marker"
        assert SpeechEvent(text="x").kind == "speech"


# ---------------------------------------------------------------------------
# Acceptance criterion 2 — graceful malformed / unclosed input
# ---------------------------------------------------------------------------


class TestMalformedInput:
    def test_lone_asterisk_does_not_crash(self):
        """A lone ``*`` at end-of-stream is silently dropped."""
        events = parse("*")
        assert events == []

    def test_unterminated_quote_does_not_crash(self):
        """An unterminated ``"`` is dropped at end-of-stream."""
        events = parse('"hello')
        assert events == []

    def test_unclosed_marker_is_dropped(self):
        """An unclosed ``*…`` at end-of-stream is dropped without crashing."""
        events = parse("*🤔")
        assert events == []

    def test_good_events_before_unclosed_marker_are_kept(self):
        """Events emitted before a malformed tail are not lost."""
        events = parse('"Good speech." *🤔')
        assert events == [SpeechEvent(text="Good speech.")]

    def test_good_events_before_unterminated_quote_are_kept(self):
        events = parse('*😊* "dangling')
        assert events == [MarkerEvent(emoji="😊")]

    def test_nested_asterisks_in_speech_pass_through(self):
        """Asterisks inside a quoted speech span are not treated as markers."""
        events = parse('"I * think * so."')
        assert events == [SpeechEvent(text="I * think * so.")]

    def test_nested_quotes_in_marker_pass_through(self):
        """Quotes inside an asterisk marker span are not treated as speech."""
        events = parse('*"nod"*')
        assert events == [MarkerEvent(emoji='"nod"')]

    def test_multiple_malformed_does_not_crash(self):
        """Multiple unclosed/empty spans without crashing; valid ones still emit.

        The stream interleaves a valid marker, an unclosed speech span, and another
        valid marker.  The two valid markers must emit; the unclosed quote is
        dropped by flush().  Must not raise.
        """
        # Valid marker, then unclosed quote containing a star, then valid marker.
        # Parsing: *😊* → MarkerEvent("😊"); then "dangling *👋* → unterminated
        # speech span accumulating " *👋* " content → dropped by flush().
        # So only the first marker emits.
        events = parse('*😊* "dangling *👋* still open')
        assert MarkerEvent(emoji="😊") in events
        # The dangling speech span is dropped; no SpeechEvent.
        assert not any(isinstance(e, SpeechEvent) for e in events)
        # Must not have raised — if we got here, we passed.

    def test_no_exception_on_random_stars(self):
        """Arbitrary star-heavy input must never raise."""
        garbled = "*** ** * *** ****"
        events = parse(garbled)  # just must not crash
        assert isinstance(events, list)

    def test_no_exception_on_random_quotes(self):
        garbled = '"" " " "a'
        events = parse(garbled)
        assert isinstance(events, list)


# ---------------------------------------------------------------------------
# Acceptance criterion 3 — streaming / incremental feeding
# ---------------------------------------------------------------------------


class TestStreamingFeed:
    def test_char_by_char_matches_whole_parse(self):
        """Feeding one character at a time yields the same result as parse()."""
        stream = '*🤔* "I wonder what that sound was." *👂* "There it is again."'
        parser = MarkerParser()
        events = _feed_all(parser, stream, chunk_size=1)
        assert events == parse(stream)

    def test_three_char_chunks_matches_whole_parse(self):
        stream = '*😊* "Hello." *😮* "World."'
        parser = MarkerParser()
        events = _feed_all(parser, stream, chunk_size=3)
        assert events == parse(stream)

    def test_marker_spanning_chunks_is_complete(self):
        """A marker split across two chunks is assembled and emitted correctly."""
        parser = MarkerParser()
        # Feed "*🤔" then "*" — the marker spans the chunk boundary
        events_1 = parser.feed("*🤔")
        assert events_1 == []  # marker not yet closed
        events_2 = parser.feed("*")
        assert events_2 == [MarkerEvent(emoji="🤔")]

    def test_speech_spanning_chunks_is_complete(self):
        """A speech span split across chunks is assembled correctly."""
        parser = MarkerParser()
        events_1 = parser.feed('"hel')
        assert events_1 == []
        events_2 = parser.feed('lo"')
        assert events_2 == [SpeechEvent(text="hello")]

    def test_events_emitted_as_soon_as_span_closes(self):
        """Each event is emitted the moment its closing delimiter arrives."""
        parser = MarkerParser()
        # Feed one marker; it must emit immediately — before the next marker.
        e1 = parser.feed("*😊*")
        assert e1 == [MarkerEvent(emoji="😊")]
        e2 = parser.feed(' "hi"')
        assert e2 == [SpeechEvent(text="hi")]

    def test_flush_returns_nothing_on_clean_state(self):
        """flush() on a completed (or empty) parser returns an empty list."""
        parser = MarkerParser()
        parser.feed('*😊* "hi"')
        assert parser.flush() == []

    def test_flush_drops_unclosed_span(self):
        """flush() drops an unclosed span and returns empty."""
        parser = MarkerParser()
        parser.feed('"unclosed')
        dropped = parser.flush()
        assert dropped == []

    def test_parser_reusable_after_flush(self):
        """After flush() the parser can continue accepting new chunks."""
        parser = MarkerParser()
        parser.feed('"unclosed')
        parser.flush()
        events = parser.feed("*😊*")
        assert events == [MarkerEvent(emoji="😊")]

    def test_large_chunked_stream(self):
        """A longer realistic stream is parsed correctly chunk-by-chunk."""
        stream = (
            '*🤔* "I wonder what that sound was." '
            '*👂* "There it is again." '
            '*😮* "Oh! It moved."'
        )
        parser = MarkerParser()
        events = _feed_all(parser, stream, chunk_size=7)
        assert events == [
            MarkerEvent(emoji="🤔"),
            SpeechEvent(text="I wonder what that sound was."),
            MarkerEvent(emoji="👂"),
            SpeechEvent(text="There it is again."),
            MarkerEvent(emoji="😮"),
            SpeechEvent(text="Oh! It moved."),
        ]

    def test_multiple_events_per_chunk(self):
        """A single chunk can close multiple spans and emit multiple events."""
        parser = MarkerParser()
        events = parser.feed('*😊* "hi" *👋*')
        assert events == [
            MarkerEvent(emoji="😊"),
            SpeechEvent(text="hi"),
            MarkerEvent(emoji="👋"),
        ]

    def test_feed_empty_chunk_is_noop(self):
        """Feeding an empty string returns an empty list and does not crash."""
        parser = MarkerParser()
        assert parser.feed("") == []
        assert parser.flush() == []
