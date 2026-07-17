"""Tests for the behavior-rules schema (:mod:`reachy.behavior.rules`).

Task t2 acceptance criteria:

1. A single ``from_dict``-style gate refuses unknown fields, non-JSON values,
   unknown behaviors/modes, and anything code-shaped, naming every rejection
   reason (mirrors :mod:`reachy.stash.record`).
2. The schema includes per-rule ``cooldown_s``/``hysteresis`` fields with
   validated defaults; "battery" appears nowhere as a field or example.
3. The loader imports stdlib only (``tomllib``), reads ``rules.toml`` under
   ``state_dir()/behavior``, and keeps the last-good config when a candidate
   fails validation.
"""

from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from reachy.behavior import rules as rules_mod
from reachy.behavior.rules import (
    COMPARATORS,
    DEFAULT_COOLDOWN_S,
    DEFAULT_HYSTERESIS,
    KIND_INHIBIT,
    KIND_REACT,
    SENSE_FIELDS,
    Mode,
    Predicate,
    Rule,
    RulesConfig,
    RulesLoader,
    default_rules_path,
    load_rules,
)
from reachy.cli._errors import CliError

VALID_TOML = """
active_mode = "calm"

[[react]]
id = "orient-to-speech"
when = { field = "speech", op = "is_true" }
run = "gaze-hold"
params = { yaw = 20.0, pitch = 5.0 }
cooldown_s = 3.0
hysteresis = 0.5

[[react]]
id = "loud-nod"
when = { field = "rms", op = "gt", value = 0.05 }
run = "nod"

[[inhibit]]
id = "quiet-while-thinking"
when = { field = "pat", op = "is_true" }
disable = ["feel-alive", "antenna-sway"]
cooldown_s = 2.0

[modes.calm]
energy = 0.5

[modes.playful]
energy = 1.5
"""


def _dict_from_toml(text: str) -> dict:
    import tomllib

    return tomllib.loads(text)


VALID = _dict_from_toml(VALID_TOML)


# ---------------------------------------------------------------------------
# Happy path
# ---------------------------------------------------------------------------


def test_valid_rules_file_parses_from_dict():
    cfg = RulesConfig.from_dict(VALID)
    assert len(cfg.react) == 2
    assert len(cfg.inhibit) == 1
    assert cfg.active_mode == "calm"
    assert set(cfg.modes) == {"calm", "playful"}
    assert cfg.modes["calm"] == Mode(name="calm", params={"energy": 0.5})


def test_react_rule_fields_are_populated():
    cfg = RulesConfig.from_dict(VALID)
    rule = next(r for r in cfg.react if r.id == "orient-to-speech")
    assert rule.kind == KIND_REACT
    assert rule.behavior == "gaze-hold"
    assert rule.params == {"yaw": 20.0, "pitch": 5.0}
    assert rule.when == Predicate(field="speech", op="is_true", value=None)
    assert rule.cooldown_s == 3.0
    assert rule.hysteresis == 0.5
    assert rule.disable == frozenset()


def test_inhibit_rule_fields_are_populated():
    cfg = RulesConfig.from_dict(VALID)
    rule = cfg.inhibit[0]
    assert rule.kind == KIND_INHIBIT
    assert rule.behavior is None
    assert rule.params == {}
    assert rule.disable == frozenset({"feel-alive", "antenna-sway"})
    assert rule.when == Predicate(field="pat", op="is_true", value=None)


def test_ordered_op_predicate_carries_numeric_value():
    cfg = RulesConfig.from_dict(VALID)
    rule = next(r for r in cfg.react if r.id == "loud-nod")
    assert rule.when == Predicate(field="rms", op="gt", value=0.05)


def test_equality_op_predicate_accepts_string_value():
    data = {
        "react": [
            {
                "id": "greet-ada",
                "when": {"field": "face", "op": "eq", "value": "Ada"},
                "run": "nod",
            }
        ]
    }
    cfg = RulesConfig.from_dict(data)
    assert cfg.react[0].when == Predicate(field="face", op="eq", value="Ada")


