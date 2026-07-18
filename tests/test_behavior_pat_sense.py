"""Tests for the proprioceptive pat sense provider (task t3).

:mod:`reachy.behavior.pat_sense` turns a commanded-vs-actual head-pose deviation
into a :attr:`reachy.behavior.sense.Sense.pat_event` cue so a data-only rule
(the deployed ``when {field=pat, op=is_true} run thoughtful``) fires on a real
pat. These tests pin, from failing-first:

* the one-tick latch cadence (r2): the driver advances the detector at the END
  of a tick and latches; the provider PEEKs that latch, delivering it to exactly
  ONE sense snapshot (identical within a tick, cleared by the next driver run);
* the ownership gate (the #66 phantom-pat fix): a non-base head owner suspends
  detection (ZERO events for the SAME deviation), and the detector re-baselines
  on the first tick back on the base layer so a persistent deviation can't fire
  spuriously right after resume;
* degradation: a ``None``/raising reader (and a missing ``ctx.pose``) degrade to
  "no pat" and never raise;
* the frame mapping (r1): a constant frame offset between commanded
  (neutral-relative) and actual (absolute) degrees is absorbed by the detector's
  EMA baseline — identical relative pats fire identically with and without it.

Deterministic throughout: an injected :class:`PatDetector` (fixed
``level2_threshold_fn``), a hand-built ``TickContext``-shaped fake, an explicit
``now`` per tick, and a fake reader. No robot, SDK, daemon, or network anywhere.
"""

from __future__ import annotations

import math
from types import SimpleNamespace

import pytest

from reachy.behavior.pat_sense import PatSenseDriver, _is_base_owner
from reachy.behavior.sense import SenseProviders, read_perception
from reachy.motion.pat import PatDetector

BASE_OWNER = "feel-alive-1"
GESTURE_OWNER = "thoughtful-3"

# Firing needs ``now - last_pat_time > pat_cooldown`` (default 2.0s) and the base
# ``last_pat_time`` starts at 0.0 — so all scripted ``now`` values sit past it.
T0 = 10.0
DT = 0.1


# --------------------------------------------------------------------------- #
# Fakes / helpers                                                             #
# --------------------------------------------------------------------------- #


def _head(pitch: float = 0.0, yaw: float = 0.0) -> dict:
    """A complete six-axis composed head-offset dict (degrees, neutral-relative)."""
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": pitch, "yaw": yaw}


def _ctx(*, now: float, owner: str | None = BASE_OWNER, pitch: float = 0.0, yaw: float = 0.0):
    """A minimal ``TickContext``-shaped fake: only the fields the driver reads."""
    return SimpleNamespace(
        now=now,
        tick=int(now * 100),
        ownership={"head": owner, "antennas": owner, "body_yaw": owner},
        pose={"head": _head(pitch, yaw), "antennas": (0.0, 0.0), "body_yaw": 0.0},
    )


class _Reader:
    """A fake actual-pose reader: ``__call__`` returns whatever ``value`` holds."""

    def __init__(self, value: tuple[float, float] | None = (0.0, 0.0)) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> tuple[float, float] | None:
        self.calls += 1
        return self.value


def _fixed_detector(**kw) -> PatDetector:
    """A :class:`PatDetector` with a large, fixed level2 threshold (level1-only tests)."""
    kw.setdefault("level2_threshold_fn", lambda: 1_000.0)
    return PatDetector(**kw)


def _drive(driver: PatSenseDriver, reader: _Reader, actual, now: float, **ctx_kw) -> None:
    """Set the reader's reading, then run one driver tick at ``now``."""
    reader.value = actual
    driver(_ctx(now=now, **ctx_kw))


def _scratch_sequence(driver: PatSenseDriver, reader: _Reader, start: float, **ctx_kw) -> float:
    """Drive dip -> release -> dip (a pitch pat); return the ``now`` of the last tick."""
    _drive(driver, reader, (-3.0, 0.0), start, **ctx_kw)
    _drive(driver, reader, (0.0, 0.0), start + DT, **ctx_kw)
    _drive(driver, reader, (-3.0, 0.0), start + 2 * DT, **ctx_kw)
    return start + 2 * DT


