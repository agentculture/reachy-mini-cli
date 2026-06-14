"""Tests for the ``--export`` / ``--export-blocks`` flags on ``think run``.

Verifies:
1. ``think run --export -`` writes valid JSONL to stdout (all three block types).
2. ``--export-blocks thinking,message`` filters to only those types.
3. ``--export foo`` exits 1 (invalid sink); ``--export-blocks bogus`` exits 1.
4. Without ``--export``, no JSONL is emitted to stdout (existing behaviour is
   preserved).
5. The status summary is on stderr (not stdout) when exporting to stdout.
6. Real ``CognitionEngine`` with stubbed collaborators emits real JSONL (end-to-end
   wiring test).

No real robot, LLM, TTS, or audio device is used — all collaborators are faked.
"""

from __future__ import annotations

import argparse
import io
import json
import sys

import pytest

from reachy.cli._commands import think as think_mod
from reachy.cli._errors import EXIT_USER_ERROR, CliError

# ---------------------------------------------------------------------------
# Helpers shared with test_think.py
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _build_think_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="reachy-mini-cli")
    sub = parser.add_subparsers(dest="command")
    think_mod.register(sub)
    return parser


def _run(argv: list[str]) -> tuple[int, str, str]:
    """Parse + dispatch a ``think ...`` argv and return (rc, stdout, stderr).

    We capture stdout/stderr manually by swapping sys.stdout/sys.stderr because
    the CLI routes JSONL to sys.stdout and status to sys.stderr directly.
    Returns (exit_code, stdout_text, stderr_text).
    """
    out_buf = io.StringIO()
    err_buf = io.StringIO()
    orig_out, orig_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out_buf, err_buf
    try:
        args = _build_think_parser().parse_args(argv)
        try:
            rc = args.func(args)
        except CliError as err:
            rc = err.code
    finally:
        sys.stdout, sys.stderr = orig_out, orig_err
    return rc, out_buf.getvalue(), err_buf.getvalue()


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _NoMotion:
    """Motion executor stub — does nothing but satisfies the interface."""

    queue = None

    def express(self, emoji: str) -> None:  # noqa: D102
        pass

    def start(self) -> None:  # noqa: D102
        pass

    def stop(self) -> None:  # noqa: D102
        pass


def _install_fakes(monkeypatch, *, stream_fn=None):
    """Patch the three heavyweight collaborators and the motion executor.

    *stream_fn* is the LLM stream fake.  Defaults to a one-sentence marked
    stream that contains one expression marker, one speech span, and raw prose
    (which becomes the ThinkingEvent.text).
    """

    def default_stream(messages, **_kw):
        # Yields a marked stream: one expression + one speech.
        yield '*😮* "Hello world." thinking prose'

    monkeypatch.setattr(
        think_mod,
        "_make_sense_feed",
        lambda args, buf: lambda: buf.feed_doa(angle_rad=0.0, rms=0.3, is_speech=True),
    )
    monkeypatch.setattr(think_mod, "_make_motion_executor", lambda args: _NoMotion())
    monkeypatch.setattr(think_mod, "_stream_sentences", stream_fn or default_stream)
    monkeypatch.setattr(think_mod, "_synthesize", lambda text, **_kw: b"pcm")
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **_kw: None)


# ---------------------------------------------------------------------------
# 1. think run --export - emits valid JSONL to stdout; all three block types
# ---------------------------------------------------------------------------


def test_export_dash_writes_jsonl_all_blocks(monkeypatch) -> None:
    """All three block types appear in stdout as valid JSONL when exporting."""
    _install_fakes(monkeypatch)

    rc, stdout, stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    assert rc == 0, f"expected exit 0, got {rc}; stderr={stderr}"
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "expected JSONL output on stdout"

    parsed = [json.loads(line) for line in lines]
    types_seen = {obj["t"] for obj in parsed}

    assert "emotion" in types_seen, f"no emotion block in {types_seen}"
    assert "message" in types_seen, f"no message block in {types_seen}"
    assert "thinking" in types_seen, f"no thinking block in {types_seen}"

    # Every line must have at least t and ts fields.
    for obj in parsed:
        assert "t" in obj
        assert "ts" in obj


def test_export_dash_status_summary_not_on_stdout(monkeypatch) -> None:
    """The status/summary line must NOT appear in stdout when exporting."""
    _install_fakes(monkeypatch)

    _rc, stdout, _stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    # Anything non-JSON on stdout would break a downstream consumer.
    for line in stdout.splitlines():
        if not line.strip():
            continue
        # Each line must parse as JSON.
        try:
            json.loads(line)
        except json.JSONDecodeError:
            pytest.fail(f"non-JSON line on stdout: {line!r}")


