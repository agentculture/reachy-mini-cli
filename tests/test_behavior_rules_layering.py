"""Two-layer rules: a shipped package resource + a box-local overriding overlay.

The end state for this repo is "the robot's behavior is rules and configuration,
not a rigid app". That needs default rules to ship — but the box-local
``rules.toml`` is an operator's tuned file. Writing defaults to that path at
install time would overwrite their tuning; refusing to write it would mean every
UPGRADED box silently gets none of the newly shipped rules.

The resolution under test here: defaults ship as a READ-ONLY PACKAGE RESOURCE
(``reachy/behavior/default_rules.toml``), and the box-local file is an OVERLAY
that OVERRIDES per rule id rather than replacing the whole set.

Three acceptance criteria:

1. Upgrading a box that has a tuned ``rules.toml`` keeps every local override in
   force AND picks up newly shipped rules.
2. A local entry can DISABLE a shipped rule (``enabled = false``).
3. A malformed overlay degrades to the SHIPPED layer, not to nothing.

Plus: the shipped layer must resolve through :mod:`importlib.resources` (the
API that works from an installed wheel/zip), never through a source-tree
``__file__`` walk, and the boot-resilience path must keep degrading gracefully.
"""

from __future__ import annotations

import ast
import inspect
import logging
import tomllib
from importlib import resources
from pathlib import Path

import pytest

from reachy.behavior import rules as rules_mod
from reachy.behavior.reload_driver import ReloadDriver
from reachy.behavior.rule_engine import STAGE as RULE_STAGE
from reachy.behavior.rules import (
    RulesConfig,
    RulesLoader,
    load_rules,
    load_shipped_rules,
    merge_rules,
)
from reachy.cli._commands import behavior as behavior_cmd
from reachy.cli._errors import CliError

SENSE_LOGGER = "reachy.sense"

# A shipped layer standing in for "what the package ships after an upgrade":
# two react rules and one inhibit rule, all bounded.
SHIPPED_TOML = """\
[[react]]
id = "pat-acknowledge"
when = { field = "pat", op = "is_true" }
run = "pet-reaction"
cooldown_s = 5.0

[[react]]
id = "orient-to-voice"
when = { field = "speech", op = "is_true" }
run = "gaze-hold"
params = { yaw = 20.0 }

[[inhibit]]
id = "quiet-when-loud"
when = { field = "rms", op = "gt", value = 0.5 }
disable = ["antenna-sway"]
"""

# The operator's tuned box-local overlay: overrides ONE shipped rule id and adds
# one of its own. It knows nothing about rules shipped in a later version.
OVERLAY_TOML = """\
[[react]]
id = "orient-to-voice"
when = { field = "speech", op = "is_true" }
run = "gaze-hold"
params = { yaw = 5.0 }
cooldown_s = 9.0

[[react]]
id = "local-only"
when = { field = "face", op = "is_true" }
run = "thoughtful"
"""

BROKEN_SCHEMA_TOML = "mystery = 1\nanother_bad = 2\n"
BROKEN_SYNTAX_TOML = "this is [ not valid toml\n"


@pytest.fixture(autouse=True)
def _isolated_state_dir(tmp_path, monkeypatch):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path / "state"))


@pytest.fixture
def shipped(monkeypatch):
    """Inject the shipped layer's TOML text (the packaging seam)."""

    def _set(text: str | None) -> None:
        monkeypatch.setattr(rules_mod, "shipped_rules_text", lambda: text)

    return _set


def _write_overlay(text: str) -> Path:
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _ids(rules) -> list[str]:
    return [r.id for r in rules]


def _by_id(cfg: RulesConfig, rule_id: str):
    for rule in (*cfg.react, *cfg.inhibit):
        if rule.id == rule_id:
            return rule
    raise AssertionError(f"no rule {rule_id!r} in {_ids(cfg.react) + _ids(cfg.inhibit)}")


def _sense_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


# --------------------------------------------------------------------------- #
# The shipped layer really ships (importlib.resources, not the source tree)    #
# --------------------------------------------------------------------------- #


def test_shipped_resource_resolves_via_importlib_resources():
    """The zip-safe API resolves it — this is what works from an installed wheel."""
    handle = resources.files(rules_mod.SHIPPED_RULES_PACKAGE).joinpath(
        rules_mod.SHIPPED_RULES_RESOURCE
    )
    assert handle.is_file()
    assert rules_mod.shipped_rules_text() is not None


def test_shipped_resource_is_valid_toml_and_passes_the_schema_gate():
    """A packaging/content regression in the shipped layer must fail loudly HERE."""
    text = rules_mod.shipped_rules_text()
    assert text is not None
    tomllib.loads(text)  # syntactically valid
    assert isinstance(load_shipped_rules(), RulesConfig)  # passes RulesConfig.from_dict


