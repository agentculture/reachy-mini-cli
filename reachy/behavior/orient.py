"""Orienting toward sound in the symbolic runtime — the ported two-tier ladder.

:func:`reachy.behavior.sense.doa_angle_to_yaw` has always mapped a Direction of
Arrival angle onto a yaw target, but its only production consumer was
:class:`reachy.motion.listen.ListenProducer` — the retiring AI-first loop. The
runtime could *sense* ``doa_angle``/``speech_detected`` and *react* discretely
through a rule, but nothing turned a live bearing into a SUSTAINED gaze target.
This module is that missing half, expressed the way every other runtime
capability is expressed: as a library behavior the engine arbitrates like any
other channel owner.

Three pieces, separable on purpose
==================================
* :class:`OrientParams` — the tunables, defaulted to the DONOR's values
  (:class:`reachy.motion.listen.ListenParams`) knob for knob. A drift guard in
  ``tests/test_behavior_orient.py`` pins them together, so "observably
  equivalent motion" cannot rot silently.
* :func:`plan_orient` — the PURE geometry: the donor's graduated ladder
  (:meth:`ListenProducer._react_to_angle`) with the motion-queue plumbing
  removed. Cited, not imported: this package stays a dependency-free leaf that
  never reaches into :mod:`reachy.motion`.
* :class:`OrientToSound` — the stateful contribution function the library entry
  ``orient-to-sound`` mints. It consults an injected *gate* for admission,
  plans a target, and eases onto it with the same minimum-jerk profile the
  donor's ``goto`` planner used, over the donor's own computed duration.

Admission is a SEAM, deliberately
=================================
The gate is a plain injectable callable ``gate(sense, now, params) ->
OrientTier``. It is not folded into :func:`plan_orient` and it is not an
``if sense.speech_detected`` buried in the middle of the behavior, because the
admission decision is the part still being hardened: task **t9** ports the old
flow's *latched-DoA guard* and plugs in here, wrapping or replacing
:class:`CorroboratedGate` with no change to the geometry or the behavior.

Why the default gate is a CONJUNCTION, not ``speech_detected``
---------------------------------------------------------------
Measured on the deployed robot, 120 samples at 0.5 s in a QUIET room with
nobody speaking (``docs/verification/2026-07-20-retire-old-flow-baseline.md``
section 2):

.. code-block:: text

    speech_detected True: 55/120  (45.8 %)
    longest consecutive True run: ~2.5 s
    distinct angles: 35, spanning 0.000-3.124 rad

So ``speech_detected`` FLICKERS — it is not latched on — while the bearing
wanders essentially the full acoustic range. A goal keyed on the bare flag
would swivel the robot at nothing about half the time, pointing somewhere
uncorrelated with anything real. :class:`CorroboratedGate` therefore requires,
cheapest first:

1. **Sound energy** — ``sense.rms >= rms_floor``. This is not a new invention:
   it is the donor's own ``sound_present``, which was exactly
   ``rms > SnapDetector.min_rms`` (0.02) in ``listen``'s audio tap. A quiet room
   has no energy however the daemon's speech flag reads, so this alone converts
   a coin flip into "only while something is actually audible".
2. **A bearing that holds still** — the angle must stay within ``dwell_tol_rad``
   for ``dwell_s`` before any HEAD-moving tier opens. The measured wander (35
   distinct angles across the full range in 60 s) cannot clear this; a person
   speaking from one place can.
3. **Words, for the deliberate turn** — the ENGAGED tier keys on
   ``sense.transcript``, which by construction is an utterance that already
   cleared the layered engagement gate
   (:mod:`reachy.behavior.transcript_sense`). That is the strongest
   corroboration available in the runtime, and it needs neither dwell nor
   loudness.

The honest limits of that stopgap, stated rather than hidden: it is *energy plus
steadiness*, so a loud steady non-speech source (a fan, music) can still open
the SPEECH tier, and a genuinely frozen-but-plausible daemon angle inside a loud
room would pass the dwell test. Closing those is precisely what t9's
latched-DoA guard is for — it can see that the angle has not CHANGED at all,
which is a different question from whether it is steady.

Pure standard library plus in-package value types. No transport, no
``reachy_mini``, no :mod:`reachy.motion` import.
"""

