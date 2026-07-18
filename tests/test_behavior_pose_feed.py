"""The composed-pose seam: ``TickContext.pose`` + ``LastPoseHolder``.

Task t1: the engine's per-tick seam (:mod:`reachy.behavior.engine`) exposes
ownership and perception but, until now, never the pose it actually composed
and streamed — so a seam rider had no way to answer "where is the robot right
now" without re-deriving it from ownership + contributions itself. This showed
up concretely in :mod:`reachy.behavior.goto_lane`: a goto with no injected
``start_pose_provider`` interpolates from neutral, so it snaps any
already-offset channel to zero at ``t=0`` before easing to its target (the
"Start pose" limitation documented in that module's docstring).

These tests pin two things:

1. ``TickContext.pose`` (:mod:`reachy.behavior.engine`) — the complete dict the
   engine streamed THIS tick, populated after streaming, additive to every
   existing seam rider (``tests/test_behavior_engine_seam.py`` covers the
   field itself; this file assumes it exists).
2. :mod:`reachy.behavior.pose_feed` — :class:`LastPoseHolder` (a ``TickBus``
   driver that stashes ``ctx.pose``) and its
   :meth:`~reachy.behavior.pose_feed.LastPoseHolder.as_start_pose_provider`
   adapter, which closes the loop with
   :class:`~reachy.behavior.goto_lane.GotoLane`: a goto wired to read the
   holder's live pose interpolates from it instead of snapping to neutral.

Deterministic throughout: a fake in-memory streaming sink plus the engine's own
injectable ``sleep`` / ``now`` / ``max_ticks`` seams, or a hand-built
``TickContext``-shaped fake for lane-only unit tests. No robot, daemon, or
network anywhere.
"""

from __future__ import annotations

import contextlib
from types import SimpleNamespace

import pytest

from reachy.behavior import engine as E
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.goto_lane import (
    EVENT_ADMITTED,
    GotoLane,
    GotoSpec,
)
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass, neutral_head
from reachy.behavior.pose_feed import LastPoseHolder
from reachy.behavior.rule_engine import TickBus

# --------------------------------------------------------------------------- #
# Shared fakes (self-contained, mirroring the other behavior-engine test      #
# files rather than importing across test modules)                           #
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


class _FakeAdmitCtx:
    """A minimal ``TickContext``-shaped fake giving a test control over admits.

    Mirrors ``tests/test_behavior_goto_lane.py``'s ``FakeCtx`` (kept local so
    this file stays self-contained): ``emit`` / ``admit`` / ``evict`` just
    record their calls, ``ownership`` is set directly by the test.
    """

    def __init__(self, now=0.0, tick=1, ownership=None):
        self.now = now
        self.tick = tick
        self.ownership = dict(ownership or {})
        self.events: list[dict] = []
        self.admitted: list = []
        self.evicted: list = []

    def emit(self, event):
        self.events.append(event)

    def admit(self, behavior):
        self.admitted.append(behavior)
        return {"ok": True, "id": behavior.id}

    def evict(self, name_or_id):
        self.evicted.append(name_or_id)
        return {"ok": True, "target": name_or_id}


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


class _SubmitAt:
    """A one-shot seam driver that submits ``spec`` to ``lane`` at/after ``at_tick``."""

    def __init__(self, at_tick, lane, spec):
        self._at = at_tick
        self._lane = lane
        self._spec = spec
        self._done = False

    def __call__(self, ctx):
        if not self._done and ctx.tick >= self._at:
            self._lane.submit(self._spec)
            self._done = True


def _hold_behavior(pitch: float, behavior_id: str = "hold-1") -> Behavior:
    """A looping STOPPABLE behavior that holds a constant off-neutral head pitch."""

    def fn(_t_local, _params, _sense):
        head = neutral_head()
        head["pitch"] = pitch
        return Contribution(head=head)

    return Behavior(
        id=behavior_id,
        name="hold",
        channels=frozenset({"head"}),
        stop_class=StopClass.STOPPABLE,
        lifetime=Lifetime(looping=True, duration=None),
        params={},
        fn=fn,
    )


# --------------------------------------------------------------------------- #
# LastPoseHolder — stash / peek                                               #
# --------------------------------------------------------------------------- #