def test_absent_for_predicate_accepts_nonnegative_duration():
    data = {
        "react": [
            {
                "id": "look-around",
                "when": {"field": "face", "op": "absent_for", "value": 30},
                "run": "gaze-hold",
            }
        ]
    }
    cfg = RulesConfig.from_dict(data)
    assert cfg.react[0].when == Predicate(field="face", op="absent_for", value=30.0)


# ---------------------------------------------------------------------------
# Criterion 2 — cooldown_s / hysteresis defaults
# ---------------------------------------------------------------------------


def test_cooldown_and_hysteresis_default_when_omitted():
    cfg = RulesConfig.from_dict(VALID)
    rule = next(r for r in cfg.react if r.id == "loud-nod")
    assert rule.cooldown_s == DEFAULT_COOLDOWN_S
    assert rule.hysteresis == DEFAULT_HYSTERESIS


def test_default_cooldown_is_nonnegative_and_documented_default():
    assert DEFAULT_COOLDOWN_S >= 0
    assert DEFAULT_HYSTERESIS >= 0


def test_negative_cooldown_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "cooldown_s": -1.0,
            }
        ]
    }
    with pytest.raises(CliError, match="cooldown_s"):
        RulesConfig.from_dict(data)


def test_negative_hysteresis_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "hysteresis": -0.1,
            }
        ]
    }
    with pytest.raises(CliError, match="hysteresis"):
        RulesConfig.from_dict(data)


def test_boolean_cooldown_is_rejected_not_treated_as_numeric():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "cooldown_s": True,
            }
        ]
    }
    with pytest.raises(CliError, match="cooldown_s"):
        RulesConfig.from_dict(data)


def test_no_battery_field_or_example_anywhere_in_module():
    source = inspect.getsource(rules_mod)
    assert "battery" not in source.lower()


def test_battery_is_not_a_valid_sense_field():
    assert "battery" not in SENSE_FIELDS


# ---------------------------------------------------------------------------
# Criterion 1 — refusal classes, each naming its reason
# ---------------------------------------------------------------------------


def test_unknown_top_level_field_is_rejected():
    data = {"nonsense": True}
    with pytest.raises(CliError, match="nonsense"):
        RulesConfig.from_dict(data)


def test_non_mapping_rules_file_is_rejected():
    with pytest.raises(CliError):
        RulesConfig.from_dict(["not", "a", "mapping"])


def test_react_must_be_a_list():
    with pytest.raises(CliError, match="react"):
        RulesConfig.from_dict({"react": {"id": "x"}})


def test_unknown_react_field_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "extra": "nope",
            }
        ]
    }
    with pytest.raises(CliError, match="extra"):
        RulesConfig.from_dict(data)


def test_react_missing_required_field_is_rejected():
    data = {"react": [{"id": "x", "when": {"field": "speech", "op": "is_true"}}]}
    with pytest.raises(CliError, match="run"):
        RulesConfig.from_dict(data)


def test_lambda_value_is_rejected_as_code_smell():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "params": {"amp": lambda: 1},
            }
        ]
    }
    with pytest.raises(CliError):
        RulesConfig.from_dict(data)


def test_unknown_behavior_in_run_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "does-not-exist-in-library",
            }
        ]
    }
    with pytest.raises(CliError, match="behavior"):
        RulesConfig.from_dict(data)


def test_unknown_behavior_in_disable_is_rejected():
    data = {
        "inhibit": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "disable": ["does-not-exist-in-library"],
            }
        ]
    }
    with pytest.raises(CliError, match="behavior"):
        RulesConfig.from_dict(data)


def test_empty_disable_list_is_rejected():
    data = {"inhibit": [{"id": "x", "when": {"field": "speech", "op": "is_true"}, "disable": []}]}
    with pytest.raises(CliError, match="disable"):
        RulesConfig.from_dict(data)


def test_unknown_run_param_key_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "params": {"not-a-real-param": 1.0},
            }
        ]
    }
    with pytest.raises(CliError, match="not-a-real-param"):
        RulesConfig.from_dict(data)


