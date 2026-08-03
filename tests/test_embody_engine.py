"""The embodiment layer's streaming cognition loop (task t10, claim c6 / honesty h6).

Three acceptance criteria, and every one of them is a claim about something
observable rather than about code shape:

1. **Every LLM call streams.** Asserted against a real loopback socket
   (``tests/fake_sse_server.py``): the received payload says ``stream: true``,
   and the deltas are proven to be consumed INCREMENTALLY by making the server
   refuse to write chunk 2 until the client has surfaced chunk 1 — a client
   that buffered the body whole would deadlock its own proof. A stalled stream
   resolves as a named timeout drop and the loop keeps running.
2. **The model is a per-request field**, resolved from process-scoped
   environment only: no ``environment.d`` read, no global mutation.
3. **Tool results and refusals flow back into the conversation**, and the
   ``thinking`` / ``message`` / ``emotion`` export contract is emitted per turn.

The inter-chunk bound is its own test class
--------------------------------------------
h6 says a stalled stream must resolve as a named drop "never a hang". The
subtlety that makes it non-obvious is measured, not guessed: streaming turns a
total deadline into an inter-chunk IDLE bound (the first content delta took
43.2 s on our own gateway while the largest gap between chunks was 0.124 s —
``docs/evidence/2026-08-01-cited-findings-from-embodiment-sibling.md``). So a
drop armed on total elapsed would kill every long think as if it were a stall.
``test_the_stall_bound_is_inter_chunk_idle_not_total_elapsed`` is the test that
fails if anyone rearms it on total elapsed.
"""

from __future__ import annotations

import ast
import copy
import dataclasses
import inspect
import json
import logging
import os
from pathlib import Path

import pytest

from reachy.behavior.goto_intent import GOTO
from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.embody import engine as engine_mod
from reachy.embody.cues import CueClass
from reachy.embody.engine import (
    DROP_REASONS,
    ENV_ATTENTION_WINDOW_S,
    ENV_SENSES_MODEL,
    ENV_WORKER_MODEL,
    REASON_EMPTY_INPUT,
    REASON_ENDPOINT_UNREACHABLE,
    REASON_INPUT_QUEUE_FULL,
    REASON_STREAM_IDLE,
    REASON_SUMMARY_STALE,
    REASON_SUMMARY_TOO_LONG,
    REASON_TOOL_ROUNDS_EXHAUSTED,
    ROLE_SENSES,
    ROLE_WORKER,
    STALE_SUMMARY_MARKER,
    EmbodyModels,
    EmbodyTurnEngine,
    first_emoji,
    resolve_attention_window_s,
)
from reachy.embody.interjection import (
    DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS,
    REFUSAL_WANTED_TO_SAY_TOO_LONG,
)
from reachy.embody.tools import CREATE_RULE, REFUSAL_RULE_NAMESPACE, SPEAK, EmbodyToolRegistry
from reachy.export.exporter import ExportHook
from reachy.speech.llm import ToolCall, TurnResult
from tests.conftest import WAIT_BUDGET_S
from tests.fake_sse_server import (
    FakeChatServer,
    Script,
    content_chunk,
    finish,
    reasoning_chunk,
    role_chunk,
    tool_call_chunk,
)

# --------------------------------------------------------------------------- #
# Doubles                                                                     #
# --------------------------------------------------------------------------- #


class ScriptedTurn:
    """A ``turn_fn`` double returning canned turns and recording every call.

    The messages list is DEEP-COPIED at call time: the engine appends tool
    results to the same list across rounds, so a shallow record would show every
    round the final conversation and quietly make the "results flow back"
    assertion vacuous.
    """

    def __init__(self, *results: TurnResult) -> None:
        self._results = list(results)
        self.calls: list[dict] = []

    def __call__(self, messages: list[dict], **kwargs) -> TurnResult:
        self.calls.append({"messages": copy.deepcopy(messages), "kwargs": kwargs})
        if not self._results:
            return TurnResult(content="", tool_calls=[], finish_reason="stop")
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    @property
    def models(self) -> list[str | None]:
        return [call["kwargs"].get("model") for call in self.calls]

    def last_messages(self) -> list[dict]:
        return self.calls[-1]["messages"]


class RecordingRegistry:
    """An :class:`~reachy.embody.tools.EmbodyToolRegistry`-shaped double."""

    def __init__(self, result: dict | None = None) -> None:
        self.dispatched: list[tuple[str, str | None, str | None]] = []
        self._result = result

    def tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": SPEAK, "parameters": {}}}]

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        self.dispatched.append((name, arguments_json, tool_call_id))
        content = json.dumps(self._result if self._result is not None else {"ok": True})
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class Sink:
    """An export sink recording every emitted block."""

    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)

    def hook(self, *, poses: dict | None = None) -> ExportHook:
        table = poses if poses is not None else {"🤔": {"head_pitch": -5.0}}
        return ExportHook(
            emit=self.emit,
            pose_resolver=table.get,
            time_fn=lambda: 1234.5,
        )

    def of_type(self, block: str) -> list[object]:
        return [event for event in self.events if getattr(event, "t", None) == block]


def _speak_call(text: str, call_id: str = "call_1") -> ToolCall:
    payload = json.dumps({"text": text})
    return ToolCall(id=call_id, name=SPEAK, arguments={"text": text}, arguments_json=payload)


#: Every :class:`~reachy.embody.engine.Limits` / :class:`~reachy.embody.engine.
#: RequestConfig` field name (issue #141/S107) — used to translate the flat,
#: per-field kwargs most tests already pass (e.g. ``max_pending=2``,
#: ``base_url=...``) into the ``limits=`` / ``request=`` keywords the
#: constructor takes now, so the individual test bodies below did not have to
#: change.
_LIMIT_FIELDS = {field.name for field in dataclasses.fields(engine_mod.Limits)}
_REQUEST_FIELDS = {field.name for field in dataclasses.fields(engine_mod.RequestConfig)}


def _build(**kwargs) -> EmbodyTurnEngine:
    """An engine with every collaborator faked unless the test says otherwise.

    Passing ``base_url=`` means the test wants the REAL streaming client against
    a loopback server, so no ``turn_fn`` double is substituted in that case.

    The engine comes back with its ATTENTION WINDOW ALREADY OPEN (issue #148).
    The shipped gate starts cold — only an utterance naming the robot wakes a
    turn — but every test in this module is about streaming, model selection,
    the tool loop or the export contract and says whatever it likes to the
    engine. ``tests/test_embody_attention.py`` owns attention; opening the
    window once here beats sprinkling the robot's name through forty unrelated
    assertions and pretending they were about being addressed.
    """
    kwargs.setdefault("registry", RecordingRegistry())
    if "base_url" not in kwargs:
        kwargs.setdefault("turn_fn", ScriptedTurn(TurnResult(content="ok", finish_reason="stop")))
    kwargs.setdefault("models", EmbodyModels(worker="worker", senses="senses"))
    limit_kwargs = {name: kwargs.pop(name) for name in list(kwargs) if name in _LIMIT_FIELDS}
    if limit_kwargs:
        kwargs.setdefault("limits", engine_mod.Limits(**limit_kwargs))
    request_kwargs = {name: kwargs.pop(name) for name in list(kwargs) if name in _REQUEST_FIELDS}
    if request_kwargs:
        kwargs.setdefault("request", engine_mod.RequestConfig(**request_kwargs))
    engine = EmbodyTurnEngine(**kwargs)
    engine.attention.note_addressed()
    return engine


