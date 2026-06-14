"""Tests for the export event model and JSONL serializer (``reachy.export.events``).

Three acceptance criteria:

1. **Correct serialization** — each of the three event types serializes via
   :func:`~reachy.export.events.to_jsonl` to a single-line JSON string with stable
   keys ``t`` and ``ts`` first, followed by the correct type-specific payload
   (emotion → ``{emoji, pose}``; message → ``{text}``; thinking → ``{cues, text}``).
   :func:`json.loads` must round-trip the output back to a dict with those keys and
   values intact.

2. **Single-line output + stdlib-only import** — ``to_jsonl()`` output contains
   NO embedded newline character (one object per line); the implementation must
   import only stdlib ``json`` — no third-party JSON library.

3. **Schema doc coverage** — ``docs/export-schema.md`` mentions each event's ``t``
   value (``thinking``, ``message``, ``emotion``) and each required key
   (``emoji``, ``pose``, ``text``, ``cues``).  A consumer needs only the doc to
   implement a compatible reader — no Python import required.
"""

from __future__ import annotations

import ast
import json
from pathlib import Path

import pytest

from reachy.export.events import (
    EmotionEvent,
    Event,
    MessageEvent,
    ThinkingEvent,
    to_jsonl,
)

# ---------------------------------------------------------------------------
# Path helpers
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
SCHEMA_DOC = REPO_ROOT / "docs" / "export-schema.md"


# ---------------------------------------------------------------------------
# Acceptance criterion 1 — correct serialization
# ---------------------------------------------------------------------------


class TestEmotionEvent:
    def test_minimal_round_trip(self):
        """EmotionEvent with no pose serializes to a dict with the right keys."""
        ev = EmotionEvent(emoji="🙂")
        line = to_jsonl(ev)
        d = json.loads(line)
        assert d["t"] == "emotion"
        assert d["ts"] == 0.0
        assert d["emoji"] == "🙂"
        assert d["pose"] is None

    def test_with_pose_round_trip(self):
        pose = {"head_pitch": -5.0, "antenna_l": 30.0}
        ev = EmotionEvent(emoji="😮", pose=pose, ts=1.5)
        d = json.loads(to_jsonl(ev))
        assert d["t"] == "emotion"
        assert d["ts"] == 1.5
        assert d["emoji"] == "😮"
        assert d["pose"] == pose

    def test_key_order_t_ts_first(self):
        """Keys ``t`` and ``ts`` must appear first in the serialized output."""
        ev = EmotionEvent(emoji="🙂", ts=2.0)
        line = to_jsonl(ev)
        keys = list(json.loads(line).keys())
        assert keys[0] == "t"
        assert keys[1] == "ts"

    def test_exact_key_set(self):
        """EmotionEvent output must carry exactly {t, ts, emoji, pose}."""
        ev = EmotionEvent(emoji="🎉")
        d = json.loads(to_jsonl(ev))
        assert set(d.keys()) == {"t", "ts", "emoji", "pose"}

    def test_t_value(self):
        assert EmotionEvent.t == "emotion"
        assert json.loads(to_jsonl(EmotionEvent(emoji="x")))["t"] == "emotion"

    def test_emoji_literal_not_escaped(self):
        """Emoji must appear literally in the JSON output (ensure_ascii=False)."""
        ev = EmotionEvent(emoji="🤔")
        line = to_jsonl(ev)
        assert "🤔" in line

    def test_frozen_dataclass(self):
        """EmotionEvent must be a frozen dataclass (immutable)."""
        ev = EmotionEvent(emoji="🙂")
        with pytest.raises((AttributeError, TypeError)):
            ev.emoji = "x"  # type: ignore[misc]

    def test_t_is_not_an_init_field(self):
        """``t`` must be a class-level ClassVar, not an ``__init__`` parameter."""
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(EmotionEvent)}
        assert "t" not in field_names


class TestMessageEvent:
    def test_minimal_round_trip(self):
        ev = MessageEvent(text="Hello, world!")
        d = json.loads(to_jsonl(ev))
        assert d["t"] == "message"
        assert d["ts"] == 0.0
        assert d["text"] == "Hello, world!"

    def test_with_ts_round_trip(self):
        ev = MessageEvent(text="Something said.", ts=42.5)
        d = json.loads(to_jsonl(ev))
        assert d["ts"] == 42.5
        assert d["text"] == "Something said."

    def test_key_order_t_ts_first(self):
        ev = MessageEvent(text="hi")
        keys = list(json.loads(to_jsonl(ev)).keys())
        assert keys[0] == "t"
        assert keys[1] == "ts"

    def test_exact_key_set(self):
        """MessageEvent output must carry exactly {t, ts, text}."""
        d = json.loads(to_jsonl(MessageEvent(text="hi")))
        assert set(d.keys()) == {"t", "ts", "text"}

    def test_t_value(self):
        assert MessageEvent.t == "message"

    def test_frozen_dataclass(self):
        ev = MessageEvent(text="hi")
        with pytest.raises((AttributeError, TypeError)):
            ev.text = "y"  # type: ignore[misc]

    def test_t_is_not_an_init_field(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(MessageEvent)}
        assert "t" not in field_names


