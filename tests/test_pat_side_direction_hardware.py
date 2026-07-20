"""Labelled physical side-scratch gate for issue #82 / completion of #70.

The two CSV fixtures were recorded at 25 Hz from /api/state/full with the
behavior engine stopped and a neutral commanded pose. Physical-side labels
were chosen before signal inspection:

* robot_left_side__operator_right_when_facing;
* robot_right_side__operator_left_when_facing.

The captures begin from opposite compliant offsets, so direction is conditioned
against each capture's untouched baseline. For cadence/reacquisition, the
right trace is mirrored into the left robot-frame and the two are averaged.
This preserves each independently asserted sign while reducing side-specific
compliance noise.
"""

from __future__ import annotations

import csv
import statistics
from collections.abc import Callable
from pathlib import Path

import pytest

from reachy.behavior.pat_sense import (
    DEFAULT_STILL_HOLD_S,
    RELEASE_AFTER_S,
    PatSenseDriver,
)
from reachy.motion.pat import PatDetector

pytestmark = pytest.mark.offline

DATA = Path(__file__).parent / "data"
LEFT_LABEL = "robot_left_side__operator_right_when_facing"
RIGHT_LABEL = "robot_right_side__operator_left_when_facing"
ENTRY_START_S = 4.0
ENTRY_DURATION_S = 0.6
# Tracked against the shipped constants rather than copied, so retuning the gate
# or the release budget cannot silently invalidate the timing assertions below.
# (v0.41.0 moved the hold 0.5 -> 1.0 s and the budget 1.0 -> 2.5 s for the
# swinging idle; both were stale literals here before that.)
SAFE_HOLD_S = DEFAULT_STILL_HOLD_S
RELEASE_BUDGET_S = RELEASE_AFTER_S

_POSE_COLUMNS = {
    "cmd_x_m",
    "cmd_y_m",
    "cmd_z_m",
    "cmd_roll_deg",
    "cmd_pitch_deg",
    "cmd_yaw_deg",
    "a_x_m",
    "a_y_m",
    "a_z_m",
    "a_roll_deg",
    "a_pitch_deg",
    "a_yaw_deg",
    "a_body_yaw_deg",
    "a_antenna_right_deg",
    "a_antenna_left_deg",
}


def _load(side: str) -> list[dict[str, str]]:
    path = DATA / f"pat_side_robot_{side}.csv"
    with path.open(newline="", encoding="utf-8") as stream:
        return list(csv.DictReader(stream))


def _phase_median(rows: list[dict[str, str]], phase: str, key: str) -> float:
    return statistics.median(float(row[key]) for row in rows if row["phase"] == phase)


def _assert_capture(rows: list[dict[str, str]], label: str) -> None:
    assert len(rows) == 300
    assert set(rows[0]) >= {"t", "label", "phase"} | _POSE_COLUMNS
    assert {row["label"] for row in rows} == {label}
    assert [row["phase"] for row in rows].count("baseline") == 50
    assert [row["phase"] for row in rows].count("scratch") == 200
    assert [row["phase"] for row in rows].count("release") == 50
    times = [float(row["t"]) for row in rows]
    assert all(after > before for before, after in zip(times, times[1:]))


def test_prelabelled_sides_condition_to_opposite_robot_frame_signs() -> None:
    """Physical robot-left maps to +yaw/left; robot-right maps to -yaw/right."""
    left = _load("left")
    right = _load("right")
    _assert_capture(left, LEFT_LABEL)
    _assert_capture(right, RIGHT_LABEL)

    left_yaw = _phase_median(left, "scratch", "a_yaw_deg") - _phase_median(
        left, "baseline", "a_yaw_deg"
    )
    right_yaw = _phase_median(right, "scratch", "a_yaw_deg") - _phase_median(
        right, "baseline", "a_yaw_deg"
    )

    assert left_yaw > 4.0
    assert right_yaw < -4.0
    assert left_yaw * right_yaw < 0.0

    # Robot-frame +yaw is left and -yaw is right. Leaning toward the labelled
    # hand therefore keeps the conditioned sign; t8 consumes this mapping.
    target_yaw_by_label = {
        LEFT_LABEL: 8.0 if left_yaw > 0.0 else -8.0,
        RIGHT_LABEL: 8.0 if right_yaw > 0.0 else -8.0,
    }
    assert target_yaw_by_label == {LEFT_LABEL: 8.0, RIGHT_LABEL: -8.0}


def test_detector_evidence_preserves_both_conditioned_fixture_signs() -> None:
    evidence_by_label: dict[str, float | None] = {}
    for side, label in (("left", LEFT_LABEL), ("right", RIGHT_LABEL)):
        rows = _load(side)
        conditioned_yaw = _phase_median(rows, "scratch", "a_yaw_deg") - _phase_median(
            rows, "baseline", "a_yaw_deg"
        )
        detector = PatDetector(min_presses=99, baseline_alpha=0.0)
        detector.update(0.0, 0.0, 0.0, conditioned_yaw, now=1.0)
        evidence_by_label[label] = detector.snapshot().yaw_deg

    assert evidence_by_label[LEFT_LABEL] is not None
    assert evidence_by_label[RIGHT_LABEL] is not None
    assert evidence_by_label[LEFT_LABEL] > 0.0
    assert evidence_by_label[RIGHT_LABEL] < 0.0


