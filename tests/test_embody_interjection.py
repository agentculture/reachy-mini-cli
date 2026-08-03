"""The interjection policy and its typed event family (issue #155, task t5).

The architecture this serves: the operator talks with Gemma, the foreground
voice. Qwen is background cognition and **never owns the mouth**. But Qwen —
and, by a later operator decision, any authorized external system — may be
AUTHORIZED to throw an interjection into the conversation when it judges one
relevant. The interjection travels as a typed, inspectable event; Gemma renders
it into speech. That is how a background mind influences a conversation without
ever speaking directly (spec claims c20/c22/c42/c43).

This module pins the five things that make that safe to ship, each as its own
section below:

1. **authorization is default OFF** and an unauthorized interjection arriving by
   ANY route is a NAMED drop — never speech, never a silent no-op (c20/h12);
2. **warm-only under the base level**, with a separate explicit *proactive*
   level for cold, and a spoken interjection NEVER opens the attention window —
   the same extend-never-open asymmetry ``tests/test_embody_attention.py`` pins
   for ``note_spoken`` (c22/h13);
3. **per-source default-deny plus a rate bound**, with source provenance on
   every event (c42/h27);
4. the **wanted-to-say artifact** is bounded, expiring, attributed, and
   structurally CONTEXT-only — the robot never wakes itself to finish an old
   sentence (c43/h28).

What this module deliberately does NOT claim
---------------------------------------------
None of it is a containment boundary. Containment rests where
``tests/test_embody_redteam.py`` says it does: the closed action set and the
fail-closed validators. This policy bounds **cost and manners** — who may put
text in front of the mind, and how often — exactly as
:mod:`reachy.embody.attention` bounds who may wake it. The red-team suite is
extended in the same change with the half of the argument that belongs there.
"""

from __future__ import annotations

import ast
import logging
from pathlib import Path

import pytest

from reachy import runtime_cues
from reachy.behavior.rules import MAX_SAY_CHARS
from reachy.embody import cues as cues_mod
from reachy.embody import interjection as interjection_mod
from reachy.embody.attention import AttentionGate
from reachy.embody.cues import ClassifiedCue, CueClass
from reachy.embody.engine import EmbodyModels, EmbodyTurnEngine, Limits
from reachy.embody.interjection import (
    ADMIT_LABELS,
    ADMITTED_CUE_CLASS,
    DEFAULT_AUTHORIZATION,
    DEFAULT_MAX_PER_WINDOW,
    DEFAULT_RATE_WINDOW_S,
    DEFAULT_SOURCES,
    DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS,
    LABEL_PROACTIVE,
    LABEL_WARM,
    REFUSAL_COLD,
    REFUSAL_EMPTY,
    REFUSAL_MALFORMED,
    REFUSAL_RATE_LIMITED,
    REFUSAL_SOURCE_DENIED,
    REFUSAL_TOO_LONG,
    REFUSAL_UNAUTHORIZED,
    REFUSAL_WANTED_TO_SAY_EMPTY,
    REFUSAL_WANTED_TO_SAY_TOO_LONG,
    REFUSALS,
    STAGE,
    WANTED_TO_SAY_CUE_CLASS,
    Authorization,
    Interjection,
    InterjectionLimits,
    InterjectionPolicy,
    make_wanted_to_say,
)
from reachy.speech.llm import TurnResult

_REPO_ROOT = Path(__file__).resolve().parents[1]

#: The one source every "authorized" fixture below allows. Named rather than
#: inlined so a test that means "the allow-listed source" cannot accidentally
#: read as "any source".
_WORKER = "worker"


# --------------------------------------------------------------------------- #
# Doubles — the same shapes the sibling embody suites use                     #
# --------------------------------------------------------------------------- #


class _Clock:
    """A hand-wound monotonic clock (cited from ``tests/test_embody_attention.py``).

    The rate bound and the attention window are both time, so both are injected:
    no test here sleeps, and a wall-clock test of a 60 s window would either
    take 60 s or prove nothing.
    """

    def __init__(self, now: float = 1000.0) -> None:
        self.now = float(now)

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += float(seconds)


