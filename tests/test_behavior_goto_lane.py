"""The goto lane — serial minjerk gotos as seam-driven stoppable contributions.

Task t5: fold the goto lane into the behavior engine. A goto is a one-shot,
time-bounded, minjerk-interpolated move expressed as a ``StopClass.STOPPABLE``
:class:`~reachy.behavior.model.Behavior`, driven onto the engine's ONE per-tick
seam (:class:`~reachy.behavior.engine.TickContext`) by a :class:`GotoLane` — a
per-tick DRIVER exactly like :class:`~reachy.behavior.rule_engine.RuleEngine`.

These tests pin, deterministically (a fake in-memory sink + the engine's injected
``sleep`` / ``now`` / ``max_ticks`` seams, or a fully-faked ``TickContext``):

* the minjerk shape of a goto contribution (start/target endpoints, monotonic
  approach, clamp past duration, only the named channels claimed);
* the lane's serial FIFO, one-in-flight-at-a-time admission;
* natural completion (``goto.done``) vs preemption (``goto.cancelled``);
* the acceptance-critical **no-resume** guarantee — a higher-priority
  ``stopping``/``unstoppable`` behavior interrupts an in-flight goto and the goto
  never resumes half-way, even after the blocker itself expires;
* the :class:`GotoLaneAdapter` MotionQueue-shaped facade (serial submit, a busy
  horizon, ``pending``), proven end-to-end in a bounded engine run.

No robot, daemon, or network anywhere.
"""

from __future__ import annotations

import contextlib

import pytest

from reachy.behavior import engine as E
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.goto_lane import (
    EVENT_ADMITTED,
    EVENT_CANCELLED,
    EVENT_DONE,
    GotoLane,
    GotoLaneAdapter,
    GotoSpec,
    build_goto_behavior,
    minjerk_progress,
)
from reachy.behavior.library import build as build_behavior
from reachy.behavior.model import Contribution, Lifetime, StopClass
from reachy.behavior.rule_engine import TickBus
from reachy.behavior.sense import EMPTY_SENSE

# --------------------------------------------------------------------------- #
# A fully-faked TickContext for precise, ownership-controlled unit tests       #
# --------------------------------------------------------------------------- #


class FakeCtx:
    """A stand-in :class:`TickContext` giving a test full control over ownership.

    The real engine's arbitration decides ownership from admitted behaviors; here
    the test sets ``ownership`` directly per tick so the lane's admit / done /
    cancel logic can be pinned without running the compose loop. ``emit`` /
    ``admit`` / ``evict`` record their calls for assertion.
    """

    def __init__(self, now, tick, ownership):
        self.now = now
        self.tick = tick
        self.ownership = dict(ownership)
        self.sense = EMPTY_SENSE
        self.events: list[dict] = []
        self.admitted: list = []
        self.evicted: list = []
        self._active: dict = {}

    def emit(self, event):
        self.events.append(event)

    def admit(self, behavior):
        self.admitted.append(behavior)
        self._active[behavior.id] = behavior
        return {"ok": True, "id": behavior.id}

    def evict(self, name_or_id):
        self.evicted.append(name_or_id)
        self._active.pop(name_or_id, None)
        return {"ok": True, "target": name_or_id}

    def active_names(self):
        return {b.name for b in self._active.values()}


# --------------------------------------------------------------------------- #
# minjerk math + goto Behavior shape                                          #
# --------------------------------------------------------------------------- #


def test_minjerk_progress_endpoints_and_clamp() -> None:
    assert minjerk_progress(0.0) == 0.0
    assert minjerk_progress(1.0) == 1.0
    assert minjerk_progress(0.5) == pytest.approx(0.5)  # symmetric midpoint
    assert minjerk_progress(-1.0) == 0.0  # clamped below
    assert minjerk_progress(2.0) == 1.0  # clamped above


