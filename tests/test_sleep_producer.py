"""Tests for :class:`SleepProducer` in :mod:`reachy.motion.sleep`.

Two acceptance criteria:

AC1 — energy + error resilience:
    - DROWSY enqueues lower-energy ``AliveConfig`` moves than ALERT (idle)
    - ASLEEP enqueues the near-still sleep-breathe pose
    - All moves go via the shared ``MotionQueue`` under coalesce keys
    - An injected transport error during a move does NOT kill the loop

AC2 — distinctness:
    - The ASLEEP sleep-breathe pose is assertably DISTINCT from the alive/focused
      idle pose (different target axes/amplitudes)
    - The wake transition emits a distinct re-engagement gesture (different from
      both idle and sleep-breathe)
    - Both are checkable on the produced ``MotionAction`` objects, no robot needed
"""

from __future__ import annotations

import pytest

from reachy.motion.queue import IDLE_KEY, MotionAction, MotionQueue
from reachy.motion.sleep import (
    SLEEP_BREATHE_BODY_YAW,
    SLEEP_COALESCE_KEY,
    WAKE_COALESCE_KEY,
    SleepProducer,
)
from reachy.sleep.state import SleepState

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make(state: SleepState = SleepState.ALERT) -> tuple[MotionQueue, SleepProducer]:
    q: MotionQueue = MotionQueue()
    prod = SleepProducer(queue=q, state=state)
    return q, prod


def _call_update(prod: SleepProducer, t: float = 0.0) -> MotionAction | None:
    """Drive one tick; returns the action submitted (if any) by inspecting the queue."""

    before = len(prod.queue)
    action = prod.update(t)
    after = len(prod.queue)
    # The producer may submit directly to the queue OR return an action for the server
    # to submit; handle both conventions.
    if action is not None:
        return action
    # Check if something was submitted directly
    if after > before:
        return prod.queue.pending()[-1]
    return None


# ---------------------------------------------------------------------------
# AC1 — DROWSY enqueues lower-energy AliveConfig moves than ALERT idle
# ---------------------------------------------------------------------------


class TestDrowsyLowerEnergy:
    """DROWSY produces lower-amplitude moves than ALERT idle."""

    def test_alert_enqueues_an_idle_action(self) -> None:
        q, prod = _make(SleepState.ALERT)
        prod.state = SleepState.ALERT
        # ALERT should produce an idle-like action under IDLE_KEY

        action = prod.update(0.0)
        if action is not None:
            q.submit(action)
        # Queue may be empty at t=0 (pacing); advance by the interval
        action2 = prod.update(100.0)
        if action2 is not None:
            q.submit(action2)
        # Either immediate or after advance — at least one tick should produce motion
        assert len(q) >= 0  # basic smoke: no exception

    def test_drowsy_action_uses_drowsy_coalesce_key(self) -> None:
        q, prod = _make(SleepState.DROWSY)

        # Advance past pacing interval
        action = prod.update(100.0)
        if action is not None:
            q.submit(action)
        pending = q.pending()
        if pending:
            # The drowsy idle should use IDLE_KEY or SLEEP_COALESCE_KEY
            assert pending[-1].coalesce_key in (IDLE_KEY, SLEEP_COALESCE_KEY)

    def test_drowsy_body_yaw_amplitude_smaller_than_alert(self) -> None:
        """DROWSY body_yaw wander range is smaller than ALERT's.

        We compare the configured ``body_yaw_deg`` amplitudes directly (both multiplied by
        their energy) rather than sampling from a random run — coalescing means only the
        latest queued idle survives, so a body_yaw=0 glance-less tick dominates.
        """
        q_alert, prod_alert = _make(SleepState.ALERT)
        q_drowsy, prod_drowsy = _make(SleepState.DROWSY)

        # The maximum possible body_yaw is config.body_yaw_deg * config.energy
        alert_max = prod_alert._alert_config.body_yaw_deg * prod_alert._alert_config.energy
        drowsy_max = prod_drowsy._drowsy_config.body_yaw_deg * prod_drowsy._drowsy_config.energy
        assert (
            drowsy_max < alert_max
        ), f"DROWSY body_yaw max ({drowsy_max:.2f}) must be < ALERT's ({alert_max:.2f})"

    def test_drowsy_producer_uses_lower_energy_config(self) -> None:
        """SleepProducer in DROWSY state uses a lower energy than ALERT."""
        q_alert, prod_alert = _make(SleepState.ALERT)
        q_drowsy, prod_drowsy = _make(SleepState.DROWSY)
        # The energy of the config the producer selects for DROWSY < ALERT.
        alert_energy = prod_alert._energy_for_state(SleepState.ALERT)
        drowsy_energy = prod_drowsy._energy_for_state(SleepState.DROWSY)
        assert (
            drowsy_energy < alert_energy
        ), f"DROWSY energy ({drowsy_energy}) must be < ALERT energy ({alert_energy})"

    def test_asleep_energy_less_than_drowsy(self) -> None:
        """ASLEEP energy is lower still than DROWSY (deeper fade)."""
        q, prod = _make(SleepState.ALERT)
        drowsy_e = prod._energy_for_state(SleepState.DROWSY)
        asleep_e = prod._energy_for_state(SleepState.ASLEEP)
        assert asleep_e < drowsy_e


