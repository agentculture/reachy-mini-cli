"""Gateway-gated integration test: the full agent tool-use round trip on cortex.

Task t8 — the live robot's day-to-day cognition currently pins
``REACHY_OPENAI_MODEL_ID`` to the lobes **senses** role (a Gemma model proxied
to a peer box); agent tool-use targets the **cortex** role instead — the model
verified to emit ``tool_calls`` with the ``qwen3_coder`` tool parser, served
locally by the same gateway. See docs/operating-reachy.md's "Cortex role
switch — agent tool-use" section for the operator-facing writeup.

Unlike the mocked unit tests (``tests/test_agent_turn.py``, ``test_speech_tools.py``),
this reads the **real** environment (no env-clearing fixture) and talks to the
lobes gateway named by ``REACHY_OPENAI_*`` — mirroring
``tests/test_speech_llm_tools_integration.py``'s gating style. It auto-skips
cleanly when the gateway is unreachable or credentials are missing, so the suite
stays green on CI and on a bare box, and pins the cortex model explicitly so the
test proves tool-calling even when the box env is pinned to the senses role.

Covers acceptance criterion 1 for task t8: a perception-style prompt with a small
tools array drives ``reachy.speech.llm.complete_turn`` through a full round trip —
tool_calls -> fake handlers -> appended OpenAI tool-result messages -> a follow-up
turn with a final assistant text and no further tool_calls (or finish_reason
``stop``). This is the one place the full multi-message tool-result conversation
shape is proven against the real server, outside the engine's fakes.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request

import pytest

from reachy.speech import llm
from reachy.speech.agent_turn import DEFAULT_AGENT_SYSTEM_PROMPT, build_user_message
from reachy.speech.events import SenseCue

# The verified cortex model (tool_use responsibility, parser qwen3_coder). Pinned
# explicitly so this test asserts tool-calling even when the box env is pinned to
# the senses (Gemma) role for day-to-day live cognition.
_CORTEX_MODEL = "sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP"

# The lobes gateway base URL is the SAME for every role — only the model id
# switches roles (see docs/operating-reachy.md's cortex-switch section) — but we
# still pin it explicitly here (rather than trust whatever REACHY_OPENAI_URL_BASE
# happens to resolve to) so this test targets the gateway regardless of the box's
# current environment.d contents.
_CORTEX_BASE_URL = "http://localhost:8001"

_PROBE_TIMEOUT = 3.0
_CALL_TIMEOUT = 30.0
# Bounded like AgentTurnEngine's max_tool_rounds — the round trip should
# converge in one or two rounds; this just guarantees the test itself
# terminates if the model never stops calling tools.
_MAX_ROUNDS = 3
# Bounded retries for the whole round trip (see the docstring in the test body) —
# tolerates transient concurrent-load hiccups from the shared live gateway.
_MAX_ATTEMPTS = 3
# Greedy (temperature 0) — this test asserts the ROUND-TRIP SHAPE (tool_calls ->
# tool results -> final text), not the model's autonomous judgment call under
# ambiguous stimulus. At the production default (0.8) a single-cue perception
# with the system prompt's explicit "call no tools when nothing warrants it"
# opt-out was observed to flake between tool_calls and a plain reply across
# otherwise-identical calls (more so under -n auto's concurrent gateway load);
# greedy decoding keeps this gateway-gated test deterministic without changing
# what conversation shape is exercised.
_TEMPERATURE = 0.0

# A small (2-tool) tools array — the same shape reachy.speech.tools.ToolRegistry
# publishes, hand-written here so this test needs no voice/robot seams.
_TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "apply_pose",
            "description": (
                "Apply a body expression by catalog emoji (e.g. \U0001f914, \U0001f62e)."
            ),
            "parameters": {
                "type": "object",
                "properties": {"emoji": {"type": "string"}},
                "required": ["emoji"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "speak",
            "description": "Speak text aloud in Reachy's spoken voice.",
            "parameters": {
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
            },
        },
    },
]

# A perception-style prompt built from the SAME production helpers
# AgentTurnEngine uses (reachy/speech/agent_turn.py) — so this test exercises
# the exact conversation shape the live agent engine sends, not a hand-rolled
# approximation.
_CUES = [SenseCue(text="someone just patted your head", timestamp=0.0)]
_PROMPT: list[dict] = [
    {"role": "system", "content": DEFAULT_AGENT_SYSTEM_PROMPT},
    {"role": "user", "content": build_user_message(_CUES)},
]


def _gateway_or_skip() -> llm.LlmConfig:
    """Resolve config from the real env (api key only); skip if unset/unreachable.

    Uses the same :meth:`LlmConfig.resolve` the production code uses to pick up
    ``REACHY_OPENAI_API_KEY`` (legacy ``REACHY_LLM_API_KEY`` honoured too), but the
    base URL is pinned to :data:`_CORTEX_BASE_URL` explicitly — this test targets
    the lobes gateway's cortex role regardless of what the box's environment.d
    happens to have configured for day-to-day (senses-role) cognition. A missing
    API key (env not loaded) or a short-timeout connection failure -> skip, never
    a hard failure.
    """
    cfg = llm.LlmConfig.resolve(base_url=_CORTEX_BASE_URL)
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


def _fake_dispatch(call: llm.ToolCall) -> str:
    """A fake tool handler — no robot, no audio, no network.

    Returns the OpenAI tool-result *content* string a real
    ``reachy.speech.tools.ToolRegistry.dispatch`` would produce for a successful
    call, without actually synthesizing speech or enqueueing motion — this test
    proves the conversation round trip, not the tool implementations (those are
    covered by ``tests/test_speech_tools.py``).
    """
    if call.name == "apply_pose":
        return json.dumps({"status": "ok", "emoji": call.arguments.get("emoji", "neutral")})
    if call.name == "speak":
        text = call.arguments.get("text", "")
        return json.dumps({"status": "ok", "chars": len(text)})
    return json.dumps({"error": f"unknown tool: {call.name!r}"})


def _assistant_tool_message(result: llm.TurnResult) -> dict:
    """The OpenAI assistant message carrying one round's tool calls.

    Mirrors ``reachy.speech.agent_turn._assistant_tool_message``'s shape — the
    assistant message must echo its own tool_calls before the tool-result
    messages so the next turn sees them paired (the OpenAI tool protocol).
    """
    return {
        "role": "assistant",
        "content": result.content or "",
        "tool_calls": [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": call.arguments_json},
            }
            for call in result.tool_calls
        ],
    }


def _run_round_trip(cfg: llm.LlmConfig) -> tuple[list[llm.ToolCall], llm.TurnResult | None]:
    """Drive one full round trip; return (every tool call seen, the final turn).

    Bounded like :class:`~reachy.speech.agent_turn.AgentTurnEngine`'s
    ``max_tool_rounds`` — the round trip should converge in one or two rounds;
    :data:`_MAX_ROUNDS` just guarantees this helper itself terminates if the
    model never stops calling tools.
    """
    messages: list[dict] = [dict(m) for m in _PROMPT]
    seen_tool_calls: list[llm.ToolCall] = []
    final: llm.TurnResult | None = None

    for _round in range(_MAX_ROUNDS):
        final = llm.complete_turn(
            messages,
            model=_CORTEX_MODEL,
            base_url=cfg.base_url,
            api_key=cfg.api_key,
            tools=_TOOLS,
            tool_choice="auto",
            temperature=_TEMPERATURE,
            timeout=_CALL_TIMEOUT,
        )
        if not final.tool_calls:
            break
        seen_tool_calls.extend(final.tool_calls)
        messages.append(_assistant_tool_message(final))
        for call in final.tool_calls:
            messages.append(
                {"role": "tool", "tool_call_id": call.id, "content": _fake_dispatch(call)}
            )

    return seen_tool_calls, final


def test_integration_agent_tool_round_trip_on_cortex():
    """Full round trip on cortex: prompt -> tool_calls -> tool results -> final text."""
    cfg = _gateway_or_skip()

    # AC-3-carryover from t1: enable_thinking:false must still ride the request
    # payload alongside the tools array, for this (cortex) model as for any other.
    req = llm._build_request(
        llm.LlmConfig(base_url=cfg.base_url, model=_CORTEX_MODEL, api_key=cfg.api_key),
        _PROMPT,
        temperature=_TEMPERATURE,
        max_tokens=None,
        stream=False,
        tools=_TOOLS,
        tool_choice="auto",
    )
    sent = json.loads(req.data)
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}
    assert sent["tools"] == _TOOLS

    # Retry the round trip a bounded number of times before failing: this is a
    # LIVE, shared gateway (other suites/processes may hit the same model
    # concurrently), and continuous-batched decoding on the server occasionally
    # drops a tool call under concurrent load even at temperature 0 — a call
    # that is reliable in isolation can flake when run alongside
    # tests/test_speech_llm_tools_integration.py under ``-n auto``. Retrying
    # tolerates that transient contention without weakening what is asserted:
    # a single successful attempt still has to complete the full round trip.
    seen_tool_calls: list[llm.ToolCall] = []
    final: llm.TurnResult | None = None
    for _attempt in range(_MAX_ATTEMPTS):
        seen_tool_calls, final = _run_round_trip(cfg)
        if seen_tool_calls:
            break

    # The model must have called at least one tool somewhere in the round trip —
    # the whole point of this test is proving the tool_calls -> tool-result ->
    # follow-up shape, not just a plain chat completion.
    assert (
        seen_tool_calls
    ), f"expected at least one tool call in the round trip after {_MAX_ATTEMPTS} attempts"
    first_call = seen_tool_calls[0]
    assert first_call.name in {"apply_pose", "speak"}
    assert isinstance(first_call.arguments, dict) and first_call.arguments

    # The follow-up turn (after tool results were appended) must converge: no
    # further tool_calls, and a real assistant text — the "final response" leg
    # of the round trip. Assert the shape, not the exact wording.
    assert final is not None
    assert not final.tool_calls, (
        f"expected the round trip to converge within {_MAX_ROUNDS} rounds once "
        "tool results were appended, but the model kept calling tools"
    )
    if final.finish_reason is not None:
        assert final.finish_reason == "stop"
    assert isinstance(final.content, str) and final.content.strip()