def test_export_dash_summary_goes_to_stderr(monkeypatch) -> None:
    """When exporting to stdout, the run completion message goes to stderr."""
    _install_fakes(monkeypatch)

    _rc, _stdout, stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    # The summary should mention turns or status — it should be on stderr.
    assert (
        "think" in stderr.lower() or "turn" in stderr.lower() or "stop" in stderr.lower()
    ), f"expected status on stderr, got: {stderr!r}"


# ---------------------------------------------------------------------------
# 2. --export-blocks filters the stdout feed
# ---------------------------------------------------------------------------


def test_export_blocks_filters_to_requested_types(monkeypatch) -> None:
    """--export-blocks thinking,message excludes emotion blocks."""
    _install_fakes(monkeypatch)

    rc, stdout, _stderr = _run(
        ["think", "run", "--export", "-", "--export-blocks", "thinking,message", "--max-turns", "1"]
    )

    assert rc == 0
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parsed = [json.loads(line) for line in lines]
    types_seen = {obj["t"] for obj in parsed}

    assert "emotion" not in types_seen, "emotion should be filtered out"
    assert (
        "thinking" in types_seen or "message" in types_seen
    ), f"expected at least thinking or message, got {types_seen}"


def test_export_blocks_single_type(monkeypatch) -> None:
    """--export-blocks thinking emits only thinking blocks."""
    _install_fakes(monkeypatch)

    rc, stdout, _stderr = _run(
        ["think", "run", "--export", "-", "--export-blocks", "thinking", "--max-turns", "1"]
    )

    assert rc == 0
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parsed = [json.loads(line) for line in lines]
    types_seen = {obj["t"] for obj in parsed}

    assert types_seen <= {"thinking"}, f"unexpected block types: {types_seen}"
    assert "thinking" in types_seen


# ---------------------------------------------------------------------------
# 3. Invalid --export / --export-blocks values exit 1
# ---------------------------------------------------------------------------


def test_invalid_export_target_exits_1(monkeypatch) -> None:
    """--export foo exits 1 with a CliError (unsupported sink)."""
    _install_fakes(monkeypatch)

    rc, _stdout, _stderr = _run(["think", "run", "--export", "foo", "--max-turns", "1"])

    assert rc == EXIT_USER_ERROR, f"expected exit 1, got {rc}"


def test_invalid_export_target_raises_cli_error_with_hint(monkeypatch) -> None:
    """The CliError raised for --export foo carries a hint mentioning '-'."""
    _install_fakes(monkeypatch)

    parser = _build_think_parser()
    args = parser.parse_args(["think", "run", "--export", "foo", "--max-turns", "1"])
    with pytest.raises(CliError) as exc_info:
        args.func(args)

    err = exc_info.value
    assert err.code == EXIT_USER_ERROR
    # Remediation must mention '-' (stdout) so an agent knows the valid sink.
    assert (
        "-" in err.remediation or "stdout" in err.remediation
    ), f"expected '-' or 'stdout' in remediation, got: {err.remediation!r}"


def test_invalid_export_blocks_value_exits_1(monkeypatch) -> None:
    """--export-blocks bogus exits 1 (via parse_blocks CliError)."""
    _install_fakes(monkeypatch)

    rc, _stdout, _stderr = _run(
        ["think", "run", "--export", "-", "--export-blocks", "bogus", "--max-turns", "1"]
    )

    assert rc == EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# 4. Without --export, no JSONL on stdout; engine built without export
# ---------------------------------------------------------------------------


def test_no_export_no_jsonl_on_stdout(monkeypatch, capsys) -> None:
    """Without --export the stdout output is the standard summary (or empty), not JSONL."""
    _install_fakes(monkeypatch)

    rc, stdout, _stderr = _run(["think", "run", "--json", "--max-turns", "1"])

    assert rc == 0
    # stdout should be the JSON summary, not JSONL event stream.
    # The summary is a single JSON object (not multiple event lines).
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert len(lines) == 1, f"expected exactly one JSON line (summary), got: {lines}"
    payload = json.loads(lines[0])
    # Summary payload has 'turns', not 't' (which is an event field).
    assert "turns" in payload
    assert "t" not in payload