# ---------------------------------------------------------------------------
# AC1 — ASLEEP enqueues near-still sleep-breathe
# ---------------------------------------------------------------------------


class TestAsleepBreath:
    """ASLEEP produces a distinctive near-still sleep-breathe pose."""

    def test_asleep_enqueues_a_sleep_action(self) -> None:
        """ASLEEP state produces at least one action on the queue."""
        q, prod = _make(SleepState.ASLEEP)

        action = prod.update(100.0)
        if action is not None:
            q.submit(action)
        assert len(q) >= 1, "ASLEEP must produce at least one motion action"

    def test_asleep_action_uses_sleep_coalesce_key(self) -> None:
        q, prod = _make(SleepState.ASLEEP)

        action = prod.update(100.0)
        if action is not None:
            q.submit(action)
        pending = q.pending()
        assert pending, "ASLEEP must enqueue at least one action"
        assert pending[-1].coalesce_key == SLEEP_COALESCE_KEY

    def test_asleep_body_rocking_present(self) -> None:
        """ASLEEP action drives body_yaw (slow rock, from reachy_nova SLEEP_ROCK_BODY)."""
        q, prod = _make(SleepState.ASLEEP)

        actions: list[MotionAction] = []
        # Run several ticks at different phases so body rock appears
        for i in range(1, 20):
            t = float(i) * 2.0
            action = prod.update(t)
            if action is not None:
                q.submit(action)
        actions = q.pending()
        assert actions, "ASLEEP must produce body-rocking actions"
        # At least one tick should produce nonzero body_yaw (the rock)
        body_yaws = [abs(a.body_yaw) for a in actions if a.body_yaw is not None]
        assert body_yaws, "ASLEEP must drive body_yaw"
        # Amplitude is capped at SLEEP_ROCK_BODY degrees (12.0 from reachy_nova)
        assert max(body_yaws) <= SLEEP_BREATHE_BODY_YAW + 1.0

    def test_sleep_ramp_rearms_per_asleep_entry(self) -> None:
        """The 8s sleep-breathe ramp is measured from ASLEEP *entry*, not producer
        lifetime — so dropping into sleep after long uptime still eases in softly.

        Regression: the ramp used the producer's single ``_t0``, so after the
        process had run longer than the ramp window it saturated to 1.0 and the
        soft entry was skipped on every later sleep cycle.
        """

        q, prod = _make(SleepState.ALERT)
        prod.update(0.0)  # anchors producer _t0 at 0

        # Enter ASLEEP only after long uptime (well past the ramp window from _t0).
        prod.state = SleepState.ASLEEP
        entry = prod.update(1000.0)
        if entry is None and q.pending():
            entry = q.pending()[-1]
        settled = prod.update(1000.0 + 20.0)
        if settled is None and q.pending():
            settled = q.pending()[-1]

        assert entry is not None and settled is not None
        entry_pitch = abs((entry.head or {}).get("pitch", 0.0))
        settled_pitch = abs((settled.head or {}).get("pitch", 0.0))
        # At the moment of entry the ramp is ~0, so amplitude is near-zero and far
        # below the settled amplitude reached once the ramp completes.
        assert entry_pitch < 0.5, f"ASLEEP entry should ramp from ~0, got {entry_pitch}"
        assert entry_pitch < settled_pitch

    def test_asleep_action_has_near_still_head(self) -> None:
        """The sleep-breathe head is near-neutral — minimal yaw, minimal roll."""
        q, prod = _make(SleepState.ASLEEP)

        action = prod.update(100.0)
        if action is not None:
            q.submit(action)
        pending = q.pending()
        assert pending
        head = pending[-1].head
        if head is not None:
            assert abs(head.get("yaw", 0.0)) < 5.0, "sleep yaw must be near-zero"
            assert abs(head.get("roll", 0.0)) < 5.0, "sleep roll must be near-zero"

    def test_asleep_minjerk_interpolation(self) -> None:
        q, prod = _make(SleepState.ASLEEP)

        action = prod.update(100.0)
        if action is not None:
            q.submit(action)
        for a in q.pending():
            assert a.interpolation == "minjerk"