def test_peek_returns_none_before_any_tick() -> None:
    holder = LastPoseHolder()
    assert holder.peek() is None


def test_holder_stashes_ctx_pose_on_each_call() -> None:
    holder = LastPoseHolder()
    pose_1 = {"head": {"pitch": 1.0}, "antennas": (0.0, 0.0), "body_yaw": 0.0}
    pose_2 = {"head": {"pitch": 2.0}, "antennas": (1.0, -1.0), "body_yaw": 3.0}
    holder(SimpleNamespace(pose=pose_1))
    assert holder.peek() == pose_1
    holder(SimpleNamespace(pose=pose_2))
    assert holder.peek() == pose_2  # latest stash wins


def test_holder_degrades_to_noop_when_ctx_has_no_pose_attribute() -> None:
    holder = LastPoseHolder()
    holder(SimpleNamespace(tick=1))  # no .pose at all -> must never raise
    assert holder.peek() is None  # stays "nothing stashed yet"


def test_holder_degrades_to_noop_when_ctx_pose_is_none() -> None:
    holder = LastPoseHolder()
    good = {"head": {"pitch": 5.0}, "antennas": (0.0, 0.0), "body_yaw": 0.0}
    holder(SimpleNamespace(pose=good))
    holder(SimpleNamespace(pose=None))  # a transient None must not clobber the last good pose
    assert holder.peek() == good


