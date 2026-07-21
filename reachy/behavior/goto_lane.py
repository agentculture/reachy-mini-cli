"""The goto lane — serial minjerk gotos as seam-driven stoppable contributions.

This module folds the one-shot ``goto`` motion path into the 50 Hz behavior
engine. A *goto* — "ease the head/antennas/body to this target over N seconds" —
is the bread-and-butter move the :mod:`reachy.motion` serial ``MotionQueue`` +
``move_goto`` executor already provides, but *outside* the engine: there, the
daemon interpolates a submitted ``goto`` and the engine's arbitration cannot see
it, so a goto and a streamed behavior can fight for the same channel. This lane
brings gotos *inside* the engine so they arbitrate like everything else.

How a goto becomes a behavior
-----------------------------
The engine streams *immediate* ``set_target`` poses at 50 Hz with **no** daemon-
side interpolation (see :mod:`reachy.behavior.library`), so a goto cannot delegate
its curve to ``move_goto``; it must produce the interpolated pose itself, per tick.
:func:`build_goto_behavior` therefore expresses a goto as a
:class:`~reachy.behavior.model.Behavior`:

* it is one-shot, ``Lifetime(looping=False, duration=duration_s)`` — **time-
  bounded** to its duration, so the engine drops it exactly when it lands;
* it is :attr:`~reachy.behavior.model.StopClass.STOPPABLE` by default — a
  safety/``stopping`` (or higher-priority ``unstoppable``) behavior can preempt it;
* it claims **exactly** the channels the goto names (head / antennas / body_yaw);
* its contribution at behavior-local time ``t`` is the minimum-jerk interpolation
  from a *start* pose to the *target* pose, ``s(τ) = 10τ³ − 15τ⁴ + 6τ⁵`` with
  ``τ = t / duration`` — the **same smooth profile the SDK's ``goto`` planner
  uses**. The math is *cited*, not imported: :func:`minjerk_progress` reproduces
  the SDK planner's own profile (it was originally cited here from the retired
  ``reachy.motion.listen_pat.minjerk_progress``) so this leaf stays a pure,
  numpy-free, stdlib-only module that imports no ``reachy_mini`` and no motion
  machinery.

Start pose — the choice, and its limitation
--------------------------------------------
A minjerk needs a *start* pose to interpolate from. The engine's per-tick seam
(:class:`~reachy.behavior.engine.TickContext`) exposes ownership and perception
but **not** the last composed pose, so a driver cannot read "where the head is"
from the seam. Two honest options: interpolate from **neutral** (like every other
:mod:`reachy.behavior.library` behavior — they are all neutral-relative offsets
the engine composes directly as the pose), or accept an **injected start-pose
provider** a caller wires to a source that *does* know the current pose.

This lane's choice: **neutral-relative by default** (``GotoLane()`` with no
provider), with an optional ``start_pose_provider`` for continuity. The
limitation, documented plainly: with no provider a goto begins its minjerk from
neutral (all-zero offsets), so a goto submitted while a channel is already
*offset* shows a C0 discontinuity at ``t=0`` — it snaps to neutral, then eases to
target. That matches the library's uniform neutral-relative model (``thoughtful``
/ ``body-turn-hold`` ease in from 0 the same way); a caller wanting continuity
injects a provider returning the live pose as a :class:`Contribution`.

Preemption — the pinned semantics
---------------------------------
:class:`GotoLane` is a per-tick seam **driver** (compose it into a
:class:`~reachy.behavior.rule_engine.TickBus` alongside the rules driver). It
holds a FIFO of pending gotos and runs **one in flight at a time** (serial):

* when idle and the queue is non-empty it :meth:`admit <GotoLane.on_tick>`s the
  next goto and tracks it until its lifetime expires;
* a goto that runs its full duration is **done** (``goto.done``);
* a goto that **loses ownership** of any channel it claims *before* its end — a
  ``stopping`` add evicted it, or a higher-priority ``unstoppable`` out-prioritised
  it per tick — is **cancelled**, never resumed. On cancellation the lane
  additionally ``ctx.evict``s the goto, so even a *short* blocker that expires
  mid-goto can never let the (otherwise still-live) behavior regain the channel
  and resume half-way. This force-evict is the crux of the no-resume guarantee.

Because the seam only exposes per-channel *owner ids* (not classes), the lane
detects preemption purely by ownership: a goto beats the ``passive`` base layer
(so it wins an idle channel and runs) but loses to a ``stopping`` /
``unstoppable`` incumbent (so it is cancelled). A consequence, documented for
honesty: while a blocking behavior *persistently* holds a goto's channel, gotos
queued behind the cancelled one are admitted and cancelled in FIFO order (one per
turn) rather than stalling forever — cancellation is the honest, observable
outcome, surfaced as ``goto.cancelled`` events.

Events
------
Every lifecycle transition is published through ``ctx.emit`` for the export feed
and any other consumer: ``goto.admitted`` (a goto went in flight), ``goto.done``
(it completed), ``goto.cancelled`` (it was preempted — with ``reason`` /
``channel`` / ``owner``). Shapes are the ``EVENT_*`` constants below.

Adapter
-------
:class:`GotoLaneAdapter` is a thin MotionQueue-shaped facade so existing
``reachy.motion.queue.MotionQueue`` callers submit **unchanged**: it accepts a
``MotionAction`` (duck-typed — no import of the motion package) or a
:class:`GotoSpec`, and mirrors the queue surface — ``submit`` (returns the goto
id), ``pending`` (oldest-first), ``busy_until`` (the serial horizon), ``__len__``.

Pure standard library (``logging`` + ``math``-free arithmetic) plus in-package
value types; nothing here touches a network, an LLM, ``reachy_mini``, or the
motion executor.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass
from typing import Callable

from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head

logger = logging.getLogger(__name__)

#: Structured event ``type`` values published through ``ctx.emit`` so a downstream
#: consumer (an export feed, a panel) can follow every goto's lifecycle.
EVENT_ADMITTED = "goto.admitted"
EVENT_DONE = "goto.done"
EVENT_CANCELLED = "goto.cancelled"

#: ``reason`` token on a ``goto.cancelled`` event: another behavior now owns the
#: claimed channel (``preempted``) versus the channel is unclaimed / the goto is
#: simply gone (``evicted``).
REASON_PREEMPTED = "preempted"
REASON_EVICTED = "evicted"

DEFAULT_INTERPOLATION = "minjerk"

# The six head axes, in the order the engine's neutral head uses.
_HEAD_KEYS = ("x", "y", "z", "roll", "pitch", "yaw")


# --------------------------------------------------------------------------- #
# Minjerk interpolation (cited, not imported)                                 #
# --------------------------------------------------------------------------- #


def minjerk_progress(tau: float) -> float:
    """The minimum-jerk position profile ``s(τ) = 10τ³ − 15τ⁴ + 6τ⁵`` on ``[0, 1]``.

    The same smooth profile the SDK's ``goto`` planner interpolates with, written
    out here (not imported) so this module stays a stdlib-only leaf with no
    motion/numpy/SDK dependency. Originally cited from the retired
    ``reachy.motion.listen_pat.minjerk_progress``, which carried the identical
    polynomial. Clamped: ``τ ≤ 0 → 0``, ``τ ≥ 1 → 1``. Monotonic non-decreasing on
    ``[0, 1]``, so a goto's approach to its target never overshoots or reverses.
    """
    if tau <= 0.0:
        return 0.0
    if tau >= 1.0:
        return 1.0
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


def _linear_progress(tau: float) -> float:
    return max(0.0, min(1.0, tau))


def _ease_progress(tau: float) -> float:
    x = max(0.0, min(1.0, tau))
    return x * x * (3.0 - 2.0 * x)  # smoothstep — the library's own ease curve


#: Interpolation-mode → progress curve. Every curve is monotonic on ``[0, 1]`` and
#: pins the endpoints, so any mode preserves the start/target guarantee. An
#: unknown mode falls back to the smooth minjerk default (never raises).
_PROGRESS: dict[str, Callable[[float], float]] = {
    "minjerk": minjerk_progress,
    "linear": _linear_progress,
    "ease": _ease_progress,
    "cartoon": minjerk_progress,
}


def _progress(mode: str, tau: float) -> float:
    return _PROGRESS.get(mode, minjerk_progress)(tau)


def _lerp(a: float, b: float, s: float) -> float:
    return a + (b - a) * s


# --------------------------------------------------------------------------- #
# GotoSpec — one pending goto request (friendly units)                        #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class GotoSpec:
    """One goto: per-channel targets in the CLI's friendly units + duration + curve.

    Field-for-field shaped like :class:`reachy.motion.queue.MotionAction` (``label``
    / ``head`` / ``antennas`` / ``body_yaw`` / ``duration`` / ``interpolation``) so a
    MotionQueue caller maps 1:1 through :class:`GotoLaneAdapter`. ``head`` is a
    partial or full six-axis offset dict (mm / degrees), ``antennas`` a
    ``(right, left)`` degree pair, ``body_yaw`` a scalar in degrees; a channel left
    ``None`` is not driven (the goto does not claim it).
    """

    label: str = "goto"
    head: dict[str, float] | None = None
    antennas: tuple[float, float] | None = None
    body_yaw: float | None = None
    duration: float = 1.0
    interpolation: str = DEFAULT_INTERPOLATION

    def channels(self) -> frozenset[str]:
        """The channels this goto drives (exactly the non-``None`` targets)."""
        claimed = set()
        if self.head is not None:
            claimed.add("head")
        if self.antennas is not None:
            claimed.add("antennas")
        if self.body_yaw is not None:
            claimed.add("body_yaw")
        return frozenset(claimed)


# --------------------------------------------------------------------------- #
# build_goto_behavior — a goto as a one-shot STOPPABLE Behavior                #
# --------------------------------------------------------------------------- #


def _goto_fn(spec: GotoSpec, start: Contribution) -> Callable[[float, dict, object], Contribution]:
    """The contribution closure: minjerk from ``start`` to ``spec``'s targets."""
    dur = spec.duration
    interp = spec.interpolation
    s_head = start.head
    s_ant = start.antennas
    s_body = start.body_yaw

    def fn(t_local: float, _params: dict, _sense: object) -> Contribution:
        s = _progress(interp, t_local / dur if dur > 0 else 1.0)
        return Contribution(
            head=_head_at(spec.head, s_head, s),
            antennas=_antennas_at(spec.antennas, s_ant, s),
            body_yaw=_body_yaw_at(spec.body_yaw, s_body, s),
        )

    return fn


