"""The ``vision`` producer: turn the head toward what it *sees*, via the motion queue.

This is the visual analogue of the ``listen`` subsystem
(:mod:`reachy.motion.listen` + :mod:`reachy.motion.server`). Where ``listen``
consumes a per-tick acoustic *Direction of Arrival* and turns the head toward
sound, :class:`VisionProducer` consumes a per-tick *camera frame*, runs the
two wave-1 pixel detectors over it (:class:`~reachy.vision.motion.MotionDetector`
and :class:`~reachy.vision.light.LightDetector`), and turns the head toward the
strongest visual event.

The design mirrors ``listen`` closely:

* **One smooth move at a time.** Decisions are issued as
  :class:`~reachy.motion.queue.MotionAction` objects onto a serial
  :class:`~reachy.motion.queue.MotionQueue`, and a built-in executor (modelled on
  :func:`reachy.motion.server._dispatch_next`) only starts the next move once the
  robot is *idle* (``t >= busy_until``). A faster-moving producer just coalesces
  its pending look under :data:`~reachy.motion.queue.LOOK_KEY`, so interpolated
  ``goto`` moves can never overlap or fight each other — every turn is a soft
  minjerk trajectory.
* **Deadband / hold idioms.** A look only commits when the new target is more
  than ``deadband`` degrees off the current heading; after committing, a ``hold``
  window suppresses re-commits so the head does not whip back and forth.
* **Holds when nothing happens.** With no detected event — or an event inside the
  deadband — *no* move is enqueued (the head holds its current heading).

**Event priority.** Per tick both detectors are fed the same frame. Motion is the
primary cue: a :class:`MotionResult` whose ``magnitude`` clears
``motion_threshold`` wins. A light *change* event (``LightResult.changed`` with a
non-``None`` ``direction``) is the fallback cue, used only when motion did not
fire. The chosen event's normalised horizontal ``direction`` in ``[-1, 1]`` is
mapped to a head-yaw target (``direction * gain * max_yaw``, clamped to
``±max_yaw``); positive yaw turns the head left, matching the rest of the CLI.

**Offline by construction.** The producer is fed frames through any object that
exposes ``get_frame() -> np.ndarray`` (the real one is
:meth:`reachy.robot.sdk_transport.SdkTransport.get_frame`; tests pass a fake that
yields synthetic frames). Moves go onto an injectable ``transport`` exposing
``move_goto(...)`` and an optional ``on_action`` callback records what was
enqueued, so a test can drive a bounded :meth:`run` (``max_ticks``) entirely in
process. Per-tick work is pure numpy (the detectors) plus a couple of float
comparisons — cheap enough for >=10 FPS on a Raspberry Pi 4.

``reachy_mini`` is never imported here (it is an uninstalled extra); the producer
only ever talks to its ``transport`` through the small ``get_frame`` /
``move_goto`` duck-typed surface, and environment failures surface as
:class:`~reachy.cli._errors.CliError` (no tracebacks).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Protocol

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.motion.queue import LOOK_KEY, MotionAction, MotionQueue
from reachy.vision.light import LightDetector, LightResult
from reachy.vision.motion import MotionDetector, MotionResult

# Default loop cadence; the camera + detectors update slowly relative to this so a
# modest tick keeps per-frame compute well within a 10 FPS budget on a Pi 4.
DEFAULT_TICK = 0.1  # 10 Hz
# Extra hold after a move completes before the next may start — a beat between turns.
SETTLE = 0.2


class _FrameSource(Protocol):
    """The slice of a transport :class:`VisionProducer` consumes: a frame getter."""

    def get_frame(self) -> object: ...  # noqa: E704  # returns an np.ndarray (H x W x C)


class _MoveSink(Protocol):
    """The slice of a transport used to issue a move (matches ``Transport.move_goto``)."""

    def move_goto(  # noqa: E704
        self,
        *,
        head: dict[str, float] | None = ...,
        antennas: tuple[float, float] | None = ...,
        body_yaw: float | None = ...,
        duration: float,
        interpolation: str,
    ) -> object: ...


@dataclass
class VisionParams:
    """Tunables for :class:`VisionProducer` (degrees, seconds, deg/s).

    Mirrors :class:`reachy.motion.listen.ListenParams`' knob style: a head-yaw
    ``gain`` and clamp (``max_yaw``), a ``deadband`` that suppresses tiny
    re-targets, a post-turn ``hold`` so the head settles, and a slew ``speed`` /
    duration floor+ceiling so even small turns are deliberate (never snappy).
    ``motion_threshold`` is the minimum motion magnitude that may drive a turn.
    """

    gain: float = 1.0
    max_yaw: float = 35.0
    deadband: float = 8.0  # ignore visual targets within this of the current heading (deg)
    hold: float = 1.0  # after turning, hold this long before reconsidering (s)
    speed: float = 30.0  # slew speed toward a new target (deg/s)
    min_dur: float = 0.4  # floor so even small turns are smooth, never instant
    max_dur: float = 2.0
    motion_threshold: float = 0.0  # extra magnitude floor above the detector's own threshold


def _head(yaw: float) -> dict[str, float]:
    """A six-axis head-offset dict that only sets yaw (the rest centred)."""
    return {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": yaw}


@dataclass(frozen=True)
class VisionEvent:
    """The chosen per-tick visual cue: a normalised horizontal ``direction`` + strength."""

    direction: float  # [-1, 1]; -1 = far left, +1 = far right
    strength: float
    source: str  # "motion" or "light"


def _choose_event(
    motion: MotionResult | None,
    light: LightResult,
    motion_threshold: float,
) -> VisionEvent | None:
    """Pick the strongest visual event for this tick (motion primary, light fallback).

    Motion wins whenever it fired and clears ``motion_threshold``. Otherwise a
    *changed* light event with a locatable centroid is used. Returns ``None`` when
    neither detector produced a usable, directional event (→ the head holds).
    """
    if motion is not None and motion.magnitude >= motion_threshold:
        return VisionEvent(direction=motion.direction, strength=motion.magnitude, source="motion")
    if light.changed and light.direction is not None:
        return VisionEvent(direction=light.direction, strength=light.mean_luma, source="light")
    return None


@dataclass
class VisionProducer:
    """Stateful frame→look decision + serial executor. Call :meth:`tick` each step.

    Construct with a ``transport`` (anything exposing ``get_frame()`` and
    ``move_goto(...)``), optionally injecting the two detectors and a
    :class:`VisionParams`. :meth:`tick` pulls one frame, feeds both detectors,
    chooses the strongest event, and enqueues *at most one* smooth head-orient
    ``goto`` toward it through the serial motion queue — exactly the way the
    ``listen`` executor issues moves (peek → ``move_goto`` → pop, one at a time,
    gated on ``busy_until``). :meth:`run` drives :meth:`tick` in a bounded loop.
    """

    transport: object
    params: VisionParams = field(default_factory=VisionParams)
    motion_detector: MotionDetector = field(default_factory=MotionDetector)
    light_detector: LightDetector = field(default_factory=LightDetector)
    queue: MotionQueue = field(default_factory=MotionQueue)

    committed: float = 0.0  # current head yaw (deg)
    _busy_until: float = 0.0
    _hold_until: float = 0.0

    # ------------------------------------------------------------------ #
    # decision                                                           #
    # ------------------------------------------------------------------ #

    def _target_yaw(self, direction: float) -> float:
        """Map a normalised horizontal ``direction`` in ``[-1, 1]`` to a clamped head yaw.

        ``direction`` is camera-space (``+1`` = right edge of the frame). Positive
        head yaw turns the robot left (matching ``doa_angle_to_yaw`` / the rest of
        the CLI), so a target on the *right* of the frame must turn the head right
        → the sign is flipped here. Scaled by ``gain * max_yaw`` then clamped.
        """
        p = self.params
        raw = -direction * p.gain * p.max_yaw
        return max(-p.max_yaw, min(p.max_yaw, raw))

    def _look_action(self, target: float, t: float) -> MotionAction:
        """Build (and commit to) one smooth minjerk head-orient toward ``target`` yaw."""
        p = self.params
        dur = max(
            p.min_dur,
            min(p.max_dur, abs(target - self.committed) / p.speed if p.speed else p.max_dur),
        )
        self.committed = target
        # Suppress re-commits until this move lands AND we've dwelt ``hold`` there.
        self._hold_until = t + dur + p.hold
        return MotionAction(
            label=f"look {target:+.0f}",
            head=_head(target),
            duration=dur,
            interpolation="minjerk",
            coalesce_key=LOOK_KEY,
        )

    def decide(self, frame: object, t: float) -> MotionAction | None:
        """Feed both detectors one ``frame`` and return a look action, or ``None``.

        Pure decision: feeds the detectors, chooses the strongest event, maps it to
        a yaw target, and returns a :class:`MotionAction` only when a turn is
        warranted (outside the ``hold`` window and more than ``deadband`` off the
        current heading). Returns ``None`` to hold (no event, in deadband, or
        within the hold window). Does not touch the queue or the transport.
        """
        motion = self.motion_detector.feed(frame)  # type: ignore[arg-type]
        light = self.light_detector.feed(frame)  # type: ignore[arg-type]
        if t < self._hold_until:
            return None
        event = _choose_event(motion, light, self.params.motion_threshold)
        if event is None:
            return None
        target = self._target_yaw(event.direction)
        if abs(target - self.committed) <= self.params.deadband:
            return None  # within deadband of current heading — hold
        return self._look_action(target, t)

    # ------------------------------------------------------------------ #
    # executor (serial, no overlap — mirrors reachy.motion.server)       #
    # ------------------------------------------------------------------ #

    def _service_queue(self, t: float, on_action: Callable | None) -> None:
        """If idle and something is queued, issue exactly one move (peek → goto → pop).

        Models :func:`reachy.motion.server._dispatch_next`: peeks, sends via
        ``move_goto``, and only :meth:`~reachy.motion.queue.MotionQueue.pop`\\ s
        once the move is accepted, so a transient send failure leaves the move
        pending to retry rather than dropping it. A new move never starts while one
        is running (``t < busy_until``), so interpolated moves never overlap.
        """
        if t < self._busy_until or not len(self.queue):
            return
        nxt = self.queue.peek()
        self.transport.move_goto(  # type: ignore[attr-defined]
            head=nxt.head,
            antennas=nxt.antennas,
            body_yaw=nxt.body_yaw,
            duration=nxt.duration,
            interpolation=nxt.interpolation,
        )
        self.queue.pop()  # accepted — now safe to remove it
        self._busy_until = t + nxt.duration + SETTLE
        if on_action is not None:
            on_action(nxt)

    def tick(self, t: float, *, on_action: Callable | None = None) -> MotionAction | None:
        """One loop step: pull a frame, decide, submit, and service the queue.

        Returns the :class:`MotionAction` decided this tick (already submitted to
        the queue), or ``None`` when the head holds. The queue is then serviced so
        at most one move is dispatched per tick and never while one is in flight.
        """
        frame = self._get_frame()
        action = self.decide(frame, t)
        if action is not None:
            self.queue.submit(action)
        self._service_queue(t, on_action)
        return action

    def _get_frame(self) -> object:
        """Pull one frame from the transport, wrapping any failure as a CliError.

        A :class:`~reachy.cli._errors.CliError` from the transport (e.g. no local
        camera) propagates unchanged; any other failure is wrapped so no traceback
        ever leaks (the agent-first error contract).
        """
        try:
            return self.transport.get_frame()  # type: ignore[attr-defined]
        except CliError:
            raise
        except Exception as err:  # noqa: BLE001
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"could not read a camera frame: {err}",
                remediation=(
                    "check the camera is connected and run on the robot itself "
                    "(local camera frames need the [sdk]/[daemon] extra)"
                ),
            ) from err

    def run(
        self,
        *,
        now: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], None] = time.sleep,
        tick: float = DEFAULT_TICK,
        max_ticks: int | None = None,
        on_action: Callable | None = None,
        stop: dict | None = None,
    ) -> int:
        """Drive :meth:`tick` until ``max_ticks`` or ``stop['flag']``; return ticks run.

        Injectable ``now`` / ``sleep`` and a bounded ``max_ticks`` make it
        deterministic for tests (no wall-clock, no robot). One move at most is
        dispatched per tick, never overlapping a move already in flight.
        """
        stop = stop if stop is not None else {"flag": False}
        ticks = 0
        while not stop["flag"]:
            self.tick(now(), on_action=on_action)
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            sleep(tick)
        return ticks
