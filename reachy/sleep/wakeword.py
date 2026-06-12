"""Pluggable wake-word backend layer for sleep mode (Tier-2 of WakeDetector).

This module owns the *optional* wake-*word* concern. :class:`reachy.sleep.wake.WakeDetector`
keeps Tier-1 (speech flag + snap) and obtains its Tier-2 backend from here via
:func:`resolve_backend`. There are exactly **two** backends:

* :class:`HttpSttBackend` — the **DEFAULT**. An external **OpenAI-compatible**
  speech-to-text endpoint (local or remote) reached over **stdlib urllib only** —
  analogous to how :mod:`reachy.speech.tts` reaches the Magpie TTS server. The
  default target is the model-gear / NVIDIA **Parakeet** STT service. The backend
  accumulates a rolling audio window (a single tick's mic chunk is far too short
  to transcribe a phrase), POSTs it as a WAV upload at most once per
  ``min_interval``, parses the JSON response, and fires when a configurable wake
  phrase (default ``"hey reachy"``) is detected. A configured-but-unreachable /
  absent endpoint degrades cleanly to "no wake-word" (``update`` returns
  ``False``) and **never raises**. No on-box STT model is bundled.

* :class:`OpenWakeWordBackend` — the optional on-box ``[cpu]`` / Raspberry-Pi
  path. ``openwakeword`` is **lazy-imported** inside :meth:`_get_engine`, only
  when this backend is selected. A missing extra degrades to ``False`` and never
  raises — importing this module pulls in NO ``openwakeword``.

Backend protocol (duck-typed)::

    class WakeBackend(Protocol):
        def update(self, sense: Sense, audio: np.ndarray) -> bool: ...
        def reset(self) -> None: ...   # optional

``update`` is called once per tick; it returns ``True`` on a detected wake-word.

External STT urllib contract — the real model-gear / Parakeet endpoint
(OpenAI-compatible ``/v1/audio/transcriptions``), resolved against the live
service (model-gear#39/#40 track the server side):

    POST  {REACHY_STT_URL}/v1/audio/transcriptions
    body: multipart/form-data
        file=<audio.wav>   a WAV container (PCM16 mono @ the mic sample rate)
        language=en        OpenAI/Parakeet language hint (REACHY_STT_LANGUAGE)
    response (JSON, any of):
        {"text": "...words..."}        → OpenAI/Parakeet shape; fire if the phrase
                                         is a substring (case-insensitive)
        {"transcript": "...text..."}   → legacy alias for ``text`` (same match)
        {"detected": true}             → explicit boolean override (server matched)
        {"phrase": "hey reachy"}       → explicit matched-phrase echo (== the phrase)

The client tolerates a missing/empty/204 response, a non-JSON body, an HTTP
error, or an unreachable host — all map to "not detected" (``False``).
"""

from __future__ import annotations

import io
import json
import logging
import os
import time
import urllib.error
import urllib.request
import uuid
import wave
from collections import deque
from typing import Callable

import numpy as np

from reachy.behavior.sense import Sense

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (env-var pattern mirrors reachy.speech.tts)
# ---------------------------------------------------------------------------

DEFAULT_PHRASE = "hey reachy"
#: model-gear / NVIDIA Parakeet STT (see ../model-gear; reachy runs on the same
#: box, so localhost). Override with REACHY_STT_URL for a remote deployment.
DEFAULT_STT_URL = "http://localhost:9002"
DEFAULT_STT_PATH = "/v1/audio/transcriptions"
DEFAULT_STT_TIMEOUT = 2.0  # seconds — short: a wake check must never stall the loop
DEFAULT_LANGUAGE = "en"
#: Parakeet expects 16 kHz mono; the WAV header carries whatever the mic feeds.
DEFAULT_SAMPLE_RATE = 16000
#: Accumulate this much audio before a transcription POST — a single tick's mic
#: chunk (tens of ms) is far too short to transcribe a wake phrase.
DEFAULT_WINDOW_SECONDS = 1.5
#: Minimum seconds between POSTs — throttles the STT server (and the loop).
DEFAULT_MIN_INTERVAL = 1.0

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


