"""Synchronous, stdlib-only OpenAI-compatible chat-completions streaming client.

This module streams an OpenAI-compatible ``/v1/chat/completions`` endpoint with
``stream=true`` and yields **complete sentences early** via
:func:`stream_sentences`, so downstream TTS can start speaking the first
sentence while the model is still generating the rest.

It is a synchronous, **standard-library** port of the async/httpx reference at
``autonomous-intelligence/realtime-api/src/realtime_api/llm_client.py`` — the
quote/paren/markdown-aware sentence splitter (``_find_sentence_breaks`` /
``_split_buffer``), the SSE parse (``data: `` lines, ``[DONE]`` sentinel,
``chunk["choices"][0]["delta"]["content"]``), and the loose-fallback regex are
faithfully preserved from that source. The transport is reimplemented on
:mod:`urllib.request`: the response object is wrapped in an
:class:`io.TextIOWrapper` and iterated line-by-line so deltas are parsed *as
they arrive* off the socket, never buffered whole.

Pure standard library (``urllib`` + ``json``) — no httpx/requests/openai — so
the module adds no runtime dependency beyond the slim base install.
"""

from __future__ import annotations

import json
import os
import re
import urllib.error
import urllib.request
from collections.abc import Iterator
from dataclasses import dataclass

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# Default endpoint + model mirror the daemon-local profile; overridable via env.
_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_MODEL = "default"
_DEFAULT_TIMEOUT = 120.0

# Fallback regex: any .!? + whitespace — used when the buffer grows very long
# without a proper sentence break so we don't starve TTS of input. (Cited
# verbatim from the reference's _SENTENCE_RE_LOOSE.)
_SENTENCE_RE_LOOSE = re.compile(
    r"(?<=[.!?])\s+"  # split after .!? + whitespace
    r"|\s*[—–]\s+"  # em-dash / en-dash (required trailing whitespace)
    r"|\s+-\s+"  # ASCII hyphen (required spaces both sides)
)

# Switch to the loose regex when the buffer exceeds this many characters.
_MAX_BUFFER_BEFORE_LOOSE = 200

_MARKDOWN_CHARS = frozenset("*_~`#")


def _env_pref(primary: str, legacy: str, default: str | None) -> str | None:
    """Presence-based precedence between the canonical + legacy env names.

    A primary variable that is *set* wins even when its value is empty — only a
    truly **unset** primary falls through to the legacy name, then the default.
    A truthiness ``or`` chain would instead treat ``""`` as "unset" and silently
    pick up the legacy/default value (e.g. sending a stale legacy API key when
    the operator explicitly set ``REACHY_OPENAI_API_KEY=""`` to mean "no auth").
    """
    if primary in os.environ:
        return os.environ[primary]
    if legacy in os.environ:
        return os.environ[legacy]
    return default


@dataclass
class LlmConfig:
    """Resolved LLM connection config.

    Read from the canonical ``REACHY_OPENAI_URL_BASE`` / ``REACHY_OPENAI_API_KEY``
    / ``REACHY_OPENAI_MODEL_ID`` environment variables, with explicit
    ``base_url=`` / ``model=`` / ``api_key=`` argument overrides taking
    precedence over the environment. The legacy ``REACHY_LLM_BASE_URL`` /
    ``REACHY_LLM_API_KEY`` / ``REACHY_LLM_MODEL`` names are still honoured as a
    fallback (used only when the matching ``REACHY_OPENAI_*`` var is unset), so
    older configs keep working.

    Precedence is by *presence*, not truthiness: an explicitly provided argument
    or a set-but-empty ``REACHY_OPENAI_*`` variable wins over the legacy name and
    the default. See :func:`_env_pref`.
    """

    base_url: str
    model: str
    api_key: str | None = None

    @classmethod
    def resolve(
        cls,
        *,
        base_url: str | None = None,
        model: str | None = None,
        api_key: str | None = None,
    ) -> "LlmConfig":
        return cls(
            base_url=(
                base_url
                if base_url is not None
                else _env_pref("REACHY_OPENAI_URL_BASE", "REACHY_LLM_BASE_URL", _DEFAULT_BASE_URL)
            ),
            model=(
                model
                if model is not None
                else _env_pref("REACHY_OPENAI_MODEL_ID", "REACHY_LLM_MODEL", _DEFAULT_MODEL)
            ),
            api_key=(
                api_key
                if api_key is not None
                else _env_pref("REACHY_OPENAI_API_KEY", "REACHY_LLM_API_KEY", None)
            ),
        )


# ---------------------------------------------------------------------------
# Quote / parenthesis-aware sentence splitter (ported from the reference)
# ---------------------------------------------------------------------------