from __future__ import annotations

import enum
import math
from dataclasses import dataclass, fields
from typing import Callable

from reachy import senselog
from reachy.behavior.model import Contribution, neutral_head
from reachy.behavior.sense import Sense, doa_angle_to_yaw

#: The ``[SENSE stage=...]`` stage name every line this module logs carries.
STAGE = "orient"

#: How long the Tier-1 antenna lean takes to reach its deflection, in seconds.
#: The donor's ``_antenna_lean`` built a 0.3 s minjerk ``MotionAction``.
ANTENNA_LEAN_S = 0.3

#: Below this absolute offset a channel counts as "home" and the behavior
#: abstains from it entirely, handing it back to the passive base layer.
HOME_EPS_DEG = 0.05


@dataclass(frozen=True)
class OrientParams:
    """Tunables for the orienting ladder (degrees, seconds, deg/s).

    Every field that has a counterpart in
    :class:`reachy.motion.listen.ListenParams` carries the DONOR's value; the
    last three are this port's own admission gate. Keep them in step with the
    donor while both exist — a test asserts it.
    """

    # -- geometry (donor: ListenParams) ------------------------------------ #
    gain: float = 0.6  # scales the ~+-90 deg acoustic span onto yaw
    max_yaw: float = 35.0  # head yaw clamp
    deadband: float = 16.0  # ignore sound within this of the current heading
    hold: float = 3.0  # after committing, hold this long before reconsidering
    alert_speed: float = 18.0  # deg/s turning toward a new (more off-axis) sound
    relax_speed: float = 18.0  # deg/s easing back toward centre
    min_dur: float = 1.5  # duration floor, so even small turns stay deliberate
    max_dur: float = 4.0
    antenna_gain: float = 1.0
    antenna_max: float = 18.0
    body_yaw_max: float = 45.0
    body_speed: float = 12.0  # deg/s (slow — a body turn is deliberate)
    head_only_band: float = 30.0  # beyond this the body escalates
    speech_orient_gain: float = 0.6  # fraction of the clamped target the speech tier uses
    speech_orient_max: float = 20.0  # hard cap on the speech-tier head-only nudge
    engaged_min_dur: float = 1.5  # duration floor for the deliberate engaged turn
    recenter_after: float = 4.0  # silence grace before the heading drifts home
    # -- admission gate (this port's own; see the module docstring) --------- #
    rms_floor: float = 0.02  # the donor's ``sound_present`` floor (SnapDetector.min_rms)
    dwell_s: float = 0.6  # the bearing must hold this long before the head moves
    dwell_tol_rad: float = 0.12  # how far the bearing may move and still count as held


#: The library entry's parameter names, in declaration order — every
#: :class:`OrientParams` field is exposed, so a rule or a standing goal retunes
#: the whole ladder with no code change.
PARAM_NAMES = tuple(f.name for f in fields(OrientParams))


class OrientTier(enum.Enum):
    """How strongly the runtime should react to what it currently hears.

    The gate's whole vocabulary, ordered by :attr:`rank` — a graduated ladder,
    not a boolean, because the donor's reaction was graded by perception level
    and flattening it would either freeze the head or swing it at noise:

    * ``NONE`` — no credible sound; the behavior abstains and the base layer keeps
      the channels.
    * ``NOISE`` — live sound, no bearing worth turning to: the donor's Tier-1
      near-side antenna lean, head untouched.
    * ``SPEECH`` — speech from a bearing that holds still: a bounded HEAD-ONLY
      orienting nudge, never a body rotation.
    * ``ENGAGED`` — an utterance addressed to the robot: the deliberate head
      turn, escalating to the body beyond ``head_only_band``.
    """

    NONE = 0
    NOISE = 1
    SPEECH = 2
    ENGAGED = 3

    @property
    def rank(self) -> int:
        """How strong this tier is; higher reacts more (``NONE`` is 0)."""
        return int(self.value)


