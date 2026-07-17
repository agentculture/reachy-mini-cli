"""Tests for the agent turn engine (:mod:`reachy.speech.agent_turn`).

The :class:`~reachy.speech.agent_turn.AgentTurnEngine` is the tool-use counterpart
of :class:`~reachy.speech.cognition.CognitionEngine`: it consumes the same
:class:`~reachy.speech.events.EventBuffer`, runs serialized agent turns through the
LLM tool loop, executes each :class:`~reachy.speech.llm.ToolCall` through an injected
:class:`~reachy.speech.tools.ToolRegistry`, and feeds the same
``thinking``/``message``/``emotion`` export sinks.

Every collaborator is faked — no live LLM, no registry side effects, no robot, no
sleeps — so the suite is fully deterministic.

Acceptance criteria (task t6)
-----------------------------
1. The full tool loop runs (snapshot → messages → LLM turn → dispatch each tool →
   append the tool-result → loop until a turn has no tool_calls, bounded), and
   emits export blocks that validate against ``docs/export-schema.md``: one
   ``ThinkingEvent`` per turn, one ``MessageEvent`` per speak/harmonics call (at
   dispatch time, in order), one ``EmotionEvent`` per apply_pose call.
2. Agent mode is a new module — with agent mode off nothing changes (covered by the
   untouched cognition/marker suites; here we only assert the new engine).
3. ``audio_optional`` degradation carries over: a failed speak dispatch degrades to
   "no speech" (logged once, latched off after consecutive failures) while tool
   dispatch results, expression motion, and export blocks keep flowing.
"""

from __future__ import annotations

import json
import logging
import threading

import pytest

from reachy.cli._errors import CliError
from reachy.export.events import EmotionEvent, MessageEvent, ThinkingEvent, to_jsonl
from reachy.export.exporter import ExportHook
from reachy.speech.agent_turn import (
    DEFAULT_AGENT_SYSTEM_PROMPT,
    AgentTurnEngine,
)
from reachy.speech.events import EventBuffer, SenseCue
from reachy.speech.llm import ToolCall, TurnResult

# ---------------------------------------------------------------------------
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------

_SENSE_LOGGER_NAME = "reachy.sense"


def _sense_records(caplog) -> list:
    return [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _const_clock(value: float = 0.0):
    return lambda: value


def _buf_with_cue(text_speech: bool = True) -> EventBuffer:
    buf = EventBuffer(clock=_const_clock())
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)  # "speech from the left"
    return buf


def _refill(buf: EventBuffer) -> None:
    buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)


class FakeRegistry:
    """A fake :class:`ToolRegistry`: publishes tool defs + records dispatches.

    ``results`` maps a tool name to either a ready ``content`` string or a callable
    ``(name, arguments_json, tool_call_id) -> content_str``.  Absent names dispatch
    to a default ``{"status": "ok"}`` result.
    """

    def __init__(self, results: dict | None = None) -> None:
        self.dispatched: list[tuple[str, object, object]] = []
        self._results = results or {}
        self._defs = [
            {"type": "function", "function": {"name": n, "parameters": {}}}
            for n in ("speak", "harmonics", "apply_pose")
        ]

    def tools(self) -> list[dict]:
        return self._defs

    def dispatch(self, name, arguments_json, tool_call_id=None) -> dict:
        self.dispatched.append((name, arguments_json, tool_call_id))
        r = self._results.get(name)
        if callable(r):
            content = r(name, arguments_json, tool_call_id)
        elif r is not None:
            content = r
        else:
            content = json.dumps({"status": "ok"})
        return {"role": "tool", "tool_call_id": tool_call_id, "content": content}


class ScriptedTurn:
    """A turn function driven by the conversation shape.

    ``responder`` receives the messages list and returns the next
    :class:`TurnResult`.  Records every messages snapshot + kwargs seen.
    """

    def __init__(self, responder) -> None:
        self._responder = responder
        self.calls: list[list[dict]] = []
        self.kwargs: list[dict] = []

    def __call__(self, messages, **kwargs) -> TurnResult:
        self.calls.append(list(messages))
        self.kwargs.append(kwargs)
        return self._responder(messages)


