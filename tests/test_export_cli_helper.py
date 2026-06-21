"""Tests for the shared export-hook CLI helper (``reachy.cli._export``).

``think run`` and ``listen run --live`` both build their JSONL export sink through
``build_export_hook`` so the two feeds are byte-identical. The helper:

- returns ``None`` when ``--export`` is absent (the no-op default),
- rejects any target other than ``-`` (stdout) with a clean exit-1 user error,
- emits matching events to the injected stream and honours ``--export-blocks``.
"""

from __future__ import annotations

import argparse
import io
import json

import pytest

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._export import build_export_hook
from reachy.export.events import EmotionEvent, MessageEvent


def _args(**kw) -> argparse.Namespace:
    kw.setdefault("export", None)
    kw.setdefault("export_blocks", None)
    return argparse.Namespace(**kw)


def test_absent_export_returns_none():
    assert build_export_hook(_args()) is None


def test_unsupported_target_is_user_error():
    with pytest.raises(CliError) as exc:
        build_export_hook(_args(export="/tmp/feed.jsonl"))
    assert exc.value.code == EXIT_USER_ERROR
    assert "stdout" in exc.value.remediation


def test_stdout_target_emits_jsonl_to_injected_stream():
    stream = io.StringIO()
    hook = build_export_hook(_args(export="-"), stream=stream)
    assert hook is not None

    hook.emit(MessageEvent(text="hello", ts=1.0))
    line = stream.getvalue().strip()
    obj = json.loads(line)
    assert obj["t"] == "message"
    assert obj["text"] == "hello"


def test_export_blocks_selection_is_honoured():
    stream = io.StringIO()
    hook = build_export_hook(_args(export="-", export_blocks="message"), stream=stream)
    assert hook is not None

    hook.emit(EmotionEvent(emoji="🎉", pose=None, ts=1.0))  # filtered out
    hook.emit(MessageEvent(text="kept", ts=1.0))  # in selection

    lines = [json.loads(line) for line in stream.getvalue().splitlines() if line.strip()]
    assert [obj["t"] for obj in lines] == ["message"]


def test_pose_resolver_returns_none_for_unknown_emoji():
    hook = build_export_hook(_args(export="-"), stream=io.StringIO())
    assert hook is not None
    assert hook.pose_resolver("🟦_not_a_real_emoji") is None
