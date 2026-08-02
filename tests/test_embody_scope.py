"""Cognition scopes: Qwen's compact, typed, expiring influence on the foreground.

Issue #155, task t12 — spec claims c2/c8, honesty conditions h1/h7.

The architecture: the operator talks with **Gemma**, the foreground voice.
**Qwen** follows the conversation in the background and injects *compact
thinking scopes* — never raw reasoning, never speech. A scope carries a goal,
the facts that matter, a suggested next step, a priority, an expiry in turns,
and a speakable flag; Gemma may use it to shape its next response but **keeps
the wording and the decision to speak**.

The three things this module pins (t12's acceptance criteria 1 and 2):

1. the artifact carries goal / relevant facts / suggested next step / priority /
   expiry / speakable **with source attribution**, its size is **bounded and
   enforced**, and its **expiry is pinned** — a stale scope cannot shape a later
   turn (c8/h7);
2. **structurally**, raw model reasoning can never reach the foreground prompt
   builder — not by a field on the artifact, not off the wire, not through the
   conversation history (c8/h7);
3. a scope is **context, never a trigger** — the same no-self-wake asymmetry
   :mod:`reachy.embody.attention` and :class:`~reachy.embody.interjection.
   WantedToSay` already carry.

What this module does NOT claim: a scope is not a containment boundary. It is
bounded text in front of a mind, exactly as ``tests/test_embody_redteam.py``
says of the interjection family.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from reachy.embody import scope as scope_mod
from reachy.embody.engine import EmbodyModels, EmbodyTurnEngine, Limits
from reachy.embody.interjection import (
    Authorization,
    InterjectionLimits,
    InterjectionPolicy,
)
from reachy.embody.scope import (
    DEFAULT_EXPIRES_AFTER_TURNS,
    DEFAULT_MAX_FACTS,
    DEFAULT_PRIORITY,
    DEFAULT_SCOPE_SOURCE,
    PRIORITIES,
    REFUSAL_EXPIRY_TOO_LONG,
    REFUSAL_FACT_TOO_LONG,
    REFUSAL_GOAL_TOO_LONG,
    REFUSAL_MALFORMED,
    REFUSAL_NEXT_STEP_TOO_LONG,
    REFUSAL_TOO_LARGE,
    REFUSAL_TOO_MANY_FACTS,
    REFUSAL_UNATTRIBUTED,
    REFUSAL_UNKNOWN_PRIORITY,
    REFUSALS,
    SCOPE_TYPE,
    STAGE,
    CognitionScope,
    ScopeLimits,
    make_scope,
    scope_from_interjection,
)
from reachy.speech.llm import TurnResult

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: Every word a "raw reasoning" field could plausibly be called. A structural
#: test is only as good as the names it refuses, so the list is explicit and
#: shared by the several pins below.
_REASONING_WORDS = ("reasoning", "reasoning_content", "thinking", "chain_of_thought", "scratchpad")


# --------------------------------------------------------------------------- #
# Doubles                                                                     #
# --------------------------------------------------------------------------- #


class _Registry:
    def tools(self) -> list[dict]:
        return []

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": '{"ok": true}'}


class _RecordingTurn:
    """A ``turn_fn`` that records every message list it was handed."""

    def __init__(self, *results: TurnResult) -> None:
        self.results = list(results) or [TurnResult(content="ok", finish_reason="stop")]
        self.calls: list[list[dict]] = []

    def __call__(self, messages, **kwargs) -> TurnResult:
        self.calls.append([dict(m) for m in messages])
        return self.results[min(len(self.calls) - 1, len(self.results) - 1)]

    def rendered(self) -> str:
        """Every message of every call, flattened to one searchable string."""
        return "\n".join(str(message) for call in self.calls for message in call)


def _engine(turn_fn=None, **limits) -> EmbodyTurnEngine:
    return EmbodyTurnEngine(
        registry=_Registry(),
        turn_fn=turn_fn if turn_fn is not None else _RecordingTurn(),
        models=EmbodyModels(worker="w", senses="s"),
        limits=Limits(min_alert_interval_s=0.0, **limits),
    )


def _a_scope(**overrides) -> CognitionScope:
    payload = {
        "goal": "Clarify what object the user is referring to",
        "source": "qwen",
        "relevant_facts": ("The latest image contains two visible objects",),
        "suggested_next_step": "Ask whether they mean the left object",
        "turn": 0,
    }
    payload.update(overrides)
    built = make_scope(**payload)
    assert built is not None, f"the fixture scope was refused: {payload}"
    return built


def _imports(path: Path) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


# =========================================================================== #
# AC 1 — the artifact: attributed, complete, bounded, expiring                #
# =========================================================================== #


def test_ac1_the_scope_carries_every_field_the_spec_names_with_source_attribution() -> None:
    """The spec's shape, field for field (issue #155's ``cognition.scope``)."""
    built = _a_scope(
        relevant_facts=(
            "The latest image contains two visible objects",
            "The user previously referred to the left object",
        ),
        priority="normal",
        expires_after_turns=2,
        speakable=False,
    )

    assert built.goal == "Clarify what object the user is referring to"
    assert built.relevant_facts == (
        "The latest image contains two visible objects",
        "The user previously referred to the left object",
    )
    assert built.suggested_next_step == "Ask whether they mean the left object"
    assert built.priority == "normal"
    assert built.expires_after_turns == 2
    assert built.speakable is False
    assert built.source == "qwen", "a scope with no attribution is not a scope"
    assert built.kind == SCOPE_TYPE


def test_ac1_the_event_shape_is_the_specs_json_shape() -> None:
    """``as_event`` IS the artifact, not a rendering of it — and it round-trips."""
    event = _a_scope().as_event()

    assert event["type"] == SCOPE_TYPE == "cognition.scope"
    for field in (
        "source",
        "goal",
        "relevant_facts",
        "suggested_next_step",
        "priority",
        "expires_after_turns",
        "speakable",
    ):
        assert field in event, field

    rebuilt = CognitionScope.from_event(event)
    assert rebuilt is not None
    assert rebuilt.as_event() == event


def test_ac1_a_scope_with_no_attribution_is_refused_by_name(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert make_scope("do a thing", source="  ", turn=0) is None
    assert f"dropped reason={REFUSAL_UNATTRIBUTED}" in caplog.text


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_ac1_a_scope_with_no_goal_is_refused(blank: str) -> None:
    assert make_scope(blank, source="qwen", turn=0) is None


def test_ac1_every_size_bound_is_enforced_and_refused_never_truncated(caplog) -> None:
    """Fail-closed on every bound: a truncated scope is a scope that lies."""
    limits = ScopeLimits()
    cases = [
        (REFUSAL_GOAL_TOO_LONG, {"goal": "g" * (limits.max_goal_chars + 1)}),
        (
            REFUSAL_FACT_TOO_LONG,
            {"relevant_facts": ("f" * (limits.max_fact_chars + 1),)},
        ),
        (
            REFUSAL_TOO_MANY_FACTS,
            {"relevant_facts": tuple(f"fact {n}" for n in range(limits.max_facts + 1))},
        ),
        (
            REFUSAL_NEXT_STEP_TOO_LONG,
            {"suggested_next_step": "s" * (limits.max_next_step_chars + 1)},
        ),
        (REFUSAL_UNKNOWN_PRIORITY, {"priority": "urgent"}),
        (
            REFUSAL_EXPIRY_TOO_LONG,
            {"expires_after_turns": limits.max_expires_after_turns + 1},
        ),
    ]
    for reason, override in cases:
        payload = {
            "goal": "Clarify the reference",
            "source": "qwen",
            "turn": 0,
            **override,
        }
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            assert make_scope(**payload) is None, reason
        assert f"dropped reason={reason}" in caplog.text, reason


def test_ac1_the_total_rendered_size_is_bounded_even_when_every_field_fits() -> None:
    """The per-field caps do not add up to the whole: the total is its own bound.

    Every field below is individually legal; together they are not. Without a
    total bound a "compact" artifact is only compact one field at a time.
    """
    limits = ScopeLimits()
    fat = {
        "goal": "g" * limits.max_goal_chars,
        "source": "qwen",
        "relevant_facts": tuple("f" * limits.max_fact_chars for _ in range(limits.max_facts)),
        "suggested_next_step": "s" * limits.max_next_step_chars,
        "turn": 0,
    }
    assert make_scope(**fat) is None

    built = _a_scope()
    assert len(built.render()) <= limits.max_total_chars


def test_ac1_an_over_large_scope_names_the_total_bound(caplog) -> None:
    limits = ScopeLimits()
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert (
            make_scope(
                "g" * limits.max_goal_chars,
                source="qwen",
                relevant_facts=tuple("f" * limits.max_fact_chars for _ in range(limits.max_facts)),
                suggested_next_step="s" * limits.max_next_step_chars,
                turn=0,
            )
            is None
        )
    assert f"dropped reason={REFUSAL_TOO_LARGE}" in caplog.text


def test_ac1_a_scope_expires_in_turns_counted_from_the_turn_that_made_it() -> None:
    """``expires_after_turns`` counts turns AFTER the one that created it."""
    built = _a_scope(turn=7, expires_after_turns=2)

    assert built.is_expired(7) is False
    assert built.is_expired(8) is False
    assert built.is_expired(9) is False
    assert built.is_expired(10) is True


def test_ac1_the_shipped_expiry_keeps_a_scope_readable_by_the_next_turn() -> None:
    """An expiry below one turn would expire before anything could read it."""
    assert DEFAULT_EXPIRES_AFTER_TURNS >= 1
    assert _a_scope(turn=3, expires_after_turns=0).expires_after_turns == 1
    assert _a_scope(turn=3, expires_after_turns=-5).is_expired(4) is False


def test_ac1_a_stale_scope_cannot_shape_a_later_turn() -> None:
    """The pin h7 asks for, end to end through the engine's own foreground lane."""
    turn_fn = _RecordingTurn()
    engine = _engine(turn_fn)
    assert engine.submit_scope(_a_scope(turn=0, expires_after_turns=1)) is True

    engine.ask("what can you see?")
    assert "Clarify what object" in turn_fn.rendered(), "a live scope must reach Gemma"

    engine.turns = 5  # five turns later the scope is long stale
    turn_fn.calls.clear()
    engine.ask("what can you see?")
    assert "Clarify what object" not in turn_fn.rendered()
    assert engine.scopes == ()