def test_no_export_engine_receives_no_export_hook(monkeypatch) -> None:
    """Without --export, the CognitionEngine is constructed with export=None.

    The export seam is a single ``ExportHook`` parameter (emit + pose_resolver +
    time_fn bundled), so the only thing to assert is that it is ``None``.
    """
    built_engines: list[dict] = []

    original_init = think_mod.CognitionEngine.__init__

    def capturing_init(self, **kwargs):
        built_engines.append({"export": kwargs.get("export")})
        original_init(self, **kwargs)

    _install_fakes(monkeypatch)
    monkeypatch.setattr(think_mod.CognitionEngine, "__init__", capturing_init)

    _run(["think", "run", "--max-turns", "1"])

    assert built_engines, "engine was never constructed"
    assert built_engines[0]["export"] is None


# ---------------------------------------------------------------------------
# 5. Real CognitionEngine + stubbed collaborators emits real JSONL (end-to-end)
# ---------------------------------------------------------------------------


def test_real_engine_export_produces_jsonl(monkeypatch) -> None:
    """Drives think run through the real CognitionEngine (not patched) with stubbed
    stream/synth/play to verify the full wiring from CLI args → exporter → stdout."""
    _install_fakes(monkeypatch)

    rc, stdout, stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    assert rc == 0, f"expected exit 0; stderr={stderr}"
    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    assert lines, "expected JSONL on stdout"

    # All lines must be valid JSON with at least t and ts.
    parsed = [json.loads(line) for line in lines]
    for obj in parsed:
        assert "t" in obj, f"missing 't' in {obj}"
        assert "ts" in obj, f"missing 'ts' in {obj}"
        assert obj["t"] in ("thinking", "message", "emotion"), f"unknown block type: {obj['t']}"

    types_seen = {obj["t"] for obj in parsed}
    # The marked stream '*😮* "Hello world." thinking prose' should produce all three types.
    assert "emotion" in types_seen
    assert "message" in types_seen
    assert "thinking" in types_seen


def test_real_engine_export_emotion_has_pose(monkeypatch) -> None:
    """EmotionEvent blocks include pose data from the pose_resolver (catalog lookup)."""
    _install_fakes(monkeypatch)

    _rc, stdout, _stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parsed = [json.loads(line) for line in lines]
    emotion_blocks = [obj for obj in parsed if obj["t"] == "emotion"]

    assert emotion_blocks, "expected at least one emotion block"
    # The emoji in the stream is '😮'; it's in the default catalog so pose must be
    # a non-None dict (a known emoji resolves to a real pose).
    for block in emotion_blocks:
        assert "emoji" in block
        assert block["pose"] is not None, "known emoji should carry a resolved pose"


def test_real_engine_export_unknown_emoji_pose_is_null(monkeypatch) -> None:
    """An emoji NOT in the catalog yields pose=null (Qodo #1 fix).

    ``Catalog.get()`` falls back to the neutral pose for unknown keys, but the
    export schema requires ``"pose": null`` for unknown emoji so consumers can
    detect them. The pose_resolver guards on ``emoji in catalog``.
    """

    def unknown_emoji_stream(messages, **_kw):
        # '🦄' (unicorn) is not in the starter expression catalog.
        yield '*🦄* "surprise."'

    _install_fakes(monkeypatch, stream_fn=unknown_emoji_stream)

    _rc, stdout, _stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    parsed = [json.loads(ln) for ln in stdout.splitlines() if ln.strip()]
    emotion_blocks = [obj for obj in parsed if obj["t"] == "emotion"]

    assert emotion_blocks, "expected at least one emotion block"
    for block in emotion_blocks:
        assert block["emoji"] == "🦄"
        assert block["pose"] is None, "unknown emoji must export pose=null"


def test_real_engine_export_thinking_carries_raw_text(monkeypatch) -> None:
    """ThinkingEvent.text is the full raw LLM output, not just the spoken portion."""
    _install_fakes(monkeypatch)

    _rc, stdout, _stderr = _run(["think", "run", "--export", "-", "--max-turns", "1"])

    lines = [ln for ln in stdout.splitlines() if ln.strip()]
    parsed = [json.loads(line) for line in lines]
    thinking_blocks = [obj for obj in parsed if obj["t"] == "thinking"]

    assert thinking_blocks, "expected at least one thinking block"
    raw_text = thinking_blocks[0]["text"]
    # The raw LLM output includes the markers and prose from the fake stream.
    # The fake yields: '*😮* "Hello world." thinking prose'
    assert (
        "thinking prose" in raw_text or "Hello world" in raw_text or "😮" in raw_text
    ), f"expected raw LLM text in ThinkingEvent, got: {raw_text!r}"
