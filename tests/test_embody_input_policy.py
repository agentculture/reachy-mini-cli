"""The embodiment layer's three-class input policy (issue #143b, task t7).

The measured defect this module exists to make unreproducible, live on
2026-08-02 with the bus bridged into ``--feed``::

    187 cues in ~40 s  ->  23 turns  ->  19 input-queue-full drops
    cue mix: 145 x "speech from the left/ahead/right", 44 x "loud sound",
             0 rule fires

Twenty-three streaming LLM calls in forty seconds and not one of them was
prompted by something the robot DECIDED — the whole flood was sense snapshots
the layer already perceives for itself through its own duplex ears. So the
policy is not a rate limit bolted on top; it is a statement about which
perceptions are worth interrupting a mind for:

* an **utterance** triggers a turn (a person is talking to the robot),
* an **alert** cue — a rule FIRE, the one thing the layer cannot learn on its
  own — triggers a turn,
* every **context** cue accumulates in a bounded, coalescing park that the
  next turn DRAINS and that never causes one.

Two arithmetic notes on the measured window, so nobody re-derives them
--------------------------------------------------------------------
The reported mix sums to 189 (145 + 44) against a reported total of 187. The
total is what both the spec's honesty condition and the acceptance criterion
name, so :data:`MEASURED_TOTAL_CUES` is authoritative here and the split is
carried at 145/42 — the shape of the flood (three speech bands plus a loud
band, over and over) is what the coalescer has to survive, not the last two
counts.

The replay drives REAL runtime feed lines through the real
:func:`reachy.embody.cues.classified_cues_for_line` and the real
:class:`~reachy.cli._commands.agent._CueReader`, never a hand-made list of
cue strings: the defect was an intake-chain property, and a replay that skips
the chain could pass while the chain still floods.
"""

from __future__ import annotations

import dataclasses
import json
import math
import threading
import time

import pytest

import reachy.cli._commands.agent as agent_mod
from reachy.embody import engine as engine_mod
from reachy.embody.cues import ClassifiedCue, CueClass
from reachy.embody.engine import (
    DEFAULT_MIN_ALERT_INTERVAL_S,
    DEFAULT_PERCEPTION_STALE_AFTER_S,
    REASON_CONTEXT_PARK_FULL,
    REASON_INPUT_QUEUE_FULL,
    REASON_PERCEPTION_SOURCES_FULL,
    REASON_PERCEPTION_STALE,
    EmbodyModels,
    EmbodyTurnEngine,
    PerceptionSnapshot,
)
from reachy.export.exporter import ExportHook
from reachy.speech.llm import TurnResult
from tests.conftest import WAIT_BUDGET_S

# --------------------------------------------------------------------------- #
# The measured window                                                         #
# --------------------------------------------------------------------------- #

#: The total the spec's honesty condition and t7's acceptance criterion name.
MEASURED_TOTAL_CUES = 187
#: "speech from the left/ahead/right", the bulk of the flood.
MEASURED_SPEECH_CUES = 145
#: "loud sound left/ahead/right" — the balance (see the module docstring).
MEASURED_LOUD_CUES = MEASURED_TOTAL_CUES - MEASURED_SPEECH_CUES

#: DoA angles (radians, 0 = left, pi/2 = ahead, pi = right) landing squarely in
#: each of :func:`reachy.runtime_cues.direction_word`'s three bands.
_DOA_BANDS = (0.4, math.pi / 2.0, 2.7)

#: Every distinct cue text the measured window can produce — six facts, which
#: is the whole point: 187 arrivals, six things actually going on.
_WINDOW_FACTS = 6


def _sense_line(index: int, *, speech: bool) -> str:
    """One runtime ``sense`` feed line, exactly as the engine's exporter writes it."""
    event: dict = {
        "t": "sense",
        "ts": 100.0 + index * 0.2,
        "tick": index + 1,
        "doa": _DOA_BANDS[index % len(_DOA_BANDS)],
    }
    if speech:
        event["speech"] = True
    else:
        event["speech"] = False
        event["rms"] = 0.31
    return json.dumps(event)


