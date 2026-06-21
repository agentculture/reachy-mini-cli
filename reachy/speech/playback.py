"""Audio playback — stream synthesized PCM to the robot speaker.

Two transport paths mirror the ``listen`` noun's sdk/http split:

* **sdk** (default) — opens a ``ReachyMini`` media session and streams PCM
  chunks via ``push_audio_sample()``.  ``reachy_mini`` is imported lazily so
  the slim base install stays installable without system audio libs.
* **http** — synthesizes a full WAV in memory, uploads it to the daemon
  (``POST /media/sounds/upload``, multipart), then triggers playback
  (``POST /media/play_sound`` with ``{"file": "<path>"}``).  Pure stdlib
  (``urllib``), no third-party runtime dep.

Transport selection:

1. The ``transport`` parameter takes precedence (``"sdk"`` or ``"http"``).
2. When ``transport`` is ``None`` (the default), ``REACHY_TRANSPORT`` env var
   is read; if that is also unset the default is ``"sdk"`` (consistent with
   the ``listen`` noun).

Public API::

    play_audio(
        pcm_bytes,
        *,
        samplerate=24000,
        transport=None,          # "sdk" | "http" | None → env/default
        base_url="http://localhost:8000",
        media_session=None,      # inject a fake for testing; sdk path only
    ) -> None

PCM conversion:  raw int16 bytes → numpy float32 ndarray (values / 32768).
"""

from __future__ import annotations

import io
import json
import os
import urllib.error
import urllib.request
import wave
from typing import Any

import numpy as np

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# How many int16 samples to push in each SDK chunk.
_CHUNK_FRAMES = 512
# Bytes per int16 sample.
_INT16_BYTES = 2
# Fallback SDK speaker rate when the media session can't report one. The reachy_mini
# GStreamer backend fixes its playback appsrc caps at this rate and does NOT resample
# pushed buffers, so PCM at any other rate must be resampled to it before pushing.
_SDK_OUTPUT_RATE_FALLBACK = 16000

DEFAULT_BASE_URL = "http://localhost:8000"
# The daemon mounts its routers under /api (health is /api/daemon/status).
_UPLOAD_PATH = "/api/media/sounds/upload"
_PLAY_PATH = "/api/media/play_sound"

# Default filename used when uploading a synthesized WAV to the daemon.
_UPLOAD_FILENAME = "tts_synth.wav"


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _open_sdk_media() -> Any:  # type: ignore[return]
    """Open a ReachyMini media manager for playback.

    Raises :class:`CliError` (exit 2) when ``reachy_mini`` is not installed so
    callers get a clean hint rather than an ImportError traceback.
    """
    try:
        from reachy_mini import ReachyMini  # type: ignore[import-untyped]
    except ImportError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the reachy_mini SDK is not installed",
            remediation=(
                "install the sdk extra: pip install 'reachy-mini-cli[sdk]', "
                "or use transport='http'"
            ),
        ) from err
    mini = ReachyMini()
    return mini.media


def _pcm_bytes_to_float32(pcm: bytes) -> np.ndarray:
    """Convert raw int16 PCM bytes to a float32 ndarray normalised to [-1, 1].

    The TTS stage produces 16-bit signed PCM.  The SDK ``push_audio_sample``
    expects float32.  Divide by 32768 (not 32767) — the same convention used
    in reachy_nova's ``TrackingManager`` and the listen/snap code.
    """
    n_samples = len(pcm) // _INT16_BYTES
    if n_samples == 0:
        return np.empty(0, dtype=np.float32)
    int16_array = np.frombuffer(pcm, dtype=np.int16)
    return int16_array.astype(np.float32) / 32768.0