def _resolve_language(override: str | None) -> str:
    """Return the STT language hint: explicit arg > ``REACHY_STT_LANGUAGE`` > 'en'."""
    return override or os.environ.get("REACHY_STT_LANGUAGE") or DEFAULT_LANGUAGE


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
    """Wake-word via an external OpenAI-compatible STT endpoint (stdlib urllib).

    The default target is the model-gear / NVIDIA **Parakeet** service
    (``POST {stt_url}/v1/audio/transcriptions``). Because a single tick's mic
    chunk is far too short to transcribe a phrase, :meth:`update` accumulates a
    rolling ``window_seconds`` audio window and POSTs it — as a **WAV upload in a
    multipart form** — at most once per ``min_interval``. The JSON response is
    matched against ``phrase``. A configured-but-unreachable endpoint, an HTTP
    error, or a non-JSON body all degrade to ``False`` — :meth:`update` never
    raises.

    Parameters
    ----------
    stt_url:
        Base URL of the STT server. Explicit arg > ``REACHY_STT_URL`` > default
        (``http://localhost:9002`` — Parakeet on the same box).
    phrase:
        Wake phrase to match (case-insensitive substring). Explicit arg >
        ``REACHY_STT_PHRASE`` > ``"hey reachy"``.
    stt_path:
        Endpoint path appended to the base URL (default
        ``/v1/audio/transcriptions``).
    timeout:
        Per-request socket timeout in seconds. Explicit arg >
        ``REACHY_STT_TIMEOUT`` > 2.0. Kept short so a slow STT never stalls the
        sleep loop.
    language:
        OpenAI/Parakeet language hint (multipart ``language`` field). Explicit
        arg > ``REACHY_STT_LANGUAGE`` > ``"en"``.
    sample_rate:
        Sample rate of the mic audio, written into the WAV header so the STT
        service interprets it correctly (Parakeet expects 16 kHz). Pass the real
        rate from the SDK transport; default 16000.
    window_seconds:
        How much trailing audio to accumulate before each POST (default 1.5 s).
        ``0`` posts whatever is buffered each eligible tick (used by tests).
    min_interval:
        Minimum seconds between POSTs (default 1.0 s) — throttles the STT server.
    clock:
        Monotonic clock for the throttle, injectable for tests (default
        :func:`time.monotonic`).
    """

    def __init__(
        self,
        *,
        stt_url: str | None = None,
        phrase: str | None = None,
        stt_path: str = DEFAULT_STT_PATH,
        timeout: float | None = None,
        language: str | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stt_url = _resolve_stt_url(stt_url)
        self.phrase = _resolve_phrase(phrase)
        self.language = _resolve_language(language)
        self._endpoint = f"{self.stt_url}{stt_path}"
        self._timeout = _resolve_stt_timeout(timeout)
        self._sample_rate = int(sample_rate) if sample_rate else DEFAULT_SAMPLE_RATE
        self._window_samples = max(0, int(window_seconds * self._sample_rate))
        self._min_interval = max(0.0, float(min_interval))
        self._clock = clock
        # Rolling audio window (oldest chunks dropped once the window is full).
        self._buffer: deque[np.ndarray] = deque()
        self._buffered = 0
        self._last_post: float | None = None

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, _sense: Sense, audio: np.ndarray) -> bool:
        """Accumulate audio; POST the window when full + due; True on a match.

        Never raises — any network / parse failure degrades to ``False``.
        """
        self._accumulate(audio)
        if self._buffered < self._window_samples:
            return False  # not enough audio yet to transcribe a phrase
        now = self._clock()
        if self._last_post is not None and (now - self._last_post) < self._min_interval:
            return False  # throttled — do not hammer the STT server
        self._last_post = now
        window = self._collect_window()
        try:
            payload = self._post(window)
        # Degrade cleanly: a network/parse failure must never crash the loop.
        except Exception as exc:  # noqa: BLE001
            logger.debug("[wakeword] STT request failed (%s); no wake-word this tick", exc)
            return False
        return self._matches(payload)

    def reset(self) -> None:
        """Clear the rolling window + throttle so a fresh wake does not re-fire."""
        self._buffer.clear()
        self._buffered = 0
        self._last_post = None

    # ------------------------------------------------------------------
    # Rolling audio window
    # ------------------------------------------------------------------

    def _accumulate(self, audio: np.ndarray) -> None:
        """Append a mic chunk, trimming the oldest so the window stays bounded."""
        if audio is None or len(audio) == 0:
            return
        chunk = np.asarray(audio, dtype=np.float32).reshape(-1)
        self._buffer.append(chunk)
        self._buffered += len(chunk)
        # Drop oldest chunks once we hold more than one window (keep ≥1 chunk).
        while (
            len(self._buffer) > 1 and self._buffered - len(self._buffer[0]) >= self._window_samples
        ):
            self._buffered -= len(self._buffer.popleft())

    def _collect_window(self) -> np.ndarray:
        """Concatenate the buffered chunks into a single float32 window."""
        if not self._buffer:
            return np.zeros(0, dtype=np.float32)
        return np.concatenate(list(self._buffer))

    # ------------------------------------------------------------------
    # Phrase matching
    # ------------------------------------------------------------------

    def _matches(self, payload: dict | None) -> bool:
        """Decide whether *payload* (a parsed JSON response) signals a wake-word.

        Honours, in order: an explicit ``detected`` boolean, a ``phrase`` field
        that *equals* the configured wake phrase (case-insensitive — a bare echo
        of some other phrase must NOT fire), then a case-insensitive substring
        match of ``self.phrase`` in the transcript (OpenAI/Parakeet ``text``, or
        its legacy ``transcript`` alias). Anything else → ``False``.
        """
        if not isinstance(payload, dict):
            return False
        if bool(payload.get("detected")):
            return True
        phrase = payload.get("phrase")
        if isinstance(phrase, str) and phrase.strip().lower() == self.phrase.lower():
            return True
        transcript = payload.get("text") or payload.get("transcript")
        if isinstance(transcript, str) and transcript:
            return self.phrase.lower() in transcript.lower()
        return False

    # ------------------------------------------------------------------
    # HTTP leg (stdlib urllib only) — seam for tests
    # ------------------------------------------------------------------

    def _post(self, audio: np.ndarray) -> dict | None:
        """POST the audio window (multipart WAV upload) and return the parsed JSON.

        Returns the decoded dict, or ``None`` for an empty / non-JSON / non-dict
        body. Raises only the underlying urllib/OS error, which :meth:`update`
        catches — kept as a raising seam so tests can stub it both ways.
        """
        wav = self._wav_bytes(audio, self._sample_rate)
        if not wav:
            return None
        body, content_type = self._multipart_body(wav)
        req = urllib.request.Request(  # nosec B310 — URL is operator config, not user input
            url=self._endpoint,
            data=body,
            method="POST",
            headers={"Content-Type": content_type},
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

    def _multipart_body(self, wav: bytes) -> tuple[bytes, str]:
        """Build a ``multipart/form-data`` body with ``file`` (WAV) + ``language``.

        Hand-rolled (stdlib only) so the wake path never grows a ``requests`` dep.
        """
        boundary = f"----reachywake{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="wake.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
            wav,
            crlf,
            f"--{boundary}\r\nContent-Disposition: form-data; "
            f'name="language"\r\n\r\n{self.language}\r\n'.encode(),
            f"--{boundary}--\r\n".encode(),
        ]
        return b"".join(parts), f"multipart/form-data; boundary={boundary}"

    @staticmethod
    def _encode_audio(audio: np.ndarray) -> bytes:
        """Encode the float32 audio window as little-endian PCM16 bytes."""
        if audio is None or len(audio) == 0:
            return b""
        clipped = np.clip(np.asarray(audio, dtype=np.float32), -1.0, 1.0)
        return (clipped * 32767.0).astype("<i2").tobytes()

    @classmethod
    def _wav_bytes(cls, audio: np.ndarray, sample_rate: int) -> bytes:
        """Wrap the float32 window in a PCM16 mono WAV container (stdlib ``wave``)."""
        pcm = cls._encode_audio(audio)
        if not pcm:
            return b""
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)  # 16-bit
            wf.setframerate(int(sample_rate) or DEFAULT_SAMPLE_RATE)
            wf.writeframes(pcm)
        return buf.getvalue()


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
    language: str | None = None,
    sample_rate: int = DEFAULT_SAMPLE_RATE,
):
    """Return a wake-word backend with ``update(sense, audio) -> bool``.

    Parameters
    ----------
    enabled:
        When ``False`` (the default), return a null backend that never fires —
        Tier-2 wake-word is off and only Tier-1 (speech/snap) is active.
    kind:
        Which backend to build when ``enabled``. ``"http"`` (default) → the
        external OpenAI-compatible STT endpoint (Parakeet); ``"openwakeword"`` →
        the optional on-box ``[cpu]`` engine (lazy import). An unknown kind falls
        back to the HTTP default with a warning.
    phrase:
        Wake phrase override (else ``REACHY_STT_PHRASE`` / ``"hey reachy"``).
    stt_url, stt_path, timeout, language, sample_rate:
        Forwarded to :class:`HttpSttBackend` for the HTTP kind. ``sample_rate``
        should be the real mic rate from the SDK transport (carried in the WAV
        header so Parakeet interprets the audio correctly).

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
        language=language,
        sample_rate=sample_rate,
    )
