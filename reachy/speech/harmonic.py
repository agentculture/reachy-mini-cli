"""Harmonic voice: a non-TTS speech backend built on the ``harmonics`` package.

Where :mod:`reachy.speech.tts` turns text into spoken-word audio via an
external Chatterbox HTTP endpoint, this module turns text into a short
*melody* — a sequence of notes rendered in Reachy's own identity signature —
and then into PCM16 audio. There is no network hop and no speech model: the
whole leg (meaning → notes → PCM) runs in-process, backed by the
``harmonics-cli`` PyPI package (import package ``harmonics``, pure stdlib).

Pipeline, mirroring ``harmonics``' own ``say`` command:

1. ``harmonics.cli._commands.say.render_notes`` parses ``*emphasis*``
   markers, infers prosodic axes from the clean text, resolves the speaking
   identity's voice :class:`~harmonics.identity.Signature` (root pitch +
   instrument, deterministic per identity string), and renders a
   word-tracking melodic contour shaded by those axes — a list of
   ``NoteEvent``.
2. ``harmonics.audio.render_wav`` renders that note sequence to a mono
   16-bit PCM WAV container at a chosen sample rate and articulation style.
3. This module strips the WAV container (stdlib ``wave`` + ``io.BytesIO``)
   down to bare PCM16 frames, matching the raw-PCM contract every other
   speech backend in this package returns (see
   :func:`reachy.speech.tts.synthesize`), so it is a drop-in alternative for
   :func:`~reachy.speech.playback.play_audio`.

Rendering is deterministic: the same text + identity + articulation always
produces byte-identical PCM, and it needs no network access — the
``harmonics`` runtime core is dependency-free stdlib.

Configuration (environment variables, read at call time — not import time —
so a test or caller can override per-call without reloading the module):
    ``REACHY_HARMONIC_IDENTITY``     — voice identity string (default
                                        ``"reachy"``). Distinct identities
                                        derive distinct signatures.
    ``REACHY_HARMONIC_ARTICULATION`` — rendering style: ``"discrete"``,
                                        ``"speechy"``, ``"smooth"`` (default),
                                        or ``"alien"``.

Function-argument overrides take precedence over env vars, matching the
convention in :mod:`reachy.speech.tts`.
"""

from __future__ import annotations

import io
import logging
import os
import wave

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

# Matches the robot speaker's real output rate (probe-verified: a 17-word
# sentence renders in ~0.085 s at this rate). Playback pushes PCM at the
# device rate with no resampling, so this must equal that rate — unlike
# tts.DEFAULT_SAMPLE_RATE (24 kHz), which the sdk playback path resamples
# down to 16 kHz before pushing.
HARMONIC_SAMPLE_RATE = 16000

DEFAULT_IDENTITY = "reachy"
DEFAULT_ARTICULATION = "smooth"


def _resolve_identity(override: str | None) -> str:
    """Return the voice identity: explicit arg > env var > default."""
    return override or os.environ.get("REACHY_HARMONIC_IDENTITY") or DEFAULT_IDENTITY


def _resolve_articulation(override: str | None) -> str:
    """Return the rendering articulation: explicit arg > env var > default."""
    return override or os.environ.get("REACHY_HARMONIC_ARTICULATION") or DEFAULT_ARTICULATION


def _extract_pcm(wav_bytes: bytes) -> bytes:
    """Strip a WAV container down to bare PCM16 frames.

    Guarded like :func:`reachy.speech.tts` treats its own WAV handling: a
    malformed/truncated container from the renderer surfaces as a structured
    :class:`CliError` (exit 2), never a bare ``wave.Error`` traceback.
    """
    try:
        with wave.open(io.BytesIO(wav_bytes), "rb") as wav:
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError) as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"harmonics returned an unreadable WAV container: {exc}",
            remediation=(
                "reinstall or upgrade harmonics-cli (`uv sync` or "
                "`pip install -U harmonics-cli`)"
            ),
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    text: str,
    *,
    identity: str | None = None,
    articulation: str | None = None,
) -> bytes:
    """Render *text* to Reachy's harmonic voice, returning raw PCM16 bytes.

    The raw text is passed straight to ``harmonics``' own ``render_notes`` —
    unlike :func:`reachy.speech.tts.synthesize`, this does **not** run
    :func:`reachy.speech.tts.clean_for_tts` first, because ``harmonics``'
    own emphasis parser gives musical meaning to ``*emphasis*`` markers
    (they drive melodic stress, not something to strip).

    Args:
        text:         Raw text to render (may contain ``*emphasis*`` markers).
        identity:     Override voice identity (overrides
                      ``REACHY_HARMONIC_IDENTITY`` env var).
        articulation: Override rendering style (overrides
                      ``REACHY_HARMONIC_ARTICULATION`` env var).

    Returns:
        Raw PCM16 mono bytes at ``HARMONIC_SAMPLE_RATE`` Hz, or ``b""`` if
        *text* is empty or whitespace-only.

    Raises:
        :class:`~reachy.cli._errors.CliError` (code 2) if the ``harmonics``
        package cannot be imported (reachy-mini-cli's install is broken,
        since ``harmonics-cli`` is a base dependency) or if the renderer
        returns an unreadable WAV container; (code 1) if the resolved
        articulation is not one of harmonics' styles.
    """
    if not text or not text.strip():
        log.debug("[harmonic] empty/whitespace-only text, skipping render")
        return b""

    try:
        from harmonics.audio import render_wav
        from harmonics.cli._commands.say import render_notes
    except ImportError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"harmonics package unavailable: {exc}",
            remediation=(
                "reachy-mini-cli's install is broken (harmonics-cli is a base "
                "dependency) — reinstall with `pip install --force-reinstall "
                "reachy-mini-cli` or `uv sync`"
            ),
        ) from exc

    resolved_identity = _resolve_identity(identity)
    resolved_articulation = _resolve_articulation(articulation)

    notes = render_notes(text, agent=resolved_identity)
    try:
        wav_bytes = render_wav(
            notes,
            sample_rate=HARMONIC_SAMPLE_RATE,
            articulation=resolved_articulation,
        )
    except ValueError as exc:
        # harmonics raises ValueError for an unknown articulation, naming the
        # valid choices in its message — translate to the structured error
        # contract instead of leaking a traceback (a bad
        # REACHY_HARMONIC_ARTICULATION would otherwise crash say/think/live).
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"invalid harmonic articulation {resolved_articulation!r}: {exc}",
            remediation=(
                "set REACHY_HARMONIC_ARTICULATION (or the articulation "
                "override) to one of the styles named above, e.g. smooth, "
                "discrete, speechy, alien"
            ),
        ) from exc
    return _extract_pcm(wav_bytes)
