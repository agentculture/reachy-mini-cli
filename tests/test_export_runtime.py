"""Tests for ``reachy.export.runtime`` — the behavior engine's OWN JSONL feed.

Task t8 (issue #70 / ``symbolic-runtime-70``). A SEPARATE contract from the
cognition feed (``reachy.export.events`` — ``thinking``/``message``/``emotion``,
produced by ``agent attach --export -``):
decision c27 says an attached agent publishes ITS OWN cognition feed through
that existing family, and this runtime feed carries ONLY the deterministic
engine's own events — perception, rule decisions, sustained intents, motion.

Covers:

1. Event model shapes — each of the four block types serializes via
   :func:`runtime_to_jsonl` with the documented keys.
2. ``to_runtime_event`` — maps the raw ``rule.fire``/``rule.suppress``/``sense``
   dicts a tick driver publishes via ``ctx.emit`` onto the event model; an
   unrecognised shape maps to ``None`` (forward-compatible).
3. ``parse_runtime_blocks`` — the ``--export-blocks`` CSV parser for this feed.
4. ``RuntimeConsumer`` — the ``TickBus``-shaped adapter: forwards mapped events
   to an injected sink, drops unmapped/malformed ones silently.
5. ``SenseSnapshotDriver`` — the ``TickBus``-shaped driver: emits a baseline
   ``"sense"`` event on the first tick, then only on a genuine change.
6. Stdout purity / disconnect-safety — reusing ``JsonlExporter`` with
   ``serialize=runtime_to_jsonl`` behaves exactly like the cognition feed's sink
   (a broken pipe never raises, self-disables after one warning).
7. The zero-LLM property — a bounded, rules-firing engine run's captured feed
   contains ONLY runtime event types; no type in this schema can represent an
   LLM call, and :data:`RUNTIME_BLOCKS` never overlaps the cognition feed's
   :data:`~reachy.export.blocks.BLOCKS`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import json
import logging
from dataclasses import dataclass, field

import pytest

from reachy.behavior import engine as E
from reachy.behavior import library as behavior_library
from reachy.behavior.engine import EngineConfig
from reachy.behavior.rule_engine import RuleEngine, TickBus, compose_rule_seam
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import EMPTY_SENSE, PatState, Sense
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.export.blocks import BLOCKS, Selection
from reachy.export.exporter import JsonlExporter
from reachy.export.runtime import (
    RUNTIME_BLOCKS,
    IntentEvent,
    MotionEvent,
    RuleEvent,
    RuntimeConsumer,
    SenseEvent,
    SenseSnapshotDriver,
    parse_runtime_blocks,
    runtime_to_jsonl,
    to_runtime_event,
)

SENSE_LOGGER = "reachy.sense"


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


class _CapturingSink:
    """A minimal ``.emit(event)`` sink that records everything it receives."""

    def __init__(self) -> None:
        self.events: list = []

    def emit(self, event) -> None:
        self.events.append(event)


@dataclass
class _Ctx:
    """A duck-typed TickContext exposing exactly what a driver needs."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    events: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)


def _react(rule_id, field_name, op, run, *, value=None, cooldown_s=5.0, hysteresis=0.0):
    when = {"field": field_name, "op": op}
    if value is not None:
        when["value"] = value
    rule = {
        "id": rule_id,
        "when": when,
        "run": run,
        "cooldown_s": cooldown_s,
        "hysteresis": hysteresis,
    }
    entry = behavior_library.LIBRARY.get(run)
    if entry is not None and entry.looping and entry.default_duration is None:
        # As of t4, an unbounded-looping target needs its own duration_s or
        # RulesConfig.from_dict refuses the rule fail-closed.
        rule["duration_s"] = 30.0
    return rule


# --------------------------------------------------------------------------- #
# 1. Event model shapes                                                       #
# --------------------------------------------------------------------------- #


