"""Wake-word attention for the embodiment layer (issue #148, task t16).

What ships before this module is an ear that treats every endpointed utterance
as a reason to wake a thinking mind: measured on a real conversation on
2026-08-02, **6 operator utterances produced 49 turns**. Task t7 closed the
runtime-cue half of that (a rule fire triggers, ambient sense cues park); this
is the other half — nothing distinguished *someone addressed the robot* from
*someone spoke near the robot*.

The behaviour, restated as the two states the gate has:

============  ==========================================  ====================
state         what wakes a turn                           how it ends
============  ==========================================  ====================
**cold**      only an utterance that NAMES the robot      —
**warm**      any utterance                               nothing heard AND
                                                          nothing spoken for
                                                          the window
============  ==========================================  ====================

Where the gate is, and why it is here rather than one layer down
----------------------------------------------------------------
In :mod:`reachy.embody.attention`, consulted by
:meth:`~reachy.embody.engine.EmbodyTurnEngine.submit_utterance` — **never** in
:mod:`reachy.speech.realtime_duplex`. ``tests/test_realtime_duplex.py`` pins
that module as ungated by construction three ways (direct imports, the whole
transitive closure, and ``sys.modules`` after a fresh import), including
``test_c4_an_utterance_the_runtime_gate_would_drop_still_reaches_the_caller``.
Those pins are correct and untouched: the wire's job is to surface what was
said, and deciding whether that is worth waking a mind for is the layer's.
:func:`test_no_wire_module_reaches_the_layers_attention_gate` asserts the
direction of that dependency from this side too.

Why the warm window is STRUCTURAL, and why speech cannot open it
----------------------------------------------------------------
``reachy/speech/engagement.py`` is the runtime hearing leg's version of this
gate and it records the failure this module must not repeat: an accept-only
history was a **one-way ratchet** — a single false accept planted a six-turn
context and every accept re-seeded it — measured live at 199 correct drops and
**39 accepts, all wrong**. The fix was made control flow, provable by a test,
rather than advisory, precisely because the model had already said YES 36/36
times.

The same ratchet has a second door here, and it is one that gate never had.
The layer's duplex session is armed once and the SERVER answers every
committed utterance out loud — "every committed turn on this session gets a
spoken reply" (``docs/evidence/2026-08-02-t14-live-acceptance.md``) — so
``note_spoken`` fires for replies to ambient chatter the gate just refused. If
speaking could OPEN attention, a robot in a talkative room would hold its own
ear open forever, by its own voice. So the rule is structural and asymmetric:
**a name opens; being heard while warm and speaking while warm both extend;
nothing else opens.**
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

from reachy.embody import attention as attention_mod
from reachy.embody.attention import (
    DEFAULT_ATTENTION_WINDOW_S,
    DEFAULT_NAMES,
    LABEL_COLD,
    LABEL_CONTEXT,
    LABEL_NAME,
    AttentionGate,
)
from reachy.embody.cues import CueClass
from reachy.embody.engine import (
    DROP_REASONS,
    REASON_NOT_ADDRESSED_COLD,
    EmbodyModels,
    EmbodyTurnEngine,
    Limits,
)
from reachy.speech.llm import TurnResult

_REPO_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------------- #
# Doubles — the same shapes the sibling embody suites use                     #
# --------------------------------------------------------------------------- #


class _ScriptedTurn:
    """A ``turn_fn`` double recording each call's user content."""

    def __init__(self, *results: TurnResult) -> None:
        self._results = list(results) or [TurnResult(content="ok", finish_reason="stop")]
        self.user_contents: list[str] = []
        self.calls = 0

    def __call__(self, messages: list[dict], **_kwargs) -> TurnResult:
        self.calls += 1
        for message in reversed(messages):
            if message.get("role") == "user":
                self.user_contents.append(message.get("content") or "")
                break
        return self._results.pop(0) if len(self._results) > 1 else self._results[0]

    @property
    def last_user_content(self) -> str:
        return self.user_contents[-1] if self.user_contents else ""


class _Registry:
    def tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "speak", "parameters": {}}}]

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": '{"ok": true}'}


