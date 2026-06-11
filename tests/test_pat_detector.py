"""Tests for PatDetector in reachy.motion.pat.

TDD — these tests define the contract; the implementation must satisfy them.

Coverage:
1. A pitch-press impulse sequence yields a level1 ("scratch") event.
2. A yaw-press impulse sequence yields a level1 ("side_pat") event.
3. Sub-threshold deviation yields no event.
4. The cooldown suppresses an immediate re-fire after a level1 event.
"""

from __future__ import annotations

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
