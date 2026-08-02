"""Qwen's governed voice: ``speak``/``harmonics`` are PROPOSALS, not playback.

Issue #155, task t12 — spec claim c2, honesty condition h1, resolving open
question q6 by operator decision.

Until this task the layer's two voice tools reached an injected ``synthesize``
+ ``play_audio`` pair directly: the worker model wrote a sentence and the room
heard it. That is a bypass of the architecture's central invariant — *Qwen
never silently replaces Gemma as the speaker*. The tool NAMES are kept (a model
that has learned to call ``speak`` keeps working) and their EFFECT is now an
**interjection** subject to :class:`reachy.embody.interjection.
InterjectionPolicy`: unauthorized use is a NAMED drop the model can read, and
authorized use becomes a typed, inspectable event — never audio.

The three pins h1 asks for, "asserted by test over the embody tool registry's
voice path":

1. the voice tools route through the policy, and the shipped (default-OFF)
   configuration refuses every one of them by name;
2. an ADMITTED proposal becomes a typed interjection event and a **speakable
   cognition scope** the foreground voice may use — the wording and the
   decision to speak stay Gemma's;
3. **no code path lets the worker's text reach TTS or playback**, pinned
   structurally over the registry's surface AND behaviourally through the real
   composition root against a recording sink.
"""

from __future__ import annotations

import ast
import inspect
import json
import logging
from pathlib import Path

import pytest

from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.embody import tools as embody_tools
from reachy.embody.interjection import (
    REFUSAL_RATE_LIMITED,
    REFUSAL_SOURCE_DENIED,
    REFUSAL_UNAUTHORIZED,
    Authorization,
    Interjection,
    InterjectionLimits,
    InterjectionPolicy,
)
from reachy.embody.tools import (
    ACTION_SET,
    ALL_REFUSALS,
    CREATE_RULE,
    GOTO,
    HARMONICS,
    RUN_BEHAVIOR,
    SPEAK,
    TOOL_SOURCE,
    EmbodyToolRegistry,
)

_REPO_ROOT = Path(__file__).resolve().parents[1]
_LAYER_ROOT = _REPO_ROOT / "reachy" / "embody"
_COMMAND_MODULE = _REPO_ROOT / "reachy" / "cli" / "_commands" / "agent.py"

#: Every module that can turn text into sound. None of them may be reachable
#: from the registry's voice path.
_AUDIO_MODULES = (
    "reachy.speech.tts",
    "reachy.speech.playback",
    "reachy.speech.voice",
    "reachy.speech.harmonic",
    "reachy.embody.media",
)


class _Publisher:
    def __init__(self) -> None:
        self.published: list[Interjection] = []

    def __call__(self, interjection: Interjection) -> None:
        self.published.append(interjection)


@pytest.fixture
def state_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    return tmp_path


def _authorized(source: str = TOOL_SOURCE, **limits) -> InterjectionPolicy:
    return InterjectionPolicy(
        limits=InterjectionLimits(
            authorization=Authorization.PROACTIVE, sources=(source,), **limits
        )
    )


def _registry(**kwargs) -> EmbodyToolRegistry:
    kwargs.setdefault("on_interjection", _Publisher())
    kwargs.setdefault("reload_seam", lambda timeout: None)
    kwargs.setdefault("await_timeout", 0.0)
    return EmbodyToolRegistry(**kwargs)


def _body(result: dict) -> dict:
    assert result["role"] == "tool"
    return json.loads(result["content"])


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
# 1 — the names survive; the effect is governed                               #
# =========================================================================== #


def test_the_action_set_still_names_speak_and_harmonics(state_dir: Path) -> None:
    """The model's habit keeps working; only what the call DOES changed."""
    assert ACTION_SET == (GOTO, SPEAK, HARMONICS, RUN_BEHAVIOR, CREATE_RULE)
    assert tuple(_registry().names()) == ACTION_SET


@pytest.mark.parametrize("tool", [SPEAK, HARMONICS])
def test_the_shipped_registry_refuses_every_proposal_by_name(
    tool: str, state_dir: Path, caplog
) -> None:
    """Default OFF is enforced, not documented (spec c22/h13, honesty h12)."""
    registry = _registry()

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        body = _body(registry.dispatch(tool, json.dumps({"text": "let me jump in"})))

    assert body["ok"] is False
    assert body["refusal"] == REFUSAL_UNAUTHORIZED
    assert body["error"], "a refusal the model cannot act on is not a refusal"
    assert f"dropped reason={REFUSAL_UNAUTHORIZED}" in caplog.text