# --------------------------------------------------------------------------- #
# _is_base_owner — robust base-layer identity                                 #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "owner_id, expected",
    [
        ("feel-alive-1", True),
        ("feel-alive-42", True),
        ("thoughtful-3", False),
        ("body-turn-hold-7", False),
        (None, False),
        ("feel-alive", False),  # no numeric seq -> engine never mints this
        ("feel-alive-foo-2", False),  # a different library name that merely shares a prefix
    ],
)
def test_is_base_owner(owner_id, expected) -> None:
    assert _is_base_owner(owner_id) is expected


# --------------------------------------------------------------------------- #
# One-tick latch cadence (r2)                                                 #
# --------------------------------------------------------------------------- #


def test_scratch_pat_latches_touch_type_then_level_once() -> None:
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)

    # dip / release -> no event yet.
    _drive(driver, reader, (-3.0, 0.0), T0)
    assert driver.peek() is None
    _drive(driver, reader, (0.0, 0.0), T0 + DT)
    assert driver.peek() is None

    # second dip -> the detector fires; the latch is (touch_type, level).
    _drive(driver, reader, (-3.0, 0.0), T0 + 2 * DT)
    assert driver.peek() == ("scratch", "level1")
    # A PEEK never consumes: repeated reads in the same tick are identical.
    assert driver.peek() == ("scratch", "level1")
    assert driver.events == 1

    # The next driver run clears the latch (one-tick delivery) -> back to None.
    _drive(driver, reader, (0.0, 0.0), T0 + 3 * DT)
    assert driver.peek() is None


def test_side_pat_latches_side_pat_touch_type() -> None:
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)

    # A yaw-dominated oscillation classifies as side_pat.
    _drive(driver, reader, (0.0, 3.0), T0)
    _drive(driver, reader, (0.0, 0.0), T0 + DT)
    _drive(driver, reader, (0.0, 3.0), T0 + 2 * DT)

    assert driver.peek() == ("side_pat", "level1")


def test_provider_flows_into_read_perception_as_pat_event() -> None:
    """The provider satisfies ``SenseProviders.pat_event``; the cue lands on Sense."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _scratch_sequence(driver, reader, T0)

    providers = SenseProviders(pat_event=driver.as_provider())
    sense = read_perception(providers)
    assert sense.pat_event == ("scratch", "level1")
    # Composing the same tick again reads the same PEEK -> identical (idempotent).
    assert read_perception(providers).pat_event == ("scratch", "level1")


def test_as_provider_is_the_peek_callable() -> None:
    driver = PatSenseDriver(reader=_Reader(), detector=_fixed_detector(), warmup_s=0.0)
    assert driver.as_provider()() is None  # nothing latched yet
    # as_provider hands back the bound peek (methods compare equal by instance+func).
    assert driver.as_provider() == driver.peek


# --------------------------------------------------------------------------- #
# Ownership gate — the #66 phantom-pat fix                                    #
# --------------------------------------------------------------------------- #


def test_same_deviation_fires_on_base_owner() -> None:
    """Control for the gate: on the base layer the deviation DOES fire."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _scratch_sequence(driver, reader, T0, owner=BASE_OWNER)
    assert driver.peek() == ("scratch", "level1")
    assert driver.events == 1


def test_same_deviation_yields_zero_events_under_a_non_base_owner() -> None:
    """The SAME deviation, but a gesture owns the head -> detection is suspended."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _scratch_sequence(driver, reader, T0, owner=GESTURE_OWNER)
    assert driver.peek() is None
    assert driver.events == 0
    # The reader is not even consulted while suspended (no work, no read).
    assert reader.calls == 0


def test_unowned_head_is_treated_as_base_and_detects() -> None:
    """``ownership['head'] is None`` (a steady neutral pose) is NOT suspended."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _scratch_sequence(driver, reader, T0, owner=None)
    assert driver.peek() == ("scratch", "level1")