def test_goto_behavior_is_a_one_shot_stoppable_bounded_contribution() -> None:
    spec = GotoSpec(head={"pitch": 10.0}, duration=2.0, interpolation="minjerk")
    beh = build_goto_behavior(spec, "g1")
    assert beh.name == "goto"
    assert beh.id == "g1"
    assert beh.stop_class is StopClass.STOPPABLE  # a stopping behavior can preempt it
    assert beh.lifetime.looping is False
    assert beh.lifetime.duration == 2.0  # time-bounded to duration_s
    assert beh.channels == frozenset({"head"})  # claims exactly the named channel


def test_goto_contribution_endpoints_and_monotonic_minjerk() -> None:
    spec = GotoSpec(head={"pitch": 10.0}, duration=2.0)
    beh = build_goto_behavior(spec, "g1")
    assert beh.contribution(0.0).head["pitch"] == pytest.approx(0.0)  # start (neutral)
    assert beh.contribution(1.0).head["pitch"] == pytest.approx(5.0)  # minjerk(0.5) == 0.5
    assert beh.contribution(2.0).head["pitch"] == pytest.approx(10.0)  # target at duration
    assert beh.contribution(3.0).head["pitch"] == pytest.approx(10.0)  # clamped past duration
    seq = [beh.contribution(t).head["pitch"] for t in (0.0, 0.25, 0.5, 1.0, 1.5, 2.0)]
    assert all(later >= earlier - 1e-9 for earlier, later in zip(seq, seq[1:]))  # monotone


def test_goto_contribution_abstains_on_unclaimed_channels() -> None:
    spec = GotoSpec(head={"pitch": 10.0}, duration=1.0)
    c = build_goto_behavior(spec, "g1").contribution(0.5)
    assert c.antennas is None and c.body_yaw is None  # only head claimed -> others abstain


def test_goto_behavior_claims_only_the_named_channels() -> None:
    spec = GotoSpec(antennas=(10.0, -10.0), body_yaw=15.0, duration=1.0)
    beh = build_goto_behavior(spec, "g2")
    assert beh.channels == frozenset({"antennas", "body_yaw"})
    c0 = beh.contribution(0.0)
    assert c0.head is None
    assert c0.antennas == pytest.approx((0.0, 0.0)) and c0.body_yaw == pytest.approx(0.0)
    c1 = beh.contribution(1.0)
    assert c1.antennas == pytest.approx((10.0, -10.0)) and c1.body_yaw == pytest.approx(15.0)


def test_goto_behavior_honours_an_injected_start_pose() -> None:
    """The start-pose provider path: interpolate from a captured pose, not neutral."""
    spec = GotoSpec(head={"pitch": 10.0}, duration=2.0)
    start = Contribution(head={"x": 0, "y": 0, "z": 0, "roll": 0, "pitch": 4.0, "yaw": 0})
    beh = build_goto_behavior(spec, "g1", start=start)
    assert beh.contribution(0.0).head["pitch"] == pytest.approx(4.0)  # starts at captured pose
    assert beh.contribution(2.0).head["pitch"] == pytest.approx(10.0)  # still lands on target


def test_build_goto_rejects_no_channels_and_bad_duration() -> None:
    with pytest.raises(ValueError):
        build_goto_behavior(GotoSpec(duration=1.0), "x")  # claims nothing
    with pytest.raises(ValueError):
        build_goto_behavior(GotoSpec(head={"pitch": 1.0}, duration=0.0), "x")  # duration <= 0


# --------------------------------------------------------------------------- #
# GotoLane driver — serial FIFO, done vs cancel, no-resume (faked ctx)         #
# --------------------------------------------------------------------------- #


def test_lane_admits_one_goto_at_a_time_in_fifo_order() -> None:
    lane = GotoLane()
    a = lane.submit(GotoSpec(head={"pitch": 10.0}, duration=1.0, label="a"))
    b = lane.submit(GotoSpec(head={"pitch": 20.0}, duration=1.0, label="b"))
    assert len(lane) == 2
    assert [s.label for s in lane.pending()] == ["a", "b"]

    c1 = FakeCtx(0.0, 1, {"head": None})
    lane.on_tick(c1)
    assert [x.id for x in c1.admitted] == [a]  # only the head of the FIFO is admitted
    assert [e["type"] for e in c1.events] == [EVENT_ADMITTED]
    assert len(lane) == 1 and lane.pending()[0].label == "b"

    c2 = FakeCtx(0.5, 2, {"head": a})  # a owns head -> running, nothing new admitted
    lane.on_tick(c2)
    assert c2.admitted == [] and c2.events == []

    c3 = FakeCtx(1.0, 3, {"head": None})  # a's duration elapsed -> done, then b admitted
    lane.on_tick(c3)
    assert [e["type"] for e in c3.events] == [EVENT_DONE, EVENT_ADMITTED]
    assert [x.id for x in c3.admitted] == [b]
    assert len(lane) == 0