# =========================================================================== #
# AC 1 — every LLM call streams                                               #
# =========================================================================== #


def test_a_turn_streams_and_the_wire_carries_stream_true_and_the_tools() -> None:
    """The claim is about the wire, so it is asserted against a received payload."""
    script = Script(chunks=[role_chunk(), content_chunk("hello"), finish()])
    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url)
        engine.submit_utterance("hi there")
        assert engine.run_turn() is True

    assert server.requests[0]["stream"] is True
    assert server.requests[0]["model"] == "worker"
    assert [tool["function"]["name"] for tool in server.requests[0]["tools"]] == [SPEAK]


def test_the_senses_lane_streams_too() -> None:
    """``ask`` is the second LLM call the layer makes, and it streams as well."""
    script = Script(chunks=[content_chunk("a cat"), finish()])
    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url)
        answer = engine.ask("what do you see?")

    assert answer == "a cat"
    assert server.requests[0]["stream"] is True
    assert server.requests[0]["model"] == "senses"
    assert "tools" not in server.requests[0]


def test_deltas_are_consumed_incrementally_never_buffered_whole() -> None:
    """The server withholds chunk 2 until the client has surfaced chunk 1.

    A client that read the whole body before yielding anything would never set
    the gate, the server would fall through on its own bounded wait, and
    ``observed`` would be ``False``.
    """
    import threading

    surfaced = threading.Event()
    observed: list[bool] = []

    def gate(index: int) -> None:
        if index == 2:  # about to write the SECOND content delta
            observed.append(surfaced.wait(WAIT_BUDGET_S))

    script = Script(
        chunks=[role_chunk(), content_chunk("first"), content_chunk(" second"), finish()],
        on_chunk=gate,
    )
    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url, on_content=lambda _text: surfaced.set())
        engine.submit_utterance("go")
        engine.run_turn()

    assert observed == [True]


def test_a_stalled_stream_is_a_named_timeout_drop(caplog) -> None:
    """h6: a stream that goes quiet mid-flight is named, never a hang."""
    script = Script(
        chunks=[content_chunk("a"), content_chunk("b"), finish()],
        stall_after=2,
        stall_timeout_s=WAIT_BUDGET_S,
    )
    with caplog.at_level("INFO", logger="reachy.sense"):
        with FakeChatServer(script=script) as server:
            engine = _build(base_url=server.base_url, idle_timeout_s=0.5)
            engine.submit_utterance("say something")
            assert engine.run_turn() is True

    assert engine.stream_timeouts == 1
    assert REASON_STREAM_IDLE in caplog.text


def test_the_loop_survives_a_stalled_stream_and_runs_the_next_turn() -> None:
    """ "...and the loop continues" — the second half of the acceptance line."""

    def scripted(payload: dict) -> Script:
        if len(payload["messages"]) <= 2:  # the first turn: system + one perception
            return Script(chunks=[content_chunk("a"), finish()], stall_after=1, stall_timeout_s=1.0)
        return Script(chunks=[content_chunk("recovered"), finish()])

    with FakeChatServer(script_fn=scripted) as server:
        engine = _build(base_url=server.base_url, idle_timeout_s=0.5)
        engine.submit_utterance("first")
        engine.run_turn()
        engine.submit_utterance("second")
        engine.run_turn()

    assert engine.turns == 2
    assert engine.stream_timeouts == 1
    assert engine.last_text == "recovered"


def test_the_stall_bound_is_inter_chunk_idle_not_total_elapsed() -> None:
    """A long think is not a stall: many chunks, each within the bound, total well over it.

    The measured shape this pins (43.2 s to first content, 0.124 s largest
    inter-chunk gap) is why the bound must be armed per-read. Armed on total
    elapsed, this test fails — which is the point.
    """
    idle = 0.5
    chunks = [content_chunk(str(i)) for i in range(8)] + [finish()]
    script = Script(chunks=chunks, chunk_delay_s=idle * 0.4)

    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url, idle_timeout_s=idle)
        engine.submit_utterance("think hard")
        engine.run_turn()

    # 9 chunks x 0.2 s = ~1.8 s of wall clock against a 0.5 s bound.
    assert engine.stream_timeouts == 0
    assert engine.last_text == "01234567"


def test_a_streamed_tool_call_is_dispatched_and_its_result_returns_over_the_wire() -> None:
    """End to end over a socket: assembled from deltas, dispatched, fed back, answered.

    The other context tests inject a ready-made ``ToolCall``; this one makes the
    real streaming assembler produce it, so the two ends of the turn loop are
    connected once rather than each tested against its own idea of the shape.
    """

    def scripted(payload: dict) -> Script:
        if any(message.get("role") == "tool" for message in payload["messages"]):
            return Script(chunks=[content_chunk("said it"), finish()])
        return Script(
            chunks=[
                tool_call_chunk(index=0, call_id="call_7", name=SPEAK),
                tool_call_chunk(index=0, arguments='{"text": "hello"}'),
                finish("tool_calls"),
            ]
        )

    registry = RecordingRegistry()
    with FakeChatServer(script_fn=scripted) as server:
        engine = _build(base_url=server.base_url, registry=registry)
        engine.submit_utterance("say hello")
        engine.run_turn()

    assert registry.dispatched == [(SPEAK, '{"text": "hello"}', "call_7")]
    assert server.requests[1]["messages"][-1]["role"] == "tool"
    assert engine.last_text == "said it"


def test_an_unreachable_endpoint_is_a_named_drop_not_a_raise(caplog) -> None:
    """A dead gateway must not raise into the layer's loop."""
    with caplog.at_level("INFO", logger="reachy.sense"):
        engine = _build(base_url="http://127.0.0.1:1")
        engine.submit_utterance("anyone home?")
        assert engine.run_turn() is True

    assert engine.stream_failures == 1
    assert REASON_ENDPOINT_UNREACHABLE in caplog.text


def test_the_streamed_reasoning_reaches_the_engine() -> None:
    """The gateway's ``delta.reasoning`` is what fills the thinking feed."""
    script = Script(
        chunks=[reasoning_chunk("Here"), reasoning_chunk(" we go"), content_chunk("hi"), finish()]
    )
    sink = Sink()
    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url, export=sink.hook())
        engine.submit_utterance("hello")
        engine.run_turn()

    thinking = sink.of_type("thinking")
    assert len(thinking) == 1
    assert "Here we go" in thinking[0].text


