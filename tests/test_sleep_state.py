"""Tests for the pure SleepStateMachine in reachy.sleep.state."""

from __future__ import annotations

import pytest

from reachy.sleep.state import SleepState, SleepStateMachine

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def make_machine(drowsy_after: float = 5.0, asleep_after: float = 10.0) -> SleepStateMachine:
    """Return a machine with tiny thresholds for deterministic tests."""
    return SleepStateMachine(drowsy_after=drowsy_after, asleep_after=asleep_after)


# ---------------------------------------------------------------------------
# Test 1 — state progression with a fake clock
# ---------------------------------------------------------------------------


class TestStateProgression:
    """With no stimulation the machine steps ALERT->DROWSY->ASLEEP."""

    def test_initial_state_is_alert(self) -> None:
        m = make_machine()
        assert m.state is SleepState.ALERT

    def test_stays_alert_before_drowsy_threshold(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=4.9)
        assert m.state is SleepState.ALERT

    def test_transitions_to_drowsy_at_threshold(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=5.0)
        assert m.state is SleepState.DROWSY

    def test_transitions_to_drowsy_past_threshold(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=7.0)
        assert m.state is SleepState.DROWSY

    def test_transitions_to_asleep_at_threshold(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=10.0)
        assert m.state is SleepState.ASLEEP

    def test_transitions_to_asleep_past_threshold(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=99.0)
        assert m.state is SleepState.ASLEEP

    def test_full_alert_to_drowsy_to_asleep_sequence(self) -> None:
        """Verify the full ALERT -> DROWSY -> ASLEEP progression step-by-step."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        assert m.state is SleepState.ALERT

        m.update(now=5.0)
        assert m.state is SleepState.DROWSY

        m.update(now=10.0)
        assert m.state is SleepState.ASLEEP


# ---------------------------------------------------------------------------
# Test 2 — reset zeroes the idle clock and returns to ALERT
# ---------------------------------------------------------------------------


class TestReset:
    """reset() zeroes the idle clock and snaps back to ALERT."""

    def test_reset_from_drowsy_returns_to_alert(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=5.0)
        assert m.state is SleepState.DROWSY
        m.reset(now=5.0)
        assert m.state is SleepState.ALERT

    def test_reset_from_asleep_returns_to_alert(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=10.0)
        assert m.state is SleepState.ASLEEP
        m.reset(now=10.0)
        assert m.state is SleepState.ALERT

    def test_reset_zeroes_idle_clock(self) -> None:
        """After reset, idle_seconds starts from zero again."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=8.0)
        m.reset(now=8.0)
        # Now advance just 3s — still under drowsy threshold.
        m.update(now=11.0)
        assert m.state is SleepState.ALERT

    def test_reset_then_full_cycle_works_again(self) -> None:
        """After reset the machine can cycle through states again."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=10.0)
        assert m.state is SleepState.ASLEEP
        m.reset(now=10.0)
        assert m.state is SleepState.ALERT
        m.update(now=15.0)
        assert m.state is SleepState.DROWSY
        m.update(now=20.0)
        assert m.state is SleepState.ASLEEP

    def test_idle_seconds_after_reset(self) -> None:
        """idle_seconds reflects time elapsed since the most recent reset."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=8.0)
        m.reset(now=8.0)
        m.update(now=11.0)
        assert pytest.approx(m.idle_seconds) == 3.0

    def test_reset_clamps_backwards_now(self) -> None:
        """reset() clamps a stale backwards ``now`` just like update() does, so the
        idle clock never lands in the past (consistency with the class contract)."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=10.0)
        # A stale reset arriving with now < last seen time must not anchor the
        # idle clock ahead of real time.
        m.reset(now=4.0)
        # idle_seconds is measured from the clamped anchor (10.0), so at a later
        # forward tick it reflects only the real elapsed time.
        m.update(now=13.0)
        assert pytest.approx(m.idle_seconds) == 3.0
        assert m.idle_seconds >= 0.0


# ---------------------------------------------------------------------------
# Test 3 — snapshot fields and default thresholds
# ---------------------------------------------------------------------------


class TestSnapshot:
    """idle_seconds and state are always exposed; defaults are in the ~75/150s range."""

    def test_idle_seconds_reflects_elapsed_time(self) -> None:
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=3.0)
        assert pytest.approx(m.idle_seconds) == 3.0

    def test_idle_seconds_zero_at_start(self) -> None:
        m = make_machine()
        m.update(now=100.0)  # first call — anchor set here
        assert pytest.approx(m.idle_seconds) == 0.0

    def test_default_drowsy_threshold_approx_75s(self) -> None:
        """Default drowsy threshold is in the 60-90 second range."""
        m = SleepStateMachine()
        assert 60 <= m.drowsy_after <= 90

    def test_default_asleep_threshold_approx_150s(self) -> None:
        """Default asleep threshold is in the 120-180 second range."""
        m = SleepStateMachine()
        assert 120 <= m.asleep_after <= 180

    def test_asleep_after_greater_than_drowsy_after(self) -> None:
        """asleep_after must be strictly greater than drowsy_after."""
        m = SleepStateMachine()
        assert m.asleep_after > m.drowsy_after

    def test_state_enum_members_exist(self) -> None:
        assert SleepState.ALERT is SleepState.ALERT
        assert SleepState.DROWSY is SleepState.DROWSY
        assert SleepState.ASLEEP is SleepState.ASLEEP


# ---------------------------------------------------------------------------
# Test 4 — purely headless: no real-time calls inside logic
# ---------------------------------------------------------------------------


class TestPurity:
    """state.py is pure: time is always injected; no hidden side-effects."""

    def test_multiple_updates_same_now_are_idempotent(self) -> None:
        """Calling update twice with the same now= doesn't advance state."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=4.9)
        state_before = m.state
        m.update(now=4.9)
        assert m.state is state_before

    def test_backwards_clock_does_not_regress_state(self) -> None:
        """If now goes backwards (stale tick), idle_seconds should not go negative."""
        m = make_machine(drowsy_after=5.0, asleep_after=10.0)
        m.update(now=0.0)
        m.update(now=8.0)
        m.update(now=3.0)  # backwards tick — should not crash or go negative
        assert m.idle_seconds >= 0.0

    def test_constructor_custom_thresholds(self) -> None:
        """Constructor-supplied thresholds override defaults."""
        m = SleepStateMachine(drowsy_after=1.0, asleep_after=2.0)
        assert m.drowsy_after == 1.0
        assert m.asleep_after == 2.0
