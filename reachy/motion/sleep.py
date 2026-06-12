"""The ``sleep`` producer: map :class:`~reachy.sleep.state.SleepState` to motion.

Translates the three wakefulness states — ALERT, DROWSY, ASLEEP — into motion
submitted to the shared serial :class:`~reachy.motion.queue.MotionQueue`:

ALERT
    Normal alive idle (full-energy :class:`~reachy.motion.idle.AliveConfig`).
    This mirrors :class:`~reachy.motion.listen.ListenProducer`'s idle layer so
    the robot stays alive and gently animated when fully awake.

DROWSY
    Progressively lower-energy alive idle — energy scales down toward ~0.2 as
    sleepiness deepens.  The robot still breathes (oscillation present) but
    ambient gaze/antenna/body wander quiets down.  Uses the same
    :func:`~reachy.motion.idle.next_pose` planner with a scaled
    :class:`~reachy.motion.idle.AliveConfig`; no new motion path.

ASLEEP
    Near-still "sleep breathe" — a slow body rock (inspired by
    reachy_nova's ``SLEEP_ROCK_FREQ = 0.07 Hz, SLEEP_ROCK_BODY = 12.0 deg``)
    plus gentle antenna breathing (±1.5 ° @ ~0.05 Hz) and a tiny head
    pitch droop.  Head yaw and roll are near-zero; the robot is visibly
    *asleep*, not just still.

Wake transition (ASLEEP/DROWSY → ALERT)
    A single re-engagement gesture (antennas snap open, head lifts, brief body
    sway) submitted once when :meth:`SleepProducer.wake` is called.  The caller
    (t8) drives this: it calls ``wake()`` when stimulation resets the FSM to ALERT.

All moves are submitted to the :class:`~reachy.motion.queue.MotionQueue`;
:func:`reachy.motion.server.run` drains the queue one move at a time.  The
producer is a **pure planner** — no ``transport`` calls, no threads — so
transport errors can never kill the loop.  Motion errors in the executor degrade
silently (the server's error ceiling, see :mod:`reachy.motion.server`).

Coalesce keys
-------------
* Drowsy/ASLEEP idle-like moves use :data:`SLEEP_COALESCE_KEY` (``"sleep_idle"``),
  independent of :data:`~reachy.motion.queue.IDLE_KEY` so a drowsy robot's pose
  does not interfere with the listen idle layer if both producers are active.
* ALERT idle moves use :data:`~reachy.motion.queue.IDLE_KEY` (matching the
  listen idle layer convention).
* Wake gesture uses ``coalesce_key=None`` for strict one-shot ordering.

Feel reference (cited, not imported)
-------------------------------------
``/home/spark/git/reachy_nova/reachy_nova/sleep_orchestrator.py``:
* ``SLEEP_ROCK_FREQ = 0.07`` Hz  → ``~14 s`` per cycle (very slow)
* ``SLEEP_ROCK_BODY = 12.0`` deg → body yaw rocking amplitude
* Antenna breath ``±1.5 °`` @ ``0.05 Hz``; 8 s startup ramp.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass, field

from reachy.motion.idle import AliveConfig, next_pose
from reachy.motion.queue import IDLE_KEY, MotionAction, MotionQueue
from reachy.sleep.state import SleepState

# ---------------------------------------------------------------------------
# Module-level tunables — edit to retune without code change
# ---------------------------------------------------------------------------

#: Coalesce key for DROWSY and ASLEEP idle-like poses (independent of IDLE_KEY so
#: the sleep layer and the listen idle layer do not clobber each other).
SLEEP_COALESCE_KEY: str = "sleep_idle"

#: Coalesce key for the wake re-engagement gesture.  ``None`` → strict one-shot
#: ordering (the wake gesture never coalesces away mid-queue).
WAKE_COALESCE_KEY: str | None = None

#: Body yaw rocking amplitude for sleep breathe (degrees).
#: Cited from reachy_nova's ``SLEEP_ROCK_BODY = 12.0``.
SLEEP_BREATHE_BODY_YAW: float = 12.0

#: Rocking frequency for the sleep body rock (Hz).
#: Cited from reachy_nova's ``SLEEP_ROCK_FREQ = 0.07``.
_SLEEP_ROCK_FREQ: float = 0.07

#: Antenna breath amplitude for sleep (degrees, ±).
#: Cited from reachy_nova's ``1.5 deg`` breath.
_SLEEP_ANTENNA_AMP: float = 1.5

#: Antenna breath frequency for sleep (Hz).
#: Cited from reachy_nova's ``0.05 Hz``.
_SLEEP_ANTENNA_FREQ: float = 0.05

#: Sleep startup ramp duration (seconds): full amplitude after this many seconds of sleep.
#: Cited from reachy_nova's 8-second ramp.
_SLEEP_RAMP_SECONDS: float = 8.0

#: Head pitch droop during sleep (degrees, negative = nod forward/down).
_SLEEP_HEAD_PITCH: float = -5.0

#: Duration of each sleep-breathe pose step (seconds).
_SLEEP_STEP_DURATION: float = 2.0

#: Interval between sleep-breathe pose submissions (seconds).
_SLEEP_INTERVAL: float = 2.0

#: Duration of the wake re-engagement gesture (seconds).
_WAKE_DURATION: float = 1.2

#: Antenna perk on wake (degrees — both sides, positive = up/forward).
_WAKE_ANTENNA_PERK: float = 14.0

#: Head lift on wake (degrees, positive pitch = slight upward tilt).
_WAKE_HEAD_PITCH: float = 4.0

#: Brief body sway on wake (degrees).
_WAKE_BODY_YAW: float = 5.0

#: Energy scaling for DROWSY state (fraction of full energy; ~0.2 at deepest drowsy).
_DROWSY_ENERGY: float = 0.2

#: Energy scaling for ASLEEP state (near-zero wander; the sleep-breathe path ignores this).
_ASLEEP_ENERGY: float = 0.05

#: Energy for ALERT state (full alive idle).
_ALERT_ENERGY: float = 1.0

#: Interpolation for all moves.
_INTERPOLATION: str = "minjerk"


def _head_dict(
    *,
    x: float = 0.0,
    y: float = 0.0,
    z: float = 0.0,
    roll: float = 0.0,
    pitch: float = 0.0,
    yaw: float = 0.0,
) -> dict[str, float]:
    """Build a six-axis head dict (all keys always present, units mm/deg)."""
    return {"x": x, "y": y, "z": z, "roll": roll, "pitch": pitch, "yaw": yaw}


# ---------------------------------------------------------------------------
# SleepProducer
# ---------------------------------------------------------------------------


@dataclass
class SleepProducer:
    """Map :class:`~reachy.sleep.state.SleepState` to motion on the shared queue.

    Construct with the target :class:`~reachy.motion.queue.MotionQueue` and the
    initial :class:`~reachy.sleep.state.SleepState`.  The caller (typically t8's
    wiring layer) owns the :class:`~reachy.sleep.state.SleepStateMachine` and
    updates ``producer.state`` each tick before calling ``producer.update()``.

    The server drives ``producer.update(t, sense)`` each tick.  The producer
    submits actions directly to :attr:`queue`; the return value is ``None``
    (the server's ``action = producer.update(...)`` path is also supported — the
    producer submits and returns ``None`` rather than returning the action, so the
    server's ``if action is not None: q.submit(action)`` branch is a no-op).

    Call :meth:`wake` when a stimulating event resets the FSM to ALERT — the
    producer emits one distinct re-engagement gesture and sets ``self.state``
    to ALERT.
    """

    queue: MotionQueue
    state: SleepState = SleepState.ALERT

    # ---- internal pacing state ----
    _t0: float | None = field(default=None, init=False, repr=False)
    _last_pose_t: float | None = field(default=None, init=False, repr=False)
    # Timestamp of the most recent entry into ASLEEP; the sleep-breathe ramp and
    # phase are measured from here (not producer lifetime) so every fresh sleep
    # cycle ramps in softly. Reset to None whenever the producer leaves ASLEEP.
    _asleep_t0: float | None = field(default=None, init=False, repr=False)
    _rng: random.Random = field(init=False, repr=False)
    # config objects built once per instance
    _alert_config: AliveConfig = field(init=False, repr=False)
    _drowsy_config: AliveConfig = field(init=False, repr=False)

    def __post_init__(self) -> None:
        # Not security-sensitive: decorative idle motion.
        self._rng = random.Random()  # nosec B311
        self._alert_config = AliveConfig(energy=_ALERT_ENERGY)
        # DROWSY: a scaled-down version of the alive config using focused() further
        # scaled by _DROWSY_ENERGY.  We build it manually to hit the exact energy.
        from dataclasses import replace

        self._drowsy_config = replace(
            self._alert_config.focused(),
            energy=_DROWSY_ENERGY,
        )

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def update(self, t: float) -> None:
        """Submit the appropriate motion action for the current :attr:`state`.

        Driven directly by the sleep arc (``run_sleep_arc``), which owns the
        :class:`~reachy.sleep.state.SleepStateMachine` and mirrors its state onto
        :attr:`state` before each call.  Submits actions directly onto
        :attr:`queue` (the caller drains it).  Paced to the idle interval so the
        queue never builds up faster than the executor can drain it.

        Parameters
        ----------
        t:
            Current monotonic time (seconds, injected by the caller).
        """
        if self._t0 is None:
            self._t0 = t

        # Pace: emit one pose per interval.
        interval = (
            _SLEEP_INTERVAL if self.state is SleepState.ASLEEP else self._alert_config.interval
        )
        if self._last_pose_t is not None and (t - self._last_pose_t) < interval:
            return
        self._last_pose_t = t

        elapsed = t - self._t0

        if self.state is SleepState.ASLEEP:
            # Measure the breathe ramp/phase from when this ASLEEP cycle began so
            # the 8-second soft entry re-arms on every fresh transition into sleep.
            if self._asleep_t0 is None:
                self._asleep_t0 = t
            self._submit_sleep_breathe(t - self._asleep_t0)
        else:
            self._asleep_t0 = None
            if self.state is SleepState.DROWSY:
                self._submit_drowsy_idle(elapsed)
            else:
                self._submit_alert_idle(elapsed)

    def wake(self) -> None:
        """Emit the wake re-engagement gesture and snap state to ALERT.

        Call this when a stimulating event (speech, touch, snap) resets the FSM
        to ALERT.  A single ordered action — antennas perk up, head lifts, brief
        body sway — is submitted with ``coalesce_key=None`` for strict one-shot
        ordering.  Safe to call when already ALERT (idempotent).
        """
        self.state = SleepState.ALERT
        self._last_pose_t = None  # reset pacing so idle fires immediately after wake
        self._asleep_t0 = None  # re-arm the sleep-breathe ramp for the next cycle

        action = MotionAction(
            label="wake_reengage",
            head=_head_dict(pitch=_WAKE_HEAD_PITCH),
            antennas=(_WAKE_ANTENNA_PERK, _WAKE_ANTENNA_PERK),
            body_yaw=_WAKE_BODY_YAW,
            duration=_WAKE_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=WAKE_COALESCE_KEY,
        )
        self.queue.submit(action)

    # ------------------------------------------------------------------
    # Public helper (exposed so tests can inspect the energy ladder)
    # ------------------------------------------------------------------

    def _energy_for_state(self, state: SleepState) -> float:
        """Return the effective energy level the producer uses for *state*.

        Exposed so tests can assert the DROWSY < ALERT and ASLEEP < DROWSY
        energy ordering without driving a full tick loop.
        """
        if state is SleepState.ALERT:
            return _ALERT_ENERGY
        if state is SleepState.DROWSY:
            return _DROWSY_ENERGY
        # ASLEEP: near-zero wander (the sleep-breathe path generates its own motion)
        return _ASLEEP_ENERGY

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _submit_alert_idle(self, elapsed: float) -> None:
        """Enqueue one full-energy alive idle pose (ALERT state)."""
        pose = next_pose(elapsed, self._rng, self._alert_config)
        action = MotionAction(
            label=f"idle sleep_alert {elapsed:.1f}",
            head=dict(pose["head"]),  # type: ignore[arg-type]
            antennas=pose["antennas"],  # type: ignore[arg-type]
            body_yaw=float(pose["body_yaw"]),  # type: ignore[arg-type]
            duration=float(pose["duration"]),  # type: ignore[arg-type]
            interpolation=str(pose["interpolation"]),
            coalesce_key=IDLE_KEY,
        )
        self.queue.submit(action)

    def _submit_drowsy_idle(self, elapsed: float) -> None:
        """Enqueue a low-energy alive idle pose (DROWSY state)."""
        pose = next_pose(elapsed, self._rng, self._drowsy_config)
        action = MotionAction(
            label=f"sleep_drowsy {elapsed:.1f}",
            head=dict(pose["head"]),  # type: ignore[arg-type]
            antennas=pose["antennas"],  # type: ignore[arg-type]
            body_yaw=float(pose["body_yaw"]),  # type: ignore[arg-type]
            duration=float(pose["duration"]),  # type: ignore[arg-type]
            interpolation=str(pose["interpolation"]),
            coalesce_key=SLEEP_COALESCE_KEY,
        )
        self.queue.submit(action)

    def _submit_sleep_breathe(self, elapsed: float) -> None:
        """Enqueue a near-still sleep-breathe pose (ASLEEP state).

        Cited from reachy_nova's ``SleepOrchestrator.tick_sleeping``:
        * Body rock: ``SLEEP_ROCK_BODY * ramp * sin(2π * SLEEP_ROCK_FREQ * t)``
        * Antenna breath: ``±1.5 ° * ramp * sin(2π * 0.05 * t)``
        * 8-second startup ramp to prevent abrupt entry.
        """
        ramp = min(1.0, elapsed / _SLEEP_RAMP_SECONDS)

        # Body rocking (very slow — ~14s per full cycle)
        rock_phase = 2.0 * math.pi * _SLEEP_ROCK_FREQ * elapsed
        rock_body = SLEEP_BREATHE_BODY_YAW * ramp * math.sin(rock_phase)

        # Antenna breathing (±1.5° @ 0.05 Hz)
        ant_phase = 2.0 * math.pi * _SLEEP_ANTENNA_FREQ * elapsed
        ant_breath = _SLEEP_ANTENNA_AMP * ramp * math.sin(ant_phase)
        # Right antenna forward (+), left antenna back (−), gentle differential sway.
        antennas = (ant_breath, -ant_breath)

        # Head: slight forward-droop pitch, near-zero yaw and roll.
        head = _head_dict(pitch=_SLEEP_HEAD_PITCH * ramp)

        action = MotionAction(
            label="sleep_breathe",
            head=head,
            antennas=antennas,
            body_yaw=rock_body,
            duration=_SLEEP_STEP_DURATION,
            interpolation=_INTERPOLATION,
            coalesce_key=SLEEP_COALESCE_KEY,
        )
        self.queue.submit(action)
