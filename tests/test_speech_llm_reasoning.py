"""The streaming reasoning seam on ``reachy/speech/llm.py`` — additive, opt-in.

Task t10 of the ``embodiment-layer`` plan. ``llm.py`` is SHARED — ``agent
attach``'s :class:`~reachy.speech.agent_turn.AgentTurnEngine` and the runtime's
engagement classifier both ride it — so the reasoning seam had to be added in a
way existing callers cannot notice. Half of this file proves the new behaviour;
the other half proves the ABSENCE of a change (the request payload, the
content-only reader, and ``TurnResult``'s existing fields).

The field name is the point
---------------------------
vLLM documents ``delta.reasoning_content``. Our gateway sends
``delta.reasoning`` — reproduced at ``localhost:8001`` (``model=cortex``,
``stream=true``, 73 chunks, delta keys ``['content', 'reasoning', 'role']``) and
independently by a sibling project;
``docs/evidence/2026-08-01-cited-findings-from-embodiment-sibling.md`` is the
record. Writing the consumer against the documented name yields a permanently
empty thinking feed with nothing to flag it, so the reproduced name is pinned
here by equality and by a live-shaped stream, not by a comment.
"""

from __future__ import annotations

import json

import pytest

from reachy.speech import llm
from tests.fake_sse_server import (
    FakeChatServer,
    Script,
    content_chunk,
    finish,
    json_body,
    reasoning_chunk,
    role_chunk,
    tool_call_chunk,
)

_MESSAGES = [{"role": "user", "content": "hello"}]


# --------------------------------------------------------------------------- #
# The reproduced field name                                                   #
# --------------------------------------------------------------------------- #


def test_the_gateway_reasoning_key_is_first_in_the_precedence_list() -> None:
    """``reasoning`` — the observed name — leads; the documented name is a fallback."""
    assert llm.REASONING_DELTA_KEYS[0] == "reasoning"
    assert "reasoning_content" in llm.REASONING_DELTA_KEYS


def test_streaming_reasoning_deltas_reach_the_turn_result_and_the_callback() -> None:
    """A stream shaped like the measured one fills ``TurnResult.reasoning``."""
    seen: list[str] = []
    script = Script(
        chunks=[
            role_chunk(),
            reasoning_chunk("Here"),
            reasoning_chunk(" we go"),
            content_chunk("Hi."),
            finish(),
        ]
    )
    with FakeChatServer(script=script) as server:
        result = llm.stream_turn(
            _MESSAGES,
            base_url=server.base_url,
            model="worker",
            on_reasoning=seen.append,
        )

    assert result.reasoning == "Here we go"
    assert seen == ["Here", " we go"]
    assert result.content == "Hi."


def test_the_documented_name_is_read_too_so_a_conforming_server_is_not_silent() -> None:
    """A server that follows vLLM's docs (``reasoning_content``) is still understood."""
    script = Script(
        chunks=[reasoning_chunk("doc", key="reasoning_content"), content_chunk("x"), finish()]
    )
    with FakeChatServer(script=script) as server:
        result = llm.stream_turn(_MESSAGES, base_url=server.base_url, model="worker")
    assert result.reasoning == "doc"


def test_a_content_only_stream_leaves_reasoning_empty() -> None:
    """No reasoning on the wire is an empty string, never ``None`` — one type for callers."""
    script = Script(chunks=[content_chunk("just words"), finish()])
    with FakeChatServer(script=script) as server:
        result = llm.stream_turn(_MESSAGES, base_url=server.base_url, model="worker")
    assert result.reasoning == ""


# --------------------------------------------------------------------------- #
# Existing callers cannot notice — the absence-of-change half                 #
# --------------------------------------------------------------------------- #


def test_stream_turn_is_unchanged_when_the_new_seam_is_unused() -> None:
    """Content + tool calls + finish_reason are exactly what they were before t10."""
    script = Script(
        chunks=[
            role_chunk(),
            content_chunk("thinking out loud"),
            tool_call_chunk(index=0, call_id="call_1", name="speak"),
            tool_call_chunk(index=0, arguments='{"text":'),
            tool_call_chunk(index=0, arguments='"hi"}'),
            finish("tool_calls"),
        ]
    )
    with FakeChatServer(script=script) as server:
        result = llm.stream_turn(_MESSAGES, base_url=server.base_url, model="cortex")

    assert result.content == "thinking out loud"
    assert result.finish_reason == "tool_calls"
    assert [(c.id, c.name, c.arguments) for c in result.tool_calls] == [
        ("call_1", "speak", {"text": "hi"})
    ]


