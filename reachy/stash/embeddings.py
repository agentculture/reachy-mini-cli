"""Stdlib-only client for the lobes gateway ``/v1/embeddings`` route.

Mirrors :mod:`reachy.speech.llm`'s ``REACHY_OPENAI_*`` env-based config
resolution style (base URL / API key), but is a small, INDEPENDENT module: the
stash package must never import :mod:`reachy.speech.llm` (the stash stays
independent of the chat/tool-use client — see the module docstring of
:mod:`reachy.stash`). ``urllib`` + ``json`` only, no new runtime dependency.

The wire shape is OpenAI's embeddings endpoint::

    POST {base_url}/v1/embeddings
    {"model": "Qwen/Qwen3-Embedding-0.6B", "input": "<text>"}
    -> {"data": [{"embedding": [...]}]}
"""

from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# Defaults mirror reachy/speech/llm.py's daemon-local profile: the lobes gateway is
# reachable at :8000 by default (on this box the live gateway is :8001 — set via
# REACHY_OPENAI_URL_BASE).
_DEFAULT_BASE_URL = "http://localhost:8000"
_DEFAULT_MODEL = "Qwen/Qwen3-Embedding-0.6B"
_DEFAULT_TIMEOUT = 30.0


@dataclass
class EmbeddingConfig:
    """Resolved embeddings-endpoint config.

    Read from ``REACHY_OPENAI_URL_BASE`` / ``REACHY_OPENAI_API_KEY`` — the SAME
    gateway credentials the LLM client uses, since it is the same lobes gateway —
    plus a dedicated ``REACHY_OPENAI_EMBED_MODEL_ID`` for the embedder model name
    (defaults to the verified-live ``Qwen/Qwen3-Embedding-0.6B``). Explicit
    keyword arguments always take precedence over the environment.
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
    ) -> "EmbeddingConfig":
        return cls(
            base_url=(
                base_url
                if base_url is not None
                else os.environ.get("REACHY_OPENAI_URL_BASE", _DEFAULT_BASE_URL)
            ),
            model=(
                model
                if model is not None
                else os.environ.get("REACHY_OPENAI_EMBED_MODEL_ID", _DEFAULT_MODEL)
            ),
            api_key=(api_key if api_key is not None else os.environ.get("REACHY_OPENAI_API_KEY")),
        )


def embed_text(
    text: str,
    *,
    base_url: str | None = None,
    model: str | None = None,
    api_key: str | None = None,
    timeout: float = _DEFAULT_TIMEOUT,
) -> list[float]:
    """POST *text* to the gateway ``/v1/embeddings`` route and return the vector.

    Raises :class:`~reachy.cli._errors.CliError` (exit code 2, environment) with a
    remediation hint on any transport / HTTP failure or an unexpected response
    shape — never a Python traceback.
    """
    cfg = EmbeddingConfig.resolve(base_url=base_url, model=model, api_key=api_key)
    url = cfg.base_url.rstrip("/") + "/v1/embeddings"
    payload = {"model": cfg.model, "input": text}
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    # Match the LLM client's convention: the literal "EMPTY" means "no key" for
    # local OpenAI-compatible servers.
    if cfg.api_key and cfg.api_key != "EMPTY":
        headers["Authorization"] = f"Bearer {cfg.api_key}"

    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(url, data=data, headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            if not (200 <= int(status) < 300):
                raise CliError(
                    code=EXIT_ENV_ERROR,
                    message=f"embeddings endpoint returned HTTP {status} ({cfg.base_url})",
                    remediation="check the gateway logs and REACHY_OPENAI_* config",
                )
            raw = resp.read()
    except urllib.error.HTTPError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"embeddings endpoint returned HTTP {err.code} ({cfg.base_url})",
            remediation=(
                "check REACHY_OPENAI_EMBED_MODEL_ID is served by this endpoint and "
                "REACHY_OPENAI_API_KEY is valid"
            ),
        ) from err
    except OSError as err:  # URLError is an OSError subclass — this covers both
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"cannot reach embeddings endpoint at {cfg.base_url}: {err}",
            remediation=(
                "start the lobes gateway or set REACHY_OPENAI_URL_BASE to a reachable endpoint"
            ),
        ) from err

    body = json.loads(raw.decode("utf-8") if isinstance(raw, bytes) else raw)
    try:
        vector = body["data"][0]["embedding"]
    except (KeyError, IndexError, TypeError) as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"embeddings response from {cfg.base_url} is missing data[0].embedding",
            remediation="check the gateway's /v1/embeddings response shape",
        ) from err

    if not isinstance(vector, list):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"embeddings response from {cfg.base_url} has a non-list embedding",
            remediation="check the gateway's /v1/embeddings response shape",
        )
    return [float(x) for x in vector]