def _speak_call(text: str = "hello", call_id: str = "c1") -> ToolCall:
    args = {"text": text}
    return ToolCall(id=call_id, name="speak", arguments=args, arguments_json=json.dumps(args))


def _pose_call(emoji: str = "🤔", call_id: str = "c2") -> ToolCall:
    args = {"emoji": emoji}
    return ToolCall(id=call_id, name="apply_pose", arguments=args, arguments_json=json.dumps(args))


def _harm_call(text: str = "la la", call_id: str = "c3") -> ToolCall:
    args = {"text": text}
    return ToolCall(id=call_id, name="harmonics", arguments=args, arguments_json=json.dumps(args))


def _err_content(msg: str = "boom") -> str:
    return json.dumps({"error": msg})


def _last_is_tool(messages) -> bool:
    return bool(messages) and messages[-1].get("role") == "tool"


class _FakeCueBuffer:
    """Minimal ``_BufferLike`` fake: a fixed list of cues, drained once by snapshot().

    Used to exercise the engine with a cue kind (e.g. touch) that has no producer
    on ``EventBuffer`` yet, without depending on a sibling task's in-flight
    ``feed_pat``-style API.
    """

    def __init__(self, cues: list[SenseCue]) -> None:
        self._cues = cues

    def snapshot(self) -> list[SenseCue]:
        cues, self._cues = self._cues, []
        return cues


# ---------------------------------------------------------------------------
# Basic turn behaviour
# ---------------------------------------------------------------------------


def test_run_turn_no_cues_is_a_noop():
    """With an empty buffer, a turn neither calls the LLM nor dispatches a tool."""
    reg = FakeRegistry()
    turn = ScriptedTurn(lambda m: TurnResult(content="x", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=EventBuffer(clock=_const_clock()), registry=reg, turn_fn=turn)

    assert engine.run_turn() is False
    assert turn.calls == []
    assert reg.dispatched == []


def test_run_turn_executes_the_tool_loop_and_returns_true():
    """cues → LLM turn with a speak call → dispatch → next turn has no calls → done."""
    reg = FakeRegistry()

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="all done", tool_calls=[], finish_reason="stop")
        return TurnResult(content="", tool_calls=[_speak_call("hi")], finish_reason="tool_calls")

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn)

    assert engine.run_turn() is True
    # The speak tool was dispatched exactly once through the registry.
    assert [d[0] for d in reg.dispatched] == ["speak"]
    # Two LLM rounds: the tool round, then the terminal (no-tools) round.
    assert len(turn.calls) == 2


def test_tools_array_is_passed_to_the_turn_function():
    """The registry's published tools array is forwarded to the LLM turn call."""
    reg = FakeRegistry()
    turn = ScriptedTurn(lambda m: TurnResult(content="ok", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn)
    engine.run_turn()
    assert turn.kwargs[0]["tools"] == reg.tools()


def test_tool_result_message_is_appended_and_seen_by_the_next_round():
    """A dispatched tool's result message is fed back into the next LLM round."""
    reg = FakeRegistry(results={"speak": json.dumps({"status": "ok", "said": "hi"})})

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="done", tool_calls=[], finish_reason="stop")
        return TurnResult(content="", tool_calls=[_speak_call("hi")], finish_reason="tool_calls")

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn)
    engine.run_turn()

    # The SECOND round's messages include the assistant tool-call turn + the tool result.
    second = turn.calls[1]
    assert any(m.get("role") == "assistant" and m.get("tool_calls") for m in second)
    tool_msgs = [m for m in second if m.get("role") == "tool"]
    assert len(tool_msgs) == 1
    assert json.loads(tool_msgs[0]["content"])["said"] == "hi"