# =========================================================================== #
# AC 2 — the model is a per-request field from process-scoped env only        #
# =========================================================================== #


def test_the_turn_uses_the_worker_model_and_ask_uses_the_senses_model() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.submit_cue("a behavior rule fired (pat-acknowledge)", cue_class=CueClass.ALERT)
    engine.run_turn()
    engine.ask("describe the clip")

    assert turn.models == ["worker", "senses"]


def test_the_role_names_are_the_shipped_defaults() -> None:
    """lobes' ``resolve_model`` accepts ROLE names, so the default IS the role."""
    models = EmbodyModels.resolve(env={})
    assert (models.worker, models.senses) == (ROLE_WORKER, ROLE_SENSES)
    assert models.model_for(ROLE_WORKER) == ROLE_WORKER
    assert models.model_for(ROLE_SENSES) == ROLE_SENSES


def test_models_resolve_from_process_env_only() -> None:
    models = EmbodyModels.resolve(
        env={ENV_WORKER_MODEL: "qwen-on-thor", ENV_SENSES_MODEL: "gemma-on-orin"}
    )
    assert (models.worker, models.senses) == ("qwen-on-thor", "gemma-on-orin")


def test_an_explicit_argument_wins_over_the_environment() -> None:
    models = EmbodyModels.resolve(env={ENV_WORKER_MODEL: "from-env"}, worker="explicit")
    assert models.worker == "explicit"


def test_resolving_and_running_never_mutate_the_environment(monkeypatch) -> None:
    """No global mutation: ``environment.d`` would re-point the runtime classifier too."""
    monkeypatch.setenv(ENV_WORKER_MODEL, "qwen-on-thor")
    before = dict(os.environ)

    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    # The one engine in this module built WITHOUT ``_build`` (the point is the
    # bare, default-configured engine), so its attention gate is genuinely cold
    # and the utterance has to name the robot to reach a model at all (#148).
    engine = EmbodyTurnEngine(registry=RecordingRegistry(), turn_fn=turn)
    engine.submit_utterance("reachy, hello")
    engine.run_turn()
    engine.ask("and?")

    assert dict(os.environ) == before
    assert turn.models == ["qwen-on-thor", ROLE_SENSES]


# =========================================================================== #
# issue #150 — the attention window is an operator knob, resolved the same   #
# way the model names are: explicit argument, then process env, then default #
# =========================================================================== #


def test_attention_window_default_matches_the_documented_constant() -> None:
    assert resolve_attention_window_s(env={}) == engine_mod.DEFAULT_ATTENTION_WINDOW_S


def test_attention_window_resolves_from_process_env_only() -> None:
    resolved = resolve_attention_window_s(env={ENV_ATTENTION_WINDOW_S: "10"})
    assert resolved == 10.0


def test_an_explicit_attention_window_wins_over_the_environment() -> None:
    resolved = resolve_attention_window_s(5.0, env={ENV_ATTENTION_WINDOW_S: "10"})
    assert resolved == 5.0


def test_an_explicit_zero_attention_window_is_not_treated_as_unset() -> None:
    """The falsy-zero hazard: an ``or`` chain would read 0.0 as "not given".

    Unlike :meth:`EmbodyModels.resolve`'s string fields (where an empty
    string and "unset" are the same thing), ``0`` is a legitimate,
    meaningfully different value here — name-only-forever, the same
    convention ``Limits.min_alert_interval_s`` already uses for ``0``. This
    pins that :func:`resolve_attention_window_s` checks *explicit* by
    identity against ``None``, not by truthiness.
    """
    resolved = resolve_attention_window_s(0.0, env={ENV_ATTENTION_WINDOW_S: "10"})
    assert resolved == 0.0


def test_attention_window_env_value_of_zero_is_also_honoured() -> None:
    resolved = resolve_attention_window_s(env={ENV_ATTENTION_WINDOW_S: "0"})
    assert resolved == 0.0


def test_a_non_numeric_attention_window_env_value_degrades_to_the_default(caplog) -> None:
    """Mirrors ``reachy.embody.media._env_int``: never raise, log and fall back."""
    with caplog.at_level("WARNING"):
        resolved = resolve_attention_window_s(env={ENV_ATTENTION_WINDOW_S: "not-a-number"})
    assert resolved == engine_mod.DEFAULT_ATTENTION_WINDOW_S
    assert "not-a-number" in caplog.text


def test_resolving_the_attention_window_never_mutates_the_environment(monkeypatch) -> None:
    """Same guarantee as the model resolution: no global mutation, no file I/O."""
    monkeypatch.setenv(ENV_ATTENTION_WINDOW_S, "12")
    before = dict(os.environ)

    resolved = resolve_attention_window_s()

    assert dict(os.environ) == before
    assert resolved == 12.0


def test_the_engine_reads_no_file_and_writes_no_environment_variable() -> None:
    """AST proof: process-scoped env only — no ``environment.d``, no ``os.environ[...] =``."""
    source = Path(engine_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)

    # ``environment.d`` may appear in prose (the docstring explains WHY it is
    # not read); it must never appear in a path the code actually uses.
    docstrings = _docstring_ids(tree)
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if id(node) not in docstrings:
                assert "environment" not in node.value.lower() or node.value.startswith("REACHY_")

    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            name = node.func.id if isinstance(node.func, ast.Name) else ""
            attr = node.func.attr if isinstance(node.func, ast.Attribute) else ""
            assert name != "open", "the layer's model config must not read a file"
            assert attr not in {"read_text", "read_bytes", "putenv", "setenv"}, attr
        if isinstance(node, (ast.Assign, ast.AugAssign)):
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            for target in targets:
                assert not _is_environ_write(target), "the model must never be set globally"


def _docstring_ids(tree: ast.AST) -> set[int]:
    """Object ids of every docstring node, so prose is exempt from the literal scan."""
    ids: set[int] = set()
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)):
            continue
        body = getattr(node, "body", [])
        if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
            ids.add(id(body[0].value))
    return ids


def _is_environ_write(target: ast.expr) -> bool:
    return (
        isinstance(target, ast.Subscript)
        and isinstance(target.value, ast.Attribute)
        and target.value.attr == "environ"
    )


# =========================================================================== #
# AC 3 — results + refusals in context; the export contract per turn          #
# =========================================================================== #


def test_a_tool_result_is_appended_to_the_conversation_the_model_sees_next() -> None:
    turn = ScriptedTurn(
        TurnResult(content="", tool_calls=[_speak_call("hello")], finish_reason="tool_calls"),
        TurnResult(content="done", finish_reason="stop"),
    )
    registry = RecordingRegistry({"ok": True, "voice": SPEAK})
    engine = _build(registry=registry, turn_fn=turn)
    engine.submit_utterance("say hello")
    engine.run_turn()

    second_round = turn.calls[1]["messages"]
    assert second_round[-2]["role"] == "assistant"
    assert second_round[-1] == {
        "role": "tool",
        "tool_call_id": "call_1",
        "content": json.dumps({"ok": True, "voice": SPEAK}),
    }


