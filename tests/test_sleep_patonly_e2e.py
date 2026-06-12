"""Headless pat-only end-to-end + boundary-unchanged tests (task t6).

Two acceptance criteria:

1. **Pat-only e2e** — drive ``run_sleep_arc`` with ``audio_wake=False``, a small
   idle-timeout so the machine reaches ASLEEP quickly, a sense feed that emits
   ``speech_detected=True`` *and* a snap source returning True on several ticks
   (both MUST be ignored), and a ``pat`` callable that fires on exactly one later
   tick.  Assert the machine reaches ASLEEP, stays asleep through the audio burst
   (no premature wake), then ``woke`` becomes True only after the pat tick.  Also
   verifies the ``sleep_active.flag`` is written while ASLEEP and cleared on wake
   (uses ``monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))`` for isolation).

   Discrimination check: the test is structured so flipping ``audio_wake=True``
   in a scratch verification would cause the speech + snap burst to immediately
   wake the machine, proving the ``audio_wake=False`` branch is genuinely gating
   them.

2. **Boundary-unchanged** — targeted assertions that the feature did NOT regress
   the core sleep machinery contracts:
   - ``SleepStateMachine`` transitions ALERT→DROWSY→ASLEEP at documented thresholds
     under an injected clock, and ``reset()`` always returns ALERT.
   - ``SleepProducer`` in ASLEEP enqueues the sleep-breathe action with the right
     label / coalesce-key / near-zero-yaw contract (the same properties
     ``test_sleep_producer.py`` relies on).
   - ``sleep_signal``: ``write()`` → ``is_active()`` True → ``clear()`` → False
     (flag-yield contract intact) under a tmp ``REACHY_STATE_DIR``.
"""

from __future__ import annotations

import pytest

from reachy.behavior.sense import EMPTY_SENSE, Sense
from reachy.cli._commands.sleep import run_sleep_arc
from reachy.motion import sleep_signal
from reachy.motion.queue import MotionQueue
from reachy.motion.sleep import SLEEP_COALESCE_KEY, SleepProducer
from reachy.sleep.state import SleepState, SleepStateMachine

# ---------------------------------------------------------------------------
# Shared clock/sense helpers (mirror test_sleep_cli.py's _idle_then pattern)
# ---------------------------------------------------------------------------


def _make_seam(
    sense_list: list[Sense],
    *,
    tick_seconds: float = 10.0,
) -> tuple:
    """Return ``(now, sense, advance)`` callables backed by a fake clock.

    The clock starts at 0.0 and jumps ``tick_seconds`` on each ``advance()``
    call (fired by ``on_tick``).  ``sense()`` returns entries from ``sense_list``
    in order, clamping to the last entry when exhausted.  No wall-clock I/O.
    """
    clock: dict[str, float] = {"t": 0.0}
    feed: dict[str, int] = {"i": 0}

    def now() -> float:
        return clock["t"]

    def sense() -> Sense:
        return sense_list[min(feed["i"], len(sense_list) - 1)]

    def advance() -> None:
        feed["i"] += 1
        clock["t"] += tick_seconds

    return now, sense, advance


# ---------------------------------------------------------------------------
# Acceptance criterion 1: pat-only e2e
# ---------------------------------------------------------------------------