class _Registry:
    def tools(self) -> list[dict]:
        return [{"type": "function", "function": {"name": "speak", "parameters": {}}}]

    def dispatch(self, name, arguments_json=None, tool_call_id=None) -> dict:
        return {"role": "tool", "tool_call_id": tool_call_id, "content": '{"ok": true}'}


def _policy(
    clock: _Clock | None = None,
    *,
    authorization: Authorization = Authorization.WARM,
    sources: tuple[str, ...] = (_WORKER,),
    attention: AttentionGate | None = None,
    **limits,
) -> InterjectionPolicy:
    """A policy on a hand-wound clock, warm-authorized for ``worker`` by default.

    The DEFAULTS of the shipped config are deliberately NOT what this helper
    builds — every closed-by-default claim below constructs its own policy with
    no arguments, so this convenience can never make a default look open.
    """
    tick = clock if clock is not None else _Clock()
    return InterjectionPolicy(
        limits=InterjectionLimits(authorization=authorization, sources=sources, **limits),
        attention=attention,
        clock=tick,
    )


def _warm(gate: AttentionGate) -> AttentionGate:
    """Open a gate the way a human does: by saying the robot's name out loud."""
    assert gate.decide("reachy, are you there").admitted is True
    return gate


def _imports(path: Path) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form (function-local too)."""
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            names.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
            names.update(f"{node.module}.{alias.name}" for alias in node.names)
    return names


def _module_source() -> str:
    return Path(interjection_mod.__file__).read_text(encoding="utf-8")


# =========================================================================== #
# AC 1 — authorization is default OFF, and every route resolves to a NAMED    #
#        drop rather than to speech (c20 / h12)                                #
# =========================================================================== #


def test_ac1_authorization_ships_off_as_a_shipped_default_not_as_documentation() -> None:
    """h13: the default-OFF state ships in the config object, not in prose."""
    assert DEFAULT_AUTHORIZATION is Authorization.OFF
    assert DEFAULT_SOURCES == ()
    shipped = InterjectionLimits()
    assert shipped.authorization is Authorization.OFF
    assert shipped.sources == ()

    unconfigured = InterjectionPolicy()
    assert unconfigured.authorization is Authorization.OFF
    assert unconfigured.sources == ()
    assert unconfigured.limits == shipped, "the config is inspectable, not hidden"


@pytest.mark.parametrize(
    "source",
    ["worker", "mesh-peer", "some-external-api", "runtime-feed", ""],
    ids=["worker-tool", "mesh-event", "api", "feed", "anonymous"],
)
def test_ac1_an_unauthorized_interjection_from_any_route_is_a_named_drop(source: str) -> None:
    """h12: with authorization absent, NO route can cause speech.

    The routes differ only in which name arrives as ``source`` — the worker's
    own speak tool, a mesh peer's typed event, an external API, the runtime
    feed. The policy is route-agnostic on purpose: one decision point means one
    place to get default-deny right.
    """
    verdict = InterjectionPolicy().admit("that reminds me of something", source=source)

    assert verdict.admitted is False
    assert verdict.label == REFUSAL_UNAUTHORIZED
    assert verdict.interjection is None, "a refused interjection produces no event"
    assert verdict.as_cue() is None, "a refusal is not a cue"


def test_ac1_the_refusal_is_named_on_the_senselog_and_carries_its_source(caplog) -> None:
    """Never a silent no-op: the drop names the reason AND who tried."""
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        InterjectionPolicy().admit("let me jump in", source="mesh-peer")

    lines = [record.getMessage() for record in caplog.records]
    assert any(
        f"stage={STAGE}" in line
        and "source=mesh-peer" in line
        and f"dropped reason={REFUSAL_UNAUTHORIZED}" in line
        for line in lines
    ), lines


def test_ac1_the_refusal_is_also_a_tool_result_the_model_can_see() -> None:
    """A refusal the model cannot see is not a refusal (the layer's house rule)."""
    result = InterjectionPolicy().admit("hello", source=_WORKER).as_result()

    assert result["ok"] is False
    assert result["refusal"] == REFUSAL_UNAUTHORIZED
    assert result["error"], "a named refusal must also say what to do about it"


def test_ac1_an_interjection_off_the_wire_never_becomes_a_cue_without_the_policy(caplog) -> None:
    """The feed/bus route, closed at the mapper: recognised, and refused by name.

    ``cues_for_runtime_event`` is a pure mapper with no policy state, so it
    cannot admit — and default-deny means it must not fall through to
    "unrecognised" either, which would hide the family. It names the drop.
    """
    event = {"t": runtime_cues.LINE_INTERJECTION, "text": "say this", "source": "mesh-peer"}

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert cues_mod.cues_for_runtime_event(event) == []
        assert cues_mod.classified_cues_for_runtime_event(event) == []

    assert f"dropped reason={cues_mod.REASON_INTERJECTION_UNADMITTED}" in caplog.text


def test_ac1_a_wanted_to_say_line_off_the_wire_is_refused_too(caplog) -> None:
    """The artifact is produced INSIDE the layer; a wire line claiming to be one lies."""
    event = {"t": runtime_cues.LINE_WANTED_TO_SAY, "text": "and another thing"}

    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert cues_mod.cues_for_runtime_event(event) == []

    assert f"dropped reason={cues_mod.REASON_WANTED_TO_SAY_OFF_WIRE}" in caplog.text


def test_ac1_neither_new_family_can_be_classified_as_an_alert_off_the_wire() -> None:
    """Belt and braces: even a future mapper that rendered text could not trigger."""
    for line_type in (runtime_cues.LINE_INTERJECTION, runtime_cues.LINE_WANTED_TO_SAY):
        assert cues_mod.classify_runtime_event({"t": line_type}) is CueClass.CONTEXT
    assert set(cues_mod.CUE_CLASSIFIERS) == set(cues_mod.CUE_MAPPERS)


def test_ac1_the_policy_module_has_no_mouth_of_its_own() -> None:
    """It decides; it never speaks. The synthesis/playback stack is unreachable."""
    imported = _imports(Path(interjection_mod.__file__))
    for forbidden in (
        "reachy.speech.tts",
        "reachy.speech.playback",
        "reachy.speech.voice",
        "reachy.speech.harmonic",
        "reachy.speech.realtime_duplex",
        "reachy.embody.media",
    ):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


def test_ac1_the_policy_never_reaches_the_llm_classifier_gate() -> None:
    """``engagement.py``'s importer set is pinned BY EQUALITY in the zero-LLM suite.

    Reaching for it here would be a separate decision; the pure ``difflib``+``re``
    matcher is what :mod:`reachy.embody.attention` uses and all this module ever
    needs is that gate's WARM/COLD answer.
    """
    imported = _imports(Path(interjection_mod.__file__))
    for forbidden in ("reachy.speech.engagement", "reachy.speech.llm"):
        assert not any(name.startswith(forbidden) for name in imported), forbidden


def test_ac1_every_refusal_name_is_exported_and_unique() -> None:
    """One vocabulary for the journal, the feed, the docs and the tests."""
    exported = {
        value
        for name, value in vars(interjection_mod).items()
        if name.startswith("REFUSAL_") and isinstance(value, str)
    }
    assert exported == set(REFUSALS)
    assert len(exported) == len([n for n in vars(interjection_mod) if n.startswith("REFUSAL_")])
    assert not set(ADMIT_LABELS) & set(REFUSALS), "an admit label is never a refusal"


@pytest.mark.parametrize("blank", ["", "   ", "\n"])
def test_ac1_a_blank_interjection_is_a_named_drop_even_when_fully_authorized(blank) -> None:
    verdict = _policy(authorization=Authorization.PROACTIVE).admit(blank, source=_WORKER)
    assert (verdict.admitted, verdict.label) == (False, REFUSAL_EMPTY)


def test_ac1_the_say_cap_is_imported_from_its_one_home_never_restated() -> None:
    """The layer validates nothing itself: a second copy of a bound is drift."""
    assert InterjectionLimits().max_chars == MAX_SAY_CHARS
    assert "reachy.behavior.rules.MAX_SAY_CHARS" in _imports(Path(interjection_mod.__file__))
    assert str(MAX_SAY_CHARS) not in _module_source(), "the number must not be restated"


def test_ac1_an_over_long_interjection_is_refused_at_the_shared_say_cap() -> None:
    verdict = _policy(authorization=Authorization.PROACTIVE).admit(
        "a" * (MAX_SAY_CHARS + 1), source=_WORKER
    )
    assert (verdict.admitted, verdict.label) == (False, REFUSAL_TOO_LONG)
    assert str(MAX_SAY_CHARS) in verdict.detail


# =========================================================================== #
# AC 2 — warm-only at the base level, proactive for cold, and speech NEVER    #
#        opens the window (c22 / h13)                                          #
# =========================================================================== #


def test_ac2_the_base_level_admits_while_attention_is_warm() -> None:
    clock = _Clock()
    gate = _warm(AttentionGate(window_s=20.0, clock=clock))
    policy = _policy(clock, authorization=Authorization.WARM, attention=gate)

    verdict = policy.admit("I found the answer to that", source=_WORKER)

    assert (verdict.admitted, verdict.label) == (True, LABEL_WARM)
    assert verdict.interjection is not None


def test_ac2_the_base_level_refuses_from_cold_by_name() -> None:
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)
    policy = _policy(clock, authorization=Authorization.WARM, attention=gate)

    verdict = policy.admit("I found the answer to that", source=_WORKER)

    assert (verdict.admitted, verdict.label) == (False, REFUSAL_COLD)
    assert verdict.as_cue() is None


