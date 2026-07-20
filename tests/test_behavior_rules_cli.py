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
duration_s = 30.0

[[inhibit]]
id = "i1"
when = { field = "doa", op = "gt", value = 0.5 }
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
    assert payload["warnings"] == []
    assert payload["counts"] == {"react": 0, "inhibit": 0, "modes": 0}


def test_rules_check_valid_file_ok_true_with_counts(capsys) -> None:
    _write_rules(GOOD_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["exists"] is True
    assert payload["reasons"] == []
    assert payload["warnings"] == []
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
    assert payload["warnings"] == []
    assert "counts" not in payload


def test_rules_check_malformed_file_text_mode_still_exits_zero(capsys) -> None:
    _write_rules(BROKEN_TOML)
    rc = main(["behavior", "rules", "check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "mystery" in out or "false" in out.lower() or "ok" in out.lower()


# --------------------------------------------------------------------------- #
# behavior rules check — unfed sense-field warnings (t16, issue: silent no-op #
# rules that validate cleanly but can never fire)                            #
# --------------------------------------------------------------------------- #

# `rms` is a schema-valid predicate field (reachy.behavior.rules.SENSE_FIELDS)
# that a composition may or may not feed (reachy.behavior.sense.FED_SENSE_FIELDS).
#
# Since t28 wired the last providers, EVERY schema-valid field is fed, so no real
# field reproduces the unfed case any more — the tests below shrink
# `FED_SENSE_FIELDS` to simulate one. That is precisely the drift the warning now
# guards against: a field added to `SENSE_FIELDS` with no provider wired for it
# in the composition root, leaving a rule that validates cleanly and can never
# fire.
UNFED_FIELD_TOML = """\
[[inhibit]]
id = "i-rms"
when = { field = "rms", op = "gt", value = 0.5 }
disable = ["speak"]
"""

# `doa` IS fed (the base DoA/speech leg is unconditional) — a control fixture
# proving a rule on a fed field never warns.
FED_FIELD_TOML = """\
[[react]]
id = "r-doa"
when = { field = "doa", op = "is_true" }
run = "nod"
duration_s = 5.0
"""

# One rule on a fed field ("doa") and one on an unfed field ("face") in the
# same file — only the unfed one should be reported.
MIXED_FIELD_TOML = """\
[[react]]
id = "r-doa"
when = { field = "doa", op = "is_true" }
run = "nod"
duration_s = 5.0

[[inhibit]]
id = "i-face"
when = { field = "face", op = "eq", value = "Ada" }
disable = ["speak"]
"""


def _simulate_unfed(monkeypatch, *fields: str) -> None:
    """Pretend the composition feeds everything EXCEPT *fields*.

    ``_unfed_field_warnings`` reads the module-global ``FED_SENSE_FIELDS``, so
    shrinking it here exercises the real linter against the real rules file.
    """
    from reachy.behavior.sense import FED_SENSE_FIELDS

    monkeypatch.setattr(
        "reachy.cli._commands.behavior.FED_SENSE_FIELDS",
        FED_SENSE_FIELDS - frozenset(fields),
    )


def test_rules_check_warns_on_a_rule_keyed_to_an_unfed_field_json(capsys, monkeypatch) -> None:
    _simulate_unfed(monkeypatch, "rms")
    _write_rules(UNFED_FIELD_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0  # a warning, not a failure
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False  # mirrors 'think expressions check': ok := no warnings
    assert payload["reasons"] == []  # not a schema/validation error
    assert len(payload["warnings"]) == 1
    warning = payload["warnings"][0]
    # names the field ...
    assert "rms" in warning
    # ... and the offending rule ...
    assert "i-rms" in warning
    # ... and WHY it cannot fire.
    assert "never fire" in warning or "cannot fire" in warning
    # the file itself is still perfectly valid TOML/schema — counts are present.
    assert payload["counts"] == {"react": 0, "inhibit": 1, "modes": 0}


def test_rules_check_warns_on_a_rule_keyed_to_an_unfed_field_text_mode(capsys, monkeypatch) -> None:
    _simulate_unfed(monkeypatch, "rms")
    _write_rules(UNFED_FIELD_TOML)
    rc = main(["behavior", "rules", "check"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "rms" in out
    assert "i-rms" in out


def test_rules_check_a_rule_on_a_fed_field_never_warns(capsys) -> None:
    _write_rules(FED_FIELD_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["warnings"] == []


def test_rules_check_only_the_unfed_rule_is_flagged_in_a_mixed_file(capsys, monkeypatch) -> None:
    _simulate_unfed(monkeypatch, "face")
    _write_rules(MIXED_FIELD_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert len(payload["warnings"]) == 1
    assert "face" in payload["warnings"][0]
    assert "i-face" in payload["warnings"][0]
    assert "r-doa" not in payload["warnings"][0]


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


# --------------------------------------------------------------------------- #
# behavior rules check — uncorroborated `speech` warnings                     #
# (retire-the-old-ai-first-flow t9, acceptance criterion 2)                   #
# --------------------------------------------------------------------------- #
#
# Measured on the deployed robot in a QUIET room with nobody speaking, 120
# samples over 60 s (docs/verification/2026-07-20-retire-old-flow-baseline.md
# section 2): `speech_detected` read True 55/120 = 45.8 % of the time, with the
# bearing wandering the full 0.000-3.124 rad range. A rule keyed on it fires on
# roughly a coin flip, pointed at nothing.
#
# A `Rule` carries exactly ONE `when` predicate (there is no conjunction in the
# schema), so ANY rule whose `when.field` is "speech" is by construction keyed
# on BARE `speech_detected` — the check needs no cross-predicate analysis.

SPEECH_ONLY_TOML = """\
[[react]]
id = "r-speech"
when = { field = "speech", op = "is_true" }
run = "nod"
duration_s = 5.0
"""

CORROBORATED_TOML = """\
[[react]]
id = "r-heard"
when = { field = "transcript", op = "is_true" }
run = "nod"
duration_s = 5.0
"""


def test_rules_check_warns_on_a_rule_keyed_on_bare_speech(capsys) -> None:
    _write_rules(SPEECH_ONLY_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0  # a warning, not a gate
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["reasons"] == []  # schema-valid; this is a LINT finding
    assert len(payload["warnings"]) == 1
    warning = payload["warnings"][0]
    assert "r-speech" in warning
    assert "speech" in warning
    assert "45.8" in warning  # cites the measurement, not just an opinion
    assert "corroborat" in warning  # names the requirement


def test_the_uncorroborated_warning_names_a_corroborating_field(capsys) -> None:
    _write_rules(SPEECH_ONLY_TOML)
    main(["behavior", "rules", "check", "--json"])
    warning = json.loads(capsys.readouterr().out)["warnings"][0]
    assert any(f in warning for f in ("transcript", "rms"))


def test_a_rule_keyed_on_a_corroborating_field_earns_no_such_warning(capsys) -> None:
    _write_rules(CORROBORATED_TOML)
    rc = main(["behavior", "rules", "check", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["warnings"] == []


def test_rules_check_text_mode_surfaces_the_uncorroborated_warning(capsys) -> None:
    _write_rules(SPEECH_ONLY_TOML)
    assert main(["behavior", "rules", "check"]) == 0
    out = capsys.readouterr().out
    assert "r-speech" in out
    assert "45.8" in out


def test_an_inhibit_rule_keyed_on_bare_speech_is_flagged_too(capsys) -> None:
    _write_rules("""\
[[inhibit]]
id = "i-speech"
when = { field = "speech", op = "is_false" }
disable = ["speak"]
""")
    main(["behavior", "rules", "check", "--json"])
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["warnings"]) == 1
    assert "i-speech" in payload["warnings"][0]


def test_no_shipped_rule_keys_on_an_uncorroborated_sense_field() -> None:
    """Acceptance criterion 2, pinned where it is scoped.

    The operator's own overlay only earns a WARNING (see the module note above
    and ``_uncorroborated_field_warnings``' docstring for why refusal would be
    the wrong trade on a boot-persistent robot). The SHIPPED layer is different:
    it is ours, it lands on every robot on upgrade, and nobody is watching a
    linter when it does — so it is enforced HARD, here, and a future task that
    ships such a rule fails CI.
    """
    shipped = rules_mod.load_shipped_rules()
    offenders = [
        rule.id
        for rule in (*shipped.react, *shipped.inhibit)
        if rule.when.field in rules_mod.UNCORROBORATED_SENSE_FIELDS
    ]
    assert offenders == [], (
        f"shipped rules {offenders} key on a bare uncorroborated sense field; "
        "measured at 45.8% true in a quiet room — pair it with a corroborating "
        "signal (transcript/rms/face/pat) instead"
    )