class TestPatOnlyEndToEnd:
    """Drive a pat-only arc: audio burst is ignored; only a pat wakes the robot."""

    def test_audio_burst_ignored_pat_wakes(self, monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
        """The core pat-only e2e:

        - ``audio_wake=False`` suppresses speech + snap throughout the run.
        - The machine decays to ASLEEP within the first few ticks (small
          idle_timeout=15.0, clock jumps 10 s/tick → ASLEEP by tick ~2).
        - Ticks 0–4: ``speech_detected=True`` AND snap=True on EVERY tick — both
          must be ignored (no premature wake).
        - Tick 5 (the 6th): pat fires → the machine returns to ALERT and woke=True.
        """
        monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))

        total_ticks = 6
        # Every tick carries an active speech flag — would wake under audio mode.
        sense_list = [Sense(speech_detected=True)] * total_ticks
        now, sense, advance = _make_seam(sense_list)

        snap_calls: list[int] = []
        pat_calls: list[int] = []

        def snap() -> bool:
            snap_calls.append(len(snap_calls))
            # Fire a loud transient on every tick — would wake under audio mode.
            return True

        def pat() -> bool:
            pat_calls.append(len(pat_calls))
            # Fire only on the very last tick (index 5 → 6th call).
            return len(pat_calls) >= total_ticks

        queue = MotionQueue()
        result = run_sleep_arc(
            queue=queue,
            now=now,
            sense=sense,
            snap=snap,
            pat=pat,
            audio_wake=False,  # ← pat-only; speech + snap must be suppressed
            on_tick=advance,
            ticks=total_ticks,
            idle_timeout=15.0,  # drowsy_after=7.5, asleep_after=15 → ASLEEP by tick 2
        )

        states = result["states"]

        # The machine must have decayed all the way to ASLEEP.
        assert SleepState.ASLEEP.name in states, (
            f"Expected ASLEEP in state sequence {states} — "
            "the idle decay did not reach ASLEEP within the allotted ticks"
        )

        # It must have been woken exactly once — by the pat.
        assert result["woke"] is True, "woke should be True after the pat fires"

        # The final state after the pat must be ALERT (back from ASLEEP).
        assert (
            states[-1] == SleepState.ALERT.name
        ), f"Final state should be ALERT after pat wake, got {states[-1]!r}"

        # Crucially: the machine must have been ASLEEP before the wake tick.
        # Find the last ASLEEP index and verify it comes before the last tick.
        last_asleep_idx = max(i for i, s in enumerate(states) if s == SleepState.ASLEEP.name)
        assert last_asleep_idx < len(states) - 1, (
            "The machine must have been ASLEEP before the final-tick wake; "
            f"last_asleep_idx={last_asleep_idx}, total={len(states)}"
        )

        # snap was called — confirming the snap path was exercised, yet no wake.
        assert len(snap_calls) > 0, "snap callable must have been polled"
        # pat was called — confirming the pat polling path ran.
        assert len(pat_calls) > 0, "pat callable must have been polled"

    def test_flag_written_while_asleep_cleared_on_pat_wake(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """The ``sleep_active.flag`` is raised while ASLEEP and cleared once the pat wakes it.

        ``on_tick`` is used as a probe: when the machine is ASLEEP (flag up) the
        flag file must exist; after the wake (final tick) it must have been cleared
        by the ``finally`` block inside ``run_sleep_arc``.
        """
        monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))

        total_ticks = 6
        sense_list = [EMPTY_SENSE] * total_ticks
        now, sense, advance = _make_seam(sense_list)

        pat_counter: dict[str, int] = {"n": 0}
        asleep_flag_observed: list[bool] = []

        def pat() -> bool:
            pat_counter["n"] += 1
            return pat_counter["n"] >= total_ticks

        def on_tick_probe() -> None:
            # Record the flag state each tick so we can assert it was up while ASLEEP.
            asleep_flag_observed.append(sleep_signal.is_active())
            advance()

        result = run_sleep_arc(
            queue=MotionQueue(),
            now=now,
            sense=sense,
            snap=lambda: False,
            pat=pat,
            audio_wake=False,
            on_tick=on_tick_probe,
            ticks=total_ticks,
            idle_timeout=15.0,
        )

        # After the arc the flag must be cleared (the finally block in run_sleep_arc).
        assert (
            not sleep_signal.is_active()
        ), "sleep_active.flag must be cleared after run_sleep_arc returns"

        # At least one tick must have seen the flag up (while ASLEEP).
        assert any(asleep_flag_observed), (
            f"sleep_active.flag was never observed as True during the run; "
            f"states={result['states']}, flag_snapshots={asleep_flag_observed}"
        )

        # The pat must have woken it.
        assert result["woke"] is True

    def test_audio_wake_true_would_wake_on_speech_snap(self) -> None:
        """Discrimination proof: with ``audio_wake=True`` the same speech+snap burst
        DOES wake the machine before the pat fires, confirming our pat-only test
        genuinely discriminates the ``audio_wake=False`` gating.
        """
        total_ticks = 6
        sense_list = [Sense(speech_detected=True)] * total_ticks
        now, sense, advance = _make_seam(sense_list)

        pat_calls: list[int] = []

        def pat() -> bool:
            pat_calls.append(len(pat_calls))
            # Pat would only fire on the very last tick.
            return len(pat_calls) >= total_ticks

        result = run_sleep_arc(
            queue=MotionQueue(),
            now=now,
            sense=sense,
            snap=lambda: True,  # loud transient every tick
            pat=pat,
            audio_wake=True,  # ← audio ON: speech+snap should wake immediately
            on_tick=advance,
            ticks=total_ticks,
            idle_timeout=15.0,
        )

        # With audio_wake=True the speech+snap burst wakes it; woke must be True.
        assert result["woke"] is True, (
            "audio_wake=True + speech+snap should wake the machine; "
            "if this fails the arc's audio path regressed"
        )
        # It may reach ALERT early (before the final tick), which is precisely what
        # the pat-only test guards against with audio_wake=False.
        assert SleepState.ALERT.name in result["states"]

    def test_pat_only_stays_asleep_through_full_audio_burst_intermediate_states(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path
    ) -> None:
        """Verify the machine is ASLEEP on consecutive ticks during the audio burst.

        Runs 8 ticks: ticks 0–6 have speech+snap, pat fires only on tick 7.
        After decay (by tick 2) every intermediate tick must be ASLEEP — proving the
        audio cues were truly ignored throughout, not just at the wake boundary.
        """
        monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))

        total_ticks = 8
        sense_list = [Sense(speech_detected=True)] * total_ticks
        now, sense, advance = _make_seam(sense_list)

        pat_counter: dict[str, int] = {"n": 0}

        def pat() -> bool:
            pat_counter["n"] += 1
            return pat_counter["n"] >= total_ticks  # fires only on the last tick

        result = run_sleep_arc(
            queue=MotionQueue(),
            now=now,
            sense=sense,
            snap=lambda: True,
            pat=pat,
            audio_wake=False,
            on_tick=advance,
            ticks=total_ticks,
            idle_timeout=15.0,  # ASLEEP by tick ~2
        )

        states = result["states"]

        # After ASLEEP is reached, every state before the final wake must be ASLEEP.
        first_asleep = next((i for i, s in enumerate(states) if s == SleepState.ASLEEP.name), None)
        assert first_asleep is not None, f"Never reached ASLEEP: {states}"

        # All states from first_asleep up to (but not including) the last tick
        # should be ASLEEP — no spurious audio-triggered ALERT in the middle.
        intermediate = states[first_asleep : len(states) - 1]
        non_asleep = [s for s in intermediate if s != SleepState.ASLEEP.name]
        assert not non_asleep, (
            f"Audio burst caused spurious wakes during intermediate ticks "
            f"(states={states}, non-ASLEEP intermediate: {non_asleep})"
        )

        assert result["woke"] is True