def test_turn_result_still_constructs_from_its_three_original_fields() -> None:
    """``reasoning`` is a defaulted field appended last: old call sites are untouched."""
    result = llm.TurnResult(content="c", tool_calls=[], finish_reason="stop")
    assert result.reasoning == ""
    assert result == llm.TurnResult(content="c", tool_calls=[], finish_reason="stop")


def test_the_request_payload_is_byte_identical_when_thinking_is_not_enabled() -> None:
    """The new ``enable_thinking`` knob defaults to the payload that shipped."""
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(cfg, _MESSAGES, temperature=0.8, max_tokens=None, stream=True)
    payload = json.loads(req.data.decode("utf-8"))
    assert payload == {
        "model": "m",
        "messages": _MESSAGES,
        "stream": True,
        "temperature": 0.8,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_enable_thinking_flips_exactly_one_field() -> None:
    """Opting in changes the template kwarg and nothing else on the wire."""
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    off = json.loads(
        llm._build_request(
            cfg, _MESSAGES, temperature=0.8, max_tokens=None, stream=True
        ).data.decode("utf-8")
    )
    on = json.loads(
        llm._build_request(
            cfg, _MESSAGES, temperature=0.8, max_tokens=None, stream=True, enable_thinking=True
        ).data.decode("utf-8")
    )
    assert on.pop("chat_template_kwargs") == {"enable_thinking": True}
    assert off.pop("chat_template_kwargs") == {"enable_thinking": False}
    assert on == off


def test_the_content_only_reader_never_yields_reasoning_text() -> None:
    """``stream_chat_completion`` (the classifier / sentence path) is unaffected.

    The engagement classifier and ``stream_sentences`` read content deltas only.
    A reasoning delta leaking into that iterator would put the model's private
    thinking into the robot's mouth.
    """
    script = Script(
        chunks=[
            reasoning_chunk("private thought"),
            content_chunk("public words"),
            finish(),
        ]
    )
    with FakeChatServer(script=script) as server:
        deltas = list(
            llm.stream_chat_completion(_MESSAGES, base_url=server.base_url, model="senses")
        )
    assert deltas == ["public words"]


def test_complete_turn_reads_a_non_streaming_reasoning_field() -> None:
    """The non-streaming leg fills the same field, so the two legs cannot drift."""
    body = json_body(
        {
            "choices": [
                {
                    "message": {"content": "answer", "reasoning": "because", "tool_calls": []},
                    "finish_reason": "stop",
                }
            ]
        }
    )
    with FakeChatServer(script=body) as server:
        result = llm.complete_turn(_MESSAGES, base_url=server.base_url, model="worker")
    assert (result.content, result.reasoning, result.finish_reason) == ("answer", "because", "stop")


# --------------------------------------------------------------------------- #
# The transport itself                                                        #
# --------------------------------------------------------------------------- #


def test_every_streaming_call_sends_stream_true_on_the_wire() -> None:
    """The claim is about the WIRE, so it is asserted against a received payload."""

    def scripted(payload: dict) -> Script:
        if payload.get("stream"):
            return Script(chunks=[content_chunk("ok"), finish()])
        return json_body({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})

    with FakeChatServer(script_fn=scripted) as server:
        llm.stream_turn(_MESSAGES, base_url=server.base_url, model="worker")
        llm.complete_turn(_MESSAGES, base_url=server.base_url, model="worker")

    assert server.paths == ["/v1/chat/completions", "/v1/chat/completions"]
    assert server.requests[0]["stream"] is True
    assert server.requests[1]["stream"] is False


def test_a_non_2xx_status_is_a_clean_cli_error_not_a_traceback() -> None:
    """The exit-2 environment-error contract holds against a real socket, too."""
    with FakeChatServer(script=json_body({"error": "nope"}, status=503)) as server:
        with pytest.raises(Exception) as excinfo:
            llm.stream_turn(_MESSAGES, base_url=server.base_url, model="worker")
    assert "503" in str(excinfo.value)
