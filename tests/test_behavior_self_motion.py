"""Tests for the self-motion-conditioned rms floor (#95) — TDD-first, written
before ``reachy/behavior/self_motion.py`` exists.

The deployed robot's rms-driven ``look-toward-sound`` rule self-sustained: its
own actuator noise (head AND antennas) cleared the 0.02 admission floor,
admitted orienting, which made more noise. Measured: a still robot in a quiet
room NEVER crosses 0.02 (1459 samples, max 0.00953); with the runtime running
in the same room the rule genuinely fired 42x in 3 h. The fix under test is a
motion-conditioned floor — NOT a global threshold raise and NOT a binary
inhibit rule:

* :class:`reachy.behavior.self_motion.SelfMotionDriver` — a TickBus driver that
  watches each tick's COMMANDED pose (head x/y/z mm + roll/pitch/yaw deg,
  BOTH antennas deg, body_yaw deg) and latches ``moving`` on any above-eps
  per-tick delta, releasing only after a continuous below-eps tail.
* ``Sense.self_moving`` — a new fed sense field (the t12 pattern), declared in
  ``_COMPOSED_PROVIDER_FIELDS`` in the same change.
* The moving floor in :func:`reachy.behavior.rms_sense.make_rms_provider`:
  while ``self_moving`` and measured rms < the moving floor (env
  ``REACHY_RMS_FLOOR_MOVING``, default infinity), the rms sense reports QUIET
  (0.0) — ``None`` still means "no reading", unchanged.

Acceptance (from the #95 brief):

1. With a moving commanded pose, an rms between 0.02 and the moving floor
   yields a quiet reading; the same rms with a still commanded pose passes
   through unchanged — deterministic, no robot.
2. ``self_moving`` is a fed sense field and a rule keyed on it fires.
3. Suppression logs once per TRANSITION (never per tick), and ``self_moving``
   releases after the ~0.25 s tail once commanded motion stops.
4. A finite ``REACHY_RMS_FLOOR_MOVING`` lets a loud reading pass while moving.
"""

from __future__ import annotations

import ast
import inspect
import logging
import math
from dataclasses import dataclass, field

import numpy as np
import pytest

from reachy.behavior.rms_sense import (
    DEFAULT_MOVING_FLOOR,
    MOVING_FLOOR_ENV,
    make_rms_provider,
)
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import SENSE_FIELDS, RulesConfig
from reachy.behavior.self_motion import (
    DEFAULT_EPS_DEG,
    DEFAULT_EPS_MM,
    DEFAULT_TAIL_S,
    SelfMotionDriver,
)
from reachy.behavior.sense import (
    _COMPOSED_PROVIDER_FIELDS,
    FED_SENSE_FIELDS,
    Sense,
    SenseProviders,
    read_perception,
)
from reachy.motion.rms import compute_rms

pytestmark = pytest.mark.offline

_DT = 0.02  # the engine's 50 Hz tick period


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


@dataclass
class _Ctx:
    """Minimal duck-typed TickContext: the two fields the driver reads."""

    now: float
    pose: object


def _pose(
    *,
    head: dict | None = None,
    antennas: tuple[float, float] = (0.0, 0.0),
    body_yaw: float = 0.0,
) -> dict:
    base = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}
    if head:
        base.update(head)
    return {"head": base, "antennas": tuple(antennas), "body_yaw": body_yaw}


def _tick(driver: SelfMotionDriver, now: float, pose: object) -> None:
    driver(_Ctx(now=now, pose=pose))


def _chunk(level: float, n: int = 320) -> np.ndarray:
    """A constant-amplitude chunk whose rms is exactly *level*."""
    return np.full(n, level, dtype=np.float32)


class _Latch:
    """A hand-driven stand-in for the driver's ``is_moving`` peek."""

    def __init__(self, moving: bool = False) -> None:
        self.moving = moving

    def __call__(self) -> bool:
        return self.moving


# --------------------------------------------------------------------------- #
# 1. SelfMotionDriver — the moving latch over the commanded pose              #
# --------------------------------------------------------------------------- #