class TestEventShapes:
    def test_sense_event_roundtrip(self):
        ev = SenseEvent(
            doa=1.5,
            speech=True,
            rms=0.2,
            pat=["scratch", "level1"],
            face="ada",
            frame_available=True,
            ts=10.0,
            tick=3,
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d == {
            "t": "sense",
            "ts": 10.0,
            "tick": 3,
            "doa": 1.5,
            "speech": True,
            "rms": 0.2,
            "pat": ["scratch", "level1"],
            "face": "ada",
            "frame_available": True,
        }

    def test_sense_event_defaults_are_none_safe(self):
        ev = SenseEvent(
            doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d["doa"] is None and d["pat"] is None and d["face"] is None

    def test_pat_state_serializes_as_a_parallel_object(self):
        ev = SenseEvent(
            doa=None,
            speech=False,
            rms=None,
            pat=["side_pat", "level1"],
            face=None,
            frame_available=False,
            pat_state={
                "availability": "available",
                "contact": True,
                "touch_type": "side_pat",
                "level": "level1",
                "yaw_deg": -3.25,
                "phase": "receptive",
                "phase_started_at": 10.0,
                "last_press_at": 10.4,
            },
        )

        d = json.loads(runtime_to_jsonl(ev))

        assert d["pat"] == ["side_pat", "level1"]
        assert d["pat_state"] == {
            "availability": "available",
            "contact": True,
            "touch_type": "side_pat",
            "level": "level1",
            "yaw_deg": -3.25,
            "phase": "receptive",
            "phase_started_at": 10.0,
            "last_press_at": 10.4,
            "blocked_reason": None,
        }

    def test_rule_event_fire_roundtrip(self):
        ev = RuleEvent(
            action="fire",
            rule="hear",
            kind="react",
            field="speech",
            op="is_true",
            reason="fired",
            behavior="nod",
            disable=[],
            ts=5.0,
            tick=7,
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d == {
            "t": "rule",
            "ts": 5.0,
            "tick": 7,
            "action": "fire",
            "rule": "hear",
            "kind": "react",
            "field": "speech",
            "op": "is_true",
            "reason": "fired",
            "behavior": "nod",
            "disable": [],
        }

    def test_rule_event_suppress_roundtrip(self):
        ev = RuleEvent(
            action="suppress",
            rule="hear",
            kind="react",
            field="speech",
            op="is_true",
            reason="cooldown",
            ts=6.0,
            tick=8,
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d["t"] == "rule"
        assert d["action"] == "suppress"
        assert d["behavior"] is None

    def test_intent_event_roundtrip(self):
        ev = IntentEvent(
            action="declare", name="stay-alert", payload={"mode": "focus"}, ts=1.0, tick=1
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d == {
            "t": "intent",
            "ts": 1.0,
            "tick": 1,
            "action": "declare",
            "name": "stay-alert",
            "payload": {"mode": "focus"},
        }

    def test_motion_event_roundtrip(self):
        ev = MotionEvent(
            action="admit",
            behavior="nod",
            channels=["head"],
            detail={"class": "stoppable"},
            ts=2.0,
            tick=2,
        )
        d = json.loads(runtime_to_jsonl(ev))
        assert d == {
            "t": "motion",
            "ts": 2.0,
            "tick": 2,
            "action": "admit",
            "behavior": "nod",
            "channels": ["head"],
            "detail": {"class": "stoppable"},
        }

    def test_no_embedded_newline(self):
        ev = SenseEvent(
            doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False
        )
        assert "\n" not in runtime_to_jsonl(ev)


# --------------------------------------------------------------------------- #
# 2. to_runtime_event mapping                                                 #
# --------------------------------------------------------------------------- #


class TestToRuntimeEvent:
    def test_rule_fire_maps_to_rule_event(self):
        raw = {
            "type": "rule.fire",
            "rule": "hear",
            "kind": "react",
            "field": "speech",
            "op": "is_true",
            "behavior": "nod",
            "disable": [],
            "reason": "fired",
            "ts": 1.0,
            "tick": 1,
        }
        mapped = to_runtime_event(raw)
        assert isinstance(mapped, RuleEvent)
        assert mapped.t == "rule"
        assert mapped.action == "fire"
        assert mapped.rule == "hear"
        assert mapped.behavior == "nod"

    def test_rule_suppress_maps_to_rule_event(self):
        raw = {
            "type": "rule.suppress",
            "rule": "hear",
            "kind": "react",
            "field": "speech",
            "op": "is_true",
            "reason": "cooldown",
            "ts": 1.0,
            "tick": 1,
        }
        mapped = to_runtime_event(raw)
        assert isinstance(mapped, RuleEvent)
        assert mapped.action == "suppress"
        assert mapped.reason == "cooldown"

    def test_inhibit_fire_carries_disable_list(self):
        raw = {
            "type": "rule.fire",
            "rule": "quiet",
            "kind": "inhibit",
            "field": "doa",
            "op": "is_true",
            "disable": ["nod", "gaze-hold"],
            "reason": "fired",
            "ts": 1.0,
            "tick": 1,
        }
        mapped = to_runtime_event(raw)
        assert mapped.disable == ["nod", "gaze-hold"]
        assert mapped.behavior is None

    def test_sense_dict_maps_to_sense_event(self):
        raw = {
            "type": "sense",
            "doa": 0.5,
            "speech": True,
            "rms": 0.1,
            "pat": ("scratch", "level1"),
            "face": "ada",
            "frame_available": True,
            "ts": 3.0,
            "tick": 4,
        }
        mapped = to_runtime_event(raw)
        assert isinstance(mapped, SenseEvent)
        assert mapped.pat == ["scratch", "level1"]  # tuple -> list

    def test_old_raw_sense_shape_parses_and_serializes_without_pat_state(self):
        raw = {
            "type": "sense",
            "doa": 0.5,
            "speech": False,
            "rms": None,
            "pat": ("scratch", "level1"),
            "face": None,
            "frame_available": False,
            "ts": 3.0,
            "tick": 4,
        }

        mapped = to_runtime_event(raw)

        assert isinstance(mapped, SenseEvent)
        assert mapped.pat_state is None
        encoded = json.loads(runtime_to_jsonl(mapped))
        assert "pat_state" not in encoded
        assert encoded["pat"] == ["scratch", "level1"]

    def test_unknown_pat_state_fields_are_ignored(self):
        raw = {
            "type": "sense",
            "pat_state": {
                "availability": "available",
                "contact": True,
                "touch_type": "side_pat",
                "level": "level1",
                "yaw_deg": 2.5,
                "phase": "receptive",
                "phase_started_at": 1.0,
                "last_press_at": 1.2,
                "future_strength": 9000,
            },
            "future_top_level": "ignored",
        }

        mapped = to_runtime_event(raw)

        assert isinstance(mapped, SenseEvent)
        assert mapped.pat_state == {
            "availability": "available",
            "contact": True,
            "touch_type": "side_pat",
            "level": "level1",
            "yaw_deg": 2.5,
            "phase": "receptive",
            "phase_started_at": 1.0,
            "last_press_at": 1.2,
            "blocked_reason": None,
        }

    def test_blocked_reason_is_carried_beside_availability(self):
        """Issue #168: a blocked reading's cause rides beside ``availability``."""
        mapped = to_runtime_event(
            {
                "type": "sense",
                "pat_state": {"availability": "blocked", "blocked_reason": "stillness"},
            }
        )

        assert isinstance(mapped, SenseEvent)
        assert mapped.pat_state["availability"] == "blocked"
        assert mapped.pat_state["blocked_reason"] == "stillness"

    @pytest.mark.parametrize("availability", ["available", "unavailable"])
    def test_blocked_reason_is_forced_none_outside_blocked(self, availability):
        """A reason paired with a non-``"blocked"`` availability is meaningless."""
        mapped = to_runtime_event(
            {
                "type": "sense",
                "pat_state": {"availability": availability, "blocked_reason": "ownership"},
            }
        )

        assert mapped.pat_state["availability"] == availability
        assert mapped.pat_state["blocked_reason"] is None

    @pytest.mark.parametrize("malformed", ["not-a-reason", 9000, [], None])
    def test_malformed_blocked_reason_degrades_to_none(self, malformed):
        mapped = to_runtime_event(
            {
                "type": "sense",
                "pat_state": {"availability": "blocked", "blocked_reason": malformed},
            }
        )

        assert mapped.pat_state["availability"] == "blocked"
        assert mapped.pat_state["blocked_reason"] is None

    @pytest.mark.parametrize("malformed", ["not-an-object", ["available"], 42])
    def test_malformed_pat_state_isolated_from_legacy_sense(self, malformed):
        mapped = to_runtime_event(
            {
                "type": "sense",
                "pat": ("scratch", "level1"),
                "pat_state": malformed,
            }
        )

        assert isinstance(mapped, SenseEvent)
        assert mapped.pat == ["scratch", "level1"]
        assert mapped.pat_state is None

    def test_malformed_pat_state_fields_degrade_independently(self):
        mapped = to_runtime_event(
            {
                "type": "sense",
                "pat": ("scratch", "level1"),
                "pat_state": {
                    "availability": [],
                    "contact": "yes",
                    "touch_type": {},
                    "level": [],
                    "yaw_deg": {},
                    "phase": [],
                    "phase_started_at": float("nan"),
                    "last_press_at": float("inf"),
                },
            }
        )

        assert isinstance(mapped, SenseEvent)
        assert mapped.pat == ["scratch", "level1"]
        assert mapped.pat_state == {
            "availability": "unavailable",
            "contact": False,
            "touch_type": None,
            "level": None,
            "yaw_deg": None,
            "phase": "idle",
            "phase_started_at": None,
            "last_press_at": None,
            "blocked_reason": None,
        }

    def test_unknown_type_maps_to_none(self):
        assert to_runtime_event({"type": "cognition.thinking", "ts": 1.0}) is None

    def test_missing_type_maps_to_none(self):
        assert to_runtime_event({"tick": 1, "ts": 1.0}) is None

    def test_non_string_type_maps_to_none(self):
        assert to_runtime_event({"type": 42}) is None

    def test_intent_declare_maps_to_intent_event(self):
        raw = {"type": "intent.declare", "name": "goal", "payload": {"x": 1}, "ts": 1.0, "tick": 1}
        mapped = to_runtime_event(raw)
        assert isinstance(mapped, IntentEvent)
        assert mapped.action == "declare"
        assert mapped.name == "goal"

    def test_unknown_intent_action_maps_to_none(self):
        assert to_runtime_event({"type": "intent.explode", "ts": 1.0}) is None

    def test_motion_admit_maps_to_motion_event(self):
        raw = {
            "type": "motion.admit",
            "behavior": "nod",
            "channels": ["head"],
            "ts": 1.0,
            "tick": 1,
        }
        mapped = to_runtime_event(raw)
        assert isinstance(mapped, MotionEvent)
        assert mapped.action == "admit"

    def test_unknown_motion_action_maps_to_none(self):
        assert to_runtime_event({"type": "motion.teleport", "ts": 1.0}) is None


# --------------------------------------------------------------------------- #
# 3. parse_runtime_blocks                                                     #
# --------------------------------------------------------------------------- #


class TestParseRuntimeBlocks:
    def test_single_block(self):
        sel = parse_runtime_blocks("rule")
        assert sel.allows("rule")
        assert not sel.allows("sense")

    def test_multiple_blocks(self):
        sel = parse_runtime_blocks("rule,sense")
        assert sel.allows("rule") and sel.allows("sense")
        assert not sel.allows("motion")

    def test_whitespace_and_dedup(self):
        sel = parse_runtime_blocks(" rule , rule ,sense ")
        assert sel.allows("rule") and sel.allows("sense")

    def test_empty_is_user_error(self):
        with pytest.raises(CliError) as exc:
            parse_runtime_blocks("   ")
        assert exc.value.code == EXIT_USER_ERROR

    def test_unknown_token_is_user_error(self):
        with pytest.raises(CliError) as exc:
            parse_runtime_blocks("thinking")  # a cognition-feed block, not a runtime one
        assert exc.value.code == EXIT_USER_ERROR
        assert "thinking" in exc.value.message

    def test_runtime_blocks_constant(self):
        assert RUNTIME_BLOCKS == ("sense", "rule", "intent", "motion")

    def test_runtime_blocks_disjoint_from_cognition_blocks(self):
        """The schema-level zero-LLM guarantee: no shared block type with the
        cognition feed, so a runtime event can never be mistaken for one."""
        assert set(RUNTIME_BLOCKS).isdisjoint(set(BLOCKS))


# --------------------------------------------------------------------------- #
# 4. RuntimeConsumer                                                          #
# --------------------------------------------------------------------------- #


class TestRuntimeConsumer:
    def test_forwards_mapped_event_to_sink(self):
        sink = _CapturingSink()
        consumer = RuntimeConsumer(sink)
        consumer(
            {
                "type": "rule.fire",
                "rule": "hear",
                "kind": "react",
                "field": "speech",
                "op": "is_true",
                "behavior": "nod",
                "reason": "fired",
                "ts": 1.0,
                "tick": 1,
            }
        )
        assert len(sink.events) == 1
        assert isinstance(sink.events[0], RuleEvent)

    def test_drops_unmapped_event_silently(self):
        sink = _CapturingSink()
        consumer = RuntimeConsumer(sink)
        consumer({"type": "cognition.thinking", "ts": 1.0})
        assert sink.events == []

    def test_malformed_event_never_raises(self):
        sink = _CapturingSink()
        consumer = RuntimeConsumer(sink)
        consumer(None)  # not even a dict
        consumer({"type": "rule.fire"})  # missing every other key -> defaults fill in
        assert sink.events == [] or isinstance(sink.events[0], RuleEvent)


# --------------------------------------------------------------------------- #
# 5. SenseSnapshotDriver                                                      #
# --------------------------------------------------------------------------- #


class TestSenseSnapshotDriver:
    def test_first_tick_always_emits_baseline(self):
        driver = SenseSnapshotDriver()
        ctx = _Ctx(now=0.1, tick=1, sense=EMPTY_SENSE)
        driver(ctx)
        assert len(ctx.events) == 1
        assert ctx.events[0]["type"] == "sense"
        assert ctx.events[0]["doa"] is None

    def test_unchanged_sense_does_not_re_emit(self):
        driver = SenseSnapshotDriver()
        ctx = _Ctx(now=0.1, tick=1, sense=Sense(doa_angle=1.0, speech_detected=True))
        driver(ctx)
        ctx.tick = 2
        ctx.now = 0.2
        driver(ctx)
        assert len(ctx.events) == 1  # only the first-tick baseline

    def test_change_triggers_a_new_emit(self):
        driver = SenseSnapshotDriver()
        ctx = _Ctx(now=0.1, tick=1, sense=Sense(doa_angle=1.0, speech_detected=False))
        driver(ctx)
        ctx.tick, ctx.now, ctx.sense = 2, 0.2, Sense(doa_angle=1.0, speech_detected=True)
        driver(ctx)
        assert len(ctx.events) == 2
        assert ctx.events[1]["speech"] is True
        assert ctx.events[1]["tick"] == 2

    def test_pat_event_serializes_as_list(self):
        driver = SenseSnapshotDriver()
        ctx = _Ctx(now=0.1, tick=1, sense=Sense(pat_event=("scratch", "level1")))
        driver(ctx)
        assert ctx.events[0]["pat"] == ["scratch", "level1"]

    def test_pat_state_serializes_without_changing_legacy_pat(self):
        state = PatState(
            availability="available",
            contact=True,
            touch_type="side_pat",
            level="level1",
            yaw_deg=-3.25,
            phase="receptive",
            phase_started_at=10.0,
            last_press_at=10.4,
        )
        driver = SenseSnapshotDriver()
        ctx = _Ctx(
            now=10.5,
            tick=1,
            sense=Sense(pat_event=("side_pat", "level1"), pat_state=state),
        )

        driver(ctx)

        assert ctx.events[0]["pat"] == ["side_pat", "level1"]
        assert ctx.events[0]["pat_state"] == {
            "availability": "available",
            "contact": True,
            "touch_type": "side_pat",
            "level": "level1",
            "yaw_deg": -3.25,
            "phase": "receptive",
            "phase_started_at": 10.0,
            "last_press_at": 10.4,
            "blocked_reason": None,
        }

    def test_stable_pat_hold_emits_once_across_fifty_hz_ticks(self):
        state = PatState(
            availability="available",
            contact=True,
            touch_type="scratch",
            level="level1",
            phase="receptive",
            phase_started_at=1.0,
            last_press_at=1.2,
        )
        driver = SenseSnapshotDriver()
        ctx = _Ctx(sense=Sense(pat_state=state))

        for tick in range(1, 101):
            ctx.tick = tick
            ctx.now = tick / 50.0
            driver(ctx)

        assert len(ctx.events) == 1

    def test_each_meaningful_pat_transition_emits_once(self):
        state = PatState(
            availability="available",
            contact=True,
            touch_type="side_pat",
            level="level1",
            yaw_deg=-2.0,
            phase="receptive",
            phase_started_at=1.0,
            last_press_at=1.2,
        )
        transitions = [
            dataclasses.replace(state, last_press_at=1.5),
            dataclasses.replace(state, yaw_deg=2.0),
            dataclasses.replace(state, level="level2"),
            dataclasses.replace(state, phase="contentment", phase_started_at=4.0),
            dataclasses.replace(state, availability="blocked"),
            dataclasses.replace(state, contact=False, phase="released", phase_started_at=5.0),
        ]
        driver = SenseSnapshotDriver()
        ctx = _Ctx(sense=Sense(pat_state=state))
        driver(ctx)

        for tick, changed in enumerate(transitions, start=2):
            ctx.tick = tick
            ctx.now = tick / 10.0
            ctx.sense = Sense(pat_state=changed)
            driver(ctx)
            driver(ctx)  # same transition never emits twice

        assert len(ctx.events) == 1 + len(transitions)


# --------------------------------------------------------------------------- #
# 6. Stdout purity / disconnect-safety via JsonlExporter reuse                #
# --------------------------------------------------------------------------- #


class TestExporterReuse:
    def test_writes_runtime_serialized_lines(self):
        buf = io.StringIO()
        exporter = JsonlExporter(buf, Selection(RUNTIME_BLOCKS), serialize=runtime_to_jsonl)
        exporter.emit(
            SenseEvent(doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False)
        )
        line = buf.getvalue().strip()
        obj = json.loads(line)
        assert obj["t"] == "sense"

    def test_broken_pipe_does_not_raise_and_disables(self, capsys):
        class _Raising:
            def write(self, *_a, **_kw):
                raise BrokenPipeError("broken")

            def flush(self, *_a, **_kw):
                pass

        exporter = JsonlExporter(_Raising(), Selection(RUNTIME_BLOCKS), serialize=runtime_to_jsonl)
        ev = SenseEvent(
            doa=None, speech=False, rms=None, pat=None, face=None, frame_available=False
        )
        exporter.emit(ev)  # must not raise
        exporter.emit(ev)  # must not raise, and must be silent now
        captured = capsys.readouterr()
        warning_lines = [ln for ln in captured.err.splitlines() if ln.strip()]
        assert len(warning_lines) == 1


# --------------------------------------------------------------------------- #
# 7. Zero-LLM property — a bounded, rules-firing engine run                   #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt: float = 0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def test_rules_firing_run_feed_is_all_runtime_types(caplog) -> None:
    """A bounded, rules-firing run's captured feed contains ONLY runtime event
    types — proving the zero-LLM/zero-token property straight from the feed,
    with no reliance on inspecting logs for the absence of an LLM call."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    sink = _CapturingSink()
    bus = compose_rule_seam(cfg, drivers=[SenseSnapshotDriver()], consumers=[RuntimeConsumer(sink)])

    def sense(_t):
        return Sense(speech_detected=True)

    tr = _FakeTransport()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ticks = E.run(
            tr,
            EngineConfig(compose_hz=50, base_layer=True, settle=False),
            sleep=lambda *_: None,
            now=_Clock(),
            max_ticks=5,
            sense=sense,
            tick_seam=bus,
        )

    assert ticks == 5
    assert sink.events, "the feed must not be empty — a rule fired and sense was sampled"
    # Every captured event is one of the four runtime block types.
    for ev in sink.events:
        assert ev.t in RUNTIME_BLOCKS
    # Explicitly: no cognition block ever appears in this feed (decision c27).
    assert not any(ev.t in BLOCKS for ev in sink.events)
    # And at least one real rule fire made it through end to end.
    assert any(isinstance(ev, RuleEvent) and ev.action == "fire" for ev in sink.events)


def test_bus_isolates_a_raising_runtime_consumer() -> None:
    """A malformed/raising runtime consumer never breaks the tick loop (TickBus's
    own fault isolation, exercised with THIS module's consumer in the mix)."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})

    def boom(_event: dict) -> None:
        raise RuntimeError("boom")

    sink = _CapturingSink()
    bus = TickBus(
        drivers=[SenseSnapshotDriver(), RuleEngine(cfg)],
        consumers=[boom, RuntimeConsumer(sink)],
    )

    def sense(_t):
        return Sense(speech_detected=True)

    tr = _FakeTransport()
    ticks = E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=2,
        sense=sense,
        tick_seam=bus,
    )
    assert ticks == 2
    assert sink.events  # the good consumer still received events despite the boom