def test_a_named_refusal_reaches_the_model_verbatim(tmp_path) -> None:
    """The real registry's refusal — name and validator words — enters the context."""
    bad_rule = ToolCall(
        id="call_9",
        name=CREATE_RULE,
        arguments={"id": "not-namespaced"},
        arguments_json=json.dumps({"id": "not-namespaced", "when": {}, "run": "nod"}),
    )
    turn = ScriptedTurn(
        TurnResult(content="", tool_calls=[bad_rule], finish_reason="tool_calls"),
        TurnResult(content="understood", finish_reason="stop"),
    )
    registry = EmbodyToolRegistry(rules_path=tmp_path / "rules.toml", reload_seam=lambda _t: None)
    engine = _build(registry=registry, turn_fn=turn)
    engine.submit_utterance("make a rule")
    engine.run_turn()

    tool_message = turn.calls[1]["messages"][-1]
    payload = json.loads(tool_message["content"])
    assert payload["ok"] is False
    assert payload["refusal"] == REFUSAL_RULE_NAMESPACE
    assert engine.refusals == 1


def test_every_turn_emits_one_thinking_block_carrying_its_cues() -> None:
    """Triggers first, then whatever context the turn drained (issue #143)."""
    sink = Sink()
    turn = ScriptedTurn(TurnResult(content="quiet", finish_reason="stop"))
    engine = _build(turn_fn=turn, export=sink.hook())
    engine.submit_cue("felt a gentle scratch on the head")
    engine.submit_utterance("who's there?")
    engine.run_turn()

    thinking = sink.of_type("thinking")
    assert len(thinking) == 1
    assert thinking[0].cues == ['heard: "who\'s there?"', "felt a gentle scratch on the head"]
    assert "quiet" in thinking[0].text
    assert thinking[0].ts == 1234.5


def test_a_voice_tool_call_emits_a_message_block() -> None:
    sink = Sink()
    turn = ScriptedTurn(
        TurnResult(content="", tool_calls=[_speak_call("hello there")], finish_reason="tool_calls"),
        TurnResult(content="", finish_reason="stop"),
    )
    engine = _build(turn_fn=turn, export=sink.hook())
    engine.submit_utterance("hi")
    engine.run_turn()

    messages = sink.of_type("message")
    assert [event.text for event in messages] == ["hello there"]


def test_an_emoji_in_the_reply_emits_an_emotion_block_with_its_pose() -> None:
    sink = Sink()
    turn = ScriptedTurn(TurnResult(content="🤔 let me think", finish_reason="stop"))
    engine = _build(turn_fn=turn, export=sink.hook())
    engine.submit_utterance("hard question")
    engine.run_turn()

    emotions = sink.of_type("emotion")
    assert [(event.emoji, event.pose) for event in emotions] == [("🤔", {"head_pitch": -5.0})]


def test_an_unknown_emoji_exports_a_null_pose() -> None:
    """The schema requires ``pose: null`` for an emoji the catalog does not know."""
    sink = Sink()
    turn = ScriptedTurn(TurnResult(content="🦄 unusual", finish_reason="stop"))
    engine = _build(turn_fn=turn, export=sink.hook())
    engine.submit_utterance("?")
    engine.run_turn()

    assert [(e.emoji, e.pose) for e in sink.of_type("emotion")] == [("🦄", None)]


def test_first_emoji_ignores_ordinary_text() -> None:
    assert first_emoji("no expression here, just words. 1234 :-)") is None
    assert first_emoji("plain 🎉 party") == "🎉"


def test_a_refusal_is_visible_in_the_exported_thinking_text(tmp_path) -> None:
    """The spec's red-team line: every refusal visible in the export feed."""
    sink = Sink()
    bad_rule = ToolCall(
        id="call_9",
        name=CREATE_RULE,
        arguments={"id": "nope"},
        arguments_json=json.dumps({"id": "nope", "when": {}, "run": "nod"}),
    )
    turn = ScriptedTurn(
        TurnResult(content="", tool_calls=[bad_rule], finish_reason="tool_calls"),
        TurnResult(content="ok", finish_reason="stop"),
    )
    registry = EmbodyToolRegistry(rules_path=tmp_path / "rules.toml", reload_seam=lambda _t: None)
    engine = _build(registry=registry, turn_fn=turn, export=sink.hook())
    engine.submit_utterance("make a rule")
    engine.run_turn()

    text = sink.of_type("thinking")[0].text
    assert CREATE_RULE in text
    assert REFUSAL_RULE_NAMESPACE in text


def test_a_failed_turn_still_reaches_the_export_feed_by_name() -> None:
    """No silent no-op: a drop names itself on the feed as well as in the journal."""
    sink = Sink()
    engine = _build(base_url="http://127.0.0.1:1", export=sink.hook())
    engine.submit_utterance("hello?")
    engine.run_turn()

    assert REASON_ENDPOINT_UNREACHABLE in sink.of_type("thinking")[0].text


# =========================================================================== #
# The loop itself                                                             #
# =========================================================================== #


def test_no_input_means_no_llm_call() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    assert engine.run_turn() is False
    assert turn.calls == []


def test_cues_and_utterances_are_both_rendered_into_the_turn() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.submit_cue("a behavior rule fired (pat-acknowledge): now doing nod")
    engine.submit_utterance("are you awake?")
    engine.run_turn()

    user = turn.last_messages()[-1]
    assert user["role"] == "user"
    assert "pat-acknowledge" in user["content"]
    assert "are you awake?" in user["content"]


def test_input_is_consumed_by_the_turn_that_ran() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.submit_utterance("something happened")
    engine.run_turn()
    assert engine.pending == 0
    assert engine.run_turn() is False


def test_the_input_buffer_is_bounded_and_names_its_drop(caplog) -> None:
    """The TRIGGER buffer's bound; the context park keeps its own (#143)."""
    engine = _build(max_pending=2)
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.submit_utterance("one") is True
        assert engine.submit_utterance("two") is True
        assert engine.submit_utterance("three") is False

    assert engine.dropped_inputs == 1
    assert REASON_INPUT_QUEUE_FULL in caplog.text
    assert engine.pending == 2


def test_an_empty_submission_is_a_named_drop(caplog) -> None:
    engine = _build()
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.submit_utterance("   ") is False
    assert REASON_EMPTY_INPUT in caplog.text
    assert engine.pending == 0


def test_the_tool_loop_is_bounded(caplog) -> None:
    """A model that never stops calling tools cannot spin forever."""
    forever = TurnResult(content="", tool_calls=[_speak_call("again")], finish_reason="tool_calls")
    turn = ScriptedTurn(forever)
    engine = _build(turn_fn=turn, max_tool_rounds=3)
    engine.submit_utterance("go")
    with caplog.at_level("INFO", logger="reachy.sense"):
        engine.run_turn()

    assert len(turn.calls) == 3
    assert REASON_TOOL_ROUNDS_EXHAUSTED in caplog.text


