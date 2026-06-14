"""Tests for :mod:`reachy.export.exporter`.

Covers the four acceptance criteria mandated for task t3:

1. Per-event flush / real-time — each selected emit produces exactly one
   write (a JSONL line ending in ``\\n``) followed immediately by one flush.
2. Selection filtering — excluded block types produce zero writes; included
   types are written.
3. Broken-pipe / passive tap — BrokenPipeError / OSError from the underlying
   stream is swallowed; a warning is written to stderr AT MOST ONCE across
   many failing emits; subsequent emits are silent no-ops.
4. Stdout purity — every line in the output buffer is json.loads()-able and
   the buffer contains only JSONL lines.
"""

from __future__ import annotations

import io
import json
import sys

from reachy.export.blocks import Selection
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent
from reachy.export.exporter import JsonlExporter

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _CallLog:
    """Minimal writable text-stream that records every method call in order."""

    def __init__(
        self,
        *,
        raise_on_write: type[Exception] | None = None,
        raise_on_flush: type[Exception] | None = None,
    ):
        self.calls: list[tuple[str, str]] = []  # ("write"/"flush", value_or_"")
        self._raise_on_write = raise_on_write
        self._raise_on_flush = raise_on_flush

    def write(self, s: str) -> None:
        if self._raise_on_write is not None:
            raise self._raise_on_write("stream broken")
        self.calls.append(("write", s))

    def flush(self) -> None:
        if self._raise_on_flush is not None:
            raise self._raise_on_flush("stream broken")
        self.calls.append(("flush", ""))


# ---------------------------------------------------------------------------
# Fixture events
# ---------------------------------------------------------------------------

EMOTION = EmotionEvent(emoji="😮", ts=1.0)
MESSAGE = MessageEvent(text="hello", ts=2.0)
THINKING = ThinkingEvent(cues=["sound"], text='*😮* "hello"', ts=3.0)


# ---------------------------------------------------------------------------
# Criterion 1 — per-event flush / real-time ordering
# ---------------------------------------------------------------------------


class TestPerEventFlush:
    def test_single_emit_write_then_flush(self):
        """One selected emit → exactly one write then one flush."""
        stream = _CallLog()
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)

        assert len(stream.calls) == 2
        op0, val0 = stream.calls[0]
        op1, _ = stream.calls[1]
        assert op0 == "write"
        assert val0.endswith("\n"), "written value must end with newline"
        assert op1 == "flush"

    def test_single_emit_content_is_jsonl(self):
        """Written line (sans newline) must be valid JSON."""
        stream = _CallLog()
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(MESSAGE)

        _, line = stream.calls[0]
        obj = json.loads(line.rstrip("\n"))
        assert obj["t"] == "message"
        assert obj["text"] == "hello"

    def test_multiple_emits_order(self):
        """Multiple selected emits produce interleaved write/flush pairs in order."""
        stream = _CallLog()
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)
        exporter.emit(MESSAGE)
        exporter.emit(THINKING)

        # Expect exactly 3 write/flush pairs = 6 calls
        assert len(stream.calls) == 6
        for i, (op, _) in enumerate(stream.calls):
            expected = "write" if i % 2 == 0 else "flush"
            assert op == expected, f"call {i}: expected {expected}, got {op}"

    def test_multiple_emits_lines_in_order(self):
        """Written lines appear in the same order as emit() calls."""
        stream = _CallLog()
        exporter = JsonlExporter(stream, Selection.all())
        events = [EMOTION, MESSAGE, THINKING]
        for ev in events:
            exporter.emit(ev)

        written_types = [
            json.loads(val.rstrip("\n"))["t"] for op, val in stream.calls if op == "write"
        ]
        assert written_types == ["emotion", "message", "thinking"]


# ---------------------------------------------------------------------------
# Criterion 2 — selection filtering
# ---------------------------------------------------------------------------


