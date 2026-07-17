"""Unit tests for OpenAI tool-calling in the stdlib LLM client (``reachy.speech.llm``).

These tests stub ``urllib.request.urlopen`` so no live server is needed. They
cover the two mocked acceptance criteria for task t1:

1. ``tools=`` is serialized into the request payload for both the streaming path
   and ``complete``/``complete_turn``; when ``tools`` is absent the payload is
   byte-identical to today.
2. A mocked SSE stream carrying ``tool_call`` deltas split across chunk
   boundaries assembles complete calls (name + valid parsed JSON arguments);
   content-only streams behave exactly as before.

The live gateway round-trip (finish_reason=tool_calls) lives in the sibling
``test_speech_llm_tools_integration.py`` so it can read the real environment.
"""

from __future__ import annotations

import io
import json

import pytest

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.speech import llm

_LLM_ENV_VARS = (
    "REACHY_OPENAI_URL_BASE",
    "REACHY_OPENAI_MODEL_ID",
    "REACHY_OPENAI_API_KEY",
    "REACHY_LLM_BASE_URL",
    "REACHY_LLM_MODEL",
    "REACHY_LLM_API_KEY",
)


@pytest.fixture(autouse=True)
def _isolate_llm_env(monkeypatch):
    """Clear every LLM env var so config resolution is hermetic."""
    for var in _LLM_ENV_VARS:
        monkeypatch.delenv(var, raising=False)


# ---------------------------------------------------------------------------
# SSE helpers
# ---------------------------------------------------------------------------


def _sse_line(obj: dict) -> bytes:
    """Encode one OpenAI SSE ``data:`` line carrying an arbitrary chunk object."""
    return f"data: {json.dumps(obj)}\n\n".encode("utf-8")


def _content_chunk(content: str, finish_reason=None) -> bytes:
    return _sse_line(
        {"choices": [{"index": 0, "delta": {"content": content}, "finish_reason": finish_reason}]}
    )


def _tool_chunk(index=0, *, call_id=None, name=None, arguments=None, finish_reason=None) -> bytes:
    """Encode a streaming ``tool_calls`` delta fragment (OpenAI/vLLM shape).

    Any of ``call_id`` / ``name`` / ``arguments`` may be omitted to mimic how a
    real server drips a tool call: id + name land in the first fragment, then the
    ``arguments`` string arrives split across later chunks.
    """
    fn: dict = {}
    if name is not None:
        fn["name"] = name
    if arguments is not None:
        fn["arguments"] = arguments
    tc: dict = {"index": index}
    if call_id is not None:
        tc["id"] = call_id
    if fn:
        tc["function"] = fn
    delta: dict = {"tool_calls": [tc]}
    return _sse_line({"choices": [{"index": 0, "delta": delta, "finish_reason": finish_reason}]})


_DONE = b"data: [DONE]\n\n"


class _FakeResponse:
    """Minimal stand-in for the object returned by ``urllib.request.urlopen``."""

    def __init__(self, body: bytes, status: int = 200):
        self._stream = io.BytesIO(body)
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def getcode(self):
        return self.status

    def read(self, size=-1):
        return self._stream.read(size)

    def readline(self, size=-1):
        return self._stream.readline(size)

    def readable(self):
        return True


def _stub_urlopen(monkeypatch, body: bytes, status: int = 200):
    captured: dict = {}

    def fake_urlopen(req, timeout=None):  # noqa: ANN001
        captured["req"] = req
        captured["timeout"] = timeout
        return _FakeResponse(body, status=status)

    monkeypatch.setattr(llm.urllib.request, "urlopen", fake_urlopen)
    return captured


def _non_streaming_body(*, content=None, tool_calls=None, finish_reason="stop") -> bytes:
    message: dict = {"role": "assistant", "content": content}
    if tool_calls is not None:
        message["tool_calls"] = tool_calls
    return json.dumps(
        {"choices": [{"index": 0, "message": message, "finish_reason": finish_reason}]}
    ).encode("utf-8")


# ---------------------------------------------------------------------------
# AC-1: tools serialization + byte-identical payload when absent
# ---------------------------------------------------------------------------

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