def test_tool_loop_is_bounded_by_max_tool_rounds():
    """A model that never stops calling tools is capped at max_tool_rounds dispatches."""
    reg = FakeRegistry()

    def responder(messages):
        # Always request another pose — never terminates on its own.
        return TurnResult(content="", tool_calls=[_pose_call()], finish_reason="tool_calls")

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn, max_tool_rounds=6)
    engine.run_turn()

    assert len(reg.dispatched) == 6  # bounded, no infinite loop
    assert len(turn.calls) == 6


# ---------------------------------------------------------------------------
# Export blocks — schema-conformant, in order
# ---------------------------------------------------------------------------


def test_export_emits_emotion_message_thinking_in_order():
    """One EmotionEvent (apply_pose), one MessageEvent (speak) at dispatch time, one
    ThinkingEvent at turn end — in stream order."""
    reg = FakeRegistry()
    exported: list[object] = []

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="the end", tool_calls=[], finish_reason="stop")
        return TurnResult(
            content="",
            tool_calls=[_pose_call("🤔"), _speak_call("hi there")],
            finish_reason="tool_calls",
        )

    turn = ScriptedTurn(responder)
    hook = ExportHook(
        emit=exported.append,
        pose_resolver=lambda e: {"head_pitch": -5.0} if e == "🤔" else None,
        time_fn=lambda: 3.0,
    )
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn, export=hook)
    engine.run_turn()

    kinds = [type(ev) for ev in exported]
    assert kinds == [EmotionEvent, MessageEvent, ThinkingEvent]
    emotion, message, thinking = exported
    assert emotion.emoji == "🤔"
    assert emotion.pose == {"head_pitch": -5.0}
    assert message.text == "hi there"
    assert set(thinking.cues) == {"speech from the left"}
    assert thinking.ts == 3.0


def test_thinking_text_includes_tool_call_representations():
    """ThinkingEvent.text carries the raw turn text incl. the tool calls made."""
    reg = FakeRegistry()
    exported: list[object] = []

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="", tool_calls=[], finish_reason="stop")
        return TurnResult(
            content="I should greet them.",
            tool_calls=[_speak_call("hello!")],
            finish_reason="tool_calls",
        )

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=turn,
        export=ExportHook(emit=exported.append, time_fn=lambda: 1.0),
    )
    engine.run_turn()

    thinking = [ev for ev in exported if isinstance(ev, ThinkingEvent)]
    assert len(thinking) == 1
    text = thinking[0].text
    assert "I should greet them." in text
    assert "speak" in text  # the tool call is represented
    assert "hello!" in text


def test_exported_blocks_validate_against_the_wire_schema():
    """Every emitted block serializes to the documented NDJSON shape."""
    reg = FakeRegistry()
    exported: list[object] = []

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="bye", tool_calls=[], finish_reason="stop")
        return TurnResult(
            content="",
            tool_calls=[_pose_call("🎉"), _harm_call("tra la")],
            finish_reason="tool_calls",
        )

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=turn,
        export=ExportHook(
            emit=exported.append,
            pose_resolver=lambda e: {"body_yaw": 10.0},
            time_fn=lambda: 2.5,
        ),
    )
    engine.run_turn()

    for ev in exported:
        obj = json.loads(to_jsonl(ev))
        assert "t" in obj and "ts" in obj
        assert obj["ts"] == 2.5
        if obj["t"] == "emotion":
            assert obj["emoji"] == "🎉"
            assert obj["pose"] == {"body_yaw": 10.0}
        elif obj["t"] == "message":
            assert obj["text"] == "tra la"
        elif obj["t"] == "thinking":
            assert isinstance(obj["cues"], list)
            assert isinstance(obj["text"], str)
        else:  # pragma: no cover - forward-compat guard
            raise AssertionError(f"unexpected block type {obj['t']!r}")