class _RecordingDetector(PatDetector):
    def __init__(self) -> None:
        super().__init__(level2_threshold_fn=lambda: 99.0)
        self.edge_times: list[float] = []

    def update(
        self,
        commanded_pitch: float,
        actual_pitch: float,
        commanded_yaw: float = 0.0,
        actual_yaw: float = 0.0,
        *,
        now: float | None = None,
    ) -> tuple[str, str] | None:
        before = self._last_press_time
        event = super().update(
            commanded_pitch,
            actual_pitch,
            commanded_yaw,
            actual_yaw,
            now=now,
        )
        if self._last_press_time != before:
            self.edge_times.append(self._last_press_time)
        return event


class _Ctx:
    def __init__(self, now: float, amount: float) -> None:
        self.now = now
        self.tick = 0
        self.ownership = {
            "head": "feel-alive-1",
            "antennas": "feel-alive-1",
            "body_yaw": "feel-alive-1",
        }
        self.pose = {
            "head": {
                "x": 0.0,
                "y": 0.0,
                "z": 0.0,
                "roll": 0.0,
                "pitch": 0.0,
                "yaw": 8.0 * amount,
            },
            "antennas": (8.0 * amount, 8.0 * amount),
            "body_yaw": 4.0 * amount,
        }


def _mirrored_average() -> list[tuple[float, float, float, str]]:
    left = _load("left")
    right = _load("right")
    left_pitch_base = _phase_median(left, "baseline", "a_pitch_deg")
    right_pitch_base = _phase_median(right, "baseline", "a_pitch_deg")
    left_yaw_base = _phase_median(left, "baseline", "a_yaw_deg")
    right_yaw_base = _phase_median(right, "baseline", "a_yaw_deg")

    samples: list[tuple[float, float, float, str]] = []
    for left_row, right_row in zip(left, right, strict=True):
        now = (float(left_row["t"]) + float(right_row["t"])) / 2.0
        pitch = (
            float(left_row["a_pitch_deg"])
            - left_pitch_base
            + float(right_row["a_pitch_deg"])
            - right_pitch_base
        ) / 2.0
        # Mirror robot-right (-yaw) into robot-left (+yaw) before averaging.
        yaw = (
            float(left_row["a_yaw_deg"])
            - left_yaw_base
            - float(right_row["a_yaw_deg"])
            + right_yaw_base
        ) / 2.0
        assert left_row["phase"] == right_row["phase"]
        samples.append((now, pitch, yaw, left_row["phase"]))
    return samples


def _run_replay(
    command_amount: Callable[[float], float],
) -> tuple[_RecordingDetector, list[float]]:
    sample = [0.0, 0.0, 0.0]
    read_times: list[float] = []

    def reader() -> tuple[float, float]:
        read_times.append(sample[0])
        return sample[1], sample[2]

    detector = _RecordingDetector()
    driver = PatSenseDriver(
        reader=reader,
        detector=detector,
        lag_tau=0.0,
        warmup_s=0.0,
    )
    for now, pitch, yaw, _phase in _mirrored_average():
        sample[:] = [now, pitch, yaw]
        driver(_Ctx(now, command_amount(now)))
    return detector, read_times


def _minjerk_entry(now: float) -> float:
    if now <= ENTRY_START_S:
        return 0.0
    end = ENTRY_START_S + ENTRY_DURATION_S
    if now >= end:
        return 1.0
    u = (now - ENTRY_START_S) / ENTRY_DURATION_S
    return 10.0 * u**3 - 15.0 * u**4 + 6.0 * u**5


def test_mirrored_average_has_measurable_natural_press_cadence() -> None:
    detector, _reads = _run_replay(lambda _now: 0.0)
    scratch_edges = [edge for edge in detector.edge_times if 2.0 <= edge < 10.0]
    gaps = [after - before for before, after in zip(scratch_edges, scratch_edges[1:])]

    assert len(scratch_edges) >= 6
    assert statistics.median(gaps) < RELEASE_BUDGET_S


def test_entry_plus_safe_hold_reacquires_inside_release_budget() -> None:
    detector, read_times = _run_replay(_minjerk_entry)
    entry_end = ENTRY_START_S + ENTRY_DURATION_S
    reopen = min(now for now in read_times if now >= entry_end)

    # The commanded pose has to stay SLOW for the complete safe hold before the
    # actual-pose reader is touched again. Note "slow", not "exactly constant":
    # ``still_eps`` is a per-tick velocity tolerance, so a minjerk entry's
    # deceleration tail drops below it shortly BEFORE the move nominally ends and
    # the hold starts accruing there. At the pre-v0.41.0 eps of 0.01 that tail was
    # under a tick and the distinction was invisible; at 0.035 it is ~2 ticks, and
    # that early-open IS the swing's slow window doing its job (#82). Allowing the
    # tail is therefore correct — but it is bounded, so a gate that opened
    # arbitrarily early during motion still fails.
    DECEL_TAIL_ALLOWANCE_S = 0.06  # ~3 ticks at 50 Hz
    assert reopen >= entry_end + SAFE_HOLD_S - DECEL_TAIL_ALLOWANCE_S
    assert reopen < entry_end + SAFE_HOLD_S + 0.06

    first_fresh_edge = min(edge for edge in detector.edge_times if edge >= reopen)
    # The contract this test NAMES: contact is reacquired within the release
    # budget, so a sustained pet survives the reaction's blind window.
    assert first_fresh_edge - reopen < RELEASE_BUDGET_S
    # A tighter pin on the observed reacquire latency, kept as a drift detector.
    # It is tuning-DEPENDENT, not a contract: 0.0802 s under the pre-v0.41.0 gate
    # (0.5 / 0.01), 0.240 s under the shipped swing-era gate (1.0 / 0.035), which
    # reopens earlier into the deceleration tail and re-baselines the detector
    # there. Re-measure and restate it when the gate moves; do not delete it.
    assert first_fresh_edge - reopen == pytest.approx(0.240, abs=0.01)