def _resample_mono(samples: np.ndarray, src_rate: int, dst_rate: int) -> np.ndarray:
    """Linearly resample mono float32 *samples* from *src_rate* to *dst_rate* Hz.

    The SDK speaker plays pushed buffers at its own fixed rate without resampling,
    so audio synthesized at a different rate (e.g. Chatterbox's 24 kHz vs the
    speaker's 16 kHz) must be converted here or it plays at the wrong pitch/speed.
    Linear interpolation is adequate for speech and keeps the dependency to numpy.
    A no-op when the rates already match or the input is empty.
    """
    if src_rate <= 0 or dst_rate <= 0 or src_rate == dst_rate or samples.size == 0:
        return samples
    n_dst = max(1, int(round(samples.size * dst_rate / src_rate)))
    src_index = np.linspace(0.0, samples.size - 1, num=n_dst)
    return np.interp(src_index, np.arange(samples.size), samples).astype(np.float32)


def _make_wav_bytes(pcm: bytes, samplerate: int) -> bytes:
    """Wrap raw int16 PCM bytes in a WAV container and return the result."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as wf:
        wf.setnchannels(1)
        wf.setsampwidth(_INT16_BYTES)
        wf.setframerate(samplerate)
        wf.writeframes(pcm)
    return buf.getvalue()


def _multipart_encode(filename: str, wav_bytes: bytes) -> tuple[bytes, str]:
    """Encode a single-file multipart/form-data body (stdlib, no third-party).

    Returns ``(body_bytes, content_type_header_value)``.
    """
    boundary = "----ReachyMiniPlaybackBoundary"
    ctype = f"multipart/form-data; boundary={boundary}"
    parts: list[bytes] = []
    parts.append(
        (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'
            f"Content-Type: audio/wav\r\n"
            f"\r\n"
        ).encode("utf-8")
    )
    parts.append(wav_bytes)
    parts.append(f"\r\n--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), ctype


def _post_for_json(req: urllib.request.Request, timeout: float) -> dict:
    """Send *req* and parse the JSON response, translating network/HTTP failures
    into a clean ``CliError`` (exit 2) — mirrors ``tts``/``llm`` so an unreachable
    or erroring daemon never leaks a traceback."""
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            return json.loads(resp.read())
    except urllib.error.HTTPError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"daemon returned HTTP {err.code} for {req.full_url}",
            remediation="check the daemon is healthy (reachy-mini-cli daemon status)",
        ) from err
    except OSError as err:  # URLError is an OSError subclass — covers both
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot reach the daemon at {req.full_url}: {err}",
            remediation=(
                "start the daemon (reachy-mini-cli daemon start), set --base-url, "
                "or use --transport sdk"
            ),
        ) from err


def _http_post_json(url: str, body: dict, timeout: float = 10.0) -> dict:
    """POST a JSON body to ``url`` and return the parsed JSON response."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=data,
        method="POST",
        headers={"Content-Type": "application/json", "Accept": "application/json"},
    )
    return _post_for_json(req, timeout)


def _http_post_multipart(url: str, body: bytes, content_type: str, timeout: float = 10.0) -> dict:
    """POST a multipart body to ``url`` and return the parsed JSON response."""
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={"Content-Type": content_type},
    )
    return _post_for_json(req, timeout)


# ---------------------------------------------------------------------------
# SDK playback
# ---------------------------------------------------------------------------


