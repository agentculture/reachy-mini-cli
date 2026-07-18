"""Tests for the ``agent`` noun — the external agent client (t11).

Pins the three acceptance criteria of decisions c11 (the loop is AI-agnostic; an
agent attaches externally) and c27 (the agent publishes its OWN cognition feed;
the runtime feed carries no cognition):

1. With the runtime running, the client attaches — reads runtime events, acts
   through the intent tools (atomic spool writes) — with **no unit edit and no
   loop restart**.
2. The agent publishes its own ``thinking``/``message``/``emotion`` feed; the
   runtime feed carries no cognition block (the client's OUTPUT feed carries only
   cognition blocks, never a runtime block).
3. Detaching the agent changes nothing about the loop: runtime ticks and rules
   continue, proven in a bounded run.

Zero network / robot / live LLM anywhere: the tool-use engine is either a fake or
the REAL :class:`~reachy.speech.agent_turn.AgentTurnEngine` driven by an injected
fake ``turn_fn``. The criterion-3 tests reuse the real bounded engine loop the way
``tests/test_behavior_intents.py`` does.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import json

import pytest

import reachy.cli._commands.agent as agent_mod
from reachy.behavior import control as control_mod
from reachy.behavior.control import CommandSpool
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.intents import DECLARE_GOAL, INTENT_NAMESPACE, IntentDriver
from reachy.behavior.rule_engine import compose_rule_seam
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import Sense
from reachy.cli._commands.agent import (
    _cues_for_runtime_event,
    _open_feed,
    _parse_runtime_line,
    _run_attach_loop,
    _RuntimeCueBuffer,
    cmd_agent_attach,
    cmd_agent_overview,
)
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.explain.catalog import ENTRIES
from reachy.speech.llm import ToolCall, TurnResult

# --------------------------------------------------------------------------- #
# Fixtures / harness                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _make_attach_args(**kw) -> argparse.Namespace:
    defaults = dict(
        json=False,
        feed="-",
        spool_dir=None,
        await_timeout=1.0,
        max_turns=None,
        max_events=None,
        export=None,
        export_blocks=None,
    )
    defaults.update(kw)
    return argparse.Namespace(**defaults)


def _scripted_turn_fn(*results: TurnResult):
    """A fake ``turn_fn`` that returns *results* in order, then empty turns.

    Zero network — this is what every test injects in place of the real
    :func:`reachy.speech.llm.stream_turn`.
    """
    state = {"n": 0}

    def turn_fn(messages, *, tools=None, **kw):  # noqa: ANN001 - test double
        i = state["n"]
        state["n"] += 1
        if i < len(results):
            return results[i]
        return TurnResult(content="", tool_calls=[])

    return turn_fn


class _FakeEngine:
    """A minimal engine implementing ``run_turn()`` over the shared cue buffer."""

    def __init__(self, buffer, export=None):
        self.buffer = buffer
        self.export = export
        self.turns = 0
        self.snapshots: list[list[str]] = []

    def run_turn(self) -> bool:
        cues = self.buffer.snapshot()
        self.snapshots.append([c.text for c in cues])
        if not cues:
            return False
        self.turns += 1
        return True


class _FakeSink:
    def __init__(self):
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


def _sense_line(**fields) -> str:
    return json.dumps({"t": "sense", "ts": 1.0, "tick": 1, **fields})


# --------------------------------------------------------------------------- #
# Runtime-event → cue mapping                                                 #
# --------------------------------------------------------------------------- #


def test_sense_event_maps_to_perception_cues():
    cues = _cues_for_runtime_event({"t": "sense", "doa": 0.0, "speech": True})
    assert cues == ["speech from the left"]

    cues = _cues_for_runtime_event({"t": "sense", "doa": 3.14, "speech": False, "rms": 0.5})
    assert cues == ["loud sound right"]

    cues = _cues_for_runtime_event({"t": "sense", "pat": ["scratch", "level1"], "face": "Ada"})
    assert cues == ["felt a gentle scratch on the head", "saw Ada"]


def test_sense_quiet_non_speech_yields_no_cue():
    assert _cues_for_runtime_event({"t": "sense", "doa": 1.57, "speech": False, "rms": 0.001}) == []


def test_rule_event_maps_to_decision_cue():
    assert _cues_for_runtime_event(
        {"t": "rule", "action": "fire", "rule": "greet", "behavior": "wave"}
    ) == ["a behavior rule fired (greet): now doing wave"]
    assert _cues_for_runtime_event(
        {"t": "rule", "action": "fire", "rule": "hush", "disable": ["nod", "shake"]}
    ) == ["a behavior rule fired (hush): stopping nod, shake"]
    assert _cues_for_runtime_event({"t": "rule", "action": "suppress", "rule": "greet"}) == [
        "a behavior rule held off (greet)"
    ]


def test_intent_and_motion_events_map_to_cues():
    assert _cues_for_runtime_event({"t": "intent", "action": "declare", "name": "nod"}) == [
        "a standing intent was set: nod"
    ]
    assert _cues_for_runtime_event({"t": "intent", "action": "clear"}) == [
        "a standing intent was cleared"
    ]
    assert _cues_for_runtime_event({"t": "motion", "action": "admit", "behavior": "sway"}) == [
        "started moving: sway"
    ]
    assert _cues_for_runtime_event({"t": "motion", "action": "evict", "behavior": "sway"}) == [
        "stopped moving: sway"
    ]
    # a low-level goto keyframe is not surfaced as a cue
    assert _cues_for_runtime_event({"t": "motion", "action": "goto"}) == []


def test_unrecognised_or_malformed_event_yields_no_cue_never_raises():
    assert _cues_for_runtime_event({"t": "cognition"}) == []  # not a runtime block
    assert _cues_for_runtime_event({"t": "thinking"}) == []  # a cognition block
    assert _cues_for_runtime_event({}) == []
    assert _cues_for_runtime_event("not a dict") == []
    assert _cues_for_runtime_event(None) == []


def test_parse_runtime_line_tolerates_junk():
    assert _parse_runtime_line('{"t":"sense"}') == {"t": "sense"}
    assert _parse_runtime_line("") is None
    assert _parse_runtime_line("   \n") is None
    assert _parse_runtime_line("not json") is None
    assert _parse_runtime_line("[1,2,3]") is None  # not a dict


def test_runtime_cue_buffer_feeds_and_snapshots():
    buf = _RuntimeCueBuffer()
    added = buf.feed_event({"t": "sense", "doa": 0.0, "speech": True})
    assert added == 1
    assert buf.feed_event({"t": "motion", "action": "goto"}) == 0  # no cue
    cues = buf.snapshot()
    assert [c.text for c in cues] == ["speech from the left"]
    assert buf.snapshot() == []  # atomically cleared


# --------------------------------------------------------------------------- #
# The attach loop bounds (fake engine, no LLM)                                #
# --------------------------------------------------------------------------- #


def test_attach_loop_runs_one_turn_per_cue_bearing_event():
    buf = _RuntimeCueBuffer()
    engine = _FakeEngine(buf)
    lines = [
        _sense_line(doa=0.0, speech=True),
        _sense_line(doa=1.57, speech=False, rms=0.0),  # quiet → no cue → no turn
        _sense_line(doa=3.14, speech=True),
    ]
    stats = _run_attach_loop(lines, buf, engine, max_turns=None, max_events=None)
    assert stats == {"events": 3, "turns": 2}
    assert engine.turns == 2


def test_attach_loop_respects_max_turns():
    buf = _RuntimeCueBuffer()
    engine = _FakeEngine(buf)
    lines = [_sense_line(doa=0.0, speech=True) for _ in range(5)]
    stats = _run_attach_loop(lines, buf, engine, max_turns=1, max_events=None)
    assert stats == {"events": 1, "turns": 1}


def test_attach_loop_respects_max_events():
    buf = _RuntimeCueBuffer()
    engine = _FakeEngine(buf)
    lines = [_sense_line(doa=0.0, speech=True) for _ in range(5)]
    stats = _run_attach_loop(lines, buf, engine, max_turns=None, max_events=2)
    assert stats == {"events": 2, "turns": 2}


def test_attach_json_summary_when_no_export(capsys):
    buf_lines = [_sense_line(doa=0.0, speech=True)]
    args = _make_attach_args(json=True, max_events=1)
    cmd_agent_attach(
        args, lines=buf_lines, engine_factory=lambda buffer, export: _FakeEngine(buffer, export)
    )
    out = capsys.readouterr()
    payload = json.loads(out.out)
    assert payload == {"status": "ok", "events": 1, "turns": 1}


# --------------------------------------------------------------------------- #
# _open_feed                                                                  #
# --------------------------------------------------------------------------- #


def test_open_feed_reads_stdin():
    src = io.StringIO('{"t":"sense"}\n{"t":"rule"}\n')
    assert list(_open_feed("-", stdin=src)) == ['{"t":"sense"}\n', '{"t":"rule"}\n']


def test_open_feed_reads_a_file(tmp_path):
    p = tmp_path / "feed.jsonl"
    p.write_text('{"t":"sense"}\n', encoding="utf-8")
    assert list(_open_feed(str(p))) == ['{"t":"sense"}\n']


def test_open_feed_missing_path_is_a_clean_env_error():
    lines = _open_feed("/no/such/feed.jsonl")  # generator — nothing raised yet
    with pytest.raises(CliError) as exc:
        list(lines)
    assert exc.value.code == EXIT_ENV_ERROR
    assert "cannot open runtime feed" in exc.value.message


# --------------------------------------------------------------------------- #
# Criterion 1 — attach reads runtime events + acts through the intent tools   #
# --------------------------------------------------------------------------- #


def test_attach_acts_through_intent_tools_no_unit_edit_no_restart(tmp_path):
    """The client, driven by a runtime event, calls an intent tool → an atomic
    command lands in the SAME intents-namespaced spool a running engine drains.
    No systemd unit is touched and the loop is never restarted — the spool write
    is the whole mechanism (the engine picks it up on its next tick)."""
    turn_fn = _scripted_turn_fn(
        TurnResult(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name=DECLARE_GOAL,
                    arguments={"goal": "nod"},
                    arguments_json='{"goal": "nod"}',
                )
            ],
        )
    )

    def factory(buffer, export):
        return agent_mod._build_default_engine(
            buffer, export, spool_dir=tmp_path, await_timeout=0.0, turn_fn=turn_fn
        )

    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    rc = cmd_agent_attach(args, lines=[_sense_line(doa=0.0, speech=True)], engine_factory=factory)
    assert rc == 0

    # The agent's action is an atomic write in the intents spool — the running
    # engine's IntentDriver would drain exactly this on its next tick.
    cmds = CommandSpool(namespace=INTENT_NAMESPACE, root=tmp_path).drain()
    assert [c["op"] for c in cmds] == [DECLARE_GOAL]
    assert cmds[0]["goal"] == "nod"


# --------------------------------------------------------------------------- #
# Criterion 2 — the agent publishes its OWN thinking/message/emotion feed      #
# --------------------------------------------------------------------------- #


def test_attach_publishes_only_cognition_blocks_never_runtime_blocks(tmp_path):
    """The client's OUTPUT feed carries thinking/message/emotion (the SAME
    exporter think/listen use) and NEVER a runtime block — the c27 split."""
    turn_fn = _scripted_turn_fn(
        TurnResult(
            content="I should greet them",
            tool_calls=[
                ToolCall(
                    id="s1",
                    name="speak",
                    arguments={"text": "Hello there"},
                    arguments_json='{"text": "Hello there"}',
                ),
                ToolCall(
                    id="p1",
                    name="apply_pose",
                    arguments={"emoji": "🙂"},
                    arguments_json='{"emoji": "🙂"}',
                ),
            ],
        )
    )

    def factory(buffer, export):
        return agent_mod._build_default_engine(
            buffer, export, spool_dir=tmp_path, await_timeout=0.0, turn_fn=turn_fn
        )

    sink = io.StringIO()
    args = _make_attach_args(export="-", max_events=1)
    cmd_agent_attach(
        args, lines=[_sense_line(doa=0.0, speech=True)], engine_factory=factory, stream=sink
    )

    blocks = [json.loads(line) for line in sink.getvalue().splitlines() if line.strip()]
    kinds = {b["t"] for b in blocks}
    assert kinds == {"thinking", "message", "emotion"}
    # Never a runtime block on the cognition feed.
    assert kinds.isdisjoint({"sense", "rule", "intent", "motion"})
    message = next(b for b in blocks if b["t"] == "message")
    assert message["text"] == "Hello there"
    thinking = next(b for b in blocks if b["t"] == "thinking")
    assert thinking["cues"] == ["speech from the left"]


def test_attach_export_keeps_stdout_pure_summary_to_stderr(capsys, tmp_path):
    """Under --export the summary must not pollute the JSONL stdout feed."""

    def factory(buffer, export):
        return _FakeEngine(buffer, export)

    args = _make_attach_args(export="-", max_events=1)
    cmd_agent_attach(args, lines=[_sense_line(doa=0.0, speech=True)], engine_factory=factory)
    out = capsys.readouterr()
    # FakeEngine emits nothing to the feed; stdout stays empty, summary is on stderr.
    assert out.out == ""
    assert "detached" in out.err


# --------------------------------------------------------------------------- #
# Criterion 3 — detaching changes nothing about the loop                      #
# --------------------------------------------------------------------------- #


def test_loop_ticks_and_rules_continue_with_no_client():
    """With NO agent client anywhere, a bounded real engine.run ticks the full
    count and its rules keep firing — the loop is entirely self-sufficient."""
    cfg = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "hear",
                    "when": {"field": "speech", "op": "is_true"},
                    "run": "nod",
                    "cooldown_s": 0.0,
                    "duration_s": 30.0,
                }
            ]
        }
    )
    events: list[dict] = []
    seam = compose_rule_seam(cfg, consumers=(events.append,))
    eng = Engine()
    sink = _FakeSink()

    ticks = engine_run(
        _FakeTransport(sink),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=12,
        engine=eng,
        control=CommandSpool(),
        sense=lambda _t: Sense(speech_detected=True),
        tick_seam=seam,
    )

    assert ticks == 12  # the loop ticked the full bounded count with no client
    assert sink.calls > 0  # it kept driving the robot
    # rules continued: at least one rule.fire event over the run
    assert any(e.get("type") == "rule.fire" and e.get("behavior") == "nod" for e in events)


def test_client_intents_land_then_loop_keeps_ticking_after_detach(tmp_path):
    """A client submits an intent while it runs, then STOPS; the engine drains the
    landed command, sustains it, and keeps ticking — detach changes nothing."""
    # --- Phase 1: the agent client acts (writes a declare_goal to the spool) ---
    turn_fn = _scripted_turn_fn(
        TurnResult(
            content="",
            tool_calls=[
                ToolCall(
                    id="c1",
                    name=DECLARE_GOAL,
                    arguments={"goal": "nod"},
                    arguments_json='{"goal": "nod"}',
                )
            ],
        )
    )

    def factory(buffer, export):
        return agent_mod._build_default_engine(
            buffer, export, spool_dir=tmp_path, await_timeout=0.0, turn_fn=turn_fn
        )

    args = _make_attach_args(spool_dir=str(tmp_path), max_events=1)
    cmd_agent_attach(args, lines=[_sense_line(doa=0.0, speech=True)], engine_factory=factory)
    # The command is on disk (NOT drained here — phase 2's engine drains it); the
    # client process has now returned (detached).
    pending = list(control_mod.commands_dir(INTENT_NAMESPACE, root=tmp_path).glob("*.json"))
    assert len(pending) == 1

    # --- Phase 2: NO client running; the real loop drains + sustains + ticks ---
    eng = Engine()
    driver = IntentDriver(root=tmp_path)
    ticks = engine_run(
        _FakeTransport(),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=20,
        engine=eng,
        control=CommandSpool(root=tmp_path),
        tick_seam=driver,
    )

    assert ticks == 20  # the loop kept ticking after the client detached
    assert "nod" in {ab.behavior.name for ab in eng.active}  # the landed intent is sustained
    state = control_mod.read_state(root=tmp_path)
    assert state["intents"]["goal"]["name"] == "nod"


# --------------------------------------------------------------------------- #
# Overview + catalog (rubric surface)                                         #
# --------------------------------------------------------------------------- #


def test_overview_text_and_json(capsys):
    assert cmd_agent_overview(_make_attach_args()) == 0
    text = capsys.readouterr().out
    assert "reachy-mini-cli agent" in text
    assert "--transport" in text  # documents the NO-transport convention

    assert cmd_agent_overview(_make_attach_args(json=True)) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli agent"
    assert len(payload["sections"]) == 3


def test_catalog_entries_resolve():
    for path in (("agent",), ("agent", "attach"), ("agent", "overview")):
        assert path in ENTRIES
        assert "agent" in ENTRIES[path]