def _measured_window_lines() -> list[str]:
    """The 40 s window as feed lines: 145 speech, 42 loud, 0 rule fires."""
    lines = [_sense_line(index, speech=True) for index in range(MEASURED_SPEECH_CUES)]
    lines += [
        _sense_line(MEASURED_SPEECH_CUES + index, speech=False)
        for index in range(MEASURED_LOUD_CUES)
    ]
    return lines


def _fire_line(rule: str, behavior: str = "nod") -> str:
    """One runtime ``rule`` fire line — the ONE alert class."""
    return json.dumps(
        {"t": "rule", "ts": 200.0, "tick": 1, "rule": rule, "action": "fire", "behavior": behavior}
    )


def _fire_text(rule: str, behavior: str = "nod") -> str:
    """The cue text :func:`reachy.runtime_cues.rule_cues` renders for that line."""
    return f"a behavior rule fired ({rule}): now doing {behavior}"


# --------------------------------------------------------------------------- #
# Doubles                                                                     #
# --------------------------------------------------------------------------- #


class _ScriptedTurn:
    """A ``turn_fn`` double recording each call's user content.

    ``on_call`` runs BEFORE the result is returned — i.e. while the engine
    still holds ``_turn_lock`` — which is how the burst test makes ten rule
    fires arrive "inside one turn window" without a second thread.
    """

    def __init__(self, *results: TurnResult, on_call=None) -> None:
        self._results = list(results) or [TurnResult(content="ok", finish_reason="stop")]
        self._on_call = on_call
        self.user_contents: list[str] = []
        self.calls = 0

    def __call__(self, messages: list[dict], **_kwargs) -> TurnResult:
        self.calls += 1
        for message in reversed(messages):
            if message.get("role") == "user":
                self.user_contents.append(message.get("content") or "")
                break
        if self._on_call is not None:
            self._on_call()
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    @property
    def last_user_content(self) -> str:
        return self.user_contents[-1] if self.user_contents else ""


class _Registry:
    def tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "speak", "parameters": {}}}]

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": '{"ok": true}'}


class _Sink:
    def __init__(self) -> None:
        self.events: list[object] = []

    def emit(self, event: object) -> None:
        self.events.append(event)

    def hook(self) -> ExportHook:
        return ExportHook(emit=self.emit, pose_resolver={}.get, time_fn=lambda: 1234.5)

    def of_type(self, block: str) -> list:
        return [event for event in self.events if getattr(event, "t", None) == block]


class _Clock:
    """A hand-wound monotonic clock: the alert interval is time, so inject it."""

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


#: :class:`~reachy.embody.engine.Limits` field names (issue #141/S107) — lets
#: this module's tests keep passing flat bounds (``max_context=2``) while the
#: constructor itself now takes only ``limits=``.
_LIMIT_FIELDS = {field.name for field in dataclasses.fields(engine_mod.Limits)}


def _build(**kwargs) -> EmbodyTurnEngine:
    """An engine on fakes, with its ATTENTION WINDOW already open (issue #148).

    The shipped gate starts cold and only the robot's name opens it, which is
    a question about WHO spoke; this module is about WHICH CLASS of perception
    is worth a turn, and its utterances stand in for "a person is talking to
    the robot". ``tests/test_embody_attention.py`` owns the other question —
    including the one place the two meet, that an ALERT still triggers a turn
    while attention is cold.
    """
    kwargs.setdefault("registry", _Registry())
    kwargs.setdefault("turn_fn", _ScriptedTurn())
    kwargs.setdefault("models", EmbodyModels(worker="worker", senses="senses"))
    limit_kwargs = {name: kwargs.pop(name) for name in list(kwargs) if name in _LIMIT_FIELDS}
    if limit_kwargs:
        kwargs.setdefault("limits", engine_mod.Limits(**limit_kwargs))
    engine = EmbodyTurnEngine(**kwargs)
    engine.attention.note_addressed()
    return engine