def test_the_registry_tools_are_published_on_every_round() -> None:
    """Never a no-tools round: lobes-cli#161 makes a tool call on such a round lossy."""
    turn = ScriptedTurn(
        TurnResult(content="", tool_calls=[_speak_call("hi")], finish_reason="tool_calls"),
        TurnResult(content="done", finish_reason="stop"),
    )
    engine = _build(turn_fn=turn)
    engine.submit_utterance("go")
    engine.run_turn()

    assert all(call["kwargs"].get("tools") for call in turn.calls)


def test_what_the_mouth_already_said_enters_the_next_turn_without_triggering_one() -> None:
    """The realtime session speaks on its own; the thinking mind must know what it said."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.note_spoken("I'm right here.")
    assert engine.pending == 0
    assert engine.run_turn() is False

    engine.submit_utterance("hello?")
    engine.run_turn()
    assert "I'm right here." in turn.last_messages()[-1]["content"]


def test_a_spoken_line_is_carried_into_one_turn_and_then_forgotten() -> None:
    """Drained by the turn that showed it — otherwise every later turn re-reads it."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.note_spoken("already said this")
    engine.submit_utterance("one")
    engine.run_turn()
    engine.submit_utterance("two")
    engine.run_turn()

    assert "already said this" in turn.calls[0]["messages"][-1]["content"]
    assert "already said this" not in turn.calls[1]["messages"][-1]["content"]


def test_a_spoken_reply_is_exported_as_a_message_block() -> None:
    sink = Sink()
    engine = _build(export=sink.hook())
    engine.note_spoken("hello from the realtime session")
    assert [event.text for event in sink.of_type("message")] == ["hello from the realtime session"]


def test_history_is_bounded() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, history_maxlen=2, senses_history_maxlen=2)
    for index in range(5):
        engine.submit_utterance(f"turn {index}")
        engine.run_turn()

    # system + 2 history pairs + the current perception
    assert len(turn.last_messages()) == 1 + 2 * 2 + 1


def test_run_stops_on_the_stop_predicate() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, sleep=lambda _s: None)
    calls = {"n": 0}

    def stop() -> bool:
        calls["n"] += 1
        return calls["n"] > 3

    def before_turn() -> None:
        engine.submit_utterance("tick")

    ran = engine.run(stop=stop, before_turn=before_turn)
    assert ran == 3


def test_run_bounded_by_max_turns() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, sleep=lambda _s: None)
    ran = engine.run(max_turns=2, before_turn=lambda: engine.submit_utterance("tick"))
    assert ran == 2


def test_the_cancel_seam_is_handed_to_the_stream() -> None:
    """A closing layer aborts an in-flight stream rather than waiting it out."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, cancel=lambda: True)
    engine.submit_utterance("tick")
    engine.run_turn()
    assert callable(turn.calls[0]["kwargs"]["cancel"])
    assert turn.calls[0]["kwargs"]["cancel"]() is True


def test_submit_cues_takes_what_the_cue_mapper_returns() -> None:
    """The composition root hands ``classified_cues_for_line``'s list straight in.

    Routing by class is ``tests/test_embody_input_policy.py``'s subject; what
    this pins is the shape — a blank cue is still refused, and a caller with no
    classification to give still gets its cues taken.
    """
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    assert engine.submit_cues(["a rule fired", "", "felt a gentle scratch"]) == 2
    assert engine.parked == 2


def test_noting_an_empty_reply_is_a_no_op() -> None:
    sink = Sink()
    engine = _build(export=sink.hook())
    engine.note_spoken("   ")
    assert sink.events == []


def test_a_turn_that_produces_nothing_at_all_is_named(caplog) -> None:
    """No text, no reasoning, no tool call — a silence the journal can explain."""
    turn = ScriptedTurn(TurnResult(content="", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.submit_utterance("what can you see?")
    with caplog.at_level("INFO", logger="reachy.sense"):
        engine.run_turn()
    assert "silent-turn" in caplog.text


def test_a_raising_turn_function_is_a_named_drop_not_a_crash(caplog) -> None:
    """Whatever goes wrong in a turn, the layer is still there for the next one."""

    def explode(_messages, **_kwargs):
        raise ValueError("model client blew up")

    engine = _build(turn_fn=explode)
    engine.submit_utterance("hello")
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.run_turn() is True
    assert engine.stream_failures == 1
    assert "stream-failed" in caplog.text


def test_a_reset_connection_is_its_own_named_drop(caplog) -> None:
    """A socket reset is not an idle stall and must not be counted as one."""

    def reset(_messages, **_kwargs):
        raise ConnectionResetError("peer went away")

    engine = _build(turn_fn=reset)
    engine.submit_utterance("hello")
    with caplog.at_level("INFO", logger="reachy.sense"):
        engine.run_turn()
    assert engine.stream_timeouts == 0
    assert engine.stream_failures == 1
    assert "stream-failed" in caplog.text


def test_a_modifier_is_never_mistaken_for_the_expression() -> None:
    """A variation selector / ZWJ is a modifier; the emoji after it is the face."""
    assert first_emoji("️\U0001f642 hello") == "🙂"


def test_max_tokens_is_forwarded_only_when_set() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, max_tokens=256)
    engine.submit_utterance("tick")
    engine.run_turn()
    assert turn.calls[0]["kwargs"]["max_tokens"] == 256

    plain = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    other = _build(turn_fn=plain)
    other.submit_utterance("tick")
    other.run_turn()
    assert "max_tokens" not in plain.calls[0]["kwargs"]


def test_run_without_a_producer_stops_rather_than_spinning() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, sleep=lambda _s: None)
    engine.submit_utterance("only one")
    assert engine.run(max_turns=5) == 1


def test_every_drop_reason_is_exported_and_unique() -> None:
    """One vocabulary shared by the journal, the export feed and these tests."""
    assert REASON_STREAM_IDLE in DROP_REASONS
    assert len(DROP_REASONS) == len(set(DROP_REASONS))
    for reason in DROP_REASONS:
        assert reason == reason.lower().strip()
        assert " " not in reason


@pytest.mark.parametrize("role", [ROLE_WORKER, ROLE_SENSES])
def test_an_unknown_role_is_refused_rather_than_guessed(role: str) -> None:
    models = EmbodyModels.resolve(env={})
    assert models.model_for(role)
    with pytest.raises(ValueError):
        models.model_for("cortex")


def test_the_goto_tool_name_is_imported_not_retyped() -> None:
    """A drift canary: the engine's voice-tool set names the shipped constants."""
    assert GOTO not in engine_mod.DEFAULT_VOICE_TOOLS
    assert SPEAK in engine_mod.DEFAULT_VOICE_TOOLS


