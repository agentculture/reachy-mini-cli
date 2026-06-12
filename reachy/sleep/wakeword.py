"""Pluggable wake-word backend layer for sleep mode (Tier-2 of WakeDetector).

This module owns the *optional* wake-*word* concern. :class:`reachy.sleep.wake.WakeDetector`
keeps Tier-1 (speech flag + snap) and obtains its Tier-2 backend from here via
:func:`resolve_backend`. There are exactly **two** backends:

* :class:`HttpSttBackend` — the **DEFAULT**. An external HTTP speech-to-text
  endpoint (local or remote) reached over **stdlib urllib only** — analogous to
  how :mod:`reachy.speech.tts` reaches the Magpie TTS server. POST the audio
  window, parse a small JSON response, and fire when a configurable wake phrase
  (default ``"hey reachy"``) is detected. A configured-but-unreachable / absent
  endpoint degrades cleanly to "no wake-word" (``update`` returns ``False``) and
  **never raises**. No on-box STT model is bundled.

* :class:`OpenWakeWordBackend` — the optional on-box ``[cpu]`` / Raspberry-Pi
  path. ``openwakeword`` is **lazy-imported** inside :meth:`_get_engine`, only
  when this backend is selected. A missing extra degrades to ``False`` and never
  raises — importing this module pulls in NO ``openwakeword``.

Backend protocol (duck-typed)::

    class WakeBackend(Protocol):
        def update(self, sense: Sense, audio: np.ndarray) -> bool: ...
        def reset(self) -> None: ...   # optional

``update`` is called once per tick; it returns ``True`` on a detected wake-word.

External STT urllib contract (parked risk r1 — kept small and well-commented so
it can be re-shaped when the real model-gear STT endpoint is wired):

    POST  {REACHY_STT_URL}/v1/audio/transcribe
    body: raw little-endian PCM16 audio window (Content-Type: application/octet-stream)
    query/header: none required (the wake phrase is matched client-side)
    response (JSON, any of):
        {"transcript": "...text..."}   → fire if the phrase is a substring (case-insensitive)
        {"detected": true}             → explicit boolean override (server already matched)
        {"phrase": "hey reachy"}       → explicit matched-phrase echo (treated as detected)

The client tolerates a missing/empty/204 response, a non-JSON body, an HTTP
error, or an unreachable host — all map to "not detected" (``False``).
"""

from __future__ import annotations

import json
import logging
import os
import urllib.error
import urllib.request

import numpy as np

from reachy.behavior.sense import Sense

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (env-var pattern mirrors reachy.speech.tts)
# ---------------------------------------------------------------------------

DEFAULT_PHRASE = "hey reachy"
DEFAULT_STT_URL = "http://localhost:9100"
DEFAULT_STT_PATH = "/v1/audio/transcribe"
DEFAULT_STT_TIMEOUT = 1.0  # seconds — short: a wake check must never stall the loop

# Backend kinds resolve_backend understands.
KIND_HTTP = "http"
KIND_OPENWAKEWORD = "openwakeword"
DEFAULT_KIND = KIND_HTTP


def _resolve_stt_url(override: str | None) -> str:
    """Return the STT base URL: explicit arg > ``REACHY_STT_URL`` > default."""
    return (override or os.environ.get("REACHY_STT_URL") or DEFAULT_STT_URL).rstrip("/")


def _resolve_phrase(override: str | None) -> str:
    """Return the wake phrase: explicit arg > ``REACHY_STT_PHRASE`` > default."""
    return override or os.environ.get("REACHY_STT_PHRASE") or DEFAULT_PHRASE


def _resolve_stt_timeout(override: float | None) -> float:
    """Return the per-request timeout: explicit arg > ``REACHY_STT_TIMEOUT`` > default."""
    if override is not None:
        return override
    env = os.environ.get("REACHY_STT_TIMEOUT")
    if env:
        try:
            return float(env)
        except ValueError:
            logger.debug("[wakeword] bad REACHY_STT_TIMEOUT=%r; using default", env)
    return DEFAULT_STT_TIMEOUT


# ---------------------------------------------------------------------------
# Null backend — used when wake-word is disabled
# ---------------------------------------------------------------------------