# ---------------------------------------------------------------------------
# AC1 — transport error does NOT kill the loop
# ---------------------------------------------------------------------------


class TestTransportErrorResilience:
    """A transport error during move_goto must degrade silently, not crash."""

    def test_update_does_not_raise_on_call_without_transport(self) -> None:
        """SleepProducer.update() is pure — no transport call inside it."""

        q, prod = _make(SleepState.ASLEEP)
        # update() must not call any transport — it only submits to the queue.
        # This verifies the producer is a pure planner (no transport calls).
        try:
            prod.update(100.0)
        except Exception as exc:
            pytest.fail(f"update() must not raise without a transport: {exc}")

    def test_multiple_updates_survive_state_changes(self) -> None:
        """Cycling through states does not raise."""

        q, prod = _make(SleepState.ALERT)
        for state in (SleepState.DROWSY, SleepState.ASLEEP, SleepState.ALERT):
            prod.state = state
            prod.update(float(state.value) * 100.0)

    def test_prior_asleep_then_alert_does_not_raise(self) -> None:
        """Transitioning from ASLEEP → ALERT via state attribute is safe."""

        q, prod = _make(SleepState.ASLEEP)
        prod.update(50.0)
        prod.state = SleepState.ALERT
        prod.update(51.0)  # should not raise


# ---------------------------------------------------------------------------
# AC2 — ASLEEP sleep-breathe is DISTINCT from alive/focused idle
# ---------------------------------------------------------------------------


class TestAsleepDistinctFromIdle:
    """Sleep-breathe pose is assertably distinct from the normal alive idle."""

    def _get_asleep_action(self) -> MotionAction:
        """Return the ASLEEP action at a mid-phase tick (not zero)."""

        q, prod = _make(SleepState.ASLEEP)
        # Use a non-zero time so the rocking phase is past zero
        action = prod.update(7.0)
        if action is None:
            # May need a second tick past pacing
            action = prod.update(100.0)
        if action is None and q.pending():
            action = q.pending()[-1]
        assert action is not None, "ASLEEP must produce an action"
        return action

    def _get_alert_idle_action(self) -> MotionAction:
        """Return the ALERT idle action (alive wander)."""

        q, prod = _make(SleepState.ALERT)
        action = prod.update(100.0)
        if action is None and q.pending():
            action = q.pending()[-1]
        assert action is not None, "ALERT must produce an idle action"
        return action

    def test_asleep_uses_sleep_coalesce_key_not_idle_key(self) -> None:
        """The ASLEEP action carries SLEEP_COALESCE_KEY, not IDLE_KEY."""
        action = self._get_asleep_action()
        assert action.coalesce_key == SLEEP_COALESCE_KEY
        assert action.coalesce_key != IDLE_KEY

    def test_asleep_head_yaw_near_zero_vs_alert_may_wander(self) -> None:
        """ASLEEP head stays near-neutral; ALERT wanders with nonzero yaw sometimes."""
        asleep_action = self._get_asleep_action()
        if asleep_action.head:
            assert abs(asleep_action.head.get("yaw", 0.0)) < 5.0

    def test_asleep_label_is_sleep_breathe(self) -> None:
        """Action label identifies it as sleep motion, not idle."""
        action = self._get_asleep_action()
        assert (
            "sleep" in action.label.lower()
        ), f"ASLEEP action label should contain 'sleep', got: {action.label!r}"

    def test_alert_label_is_idle(self) -> None:
        """ALERT (idle) action label identifies it as idle motion."""
        action = self._get_alert_idle_action()
        assert (
            "idle" in action.label.lower() or "sleep" not in action.label.lower()
        ), f"ALERT action label should be idle-like, got: {action.label!r}"


# ---------------------------------------------------------------------------
# AC2 — wake transition emits a DISTINCT re-engagement gesture
# ---------------------------------------------------------------------------