def _drain_lines(engine: EmbodyTurnEngine, lines: list[str]) -> agent_mod._CueReader:
    """Push *lines* through the REAL cue reader thread and wait for it to finish."""
    reader = agent_mod._CueReader(iter(lines), engine)
    reader.start()
    deadline = time.monotonic() + WAIT_BUDGET_S
    while time.monotonic() < deadline:
        if reader.done:
            return reader
        time.sleep(0.005)
    raise AssertionError("the cue reader never drained the replayed window")


# =========================================================================== #
# AC 1 — the measured window produces ZERO turns                              #
# =========================================================================== #


def test_replaying_the_measured_forty_second_window_produces_no_turns() -> None:
    """187 cues, 0 rule fires, 0 utterances -> 0 turns, 0 LLM calls, 0 drops."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)

    reader = _drain_lines(engine, _measured_window_lines())

    assert reader.events == MEASURED_TOTAL_CUES
    assert reader.cues == MEASURED_TOTAL_CUES, "every cue was accepted, none refused"
    assert engine.pending == 0, "not one of them is a trigger"
    assert engine.run_turn() is False
    assert engine.turns == 0
    assert turn.calls == 0, "the flood reached no model"
    assert engine.dropped_inputs == 0, "the flood is COALESCED, never dropped"
    assert engine.parked == _WINDOW_FACTS


# =========================================================================== #
# AC 2 — one following rule fire produces exactly one turn, carrying the park  #
# =========================================================================== #


def test_a_rule_fire_after_the_flood_produces_exactly_one_turn_carrying_the_context() -> None:
    sink = _Sink()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, export=sink.hook())

    _drain_lines(engine, _measured_window_lines())
    _drain_lines(engine, [_fire_line("pat-acknowledge")])

    assert engine.pending == 1, "the fire is the only trigger"
    assert engine.run_turn() is True
    assert engine.run_turn() is False, "exactly one turn, not one per parked fact"
    assert turn.calls == 1

    content = turn.last_user_content
    assert _fire_text("pat-acknowledge") in content
    assert "speech from the left (x" in content, "the flood arrives coalesced, with a count"

    thinking = sink.of_type("thinking")
    assert len(thinking) == 1
    assert f"context={_WINDOW_FACTS} coalesced-from={MEASURED_TOTAL_CUES}" in thinking[0].text
    assert engine.parked == 0, "the turn that showed the park drained it"


# =========================================================================== #
# AC 3 — a burst of ten fires inside one turn window costs at most two turns   #
# =========================================================================== #


def test_a_burst_of_ten_rule_fires_inside_one_turn_window_produces_at_most_two_turns() -> None:
    """``cooldown_s=0`` is legal and several rules fire per tick — contain it.

    The nine remaining fires land while the first turn is IN FLIGHT (the
    ``turn_fn`` submits them, so the engine is holding ``_turn_lock``), which
    is the shape the challenge finding named. The clock is wound past the
    minimum interval between turns on purpose: this test measures the
    COALESCING alone, so it cannot pass by accident on the rate limit.
    """
    clock = _Clock()
    burst = [f"rule-{index}" for index in range(10)]
    engine: EmbodyTurnEngine | None = None

    def _rest_of_the_burst_arrives() -> None:
        for rule in burst[1:]:
            engine.submit_cue(_fire_text(rule), cue_class=CueClass.ALERT)

    turn = _ScriptedTurn(on_call=_rest_of_the_burst_arrives)
    engine = _build(turn_fn=turn, now_fn=clock)
    engine.submit_cue(_fire_text(burst[0]), cue_class=CueClass.ALERT)

    turns = 0
    while engine.run_turn():
        turns += 1
        clock.advance(DEFAULT_MIN_ALERT_INTERVAL_S * 2)
        assert turns <= 3, "the burst is spawning a turn per fire again"
        turn._on_call = None

    assert turns == 2
    seen = "\n".join(turn.user_contents)
    for rule in burst:
        assert _fire_text(rule) in seen, f"{rule} never reached the model"


def test_a_minimum_interval_bounds_alert_triggered_turns() -> None:
    """A held-back alert is DEFERRED, never dropped: it rides the next turn."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock, min_alert_interval_s=5.0)

    engine.submit_cue(_fire_text("first"), cue_class=CueClass.ALERT)
    assert engine.run_turn() is True

    clock.advance(1.0)
    engine.submit_cue(_fire_text("second"), cue_class=CueClass.ALERT)
    assert engine.run_turn() is False, "inside the interval, an alert waits"
    assert engine.pending == 1, "and is still pending — deferred, not dropped"
    assert engine.dropped_inputs == 0

    clock.advance(5.0)
    assert engine.run_turn() is True
    assert _fire_text("second") in turn.last_user_content


