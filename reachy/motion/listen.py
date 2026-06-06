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

**Tier-2 head→body escalation:** when a committing speech/snap event points
beyond ``head_only_band`` degrees, the head alone cannot reach the source. In
that case a single combined action turns the body toward the source (clamped to
±``body_yaw_max``) *and* re-centres the head to the residual angle
(``desired - body_yaw``, clamped to ±``max_yaw``) so the robot faces the sound
with head close to centre. The near-side antenna is folded into the same action.
"""

from __future__ import annotations

from dataclasses import dataclass, field

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
    # Tier-2 head→body escalation
    body_yaw_max: float = 45.0  # maximum body yaw rotation in degrees
    body_speed: float = 12.0  # deg/s (slow — body turn is deliberate, not snappy)
    head_only_band: float = 30.0  # |desired| <= this → head-only; beyond → body escalation


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


def _antenna_tuple(desired: float, params: ListenParams) -> tuple[float, float]:
    """Return the ``(right, left)`` antenna pair for a given head *desired* yaw.

    Used to fold the antenna pose into a committing head-turn action.  When
    *desired* is effectively zero (recentering) both sides return to neutral.
    """
    if abs(desired) < 1e-9:
        return (0.0, 0.0)
    p = params
    lean = min(1.0, abs(desired) / p.max_yaw) * p.antenna_max * p.antenna_gain
    if desired > 0:
        return (0.0, lean)
    return (lean, 0.0)


@dataclass
class ListenProducer:
    """Stateful DoA→look decision. Call :meth:`update` each tick."""

    params: ListenParams
    committed: float = 0.0  # current head yaw
    body: float = field(default=0.0)  # current body yaw
    _last_live_t: float | None = None
    _hold_until: float = 0.0

    def _move_to(self, target: float, t: float, *, body_yaw: float | None = None) -> MotionAction:
        """Commit a head turn to *target*; optionally drive ``body_yaw`` in the same move.

        The near-side antenna pose is folded into the same action so the head
        and antenna move together.  On recentering to 0 the antennas return to
        neutral ``(0.0, 0.0)``.  ``body_yaw`` is left ``None`` (body not driven)
        except on a recenter, where it is ``0.0`` to bring the body home too.
        """
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
        antennas = _antenna_tuple(target, p)
        return MotionAction(
            label=f"{kind} {target:+.0f}",
            head=_head(target),
            antennas=antennas,
            body_yaw=body_yaw,
            duration=dur,
            interpolation="minjerk",
            coalesce_key=LOOK_KEY,
        )

    def _escalate_to_body(self, desired: float, t: float) -> MotionAction:
        """Emit a combined head+body action that brings the robot to face *desired*.

        The body rotates toward the source (clamped to ±``body_yaw_max``); the
        head takes the residual ``desired - new_body_yaw`` (clamped to
        ±``max_yaw``) so head + body together point at the source and the head
        sits closer to centre.  The near-side antenna (relative to the final head
        yaw) is folded into the same action.
        """
        p = self.params
        sign = 1.0 if desired >= 0 else -1.0
        new_body = sign * min(abs(desired), p.body_yaw_max)
        residual = desired - new_body
        new_head = max(-p.max_yaw, min(p.max_yaw, residual))

        body_delta = abs(new_body - self.body)
        dur = max(
            p.min_dur,
            min(p.max_dur, body_delta / p.body_speed if p.body_speed else p.max_dur),
        )

        self.committed = new_head
        self.body = new_body
        self._hold_until = t + dur + p.hold

        antennas = _antenna_tuple(new_head, p)
        return MotionAction(
            label=f"escalate body {new_body:+.0f} head {new_head:+.0f}",
            head=_head(new_head),
            antennas=antennas,
            body_yaw=new_body,
            duration=dur,
            interpolation="minjerk",
            coalesce_key=LOOK_KEY,
        )

    def _react_to_angle(
        self, angle: float, t: float, *, triggered: bool, live: bool
    ) -> MotionAction | None:
        """A Tier-2 turn (on a speech/snap trigger) or a Tier-1 antenna lean, or ``None``."""
        p = self.params
        raw_desired = doa_angle_to_yaw(angle, p.gain)  # unclamped — drives escalation
        desired = max(-p.max_yaw, min(p.max_yaw, raw_desired))  # clamped head-only target
        if triggered and abs(desired - self.committed) > p.deadband:
            if abs(raw_desired) > p.head_only_band:
                return self._escalate_to_body(raw_desired, t)
            return self._move_to(desired, t)
        if live:
            return _antenna_lean(desired, p)
        return None

    def _recenter(self, t: float, live: bool) -> MotionAction | None:
        """Ease head AND body back to center once, after a grace period of no live sound."""
        p = self.params
        if (
            not live
            and abs(self.committed) > 1e-9  # off-center (committed is exactly 0 at center)
            and self._last_live_t is not None
            and (t - self._last_live_t) >= p.recenter_after
        ):
            self.body = 0.0
            return self._move_to(0.0, t, body_yaw=0.0)
        return None

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

        **Tier 2 escalation (head+body):** when the raw *desired* direction exceeds
        ``head_only_band``, the body rotates toward the source while the head
        re-centres on the residual — head and body together face the source.

        **Tier 1 (antenna lean)** fires on any *live* tick with no head turn committed
        or held: the near-side antenna deflects toward the sound. ``live`` is
        ``sound_present`` when an audio path exists, else (HTTP/remote)
        ``sense.doa_angle is not None`` as a degraded best-effort — never a stale
        latched angle during true silence.

        **Recenter:** once sound has been non-live for ``recenter_after`` seconds and
        the head is off-center, ease back to center once (head AND body both return).
        """
        angle = sense.doa_angle
        # Effective liveness: prefer the live mic floor; fall back to the (latched)
        # angle only when there is no audio path at all (HTTP/remote profile).
        live = sound_present if sound_present is not None else (angle is not None)
        if self.params.speech_only:
            live = live and sense.speech_detected
        if live:
            self._last_live_t = t

        if t < self._hold_until:
            # Holding at the just-committed direction — ignore everything else.
            return None

        # A turn is a deliberate event: detected speech or a loud snap. A bare latched
        # angle (no speech, no snap) must never commit a turn.
        triggered = sense.speech_detected or snap
        if angle is not None:
            reaction = self._react_to_angle(angle, t, triggered=triggered, live=live)
            if reaction is not None:
                return reaction
        return self._recenter(t, live)
