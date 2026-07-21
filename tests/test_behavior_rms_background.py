"""The adaptive rms admission — ratio over a rolling background (#102 / t36).

TDD-first: written before ``reachy/behavior/rms_background.py`` exists.

Why the absolute floor had to go (the measured evidence)
========================================================
Hands-on measurement on the deployed robot, 2026-07-21
(``docs/verification/2026-07-21-live-verification-night.md`` section 4, issue
#102): the mic background drifts **~25x** across conditions the same robot
lives in within 24 h.

===============================================  =====================  ========
condition                                        still-room rms         >= 0.02
===============================================  =====================  ========
daytime baseline                                 p50 0.004, max 0.0095  0 %
night, no streaming                              p50 0.0207, p90 0.053  51.7 %
night, 50 Hz target streaming (the NORMAL state) p50 0.034, p99 0.085   99.1 %
post-motion settle 0-4 s                         p50 0.07-0.13          100 %
===============================================  =====================  ========

So the deployed absolute floor ``0.02`` sits UNDER the night background — every
empty-room admission was just the background, honestly reported — while any
value above the night state deafens the daytime robot. **No absolute value is
right in both rooms.** The admission predicate has to be RELATIVE: a ratio over
a rolling estimate of the CURRENT background, which is the shape the 0.02 was
originally lifted FROM (``reachy.motion.snap.SnapDetector.min_rms`` was a floor
*inside* a ratio-above-rolling-average test).

What this module asserts
========================
1. **The stepped-background property (the contract's centrepiece).** A
   synthetic trace whose background steps ``0.004 -> 0.021 -> 0.034`` produces
   ZERO admissions at any steady level, while a transient standing ratio-x
   above the CURRENT background admits at EVERY level — driven through the
   SHIPPED admission path (fake audio -> ``RmsSense`` -> ``SenseProviders`` ->
   ``read_perception`` -> the real ``RuleEngine`` over the real shipped rules),
   never a hand-built ``Sense``.
2. **The estimator excludes what it must not learn.** Self-moving/gated windows
   are excluded (a self-noise-inflated background masks real sound — and with
   the shipped INFINITE moving floor those windows read ``0.0``, which would
   drag the background to the silence guard and manufacture a phantom spike on
   the next real reading), and the estimate FREEZES rather than adapts during
   an admitted episode (else a talker trains the robot deaf to themselves).
3. **The tunables are the ratio and the window**, stated once and pinned across
   ``default_rules.toml``, ``OrientParams`` and the estimator's own defaults.
4. **Tier 2 is EARNED** (d6). Clearing the ratio buys an antenna lean; the
   head/body turn additionally needs sound that is LOUD relative to the room or
   ONGOING. Driven off the same estimator and the same still-room warm-up, so
   the only difference between "lean" and "turn" is the sound itself. The
   measured reason: with the absolute floor the rule fired 203 times in 8
   minutes and the pat sense recorded ZERO detections in 5 minutes — the pat
   sense suspends while another behavior owns the head, so a head that keeps
   turning cannot feel a pat.

Everything here is deterministic: an injected clock, a seeded RNG, and no I/O.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import numpy as np
import pytest

from reachy.behavior.orient import CorroboratedGate, OrientParams, OrientTier
from reachy.behavior.rms_background import (
    DEFAULT_MIN_SAMPLES,
    DEFAULT_RATIO,
    DEFAULT_SILENCE_FLOOR,
    DEFAULT_WINDOW_S,
    RmsBackground,
)
from reachy.behavior.rms_sense import RmsSense
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import CORROBORATING_SENSE_FIELDS, SENSE_FIELDS, load_shipped_rules
from reachy.behavior.sense import (
    _COMPOSED_PROVIDER_FIELDS,
    EMPTY_SENSE,
    FED_SENSE_FIELDS,
    Sense,
    SenseProviders,
    read_perception,
)

pytestmark = pytest.mark.offline

TICK_S = 0.02  # the runtime's 50 Hz tick

#: The three measured still-room backgrounds, in the order the trace walks them
#: (see the module docstring's table). The first is the daytime baseline the
#: shipped 0.02 floor was calibrated against; the last is the runtime's NORMAL
#: night state, where 99.1 % of samples cleared that floor.
MEASURED_BACKGROUNDS = (0.004, 0.0207, 0.034)

#: The measured multiplicative spread of a still room, top to median. Every
#: measured condition tops out about **2.5x** its own p50 — daytime max/p50 =
#: 0.0095/0.004 over 1459 samples, night p90/p50 = 0.053/0.0207, night-streaming
#: p99/p50 = 0.085/0.034 — so the synthetic trace draws a right-skewed
#: (lognormal) still room and CLAMPS it there. Clamping is the faithful choice:
#: an unclamped lognormal tail invents still-room samples 4-5x the median that
#: no measurement on this robot has ever produced, and the test would then be
#: asserting against a room that does not exist.
SPREAD_CLAMP = 2.5
SPREAD_SIGMA = math.log(SPREAD_CLAMP) / 2.326  # p99 lands on the clamp


def _chunk(rms: float, n: int = 256) -> np.ndarray:
    """A mic chunk whose loudness is EXACTLY *rms* (a constant-magnitude wave)."""
    return np.full(n, float(rms), dtype=np.float32)


def _spread(rng, level: float) -> float:
    """One still-room sample around *level*, right-skewed and clamped."""
    factor = min(SPREAD_CLAMP, max(1.0 / SPREAD_CLAMP, math.exp(rng.normal(0.0, SPREAD_SIGMA))))
    return float(level * factor)


def _still_room(level: float, ticks: int, *, seed: int) -> list[float]:
    """``ticks`` still-room rms samples whose median is *level*.

    Not white noise around a mean: the measured distributions are right-skewed
    (a floor with occasional louder samples), which is exactly the shape that
    made an absolute threshold fail, so the trace reproduces it rather than
    smoothing it away.
    """
    rng = np.random.default_rng(seed)
    return [_spread(rng, level) for _ in range(ticks)]


def _drifting_room(start: float, end: float, ticks: int, *, seed: int) -> list[float]:
    """A room whose background DRIFTS geometrically from *start* to *end*.

    The measurement is a drift, not a step: the ~25x range is walked across a
    day (AGC, a pipeline degrading, the hold hum coming up), never in one tick.
    A synthetic step would be a 5x transient — real sound, correctly admitted —
    and would test something nobody deployed. Geometric because the quantity is
    multiplicative: each equal slice of time multiplies the level by the same
    factor, which is what "drifts 25x" means.
    """
    rng = np.random.default_rng(seed)
    span = end / start
    return [_spread(rng, start * span ** (i / max(1, ticks - 1))) for i in range(ticks)]


# --------------------------------------------------------------------------- #
# Test doubles — the shipped rule engine's per-tick seam                      #
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    """The engine's per-tick seam surface, recording what a rule did."""

    now: float = 0.0
    tick: int = 0
    sense: object = EMPTY_SENSE
    ownership: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        return {"ok": True, "target": name}

    def active_names(self) -> set[str]:
        return set()


