"""t18 — re-home the expression pose catalog verbs onto `behavior`.

`think expressions {list,check,overview}` inspect `reachy.speech.expressions`'s
TOML-backed pose catalog and `reachy.speech.distinctness`'s geometric
similarity check — neither is LLM-coupled, and both stay needed after `think`
retires (`reachy.speech.tools`'s `apply_pose` tool imports the catalog
directly). This gives the catalog a new CLI home under `behavior` — the
surviving presence noun, which already carries a sibling sub-noun (`rules`)
in exactly this "render + lint a file, no running engine needed" shape.

`think expressions` itself is untouched here — a later task in this arc
retires the `think` noun wholesale. The duplication between the two homes is
deliberate and temporary; this file only asserts the NEW `behavior expressions`
surface. See `tests/test_think_expressions.py` for the original verb tests and
`tests/test_behavior_rules_cli.py` for the sibling sub-noun's CLI conventions
this mirrors.
"""

from __future__ import annotations

import json

import pytest

from reachy.cli import main
from reachy.cli._commands import behavior as behavior_cmd
from reachy.explain import known_paths
from reachy.speech.expressions import Catalog


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# --------------------------------------------------------------------------- #
# behavior expressions / behavior expressions list                           #
# --------------------------------------------------------------------------- #


def test_expressions_list_text(capsys) -> None:
    rc = main(["behavior", "expressions"])
    assert rc == 0
    out = capsys.readouterr().out
    for emoji in (k for k in Catalog().keys() if k != "neutral"):
        assert emoji in out


def test_expressions_list_explicit_verb(capsys) -> None:
    rc = main(["behavior", "expressions", "list"])
    assert rc == 0
    assert "🤔" in capsys.readouterr().out


def test_expressions_list_json(capsys) -> None:
    rc = main(["behavior", "expressions", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert "expressions" in payload
    keys = [e["emoji"] for e in payload["expressions"]]
    assert "🤔" in keys
    # neutral is the fallback, not an advertised expression.
    assert "neutral" not in keys
    # each carries a descriptor.
    assert all("descriptor" in e for e in payload["expressions"])


def test_expressions_list_matches_think_expressions_list(capsys) -> None:
    """Both homes read the SAME catalog and generate the SAME descriptors."""
    rc = main(["behavior", "expressions", "--json"])
    assert rc == 0
    behavior_payload = json.loads(capsys.readouterr().out)

    rc = main(["think", "expressions", "--json"])
    assert rc == 0
    think_payload = json.loads(capsys.readouterr().out)

    assert behavior_payload == think_payload


def test_bare_expressions_lists(capsys) -> None:
    """`behavior expressions` with no sub-verb lists the catalog (mirrors
    `behavior rules` / `think expressions`'s bare-defaults-to-list idiom)."""
    rc = main(["behavior", "expressions"])
    assert rc == 0
    assert capsys.readouterr().out.strip()


# --------------------------------------------------------------------------- #
# behavior expressions check                                                 #
# --------------------------------------------------------------------------- #


def test_expressions_check_clean_text(capsys) -> None:
    rc = main(["behavior", "expressions", "check"])
    assert rc == 0  # clean check is exit 0
    out = capsys.readouterr().out
    assert "clean" in out.lower()


def test_expressions_check_clean_json(capsys) -> None:
    rc = main(["behavior", "expressions", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["flagged"] == []


def test_expressions_check_flags_a_near_duplicate_still_exits_zero(monkeypatch, capsys) -> None:
    """A flagged pair is a warning, not a gate — exit stays 0; --json ok=false
    is the machine-readable signal (mirrors `behavior rules check` / the
    original `think expressions check`)."""
    monkeypatch.setattr(behavior_cmd, "_find_too_similar", lambda cat: [("🤔", "😐", 0.12)])
    rc = main(["behavior", "expressions", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["flagged"] == [["🤔", "😐", 0.12]]


def test_expressions_check_flags_a_near_duplicate_text(monkeypatch, capsys) -> None:
    monkeypatch.setattr(behavior_cmd, "_find_too_similar", lambda cat: [("🤔", "😐", 0.12)])
    rc = main(["behavior", "expressions", "check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "🤔" in out and "😐" in out


# --------------------------------------------------------------------------- #
# behavior expressions overview                                              #
# --------------------------------------------------------------------------- #


def test_expressions_overview_text(capsys) -> None:
    rc = main(["behavior", "expressions", "overview"])
    assert rc == 0
    assert "expressions" in capsys.readouterr().out.lower()


def test_expressions_overview_json(capsys) -> None:
    rc = main(["behavior", "expressions", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli behavior expressions"
    verbs = "\n".join(payload["sections"][1]["items"])
    assert "expressions check" in verbs
    assert "expressions overview" in verbs


def test_expressions_overview_reachable_via_behavior_overview(capsys) -> None:
    assert main(["behavior", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = "\n".join(payload["sections"][0]["items"])
    assert "behavior expressions" in verbs


# --------------------------------------------------------------------------- #
# explain catalog wiring                                                     #
# --------------------------------------------------------------------------- #


def test_catalog_has_expressions_entries() -> None:
    paths = known_paths()
    assert ("behavior", "expressions") in paths
    assert ("behavior", "expressions", "list") in paths
    assert ("behavior", "expressions", "check") in paths
    assert ("behavior", "expressions", "overview") in paths


def test_explain_resolves_expressions_paths(capsys) -> None:
    for path in [
        ("behavior", "expressions"),
        ("behavior", "expressions", "list"),
        ("behavior", "expressions", "check"),
        ("behavior", "expressions", "overview"),
    ]:
        rc = main(["explain", *path])
        assert rc == 0
        capsys.readouterr()