def test_rebaseline_on_resume_and_persistent_deviation_does_not_fire() -> None:
    """#66: a gesture suspends detection; on return to base the detector resets,
    and a deviation that persists across the resume must not fire spuriously."""
    reader = _Reader()
    detector = _fixed_detector()
    driver = PatSenseDriver(reader=reader, detector=detector, warmup_s=0.0)

    # Phase A: while a gesture owns the head, a full oscillating pat fires nothing.
    _scratch_sequence(driver, reader, T0, owner=GESTURE_OWNER)
    assert driver.events == 0
    assert detector._state == "idle"  # never advanced while suspended

    # Phase B: ownership returns to base with the head still pressed (persistent
    # deviation carried over). The first base tick re-baselines (reset), so the
    # settled/offset pose seeds a clean baseline. A SUSTAINED press is a single
    # edge (< min_presses) and must NOT fire.
    for i in range(4):
        _drive(driver, reader, (-3.0, 0.0), T0 + (3 + i) * DT, owner=BASE_OWNER)
        assert driver.peek() is None, f"steady deviation fired spuriously at step {i}"
    assert driver.events == 0

    # ...but detection is not permanently broken. Under the deviation high-pass
    # the persistent hold contributes NO press edge (it is exactly the signal
    # the wander rejection removes) and the high-pass state has adapted to the
    # held level — so first let the hand release and the head settle (the
    # high-pass re-centres within ~2x its 0.3 s tau), then a fresh pat fires.
    for i in range(6):
        _drive(driver, reader, (0.0, 0.0), T0 + (7 + i) * DT, owner=BASE_OWNER)
    assert driver.events == 0
    _scratch_sequence(driver, reader, T0 + 13 * DT, owner=BASE_OWNER)
    assert driver.peek() == ("scratch", "level1")
    assert driver.events == 1


def test_detector_not_advanced_while_suspended() -> None:
    """A suspended tick performs no ``detector.update`` (state is untouched)."""
    reader = _Reader(value=(-3.0, 0.0))
    detector = _fixed_detector()
    driver = PatSenseDriver(reader=reader, detector=detector, warmup_s=0.0)
    for i in range(10):
        driver(_ctx(now=T0 + i * DT, owner=GESTURE_OWNER, pitch=0.0))
    assert len(detector.deviation_history) == 0  # update() never ran
    assert reader.calls == 0


# --------------------------------------------------------------------------- #
# Frame / unit mapping (r1) — constant offset absorbed by the EMA baseline    #
# --------------------------------------------------------------------------- #


def _warm_and_pat(offset: float) -> PatSenseDriver:
    """Warm a driver's EMA baseline to ``offset``, then drive an identical
    relative scratch (dip 4deg below the settled baseline, release, dip)."""
    reader = _Reader()
    # A faster EMA (0.05) converges within the warmup window while still tracking
    # only the STEADY part — the transient press still crosses the threshold.
    driver = PatSenseDriver(
        reader=reader, detector=_fixed_detector(baseline_alpha=0.05), warmup_s=0.0
    )

    now = 0.0
    # Warmup: actual sits at the constant frame offset; commanded steady at 0.
    for _ in range(150):
        _drive(driver, reader, (offset, offset), now, owner=BASE_OWNER)
        now += 0.02
    assert driver.peek() is None  # a steady offset never fires

    # Identical RELATIVE pat: dip to (offset - 4), release to offset, dip again.
    _drive(driver, reader, (offset - 4.0, offset), now, owner=BASE_OWNER)
    _drive(driver, reader, (offset, offset), now + DT, owner=BASE_OWNER)
    _drive(driver, reader, (offset - 4.0, offset), now + 2 * DT, owner=BASE_OWNER)
    return driver


def test_constant_frame_offset_is_absorbed_detection_still_works() -> None:
    """A constant commanded-vs-actual frame offset (neutral-relative vs absolute
    degrees) is cancelled by the EMA baseline: the same relative pat fires the
    same event with and without it."""
    no_offset = _warm_and_pat(0.0)
    with_offset = _warm_and_pat(5.0)
    assert no_offset.peek() == ("scratch", "level1")
    assert with_offset.peek() == ("scratch", "level1")


# --------------------------------------------------------------------------- #
# Degradation — never raises, no event                                        #
# --------------------------------------------------------------------------- #


def test_none_reader_reading_degrades_to_no_event() -> None:
    reader = _Reader(value=None)  # SDK disconnected / absent
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    for i in range(5):
        driver(_ctx(now=T0 + i * DT, owner=BASE_OWNER))
        assert driver.peek() is None
    assert driver.events == 0