class TestSelectionFiltering:
    def test_excluded_type_produces_no_writes(self):
        """An event whose type is not in the selection is silently dropped."""
        sel = Selection(["message", "emotion"])  # no "thinking"
        stream = _CallLog()
        exporter = JsonlExporter(stream, sel)
        exporter.emit(THINKING)

        assert stream.calls == []

    def test_included_type_is_written(self):
        """An event whose type IS in the selection is written and flushed."""
        sel = Selection(["thinking"])
        stream = _CallLog()
        exporter = JsonlExporter(stream, sel)
        exporter.emit(THINKING)

        assert len(stream.calls) == 2

    def test_mixed_selection(self):
        """Only events matching the selection pass through."""
        sel = Selection(["emotion"])
        stream = _CallLog()
        exporter = JsonlExporter(stream, sel)
        exporter.emit(EMOTION)  # allowed
        exporter.emit(MESSAGE)  # blocked
        exporter.emit(THINKING)  # blocked
        exporter.emit(EMOTION)  # allowed

        write_ops = [(op, val) for op, val in stream.calls if op == "write"]
        assert len(write_ops) == 2
        for _, val in write_ops:
            assert json.loads(val.rstrip("\n"))["t"] == "emotion"

    def test_empty_selection_blocks_all(self):
        """A selection with no types drops everything."""
        sel = Selection([])  # empty — nothing allowed
        stream = _CallLog()
        exporter = JsonlExporter(stream, sel)
        exporter.emit(EMOTION)
        exporter.emit(MESSAGE)
        exporter.emit(THINKING)

        assert stream.calls == []


# ---------------------------------------------------------------------------
# Criterion 3 — broken-pipe / passive tap
# ---------------------------------------------------------------------------


class TestBrokenPipe:
    def _capture_stderr(self, capsys, stream_cls_kwargs: dict, events: list) -> tuple[list, str]:
        """Emit *events* with a broken stream; return (calls, stderr)."""
        stream = _CallLog(**stream_cls_kwargs)
        exporter = JsonlExporter(stream, Selection.all())
        for ev in events:
            exporter.emit(ev)
        captured = capsys.readouterr()
        return stream.calls, captured.err

    def test_broken_pipe_on_write_does_not_raise(self, capsys):
        """BrokenPipeError from write() must NOT propagate."""
        stream = _CallLog(raise_on_write=BrokenPipeError)
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)  # must not raise

    def test_oserror_on_write_does_not_raise(self, capsys):
        """OSError from write() must NOT propagate."""
        stream = _CallLog(raise_on_write=OSError)
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)  # must not raise

    def test_oserror_on_flush_does_not_raise(self, capsys):
        """OSError from flush() must NOT propagate."""
        stream = _CallLog(raise_on_flush=OSError)
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)  # must not raise

    def test_value_error_on_write_does_not_raise(self, capsys):
        """ValueError (closed stream) from write() must NOT propagate."""
        buf = io.StringIO()
        buf.close()
        exporter = JsonlExporter(buf, Selection.all())
        exporter.emit(EMOTION)  # must not raise

    def test_broken_stderr_warning_does_not_propagate(self, monkeypatch):
        """When stderr is ALSO broken (e.g. ``2>&1 | head``), the warning write
        must be swallowed too — emit stays pipe-safe (Qodo #2)."""

        class _BrokenStderr:
            def write(self, *_a, **_kw):
                raise BrokenPipeError("stderr broken")

            def flush(self, *_a, **_kw):
                raise BrokenPipeError("stderr broken")

        monkeypatch.setattr(sys, "stderr", _BrokenStderr())
        stream = _CallLog(raise_on_write=BrokenPipeError)
        exporter = JsonlExporter(stream, Selection.all())
        # Both stdout and stderr are broken pipes: emit must STILL not raise.
        exporter.emit(EMOTION)
        exporter.emit(MESSAGE)  # subsequent emits are silent no-ops

    def test_warning_written_to_stderr_exactly_once(self, capsys):
        """A warning is written to stderr exactly once across repeated failures."""
        stream = _CallLog(raise_on_write=BrokenPipeError)
        exporter = JsonlExporter(stream, Selection.all())
        for _ in range(5):
            exporter.emit(EMOTION)
        captured = capsys.readouterr()
        # Must contain exactly one warning line
        warning_lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(warning_lines) == 1, f"expected 1 warning line, got: {warning_lines}"

    def test_warning_written_to_stderr_exactly_once_oserror(self, capsys):
        """Same at-most-once guarantee for OSError."""
        stream = _CallLog(raise_on_write=OSError)
        exporter = JsonlExporter(stream, Selection.all())
        for _ in range(3):
            exporter.emit(MESSAGE)
        captured = capsys.readouterr()
        warning_lines = [line for line in captured.err.splitlines() if line.strip()]
        assert len(warning_lines) == 1

    def test_subsequent_emits_are_silent_noop_after_broken(self, capsys):
        """After the first pipe error, subsequent emits do nothing (no extra stderr)."""
        stream = _CallLog(raise_on_write=BrokenPipeError)
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)  # triggers first (and only) warning
        capsys.readouterr()  # drain

        exporter.emit(MESSAGE)  # must be completely silent
        exporter.emit(THINKING)
        captured = capsys.readouterr()
        assert captured.err == "", "no additional stderr after broken flag set"

    def test_broken_on_flush_sets_broken_flag(self, capsys):
        """A BrokenPipeError raised during flush() also sets the broken flag."""
        stream = _CallLog(raise_on_flush=BrokenPipeError)
        exporter = JsonlExporter(stream, Selection.all())
        exporter.emit(EMOTION)
        capsys.readouterr()  # drain first warning

        exporter.emit(MESSAGE)
        captured = capsys.readouterr()
        assert captured.err == ""