class _Clock:
    """A hand-wound monotonic clock: attention is time, so inject it.

    No test in this module sleeps. Every cadence in this codebase is injectable
    for exactly this reason, and a wall-clock test of a 45 s window would
    either take 45 s or prove nothing.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


def _engine(clock: _Clock, turn: _ScriptedTurn | None = None, **limits) -> EmbodyTurnEngine:
    """A real engine on a hand-wound clock, with everything else faked."""
    return EmbodyTurnEngine(
        registry=_Registry(),
        turn_fn=turn if turn is not None else _ScriptedTurn(),
        models=EmbodyModels(worker="worker", senses="senses"),
        limits=Limits(**limits),
        now_fn=clock,
    )


def _fire_text(rule: str = "pat-acknowledge", behavior: str = "nod") -> str:
    """The cue text :func:`reachy.runtime_cues.rule_cues` renders for a fire."""
    return f"a behavior rule fired ({rule}): now doing {behavior}"


# =========================================================================== #
# AC 1 — cold + a nameless utterance: no turn, one NAMED drop                  #
# =========================================================================== #


def test_ac1_a_nameless_utterance_while_cold_runs_no_turn_and_names_its_drop(caplog) -> None:
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _engine(clock, turn)

    with caplog.at_level("INFO", logger="reachy.sense"):
        assert engine.submit_utterance("could you pass me the salt please") is False

    assert engine.pending == 0
    assert engine.run_turn() is False
    assert turn.calls == 0, "ambient chatter reached the model"
    assert engine.unaddressed_utterances == 1
    assert LABEL_COLD in caplog.text
    assert "pass me the salt" in caplog.text, "a drop must say WHICH utterance it ignored"


def test_ac1_the_cold_drop_reason_is_in_the_engines_one_vocabulary() -> None:
    """The label is the ``senselog.drop`` reason verbatim, as engagement.py does."""
    assert REASON_NOT_ADDRESSED_COLD == LABEL_COLD == "not-addressed-cold"
    assert REASON_NOT_ADDRESSED_COLD in DROP_REASONS
    assert len(DROP_REASONS) == len(set(DROP_REASONS))


# =========================================================================== #
# AC 2 — cold + "reachy, ...": a turn, and the window opens                    #
# =========================================================================== #


def test_ac2_the_robots_name_opens_attention_and_runs_a_turn() -> None:
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _engine(clock, turn)

    assert engine.attention.is_warm() is False
    assert engine.submit_utterance("reachy, are you there?") is True
    assert engine.run_turn() is True
    assert 'heard: "reachy, are you there?"' in turn.last_user_content
    assert engine.attention.is_warm() is True


def test_ac2_the_opening_is_announced_on_the_journal(caplog) -> None:
    """A state change an operator will be asked about must be observable."""
    engine = _engine(_Clock())
    with caplog.at_level("INFO", logger="reachy.sense"):
        engine.submit_utterance("reachy hello")
    assert "attention open" in caplog.text
    assert LABEL_NAME in caplog.text


def test_ac2_a_common_stt_mishearing_of_the_name_opens_attention() -> None:
    """The matcher's whole point: STT hears "richie", the robot still wakes."""
    engine = _engine(_Clock())
    assert engine.submit_utterance("richie can you nod") is True


def test_ac2_an_everyday_r_word_does_not_open_attention() -> None:
    """The #104 collision family — ``is_name_match``'s phonetic guard earns its keep."""
    engine = _engine(_Clock())
    for ambient in ("that was really good", "the reality is different", "back to the room"):
        assert engine.submit_utterance(ambient) is False, ambient
    assert engine.attention.is_warm() is False


# =========================================================================== #
# AC 3 — warm + a nameless utterance: a turn, and the window refreshes         #
# =========================================================================== #


def test_ac3_a_nameless_utterance_while_warm_runs_a_turn_and_refreshes_the_window() -> None:
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _engine(clock, turn, attention_window_s=20.0)

    assert engine.submit_utterance("reachy, hello") is True
    clock.advance(15.0)
    assert engine.submit_utterance("what can you see?") is True, "warm: no name needed"

    # The refresh is what a back-and-forth relies on: 15 + 15 = 30 s from the
    # name, which a window anchored only on the OPENING would have closed.
    clock.advance(15.0)
    assert engine.submit_utterance("and now?") is True
    assert turn.calls == 0, "no turn has been run yet — this is about admission"
    assert engine.pending == 3


def test_ac3_a_dropped_utterance_never_refreshes_the_window() -> None:
    """Otherwise a talkative room holds the ear open with chatter it was refused."""
    clock = _Clock()
    engine = _engine(clock, attention_window_s=20.0)

    for _ in range(50):
        assert engine.submit_utterance("two people talking about lunch") is False
        clock.advance(1.0)
    assert engine.attention.is_warm() is False


# =========================================================================== #
# AC 4 — the layer's own spoken answer refreshes the window                    #
# =========================================================================== #