def _head_at(target: dict | None, start: dict | None, s: float) -> dict | None:
    if target is None:
        return None
    base = start if start is not None else neutral_head()
    return {k: _lerp(base.get(k, 0.0), target.get(k, 0.0), s) for k in _HEAD_KEYS}


def _antennas_at(target, start, s: float):
    if target is None:
        return None
    base = start if start is not None else (0.0, 0.0)
    return (_lerp(base[0], target[0], s), _lerp(base[1], target[1], s))


def _body_yaw_at(target: float | None, start: float | None, s: float) -> float | None:
    if target is None:
        return None
    return _lerp(start if start is not None else 0.0, target, s)


def build_goto_behavior(
    spec: GotoSpec,
    behavior_id: str,
    *,
    start: Contribution | None = None,
    stop_class: StopClass = StopClass.STOPPABLE,
) -> Behavior:
    """Build the one-shot, time-bounded :class:`Behavior` realising ``spec``.

    The behavior claims exactly ``spec.channels()``, lives ``Lifetime(looping=False,
    duration=spec.duration)``, and (by default) contends as ``STOPPABLE`` so a
    higher-priority ``stopping`` / ``unstoppable`` behavior can preempt it. ``start``
    is the pose the minjerk interpolates *from*: a :class:`Contribution` (from a
    start-pose provider) for continuity, or ``None`` for the neutral-relative
    default (see the module docstring's start-pose note). Raises ``ValueError`` for
    a goto that claims no channel or has a non-positive duration.
    """
    channels = spec.channels()
    if not channels:
        raise ValueError("a goto must name at least one channel (head / antennas / body_yaw)")
    if spec.duration <= 0:
        raise ValueError(f"a goto duration must be > 0 (got {spec.duration!r})")
    start_c = start if start is not None else Contribution()
    return Behavior(
        id=behavior_id,
        name="goto",
        channels=channels,
        stop_class=stop_class,
        lifetime=Lifetime(looping=False, duration=spec.duration),
        params={},
        fn=_goto_fn(spec, start_c),
        wants_sense=False,
    )


