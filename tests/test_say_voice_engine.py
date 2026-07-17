"""Tests for ``say run --voice-engine {tts,harmonic}`` wiring (task t3).

Mirrors the harness style of ``tests/test_say.py``: no live robot / TTS server
/ audio device — ``say_mod._synthesize`` and ``say_mod._play_audio`` are
monkeypatched, and ``cmd_say_run`` is driven directly via an
:class:`argparse.Namespace`.

The harmonic engine itself is NOT monkeypatched here (it is deterministic,
offline, pure-stdlib+``harmonics-cli`` — see ``reachy/speech/harmonic.py`` and
its own ``tests/test_harmonic.py``); these tests instead prove that selecting
``--voice-engine harmonic`` never reaches the tts leg (``reachy.speech.tts.synthesize``
/ the ``say_mod._synthesize`` alias) and that the real harmonic PCM is handed to
playback at the harmonic sample rate.
"""

from __future__ import annotations

import argparse

import pytest

import reachy.cli._commands.say as say_mod
import reachy.speech.harmonic as harmonic_mod
import reachy.speech.tts as tts_mod
from reachy.cli._commands.say import cmd_say_run
from reachy.cli._errors import CliError
from reachy.speech.voice import VOICE_ENGINE_ENV

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_args(**kwargs) -> argparse.Namespace:
    """Build a Namespace with ``say run`` defaults plus any overrides.

    Includes ``voice_engine`` (absent from ``test_say.py``'s ``_make_args``,
    which predates this flag) so every test here exercises the same Namespace
    shape ``register()`` produces via argparse.
    """
    defaults = {
        "text": "hi",
        "voice_engine": None,
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


def _forbid_tts_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make any call into the real tts leg fail the test loudly.

    Patches both the module alias (``say_mod._synthesize``) and the underlying
    ``reachy.speech.tts.synthesize`` function, so a wrong branch is caught
    regardless of whether ``say.py`` calls through the alias or a fresh import.
    """

    def _boom(*_a, **_k):
        raise AssertionError("tts leg must not be called under --voice-engine harmonic")

    monkeypatch.setattr(say_mod, "_synthesize", _boom)
    monkeypatch.setattr(tts_mod, "synthesize", _boom)


# ---------------------------------------------------------------------------
# --voice-engine harmonic: plays real harmonic PCM at the harmonic samplerate,
# never touches the tts leg
# ---------------------------------------------------------------------------


def test_harmonic_engine_plays_pcm_at_harmonic_samplerate(monkeypatch) -> None:
    """--voice-engine harmonic: playback receives non-empty PCM, samplerate=16000."""
    _forbid_tts_call(monkeypatch)
    played: list[dict] = []
    monkeypatch.setattr(
        say_mod,
        "_play_audio",
        lambda data, **kw: played.append({"data": data, "kwargs": kw}),
    )

    rc = cmd_say_run(_make_args(text="hi", voice_engine="harmonic"))

    assert rc == 0
    assert len(played) == 1
    assert len(played[0]["data"]) > 0
    assert played[0]["kwargs"].get("samplerate") == 16000
    assert played[0]["kwargs"]["samplerate"] == harmonic_mod.HARMONIC_SAMPLE_RATE


def test_harmonic_engine_no_tts_call_made(monkeypatch) -> None:
    """--voice-engine harmonic never invokes the tts synthesize leg."""
    _forbid_tts_call(monkeypatch)
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    # If say.py mistakenly called the tts leg, _forbid_tts_call's stub raises
    # AssertionError, which would propagate out of cmd_say_run and fail here.
    rc = cmd_say_run(_make_args(text="hi", voice_engine="harmonic"))
    assert rc == 0


def test_harmonic_engine_ignores_voice_and_speed_flags(monkeypatch) -> None:
    """--voice-engine harmonic --voice somebody --speed 2 still runs the harmonic leg."""
    _forbid_tts_call(monkeypatch)
    played: list[dict] = []
    monkeypatch.setattr(
        say_mod,
        "_play_audio",
        lambda data, **kw: played.append({"data": data, "kwargs": kw}),
    )

    rc = cmd_say_run(
        _make_args(
            text="hi",
            voice_engine="harmonic",
            voice="somebody",
            speed=2.0,
            tts_url="http://ignored:1234",
        )
    )

    assert rc == 0
    assert len(played) == 1
    assert len(played[0]["data"]) > 0
    assert played[0]["kwargs"].get("samplerate") == 16000


def test_harmonic_engine_empty_text_skips_play(monkeypatch) -> None:
    """Whitespace-only text renders to b'' (harmonic contract) and play is skipped."""
    _forbid_tts_call(monkeypatch)
    played: list = []
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: played.append(1))

    rc = cmd_say_run(_make_args(text="   ", voice_engine="harmonic"))
    assert rc == 0
    assert played == []


# ---------------------------------------------------------------------------
# REACHY_VOICE_ENGINE env fallback + explicit-flag precedence
# ---------------------------------------------------------------------------


def test_env_var_selects_harmonic_with_no_flag(monkeypatch) -> None:
    """REACHY_VOICE_ENGINE=harmonic with no --voice-engine flag -> harmonic leg."""
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")
    _forbid_tts_call(monkeypatch)
    played: list[dict] = []
    monkeypatch.setattr(
        say_mod,
        "_play_audio",
        lambda data, **kw: played.append({"data": data, "kwargs": kw}),
    )

    rc = cmd_say_run(_make_args(text="hi", voice_engine=None))

    assert rc == 0
    assert len(played) == 1
    assert played[0]["kwargs"].get("samplerate") == 16000


def test_explicit_tts_flag_overrides_harmonic_env(monkeypatch) -> None:
    """--voice-engine tts overrides REACHY_VOICE_ENGINE=harmonic back to tts."""
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")
    calls: list[dict] = []
    played: list = []

    def _synth(text, *, tts_url=None, voice=None, timeout=30.0):
        calls.append({"text": text, "tts_url": tts_url, "voice": voice, "timeout": timeout})
        return b"tts-pcm"

    monkeypatch.setattr(say_mod, "_synthesize", _synth)
    monkeypatch.setattr(say_mod, "_play_audio", lambda data, **kw: played.append(kw))

    rc = cmd_say_run(_make_args(text="hi", voice_engine="tts"))

    assert rc == 0
    assert len(calls) == 1
    assert calls[0]["text"] == "hi"
    # tts leg: byte-identical playback kwargs to pre-voice-engine behaviour —
    # no samplerate override (play_audio's own 24000 default applies).
    assert "samplerate" not in played[0]


# ---------------------------------------------------------------------------
# Unknown engine name -> CliError (exit 1, error+hint contract)
# ---------------------------------------------------------------------------


def test_unknown_voice_engine_raises_cli_error(monkeypatch) -> None:
    """An unrecognised --voice-engine value raises CliError(code=1)."""
    _forbid_tts_call(monkeypatch)
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    args = _make_args(text="hi", voice_engine="nonexistent-engine")
    with pytest.raises(CliError) as exc_info:
        cmd_say_run(args)

    err = exc_info.value
    assert err.code == 1
    assert err.message
    assert err.remediation


def test_unknown_voice_engine_from_env_raises_cli_error(monkeypatch) -> None:
    """An unrecognised REACHY_VOICE_ENGINE value raises CliError(code=1)."""
    monkeypatch.setenv(VOICE_ENGINE_ENV, "bogus-engine")
    _forbid_tts_call(monkeypatch)
    monkeypatch.setattr(say_mod, "_play_audio", lambda *a, **k: None)

    args = _make_args(text="hi", voice_engine=None)
    with pytest.raises(CliError) as exc_info:
        cmd_say_run(args)

    assert exc_info.value.code == 1


# ---------------------------------------------------------------------------
# Default-path regression: no flag, no env -> tts synthesize alias is called
# ---------------------------------------------------------------------------


def test_default_no_flag_no_env_uses_tts_synthesize_alias(monkeypatch) -> None:
    """With no --voice-engine flag and no REACHY_VOICE_ENGINE env, the tts leg runs."""
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)
    calls: list[dict] = []
    played: list = []

    def _synth(text, *, tts_url=None, voice=None, timeout=30.0):
        calls.append({"text": text})
        return b"tts-pcm"

    monkeypatch.setattr(say_mod, "_synthesize", _synth)
    monkeypatch.setattr(say_mod, "_play_audio", lambda data, **kw: played.append(kw))

    rc = cmd_say_run(_make_args(text="hi", voice_engine=None))

    assert rc == 0
    assert calls == [{"text": "hi"}]
    assert "samplerate" not in played[0]


# ---------------------------------------------------------------------------
# register(): --voice-engine parses and defaults to None
# ---------------------------------------------------------------------------


def test_register_voice_engine_flag_defaults_to_none() -> None:
    """'say run' with no --voice-engine flag parses with voice_engine=None."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "run", "hello"])
    assert args.voice_engine is None


def test_register_voice_engine_flag_accepts_harmonic() -> None:
    """'say run --voice-engine harmonic' parses successfully."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    args = parser.parse_args(["say", "run", "--voice-engine", "harmonic", "hello"])
    assert args.voice_engine == "harmonic"


def test_register_voice_engine_flag_rejects_bad_choice() -> None:
    """argparse rejects a --voice-engine value outside {tts,harmonic} at parse time."""
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers(dest="noun")
    say_mod.register(sub)

    with pytest.raises(SystemExit):
        parser.parse_args(["say", "run", "--voice-engine", "nonexistent-engine", "hello"])
