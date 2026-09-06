"""Composition tests for the per-sense availability block (task t3, issue #120b).

The unit tests in ``tests/test_behavior_sense_availability.py`` prove the rider
against a hand-built spool; these prove ``_compose_run_seam`` actually stitches
it onto the engine's ONE ``TickBus``, by driving the REAL
``behavior engine run`` CLI path against a fake transport (no robot, no daemon,
no SDK, no network) and reading the ``state.json`` it leaves behind.

The load-bearing property is criterion 1's "the SAME code produces both": the
two directions below differ ONLY in what the availability seam
(``face_sense._find_spec`` / ``sense_availability._find_spec``, plus the
composition's own ``build_face_recognition`` factory) reports — no branch, no
flag, no code edit between them.
"""

from __future__ import annotations

import contextlib

import pytest

from reachy.behavior import control, face_sense, sense_availability
from reachy.behavior.sense_availability import AVAILABILITY_SENSES, STATE_KEY
from reachy.cli import main

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# Fakes / fixtures                                                            #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (a mic-less box)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _QuietTransport()
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


@pytest.fixture(autouse=True)
def _restore_vision_latch():
    saved = face_sense._VISION_WARNED
    yield
    face_sense._VISION_WARNED = saved


def _drivers_of(tick_seam) -> list:
    """The TickBus driver list behind whatever wrappers the seam is wearing.

    ``_compose_run_seam`` returns a ``TickMetrics`` around the ``TickBus``;
    unwrap by shape rather than by one hard-coded private name so a metrics-side
    refactor cannot silently turn this assertion into a no-op.
    """
    seam = tick_seam
    for _ in range(4):
        drivers = getattr(seam, "_drivers", None)
        if drivers is not None:
            return list(drivers)
        inner = getattr(seam, "_inner", None) or getattr(seam, "_seam", None)
        if inner is None:
            break
        seam = inner
    raise AssertionError(f"no driver list found behind {type(tick_seam).__name__}")


def _run_engine(ticks: int = 6) -> dict:
    assert main(["behavior", "engine", "run", "--max-ticks", str(ticks)]) == 0
    state = control.read_state()
    assert isinstance(state, dict), "the engine published no state.json"
    return state


# --------------------------------------------------------------------------- #
# 1. Criterion 1 — the [vision] flip, through the seam only                    #
# --------------------------------------------------------------------------- #


def test_state_json_reports_face_dead_with_vision_extra_absent(_isolated, monkeypatch):
    """Issue #120 verbatim: an [sdk]-equipped box with no [vision] extra.

    Before this task the ONLY record of that fact was one warning at boot; now it
    stands in ``state.json`` for as long as the runtime lives.
    """
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)  # no cv2
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: object())  # sdk present

    state = _run_engine()
    assert state[STATE_KEY]["face"] == {
        "available": False,
        "reason": "vision-extra-absent",
        "live": None,
        "last_frame_at": None,
    }


def test_state_json_reports_face_available_with_the_vision_extra(_isolated, monkeypatch):
    """The other direction, same code: only the availability seam moved."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())  # cv2 present
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: object())  # sdk present
    # cv2 is not really installed, so the real lazy import would still fail; the
    # composition's own factory is the seam that says "a recognizer was built".
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.build_face_recognition", lambda: (object(), object())
    )

    state = _run_engine()
    assert state[STATE_KEY]["face"] == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }


def test_the_bare_ci_box_reports_every_sense_dead_with_a_named_reason(_isolated, monkeypatch):
    """A bare box — neither extra — says so, sense by sense.

    The bare condition is INJECTED rather than read off the running interpreter:
    reading the real environment made this pass on bare CI and fail on any dev
    box that happens to have ``[sdk]`` installed, which is a property of the
    machine, not of the code under test.
    """
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: None)

    state = _run_engine()
    block = state[STATE_KEY]
    assert set(block) == set(AVAILABILITY_SENSES)
    dead = {name: entry["reason"] for name, entry in block.items() if not entry["available"]}
    # self_moving reads only ctx.pose, so it is alive even here.
    assert set(dead) == set(AVAILABILITY_SENSES) - {"self_moving"}
    assert all(isinstance(reason, str) and reason.strip() for reason in dead.values())
    assert dead["face"] == "vision-extra-absent"
    assert block["self_moving"] == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }


# --------------------------------------------------------------------------- #
# 2. Criterion 2 — coverage, and additivity against the rest of state.json     #
# --------------------------------------------------------------------------- #


def test_the_senses_block_covers_every_declared_sense(_isolated):
    state = _run_engine()
    assert set(state[STATE_KEY]) == set(AVAILABILITY_SENSES)


def test_the_senses_block_is_additive_to_the_engine_state(_isolated):
    """The rider merges; it never replaces. ``behavior status`` and the liveness
    heartbeat both keep reading exactly what they read before."""
    state = _run_engine()
    for key in ("updated", "compose_hz", "active", "ownership", "doa"):
        assert key in state, f"the rider clobbered the engine's own {key!r} key"
    assert isinstance(state["updated"], (int, float))


def test_the_block_survives_the_full_run_not_just_the_first_tick(_isolated):
    """A heartbeat write lands mid-run; the rider repairs it every tick after."""
    state = _run_engine(ticks=60)  # > compose_hz/2, so at least two heartbeat writes
    assert set(state[STATE_KEY]) == set(AVAILABILITY_SENSES)


# --------------------------------------------------------------------------- #
# 3. The rider is actually on the ONE TickBus                                  #
# --------------------------------------------------------------------------- #


def test_the_availability_rider_is_composed_onto_the_tick_seam(_isolated):
    """Composition assertion, so a later edit to ``_compose_run_seam`` that drops
    the rider fails here rather than silently emptying the block."""
    from reachy.behavior.engine import EngineConfig
    from reachy.behavior.sense_availability import SenseAvailabilityDriver
    from reachy.cli._commands import behavior as behavior_mod

    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(), EngineConfig(compose_hz=50, base_layer=True, settle=False), None, None
    )
    try:
        drivers = _drivers_of(tick_seam)
        assert any(isinstance(d, SenseAvailabilityDriver) for d in drivers), [
            type(d).__name__ for d in drivers
        ]
    finally:
        resources.close()


def test_the_probe_composition_publishes_no_senses_block(_isolated, tmp_path):
    """The observation-only probe seam wires no sense providers, so it must claim
    no availability either — an empty block would be a lie, a full one worse."""
    from reachy.behavior.engine import EngineConfig
    from reachy.behavior.sense_availability import SenseAvailabilityDriver
    from reachy.cli._commands import behavior as behavior_mod

    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        _QuietTransport(),
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        None,
        None,
        probe=("held", lambda record: None),
    )
    try:
        drivers = _drivers_of(tick_seam)
        assert not any(isinstance(d, SenseAvailabilityDriver) for d in drivers)
    finally:
        resources.close()
