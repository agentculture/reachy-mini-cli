"""The ``listen`` producer: turn the head toward sustained sound, via the motion queue.

A pure-ish decision object: feed it ``(t, sense)`` each tick and it returns a
:class:`~reachy.motion.queue.MotionAction` to submit (a coalescing look-at) when — and
only when — a sound is both far enough from the current heading (``deadband``) and has
persisted long enough (``dwell``). Transient/noisy DoA never accumulates dwell, so the
head holds still and only turns deliberately. Turns *toward* a more off-axis sound are
``alert_speed`` (a touch quick); moves back *toward* center are ``relax_speed`` (a slow,
gentle relax). After ``recenter_after`` seconds with no usable sound it eases back to
centre. The smooth motor trajectory itself is the daemon's job (the action is a minjerk
``goto``); this object only decides *when* and *where*.
"""

from __future__ import annotations

from dataclasses import dataclass

from reachy.behavior.sense import Sense, doa_angle_to_yaw
from reachy.motion.queue import LOOK_KEY, MotionAction


@dataclass
class ListenParams:
    """Tunables for :class:`ListenProducer` (degrees, seconds, deg/s)."""

    gain: float = 0.6
    max_yaw: float = 35.0
    deadband: float = 16.0  # ignore sound within this of the current heading
    dwell: float = 1.5  # a new direction must persist this long before turning
    hold: float = 3.0  # after turning, stay at that direction this long before reconsidering
    alert_speed: float = 18.0  # deg/s turning toward a new (more off-axis) sound
    relax_speed: float = 18.0  # deg/s easing back toward center (same smooth pace as turns)
    min_dur: float = 1.5  # floor so even small turns are deliberate, never snappy
    max_dur: float = 4.0
    speech_only: bool = False
    recenter_after: float = 4.0  # ease to center after this long with no usable sound


def _head(yaw: float) -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": yaw}


@dataclass
class ListenProducer:
    """Stateful DoA→look decision. Call :meth:`update` each tick."""

    params: ListenParams
    committed: float = 0.0
    _cand: float | None = None
    _cand_since: float | None = None
    _last_signal_t: float | None = None
    _hold_until: float = 0.0

    def _move_to(self, target: float, t: float) -> MotionAction:
        p = self.params
        toward_center = abs(target) < abs(self.committed)
        speed = p.relax_speed if toward_center else p.alert_speed
        dur = max(
            p.min_dur, min(p.max_dur, abs(target - self.committed) / speed if speed else p.max_dur)
        )
        self.committed = target
        self._cand = self._cand_since = None
        # Commit to this heading: ignore new directions until the move lands AND we've
        # dwelt `hold` seconds there, so the head doesn't whip back and forth.
        self._hold_until = t + dur + p.hold
        kind = "relax" if toward_center else "look"
        return MotionAction(
            label=f"{kind} {target:+.0f}",
            head=_head(target),
            duration=dur,
            interpolation="minjerk",
            coalesce_key=LOOK_KEY,
        )

    def update(self, t: float, sense: Sense) -> MotionAction | None:
        """Return a look-at action to submit this tick, or ``None`` to hold."""
        p = self.params
        angle = sense.doa_angle
        signal = angle is not None and (not p.speech_only or sense.speech_detected)
        if signal:
            self._last_signal_t = t
        if t < self._hold_until:
            # Holding at the just-committed direction — ignore new candidates entirely.
            self._cand = self._cand_since = None
            return None
        if not signal:
            # No usable sound: after a grace period, ease back to center once.
            if (
                abs(self.committed) > 1e-9  # off-center (committed is set to exactly 0 at center)
                and self._last_signal_t is not None
                and (t - self._last_signal_t) >= p.recenter_after
            ):
                return self._move_to(0.0, t)
            return None
        desired = max(-p.max_yaw, min(p.max_yaw, doa_angle_to_yaw(angle, p.gain)))
        if abs(desired - self.committed) > p.deadband:
            if self._cand is None or abs(desired - self._cand) > p.deadband:
                self._cand, self._cand_since = desired, t
            elif (t - self._cand_since) >= p.dwell:
                return self._move_to(desired, t)
        else:
            self._cand = self._cand_since = None
        return None
