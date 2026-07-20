"""Self-motion latch over the COMMANDED pose — the sense behind the moving rms
floor (issue #95).

Why this exists — the self-noise loop, measured
-----------------------------------------------
The deployed runtime's rms-driven ``look-toward-sound`` rule self-sustained:
the robot's OWN actuator noise (head and, notably, the ANTENNAS the orienting
reaction sweeps) cleared the rule's 0.02 admission floor, which admitted
orienting, which made more noise. Measured on the deployed box: a still robot
in a quiet room NEVER crosses 0.02 (1459 samples, max 0.00953), while with the
runtime running in the same room the rule genuinely fired 42x in 3 h. The 0.02
still-floor itself is measured CORRECT and must not move; what was missing is
the condition "is that loudness plausibly my own actuators?".

:class:`SelfMotionDriver` supplies that condition: a
:class:`~reachy.behavior.rule_engine.TickBus` driver (``callable(ctx) ->
None``, like :class:`~reachy.behavior.pat_sense.PatSenseDriver`) that watches
each tick's COMMANDED pose from ``ctx.pose`` — head x/y/z (mm) and
roll/pitch/yaw (deg), BOTH antennas (deg), and body_yaw (deg); the antennas
are deliberately included because they drove the loop — computes per-tick
per-axis deltas, and latches ``moving=True`` the moment any axis exceeds its
eps. :meth:`is_moving` is the non-consuming peek the rms provider (and the
``self_moving`` sense field) reads.

The eps values — pat_sense's still_eps precedent
------------------------------------------------
Each eps is a per-tick VELOCITY tolerance, exactly the quantity
:data:`reachy.behavior.pat_sense.DEFAULT_STILL_EPS` (0.035 deg/tick) judges
for the pat stillness gate — the one commanded-motion threshold in this repo
with hands-on calibration behind it. The deg default reuses that measured
value; the mm default carries the same per-tick magnitude over to the
millimetre axes (0.035 mm/tick ≈ 1.75 mm/s at 50 Hz — comfortably below any
deliberate head translation, comfortably above float jitter). Both are
CONSERVATIVE in the direction that matters for #95: a smaller eps flags motion
sooner, and the failure mode being fixed is UNDER-suppression (the robot
hearing itself), not over-suppression.

The release tail — why 0.25 s, not pat's 1.0 s
----------------------------------------------
``moving`` releases only after a CONTINUOUS below-eps tail
(:data:`DEFAULT_TAIL_S`, env :data:`TAIL_S_ENV`). The tail is short because
actuator NOISE stops almost immediately when the command does — the
holding-servo hum on a stopped head is measured below the 0.02 floor (the
still-robot 1459-sample run above was taken with servos powered and holding).
That is a different physical question from pat_sense's 1.0 s
``still_hold_s``, which waits for the PLANT (pose ringing) to settle; sound
does not ring, so a 1.0 s tail would only blind the rms sense for longer than
the noise actually lasts.

Degradation
-----------
Never raises out of the driver (mirroring
:class:`~reachy.behavior.pat_sense.PatSenseDriver`'s never-raise posture): a
missing/malformed ``ctx.pose`` or clock degrades to "no motion evidence this
tick" — the previous sample is dropped so a pose step observed ACROSS the gap
can never read as motion (the same re-seed contract pat_sense applies after a
detection gap), and the latch itself is left unchanged, which errs toward
suppression — the conservative direction for this defect class. In the wired
engine ``ctx.pose`` and ``ctx.now`` are always populated, so the degrade paths
exist for foreign/legacy contexts only.

Import-safe without ``reachy_mini`` and pure stdlib: the driver touches ONLY
``ctx.pose`` (the engine's own composed dict) — no SDK, no transport, no
numpy — so composition wires it UNCONDITIONALLY on a bare box.
"""

from __future__ import annotations

import logging
import math

logger = logging.getLogger(__name__)

#: Per-tick tolerance for the rotational axes (deg/tick): head roll/pitch/yaw,
#: both antennas, body_yaw. Reuses the hands-on-calibrated value of
#: :data:`reachy.behavior.pat_sense.DEFAULT_STILL_EPS` (see the module
#: docstring) rather than inventing a second commanded-velocity threshold.
DEFAULT_EPS_DEG = 0.035

#: Per-tick tolerance for the translational head axes (mm/tick) — the same
#: per-tick magnitude as the deg tolerance, carried over to millimetres
#: (≈1.75 mm/s at 50 Hz).
DEFAULT_EPS_MM = 0.035

#: Continuous below-eps time (seconds) before ``moving`` releases. Short on
#: purpose — actuator noise stops with the command (the holding-servo hum is
#: measured below the 0.02 rms floor), unlike pat's 1.0 s pose-settle hold.
DEFAULT_TAIL_S = 0.25

#: Env overrides, read at COMPOSITION time (``reachy.cli._commands.behavior``
#: resolves them, mirroring the ``REACHY_PAT_*`` tuning pattern) — the driver
#: itself never reads the environment, so tests stay deterministic.
TAIL_S_ENV = "REACHY_SELF_MOVING_TAIL_S"
EPS_DEG_ENV = "REACHY_SELF_MOVING_EPS_DEG"
EPS_MM_ENV = "REACHY_SELF_MOVING_EPS_MM"