class TestThinkingEvent:
    def test_minimal_round_trip(self):
        ev = ThinkingEvent(cues=["sound", "motion"], text="Hmm.")
        d = json.loads(to_jsonl(ev))
        assert d["t"] == "thinking"
        assert d["ts"] == 0.0
        assert d["cues"] == ["sound", "motion"]
        assert d["text"] == "Hmm."

    def test_with_ts_round_trip(self):
        ev = ThinkingEvent(cues=[], text="Quiet.", ts=9.9)
        d = json.loads(to_jsonl(ev))
        assert d["ts"] == 9.9
        assert d["cues"] == []

    def test_key_order_t_ts_first(self):
        ev = ThinkingEvent(cues=["x"], text="y")
        keys = list(json.loads(to_jsonl(ev)).keys())
        assert keys[0] == "t"
        assert keys[1] == "ts"

    def test_exact_key_set(self):
        """ThinkingEvent output must carry exactly {t, ts, cues, text}."""
        d = json.loads(to_jsonl(ThinkingEvent(cues=[], text="")))
        assert set(d.keys()) == {"t", "ts", "cues", "text"}

    def test_t_value(self):
        assert ThinkingEvent.t == "thinking"

    def test_frozen_dataclass(self):
        ev = ThinkingEvent(cues=[], text="hi")
        with pytest.raises((AttributeError, TypeError)):
            ev.text = "y"  # type: ignore[misc]

    def test_t_is_not_an_init_field(self):
        import dataclasses

        field_names = {f.name for f in dataclasses.fields(ThinkingEvent)}
        assert "t" not in field_names


# ---------------------------------------------------------------------------
# Acceptance criterion 2 — single-line output + stdlib-only import
# ---------------------------------------------------------------------------


class TestOutputShape:
    def test_no_newline_in_emotion(self):
        line = to_jsonl(EmotionEvent(emoji="🙂"))
        assert "\n" not in line

    def test_no_newline_in_message(self):
        line = to_jsonl(MessageEvent(text="hi"))
        assert "\n" not in line

    def test_no_newline_in_thinking(self):
        line = to_jsonl(ThinkingEvent(cues=["a"], text="b"))
        assert "\n" not in line

    def test_no_trailing_newline(self):
        """to_jsonl() must NOT append a trailing newline."""
        for ev in [
            EmotionEvent(emoji="x"),
            MessageEvent(text="x"),
            ThinkingEvent(cues=[], text="x"),
        ]:
            assert not to_jsonl(ev).endswith("\n")

    def test_valid_json_for_all_three(self):
        for ev in [
            EmotionEvent(emoji="🙂", pose={"a": 1}),
            MessageEvent(text="hello"),
            ThinkingEvent(cues=["x"], text="y"),
        ]:
            parsed = json.loads(to_jsonl(ev))
            assert isinstance(parsed, dict)

    def test_stdlib_json_only(self):
        """``reachy.export.events`` must not import any third-party JSON library."""
        # Re-read the source and parse its imports via AST — avoids any
        # module-state contamination from the live import.
        src_path = REPO_ROOT / "reachy" / "export" / "events.py"
        tree = ast.parse(src_path.read_text())
        third_party = {"ujson", "orjson", "simplejson", "rapidjson"}
        for node in ast.walk(tree):
            if isinstance(node, (ast.Import, ast.ImportFrom)):
                mod = ""
                if isinstance(node, ast.Import):
                    for alias in node.names:
                        mod = alias.name.split(".")[0]
                        assert mod not in third_party, f"Forbidden import: {mod}"
                else:
                    mod = (node.module or "").split(".")[0]
                    assert mod not in third_party, f"Forbidden import: {mod}"

    def test_compact_no_spaces_around_separators(self):
        """Output must use compact separators (no space after ``,`` or ``:``))."""
        line = to_jsonl(MessageEvent(text="hi"))
        # compact JSON has no ': ' or ', ' patterns
        assert ": " not in line
        assert ", " not in line


# ---------------------------------------------------------------------------
# Acceptance criterion 3 — schema doc coverage
# ---------------------------------------------------------------------------


class TestSchemaDoc:
    def _doc_text(self) -> str:
        assert SCHEMA_DOC.exists(), f"Schema doc not found: {SCHEMA_DOC}"
        return SCHEMA_DOC.read_text(encoding="utf-8")

    def test_doc_exists(self):
        assert SCHEMA_DOC.exists()

    def test_doc_mentions_all_t_values(self):
        text = self._doc_text()
        for t_val in ("thinking", "message", "emotion"):
            assert t_val in text, f"Schema doc missing t-value: {t_val!r}"

    def test_doc_mentions_all_required_keys(self):
        text = self._doc_text()
        for key in ("emoji", "pose", "text", "cues"):
            assert key in text, f"Schema doc missing field key: {key!r}"

    def test_doc_mentions_ts_and_t_keys(self):
        text = self._doc_text()
        assert "ts" in text
        assert '"t"' in text or "`t`" in text or "| t " in text or " t " in text

    def test_doc_contains_example_lines_parseable_as_json(self):
        """The schema doc must contain at least one parseable JSON example line."""
        text = self._doc_text()
        found = 0
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("{") and stripped.endswith("}"):
                try:
                    json.loads(stripped)
                    found += 1
                except json.JSONDecodeError:
                    pass
        assert found >= 3, f"Expected at least 3 JSON example lines, found {found}"


# ---------------------------------------------------------------------------
# Union alias
# ---------------------------------------------------------------------------


class TestEventAlias:
    def test_event_alias_covers_all_three(self):
        """Event union alias must be usable as a type annotation for all three."""
        # At runtime, just assert the names are importable and are the right types.
        import dataclasses

        for cls in (EmotionEvent, MessageEvent, ThinkingEvent):
            assert dataclasses.is_dataclass(cls)

    def test_event_alias_is_exported(self):
        """``Event`` must be importable from ``reachy.export.events``."""
        # Already imported at top; just confirm it resolves.
        assert Event is not None
