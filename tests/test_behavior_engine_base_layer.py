"""The engine's base-layer lifecycle — #183 (spec targets c38, h28).

``feel-alive`` used to be seeded exactly once, at start, with nothing able to
re-create an ``is_base=True`` behavior afterwards: a by-name stop or an
inhibition-driven evict removed the base layer permanently (its id lingering in
``_base_ids``), and ``Engine.state()`` had no way to tell an operator whether
the robot was still, and why.

These tests pin the engine side of the fix:

* the engine tracks whether the base was ever seeded, whether a base behavior is
  active right now, and the CAUSE of its last removal (``"stop"`` for a by-name
  or by-id :meth:`Engine.stop`, ``"inhibition"`` for a :meth:`Engine.evict` —
  the call ``TickContext.evict`` is bound to and the one
  ``IntentDriver._enforce_inhibitions`` reaches);
* :meth:`Engine.ensure_base` re-seeds with the ORIGINAL energy, is idempotent
  while a base is active, emits exactly one senselog line per real re-seed, and
  is exposed on the per-tick seam as ``ctx.ensure_base``;
* ``state()`` gains an additive ``base_layer`` block;
* the un-stop carve-out: an unbounded ``add`` of ``feel-alive`` re-seeds the base
  proper (``is_base=True``, id in ``_base_ids``), while an add carrying a
  duration stays an ordinary bounded behavior.

Deterministic throughout: the engine's injected ``now`` / ``max_ticks`` seams
plus an in-memory sink — no robot, daemon, or network.
"""

from __future__ import annotations

import contextlib
import logging

import pytest

from reachy.behavior.engine import BASE_LAYER_NAME, Engine, EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.model import Lifetime, StopClass

SENSE_LOGGER = "reachy.sense"


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


class _FakeSink:
    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    @contextlib.contextmanager
    def streaming(self):
        yield _FakeSink()


def _sense_lines(caplog) -> list[str]:
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


def _seeded_engine(energy: float = 0.4) -> Engine:
    engine = Engine()
    engine.seed_base_layer(0.0, energy)
    return engine


def _base_actives(engine: Engine) -> list:
    return [ab for ab in engine.active if ab.is_base]


def _state(engine: Engine) -> dict:
    return engine.state(1.0, EngineConfig())


# --------------------------------------------------------------------------- #
# state(): the additive base_layer block                                      #
# --------------------------------------------------------------------------- #


def test_a_fresh_engine_reports_a_never_seeded_base_layer():
    assert _state(Engine())["base_layer"] == {
        "seeded": False,
        "active": False,
        "stopped_by": None,
    }


def test_a_seeded_engine_reports_an_active_base_layer():
    assert _state(_seeded_engine())["base_layer"] == {
        "seeded": True,
        "active": True,
        "stopped_by": None,
    }


def test_the_base_layer_block_is_additive_and_leaves_every_existing_key_alone():
    engine = _seeded_engine()
    state = _state(engine)
    for key in ("updated", "compose_hz", "active", "ownership", "doa"):
        assert key in state
    assert state["active"][0]["name"] == BASE_LAYER_NAME
    assert state["active"][0]["base"] is True


# --------------------------------------------------------------------------- #
# The removal cause                                                           #
# --------------------------------------------------------------------------- #


def test_a_by_name_stop_of_feel_alive_reports_stopped_by_stop():
    engine = _seeded_engine()
    engine.stop(BASE_LAYER_NAME)
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": False,
        "stopped_by": "stop",
    }


def test_a_stop_by_id_of_the_base_layer_reports_stopped_by_stop():
    engine = Engine()
    base_id = engine.seed_base_layer(0.0, 1.0)
    engine.stop(base_id)
    assert _state(engine)["base_layer"]["stopped_by"] == "stop"


def test_an_evict_of_the_base_id_reports_stopped_by_inhibition():
    engine = Engine()
    base_id = engine.seed_base_layer(0.0, 1.0)
    engine.evict(base_id)
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": False,
        "stopped_by": "inhibition",
    }


def test_an_evict_by_name_of_feel_alive_reports_stopped_by_inhibition():
    engine = _seeded_engine()
    engine.evict(BASE_LAYER_NAME)
    assert _state(engine)["base_layer"]["stopped_by"] == "inhibition"


