"""TTS synth client for Magpie-style HTTP `/v1/audio/synthesize`.

Sends cleaned text to the endpoint via synchronous ``urllib.request`` (stdlib
only — no httpx / requests) and returns raw PCM16 bytes.

Configuration (environment variables):
    ``REACHY_TTS_URL``   — base URL of the TTS server (default ``http://localhost:9000``).
    ``REACHY_TTS_VOICE`` — voice id sent in the POST form (default a Magpie voice).

Function-argument overrides take precedence over env vars.

Cited from: autonomous-intelligence/realtime-api/src/realtime_api/tts_client.py
  (text-cleaning regexes and split algorithm; ported from async httpx to sync urllib).
"""

from __future__ import annotations

import io
import logging
import os
import re
import urllib.error
import urllib.parse
import urllib.request
import wave

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

DEFAULT_TTS_URL = "http://localhost:9000"
# Magpie multilingual default (matches the reference TTS in ../model-gear). Override
# with --voice / REACHY_TTS_VOICE; list valid ids at GET {TTS_URL}/v1/audio/list_voices.
DEFAULT_VOICE = "Magpie-Multilingual.EN-US.Mia.Calm"
DEFAULT_LANGUAGE = "en-US"
DEFAULT_ENCODING = "LINEAR_PCM"
DEFAULT_SAMPLE_RATE = 22050
DEFAULT_TIMEOUT = 30.0

# Max *cleaned* characters per TTS request (Magpie Triton model sequence limit).
# ~660 clean chars is the empirically safe ceiling; we use 600 for headroom.
_MAX_CLEAN_CHARS = 600

# ---------------------------------------------------------------------------
# Text-cleaning regexes (ported verbatim from reference tts_client.py)
# ---------------------------------------------------------------------------

# Supplementary Multilingual Plane + common emoji ranges
_EMOJI_RE = re.compile(
    "["
    "\U0001f600-\U0001f64f"  # emoticons
    "\U0001f300-\U0001f5ff"  # symbols & pictographs
    "\U0001f680-\U0001f6ff"  # transport & map
    "\U0001f700-\U0001f77f"  # alchemical symbols
    "\U0001f780-\U0001f7ff"  # geometric shapes extended
    "\U0001f800-\U0001f8ff"  # supplemental arrows-C
    "\U0001f900-\U0001f9ff"  # supplemental symbols & pictographs (e.g. 🤖 U+1F916)
    "\U0001fa00-\U0001fa6f"  # chess symbols
    "\U0001fa70-\U0001faff"  # symbols & pictographs extended-A
    "\U0001f1e0-\U0001f1ff"  # flags
    "\U00002702-\U000027b0"  # dingbats
    "\U0000fe00-\U0000fe0f"  # variation selectors
    "\U0000200d"  # zero-width joiner
    "\U000024c2-\U0001f251"
    "]+",
    flags=re.UNICODE,
)

# Markdown-style formatting chars: *, _, ~, `, #
_MARKDOWN_RE = re.compile(r"[*_~`#]")


# ---------------------------------------------------------------------------
# Public text-cleaning helpers
# ---------------------------------------------------------------------------


def clean_for_tts(text: str) -> str:
    """Strip emoji, markdown, dashes, quotes and normalize whitespace for TTS input.

    Returns the cleaned string (may be empty if the input was entirely noise).

    Cited from ``realtime_api.tts_client._clean_for_tts``.
    """
    # Strip emoji
    text = _EMOJI_RE.sub(" ", text)
    # Strip markdown formatting chars
    text = _MARKDOWN_RE.sub("", text)
    # Em-dash / en-dash → comma (natural pause; raw dashes confuse TTS)
    text = text.replace("—", ", ")
    text = text.replace("–", ", ")
    # Curly single quotes / apostrophes → ASCII apostrophe (preserves contractions)
    text = text.replace("‘", "'")
    text = text.replace("’", "'")
    # Strip double-quotes (TTS doesn't need to voice them)
    text = re.sub(r'["“”]', "", text)
    # Remove markdown list markers at line start:  - item  /  1. item
    text = re.sub(r"(?m)^\s*-\s+", " ", text)
    text = re.sub(r"(?m)^\s*\d+[.)]\s+", " ", text)
    # Collapse whitespace / newlines
    text = re.sub(r"\s+", " ", text).strip()
    return text


