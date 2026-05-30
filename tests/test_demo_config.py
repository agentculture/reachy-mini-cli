"""Tests for ``reachy.demo_config`` — the persisted demo-mode tuning file."""

from __future__ import annotations

import json

import pytest

from reachy import demo_config as dconf
from reachy.alive import AliveConfig
from reachy.cli._errors import CliError


@pytest.fixture(autouse=True)
def _isolate_config(monkeypatch, tmp_path):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path))


def test_load_defaults_when_absent() -> None:
    cfg = dconf.load()
    assert cfg.transport == "http"
    assert cfg.interval == AliveConfig.interval
    assert cfg.energy == AliveConfig.energy
    assert cfg.seed is None


def test_save_then_load_roundtrip() -> None:
    cfg = dconf.DemoConfig(energy=0.7, interval=3.0, seed=11, wake=False)
    path = dconf.save(cfg)
    assert path == dconf.config_path()
    loaded = dconf.load()
    assert loaded.energy == 0.7
    assert loaded.interval == 3.0
    assert loaded.seed == 11
    assert loaded.wake is False


def test_apply_set_coerces_types() -> None:
    cfg = dconf.DemoConfig()
    dconf.apply_set(
        cfg, ["energy=0.5", "interval=4", "seed=9", "settle=false", "interpolation=ease"]
    )
    assert cfg.energy == 0.5
    assert isinstance(cfg.interval, float) and cfg.interval == 4.0
    assert cfg.seed == 9
    assert cfg.settle is False
    assert cfg.interpolation == "ease"


def test_apply_set_seed_none() -> None:
    cfg = dconf.DemoConfig(seed=5)
    dconf.apply_set(cfg, ["seed=none"])
    assert cfg.seed is None


def test_apply_set_rejects_missing_equals() -> None:
    with pytest.raises(CliError):
        dconf.apply_set(dconf.DemoConfig(), ["energy"])


def test_apply_set_rejects_unknown_key() -> None:
    with pytest.raises(CliError):
        dconf.apply_set(dconf.DemoConfig(), ["wobble=1"])


def test_apply_set_rejects_bad_enum() -> None:
    with pytest.raises(CliError):
        dconf.apply_set(dconf.DemoConfig(), ["interpolation=zoom"])


def test_apply_set_rejects_bad_bool() -> None:
    with pytest.raises(CliError):
        dconf.apply_set(dconf.DemoConfig(), ["wake=maybe"])


def test_validate_flags_bad_values() -> None:
    cfg = dconf.DemoConfig(interval=0, energy=-1, timeout=0)
    errors = dconf.validate(cfg)
    assert any("interval" in e for e in errors)
    assert any("energy" in e for e in errors)
    assert any("timeout" in e for e in errors)


def test_validate_ok_for_defaults() -> None:
    assert dconf.validate(dconf.DemoConfig()) == []


def test_to_alive_config_maps_fields() -> None:
    cfg = dconf.DemoConfig(interval=3.0, energy=0.6, interpolation="cartoon", seed=2)
    ac = cfg.to_alive_config()
    assert (ac.interval, ac.energy, ac.interpolation, ac.seed) == (3.0, 0.6, "cartoon", 2)


def test_load_tolerates_corrupt_file() -> None:
    dconf.config_path().parent.mkdir(parents=True, exist_ok=True)
    dconf.config_path().write_text("{ not json", encoding="utf-8")
    assert dconf.load().energy == AliveConfig.energy  # falls back to defaults


def test_load_tolerates_non_dict_json() -> None:
    dconf.config_path().parent.mkdir(parents=True, exist_ok=True)
    dconf.config_path().write_text(json.dumps([1, 2, 3]), encoding="utf-8")
    assert dconf.load().transport == "http"


def test_ensure_creates_default_when_missing() -> None:
    assert not dconf.config_path().exists()
    path = dconf.ensure()
    assert path == dconf.config_path()
    assert path.is_file()


def test_ensure_keeps_existing(tmp_path) -> None:
    custom = tmp_path / "keep.json"
    dconf.save(dconf.DemoConfig(energy=0.3), str(custom))
    dconf.ensure(str(custom))
    assert dconf.load(str(custom)).energy == 0.3  # not overwritten