@dataclass(frozen=True)
class OrientTarget:
    """One planned commitment: where to point, and how long the move takes.

    A channel left ``None`` is one this commitment does not drive (the NOISE
    tier drives only ``antennas``, the SPEECH tier never drives ``body_yaw``).
    ``duration`` is the donor's own computed move time, so the ease onto the
    target takes exactly as long as the retiring loop's ``goto`` did.
    """

    head_yaw: float | None
    body_yaw: float | None
    antennas: tuple[float, float] | None
    duration: float


#: The admission seam. ``gate(sense, now, params) -> OrientTier``: given this
#: tick's perception snapshot, the behavior-local clock and the effective
#: tuning, decide how strongly to react. Duck-typed exactly like
#: :class:`reachy.behavior.sense.DoaPoller`'s ``read`` callable, so t9's
#: latched-DoA guard is wired in as a plain callable with no import cycle.
OrientGate = Callable[[Sense, float, OrientParams], OrientTier]


# --------------------------------------------------------------------------- #
# Pure geometry — the donor's ladder, cited                                    #
# --------------------------------------------------------------------------- #


def minjerk_progress(tau: float) -> float:
    """The minimum-jerk position profile ``s(t) = 10t^3 - 15t^4 + 6t^5`` on ``[0, 1]``.

    The SAME profile the SDK's ``goto`` planner interpolated the donor's turns
    with, and the same one :func:`reachy.behavior.goto_lane.minjerk_progress`
    documents. Re-stated here (not imported) so this module stays a leaf with
    no dependency on the goto lane; clamped and monotonic, so an approach never
    overshoots or reverses.
    """
    if tau <= 0.0:
        return 0.0
    if tau >= 1.0:
        return 1.0
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


def _clamp(value: float, limit: float) -> float:
    return max(-limit, min(limit, value))


def antenna_pair(desired: float, params: OrientParams) -> tuple[float, float]:
    """The ``(right, left)`` antenna pair leaning toward a head *desired* yaw.

    Cited from the donor's ``_antenna_tuple``: only the NEAR-side antenna
    deflects, and the right joint's sign is MIRRORED from the left, so a
    positive value on the right would tilt it toward the centre instead of
    toward the sound. A desired of zero returns both to neutral.
    """
    if abs(desired) < 1e-9:
        return (0.0, 0.0)
    lean = min(1.0, abs(desired) / params.max_yaw) * params.antenna_max * params.antenna_gain
    if desired > 0:  # sound on the left
        return (0.0, lean)
    return (-lean, 0.0)


def _duration(raw: float, params: OrientParams, floor: float | None = None) -> float:
    """Clamp a computed move duration to the donor's never-degenerate range."""
    lo = params.min_dur if floor is None else max(params.min_dur, floor)
    if not math.isfinite(raw) or raw <= 0.0:
        return lo
    return max(lo, min(params.max_dur, raw))