@pytest.mark.parametrize("tool", [SPEAK, HARMONICS])
def test_a_refused_proposal_publishes_nothing_at_all(tool: str, state_dir: Path) -> None:
    publisher = _Publisher()
    registry = _registry(on_interjection=publisher)
    _body(registry.dispatch(tool, json.dumps({"text": "let me jump in"})))
    assert publisher.published == []


def test_an_unlisted_source_is_refused_even_with_authorization_on(state_dir: Path) -> None:
    """Per-source default-deny: naming a LEVEL is not naming a SOURCE."""
    registry = _registry(interjection=_authorized(source="mesh-peer"))
    body = _body(registry.dispatch(SPEAK, json.dumps({"text": "hello"})))
    assert body["refusal"] == REFUSAL_SOURCE_DENIED


def test_the_rate_bound_reaches_the_tool_route_too(state_dir: Path) -> None:
    """One policy, one budget — the tool route gets no shortcut the wire lacks."""
    registry = _registry(interjection=_authorized(max_per_window=1, rate_window_s=60.0))
    assert _body(registry.dispatch(SPEAK, json.dumps({"text": "one"})))["ok"] is True
    assert (
        _body(registry.dispatch(SPEAK, json.dumps({"text": "two"})))["refusal"]
        == REFUSAL_RATE_LIMITED
    )


def test_every_voice_refusal_name_is_in_the_registrys_declared_vocabulary(
    state_dir: Path,
) -> None:
    """One vocabulary: the policy's names are imported, never restated here."""
    from reachy.embody.interjection import REFUSALS as POLICY_REFUSALS

    assert POLICY_REFUSALS <= ALL_REFUSALS
    assert embody_tools.REFUSALS <= ALL_REFUSALS
    body = _body(_registry().dispatch(SPEAK, json.dumps({"text": "hi"})))
    assert body["refusal"] in ALL_REFUSALS


def test_the_say_cap_is_still_checked_before_the_policy_sees_it(state_dir: Path) -> None:
    """The shared bound still comes from its ONE home, ahead of the policy."""
    registry = _registry(interjection=_authorized())
    body = _body(registry.dispatch(SPEAK, json.dumps({"text": "a" * (MAX_SAY_CHARS + 1)})))
    assert body["refusal"] == embody_tools.REFUSAL_SAY


def test_a_missing_interjection_route_is_a_named_refusal_not_a_silent_success(
    state_dir: Path,
) -> None:
    """A success the model is told about that did nothing is a lie."""
    registry = EmbodyToolRegistry(
        interjection=_authorized(), on_interjection=None, reload_seam=lambda timeout: None
    )
    body = _body(registry.dispatch(SPEAK, json.dumps({"text": "hello"})))
    assert body["ok"] is False
    assert body["refusal"] == embody_tools.REFUSAL_NO_VOICE


# =========================================================================== #
# 2 — an ADMITTED proposal is a typed event, and Gemma keeps the wording      #
# =========================================================================== #


@pytest.mark.parametrize("tool", [SPEAK, HARMONICS])
def test_an_admitted_proposal_becomes_a_typed_interjection_event(
    tool: str, state_dir: Path
) -> None:
    publisher = _Publisher()
    registry = _registry(interjection=_authorized(), on_interjection=publisher)

    body = _body(registry.dispatch(tool, json.dumps({"text": "shall I mention the kettle?"})))

    assert body["ok"] is True
    assert body["interjection"]["text"] == "shall I mention the kettle?"
    assert body["interjection"]["source"] == TOOL_SOURCE
    assert body["voice"] == tool, "which voice was proposed is still part of the record"
    assert [i.text for i in publisher.published] == ["shall I mention the kettle?"]


def test_the_published_event_carries_provenance_and_nothing_executable(
    state_dir: Path,
) -> None:
    publisher = _Publisher()
    registry = _registry(interjection=_authorized(), on_interjection=publisher)
    registry.dispatch(SPEAK, json.dumps({"text": "hello"}))

    event = publisher.published[0].as_event()
    assert event["source"] == TOOL_SOURCE
    assert set(event) & set(ACTION_SET) == set(), "an interjection names no action"


def test_a_publisher_that_raises_is_a_named_refusal_not_a_dead_turn(
    state_dir: Path,
) -> None:
    def _boom(_interjection: Interjection) -> None:
        raise RuntimeError("the feed exploded")

    registry = _registry(interjection=_authorized(), on_interjection=_boom)
    body = _body(registry.dispatch(SPEAK, json.dumps({"text": "hello"})))
    assert body["ok"] is False
    assert body["refusal"] == embody_tools.REFUSAL_TOOL_ERROR


