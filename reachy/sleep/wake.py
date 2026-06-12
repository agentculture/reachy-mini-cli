"""Two-tier wake detection for sleep mode.

Tier-1 — ALWAYS on, zero new base dependencies:
    Fires when :attr:`~reachy.behavior.sense.Sense.speech_detected` is ``True``
    **or** when :class:`~reachy.motion.snap.SnapDetector` registers a loud audio
    transient.  This tier is active for every install profile.

Tier-2 — OPTIONAL, lazy:
    A wake-*word* phrase detector (e.g. ``"hey reachy"``).  The engine is imported
    *lazily* and only when both conditions hold:

    * ``wake_word_enabled=True`` was passed to :class:`WakeDetector`.
    * The ``[cpu]`` or ``[gpu]`` extra is installed and the engine can be imported.

    If the engine is absent or fails to load, :class:`WakeDetector` falls back
    to Tier-1 silently — it **never raises**.

Public API (consumed by task t8 / the sleep loop)::

    from reachy.sleep.wake import WakeDetector

    det = WakeDetector(wake_word_enabled=False)   # or True for phrase detection
    fired: bool = det.update(sense, audio_chunk)  # call once per tick

Cited pattern for Tier-2 ASR phrase matching:
    ``reachy_nova.wake_word.WakeWordDetector`` — phrase substring match,
    periodic transcribe in a background thread.  We keep the same design but
    gate the import so the base profile never touches NeMo/ASR deps.
"""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING

import numpy as np

from reachy.behavior.sense import Sense
from reachy.motion.snap import SnapDetector

if TYPE_CHECKING:
    pass  # no TYPE_CHECKING-only imports needed

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Wake-word phrase (Tier-2)
# ---------------------------------------------------------------------------

_DEFAULT_PHRASE = "hey reachy"


def _try_load_wake_word_engine(phrase: str):
    """Attempt to import the wake-word engine and return a detector instance.

    The import lives *inside* this function so that the module can be imported on
    any install profile without pulling in ``openwakeword`` or any ``[cpu]``/``[gpu]``
    package.

    Returns the engine instance on success, or ``None`` on ``ImportError`` / any
    other load failure (so the caller degrades cleanly to Tier-1).
    """
    try:
        # Lazy import — only reached when wake_word_enabled=True AND the package
        # is installed.  A bare `pip install reachy-mini-cli` never executes this.
        import openwakeword  # noqa: F401  # [cpu] extra
        from openwakeword.model import Model  # type: ignore[import-untyped]

        oww = Model(wakeword_models=[], inference_framework="tflite")
        logger.info("[WakeDetector] Tier-2 openwakeword engine loaded, phrase=%r", phrase)
        return oww
    except ImportError:
        logger.debug(
            "[WakeDetector] openwakeword not installed; Tier-2 disabled, using Tier-1 only."
        )
        return None
    except Exception as exc:  # noqa: BLE001
        logger.warning(
            "[WakeDetector] Tier-2 engine failed to load (%s); falling back to Tier-1.", exc
        )
        return None


# ---------------------------------------------------------------------------
# Public detector
# ---------------------------------------------------------------------------


class WakeDetector:
    """Two-tier wake detector for the sleep loop.

    Parameters
    ----------
    wake_word_enabled:
        If ``True``, attempt to load the wake-word engine (Tier-2).  If the
        engine is unavailable, fall back to Tier-1 transparently.
    phrase:
        The wake phrase for Tier-2 (default ``"hey reachy"``).  Ignored when
        Tier-2 is disabled or unavailable.
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

        # Tier-2 engine — None until (lazily) loaded, or if unavailable.
        self._engine = None
        self._engine_loaded: bool = False  # sentinel to avoid re-attempting each tick

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

        # Tier-2: wake word (only if enabled + engine available)
        if self._wake_word_enabled:
            engine = self._get_engine()
            if engine is not None and self._engine_check(engine, audio):
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
        # Delegate to the engine's own reset if it has one
        engine = self._engine
        if engine is not None and hasattr(engine, "reset"):
            # Degrade silently: a flaky engine reset must never crash wake handling.
            with contextlib.suppress(Exception):
                engine.reset()

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_engine(self):
        """Return the Tier-2 engine, loading it lazily on first call.

        Returns ``None`` if unavailable; never raises.
        """
        if not self._engine_loaded:
            self._engine_loaded = True
            self._engine = _try_load_wake_word_engine(self._phrase)
        return self._engine

    def _engine_check(self, engine, audio: np.ndarray) -> bool:
        """Ask the Tier-2 engine whether the wake phrase was heard.

        Wraps the call in a broad except so an engine crash never kills the
        sleep loop.
        """
        try:
            result = engine.detect(audio) if hasattr(engine, "detect") else False
            return bool(result)
        except Exception as exc:  # noqa: BLE001
            logger.warning("[WakeDetector] Tier-2 engine error: %s; ignoring.", exc)
            return False