# ---------------------------------------------------------------------------
# Criterion 4 — stdout purity
# ---------------------------------------------------------------------------


class TestStdoutPurity:
    def test_all_lines_json_parseable(self):
        """Every line written to the buffer is valid JSON."""
        buf = io.StringIO()
        exporter = JsonlExporter(buf, Selection.all())
        for ev in [EMOTION, MESSAGE, THINKING]:
            exporter.emit(ev)

        content = buf.getvalue()
        lines = content.splitlines()
        assert len(lines) == 3
        for line in lines:
            obj = json.loads(line)
            assert "t" in obj
            assert "ts" in obj

    def test_buffer_contains_only_jsonl(self):
        """Buffer contains exactly one JSON object per emitted event, nothing else."""
        buf = io.StringIO()
        exporter = JsonlExporter(buf, Selection.all())
        events = [EMOTION, MESSAGE, THINKING, EMOTION]
        for ev in events:
            exporter.emit(ev)

        content = buf.getvalue()
        # Must end with newline (each line has one)
        assert content.endswith("\n")
        lines = content.split("\n")
        # Last element after final \n is empty string
        non_empty = [ln for ln in lines if ln]
        assert len(non_empty) == len(events)
        for ln in non_empty:
            json.loads(ln)  # raises if not valid JSON

    def test_event_fields_correct_in_output(self):
        """Verify each event type serializes the correct fields to the buffer."""
        buf = io.StringIO()
        exporter = JsonlExporter(buf, Selection.all())
        exporter.emit(EmotionEvent(emoji="🎉", pose={"head_tilt": 10.0}, ts=42.0))
        exporter.emit(MessageEvent(text="world", ts=43.0))
        exporter.emit(ThinkingEvent(cues=["motion", "sound"], text='*🎉* "world"', ts=44.0))

        lines = buf.getvalue().splitlines()
        em, msg, th = [json.loads(ln) for ln in lines]

        assert em == {"t": "emotion", "ts": 42.0, "emoji": "🎉", "pose": {"head_tilt": 10.0}}
        assert msg == {"t": "message", "ts": 43.0, "text": "world"}
        assert th == {
            "t": "thinking",
            "ts": 44.0,
            "cues": ["motion", "sound"],
            "text": '*🎉* "world"',
        }

    def test_selection_filtered_events_absent_from_buffer(self):
        """Events excluded by selection leave no trace in the output."""
        buf = io.StringIO()
        sel = Selection(["message"])
        exporter = JsonlExporter(buf, sel)
        exporter.emit(EMOTION)
        exporter.emit(MESSAGE)
        exporter.emit(THINKING)

        lines = buf.getvalue().splitlines()
        assert len(lines) == 1
        assert json.loads(lines[0])["t"] == "message"