def test_non_numeric_run_param_value_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true"},
                "run": "nod",
                "params": {"amp": "loud"},
            }
        ]
    }
    with pytest.raises(CliError, match="amp"):
        RulesConfig.from_dict(data)


def test_unknown_predicate_field_is_rejected():
    data = {
        "react": [{"id": "x", "when": {"field": "battery", "op": "lt", "value": 1}, "run": "nod"}]
    }
    with pytest.raises(CliError, match="field"):
        RulesConfig.from_dict(data)


def test_unknown_predicate_op_is_rejected():
    data = {"react": [{"id": "x", "when": {"field": "speech", "op": "flargle"}, "run": "nod"}]}
    with pytest.raises(CliError, match="op"):
        RulesConfig.from_dict(data)


def test_boolean_op_with_value_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "speech", "op": "is_true", "value": 1},
                "run": "nod",
            }
        ]
    }
    with pytest.raises(CliError, match="value"):
        RulesConfig.from_dict(data)


def test_ordered_op_missing_value_is_rejected():
    data = {"react": [{"id": "x", "when": {"field": "rms", "op": "gt"}, "run": "nod"}]}
    with pytest.raises(CliError, match="value"):
        RulesConfig.from_dict(data)


def test_ordered_op_nonnumeric_value_is_rejected():
    data = {
        "react": [{"id": "x", "when": {"field": "rms", "op": "gt", "value": "loud"}, "run": "nod"}]
    }
    with pytest.raises(CliError, match="value"):
        RulesConfig.from_dict(data)


def test_absent_for_negative_value_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "face", "op": "absent_for", "value": -5},
                "run": "nod",
            }
        ]
    }
    with pytest.raises(CliError, match="value"):
        RulesConfig.from_dict(data)


def test_equality_op_with_dict_value_is_rejected():
    data = {
        "react": [
            {
                "id": "x",
                "when": {"field": "face", "op": "eq", "value": {"nested": True}},
                "run": "nod",
            }
        ]
    }
    with pytest.raises(CliError, match="scalar"):
        RulesConfig.from_dict(data)


def test_duplicate_rule_id_across_react_and_inhibit_is_rejected():
    data = {
        "react": [{"id": "dup", "when": {"field": "speech", "op": "is_true"}, "run": "nod"}],
        "inhibit": [
            {
                "id": "dup",
                "when": {"field": "speech", "op": "is_true"},
                "disable": ["nod"],
            }
        ],
    }
    with pytest.raises(CliError, match="unique"):
        RulesConfig.from_dict(data)


def test_active_mode_referencing_unknown_mode_is_rejected():
    data = {"active_mode": "does-not-exist", "modes": {"calm": {"energy": 1.0}}}
    with pytest.raises(CliError, match="active_mode"):
        RulesConfig.from_dict(data)


def test_modes_defined_without_active_mode_is_rejected():
    data = {"modes": {"calm": {"energy": 1.0}}}
    with pytest.raises(CliError, match="active_mode"):
        RulesConfig.from_dict(data)


def test_active_mode_with_no_modes_defined_is_rejected():
    data = {"active_mode": "calm"}
    with pytest.raises(CliError, match="active_mode"):
        RulesConfig.from_dict(data)


def test_mode_param_nonnumeric_value_is_rejected():
    data = {"active_mode": "calm", "modes": {"calm": {"energy": "high"}}}
    with pytest.raises(CliError):
        RulesConfig.from_dict(data)


def test_empty_rules_file_is_valid_and_inert():
    cfg = RulesConfig.from_dict({})
    assert cfg == RulesConfig()
    assert cfg.react == ()
    assert cfg.inhibit == ()
    assert cfg.modes == {}
    assert cfg.active_mode is None


# ---------------------------------------------------------------------------
# Criterion 3 — loader: state_dir placement, stdlib only, last-good retention
# ---------------------------------------------------------------------------