# =========================================================================== #
# 3 — no code path lets worker text reach TTS or playback                     #
# =========================================================================== #


def test_the_registry_accepts_no_audio_seam_at_all() -> None:
    """Structural: the door is ABSENT, not closed (the ``register`` precedent)."""
    parameters = set(inspect.signature(EmbodyToolRegistry.__init__).parameters)
    assert parameters == {
        "self",
        "interjection",
        "on_interjection",
        "source",
        "spool_root",
        "rules_path",
        "await_timeout",
        "catalog",
        "reload_seam",
    }
    for gone in ("speak", "harmonics", "play", "sink", "synthesize"):
        assert gone not in parameters, gone


def test_the_tool_module_names_no_synthesis_and_no_playback() -> None:
    imported = _imports(Path(embody_tools.__file__))
    for forbidden in _AUDIO_MODULES:
        assert not any(name.startswith(forbidden) for name in imported), forbidden
    assert "reachy.embody.interjection" in imported, "the policy IS the voice path now"


def test_the_voice_handlers_only_route_is_through_the_policy() -> None:
    """AST over the handler factory: the admit call is not optional."""
    tree = ast.parse(Path(embody_tools.__file__).read_text(encoding="utf-8"))
    factory = next(
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.FunctionDef) and node.name == "_make_voice_handler"
    )
    calls = {
        node.func.attr
        for node in ast.walk(factory)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)
    }
    assert "admit" in calls, "the voice handler must ask the policy"
    assert "play" not in calls and "play_audio" not in calls


def test_no_layer_module_hands_the_registry_a_playing_callable() -> None:
    """The composition root cannot re-open the door it no longer has a key to."""
    source = _COMMAND_MODULE.read_text(encoding="utf-8")
    assert "_build_voice_seams" not in source, (
        "the layer's own TTS->playback pipe for worker text is retired: the "
        "audio a human hears traces only to the realtime floor (spec c2/h1)"
    )