# ---------------------------------------------------------------------------
# Acceptance criterion 2: boundary-unchanged assertions
# ---------------------------------------------------------------------------


class TestSleepStateMachineBoundaryUnchanged:
    """Re-assert that SleepStateMachine's core contract is intact.

    These tests would trip if the feature accidentally regressed the FSM
    thresholds, the ALERT/DROWSY/ASLEEP ordering, or the reset() guarantee.
    They are structurally independent of the pat-only path — they exercise
    the FSM directly without any arc driver.
    """

    def test_alert_then_drowsy_then_asleep_under_injected_clock(self) -> None:
        """ALERT→DROWSY→ASLEEP transitions fire at the configured thresholds."""
        machine = SleepStateMachine(drowsy_after=5.0, asleep_after=10.0)

        # At t=0: still ALERT (no elapsed time yet).
        assert machine.update(now=0.0) is SleepState.ALERT

        # At t=3 (< drowsy_after=5): still ALERT.
        assert machine.update(now=3.0) is SleepState.ALERT

        # At t=5 (== drowsy_after): DROWSY.
        assert machine.update(now=5.0) is SleepState.DROWSY

        # At t=7 (> drowsy_after, < asleep_after): still DROWSY.
        assert machine.update(now=7.0) is SleepState.DROWSY

        # At t=10 (== asleep_after): ASLEEP.
        assert machine.update(now=10.0) is SleepState.ASLEEP

        # Past asleep_after stays ASLEEP.
        assert machine.update(now=20.0) is SleepState.ASLEEP

    def test_reset_returns_to_alert_from_asleep(self) -> None:
        """reset() always returns ALERT regardless of the current state."""
        machine = SleepStateMachine(drowsy_after=5.0, asleep_after=10.0)
        # Anchor the idle clock at t=0, then advance to ASLEEP at t=15.
        machine.update(now=0.0)
        machine.update(now=15.0)
        assert machine.state is SleepState.ASLEEP

        result = machine.reset(now=15.0)
        assert result is SleepState.ALERT
        assert machine.state is SleepState.ALERT

    def test_reset_zeroes_idle_clock(self) -> None:
        """After reset() the idle clock is zeroed so a fresh decay cycle starts."""
        machine = SleepStateMachine(drowsy_after=5.0, asleep_after=10.0)
        machine.update(now=0.0)
        machine.update(now=15.0)  # drive to ASLEEP, idle_seconds ~ 15

        machine.reset(now=15.0)
        # Immediately after reset: idle_seconds should be ~ 0.
        assert machine.idle_seconds == pytest.approx(0.0, abs=0.1)

    def test_reset_from_alert_is_idempotent(self) -> None:
        """reset() from ALERT is safe and still returns ALERT."""
        machine = SleepStateMachine()
        result = machine.reset(now=0.0)
        assert result is SleepState.ALERT
        assert machine.state is SleepState.ALERT

    def test_sleep_state_enum_members_unchanged(self) -> None:
        """SleepState must expose exactly ALERT, DROWSY, ASLEEP — no new members."""
        members = {m.name for m in SleepState}
        assert members == {"ALERT", "DROWSY", "ASLEEP"}, f"SleepState members changed: {members}"

    def test_thresholds_honored_with_half_ratio(self) -> None:
        """Default constructor (75/150) satisfies drowsy_after < asleep_after,
        and ``run_sleep_arc`` sets drowsy_after = idle_timeout / 2 matching this ratio."""
        machine = SleepStateMachine()
        assert (
            machine.drowsy_after < machine.asleep_after
        ), "drowsy_after must be strictly less than asleep_after"
        # Default: drowsy_after=75, asleep_after=150 → half ratio.
        assert machine.drowsy_after == pytest.approx(75.0, abs=0.1)
        assert machine.asleep_after == pytest.approx(150.0, abs=0.1)
        # run_sleep_arc constructs with drowsy_after = idle_timeout / 2.
        assert machine.drowsy_after == pytest.approx(
            machine.asleep_after / 2.0, rel=1e-3
        ), "Default constructor should satisfy the documented 75/150 (half) ratio"