def test_an_utterance_is_never_held_back_by_the_alert_interval() -> None:
    """A person talking outranks the rate limit, and a waiting alert rides along."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock, min_alert_interval_s=5.0)

    engine.submit_cue(_fire_text("first"), cue_class=CueClass.ALERT)
    assert engine.run_turn() is True

    engine.submit_cue(_fire_text("second"), cue_class=CueClass.ALERT)
    engine.submit_utterance("are you listening?")
    assert engine.run_turn() is True

    content = turn.last_user_content
    assert 'heard: "are you listening?"' in content
    assert _fire_text("second") in content


# =========================================================================== #
# AC 4 — the drain is counted on the journal AND on the export feed            #
# =========================================================================== #


def test_the_turn_names_what_it_drained_on_the_journal_and_the_export_feed(caplog) -> None:
    """A silent coalescer is indistinguishable from a dropper."""
    sink = _Sink()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, export=sink.hook())

    for _ in range(3):
        engine.submit_cue("speech from the left")
    engine.submit_cue("loud sound ahead")
    engine.submit_cue(_fire_text("pat-acknowledge"), cue_class=CueClass.ALERT)

    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.run_turn() is True

    assert "triggers=1 context=2 coalesced-from=4" in caplog.text

    thinking = sink.of_type("thinking")[0]
    assert "triggers=1 context=2 coalesced-from=4" in thinking.text
    assert thinking.cues == [
        _fire_text("pat-acknowledge"),
        "speech from the left (x3)",
        "loud sound ahead",
    ]


def test_the_context_park_is_a_section_of_its_own_in_the_prompt() -> None:
    """Triggers and background are distinguishable to the model, not one list."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    engine.submit_cue("speech from the left")
    engine.submit_utterance("hello")
    engine.run_turn()

    content = turn.last_user_content
    assert content.index('heard: "hello"') < content.index("speech from the left")
    assert "in the background" in content


# =========================================================================== #
# The park's own discipline                                                   #
# =========================================================================== #


def test_a_context_cue_never_triggers_a_turn() -> None:
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    assert engine.submit_cue("a camera frame is available") is True
    assert engine.parked == 1
    assert engine.pending == 0
    assert engine.run_turn() is False
    assert turn.calls == 0


def test_context_coalesces_on_the_cue_text_and_carries_a_count() -> None:
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    for _ in range(145):
        engine.submit_cue("speech from the left")
    engine.submit_cue("speech from the right")
    assert engine.parked == 2

    engine.submit_utterance("hi")
    engine.run_turn()
    content = turn.last_user_content
    assert "speech from the left (x145)" in content
    assert "speech from the right" in content
    assert "(x1)" not in content, "a single sighting reads as a fact, not a tally"


def test_a_repeat_of_a_parked_fact_never_counts_against_the_bound() -> None:
    """The bound is on DISTINCT facts; a flood of one fact can never fill it."""
    engine = _build(max_context=2)
    for _ in range(500):
        assert engine.submit_cue("speech from the left") is True
    assert engine.parked == 1
    assert engine.dropped_inputs == 0


def test_the_context_park_is_bounded_and_names_its_drop(caplog) -> None:
    engine = _build(max_context=2)
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.submit_cue("one") is True
        assert engine.submit_cue("two") is True
        assert engine.submit_cue("three") is False

    assert engine.parked == 2
    assert engine.dropped_inputs == 1
    assert REASON_CONTEXT_PARK_FULL in caplog.text


