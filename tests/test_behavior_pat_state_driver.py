"""Persistent PatState and complete-command gating for PatSenseDriver."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from reachy.behavior.pat_sense import RELEASE_AFTER_S, PatSenseDriver
from reachy.behavior.sense import PatState
from reachy.motion.pat import PatDetector, PatEvidence

T0 = 10.0
BASE_OWNER = "feel-alive-1"
REACTION_OWNER = "pet-reaction-2"


def _head(**updates: float) -> dict[str, float]:
    pose = {
        "x": 0.0,
        "y": 0.0,
        "z": 0.0,
        "roll": 0.0,
        "pitch": 0.0,
        "yaw": 0.0,
    }
    pose.update(updates)
    return pose


def _ctx(
    now: float,
    *,
    head: dict[str, float] | None = None,
    antennas: tuple[float, float] = (0.0, 0.0),
    body_yaw: float = 0.0,
    owners: tuple[str | None, str | None, str | None] = (
        BASE_OWNER,
        BASE_OWNER,
        BASE_OWNER,
    ),
):
    return SimpleNamespace(
        now=now,
        tick=int(now * 100),
        ownership={
            "head": owners[0],
            "antennas": owners[1],
            "body_yaw": owners[2],
        },
        pose={
            "head": head if head is not None else _head(),
            "antennas": antennas,
            "body_yaw": body_yaw,
        },
    )


class _Reader:
    def __init__(self, value: tuple[float, float] | None = (0.0, 0.0)) -> None:
        self.value = value
        self.calls = 0

    def __call__(self) -> tuple[float, float] | None:
        self.calls += 1
        return self.value


class _CountingDetector(PatDetector):
    def __init__(self, **kwargs) -> None:
        kwargs.setdefault("baseline_alpha", 0.0)
        kwargs.setdefault("level2_threshold_fn", lambda: 100.0)
        super().__init__(**kwargs)
        self.clear_calls = 0

    def clear_interaction(self) -> None:
        self.clear_calls += 1
        super().clear_interaction()

    def clear_presses(self) -> None:
        super().clear_presses()


def _driver(
    reader: _Reader,
    *,
    detector: PatDetector | None = None,
    still_hold_s: float = 0.0,
    enough_after_fn=lambda: 9.0,
) -> PatSenseDriver:
    return PatSenseDriver(
        reader=reader,
        detector=detector
        or PatDetector(
            min_presses=99,
            baseline_alpha=0.0,
            press_threshold=0.5,
            release_threshold=0.2,
            yaw_press_threshold=0.5,
            yaw_release_threshold=0.2,
            level2_threshold_fn=lambda: 100.0,
        ),
        lag_tau=0.0,
        hp_tau=0.0,
        warmup_s=0.0,
        still_hold_s=still_hold_s,
        max_observation_gap_s=0.0,
        enough_after_fn=enough_after_fn,
    )


def _tick(
    driver: PatSenseDriver,
    reader: _Reader,
    now: float,
    actual: tuple[float, float] | None,
    **ctx_kwargs,
) -> PatState:
    reader.value = actual
    driver(_ctx(now, **ctx_kwargs))
    return driver.peek_state()


@pytest.mark.parametrize(
    "changed",
    [
        {"head": _head(x=1.0)},
        {"head": _head(y=1.0)},
        {"head": _head(z=1.0)},
        {"head": _head(roll=1.0)},
        {"head": _head(pitch=1.0)},
        {"head": _head(yaw=1.0)},
        {"body_yaw": 1.0},
        {"antennas": (1.0, 0.0)},
        {"antennas": (0.0, 1.0)},
    ],
    ids=[
        "head-x",
        "head-y",
        "head-z",
        "head-roll",
        "head-pitch",
        "head-yaw",
        "body-yaw",
        "right-antenna",
        "left-antenna",
    ],
)
def test_every_command_axis_clears_on_gap_entry_once(changed) -> None:
    reader = _Reader()
    detector = _CountingDetector(min_presses=99)
    driver = _driver(reader, detector=detector, still_hold_s=0.5)

    assert _tick(driver, reader, T0, (0.0, 0.0)).availability == "blocked"
    assert detector.clear_calls == 1
    assert _tick(driver, reader, T0 + 0.5, (0.0, 0.0)).availability == "available"
    assert reader.calls == 1
    assert detector.clear_calls == 1

    blocked = _tick(driver, reader, T0 + 1.0, (9.0, 9.0), **changed)
    assert blocked.availability == "blocked"
    assert blocked.contact is False
    assert blocked.level is None
    assert blocked.phase == "idle"
    assert reader.calls == 1
    assert detector.clear_calls == 2

    assert _tick(driver, reader, T0 + 1.4, (9.0, 9.0), **changed).availability == "blocked"
    available = _tick(driver, reader, T0 + 1.5, (0.0, 0.0), **changed)
    assert available.availability == "available"
    assert reader.calls == 2
    assert detector.clear_calls == 2


def test_ownership_edge_reearns_hold_but_any_settled_owner_can_sense() -> None:
    reader = _Reader()
    detector = _CountingDetector(min_presses=99)
    driver = _driver(reader, detector=detector, still_hold_s=0.5)

    _tick(driver, reader, T0, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.5, (0.0, 0.0))
    assert detector.clear_calls == 1

    reaction_owners = (REACTION_OWNER, REACTION_OWNER, REACTION_OWNER)
    blocked = _tick(
        driver,
        reader,
        T0 + 1.0,
        (0.0, 0.0),
        owners=reaction_owners,
    )
    assert blocked.availability == "blocked"
    assert blocked.contact is False
    assert blocked.level is None
    assert blocked.phase == "idle"
    assert detector.clear_calls == 2

    _tick(driver, reader, T0 + 1.4, (0.0, 0.0), owners=reaction_owners)
    available = _tick(
        driver,
        reader,
        T0 + 1.5,
        (0.0, 0.0),
        owners=reaction_owners,
    )
    assert available.availability == "available"
    assert detector.clear_calls == 2


def test_incomplete_command_is_blocked_before_reader() -> None:
    reader = _Reader()
    driver = _driver(reader, still_hold_s=0.5)
    incomplete = _head()
    incomplete.pop("roll")

    state = _tick(driver, reader, T0, (0.0, 0.0), head=incomplete)

    assert state.availability == "blocked"
    assert reader.calls == 0


def test_fresh_contact_then_release_budget_of_observed_silence_releases() -> None:
    """Contact survives silence up to ``RELEASE_AFTER_S``, then releases on it.

    Timed against the constant rather than a literal so the budget can be
    retuned (1.0 -> 2.5 s in v0.41.0, so a sustained pet outlives the reaction's
    own blind window) without silently invalidating this test.
    """
    reader = _Reader()
    driver = _driver(reader)

    receptive = _tick(driver, reader, T0, (0.0, 3.0))
    assert receptive == PatState(
        availability="available",
        contact=True,
        touch_type="side_pat",
        level=None,
        yaw_deg=3.0,
        phase="receptive",
        phase_started_at=T0,
        last_press_at=T0,
    )

    _tick(driver, reader, T0 + 0.1, (0.0, 0.0))
    still_contact = _tick(driver, reader, T0 + RELEASE_AFTER_S - 0.01, (0.0, 0.0))
    assert still_contact.contact is True
    assert still_contact.phase == "receptive"

    released = _tick(driver, reader, T0 + RELEASE_AFTER_S, (0.0, 0.0))
    assert released.availability == "available"
    assert released.contact is False
    assert released.phase == "released"
    assert released.phase_started_at == pytest.approx(T0 + RELEASE_AFTER_S)
    assert released.last_press_at == T0


def test_blocked_and_unavailable_gaps_suspend_then_bound_the_interaction() -> None:
    reader = _Reader()
    driver = _driver(reader, still_hold_s=0.5)

    _tick(driver, reader, T0, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.5, (0.0, 0.0))
    contact = _tick(driver, reader, T0 + 0.6, (0.0, -3.0))
    assert contact.contact is True

    # A gap SUSPENDS a live interaction rather than ending it (2026-07-20 sustain
    # fix): "cannot sense right now" is not "the human let go". Availability
    # carries the uncertainty while contact and the phase are preserved.
    blocked = _tick(driver, reader, T0 + 2.0, None, head=_head(x=1.0))
    assert blocked.availability == "blocked"
    assert blocked.contact is True
    assert blocked.phase == "receptive"

    _tick(driver, reader, T0 + 2.4, None, head=_head(x=1.0))
    unavailable = _tick(driver, reader, T0 + 2.5, None, head=_head(x=1.0))
    assert unavailable.availability == "unavailable"
    assert unavailable.contact is True
    assert unavailable.phase == "receptive"

    # ...but suspension is BOUNDED. On recovery the blind stretch is charged to
    # the release budget from when the gap opened, so this ~18 s blackout cannot
    # masquerade as a hand that never left. Short gaps (a reaction's own ~1.24 s
    # blind window) stay inside the 2.5 s budget and keep laddering; this does not.
    recovered = _tick(driver, reader, T0 + 20.0, (0.0, 0.0), head=_head(x=1.0))
    assert recovered.availability == "available"
    assert recovered.contact is False
    assert recovered.phase == "released"


def test_contact_clock_drives_contentment_warning_enough_and_cooldown() -> None:
    reader = _Reader()
    driver = _driver(reader, enough_after_fn=lambda: 9.0)

    state = _tick(driver, reader, T0, (0.0, 3.0))
    assert state.phase == "receptive"

    # Fresh same-side presses every 0.5 s keep contact honest. Release samples
    # between them re-arm the yaw edge without ending the interaction.
    for step in range(1, 19):
        now = T0 + step * 0.5
        _tick(driver, reader, now - 0.1, (0.0, 0.0))
        state = _tick(driver, reader, now, (0.0, 3.0))
        if step == 8:
            assert state.phase == "contentment"
            assert state.phase_started_at == pytest.approx(T0 + 4.0)
        if step == 16:
            assert state.phase == "warning"
            assert state.phase_started_at == pytest.approx(T0 + 8.0)

    assert state.phase == "enough"
    assert state.phase_started_at == pytest.approx(T0 + 9.0)
    assert state.contact is True

    cooldown = _tick(driver, reader, T0 + 9.1, (0.0, 3.0))
    assert cooldown.phase == "cooldown"
    assert cooldown.contact is False
    assert cooldown.touch_type is None
    assert cooldown.yaw_deg is None

    still_cooling = _tick(driver, reader, T0 + 13.9, (0.0, 3.0))
    assert still_cooling.phase == "cooldown"
    idle = _tick(driver, reader, T0 + 14.1, (0.0, 0.0))
    assert idle == PatState(
        availability="available",
        phase="idle",
        phase_started_at=T0 + 14.1,
    )


def test_enough_gap_hides_contact_then_resumes_full_cooldown() -> None:
    reader = _Reader()
    driver = _driver(reader, enough_after_fn=lambda: 8.0)

    _tick(driver, reader, T0, (0.0, 3.0))
    for step in range(1, 17):
        now = T0 + step * 0.5
        _tick(driver, reader, now - 0.1, (0.0, 0.0))
        state = _tick(driver, reader, now, (0.0, 3.0))
    assert state.phase == "enough"

    blocked_head = _head()
    blocked_head.pop("roll")
    blocked = _tick(driver, reader, T0 + 100.0, None, head=blocked_head)
    assert blocked.availability == "blocked"
    assert blocked.contact is False
    assert blocked.level is None
    assert blocked.phase == "idle"

    recovered = _tick(driver, reader, T0 + 100.1, (0.0, 0.0))
    assert recovered.availability == "available"
    assert recovered.phase == "cooldown"
    assert recovered.contact is False
    assert recovered.level is None
    assert recovered.phase_started_at == pytest.approx(T0 + 100.1)

    # Physical pat-shaped samples cannot reach the detector during the full
    # safe-observation cooldown, even though the rule's original fire is old.
    _tick(driver, reader, T0 + 100.2, (-3.0, 0.0))
    _tick(driver, reader, T0 + 100.3, (0.0, 0.0))
    before_expiry = _tick(driver, reader, T0 + 105.0, (-3.0, 0.0))
    assert before_expiry.phase == "cooldown"
    assert driver.peek() is None
    assert driver.detector.snapshot() == PatEvidence()

    expired = _tick(driver, reader, T0 + 105.1, (0.0, 0.0))
    assert expired.phase == "idle"
    assert expired.contact is False


def test_mid_cooldown_gap_pauses_remaining_budget() -> None:
    reader = _Reader()
    driver = _driver(reader, enough_after_fn=lambda: 8.0)

    _tick(driver, reader, T0, (0.0, 3.0))
    for step in range(1, 17):
        now = T0 + step * 0.5
        _tick(driver, reader, now - 0.1, (0.0, 0.0))
        state = _tick(driver, reader, now, (0.0, 3.0))
    assert state.phase == "enough"
    assert _tick(driver, reader, T0 + 8.1, (0.0, 0.0)).phase == "cooldown"

    # Consume two seconds of safe cooldown, then suspend it in a long gap.
    assert _tick(driver, reader, T0 + 10.1, (0.0, 0.0)).phase == "cooldown"
    blocked_head = _head()
    blocked_head.pop("roll")
    blocked = _tick(driver, reader, T0 + 10.2, None, head=blocked_head)
    assert blocked.availability == "blocked"
    assert blocked.contact is False
    assert blocked.level is None
    assert blocked.phase == "idle"

    _tick(driver, reader, T0 + 100.0, None, head=blocked_head)
    resumed = _tick(driver, reader, T0 + 100.1, (0.0, 0.0))
    assert resumed.phase == "cooldown"

    # Roughly three seconds remained: the gap neither expired nor restarted it.
    assert _tick(driver, reader, T0 + 103.0, (0.0, 0.0)).phase == "cooldown"
    assert _tick(driver, reader, T0 + 103.1, (0.0, 0.0)).phase == "idle"


def test_input_gap_clears_interaction_once_on_entry_not_recovery() -> None:
    reader = _Reader()
    detector = _CountingDetector(min_presses=99)
    driver = _driver(reader, detector=detector)

    contact = _tick(driver, reader, T0, (0.0, -3.0))
    assert contact.contact is True
    assert detector.clear_calls == 0

    # The SUBJECT of this test is the clear COUNT: the detector is cleared exactly
    # once, on gap entry, and never again on repeat or on recovery. That is the
    # escalation guard and it is unchanged. The interaction itself is suspended
    # rather than ended (2026-07-20 sustain fix), so contact and phase survive a
    # gap this short — 0.2 s, far inside the release budget.
    unavailable = _tick(driver, reader, T0 + 0.1, None)
    assert unavailable.availability == "unavailable"
    assert unavailable.contact is True
    assert unavailable.phase == "receptive"
    assert detector.clear_calls == 1

    repeated = _tick(driver, reader, T0 + 0.2, None)
    assert repeated == unavailable
    assert detector.clear_calls == 1

    recovered = _tick(driver, reader, T0 + 0.3, (0.0, 0.0))
    assert recovered.availability == "available"
    assert recovered.contact is True
    assert recovered.phase == "receptive"
    assert detector.clear_calls == 1


def test_gap_cannot_pair_press_edges_and_legacy_latch_stays_identical() -> None:
    reader = _Reader()
    detector = _CountingDetector(
        min_presses=2,
        pat_cooldown=0.0,
        press_threshold=0.5,
        release_threshold=0.2,
        yaw_press_threshold=0.5,
        yaw_release_threshold=0.2,
    )
    driver = _driver(reader, detector=detector, still_hold_s=0.5)

    _tick(driver, reader, T0, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.5, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.6, (-3.0, 0.0))
    _tick(driver, reader, T0 + 0.7, (0.0, 0.0))
    assert driver.peek() is None

    # A complete-pose command edge discards the first half of the press pair.
    _tick(driver, reader, T0 + 1.0, (-3.0, 0.0), antennas=(1.0, 0.0))
    _tick(driver, reader, T0 + 1.5, (0.0, 0.0), antennas=(1.0, 0.0))
    _tick(driver, reader, T0 + 1.6, (-3.0, 0.0), antennas=(1.0, 0.0))
    assert driver.peek() is None

    _tick(driver, reader, T0 + 1.7, (0.0, 0.0), antennas=(1.0, 0.0))
    _tick(driver, reader, T0 + 1.8, (-3.0, 0.0), antennas=(1.0, 0.0))
    assert driver.peek() == ("scratch", "level1")
    assert driver.peek() == ("scratch", "level1")
    assert driver.as_state_provider()() == driver.peek_state()


@pytest.mark.parametrize("gap_kind", ["motion", "ownership", "input"])
def test_level1_cannot_escalate_across_a_detection_gap(gap_kind: str) -> None:
    reader = _Reader()
    detector = _CountingDetector(
        min_presses=2,
        pat_cooldown=0.0,
        baseline_alpha=0.0,
        press_threshold=0.5,
        release_threshold=0.2,
        yaw_press_threshold=0.5,
        yaw_release_threshold=0.2,
        level2_threshold_fn=lambda: 0.5,
    )
    driver = _driver(reader, detector=detector, still_hold_s=0.5)

    _tick(driver, reader, T0, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.5, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.6, (-3.0, 0.0))
    _tick(driver, reader, T0 + 0.7, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.8, (-3.0, 0.0))
    assert driver.peek() == ("scratch", "level1")

    if gap_kind == "motion":
        gap_state = _tick(driver, reader, T0 + 1.0, None, antennas=(1.0, 0.0))
        _tick(driver, reader, T0 + 100.0, (0.0, 0.0), antennas=(1.0, 0.0))
        recovered = _tick(
            driver,
            reader,
            T0 + 100.5,
            (-3.0, 0.0),
            antennas=(1.0, 0.0),
        )
    elif gap_kind == "ownership":
        owners = (REACTION_OWNER, REACTION_OWNER, REACTION_OWNER)
        gap_state = _tick(driver, reader, T0 + 1.0, None, owners=owners)
        _tick(driver, reader, T0 + 100.0, (0.0, 0.0), owners=owners)
        recovered = _tick(driver, reader, T0 + 100.5, (-3.0, 0.0), owners=owners)
    else:
        gap_state = _tick(driver, reader, T0 + 1.0, None)
        recovered = _tick(driver, reader, T0 + 100.0, (-3.0, 0.0))

    # NEW CONTRACT (2026-07-20 sustain fix): a gap SUSPENDS a live interaction
    # instead of ending it — "cannot sense right now" is not "the human let go".
    # Contact / level / phase are preserved and `availability` carries the
    # uncertainty, so a reaction whose own motion closed the gate keeps holding
    # instead of dropping back to idle under a still-moving hand.
    assert gap_state.availability in {"blocked", "unavailable"}
    assert gap_state.contact is True
    assert gap_state.level == "level1"
    assert gap_state.phase == "receptive"
    # What must NOT survive a gap is ESCALATION, and that guarantee is untouched:
    # the detector is still cleared, so press edges either side of the gap can
    # never pair into a level2 that was never physically sustained.
    assert detector.clear_calls == 2
    assert driver.peek() is None
    assert recovered.level is None
    assert detector.snapshot().level is None
    assert detector._state == "idle"
    assert detector.clear_calls == 2


def test_boot_warmup_publishes_idle_and_clears_interaction_once_on_exit() -> None:
    reader = _Reader()
    detector = _CountingDetector(
        min_presses=2,
        pat_cooldown=0.0,
        press_threshold=0.5,
        release_threshold=0.2,
        yaw_press_threshold=0.5,
        yaw_release_threshold=0.2,
        level2_threshold_fn=lambda: 0.5,
    )
    driver = PatSenseDriver(
        reader=reader,
        detector=detector,
        lag_tau=0.0,
        hp_tau=0.0,
        warmup_s=1.0,
        still_hold_s=0.0,
    )

    for now, actual in (
        (T0, (-3.0, 0.0)),
        (T0 + 0.1, (0.0, 0.0)),
        (T0 + 0.2, (-3.0, 0.0)),
        (T0 + 0.3, (0.0, 0.0)),
    ):
        state = _tick(driver, reader, now, actual)
        assert state.availability == "available"
        assert state.phase == "idle"
        assert state.contact is False
        assert state.level is None

    assert detector.snapshot().level == "level1"
    assert detector.clear_calls == 0

    exited = _tick(driver, reader, T0 + 1.0, (0.0, 0.0))
    assert exited == PatState(
        availability="available",
        phase="idle",
        phase_started_at=T0 + 1.0,
    )
    assert detector.snapshot() == PatEvidence()
    assert detector._state == "idle"
    assert detector.clear_calls == 1

    _tick(driver, reader, T0 + 1.1, (0.0, 0.0))
    assert detector.clear_calls == 1


def test_logical_clock_gap_ends_level1_once_before_current_sample() -> None:
    reader = _Reader()
    detector = _CountingDetector(
        min_presses=2,
        pat_cooldown=0.0,
        baseline_alpha=0.0,
        press_threshold=0.5,
        release_threshold=0.2,
        yaw_press_threshold=0.5,
        yaw_release_threshold=0.2,
        level2_threshold_fn=lambda: 0.05,
    )
    driver = PatSenseDriver(
        reader=reader,
        detector=detector,
        lag_tau=0.0,
        hp_tau=0.0,
        warmup_s=0.0,
        still_hold_s=0.0,
        max_observation_gap_s=0.2,
    )

    _tick(driver, reader, T0, (-3.0, 0.0))
    _tick(driver, reader, T0 + 0.1, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.2, (-3.0, 0.0))
    assert driver.peek() == ("scratch", "level1")

    recovered = _tick(driver, reader, T0 + 0.5, (-3.0, 0.0))

    assert driver.peek() is None
    assert recovered.phase == "receptive"
    assert recovered.level is None
    assert detector._state == "idle"
    assert detector.clear_calls == 1


def test_reaction_motion_does_not_end_the_pet_it_was_admitted_for() -> None:
    """The sustain regression: a pet survives the reaction's own head motion.

    Reproduces the 2026-07-20 field bug directly. Admitting a reaction moves the
    head, which closes the stillness gate, which used to ``_end_interaction`` the
    very contact that triggered it. A continuous scratch was chopped into repeated
    level1s that never laddered past ``receptive``, and the robot dropped back to
    idle motion under a still-moving hand.

    Here the commanded pose moves for the length of a real entry slew, then holds,
    exactly as ``pet_reaction`` drives it. Contact and the phase clock must both
    survive, and the ladder must go on to reach ``contentment``.
    """
    reader = _Reader()
    driver = _driver(reader, still_hold_s=0.5, enough_after_fn=lambda: 9.0)

    # Earn the stillness gate first, then take a real press.
    _tick(driver, reader, T0, (0.0, 0.0))
    _tick(driver, reader, T0 + 0.5, (0.0, 0.0))
    contact_at = T0 + 0.6
    assert _tick(driver, reader, contact_at, (0.0, 3.0)).phase == "receptive"

    # The reaction slews the head — commanded pose changes every tick, so the
    # stillness gate is shut and nothing can be sensed.
    for step in range(1, 13):
        blind = _tick(
            driver, reader, contact_at + step * 0.02, (0.0, 3.0), head=_head(yaw=step * 1.0)
        )
        assert blind.availability == "blocked"
        assert blind.contact is True, "the reaction's own motion ended its pet"
        assert blind.phase == "receptive"

    # It then HOLDS the contact pose, so the gate re-earns its quiet window and
    # sensing resumes with the interaction STILL ALIVE. A hand that keeps
    # scratching supplies fresh press edges against that held pose, and the
    # ladder goes on to reach contentment — unreachable before the fix, because
    # the reaction's own motion restarted the clock every time.
    held = _head(yaw=12.0)
    state = None
    for step in range(1, 30):
        now = contact_at + 0.24 + step * 0.4
        # Actuals are RELATIVE to the held commanded pose (yaw=12): 12.0 is the
        # hand off, 15.0 is a fresh press. Using 0.0 here would read as a
        # permanent -12 deg deviation, so the press edge would never re-arm.
        _tick(driver, reader, now - 0.15, (0.0, 12.0), head=held)
        state = _tick(driver, reader, now, (0.0, 15.0), head=held)
        if state.phase == "contentment":
            break
    assert state is not None
    assert state.phase == "contentment"
    assert state.contact is True