def test_ac2_the_base_level_refuses_once_the_window_has_elapsed() -> None:
    clock = _Clock()
    gate = _warm(AttentionGate(window_s=20.0, clock=clock))
    policy = _policy(clock, authorization=Authorization.WARM, attention=gate)

    assert policy.admit("still relevant", source=_WORKER).admitted is True
    clock.advance(21.0)
    assert policy.admit("no longer relevant", source=_WORKER).label == REFUSAL_COLD


def test_ac2_the_proactive_level_is_the_separate_explicit_permission_for_cold() -> None:
    """Two levels, not a boolean: 'may interject' and 'may interject UNINVITED'."""
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)
    policy = _policy(clock, authorization=Authorization.PROACTIVE, attention=gate)

    verdict = policy.admit("the kettle has boiled", source=_WORKER)

    assert (verdict.admitted, verdict.label) == (True, LABEL_PROACTIVE)
    assert gate.is_warm() is False, "admitting from cold must not warm the gate"


def test_ac2_a_spoken_interjection_never_opens_the_attention_window() -> None:
    """The mirror of ``note_spoken``'s extend-never-open pin, one family over.

    The duplex session is armed once and the SERVER answers every committed
    utterance, so a voice that could OPEN attention would be a robot waking
    itself up. An interjection is the layer speaking uninvited — exactly the
    case where that would be worst.
    """
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)
    policy = _policy(clock, authorization=Authorization.PROACTIVE, attention=gate)

    verdict = policy.admit("the kettle has boiled", source=_WORKER)
    assert verdict.admitted is True
    assert policy.note_spoken(verdict.interjection.text) is False

    assert gate.is_warm() is False
    assert gate.decide("anyway, as I was saying").admitted is False