def test_default_rules_path_is_state_dir_behavior_rules_toml(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    assert default_rules_path() == tmp_path / "behavior" / "rules.toml"


def test_load_rules_missing_file_returns_empty_config(tmp_path):
    path = tmp_path / "behavior" / "rules.toml"
    assert not path.exists()
    assert load_rules(path) == RulesConfig()


def test_load_rules_parses_valid_file_on_disk(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(VALID_TOML, encoding="utf-8")
    cfg = load_rules(path)
    assert len(cfg.react) == 2
    assert cfg.active_mode == "calm"


def test_load_rules_bad_toml_syntax_raises(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text("this is [ not valid toml", encoding="utf-8")
    with pytest.raises(CliError):
        load_rules(path)


def test_load_rules_schema_invalid_content_raises(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(
        '[[react]]\nid = "x"\nwhen = { field = "battery", op = "lt", value = 1 }\nrun = "nod"\n',
        encoding="utf-8",
    )
    with pytest.raises(CliError, match="field"):
        load_rules(path)


def test_loader_first_reload_with_missing_file_is_empty_not_error(tmp_path):
    loader = RulesLoader(tmp_path / "rules.toml")
    cfg = loader.reload()
    assert cfg == RulesConfig()
    assert loader.current == RulesConfig()
    assert loader.last_error is None


def test_loader_reload_picks_up_a_freshly_written_valid_file(tmp_path):
    path = tmp_path / "rules.toml"
    loader = RulesLoader(path)
    loader.reload()
    path.write_text(VALID_TOML, encoding="utf-8")
    cfg = loader.reload()
    assert len(cfg.react) == 2
    assert loader.current == cfg


def test_loader_keeps_last_good_config_on_bad_toml_candidate(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(VALID_TOML, encoding="utf-8")
    loader = RulesLoader(path)
    good = loader.reload()
    assert len(good.react) == 2

    path.write_text("not [ valid toml", encoding="utf-8")
    kept = loader.reload()

    assert kept == good
    assert loader.current == good
    assert loader.last_error is not None


def test_loader_keeps_last_good_config_on_schema_invalid_candidate(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(VALID_TOML, encoding="utf-8")
    loader = RulesLoader(path)
    good = loader.reload()

    path.write_text(
        '[[react]]\nid = "x"\nwhen = { field = "battery", op = "lt", value = 1 }\nrun = "nod"\n',
        encoding="utf-8",
    )
    kept = loader.reload()

    assert kept == good
    assert loader.current == good
    assert loader.last_error is not None


def test_loader_recovers_once_a_later_candidate_is_valid_again(tmp_path):
    path = tmp_path / "rules.toml"
    path.write_text(VALID_TOML, encoding="utf-8")
    loader = RulesLoader(path)
    loader.reload()

    path.write_text("not [ valid toml", encoding="utf-8")
    loader.reload()
    assert loader.last_error is not None

    path.write_text(VALID_TOML, encoding="utf-8")
    recovered = loader.reload()
    assert len(recovered.react) == 2
    assert loader.last_error is None


def test_loader_uses_default_rules_path_when_none_given(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    loader = RulesLoader()
    assert loader.path == tmp_path / "behavior" / "rules.toml"


def test_module_imports_stdlib_and_reachy_only():
    source_path = Path(inspect.getfile(rules_mod))
    tree = ast.parse(source_path.read_text(encoding="utf-8"))
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                modules.add(alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                modules.add(node.module.split(".")[0])
    allowed = {
        "__future__",
        "logging",
        "tomllib",
        "collections",
        "dataclasses",
        "pathlib",
        "reachy",
    }
    assert modules <= allowed, f"unexpected non-stdlib/non-reachy import(s): {modules - allowed}"


def test_comparators_and_sense_fields_are_exported_and_nonempty():
    assert SENSE_FIELDS
    assert COMPARATORS
    assert {"is_true", "is_false", "lt", "gt", "eq", "ne", "absent_for"} <= COMPARATORS


def test_rule_dataclass_has_declared_shape():
    # Structural check that Rule exposes exactly the documented fields — a
    # future accidental field addition/removal breaks this loudly.
    field_names = {f.name for f in Rule.__dataclass_fields__.values()}
    assert field_names == {
        "id",
        "kind",
        "when",
        "cooldown_s",
        "hysteresis",
        "behavior",
        "params",
        "disable",
    }


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