def test_ac4_the_layers_own_spoken_answer_refreshes_the_window() -> None:
    """``note_spoken`` is the seam; a long answer must not time the human out."""
    clock = _Clock()
    engine = _engine(clock, attention_window_s=20.0)

    assert engine.submit_utterance("reachy, tell me a story") is True
    clock.advance(18.0)
    engine.note_spoken("Once upon a time there was a very small robot.")

    clock.advance(18.0)  # 36 s after the name — cold without the refresh
    assert engine.submit_utterance("what happened next?") is True


def test_ac4_speaking_while_cold_never_opens_attention() -> None:
    """The second ratchet door, and the reason the rule is asymmetric.

    The duplex session is armed once and the server replies to EVERY committed
    utterance — including the ambient ones this gate just refused. If the
    layer's own voice could open its ear, a talkative room would keep it awake
    through the robot's own replies to conversations it is not part of.
    """
    clock = _Clock()
    engine = _engine(clock, attention_window_s=20.0)

    assert engine.submit_utterance("could you pass me the salt") is False
    engine.note_spoken("I am not sure I can reach the salt.")

    assert engine.attention.is_warm() is False
    assert engine.submit_utterance("anyway, as I was saying") is False


def test_ac4_a_spoken_answer_is_still_recorded_as_context_while_cold() -> None:
    """Attention and the already-said buffer are different questions."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _engine(clock, turn)

    engine.note_spoken("I am right here.")
    assert engine.submit_utterance("reachy, hello again") is True
    assert engine.run_turn() is True
    assert "I have already said out loud" in turn.last_user_content
    assert "I am right here." in turn.last_user_content


# =========================================================================== #
# AC 5 — the window elapses, and the ear closes again                          #
# =========================================================================== #


def test_ac5_the_window_elapses_back_to_cold_and_the_next_nameless_utterance_is_ignored() -> None:
    clock = _Clock()
    engine = _engine(clock, attention_window_s=20.0)

    assert engine.submit_utterance("reachy, are you awake?") is True
    clock.advance(20.001)

    assert engine.attention.is_warm() is False
    assert engine.submit_utterance("so what do you think") is False
    assert engine.unaddressed_utterances == 1
    assert engine.submit_utterance("reachy, are you awake?") is True, "the name always works"


# =========================================================================== #
# AC 6 — a rule fire still triggers a turn while cold                          #
# =========================================================================== #


def test_ac6_a_rule_fire_still_triggers_a_turn_while_attention_is_cold() -> None:
    """Attention gates the EAR, never the robot's own reactions (#143 + #148)."""
    clock = _Clock()
    turn = _ScriptedTurn()
    engine = _engine(clock, turn)

    assert engine.attention.is_warm() is False
    assert engine.submit_cue(_fire_text(), cue_class=CueClass.ALERT) is True
    assert engine.pending == 1
    assert engine.run_turn() is True
    assert _fire_text() in turn.last_user_content


def test_ac6_a_rule_fire_does_not_open_the_ear_either() -> None:
    """A reflex is worth a thought; it is not an invitation into the room."""
    clock = _Clock()
    engine = _engine(clock)

    engine.submit_cue(_fire_text(), cue_class=CueClass.ALERT)
    assert engine.run_turn() is True
    assert engine.attention.is_warm() is False
    assert engine.submit_utterance("did you just move?") is False


def test_ac6_context_cues_are_untouched_by_attention() -> None:
    engine = _engine(_Clock())
    assert engine.submit_cue("speech from the left") is True
    assert engine.parked == 1


# =========================================================================== #
# AC 7 — the window length is configurable, and lives in Limits                #
# =========================================================================== #


def test_ac7_the_window_length_is_a_limits_field_with_the_documented_default() -> None:
    assert Limits().attention_window_s == DEFAULT_ATTENTION_WINDOW_S
    assert DEFAULT_ATTENTION_WINDOW_S == pytest.approx(45.0)


def test_ac7_a_configured_window_is_the_one_the_engine_uses() -> None:
    clock = _Clock()
    engine = _engine(clock, attention_window_s=5.0)

    assert engine.submit_utterance("reachy, hi") is True
    clock.advance(4.0)
    assert engine.submit_utterance("still there?") is True
    clock.advance(5.001)
    assert engine.submit_utterance("still there?") is False


def test_ac7_a_zero_window_means_name_only_forever() -> None:
    """The same convention ``min_alert_interval_s`` already uses for ``0``."""
    clock = _Clock()
    engine = _engine(clock, attention_window_s=0.0)

    assert engine.submit_utterance("reachy, hi") is True
    assert engine.submit_utterance("and hello again") is False