def test_harmonics_call_also_emits_a_message_block():
    """Harmonics is an audio/utterance tool too — it emits a MessageEvent."""
    reg = FakeRegistry()
    exported: list[object] = []

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="", tool_calls=[], finish_reason="stop")
        return TurnResult(content="", tool_calls=[_harm_call("do re mi")], finish_reason="tool")

    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=ScriptedTurn(responder),
        export=ExportHook(emit=exported.append, time_fn=lambda: 0.0),
    )
    engine.run_turn()
    msgs = [ev for ev in exported if isinstance(ev, MessageEvent)]
    assert [m.text for m in msgs] == ["do re mi"]


# ---------------------------------------------------------------------------
# audio_optional degradation
# ---------------------------------------------------------------------------


def _one_speak_turn(messages) -> TurnResult:
    if _last_is_tool(messages):
        return TurnResult(content="ok", tool_calls=[], finish_reason="stop")
    return TurnResult(content="", tool_calls=[_speak_call("hi")], finish_reason="tool_calls")


def test_strict_mode_raises_on_speak_dispatch_error():
    """Default (audio_optional=False): a failed speak dispatch aborts the turn."""
    reg = FakeRegistry(results={"speak": _err_content("TTS unreachable")})
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(), registry=reg, turn_fn=ScriptedTurn(_one_speak_turn)
    )
    with pytest.raises(CliError):
        engine.run_turn()


def test_audio_optional_absorbs_a_failed_speak_and_completes():
    """audio_optional=True: a failed speak degrades to no-speech; the turn completes."""
    reg = FakeRegistry(results={"speak": _err_content()})
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=ScriptedTurn(_one_speak_turn),
        audio_optional=True,
    )
    assert engine.run_turn() is True  # no raise


def test_audio_optional_still_emits_message_and_thinking_when_tts_dead():
    """The thought still reaches the export sink even when speech synthesis fails."""
    reg = FakeRegistry(results={"speak": _err_content()})
    exported: list[object] = []
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=ScriptedTurn(_one_speak_turn),
        export=ExportHook(emit=exported.append, time_fn=lambda: 1.0),
        audio_optional=True,
    )
    engine.run_turn()
    assert any(isinstance(ev, MessageEvent) and ev.text == "hi" for ev in exported)
    assert any(isinstance(ev, ThinkingEvent) for ev in exported)


def test_audio_latches_off_after_threshold_consecutive_failures():
    """Once muted, no further speak dispatch is attempted — but messages still flow."""
    reg = FakeRegistry(results={"speak": _err_content()})
    exported: list[object] = []
    buf = _buf_with_cue()
    engine = AgentTurnEngine(
        buffer=buf,
        registry=reg,
        turn_fn=ScriptedTurn(_one_speak_turn),
        export=ExportHook(emit=exported.append, time_fn=lambda: 1.0),
        audio_optional=True,
    )
    # Threshold defaults to 2: turn1 fails (streak 1), turn2 fails (streak 2 → mute),
    # turn3/4 are muted so the speak tool is never dispatched again.
    for _ in range(4):
        engine.run_turn()
        _refill(buf)

    speak_dispatches = [d for d in reg.dispatched if d[0] == "speak"]
    assert len(speak_dispatches) == 2  # capped at the mute threshold
    # A MessageEvent was still exported every turn, muted or not.
    msgs = [ev for ev in exported if isinstance(ev, MessageEvent)]
    assert len(msgs) == 4


def test_apply_pose_keeps_dispatching_while_audio_is_muted():
    """Muting the audio sink must not disable non-audio tools (pose/expression)."""
    reg = FakeRegistry(results={"speak": _err_content()})
    buf = _buf_with_cue()

    def responder(messages):
        if _last_is_tool(messages):
            return TurnResult(content="", tool_calls=[], finish_reason="stop")
        return TurnResult(
            content="",
            tool_calls=[_speak_call("hi"), _pose_call("🎉")],
            finish_reason="tool_calls",
        )

    engine = AgentTurnEngine(
        buffer=buf, registry=reg, turn_fn=ScriptedTurn(responder), audio_optional=True
    )
    for _ in range(4):
        engine.run_turn()
        _refill(buf)

    pose_dispatches = [d for d in reg.dispatched if d[0] == "apply_pose"]
    assert len(pose_dispatches) == 4  # never suppressed by the audio latch