class _Trace:
    """Drive a per-tick rms trace through the SHIPPED admission path.

    One ``RmsSense`` (so the background estimator and the moving-floor gate keep
    their state), one ``RuleEngine`` over the real shipped rules (so cooldown
    and hysteresis are the real ones), and the real ``read_perception`` in
    between — the point of this harness is that NOTHING between the mic chunk
    and the admission is stubbed.
    """

    def __init__(self, *, background: RmsBackground | None = None, moving=None) -> None:
        self.chunk: np.ndarray | None = None
        self.sense = RmsSense(
            lambda: self.chunk,
            moving=moving,
            background=background if background is not None else RmsBackground(),
        )
        self.providers = SenseProviders(rms=self.sense.rms, rms_ratio=self.sense.ratio)
        self.engine = RuleEngine(load_shipped_rules())
        self.t = 0.0
        self.tick = 0
        self.admissions: list[tuple[float, str]] = []
        self.ratios: list[float | None] = []

    def feed(self, values) -> None:
        for value in values:
            self.chunk = _chunk(value)
            self.sense.pull(self.t)
            ctx = _Ctx(now=self.t, tick=self.tick, sense=read_perception(self.providers))
            self.engine.on_tick(ctx)
            self.ratios.append(ctx.sense.rms_ratio)
            self.admissions.extend((self.t, b.name) for b in ctx.admits)
            self.t += TICK_S
            self.tick += 1

    def admitted_since(self, t0: float) -> list[str]:
        return [name for t, name in self.admissions if t >= t0]


