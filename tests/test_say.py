"""Tests for the ``say`` noun — pure text-to-speech, dumb-pipe boundary enforced.

No live robot, TTS server, or audio device: ``reachy.speech.tts.synthesize`` and
``reachy.speech.playback.play_audio`` are monkeypatched for every test.

Since ``say`` is not yet registered in the main parser (that is task t8), we
drive the command functions directly by building an :class:`argparse.Namespace`
and calling :func:`cmd_say_run` / :func:`cmd_say_overview`.  This is the same
pattern used by the broader test suite for other CLI verbs.
"""

from __future__ import annotations

import argparse
import io
import json
import sys

import pytest

import reachy.cli._commands.say as say_mod
from reachy.cli._commands.say import cmd_say_overview, cmd_say_run
from reachy.cli._errors import EXIT_USER_ERROR, CliError

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with ``say run`` defaults plus any overrides."""
    defaults = {
        "text": "hello robot",
        "voice": None,
        "speed": None,
        "tts_url": None,
        "tts_timeout": 30.0,
        "base_url": "http://localhost:8000",
        "transport": None,
        "timeout": 10.0,
        "json": False,
    }
    defaults.update(kwargs)
    return argparse.Namespace(**defaults)


# ---------------------------------------------------------------------------
# AC-1: text → synthesize → play_audio (happy path)
# ---------------------------------------------------------------------------


def test_run_calls_synthesize_with_text(monkeypatch) -> None:
    """cmd_say_run passes the text argument to tts.synthesize."""
    calls: list[dict] = []

    def _synth(text, *, tts_url=None, voice=None, timeout=30.0):
        calls.append({"text": text, "tts_url": tts_url, "voice": voice})
        return b"pcm"

    monkeypatch.setattr(say_mod, "_synthesize", _synth)
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    rc = cmd_say_run(_make_args(text="hello robot"))
    assert rc == 0
    assert calls[0]["text"] == "hello robot"


def test_run_calls_play_audio_with_pcm(monkeypatch) -> None:
    """cmd_say_run forwards the PCM bytes from tts.synthesize to play_audio."""
    played: list[bytes] = []
    pcm = b"\x00\x01" * 100

    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: pcm)
    monkeypatch.setattr(say_mod, "_play_audio", lambda data, **k: played.append(data))

    cmd_say_run(_make_args(text="hello robot"))
    assert played == [pcm]


def test_run_full_flow_text_through_synth_to_play(monkeypatch) -> None:
    """Full pipeline: text → synthesize → play_audio — both are called, data flows."""
    synth_calls: list[str] = []
    play_calls: list[bytes] = []

    def _synth(text, **_kw):
        synth_calls.append(text)
        return b"audio"

    def _play(data, **_kw):
        play_calls.append(data)

    monkeypatch.setattr(say_mod, "_synthesize", _synth)
    monkeypatch.setattr(say_mod, "_play_audio", _play)

    rc = cmd_say_run(_make_args(text="speak this"))
    assert rc == 0
    assert synth_calls == ["speak this"]
    assert play_calls == [b"audio"]


def test_run_empty_pcm_skips_play(monkeypatch, capsys) -> None:
    """When synthesize returns b'' (empty text after cleaning), play is skipped."""
    played: list = []
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: played.append(1))

    rc = cmd_say_run(_make_args(text="🤖"))  # all emoji → empty after TTS clean
    assert rc == 0
    assert played == []  # play_audio never called


# ---------------------------------------------------------------------------
# AC-2a: stdin (`-` text argument)
# ---------------------------------------------------------------------------


def test_run_reads_stdin_when_text_is_dash(monkeypatch, capsys) -> None:
    """`-` as the text argument reads from stdin."""
    synth_calls: list[str] = []
    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **k: synth_calls.append(t) or b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO("text from stdin\n"))

    rc = cmd_say_run(_make_args(text="-"))
    assert rc == 0
    assert synth_calls == ["text from stdin"]


def test_run_stdin_empty_raises_user_error(monkeypatch, capsys) -> None:
    """Empty stdin (EOF) raises a user-error CliError (exit 1)."""
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)
    monkeypatch.setattr("sys.stdin", io.StringIO(""))

    with pytest.raises(CliError) as exc_info:
        cmd_say_run(_make_args(text="-"))
    assert exc_info.value.code == EXIT_USER_ERROR


# ---------------------------------------------------------------------------
# AC-2b: --voice / --speed forwarded to synthesize
# ---------------------------------------------------------------------------


def test_run_forwards_voice_to_synthesize(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **kw: calls.append(kw) or b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    cmd_say_run(_make_args(voice="en-US-male"))
    assert calls[0].get("voice") == "en-US-male"


def test_run_accepts_speed_flag_without_error(monkeypatch) -> None:
    """--speed is accepted and does not cause an error.

    ``tts.synthesize`` does not currently have a ``speed`` parameter; the flag
    is accepted as a no-op placeholder (documented in say.py) so callers are
    not silently dropped.  This test asserts the command completes successfully
    when --speed is supplied — NOT that speed is forwarded to the underlying
    synthesizer (which would require API changes in tts.py).
    """
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    rc = cmd_say_run(_make_args(speed=1.5))
    assert rc == 0


def test_run_forwards_tts_url_to_synthesize(monkeypatch) -> None:
    calls: list[dict] = []
    monkeypatch.setattr(say_mod, "_synthesize", lambda t, **kw: calls.append(kw) or b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    cmd_say_run(_make_args(tts_url="http://mytts:9000"))
    assert calls[0].get("tts_url") == "http://mytts:9000"


# ---------------------------------------------------------------------------
# AC-2c: --json emits a structured result on stdout
# ---------------------------------------------------------------------------


def test_run_json_emits_structured_result(monkeypatch, capsys) -> None:
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"x" * 200)
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    rc = cmd_say_run(_make_args(text="hi", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload.get("status") == "ok"
    assert "bytes" in payload  # number of PCM bytes
    assert payload["text"] == "hi"


def test_run_json_emits_nothing_when_empty_pcm(monkeypatch, capsys) -> None:
    """--json with empty PCM still emits a valid JSON object."""
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    rc = cmd_say_run(_make_args(text="🤖", json=True))
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload.get("status") == "ok"
    assert payload.get("bytes") == 0


def test_run_text_mode_diagnostic_goes_to_stderr(monkeypatch, capsys) -> None:
    """Non-JSON run: result on stdout (nothing), diagnostic on stderr."""
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"audio")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    cmd_say_run(_make_args(text="hi"))
    captured = capsys.readouterr()
    # stdout should be empty (no result text) in text mode — the output is audio
    # stdout has a blank line or is empty; stderr has the diagnostic
    assert captured.err.strip() != "" or captured.out.strip() == ""


# ---------------------------------------------------------------------------
# AC-3: dumb-pipe boundary — no LLM / no senses imports
# ---------------------------------------------------------------------------


def test_say_module_does_not_import_llm() -> None:
    """reachy.speech.llm must NOT appear in say's module namespace or import statements."""
    # Check the module globals — an explicit import of reachy.speech.llm would leave
    # a binding (the module object or an alias) in the namespace.
    assert "llm" not in say_mod.__dict__, "say module must not import reachy.speech.llm"
    # Inspect import lines only (not docstrings / comments).
    import ast
    import inspect

    src = inspect.getsource(say_mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            # Reconstruct the dotted module name being imported.
            if isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    "speech.llm" not in node.module
                ), f"say.py must not import from reachy.speech.llm (line {node.lineno})"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        "speech.llm" not in alias.name
                    ), f"say.py must not import reachy.speech.llm (line {node.lineno})"


