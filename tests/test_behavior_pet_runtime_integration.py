"""End-to-end offline traces for the persistent pat reaction runtime."""

from __future__ import annotations

import contextlib
from dataclasses import dataclass

import pytest

from reachy.behavior import engine as engine_mod
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.pat_sense import PatSenseDriver as RealPatSenseDriver
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig
from reachy.cli._commands import behavior as behavior_mod
from reachy.motion.pat import PatDetector

pytestmark = pytest.mark.offline

DT = 0.02
REACTION_PREFIX = "rule:pat-to-pet-reaction:"
#: The react rule's cooldown, defined once and used both to build the rules
#: config below and to assert the spacing between repeat admissions.
RULE_COOLDOWN_S = 5.0


class _TrackingSink:
    def __init__(self) -> None:
        self.targets: list[dict] = []

    def set_target(self, *, head, antennas, body_yaw):
        self.targets.append(
            {
                "head": dict(head),
                "antennas": tuple(antennas),
                "body_yaw": float(body_yaw),
            }
        )
        return {"ok": True}

    @property
    def tick(self) -> int:
        # The first target is engine.run's neutral connectivity preflight.
        return max(0, len(self.targets) - 1)

    @property
    def commanded_pitch_yaw(self) -> tuple[float, float]:
        if not self.targets:
            return (0.0, 0.0)
        head = self.targets[-1]["head"]
        return (head["pitch"], head["yaw"])


class _TrackingTransport:
    name = "tracking-fake"

    def __init__(self) -> None:
        self.sink = _TrackingSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


class _MirroredPatReader:
    """Track command perfectly and inject labelled robot-frame yaw presses."""

    def __init__(
        self,
        sink: _TrackingSink,
        sign: int,
        *,
        fail_from_tick: int | None = None,
    ) -> None:
        self._sink = sink
        self._sign = sign
        self._fail_from_tick = fail_from_tick
        self.connected = False
        self.closed = False

    def warm_up(self) -> bool:
        """t28's setup-thread connect. Present because the composition warms its
        holders unconditionally — a fake without it is not a held reader."""
        self.connected = True
        return True

    def read(self) -> tuple[float, float] | None:
        pitch, yaw = self._sink.commanded_pitch_yaw
        tick = self._sink.tick
        if self._fail_from_tick is not None and tick >= self._fail_from_tick:
            return None
        initial_edge = tick in {28, 30}
        sustained_edge = 60 <= tick < 600 and tick % 20 == 0
        external_yaw = 3.0 * self._sign if initial_edge or sustained_edge else 0.0
        return (pitch, yaw + external_yaw)

    def close(self) -> None:
        self.closed = True


class _StepClock:
    def __init__(self, on_step=None) -> None:
        self.t = 0.0
        self._on_step = on_step

    def __call__(self) -> float:
        self.t += DT
        if self._on_step is not None:
            self._on_step()
        return self.t


class _StaticRules:
    def __init__(self) -> None:
        config = RulesConfig.from_dict(
            {
                "react": [
                    {
                        "id": "pat-to-pet-reaction",
                        "when": {"field": "pat", "op": "is_true"},
                        "run": "pet-reaction",
                        "cooldown_s": RULE_COOLDOWN_S,
                    }
                ]
            }
        )
        self._engine = RuleEngine(config)

    def __call__(self, ctx) -> None:
        self._engine(ctx)

    def set_active_mode(self, name: str | None) -> None:
        self._engine.set_active_mode(name)

    @staticmethod
    def known_modes() -> tuple[str, ...]:
        return ()


@dataclass
class _Run:
    sign: int
    events: list[dict]
    ticks: list[dict]
    targets: list[dict]
    engine: Engine
    reader: _MirroredPatReader