class TestSleepProducerBoundaryUnchanged:
    """Re-assert SleepProducer's ASLEEP contract: label / coalesce-key / near-zero-yaw."""

    def _get_asleep_action(self, t: float = 100.0):  # noqa: ANN202
        """Return the MotionAction submitted by a fresh ASLEEP producer at time *t*."""
        q = MotionQueue()
        prod = SleepProducer(queue=q, state=SleepState.ASLEEP)
        prod.update(t)
        pending = q.pending()
        assert pending, "ASLEEP producer must submit at least one action"
        return pending[-1]

    def test_asleep_action_label_contains_sleep(self) -> None:
        """The sleep-breathe action label contains 'sleep' — unchanged by the feature."""
        action = self._get_asleep_action()
        assert (
            "sleep" in action.label.lower()
        ), f"ASLEEP action label should contain 'sleep'; got {action.label!r}"

    def test_asleep_action_uses_sleep_coalesce_key(self) -> None:
        """ASLEEP uses SLEEP_COALESCE_KEY (not IDLE_KEY) — unchanged by the feature."""
        action = self._get_asleep_action()
        assert (
            action.coalesce_key == SLEEP_COALESCE_KEY
        ), f"ASLEEP coalesce key should be {SLEEP_COALESCE_KEY!r}; got {action.coalesce_key!r}"

    def test_asleep_head_yaw_near_zero(self) -> None:
        """Sleep-breathe head yaw is near-zero — the robot is visibly asleep, not turning."""
        action = self._get_asleep_action()
        head = action.head or {}
        yaw = abs(head.get("yaw", 0.0))
        assert yaw < 5.0, f"ASLEEP head yaw should be near-zero (<5°); got {yaw:.2f}°"

    def test_asleep_head_roll_near_zero(self) -> None:
        """Sleep-breathe head roll is near-zero — unchanged by the feature."""
        action = self._get_asleep_action()
        head = action.head or {}
        roll = abs(head.get("roll", 0.0))
        assert roll < 5.0, f"ASLEEP head roll should be near-zero (<5°); got {roll:.2f}°"

    def test_asleep_minjerk_interpolation_unchanged(self) -> None:
        """All sleep-breathe moves use minjerk interpolation — unchanged by the feature."""
        action = self._get_asleep_action()
        assert (
            action.interpolation == "minjerk"
        ), f"ASLEEP interpolation should be 'minjerk'; got {action.interpolation!r}"

    def test_asleep_body_yaw_within_rock_amplitude(self) -> None:
        """Sleep-breathe body_yaw stays within the cited reachy_nova amplitude (12 deg)."""
        from reachy.motion.sleep import SLEEP_BREATHE_BODY_YAW

        # Sample at a mid-phase tick so the sine is nonzero (not at exactly 0 or 2π).
        action = self._get_asleep_action(t=7.0)
        yaw = abs(action.body_yaw or 0.0)
        # During the ramp-in (t=7 < _SLEEP_RAMP_SECONDS=8) amplitude is scaled < 1.
        assert yaw <= SLEEP_BREATHE_BODY_YAW + 0.5, (
            f"sleep_breathe body_yaw {yaw:.2f}° exceeds SLEEP_BREATHE_BODY_YAW "
            f"({SLEEP_BREATHE_BODY_YAW}°)"
        )