# ---------------------------------------------------------------------------
# Multi-turn rolling history
# ---------------------------------------------------------------------------


def test_prior_turn_context_flows_into_the_next_turn():
    """A bounded rolling history keeps consecutive turns coherent."""
    reg = FakeRegistry()
    buf = _buf_with_cue()
    finals = iter(["first reply", "second reply"])

    def responder(messages):
        # No tools — each turn is a single final assistant text.
        return TurnResult(content=next(finals), tool_calls=[], finish_reason="stop")

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(buffer=buf, registry=reg, turn_fn=turn)

    engine.run_turn()
    _refill(buf)
    engine.run_turn()

    # The second turn's prompt carries turn-1's user cue + assistant reply as history.
    second = turn.calls[1]
    assert any(m.get("role") == "assistant" and m.get("content") == "first reply" for m in second)
    user_msgs = [m for m in second if m.get("role") == "user"]
    assert len(user_msgs) == 2  # history user + current user


def test_history_is_bounded():
    """History never grows without bound (mirrors the 6-entry discipline)."""
    reg = FakeRegistry()
    buf = _buf_with_cue()

    def responder(messages):
        return TurnResult(content="ok", tool_calls=[], finish_reason="stop")

    turn = ScriptedTurn(responder)
    engine = AgentTurnEngine(buffer=buf, registry=reg, turn_fn=turn, history_maxlen=3)
    for _ in range(6):
        engine.run_turn()
        _refill(buf)

    last = turn.calls[-1]
    user_msgs = [m for m in last if m.get("role") == "user"]
    # At most history_maxlen history users + the current one.
    assert len(user_msgs) <= 3 + 1


# ---------------------------------------------------------------------------
# run() loop + ThinkHook compatibility
# ---------------------------------------------------------------------------


def test_run_loop_runs_turns_and_stops_at_max_turns():
    reg = FakeRegistry()
    buf = EventBuffer(clock=_const_clock())
    turns: list = []

    def responder(messages):
        turns.append(messages)
        return TurnResult(content="tick", tool_calls=[], finish_reason="stop")

    engine = AgentTurnEngine(
        buffer=buf, registry=reg, turn_fn=ScriptedTurn(responder), sleep=lambda _s: None
    )

    def feeder():
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    ran = engine.run(max_turns=3, before_turn=feeder)
    assert ran == 3
    assert len(turns) == 3


def test_run_loop_stops_on_predicate():
    reg = FakeRegistry()
    buf = _buf_with_cue()
    count = {"n": 0}

    def responder(messages):
        count["n"] += 1
        return TurnResult(content="x", tool_calls=[], finish_reason="stop")

    engine = AgentTurnEngine(
        buffer=buf, registry=reg, turn_fn=ScriptedTurn(responder), sleep=lambda _s: None
    )

    def feeder():
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)

    ran = engine.run(max_turns=10, stop=lambda: count["n"] >= 2, before_turn=feeder)
    assert ran == 2


def test_engine_exposes_buffer_attribute_for_thinkhook():
    """ThinkHook reads a ``.buffer`` attribute via getattr — expose it so a later
    wiring task can hand this engine to ThinkHook unchanged."""
    buf = _buf_with_cue()
    engine = AgentTurnEngine(
        buffer=buf, registry=FakeRegistry(), turn_fn=ScriptedTurn(lambda m: None)
    )
    assert engine.buffer is buf


