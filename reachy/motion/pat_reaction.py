"""The ``pat_reaction`` producer: lean-into-touch gesture sequence on a pat event.

Translates a pat detector event (``touch_type`` + ``level``) into a small
ordered sequence of :class:`~reachy.motion.queue.MotionAction` objects enqueued
onto the shared serial :class:`~reachy.motion.queue.MotionQueue`.  The executor
handles all I/O; this module is a **pure planner** — no transport calls, no
sleeps, no threads.

Touch types
-----------
``scratch``
    Head tilts gently downward (pitch down) as if leaning into a neck scratch,
    with both antennas raised in an affection overlay.  A brief nuzzle hold
    follows, then a sigh settle back to neutral.

``side_pat``
    Head yaws toward the patting hand and the body follows with a soft matching
    yaw, giving the "I turn to enjoy this" lean.  The same antenna affection
    overlay applies.  Nuzzle hold, then settle back to neutral.

Level scaling
-------------
``level1`` (single / light pat) — base amplitudes and durations.
``level2`` (sustained / heavier pat) — amplitudes scaled by :data:`LEVEL2_SCALE`,
lean duration scaled to :data:`LEAN_DURATION_L2`.

Coalescing
----------
Each action in the sequence uses ``coalesce_key=None`` so the three moves
(lean, nuzzle, settle) queue **strictly in order** — the same one-shot semantics
as :class:`~reachy.motion.expression.ExpressionProducer`.

Head axis key names
-------------------
Confirmed from ``reachy/motion/listen.py`` line 84::

    {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": <deg>, "yaw": <deg>}

All six keys are always present in the ``head`` dict for clarity, with unused
axes set to ``0.0``.
"""

from __future__ import annotations

from dataclasses import dataclass

from reachy.motion.queue import MotionAction, MotionQueue

# ---------------------------------------------------------------------------
# Module-level tunable constants — edit these to retune poses without code change
# ---------------------------------------------------------------------------

#: Downward pitch for the scratch lean (degrees, positive = pitch down).
#: Negated when building the head dict so the head dips toward the hand.
LEAN_PITCH_DOWN: float = 12.0

#: Head yaw toward the patting hand for a side-pat lean (degrees).
#: The hand is assumed to be on the robot's right; negate to flip side.
LEAN_YAW_SIDE: float = 14.0

#: Soft body yaw to follow the head on a side-pat (degrees, same sign as yaw).
SIDE_BODY_YAW: float = 8.0

#: Both-antenna raise during the lean (degrees, positive = up/forward).
#: Applied symmetrically to (right, left) as a gentle affection signal.
ANTENNA_AFFECTION: float = 10.0

#: Lean duration for a level1 (light/single) pat, in seconds.
LEAN_DURATION_L1: float = 1.2

#: Lean duration for a level2 (sustained/heavier) pat, in seconds.
LEAN_DURATION_L2: float = 1.8

#: Duration of the brief nuzzle/hold phase (same for both levels).
NUZZLE_DURATION: float = 0.8

#: Duration of the settling sigh back to neutral (same for both levels).
SETTLE_DURATION: float = 1.5

#: Amplitude and duration scale factor for level2 (must be > 1.0).
LEVEL2_SCALE: float = 1.3

#: Interpolation curve — minjerk for all moves (smooth, no snap).
_INTERPOLATION: str = "minjerk"

_VALID_TOUCH_TYPES = frozenset({"scratch", "side_pat"})
_VALID_LEVELS = frozenset({"level1", "level2"})


def reaction_duration(level: str = "level1") -> float:
    """Total wall-clock seconds a :meth:`PatReaction.react` sequence takes to play.

    The sum of the lean, nuzzle, and settle phase durations — the lean phase is
    longer for a ``"level2"`` (sustained) pat. A caller driving the reaction (the
    ``pat`` run loop) uses this to hold the pat-active signal — and pause its own
    sensing — for exactly as long as the robot is executing its own lean, so the
    deliberate motion is never mistaken for a fresh pat (no self-trigger).
    """
    lean_dur = LEAN_DURATION_L2 if level == "level2" else LEAN_DURATION_L1
    return lean_dur + NUZZLE_DURATION + SETTLE_DURATION


