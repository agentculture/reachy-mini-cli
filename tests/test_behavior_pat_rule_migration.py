"""Activation/rollback fixtures and operator-truth contracts for pet reaction."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import pytest

from reachy.behavior import reload_driver
from reachy.behavior import rules as rules_mod
from reachy.behavior.reload_driver import ReloadDriver
from reachy.behavior.rules import RulesLoader, load_rules
from reachy.behavior.sense import EMPTY_SENSE
from reachy.explain.catalog import ENTRIES


@pytest.fixture(autouse=True)
def _no_shipped_rules(monkeypatch):
    """Blank the SHIPPED rules layer for this module.

    These tests exercise the box-local OVERLAY and the loader/CLI mechanics
    around it, not the product decision of what the release ships. Pinning them
    to whatever ``reachy/behavior/default_rules.toml`` happens to contain would
    churn them on every change to the shipped defaults while testing nothing
    about the mechanism. The real shipped content is asserted in
    ``tests/test_behavior_default_rules.py``; the two-layer merge itself in
    ``tests/test_behavior_rules_layering.py``.
    """
    monkeypatch.setattr(rules_mod, "shipped_rules_text", lambda: None)


REPO_ROOT = Path(__file__).resolve().parents[1]
FIXTURE_DIR = REPO_ROOT / "docs" / "fixtures" / "behavior-rules"
CANDIDATE = FIXTURE_DIR / "pat-pet-reaction.toml"
ROLLBACK = FIXTURE_DIR / "pat-thoughtful-rollback.toml"
GUIDE = REPO_ROOT / "docs" / "operating-reachy.md"


@dataclass
class _Ctx:
    now: float = 0.0
    tick: int = 0
    sense: object = EMPTY_SENSE
    ownership: dict = field(default_factory=dict)
    events: list = field(default_factory=list)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        return {"ok": True, "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        return {"ok": True, "target": name}

    def active_names(self) -> set[str]:
        return {behavior.name for behavior in self.admits} - set(self.evicts)


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))


def test_candidate_and_rollback_fixtures_validate_as_bounded_data_only_rules() -> None:
    candidate = load_rules(CANDIDATE)
    rollback = load_rules(ROLLBACK)

    assert len(candidate.react) == len(rollback.react) == 1
    assert candidate.react[0].id == rollback.react[0].id == "pat-acknowledge"
    assert candidate.react[0].when.field == rollback.react[0].when.field == "pat"
    assert candidate.react[0].behavior == "pet-reaction"
    assert rollback.react[0].behavior == "thoughtful"
    assert candidate.react[0].duration_s is None
    assert rollback.react[0].duration_s is None


def test_candidate_and_rollback_each_apply_through_one_live_reload(tmp_path) -> None:
    rules_path = tmp_path / "behavior" / "rules.toml"
    rules_path.parent.mkdir(parents=True)
    rules_path.write_text(ROLLBACK.read_text(encoding="utf-8"), encoding="utf-8")
    loader = RulesLoader(rules_path)
    loader.reload()  # boot state: the documented prior thoughtful rule
    driver = ReloadDriver(loader)
    ctx = _Ctx()

    rules_path.write_text(CANDIDATE.read_text(encoding="utf-8"), encoding="utf-8")
    activation_id = reload_driver.submit_reload()
    ctx.tick, ctx.now = 1, 1.0
    driver(ctx)
    activation = reload_driver.await_result(activation_id, timeout=0, sleep=lambda _: None)
    assert activation["ok"] is True
    assert loader.current.react[0].behavior == "pet-reaction"

    rules_path.write_text(ROLLBACK.read_text(encoding="utf-8"), encoding="utf-8")
    rollback_id = reload_driver.submit_reload()
    ctx.tick, ctx.now = 2, 2.0
    driver(ctx)
    rollback = reload_driver.await_result(rollback_id, timeout=0, sleep=lambda _: None)
    assert rollback["ok"] is True
    assert loader.current.react[0].behavior == "thoughtful"


def _section(text: str, start: str, end: str) -> str:
    return text.split(start, 1)[1].split(end, 1)[0]


def test_operator_guide_has_exact_one_reload_activation_and_rollback_commands() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    activation = _section(guide, "#### Activate pet reaction", "#### Roll back to thoughtful")
    rollback = _section(guide, "#### Roll back to thoughtful", "### Bounded reactions")

    assert "docs/fixtures/behavior-rules/pat-pet-reaction.toml" in activation
    assert "docs/fixtures/behavior-rules/pat-thoughtful-rollback.toml" in rollback
    for block in (activation, rollback):
        assert "behavior rules check --json" in block
        assert block.count("reachy-mini-cli behavior reload") == 1
        assert "engine restart" not in block


@pytest.mark.parametrize(
    "truth",
    [
        "8–12 seconds",
        "four-second",
        "side-only",
        "non-directional scratch",
        "level plus fresh-press recency",
        "complete commanded pose",
        "unavailable",
        "pat_state",
        "coordinated done gesture",
        "12 seconds",
        "no front/back",
    ],
)
def test_behavior_explain_catalog_carries_pettable_runtime_truth(truth: str) -> None:
    assert truth in ENTRIES[("behavior",)]


def test_guide_records_before_state_and_excludes_out_of_scope_claims() -> None:
    guide = GUIDE.read_text(encoding="utf-8")
    pat_section = _section(guide, "### The pat sense", "### Bounded reactions")

    assert "6eab58e" in pat_section
    assert "`thoughtful`" in pat_section
    assert "continuously changing" in pat_section
    assert "no front/back" in pat_section
    assert "does not infer contact during arbitrary motion" in pat_section
    assert "does not add RMS or face providers" in pat_section
    assert "does not add issue #78" in pat_section
    assert "does not start a second `MotionQueue`" in pat_section