def plan_orient(
    tier: OrientTier,
    angle: float,
    params: OrientParams,
    *,
    head_yaw: float,
    body_yaw: float,
) -> OrientTarget | None:
    """Plan this tier's commitment toward *angle*, or ``None`` for "stay put".

    A direct port of :meth:`reachy.motion.listen.ListenProducer._react_to_angle`
    with the ``MotionAction`` plumbing dropped. *head_yaw* / *body_yaw* are the
    behavior's CURRENT committed targets, which the deadband and the duration
    arithmetic are measured against — the same role ``ListenProducer.committed``
    and ``.body`` played.

    * ``ENGAGED`` — the deliberate turn. Beyond ``head_only_band`` the body
      rotates toward the source (clamped to ``body_yaw_max``) and the head takes
      the RESIDUAL, so head and body together face the sound with the head near
      centre. Its duration floor is raised to ``engaged_min_dur``.
    * ``SPEECH`` — a bounded head-only nudge: a fraction of the clamped target,
      hard-capped at ``speech_orient_max``, never escalating to the body.
    * ``NOISE`` — the Tier-1 antenna lean alone.
    * ``NONE`` — nothing.
    """
    raw_desired = doa_angle_to_yaw(angle, params.gain)  # unclamped: drives escalation
    desired = _clamp(raw_desired, params.max_yaw)
    off_heading = abs(desired - head_yaw) > params.deadband

    if tier is OrientTier.ENGAGED and off_heading:
        floor = params.engaged_min_dur
        if abs(raw_desired) > params.head_only_band:
            return _escalate(raw_desired, params, body_yaw=body_yaw, floor=floor)
        return _head_only(desired, params, head_yaw=head_yaw, floor=floor)

    if tier is OrientTier.SPEECH and off_heading:
        mag = min(abs(desired) * params.speech_orient_gain, params.speech_orient_max)
        target = math.copysign(mag, desired) if desired else 0.0
        return _head_only(target, params, head_yaw=head_yaw)

    if tier in (OrientTier.NOISE, OrientTier.SPEECH, OrientTier.ENGAGED):
        antennas = antenna_pair(desired, params)
        if antennas == (0.0, 0.0):
            return None  # a front-facing sound needs no lean (the donor's guard)
        return OrientTarget(None, None, antennas, ANTENNA_LEAN_S)
    return None


def _head_only(
    target: float, params: OrientParams, *, head_yaw: float, floor: float | None = None
) -> OrientTarget:
    """A head turn to *target*, with the near-side antenna folded in."""
    toward_centre = abs(target) < abs(head_yaw)
    speed = params.relax_speed if toward_centre else params.alert_speed
    raw = abs(target - head_yaw) / speed if speed else params.max_dur
    return OrientTarget(
        head_yaw=target,
        body_yaw=None,
        antennas=antenna_pair(target, params),
        duration=_duration(raw, params, floor),
    )


def _escalate(
    raw_desired: float, params: OrientParams, *, body_yaw: float, floor: float | None
) -> OrientTarget:
    """A combined head+body commitment bringing the robot to face the source."""
    sign = 1.0 if raw_desired >= 0 else -1.0
    new_body = sign * min(abs(raw_desired), params.body_yaw_max)
    new_head = _clamp(raw_desired - new_body, params.max_yaw)
    raw = abs(new_body - body_yaw) / params.body_speed if params.body_speed else params.max_dur
    return OrientTarget(
        head_yaw=new_head,
        body_yaw=new_body,
        antennas=antenna_pair(new_head, params),
        duration=_duration(raw, params, floor),
    )


# --------------------------------------------------------------------------- #
# The default admission gate                                                  #
# --------------------------------------------------------------------------- #


