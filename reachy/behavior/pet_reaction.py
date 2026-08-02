"""Dog-like, sensor-driven response to a persistent :class:`PatState`.

The reaction settles *into* a hand instead of animating continuously: it latches
the first touch kind and first credible signed side-yaw, slews every claimed
channel into a complete fixed pose, then emits that pose bit-for-bit unchanged so
the proprioceptive stillness gate can reopen.  Persistent PatState phases select
receptive/content/warning antenna meanings without re-reading noisy direction.

An observed release or ``enough`` phase starts one bounded, coordinated finishing
gesture (head/body wiggle plus antenna reorientation), whose changed full pose
deliberately closes sensing.  Completion is explicit via ``Contribution(done)``
so the engine releases head, antennas, and body to the passive base in that same
tick.  Blocked/unavailable input is never interpreted as release or escalation:
it gets a short reacquisition grace, then finishes under a distinct loss reason.

This module is only the fresh per-instance generator.  Library registration and
runtime composition belong to later plan tasks; no legacy MotionQueue is used.
"""

from __future__ import annotations

import math
from typing import Callable, cast

from reachy.behavior.model import Contribution, neutral_head
from reachy.behavior.pat_sense import DEFAULT_STILL_HOLD_S
from reachy.behavior.sense import PatState, Sense

# Public timing/limit constants make the physical envelope directly testable.
NOMINAL_TICK_S = 0.02
#: Grace before a reaction gives up because the sense stopped answering. This is
#: the FAULT case — ``unavailable`` means the pose reader itself failed — so it
#: stays short.
SENSE_LOSS_GRACE_S = 1.0

PAT_COOLDOWN_S = 5.0
MAX_CONTACT_S = 15.0
DONE_GESTURE_S = 1.2

YAW_DEADBAND_DEG = 0.75

#: Deviation -> lean gains. RAISED 2.5/1.0 -> 6.0/2.5 (2026-07-20) from measured
#: hardware data, not taste: nine live side-pats through the export feed gave
#: |yaw_deg| of 1.30-2.08 deg (median 1.70). That is a structural consequence of
#: DEFAULT_PRESS_THRESHOLD = 1.2 — detection happens right AT the threshold, so
#: the deviation handed to the planner is always near it. At the old 2.5 the
#: resulting turn was 3.3-5.2 deg, comparable to SIDE_PITCH_DEG and swamped by
#: feel-alive's own swing, which the operator read as "moving straight forward".
#: At 6.0 the median pat turns ~10.2 deg and anything >= 2.0 deg clamps at the
#: 12 deg limit — legible as a turn toward the hand. Re-derive these if the press
#: threshold moves: gain and threshold are coupled through this arithmetic.
SIDE_HEAD_GAIN = 6.0
SIDE_BODY_GAIN = 2.5

#: Sign applied to the measured deviation when planning the side-pat lean.
#:
#: ``PatState.yaw_deg`` is the signed actual-minus-commanded deviation
#: (:mod:`reachy.motion.pat`), so a hand resting on the robot's RIGHT pushes the
#: head LEFT and the deviation carries the left sign. Seeking the hand therefore
#: means moving OPPOSITE the deviation: ``-1`` presses the head back along the
#: axis it was pushed, so it meets the palm and sustains contact — the
#: "cat leaning into a scratch" read. ``+1`` (the behaviour through v0.40.0)
#: continued WITH the push, which reads as yielding away from the hand.
SIDE_SEEK_SIGN = -1.0

HEAD_YAW_LIMIT_DEG = 12.0
HEAD_PITCH_LIMIT_DEG = 12.0
BODY_YAW_LIMIT_DEG = 6.0
ANTENNA_LIMIT_DEG = 20.0

# Per-call limits at the engine's 50 Hz cadence.  They also contain a delayed
# fake/replay clock: one invocation can never jump farther because wall time did.
# Raised 0.5/0.25 -> 1.0/0.5 (2026-07-20) IN STEP with SIDE_HEAD_GAIN, and for a
# structural reason rather than for speed's sake. The entry slew is part of the
# reaction's blind window (slew + gate re-arm), which RELEASE_AFTER_S must
# outlast or a sustained pet dies mid-gesture and never ladders receptive ->
# contentment -> warning (the t12 sustain bug). Raising the gain alone doubled
# the slew (12 deg at 0.5 deg/tick = 0.48 s vs 0.24 s) and the offline labelled
# trace stopped reaching contentment at all. Doubling the step restores the old
# TIMING at the new amplitude, so the gesture is bigger without being slower.
HEAD_ROTATION_STEP_DEG = 1.0
BODY_YAW_STEP_DEG = 0.5
ANTENNA_STEP_DEG = 1.0