def test_raising_reader_degrades_to_no_event_and_never_raises() -> None:
    def boom() -> tuple[float, float] | None:
        raise RuntimeError("reader exploded")

    driver = PatSenseDriver(reader=boom, detector=_fixed_detector(), warmup_s=0.0)
    # Must not raise out of the driver.
    driver(_ctx(now=T0, owner=BASE_OWNER))
    assert driver.peek() is None
    assert driver.events == 0


def test_missing_ctx_pose_skips_the_tick() -> None:
    reader = _Reader(value=(-3.0, 0.0))
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    driver(SimpleNamespace(now=T0, ownership={"head": BASE_OWNER}))  # no .pose at all
    assert driver.peek() is None
    driver(SimpleNamespace(now=T0, ownership={"head": BASE_OWNER}, pose=None))
    assert driver.peek() is None


def test_malformed_reading_shape_degrades() -> None:
    reader = _Reader(value=(1.0, 2.0, 3.0))  # wrong arity -> unpack fails
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    driver(_ctx(now=T0, owner=BASE_OWNER))
    assert driver.peek() is None


def test_default_detector_when_none_injected() -> None:
    driver = PatSenseDriver(reader=_Reader(), warmup_s=0.0)
    assert isinstance(driver.detector, PatDetector)


# --------------------------------------------------------------------------- #
# Lag compensation — the d1 live fix (issue #79)                              #
# --------------------------------------------------------------------------- #
#
# On the real robot the base layer's own gaze wander (±7°/±12°) plus servo/
# transport lag made same-tick commanded-vs-actual read as sustained phantom
# presses (deviation ≈ lag × d(commanded)/dt ≫ the 1.2° thresholds). The fix
# low-passes the COMMANDED pose (tau ≈ plant lag) before the detector. These
# tests reproduce the mechanism deterministically: a sinusoidal commanded sway
# whose lagged actual fires the unfiltered driver but not the filtered one,
# while a real pat (an external impulse on ACTUAL, which the filter never
# touches) still fires through the wander.


def _sway(t: float) -> tuple[float, float]:
    """A wander-shaped commanded pose: 4° pitch sway at a 2.5 s period."""
    return (4.0 * math.sin(2.0 * math.pi * t / 2.5), 0.0)


_PLANT_LAG = 0.3  # seconds — actual(t) = commanded(t - _PLANT_LAG) * _PLANT_GAIN
_PLANT_GAIN = 1.2  # the measured overshoot: the plant tracks 1.1-1.2x the command
_TICK = 0.02  # the engine's 50 Hz period


def _run_wander(
    driver: PatSenseDriver, reader: _Reader, *, seconds: float, pat_at: float | None = None
) -> None:
    """Drive ``seconds`` of 50 Hz wander; optionally superimpose a pat on ACTUAL.

    The pat is two 0.3 s presses of −3° pitch separated by a 0.3 s release,
    starting at ``pat_at`` — an external force the command stream knows nothing
    about, exactly like a hand.
    """
    ticks = int(seconds / _TICK)
    for i in range(ticks):
        t = T0 + i * _TICK
        cp, cy = _sway(t)
        lp, ly = _sway(t - _PLANT_LAG)
        ap, ay = lp * _PLANT_GAIN, ly * _PLANT_GAIN
        if pat_at is not None:
            dt_pat = t - pat_at
            if (0.0 <= dt_pat < 0.3) or (0.6 <= dt_pat < 0.9):
                ap -= 3.0
        _drive(driver, reader, (ap, ay), t, pitch=cp, yaw=cy)


def test_wandering_commanded_with_plant_lag_never_fires() -> None:
    """The live d1 scenario: continuous wander + plant lag -> ZERO pat events."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _run_wander(driver, reader, seconds=30.0)
    assert driver.events == 0
    assert driver.peek() is None


def test_wander_with_lag_fires_without_the_filter() -> None:
    """Regression guard: lag_tau=0 (raw passthrough) reproduces the false fires.

    Documents that the low-pass is the thing standing between the base layer's
    wander and phantom pats — if this starts failing, the sway/lag calibration
    no longer models the defect and the suite has lost its d1 coverage.
    """
    reader = _Reader()
    driver = PatSenseDriver(
        reader=reader, detector=_fixed_detector(), lag_tau=0.0, hp_tau=0.0, warmup_s=0.0
    )
    _run_wander(driver, reader, seconds=30.0)
    assert driver.events > 0


def test_real_pat_during_wander_still_fires() -> None:
    """A hand's impulse rides ACTUAL (unfiltered) -> detection survives the fix."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    # Warm the filter + baseline on pure wander first, then pat mid-wander.
    _run_wander(driver, reader, seconds=10.0, pat_at=T0 + 6.0)
    assert driver.events >= 1


