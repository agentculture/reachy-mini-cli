"""Gateway-gated integration test: forge's default model resolves live (#132).

Unlike the mocked unit tests in ``test_forge_client.py``, this talks to the
REAL lobes gateway named by ``FORGE_BASE_URL`` (falling back to
:data:`reachy.forge.client.DEFAULT_FORGE_BASE_URL`, exactly as
:meth:`ForgeClient._run_inner` resolves it) and the real forge/gateway
credentials, and auto-skips cleanly when unreachable or unconfigured — so the
suite stays green on CI and on a bare box. Mirrors
``test_speech_llm_tools_integration.py`` / ``test_stash_embeddings_integration.py``'s
gating style.

This drives the exact transport function (:func:`reachy.forge.client._default_transport`)
and URL-building :meth:`ForgeClient._run_inner` uses, with the real
``DEFAULT_FORGE_MODEL`` and a small bounded ``max_tokens`` — proving live model
RESOLUTION (the #132 regression: the old default, the served id ``qwen3``,
404s) rather than running a full open-ended skill-generation round trip, whose
wall-clock the coder model's own "thinking" budget controls and this test has
no reason to bound.
"""

from __future__ import annotations

import os
import urllib.error
import urllib.request

import pytest

from reachy.forge.client import (
    DEFAULT_FORGE_BASE_URL,
    DEFAULT_FORGE_MODEL,
    _default_transport,
    _resolve_forge_api_key,
)

_PROBE_TIMEOUT = 3.0
_CALL_TIMEOUT = 15.0

#: Substrings that mean "the model id itself doesn't resolve" — the exact #132
#: failure mode, as opposed to a transient/other transport error.
_MODEL_NOT_FOUND_MARKERS = ("404", "model_not_found", "does not exist")


def _gateway_or_skip() -> tuple[str, str]:
    """Resolve forge's real base_url/api_key; skip cleanly if unset/unreachable.

    Uses the SAME resolution :meth:`ForgeClient._run_inner` uses
    (``FORGE_BASE_URL`` / :func:`_resolve_forge_api_key`), so this test targets
    exactly what a real dispatch would.
    """
    base_url = os.environ.get("FORGE_BASE_URL") or DEFAULT_FORGE_BASE_URL
    api_key = _resolve_forge_api_key()
    if not api_key:
        pytest.skip(
            "no forge/gateway credentials resolved "
            "(FORGE_API_KEY / REACHY_OPENAI_API_KEY unset) — skipping"
        )

    url = base_url.rstrip("/") + "/models"
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {api_key}"})
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT):  # nosec B310
            pass
    except urllib.error.HTTPError as err:
        # A 4xx means the server is *up* — proceed; only a hard transport
        # failure or a dead server (5xx) should skip.
        if err.code >= 500:
            pytest.skip(f"forge gateway {base_url} returned HTTP {err.code} — skipping")
    except OSError as err:
        pytest.skip(f"forge gateway {base_url} unreachable ({err}) — skipping")
    return base_url, api_key


def test_integration_forge_default_model_resolves_via_cortex_role(monkeypatch):
    """The exact #132 regression check: DEFAULT_FORGE_MODEL must not 404.

    Live-probed 2026-08-02 (see the plan/spec + tests/test_gateway_role_model_defaults.py):
    ``qwen3`` -> ``model_not_found`` (HTTP 404); ``cortex`` -> HTTP 200. This
    calls the real gateway with the exact default the production code ships,
    so a future model promotion that breaks role resolution fails this test
    loudly instead of silently breaking forge on the next deploy.
    """
    monkeypatch.delenv("FORGE_MODEL", raising=False)  # exercise DEFAULT_FORGE_MODEL itself
    base_url, api_key = _gateway_or_skip()

    url = base_url.rstrip("/") + "/chat/completions"
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {api_key}"}
    payload = {
        "model": DEFAULT_FORGE_MODEL,
        "messages": [{"role": "user", "content": "Reply with exactly one word: OK"}],
        "max_tokens": 5,
    }
    try:
        response = _default_transport(url, payload, headers, _CALL_TIMEOUT)
    except Exception as err:  # noqa: BLE001 - the assertion below decides fail vs skip
        text = str(err)
        model_missing = any(marker in text for marker in _MODEL_NOT_FOUND_MARKERS)
        assert (
            not model_missing
        ), f"DEFAULT_FORGE_MODEL {DEFAULT_FORGE_MODEL!r} failed to resolve live: {text}"
        pytest.skip(f"forge endpoint transport failed for a non-#132 reason: {text}")

    assert response.get("choices"), f"unexpected reply shape from {base_url}: {response}"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