#: Grace for ``blocked``, which is NOT a fault: it is the expected consequence of
#: this reaction's OWN motion. The commanded pose moves for the entry slew, and
#: the stillness gate must then re-earn ``DEFAULT_STILL_HOLD_S`` before sensing
#: reopens — so the reaction is necessarily blind through both.
#:
#: Under shipped v0.41.0 defaults that is 0.40 s of ANTENNA slew (the binding
#: axis: 20 deg at ``ANTENNA_STEP_DEG``, longer than the head's 0.24 s) plus a
#: 1.0 s hold = 1.40 s, which EXCEEDS ``SENSE_LOSS_GRACE_S``. Reusing that grace
#: aborted the gesture mid-flight as ``sensing_lost`` (found in review on #90;
#: masked locally because the runtime integration test pins ``still_hold_s=0.5``).
#:
#: DERIVED rather than hardcoded, so retuning any of the rate limits, the axis
#: clamps, or the stillness hold keeps this correct by construction.
_WORST_ENTRY_SLEW_S = (
    max(
        HEAD_YAW_LIMIT_DEG / HEAD_ROTATION_STEP_DEG,
        HEAD_PITCH_LIMIT_DEG / HEAD_ROTATION_STEP_DEG,
        ANTENNA_LIMIT_DEG / ANTENNA_STEP_DEG,
        BODY_YAW_LIMIT_DEG / BODY_YAW_STEP_DEG,
    )
    * NOMINAL_TICK_S
)
BLOCKED_GRACE_S = DEFAULT_STILL_HOLD_S + _WORST_ENTRY_SLEW_S + 0.5

#: Forward lean accompanying a side-pat turn. Raised 2.5 -> 4.0 (2026-07-20) on
#: operator feedback that the reaction read as "moving straight forward": with
#: the old 2.5 gain the yaw was only ~4 deg, comparable to this pitch, so the
#: two blended into a nod. Kept as a deliberate accent — the head leans in as
#: well as turning — but now well under the ~10 deg yaw so the turn dominates.
SIDE_PITCH_DEG = 4.0
SCRATCH_PITCH_DEG = 8.0
HEAD_WIGGLE_DEG = 4.0
PITCH_WIGGLE_DEG = 2.0
BODY_WIGGLE_DEG = 2.5

# Antisymmetric pairs draw the antennas TOGETHER: the right joint's sign is
# mirrored from the left (reachy/motion/listen.py:154-157), hardware-verified
# again on 2026-07-20 by holding (+12, -12) on the deployed robot. Amplitudes
# raised from 12/17 after that check — the contraction was legible but
# understated at 12 deg, and the ladder reads better with more range against
# the 20 deg ANTENNA_LIMIT_DEG ceiling.
RECEPTIVE_ANTENNAS = (15.0, -15.0)
CONTENTMENT_ANTENNAS = (20.0, -20.0)
WARNING_ANTENNAS = (-6.0, 6.0)
DONE_ANTENNAS = (-10.0, -10.0)

_CONTACT_PHASES = ("receptive", "contentment", "warning")
_CONTACT_PHASE_RANK = {phase: rank for rank, phase in enumerate(_CONTACT_PHASES)}
_CONTACT_ANTENNAS = {
    "receptive": RECEPTIVE_ANTENNAS,
    "contentment": CONTENTMENT_ANTENNAS,
    "warning": WARNING_ANTENNAS,
}
_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def _step(current: float, target: float, maximum: float) -> float:
    delta = target - current
    if abs(delta) <= maximum:
        return target
    return current + math.copysign(maximum, delta)


def _smoothstep(progress: float) -> float:
    p = max(0.0, min(1.0, progress))
    return p * p * (3.0 - 2.0 * p)


