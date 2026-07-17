"""t9 — additive ``behavior status`` extensions: rules health + agent intents.

``behavior status`` now additionally reports:

* a ``rules`` section — path + counts (via ``reachy.behavior.rules.RulesLoader``,
  which never raises even on a malformed file — see
  ``reachy.cli._commands.behavior._rules_status``);
* the state.json ``"intents"`` object (goal/inhibitions/mode), surfaced
  verbatim when the engine (via ``reachy.behavior.intents.IntentDriver``) has
  published one.

Every EXISTING status field/test stays untouched — this file only adds new,
independent assertions, in a new file per the task's TDD constraints (no edits
to ``tests/test_behavior.py``).

No real robot/daemon/engine process: state is written directly to the spool
files under an isolated ``REACHY_STATE_DIR``, mirroring
``tests/test_behavior_reload.py`` / ``tests/test_behavior.py``.
"""

from __future__ import annotations

import json

import pytest

from reachy.behavior import control
from reachy.behavior import rules as rules_mod
from reachy.cli import main

BROKEN_TOML = """\
mystery = 1
another_bad = 2
"""

GOOD_TOML = """\
[[react]]
id = "r1"
when = { field = "doa", op = "absent_for", value = 0 }
run = "nod"
cooldown_s = 0
"""


def _write_rules(text: str):
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    # No real daemon reachable from a bounded, deterministic test.
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: False)


# --------------------------------------------------------------------------- #
# rules section                                                              #
# --------------------------------------------------------------------------- #


def test_status_rules_section_present_with_no_rules_file(capsys) -> None:
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    rules = payload["rules"]
    assert rules["exists"] is False
    assert rules["ok"] is True
    assert rules["react"] == 0
    assert rules["inhibit"] == 0
    assert rules["modes"] == 0
    assert "error" not in rules


def test_status_rules_section_reports_good_file_counts(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    rules = payload["rules"]
    assert rules["exists"] is True
    assert rules["ok"] is True
    assert rules["react"] == 1
    assert rules["inhibit"] == 0


def test_status_rules_section_reports_malformed_file_without_crashing(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "status", "--json"])
    assert rc == 0  # status must never crash because rules are malformed
    payload = json.loads(capsys.readouterr().out)
    rules = payload["rules"]
    assert rules["exists"] is True
    assert rules["ok"] is False
    assert "mystery" in rules["error"]
    assert "another_bad" in rules["error"]
    # base counts stay at the all-empty default (last-good, never had a good one)
    assert rules["react"] == 0
    assert rules["inhibit"] == 0


def test_status_rules_section_text_mode_smoke(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "status"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


# --------------------------------------------------------------------------- #
# intents surfacing (additive, only when published)                          #
# --------------------------------------------------------------------------- #


def test_status_surfaces_intents_when_published(capsys) -> None:
    control.CommandSpool().write_state(
        {
            "active": [],
            "ownership": {},
            "intents": {
                "goal": {"name": "nod", "params": {}},
                "inhibitions": ["shake"],
                "mode": "calm",
            },
        }
    )
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["intents"] == {
        "goal": {"name": "nod", "params": {}},
        "inhibitions": ["shake"],
        "mode": "calm",
    }


def test_status_omits_intents_key_when_absent_from_published_state(capsys) -> None:
    # Published state WITHOUT an "intents" key (e.g. an engine predating the
    # intents wave) must not fabricate one.
    control.CommandSpool().write_state({"active": [], "ownership": {}})
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "intents" not in payload


def test_status_omits_intents_key_when_engine_not_running(capsys) -> None:
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "intents" not in payload
    assert payload["note"] == "engine has not published state (not running, or just started)"


# --------------------------------------------------------------------------- #
# byte-compatibility: existing fields are still exactly what they were       #
# --------------------------------------------------------------------------- #


def test_status_existing_fields_unchanged_without_engine(capsys) -> None:
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["engine"]["process"] == "stopped"
    assert payload["active"] == []
    assert payload["ownership"] == {"head": None, "antennas": None, "body_yaw": None}


def test_status_existing_fields_unchanged_with_published_state(capsys) -> None:
    control.CommandSpool().write_state(
        {
            "active": ["nod-1"],
            "ownership": {"head": "nod-1", "antennas": None, "body_yaw": None},
            "compose_hz": 50.0,
            "doa": {"angle": None, "speech_detected": False},
        }
    )
    rc = main(["behavior", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["active"] == ["nod-1"]
    assert payload["ownership"]["head"] == "nod-1"
    assert payload["compose_hz"] == 50.0
    assert payload["doa"] == {"angle": None, "speech_detected": False}