def test_holder_usable_directly_as_a_bare_tick_seam() -> None:
    """The holder is a plain ``callable(ctx) -> None`` -- valid as ``tick_seam`` itself."""
    holder = LastPoseHolder()
    tr = _FakeTransport()
    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=3,
        tick_seam=holder,
    )
    pose = holder.peek()
    assert pose is not None
    assert set(pose["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}
    assert isinstance(pose["antennas"], tuple) and len(pose["antennas"]) == 2
    assert isinstance(pose["body_yaw"], float)
    assert pose == tr.sink.poses[-1]  # exactly the last tick's streamed pose


# --------------------------------------------------------------------------- #
# as_start_pose_provider -- the Contribution adapter                          #
# --------------------------------------------------------------------------- #


def test_provider_returns_neutral_contribution_before_any_stash() -> None:
    holder = LastPoseHolder()
    provider = holder.as_start_pose_provider()
    c = provider()
    assert isinstance(c, Contribution)
    assert c.head is None and c.antennas is None and c.body_yaw is None


def test_provider_converts_the_stashed_pose_dict_to_a_contribution() -> None:
    holder = LastPoseHolder()
    pose = {
        "head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 8.0, "yaw": 0.0},
        "antennas": (3.0, -3.0),
        "body_yaw": 5.0,
    }
    holder(SimpleNamespace(pose=pose))
    c = holder.as_start_pose_provider()()
    assert c.head == pose["head"]
    assert c.antennas == pytest.approx(pose["antennas"])
    assert c.body_yaw == pytest.approx(pose["body_yaw"])


def test_provider_reflects_the_latest_stash_not_the_first() -> None:
    holder = LastPoseHolder()
    holder(SimpleNamespace(pose={"head": {"pitch": 1.0}, "antennas": (0.0, 0.0), "body_yaw": 0.0}))
    holder(SimpleNamespace(pose={"head": {"pitch": 9.0}, "antennas": (0.0, 0.0), "body_yaw": 0.0}))
    provider = holder.as_start_pose_provider()
    assert provider().head["pitch"] == pytest.approx(9.0)


# --------------------------------------------------------------------------- #
# GotoLane wired with the holder -- interpolates from the live pose           #
# --------------------------------------------------------------------------- #


def test_goto_lane_with_holder_provider_starts_from_the_stashed_pose_not_neutral() -> None:
    """Acceptance: no C0 snap-to-neutral -- the goto's t=0 contribution equals
    the live pose the holder last stashed, not the library's neutral default."""
    holder = LastPoseHolder()
    stashed = {
        "head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 8.0, "yaw": 0.0},
        "antennas": (3.0, -3.0),
        "body_yaw": 5.0,
    }
    holder(SimpleNamespace(pose=stashed))  # a prior tick already stashed the live pose

    lane = GotoLane(start_pose_provider=holder.as_start_pose_provider())
    lane.submit(GotoSpec(head={"pitch": 20.0}, antennas=(10.0, -10.0), body_yaw=15.0, duration=2.0))

    ctx = _FakeAdmitCtx(ownership={"head": None, "antennas": None, "body_yaw": None})
    lane.on_tick(ctx)

    assert [e["type"] for e in ctx.events] == [EVENT_ADMITTED]
    goto = ctx.admitted[0]
    c0 = goto.contribution(0.0)
    # Starts from the LIVE pose, not neutral (0.0) -> no discontinuity at t=0.
    assert c0.head["pitch"] == pytest.approx(8.0)
    assert c0.antennas == pytest.approx((3.0, -3.0))
    assert c0.body_yaw == pytest.approx(5.0)
    # ...and still lands cleanly on the goto's own target at its duration.
    c_end = goto.contribution(2.0)
    assert c_end.head["pitch"] == pytest.approx(20.0)
    assert c_end.antennas == pytest.approx((10.0, -10.0))
    assert c_end.body_yaw == pytest.approx(15.0)


def test_goto_lane_with_holder_provider_falls_back_to_neutral_before_any_stash() -> None:
    """Before any tick has stashed a pose the holder-wired lane behaves exactly
    like a bare ``GotoLane()`` -- the same honest neutral default, not a crash."""
    holder = LastPoseHolder()  # nothing stashed yet
    lane = GotoLane(start_pose_provider=holder.as_start_pose_provider())
    lane.submit(GotoSpec(head={"pitch": 20.0}, duration=2.0))

    ctx = _FakeAdmitCtx(ownership={"head": None, "antennas": None, "body_yaw": None})
    lane.on_tick(ctx)

    goto = ctx.admitted[0]
    assert goto.contribution(0.0).head["pitch"] == pytest.approx(0.0)  # neutral start


# --------------------------------------------------------------------------- #
# End-to-end: a live engine run proves the streamed pose never snaps to zero  #
# --------------------------------------------------------------------------- #


def test_engine_goto_via_holder_takes_over_from_the_live_pose_no_neutral_snap() -> None:
    """A ``hold`` behavior drives head to pitch=8 (off-neutral); a goto submitted
    later, wired to the live pose through ``LastPoseHolder``, takes over head
    WITHOUT the streamed pitch ever dipping toward neutral first."""
    holder = LastPoseHolder()
    lane = GotoLane(start_pose_provider=holder.as_start_pose_provider())
    hold = _hold_behavior(8.0)
    goto_spec = GotoSpec(head={"pitch": 20.0}, duration=0.3)

    bus = TickBus(drivers=[_InjectAt(1, hold), holder, _SubmitAt(5, lane, goto_spec), lane])
    tr = _FakeTransport()
    eng = Engine()
    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=60,
        engine=eng,
        tick_seam=bus,
    )

    pitches = [p["head"]["pitch"] for p in tr.sink.poses]
    # poses[0] is the preflight neutral pose; the hold behavior owns head from
    # tick 2 onward (admitted during tick 1's seam call).
    settled = pitches[2:]
    assert min(settled) >= 7.0  # never dips toward neutral once hold/goto own head
    assert max(settled) == pytest.approx(20.0, abs=0.5)  # the goto still reaches its target


def test_engine_goto_without_provider_shows_the_neutral_snap_for_contrast() -> None:
    """Contrast case: a bare ``GotoLane()`` (no ``start_pose_provider``) DOES
    snap to neutral at ``t=0`` -- the documented limitation the holder fixes."""
    lane = GotoLane()  # no provider -> neutral-relative start (the documented limitation)
    hold = _hold_behavior(8.0)
    goto_spec = GotoSpec(head={"pitch": 20.0}, duration=0.3)

    bus = TickBus(drivers=[_InjectAt(1, hold), _SubmitAt(5, lane, goto_spec), lane])
    tr = _FakeTransport()
    eng = Engine()
    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=20,
        engine=eng,
        tick_seam=bus,
    )

    pitches = [p["head"]["pitch"] for p in tr.sink.poses]
    settled = pitches[2:]
    # Without a live-pose provider the goto starts its minjerk from neutral, so
    # the streamed pitch drops from 8.0 toward 0 right at admission.
    assert min(settled) < 4.0
