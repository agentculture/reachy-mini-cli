"""Gateway-gated integration test for the stash embeddings client.

Unlike the mocked unit tests in ``test_stash_embeddings.py``, this talks to the
REAL lobes gateway named by ``REACHY_OPENAI_*`` and auto-skips cleanly when the
gateway is unreachable, credentials are missing, or the short probe times out —
so the suite stays green on CI and on a bare box. Mirrors the skip pattern used
by ``test_speech_llm_tools_integration.py``.
"""

from __future__ import annotations

import urllib.error
import urllib.request

import pytest

from reachy.cli._errors import CliError
from reachy.stash.embeddings import EmbeddingConfig, embed_text

_PROBE_TIMEOUT = 3.0
_CALL_TIMEOUT = 15.0


def _gateway_or_skip() -> EmbeddingConfig:
    cfg = EmbeddingConfig.resolve()
    if not cfg.api_key or cfg.api_key == "EMPTY":
        pytest.skip("gateway credentials not set (REACHY_OPENAI_API_KEY unset) — skipping")

    url = cfg.base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT):  # nosec B310
            pass
    except urllib.error.HTTPError as err:
        if err.code >= 500:
            pytest.skip(f"gateway {cfg.base_url} returned HTTP {err.code} — skipping")
    except OSError as err:
        pytest.skip(f"gateway {cfg.base_url} unreachable ({err}) — skipping")
    return cfg


def test_integration_embed_one_real_explanation_returns_a_nonempty_vector():
    cfg = _gateway_or_skip()
    try:
        vector = embed_text(
            "a gentle nod used to acknowledge that Reachy heard something",
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            timeout=_CALL_TIMEOUT,
        )
    except CliError as err:
        # The /v1/models probe above only proves the gateway process is up — the
        # embedder route/model can still be slow, unloaded, or down independently.
        # Any transport-level failure here means "unreachable for this route",
        # which is exactly what a gateway-gated test should skip on, not fail.
        pytest.skip(f"embeddings route unavailable ({err.message}) — skipping")
    assert isinstance(vector, list)
    assert len(vector) > 0
    assert all(isinstance(x, float) for x in vector)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
