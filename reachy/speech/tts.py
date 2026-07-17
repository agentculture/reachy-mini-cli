"""TTS synth client — Chatterbox `/v1/audio/synthesize` or the lobes gateway's
OpenAI-style `/v1/audio/speech`.

Sends cleaned text to one of two HTTP routes via synchronous ``urllib.request``
(stdlib only — no httpx / requests) and returns raw PCM16 bytes:

* ``"chatterbox"`` (default) — POSTs ``{"text": …, "voice": …}`` to
  ``{REACHY_TTS_URL}/v1/audio/synthesize``. The contract of model-gear's
  Chatterbox TTS sidecar (which replaced the earlier Magpie NIM).
* ``"openai"`` — POSTs an OpenAI-shaped ``{"model": …, "input": …, "voice": …}``
  body to ``{REACHY_OPENAI_URL_BASE}/v1/audio/speech`` — the same lobes gateway
  process that serves LLM chat completions (:mod:`reachy.speech.llm`), one more
  route on it rather than a separate service. Auth is a Bearer token from
  ``REACHY_OPENAI_API_KEY`` (the same env var ``llm.py`` uses) sent only when set.

**Live-verified response shape (2026-07-17 probe against the lobes gateway,
``tts`` role = ResembleAI/chatterbox, gateway :8001)**: with no
``response_format`` in the request body, ``POST /v1/audio/speech`` replies
``200`` with ``Content-Type: audio/wav`` — a full RIFF/WAVE container, PCM16
mono @ **24 kHz** (matches ``DEFAULT_SAMPLE_RATE`` below). Passing
``"response_format": "pcm"`` instead returns ``Content-Type: audio/pcm`` — bare
PCM16 with no header, same rate. Requesting the route with no ``Authorization``
header returns ``401``. This module does not send ``response_format`` (so the
gateway's WAV default applies) and relies on ``_extract_pcm`` — already needed
for a WAV-returning Chatterbox — to unwrap either shape uniformly; a bare-PCM
response passes through unchanged either way.

Configuration (environment variables):
    ``REACHY_TTS_ROUTE`` — ``"chatterbox"`` (default) or ``"openai"``, selects
                            which route ``synthesize()`` targets.
    ``REACHY_TTS_URL``   — base URL of the Chatterbox server (default
                            ``http://localhost:9000``); also usable as an
                            explicit ``tts_url=`` override for either route.
    ``REACHY_TTS_VOICE`` — voice id sent in the JSON body. Unset → ``null``, i.e.
                           the server's single built-in default voice.
    ``REACHY_TTS_MODEL`` — model id sent in the OpenAI-shaped ``openai`` route
                            payload (default ``"ResembleAI/chatterbox"``,
                            matching the gateway's live ``tts`` role capability).
    ``REACHY_OPENAI_URL_BASE`` — gateway base URL for the ``openai`` route
                            (default ``http://localhost:8000``, mirroring
                            :mod:`reachy.speech.llm`'s default).
    ``REACHY_OPENAI_API_KEY`` — Bearer token for the ``openai`` route. Omitted
                            (no ``Authorization`` header) when unset.

Function-argument overrides (``tts_url=``, ``voice=``, ``route=``, ``model=``)
take precedence over env vars. When ``route`` is not selected at all (no env,
no kwarg), behavior is byte-identical to the pre-gateway Chatterbox-only client.

Cited from: autonomous-intelligence/realtime-api/src/realtime_api/tts_client.py
  (text-cleaning regexes and split algorithm; ported from async httpx to sync urllib).
"""

from __future__ import annotations

import io
import json
import logging
import os
import re
import urllib.error
import urllib.request
import wave

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants & defaults
# ---------------------------------------------------------------------------

DEFAULT_TTS_URL = "http://localhost:9000"
# Chatterbox exposes a single built-in default voice; ``None`` → ``"voice": null``
# in the JSON body selects it. Override with --voice / REACHY_TTS_VOICE only if the
# server defines named voices.
DEFAULT_VOICE: str | None = None
# Chatterbox returns PCM16 mono @ 24 kHz; the playback stage must use the same rate.
# Live-verified (2026-07-17): the gateway's OpenAI-style /v1/audio/speech route
# returns the same PCM16 @ 24 kHz, so one playback rate serves both routes.
DEFAULT_SAMPLE_RATE = 24000
DEFAULT_TIMEOUT = 30.0

# ---------------------------------------------------------------------------
# Gateway OpenAI-style route (/v1/audio/speech) — env names + defaults
# ---------------------------------------------------------------------------