def split_for_tts(text: str, max_chars: int = _MAX_CLEAN_CHARS) -> list[str]:
    """Split *text* into chunks of at most *max_chars* characters.

    Tries to break at the last ``", "`` before the limit, then the last
    ``" "``, and falls back to a hard cut only when no natural break exists.
    Returns a single-element list when the text already fits.

    Cited from ``realtime_api.tts_client._split_for_tts``.
    """
    if len(text) <= max_chars:
        return [text]

    chunks: list[str] = []
    remaining = text
    while len(remaining) > max_chars:
        window = remaining[:max_chars]
        # Prefer splitting at last ", " (natural pause)
        idx = window.rfind(", ")
        if idx > 0:
            cut = idx + 2  # keep the comma+space with the left chunk
        else:
            # Fall back to last space
            idx = window.rfind(" ")
            if idx > 0:
                cut = idx + 1
            else:
                # Hard cut — no good break point
                cut = max_chars
        chunk = remaining[:cut].strip()
        if chunk:
            chunks.append(chunk)
        remaining = remaining[cut:].strip()

    if remaining:
        chunks.append(remaining)
    return chunks


# ---------------------------------------------------------------------------
# Internal HTTP helper
# ---------------------------------------------------------------------------


def _resolve_tts_url(override: str | None) -> str:
    """Return the TTS base URL: explicit arg > env var > default."""
    return (override or os.environ.get("REACHY_TTS_URL") or DEFAULT_TTS_URL).rstrip("/")


def _resolve_voice(override: str | None) -> str:
    """Return the TTS voice: explicit arg > env var > default."""
    return override or os.environ.get("REACHY_TTS_VOICE") or DEFAULT_VOICE


def _extract_pcm(raw: bytes) -> bytes:
    """Return bare PCM16 samples from *raw*.

    The Magpie TTS returns a full RIFF/WAVE container even for ``LINEAR_PCM``,
    but the rest of the pipeline (playback / cognition) expects raw PCM16 @
    22050 Hz — feeding it a WAV would double-wrap the header and play noise.
    Unwrap the ``data`` chunk when *raw* is a WAV; pass it through unchanged for
    a server that already returns bare PCM.
    """
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        return raw


# Minimum plausible audio: ~15 ms per cleaned char (real speech at this voice is
# ~60-80 ms/char, so 15 ms is a conservative floor). The Magpie server
# intermittently returns a truncated clip; anything below this is a bad response
# we retry. Cited from realtime-api's tts_client truncation guard.
_MIN_SECONDS_PER_CHAR = 0.015
_BYTES_PER_SAMPLE = 2  # PCM16
_SYNTH_ATTEMPTS = 3


def _is_truncated(clean: str, pcm: bytes) -> bool:
    """True when *pcm* is implausibly short for *clean* (a truncated response)."""
    if len(clean) <= 10:
        return False
    duration = len(pcm) / _BYTES_PER_SAMPLE / DEFAULT_SAMPLE_RATE
    return duration < max(0.5, len(clean) * _MIN_SECONDS_PER_CHAR)


def _post_synth(
    clean: str,
    endpoint_url: str,
    voice: str,
    timeout: float,
) -> bytes:
    """Synthesize *clean*, retrying when the server returns truncated audio.

    The Magpie endpoint occasionally returns a short/truncated clip for valid
    input; we re-request up to ``_SYNTH_ATTEMPTS`` times and keep the longest
    result rather than play a clipped fragment. Raises
    :class:`~reachy.cli._errors.CliError` (code 2) on network/HTTP failure.
    """
    best = b""
    for attempt in range(_SYNTH_ATTEMPTS):
        pcm = _synth_once(clean, endpoint_url, voice, timeout)
        if len(pcm) > len(best):
            best = pcm
        if not _is_truncated(clean, pcm):
            return pcm
        log.warning(
            "[tts] truncated audio (%d bytes for %d chars), attempt %d/%d",
            len(pcm),
            len(clean),
            attempt + 1,
            _SYNTH_ATTEMPTS,
        )
    return best