def test_ac1_the_shipped_defaults_are_the_documented_ones() -> None:
    assert DEFAULT_SCOPE_SOURCE == "qwen"
    assert DEFAULT_PRIORITY in PRIORITIES
    assert ScopeLimits().max_facts == DEFAULT_MAX_FACTS
    assert _a_scope().expires_after_turns == DEFAULT_EXPIRES_AFTER_TURNS
    assert _a_scope().speakable is False, "a scope is thinking, not speech, by default"


def test_ac1_every_refusal_name_is_exported_and_unique() -> None:
    """One vocabulary for the journal, the feed, the docs and the tests."""
    exported = {
        value
        for name, value in vars(scope_mod).items()
        if name.startswith("REFUSAL_") and isinstance(value, str)
    }
    assert exported == set(REFUSALS)
    assert len(exported) == len([n for n in vars(scope_mod) if n.startswith("REFUSAL_")])


def test_ac1_a_malformed_wire_scope_is_named_never_raised(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        for junk in (None, [], "cognition.scope", {"type": "something-else"}, {"type": SCOPE_TYPE}):
            assert scope_mod.scope_from_event(junk, turn=0) is None
    assert f"dropped reason={REFUSAL_MALFORMED}" in caplog.text
    assert STAGE == "scope"


# =========================================================================== #
# AC 1b — coalescing keys on kind + goal, never on free text (issue #154)     #
# =========================================================================== #


def test_ac1b_the_coalescing_key_is_kind_and_goal_never_the_free_text() -> None:
    """Issue #154's lesson, one family over: free text must never be a key."""
    first = _a_scope(relevant_facts=("one object",), suggested_next_step="ask about it")
    second = _a_scope(relevant_facts=("two objects",), suggested_next_step="ask which one")

    assert first.key() == second.key()
    assert first.key() == (SCOPE_TYPE, first.goal.strip().casefold())
    assert _a_scope(goal="Decide whether to answer").key() != first.key()


def test_ac1b_a_new_scope_for_the_same_goal_replaces_rather_than_accumulates() -> None:
    engine = _engine()
    for step in range(50):
        assert engine.submit_scope(_a_scope(suggested_next_step=f"try {step}")) is True

    assert len(engine.scopes) == 1, "one goal is one slot, however often it is restated"
    assert engine.scopes[0].suggested_next_step == "try 49", "latest-wins"


def test_ac1b_the_scope_park_is_bounded_and_names_its_overflow(caplog) -> None:
    engine = _engine(max_scopes=2)
    assert engine.submit_scope(_a_scope(goal="goal one")) is True
    assert engine.submit_scope(_a_scope(goal="goal two")) is True

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert engine.submit_scope(_a_scope(goal="goal three")) is False
    assert "scope-park-full" in caplog.text
    assert len(engine.scopes) == 2


def test_ac1b_an_expired_scope_frees_its_slot_for_a_new_goal() -> None:
    engine = _engine(max_scopes=1)
    assert engine.submit_scope(_a_scope(goal="goal one", turn=0, expires_after_turns=1)) is True
    engine.turns = 4
    assert engine.submit_scope(_a_scope(goal="goal two", turn=4)) is True
    assert [s.goal for s in engine.scopes] == ["goal two"]


# =========================================================================== #
# AC 2 — raw model reasoning NEVER reaches the foreground prompt builder      #
# =========================================================================== #


def test_ac2_the_artifact_has_no_field_that_could_carry_raw_reasoning() -> None:
    """Structural: the type itself has no door for it."""
    fields = set(CognitionScope.__dataclass_fields__)
    for word in _REASONING_WORDS:
        assert not any(word in name for name in fields), f"{word} in {sorted(fields)}"


def test_ac2_a_wire_scope_carrying_reasoning_drops_it_rather_than_forwarding_it() -> None:
    """``from_event`` reads only the fields it knows; the rest never exists."""
    event = _a_scope().as_event()
    event["reasoning"] = "the model's private chain of thought"
    event["reasoning_content"] = "more of it"

    rebuilt = CognitionScope.from_event(event)
    assert rebuilt is not None
    assert "chain of thought" not in rebuilt.render()
    assert "reasoning" not in rebuilt.as_event()


def test_ac2_the_foreground_prompt_builder_never_reads_a_reasoning_field() -> None:
    """AST: nothing on ``ask``'s message-building path so much as names one.

    ``ask`` is the FOREGROUND (Gemma) lane. The worker's streamed reasoning is
    exported on the ``thinking`` block — the feed, deliberately — and this pins
    that the prompt builder is not on speaking terms with it.
    """
    from reachy.embody import engine as engine_mod

    source = Path(engine_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    builders = {"ask", "_summary_message", "_scope_message", "_senses_window", "_history_messages"}
    offenders: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.FunctionDef) or node.name not in builders:
            continue
        body = ast.dump(node)
        offenders += [f"{node.name} names {word!r}" for word in _REASONING_WORDS if word in body]
    assert offenders == [], offenders


def test_ac2_a_worker_turns_reasoning_never_reaches_the_foreground_lane() -> None:
    """The behavioural half, end to end over the ONE shared history.

    A worker turn whose stream carried reasoning is recorded by its CONTENT
    only, so the very next foreground question cannot see the reasoning even
    though it replays that turn.
    """
    secret = "SECRET-CHAIN-OF-THOUGHT-9f2a"
    turn_fn = _RecordingTurn(
        TurnResult(content="I will wait", reasoning=secret, finish_reason="stop")
    )
    engine = _engine(turn_fn)
    engine.submit_cue("a rule fired", cue_class=_alert())
    assert engine.run_turn() is True

    turn_fn.calls.clear()
    engine.ask("what can you see?")
    assert secret not in turn_fn.rendered(), "raw reasoning reached the foreground prompt"


def test_ac2_thinking_stays_off_so_the_gateway_never_streams_reasoning_at_all() -> None:
    """The existing stance, made unbreakable rather than merely current."""
    from reachy.embody.engine import RequestConfig

    assert RequestConfig().enable_thinking is False


def test_ac2_the_scope_module_reaches_no_model_and_no_mouth() -> None:
    """It is a data type plus its bounds; it calls nothing and speaks nothing."""
    imported = _imports(Path(scope_mod.__file__))
    for forbidden in (
        "reachy.speech.llm",
        "reachy.speech.tts",
        "reachy.speech.playback",
        "reachy.speech.voice",
        "reachy.speech.realtime_duplex",
        "reachy.embody.media",
        "reachy.embody.tools",
    ):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


# =========================================================================== #
# AC 2b — a scope is CONTEXT for the foreground, never a trigger              #
# =========================================================================== #


def _alert():
    from reachy.embody.cues import CueClass

    return CueClass.ALERT


def test_ac2b_submitting_a_scope_never_makes_the_mind_run_a_turn() -> None:
    engine = _engine()
    for step in range(10):
        engine.submit_scope(_a_scope(goal=f"goal {step}"))

    assert engine.pending == 0
    assert engine.run_turn() is False, "a scope must never wake the mind"


def test_ac2b_submit_scope_has_no_parameter_that_could_make_it_a_trigger() -> None:
    """Structural, like ``submit_perception``: there is no door, not a closed one."""
    import inspect

    signature = inspect.signature(EmbodyTurnEngine.submit_scope)
    assert set(signature.parameters) == {"self", "scope"}


def test_ac2b_the_scope_reaches_gemma_as_a_system_message_beside_the_summary() -> None:
    turn_fn = _RecordingTurn()
    engine = _engine(turn_fn)
    engine.update_summary("Earlier the operator asked about the kettle.")
    engine.submit_scope(_a_scope())

    engine.ask("what can you see?")

    roles = [m["role"] for m in turn_fn.calls[0]]
    assert roles[-1] == "user", "the caller's own prompt is still last and unmodified"
    system_text = "\n".join(str(m["content"]) for m in turn_fn.calls[0] if m["role"] == "system")
    assert "kettle" in system_text
    assert "Clarify what object the user is referring to" in system_text
    assert "qwen" in system_text, "the scope is attributed in the prompt too"


def test_ac2b_no_scope_means_no_extra_message_at_all() -> None:
    turn_fn = _RecordingTurn()
    engine = _engine(turn_fn)
    engine.ask("hello")
    assert [m["role"] for m in turn_fn.calls[0]] == ["user"]


# =========================================================================== #
# AC 2c — an ADMITTED interjection is the speakable face of a scope           #
# =========================================================================== #


def _admitted(text: str = "shall I mention the kettle?", source: str = "worker"):
    policy = InterjectionPolicy(
        limits=InterjectionLimits(authorization=Authorization.PROACTIVE, sources=(source,))
    )
    verdict = policy.admit(text, source=source)
    assert verdict.admitted is True
    return verdict.interjection


def test_ac2c_an_admitted_interjection_becomes_a_speakable_scope() -> None:
    built = scope_from_interjection(_admitted(), turn=3)

    assert built is not None
    assert built.speakable is True, "an interjection is the SPEAKABLE face of a scope"
    assert built.source == "worker", "attributed to whoever proposed it"
    assert "shall I mention the kettle?" in built.suggested_next_step
    assert built.created_turn == 3


def test_ac2c_two_proposals_from_one_source_occupy_one_slot_latest_wins() -> None:
    """The goal names the source, so the key is kind+goal and stays per-source."""
    first = scope_from_interjection(_admitted("say A"), turn=0)
    second = scope_from_interjection(_admitted("say B"), turn=0)
    other = scope_from_interjection(_admitted("say C", source="mesh-peer"), turn=0)

    assert first.key() == second.key()
    assert other.key() != first.key()


def test_ac2c_the_engine_records_an_interjection_as_a_scope_without_triggering() -> None:
    engine = _engine()
    built = engine.note_interjection(_admitted())

    assert built is not None
    assert engine.scopes == (built,)
    assert engine.pending == 0, "the worker's own proposal must not wake the worker"


def test_ac2c_an_interjection_from_the_wire_may_alert_the_mind() -> None:
    """t5's ``ADMITTED_CUE_CLASS``: an external proposal is worth a turn."""
    engine = _engine()
    engine.note_interjection(_admitted(source="mesh-peer"), alert=True)

    assert engine.pending == 1
    assert len(engine.scopes) == 1


def test_ac2c_the_speakable_scope_still_leaves_the_wording_to_the_foreground() -> None:
    """c2: Gemma keeps the wording and the decision to speak."""
    engine = _engine()
    engine.note_interjection(_admitted())
    rendered = engine.scopes[0].render()

    assert "may" in rendered or "decide" in rendered or "consider" in rendered.lower(), rendered