# Route selection: "chatterbox" (today's /v1/audio/synthesize leg, default) or
# "openai" (the lobes gateway's OpenAI-shaped /v1/audio/speech leg).
_ROUTE_CHATTERBOX = "chatterbox"
_ROUTE_OPENAI = "openai"
_VALID_ROUTES = (_ROUTE_CHATTERBOX, _ROUTE_OPENAI)
DEFAULT_TTS_ROUTE = _ROUTE_CHATTERBOX

# The gateway shares its base URL + Bearer auth with the LLM client
# (reachy.speech.llm.LlmConfig's REACHY_OPENAI_URL_BASE / REACHY_OPENAI_API_KEY)
# by convention — /v1/audio/speech is one more route on the same lobes gateway
# process, not a separate service. Default mirrors llm.py's own default.
DEFAULT_GATEWAY_URL = "http://localhost:8000"
# Matches the gateway's live "tts" role capability (model-gear ResembleAI/chatterbox
# backend), confirmed via the 2026-07-17 /capabilities probe.
DEFAULT_OPENAI_TTS_MODEL = "ResembleAI/chatterbox"

# Max *cleaned* characters per TTS request (TTS model sequence limit).
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


def _resolve_voice(override: str | None) -> str | None:
    """Return the TTS voice: explicit arg > env var > default (``None`` → server default)."""
    return override or os.environ.get("REACHY_TTS_VOICE") or DEFAULT_VOICE


def _resolve_route(override: str | None) -> str:
    """Return the TTS route: explicit arg > ``REACHY_TTS_ROUTE`` env > ``"chatterbox"``.

    Raises:
        :class:`~reachy.cli._errors.CliError` (code 1) when the resolved name
        isn't a registered route.
    """
    resolved = (override or os.environ.get("REACHY_TTS_ROUTE") or DEFAULT_TTS_ROUTE).strip().lower()
    if resolved not in _VALID_ROUTES:
        valid = ", ".join(_VALID_ROUTES)
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown TTS route: {resolved!r}",
            remediation=f"set REACHY_TTS_ROUTE (or pass route=) to one of: {valid}",
        )
    return resolved


def _resolve_gateway_url(override: str | None) -> str:
    """Return the gateway base URL for the ``openai`` route: explicit arg > env > default."""
    return (override or os.environ.get("REACHY_OPENAI_URL_BASE") or DEFAULT_GATEWAY_URL).rstrip("/")


def _resolve_openai_model(override: str | None) -> str:
    """Return the model id for the ``openai`` route's payload: explicit arg > env > default."""
    return override or os.environ.get("REACHY_TTS_MODEL") or DEFAULT_OPENAI_TTS_MODEL


def _resolve_api_key() -> str | None:
    """Return the Bearer token for the ``openai`` route (``REACHY_OPENAI_API_KEY``, or ``None``).

    The literal sentinel ``"EMPTY"`` is treated as "no key" (matching
    :mod:`reachy.speech.llm`'s convention) so a placeholder value never leaks
    ``Authorization: Bearer EMPTY`` into the request.
    """
    key = os.environ.get("REACHY_OPENAI_API_KEY")
    if key == "EMPTY":
        return None
    return key or None


def _extract_pcm(raw: bytes) -> bytes:
    """Return bare PCM16 samples from *raw*.

    Chatterbox returns bare PCM16 (``Content-Type: audio/pcm``), so the common
    path is a straight pass-through. But a Magpie-style server returns a full
    RIFF/WAVE container, and the rest of the pipeline (playback / cognition)
    expects raw PCM16 @ 24 kHz — feeding it a WAV would double-wrap the header
    and play noise. Unwrap the ``data`` chunk when *raw* is a WAV; pass it
    through unchanged for a server that already returns bare PCM.
    """
    if raw[:4] != b"RIFF" or raw[8:12] != b"WAVE":
        return raw
    try:
        with wave.open(io.BytesIO(raw), "rb") as wav:
            return wav.readframes(wav.getnframes())
    except (wave.Error, EOFError):
        return raw