def test_a_context_flood_never_touches_the_trigger_bound(caplog) -> None:
    """Two buffers, two bounds, two names — the flood cannot starve the ears."""
    engine = _build(max_pending=2, max_context=8)
    with caplog.at_level("INFO", logger="reachy.sense"):
        for index in range(500):
            engine.submit_cue(f"fact {index % 8}")
        assert engine.submit_utterance("can you hear me?") is True

    assert engine.pending == 1
    assert engine.dropped_inputs == 0
    assert REASON_INPUT_QUEUE_FULL not in caplog.text


def test_the_park_is_drained_by_the_turn_that_showed_it() -> None:
    """Otherwise every later turn re-reads the same ambient background forever."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    engine.submit_cue("speech from the left")
    engine.submit_utterance("first")
    engine.run_turn()

    engine.submit_utterance("second")
    engine.run_turn()
    assert "speech from the left" not in turn.last_user_content


def test_the_submit_path_is_safe_from_two_threads_at_once() -> None:
    """Intake and the duplex utterance tap both submit; the counts must add up."""
    engine = _build(max_context=8, max_pending=512)
    barrier = threading.Barrier(4)

    def _flood(worker: int) -> None:
        barrier.wait()
        for index in range(200):
            engine.submit_cue(f"fact {index % 4}")
            if index % 50 == 0:
                engine.submit_utterance(f"worker {worker} line {index}")

    threads = [threading.Thread(target=_flood, args=(worker,)) for worker in range(4)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=WAIT_BUDGET_S)
        assert not thread.is_alive(), "a submit blocked — the intake must never wait"

    assert engine.parked == 4
    assert engine.pending == 16
    assert engine.dropped_inputs == 0


# =========================================================================== #
# Kind-aware park — free-text perception is latest-wins, not text-keyed       #
# (issue #154, task t3)                                                       #
# =========================================================================== #
#
# Text-identity coalescing (the tests above) is exactly right for the closed
# cue vocabulary and exactly wrong for free-form perception text: two
# renderings of the same room share no key, so every changed clip poll used to
# add a NEW park entry, filling ``DEFAULT_MAX_CONTEXT`` within minutes and
# crowding out genuine runtime facts. ``submit_perception`` is a SEPARATE
# intake path — never a flag on ``submit_cue`` — so the closed-vocabulary path
# above needs no change at all, which is exactly what AC2 (this task's other
# half) pins by staying passing untouched.

#: Roughly one hour of a 20 s clip-poll cadence (issue #139's ``_ClipAsker``):
#: 3600 / 20 = 180 arrivals, every one a DIFFERENT description.
_HOUR_OF_PERCEPTION_UPDATES = 180


def _room_description(index: int) -> str:
    """A free-text perception update that is NEVER byte-identical to another.

    Mirrors the spec's own example (issue #154): "a kitchen with someone at
    the counter" vs "a kitchen, a person near the counter" — same fact, no
    shared key.
    """
    return f"a kitchen, frame {index}: someone near the counter, cup #{index % 7} in hand"


def test_an_hour_of_free_text_perception_updates_occupies_one_slot() -> None:
    """AC1: 180 distinct descriptions over one simulated hour -> ONE park slot."""
    engine = _build()
    for index in range(_HOUR_OF_PERCEPTION_UPDATES):
        assert engine.submit_perception(_room_description(index)) is True

    assert engine.parked == 1, "one source, one slot, no matter how many updates"
    assert engine.dropped_inputs == 0, "a latest-wins slot never refuses a replacement"


def test_the_parked_slot_shows_only_the_latest_description() -> None:
    """The turn a person's question drains reads the CURRENT room, not the log of it."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    for index in range(_HOUR_OF_PERCEPTION_UPDATES):
        engine.submit_perception(_room_description(index))
    engine.submit_utterance("what do you see?")
    engine.run_turn()

    content = turn.last_user_content
    assert _room_description(_HOUR_OF_PERCEPTION_UPDATES - 1) in content
    assert _room_description(0) not in content, "the first sighting was replaced, not kept"
    assert _room_description(90) not in content, "no sighting survives but the latest"