# =========================================================================== #
# AC 8 — the gate is in the LAYER; the wire stays ungated                      #
# =========================================================================== #


def _imports(path: Path) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form (AST, never grep)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def test_no_wire_module_reaches_the_layers_attention_gate() -> None:
    """The dependency runs ONE way: the layer knows the wire, never the reverse."""
    for path in sorted((_REPO_ROOT / "reachy" / "speech").rglob("*.py")):
        assert not any(
            name.startswith("reachy.embody") for name in _imports(path)
        ), f"{path.name} imports the layer — the wire must stay ungated (c4)"


def test_the_gate_uses_the_pure_name_matcher_and_no_language_model() -> None:
    """``name_match`` is pure difflib+re; ``engagement`` carries an LLM classifier.

    ``tests/test_zero_llm_boundary.py`` pins ``engagement``'s importer set by
    EQUALITY, so reaching for it here would be a separate decision riding along
    on a wake-word feature. This test is that decision, written down.
    """
    imported = _imports(Path(attention_mod.__file__))
    assert "reachy.speech.name_match" in imported, "vacuity guard: the matcher IS used"
    for forbidden in ("reachy.speech.engagement", "reachy.speech.llm", "reachy.speech.stt"):
        assert not any(name.startswith(forbidden) for name in imported)


def test_the_layer_answers_to_the_same_names_the_runtime_gate_does() -> None:
    """One robot, one set of names — and since #177 there is one TUPLE too.

    Both names are now aliases of ``reachy.speech.name_match.SHIPPED_NAMES``,
    the ONE place the shipped pair is spelled, so this is no longer a guard
    against two hand-copied tuples drifting apart but a guard against either
    one being re-hardcoded back into a copy.
    """
    from reachy.speech.engagement import DEFAULT_NAMES as RUNTIME_NAMES
    from reachy.speech.name_match import SHIPPED_NAMES

    assert DEFAULT_NAMES == RUNTIME_NAMES
    assert DEFAULT_NAMES == SHIPPED_NAMES
    assert RUNTIME_NAMES == SHIPPED_NAMES


def test_the_gate_answers_to_the_operators_configured_names() -> None:
    """A configured name is a name the gate opens on — the point of #177.

    ``nova`` is NOT shipped (it is a mesh peer's name); it appears here only as
    a value an operator configured, exactly as ``tests/test_name_match.py``
    uses it.
    """
    gate = AttentionGate(window_s=20.0, clock=_Clock())
    gate.set_names(("reachy", "robot", "nova"))

    assert gate.names == ("reachy", "robot", "nova")
    assert gate.decide("nova, are you there?").admitted
    assert gate.decide("what time is it").admitted  # warm now


def test_setting_no_names_leaves_the_gate_answering_to_the_shipped_pair() -> None:
    """Fail-closed: an empty/blank names list must not make the robot deaf."""
    gate = AttentionGate(window_s=20.0, clock=_Clock())

    gate.set_names(())
    assert gate.names == DEFAULT_NAMES
    gate.set_names(("", "   "))
    assert gate.names == DEFAULT_NAMES
    assert gate.decide("reachy, hello").admitted


# =========================================================================== #
# The gate itself — the unit the engine composes                              #
# =========================================================================== #


def test_the_gate_reports_which_rule_admitted_an_utterance() -> None:
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)

    opened = gate.decide("reachy, hello")
    assert (opened.admitted, opened.label, opened.opened) == (True, LABEL_NAME, True)

    extended = gate.decide("what time is it")
    assert (extended.admitted, extended.label, extended.opened) == (True, LABEL_CONTEXT, False)

    again = gate.decide("reachy, still there")
    assert again.opened is False, "already warm: a name extends, it does not re-open"

    clock.advance(21.0)
    refused = gate.decide("what time is it")
    assert (refused.admitted, refused.label) == (False, LABEL_COLD)


def test_the_gate_never_admits_an_empty_or_wordless_utterance() -> None:
    gate = AttentionGate(window_s=20.0, clock=_Clock())
    for junk in ("", "   ", "...", "?!"):
        assert gate.decide(junk).admitted is False, repr(junk)


def test_note_spoken_reports_whether_it_actually_extended_anything() -> None:
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)

    assert gate.note_spoken() is False, "cold: the layer's own voice opens nothing"
    gate.decide("reachy, hi")
    assert gate.note_spoken() is True