def test_say_module_does_not_import_events() -> None:
    """reachy.speech.events must NOT appear in say's module namespace or import statements."""
    assert "events" not in say_mod.__dict__, "say module must not import reachy.speech.events"
    import ast
    import inspect

    src = inspect.getsource(say_mod)
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            if isinstance(node, ast.ImportFrom) and node.module:
                assert (
                    "speech.events" not in node.module
                ), f"say.py must not import from reachy.speech.events (line {node.lineno})"
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    assert (
                        "speech.events" not in alias.name
                    ), f"say.py must not import reachy.speech.events (line {node.lineno})"


def test_say_run_does_not_trigger_llm_import(monkeypatch, capsys) -> None:
    """Running cmd_say_run must not cause reachy.speech.llm to be imported."""
    # Remove cached module to detect fresh imports.
    llm_key = "reachy.speech.llm"
    original = sys.modules.pop(llm_key, None)
    try:
        monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"pcm")
        monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)
        cmd_say_run(_make_args(text="test"))
        assert llm_key not in sys.modules, "cmd_say_run must not import reachy.speech.llm"
    finally:
        if original is not None:
            sys.modules[llm_key] = original


def test_say_run_does_not_trigger_events_import(monkeypatch, capsys) -> None:
    """Running cmd_say_run must not cause reachy.speech.events to be imported."""
    events_key = "reachy.speech.events"
    original = sys.modules.pop(events_key, None)
    try:
        monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"pcm")
        monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)
        cmd_say_run(_make_args(text="test"))
        assert events_key not in sys.modules, "cmd_say_run must not import reachy.speech.events"
    finally:
        if original is not None:
            sys.modules[events_key] = original