# --------------------------------------------------------------------------- #
# Warmup mute — d1's second iteration (boot + post-gesture ghost fires)       #
# --------------------------------------------------------------------------- #
#
# reset()/boot wipes the EMA baseline, and until it reconverges (~2x its
# 6.7 s time constant) the unlearned frame offset + wander edges read as
# presses — observed live as a fire at boot and a fire seconds after a
# gesture ended. The warmup mutes LATCHING (never the detector update, which
# is the convergence itself) for warmup_s after boot and after every
# suspended -> resumed re-baseline.


def test_boot_ghost_is_muted_by_default_warmup() -> None:
    """A pat-shaped deviation inside the boot warmup window latches nothing."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector())  # default warmup
    _scratch_sequence(driver, reader, T0)
    assert driver.events == 0
    assert driver.peek() is None


def test_pat_after_boot_warmup_fires() -> None:
    """The same pat AFTER the warmup window fires normally."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=2.0)
    _drive(driver, reader, (0.0, 0.0), T0)  # first update arms warmup [T0, T0+2)
    _scratch_sequence(driver, reader, T0 + 2.5)
    assert driver.events == 1


def test_resume_keeps_learned_baseline_no_post_gesture_ghost() -> None:
    """The live post-nod ghost: a LEARNED frame offset + sub-threshold wobble
    must stay silent after a gesture ends. clear_presses() keeps the EMA
    baselines; a full reset() wipes them, and with the real slow alpha (0.003,
    ~6.7 s time constant) the decaying offset + wobble crosses the press/release
    band repeatedly and fires — pat.py's clear_presses docstring names this
    exact re-seeding chain, and it is what d1's second live iteration hit."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    # Phase 1 — learn the +3 deg yaw frame offset (pure offset: at most one
    # press edge, never min_presses). 50 s at 50 Hz ≈ 7.5 EMA time constants.
    t = T0
    for i in range(2500):
        t = T0 + i * 0.02
        _drive(driver, reader, (0.0, 3.0), t)
    assert driver.events == 0
    # Phase 2 — converged sanity: offset + 1 deg wobble stays under threshold.
    base = t + 0.02
    for i in range(500):
        tt = base + i * 0.02
        wobble = 1.0 * math.sin(2.0 * math.pi * tt / 1.0)
        _drive(driver, reader, (0.0, 3.0 + wobble), tt)
    assert driver.events == 0
    # Phase 3 — a gesture takes the head, then the resume edge; the same
    # offset + wobble continues. Baselines KEPT -> silent for the full 30 s.
    t2 = base + 500 * 0.02
    _drive(driver, reader, (0.0, 3.0), t2, owner=GESTURE_OWNER)
    base3 = t2 + 0.02
    for i in range(1500):
        tt = base3 + i * 0.02
        wobble = 1.0 * math.sin(2.0 * math.pi * tt / 1.0)
        _drive(driver, reader, (0.0, 3.0 + wobble), tt)
    assert driver.events == 0


def test_real_pat_right_after_resume_fires_no_deadzone() -> None:
    """clear_presses (not reset) means a pat seconds after a gesture still fires."""
    reader = _Reader()
    driver = PatSenseDriver(reader=reader, detector=_fixed_detector(), warmup_s=0.0)
    _drive(driver, reader, (0.0, 0.0), T0)
    _drive(driver, reader, (0.0, 0.0), T0 + 0.1, owner=GESTURE_OWNER)
    _drive(driver, reader, (0.0, 0.0), T0 + 0.2)  # resume: clear_presses only
    _scratch_sequence(driver, reader, T0 + 0.3)
    assert driver.events == 1