class TestSelfMotionDriver:
    def test_a_still_commanded_pose_never_latches_moving(self) -> None:
        driver = SelfMotionDriver()
        for i in range(50):
            _tick(driver, i * _DT, _pose())
        assert driver.is_moving() is False

    def test_a_head_yaw_step_latches_moving(self) -> None:
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 0.5}))  # 0.5 deg >> eps_deg
        assert driver.is_moving() is True

    def test_antenna_motion_alone_latches_moving(self) -> None:
        # The antennas are INCLUDED in the watched axes: in the #95 incident the
        # orienting reaction's antenna sweep was part of the self-noise loop.
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(antennas=(8.0, 0.0)))
        assert driver.is_moving() is True

    def test_body_yaw_motion_alone_latches_moving(self) -> None:
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(body_yaw=1.0))
        assert driver.is_moving() is True

    def test_an_mm_axis_step_latches_with_the_mm_eps(self) -> None:
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"z": DEFAULT_EPS_MM * 3.0}))
        assert driver.is_moving() is True

    def test_sub_eps_jitter_never_latches(self) -> None:
        driver = SelfMotionDriver()
        for i in range(50):
            wiggle = (0.4 * DEFAULT_EPS_DEG) if i % 2 else 0.0
            _tick(driver, i * _DT, _pose(head={"yaw": wiggle}))
        assert driver.is_moving() is False

    def test_moving_releases_after_the_tail_once_commanded_motion_stops(self) -> None:
        """Acceptance 3 (release half): actuator noise stops almost immediately
        when the command does (the holding-servo hum is measured below the 0.02
        floor), so the tail is short — ``moving`` must clear once the commanded
        pose has been below-eps for a continuous ``DEFAULT_TAIL_S``."""
        driver = SelfMotionDriver()
        # 5 ticks of commanded motion...
        for i in range(5):
            _tick(driver, i * _DT, _pose(head={"yaw": i * 1.0}))
        assert driver.is_moving() is True
        last_motion_t = 4 * _DT
        # ... then a dead-still command. The latch must hold through the tail...
        t = last_motion_t
        while t + _DT - last_motion_t < DEFAULT_TAIL_S:
            t += _DT
            _tick(driver, t, _pose(head={"yaw": 4.0}))
            assert driver.is_moving() is True, f"released early at t={t}"
        # ... and release on the first tick at/after the tail.
        t += _DT
        _tick(driver, t, _pose(head={"yaw": 4.0}))
        assert driver.is_moving() is False

    def test_motion_inside_the_tail_re_arms_it(self) -> None:
        driver = SelfMotionDriver(tail_s=0.25)
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 1.0}))
        # quiet for a bit less than the tail...
        for i in range(2, 12):
            _tick(driver, i * _DT, _pose(head={"yaw": 1.0}))
        assert driver.is_moving() is True
        # ... a fresh nudge restamps the motion clock ...
        _tick(driver, 12 * _DT, _pose(head={"yaw": 2.0}))
        # ... so another almost-a-tail of quiet still holds the latch.
        for i in range(13, 24):
            _tick(driver, i * _DT, _pose(head={"yaw": 2.0}))
        assert driver.is_moving() is True

    def test_is_moving_is_a_non_consuming_peek(self) -> None:
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 1.0}))
        assert driver.is_moving() is driver.is_moving() is True

    def test_a_missing_or_malformed_pose_degrades_without_raising(self) -> None:
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, None)  # no pose this tick
        _tick(driver, 2 * _DT, {"head": "not-a-dict"})  # malformed
        driver(object())  # not even TickContext-shaped
        assert driver.is_moving() is False

    def test_a_gap_crossing_step_is_not_motion_evidence(self) -> None:
        # A pose step observed ACROSS a missing-pose gap must not latch: the
        # previous sample is dropped at the gap, so the first sample after it
        # has nothing to delta against (mirrors pat_sense's re-seed contract).
        driver = SelfMotionDriver()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, None)
        _tick(driver, 2 * _DT, _pose(head={"yaw": 30.0}))
        assert driver.is_moving() is False

    def test_module_stays_a_dependency_free_leaf(self) -> None:
        import reachy.behavior.self_motion as mod

        tree = ast.parse(inspect.getsource(mod))
        roots: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                roots.update(alias.name.split(".")[0] for alias in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                roots.add(node.module.split(".")[0])
        forbidden = {"reachy_mini", "cv2", "numpy"}
        assert not (roots & forbidden), f"self_motion.py must stay stdlib-only: {roots & forbidden}"


# --------------------------------------------------------------------------- #
# 2. The moving floor in the rms provider path (acceptance 1 + 4)             #
# --------------------------------------------------------------------------- #


class TestMovingFloor:
    def test_moving_suppresses_an_rms_between_the_still_floor_and_the_moving_floor(self) -> None:
        """Acceptance 1: 0.05 sits between the 0.02 still-floor and the default
        infinite moving floor — while self-moving it reads QUIET (0.0); the
        identical chunk with a still commanded pose passes through unchanged."""
        latch = _Latch(moving=True)
        chunk = _chunk(0.05)
        provider = make_rms_provider(lambda: chunk, moving=latch)
        assert provider() == 0.0

        latch.moving = False
        assert provider() == pytest.approx(compute_rms(chunk))

    def test_the_default_moving_floor_is_infinite(self) -> None:
        assert math.isinf(DEFAULT_MOVING_FLOOR)
        # Even a very loud reading is suppressed while moving at the default.
        provider = make_rms_provider(lambda: _chunk(0.9), moving=_Latch(moving=True))
        assert provider() == 0.0

    def test_none_still_means_no_reading_while_moving(self) -> None:
        # The moving floor reports QUIET (0.0), never "no reading": None keeps
        # meaning "no chunk this tick" on both sides of the gate.
        provider = make_rms_provider(lambda: None, moving=_Latch(moving=True))
        assert provider() is None

    def test_a_finite_moving_floor_lets_a_loud_reading_pass_while_moving(self) -> None:
        """Acceptance 4: the future measured-floor mode — a reading ABOVE a
        finite moving floor passes through even while self-moving."""
        latch = _Latch(moving=True)
        loud = _chunk(0.25)
        provider = make_rms_provider(lambda: loud, moving=latch, moving_floor=0.04)
        assert provider() == pytest.approx(compute_rms(loud))

    def test_a_finite_moving_floor_still_suppresses_below_it(self) -> None:
        provider = make_rms_provider(
            lambda: _chunk(0.01), moving=_Latch(moving=True), moving_floor=0.04
        )
        assert provider() == 0.0

    def test_no_moving_seam_wired_keeps_the_legacy_behavior(self) -> None:
        chunk = _chunk(0.05)
        provider = make_rms_provider(lambda: chunk)
        assert provider() == pytest.approx(compute_rms(chunk))

    def test_a_raising_moving_peek_degrades_to_not_moving(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("latch unavailable")

        chunk = _chunk(0.05)
        provider = make_rms_provider(lambda: chunk, moving=_boom)
        assert provider() == pytest.approx(compute_rms(chunk))


# --------------------------------------------------------------------------- #
# 3. Observability — one senselog line per transition, never per tick (acc 3) #
# --------------------------------------------------------------------------- #


class TestSuppressionLogging:
    def _gate_lines(self, caplog) -> list[str]:
        return [
            record.getMessage()
            for record in caplog.records
            if record.name == "reachy.sense" and "moving-floor" in record.getMessage()
        ]

    def test_suppression_logs_once_per_transition_never_per_tick(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="reachy.sense")
        latch = _Latch(moving=True)
        chunk = _chunk(0.05)
        provider = make_rms_provider(lambda: chunk, moving=latch)

        for _ in range(50):
            assert provider() == 0.0
        opened = [line for line in self._gate_lines(caplog) if "opened" in line]
        assert len(opened) == 1, f"expected ONE open line for 50 suppressed ticks: {opened}"
        # The open line names the reason and the active floor.
        assert "self-moving" in opened[0]
        assert "floor=inf" in opened[0]

        latch.moving = False
        assert provider() == pytest.approx(compute_rms(chunk))
        closed = [line for line in self._gate_lines(caplog) if "closed" in line]
        assert len(closed) == 1
        # The close line names how many ticks the gate held.
        assert "held_ticks=50" in closed[0]

    def test_a_still_run_logs_nothing(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="reachy.sense")
        provider = make_rms_provider(lambda: _chunk(0.05), moving=_Latch(moving=False))
        for _ in range(20):
            provider()
        assert self._gate_lines(caplog) == []

    def test_the_lines_follow_the_senselog_grammar(self, caplog) -> None:
        caplog.set_level(logging.INFO, logger="reachy.sense")
        provider = make_rms_provider(lambda: _chunk(0.05), moving=_Latch(moving=True))
        provider()
        (line,) = self._gate_lines(caplog)
        assert line.startswith("[SENSE stage=")
        assert "source=rms" in line
        assert "event=moving-floor" in line


# --------------------------------------------------------------------------- #
# 4. self_moving is a fed sense field (acceptance 2)                          #
# --------------------------------------------------------------------------- #


class TestSelfMovingSenseField:
    def test_sense_declares_self_moving_with_a_false_default(self) -> None:
        assert Sense().self_moving is False
        assert Sense(self_moving=True).self_moving is True

    def test_read_perception_peeks_the_self_moving_provider(self) -> None:
        snap = read_perception(SenseProviders(self_moving=lambda: True))
        assert snap.self_moving is True

    def test_a_raising_self_moving_provider_degrades_to_false(self) -> None:
        def _boom() -> bool:
            raise RuntimeError("no latch")

        assert read_perception(SenseProviders(self_moving=_boom)).self_moving is False

    def test_self_moving_is_declared_fed_in_the_same_change(self) -> None:
        # The repo invariant: wiring a provider and declaring it fed move
        # TOGETHER — a stale _COMPOSED_PROVIDER_FIELDS makes `behavior rules
        # check` lie in one direction or the other.
        assert "self_moving" in _COMPOSED_PROVIDER_FIELDS
        assert "self_moving" in FED_SENSE_FIELDS
        assert "self_moving" in SENSE_FIELDS


# --------------------------------------------------------------------------- #
# 5. A rule keyed on self_moving fires end-to-end (the t12 pattern)           #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """Minimal duck-typed TickContext, mirroring test_behavior_rms_sense.py."""

    now: float = 0.0
    tick: int = 0
    sense: object = None
    ownership: dict = field(default_factory=dict)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set()


def _self_moving_rule_engine() -> RuleEngine:
    cfg = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "busy",
                    "when": {"field": "self_moving", "op": "is_true"},
                    "run": "thoughtful",  # looping=False: no duration_s required
                    "cooldown_s": 1.0,
                }
            ]
        }
    )
    return RuleEngine(cfg)