class CorroboratedGate:
    """The interim admission rule: energy, then a steady bearing, then words.

    See the module docstring for the measured evidence behind each conjunct and
    for what this deliberately does NOT close (which is t9's job). Stateful
    only in the dwell tracker — a running reference bearing plus the time it was
    adopted — so it is O(1) per tick and deterministic under an injected clock.

    Never raises: a hostile or partially-shaped snapshot resolves to
    :data:`OrientTier.NONE`, mirroring
    :class:`reachy.behavior.sense.DoaPoller`'s "any failure means no reading"
    contract. A sense that cannot be read must leave the robot still.
    """

    def __init__(self) -> None:
        self._ref_angle: float | None = None
        self._ref_since: float = 0.0

    def __call__(self, sense: Sense, now: float, params: OrientParams) -> OrientTier:
        try:
            return self._decide(sense, now, params)
        except Exception:  # noqa: BLE001 - an unreadable sense must never steer or raise
            self._ref_angle = None
            return OrientTier.NONE

    def _decide(self, sense: Sense, now: float, params: OrientParams) -> OrientTier:
        angle = sense.doa_angle
        if angle is None or not math.isfinite(float(angle)):
            self._ref_angle = None  # no bearing: the dwell clock restarts from scratch
            return OrientTier.NONE

        # An ADDRESSED utterance already cleared the layered engagement gate, so it
        # is corroboration in its own right — no dwell, no loudness threshold.
        if sense.transcript is not None:
            self._adopt(float(angle), now)
            return OrientTier.ENGAGED

        rms = sense.rms
        if rms is None or not (float(rms) >= params.rms_floor):
            self._ref_angle = None
            return OrientTier.NONE

        held_for = self._adopt(float(angle), now, tol=params.dwell_tol_rad)
        if sense.speech_detected and held_for >= params.dwell_s:
            return OrientTier.SPEECH
        return OrientTier.NOISE

    def _adopt(self, angle: float, now: float, *, tol: float = 0.0) -> float:
        """Track the dwell reference; return how long the bearing has held."""
        if self._ref_angle is None or abs(angle - self._ref_angle) > tol:
            self._ref_angle = angle
            self._ref_since = now
            return 0.0
        return max(0.0, now - self._ref_since)


# --------------------------------------------------------------------------- #
# The behavior — a sustained gaze goal                                        #
# --------------------------------------------------------------------------- #


class _Axis:
    """One eased scalar channel: hold a value, ease toward a new target.

    Each re-target restarts a minimum-jerk interpolation FROM the current value
    (not from neutral), so a bearing that moves mid-turn never snaps — the
    continuity the goto lane gets from an injected start-pose provider, held
    internally here because a library behavior has no seam to read the live pose
    from.
    """

    __slots__ = ("value", "target", "_from", "_t0", "_dur")

    def __init__(self) -> None:
        self.value = 0.0
        self.target = 0.0
        self._from = 0.0
        self._t0 = 0.0
        self._dur = 0.0

    def retarget(self, target: float, duration: float, now: float) -> None:
        if target == self.target and self._dur > 0.0:
            return
        self._from = self.value
        self.target = target
        self._t0 = now
        self._dur = max(1e-6, duration)

    def advance(self, now: float) -> float:
        if self._dur <= 0.0:
            self.value = self.target
            return self.value
        s = minjerk_progress((now - self._t0) / self._dur)
        self.value = self._from + (self.target - self._from) * s
        return self.value

    @property
    def home(self) -> bool:
        return abs(self.value) < HOME_EPS_DEG and abs(self.target) < HOME_EPS_DEG


