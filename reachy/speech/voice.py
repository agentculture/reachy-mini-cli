"""Voice engine resolver — picks between the TTS and harmonic speech backends.

``say``, ``think``, and any future speech-emitting noun each need to answer
the same question — "which synthesize() function, and at what sample rate,
should I hand to playback?" — so this module answers it once instead of
letting each command module triplicate the selection logic.

Two engines are registered today:

* ``"tts"``      — :func:`reachy.speech.tts.synthesize` (external Chatterbox
                    HTTP endpoint, PCM16 @ 24 kHz). The default.
* ``"harmonic"`` — :func:`reachy.speech.harmonic.synthesize` (in-process
                    note-melody rendering, PCM16 @ 16 kHz). See
                    :mod:`reachy.speech.harmonic`.

This module intentionally imports neither :mod:`reachy.speech.llm` nor
:mod:`reachy.speech.events` — ``say``'s import-boundary test asserts those
never appear in ``say``'s import graph, and ``voice`` must stay safe to
import from ``say`` as well as ``think``.
"""

from __future__ import annotations

import os
from collections.abc import Callable
from dataclasses import dataclass

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.speech.harmonic import HARMONIC_SAMPLE_RATE
from reachy.speech.harmonic import synthesize as _harmonic_synthesize
from reachy.speech.tts import DEFAULT_SAMPLE_RATE as _TTS_SAMPLE_RATE
from reachy.speech.tts import synthesize as _tts_synthesize

VOICE_ENGINE_ENV = "REACHY_VOICE_ENGINE"

DEFAULT_ENGINE = "tts"


@dataclass(frozen=True)
class VoiceEngine:
    """A resolved speech backend: its name, ``synthesize`` callable, and sample rate."""

    name: str
    synthesize: Callable[..., bytes]
    samplerate: int


_ENGINES: dict[str, VoiceEngine] = {
    "tts": VoiceEngine(name="tts", synthesize=_tts_synthesize, samplerate=_TTS_SAMPLE_RATE),
    "harmonic": VoiceEngine(
        name="harmonic", synthesize=_harmonic_synthesize, samplerate=HARMONIC_SAMPLE_RATE
    ),
}


def resolve_voice_engine(name: str | None = None) -> VoiceEngine:
    """Resolve the :class:`VoiceEngine` to use: explicit *name* > env var > ``"tts"``.

    Args:
        name: Explicit engine name (``"tts"`` or ``"harmonic"``). Overrides
              the ``REACHY_VOICE_ENGINE`` env var when given.

    Returns:
        The matching :class:`VoiceEngine`.

    Raises:
        :class:`~reachy.cli._errors.CliError` (code 1) when *name* (or the
        env var) names an engine that isn't registered.
    """
    resolved_name = name or os.environ.get(VOICE_ENGINE_ENV) or DEFAULT_ENGINE

    engine = _ENGINES.get(resolved_name)
    if engine is None:
        valid = ", ".join(sorted(_ENGINES))
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown voice engine: {resolved_name!r}",
            remediation=f"choose one of: {valid}",
        )
    return engine