def _play_sdk(
    pcm: bytes,
    *,
    samplerate: int = _SDK_OUTPUT_RATE_FALLBACK,
    media_session: Any | None = None,
) -> None:
    """Stream PCM to the robot speaker via the SDK media session.

    *samplerate* is the rate the PCM was synthesized at. The SDK speaker plays
    pushed buffers at its own fixed output rate without resampling, so the PCM is
    resampled to the session's ``get_output_audio_samplerate()`` (falling back to
    :data:`_SDK_OUTPUT_RATE_FALLBACK`) before pushing — otherwise audio at a
    different rate plays at the wrong pitch/speed.

    If ``media_session`` is provided it is used directly (dependency injection
    for testing).  Otherwise ``_open_sdk_media()`` is called to open a real one
    (which may raise CliError if the SDK extra is absent).
    """
    if media_session is None:
        try:
            media_session = _open_sdk_media()
        except ImportError as err:
            # _open_sdk_media normally wraps ImportError into CliError, but a
            # monkeypatched stub might raise ImportError directly — catch it here
            # so callers always get a CliError regardless.
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="the reachy_mini SDK is not installed",
                remediation=(
                    "install the sdk extra: pip install 'reachy-mini-cli[sdk]', "
                    "or use transport='http'"
                ),
            ) from err

    samples = _pcm_bytes_to_float32(pcm)
    if len(samples) == 0:
        return

    # Resample to the speaker's real output rate (the SDK does not do this for us).
    try:
        target_rate = int(media_session.get_output_audio_samplerate())
    except Exception:  # noqa: BLE001 — any backend hiccup falls back to the known rate
        target_rate = _SDK_OUTPUT_RATE_FALLBACK
    if target_rate <= 0:
        target_rate = _SDK_OUTPUT_RATE_FALLBACK
    samples = _resample_mono(samples, samplerate, target_rate)

    media_session.start_playing()

    offset = 0
    while offset < len(samples):
        chunk = samples[offset : offset + _CHUNK_FRAMES]
        media_session.push_audio_sample(chunk)
        offset += _CHUNK_FRAMES


# ---------------------------------------------------------------------------
# HTTP playback
# ---------------------------------------------------------------------------


def _play_http(
    pcm: bytes,
    *,
    samplerate: int,
    base_url: str,
    timeout: float = 10.0,
) -> None:
    """Upload a WAV to the daemon and trigger playback over HTTP.

    Step 1: ``POST {base_url}/media/sounds/upload`` (multipart) — daemon saves
            the file and returns ``{"path": "<name>"}`` (or similar).
    Step 2: ``POST {base_url}/media/play_sound`` with ``{"file": "<path>"}``.
    """
    base = base_url.rstrip("/")
    wav = _make_wav_bytes(pcm, samplerate)
    body, ctype = _multipart_encode(_UPLOAD_FILENAME, wav)

    upload_resp = _http_post_multipart(
        f"{base}{_UPLOAD_PATH}",
        body,
        ctype,
        timeout=timeout,
    )
    saved_path: str = upload_resp.get("path", _UPLOAD_FILENAME)

    _http_post_json(
        f"{base}{_PLAY_PATH}",
        {"file": saved_path},
        timeout=timeout,
    )


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def play_audio(
    pcm_bytes: bytes,
    *,
    samplerate: int = 22050,
    transport: str | None = None,
    base_url: str = DEFAULT_BASE_URL,
    media_session: Any | None = None,
    timeout: float = 10.0,
) -> None:
    """Play synthesized PCM audio through the robot speaker.

    Parameters
    ----------
    pcm_bytes:
        Raw 16-bit signed PCM audio (mono, little-endian).
    samplerate:
        Sample rate of the PCM data (default 24 000 Hz — Chatterbox TTS output).
        On the sdk path the audio is resampled from this rate to the speaker's
        real output rate before being pushed; on the http path it sets the
        uploaded WAV's header so the daemon resamples it.
    transport:
        ``"sdk"`` or ``"http"``.  ``None`` reads ``REACHY_TRANSPORT`` from the
        environment, falling back to ``"sdk"`` when unset (matching the listen
        noun convention).
    base_url:
        Daemon base URL for the http transport (default ``http://localhost:8000``).
    media_session:
        Inject a fake media manager for testing (sdk path only).  When ``None``
        the sdk path calls ``_open_sdk_media()`` to obtain a real one.
    timeout:
        HTTP request timeout in seconds (http path only; default 10.0).
    """
    effective_transport = transport or os.environ.get("REACHY_TRANSPORT", "sdk")

    if effective_transport == "sdk":
        _play_sdk(pcm_bytes, samplerate=samplerate, media_session=media_session)
    elif effective_transport == "http":
        _play_http(pcm_bytes, samplerate=samplerate, base_url=base_url, timeout=timeout)
    else:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"unknown playback transport {effective_transport!r}",
            remediation="set transport to 'sdk' or 'http', or set REACHY_TRANSPORT accordingly",
        )
