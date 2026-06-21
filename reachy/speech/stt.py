"""Shared speech-to-text transcription client (model-gear / NVIDIA Parakeet).

This module owns the reusable *transcription* leg — the WAV-multipart + stdlib
``urllib`` + rolling-window + throttle machinery — factored out of
:class:`reachy.sleep.wakeword.HttpSttBackend`. Where the wake-word backend
returns a *boolean* (did the wake phrase fire), :class:`Transcriber` returns the
transcript **text** (a ``str`` or ``None``), so it can back both the wake-word
backend and a new cognition transcript hook from one place.

It reaches an external **OpenAI-compatible** speech-to-text endpoint (local or
remote) over **stdlib urllib only** — analogous to how :mod:`reachy.speech.tts`
reaches the Magpie TTS server. The default target is the model-gear / NVIDIA
**Parakeet** STT service. Because a single tick's mic chunk (tens of ms) is far
too short to transcribe a phrase, :meth:`transcribe` accumulates a rolling audio
window and POSTs it as a WAV upload at most once per ``min_interval``. A
configured-but-unreachable / absent endpoint, an HTTP error, an empty body, or a
non-JSON response all degrade cleanly to ``None`` and **never raise**. No on-box
STT model is bundled — the heavy STT is externally managed behind the HTTP
service (``REACHY_STT_URL``).

External STT urllib contract — the real model-gear / Parakeet endpoint
(OpenAI-compatible ``/v1/audio/transcriptions``):

    POST  {REACHY_STT_URL}/v1/audio/transcriptions
    body: multipart/form-data
        file=<audio.wav>   a WAV container (PCM16 mono @ the mic sample rate)
        language=en        OpenAI/Parakeet language hint (REACHY_STT_LANGUAGE)
    response (JSON):
        {"text": "...words..."}        → OpenAI/Parakeet shape (returned verbatim)
        {"transcript": "...text..."}   → legacy alias for ``text`` (same return)

The client tolerates a missing/empty/204 response, a non-JSON body, an HTTP
error, or an unreachable host — all map to ``None`` (no transcript this tick).
This module uses **stdlib + numpy only**: importing it pulls in NO ``requests``
or ``openai`` (no on-box ASR library either).
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

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Configuration defaults (env-var pattern mirrors reachy.speech.tts /
# reachy.sleep.wakeword — kept identical so the two STT consumers agree).
# ---------------------------------------------------------------------------

#: model-gear / NVIDIA Parakeet STT (reachy runs on the same box, so localhost).
#: Override with REACHY_STT_URL for a remote deployment.
DEFAULT_STT_URL = "http://localhost:9002"
DEFAULT_STT_PATH = "/v1/audio/transcriptions"
DEFAULT_STT_TIMEOUT = 2.0  # seconds — short: transcription must never stall a loop
DEFAULT_LANGUAGE = "en"
#: Parakeet expects 16 kHz mono; the WAV header carries whatever the mic feeds.
DEFAULT_SAMPLE_RATE = 16000
#: Accumulate this much audio before a transcription POST — a single tick's mic
#: chunk (tens of ms) is far too short to transcribe a phrase.
DEFAULT_WINDOW_SECONDS = 1.5
#: Minimum seconds between POSTs — throttles the STT server (and the loop).
DEFAULT_MIN_INTERVAL = 1.0


def _resolve_stt_url(override: str | None) -> str:
    """Return the STT base URL: explicit arg > ``REACHY_STT_URL`` > default."""
    return (override or os.environ.get("REACHY_STT_URL") or DEFAULT_STT_URL).rstrip("/")


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
            logger.debug("[stt] bad REACHY_STT_TIMEOUT=%r; using default", env)
    return DEFAULT_STT_TIMEOUT


# ---------------------------------------------------------------------------
# Transcriber — the shared transcription client (stdlib urllib only)
# ---------------------------------------------------------------------------


class Transcriber:
    """Transcribe rolling mic audio via an external OpenAI-compatible STT endpoint.

    The default target is the model-gear / NVIDIA **Parakeet** service
    (``POST {stt_url}/v1/audio/transcriptions``). Because a single tick's mic
    chunk is far too short to transcribe a phrase, :meth:`transcribe` accumulates
    a rolling ``window_seconds`` audio window and POSTs it — as a **WAV upload in
    a multipart form** — at most once per ``min_interval``. The JSON response's
    ``text`` (or its legacy ``transcript`` alias) is returned as the transcript
    string. Not enough audio yet, a throttled tick, a configured-but-unreachable
    endpoint, an HTTP error, an empty body, or a non-JSON body all degrade to
    ``None`` — :meth:`transcribe` never raises.

    Parameters
    ----------
    stt_url:
        Base URL of the STT server. Explicit arg > ``REACHY_STT_URL`` > default
        (``http://localhost:9002`` — Parakeet on the same box).
    stt_path:
        Endpoint path appended to the base URL (default
        ``/v1/audio/transcriptions``).
    timeout:
        Per-request socket timeout in seconds. Explicit arg >
        ``REACHY_STT_TIMEOUT`` > 2.0. Kept short so a slow STT never stalls the
        caller's loop.
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
        stt_path: str = DEFAULT_STT_PATH,
        timeout: float | None = None,
        language: str | None = None,
        sample_rate: int = DEFAULT_SAMPLE_RATE,
        window_seconds: float = DEFAULT_WINDOW_SECONDS,
        min_interval: float = DEFAULT_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.stt_url = _resolve_stt_url(stt_url)
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

    def transcribe(self, audio: np.ndarray) -> str | None:
        """Accumulate audio; POST the window when full + due; return the transcript.

        Returns the response ``text`` (or its legacy ``transcript`` alias) as a
        non-empty ``str``, or ``None`` when there is not enough audio yet, the
        POST is throttled, or any network / parse failure occurs. Never raises —
        every failure degrades to ``None``.
        """
        return self._extract_text(self.transcribe_payload(audio))

    def transcribe_payload(self, audio: np.ndarray) -> dict | None:
        """Accumulate audio; POST the window when full + due; return the raw JSON.

        Identical window + throttle + POST logic to :meth:`transcribe`, but
        returns the *parsed JSON payload dict* (the response body before any
        ``text`` extraction) rather than the transcript string — so callers that
        need other response fields (e.g. the wake-word backend's ``detected`` /
        ``phrase`` echo) can inspect them. Returns ``None`` when there is not
        enough audio yet, the POST is throttled, the body is empty / non-JSON /
        non-dict, or any network failure occurs. Never raises — every failure
        degrades to ``None``.
        """
        self._accumulate(audio)
        if self._buffered < self._window_samples:
            return None  # not enough audio yet to transcribe a phrase
        now = self._clock()
        if self._last_post is not None and (now - self._last_post) < self._min_interval:
            return None  # throttled — do not hammer the STT server
        self._last_post = now
        window = self._collect_window()
        try:
            return self._post(window)
        # Degrade cleanly: a network/parse failure must never crash the loop.
        except Exception as exc:  # noqa: BLE001
            logger.debug("[stt] transcription request failed (%s); no transcript this tick", exc)
            return None

    def transcribe_once(self, audio: np.ndarray) -> str | None:
        """Transcribe a COMPLETE utterance buffer in a single POST — no window/throttle.

        For callers that do their own endpointing (accumulate a whole utterance,
        then transcribe on a pause), this bypasses the rolling-window + throttle of
        :meth:`transcribe` so the STT server sees the *full phrase* rather than a
        ``window_seconds`` slice — the difference between "dog near the riverbank"
        and the whole sentence. Returns the transcript string, or ``None`` for empty
        input / any network or parse failure. Never raises.
        """
        if audio is None or len(audio) == 0:
            return None
        buf = np.asarray(audio, dtype=np.float32).reshape(-1)
        try:
            return self._extract_text(self._post(buf))
        # Degrade cleanly: a network/parse failure must never crash the caller's loop.
        except Exception as exc:  # noqa: BLE001
            logger.debug("[stt] utterance transcription failed (%s); no transcript", exc)
            return None

    def reset(self) -> None:
        """Clear the rolling window + throttle so the next window starts fresh."""
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
        # Drop oldest chunks once we hold more than one window (keep >=1 chunk).
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
    # Transcript extraction
    # ------------------------------------------------------------------

    @staticmethod
    def _extract_text(payload: dict | None) -> str | None:
        """Return the transcript string from a parsed JSON response, or ``None``.

        Honours the OpenAI/Parakeet ``text`` field, falling back to the legacy
        ``transcript`` alias. A missing / non-string / empty value yields
        ``None``.
        """
        if not isinstance(payload, dict):
            return None
        text = payload.get("text")
        if not isinstance(text, str) or not text:
            text = payload.get("transcript")
        if isinstance(text, str) and text:
            return text
        return None

    # ------------------------------------------------------------------
    # HTTP leg (stdlib urllib only) — seam for tests
    # ------------------------------------------------------------------

    def _post(self, audio: np.ndarray) -> dict | None:
        """POST the audio window (multipart WAV upload) and return the parsed JSON.

        Returns the decoded dict, or ``None`` for an empty / non-JSON / non-dict
        body. Raises only the underlying urllib/OS error, which :meth:`transcribe`
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
                logger.debug("[stt] STT returned HTTP %s; no transcript", status)
                return None
            raw = resp.read()
        if not raw:
            return None
        try:
            decoded = json.loads(raw.decode("utf-8"))
        except ValueError:  # UnicodeDecodeError is a ValueError subclass
            logger.debug("[stt] STT response was not JSON; no transcript")
            return None
        return decoded if isinstance(decoded, dict) else None

    def _multipart_body(self, wav: bytes) -> tuple[bytes, str]:
        """Build a ``multipart/form-data`` body with ``file`` (WAV) + ``language``.

        Hand-rolled (stdlib only) so the STT path never grows a ``requests`` dep.
        """
        boundary = f"----reachystt{uuid.uuid4().hex}"
        crlf = b"\r\n"
        parts = [
            f'--{boundary}\r\nContent-Disposition: form-data; name="file"; '
            'filename="audio.wav"\r\nContent-Type: audio/wav\r\n\r\n'.encode(),
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
