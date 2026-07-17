"""t9 — the ``behavior rules`` / ``behavior rules check`` CLI surface.

Two verbs, both pure file reads against ``reachy.behavior.rules`` (no running
engine needed):

* ``behavior rules`` (bare, or ``rules list``) — renders the loaded
  ``rules.toml``; a missing file is not an error, a malformed one is a clean
  exit-1 ``CliError`` (``load_rules`` already raises it — this verb is a
  straight read, not a lint).
* ``behavior rules check`` — a linter mirroring ``think expressions check``'s
  exit-0-warnings idiom: a malformed file reports ``ok=False`` + reasons but
  still exits 0; only a genuine I/O failure on an EXISTING path is a clean
  exit-2.

No real robot/daemon/engine process: every test only touches the filesystem
under an isolated ``REACHY_STATE_DIR``, mirroring ``tests/test_behavior_reload.py``.
"""

from __future__ import annotations

import json

import pytest

from reachy.behavior import rules as rules_mod
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_cmd
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.explain import known_paths

GOOD_TOML = """\
active_mode = "calm"

[[react]]
id = "r1"
when = { field = "doa", op = "absent_for", value = 0 }
run = "nod"
cooldown_s = 0

[[inhibit]]
id = "i1"
when = { field = "rms", op = "gt", value = 0.5 }
disable = ["speak"]

[modes.calm]
amp = 5
"""

# Deliberately broken: two unknown top-level fields (mirrors test_behavior_reload.py).
BROKEN_TOML = """\
mystery = 1
another_bad = 2
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


# --------------------------------------------------------------------------- #
# behavior rules / behavior rules list                                       #
# --------------------------------------------------------------------------- #


def test_rules_list_missing_file_reports_empty_config_json(capsys) -> None:
    rc = main(["behavior", "rules", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is False
    assert payload["react"] == []
    assert payload["inhibit"] == []
    assert payload["modes"] == {}
    assert payload["active_mode"] is None
    assert "note" in payload


def test_rules_list_and_explicit_list_verb_agree(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "rules", "--json"])
    assert rc == 0
    bare = json.loads(capsys.readouterr().out)

    rc = main(["behavior", "rules", "list", "--json"])
    assert rc == 0
    explicit = json.loads(capsys.readouterr().out)

    assert bare == explicit


def test_rules_list_renders_loaded_config_json(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "rules", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["exists"] is True
    assert "note" not in payload
    assert payload["active_mode"] == "calm"

    assert len(payload["react"]) == 1
    r = payload["react"][0]
    assert r["id"] == "r1"
    assert r["run"] == "nod"
    assert r["when"] == {"field": "doa", "op": "absent_for", "value": 0.0}
    assert r["cooldown_s"] == 0.0

    assert len(payload["inhibit"]) == 1
    i = payload["inhibit"][0]
    assert i["id"] == "i1"
    assert i["disable"] == ["speak"]

    assert payload["modes"] == {"calm": {"amp": 5.0}}


def test_rules_list_malformed_file_is_a_clean_exit_one(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "rules", "--json"])
    assert rc == 1
    err = capsys.readouterr().err
    assert '"mystery"' in err or "mystery" in err
    assert "another_bad" in err


def test_rules_list_malformed_file_text_mode_error_contract(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "rules"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "mystery" in err and "another_bad" in err


def test_rules_list_text_mode_smoke(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "rules"])
    assert rc == 0
    out = capsys.readouterr().out
    assert out.strip()
    assert "r1" in out


def test_rules_bad_flag_is_a_clean_cli_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["behavior", "rules", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


# --------------------------------------------------------------------------- #
# behavior rules check                                                       #
# --------------------------------------------------------------------------- #


def test_rules_check_missing_file_ok_true_json(capsys) -> None:
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["exists"] is False
    assert payload["reasons"] == []
    assert payload["counts"] == {"react": 0, "inhibit": 0, "modes": 0}


def test_rules_check_valid_file_ok_true_with_counts(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["exists"] is True
    assert payload["reasons"] == []
    assert payload["counts"] == {"react": 1, "inhibit": 1, "modes": 1}


def test_rules_check_malformed_file_still_exits_zero(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0  # a linter, not a gate
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["exists"] is True
    assert len(payload["reasons"]) == 1
    assert "mystery" in payload["reasons"][0]
    assert "another_bad" in payload["reasons"][0]
    assert "counts" not in payload


def test_rules_check_malformed_file_text_mode_still_exits_zero(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "rules", "check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mystery" in out or "false" in out.lower() or "ok" in out.lower()


def test_rules_check_payload_unreadable_path_raises_env_error(tmp_path) -> None:
    """The reader-injection seam: a genuine I/O failure on an EXISTING path is a
    clean exit-2 CliError, never folded into ok=False like a content problem."""
    path = _write_rules(GOOD_TOML)

    def _raising_reader(_p):
        raise OSError("permission denied")

    with pytest.raises(CliError) as exc:
        behavior_cmd._rules_check_payload(path, reader=_raising_reader)
    assert exc.value.code == EXIT_ENV_ERROR
    assert "could not be read" in exc.value.message


def test_rules_check_cli_propagates_env_error(monkeypatch, capsys) -> None:
    def _raise(*_a, **_k):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="rules file X could not be read: boom",
            remediation="check file permissions",
        )

    monkeypatch.setattr("reachy.cli._commands.behavior._rules_check_payload", _raise)
    rc = main(["behavior", "rules", "check"])
    assert rc == 2
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "could not be read" in err


# --------------------------------------------------------------------------- #
# behavior rules overview                                                    #
# --------------------------------------------------------------------------- #


def test_rules_overview_json(capsys) -> None:
    rc = main(["behavior", "rules", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli behavior rules"
    verbs = "\n".join(payload["sections"][1]["items"])
    assert "rules check" in verbs
    assert "rules overview" in verbs


def test_rules_overview_reachable_via_behavior_overview(capsys) -> None:
    assert main(["behavior", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = "\n".join(payload["sections"][0]["items"])
    assert "behavior rules" in verbs
    assert "behavior rules check" in verbs


# --------------------------------------------------------------------------- #
# explain catalog wiring                                                     #
# --------------------------------------------------------------------------- #


def test_catalog_has_rules_entries() -> None:
    paths = known_paths()
    assert ("behavior", "rules") in paths
    assert ("behavior", "rules", "check") in paths
    assert ("behavior", "rules", "overview") in paths


def test_explain_resolves_rules_paths(capsys) -> None:
    for path in [("behavior", "rules"), ("behavior", "rules", "check")]:
        rc = main(["explain", *path])
        assert rc == 0
        capsys.readouterr()
