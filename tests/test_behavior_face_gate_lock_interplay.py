"""The still-only face gate (#179) and a HELD face lock, together.

The gate's whole point is that a detection submitted while the head is slewing
is expensive (the CM4's tick rate went 50 -> 7 Hz) and wrong (a blurred bbox
stamped in a head frame that has already moved on). But the lock is exactly the
consumer that MOVES the head — so a gate that simply refuses to detect while
moving would blind the lock precisely while it chases a face, and the lock
would then report ``face-lost`` on a person it can see perfectly well.

Hence the two-speed gate, and hence this file: the gate degrades to a slow
cadence (:data:`~reachy.behavior.face_sense.DEFAULT_HELD_DETECT_INTERVAL`)
while a lock is held, rather than to nothing, and the resulting bbox gaps are
checked against the LOCK's own timers — ``FACE_LOST_AFTER_S`` (3.0 s) and
``MAX_FACE_AGE_S`` (1.5 s), neither of which this arc changes.

Nothing here touches a robot, a camera, a clock that sleeps, or cv2: the media
client, the detector and the store are fakes, the gate's clock is a list, and
the lock is driven tick by tick exactly as
``tests/test_behavior_face_lock_lifecycle.py`` drives it.
"""

from __future__ import annotations

from dataclasses import dataclass, field

import numpy as np
import pytest

from reachy.behavior import face_sense as FS
from reachy.behavior.face_lock import (
    EVENT_FACE_LOST,
    FACE_LOCK_BEHAVIOR,
    LOCK_FACE,
    MAX_FACE_AGE_S,
    FaceLockDriver,
)
from reachy.behavior.face_sense import FaceSenseDriver
from reachy.behavior.intents import IntentDriver
from reachy.behavior.sense import EMPTY_SENSE, Sense

# --------------------------------------------------------------------------- #
# Fakes — the narrow surfaces each driver consumes                            #
# --------------------------------------------------------------------------- #


def _frame() -> np.ndarray:
    return np.zeros((6, 8, 3), dtype=np.uint8)


class _Media:
    def __init__(self) -> None:
        self.camera_available = True

    def frame(self):
        return _frame()


class _Detection:
    bbox_norm = (0.4, 0.4, 0.6, 0.6)
    embedding = (0.1, 0.2)


class _Engine:
    def __init__(self) -> None:
        self.calls = 0

    def detect(self, frame):
        self.calls += 1
        return _Detection()


class _Store:
    def match(self, embedding):
        return None  # an unrecognised face still has a POSITION


class _Ctx:
    def __init__(self, now: float) -> None:
        self.now = now


@dataclass
class _LockCtx:
    """The duck-typed TickContext ``test_behavior_face_lock_lifecycle.py`` uses."""

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        self._active.add(behavior.name)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, target: str) -> dict:
        self.evicts.append(target)
        self._active.discard(FACE_LOCK_BEHAVIOR)
        return {"ok": True, "op": "stop", "target": target}

    def active_names(self) -> set:
        return set(self._active)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


def _build(clock, **kwargs) -> tuple[FaceSenseDriver, _Engine]:
    engine = _Engine()
    driver = FaceSenseDriver(
        media=_Media(),
        engine=engine,
        store=_Store(),
        detect_interval=0.5,
        frame_interval_s=0.0,
        clock=lambda: clock[0],
        start_worker=False,
        **kwargs,
    )
    return driver, engine


def _run(driver, engine, clock, *, seconds: float, step: float = 0.1) -> None:
    """Advance the fake clock, driving one tick and one worker iteration per step."""
    ticks = int(round(seconds / step))
    for _ in range(ticks):
        clock[0] = round(clock[0] + step, 6)
        driver(_Ctx(clock[0]))
        driver._worker_tick()


# --------------------------------------------------------------------------- #
# (a) a moving head detects nothing; a settled one detects again              #
# --------------------------------------------------------------------------- #


def test_two_seconds_of_motion_submits_no_detection_at_all() -> None:
    clock = [0.0]
    driver, engine = _build(clock, moving=lambda: True)
    _run(driver, engine, clock, seconds=2.0)
    assert engine.calls == 0
    driver.close()