def _update_nesting(ch: str, paren_depth: int, quote_open: bool) -> tuple[int, bool]:
    """Fold one character into the quote / paren nesting state."""
    if ch == "“":  # left "
        quote_open = True
    elif ch == "”":  # right "
        quote_open = False
    elif ch == '"':  # ASCII — toggle
        quote_open = not quote_open

    if ch == "(":
        paren_depth += 1
    elif ch == ")":
        paren_depth = max(0, paren_depth - 1)

    return paren_depth, quote_open


def _ends_sentence(text: str, i: int) -> bool:
    """Whether index *i* is sentence-terminal: ``.!?`` directly, or a closing
    quote / paren that immediately follows terminal punctuation (``."`` ``!)``)."""
    ch = text[i]
    if ch in ".!?":
        return True
    if ch in '"”)':
        k = i - 1
        while k >= 0 and text[k] in "\"”)’'":
            k -= 1
        return k >= 0 and text[k] in ".!?"
    return False


def _next_sentence_start(text: str, i: int) -> int | None:
    """After a terminal at *i*, return the raw index where the next sentence
    starts — skipping closing markdown then requiring whitespace — but only when
    the following character (through opening markdown) is uppercase. Else None.
    """
    # Skip closing markdown after terminal punct (e.g. !** or ."*)
    j = i + 1
    while j < len(text) and text[j] in _MARKDOWN_CHARS:
        j += 1
    # Must find at least one whitespace character
    ws_start = j
    while j < len(text) and text[j] in " \t\n\r":
        j += 1
    if j == ws_start:
        return None
    # Start of the next sentence in raw text (may include opening markdown)
    sentence_start = j
    # Peek past opening markdown to find the actual first letter
    while j < len(text) and text[j] in _MARKDOWN_CHARS:
        j += 1
    if j < len(text) and text[j].isupper():
        return sentence_start
    return None


def _find_sentence_breaks(text: str) -> list[int]:
    """Return character indices where new sentences start.

    A break is placed after terminal punctuation (``.!?``) — or a closing
    quote / paren that immediately follows terminal punctuation — when:

    1. We are not inside quotation marks or parentheses, **and**
    2. the next non-whitespace character (ignoring markdown formatting like
       ``**``) is an uppercase letter.

    This avoids splitting inside quoted speech (``"Hey! What's up?"``) and
    parenthetical asides (``(well, speaking!) or …``), while still detecting
    boundaries through markdown.
    """
    breaks: list[int] = []
    paren_depth = 0
    quote_open = False

    for i, ch in enumerate(text):
        paren_depth, quote_open = _update_nesting(ch, paren_depth, quote_open)
        # While nested inside quotes or parens, no boundary is possible.
        if paren_depth > 0 or quote_open:
            continue
        if not _ends_sentence(text, i):
            continue
        start = _next_sentence_start(text, i)
        if start is not None:
            breaks.append(start)

    return breaks


def _split_buffer(text: str, loose: bool = False) -> tuple[list[str], str]:
    """Split *text* into ``(complete_sentences, remaining_buffer)``.

    In normal mode uses the quote/paren-aware boundary finder. In *loose* mode
    falls back to a simple regex that ignores nesting and letter-case — this
    keeps TTS fed when the buffer grows very long without a proper break.
    """
    if loose:
        parts = _SENTENCE_RE_LOOSE.split(text)
        sentences = [s.strip() for s in parts[:-1] if s.strip()]
        return sentences, parts[-1]

    breaks = _find_sentence_breaks(text)
    if not breaks:
        return [], text

    sentences: list[str] = []
    start = 0
    for brk in breaks:
        sentence = text[start:brk].strip()
        if sentence:
            sentences.append(sentence)
        start = brk
    return sentences, text[start:]


# ---------------------------------------------------------------------------
# SSE transport (synchronous urllib, incremental line iteration)
# ---------------------------------------------------------------------------