class _NullBackend:
    """A wake-word backend that never fires (Tier-2 disabled)."""

    def update(self, _sense: Sense, _audio: np.ndarray) -> bool:
        return False

    def reset(self) -> None:
        return None


# ---------------------------------------------------------------------------
# External HTTP STT backend (DEFAULT) — stdlib urllib only
# ---------------------------------------------------------------------------


class HttpSttBackend:
    """Wake-word via an external HTTP speech-to-text endpoint (stdlib urllib).

    The audio window is POSTed to ``{stt_url}{stt_path}`` and the JSON response is
    matched against ``phrase``. A configured-but-unreachable endpoint, an HTTP
    error, or a non-JSON body all degrade to ``False`` — :meth:`update` never
    raises.

    Parameters
    ----------
    stt_url:
        Base URL of the STT server. Explicit arg > ``REACHY_STT_URL`` > default.
    phrase:
        Wake phrase to match (case-insensitive substring). Explicit arg >
        ``REACHY_STT_PHRASE`` > ``"hey reachy"``.
    stt_path:
        Endpoint path appended to the base URL (default ``/v1/audio/transcribe``).
    timeout:
        Per-request socket timeout in seconds. Explicit arg >
        ``REACHY_STT_TIMEOUT`` > 1.0. Kept short so a slow STT never stalls the
        sleep loop.
    """

    def __init__(
        self,
        *,
        stt_url: str | None = None,
        phrase: str | None = None,
        stt_path: str = DEFAULT_STT_PATH,
        timeout: float | None = None,
    ) -> None:
        self.stt_url = _resolve_stt_url(stt_url)
        self.phrase = _resolve_phrase(phrase)
        self._endpoint = f"{self.stt_url}{stt_path}"
        self._timeout = _resolve_stt_timeout(timeout)

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, _sense: Sense, audio: np.ndarray) -> bool:
        """Send the audio window for transcription; return True on a phrase match.

        Never raises — any network / parse failure degrades to ``False``.
        """
        try:
            payload = self._post(audio)
        # Degrade cleanly: a network/parse failure must never crash the loop.
        except Exception as exc:  # noqa: BLE001
            logger.debug("[wakeword] STT request failed (%s); no wake-word this tick", exc)
            return False
        return self._matches(payload)

    def reset(self) -> None:
        """No client-side state to reset (the server is stateless to us)."""
        return None

    # ------------------------------------------------------------------
    # Phrase matching
    # ------------------------------------------------------------------

    def _matches(self, payload: dict | None) -> bool:
        """Decide whether *payload* (a parsed JSON response) signals a wake-word.

        Honours, in order: an explicit ``detected`` boolean, a ``phrase`` field
        that *equals* the configured wake phrase (case-insensitive — a bare echo
        of some other phrase must NOT fire), then a case-insensitive substring
        match of ``self.phrase`` in ``transcript``. Anything else → ``False``.
        """
        if not isinstance(payload, dict):
            return False
        if bool(payload.get("detected")):
            return True
        phrase = payload.get("phrase")
        if isinstance(phrase, str) and phrase.strip().lower() == self.phrase.lower():
            return True
        transcript = payload.get("transcript")
        if isinstance(transcript, str) and transcript:
            return self.phrase.lower() in transcript.lower()
        return False

    # ------------------------------------------------------------------
    # HTTP leg (stdlib urllib only) — seam for tests
    # ------------------------------------------------------------------

    def _post(self, audio: np.ndarray) -> dict | None:
        """POST the audio window to the STT endpoint and return the parsed JSON.

        Returns the decoded dict, or ``None`` for an empty / non-JSON / non-dict
        body. Raises only the underlying urllib/OS error, which :meth:`update`
        catches — kept as a raising seam so tests can stub it both ways.
        """
        body = self._encode_audio(audio)
        req = urllib.request.Request(  # nosec B310 — URL is operator config, not user input
            url=self._endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": "application/octet-stream"},
        )
        with urllib.request.urlopen(req, timeout=self._timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            if int(status) >= 400:
                logger.debug("[wakeword] STT returned HTTP %s; no wake-word", status)
                return None
            raw = resp.read()
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError is a ValueError subclass
            logger.debug("[wakeword] STT response was not JSON; no wake-word")
            return None
        return decoded if isinstance(decoded, dict) else None

    @staticmethod
    def _encode_audio(audio: np.ndarray) -> bytes:
        """Encode the float32 audio window as little-endian PCM16 bytes."""
        if audio is None or len(audio) == 0:
            return b""
        clipped = np.clip(audio.astype(np.float32), -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()


# ---------------------------------------------------------------------------
# openwakeword backend (optional [cpu]) — lazy import only
# ---------------------------------------------------------------------------


class OpenWakeWordBackend:
    """On-box wake-word via ``openwakeword`` (optional ``[cpu]`` extra).

    ``openwakeword`` is imported *lazily* on first :meth:`update` — importing this
    module never pulls it in. A missing extra (the bare-install case) degrades to
    ``False`` and never raises.
    """

    def __init__(self, *, phrase: str | None = None) -> None:
        self.phrase = _resolve_phrase(phrase)
        self._engine = None
        self._engine_loaded = False

    def update(self, _sense: Sense, audio: np.ndarray) -> bool:
        engine = self._get_engine()
        if engine is None:
            return False
        try:
            result = engine.detect(audio) if hasattr(engine, "detect") else False
            return bool(result)
        # An engine crash must not kill the loop.
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wakeword] openwakeword error: %s; ignoring this tick", exc)
            return False

    def reset(self) -> None:
        engine = self._engine
        if engine is not None and hasattr(engine, "reset"):
            import contextlib

            with contextlib.suppress(Exception):
                engine.reset()

    def _get_engine(self):
        """Lazily load the openwakeword engine; return ``None`` if unavailable."""
        if not self._engine_loaded:
            self._engine_loaded = True
            self._engine = self._load_engine()
        return self._engine

    def _load_engine(self):
        """Import + construct the openwakeword engine, or ``None`` on any failure.

        The import lives here so the module body stays dependency-free on the base
        profile — a bare ``pip install reachy-mini-cli`` never executes this.
        """
        try:
            import openwakeword  # noqa: F401  # [cpu] extra — lazy
            from openwakeword.model import Model  # type: ignore[import-untyped]

            engine = Model(wakeword_models=[], inference_framework="tflite")
            logger.info("[wakeword] openwakeword engine loaded, phrase=%r", self.phrase)
            return engine
        except ImportError:
            logger.debug("[wakeword] openwakeword not installed; Tier-2 wake-word disabled")
            return None
        except Exception as exc:  # noqa: BLE001
            logger.warning("[wakeword] openwakeword failed to load (%s); disabled", exc)
            return None


# ---------------------------------------------------------------------------
# Factory
# ---------------------------------------------------------------------------


def resolve_backend(
    *,
    enabled: bool = False,
    kind: str = DEFAULT_KIND,
    phrase: str | None = None,
    stt_url: str | None = None,
    stt_path: str = DEFAULT_STT_PATH,
    timeout: float | None = None,
):
    """Return a wake-word backend with ``update(sense, audio) -> bool``.

    Parameters
    ----------
    enabled:
        When ``False`` (the default), return a null backend that never fires —
        Tier-2 wake-word is off and only Tier-1 (speech/snap) is active.
    kind:
        Which backend to build when ``enabled``. ``"http"`` (default) → the
        external HTTP STT override; ``"openwakeword"`` → the optional on-box
        ``[cpu]`` engine (lazy import). An unknown kind falls back to the HTTP
        default with a warning.
    phrase:
        Wake phrase override (else ``REACHY_STT_PHRASE`` / ``"hey reachy"``).
    stt_url, stt_path, timeout:
        Forwarded to :class:`HttpSttBackend` for the HTTP kind.

    Never raises for a missing/unreachable backend — resolution itself is
    side-effect-free (no network call, no openwakeword import).
    """
    if not enabled:
        return _NullBackend()

    if kind == KIND_OPENWAKEWORD:
        return OpenWakeWordBackend(phrase=phrase)

    if kind != KIND_HTTP:
        logger.warning("[wakeword] unknown backend kind %r; using HTTP STT default", kind)

    return HttpSttBackend(
        stt_url=stt_url,
        phrase=phrase,
        stt_path=stt_path,
        timeout=timeout,
    )