def test_ac2_a_spoken_interjection_extends_a_live_window() -> None:
    """The other half of the asymmetry: it EXTENDS, it just never OPENS."""
    clock = _Clock()
    gate = _warm(AttentionGate(window_s=20.0, clock=clock))
    policy = _policy(clock, authorization=Authorization.WARM, attention=gate)

    clock.advance(18.0)
    assert policy.note_spoken("here is what I found") is True

    clock.advance(18.0)  # 36 s after the name — cold without the extension
    assert gate.decide("what did you find?").admitted is True


def test_ac2_without_an_attention_gate_the_base_level_is_closed() -> None:
    """Fail-closed: unknown attention reads as COLD, never as 'probably fine'."""
    warm_only = InterjectionPolicy(
        limits=InterjectionLimits(authorization=Authorization.WARM, sources=(_WORKER,)),
        attention=None,
    )
    assert warm_only.admit("hello", source=_WORKER).label == REFUSAL_COLD

    proactive = InterjectionPolicy(
        limits=InterjectionLimits(authorization=Authorization.PROACTIVE, sources=(_WORKER,)),
        attention=None,
    )
    assert proactive.admit("hello", source=_WORKER).admitted is True


def test_ac2_note_spoken_without_a_gate_is_a_no_op_not_a_crash() -> None:
    assert _policy(authorization=Authorization.PROACTIVE).note_spoken("anything") is False