def test_shipped_layer_is_not_read_through_a_source_tree_dunder_file():
    """No ``Path(__file__).parent / ...`` shortcut — that never works from a wheel/zip.

    The importlib.resources handle above resolves identically in a source
    checkout and an installed wheel, so a source-tree read would pass that test
    while silently shipping nothing. This guards the implementation itself.
    """
    source = Path(inspect.getfile(rules_mod)).read_text(encoding="utf-8")
    tree = ast.parse(source)
    dunder_file = [
        node for node in ast.walk(tree) if isinstance(node, ast.Name) and node.id == "__file__"
    ]
    assert dunder_file == []


def test_shipped_resource_lives_under_the_packaged_reachy_tree():
    """hatchling packages ``reachy`` wholesale — assert nothing excludes the file."""
    root = Path(inspect.getfile(rules_mod)).parents[2]
    pyproject = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    wheel = pyproject["tool"]["hatch"]["build"]["targets"]["wheel"]
    assert wheel["packages"] == ["reachy"]
    assert "exclude" not in wheel and "only-include" not in wheel
    assert (root / "reachy" / "behavior" / rules_mod.SHIPPED_RULES_RESOURCE).is_file()


# --------------------------------------------------------------------------- #
# Criterion 1 — an upgrade keeps local overrides AND gains new shipped rules   #
# --------------------------------------------------------------------------- #