def test_free_text_perception_never_triggers_a_turn() -> None:
    """Perception enters as CONTEXT and structurally cannot become a trigger."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)
    assert engine.submit_perception("a kitchen, empty") is True
    assert engine.parked == 1
    assert engine.pending == 0
    assert engine.run_turn() is False
    assert turn.calls == 0


def test_every_runtime_fact_class_remains_representable_after_a_perception_flood() -> None:
    """AC1's other half: the flood must never crowd out a genuine runtime fact.

    A tight ``max_context`` (3) stands in for the closed cue vocabulary's own
    budget; flooding hundreds of DIFFERENT perception descriptions first must
    leave the full budget available to sense/intent/motion/rule-suppression
    facts, because they are parked in a completely separate dict.
    """
    engine = _build(max_context=3)
    for index in range(500):
        assert engine.submit_perception(_room_description(index)) is True

    # One representative cue per runtime-fact class (reachy.runtime_cues'
    # own rendered phrasing for sense / rule-suppress / intent / motion).
    runtime_facts = [
        "a camera frame is available",  # sense
        "a behavior rule held off (pat-acknowledge)",  # rule suppression
        "a standing intent was set: greet",  # intent
        "started moving: nod",  # motion
    ]
    for fact in runtime_facts[:3]:
        assert engine.submit_cue(fact) is True, f"{fact!r} was refused despite the flood"
    # The 4th distinct cue is refused on the SAME bound as always — max_context
    # is still exactly 3, proving the flood neither shrank nor grew it.
    assert engine.submit_cue(runtime_facts[3]) is False

    assert engine.parked == 4, "1 perception slot + 3 admitted runtime facts"


def test_perception_sources_get_independent_slots() -> None:
    """'One slot per perception source' — a second source is a second slot."""
    engine = _build()
    for index in range(50):
        engine.submit_perception(_room_description(index), source="vision")
    for index in range(50):
        engine.submit_perception(f"ambient hum, sample {index}", source="audio-scene")

    assert engine.parked == 2


def test_the_perception_park_is_bounded_by_distinct_sources_and_names_its_drop(caplog) -> None:
    """A genuinely new SOURCE, not a new description, is the only thing that fills it."""
    engine = _build(max_perception_sources=2)
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.submit_perception("a kitchen, empty", source="vision") is True
        assert engine.submit_perception("quiet room", source="audio-scene") is True
        assert engine.submit_perception("a third source", source="thermal") is False

    assert engine.parked == 2
    assert engine.dropped_inputs == 1
    assert REASON_PERCEPTION_SOURCES_FULL in caplog.text
    # And neither existing slot was disturbed by the refusal.
    assert engine.submit_perception("still empty", source="vision") is True
    assert engine.parked == 2


def test_a_perception_replacement_is_visible_in_the_coalesced_accounting(caplog) -> None:
    """A silent coalescer is indistinguishable from a dropper — replacements included.

    Mirrors ``test_the_turn_names_what_it_drained_on_the_journal_and_the_export_feed``
    for the new coalescing key: the perception slot's ``count`` keeps rising on
    every REPLACEMENT (not just every repeat), so it still adds its share to
    ``coalesced-from``, and its rendering marks an update apart from a repeat.
    """
    sink = _Sink()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, export=sink.hook())

    for _ in range(3):
        engine.submit_cue("speech from the left")
    engine.submit_cue("loud sound ahead")
    for index in range(3):
        engine.submit_perception(f"kitchen, state {index}")
    engine.submit_cue(
        "a behavior rule fired (pat-acknowledge): now doing nod", cue_class=CueClass.ALERT
    )

    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.run_turn() is True

    # 3 context entries: "speech from the left" (x3), "loud sound ahead" (x1),
    # the one perception slot (3 updates) -> coalesced-from = 3 + 1 + 3 = 7.
    assert "triggers=1 context=3 coalesced-from=7" in caplog.text

    thinking = sink.of_type("thinking")[0]
    assert "triggers=1 context=3 coalesced-from=7" in thinking.text
    assert "kitchen, state 2 (updated x3)" in thinking.cues, "the LATEST text, marked as updated"
    assert not any(
        "kitchen, state 0" in cue for cue in thinking.cues
    ), "a replaced text never lingers"
    assert "speech from the left (x3)" in thinking.cues, "the repeat-count phrasing is untouched"

    content = turn.last_user_content
    assert (
        "kitchen, state 2 (updated x3)" in content
    ), "the model's own prompt shows the update mark"


# =========================================================================== #
# Structured snapshots, fresh and latest-wins (issue #155 c7/h6, task t13)    #
# =========================================================================== #
#
# t3 built the latest-wins park and the ``PerceptionSlot`` coalescing key;
# t13 closes the two seams t3 deliberately left open: the park entry is now a
# structured ``PerceptionSnapshot`` (summary/entities/confidence/capture
# time/frame ref) rather than bare text, and the slot PERSISTS across turns
# until it is superseded or goes stale, instead of being drained by the one
# turn that happened to run right after a poll. The closed-vocabulary cue
# park above is untouched by any of this — every test in this section only
# ever calls ``submit_perception``.


def _snapshot(summary: str, *, clock: _Clock, **kwargs) -> PerceptionSnapshot:
    """A convenience builder stamping ``captured_at`` from the test's own clock."""
    kwargs.setdefault("captured_at", clock.now)
    return PerceptionSnapshot(summary=summary, **kwargs)


