"""The engine's ONE injected per-tick seam — ``engine.run(tick_seam=...)``.

Criterion 4 of the rules plan: ``engine.py`` grows exactly one injected
event-callback seam that the rule, goto, and export consumers all ride, so those
downstream tasks add no further ``engine.py`` edits. These tests pin the seam
contract itself (independent of the rule engine): the callable is invoked once
per tick with a :class:`~reachy.behavior.engine.TickContext` exposing a stable
set of fields, its ``admit`` / ``evict`` / ``active_names`` mutate the live
active set, its ``emit`` routes to the seam's ``.emit`` fan-out, perception is
read ungated while a seam is installed, and a ``tick_seam=None`` run is
byte-for-byte the pre-seam behavior.

Deterministic throughout: a fake in-memory streaming sink plus the engine's own
injectable ``sleep`` / ``now`` / ``max_ticks`` seams — no robot, daemon, or
network.
"""

from __future__ import annotations

import contextlib

import pytest

from reachy.behavior import engine as E
from reachy.behavior.engine import Engine, EngineConfig, TickContext
from reachy.behavior.library import build as build_behavior
from reachy.behavior.model import Lifetime, StopClass
from reachy.behavior.sense import Sense


class _FakeSink:
    def __init__(self):
        self.poses = []
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

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


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _run(*, tick_seam=None, sense=None, max_ticks=3, base_layer=True, engine=None):
    tr = _FakeTransport()
    ticks = E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=base_layer, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=max_ticks,
        engine=engine,
        sense=sense,
        tick_seam=tick_seam,
    )
    return tr, ticks


# --------------------------------------------------------------------------- #
# Invocation cadence + TickContext shape                                      #
# --------------------------------------------------------------------------- #


def test_tick_seam_invoked_once_per_tick() -> None:
    seen = []
    _run(tick_seam=lambda ctx: seen.append(ctx.tick), max_ticks=4)
    assert seen == [1, 2, 3, 4]  # once per tick, 1-based, in order


def test_tick_context_exposes_the_documented_fields() -> None:
    captured = []
    _run(tick_seam=captured.append, max_ticks=1)
    ctx = captured[0]
    assert isinstance(ctx, TickContext)
    assert isinstance(ctx.now, float) and ctx.now > 0
    assert ctx.tick == 1
    assert isinstance(ctx.sense, Sense)
    assert set(ctx.ownership) == {"head", "antennas", "body_yaw"}
    assert callable(ctx.emit)
    assert callable(ctx.admit)
    assert callable(ctx.evict)
    assert callable(ctx.active_names)


def test_ctx_now_tracks_the_injected_clock() -> None:
    times = []
    _run(tick_seam=lambda ctx: times.append(ctx.now), max_ticks=3)
    # run() consumes the first now() for start_t before the loop; ticks see 0.04+.
    assert times == [pytest.approx(0.04), pytest.approx(0.06), pytest.approx(0.08)]


# --------------------------------------------------------------------------- #
# The engine-control surface: admit / evict / active_names                    #
# --------------------------------------------------------------------------- #


def _nod():
    return build_behavior(
        "nod",
        {"amp": 12.0, "period": 0.8},
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        "seam-nod-1",
    )


def test_ctx_admit_puts_a_built_behavior_onto_the_active_set() -> None:
    eng = Engine()
    admitted = {"done": False}

    def seam(ctx):
        if not admitted["done"]:
            ctx.admit(_nod())
            admitted["done"] = True

    _run(tick_seam=seam, engine=eng, max_ticks=3)
    names = {ab.behavior.name for ab in eng.active}
    assert "nod" in names
    # nod (stoppable) beats the passive feel-alive base layer for the head channel.
    assert eng._last_ownership["head"] == "seam-nod-1"


def test_ctx_active_names_reflects_the_live_set() -> None:
    eng = Engine()
    seen = []

    def seam(ctx):
        seen.append(ctx.active_names())
        if ctx.tick == 1:
            ctx.admit(_nod())

    _run(tick_seam=seam, engine=eng, max_ticks=3)
    assert seen[0] == {"feel-alive"}  # only the base layer at first
    assert "nod" in seen[-1]  # after admit