# Minimum plausible audio: ~15 ms per cleaned char (real speech at this voice is
# ~60-80 ms/char, so 15 ms is a conservative floor). The TTS server may
# intermittently return a truncated clip; anything below this is a bad response
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
    voice: str | None,
    timeout: float,
    *,
    route: str = _ROUTE_CHATTERBOX,
    model: str | None = None,
    api_key: str | None = None,
) -> bytes:
    """Synthesize *clean*, retrying when the server returns truncated audio.

    The TTS endpoint occasionally returns a short/truncated clip for valid
    input; we re-request up to ``_SYNTH_ATTEMPTS`` times and keep the longest
    result rather than play a clipped fragment. Raises
    :class:`~reachy.cli._errors.CliError` (code 2) on network/HTTP failure.
    """
    best = b""
    for attempt in range(_SYNTH_ATTEMPTS):
        pcm = _synth_once(
            clean, endpoint_url, voice, timeout, route=route, model=model, api_key=api_key
        )
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
    voice: str | None,
    timeout: float,
    *,
    route: str = _ROUTE_CHATTERBOX,
    model: str | None = None,
    api_key: str | None = None,
) -> bytes:
    """POST *clean* text to *endpoint_url* and return raw PCM bytes (one attempt).

    On the ``"chatterbox"`` route the body is JSON ``{"text": …, "voice": …}``
    (Chatterbox's contract). On the ``"openai"`` route the body is the
    OpenAI-shaped ``{"model": …, "input": …, "voice": …}`` and, when *api_key*
    is set, an ``Authorization: Bearer <api_key>`` header is added — the lobes
    gateway's ``/v1/audio/speech`` contract (live-verified 2026-07-17: WAV
    response, PCM16 @ 24 kHz, ``401`` without the header). Either way a
    ``None`` voice serialises to ``null``, selecting the server's default
    voice. Raises :class:`~reachy.cli._errors.CliError` (code 2) on any
    network or HTTP-level failure — no raw exception ever escapes this
    function.
    """
    headers = {"Content-Type": "application/json"}
    if route == _ROUTE_OPENAI:
        body = json.dumps({"model": model, "input": clean, "voice": voice}).encode("utf-8")
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
    else:
        body = json.dumps({"text": clean, "voice": voice}).encode("utf-8")

    req = urllib.request.Request(  # nosec B310 — URL is caller-controlled config, not user input
        url=endpoint_url,
        data=body,
        method="POST",
        headers=headers,
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
    route: str | None = None,
    model: str | None = None,
) -> bytes:
    """Synthesize *text* via Chatterbox or the lobes gateway, returning PCM16 bytes.

    The text is cleaned (markdown/emoji stripped, whitespace normalized) and
    split into chunks that stay within the model's sequence-length limit before
    being sent.  All chunks are concatenated into a single bytes object.

    Args:
        text:     Raw text to synthesize (may contain markdown, emoji, etc.).
        tts_url:  Override base URL (overrides ``REACHY_TTS_URL`` for the
                  ``"chatterbox"`` route, or ``REACHY_OPENAI_URL_BASE`` for the
                  ``"openai"`` route).
        voice:    Override voice identifier (overrides ``REACHY_TTS_VOICE`` env var).
        timeout:  Per-request socket timeout in seconds (default 30).
        route:    ``"chatterbox"`` (default) or ``"openai"`` — overrides
                  ``REACHY_TTS_ROUTE``. Selecting neither (no arg, no env)
                  keeps today's Chatterbox-only behavior byte-identical.
        model:    Model id sent in the ``"openai"`` route's payload (overrides
                  ``REACHY_TTS_MODEL``); ignored on the ``"chatterbox"`` route.

    Returns:
        Raw PCM16 bytes at ``DEFAULT_SAMPLE_RATE`` Hz, or ``b""`` if the cleaned
        text is empty.

    Raises:
        :class:`~reachy.cli._errors.CliError` (code 2) when the TTS server is
        unreachable or returns an HTTP error; (code 1) when *route* (or
        ``REACHY_TTS_ROUTE``) names an unregistered route. No other exception
        type escapes.
    """
    resolved_route = _resolve_route(route)
    resolved_voice = _resolve_voice(voice)

    if resolved_route == _ROUTE_OPENAI:
        base_url = _resolve_gateway_url(tts_url)
        endpoint = f"{base_url}/v1/audio/speech"
        resolved_model = _resolve_openai_model(model)
        api_key = _resolve_api_key()
    else:
        base_url = _resolve_tts_url(tts_url)
        endpoint = f"{base_url}/v1/audio/synthesize"
        resolved_model = None
        api_key = None

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
        pcm = _post_synth(
            chunk,
            endpoint,
            resolved_voice,
            timeout,
            route=resolved_route,
            model=resolved_model,
            api_key=api_key,
        )
        if pcm:
            parts.append(pcm)

    return b"".join(parts)
