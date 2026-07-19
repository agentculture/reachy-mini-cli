"""Tests for PatDetector in reachy.motion.pat.

TDD — these tests define the contract; the implementation must satisfy them.

Coverage:
1. A pitch-press impulse sequence yields a level1 ("scratch") event.
2. A yaw-press impulse sequence yields a level1 ("side_pat") event.
3. Sub-threshold deviation yields no event.
4. The cooldown suppresses an immediate re-fire after a level1 event.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from reachy.motion.pat import PatDetector

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pitch_press(detector: PatDetector, now: float, *, n: int = 3) -> list:
    """Feed *n* distinct pitch-press impulses.

    Each impulse: one sample well below -press_threshold (pressed), then one
    sample back to 0 (released).  Returns list of non-None results.
    """
    events = []
    for i in range(n):
        t_press = now + i * 0.4
        result = detector.update(0.0, -5.0, now=t_press)  # clear press
        if result is not None:
            events.append(result)
        t_release = t_press + 0.1
        result = detector.update(0.0, 0.0, now=t_release)  # release
        if result is not None:
            events.append(result)
    return events


def _yaw_press(detector: PatDetector, now: float, *, n: int = 3) -> list:
    """Feed *n* distinct yaw-press impulses."""
    events = []
    for i in range(n):
        t_press = now + i * 0.4
        result = detector.update(0.0, 0.0, 0.0, 5.0, now=t_press)  # yaw press
        if result is not None:
            events.append(result)
        t_release = t_press + 0.1
        result = detector.update(0.0, 0.0, 0.0, 0.0, now=t_release)  # release
        if result is not None:
            events.append(result)
    return events


# ---------------------------------------------------------------------------
# Test 1: pitch-press impulses → level1 "scratch"
# ---------------------------------------------------------------------------


def test_pitch_press_yields_level1_scratch():
    """Feeding repeated pitch-press impulses must fire ("level1", "scratch")."""
    det = PatDetector(level2_threshold_fn=lambda: 6.0)
    now = 1000.0
    events = _pitch_press(det, now, n=3)
    assert len(events) >= 1, "Expected at least one detection event"
    assert events[0] == ("level1", "scratch"), f"Unexpected event: {events[0]}"


# ---------------------------------------------------------------------------
# Test 2: yaw-press impulses → level1 "side_pat"
# ---------------------------------------------------------------------------


def test_yaw_press_yields_level1_side_pat():
    """Feeding repeated yaw-press impulses must fire ("level1", "side_pat")."""
    det = PatDetector(level2_threshold_fn=lambda: 6.0)
    now = 2000.0
    events = _yaw_press(det, now, n=3)
    assert len(events) >= 1, "Expected at least one detection event"
    assert events[0] == ("level1", "side_pat"), f"Unexpected event: {events[0]}"


# ---------------------------------------------------------------------------
# Test 3: sub-threshold deviation → no event
# ---------------------------------------------------------------------------


def test_subthreshold_yields_no_event():
    """Tiny deviations (below press_threshold) must not produce any event."""
    det = PatDetector()
    now = 3000.0
    for i in range(20):
        result = det.update(0.0, -0.3, 0.0, 0.2, now=now + i * 0.2)
        assert result is None, f"Unexpected event at step {i}: {result}"


# ---------------------------------------------------------------------------
# Test 4: cooldown suppresses immediate re-fire
# ---------------------------------------------------------------------------


def test_cooldown_suppresses_refire():
    """After a level1 event fires, another burst within pat_cooldown must be silent."""
    det = PatDetector(level2_threshold_fn=lambda: 6.0)
    now = 4000.0

    # First burst — expect level1
    events_first = _pitch_press(det, now, n=3)
    assert len(events_first) >= 1
    assert events_first[0][0] == "level1"

    # Immediately try a second burst — starts right after the first
    now2 = now + 0.5  # well within pat_cooldown (default 2.0s)
    events_second = _pitch_press(det, now2, n=3)
    assert len(events_second) == 0, f"Expected no event within cooldown, got: {events_second}"


# ---------------------------------------------------------------------------
# Test 5: level2 fires when sustained interaction continues past threshold
# ---------------------------------------------------------------------------


def test_sustained_interaction_yields_level2():
    """Continuing to pat past the level2_threshold must fire ("level2", touch_type)."""
    fixed_l2 = 4.0
    det = PatDetector(level2_threshold_fn=lambda: fixed_l2)
    now = 5000.0

    # Trigger level1
    events = _pitch_press(det, now, n=3)
    assert events and events[0][0] == "level1"

    # Keep pressing past level2 threshold
    l2_start = now + 1.2  # just after level1 fires
    l2_presses = _pitch_press(det, l2_start + fixed_l2 + 0.1, n=1)
    # Feed one idle sample well past threshold so state machine advances
    result = det.update(0.0, 0.0, now=l2_start + fixed_l2 + 0.5)
    if result is not None:
        l2_presses.append(result)

    all_events = events + l2_presses
    levels = [e[0] for e in all_events]
    assert "level2" in levels, f"Expected level2 in events, got: {all_events}"


# ---------------------------------------------------------------------------
# Test 6: no reachy_mini import (stdlib + numpy only)
# ---------------------------------------------------------------------------


def test_no_reachy_mini_import():
    """pat.py must not import reachy_mini — only numpy + stdlib."""
    import sys

    # Remove cached module if present
    for key in list(sys.modules.keys()):
        if "reachy.motion.pat" in key:
            del sys.modules[key]

    import inspect

    import reachy.motion.pat as pat_module

    src = inspect.getsource(pat_module)
    assert "reachy_mini" not in src, "pat.py must not import reachy_mini"
    assert "import numpy" in src or "import numpy as np" in src, "pat.py must use numpy"


def test_snapshot_adds_signed_evidence_without_changing_legacy_tuple() -> None:
    det = PatDetector(
        min_presses=2,
        pat_cooldown=0.0,
        baseline_alpha=0.0,
        level2_threshold_fn=lambda: 6.0,
    )

    assert det.update(0.0, 0.0, 0.0, 3.0, now=10.0) is None
    det.update(0.0, 0.0, 0.0, 0.0, now=10.1)
    event = det.update(0.0, 0.0, 0.0, 4.0, now=10.4)

    assert event == ("level1", "side_pat")
    evidence = det.snapshot()
    assert evidence.pressed is True
    assert evidence.touch_type == "side_pat"
    assert evidence.level == "level1"
    assert evidence.yaw_deg == pytest.approx(4.0)
    assert evidence.last_press_at == pytest.approx(10.4)
    with pytest.raises(FrozenInstanceError):
        evidence.yaw_deg = -4.0  # type: ignore[misc]


def test_snapshot_uses_latest_qualifying_yaw_and_ignores_deadband() -> None:
    det = PatDetector(min_presses=99, baseline_alpha=0.0)

    det.update(0.0, 0.0, 0.0, -3.0, now=20.0)
    negative = det.snapshot()
    det.update(0.0, 0.0, 0.0, 0.0, now=20.1)
    det.update(0.0, 0.0, 0.0, 0.2, now=20.2)
    deadband = det.snapshot()
    det.update(0.0, 0.0, 0.0, 3.5, now=20.5)
    positive = det.snapshot()
    det.update(0.0, 0.0, 0.0, 0.0, now=20.6)
    det.update(0.0, 0.0, 0.0, 4.0, now=20.9)
    repeated = det.snapshot()

    assert negative.yaw_deg == pytest.approx(-3.0)
    assert deadband.yaw_deg == pytest.approx(-3.0)
    assert deadband.last_press_at == negative.last_press_at
    assert positive.yaw_deg == pytest.approx(3.5)
    assert repeated.yaw_deg == pytest.approx(4.0)
    assert repeated.last_press_at == pytest.approx(20.9)


def test_simultaneous_axes_use_normalized_dominance_with_pitch_winning_ties() -> None:
    det = PatDetector(
        min_presses=99,
        press_threshold=1.0,
        yaw_press_threshold=1.0,
        baseline_alpha=0.0,
    )

    det.update(0.0, -3.0, 0.0, 2.0, now=30.0)
    pitch_dominant = det.snapshot()
    det.update(0.0, 0.0, 0.0, 0.0, now=30.1)
    det.update(0.0, -2.0, 0.0, 4.0, now=30.5)
    yaw_dominant = det.snapshot()
    det.update(0.0, 0.0, 0.0, 0.0, now=30.6)
    det.update(0.0, -3.0, 0.0, -3.0, now=31.0)
    tie = det.snapshot()

    assert pitch_dominant.touch_type == "scratch"
    assert pitch_dominant.yaw_deg is None
    assert yaw_dominant.touch_type == "side_pat"
    assert yaw_dominant.yaw_deg == pytest.approx(4.0)
    assert tie.touch_type == "scratch"
    assert tie.yaw_deg is None


def _level1_with_exactly_two_presses(det: PatDetector, now: float) -> float:
    assert det.update(0.0, -5.0, now=now) is None
    assert det.update(0.0, 0.0, now=now + 0.1) is None
    level1_at = now + 0.4
    assert det.update(0.0, -5.0, now=level1_at) == ("level1", "scratch")
    assert det.update(0.0, 0.0, now=level1_at + 0.1) is None
    return level1_at


def test_level1_followed_by_silence_cannot_invent_level2() -> None:
    det = PatDetector(
        pat_cooldown=0.0,
        interaction_gap_timeout=5.0,
        level2_threshold_fn=lambda: 0.5,
    )
    level1_at = _level1_with_exactly_two_presses(det, 40.0)

    assert det.update(0.0, 0.0, now=level1_at + 0.6) is None
    assert det.snapshot().level == "level1"

    assert det.update(0.0, 0.0, now=level1_at + 5.1) is None
    released = det.snapshot()
    assert released.pressed is False
    assert released.touch_type is None
    assert released.level is None
    assert released.yaw_deg is None
    assert released.last_press_at is None


def test_fresh_post_level1_press_can_escalate_and_cooldown_clears() -> None:
    det = PatDetector(
        pat_cooldown=0.0,
        level2_cooldown=0.5,
        level2_threshold_fn=lambda: 0.5,
    )
    level1_at = _level1_with_exactly_two_presses(det, 50.0)

    event = det.update(0.0, -5.0, now=level1_at + 0.6)
    assert event == ("level2", "scratch")
    assert det.snapshot().level == "level2"

    det.update(0.0, 0.0, now=level1_at + 0.7)
    assert det.update(0.0, 0.0, now=level1_at + 1.2) is None
    assert det.snapshot().touch_type is None
    assert det.snapshot().level is None
    assert det.snapshot().last_press_at is None


def test_clear_presses_clears_evidence_but_keeps_ema_baseline() -> None:
    det = PatDetector(min_presses=99, baseline_alpha=0.5)
    det.update(0.0, 0.0, 0.0, -3.0, now=60.0)
    learned_yaw_baseline = det._yaw_baseline_offset
    assert det.snapshot().touch_type == "side_pat"

    det.clear_presses()

    assert det._yaw_baseline_offset == learned_yaw_baseline
    assert det.snapshot().pressed is False
    assert det.snapshot().touch_type is None
    assert det.snapshot().level is None
    assert det.snapshot().yaw_deg is None
    assert det.snapshot().last_press_at is None