def _build_request(
    cfg: LlmConfig,
    messages: list[dict],
    *,
    temperature: float,
    max_tokens: int | None,
) -> urllib.request.Request:
    url = cfg.base_url.rstrip("/") + "/v1/chat/completions"
    payload: dict = {
        "model": cfg.model,
        "messages": messages,
        "stream": True,
        "temperature": temperature,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    if max_tokens is not None:
        payload["max_tokens"] = max_tokens

    headers = {"Content-Type": "application/json", "Accept": "text/event-stream"}
    # Bearer auth only when a real key is present (the reference treats the
    # literal "EMPTY" as "no key" for local OpenAI-compatible servers).
    if cfg.api_key and cfg.api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    data = json.dumps(payload).encode("utf-8")
    return urllib.request.Request(url, data=data, headers=headers, method="POST")


def stream_chat_completion(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    max_tokens: int | None = None,
    base_url: str | None = None,
    api_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> Iterator[str]:
    """Stream a chat completion, yielding text content deltas as they arrive.

    Uses the standard OpenAI SSE streaming format. The response is iterated
    line-by-line so each ``data:`` delta is parsed and yielded the moment it
    comes off the socket — nothing is buffered to completion first.

    Raises :class:`CliError` (exit code 2, environment) with a remediation hint
    if the endpoint is unreachable or returns a non-2xx status — never a
    Python traceback.
    """
    cfg = LlmConfig.resolve(base_url=base_url, model=model, api_key=api_key)
    req = _build_request(cfg, messages, temperature=temperature, max_tokens=max_tokens)

    try:
        resp_cm = urllib.request.urlopen(req, timeout=timeout)  # nosec B310
    except urllib.error.HTTPError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"LLM endpoint returned HTTP {err.code} ({cfg.base_url})",
            remediation=(
                "check REACHY_OPENAI_MODEL_ID is served by this endpoint and "
                "REACHY_OPENAI_API_KEY is valid"
            ),
        ) from err
    except OSError as err:  # URLError is an OSError subclass — this covers both
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot reach LLM at {cfg.base_url}: {err}",
            remediation=(
                "start the LLM server or set REACHY_OPENAI_URL_BASE (and "
                "REACHY_OPENAI_API_KEY / REACHY_OPENAI_MODEL_ID) to a reachable endpoint"
            ),
        ) from err

    with resp_cm as resp:
        status = getattr(resp, "status", None) or resp.getcode()
        if not (200 <= int(status) < 300):
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"LLM endpoint returned HTTP {status} ({cfg.base_url})",
                remediation="check the LLM server logs and your model/credentials",
            )

        # Iterate the raw byte response one line at a time and decode each line
        # as it arrives — ``readline`` pulls only the next line off the wire, so
        # deltas are parsed incrementally rather than buffered to completion.
        yield from _iter_sse_deltas(resp)


def _iter_sse_deltas(resp) -> Iterator[str]:  # noqa: ANN001
    """Parse an SSE byte stream line-by-line, yielding content deltas.

    Honors the OpenAI contract: lines beginning ``data: ``; the ``[DONE]``
    sentinel terminates the stream; malformed JSON / missing keys are skipped.
    Reads raw bytes and decodes per line so it works against any file-like
    response (the real ``http.client.HTTPResponse`` and test doubles alike).
    """
    while True:
        raw = resp.readline()
        if not raw:  # EOF (b"" or "")
            break
        line = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        line = line.rstrip("\r\n")
        if not line.startswith("data: "):
            continue
        data = line[6:]
        if data == "[DONE]":
            break
        try:
            chunk = json.loads(data)
            delta = chunk["choices"][0].get("delta", {})
            content = delta.get("content")
        except (json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue
        if content:
            yield content


def stream_sentences(
    messages: list[dict],
    *,
    model: str | None = None,
    temperature: float = 0.8,
    base_url: str | None = None,
    api_key: str | None = None,
    cancel=None,
) -> Iterator[str]:
    """Stream a chat completion and yield complete sentences early.

    Buffers incoming deltas and emits each complete sentence as soon as its
    boundary is detected — so the first sentence is yielded long before the
    model finishes. Uses the quote/paren-aware splitter, falling back to the
    loose regex once the buffer exceeds *_MAX_BUFFER_BEFORE_LOOSE* chars. The
    trailing partial buffer is flushed as a final sentence at end-of-stream.

    ``cancel`` is an optional zero-arg predicate (or an object with ``is_set``);
    when it signals truthy, streaming stops after the current delta.
    """
    is_cancelled = _coerce_cancel(cancel)
    buffer = ""
    for delta in stream_chat_completion(
        messages,
        model=model,
        temperature=temperature,
        base_url=base_url,
        api_key=api_key,
    ):
        if is_cancelled():
            break
        buffer += delta
        sentences, buffer = _split_buffer(buffer, loose=len(buffer) > _MAX_BUFFER_BEFORE_LOOSE)
        for sentence in sentences:
            yield sentence

    if buffer.strip():
        yield buffer.strip()


def _coerce_cancel(cancel) -> "callable":  # noqa: ANN001
    """Normalize a cancel token (None / callable / Event-like) to a predicate."""
    if cancel is None:
        return lambda: False
    if hasattr(cancel, "is_set"):
        return cancel.is_set
    if callable(cancel):
        return cancel
    return lambda: bool(cancel)