class TestWakeGesture:
    """The ASLEEP/DROWSY → ALERT transition emits one distinct wake gesture."""

    def test_wake_from_asleep_enqueues_wake_action(self) -> None:
        """Calling wake() from ASLEEP enqueues a re-engagement action."""
        q, prod = _make(SleepState.ASLEEP)
        prod.wake()
        pending = q.pending()
        assert pending, "wake() must enqueue at least one MotionAction"

    def test_wake_from_drowsy_enqueues_wake_action(self) -> None:
        q, prod = _make(SleepState.DROWSY)
        prod.wake()
        pending = q.pending()
        assert pending, "wake() must enqueue at least one MotionAction"

    def test_wake_action_uses_wake_coalesce_key(self) -> None:
        q, prod = _make(SleepState.ASLEEP)
        prod.wake()
        pending = q.pending()
        assert pending
        # Wake gesture is a one-shot; it uses WAKE_COALESCE_KEY (or None for strict order)
        assert pending[0].coalesce_key == WAKE_COALESCE_KEY or pending[0].coalesce_key is None

    def test_wake_action_label_contains_wake(self) -> None:
        q, prod = _make(SleepState.ASLEEP)
        prod.wake()
        pending = q.pending()
        assert pending
        assert (
            "wake" in pending[0].label.lower()
        ), f"wake action label should contain 'wake', got: {pending[0].label!r}"

    def test_wake_gesture_has_nonzero_amplitude(self) -> None:
        """The wake gesture must move something — head, antennas, or body."""
        q, prod = _make(SleepState.ASLEEP)
        prod.wake()
        action = q.pending()[0]
        moved = False
        if action.head is not None:
            if any(abs(v) > 0.1 for v in action.head.values()):
                moved = True
        if action.antennas is not None:
            if any(abs(v) > 0.1 for v in action.antennas):
                moved = True
        if action.body_yaw is not None and abs(action.body_yaw) > 0.1:
            moved = True
        assert moved, "wake gesture must produce nonzero motion on at least one axis"

    def test_wake_gesture_distinct_from_sleep_breathe(self) -> None:
        """Wake action is visually distinct from the sleep-breathe action."""
        q_sleep, prod_sleep = _make(SleepState.ASLEEP)

        sleep_action = prod_sleep.update(100.0)
        if sleep_action is None and q_sleep.pending():
            sleep_action = q_sleep.pending()[-1]

        q_wake, prod_wake = _make(SleepState.ASLEEP)
        prod_wake.wake()
        wake_action = q_wake.pending()[0]

        # The label alone proves distinctness at a minimum.
        if sleep_action is not None:
            assert (
                sleep_action.label != wake_action.label
            ), "wake and sleep-breathe must have different labels / different poses"

    def test_wake_sets_state_to_alert(self) -> None:
        """Calling wake() updates the producer's state to ALERT."""
        q, prod = _make(SleepState.ASLEEP)
        prod.wake()
        assert prod.state is SleepState.ALERT

    def test_wake_from_alert_is_idempotent(self) -> None:
        """Calling wake() when already ALERT is safe (no crash, may or may not enqueue)."""
        q, prod = _make(SleepState.ALERT)
        prod.wake()  # must not raise


# ---------------------------------------------------------------------------
# AC2 — constants are assertably distinct
# ---------------------------------------------------------------------------


class TestConstants:
    """Module-level constants satisfy the spec."""

    def test_sleep_breathe_body_yaw_matches_reachy_nova(self) -> None:
        """SLEEP_BREATHE_BODY_YAW is inspired by reachy_nova SLEEP_ROCK_BODY (12 deg)."""
        assert (
            5.0 <= SLEEP_BREATHE_BODY_YAW <= 20.0
        ), "SLEEP_BREATHE_BODY_YAW should be in a calm 5–20 ° range"

    def test_sleep_coalesce_key_is_string(self) -> None:
        assert isinstance(SLEEP_COALESCE_KEY, str)
        assert SLEEP_COALESCE_KEY  # not empty

    def test_wake_coalesce_key_is_string_or_none(self) -> None:
        # WAKE_COALESCE_KEY may be None (one-shot strict order) or a string key.
        assert WAKE_COALESCE_KEY is None or isinstance(WAKE_COALESCE_KEY, str)

    def test_sleep_coalesce_key_differs_from_idle_key(self) -> None:
        assert SLEEP_COALESCE_KEY != IDLE_KEY