def test_ac2_an_admitted_interjection_rides_the_alert_lane() -> None:
    """c22 cites the alert class as the precedent: trigger from cold, open nothing."""
    clock = _Clock()
    policy = _policy(clock, authorization=Authorization.PROACTIVE)

    cue = policy.admit("the kettle has boiled", source=_WORKER).as_cue()

    assert isinstance(cue, ClassifiedCue)
    assert cue.cue_class is ADMITTED_CUE_CLASS is CueClass.ALERT
    assert "the kettle has boiled" in cue.text


# =========================================================================== #
# AC 3 — per-source default-deny, a rate bound, and provenance on every event #
#        (c42 / h27)                                                           #
# =========================================================================== #


def test_ac3_an_unknown_source_is_denied_even_at_the_proactive_level() -> None:
    policy = _policy(authorization=Authorization.PROACTIVE, sources=(_WORKER,))

    verdict = policy.admit("trust me", source="somebody-else")

    assert (verdict.admitted, verdict.label) == (False, REFUSAL_SOURCE_DENIED)
    assert policy.is_authorized("somebody-else") is False
    assert policy.is_authorized(_WORKER) is True


def test_ac3_the_source_allow_list_ships_empty() -> None:
    """Default-deny per source: naming a level is not the same as naming a source."""
    level_only = InterjectionPolicy(
        limits=InterjectionLimits(authorization=Authorization.PROACTIVE)
    )
    assert level_only.sources == ()
    assert level_only.admit("hello", source=_WORKER).label == REFUSAL_SOURCE_DENIED


def test_ac3_every_admitted_interjection_carries_its_source_provenance() -> None:
    policy = _policy(authorization=Authorization.PROACTIVE, sources=("mesh-peer",))

    verdict = policy.admit("a peer's suggestion", source="mesh-peer")

    assert verdict.interjection.source == "mesh-peer"
    assert verdict.interjection.as_event()["source"] == "mesh-peer"
    assert "mesh-peer" in verdict.interjection.render()
    assert verdict.as_result()["interjection"]["source"] == "mesh-peer"


def test_ac3_an_interjection_event_round_trips_through_its_wire_shape() -> None:
    """Typed and inspectable means reconstructable, not merely printable."""
    policy = _policy(authorization=Authorization.PROACTIVE)
    original = policy.admit("say this bit", source=_WORKER).interjection

    assert Interjection.from_event(original.as_event()) == original


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "not a dict",
        {},
        {"t": "interjection"},
        {"t": "interjection", "text": "hi"},
        {"t": "interjection", "source": "worker"},
        {"t": "interjection", "text": "", "source": "worker"},
        {"t": "rule", "text": "hi", "source": "worker"},
    ],
)
def test_ac3_a_malformed_interjection_event_is_a_named_refusal_not_a_crash(bad) -> None:
    assert Interjection.from_event(bad) is None

    verdict = _policy(authorization=Authorization.PROACTIVE).admit_event(bad)
    assert (verdict.admitted, verdict.label) == (False, REFUSAL_MALFORMED)