def test_thinkhook_can_drive_the_engine_run_with_stop_only():
    """ThinkHook calls ``engine.run(stop=...)`` on a worker; that contract works."""
    reg = FakeRegistry()
    buf = _buf_with_cue()
    stop_flag = {"stop": False}

    def responder(messages):
        stop_flag["stop"] = True  # end after the first real turn
        return TurnResult(content="ok", tool_calls=[], finish_reason="stop")

    engine = AgentTurnEngine(
        buffer=buf, registry=reg, turn_fn=ScriptedTurn(responder), sleep=lambda _s: None
    )
    ran = engine.run(stop=lambda: stop_flag["stop"])
    assert ran == 1


# ---------------------------------------------------------------------------
# Serialization + constants
# ---------------------------------------------------------------------------


def test_run_turn_serializes_turns_with_a_lock():
    """Two concurrent run_turn() calls cannot overlap — the second blocks."""
    reg = FakeRegistry()
    buf = _buf_with_cue()

    a_running = threading.Event()
    release_a = threading.Event()
    b_running = threading.Event()
    order: list[str] = []

    def responder(messages):
        if not a_running.is_set():
            order.append("A")
            a_running.set()
            assert release_a.wait(timeout=5.0)
        else:
            order.append("B")
            b_running.set()
        # keep a cue around for B to consume
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)
        return TurnResult(content="ok", tool_calls=[], finish_reason="stop")

    engine = AgentTurnEngine(buffer=buf, registry=reg, turn_fn=ScriptedTurn(responder))

    ta = threading.Thread(target=engine.run_turn)
    ta.start()
    assert a_running.wait(timeout=5.0)

    tb = threading.Thread(target=engine.run_turn)
    tb.start()
    assert not b_running.wait(timeout=0.2), "second turn overlapped the first"

    release_a.set()
    ta.join(timeout=5.0)
    tb.join(timeout=5.0)
    assert order == ["A", "B"]


def test_default_system_prompt_is_a_module_constant():
    assert isinstance(DEFAULT_AGENT_SYSTEM_PROMPT, str)
    assert "Reachy" in DEFAULT_AGENT_SYSTEM_PROMPT
    # It names the tool-only contract so ops can see (and tune) the guidance.
    assert "tool" in DEFAULT_AGENT_SYSTEM_PROMPT.lower()


def test_default_system_prompt_names_touch_as_a_perception():
    """A sibling task feeds pat/touch cues into the buffer — the prompt must give
    the model a frame for them (e.g. "touch" or "petted"/"patted")."""
    lowered = DEFAULT_AGENT_SYSTEM_PROMPT.lower()
    assert "touch" in lowered or "petted" in lowered or "patted" in lowered


# ---------------------------------------------------------------------------
# Touch/pat perception (t5)
# ---------------------------------------------------------------------------