# --------------------------------------------------------------------------- #
# GotoLane — the per-tick seam driver                                         #
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _Pending:
    """A queued goto that has not gone in flight yet (id assigned at submit)."""

    id: str
    spec: GotoSpec


@dataclass
class _Inflight:
    """The single goto currently admitted to the engine, and when it ends."""

    behavior: Behavior
    spec: GotoSpec
    admit_now: float
    end_time: float


class GotoLane:
    """A serial FIFO of minjerk gotos, driven onto the engine's per-tick seam.

    Construct one, submit gotos to it (from any thread — the queue is lock-guarded
    like :class:`reachy.motion.queue.MotionQueue`), and compose it as a driver into
    a :class:`~reachy.behavior.rule_engine.TickBus` passed to
    ``engine.run(tick_seam=...)``. On every tick it advances at most one in-flight
    goto (done / cancel) and, when idle, admits the next queued goto — one move at a
    time, in FIFO order. See the module docstring for the preemption / no-resume /
    start-pose semantics.

    Parameters
    ----------
    start_pose_provider:
        Optional ``() -> Contribution`` sampled at admit time to seed each goto's
        minjerk *start* pose (for C0 continuity). ``None`` (the default) uses the
        neutral-relative start.
    id_prefix:
        Prefix for the ids this lane mints (``"<prefix>-<n>"``); the id is also the
        admitted behavior's id, so ownership checks and events reference it.
    stop_class:
        The contention class each goto is admitted with (default ``STOPPABLE`` — the
        acceptance semantics; a preemptible move).
    """

    def __init__(
        self,
        *,
        start_pose_provider: Callable[[], Contribution] | None = None,
        id_prefix: str = "goto",
        stop_class: StopClass = StopClass.STOPPABLE,
    ) -> None:
        self._queue: list[_Pending] = []
        self._inflight: _Inflight | None = None
        self._provider = start_pose_provider
        self._prefix = id_prefix
        self._stop_class = stop_class
        self._seq = 0
        self._lock = threading.Lock()

    # -- submission surface ------------------------------------------------ #

    def submit(self, spec: GotoSpec) -> str:
        """Enqueue ``spec`` (strict FIFO — no coalescing); return its goto id.

        Thread-safe: a producer thread may submit while the engine thread drains,
        mirroring :class:`reachy.motion.queue.MotionQueue`. Unlike a reactive queue
        this lane does **not** coalesce — every one-shot goto runs (or is cancelled)
        in order, matching ``MotionAction(coalesce_key=None)`` semantics.
        """
        with self._lock:
            self._seq += 1
            gid = f"{self._prefix}-{self._seq}"
            self._queue.append(_Pending(gid, spec))
        return gid

    def pending(self) -> list[GotoSpec]:
        """The queued (not-yet-in-flight) goto specs, oldest-first (for status)."""
        with self._lock:
            return [p.spec for p in self._queue]

    def busy_until(self, now: float) -> float:
        """The wall-clock horizon all currently-known goto work finishes by.

        The serial projection: the in-flight goto's end (or ``now``) plus every
        queued goto's duration behind it. Equals ``now`` when idle and empty, so
        ``busy_until(now) > now`` means the lane is busy. Mirrors the executor's
        ``busy_until`` role for :class:`reachy.motion.queue.MotionQueue` callers.
        """
        with self._lock:
            horizon = max(now, self._inflight.end_time) if self._inflight is not None else now
            for pending in self._queue:
                horizon += pending.spec.duration
            return horizon

    def inflight_id(self) -> str | None:
        """The id of the goto currently in flight, or ``None`` when idle."""
        with self._lock:
            return self._inflight.behavior.id if self._inflight is not None else None

    def __len__(self) -> int:
        with self._lock:
            return len(self._queue)

    # -- seam driver ------------------------------------------------------- #

    def __call__(self, ctx) -> None:
        """Usable directly as a ``TickBus`` driver / ``engine.run(tick_seam=...)``."""
        self.on_tick(ctx)

    def on_tick(self, ctx) -> None:
        """Advance the in-flight goto (done / cancel), then admit the next if idle."""
        if self._inflight is not None:
            self._evaluate_inflight(ctx)
        if self._inflight is None and self._has_pending():
            self._admit_next(ctx)

    def _has_pending(self) -> bool:
        with self._lock:
            return bool(self._queue)

    def _evaluate_inflight(self, ctx) -> None:
        inflight = self._inflight
        assert inflight is not None  # guarded by the caller  # noqa: S101
        gid = inflight.behavior.id
        # Natural completion first: at/after its end the engine has expired it, so
        # its channels are no longer its own — but this is DONE, not a preemption.
        if ctx.now >= inflight.end_time:
            self._inflight = None
            self._emit(ctx, EVENT_DONE, id=gid, label=inflight.spec.label)
            return
        # Still within its duration: it must own every channel it claims. Losing any
        # (a stopping add evicted it, or an unstoppable out-prioritised it) is a
        # preemption -> cancel, force-evict (so it can never resume half-way), drop.
        lost = sorted(ch for ch in inflight.behavior.channels if ctx.ownership.get(ch) != gid)
        if lost:
            channel = lost[0]
            owner = ctx.ownership.get(channel)
            reason = REASON_PREEMPTED if owner is not None else REASON_EVICTED
            ctx.evict(gid)
            self._inflight = None
            self._emit(
                ctx,
                EVENT_CANCELLED,
                id=gid,
                label=inflight.spec.label,
                reason=reason,
                channel=channel,
                owner=owner,
            )

    def _admit_next(self, ctx) -> None:
        with self._lock:
            pending = self._queue.pop(0)
        start = self._provider() if self._provider is not None else None
        behavior = build_goto_behavior(
            pending.spec, pending.id, start=start, stop_class=self._stop_class
        )
        ctx.admit(behavior)
        self._inflight = _Inflight(
            behavior=behavior,
            spec=pending.spec,
            admit_now=ctx.now,
            end_time=ctx.now + pending.spec.duration,
        )
        self._emit(
            ctx,
            EVENT_ADMITTED,
            id=pending.id,
            label=pending.spec.label,
            channels=sorted(behavior.channels),
            duration=pending.spec.duration,
        )

    def _emit(self, ctx, event_type: str, **fields) -> None:
        ctx.emit({"type": event_type, "ts": ctx.now, "tick": ctx.tick, **fields})


