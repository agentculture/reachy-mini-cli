"""Qwen's rolling summary PRODUCER — the half task t4 deliberately left open.

Issue #155 / #154 decision c30, task t12. t4 built the plumbing: one shared
history, Gemma's ``m``-turn window as a strict suffix of Qwen's ``n``,
:meth:`~reachy.embody.engine.EmbodyTurnEngine.update_summary` with its
fail-closed character bound, and
:meth:`~reachy.embody.engine.EmbodyTurnEngine.mark_summary_stale` as the named
staleness state. It left the thing that CALLS them to this task.

What this module pins:

* **one summary, Qwen's, never regenerated per lane** (decision c30) — there is
  exactly one production caller of ``update_summary`` in the whole repo, and it
  is this producer;
* the producer folds **turns older than Gemma's window** into the rolling
  summary, and is triggered by the conversation moving on rather than by a
  timer alone;
* every failure — a dead worker LLM, an empty answer, an over-long answer the
  engine refuses — resolves to :meth:`~reachy.embody.engine.EmbodyTurnEngine.
  mark_summary_stale` and a named drop, **never an exception on the caller's
  thread and never a silent narrowing of Gemma's memory**;
* the producer's own LLM call carries **no layer context** — no stale marker,
  no scope, no window — so a summary can never quote the marker that says the
  summary could not be refreshed.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from reachy.embody.engine import (
    STALE_SUMMARY_MARKER,
    EmbodyModels,
    EmbodyTurnEngine,
    Limits,
)
from reachy.embody.summary import (
    DEFAULT_MAX_BACKLOG_TURNS,
    DEFAULT_MIN_NEW_TURNS,
    DEFAULT_POLL_INTERVAL_S,
    SUMMARY_SYSTEM_PROMPT,
    SummaryLimits,
    SummaryProducer,
    build_summary_prompt,
)
from reachy.speech.llm import TurnResult

_REPO_ROOT = Path(__file__).resolve().parents[1]


class _Registry:
    def tools(self) -> list[dict]:
        return []

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": '{"ok": true}'}


class _RecordingTurn:
    def __init__(self, *results: TurnResult) -> None:
        self.results = list(results) or [TurnResult(content="a summary", finish_reason="stop")]
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **kwargs) -> TurnResult:
        self.calls.append([dict(m) for m in messages])
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]


def _engine(turn_fn=None, **limits) -> EmbodyTurnEngine:
    bounds = {"senses_history_maxlen": 2, "history_maxlen": 10, "min_alert_interval_s": 0.0}
    bounds.update(limits)
    return EmbodyTurnEngine(
        registry=_Registry(),
        turn_fn=turn_fn if turn_fn is not None else _RecordingTurn(),
        models=EmbodyModels(worker="w", senses="s"),
        limits=Limits(**bounds),
    )


def _talk(engine: EmbodyTurnEngine, turns: int) -> None:
    """Run *turns* worker turns so the shared history grows."""
    for index in range(turns):
        engine.submit_cue(f"a rule fired {index}", cue_class=_alert())
        assert engine.run_turn() is True


def _alert():
    from reachy.embody.cues import CueClass

    return CueClass.ALERT


class _Summarizer:
    """A recording stand-in for the worker LLM call."""

    def __init__(self, answer: str | Exception = "The operator asked about the kettle.") -> None:
        self.answer = answer
        self.prompts: list[str] = []

    def __call__(self, prompt: str) -> str:
        self.prompts.append(prompt)
        if isinstance(self.answer, Exception):
            raise self.answer
        return self.answer


# =========================================================================== #
# The trigger: turns older than Gemma's window, folded into ONE summary       #
# =========================================================================== #


def test_the_backlog_is_exactly_what_falls_outside_gemmas_window() -> None:
    engine = _engine(senses_history_maxlen=2, history_maxlen=10)
    _talk(engine, 5)

    assert len(engine.backlog()) == 3, "5 turns, Gemma sees the last 2"
    assert engine.backlog() == list(engine.history())[:3]


def test_the_producer_waits_until_the_conversation_has_actually_moved_on() -> None:
    engine = _engine()
    summarizer = _Summarizer()
    producer = SummaryProducer(engine, summarize=summarizer, limits=SummaryLimits(min_new_turns=4))

    _talk(engine, 3)
    assert producer.poll_once() is False, "three turns is not yet worth a gateway call"
    assert summarizer.prompts == []

    _talk(engine, 2)
    assert producer.poll_once() is True
    assert len(summarizer.prompts) == 1


def test_a_refreshed_summary_reaches_gemmas_context() -> None:
    turn_fn = _RecordingTurn()
    engine = _engine(turn_fn)
    producer = SummaryProducer(
        engine,
        summarize=_Summarizer("The operator asked about the kettle."),
        limits=SummaryLimits(min_new_turns=1),
    )
    _talk(engine, 4)
    assert producer.poll_once() is True

    turn_fn.calls.clear()
    engine.ask("what can you see?")
    rendered = "\n".join(str(m) for m in turn_fn.calls[0])
    assert "kettle" in rendered
    assert STALE_SUMMARY_MARKER not in rendered


def test_the_prompt_carries_the_backlog_and_the_previous_summary() -> None:
    engine = _engine()
    engine.update_summary("Earlier: the operator introduced themselves.")
    summarizer = _Summarizer()
    producer = SummaryProducer(engine, summarize=summarizer, limits=SummaryLimits(min_new_turns=1))
    _talk(engine, 4)
    assert producer.poll_once() is True

    prompt = summarizer.prompts[0]
    assert "introduced themselves" in prompt, "the rolling summary rolls"
    assert "a rule fired 0" in prompt, "the oldest backlog turn is what needs folding in"
    assert "a rule fired 3" not in prompt, "Gemma still sees the newest turns verbatim"


def test_the_prompt_names_the_engines_own_character_bound() -> None:
    """The producer asks for a summary the engine will accept, not one it refuses."""
    engine = _engine()
    prompt = build_summary_prompt(
        backlog=[("the operator said hello", "I waved")],
        previous="",
        max_chars=engine.summary_max_chars,
    )
    assert str(engine.summary_max_chars) in prompt


def test_the_backlog_folded_into_one_prompt_is_bounded() -> None:
    engine = _engine(senses_history_maxlen=1, history_maxlen=60)
    summarizer = _Summarizer()
    producer = SummaryProducer(
        engine,
        summarize=summarizer,
        limits=SummaryLimits(min_new_turns=1, max_backlog_turns=3),
    )
    _talk(engine, 12)
    assert producer.poll_once() is True

    prompt = summarizer.prompts[0]
    assert prompt.count("a rule fired") <= 3 * 2, "at most max_backlog_turns turns per prompt"


# =========================================================================== #
# Failure is NAMED, never silent and never fatal (spec c45 / honesty h30)     #
# =========================================================================== #


@pytest.mark.parametrize(
    "answer",
    [RuntimeError("the gateway is down"), "", "   "],
    ids=["worker-raised", "empty-answer", "blank-answer"],
)
def test_a_failed_maintenance_pass_marks_the_summary_stale_by_name(answer, caplog) -> None:
    engine = _engine()
    engine.update_summary("Earlier: the operator introduced themselves.")
    producer = SummaryProducer(
        engine, summarize=_Summarizer(answer), limits=SummaryLimits(min_new_turns=1)
    )
    _talk(engine, 4)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert producer.poll_once() is True

    assert engine.summary_is_stale is True
    assert engine.summary_stale_count == 1
    assert producer.failures == 1
    assert "dropped reason=summary-stale" in caplog.text
    assert (
        engine.summary == "Earlier: the operator introduced themselves."
    ), "a failed refresh must never erase what was last known"


def test_an_over_long_answer_is_refused_by_the_engine_and_marked_stale() -> None:
    engine = _engine()
    producer = SummaryProducer(
        engine,
        summarize=_Summarizer("x" * (engine.summary_max_chars + 1)),
        limits=SummaryLimits(min_new_turns=1),
    )
    _talk(engine, 4)
    assert producer.poll_once() is True

    assert engine.summary_is_stale is True
    assert engine.summary == "", "the engine refused it rather than truncating it"


def test_the_marker_clears_on_the_next_successful_pass() -> None:
    engine = _engine()
    summarizer = _Summarizer(RuntimeError("down"))
    producer = SummaryProducer(engine, summarize=summarizer, limits=SummaryLimits(min_new_turns=1))
    _talk(engine, 4)
    assert producer.poll_once() is True
    assert engine.summary_is_stale is True

    summarizer.answer = "The operator asked about the kettle."
    _talk(engine, 2)
    assert producer.poll_once() is True
    assert engine.summary_is_stale is False
    assert producer.updates == 1


def test_a_failed_pass_is_retried_rather_than_latched_off() -> None:
    """No permanent failure latch — the engine's own stance, inherited."""
    engine = _engine()
    producer = SummaryProducer(
        engine, summarize=_Summarizer(RuntimeError("down")), limits=SummaryLimits(min_new_turns=1)
    )
    for _ in range(3):
        _talk(engine, 3)
        assert producer.poll_once() is True
    assert producer.failures == 3