def test_pat_only_cue_triggers_an_agent_turn():
    """A lone touch cue (no words, no DoA) is enough to fire a turn, and the cue
    text reaches the LLM as part of the user perception message."""
    reg = FakeRegistry()
    cue_text = "felt a gentle scratch on the head"
    buf = _FakeCueBuffer([SenseCue(text=cue_text, timestamp=0.0)])
    turn = ScriptedTurn(lambda m: TurnResult(content="aww", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=buf, registry=reg, turn_fn=turn)

    assert engine.run_turn() is True
    assert len(turn.calls) == 1
    user_messages = [m for m in turn.calls[0] if m.get("role") == "user"]
    assert any(cue_text in m.get("content", "") for m in user_messages)


# ---------------------------------------------------------------------------
# [SENSE] instrumentation (task t4)
# ---------------------------------------------------------------------------


def test_run_turn_logs_a_sense_turn_line_with_cue_count(caplog):
    """A turn that fires logs exactly one [SENSE stage=turn] line naming the cue count."""
    reg = FakeRegistry()
    turn = ScriptedTurn(lambda m: TurnResult(content="ok", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        assert engine.run_turn() is True

    records = _sense_records(caplog)
    turn_records = [r for r in records if "stage=turn" in r.getMessage()]
    assert len(turn_records) == 1
    assert "cue_count=1" in turn_records[0].getMessage()


def test_run_turn_no_cues_logs_no_sense_turn_line(caplog):
    """An empty-buffer no-op turn never fires the stage=turn line."""
    reg = FakeRegistry()
    turn = ScriptedTurn(lambda m: TurnResult(content="x", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=EventBuffer(clock=_const_clock()), registry=reg, turn_fn=turn)

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        assert engine.run_turn() is False

    assert _sense_records(caplog) == []


def test_audio_optional_failure_logs_a_sense_drop_line(caplog):
    """The first absorbed speak failure of a streak emits a greppable [SENSE] drop
    line, additive to the existing warning log (not a replacement)."""
    reg = FakeRegistry(results={"speak": _err_content()})
    engine = AgentTurnEngine(
        buffer=_buf_with_cue(),
        registry=reg,
        turn_fn=ScriptedTurn(_one_speak_turn),
        audio_optional=True,
    )

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        engine.run_turn()

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped reason=audio-muted" in r.getMessage()]
    assert len(drop_records) == 1
    assert drop_records[0].getMessage().startswith("[SENSE stage=action")


def test_audio_latch_logs_a_second_sense_drop_line_when_muted(caplog):
    """Reaching the mute threshold fires its own drop line, in addition to the
    first-failure drop line — one per existing warning call site."""
    reg = FakeRegistry(results={"speak": _err_content()})
    buf = _buf_with_cue()
    engine = AgentTurnEngine(
        buffer=buf,
        registry=reg,
        turn_fn=ScriptedTurn(_one_speak_turn),
        audio_optional=True,
    )

    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        # DEFAULT_AUDIO_MUTE_THRESHOLD == 2: turn1 -> first-failure drop, turn2 ->
        # mute-threshold drop. Two turns, two drop lines total.
        for _ in range(2):
            engine.run_turn()
            _refill(buf)

    records = _sense_records(caplog)
    drop_records = [r for r in records if "dropped reason=audio-muted" in r.getMessage()]
    assert len(drop_records) == 2


# ---------------------------------------------------------------------------
# Hot registration — the tool list is rebuilt per turn (task t13 restart-note)
# ---------------------------------------------------------------------------


def test_hot_registered_tool_is_callable_on_the_next_turn():
    """t13 restart-note finding: the engine reads ``registry.tools()`` FRESH on every
    round of every turn (agent_turn.py, ``_run_agent_turn``), so a tool hot-registered
    into the LIVE registry between turns is published on the very next turn — no
    per-session snapshot, no restart, no deferred-until-restart line needed."""
    reg = FakeRegistry()
    turn = ScriptedTurn(lambda m: TurnResult(content="ok", tool_calls=[], finish_reason="stop"))
    engine = AgentTurnEngine(buffer=_buf_with_cue(), registry=reg, turn_fn=turn)

    engine.run_turn()
    tools_turn1 = [d["function"]["name"] for d in turn.kwargs[-1]["tools"]]
    assert "wave-hello" not in tools_turn1

    # Hot-register a new tool into the LIVE registry (exactly what forge activation does).
    reg._defs.append({"type": "function", "function": {"name": "wave-hello", "parameters": {}}})

    _refill(engine.buffer)
    engine.run_turn()
    tools_turn2 = [d["function"]["name"] for d in turn.kwargs[-1]["tools"]]
    assert "wave-hello" in tools_turn2, "a tool registered between turns must be callable next turn"


def test_agent_turn_module_does_not_import_forge():
    """CRITICAL BOUNDARY (task t13): agent_turn.py must not import reachy.forge — the
    forge dispatch/activation seams arrive injected at composition, never imported here."""
    import ast
    import inspect

    import reachy.speech.agent_turn as agent_mod

    tree = ast.parse(inspect.getsource(agent_mod))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    for name in names:
        assert "reachy.forge" not in name, f"agent_turn.py must not import forge ({name!r})"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