def test_a_rule_keyed_on_self_moving_fires_against_the_real_wiring() -> None:
    """Full hand-built wiring: driver latch -> SenseProviders -> read_perception
    -> Sense -> RuleEngine (the exact end-to-end shape t12 proved for rms)."""
    driver = SelfMotionDriver()
    _tick(driver, 0.0, _pose())
    _tick(driver, _DT, _pose(head={"yaw": 1.0}))

    sense = read_perception(SenseProviders(self_moving=driver.is_moving))
    assert sense.self_moving is True

    engine = _self_moving_rule_engine()
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)

    assert len(ctx.admits) == 1
    assert ctx.admits[0].name == "thoughtful"


def test_a_still_driver_never_fires_the_same_rule() -> None:
    driver = SelfMotionDriver()
    for i in range(10):
        _tick(driver, i * _DT, _pose())

    sense = read_perception(SenseProviders(self_moving=driver.is_moving))
    assert sense.self_moving is False

    engine = _self_moving_rule_engine()
    ctx = _RecordingCtx(now=0.25, tick=1, sense=sense)
    engine.on_tick(ctx)

    assert ctx.admits == []


# --------------------------------------------------------------------------- #
# 6. Env resolution at the composition root                                   #
# --------------------------------------------------------------------------- #


class TestEnvResolution:
    def test_the_moving_floor_defaults_to_infinity(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.delenv(MOVING_FLOOR_ENV, raising=False)
        assert math.isinf(behavior_mod._rms_moving_floor())

    def test_the_moving_floor_accepts_the_string_inf(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.setenv(MOVING_FLOOR_ENV, "inf")
        assert math.isinf(behavior_mod._rms_moving_floor())

    def test_the_moving_floor_accepts_a_finite_number(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.setenv(MOVING_FLOOR_ENV, "0.04")
        assert behavior_mod._rms_moving_floor() == pytest.approx(0.04)

    def test_a_malformed_moving_floor_is_a_clean_user_error(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod
        from reachy.cli._errors import CliError

        monkeypatch.setenv(MOVING_FLOOR_ENV, "banana")
        with pytest.raises(CliError):
            behavior_mod._rms_moving_floor()

    def test_a_negative_or_nan_moving_floor_is_refused(self, monkeypatch) -> None:
        from reachy.cli._commands import behavior as behavior_mod
        from reachy.cli._errors import CliError

        for bad in ("-1", "nan"):
            monkeypatch.setenv(MOVING_FLOOR_ENV, bad)
            with pytest.raises(CliError):
                behavior_mod._rms_moving_floor()

    def test_the_tail_env_shortens_the_release(self, monkeypatch) -> None:
        from reachy.behavior.self_motion import TAIL_S_ENV
        from reachy.cli._commands import behavior as behavior_mod

        monkeypatch.setenv(TAIL_S_ENV, "0.1")
        driver = behavior_mod._make_self_motion()
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 1.0}))
        assert driver.is_moving() is True
        # Quiet from t=0.02; a 0.1 s tail releases by t=0.14 — far inside the
        # 0.25 s default (which would still be holding here).
        for i in range(2, 8):
            _tick(driver, i * _DT, _pose(head={"yaw": 1.0}))
        assert driver.is_moving() is False

    def test_unset_env_builds_the_shipped_defaults(self, monkeypatch) -> None:
        from reachy.behavior.self_motion import EPS_DEG_ENV, EPS_MM_ENV, TAIL_S_ENV
        from reachy.cli._commands import behavior as behavior_mod

        for name in (TAIL_S_ENV, EPS_DEG_ENV, EPS_MM_ENV):
            monkeypatch.delenv(name, raising=False)
        driver = behavior_mod._make_self_motion()
        assert isinstance(driver, SelfMotionDriver)
        assert DEFAULT_TAIL_S == 0.25


# --------------------------------------------------------------------------- #
# 7. The camera-gate variant (#179, deviation d6) — per-second eps, no antennas #
# --------------------------------------------------------------------------- #


class TestCameraGateVariant:
    """The face gate's latch answers "is the CAMERA moving fast enough to blur
    a frame?", so it drops the antennas and judges velocity against real tick
    time. Measured live on the Wireless: antenna-sway (18 deg / 3 s, ~38 deg/s
    peak) tripped a per-tick 20 deg/s gate on every half-swing, and a 500 ms
    tick stall turned the base layer's 2.7 deg/s wander into a slew-class step."""

    def test_the_default_latch_is_per_tick_and_watches_antennas(self) -> None:
        driver = SelfMotionDriver()
        assert driver.per_second is False
        assert driver.watches_antennas is True

    def test_antenna_sway_never_latches_a_camera_gate(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        assert driver.watches_antennas is False
        for i in range(150):  # 3 s of an 18 deg / 3 s sway: peak ~38 deg/s
            t = i * _DT
            sway = 18.0 * math.sin(2.0 * math.pi * t / 3.0)
            _tick(driver, t, _pose(antennas=(sway, -sway)))
            assert driver.is_moving() is False, f"antenna sway latched the camera gate at tick {i}"

    def test_a_head_slew_still_latches_a_camera_gate(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 2.0}))  # 100 deg/s
        assert driver.is_moving() is True

    def test_body_yaw_still_latches_a_camera_gate(self) -> None:
        # body_yaw rotates the whole head assembly, camera included.
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(body_yaw=2.0))
        assert driver.is_moving() is True

    def test_a_slow_wander_over_a_stalled_tick_is_not_a_slew(self) -> None:
        # 2.7 deg/s over a 500 ms stall is a 1.35 deg step — well past a
        # per-tick 0.4 deg threshold, well under 20 deg/s.
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 0.0, _pose())
        _tick(driver, 0.5, _pose(head={"yaw": 1.35}))
        assert driver.is_moving() is False

    def test_the_same_step_inside_one_tick_is_a_slew(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"yaw": 1.35}))  # 67 deg/s
        assert driver.is_moving() is True

    def test_a_tick_with_no_elapsed_time_carries_no_velocity_evidence(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 1.0, _pose())
        _tick(driver, 1.0, _pose(head={"yaw": 5.0}))  # dt == 0: skipped, never a div-by-zero
        assert driver.is_moving() is False

    def test_a_degraded_tick_drops_the_clock_sample_too(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0, watch_antennas=False)
        _tick(driver, 0.0, _pose())
        _tick(driver, 0.5, None)  # gap: both the pose and its clock are dropped
        _tick(driver, 0.52, _pose(head={"yaw": 5.0}))  # first post-gap sample: no delta
        assert driver.is_moving() is False

    def test_a_missing_mm_value_borrows_the_deg_one(self) -> None:
        driver = SelfMotionDriver(eps_deg_s=20.0)
        assert driver.per_second is True
        assert driver._eps_mm == pytest.approx(20.0)
        _tick(driver, 0.0, _pose())
        _tick(driver, _DT, _pose(head={"z": 1.0}))  # 50 mm/s
        assert driver.is_moving() is True