def test_the_composed_layer_plays_nothing_when_the_worker_calls_speak(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The behavioural half of h1, through the REAL composition root.

    The layer is composed exactly as production composes it, then the worker's
    own ``speak`` tool is dispatched. The profile sink — the one thing in the
    layer that can make a sound — must not move.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from tests.test_agent_embody import _compose, _media, _SessionFactory

    media = _media()
    layer, _args, _sink = _compose(media=media, session_factory=_SessionFactory(), lines=iter(()))
    try:
        body = _body(layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c1"))
        assert body["ok"] is False
        assert body["refusal"] == REFUSAL_UNAUTHORIZED
        assert media.sink._backend.played == [], "the worker's text reached the speaker"
    finally:
        layer.close()


def test_the_layer_still_has_a_mouth_it_just_is_not_the_workers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The duplex session — the FOREGROUND voice — still plays through the sink.

    h1 says the audio a human hears traces only to the realtime floor. That is a
    statement about WHOSE text reaches the speaker, not a claim that the layer
    went mute.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from tests.test_agent_embody import _compose, _media, _SessionFactory

    factory = _SessionFactory()
    media = _media()
    layer, _args, _sink = _compose(media=media, session_factory=factory, lines=iter(()))
    try:
        assert factory.last.kwargs["play"] == media.sink.play
    finally:
        layer.close()


# =========================================================================== #
# 4 — the WIRE route reaches the policy BEFORE the pure mapper (t5's refusal) #
# =========================================================================== #


def _interjection_line(text: str = "say the kettle is boiling", source: str = "mesh-peer") -> str:
    from reachy import runtime_cues

    return json.dumps(
        {"t": runtime_cues.LINE_INTERJECTION, "text": text, "source": source, "id": "e1"}
    )


class _Engine:
    """Just enough engine for the cue reader."""

    def __init__(self) -> None:
        self.cues: list[object] = []
        self.interjections: list[tuple[object, bool]] = []

    def submit_cues(self, cues) -> int:
        collected = list(cues)
        self.cues.extend(collected)
        return len(collected)

    def note_interjection(self, interjection, *, alert: bool = False):
        self.interjections.append((interjection, alert))
        return None


def test_an_authorized_wire_interjection_reaches_the_engine_through_the_policy() -> None:
    """t5 refuses these at the mapper on purpose; the composition root routes them."""
    from reachy.cli._commands import agent as agent_mod

    engine = _Engine()
    reader = agent_mod._CueReader(
        iter([_interjection_line()]),
        engine,
        interjections=_authorized(source="mesh-peer"),
    )
    reader._run()

    assert engine.cues == [], "an interjection is not a cue the mapper renders"
    assert len(engine.interjections) == 1
    interjection, alert = engine.interjections[0]
    assert interjection.text == "say the kettle is boiling"
    assert alert is True, "an EXTERNAL proposal is worth waking the mind for"


def test_an_unauthorized_wire_interjection_is_a_named_drop_and_reaches_nothing(caplog) -> None:
    """The SHIPPED policy is the closed one, so the wire route is closed too."""
    from reachy.cli._commands import agent as agent_mod

    engine = _Engine()
    reader = agent_mod._CueReader(
        iter([_interjection_line()]), engine, interjections=InterjectionPolicy()
    )

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        reader._run()

    assert engine.interjections == []
    assert engine.cues == []
    assert f"dropped reason={REFUSAL_UNAUTHORIZED}" in caplog.text


def test_forgetting_to_wire_the_policy_is_loud_rather_than_permissive(caplog) -> None:
    """t5's mapper refusal is the backstop, and it is still the one that fires."""
    from reachy.cli._commands import agent as agent_mod
    from reachy.embody import cues as cues_mod

    engine = _Engine()
    reader = agent_mod._CueReader(iter([_interjection_line()]), engine)

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        reader._run()

    assert engine.interjections == []
    assert engine.cues == []
    assert f"dropped reason={cues_mod.REASON_INTERJECTION_UNADMITTED}" in caplog.text


def test_ordinary_runtime_lines_are_untouched_by_the_interjection_route() -> None:
    from reachy.cli._commands import agent as agent_mod

    engine = _Engine()
    line = json.dumps({"t": "rule", "action": "fire", "id": "pat-acknowledge"})
    reader = agent_mod._CueReader(iter([line]), engine, interjections=_authorized())
    reader._run()

    assert engine.interjections == []
    assert len(engine.cues) == 1


# =========================================================================== #
# 5 — the composition joins: one policy, one gate, one summary                #
# =========================================================================== #


def test_the_policy_reads_the_engines_own_attention_gate_not_a_second_one(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The circle composition closes: policy -> registry -> engine -> gate.

    Two state machines answering "is a conversation live?" is exactly how the
    two would come to disagree, so the policy forwards to the ONE gate the
    engine owns — through a late-bound accessor, because the engine is built
    from the registry the policy configures.
    """
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from tests.test_agent_embody import _compose, _media, _SessionFactory

    layer, _args, _sink = _compose(
        media=_media(),
        session_factory=_SessionFactory(),
        lines=iter(()),
        # Only the LIMITS are injectable — the gate wiring is composition's job.
        interjection_limits=InterjectionLimits(
            authorization=Authorization.WARM, sources=(TOOL_SOURCE,)
        ),
    )
    try:
        # WARM authorization into a cold room: refused.
        cold = _body(layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c1"))
        assert cold["ok"] is False

        # The human says the robot's name — the ONE gate warms, and the policy
        # sees it without ever having been handed a gate of its own.
        assert layer.engine.attention.decide("reachy, are you there").admitted is True
        warm = _body(layer.registry.dispatch(SPEAK, json.dumps({"text": "hello"}), "c2"))
        assert warm["ok"] is True
        assert [s.suggested_next_step for s in layer.engine.scopes] == ["hello"]
        assert layer.engine.pending == 0, "the worker's own proposal must not wake the worker"
    finally:
        layer.close()


def test_the_late_attention_shim_reads_cold_before_the_engine_exists() -> None:
    """Fail-closed while the slot is empty — a technicality must not admit."""
    from reachy.cli._commands import agent as agent_mod

    empty = agent_mod._LateAttention(lambda: None)
    assert empty.is_warm() is False
    assert empty.note_spoken() is False


def test_the_publisher_is_a_no_op_before_the_engine_lands_in_its_slot() -> None:
    from reachy.cli._commands import agent as agent_mod

    publish = agent_mod._interjection_publisher(lambda: None, alert=False)
    publish(object())  # must not raise


def test_the_composed_layer_owns_exactly_one_summary_producer(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Decision c30 at the composition level: one producer, started and stopped."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from reachy.embody.summary import SummaryProducer
    from tests.test_agent_embody import _compose, _media, _SessionFactory

    layer, _args, _sink = _compose(
        media=_media(), session_factory=_SessionFactory(), lines=iter(())
    )
    try:
        assert isinstance(layer.summary, SummaryProducer)
        layer.start()
        assert layer.summary.thread is not None and layer.summary.thread.is_alive()
    finally:
        layer.close()
    layer.summary.thread.join(timeout=5.0)
    assert not layer.summary.thread.is_alive(), "the producer thread outlived the layer"
