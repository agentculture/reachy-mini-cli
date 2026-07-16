"""Tests for reachy.speech.harmonic — the harmonics-cli-backed voice leg.

Tests are written test-first per the acceptance criteria:
  1. Rendering is deterministic (same input -> byte-identical PCM).
  2. Empty / whitespace-only text -> b"" (mirrors tts.synthesize's contract).
  3. Distinct identities render distinct motifs.
  4. REACHY_HARMONIC_IDENTITY / REACHY_HARMONIC_ARTICULATION env overrides are honoured.
  5. No network access is ever attempted (the whole leg is in-process).
  6. Rendering a real sentence completes well under a hard wall-clock budget.
  7. Output is valid PCM16 (even byte length, non-silent).
"""

from __future__ import annotations

import socket
import time
import urllib.request

import pytest

from reachy.cli._errors import CliError
from reachy.speech.harmonic import (
    DEFAULT_ARTICULATION,
    DEFAULT_IDENTITY,
    HARMONIC_SAMPLE_RATE,
    synthesize,
)

# ---------------------------------------------------------------------------
# Determinism
# ---------------------------------------------------------------------------


def test_synthesize_is_deterministic() -> None:
    """Two calls with the same sentence produce byte-identical, non-empty PCM."""
    sentence = "Hello there, this is Reachy speaking in harmonics."
    first = synthesize(sentence)
    second = synthesize(sentence)
    assert first == second
    assert len(first) > 0


# ---------------------------------------------------------------------------
# Empty / whitespace-only text
# ---------------------------------------------------------------------------


def test_synthesize_empty_text_returns_empty_bytes() -> None:
    assert synthesize("") == b""


def test_synthesize_whitespace_only_returns_empty_bytes() -> None:
    assert synthesize("   \n\t  ") == b""


# ---------------------------------------------------------------------------
# Identity motif distinctness
# ---------------------------------------------------------------------------


def test_different_identity_renders_different_motif() -> None:
    sentence = "The quick brown fox jumps over the lazy dog."
    reachy_pcm = synthesize(sentence, identity="reachy")
    other_pcm = synthesize(sentence, identity="other")
    assert reachy_pcm != other_pcm


def test_default_identity_matches_reachy() -> None:
    """DEFAULT_IDENTITY is 'reachy' and is what a no-arg call renders with."""
    assert DEFAULT_IDENTITY == "reachy"
    sentence = "Reachy says hello."
    default_pcm = synthesize(sentence)
    explicit_pcm = synthesize(sentence, identity="reachy")
    assert default_pcm == explicit_pcm


def test_default_articulation_is_smooth() -> None:
    assert DEFAULT_ARTICULATION == "smooth"


# ---------------------------------------------------------------------------
# Env-var configuration
# ---------------------------------------------------------------------------


def test_env_identity_override_changes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "Testing environment overrides for identity."
    baseline = synthesize(sentence)

    monkeypatch.setenv("REACHY_HARMONIC_IDENTITY", "someone-else")
    overridden = synthesize(sentence)

    assert overridden != baseline


def test_env_articulation_override_changes_output(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "Testing environment overrides for articulation."
    baseline = synthesize(sentence)

    monkeypatch.setenv("REACHY_HARMONIC_ARTICULATION", "discrete")
    overridden = synthesize(sentence)

    assert overridden != baseline


def test_explicit_identity_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "Explicit args win over env vars."
    monkeypatch.setenv("REACHY_HARMONIC_IDENTITY", "env-identity")

    via_env = synthesize(sentence)
    via_explicit = synthesize(sentence, identity="reachy")

    assert via_env != via_explicit
    assert via_explicit == synthesize(sentence, identity="reachy")


def test_explicit_articulation_arg_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    sentence = "Explicit articulation wins over env var too."
    monkeypatch.setenv("REACHY_HARMONIC_ARTICULATION", "alien")

    via_env = synthesize(sentence)
    via_explicit = synthesize(sentence, articulation="smooth")

    assert via_env != via_explicit


# ---------------------------------------------------------------------------
# No-network guard — the whole leg runs in-process
# ---------------------------------------------------------------------------


def test_synthesize_makes_no_network_call(monkeypatch: pytest.MonkeyPatch) -> None:
    """synthesize() must never touch the network; it is a pure in-process render."""

    def _fail_urlopen(*_args, **_kwargs):
        raise AssertionError("synthesize() must not call urllib.request.urlopen")

    def _fail_create_connection(*_args, **_kwargs):
        raise AssertionError("synthesize() must not call socket.create_connection")

    monkeypatch.setattr(urllib.request, "urlopen", _fail_urlopen)
    monkeypatch.setattr(socket, "create_connection", _fail_create_connection)

    result = synthesize("No network should be touched for this sentence.")
    assert len(result) > 0


# ---------------------------------------------------------------------------
# Timing
# ---------------------------------------------------------------------------


def test_synthesize_renders_long_sentence_quickly() -> None:
    """A 15+ word sentence renders well under a hard 3s wall-clock budget."""
    sentence = (
        "Reachy is a small expressive robot that listens, thinks, and speaks "
        "with a gentle harmonic voice instead of ordinary speech synthesis today."
    )
    assert len(sentence.split()) >= 15

    start = time.monotonic()
    result = synthesize(sentence)
    elapsed = time.monotonic() - start

    assert elapsed < 3.0, f"harmonic synthesize took {elapsed:.3f}s, expected < 3.0s"
    assert len(result) > 0


# ---------------------------------------------------------------------------
# PCM16 validity
# ---------------------------------------------------------------------------


def test_output_is_valid_pcm16() -> None:
    sentence = "Checking that the PCM output is well-formed."
    pcm = synthesize(sentence)

    assert len(pcm) % 2 == 0, "PCM16 output must have an even byte length"
    assert pcm != b"\x00" * len(pcm), "PCM output should not be pure silence"


def test_sample_rate_constant() -> None:
    assert HARMONIC_SAMPLE_RATE == 16000


# ---------------------------------------------------------------------------
# Broken install guard
# ---------------------------------------------------------------------------


def test_missing_harmonics_package_raises_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """If the harmonics package cannot be imported, synthesize() raises a clean CliError."""
    import builtins

    real_import = builtins.__import__

    def _fake_import(name, *args, **kwargs):
        if name == "harmonics.audio" or name.startswith("harmonics."):
            raise ImportError(f"simulated missing package: {name}")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _fake_import)

    with pytest.raises(CliError) as exc_info:
        synthesize("This should fail to import harmonics.")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation


def test_invalid_articulation_raises_user_cli_error(monkeypatch: pytest.MonkeyPatch) -> None:
    """A bad REACHY_HARMONIC_ARTICULATION surfaces as CliError exit 1, not a traceback."""
    monkeypatch.setenv("REACHY_HARMONIC_ARTICULATION", "operatic")

    with pytest.raises(CliError) as exc_info:
        synthesize("hello there")

    err = exc_info.value
    assert err.code == 1
    assert "operatic" in err.message
    assert err.remediation


def test_unreadable_wav_container_raises_env_cli_error() -> None:
    """Malformed WAV bytes from the renderer surface as CliError exit 2 (guarded parse)."""
    from reachy.speech.harmonic import _extract_pcm

    with pytest.raises(CliError) as exc_info:
        _extract_pcm(b"not a wav container at all")

    err = exc_info.value
    assert err.code == 2
    assert err.remediation