# =========================================================================== #
# issue #141/S107 — bounds/request config live in frozen dataclasses,         #
# seams stay explicit                                                        #
# =========================================================================== #
#
# Sonar's CONFIGURED threshold for this project (queried live against
# SonarCloud, not assumed from the rule's language-wide default of 7) is
# 13 authorized parameters ("Method __init__ has 21 parameters, which is
# greater than the 13 authorized"). Moving only the 8 fields the issue names
# by example (max_tool_rounds, history_maxlen, max_pending, spoken_maxlen and
# the several timeouts) into Limits leaves this constructor at 17 — still
# over. :class:`~reachy.embody.engine.RequestConfig` is the second grouping
# that closes the remaining gap: not a seam (no field is callable) and not a
# resource/time bound either, but equally not an injected collaborator, so it
# belongs on the "grouped" side of the seam/non-seam line the issue draws.


def test_limits_defaults_match_the_documented_module_constants() -> None:
    """The refactor must not change a single default — only where it lives."""
    limits = engine_mod.Limits()
    assert limits.idle_timeout_s == engine_mod.DEFAULT_IDLE_TIMEOUT_S
    assert limits.max_tool_rounds == engine_mod.DEFAULT_MAX_TOOL_ROUNDS
    assert limits.history_maxlen == engine_mod.DEFAULT_HISTORY_MAXLEN
    assert limits.senses_history_maxlen == engine_mod.DEFAULT_SENSES_HISTORY_MAXLEN
    assert limits.summary_max_chars == engine_mod.DEFAULT_SUMMARY_MAX_CHARS
    assert limits.max_pending == engine_mod.DEFAULT_MAX_PENDING
    assert limits.max_context == engine_mod.DEFAULT_MAX_CONTEXT
    assert limits.min_alert_interval_s == engine_mod.DEFAULT_MIN_ALERT_INTERVAL_S
    assert limits.spoken_maxlen == engine_mod.DEFAULT_SPOKEN_MAXLEN
    assert limits.turn_interval == engine_mod.DEFAULT_TURN_INTERVAL
    assert limits.attention_window_s == engine_mod.DEFAULT_ATTENTION_WINDOW_S


def test_limits_is_frozen() -> None:
    limits = engine_mod.Limits()
    with pytest.raises(dataclasses.FrozenInstanceError):
        limits.max_pending = 1  # type: ignore[misc]


def test_request_config_defaults_match_the_documented_module_constants() -> None:
    """Same guarantee as Limits: grouping must not change a single default."""
    request = engine_mod.RequestConfig()
    assert request.system_prompt == engine_mod.DEFAULT_EMBODY_SYSTEM_PROMPT
    assert request.base_url is None
    assert request.api_key is None
    assert request.temperature == engine_mod.DEFAULT_TEMPERATURE
    assert request.max_tokens is None
    assert request.enable_thinking is False


def test_request_config_is_frozen() -> None:
    request = engine_mod.RequestConfig()
    with pytest.raises(dataclasses.FrozenInstanceError):
        request.temperature = 1.0  # type: ignore[misc]


def test_the_constructor_keeps_seams_explicit_and_moves_bounds_and_request_config_out() -> None:
    """S107's fix: bounds and request config each collapse to one keyword."""
    params = inspect.signature(EmbodyTurnEngine.__init__).parameters
    names = set(params) - {"self"}

    assert names.isdisjoint(_LIMIT_FIELDS), "a bound is still a bare parameter"
    assert names.isdisjoint(_REQUEST_FIELDS), "a request field is still a bare parameter"
    assert {"limits", "request"} <= names

    seams = {
        "registry",
        "turn_fn",
        "export",
        "models",
        "on_content",
        "on_reasoning",
        "cancel",
        "now_fn",
        "sleep",
    }
    assert seams <= names, "an injectable seam must stay an explicit parameter"
    assert all(params[name].kind is inspect.Parameter.KEYWORD_ONLY for name in names)


def test_the_constructor_clears_this_projects_configured_s107_threshold() -> None:
    """The hard acceptance criterion: not "fewer parameters", but under the gate.

    13 is this project's CONFIGURED ``python:S107`` threshold, queried live
    against SonarCloud rather than assumed — the language-wide default (7)
    would be the wrong number to pin here.
    """
    params = inspect.signature(EmbodyTurnEngine.__init__).parameters
    count = len(params) - 1  # exclude self
    assert count <= 13, f"{count} parameters still exceeds the 13 authorized"


def test_a_bound_passed_through_limits_reaches_the_engine() -> None:
    """Behavioural proof, not just a signature check: the value actually takes."""
    engine = _build(limits=engine_mod.Limits(max_pending=1))
    assert engine.submit_utterance("first") is True
    assert engine.submit_utterance("second") is False
    assert engine.dropped_inputs == 1


def test_a_field_passed_through_request_config_reaches_the_engine() -> None:
    """Behavioural proof for the second grouping, mirroring the Limits one."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, request=engine_mod.RequestConfig(max_tokens=99))
    engine.submit_utterance("tick")
    engine.run_turn()
    assert turn.calls[0]["kwargs"]["max_tokens"] == 99


# =========================================================================== #
# issue #139/h9 — ask() carries a multimodal clip question with no branching  #
# =========================================================================== #
#
# The wiring itself (clip -> ask() -> CONTEXT, never a trigger) is task t11's
# job and lives entirely in tests/test_agent_embody.py, alongside
# reachy.cli._commands.agent.build_clip_question (the content-shaping helper
# that reads the clip file — deliberately NOT in this module, since this
# module's own model-config claim, "the engine reads no file", is machine-
# checked by an AST scan a few tests up). What belongs HERE is ask()'s own
# contract: it forwards whatever content it is given verbatim, string or an
# OpenAI-style multimodal list, with no branching of its own.


def test_ask_forwards_multimodal_content_verbatim_no_branching() -> None:
    """ask() must not care whether prompt is a string or a content list."""
    script = Script(chunks=[content_chunk("a red square"), finish()])
    with FakeChatServer(script=script) as server:
        engine = _build(base_url=server.base_url)
        content = [
            {"type": "text", "text": "describe this clip"},
            {"type": "video_url", "video_url": {"url": "data:video/mp4;base64,QUJD"}},
        ]
        answer = engine.ask(content)

    assert answer == "a red square"
    sent_messages = server.requests[0]["messages"]
    user_message = next(m for m in sent_messages if m["role"] == "user")
    assert user_message["content"] == content


# =========================================================================== #
# issue #154 decision c30 — nested windows onto ONE history + Qwen's summary  #
# (spec claims c3/c45, honesty h2/h30 — task t4)                              #
# =========================================================================== #
#
# There is ONE conversation history. Qwen (the worker lane, run_turn) sees the
# last n=60 turns verbatim; Gemma (the senses lane, ask()) sees the last m=20
# turns — a STRICT SUFFIX of the SAME deque, never a second, independently
# maintained one — plus a Qwen-maintained summary of everything older. This
# task builds the plumbing and the staleness marker; task t12 builds the
# actual summary PRODUCER (the LLM call that writes it).


def test_construction_refuses_a_senses_window_wider_than_the_worker_window() -> None:
    """m<=n is refused fail-closed at construction, never silently clamped."""
    with pytest.raises(ValueError):
        engine_mod.Limits(history_maxlen=10, senses_history_maxlen=11)


def test_an_equal_window_is_allowed_the_bound_is_m_lte_n_not_m_lt_n() -> None:
    limits = engine_mod.Limits(history_maxlen=10, senses_history_maxlen=10)
    assert limits.senses_history_maxlen == limits.history_maxlen == 10


def test_the_shipped_defaults_are_m20_nested_in_n60() -> None:
    """Decision c30, sized against the t1 media measurement (401 vs 2399 tokens)."""
    limits = engine_mod.Limits()
    assert limits.senses_history_maxlen == engine_mod.DEFAULT_SENSES_HISTORY_MAXLEN == 20
    assert limits.history_maxlen == engine_mod.DEFAULT_HISTORY_MAXLEN == 60


def test_gemmas_window_is_a_strict_suffix_of_qwens_over_one_shared_history() -> None:
    """ONE deque, not two: Gemma's window is exactly the tail of the worker's own."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, history_maxlen=5, senses_history_maxlen=3)
    for index in range(8):
        engine.submit_utterance(f"turn {index}")
        engine.run_turn()
    worker_user_texts = [m["content"] for m in turn.last_messages() if m["role"] == "user"]

    engine.ask("what's going on?")
    senses_user_texts = [m["content"] for m in turn.last_messages() if m["role"] == "user"]

    # The worker's own last call already shows its full n-turn window ending
    # in the turn it is currently building; Gemma's window (minus the fresh
    # prompt) is exactly the tail 3 of that same sequence.
    assert senses_user_texts[:-1] == worker_user_texts[-3:]
    assert senses_user_texts[-1] == "what's going on?"


