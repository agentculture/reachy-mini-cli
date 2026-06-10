"""Tests for ``think``'s t8 integration: signal lifecycle, prompt vocabulary,
expression motion wiring, and the ``think expressions`` sub-noun verbs.

No real robot / daemon / LLM / TTS: collaborators are faked exactly as in
``tests/test_think.py``. The motion executor is exercised through a fake
transport that records ``move_goto`` calls.
"""

from __future__ import annotations

import argparse
import json

import pytest

from reachy.cli._commands import think as think_mod
from reachy.cli._errors import CliError
from reachy.speech import cognition_signal
from reachy.speech.expressions import Catalog


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


def _run(argv: list[str]) -> int:
    args = _build_think_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as err:
        return err.code


class _Recorder:
    def __init__(self) -> None:
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        self.played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))


class _FakeTransport:
    """Records move_goto calls so expression moves can be observed."""

    name = "fake"

    def __init__(self) -> None:
        self.moves: list[dict] = []

    def move_goto(self, **kwargs) -> None:
        self.moves.append(kwargs)


def _wire_common(monkeypatch, rec, transport, *, stream):
    """Fake the sense feed, the LLM/TTS/playback legs, and the transport."""

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.3, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)
    monkeypatch.setattr(think_mod, "get_transport", lambda args: transport)


# ---------------------------------------------------------------------------
# Criterion 1a — cognition-active signal lifecycle (set on start, cleared on exit)
# ---------------------------------------------------------------------------


def test_run_clears_cognition_signal_on_clean_exit(monkeypatch) -> None:
    rec = _Recorder()
    transport = _FakeTransport()

    def fake_stream(messages, **_kw):
        yield '"Hi there."'

    _wire_common(monkeypatch, rec, transport, stream=fake_stream)

    assert not cognition_signal.is_active()
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    # Flag must be cleared once the run returns.
    assert not cognition_signal.is_active()


def test_run_sets_cognition_signal_during_the_loop(monkeypatch) -> None:
    """The flag is active *while* the loop runs (observed mid-turn)."""
    rec = _Recorder()
    transport = _FakeTransport()
    seen: list[bool] = []

    def fake_stream(messages, **_kw):
        seen.append(cognition_signal.is_active())
        yield '"On."'

    _wire_common(monkeypatch, rec, transport, stream=fake_stream)
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    assert seen == [True], "cognition signal must be active during the loop"
    assert not cognition_signal.is_active()


def test_run_clears_cognition_signal_on_error(monkeypatch) -> None:
    """An LLM CliError still clears the signal (context manager finally)."""
    rec = _Recorder()
    transport = _FakeTransport()

    def boom_stream(messages, **_kw):
        raise CliError(code=2, message="LLM down", remediation="start it")
        yield  # pragma: no cover

    _wire_common(monkeypatch, rec, transport, stream=boom_stream)
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 2
    assert not cognition_signal.is_active()


# ---------------------------------------------------------------------------
# Criterion 1b — the prompt advertises the catalog emoji vocabulary
# ---------------------------------------------------------------------------


def test_prompt_advertises_catalog_emoji_vocabulary(monkeypatch) -> None:
    """The system prompt lists the catalog emojis + the marker convention."""
    rec = _Recorder()
    transport = _FakeTransport()
    captured: list[str] = []

    def fake_stream(messages, **_kw):
        captured.append(messages[0]["content"])  # the system prompt
        yield '"ok."'

    _wire_common(monkeypatch, rec, transport, stream=fake_stream)
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0

    sys_prompt = captured[0]
    cat = Catalog()
    expression_emojis = [k for k in cat.keys() if k != "neutral"]
    # Every catalog emoji is advertised.
    for emoji in expression_emojis:
        assert emoji in sys_prompt, f"prompt must advertise {emoji}"
    # And the marker/speech convention is described.
    assert "*" in sys_prompt and '"' in sys_prompt