def test_ac3_admit_event_is_the_same_policy_as_admit() -> None:
    """One decision point: the wire route gets no shortcut the tool route lacks."""
    policy = _policy(authorization=Authorization.PROACTIVE, sources=("mesh-peer",))
    event = {"t": runtime_cues.LINE_INTERJECTION, "text": "hello", "source": "unlisted"}

    assert policy.admit_event(event).label == REFUSAL_SOURCE_DENIED


def test_ac3_the_rate_bound_refuses_a_source_that_exceeds_it() -> None:
    clock = _Clock()
    policy = _policy(
        clock,
        authorization=Authorization.PROACTIVE,
        max_per_window=2,
        rate_window_s=60.0,
    )

    assert policy.admit("one", source=_WORKER).admitted is True
    clock.advance(1.0)
    assert policy.admit("two", source=_WORKER).admitted is True
    clock.advance(1.0)

    refused = policy.admit("three", source=_WORKER)
    assert (refused.admitted, refused.label) == (False, REFUSAL_RATE_LIMITED)


def test_ac3_the_rate_window_reopens_once_it_has_elapsed() -> None:
    clock = _Clock()
    policy = _policy(
        clock, authorization=Authorization.PROACTIVE, max_per_window=1, rate_window_s=60.0
    )

    assert policy.admit("one", source=_WORKER).admitted is True
    clock.advance(59.0)
    assert policy.admit("two", source=_WORKER).label == REFUSAL_RATE_LIMITED
    clock.advance(2.0)
    assert policy.admit("three", source=_WORKER).admitted is True


def test_ac3_the_rate_bound_is_per_source() -> None:
    clock = _Clock()
    policy = _policy(
        clock,
        authorization=Authorization.PROACTIVE,
        sources=(_WORKER, "mesh-peer"),
        max_per_window=1,
        rate_window_s=60.0,
    )

    assert policy.admit("one", source=_WORKER).admitted is True
    assert policy.admit("two", source=_WORKER).label == REFUSAL_RATE_LIMITED
    assert policy.admit("mine", source="mesh-peer").admitted is True


def test_ac3_a_refused_interjection_never_spends_the_rate_budget() -> None:
    """A cold refusal must not cost the source its one chance to speak later."""
    clock = _Clock()
    gate = AttentionGate(window_s=20.0, clock=clock)
    policy = _policy(
        clock,
        authorization=Authorization.WARM,
        attention=gate,
        max_per_window=1,
        rate_window_s=60.0,
    )

    assert policy.admit("too early", source=_WORKER).label == REFUSAL_COLD
    _warm(gate)
    assert policy.admit("now then", source=_WORKER).admitted is True


def test_ac3_a_denied_source_never_allocates_rate_state() -> None:
    """The rate table is keyed by ALLOW-LISTED sources, so a spoofed-name flood
    cannot grow it: default-deny bounds the policy's memory as well as its
    manners."""
    policy = _policy(authorization=Authorization.PROACTIVE, sources=(_WORKER,))
    for index in range(200):
        assert policy.admit("hi", source=f"forged-{index}").label == REFUSAL_SOURCE_DENIED

    assert policy.tracked_sources() == ()
    assert policy.admit("hi", source=_WORKER).admitted is True
    assert policy.tracked_sources() == (_WORKER,)


def test_ac3_a_zero_budget_admits_nothing() -> None:
    policy = _policy(authorization=Authorization.PROACTIVE, max_per_window=0)
    assert policy.admit("hello", source=_WORKER).label == REFUSAL_RATE_LIMITED


def test_ac3_the_shipped_rate_bound_has_the_documented_defaults() -> None:
    limits = InterjectionLimits()
    assert (limits.max_per_window, limits.rate_window_s) == (
        DEFAULT_MAX_PER_WINDOW,
        DEFAULT_RATE_WINDOW_S,
    )
    assert DEFAULT_MAX_PER_WINDOW > 0 and DEFAULT_RATE_WINDOW_S > 0