# --------------------------------------------------------------------------- #
# 1. The contract's centrepiece — a stepped background admits nothing steady  #
# --------------------------------------------------------------------------- #


def test_a_stepped_background_never_admits_while_it_is_merely_the_background() -> None:
    """The #102 property, end to end: 0.004 -> 0.0207 -> 0.034, zero admissions.

    Three 20 s still phases at the measured medians, joined by 30 s drifts
    (see :func:`_drifting_room`) — 120 s / 6000 ticks of a room that walks the
    whole measured 24 h range, with NOTHING in it but the room.

    Under the retired absolute 0.02 floor the second phase would admit on 51.7 %
    of ticks and the third on 99.1 %. Under a ratio over the rolling background
    nothing here is loud RELATIVE to itself at any point, drifts included, which
    is the whole point: "loud" is a comparison, not a number.
    """
    trace = _Trace()
    day, evening, night = MEASURED_BACKGROUNDS
    trace.feed(_still_room(day, 1000, seed=0))
    trace.feed(_drifting_room(day, evening, 1500, seed=1))
    trace.feed(_still_room(evening, 1000, seed=2))
    trace.feed(_drifting_room(evening, night, 1500, seed=3))
    trace.feed(_still_room(night, 1000, seed=4))
    assert trace.admissions == []


@pytest.mark.parametrize("level", MEASURED_BACKGROUNDS)
def test_a_transient_above_the_current_background_admits_at_every_level(level: float) -> None:
    """The other half: the SAME relative transient is heard in all three rooms.

    A gate that admits nothing is trivially quiet and useless. At each measured
    background a burst standing ``2 * DEFAULT_RATIO`` above the CURRENT
    background must admit — including at 0.034, where a fixed threshold high
    enough to survive the night would have to sit above 0.085 and would then be
    deaf all day.
    """
    trace = _Trace()
    trace.feed(_still_room(level, 1000, seed=13))
    assert trace.admissions == []
    mark = trace.t
    trace.feed([level * 2.0 * DEFAULT_RATIO] * 10)
    assert trace.admitted_since(mark) == ["orient-to-sound"]


def test_the_same_absolute_loudness_is_loud_in_one_room_and_background_in_another() -> None:
    """0.03 is a shout at the daytime baseline and silence at night.

    This is the single fact no absolute floor can express, stated as a test.
    """
    quiet_room = _Trace()
    quiet_room.feed(_still_room(0.004, 1000, seed=7))
    quiet_room.feed([0.03] * 5)
    assert quiet_room.admitted_since(0.0) == ["orient-to-sound"]

    night_room = _Trace()
    night_room.feed(_still_room(0.034, 1000, seed=7))
    mark = night_room.t
    night_room.feed([0.03] * 5)
    assert night_room.admitted_since(mark) == []


# --------------------------------------------------------------------------- #
# 2a. Exclusion — the estimator never learns from the robot's own noise       #
# --------------------------------------------------------------------------- #