# --------------------------------------------------------------------------- #
# GotoLaneAdapter — the MotionQueue-shaped facade                             #
# --------------------------------------------------------------------------- #


def _to_spec(action) -> GotoSpec:
    """Coerce a submitted move into a :class:`GotoSpec`.

    Accepts a :class:`GotoSpec` as-is, or any ``MotionAction``-shaped object
    (duck-typed on ``label`` / ``head`` / ``antennas`` / ``body_yaw`` / ``duration``
    / ``interpolation``) so a :class:`reachy.motion.queue.MotionQueue` caller
    submits unchanged, without this leaf importing the motion package.
    """
    if isinstance(action, GotoSpec):
        return action
    return GotoSpec(
        label=getattr(action, "label", "goto"),
        head=getattr(action, "head", None),
        antennas=getattr(action, "antennas", None),
        body_yaw=getattr(action, "body_yaw", None),
        duration=getattr(action, "duration", 1.0),
        interpolation=getattr(action, "interpolation", DEFAULT_INTERPOLATION),
    )


class GotoLaneAdapter:
    """A MotionQueue-shaped facade over a :class:`GotoLane`.

    Lets existing :class:`reachy.motion.queue.MotionQueue` callers reach the engine
    goto lane **unchanged** — submit a ``MotionAction`` (or a :class:`GotoSpec`) and
    read the same surface: ``submit`` (returns the goto id, unlike the queue's
    ``None`` — the one intentional difference, so a caller can track/await a goto),
    ``pending`` (oldest-first), ``busy_until`` (the serial horizon), ``__len__``.
    Ride the wrapped :attr:`lane` as the engine seam driver.
    """

    def __init__(self, lane: GotoLane | None = None, **lane_kwargs) -> None:
        self._lane = lane if lane is not None else GotoLane(**lane_kwargs)

    @property
    def lane(self) -> GotoLane:
        """The wrapped :class:`GotoLane` — compose it as the engine's seam driver."""
        return self._lane

    def submit(self, action) -> str:
        """Enqueue a ``MotionAction``/``GotoSpec`` (serial FIFO); return its goto id."""
        return self._lane.submit(_to_spec(action))

    def pending(self) -> list[GotoSpec]:
        """The queued (not-yet-in-flight) goto specs, oldest-first."""
        return self._lane.pending()

    def busy_until(self, now: float) -> float:
        """The serial horizon all currently-known goto work finishes by."""
        return self._lane.busy_until(now)

    def __len__(self) -> int:
        return len(self._lane)