def test_build_request_without_tools_is_byte_identical():
    """No ``tools``/``tool_choice`` kwarg => payload has neither key, unchanged shape."""
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(
        cfg, [{"role": "user", "content": "hi"}], temperature=0.8, max_tokens=None, stream=True
    )
    payload = json.loads(req.data)
    assert payload == {
        "model": "m",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "temperature": 0.8,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert "tools" not in payload
    assert "tool_choice" not in payload


def test_build_request_serializes_tools_and_choice():
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(
        cfg,
        [{"role": "user", "content": "hi"}],
        temperature=0.8,
        max_tokens=None,
        stream=True,
        tools=[_APPLY_POSE_TOOL],
        tool_choice="auto",
    )
    payload = json.loads(req.data)
    assert payload["tools"] == [_APPLY_POSE_TOOL]
    assert payload["tool_choice"] == "auto"
    # enable_thinking:false must survive alongside tools (AC-3 wire requirement).
    assert payload["chat_template_kwargs"] == {"enable_thinking": False}


def test_build_request_tool_choice_absent_when_none():
    cfg = llm.LlmConfig(base_url="http://x", model="m", api_key="EMPTY")
    req = llm._build_request(
        cfg,
        [{"role": "user", "content": "hi"}],
        temperature=0.8,
        max_tokens=None,
        stream=False,
        tools=[_APPLY_POSE_TOOL],
    )
    payload = json.loads(req.data)
    assert payload["tools"] == [_APPLY_POSE_TOOL]
    assert "tool_choice" not in payload


def test_complete_serializes_tools(monkeypatch):
    body = _non_streaming_body(content="ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL], tool_choice="auto")
    sent = json.loads(captured["req"].data)
    assert sent["tools"] == [_APPLY_POSE_TOOL]
    assert sent["tool_choice"] == "auto"


def test_complete_without_tools_sends_no_tools_key(monkeypatch):
    body = _non_streaming_body(content="ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete([{"role": "user", "content": "hi"}])
    sent = json.loads(captured["req"].data)
    assert "tools" not in sent
    assert "tool_choice" not in sent


def test_stream_chat_completion_serializes_tools(monkeypatch):
    body = _content_chunk("hi", finish_reason="stop") + _DONE
    captured = _stub_urlopen(monkeypatch, body)
    list(
        llm.stream_chat_completion(
            [{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL], tool_choice="auto"
        )
    )
    sent = json.loads(captured["req"].data)
    assert sent["tools"] == [_APPLY_POSE_TOOL]
    assert sent["tool_choice"] == "auto"


def test_stream_chat_completion_without_tools_byte_identical(monkeypatch):
    """The content-only streaming payload is unchanged when tools are absent."""
    body = _content_chunk("hi", finish_reason="stop") + _DONE
    captured = _stub_urlopen(monkeypatch, body)
    list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    sent = json.loads(captured["req"].data)
    assert sent == {
        "model": "default",
        "messages": [{"role": "user", "content": "hi"}],
        "stream": True,
        "temperature": 0.8,
        "chat_template_kwargs": {"enable_thinking": False},
    }


# ---------------------------------------------------------------------------
# AC-2: streaming tool_call delta assembly across chunk boundaries
# ---------------------------------------------------------------------------


def test_stream_turn_assembles_tool_call_split_across_chunks(monkeypatch):
    """id+name in one fragment, JSON arguments dripped across two more (real shape)."""
    body = (
        _content_chunk("")  # opening role/content chunk
        + _tool_chunk(0, call_id="call_1", name="apply_pose")
        + _tool_chunk(0, arguments='{"emoji"')
        + _tool_chunk(0, arguments=': "🤔"}')  # 🤔 split away from the key
        + _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _DONE
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn(
        [{"role": "user", "content": "pat"}], tools=[_APPLY_POSE_TOOL], tool_choice="auto"
    )
    assert result.finish_reason == "tool_calls"
    assert result.content == ""
    assert len(result.tool_calls) == 1
    call = result.tool_calls[0]
    assert call.id == "call_1"
    assert call.name == "apply_pose"
    assert call.arguments == {"emoji": "\U0001f914"}  # parsed dict, not a string
    assert isinstance(call.arguments, dict)


def test_stream_turn_multiple_tool_calls_assembled_by_index(monkeypatch):
    body = (
        _tool_chunk(0, call_id="c0", name="speak", arguments='{"text"')
        + _tool_chunk(1, call_id="c1", name="apply_pose", arguments='{"emoji"')
        + _tool_chunk(0, arguments=': "hi"}')
        + _tool_chunk(1, arguments=': "🙂"}')
        + _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _DONE
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert result.finish_reason == "tool_calls"
    assert [c.name for c in result.tool_calls] == ["speak", "apply_pose"]
    assert result.tool_calls[0].arguments == {"text": "hi"}
    assert result.tool_calls[1].arguments == {"emoji": "\U0001f642"}


def test_stream_turn_content_only_matches_old_behavior(monkeypatch):
    """A content-only stream yields the same text and no tool calls."""
    seen: list[str] = []
    body = _content_chunk("Hello ") + _content_chunk("world.", finish_reason="stop") + _DONE
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], on_content=seen.append)
    assert result.content == "Hello world."
    assert result.tool_calls == []
    assert result.finish_reason == "stop"
    # The on_content callback fires per delta, in order, as they arrive.
    assert seen == ["Hello ", "world."]


def test_stream_turn_finalizes_tool_calls_on_done_without_finish_reason(monkeypatch):
    """A stream that ends at [DONE] without an explicit finish_reason still assembles."""
    body = _tool_chunk(0, call_id="c0", name="apply_pose", arguments='{"emoji": "🙂"}') + _DONE
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].arguments == {"emoji": "\U0001f642"}


def test_stream_turn_no_args_tool_call_yields_empty_dict(monkeypatch):
    body = (
        _tool_chunk(0, call_id="c0", name="wave", arguments="")
        + _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _DONE
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert result.tool_calls[0].name == "wave"
    assert result.tool_calls[0].arguments == {}


def test_stream_turn_malformed_arguments_degrade_to_empty_dict(monkeypatch):
    """Un-parseable accumulated arguments must not raise; keep the raw string."""
    body = (
        _tool_chunk(0, call_id="c0", name="apply_pose", arguments="{not json")
        + _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _DONE
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert result.tool_calls[0].arguments == {}
    assert result.tool_calls[0].arguments_json == "{not json"


def test_stream_turn_mixed_content_then_tool_call(monkeypatch):
    body = (
        _content_chunk("Let me pose. ")
        + _tool_chunk(0, call_id="c0", name="apply_pose", arguments='{"emoji": "🎉"}')
        + _sse_line({"choices": [{"index": 0, "delta": {}, "finish_reason": "tool_calls"}]})
        + _DONE
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert result.content == "Let me pose. "
    assert result.tool_calls[0].name == "apply_pose"
    assert result.tool_calls[0].arguments == {"emoji": "\U0001f389"}


# ---------------------------------------------------------------------------
# Existing content-only readers stay byte-identical (regression guard)
# ---------------------------------------------------------------------------


def test_iter_sse_deltas_content_only_unchanged(monkeypatch):
    body = _content_chunk("Hello ") + _content_chunk("world.") + _DONE
    _stub_urlopen(monkeypatch, body)
    deltas = list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert deltas == ["Hello ", "world."]


def test_stream_chat_completion_ignores_tool_call_deltas(monkeypatch):
    """The content-only reader silently skips tool_call chunks (no crash, no text)."""
    body = _tool_chunk(0, call_id="c0", name="apply_pose", arguments='{"emoji": "🙂"}') + _DONE
    _stub_urlopen(monkeypatch, body)
    deltas = list(llm.stream_chat_completion([{"role": "user", "content": "hi"}]))
    assert deltas == []


# ---------------------------------------------------------------------------
# Non-streaming complete_turn
# ---------------------------------------------------------------------------


def test_complete_turn_parses_tool_calls(monkeypatch):
    body = _non_streaming_body(
        content=None,
        tool_calls=[
            {
                "id": "chatcmpl-tool-x",
                "type": "function",
                "function": {"name": "apply_pose", "arguments": '{"emoji": "😊"}'},
            }
        ],
        finish_reason="tool_calls",
    )
    _stub_urlopen(monkeypatch, body)
    result = llm.complete_turn(
        [{"role": "user", "content": "pat"}], tools=[_APPLY_POSE_TOOL], tool_choice="auto"
    )
    assert result.finish_reason == "tool_calls"
    assert result.content == ""
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].id == "chatcmpl-tool-x"
    assert result.tool_calls[0].name == "apply_pose"
    assert result.tool_calls[0].arguments == {"emoji": "\U0001f60a"}


def test_complete_turn_content_only(monkeypatch):
    body = _non_streaming_body(content="Hello there.", finish_reason="stop")
    _stub_urlopen(monkeypatch, body)
    result = llm.complete_turn([{"role": "user", "content": "hi"}])
    assert result.content == "Hello there."
    assert result.tool_calls == []
    assert result.finish_reason == "stop"


def test_complete_turn_sends_stream_false(monkeypatch):
    body = _non_streaming_body(content="ok")
    captured = _stub_urlopen(monkeypatch, body)
    llm.complete_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    sent = json.loads(captured["req"].data)
    assert sent["stream"] is False
    assert sent["tools"] == [_APPLY_POSE_TOOL]
    assert sent["chat_template_kwargs"] == {"enable_thinking": False}


# ---------------------------------------------------------------------------
# Error contract (CliError code 2, no tracebacks) for the new tool paths
# ---------------------------------------------------------------------------


def test_stream_turn_unreachable_raises_clierror(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(CliError) as ei:
        llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert ei.value.code == EXIT_ENV_ERROR
    assert ei.value.remediation


def test_stream_turn_non_200_raises_clierror(monkeypatch):
    _stub_urlopen(monkeypatch, b"upstream boom", status=500)
    with pytest.raises(CliError) as ei:
        llm.stream_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert ei.value.code == EXIT_ENV_ERROR


def test_complete_turn_unreachable_raises_clierror(monkeypatch):
    import urllib.error

    def boom(req, timeout=None):  # noqa: ANN001
        raise urllib.error.URLError("Connection refused")

    monkeypatch.setattr(llm.urllib.request, "urlopen", boom)
    with pytest.raises(CliError) as ei:
        llm.complete_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert ei.value.code == EXIT_ENV_ERROR
    assert ei.value.remediation


def test_complete_turn_non_200_raises_clierror(monkeypatch):
    _stub_urlopen(monkeypatch, b"upstream boom", status=500)
    with pytest.raises(CliError) as ei:
        llm.complete_turn([{"role": "user", "content": "hi"}], tools=[_APPLY_POSE_TOOL])
    assert ei.value.code == EXIT_ENV_ERROR