def test_lane_emits_goto_done_on_natural_completion_without_evicting() -> None:
    lane = GotoLane()
    gid = lane.submit(GotoSpec(head={"pitch": 10.0}, duration=1.0))
    lane.on_tick(FakeCtx(0.0, 1, {"head": None}))  # admit
    lane.on_tick(FakeCtx(0.5, 2, {"head": gid}))  # running
    ctx = FakeCtx(1.0, 3, {"head": None})  # now >= end -> done
    lane.on_tick(ctx)
    done = [e for e in ctx.events if e["type"] == EVENT_DONE]
    assert len(done) == 1 and done[0]["id"] == gid
    assert ctx.evicted == []  # a natural finish never force-evicts


def test_lane_cancels_and_evicts_when_channel_ownership_is_lost() -> None:
    """Preemption: ownership of a claimed channel is lost before the goto's end.

    The lane emits ``goto.cancelled`` and force-evicts the goto (so it can never
    regain the channel and resume half-way), then never resumes it.
    """
    lane = GotoLane()
    gid = lane.submit(GotoSpec(head={"pitch": 10.0}, duration=5.0))
    lane.on_tick(FakeCtx(0.0, 1, {"head": None}))  # admit
    lane.on_tick(FakeCtx(0.1, 2, {"head": gid}))  # running (owns head)

    ctx = FakeCtx(0.2, 3, {"head": "blocker-1"})  # a higher-priority behavior seized head
    lane.on_tick(ctx)
    cancelled = [e for e in ctx.events if e["type"] == EVENT_CANCELLED]
    assert len(cancelled) == 1
    assert cancelled[0]["id"] == gid
    assert cancelled[0]["reason"] == "preempted"
    assert cancelled[0]["owner"] == "blocker-1"  # names who seized the channel
    assert cancelled[0]["channel"] == "head"
    assert ctx.evicted == [gid]  # force-evicted so it can NEVER resume half-way

    # Even once the channel is free again, the cancelled goto never comes back.
    resumed = FakeCtx(5.0, 4, {"head": None})
    lane.on_tick(resumed)
    assert resumed.admitted == [] and resumed.events == []


def test_lane_cancel_reason_is_evicted_when_no_new_owner() -> None:
    lane = GotoLane()
    gid = lane.submit(GotoSpec(head={"pitch": 10.0}, duration=5.0))
    lane.on_tick(FakeCtx(0.0, 1, {"head": None}))
    lane.on_tick(FakeCtx(0.1, 2, {"head": gid}))
    ctx = FakeCtx(0.2, 3, {"head": None})  # goto gone, channel unclaimed (evicted)
    lane.on_tick(ctx)
    cancelled = [e for e in ctx.events if e["type"] == EVENT_CANCELLED]
    assert len(cancelled) == 1
    assert cancelled[0]["reason"] == "evicted"
    assert cancelled[0]["owner"] is None


def test_lane_busy_until_is_the_serial_horizon() -> None:
    lane = GotoLane()
    assert lane.busy_until(5.0) == pytest.approx(5.0)  # idle + empty -> now
    lane.submit(GotoSpec(head={"pitch": 1.0}, duration=2.0))
    lane.submit(GotoSpec(head={"pitch": 2.0}, duration=3.0))
    assert lane.busy_until(0.0) == pytest.approx(5.0)  # 2 + 3 queued
    lane.on_tick(FakeCtx(0.0, 1, {"head": None}))  # admit first (ends at 2.0), one pending (3.0)
    assert lane.busy_until(0.0) == pytest.approx(5.0)  # max(0, 2) + 3
    assert lane.busy_until(1.0) == pytest.approx(5.0)  # max(1, 2) + 3
    assert lane.busy_until(2.5) == pytest.approx(5.5)  # max(2.5, 2) + 3