_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")

#: How many leading entries of the watched-pose tuple are millimetre axes
#: (head x/y/z); every later entry — head rotations, body_yaw, both antennas —
#: is judged with the deg tolerance.
_MM_AXIS_COUNT = 3


class SelfMotionDriver:
    """Latch "the engine is commanding motion" from per-tick ``ctx.pose`` deltas.

    Register :meth:`__call__` on the engine's ``tick_seam`` (grouped with the
    other latching sense drivers, so the latch reflects THIS tick's commanded
    pose before the NEXT tick's sense read — the same end-of-tick cadence
    :class:`~reachy.behavior.pat_sense.PatSenseDriver` documents), then wire
    :meth:`is_moving` wherever the condition is consumed: the ``self_moving``
    sense provider and :func:`reachy.behavior.rms_sense.make_rms_provider`'s
    ``moving`` seam.
    """

    def __init__(
        self,
        *,
        eps_deg: float = DEFAULT_EPS_DEG,
        eps_mm: float = DEFAULT_EPS_MM,
        tail_s: float = DEFAULT_TAIL_S,
    ) -> None:
        self._eps_deg = max(0.0, float(eps_deg))
        self._eps_mm = max(0.0, float(eps_mm))
        self._tail_s = max(0.0, float(tail_s))
        #: The previous tick's watched-pose tuple, or ``None`` before the first
        #: valid sample / after a degrade (so a gap-crossing step never deltas).
        self._last: tuple[float, ...] | None = None
        #: Clock reading of the last above-eps delta, while the latch is set.
        self._last_motion_t: float | None = None
        self._moving = False

    # ------------------------------------------------------------------
    # TickBus driver entry point
    # ------------------------------------------------------------------

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """One tick: delta the commanded pose, latch or release. Never raises."""
        try:
            self._process(ctx)
        except Exception:  # noqa: BLE001 — a sense tap must never crash the loop
            logger.warning("SelfMotionDriver tick raised; motion latch unchanged", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        now = self._now(ctx)
        pose = self._watched_pose(ctx)
        if pose is None or now is None:
            # No usable sample: drop the previous one so the next valid tick
            # cannot delta across the gap, and leave the latch as it stands
            # (erring toward suppression — see "Degradation" above).
            self._last = None
            return
        previous = self._last
        self._last = pose
        if previous is None:
            return  # first sample (boot or post-gap): nothing to delta against
        if self._exceeds_eps(pose, previous):
            self._moving = True
            self._last_motion_t = now
            return
        if (
            self._moving
            and self._last_motion_t is not None
            and now - self._last_motion_t >= self._tail_s
        ):
            self._moving = False
            self._last_motion_t = None

    def _exceeds_eps(self, current: tuple[float, ...], previous: tuple[float, ...]) -> bool:
        for index, (cur, prev) in enumerate(zip(current, previous)):
            eps = self._eps_mm if index < _MM_AXIS_COUNT else self._eps_deg
            if abs(cur - prev) > eps:
                return True
        return False

    # ------------------------------------------------------------------
    # Provider seam
    # ------------------------------------------------------------------

    def is_moving(self) -> bool:
        """The current latch — a non-consuming PEEK, safe to call many times a tick.

        Directly usable as ``SenseProviders(self_moving=driver.is_moving)`` and
        as ``make_rms_provider``'s ``moving`` seam. Never raises.
        """
        return self._moving

    # ------------------------------------------------------------------
    # Defensive readers of the (duck-typed) TickContext
    # ------------------------------------------------------------------

    @staticmethod
    def _watched_pose(ctx) -> tuple[float, ...] | None:  # type: ignore[no-untyped-def]
        """The 9-axis watched tuple from ``ctx.pose``, or ``None`` on any defect.

        Same defensive shape (and the same axis ordering: six head axes, then
        body_yaw, then both antennas) as
        :meth:`reachy.behavior.pat_sense.PatSenseDriver._commanded_pose` —
        restated here rather than imported so this module stays a stdlib-only
        leaf (pat_sense pulls in numpy through the detector).
        """
        pose = getattr(ctx, "pose", None)
        if not isinstance(pose, dict):
            return None
        head = pose.get("head")
        antennas = pose.get("antennas")
        if not isinstance(head, dict) or not isinstance(antennas, (tuple, list)):
            return None
        if (
            len(antennas) != 2
            or "body_yaw" not in pose
            or any(axis not in head for axis in _HEAD_AXES)
        ):
            return None
        try:
            values = tuple(float(head[axis]) for axis in _HEAD_AXES) + (
                float(pose["body_yaw"]),
                float(antennas[0]),
                float(antennas[1]),
            )
        except (TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    @staticmethod
    def _now(ctx) -> float | None:  # type: ignore[no-untyped-def]
        now = getattr(ctx, "now", None)
        if not isinstance(now, (int, float)):
            return None
        value = float(now)
        return value if math.isfinite(value) else None