def _complete_pose(
    *,
    pitch: float = 0.0,
    yaw: float = 0.0,
    antennas: tuple[float, float] = (0.0, 0.0),
    body_yaw: float = 0.0,
) -> Contribution:
    head = neutral_head()
    head.update(
        pitch=_clamp(pitch, HEAD_PITCH_LIMIT_DEG),
        yaw=_clamp(yaw, HEAD_YAW_LIMIT_DEG),
    )
    return Contribution(
        head=head,
        antennas=tuple(_clamp(value, ANTENNA_LIMIT_DEG) for value in antennas),
        body_yaw=_clamp(body_yaw, BODY_YAW_LIMIT_DEG),
    )


class PetReaction:
    """One isolated reaction state machine; callable as a contribution function."""

    def __init__(self) -> None:
        self._pose = _complete_pose()
        self._touch_type: str | None = None
        self._side_head_yaw = 0.0
        self._side_body_yaw = 0.0
        self._direction_latched = False
        self._phase: str | None = None
        self._loss_started_at: float | None = None
        self._finish_started_at: float | None = None
        self._finish_start_pose: Contribution | None = None
        self._finish_reason: str | None = None
        self._finish_count = 0
        self._done = False
        self._last_t = 0.0

    @property
    def phase(self) -> str | None:
        """Highest available contact phase observed by this reaction."""
        return self._phase

    @property
    def finish_reason(self) -> str | None:
        """Latched terminal cause; sensing loss is distinct from physical release."""
        return self._finish_reason

    @property
    def finish_started_at(self) -> float | None:
        return self._finish_started_at

    @property
    def finish_count(self) -> int:
        """Number of coordinated finishing gestures started (always zero or one)."""
        return self._finish_count

    def __call__(self, t_local: float, _params: dict, sense: Sense) -> Contribution:
        if self._done:
            return Contribution(done=True)

        try:
            t = float(t_local)
            if not math.isfinite(t):
                raise ValueError("non-finite behavior-local time")
            t = max(self._last_t, max(0.0, t))
            self._last_t = t
        except Exception:  # malformed clock becomes a safe finish
            t = self._last_t
            self._begin_finish(t, "fault")

        try:
            self._observe(t, sense)
        except Exception:  # perception must never kill the engine
            self._begin_finish(t, "fault")

        if self._done:
            return Contribution(done=True)
        if self._finish_started_at is None and t >= MAX_CONTACT_S:
            self._begin_finish(t, "safety_backstop")

        if self._finish_started_at is not None:
            elapsed = t - self._finish_started_at
            if elapsed >= DONE_GESTURE_S:
                self._done = True
                return Contribution(done=True)
            # Change the complete command on the trigger tick, closing sensing
            # immediately without exceeding one tick's slew envelope.
            target = self._finish_target(max(NOMINAL_TICK_S, elapsed))
        else:
            target = self._contact_target()
        self._pose = self._slew(target)
        return self._pose

    def _observe(self, t: float, sense: Sense) -> None:
        # Once ending starts, later persistent enough/cooldown snapshots cannot
        # restart or truncate the single coordinated gesture.
        if self._finish_started_at is not None:
            return
        state = sense.pat_state
        if not isinstance(state, PatState):
            raise TypeError("sense.pat_state must be PatState")

        if state.availability != "available":
            # `blocked` is this reaction's own motion closing the stillness gate
            # — expected, and necessarily longer than a reader fault. Treating
            # the two alike aborted the gesture mid-flight (see BLOCKED_GRACE_S).
            grace = BLOCKED_GRACE_S if state.availability == "blocked" else SENSE_LOSS_GRACE_S
            if self._loss_started_at is None:
                self._loss_started_at = t
            elif t - self._loss_started_at >= grace:
                self._begin_finish(t, "sensing_lost")
            return

        self._loss_started_at = None
        if state.phase == "cooldown":
            # Admission while the producer's persistent cooldown is active must
            # not replay the just-finished response.
            self._done = True
            return
        if state.phase == "enough":
            self._begin_finish(t, "enough")
            return
        if state.phase == "released" or not state.contact:
            self._begin_finish(t, "observed_release")
            return
        self._latch_touch(state)
        incoming_rank = _CONTACT_PHASE_RANK.get(state.phase)
        if incoming_rank is None:
            incoming_rank = _CONTACT_PHASE_RANK["receptive"]
        current_rank = _CONTACT_PHASE_RANK.get(self._phase or "", -1)
        if incoming_rank >= current_rank:
            self._phase = _CONTACT_PHASES[incoming_rank]

    def _latch_touch(self, state: PatState) -> None:
        if self._touch_type is None and state.touch_type in {"scratch", "side_pat"}:
            self._touch_type = state.touch_type
        if self._touch_type != "side_pat" or self._direction_latched:
            return
        yaw = state.yaw_deg
        if yaw is None or not math.isfinite(yaw) or abs(yaw) <= YAW_DEADBAND_DEG:
            return
        self._side_head_yaw = _clamp(yaw * SIDE_SEEK_SIGN * SIDE_HEAD_GAIN, HEAD_YAW_LIMIT_DEG)
        self._side_body_yaw = _clamp(yaw * SIDE_SEEK_SIGN * SIDE_BODY_GAIN, BODY_YAW_LIMIT_DEG)
        self._direction_latched = True

    def _contact_target(self) -> Contribution:
        if self._phase is None:
            return _complete_pose()
        antennas = _CONTACT_ANTENNAS[self._phase]
        if self._touch_type is None:
            return _complete_pose(antennas=antennas)
        if self._touch_type == "scratch":
            return _complete_pose(pitch=SCRATCH_PITCH_DEG, antennas=antennas)
        return _complete_pose(
            pitch=SIDE_PITCH_DEG,
            yaw=self._side_head_yaw,
            antennas=antennas,
            body_yaw=self._side_body_yaw,
        )

    def _begin_finish(self, t: float, reason: str) -> None:
        if self._finish_started_at is not None or self._done:
            return
        self._finish_started_at = t
        self._finish_start_pose = self._pose
        self._finish_reason = reason
        self._finish_count += 1

    def _finish_target(self, elapsed: float) -> Contribution:
        start = cast(Contribution, self._finish_start_pose)
        head = cast(dict[str, float], start.head)
        antennas = cast(tuple[float, float], start.antennas)
        body_yaw = cast(float, start.body_yaw)
        progress = min(1.0, elapsed / DONE_GESTURE_S)
        envelope = math.sin(math.pi * progress)
        wave = math.sin(4.0 * math.pi * progress)
        slower_wave = math.sin(2.0 * math.pi * progress)
        antenna_progress = _smoothstep(progress)

        return _complete_pose(
            pitch=head["pitch"] + PITCH_WIGGLE_DEG * envelope * slower_wave,
            yaw=head["yaw"] + HEAD_WIGGLE_DEG * envelope * wave,
            antennas=(
                antennas[0] + (DONE_ANTENNAS[0] - antennas[0]) * antenna_progress,
                antennas[1] + (DONE_ANTENNAS[1] - antennas[1]) * antenna_progress,
            ),
            body_yaw=body_yaw - BODY_WIGGLE_DEG * envelope * wave,
        )

    def _slew(self, target: Contribution) -> Contribution:
        current_head = cast(dict[str, float], self._pose.head)
        target_head = cast(dict[str, float], target.head)
        current_antennas = cast(tuple[float, float], self._pose.antennas)
        target_antennas = cast(tuple[float, float], target.antennas)
        current_body = cast(float, self._pose.body_yaw)
        target_body = cast(float, target.body_yaw)

        head = {
            axis: _step(current_head[axis], target_head[axis], HEAD_ROTATION_STEP_DEG)
            for axis in _HEAD_AXES
        }
        return Contribution(
            head=head,
            antennas=(
                _step(current_antennas[0], target_antennas[0], ANTENNA_STEP_DEG),
                _step(current_antennas[1], target_antennas[1], ANTENNA_STEP_DEG),
            ),
            body_yaw=_step(current_body, target_body, BODY_YAW_STEP_DEG),
        )


def make_pet_reaction() -> Callable[[float, dict, Sense], Contribution]:
    """Return one fresh stateful pet-reaction contribution function."""
    return PetReaction()


__all__ = [
    "ANTENNA_LIMIT_DEG",
    "ANTENNA_STEP_DEG",
    "BODY_YAW_LIMIT_DEG",
    "BODY_YAW_STEP_DEG",
    "DONE_GESTURE_S",
    "HEAD_PITCH_LIMIT_DEG",
    "HEAD_ROTATION_STEP_DEG",
    "HEAD_YAW_LIMIT_DEG",
    "MAX_CONTACT_S",
    "PAT_COOLDOWN_S",
    "PetReaction",
    "SENSE_LOSS_GRACE_S",
    "BLOCKED_GRACE_S",
    "make_pet_reaction",
]
