"""Tests for the ``rms`` (loudness) sense provider — TDD-first, written before
``reachy/behavior/rms_sense.py`` exists.

``reachy.behavior.sense.Sense`` already declares an ``rms`` field and
``SenseProviders`` already declares an ``rms`` slot (sense.py, ~lines 126/230),
and ``reachy.behavior.rules.SENSE_FIELDS`` already accepts ``rms`` as a valid
rule predicate field — but nothing has ever fed it, so a rule keyed on ``rms``
validates cleanly and then silently never fires. This module proves the fix:
:func:`reachy.behavior.rms_sense.make_rms_provider` adapts an injected mic-chunk
source into the zero-arg :data:`reachy.behavior.sense.RmsProvider` shape, using
the identical loudness maths ``reachy.motion.snap.SnapDetector`` already uses
(via the shared :func:`reachy.motion.rms.compute_rms` helper t12 extracted) —
never a second definition.

Task t12's audio source is ``reachy.robot.media_client.HeldMediaClient.audio``
(a zero-arg bound method returning a mic chunk or ``None``), taken as an
INJECTED dependency — this module (and its tests) never construct one and never
open a second reader; a plain fake stands in for it throughout.

Acceptance:

1. A rule keyed on ``rms`` admits a behavior against injected sense — proven
   end-to-end through the real wiring (fake audio source -> provider ->
   SenseProviders -> read_perception -> Sense -> RuleEngine), not a hand-built
   ``Sense(rms=...)``.
2. Covered by the offline lane (``pytest -m offline``) with the audio source
   unreachable: the whole module is marked ``offline`` (nothing here ever
   touches a network — tests/conftest.py's socket-block guard would fail loudly
   if it did), and a dedicated scenario simulates an unreachable audio source
   (returns ``None`` / raises) and asserts a safe, non-crashing degradation.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from reachy.behavior.rms_sense import AudioChunkProvider, make_rms_provider, rms_from_chunk
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import NO_PROVIDERS, SenseProviders, read_perception
from reachy.motion.rms import compute_rms

pytestmark = pytest.mark.offline


def _loud_chunk(n: int = 512, amplitude: float = 0.5) -> np.ndarray:
    return (np.random.default_rng(1).uniform(-amplitude, amplitude, n)).astype(np.float32)


def _quiet_chunk(n: int = 512, amplitude: float = 0.001) -> np.ndarray:
    return (np.random.default_rng(0).uniform(-amplitude, amplitude, n)).astype(np.float32)


# --------------------------------------------------------------------------- #
# rms_from_chunk — pure chunk -> loudness (or None) mapping                   #
# --------------------------------------------------------------------------- #


class TestRmsFromChunk:
    def test_none_chunk_is_no_reading(self) -> None:
        assert rms_from_chunk(None) is None

    def test_empty_chunk_is_no_reading(self) -> None:
        assert rms_from_chunk(np.array([], dtype=np.float32)) is None

    def test_loud_chunk_matches_the_shared_compute_rms_helper(self) -> None:
        chunk = _loud_chunk()
        assert rms_from_chunk(chunk) == pytest.approx(compute_rms(chunk))

    def test_never_raises_on_a_degenerate_chunk(self) -> None:
        # A malformed/ragged input must degrade to None, not raise -- this
        # runs inside a provider a 50 Hz tick can never afford to crash.
        assert rms_from_chunk("not-an-array") is None


# --------------------------------------------------------------------------- #
# make_rms_provider — adapts an injected audio source into an RmsProvider      #
# --------------------------------------------------------------------------- #


class TestMakeRmsProvider:
    def test_reads_the_injected_source_and_computes_loudness(self) -> None:
        chunk = _loud_chunk()
        provider = make_rms_provider(lambda: chunk)
        assert provider() == pytest.approx(compute_rms(chunk))

    def test_quiet_chunk_yields_a_small_but_present_reading(self) -> None:
        chunk = _quiet_chunk()
        provider = make_rms_provider(lambda: chunk)
        value = provider()
        assert value is not None
        assert value == pytest.approx(compute_rms(chunk))

    def test_peeking_twice_in_one_tick_is_stable_for_a_peeking_source(self) -> None:
        # Mirrors sense.py's "peek, not take" contract: a well-behaved
        # (non-consuming) audio source returns the same chunk each call within
        # one tick, so the provider must read the identical loudness twice.
        chunk = _loud_chunk()
        provider = make_rms_provider(lambda: chunk)
        assert provider() == provider()

    def test_opens_no_media_client_of_its_own(self) -> None:
        # The provider only ever calls the injected callable -- it must import
        # neither reachy_mini nor reachy.robot.media_client, staying a
        # dependency-free leaf like the rest of reachy/behavior/. Only actual
        # import statements are checked (a docstring may still discuss why).
        import inspect

        import reachy.behavior.rms_sense as mod

        for line in inspect.getsource(mod).splitlines():
            stripped = line.strip()
            if stripped.startswith(("import ", "from ")):
                assert "reachy_mini" not in stripped
                assert "media_client" not in stripped

    # -- unreachable / degraded audio source (acceptance 2) ----------------- #

    def test_degrades_to_none_when_source_returns_none(self) -> None:
        provider = make_rms_provider(lambda: None)
        assert provider() is None

    def test_degrades_to_none_when_source_raises(self) -> None:
        def _unreachable() -> None:
            raise ConnectionError("media client unreachable")

        provider = make_rms_provider(_unreachable)
        assert provider() is None

    def test_matches_the_declared_audio_chunk_provider_shape(self) -> None:
        # Type-alias sanity: any zero-arg callable returning a chunk-or-None
        # (the exact shape of HeldMediaClient.audio, bound-method or fake)
        # is accepted with no adaptation required.
        source: AudioChunkProvider = lambda: None  # noqa: E731
        assert make_rms_provider(source)() is None


# --------------------------------------------------------------------------- #
# Acceptance 1 — a rule keyed on rms admits a behavior against injected sense  #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """Minimal duck-typed TickContext, mirroring test_behavior_rule_engine.py."""

    now: float = 0.0
    tick: int = 0
    sense: object = None
    ownership: dict = field(default_factory=dict)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set()


def _rms_rule_engine(*, threshold: float = 0.05) -> RuleEngine:
    cfg = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "loud",
                    "when": {"field": "rms", "op": "gt", "value": threshold},
                    "run": "thoughtful",  # looping=False: no duration_s required
                    "cooldown_s": 1.0,
                }
            ]
        }
    )
    return RuleEngine(cfg)


def test_rule_keyed_on_rms_admits_a_behavior_against_injected_sense() -> None:
    """The full wiring: fake audio -> provider -> SenseProviders -> Sense -> rule fire."""
    chunk = _loud_chunk()
    providers = SenseProviders(rms=make_rms_provider(lambda: chunk))
    sense = read_perception(providers)
    assert sense.rms is not None
    assert sense.rms > 0.05  # the loud chunk clears the rule's threshold

    engine = _rms_rule_engine(threshold=0.05)
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)

    assert len(ctx.admits) == 1
    assert ctx.admits[0].name == "thoughtful"


def test_a_quiet_injected_sense_never_admits_the_same_rule() -> None:
    chunk = _quiet_chunk()
    providers = SenseProviders(rms=make_rms_provider(lambda: chunk))
    sense = read_perception(providers)
    assert sense.rms is not None
    assert sense.rms < 0.05

    engine = _rms_rule_engine(threshold=0.05)
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)

    assert ctx.admits == []


def test_no_provider_wired_leaves_rms_none_and_the_rule_never_fires() -> None:
    # NO_PROVIDERS mirrors a box with the rms provider not yet composed in --
    # sense.py's own documented default degradation.
    sense = read_perception(NO_PROVIDERS)
    assert sense.rms is None

    engine = _rms_rule_engine(threshold=0.05)
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)

    assert ctx.admits == []


# --------------------------------------------------------------------------- #
# Acceptance 2 — unreachable audio source, end-to-end through the rule engine #
# --------------------------------------------------------------------------- #


def test_unreachable_audio_source_never_crashes_the_rule_tick() -> None:
    """The audio source (an unreachable/absent media client) raises on every
    call -- the provider must still degrade Sense.rms to None and the rule
    engine must still run the tick cleanly (no admission, no exception)."""

    def _unreachable() -> None:
        raise ConnectionError("media client unreachable")

    providers = SenseProviders(rms=make_rms_provider(_unreachable))
    sense = read_perception(providers)
    assert sense.rms is None

    engine = _rms_rule_engine(threshold=0.05)
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)  # must not raise

    assert ctx.admits == []
