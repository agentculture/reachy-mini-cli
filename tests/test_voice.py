"""Tests for reachy.speech.voice — the tts/harmonic engine resolver.

Tests are written test-first per the acceptance criteria:
  1. resolve_voice_engine(None) with no env var -> the "tts" engine.
  2. resolve_voice_engine("harmonic") -> the "harmonic" engine.
  3. REACHY_VOICE_ENGINE env var selects the engine; an explicit name overrides it.
  4. An unknown engine name raises CliError(code=1).
"""

from __future__ import annotations

import pytest

import reachy.speech.harmonic as harmonic_mod
import reachy.speech.tts as tts_mod
from reachy.cli._errors import CliError
from reachy.speech.voice import VOICE_ENGINE_ENV, resolve_voice_engine

# ---------------------------------------------------------------------------
# Default resolution
# ---------------------------------------------------------------------------


def test_resolve_default_is_tts(monkeypatch: pytest.MonkeyPatch) -> None:
    """No explicit name, no env var -> the "tts" engine, 24kHz, tts.synthesize."""
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)

    engine = resolve_voice_engine(None)

    assert engine.name == "tts"
    assert engine.samplerate == tts_mod.DEFAULT_SAMPLE_RATE
    assert engine.samplerate == 24000
    assert engine.synthesize is tts_mod.synthesize


def test_resolve_default_no_args(monkeypatch: pytest.MonkeyPatch) -> None:
    """Calling with no arguments at all behaves the same as passing None."""
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)

    engine = resolve_voice_engine()

    assert engine.name == "tts"


# ---------------------------------------------------------------------------
# Explicit "harmonic" selection
# ---------------------------------------------------------------------------


def test_resolve_explicit_harmonic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)

    engine = resolve_voice_engine("harmonic")

    assert engine.name == "harmonic"
    assert engine.samplerate == harmonic_mod.HARMONIC_SAMPLE_RATE
    assert engine.samplerate == 16000
    assert engine.synthesize is harmonic_mod.synthesize


# ---------------------------------------------------------------------------
# Env-var selection + explicit-arg precedence
# ---------------------------------------------------------------------------


def test_env_var_selects_harmonic(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")

    engine = resolve_voice_engine(None)

    assert engine.name == "harmonic"


def test_explicit_name_overrides_env_var(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")

    engine = resolve_voice_engine("tts")

    assert engine.name == "tts"


def test_env_var_selects_tts_explicitly(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VOICE_ENGINE_ENV, "tts")

    engine = resolve_voice_engine(None)

    assert engine.name == "tts"


# ---------------------------------------------------------------------------
# Unknown engine name
# ---------------------------------------------------------------------------


def test_unknown_engine_name_raises_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)

    with pytest.raises(CliError) as exc_info:
        resolve_voice_engine("nonexistent-engine")

    err = exc_info.value
    assert err.code == 1
    assert err.remediation
    assert "tts" in err.remediation
    assert "harmonic" in err.remediation


def test_unknown_engine_name_from_env_raises_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv(VOICE_ENGINE_ENV, "bogus")

    with pytest.raises(CliError) as exc_info:
        resolve_voice_engine(None)

    assert exc_info.value.code == 1