def test_upgrade_keeps_local_override_and_picks_up_new_shipped_rules(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay(OVERLAY_TOML)

    cfg = load_rules(path)

    # the operator's tuning survives the upgrade
    tuned = _by_id(cfg, "orient-to-voice")
    assert tuned.params["yaw"] == 5.0
    assert tuned.cooldown_s == 9.0
    # ...and the newly shipped rule the overlay never heard of is now in force
    assert _by_id(cfg, "pat-acknowledge").behavior == "pet-reaction"
    assert _by_id(cfg, "quiet-when-loud").disable == frozenset({"antenna-sway"})
    # ...and the overlay's own local rule is untouched
    assert _by_id(cfg, "local-only").behavior == "thoughtful"


def test_override_replaces_the_shipped_entry_wholesale_not_field_by_field(shipped):
    """An override is one whole rule, never a half-shipped/half-local hybrid."""
    shipped(SHIPPED_TOML)
    path = _write_overlay("""\
[[react]]
id = "orient-to-voice"
when = { field = "face", op = "is_true" }
run = "nod"
duration_s = 4.0
""")
    rule = _by_id(load_rules(path), "orient-to-voice")
    assert rule.behavior == "nod"
    assert rule.when.field == "face"
    assert rule.params == {}  # the shipped yaw=20.0 did NOT leak through
    assert rule.cooldown_s == rules_mod.DEFAULT_COOLDOWN_S


def test_override_keeps_the_shipped_rules_position(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay(OVERLAY_TOML)
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["pat-acknowledge", "orient-to-voice", "local-only"]


def test_shipped_only_box_with_no_overlay_still_gets_the_shipped_rules(shipped):
    shipped(SHIPPED_TOML)
    cfg = load_rules(rules_mod.default_rules_path())
    assert _ids(cfg.react) == ["pat-acknowledge", "orient-to-voice"]
    assert _ids(cfg.inhibit) == ["quiet-when-loud"]


def test_overlay_may_override_across_kinds_by_id(shipped):
    """Precedence is per RULE ID, regardless of react/inhibit kind."""
    shipped(SHIPPED_TOML)
    path = _write_overlay("""\
[[inhibit]]
id = "orient-to-voice"
when = { field = "speech", op = "is_true" }
disable = ["nod"]
""")
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["pat-acknowledge"]
    assert _ids(cfg.inhibit) == ["orient-to-voice", "quiet-when-loud"]


def test_overlay_modes_override_shipped_modes_by_name(shipped):
    shipped("""\
active_mode = "calm"

[modes.calm]
energy = 0.2

[modes.lively]
energy = 0.9
""")
    path = _write_overlay("""\
active_mode = "lively"

[modes.lively]
energy = 0.5
""")
    cfg = load_rules(path)
    assert cfg.active_mode == "lively"
    assert cfg.modes["lively"].params == {"energy": 0.5}
    assert cfg.modes["calm"].params == {"energy": 0.2}  # shipped mode still there


def test_shipped_active_mode_holds_when_the_overlay_selects_none(shipped):
    shipped('active_mode = "calm"\n\n[modes.calm]\nenergy = 0.2\n')
    path = _write_overlay(OVERLAY_TOML)
    cfg = load_rules(path)
    assert cfg.active_mode == "calm"


def test_include_shipped_false_reads_the_overlay_alone(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay(OVERLAY_TOML)
    cfg = load_rules(path, include_shipped=False)
    assert _ids(cfg.react) == ["orient-to-voice", "local-only"]
    assert cfg.inhibit == ()


# --------------------------------------------------------------------------- #
# Criterion 2 — a local entry can DISABLE a shipped rule                      #
# --------------------------------------------------------------------------- #


def test_overlay_tombstone_disables_a_shipped_rule(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay('[[react]]\nid = "pat-acknowledge"\nenabled = false\n')
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["orient-to-voice"]
    assert "pat-acknowledge" in cfg.disabled


def test_overlay_tombstone_disables_a_shipped_inhibit_rule(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay('[[inhibit]]\nid = "quiet-when-loud"\nenabled = false\n')
    cfg = load_rules(path)
    assert cfg.inhibit == ()


def test_a_tombstone_may_carry_the_whole_copied_stanza(shipped):
    """The operator copies the shipped stanza and flips ONE line — that must work."""
    shipped(SHIPPED_TOML)
    path = _write_overlay("""\
[[react]]
id = "pat-acknowledge"
enabled = false
when = { field = "pat", op = "is_true" }
run = "pet-reaction"
cooldown_s = 5.0
""")
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["orient-to-voice"]


def test_enabled_true_is_the_default_and_a_plain_no_op(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay("""\
[[react]]
id = "orient-to-voice"
enabled = true
when = { field = "speech", op = "is_true" }
run = "gaze-hold"
params = { yaw = 1.0 }
""")
    cfg = load_rules(path)
    assert _by_id(cfg, "orient-to-voice").params["yaw"] == 1.0


def test_a_tombstone_for_an_unknown_id_is_inert_not_an_error(shipped):
    """A shipped rule REMOVED upstream must not brick an overlay that disabled it."""
    shipped(SHIPPED_TOML)
    path = _write_overlay('[[react]]\nid = "long-gone"\nenabled = false\n')
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["pat-acknowledge", "orient-to-voice"]
    assert "long-gone" in cfg.disabled


def test_a_tombstone_needs_only_an_id():
    cfg = RulesConfig.from_dict({"react": [{"id": "x", "enabled": False}]})
    assert cfg.react == ()
    assert cfg.disabled == frozenset({"x"})


def test_a_tombstone_without_an_id_is_refused():
    with pytest.raises(CliError, match="id"):
        RulesConfig.from_dict({"react": [{"enabled": False}]})


def test_enabled_must_be_a_boolean():
    with pytest.raises(CliError, match="enabled"):
        RulesConfig.from_dict({"react": [{"id": "x", "enabled": "no"}]})


def test_a_tombstone_still_rejects_unknown_fields():
    with pytest.raises(CliError, match="unexpected"):
        RulesConfig.from_dict({"react": [{"id": "x", "enabled": False, "code": "boom"}]})


def test_a_tombstone_and_a_live_rule_sharing_an_id_in_one_file_is_a_duplicate():
    with pytest.raises(CliError, match="duplicate"):
        RulesConfig.from_dict(
            {
                "react": [
                    {"id": "x", "enabled": False},
                    {
                        "id": "x",
                        "when": {"field": "pat", "op": "is_true"},
                        "run": "thoughtful",
                    },
                ]
            }
        )


def test_a_shipped_tombstone_is_overridable_by_a_live_overlay_entry(shipped):
    """Re-enabling: the overlay's live entry for an id beats a shipped tombstone."""
    shipped('[[react]]\nid = "x"\nenabled = false\n')
    path = _write_overlay(
        '[[react]]\nid = "x"\nwhen = { field = "pat", op = "is_true" }\nrun = "thoughtful"\n'
    )
    cfg = load_rules(path)
    assert _ids(cfg.react) == ["x"]
    assert "x" not in cfg.disabled


# --------------------------------------------------------------------------- #
# Criterion 3 — a malformed overlay degrades to the SHIPPED layer             #
# --------------------------------------------------------------------------- #


def test_loader_first_reload_with_a_malformed_overlay_falls_back_to_shipped(shipped):
    shipped(SHIPPED_TOML)
    _write_overlay(BROKEN_SCHEMA_TOML)

    loader = RulesLoader()
    cfg = loader.reload()

    assert _ids(cfg.react) == ["pat-acknowledge", "orient-to-voice"]
    assert loader.current == cfg
    assert loader.last_error is not None
    assert "mystery" in loader.last_error


def test_loader_falls_back_to_shipped_on_bad_overlay_toml_syntax(shipped):
    shipped(SHIPPED_TOML)
    _write_overlay(BROKEN_SYNTAX_TOML)

    loader = RulesLoader()
    cfg = loader.reload()

    assert _ids(cfg.react) == ["pat-acknowledge", "orient-to-voice"]
    assert loader.last_error is not None


def test_loader_current_is_the_shipped_layer_before_any_reload(shipped):
    shipped(SHIPPED_TOML)
    loader = RulesLoader()
    assert _ids(loader.current.react) == ["pat-acknowledge", "orient-to-voice"]
    assert loader.last_error is None


def test_loader_keeps_the_last_good_MERGED_config_when_the_overlay_breaks(shipped):
    """Last-good retention spans BOTH layers, not just the shipped floor."""
    shipped(SHIPPED_TOML)
    path = _write_overlay(OVERLAY_TOML)
    loader = RulesLoader()
    good = loader.reload()
    assert _by_id(good, "orient-to-voice").params["yaw"] == 5.0

    path.write_text(BROKEN_SCHEMA_TOML, encoding="utf-8")
    kept = loader.reload()

    assert kept == good  # the tuned override, not the shipped floor
    assert loader.last_error is not None


def test_loader_recovers_to_the_merged_config_once_the_overlay_is_valid_again(shipped):
    shipped(SHIPPED_TOML)
    path = _write_overlay(BROKEN_SCHEMA_TOML)
    loader = RulesLoader()
    loader.reload()
    assert loader.last_error is not None

    path.write_text(OVERLAY_TOML, encoding="utf-8")
    cfg = loader.reload()

    assert loader.last_error is None
    assert _by_id(cfg, "orient-to-voice").params["yaw"] == 5.0
    assert _by_id(cfg, "pat-acknowledge").behavior == "pet-reaction"


def test_load_rules_still_raises_on_a_malformed_overlay(shipped):
    """The linter contract is unchanged: ``rules check`` must still see the error."""
    shipped(SHIPPED_TOML)
    path = _write_overlay(BROKEN_SCHEMA_TOML)
    with pytest.raises(CliError, match="mystery"):
        load_rules(path)


def test_a_malformed_SHIPPED_layer_degrades_instead_of_breaking_a_good_overlay(shipped, caplog):
    """A packaging defect is OUR bug — it must never make an operator's box unbootable."""
    shipped(BROKEN_SCHEMA_TOML)
    path = _write_overlay(OVERLAY_TOML)

    with caplog.at_level(logging.WARNING, logger="reachy.behavior.rules"):
        cfg = load_rules(path)

    assert _ids(cfg.react) == ["orient-to-voice", "local-only"]
    assert any("shipped" in r.getMessage() for r in caplog.records)


def test_load_shipped_rules_raises_on_malformed_shipped_content(shipped):
    shipped(BROKEN_SCHEMA_TOML)
    with pytest.raises(CliError, match="mystery"):
        load_shipped_rules()


def test_a_missing_shipped_resource_degrades_to_the_overlay_alone(shipped):
    shipped(None)
    path = _write_overlay(OVERLAY_TOML)
    assert _ids(load_rules(path).react) == ["orient-to-voice", "local-only"]


# --------------------------------------------------------------------------- #
# Boot resilience (reachy.cli._commands.behavior._boot_tick_seam)             #
# --------------------------------------------------------------------------- #


def test_boot_seam_with_a_malformed_overlay_runs_the_shipped_layer(shipped, caplog):
    """Degrade to shipped, not to nothing — with exactly one logged drop, no crash."""
    shipped(SHIPPED_TOML)
    _write_overlay(BROKEN_SCHEMA_TOML)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        seam = behavior_cmd._boot_tick_seam()

    assert isinstance(seam, ReloadDriver)
    assert _ids(seam.loader.current.react) == ["pat-acknowledge", "orient-to-voice"]
    drops = [ln for ln in _sense_lines(caplog) if f"stage={RULE_STAGE}" in ln]
    assert len(drops) == 1
    assert "event=boot" in drops[0]


def test_boot_seam_with_a_malformed_overlay_and_no_shipped_rules_is_still_bare(shipped, caplog):
    """The pre-existing contract when there is genuinely nothing to fall back to."""
    shipped("")
    _write_overlay(BROKEN_SCHEMA_TOML)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        seam = behavior_cmd._boot_tick_seam()

    assert seam is None
    assert len([ln for ln in _sense_lines(caplog) if f"stage={RULE_STAGE}" in ln]) == 1


# --------------------------------------------------------------------------- #
# merge_rules directly                                                        #
# --------------------------------------------------------------------------- #


def test_merge_rules_with_an_empty_overlay_is_the_base(shipped):
    shipped(SHIPPED_TOML)
    base = load_shipped_rules()
    assert merge_rules(base, RulesConfig()) == base


def test_merge_rules_with_an_empty_base_is_the_overlay():
    overlay = RulesConfig.from_dict(tomllib.loads(OVERLAY_TOML))
    assert merge_rules(RulesConfig(), overlay) == overlay