class OrientToSound:
    """The ``orient-to-sound`` contribution function: a sustained gaze goal.

    Callable as ``fn(t_local, params, sense) -> Contribution``, so it plugs
    straight into :class:`reachy.behavior.model.Behavior` like any other
    sensor-driven library entry (``pet-reaction`` is the sibling). Every tick it

    1. resolves the effective :class:`OrientParams` from the behavior's own
       ``params`` dict (so a rule or standing goal retunes it live),
    2. asks the injected :data:`OrientGate` which tier this perception earns,
    3. plans a target with :func:`plan_orient` when the ``hold`` window is open,
    4. advances each channel's minjerk ease and returns the composed offsets.

    Channel discipline — why this ABSTAINS rather than freezing
    -----------------------------------------------------------
    The behavior claims all three channels, but a channel it is not currently
    driving is returned as ``None``. :func:`reachy.behavior.arbitration.arbitrate`
    is abstention-aware, so an unused channel falls straight through to the next
    claimant — in practice the passive ``feel-alive`` base layer keeps breathing
    with it. That is why a quiet room leaves the robot alive rather than locked
    at neutral, and why the antenna-only NOISE tier does not freeze the head.

    Preemption is therefore the ordinary contention story: this is a
    ``stoppable`` behavior, so a pat reaction admitted after it (same class,
    later) takes every shared channel, and a ``stopping``/``unstoppable``
    behavior — how a sleep presence would be admitted — takes them by priority
    or evicts outright at admit time. Nothing here needs to know about either.

    Never raises. A hostile snapshot, a non-finite or rewinding clock, or a
    malformed params dict degrades to "no reading" for the tick, exactly like
    :class:`reachy.behavior.sense.DoaPoller`.
    """

    def __init__(self, params: OrientParams | None = None, *, gate: OrientGate | None = None):
        self._params = params if params is not None else OrientParams()
        self._explicit_params = params is not None
        self._gate: OrientGate = gate if gate is not None else CorroboratedGate()
        self._head = _Axis()
        self._body = _Axis()
        self._ant_r = _Axis()
        self._ant_l = _Axis()
        self._src: dict | None = None
        self._hold_until = 0.0
        self._last_live_t: float | None = None
        self._last_t = 0.0
        self._last_tier = OrientTier.NONE

    # -- introspection (tests, and any future status view) ------------------ #

    @property
    def head_yaw(self) -> float:
        """The head yaw offset currently being commanded, in degrees."""
        return self._head.value

    @property
    def target_head_yaw(self) -> float:
        """The head yaw offset currently being eased toward, in degrees."""
        return self._head.target

    @property
    def body_yaw(self) -> float:
        """The body yaw offset currently being commanded, in degrees."""
        return self._body.value

    # -- the contribution function ------------------------------------------ #

    def __call__(self, t_local: float, params: dict, sense) -> Contribution:
        now = self._clock(t_local)
        effective = self._resolve(params)
        tier = self._tier(sense, now, effective)
        self._observe(tier, sense)
        if tier is not OrientTier.NONE:
            self._last_live_t = now
        self._commit(tier, sense, now, effective)
        self._release(tier, now, effective)
        return self._compose()

    def _observe(self, tier: OrientTier, sense) -> None:
        """Log every tier TRANSITION — and only transitions.

        Bounded by construction: a 50 Hz behavior that logged per tick would
        bury the journal, but the tier changes only when perception genuinely
        does, so this is the trace an operator greps to answer "did the gate
        open, and on what?" — the question the live check for this port, and
        task t9's latched-DoA guard, both turn on. A fall back to ``NONE`` is a
        DROP naming the tier that ended, so a gate closing is never a silent
        no-op (:mod:`reachy.senselog`'s discipline).
        """
        if tier is self._last_tier:
            return
        previous, self._last_tier = self._last_tier, tier
        angle = getattr(sense, "doa_angle", None)
        bearing = "none" if angle is None else f"{float(angle):.3f}rad"
        if tier is OrientTier.NONE:
            senselog.drop(STAGE, "doa", "tier", f"closed from={previous.name} bearing={bearing}")
        else:
            senselog.stage(STAGE, "doa", "tier", f"{previous.name}->{tier.name} bearing={bearing}")

    def _clock(self, t_local: float) -> float:
        """A monotonic, finite behavior-local clock (a bad one simply does not advance)."""
        try:
            t = float(t_local)
            if not math.isfinite(t):
                return self._last_t
            t = max(self._last_t, max(0.0, t))
        except (TypeError, ValueError):
            return self._last_t
        self._last_t = t
        return t

    def _resolve(self, params: dict) -> OrientParams:
        """The effective tuning: the behavior's params dict over the defaults.

        Rebuilt only when the dict's contents actually change, so the steady
        state costs one dict comparison per tick. An explicitly-constructed
        ``OrientParams`` (the injection path) wins outright — a caller that
        passed tuning in did not ask for a params dict to override it.
        """
        if self._explicit_params or not params:
            return self._params
        if params == self._src:
            return self._params
        values = {}
        for name in PARAM_NAMES:
            raw = params.get(name)
            if raw is None:
                continue
            try:
                value = float(raw)
            except (TypeError, ValueError):
                continue
            if math.isfinite(value):
                values[name] = value
        self._src = dict(params)
        self._params = OrientParams(**values)
        return self._params

    def _tier(self, sense, now: float, params: OrientParams) -> OrientTier:
        try:
            tier = self._gate(sense, now, params)
        except Exception:  # noqa: BLE001 - a raising gate means "no reading", never a crash
            return OrientTier.NONE
        return tier if isinstance(tier, OrientTier) else OrientTier.NONE

    def _commit(self, tier: OrientTier, sense, now: float, params: OrientParams) -> None:
        """Plan and adopt a new target, unless the hold window is still closed."""
        if tier is OrientTier.NONE or now < self._hold_until:
            return
        angle = getattr(sense, "doa_angle", None)
        if angle is None:
            return
        target = plan_orient(
            tier, float(angle), params, head_yaw=self._head.target, body_yaw=self._body.target
        )
        if target is None:
            return
        if target.head_yaw is not None:
            self._head.retarget(target.head_yaw, target.duration, now)
        if target.body_yaw is not None:
            self._body.retarget(target.body_yaw, target.duration, now)
        if target.antennas is not None:
            self._ant_r.retarget(target.antennas[0], target.duration, now)
            self._ant_l.retarget(target.antennas[1], target.duration, now)
        # Only a committing HEAD move opens a hold window — an antenna-only lean is
        # cheap and leaves the ladder free to react again on the next tick. That is
        # the donor's rule exactly: its ``_hold_until`` was set by ``_move_to`` /
        # ``_escalate_to_body`` alone, while an OPEN hold suppressed re-commits and
        # leans alike (the early return above).
        if target.head_yaw is not None:
            self._hold_until = now + target.duration + params.hold

    def _release(self, tier: OrientTier, now: float, params: OrientParams) -> None:
        """Ease the committed heading home after the donor's silence grace."""
        if tier is not OrientTier.NONE:
            return
        if self._last_live_t is None or (now - self._last_live_t) < params.recenter_after:
            return
        for axis, speed in (
            (self._head, params.relax_speed),
            (self._body, params.body_speed),
            (self._ant_r, params.relax_speed),
            (self._ant_l, params.relax_speed),
        ):
            if axis.target != 0.0:
                axis.retarget(
                    0.0, _duration(abs(axis.value) / speed if speed else 0.0, params), now
                )
        self._hold_until = 0.0

    def _compose(self) -> Contribution:
        now = self._last_t
        head_yaw = self._head.advance(now)
        body_yaw = self._body.advance(now)
        right = self._ant_r.advance(now)
        left = self._ant_l.advance(now)
        head = None
        if not self._head.home:
            head = neutral_head()
            head["yaw"] = head_yaw
        antennas = None
        if not (self._ant_r.home and self._ant_l.home):
            antennas = (right, left)
        return Contribution(
            head=head,
            antennas=antennas,
            body_yaw=None if self._body.home else body_yaw,
        )


def make_orient_to_sound() -> Callable[[float, dict, Sense], Contribution]:
    """Return one fresh, stateful ``orient-to-sound`` contribution function.

    The zero-argument factory shape :class:`reachy.behavior.library.LibraryEntry`
    calls per behavior instance (``make_fn``), so every admission gets its own
    dwell tracker and ease state. Construct :class:`OrientToSound` directly to
    inject tuning or a different gate.
    """
    return OrientToSound()


__all__ = [
    "ANTENNA_LEAN_S",
    "CorroboratedGate",
    "HOME_EPS_DEG",
    "OrientGate",
    "OrientParams",
    "OrientTarget",
    "OrientTier",
    "OrientToSound",
    "PARAM_NAMES",
    "STAGE",
    "antenna_pair",
    "make_orient_to_sound",
    "minjerk_progress",
    "plan_orient",
]
