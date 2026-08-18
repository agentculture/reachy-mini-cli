"""Contract tests for the event-stable proprioceptive pat snapshot."""

from __future__ import annotations

import dataclasses

import pytest

from reachy.behavior.sense import (
    UNAVAILABLE_PAT_STATE,
    PatState,
    Sense,
    SenseProviders,
    read_perception,
)


def _active_pat_state() -> PatState:
    return PatState(
        availability="available",
        contact=True,
        touch_type="side_pat",
        level="level1",
        yaw_deg=-3.25,
        phase="receptive",
        phase_started_at=10.0,
        last_press_at=10.4,
    )


def test_pat_state_is_frozen_and_event_stable() -> None:
    first = _active_pat_state()
    same_meaning_later_tick = _active_pat_state()

    assert first == same_meaning_later_tick
    assert tuple(field.name for field in dataclasses.fields(first)) == (
        "availability",
        "contact",
        "touch_type",
        "level",
        "yaw_deg",
        "phase",
        "phase_started_at",
        "last_press_at",
        "blocked_reason",
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        first.contact = False  # type: ignore[misc]


def test_meaningful_pat_state_transitions_change_equality() -> None:
    active = _active_pat_state()

    assert dataclasses.replace(active, contact=False) != active
    assert dataclasses.replace(active, yaw_deg=3.25) != active
    assert dataclasses.replace(active, phase="contentment", phase_started_at=14.0) != active
    assert dataclasses.replace(active, last_press_at=10.8) != active


def test_unavailable_pat_state_is_distinct_from_observed_no_contact() -> None:
    observed_no_contact = PatState(
        availability="available",
        contact=False,
        phase="released",
        phase_started_at=12.0,
        last_press_at=11.0,
    )

    assert UNAVAILABLE_PAT_STATE.availability == "unavailable"
    assert UNAVAILABLE_PAT_STATE.contact is False
    assert UNAVAILABLE_PAT_STATE != observed_no_contact


def test_sense_construction_remains_compatible_with_doa_and_legacy_pat_event() -> None:
    doa_only = Sense(doa_angle=0.5, speech_detected=True)
    legacy_pat_only = Sense(pat_event=("scratch", "level1"))

    assert doa_only.pat_state == UNAVAILABLE_PAT_STATE
    assert legacy_pat_only.pat_event == ("scratch", "level1")
    assert legacy_pat_only.pat_state == UNAVAILABLE_PAT_STATE


def test_pat_state_provider_is_a_non_consuming_peek() -> None:
    held = _active_pat_state()
    calls = 0

    def peek() -> PatState:
        nonlocal calls
        calls += 1
        return held

    providers = SenseProviders(pat_state=peek)

    first_consumer = read_perception(providers)
    second_consumer = read_perception(providers)

    assert first_consumer.pat_state is held
    assert second_consumer.pat_state is held
    assert calls == 2


@pytest.mark.parametrize("provider", [None, lambda: (_ for _ in ()).throw(RuntimeError("gone"))])
def test_missing_or_raising_pat_state_provider_means_unavailable(provider) -> None:
    snap = read_perception(SenseProviders(pat_state=provider))

    assert snap.pat_state == UNAVAILABLE_PAT_STATE
    assert snap.pat_state.availability == "unavailable"
    assert snap.pat_state.contact is False


def test_legacy_pat_event_provider_remains_identical_with_pat_state_added() -> None:
    snap = read_perception(
        SenseProviders(
            pat_event=lambda: ("side_pat", "level2"),
            pat_state=lambda: _active_pat_state(),
        )
    )

    assert snap.pat_event == ("side_pat", "level2")
    assert isinstance(snap.pat_event, tuple)