def test_the_producer_never_raises_on_the_callers_thread() -> None:
    class _SickEngine:
        turns = 99

        def backlog(self):
            raise RuntimeError("the history lock exploded")

    producer = SummaryProducer(_SickEngine(), summarize=_Summarizer())
    assert producer.poll_once() is False


# =========================================================================== #
# ONE summary, Qwen's — never regenerated per lane (decision c30)             #
# =========================================================================== #


def test_exactly_one_production_module_calls_update_summary() -> None:
    """c30, machine-checked: a second producer is a second summary to disagree."""
    callers = sorted(
        path.relative_to(_REPO_ROOT).as_posix()
        for path in (_REPO_ROOT / "reachy").rglob("*.py")
        if _calls(path, "update_summary")
    )
    assert callers == ["reachy/embody/summary.py"], callers


def test_the_producer_asks_the_worker_lane_and_carries_no_layer_context() -> None:
    """The summary call is Qwen's, and it starts from a clean sheet.

    Sending ``ask``'s usual context would feed the stale-summary MARKER back
    into the model that is meant to replace it — the summary would then quote
    the sentence saying it could not be refreshed.
    """
    turn_fn = _RecordingTurn()
    engine = _engine(turn_fn)
    engine.mark_summary_stale("the worker was down")
    engine.submit_cue("some background", cue_class=_alert())
    engine.run_turn()
    producer = SummaryProducer(engine, limits=SummaryLimits(min_new_turns=1))
    _talk(engine, 3)

    turn_fn.calls.clear()
    assert producer.poll_once() is True

    messages = turn_fn.calls[-1]
    rendered = "\n".join(str(m) for m in messages)
    assert STALE_SUMMARY_MARKER not in rendered
    assert messages[0] == {"role": "system", "content": SUMMARY_SYSTEM_PROMPT}
    assert [m["role"] for m in messages] == ["system", "user"]


def test_the_shipped_defaults_are_the_documented_ones() -> None:
    shipped = SummaryLimits()
    assert shipped.min_new_turns == DEFAULT_MIN_NEW_TURNS
    assert shipped.poll_interval_s == DEFAULT_POLL_INTERVAL_S
    assert shipped.max_backlog_turns == DEFAULT_MAX_BACKLOG_TURNS
    assert shipped.min_new_turns >= 1


def _calls(path: Path, attribute: str) -> bool:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == attribute and not _is_self(node.func.value):
                return True
    return False


def _is_self(node: ast.AST) -> bool:
    """``self.update_summary(...)`` is the engine defining it, not a caller."""
    return isinstance(node, ast.Name) and node.id == "self"