def test_prompt_vocabulary_is_pulled_from_catalog_not_hardcoded(monkeypatch) -> None:
    """A non-default catalog injected into the builder changes the advertised set."""
    # build the prompt directly from a tiny custom catalog.
    prompt = think_mod._build_system_prompt(emojis=["🤖", "🎈"])
    assert "🤖" in prompt and "🎈" in prompt
    # default catalog emojis NOT in the custom list are absent.
    assert "🤔" not in prompt


# ---------------------------------------------------------------------------
# Criterion 1c — markers drive the motion queue → executor → transport.move_goto
# ---------------------------------------------------------------------------


def test_expression_marker_drives_a_move_through_the_executor(monkeypatch) -> None:
    """An LLM expression marker becomes a transport.move_goto via the queue+executor."""
    rec = _Recorder()
    transport = _FakeTransport()

    def fake_stream(messages, **_kw):
        yield '*🎉* "Yay!"'

    _wire_common(monkeypatch, rec, transport, stream=fake_stream)
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    # Only the quoted text was spoken.
    assert rec.played_texts == ["Yay!"]
    # The 🎉 marker reached the robot as a move (the executor drained the queue).
    assert len(transport.moves) >= 1, "expression marker must produce a move_goto"


def test_no_marker_means_no_move(monkeypatch) -> None:
    """Speech with no expression marker drives no motion (stillness is the posture)."""
    rec = _Recorder()
    transport = _FakeTransport()

    def fake_stream(messages, **_kw):
        yield '"Just talking, no gesture."'

    _wire_common(monkeypatch, rec, transport, stream=fake_stream)
    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    assert rec.played_texts == ["Just talking, no gesture."]
    assert transport.moves == [], "no marker → no move"


# ---------------------------------------------------------------------------
# Criterion 2 — think expressions list / check verbs
# ---------------------------------------------------------------------------


def test_expressions_list_text(capsys) -> None:
    rc = _run(["think", "expressions"])
    assert rc == 0
    out = capsys.readouterr().out
    # Lists the catalog emojis.
    for emoji in (k for k in Catalog().keys() if k != "neutral"):
        assert emoji in out


def test_expressions_list_explicit_verb(capsys) -> None:
    rc = _run(["think", "expressions", "list"])
    assert rc == 0
    assert "🤔" in capsys.readouterr().out


def test_expressions_list_json(capsys) -> None:
    rc = _run(["think", "expressions", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "expressions" in payload
    keys = [e["emoji"] for e in payload["expressions"]]
    assert "🤔" in keys
    # neutral is the fallback, not an advertised expression.
    assert "neutral" not in keys
    # each carries a descriptor.
    assert all("descriptor" in e for e in payload["expressions"])


def test_expressions_check_clean_text(capsys) -> None:
    rc = _run(["think", "expressions", "check"])
    assert rc == 0  # clean check is exit 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_expressions_check_clean_json(capsys) -> None:
    rc = _run(["think", "expressions", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["flagged"] == []


def test_expressions_check_flags_a_near_duplicate(monkeypatch, capsys) -> None:
    """When find_too_similar flags pairs, check reports them but still exits 0."""
    monkeypatch.setattr(think_mod, "_find_too_similar", lambda cat: [("🤔", "😐", 0.12)])
    rc = _run(["think", "expressions", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["flagged"] == [["🤔", "😐", 0.12]]


def test_expressions_overview_text(capsys) -> None:
    rc = _run(["think", "expressions", "overview"])
    assert rc == 0
    assert "expressions" in capsys.readouterr().out.lower()


def test_expressions_overview_json(capsys) -> None:
    rc = _run(["think", "expressions", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"]


def test_bare_expressions_lists(capsys) -> None:
    """`think expressions` with no sub-verb lists the catalog."""
    rc = _run(["think", "expressions"])
    assert rc == 0
    assert capsys.readouterr().out.strip()