def test_gemmas_context_stays_bounded_over_100_plus_turns() -> None:
    """Honesty h2: a long conversation does not grow Gemma's per-call context."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn, history_maxlen=60, senses_history_maxlen=20)
    for index in range(60):
        engine.submit_utterance(f"turn {index}")
        engine.run_turn()
    engine.ask("what's up?")
    count_after_60_turns = len(turn.last_messages())

    for index in range(60, 160):
        engine.submit_utterance(f"turn {index}")
        engine.run_turn()
    engine.ask("what's up?")
    count_after_160_turns = len(turn.last_messages())

    assert count_after_60_turns == count_after_160_turns
    # m history turns x 2 messages (user + assistant) + the fresh prompt; no
    # summary was ever set, so there is no extra system message.
    assert count_after_160_turns == 2 * 20 + 1


def test_the_worker_lane_never_sees_the_summary_only_gemma_does() -> None:
    """Never regenerate the summary per lane (design note): Qwen already has the turns."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.update_summary("earlier: the kitchen was quiet.")
    engine.submit_utterance("hello again")
    engine.run_turn()
    worker_messages = turn.last_messages()
    assert not any("kitchen was quiet" in str(m.get("content", "")) for m in worker_messages)

    engine.ask("what's up?")
    senses_messages = turn.last_messages()
    assert any("kitchen was quiet" in str(m.get("content", "")) for m in senses_messages)


def test_a_down_worker_yields_a_stale_summary_marker_and_a_named_drop(caplog) -> None:
    """c45/h30: never a silent narrowing of Gemma's memory to just the m turns."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.submit_utterance("earlier turn")
    engine.run_turn()

    with caplog.at_level("INFO", logger="reachy.sense"):
        engine.mark_summary_stale("worker llm unreachable")
    assert engine.summary_stale_count == 1
    assert REASON_SUMMARY_STALE in caplog.text

    engine.ask("what's up?")
    senses_messages = turn.last_messages()
    system_text = " ".join(
        str(m.get("content", "")) for m in senses_messages if m["role"] == "system"
    )
    assert STALE_SUMMARY_MARKER in system_text
    # The m-turn window itself is untouched: still there beside the marker,
    # never narrowed away by the failed summary maintenance pass.
    assert any("earlier turn" in str(m.get("content", "")) for m in senses_messages)


def test_the_stale_marker_clears_on_the_next_successful_summary_update() -> None:
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)
    engine.mark_summary_stale("worker unreachable")
    assert engine.summary_is_stale is True

    assert engine.update_summary("earlier: the room was quiet.") is True
    assert engine.summary_is_stale is False

    engine.ask("what's up?")
    senses_messages = turn.last_messages()
    system_text = " ".join(
        str(m.get("content", "")) for m in senses_messages if m["role"] == "system"
    )
    assert STALE_SUMMARY_MARKER not in system_text
    assert "the room was quiet" in system_text


def test_an_overlong_summary_is_refused_not_truncated(caplog) -> None:
    """The summary itself is bounded (design note); a caller cannot silently exceed it."""
    engine = _build(limits=engine_mod.Limits(summary_max_chars=10))
    with caplog.at_level("INFO", logger="reachy.sense"):
        accepted = engine.update_summary("this summary is definitely longer than ten chars")
    assert accepted is False
    assert REASON_SUMMARY_TOO_LONG in caplog.text
    assert engine.summary_is_stale is False


def test_an_empty_summary_update_is_a_named_drop_not_a_silent_clear(caplog) -> None:
    engine = _build()
    with caplog.at_level("INFO", logger="reachy.sense"):
        accepted = engine.update_summary("   ")
    assert accepted is False
    assert REASON_EMPTY_INPUT in caplog.text


def test_summary_reasons_are_in_the_shared_drop_vocabulary() -> None:
    assert REASON_SUMMARY_STALE in DROP_REASONS
    assert REASON_SUMMARY_TOO_LONG in DROP_REASONS


# =========================================================================== #
# t7 — the said/unsaid record and the wanted-to-say artifact                   #
# (spec claims c34/c39/c41, honesty h22/h24/h26)                              #
# =========================================================================== #

_CUT_TEXT = "one two three four five six"
_CUT_SAID = "one two"
_CUT_UNSAID = "three four five six"


@dataclasses.dataclass(frozen=True)
class _StubSplit:
    """The four fields the engine reads — the duck type, without the wire.

    The engine takes the split as a structural type on purpose (it must not
    import the WebSocket client to record what its own mouth did), so most of
    these tests hand it this stub; the one test about the JOIN hands it the
    real :class:`~reachy.speech.realtime_duplex.SpokenSplit`.
    """

    response_id: str
    text: str
    said: str
    unsaid: str


def _split(
    said: str = _CUT_SAID,
    unsaid: str = _CUT_UNSAID,
    *,
    text: str = _CUT_TEXT,
    response_id: str = "resp_cut",
) -> _StubSplit:
    return _StubSplit(response_id=response_id, text=text, said=said, unsaid=unsaid)


def _last_user(turn: ScriptedTurn) -> str:
    return turn.last_messages()[-1]["content"]


def test_t7_a_cut_reply_records_only_the_measured_said_portion_as_spoken() -> None:
    """c34: the mind learns exactly what the room got, never the whole reply."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    engine.note_interrupted_reply(_split())
    engine.submit_utterance("sorry, go on")
    engine.run_turn()

    said_section = _last_user(turn)
    assert f'"{_CUT_SAID}"' in said_section
    assert f'"{_CUT_TEXT}"' not in said_section, "the unspoken remainder was recorded as said"


