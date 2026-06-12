"""Two-tier wake detection for sleep mode.

Tier-1 — ALWAYS on, zero new base dependencies:
    Fires when :attr:`~reachy.behavior.sense.Sense.speech_detected` is ``True``
    **or** when :class:`~reachy.motion.snap.SnapDetector` registers a loud audio
    transient.  This tier is active for every install profile and lives here.

Tier-2 — OPTIONAL, pluggable wake-*word* backend:
    A wake-*word* phrase detector (e.g. ``"hey reachy"``).  The concern is owned
    by :mod:`reachy.sleep.wakeword`; :class:`WakeDetector` obtains a backend via
    :func:`reachy.sleep.wakeword.resolve_backend` and calls ``backend.update``
    once per tick.  There are exactly two backends:

    * an **external HTTP STT** override (the DEFAULT) reached over stdlib urllib;
    * **openwakeword** behind the ``[cpu]`` extra (lazy-imported there, never
      here).

    A configured-but-unreachable/absent backend degrades to "no wake-word"
    (returns ``False``) and **never raises** — :class:`WakeDetector` then falls
    back to Tier-1 transparently.  Importing this module pulls in NO
    ``openwakeword`` and no ASR library.

Public API (consumed by task t8 / the sleep loop)::

    from reachy.sleep.wake import WakeDetector

    det = WakeDetector(wake_word_enabled=False)   # or True for phrase detection
    fired: bool = det.update(sense, audio_chunk)  # call once per tick
"""

from __future__ import annotations

import logging

import numpy as np

from reachy.behavior.sense import Sense
from reachy.motion.snap import SnapDetector
from reachy.sleep import wakeword
from reachy.sleep.wakeword import (  # noqa: F401  (back-compat re-export)
    DEFAULT_PHRASE as _DEFAULT_PHRASE,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Public detector
# ---------------------------------------------------------------------------


class WakeDetector:
    """Two-tier wake detector for the sleep loop.

    Parameters
    ----------
    wake_word_enabled:
        If ``True``, resolve a Tier-2 wake-word backend (default the external
        HTTP STT override).  If the backend is unavailable/unreachable, fall back
        to Tier-1 transparently — it never raises.
    phrase:
        The wake phrase for Tier-2 (default ``"hey reachy"``).  Ignored when
        Tier-2 is disabled.
    wake_word_kind:
        Which Tier-2 backend to build (``"http"`` default, or ``"openwakeword"``).
        Forwarded to :func:`reachy.sleep.wakeword.resolve_backend`.
    wake_word_sample_rate:
        Mic sample rate forwarded to the HTTP STT backend's WAV header (the real
        rate from the SDK transport; default 16000). Ignored by openwakeword.
    snap_ratio:
        Loudness ratio threshold forwarded to :class:`~reachy.motion.snap.SnapDetector`.
    snap_min_rms:
        Absolute RMS floor forwarded to :class:`~reachy.motion.snap.SnapDetector`.
    snap_history:
        Rolling-window length forwarded to :class:`~reachy.motion.snap.SnapDetector`.
    """

    def __init__(
        self,
        *,
        wake_word_enabled: bool = False,
        phrase: str = _DEFAULT_PHRASE,
        wake_word_kind: str = wakeword.DEFAULT_KIND,
        wake_word_sample_rate: int = wakeword.DEFAULT_SAMPLE_RATE,
        snap_ratio: float = 5.0,
        snap_min_rms: float = 0.02,
        snap_history: int = 30,
    ) -> None:
        # Retain the snap configuration so reset() can rebuild the detector
        # without reaching into SnapDetector's private attributes.
        self._snap_ratio = snap_ratio
        self._snap_min_rms = snap_min_rms
        self._snap_history = snap_history
        self._snap = SnapDetector(
            ratio=snap_ratio,
            min_rms=snap_min_rms,
            history=snap_history,
        )
        self._wake_word_enabled = wake_word_enabled
        self._phrase = phrase

        # Tier-2 backend (pluggable).  resolve_backend is side-effect-free: no
        # network call, no openwakeword import happens here — both are deferred
        # to the backend's first update().  When disabled, this is a null backend
        # that never fires.
        self._backend = wakeword.resolve_backend(
            enabled=wake_word_enabled,
            kind=wake_word_kind,
            phrase=phrase,
            sample_rate=wake_word_sample_rate,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, sense: Sense, audio: np.ndarray) -> bool:
        """Feed one tick's sensor snapshot and audio chunk.

        Parameters
        ----------
        sense:
            Latest :class:`~reachy.behavior.sense.Sense` snapshot (for
            ``speech_detected``).
        audio:
            Float32 audio chunk (same chunk fed to the snap detector).

        Returns
        -------
        bool
            ``True`` when any tier fires a wake event this tick; ``False``
            otherwise.
        """
        # Tier-1a: speech flag
        if sense.speech_detected:
            logger.debug("[WakeDetector] Tier-1 fired (speech_detected)")
            self._snap.feed(audio)  # keep history consistent
            return True

        # Tier-1b: loud audio transient
        if self._snap.feed(audio):
            logger.debug("[WakeDetector] Tier-1 fired (snap)")
            return True

        # Tier-2: pluggable wake-word backend (never raises; null when disabled)
        if self._backend.update(sense, audio):
            logger.info("[WakeDetector] Tier-2 fired (wake word)")
            return True

        return False

    def reset(self) -> None:
        """Reset internal state (call when the robot wakes to avoid re-triggering)."""
        self._snap = SnapDetector(
            ratio=self._snap_ratio,
            min_rms=self._snap_min_rms,
            history=self._snap_history,
        )
        # Delegate to the backend's own reset (a no-op for the null/HTTP backends).
        reset = getattr(self._backend, "reset", None)
        if callable(reset):
            reset()