def _synth_once(
    clean: str,
    endpoint_url: str,
    voice: str,
    timeout: float,
) -> bytes:
    """POST *clean* text to *endpoint_url* and return raw PCM bytes (one attempt).

    Raises :class:`~reachy.cli._errors.CliError` (code 2) on any network or
    HTTP-level failure — no raw exception ever escapes this function.
    """
    form_data = urllib.parse.urlencode(
        {
            "text": clean,
            "language": DEFAULT_LANGUAGE,
            "voice": voice,
            "encoding": DEFAULT_ENCODING,
            "sample_rate_hz": str(DEFAULT_SAMPLE_RATE),
        }
    ).encode("utf-8")

    req = urllib.request.Request(  # nosec B310 — URL is caller-controlled config, not user input
        url=endpoint_url,
        data=form_data,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            if int(status) >= 400:
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"TTS server returned HTTP {status}",
                    remediation=(
                        f"check the TTS service at {endpoint_url} is running and healthy; "
                        "set REACHY_TTS_URL to the correct base URL"
                    ),
                )
            return _extract_pcm(resp.read())
    except CliError:
        raise
    except urllib.error.HTTPError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"TTS request failed: HTTP {exc.code} {exc.reason}",
            remediation=(
                f"check the TTS service at {endpoint_url} is running and healthy; "
                "set REACHY_TTS_URL to the correct base URL"
            ),
        ) from exc
    except urllib.error.URLError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"TTS endpoint unreachable: {exc.reason}",
            remediation=(
                f"start the TTS server or point REACHY_TTS_URL at a reachable host "
                f"(tried {endpoint_url})"
            ),
        ) from exc
    except OSError as exc:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"TTS network error: {exc}",
            remediation=(
                f"check network connectivity to the TTS server at {endpoint_url}; "
                "set REACHY_TTS_URL to override the default"
            ),
        ) from exc


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def synthesize(
    text: str,
    *,
    tts_url: str | None = None,
    voice: str | None = None,
    timeout: float = DEFAULT_TIMEOUT,
) -> bytes:
    """Synthesize *text* via a Magpie-style TTS endpoint, returning PCM16 bytes.

    The text is cleaned (markdown/emoji stripped, whitespace normalized) and
    split into chunks that stay within Magpie's sequence-length limit before
    being sent.  All chunks are concatenated into a single bytes object.

    Args:
        text:     Raw text to synthesize (may contain markdown, emoji, etc.).
        tts_url:  Override base URL (overrides ``REACHY_TTS_URL`` env var).
        voice:    Override voice identifier (overrides ``REACHY_TTS_VOICE`` env var).
        timeout:  Per-request socket timeout in seconds (default 30).

    Returns:
        Raw PCM16 bytes at ``DEFAULT_SAMPLE_RATE`` Hz, or ``b""`` if the cleaned
        text is empty.

    Raises:
        :class:`~reachy.cli._errors.CliError` (code 2) when the TTS server is
        unreachable or returns an HTTP error.  No other exception type escapes.
    """
    base_url = _resolve_tts_url(tts_url)
    resolved_voice = _resolve_voice(voice)
    endpoint = f"{base_url}/v1/audio/synthesize"

    clean = clean_for_tts(text)
    if not clean:
        log.debug("[tts] empty text after cleaning (original: %.40s)", text)
        return b""

    chunks = split_for_tts(clean)
    if len(chunks) > 1:
        log.debug("[tts] split into %d chunks (%d total chars)", len(chunks), len(clean))

    parts: list[bytes] = []
    for i, chunk in enumerate(chunks):
        log.debug("[tts] chunk %d/%d (%d chars)", i + 1, len(chunks), len(chunk))
        pcm = _post_synth(chunk, endpoint, resolved_voice, timeout)
        if pcm:
            parts.append(pcm)

    return b"".join(parts)