def test_ac3_an_admission_is_announced_on_the_senselog_with_its_source(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        _policy(authorization=Authorization.PROACTIVE).admit("hello there", source=_WORKER)

    assert f"stage={STAGE}" in caplog.text
    assert f"source={_WORKER}" in caplog.text
    assert LABEL_PROACTIVE in caplog.text


# =========================================================================== #
# AC 4 — the wanted-to-say artifact: bounded, expiring, attributed, and       #
#        structurally CONTEXT-only (c43 / h28)                                 #
# =========================================================================== #


def test_ac4_a_wanted_to_say_artifact_is_attributed_to_its_interrupted_response() -> None:
    artifact = make_wanted_to_say("and the third thing is", response_id="resp-7", turn=3)

    assert artifact.response_id == "resp-7"
    assert artifact.created_turn == 3
    assert artifact.as_event()["response_id"] == "resp-7"


def test_ac4_a_wanted_to_say_artifact_expires_in_turns() -> None:
    """``expires_in_turns`` counts turns AFTER the one that created it."""
    artifact = make_wanted_to_say(
        "...", response_id="resp-7", turn=3, limits=InterjectionLimits(wanted_to_say_expiry_turns=1)
    )

    assert artifact.is_expired(3) is False
    assert artifact.is_expired(4) is False, "readable by the NEXT turn (c34's honesty)"
    assert artifact.is_expired(5) is True


def test_ac4_the_shipped_expiry_keeps_the_artifact_readable_past_one_exchange() -> None:
    artifact = make_wanted_to_say("...", response_id="resp-7", turn=0)

    assert artifact.is_expired(DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS) is False
    assert artifact.is_expired(DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS + 1) is True


def test_ac4_an_expiry_below_one_turn_is_raised_to_one() -> None:
    """A zero-turn artifact would expire before the turn that was kept for it."""
    artifact = make_wanted_to_say(
        "...", response_id="r", turn=7, limits=InterjectionLimits(wanted_to_say_expiry_turns=0)
    )

    assert artifact.expires_in_turns == 1
    assert artifact.is_expired(8) is False


def test_ac4_the_expiry_default_is_a_limits_field_with_the_documented_default() -> None:
    assert InterjectionLimits().wanted_to_say_expiry_turns == DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS
    assert DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS >= 1, "it must survive to the next turn"

    artifact = make_wanted_to_say("...", response_id="r", turn=0)
    assert artifact.expires_in_turns == DEFAULT_WANTED_TO_SAY_EXPIRY_TURNS


def test_ac4_an_over_long_remainder_is_refused_by_name_never_truncated(caplog) -> None:
    """A truncated remainder is a false record of what the robot meant to say."""
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert make_wanted_to_say("a" * (MAX_SAY_CHARS + 1), response_id="r", turn=0) is None

    assert f"dropped reason={REFUSAL_WANTED_TO_SAY_TOO_LONG}" in caplog.text


def test_ac4_a_blank_remainder_is_refused_by_name(caplog) -> None:
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        assert make_wanted_to_say("   ", response_id="r", turn=0) is None

    assert f"dropped reason={REFUSAL_WANTED_TO_SAY_EMPTY}" in caplog.text


def test_ac4_the_artifact_is_structurally_context_only() -> None:
    """Its lane is a constant with exactly one possible value — not a parameter."""
    artifact = make_wanted_to_say("and another thing", response_id="r", turn=0)
    cue = artifact.as_cue()

    assert WANTED_TO_SAY_CUE_CLASS is CueClass.CONTEXT
    assert cue.cue_class is CueClass.CONTEXT
    assert "cue_class" not in {f for f in artifact.__dataclass_fields__}


def test_ac4_no_code_path_gives_the_artifact_the_trigger_class() -> None:
    """AST, never grep: the ALERT lane must be unreachable from this family.

    Scoped to the artifact's own class body and its factory, so the pin says
    something specific — the interjection half of the module is *supposed* to
    reach ALERT.
    """
    tree = ast.parse(_module_source())
    subjects = [
        node
        for node in tree.body
        if (isinstance(node, ast.ClassDef) and node.name == "WantedToSay")
        or (isinstance(node, ast.FunctionDef) and "wanted_to_say" in node.name)
    ]
    assert subjects, "vacuity guard: the wanted-to-say surface must exist"

    for subject in subjects:
        for node in ast.walk(subject):
            if isinstance(node, ast.Attribute):
                assert node.attr != "ALERT", f"{subject.name} reaches the trigger class"
            if isinstance(node, ast.Name):
                assert node.id != "ADMITTED_CUE_CLASS", f"{subject.name} reaches the alert lane"


def test_ac4_the_engine_parks_the_artifact_and_never_triggers_on_it() -> None:
    """The end-to-end structural pin, against the REAL engine.

    ``submit_cues`` routes by class, so a CONTEXT-classed artifact lands in the
    park and no turn runs — the robot never wakes itself to finish an old
    sentence.
    """
    engine = EmbodyTurnEngine(
        registry=_Registry(),
        turn_fn=lambda messages, **kwargs: TurnResult(content="ok", finish_reason="stop"),
        models=EmbodyModels(worker="worker", senses="senses"),
        limits=Limits(),
        now_fn=_Clock(),
    )
    artifact = make_wanted_to_say("and the third thing is", response_id="resp-7", turn=0)

    assert engine.submit_cues([artifact.as_cue()]) == 1
    assert (engine.pending, engine.parked) == (0, 1)
    assert engine.run_turn() is False


def test_ac4_the_artifact_renders_through_the_one_cue_vocabulary() -> None:
    artifact = make_wanted_to_say("and the third thing is", response_id="r", turn=0)

    assert artifact.render() == runtime_cues.wanted_to_say_cue("and the third thing is")
    assert "and the third thing is" in artifact.render()
    assert artifact.as_cue().text == artifact.render()


# =========================================================================== #
# The cue vocabulary stays CLOSED — one phrasing per family                    #
# =========================================================================== #


def test_the_two_new_families_have_exactly_one_phrasing_each() -> None:
    """Equal facts render equal text, which is what makes coalescing correct.

    Pinned against the LITERAL rendering rather than against a second call to
    the same function. ``f(x) == f(x)`` is a self-comparison — it holds for any
    pure function, so it would survive a silent rewording, and rewording is
    precisely what breaks coalescing: the park keys on the cue TEXT, so two
    renderings of one fact must be byte-identical across the process. Naming
    the string is what makes a wording change fail here.
    """
    assert runtime_cues.interjection_cue("hello", "worker") == 'worker suggests saying: "hello"'
    assert runtime_cues.wanted_to_say_cue("x") == 'I was interrupted before saying: "x"'

    # ...and DIFFERENT facts must not collide, or coalescing would merge two
    # separate events into one count. The source is part of the fact (two
    # sources proposing the same sentence are two proposals, not one).
    assert runtime_cues.interjection_cue("hello", "worker") != runtime_cues.interjection_cue(
        "hello", "mesh-peer"
    )
    assert runtime_cues.wanted_to_say_cue("x") != runtime_cues.wanted_to_say_cue("y")


def test_the_vocabulary_module_stays_dependency_free() -> None:
    """``runtime_cues`` is imported at module scope by both cognition roots."""
    imported = _imports(_REPO_ROOT / "reachy" / "runtime_cues.py")
    assert not any(name.startswith("reachy") for name in imported), imported


def test_the_two_new_line_types_are_named_in_the_one_vocabulary_module() -> None:
    assert runtime_cues.LINE_INTERJECTION == "interjection"
    assert runtime_cues.LINE_WANTED_TO_SAY == "wanted_to_say"
    assert set(runtime_cues.EMBODY_LINE_TYPES) == {
        runtime_cues.LINE_INTERJECTION,
        runtime_cues.LINE_WANTED_TO_SAY,
    }
    assert not set(runtime_cues.EMBODY_LINE_TYPES) & {"rule", "sense", "intent", "motion"}