def test_ctx_evict_removes_named_behaviors() -> None:
    eng = Engine()

    def seam(ctx):
        if ctx.tick == 1:
            ctx.admit(_nod())
        elif ctx.tick == 2:
            ctx.evict("nod")

    _run(tick_seam=seam, engine=eng, max_ticks=3)
    assert "nod" not in {ab.behavior.name for ab in eng.active}


# --------------------------------------------------------------------------- #
# emit fan-out                                                                #
# --------------------------------------------------------------------------- #


class _EmittingSeam:
    """A seam that publishes an event each tick; .emit is its consumer fan-out."""

    def __init__(self):
        self.received = []

    def emit(self, event):  # engine wires ctx.emit -> this
        self.received.append(event)

    def __call__(self, ctx):
        ctx.emit({"tick": ctx.tick})


def test_ctx_emit_routes_to_the_seam_emit_method() -> None:
    seam = _EmittingSeam()
    _run(tick_seam=seam, max_ticks=3)
    assert seam.received == [{"tick": 1}, {"tick": 2}, {"tick": 3}]


def test_ctx_emit_is_a_safe_noop_when_seam_has_no_emit() -> None:
    # A bare callable seam (no .emit) -> ctx.emit is a no-op, never raises.
    def seam(ctx):
        ctx.emit({"anything": True})

    _tr, ticks = _run(tick_seam=seam, max_ticks=2)
    assert ticks == 2


# --------------------------------------------------------------------------- #
# Ungated perception while a seam is installed                                #
# --------------------------------------------------------------------------- #


def test_seam_forces_ungated_perception_every_tick() -> None:
    calls = {"n": 0}

    def sense(_t):
        calls["n"] += 1
        return Sense(speech_detected=True)

    got = []
    _run(tick_seam=lambda ctx: got.append(ctx.sense.speech_detected), sense=sense, max_ticks=3)
    assert calls["n"] == 3  # read every tick despite no wants_sense behavior
    assert got == [True, True, True]


def test_no_seam_keeps_perception_gated() -> None:
    calls = {"n": 0}

    def sense(_t):
        calls["n"] += 1
        return Sense(speech_detected=True)

    _run(tick_seam=None, sense=sense, max_ticks=3)
    assert calls["n"] == 0  # gated off with no wants_sense behavior and no seam


# --------------------------------------------------------------------------- #
# tick_seam=None is byte-for-byte the pre-seam behavior                       #
# --------------------------------------------------------------------------- #


def test_run_without_seam_is_unchanged() -> None:
    tr, ticks = _run(tick_seam=None, max_ticks=3)
    assert ticks == 3
    assert tr.sink.calls == 4  # preflight + 3 ticks (settle=False)
    for pose in tr.sink.poses:
        assert set(pose["head"]) == {"x", "y", "z", "roll", "pitch", "yaw"}


# --------------------------------------------------------------------------- #
# Multiple riders share the seam (goto + export ride alongside rules)         #
# --------------------------------------------------------------------------- #


def test_multiple_drivers_ride_one_seam_via_a_fanning_callable() -> None:
    """A composite seam fans one tick out to several riders — the t5/t8 pattern."""
    a, b = [], []

    class _Fan:
        def __init__(self, drivers):
            self._drivers = drivers

        def __call__(self, ctx):
            for d in self._drivers:
                d(ctx)

    seam = _Fan([lambda c: a.append(c.tick), lambda c: b.append(c.tick)])
    _run(tick_seam=seam, max_ticks=3)
    assert a == [1, 2, 3] and b == [1, 2, 3]


def test_admit_behavior_matches_add_outcome_shape() -> None:
    """The seam's admit path (Engine.admit_behavior) returns add's outcome dict."""
    eng = Engine()
    result = eng.admit_behavior(_nod(), now=0.0)
    assert result["ok"] is True
    assert result["op"] == "add"
    assert result["id"] == "seam-nod-1"
    assert result["name"] == "nod"
    assert result["class"] == "stoppable"
    assert result["channels"] == ["head"]