def test_evict_returns_the_same_outcome_shape_as_stop():
    engine = _seeded_engine()
    result = engine.evict(BASE_LAYER_NAME)
    assert result["ok"] is True
    assert result["op"] == "stop"
    assert result["target"] == BASE_LAYER_NAME
    assert result["count"] == 1
    assert result["unknown"] is False


def test_stopping_a_non_base_behavior_leaves_the_base_layer_cause_untouched():
    engine = _seeded_engine()
    engine.add("nod", {}, StopClass.STOPPABLE, Lifetime(looping=False, duration=1.0), 0.0)
    engine.stop("nod")
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": True,
        "stopped_by": None,
    }


def test_stop_all_keeps_the_base_layer_and_records_no_cause():
    engine = _seeded_engine()
    engine.add("nod", {}, StopClass.STOPPABLE, Lifetime(looping=False, duration=1.0), 0.0)
    engine.stop("all")
    assert [ab.behavior.name for ab in engine.active] == [BASE_LAYER_NAME]
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": True,
        "stopped_by": None,
    }


# --------------------------------------------------------------------------- #
# ensure_base                                                                 #
# --------------------------------------------------------------------------- #


def test_ensure_base_restores_the_base_layer_after_a_stop_and_clears_the_cause():
    engine = _seeded_engine(energy=0.4)
    engine.stop(BASE_LAYER_NAME)
    new_id = engine.ensure_base(2.0)
    assert new_id is not None
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": True,
        "stopped_by": None,
    }
    assert len(_base_actives(engine)) == 1
    assert _base_actives(engine)[0].behavior.id == new_id


def test_the_re_seeded_base_layer_keeps_the_original_energy():
    engine = _seeded_engine(energy=0.4)
    first = engine.active[0].behavior
    engine.evict(BASE_LAYER_NAME)
    engine.ensure_base(2.0)
    second = engine.active[0].behavior
    assert second.params["energy"] == pytest.approx(0.4)
    assert second.params == first.params


def test_stop_all_keeps_the_re_seeded_base_id():
    engine = _seeded_engine()
    engine.evict(BASE_LAYER_NAME)
    new_id = engine.ensure_base(2.0)
    assert new_id in engine._base_ids
    engine.stop("all")
    assert [ab.behavior.id for ab in engine.active] == [new_id]


def test_ensure_base_is_a_no_op_while_a_base_is_active():
    engine = _seeded_engine()
    assert engine.ensure_base(2.0) is None
    assert len(_base_actives(engine)) == 1
    assert [ab.behavior.name for ab in engine.active] == [BASE_LAYER_NAME]


def test_ensure_base_on_an_engine_that_never_seeded_a_base_seeds_one():
    engine = Engine()
    new_id = engine.ensure_base(0.0)
    assert new_id is not None
    assert _state(engine)["base_layer"]["seeded"] is True
    assert len(_base_actives(engine)) == 1


def test_every_real_re_seed_emits_exactly_one_senselog_line(caplog):
    engine = _seeded_engine()
    engine.evict(BASE_LAYER_NAME)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        engine.ensure_base(2.0)
    lines = _sense_lines(caplog)
    assert len(lines) == 1
    assert "stage=engine" in lines[0]
    assert "source=base-layer" in lines[0]


def test_a_no_op_ensure_base_emits_no_senselog_line(caplog):
    engine = _seeded_engine()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        assert engine.ensure_base(2.0) is None
    assert _sense_lines(caplog) == []


def test_the_tick_seam_exposes_ensure_base(caplog):
    engine = Engine()
    seen: list = []

    def seam(ctx):
        if ctx.tick == 1:
            ctx.evict(BASE_LAYER_NAME)
        elif ctx.tick == 2:
            seen.append(ctx.ensure_base())

    clock = iter([i * 0.02 for i in range(1, 200)])
    engine_run(
        _FakeTransport(),
        EngineConfig(compose_hz=50.0, base_layer=True, settle=False),
        sleep=lambda _s: None,
        now=lambda: next(clock),
        max_ticks=3,
        engine=engine,
        tick_seam=seam,
    )
    assert seen and seen[0] is not None
    assert len(_base_actives(engine)) == 1