def _run_side(
    monkeypatch,
    sign: int,
    *,
    fail_from_tick: int | None = None,
    stop_at_tick: int | None = None,
    max_ticks: int = 960,
) -> _Run:
    transport = _TrackingTransport()
    reader = _MirroredPatReader(
        transport.sink,
        sign,
        fail_from_tick=fail_from_tick,
    )
    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.setattr(behavior_mod, "_make_state_reader", lambda: reader)

    def make_driver(**kwargs):
        detector = PatDetector(
            press_threshold=0.5,
            release_threshold=0.2,
            yaw_press_threshold=0.5,
            yaw_release_threshold=0.2,
            min_presses=2,
            pat_cooldown=0.0,
            interaction_gap_timeout=30.0,
            baseline_alpha=0.0,
            level2_threshold_fn=lambda: 100.0,
        )
        # `kwargs` carries `still_hold_s`/`still_eps` from `_compose_run_seam`
        # (t2's tuning surface). This test is about the REACTION chain and side
        # direction, not about whichever gate tuning happens to ship, so pin the
        # gate explicitly instead of inheriting it: the synthetic trace below is
        # timed against a 0.5 s hold, and inheriting the v0.41.0 swing-era
        # defaults (1.0 / 0.035) silently retimed it until nothing was admitted
        # at all. Pinning keeps this test measuring what it names.
        kwargs = {**kwargs, "still_hold_s": 0.5, "still_eps": 0.01}
        return RealPatSenseDriver(
            **kwargs,
            detector=detector,
            lag_tau=0.0,
            hp_tau=0.0,
            warmup_s=0.0,
            enough_after_fn=lambda: 9.0,
        )

    monkeypatch.setattr(behavior_mod, "PatSenseDriver", make_driver)
    config = EngineConfig(compose_hz=50.0, base_layer=True, energy=0.0, settle=False)
    events: list[dict] = []
    ticks: list[dict] = []
    sense, seam, held_reader = behavior_mod._compose_run_seam(
        transport,
        config,
        _StaticRules(),
        events.append,
    )
    engine = Engine()
    stopped = False

    def stop_reaction() -> None:
        nonlocal stopped
        if stop_at_tick is None or stopped or transport.sink.tick < stop_at_tick:
            return
        engine.stop("pet-reaction")
        stopped = True

    try:
        engine_mod.run(
            transport,
            config,
            engine=engine,
            now=_StepClock(stop_reaction),
            sleep=lambda *_: None,
            max_ticks=max_ticks,
            sense=sense,
            tick_seam=seam,
            emit=ticks.append,
        )
    finally:
        if held_reader is not None:
            held_reader.close()
    return _Run(sign, events, ticks, transport.sink.targets, engine, reader)