def test_lane_admitted_event_shape() -> None:
    lane = GotoLane()
    gid = lane.submit(GotoSpec(head={"pitch": 10.0}, body_yaw=5.0, duration=1.5, label="hi"))
    ctx = FakeCtx(0.7, 9, {"head": None})
    lane.on_tick(ctx)
    ev = ctx.events[0]
    assert ev["type"] == EVENT_ADMITTED
    assert ev["id"] == gid and ev["label"] == "hi"
    assert ev["duration"] == 1.5
    assert ev["channels"] == ["body_yaw", "head"]
    assert ev["ts"] == 0.7 and ev["tick"] == 9


# --------------------------------------------------------------------------- #
# Deterministic engine integration harness                                    #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.poses = []

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


class _InjectAt:
    """A one-shot seam driver that admits ``behavior`` once, at/after ``at_tick``."""

    def __init__(self, at_tick, behavior):
        self._at = at_tick
        self._beh = behavior
        self._done = False

    def __call__(self, ctx):
        if not self._done and ctx.tick >= self._at:
            ctx.admit(self._beh)
            self._done = True


def _run_engine(drivers, consumers=(), *, engine=None, max_ticks=40):
    tr = _FakeTransport()
    bus = TickBus(drivers=list(drivers), consumers=list(consumers))
    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=max_ticks,
        engine=engine,
        tick_seam=bus,
    )
    return tr


# --------------------------------------------------------------------------- #
# Engine integration — the lane rides the real seam                           #
# --------------------------------------------------------------------------- #


def test_engine_goto_is_admitted_runs_the_head_then_completes() -> None:
    events: list[dict] = []
    lane = GotoLane()
    lane.submit(GotoSpec(head={"pitch": 10.0}, duration=0.2, label="hi"))
    eng = Engine()
    tr = _run_engine([lane], [events.append], engine=eng, max_ticks=40)
    types = [e["type"] for e in events]
    assert EVENT_ADMITTED in types
    assert EVENT_DONE in types
    assert EVENT_CANCELLED not in types
    # The goto actually drove the head toward its target while in flight...
    pitches = [p["head"]["pitch"] for p in tr.sink.poses]
    assert max(pitches) > 8.0 and max(pitches) <= 10.5
    # ...and left no goto behavior behind once it completed.
    assert all(ab.behavior.name != "goto" for ab in eng.active)


def test_engine_stopping_behavior_preempts_in_flight_goto_no_resume() -> None:
    """Acceptance 1: a higher-priority STOPPING behavior interrupts the goto,
    which is cancelled and never resumes (a stopping admit evicts the stoppable
    goto outright)."""
    events: list[dict] = []
    lane = GotoLane()
    lane.submit(GotoSpec(head={"pitch": 10.0}, duration=5.0))
    blocker = build_behavior(
        "gaze-hold",
        {"yaw": 18.0, "pitch": 10.0, "roll": 0.0, "z": 0.0},
        StopClass.STOPPING,
        Lifetime(looping=False, duration=1.0),
        "blocker-1",
    )
    eng = Engine()
    _run_engine([lane, _InjectAt(4, blocker)], [events.append], engine=eng, max_ticks=100)
    cancelled = [e for e in events if e["type"] == EVENT_CANCELLED]
    assert len(cancelled) == 1
    assert cancelled[0]["owner"] == "blocker-1" and cancelled[0]["reason"] == "preempted"
    assert all(e["type"] != EVENT_DONE for e in events)  # never completed
    assert len([e for e in events if e["type"] == EVENT_ADMITTED]) == 1  # never re-admitted
    assert all(ab.behavior.name != "goto" for ab in eng.active)  # gone for good