def test_the_settle_is_its_own_constant_and_not_self_motions_tail() -> None:
    """The camera-blur settle is a DIFFERENT quantity from the actuator-noise tail.

    ``self_motion.DEFAULT_TAIL_S`` (0.25 s) bounds how long the latch keeps
    reading "moving" after the commanded pose stops changing; ``pat_sense``'s
    ``still_hold_s`` (1.0 s) is a servo settle. Neither is a measurement of how
    long the camera keeps returning a blurred frame in a stale head frame — so
    this module owns its own number, and it is documented as unmeasured
    (issue #179's park).
    """
    from reachy.behavior import self_motion

    assert FS.DEFAULT_STILL_SETTLE_S != self_motion.DEFAULT_TAIL_S
    assert FS.DEFAULT_STILL_SETTLE_S > 0.0


# --------------------------------------------------------------------------- #
# (b) a HELD lock degrades the cadence instead of silencing the sense         #
# --------------------------------------------------------------------------- #


def test_a_held_lock_keeps_detecting_at_the_held_cadence_while_moving() -> None:
    clock = [0.0]
    driver, engine = _build(
        clock,
        moving=lambda: True,
        lock_held=lambda: True,
        held_detect_interval=1.5,
    )
    _run(driver, engine, clock, seconds=6.0)
    # Never zero (that would blind the lock), and never the free-running rate.
    assert engine.calls >= 3
    assert engine.calls <= 6.0 / 1.5 + 1
    driver.close()


def test_the_held_cadence_is_slower_than_the_free_running_one() -> None:
    assert FS.DEFAULT_HELD_DETECT_INTERVAL > FS.DEFAULT_DETECT_INTERVAL


# --------------------------------------------------------------------------- #
# (c) the LOCK's own timers survive the widened bbox gaps                     #
# --------------------------------------------------------------------------- #


def _lock_driver() -> tuple[FaceLockDriver, object, _LockCtx]:
    intents = IntentDriver()
    driver = FaceLockDriver(
        inhibitions_getter=lambda: intents.inhibitions,
        inhibitions_setter=intents.set_inhibitions,
    )
    driver.register_into(intents.registry)
    ctx = _LockCtx(sense=Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=0.0))
    intents.registry.dispatch({"op": LOCK_FACE}, ctx)
    return driver, intents.registry, ctx


def test_bbox_gaps_of_one_held_cadence_never_report_face_lost() -> None:
    """The merge gate: a held lock on a moving head is not a lost face.

    Detections land one ``DEFAULT_HELD_DETECT_INTERVAL`` apart, so ``face_age_s``
    saws between 0 and 1.5 s. The lock stops STEERING above ``MAX_FACE_AGE_S``
    (1.5 s) for a moment, and reports ``face-lost`` only after 3.0 s of
    continuous absence — which this sawtooth never reaches.
    """
    driver, _registry, ctx = _lock_driver()
    now = 0.0
    last_detect = 0.0
    for _ in range(1500):  # 30 s at 50 Hz
        now = round(now + 0.02, 6)
        if now - last_detect >= FS.DEFAULT_HELD_DETECT_INTERVAL:
            last_detect = now
        age = now - last_detect
        ctx.sense = Sense(face_bbox=(0.4, 0.4, 0.2, 0.2), face_age_s=age)
        ctx.now = now
        ctx.tick += 1
        driver.on_tick(ctx, now)

    lost = [e for e in ctx.events if e.get("type") == EVENT_FACE_LOST]
    assert lost == [], lost
    assert driver.locked is True


def test_the_bbox_ttl_covers_a_settle_plus_one_free_running_detection() -> None:
    """The TTL derivation, asserted rather than only written in a comment.

    After a slew ends the worst case is: the settle elapses, then one whole
    detect interval passes before the next detection lands. The position must
    still be held across that, or a gaze one-shot planned right after a slew
    would see no face at all.
    """
    assert FS.DEFAULT_FACE_BBOX_TTL_S >= FS.DEFAULT_STILL_SETTLE_S + FS.DEFAULT_DETECT_INTERVAL


def test_the_bbox_ttl_matches_the_locks_own_max_face_age() -> None:
    """Widening the TTL past the lock's staleness limit would buy the lock nothing.

    The lock refuses to steer on a reading older than ``MAX_FACE_AGE_S``, so a
    position held longer than that is invisible to it — which is why the
    still-only gate does NOT lengthen the TTL.
    """
    assert FS.DEFAULT_FACE_BBOX_TTL_S == MAX_FACE_AGE_S


def test_the_held_cadence_stays_inside_the_locks_face_lost_window() -> None:
    """The one inequality that makes (c) hold for any lock, not just this fake one."""
    from reachy.behavior.face_lock import FACE_LOST_AFTER_S

    assert FS.DEFAULT_HELD_DETECT_INTERVAL < FACE_LOST_AFTER_S
