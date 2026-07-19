"""Explicit behavior-completion semantics at the generic engine boundary."""

from __future__ import annotations

import contextlib

from reachy.behavior import engine as E
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.model import Behavior, Contribution, Lifetime, StopClass


def _behavior(
    *,
    behavior_id: str,
    name: str = "finite-reaction",
    done: bool = False,
    head_yaw: float | None = 12.0,
) -> Behavior:
    head = None
    if head_yaw is not None:
        head = {
            "x": 0.0,
            "y": 0.0,
            "z": 0.0,
            "roll": 0.0,
            "pitch": 0.0,
            "yaw": head_yaw,
        }
    return Behavior(
        id=behavior_id,
        name=name,
        channels=frozenset({"head"}),
        stop_class=StopClass.STOPPABLE,
        lifetime=Lifetime(looping=True, duration=None),
        params={},
        fn=lambda _t, _params, _sense: Contribution(head=head, done=done),
    )


def test_contribution_completion_is_explicit_and_defaults_false() -> None:
    assert Contribution().done is False
    assert Contribution(done=True).done is True


def test_completed_behavior_is_removed_before_same_tick_arbitration() -> None:
    engine = Engine()
    base_id = engine.seed_base_layer(now=0.0, energy=0.0)
    engine.admit_behavior(_behavior(behavior_id="reaction-1", done=True), now=0.0)

    tick = engine.compose_tick(0.1)

    assert tick["completed"] == ["reaction-1"]
    assert tick["expired"] == []
    assert tick["ownership"]["head"] == base_id
    assert [active.behavior.id for active in engine.active] == [base_id]


def test_abstaining_contribution_remains_active() -> None:
    engine = Engine()
    base_id = engine.seed_base_layer(now=0.0, energy=0.0)
    engine.admit_behavior(
        _behavior(behavior_id="waiting-1", done=False, head_yaw=None),
        now=0.0,
    )

    tick = engine.compose_tick(0.1)

    assert tick["completed"] == []
    assert tick["ownership"]["head"] == base_id
    assert {active.behavior.id for active in engine.active} == {base_id, "waiting-1"}


class _Sink:
    def set_target(self, **_pose):
        return {"status": "ok"}


class _Transport:
    name = "fake"

    @contextlib.contextmanager
    def streaming(self):
        yield _Sink()


class _Clock:
    def __init__(self):
        self.t = 0.0

    def __call__(self):
        self.t += 0.02
        return self.t


def test_completed_name_is_absent_before_seam_can_readmit_it() -> None:
    engine = Engine()
    active_names_by_tick: dict[int, set[str]] = {}
    admissions: list[str] = []

    def seam(ctx) -> None:
        active_names_by_tick[ctx.tick] = ctx.active_names()
        if ctx.tick == 1:
            ctx.admit(_behavior(behavior_id="reaction-first", done=True))
        elif ctx.tick == 2 and "finite-reaction" not in ctx.active_names():
            outcome = ctx.admit(_behavior(behavior_id="reaction-second", done=False))
            admissions.append(outcome["id"])

    ticks = E.run(
        _Transport(),
        EngineConfig(base_layer=True, settle=False),
        sleep=lambda _seconds: None,
        now=_Clock(),
        max_ticks=3,
        engine=engine,
        tick_seam=seam,
    )

    assert ticks == 3
    assert "finite-reaction" not in active_names_by_tick[2]
    assert admissions == ["reaction-second"]
    assert "finite-reaction" in active_names_by_tick[3]