def test_t7_the_unsaid_remainder_is_readable_by_the_next_turn() -> None:
    """h22: the remainder is kept so the mind can decide whether it still matters."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    artifact = engine.note_interrupted_reply(_split())
    assert artifact is not None
    engine.submit_utterance("what were you saying?")
    engine.run_turn()

    assert artifact.render() in _last_user(turn)
    assert _CUT_UNSAID in _last_user(turn)


def test_t7_the_remainder_is_attributed_to_the_interrupted_response() -> None:
    engine = _build()
    artifact = engine.note_interrupted_reply(_split())

    assert artifact is not None
    assert artifact.response_id == "resp_cut"
    assert artifact.text == _CUT_UNSAID
    assert engine.wanted_to_say == (artifact,)


def test_t7_the_remainder_never_triggers_a_turn() -> None:
    """The robot never wakes itself up to finish an old sentence (c43)."""
    engine = _build()
    engine.note_interrupted_reply(_split())

    assert engine.pending == 0
    assert engine.parked == 1
    assert engine.run_turn() is False


def test_t7_a_cut_after_the_reply_was_recorded_whole_corrects_the_record() -> None:
    """The ordering phase 1 actually produces: ``response.done`` first, cut second.

    The wire delivers a reply seconds ahead of the speaker, so the human who
    interjects over the tail does it long after ``on_response`` fired. The
    record must end up describing the room, not the wire.
    """
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    engine.note_spoken(_CUT_TEXT)
    engine.note_interrupted_reply(_split())
    engine.submit_utterance("sorry, carry on")
    engine.run_turn()

    content = _last_user(turn)
    assert f'"{_CUT_SAID}"' in content
    assert f'"{_CUT_TEXT}"' not in content
    assert content.count("I have already said out loud") == 1


def test_t7_a_reply_cut_before_a_word_was_heard_is_never_recorded_as_spoken() -> None:
    """Nothing measured means nothing said — the donor's own load-bearing guard."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    artifact = engine.note_interrupted_reply(_split(said="", unsaid=_CUT_TEXT))
    engine.submit_utterance("hello?")
    engine.run_turn()

    assert artifact is not None
    assert artifact.text == _CUT_TEXT
    assert "I have already said out loud" not in _last_user(turn)
    assert artifact.render() in _last_user(turn)


def test_t7_an_over_long_remainder_is_a_named_drop_and_the_said_half_still_stands(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Refused, never truncated (t5's rule) — and never silently."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        artifact = engine.note_interrupted_reply(
            _split(said="here we go", unsaid="a" * (MAX_SAY_CHARS + 1))
        )

    assert artifact is None
    assert REFUSAL_WANTED_TO_SAY_TOO_LONG in caplog.text
    engine.submit_utterance("go on")
    engine.run_turn()
    assert '"here we go"' in _last_user(turn)


def test_t7_the_canonical_record_of_remainders_is_bounded_and_expires() -> None:
    """c43: bounded and expiring, so a stale sentence cannot shape a later turn."""
    engine = _build(wanted_to_say_maxlen=2)
    for index in range(4):
        engine.note_interrupted_reply(_split(unsaid=f"remainder {index}"))

    assert len(engine.wanted_to_say) == 2
    assert [artifact.text for artifact in engine.wanted_to_say] == ["remainder 2", "remainder 3"]

    for _ in range(DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS + 1):
        engine.submit_utterance("keep talking")
        engine.run_turn()
    assert engine.wanted_to_say == ()


def test_t7_the_layers_record_makes_no_claim_the_server_matches() -> None:
    """h24: the divergence is knowing and bounded, and the docstring says so."""
    doc = EmbodyTurnEngine.note_interrupted_reply.__doc__ or ""

    assert "server" in doc.lower()
    assert "overstate" in doc.lower()
    engine = _build()
    engine.note_interrupted_reply(_split())
    # Nothing in the canonical record asserts anything about the floor: the
    # said half is the measured prefix and the unsaid half is an artifact.
    assert [artifact.text for artifact in engine.wanted_to_say] == [_CUT_UNSAID]
    assert engine.replies_cut == 1


def test_t7_the_engine_consumes_the_real_duplex_split_type() -> None:
    """The join, unmocked: the wire's own dataclass satisfies the engine's seam."""
    from reachy.speech.realtime_duplex import PlaybackProgress, split_spoken

    engine = _build()
    split = split_spoken(
        _CUT_TEXT,
        PlaybackProgress(
            response_id="resp_cut",
            queued_bytes=48,
            played_bytes=16,
            in_flight_bytes=0,
            skipped_bytes=32,
            cancelled=True,
            total_bytes=48,
        ),
    )

    assert (split.said, split.unsaid) == (_CUT_SAID, _CUT_UNSAID)
    artifact = engine.note_interrupted_reply(split)
    assert artifact is not None
    assert artifact.response_id == "resp_cut"


def test_t7_a_reply_that_played_whole_is_recorded_exactly_as_before() -> None:
    """No cut, no artifact: the ordinary path is untouched."""
    engine = _build()
    artifact = engine.note_interrupted_reply(_split(said=_CUT_TEXT, unsaid=""))

    assert artifact is None
    assert engine.wanted_to_say == ()
    assert engine.parked == 0


def test_t7_a_cut_that_heard_nothing_removes_the_record_it_had_already_made() -> None:
    """The correction's other half: cut before a word landed, after ``response.done``."""
    turn = ScriptedTurn(TurnResult(content="ok", finish_reason="stop"))
    engine = _build(turn_fn=turn)

    engine.note_spoken(_CUT_TEXT)
    artifact = engine.note_interrupted_reply(_split(said="", unsaid=_CUT_TEXT))
    engine.submit_utterance("hello?")
    engine.run_turn()

    assert artifact is not None
    assert artifact.text == _CUT_TEXT
    assert "I have already said out loud" not in _last_user(turn)
    assert artifact.render() in _last_user(turn)


def test_t7_a_cut_reply_with_no_text_at_all_is_counted_and_keeps_nothing() -> None:
    """A reply whose text never arrived: named and counted, never invented."""
    engine = _build()

    assert engine.note_interrupted_reply(_split(said="", unsaid="", text="")) is None
    assert (engine.replies_cut, engine.parked, engine.wanted_to_say) == (1, 0, ())