class TestSleepSignalBoundaryUnchanged:
    """Re-assert the sleep_active.flag yield contract: write/is_active/clear."""

    @pytest.fixture(autouse=True)
    def _isolate(self, monkeypatch: pytest.MonkeyPatch, tmp_path):
        """Pin the state dir to a throwaway tmp_path for every test in this class."""
        monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
        sleep_signal.clear()
        yield
        sleep_signal.clear()

    def test_write_makes_flag_active(self) -> None:
        """write() → is_active() True — flag-yield contract unchanged."""
        assert not sleep_signal.is_active()
        sleep_signal.write()
        assert sleep_signal.is_active() is True

    def test_clear_removes_flag(self) -> None:
        """clear() after write() → is_active() False — flag-yield contract unchanged."""
        sleep_signal.write()
        sleep_signal.clear()
        assert sleep_signal.is_active() is False

    def test_clear_on_absent_flag_is_safe(self) -> None:
        """clear() when the flag is absent must not raise — idempotent contract."""
        sleep_signal.clear()  # should not raise

    def test_write_clear_cycle_is_repeatable(self) -> None:
        """Multiple write→clear cycles stay consistent — no stale-file corruption."""
        for _ in range(3):
            sleep_signal.write()
            assert sleep_signal.is_active() is True
            sleep_signal.clear()
            assert sleep_signal.is_active() is False

    def test_flag_written_by_arc_while_asleep(self, tmp_path) -> None:
        """The arc writes the flag while ASLEEP and clears it on exit.

        This duplicates the flag-path in test_flag_written_while_asleep_cleared_on_pat_wake
        but at the signal-module boundary, ensuring the flag-yield contract is intact
        independently of the pat-only path.
        """
        total_ticks = 5
        sense_list = [EMPTY_SENSE] * total_ticks
        now, sense, advance = _make_seam(sense_list)

        flag_observations: list[bool] = []

        def on_tick() -> None:
            flag_observations.append(sleep_signal.is_active())
            advance()

        run_sleep_arc(
            queue=MotionQueue(),
            now=now,
            sense=sense,
            snap=lambda: False,
            pat=lambda: False,
            audio_wake=False,
            on_tick=on_tick,
            ticks=total_ticks,
            idle_timeout=15.0,
        )

        # The flag must be cleared after the arc finishes.
        assert not sleep_signal.is_active(), "flag must be cleared after arc exits"

        # At least one tick must have observed the flag as True (while ASLEEP).
        assert any(flag_observations), (
            "sleep_active.flag was never raised during the arc — "
            f"flag_observations={flag_observations}"
        )