def test_a_structured_snapshot_carries_its_fields_into_the_turn() -> None:
    """AC1: summary, entities and confidence all reach the model's context."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)
    snapshot = _snapshot(
        "a kitchen, someone at the counter",
        clock=clock,
        entities=("person", "counter"),
        confidence=0.82,
        frame_ref="/state/clip.mp4",
    )

    assert engine.submit_perception(snapshot) is True
    engine.submit_utterance("what do you see?")
    assert engine.run_turn() is True

    content = turn.last_user_content
    assert "a kitchen, someone at the counter" in content
    assert "entities: person, counter" in content
    assert "confidence=0.82" in content
    assert "/state/clip.mp4" not in content, "the frame path is attribution, never narrated prose"


def test_a_perception_snapshot_persists_across_multiple_turns_until_superseded() -> None:
    """AC2's core: a turn between two clip polls still sees the room (closes #153)."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)
    engine.submit_perception(_snapshot("a kitchen, empty", clock=clock))

    engine.submit_utterance("first question")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content, "the FIRST turn sees the snapshot"

    # No new clip arrived — the OLD cue-park behaviour would have drained the
    # slot on the first turn and left the second with nothing at all.
    clock.advance(5.0)
    engine.submit_utterance("second question")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content, "the SECOND turn still sees it"

    # A later poll supersedes it — the OLD text must never linger beside the new.
    clock.advance(1.0)
    engine.submit_perception(_snapshot("a kitchen, someone arrived", clock=clock))
    engine.submit_utterance("third question")
    assert engine.run_turn() is True
    content = turn.last_user_content
    assert "a kitchen, someone arrived" in content
    assert "a kitchen, empty" not in content, "the superseded snapshot never lingers"


def test_a_stale_perception_snapshot_expires_and_is_absent_from_the_next_turn(caplog) -> None:
    """AC2's other half: a stale snapshot expires rather than lingers."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)
    engine.submit_perception(_snapshot("a kitchen, empty", clock=clock))
    assert engine.parked == 1

    clock.advance(DEFAULT_PERCEPTION_STALE_AFTER_S + 1.0)
    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.parked == 0, "the stale slot is evicted on read, not merely hidden"
    assert REASON_PERCEPTION_STALE in caplog.text

    engine.submit_utterance("what do you see?")
    assert engine.run_turn() is True
    assert "a kitchen, empty" not in turn.last_user_content


def test_a_perception_snapshot_within_the_freshness_window_survives_a_gap() -> None:
    """The boundary case: a gap that has NOT yet crossed the staleness bound."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)
    engine.submit_perception(_snapshot("a kitchen, empty", clock=clock))

    clock.advance(DEFAULT_PERCEPTION_STALE_AFTER_S - 1.0)
    engine.submit_utterance("what do you see?")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content, "still fresh, one second inside the bound"


