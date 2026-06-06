"""The ``listen`` producer: turn the head toward sound, via the motion queue.

A pure-ish decision object: feed it ``(t, sense)`` each tick (plus the live
``snap`` / ``sound_present`` liveness signals) and it returns a
:class:`~reachy.motion.queue.MotionAction` to submit. The head turns (Tier-2)
*only* on a deliberate event — detected **speech** or a loud **snap** — and only
when that sound is far enough off-axis (``deadband``). A bare DoA ``angle``
never commits a turn on its own, because the daemon **latches** the angle: it
holds the last direction through silence, so ``angle is not None`` is not a
"sound is happening now" signal. Turns *toward* a more off-axis sound are
``alert_speed`` (a touch quick); moves back *toward* center are ``relax_speed``
(a slow, gentle relax). After ``recenter_after`` seconds with no *live* sound it
eases back to centre. The smooth motor trajectory itself is the daemon's job
(the action is a minjerk ``goto``); this object only decides *when* and *where*.

**Liveness vs. latched angle.** The honest "sound now" signals are
``sense.speech_detected``, the ``snap`` transient, and ``sound_present`` (live
mic energy above the ambient floor). ``sound_present is None`` means there is no
audio path (the HTTP/remote profile) — we then fall back to
``sense.doa_angle is not None`` as a degraded best-effort. The effective boolean
``live`` drives both the Tier-1 lean gate and the recenter silence clock, so a
frozen/latched angle during true silence neither leans nor blocks recentering.

**Tier-1 antenna lean:** on every tick where sound is *live* but no head turn is
committed or held this tick, the *near-side* antenna deflects gently toward the
sound instead. The head is never driven by this path — only the antenna that
faces the sound moves; the far antenna returns to neutral (0°). Repeated leans
coalesce via ``ANTENNA_KEY`` so only the latest intent queues.
"""

from __future__ import annotations

from dataclasses import dataclass

from reachy.behavior.sense import Sense, doa_angle_to_yaw
from reachy.motion.queue import ANTENNA_KEY, LOOK_KEY, MotionAction


@dataclass
class ListenParams:
    """Tunables for :class:`ListenProducer` (degrees, seconds, deg/s)."""

    gain: float = 0.6
    max_yaw: float = 35.0
    deadband: float = 16.0  # ignore sound within this of the current heading
    dwell: float = 1.5  # retained for backward compat (CLI --dwell); no longer used
    hold: float = 3.0  # after turning, stay at that direction this long before reconsidering
    alert_speed: float = 18.0  # deg/s turning toward a new (more off-axis) sound
    relax_speed: float = 18.0  # deg/s easing back toward center (same smooth pace as turns)
    min_dur: float = 1.5  # floor so even small turns are deliberate, never snappy
    max_dur: float = 4.0
    speech_only: bool = False
    recenter_after: float = 4.0  # ease to center after this long with no live sound
    antenna_gain: float = 1.0  # scales the lean magnitude (1.0 = full proportion of max_yaw)
    antenna_max: float = 18.0  # maximum near-side antenna deflection in degrees


def _head(yaw: float) -> dict[str, float]:
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": yaw}


def _antenna_lean(desired: float, params: ListenParams) -> MotionAction | None:
    """Build a Tier-1 near-side antenna lean for *desired* yaw (degrees).

    Only the antenna on the near side (toward the sound) deflects; the far
    antenna returns to neutral (0°).  Returns ``None`` when ``desired`` is
    effectively zero (front-facing sound → no lean needed).

    ``antennas`` tuple is ``(right, left)``.  Positive yaw = sound on the left,
    so left antenna leans; negative yaw = sound on the right, so right antenna
    leans.
    """
    if abs(desired) < 1e-9:
        return None
    p = params
    lean = min(1.0, abs(desired) / p.max_yaw) * p.antenna_max * p.antenna_gain
    if desired > 0:
        # Sound on the left — left antenna leans toward it.
        right_a, left_a = 0.0, lean
    else:
        # Sound on the right — right antenna leans toward it.
        right_a, left_a = lean, 0.0
    return MotionAction(
        label=f"antenna lean {desired:+.0f}",
        head=None,
        antennas=(right_a, left_a),
        duration=0.3,
        interpolation="minjerk",
        coalesce_key=ANTENNA_KEY,
    )


@dataclass
class ListenProducer:
    """Stateful DoA→look decision. Call :meth:`update` each tick."""

    params: ListenParams
    committed: float = 0.0
    _last_live_t: float | None = None
    _hold_until: float = 0.0

    def _move_to(self, target: float, t: float) -> MotionAction:
        p = self.params
        toward_center = abs(target) < abs(self.committed)
        speed = p.relax_speed if toward_center else p.alert_speed
        dur = max(
            p.min_dur, min(p.max_dur, abs(target - self.committed) / speed if speed else p.max_dur)
        )
        self.committed = target
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

    def update(
        self,
        t: float,
        sense: Sense,
        *,
        snap: bool = False,
        sound_present: bool | None = None,
    ) -> MotionAction | None:
        """Return a look-at (or antenna-lean) action to submit this tick, or ``None``.

        **Tier 2 (head turn)** commits toward the DoA *only* on a deliberate event —
        ``sense.speech_detected`` or a loud ``snap`` — and only when that direction is
        more than ``deadband`` off the current heading. A bare latched ``angle`` (no
        speech, no snap) never turns the head. After a commit, the ``hold`` window
        suppresses re-commits.

        **Tier 1 (antenna lean)** fires on any *live* tick with no head turn committed
        or held: the near-side antenna deflects toward the sound. ``live`` is
        ``sound_present`` when an audio path exists, else (HTTP/remote)
        ``sense.doa_angle is not None`` as a degraded best-effort — never a stale
        latched angle during true silence.

        **Recenter:** once sound has been non-live for ``recenter_after`` seconds and
        the head is off-center, ease back to center once.
        """
        p = self.params
        angle = sense.doa_angle
        # Effective liveness: prefer the live mic floor; fall back to the (latched)
        # angle only when there is no audio path at all (HTTP/remote profile).
        live = sound_present if sound_present is not None else (angle is not None)
        if p.speech_only:
            live = live and sense.speech_detected
        if live:
            self._last_live_t = t

        if t < self._hold_until:
            # Holding at the just-committed direction — ignore everything else.
            return None

        # A turn is a deliberate event: detected speech or a loud snap. Without one,
        # the head never moves (the latched angle alone must not commit a turn).
        triggered = sense.speech_detected or snap

        if angle is not None:
            desired = max(-p.max_yaw, min(p.max_yaw, doa_angle_to_yaw(angle, p.gain)))
            if triggered and abs(desired - self.committed) > p.deadband:
                # Tier 2: off-axis speech/snap — commit the head turn.
                return self._move_to(desired, t)
            if live:
                # Tier 1: live sound, no turn this tick — acknowledge with a near-side
                # lean (only when off-front; front-facing sound yields no lean).
                return _antenna_lean(desired, p)

        # No live sound this tick. After a grace period off-center, ease to center once.
        if (
            not live
            and abs(self.committed) > 1e-9  # off-center (committed is exactly 0 at center)
            and self._last_live_t is not None
            and (t - self._last_live_t) >= p.recenter_after
        ):
            return self._move_to(0.0, t)
        return None