def _head_dict(*, pitch: float = 0.0, yaw: float = 0.0) -> dict[str, float]:
    """Build a six-axis head dict with only ``pitch`` and/or ``yaw`` non-zero.

    Key names confirmed from ``reachy/motion/listen.py`` line 84:
    ``{"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": …, "yaw": …}``
    """
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": pitch, "yaw": yaw}


@dataclass
class PatReaction:
    """Map a pat detector event to a calm lean/nuzzle/settle gesture sequence.

    Construct with the :class:`~reachy.motion.queue.MotionQueue` the executor
    drains; call :meth:`react` on every pat event.  The three resulting
    :class:`~reachy.motion.queue.MotionAction` objects are submitted with
    ``coalesce_key=None`` so they execute in strict order.

    Example::

        producer = PatReaction(queue=motion_queue)
        producer.react("scratch")          # single pat on the head/neck
        producer.react("side_pat", "level2")  # sustained pat on the side
    """

    queue: MotionQueue

    def react(self, touch_type: str, level: str = "level1") -> None:
        """Build and enqueue a lean→nuzzle→settle sequence for *touch_type*.

        Parameters
        ----------
        touch_type:
            ``"scratch"`` (head/neck area) or ``"side_pat"`` (side of head).
        level:
            ``"level1"`` (light/single pat) or ``"level2"`` (sustained/heavier).

        Raises
        ------
        ValueError
            If *touch_type* or *level* is not a recognised value.
        """
        if touch_type not in _VALID_TOUCH_TYPES:
            raise ValueError(
                f"unknown touch_type {touch_type!r}; expected one of {sorted(_VALID_TOUCH_TYPES)}"
            )
        if level not in _VALID_LEVELS:
            raise ValueError(f"unknown level {level!r}; expected one of {sorted(_VALID_LEVELS)}")

        scale = LEVEL2_SCALE if level == "level2" else 1.0
        lean_dur = LEAN_DURATION_L2 if level == "level2" else LEAN_DURATION_L1

        if touch_type == "scratch":
            self._react_scratch(scale, lean_dur)
        else:
            self._react_side_pat(scale, lean_dur)

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _react_scratch(self, scale: float, lean_dur: float) -> None:
        """Enqueue lean-down → nuzzle → settle for a head/neck scratch."""
        lean_pitch = -(LEAN_PITCH_DOWN * scale)  # negative = pitch down

        lean = MotionAction(
            label="pat_scratch_lean",
            head=_head_dict(pitch=lean_pitch),
            antennas=(ANTENNA_AFFECTION, ANTENNA_AFFECTION),
            body_yaw=None,
            duration=lean_dur,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        nuzzle = MotionAction(
            label="pat_scratch_nuzzle",
            head=_head_dict(pitch=lean_pitch),
            antennas=(ANTENNA_AFFECTION, ANTENNA_AFFECTION),
            body_yaw=None,
            duration=NUZZLE_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        settle = MotionAction(
            label="pat_scratch_settle",
            head=_head_dict(pitch=0.0),
            antennas=(0.0, 0.0),
            body_yaw=None,
            duration=SETTLE_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        self.queue.submit(lean)
        self.queue.submit(nuzzle)
        self.queue.submit(settle)

    def _react_side_pat(self, scale: float, lean_dur: float) -> None:
        """Enqueue yaw-toward → nuzzle → settle for a side-of-head pat."""
        lean_yaw = LEAN_YAW_SIDE * scale
        body_yaw = SIDE_BODY_YAW * scale

        lean = MotionAction(
            label="pat_side_lean",
            head=_head_dict(yaw=lean_yaw),
            antennas=(ANTENNA_AFFECTION, ANTENNA_AFFECTION),
            body_yaw=body_yaw,
            duration=lean_dur,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        nuzzle = MotionAction(
            label="pat_side_nuzzle",
            head=_head_dict(yaw=lean_yaw),
            antennas=(ANTENNA_AFFECTION, ANTENNA_AFFECTION),
            body_yaw=body_yaw,
            duration=NUZZLE_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        settle = MotionAction(
            label="pat_side_settle",
            head=_head_dict(yaw=0.0),
            antennas=(0.0, 0.0),
            body_yaw=0.0,
            duration=SETTLE_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=None,
        )
        self.queue.submit(lean)
        self.queue.submit(nuzzle)
        self.queue.submit(settle)