def test_zero_disables_the_perception_staleness_bound() -> None:
    """Mirrors ``min_alert_interval_s``/``attention_window_s``'s own zero-disables convention."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock, perception_stale_after_s=0.0)
    engine.submit_perception(_snapshot("a kitchen, empty", clock=clock))

    clock.advance(DEFAULT_PERCEPTION_STALE_AFTER_S * 100)
    engine.attention.note_addressed()  # re-open attention; this test is about PERCEPTION staleness
    engine.submit_utterance("what do you see?")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content


def test_a_bare_string_is_still_accepted_and_never_expires() -> None:
    """Backward compatibility: a caller with no structure to give still works.

    A bare string is wrapped into a summary-only snapshot stamped with the
    engine's OWN clock at intake, so it behaves exactly like every other
    persisted snapshot — it is not a second, timeless code path.
    """
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)
    assert engine.submit_perception("a kitchen, empty") is True

    engine.submit_utterance("first")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content

    clock.advance(5.0)
    engine.submit_utterance("second")
    assert engine.run_turn() is True
    assert "a kitchen, empty" in turn.last_user_content, "a bare string persists too"


def test_the_offline_what_can_you_see_flow_reads_the_latest_valid_snapshot() -> None:
    """AC2, end to end: the mechanical shape of closing issue #153 offline.

    Mirrors the exact defect t1 reproduced (asked what it can see with no
    media/context attached, the senses model claimed blindness): here, the
    WORKER lane's context is what a person's question reads, and it must
    carry the room description from the latest snapshot the clip asker
    parked — never nothing, and never a snapshot that has gone stale.
    """
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn, now_fn=clock)

    # Two changed clip polls, exactly as ``_ClipAsker`` would submit them.
    engine.submit_perception(_snapshot("a kitchen, someone is cooking", clock=clock))
    clock.advance(20.0)
    engine.submit_perception(_snapshot("a kitchen, someone is at the counter", clock=clock))

    engine.submit_utterance("what can you see?")
    assert engine.run_turn() is True
    content = turn.last_user_content
    assert "a kitchen, someone is at the counter" in content, "answers from the LATEST snapshot"
    assert "a kitchen, someone is cooking" not in content, "the superseded one never lingers"

    # The clip asker stops publishing (camera unplugged, process died, ...):
    # the last snapshot must eventually stop being offered as current.
    clock.advance(DEFAULT_PERCEPTION_STALE_AFTER_S + 1.0)
    engine.submit_utterance("what can you see now?")
    assert engine.run_turn() is True
    assert "a kitchen" not in turn.last_user_content, "a stale snapshot expires, never lingers"


# =========================================================================== #
# The intake routing — the class must survive the trip from the feed          #
# =========================================================================== #


def test_the_cue_reader_routes_each_line_by_its_class() -> None:
    """A sense line parks; a rule FIRE triggers; a rule SUPPRESSION parks."""
    turn = _ScriptedTurn()
    engine = _build(turn_fn=turn)

    _drain_lines(
        engine,
        [
            _sense_line(0, speech=True),
            json.dumps({"t": "rule", "ts": 1.0, "rule": "held", "action": "suppress"}),
            json.dumps({"t": "motion", "ts": 2.0, "action": "admit", "behavior": "nod"}),
        ],
    )
    assert engine.pending == 0
    assert engine.parked == 3

    _drain_lines(engine, [_fire_line("pat-acknowledge")])
    assert engine.pending == 1


def test_submit_cues_routes_classified_cues_and_bare_strings_park() -> None:
    engine = _build()
    accepted = engine.submit_cues(
        [
            ClassifiedCue(text=_fire_text("pat-acknowledge"), cue_class=CueClass.ALERT),
            ClassifiedCue(text="speech from the left", cue_class=CueClass.CONTEXT),
            "a camera frame is available",
        ]
    )
    assert accepted == 3
    assert engine.pending == 1
    assert engine.parked == 2


@pytest.mark.parametrize("cue_class", list(CueClass))
def test_every_cue_class_has_a_route(cue_class: CueClass) -> None:
    """A class the engine does not route would silently vanish."""
    engine = _build()
    assert engine.submit_cue("something happened", cue_class=cue_class) is True
    assert engine.pending + engine.parked == 1