@pytest.mark.parametrize("sign", [1, -1], ids=["robot-left", "robot-right"])
def test_labelled_side_trace_reacts_holds_reacquires_and_completes(monkeypatch, sign) -> None:
    run = _run_side(monkeypatch, sign)

    sense_events = [event for event in run.events if event.get("type") == "sense"]
    legacy = [event["pat"] for event in sense_events if event.get("pat")]
    # The reaction ownership edge ends the detector interaction. Continued
    # post-handoff presses may therefore form a fresh level1, but can never
    # reuse the pre-handoff clock as level2; the rule cooldown still admits
    # exactly one reaction below.
    assert legacy
    assert all(event == ["side_pat", "level1"] for event in legacy)
    signed_states = [
        event["pat_state"]
        for event in sense_events
        if event.get("pat_state", {}).get("yaw_deg") is not None
    ]
    assert signed_states
    assert all(state["yaw_deg"] * sign > 0.0 for state in signed_states)

    rule_fires = [event for event in run.events if event.get("type") == "rule.fire"]
    # Only pet-reaction is ever admitted, and repeat admissions are COOLDOWN-SPACED.
    #
    # This previously asserted exactly one fire, which was an artifact of the old
    # SIDE_HEAD_GAIN: raising it lengthened the reaction's slew and shifted where
    # the trace's continuing injected presses land, yielding a second legitimate
    # admission inside these 19.2 s. Pinning the count made a gain change look
    # like a regression. The cooldown is the actual contract, so assert that.
    #
    # This can never mask the #66 self-retrigger class: _MirroredPatReader tracks
    # the commanded pose EXACTLY, so the robot's own reaction motion contributes
    # zero deviation by construction. A runaway would also violate the spacing.
    assert rule_fires, "the pat never admitted a reaction"
    assert {event.get("behavior") for event in rule_fires} == {"pet-reaction"}
    fire_times = [event["ts"] for event in rule_fires]
    gaps = [later - earlier for earlier, later in zip(fire_times, fire_times[1:])]
    assert all(gap >= RULE_COOLDOWN_S for gap in gaps), fire_times

    ownership = [tick["ownership"]["head"] for tick in run.ticks]
    reaction_ticks = [
        index
        for index, owner in enumerate(ownership, start=1)
        if isinstance(owner, str) and owner.startswith(REACTION_PREFIX)
    ]
    assert reaction_ticks
    reaction_poses = [run.targets[index] for index in reaction_ticks]
    settled = reaction_poses[40:]
    # The SENSE carries the push direction (asserted above); the REACTION opposes
    # it, pressing back toward the hand rather than following the shove. So the
    # settled pose must land on the sign opposite the labelled trace's.
    assert max(pose["head"]["yaw"] * -sign for pose in settled) > 2.0
    assert max(pose["body_yaw"] * -sign for pose in settled) > 1.0

    states = [event.get("pat_state") for event in sense_events]
    phases = {state["phase"] for state in states if state is not None}
    assert {"receptive", "contentment", "warning", "enough", "cooldown"} <= phases
    assert any(state and state["availability"] == "blocked" for state in states)
    assert any(state and state["availability"] == "available" for state in states)

    # The contentment/warning antenna moves each close sensing, but the real
    # driver regains 0.5-second stillness before the reaction's 1-second loss
    # grace. It therefore reaches `enough`, completes once, and returns all
    # channels to the single passive base without a cooldown re-admission.
    assert ownership[-1].startswith("feel-alive-")
    assert {active.behavior.name for active in run.engine.active} == {"feel-alive"}
    assert run.reader.closed is True


def test_reader_failure_after_admission_finishes_without_claiming_release(monkeypatch) -> None:
    run = _run_side(monkeypatch, 1, fail_from_tick=70, max_ticks=300)

    sense_events = [event for event in run.events if event.get("type") == "sense"]
    states = [event["pat_state"] for event in sense_events if event.get("pat_state")]
    assert any(state["availability"] == "unavailable" for state in states)
    assert not any(state["phase"] == "released" for state in states)
    assert [event.get("behavior") for event in run.events if event.get("type") == "rule.fire"] == [
        "pet-reaction"
    ]
    assert {active.behavior.name for active in run.engine.active} == {"feel-alive"}
    assert run.ticks[-1]["ownership"]["head"].startswith("feel-alive-")


def test_explicit_stop_drops_the_reaction_and_all_channel_owners(monkeypatch) -> None:
    run = _run_side(monkeypatch, 1, stop_at_tick=80, max_ticks=180)

    assert any(
        isinstance(tick["ownership"]["head"], str)
        and tick["ownership"]["head"].startswith(REACTION_PREFIX)
        for tick in run.ticks
    )
    assert {active.behavior.name for active in run.engine.active} == {"feel-alive"}
    final_owners = run.ticks[-1]["ownership"]
    assert len(set(final_owners.values())) == 1
    assert final_owners["head"].startswith("feel-alive-")


def test_composition_source_has_one_pat_driver_and_no_dynamic_rule_parameters() -> None:
    # The invariant this tripwire guards is unchanged by t28's provider wiring:
    # the two pat views must come from ONE driver (one held reader, one
    # detector), never a second reader opened per view. Only the wording moved
    # when the docstring grew to describe all six composed providers, so the
    # match is on reflowed text rather than one hard-wrapped line.
    source = " ".join((behavior_mod._compose_run_seam.__doc__ or "").split())
    assert "``pat_event`` / ``pat_state`` — two PEEKs of the ONE" in source
    assert ":class:`PatSenseDriver`" in source
    assert "one held reader and detector" in source

    config = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "pat-to-pet-reaction",
                    "when": {"field": "pat", "op": "is_true"},
                    "run": "pet-reaction",
                }
            ]
        }
    )
    rule = config.react[0]
    assert rule.params == {}
    assert rule.duration_s is None