def test_engine_unstoppable_blocker_cancels_goto_and_it_never_resumes_half_way() -> None:
    """Acceptance 1, the hard case: an UNSTOPPABLE blocker does NOT evict the
    stoppable goto on admit — it only out-prioritises it per tick. Without the
    lane force-evicting on ownership loss, the goto would resume when the (short)
    blocker expires, mid-way through its motion. The lane must prevent exactly
    that: head ownership never returns to the goto after cancellation."""
    events: list[dict] = []
    owners: list[tuple[int, object]] = []
    lane = GotoLane()
    gid = lane.submit(GotoSpec(head={"pitch": 10.0}, duration=5.0))
    blocker = build_behavior(
        "gaze-hold",
        {"yaw": 18.0, "pitch": 10.0, "roll": 0.0, "z": 0.0},
        StopClass.UNSTOPPABLE,
        Lifetime(looping=False, duration=0.3),  # expires long before the goto would
        "blk",
    )
    recorder = lambda ctx: owners.append((ctx.tick, ctx.ownership.get("head")))  # noqa: E731
    eng = Engine()
    _run_engine([lane, _InjectAt(4, blocker), recorder], [events.append], engine=eng, max_ticks=120)
    cancelled = [e for e in events if e["type"] == EVENT_CANCELLED]
    assert len(cancelled) == 1 and cancelled[0]["id"] == gid
    cancel_tick = cancelled[0]["tick"]
    # The blocker was short (0.3 s) and expired well within the run, yet the goto
    # never regains the head channel and never re-admits — no half-way resume.
    after_cancel = [owner for (tick, owner) in owners if tick > cancel_tick]
    assert gid not in after_cancel
    assert all(ab.behavior.id != gid for ab in eng.active)
    assert all(e["type"] != EVENT_DONE for e in events)


# --------------------------------------------------------------------------- #
# GotoLaneAdapter — MotionQueue-shaped facade, serial submit + busy horizon    #
# --------------------------------------------------------------------------- #


def test_adapter_accepts_motionaction_and_mirrors_the_queue_surface() -> None:
    from reachy.motion.queue import MotionAction

    adapter = GotoLaneAdapter()
    ids = [
        adapter.submit(MotionAction(label=f"m{i}", head={"pitch": 5.0 * i}, duration=0.2))
        for i in (1, 2, 3)
    ]
    assert all(isinstance(gid, str) for gid in ids) and len(set(ids)) == 3  # submit -> distinct id
    assert len(adapter) == 3  # __len__ like MotionQueue
    assert [s.label for s in adapter.pending()] == ["m1", "m2", "m3"]  # FIFO, oldest-first
    assert adapter.busy_until(0.0) == pytest.approx(0.6)  # serial horizon: 3 x 0.2


def test_adapter_runs_serial_fifo_end_to_end_in_a_bounded_engine_run() -> None:
    """Acceptance 2: MotionQueue-family callers reach the engine lane through the
    adapter with unchanged serial (one-at-a-time, FIFO) semantics + a busy
    horizon, proven in a bounded deterministic run."""
    from reachy.motion.queue import MotionAction

    events: list[dict] = []
    adapter = GotoLaneAdapter()
    ids = [
        adapter.submit(MotionAction(label=f"m{i}", head={"pitch": 5.0 * i}, duration=0.2))
        for i in (1, 2, 3)
    ]
    eng = Engine()
    _run_engine([adapter.lane], [events.append], engine=eng, max_ticks=80)

    # admitted / done strictly alternate in submit order -> exactly one in flight
    # at a time, drained FIFO.
    seq = [(e["type"], e.get("id")) for e in events if e["type"] in (EVENT_ADMITTED, EVENT_DONE)]
    assert seq == [
        (EVENT_ADMITTED, ids[0]),
        (EVENT_DONE, ids[0]),
        (EVENT_ADMITTED, ids[1]),
        (EVENT_DONE, ids[1]),
        (EVENT_ADMITTED, ids[2]),
        (EVENT_DONE, ids[2]),
    ]
    assert all(e["type"] != EVENT_CANCELLED for e in events)  # nothing preempts them
    assert len(adapter) == 0  # queue drained
    assert adapter.busy_until(100.0) == pytest.approx(100.0)  # idle again