# --------------------------------------------------------------------------- #
# The un-stop carve-out: an unbounded add of feel-alive re-seeds the base      #
# --------------------------------------------------------------------------- #


def test_an_unbounded_add_of_feel_alive_re_seeds_the_base_proper():
    engine = _seeded_engine(energy=0.4)
    engine.stop(BASE_LAYER_NAME)
    result = engine.add(
        BASE_LAYER_NAME,
        {"energy": 0.4},
        StopClass.PASSIVE,
        Lifetime(looping=True, duration=None),
        3.0,
    )
    assert result["ok"] is True
    assert result["op"] == "add"
    assert result["name"] == BASE_LAYER_NAME
    assert set(result) >= {"ok", "op", "id", "name", "class", "channels", "evicted", "blocked"}
    assert len(_base_actives(engine)) == 1
    assert _base_actives(engine)[0].behavior.id == result["id"]
    assert result["id"] in engine._base_ids
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": True,
        "stopped_by": None,
    }


def test_the_un_stop_add_ignores_caller_params_and_keeps_the_engine_energy():
    """The CLI fills every library default into an add payload (energy=1.0
    included); the base re-seed must restore the run's configured energy."""
    engine = _seeded_engine(energy=0.4)
    engine.stop(BASE_LAYER_NAME)
    entry_defaults = {"energy": 1.0, "breathe_z": 99.0}
    engine.add(
        BASE_LAYER_NAME,
        entry_defaults,
        StopClass.PASSIVE,
        Lifetime(looping=True, duration=None),
        3.0,
    )
    base = _base_actives(engine)[0].behavior
    assert base.params["energy"] == pytest.approx(0.4)
    assert base.params["breathe_z"] != 99.0


def test_an_unbounded_add_of_feel_alive_while_a_base_is_active_is_a_noted_no_op():
    engine = _seeded_engine()
    existing = engine.active[0].behavior.id
    result = engine.add(
        BASE_LAYER_NAME,
        {},
        StopClass.PASSIVE,
        Lifetime(looping=True, duration=None),
        3.0,
    )
    assert result["ok"] is True
    assert result["id"] == existing
    assert result.get("note")
    assert len(_base_actives(engine)) == 1
    assert [ab.behavior.name for ab in engine.active] == [BASE_LAYER_NAME]


def test_a_bounded_add_of_feel_alive_stays_an_ordinary_behavior():
    engine = _seeded_engine()
    engine.stop(BASE_LAYER_NAME)
    result = engine.add(
        BASE_LAYER_NAME,
        {},
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=5.0),
        3.0,
    )
    assert result["ok"] is True
    assert _base_actives(engine) == []
    assert result["id"] not in engine._base_ids
    assert _state(engine)["base_layer"] == {
        "seeded": True,
        "active": False,
        "stopped_by": "stop",
    }


def test_a_non_looping_add_of_feel_alive_stays_an_ordinary_behavior():
    engine = Engine()
    result = engine.add(
        BASE_LAYER_NAME,
        {},
        StopClass.STOPPABLE,
        Lifetime(looping=False, duration=2.0),
        0.0,
    )
    assert _base_actives(engine) == []
    assert result["id"] not in engine._base_ids
    assert _state(engine)["base_layer"]["seeded"] is False


def test_an_unbounded_add_of_another_behavior_is_never_treated_as_the_base():
    engine = Engine()
    result = engine.add(
        "antenna-sway",
        {},
        StopClass.STOPPABLE,
        Lifetime(looping=True, duration=None),
        0.0,
    )
    assert _base_actives(engine) == []
    assert result["id"] not in engine._base_ids


def test_the_un_stop_add_reaches_the_engine_through_the_spool_apply_path():
    engine = _seeded_engine()
    engine.stop(BASE_LAYER_NAME)
    result = engine.apply(
        {
            "op": "add",
            "name": BASE_LAYER_NAME,
            "params": {},
            "class": "passive",
            "lifetime": {"looping": True, "duration": None},
        },
        4.0,
    )
    assert result["ok"] is True
    assert len(_base_actives(engine)) == 1
    assert _state(engine)["base_layer"]["stopped_by"] is None
