"""Gateway-gated integration tests for tool-calling against the live cortex model.

Unlike the mocked unit tests, these read the **real** environment (no env-clearing
fixture) and talk to the lobes gateway named by ``REACHY_OPENAI_*``. Every test
auto-skips cleanly when the gateway is unreachable, credentials are missing, or the
short probe times out — so the suite stays green on CI and on a bare box.

Covers acceptance criterion 3 for task t1:

- a tool-warranting prompt to the cortex model returns ``finish_reason=tool_calls``
  (strict, non-streaming), with ``chat_template_kwargs: {enable_thinking: false}``
  still present in the payload;
- the streaming path is checked leniently (plan risk r2): it asserts when the
  gateway streams tool_call deltas and skips with a clear reason otherwise.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from reachy.speech import llm

# The verified cortex model (tool_use responsibility, parser qwen3_coder). We pin
# it explicitly so the test asserts tool-calling even when the box env is pinned to
# the senses (Gemma) role for day-to-day live cognition.
_CORTEX_MODEL = "sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP"

_PROBE_TIMEOUT = 3.0
_CALL_TIMEOUT = 30.0

# The verified-working tool + prompt from the 2026-07-17 probe.
_APPLY_POSE_TOOL = {
    "type": "function",
    "function": {
        "name": "apply_pose",
        "parameters": {
            "type": "object",
            "properties": {"emoji": {"type": "string"}},
            "required": ["emoji"],
        },
    },
}
_PROMPT = [{"role": "user", "content": "Someone just patted your head. React by applying a pose."}]


def _gateway_or_skip() -> llm.LlmConfig:
    """Resolve config from the real env; skip cleanly if unset or unreachable.

    Uses the same :meth:`LlmConfig.resolve` the production code uses so the test
    honours ``REACHY_OPENAI_*`` / legacy ``REACHY_LLM_*`` exactly. A missing API
    key (env not loaded) or a short-timeout connection failure -> skip, never a
    hard failure.
    """
    cfg = llm.LlmConfig.resolve()
    if not cfg.api_key or cfg.api_key == "EMPTY":
        pytest.skip("gateway credentials not set (REACHY_OPENAI_API_KEY unset) — skipping")
    if not cfg.base_url:
        pytest.skip("no REACHY_OPENAI_URL_BASE resolved — skipping")

    # Cheap reachability probe against /v1/models with a short timeout.
    url = cfg.base_url.rstrip("/") + "/v1/models"
    headers = {"Authorization": f"Bearer {cfg.api_key}"}
    req = urllib.request.Request(url, headers=headers, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=_PROBE_TIMEOUT):  # nosec B310
            pass
    except urllib.error.HTTPError as err:
        # A 4xx means the server is *up* (auth/route quirk) — proceed; the real
        # call will surface a genuine problem. Only a hard transport failure skips.
        if err.code >= 500:
            pytest.skip(f"gateway {cfg.base_url} returned HTTP {err.code} — skipping")
    except OSError as err:
        pytest.skip(f"gateway {cfg.base_url} unreachable ({err}) — skipping")
    return cfg


def test_integration_complete_turn_returns_tool_calls():
    """Strict: a non-streaming tool-warranting turn against cortex -> finish_reason=tool_calls."""
    cfg = _gateway_or_skip()

    # Assert the wire payload still carries enable_thinking:false alongside tools
    # (AC-3 requirement) — inspect the request the client builds for this call.
    req = llm._build_request(
        llm.LlmConfig(base_url=cfg.base_url, model=_CORTEX_MODEL, api_key=cfg.api_key),
        _PROMPT,
        temperature=0.8,
        max_tokens=None,
        stream=False,
        tools=[_APPLY_POSE_TOOL],
        tool_choice="auto",
    )
    sent = json.loads(req.data)
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["tools"] == [_APPLY_POSE_TOOL]

    result = llm.complete_turn(
        _PROMPT,
        model=_CORTEX_MODEL,
        tools=[_APPLY_POSE_TOOL],
        tool_choice="auto",
        timeout=_CALL_TIMEOUT,
    )
    assert result.finish_reason == "tool_calls"
    assert result.tool_calls, "expected at least one tool call"
    call = result.tool_calls[0]
    assert call.name == "apply_pose"
    assert isinstance(call.arguments, dict)
    assert "emoji" in call.arguments


def test_integration_stream_turn_tool_calls_lenient():
    """Lenient (risk r2): assert streamed tool_call assembly, else skip with the finding."""
    _gateway_or_skip()  # skip cleanly when the gateway is unreachable / unconfigured
    result = llm.stream_turn(
        _PROMPT,
        model=_CORTEX_MODEL,
        tools=[_APPLY_POSE_TOOL],
        tool_choice="auto",
        timeout=_CALL_TIMEOUT,
    )
    if result.finish_reason != "tool_calls" or not result.tool_calls:
        pytest.skip(
            "gateway did not stream tool_call deltas "
            f"(finish_reason={result.finish_reason!r}, tool_calls={len(result.tool_calls)}) "
            "— streaming tool-calls unverified server-side (plan risk r2)"
        )
    call = result.tool_calls[0]
    assert call.name == "apply_pose"
    assert isinstance(call.arguments, dict)
    assert "emoji" in call.arguments