def test_an_excluded_window_does_not_move_the_estimate() -> None:
    """Self-noise must not inflate the background — it would mask real sound."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    settled = bg.level
    assert settled == pytest.approx(0.004, rel=0.05)
    for i in range(500, 1000):
        bg.observe(0.09, i * TICK_S, excluded=True)  # the robot's own actuators
    assert bg.level == pytest.approx(settled, rel=0.05)


def test_the_gated_quiet_of_a_moving_robot_cannot_manufacture_a_phantom_spike() -> None:
    """The exclusion's OTHER direction, and the sharper one.

    The shipped moving floor is INFINITE (``rms_sense``'s #95 gate): while the
    engine commands motion EVERY reading is reported ``0.0``. Learning from
    those zeros would drag the background onto the silence guard, so the first
    real reading after the move would stand hundreds of times "above
    background" and admit — a phantom fire manufactured by the fix for the
    previous phantom fire. The exclusion closes that.
    """
    moving = {"value": False}
    trace = _Trace(moving=lambda: moving["value"])
    trace.feed(_still_room(0.004, 1000, seed=3))
    moving["value"] = True
    trace.feed(_still_room(0.004, 500, seed=4))  # gate reports 0.0 throughout
    assert set(trace.ratios[-500:]) == {0.0}  # quiet, as the #95 gate intends
    moving["value"] = False
    mark = trace.t
    trace.feed(_still_room(0.004, 200, seed=5))
    assert trace.admitted_since(mark) == []


# --------------------------------------------------------------------------- #
# 2b. Freeze — an admitted episode does not train the robot deaf to itself    #
# --------------------------------------------------------------------------- #


def test_a_sustained_episode_freezes_the_estimate_instead_of_adapting_to_it() -> None:
    """A talker holding the room must stay audible for as long as they talk.

    Without the freeze, a 10 s utterance longer than half the rolling window
    becomes the median — the robot adapts to the voice and stops hearing it
    mid-sentence, which is the failure a naive rolling estimate would ship.
    """
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    quiet_level = bg.level
    ratios = [bg.observe(0.05, (500 + i) * TICK_S) for i in range(500)]  # 10 s of speech
    assert min(r for r in ratios if r is not None) >= DEFAULT_RATIO
    assert bg.level == pytest.approx(quiet_level, rel=0.05)


def test_the_freeze_releases_after_the_episode_so_a_real_step_is_still_tracked() -> None:
    """Frozen during an episode, adaptive again afterwards — not a latch."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    t = 500 * TICK_S
    for _ in range(200):  # one episode
        bg.observe(0.05, t)
        t += TICK_S
    for _ in range(2000):  # 40 s of a genuinely louder room
        bg.observe(0.034, t)
        t += TICK_S
    assert bg.level == pytest.approx(0.034, rel=0.05)


def test_a_background_step_is_tracked_within_about_one_window() -> None:
    """The estimate follows the room, and the window bounds how fast."""
    bg = RmsBackground()
    t = 0.0
    for _ in range(500):
        bg.observe(0.004, t)
        t += TICK_S
    # Step the room up by exactly the measured 5x day->night drift, but below
    # the episode ratio so nothing here is mistaken for an episode.
    step = 0.004 * (DEFAULT_RATIO - 1.0)
    deadline = t + DEFAULT_WINDOW_S + 1.0
    while t < deadline:
        bg.observe(step, t)
        t += TICK_S
    assert bg.level == pytest.approx(step, rel=0.05)


# --------------------------------------------------------------------------- #
# 3. The silence guard — digital silence cannot manufacture a ratio           #
# --------------------------------------------------------------------------- #


def test_the_silence_guard_sits_below_every_measured_room() -> None:
    """It must NEVER be the operative threshold in a room we have measured.

    The quietest measured still room has p50 0.004 (daytime baseline); the
    guard is well under that, so in every measured condition the denominator is
    the real estimate and the guard is inert. Stated as a test because a guard
    that quietly becomes the threshold is an absolute floor wearing a disguise
    — exactly what this task removed.
    """
    assert DEFAULT_SILENCE_FLOOR < min(MEASURED_BACKGROUNDS) / 3.0


def test_a_digitally_silent_stream_cannot_produce_a_ratio_spike() -> None:
    """Zero background + one dither-level blip must not read as a shout."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.0, i * TICK_S)
    assert bg.level == pytest.approx(0.0)
    # A 16-bit dither LSB is ~3e-5; the guard is ~30x above it, so even a
    # thousand-fold blip over the (zero) estimate stays far below admission.
    ratio = bg.observe(3e-5, 500 * TICK_S)
    assert ratio is not None and ratio < 1.0


def test_a_silent_room_still_admits_a_real_sound() -> None:
    """The guard bounds the denominator; it does not deafen the robot."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.0, i * TICK_S)
    assert bg.observe(0.05, 500 * TICK_S) >= DEFAULT_RATIO


# --------------------------------------------------------------------------- #
# 4. Cold start and hostile input — fail closed, never raise                  #
# --------------------------------------------------------------------------- #


def test_a_cold_estimator_reports_no_ratio_at_all() -> None:
    """Fail closed: with no background yet there is nothing to be loud against."""
    bg = RmsBackground()
    assert bg.level is None
    assert bg.observe(0.5, 0.0) is None


def test_the_estimator_warms_within_its_minimum_sample_count() -> None:
    bg = RmsBackground()
    for i in range(DEFAULT_MIN_SAMPLES + 1):
        bg.observe(0.004, i * TICK_S)
    assert bg.level is not None


def test_no_reading_neither_learns_nor_holds() -> None:
    """``None`` is "no reading" — distinct from a measured quiet (0.0)."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    settled = bg.level
    for i in range(500, 1000):
        assert bg.observe(None, i * TICK_S) is None
    assert bg.level == pytest.approx(settled)


@pytest.mark.parametrize("bad", [float("nan"), float("inf"), -1.0, "loud", None, object()])
def test_hostile_readings_degrade_to_no_reading(bad) -> None:
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    settled = bg.level
    assert bg.observe(bad, 10.0) is None
    assert bg.level == pytest.approx(settled)


@pytest.mark.parametrize("bad_now", [float("nan"), float("inf"), "later", None])
def test_a_hostile_clock_never_raises(bad_now) -> None:
    bg = RmsBackground()
    bg.observe(0.004, bad_now)  # must not raise
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    assert bg.level is not None


def test_a_rewinding_clock_does_not_corrupt_the_window() -> None:
    """The engine's clock is monotonic; a foreign one must not break the sense."""
    bg = RmsBackground()
    for i in range(500):
        bg.observe(0.004, i * TICK_S)
    settled = bg.level
    for i in range(200):
        bg.observe(0.004, -float(i))
    assert bg.level == pytest.approx(settled, rel=0.05)


# --------------------------------------------------------------------------- #
# 5. The provider seam — one mic read, two coherent peeks                     #
# --------------------------------------------------------------------------- #


def test_rms_stays_the_raw_measurement_while_the_ratio_is_derived() -> None:
    """``Sense.rms`` is an honest loudness other consumers read; only the
    admission predicate is relative."""
    trace = _Trace()
    trace.feed(_still_room(0.004, 600, seed=11))
    trace.chunk = _chunk(0.08)
    trace.sense.pull(trace.t)
    snap = read_perception(trace.providers)
    assert snap.rms == pytest.approx(0.08, rel=1e-3)
    assert snap.rms_ratio == pytest.approx(0.08 / trace.sense.background.level, rel=1e-3)


def test_a_repeated_pull_within_one_tick_feeds_the_estimator_once() -> None:
    """``read_perception`` may be called twice a tick; the background must not
    see the sample twice (the ``_AudioTap`` idempotence contract, restated)."""
    chunk = _chunk(0.004)
    sense = RmsSense(lambda: chunk, background=RmsBackground())
    for i in range(DEFAULT_MIN_SAMPLES + 5):
        t = i * TICK_S
        sense.pull(t)
        sense.pull(t)
        sense.pull(t)
    assert sense.background.samples == DEFAULT_MIN_SAMPLES + 5


def test_an_unreachable_audio_source_degrades_to_no_reading() -> None:
    def _boom():
        raise RuntimeError("media client is gone")

    sense = RmsSense(_boom, background=RmsBackground())
    sense.pull(0.0)
    assert sense.rms() is None
    assert sense.ratio() is None


def test_without_a_background_the_ratio_provider_is_simply_absent() -> None:
    """No estimator wired -> no derived field, and the raw one is untouched."""
    chunk = _chunk(0.05)
    sense = RmsSense(lambda: chunk)
    sense.pull(0.0)
    assert sense.rms() == pytest.approx(0.05, rel=1e-3)
    assert sense.ratio() is None


# --------------------------------------------------------------------------- #
# 6. The tunables — one ratio, one window, stated once                        #
# --------------------------------------------------------------------------- #


def test_the_orient_params_expose_the_ratio_and_the_window() -> None:
    """Criterion 3: the ratio + window ARE the admission tunables."""
    params = OrientParams()
    assert params.rms_ratio == pytest.approx(DEFAULT_RATIO)
    assert params.rms_background_s == pytest.approx(DEFAULT_WINDOW_S)


def test_the_shipped_rule_keys_on_the_relative_field_at_the_behaviors_own_ratio() -> None:
    """One number, not two — the rule can never admit what the gate refuses."""
    rule = next(r for r in load_shipped_rules().react if r.id == "look-toward-sound")
    assert rule.when.field == "rms_ratio"
    assert rule.when.value == pytest.approx(OrientParams().rms_ratio)


def test_no_shipped_rule_keys_on_an_absolute_loudness_any_more() -> None:
    """The #102 root closure, asserted where a future edit would undo it."""
    assert {r.when.field for r in load_shipped_rules().react} == {
        "pat",
        "rms_ratio",
        "transcript",
    }


def test_the_relative_field_is_schema_valid_fed_and_named_as_corroborating() -> None:
    """The MANDATORY declared-truth trio, moved in the SAME change as the wiring."""
    assert "rms_ratio" in SENSE_FIELDS
    assert "rms_ratio" in _COMPOSED_PROVIDER_FIELDS
    assert "rms_ratio" in FED_SENSE_FIELDS
    assert "rms_ratio" in CORROBORATING_SENSE_FIELDS


def test_the_default_ratio_reproduces_the_retired_absolute_floor_by_day() -> None:
    """Why 5.0 and not some other number, stated as arithmetic.

    ``DEFAULT_RATIO`` is ``SnapDetector``'s own default ratio — the value the
    0.02 was extracted from as its ``min_rms`` companion — and 5x the measured
    DAYTIME background (p50 0.004) is 0.020, i.e. the retired absolute floor
    exactly. The daytime robot's sensitivity is therefore unchanged by this
    task; only the night robot's is repaired.
    """
    assert DEFAULT_RATIO * MEASURED_BACKGROUNDS[0] == pytest.approx(0.02)


# --------------------------------------------------------------------------- #
# 7. Tier 2 over the SAME estimator — a turn is earned (d6)                   #
# --------------------------------------------------------------------------- #
#
# The stepped-background property above is about admission. d6 adds a second
# question the same estimator answers: once sound IS admitted, does it earn the
# HEAD? Live-verified why it matters: with the absolute floor the rule fired 203
# times in 8 minutes, and the pat sense — suspended while another behavior owns
# the head — recorded zero detections in 5 minutes. These tests drive the real
# gate off the real estimator, so the promotion is pinned against measured
# still-room audio rather than hand-written ratios.


class _GateTrace:
    """Drive a per-tick rms trace through estimator -> ratio -> the real gate."""

    def __init__(self) -> None:
        self.chunk: np.ndarray | None = None
        self.sense = RmsSense(lambda: self.chunk, background=RmsBackground())
        self.providers = SenseProviders(rms=self.sense.rms, rms_ratio=self.sense.ratio)
        self.gate = CorroboratedGate()
        self.params = OrientParams()
        self.t = 0.0
        self.tiers: list[OrientTier] = []

    def feed(self, values, *, angle: float = 0.5, speech: bool = True) -> None:
        base = Sense(doa_angle=angle, speech_detected=speech)
        for value in values:
            self.chunk = _chunk(value)
            self.sense.pull(self.t)
            snap = read_perception(self.providers, base=base)
            self.tiers.append(self.gate(snap, self.t, self.params))
            self.t += TICK_S

    def since(self, index: int) -> list[OrientTier]:
        return self.tiers[index:]


def _warm(trace: _GateTrace, level: float = 0.004, ticks: int = 1000) -> int:
    """Fill the estimator from a still room; return the tick index after it."""
    trace.feed(_still_room(level, ticks, seed=21))
    assert set(trace.tiers) == {OrientTier.NONE}, "a still room must not even lean"
    return len(trace.tiers)


def test_a_brief_transient_leans_the_antennas_and_never_turns_the_head() -> None:
    """0.4 s of sound 8x the room: heard (tier 1), not faced (tier 2)."""
    trace = _GateTrace()
    mark = _warm(trace)
    trace.feed([0.004 * 8.0] * 20)
    verdicts = trace.since(mark)
    assert OrientTier.NOISE in verdicts
    assert OrientTier.SPEECH not in verdicts


def test_the_same_sound_sustained_does_turn_the_head() -> None:
    """...and 3 s of the identical sound does. The difference is time, not level.

    The acceptance criterion's tier-2 half, driven off the same estimator and
    the same still-room warm-up as the brief case above — so the ONLY thing that
    changed between "lean" and "turn" is how long the sound lasted.
    """
    trace = _GateTrace()
    mark = _warm(trace)
    trace.feed([0.004 * 8.0] * 150)
    verdicts = trace.since(mark)
    assert OrientTier.SPEECH in verdicts
    first = verdicts.index(OrientTier.SPEECH)
    assert first * TICK_S >= trace.params.sustain_s


def test_a_loud_sound_turns_the_head_without_waiting() -> None:
    """The LOUD branch, measured against the room the estimator learned."""
    trace = _GateTrace()
    mark = _warm(trace)
    trace.feed([0.004 * (trace.params.rms_ratio_loud + 5.0)] * 40)
    verdicts = trace.since(mark)
    assert OrientTier.SPEECH in verdicts
    assert verdicts.index(OrientTier.SPEECH) * TICK_S < trace.params.sustain_s


def test_the_night_room_does_not_turn_the_head_at_what_the_day_room_would() -> None:
    """The whole point, at tier 2: 0.032 is a shout by day and the floor by night."""
    day = _GateTrace()
    day_mark = _warm(day, 0.004)
    day.feed([0.032] * 150)
    assert OrientTier.SPEECH in day.since(day_mark)

    night = _GateTrace()
    night_mark = _warm(night, 0.034)
    night.feed([0.032] * 150)
    assert night.since(night_mark) == [OrientTier.NONE] * 150


# --------------------------------------------------------------------------- #
# 8. Env resolution at the composition root                                   #
# --------------------------------------------------------------------------- #
#
# The estimator's WINDOW and silence floor are sense-layer composition knobs and
# follow `REACHY_PAT_*` / `REACHY_SELF_MOVING_*`: read once, at composition, by
# the CLI, so the modules themselves stay environment-free and deterministic.
# The three ADMISSION knobs (`rms_ratio`, `rms_ratio_loud`, `sustain_s`) are
# behavior params, so they travel through the rule engine's param overlay
# instead — same env convention, different delivery, because a behavior is minted
# from the catalog on admission and never sees composition.


class TestEnvResolution:
    def test_the_window_and_floor_default_to_the_shipped_values(self, monkeypatch) -> None:
        from reachy.behavior.rms_background import SILENCE_FLOOR_ENV, WINDOW_S_ENV
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.delenv(WINDOW_S_ENV, raising=False)
        monkeypatch.delenv(SILENCE_FLOOR_ENV, raising=False)
        background = behavior_mod._rms_background()
        assert background._window_s == pytest.approx(DEFAULT_WINDOW_S)
        assert background._silence_floor == pytest.approx(DEFAULT_SILENCE_FLOOR)

    def test_the_window_env_retunes_the_estimator(self, monkeypatch) -> None:
        from reachy.behavior.rms_background import WINDOW_S_ENV
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.setenv(WINDOW_S_ENV, "30")
        assert behavior_mod._rms_background()._window_s == pytest.approx(30.0)

    def test_a_malformed_window_is_a_clean_user_error(self, monkeypatch) -> None:
        from reachy.behavior.rms_background import WINDOW_S_ENV
        from reachy.cli._commands import behavior as behavior_mod
        from reachy.cli._errors import CliError

        monkeypatch.setenv(WINDOW_S_ENV, "banana")
        with pytest.raises(CliError):
            behavior_mod._rms_background()

    def test_no_orient_env_set_means_no_overlay_at_all(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod

        for name in behavior_mod._ORIENT_PARAM_ENV:
            monkeypatch.delenv(name, raising=False)
        assert behavior_mod._behavior_param_overrides() == {}

    def test_the_three_admission_knobs_are_env_overridable(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.setenv("REACHY_ORIENT_RMS_RATIO", "7")
        monkeypatch.setenv("REACHY_ORIENT_RMS_RATIO_LOUD", "22")
        monkeypatch.setenv("REACHY_ORIENT_SUSTAIN_S", "2.5")
        assert behavior_mod._behavior_param_overrides() == {
            "orient-to-sound": {
                "rms_ratio": pytest.approx(7.0),
                "rms_ratio_loud": pytest.approx(22.0),
                "sustain_s": pytest.approx(2.5),
            }
        }

    def test_a_malformed_orient_env_is_a_clean_user_error(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod
        from reachy.cli._errors import CliError

        monkeypatch.setenv("REACHY_ORIENT_SUSTAIN_S", "soon")
        with pytest.raises(CliError):
            behavior_mod._behavior_param_overrides()

    def test_every_env_name_maps_to_a_real_orient_knob(self) -> None:
        """A typo here is a knob nobody can tune and nothing would report."""
        from reachy.behavior import library
        from reachy.cli._commands import behavior as behavior_mod

        catalog = library.LIBRARY["orient-to-sound"].params
        assert set(behavior_mod._ORIENT_PARAM_ENV.values()) <= set(catalog)