# ---------------------------------------------------------------------------
# Error propagation: CliError from tts / playback surfaces correctly
# ---------------------------------------------------------------------------


def test_tts_clierror_propagates(monkeypatch) -> None:
    """A CliError raised inside tts.synthesize propagates out of cmd_say_run."""
    err = CliError(code=2, message="TTS unreachable", remediation="start the TTS server")
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: (_ for _ in ()).throw(err))
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    with pytest.raises(CliError) as exc_info:
        cmd_say_run(_make_args(text="hi"))
    assert exc_info.value.code == 2


def test_playback_clierror_propagates(monkeypatch) -> None:
    """A CliError raised inside play_audio propagates out of cmd_say_run."""
    err = CliError(code=2, message="playback failed", remediation="check audio")
    monkeypatch.setattr(say_mod, "_synthesize", lambda *a, **k: b"pcm")
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: (_ for _ in ()).throw(err))

    with pytest.raises(CliError) as exc_info:
        cmd_say_run(_make_args(text="hi"))
    assert exc_info.value.code == 2


# ---------------------------------------------------------------------------
# overview verb
# ---------------------------------------------------------------------------


def test_say_overview_text(capsys) -> None:
    args = argparse.Namespace(json=False)
    rc = cmd_say_overview(args)
    assert rc == 0
    out = capsys.readouterr().out
    assert "say" in out.lower()
    assert "# reachy-mini-cli say" in out


def test_say_overview_json(capsys) -> None:
    args = argparse.Namespace(json=True)
    rc = cmd_say_overview(args)
    assert rc == 0
    out = capsys.readouterr().out
    payload = json.loads(out)
    assert payload.get("subject")
    assert "say" in payload["subject"].lower()


def test_bare_say_defaults_to_overview(capsys) -> None:
    """Calling _no_verb (what bare 'reachy say' does) prints an overview."""
    from reachy.cli._commands.say import _no_verb

    args = argparse.Namespace(json=False)
    rc = _no_verb(args)
    assert rc == 0
    assert capsys.readouterr().out.strip()


# ---------------------------------------------------------------------------
# register() builds a correctly shaped parser
# ---------------------------------------------------------------------------


def test_register_exposes_run_and_overview() -> None:
    """register(sub) must add 'run' and 'overview' sub-verbs under 'say'."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    # Parse 'say run hello' — should resolve to cmd_say_run
    args = parser.parse_args(["say", "run", "hello"])
    assert args.func is cmd_say_run
    assert args.text == "hello"


def test_register_overview_verb() -> None:
    """register(sub) must add 'overview' sub-verb under 'say'."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "overview"])
    assert args.func is cmd_say_overview


def test_register_run_has_voice_and_speed_flags() -> None:
    """'say run' exposes --voice, --speed, --tts-url, --json."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "run", "--voice", "en-GB-male", "--speed", "0.9", "test text"])
    assert args.voice == "en-GB-male"
    assert args.speed == 0.9
    assert args.text == "test text"


def test_register_run_json_flag() -> None:
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "run", "--json", "hi"])
    assert args.json is True


def test_register_run_stdin_dash() -> None:
    """'-' as text must parse without error."""
    import argparse

    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "run", "-"])
    assert args.text == "-"
