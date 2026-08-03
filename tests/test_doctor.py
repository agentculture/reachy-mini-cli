"""Tests for the `doctor` `sense_extras` check (issue #120a, task t2).

`_diagnose()` takes an injectable `cv2_probe` seam expressly so these tests
never have to simulate a missing module by evicting `cv2` (or anything else)
from `sys.modules` — this repo has already been bitten by that pattern once
(the sleep-mode boundary tests): eviction pollutes sibling tests because the
suite runs under `pytest -n auto`, where many tests share one worker process.
Every test below either passes a fake probe directly (the preferred seam) or
monkeypatches the plain `_cv2_available` module attribute — which `_diagnose`
looks up fresh at call time rather than binding as a default argument, so the
monkeypatch reaches the real `cmd_doctor` / `main()` entry point too. Neither
approach touches `sys.modules`.
"""

from __future__ import annotations

import json
import sys

import pytest

from reachy.cli import main
from reachy.cli._commands import doctor as doctor_module
from reachy.cli._commands.doctor import _diagnose


def _find(checks: list[dict[str, object]], check_id: str) -> dict[str, object]:
    for check in checks:
        if check["id"] == check_id:
            return check
    raise AssertionError(f"no check named {check_id!r} among {[c['id'] for c in checks]!r}")


# --------------------------------------------------------------------------- #
# Criterion 1 — cv2 absent: sense_extras fails, severity warning, remediation #
# names BOTH the pip and the uv tool install forms                           #
# --------------------------------------------------------------------------- #


def test_sense_extras_fails_when_cv2_probe_reports_absent() -> None:
    report = _diagnose(cv2_probe=lambda: False)
    check = _find(report["checks"], "sense_extras")

    assert check["passed"] is False
    assert check["severity"] == "warning"


def test_sense_extras_remediation_names_both_install_forms_when_cv2_absent() -> None:
    report = _diagnose(cv2_probe=lambda: False)
    check = _find(report["checks"], "sense_extras")

    assert "pip install 'reachy-mini-cli[vision]'" in check["remediation"]
    assert 'uv tool install --force --editable ".[daemon,vision]"' in check["remediation"]


def test_uv_tool_install_extra_flag_form_is_never_suggested() -> None:
    """Regression guard for the exact defect in #120: `uv tool install` has no
    `--extra` flag (it errors: unexpected argument '--extra'). The remediation
    must give the working spec-embedded form, never the natural-looking-but-wrong
    `--extra vision`, which is how the deployed box ended up without the extra."""
    report = _diagnose(cv2_probe=lambda: False)
    check = _find(report["checks"], "sense_extras")

    assert "--extra vision" not in check["remediation"]
    assert '".[daemon,vision]"' in check["remediation"]


def test_sense_extras_failure_makes_the_whole_report_unhealthy() -> None:
    report = _diagnose(cv2_probe=lambda: False)
    assert report["healthy"] is False


def test_doctor_json_reports_sense_extras_failed_without_cv2(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """CLI-level plumbing, exercised through the real `main()` entry point."""
    monkeypatch.setattr(doctor_module, "_cv2_available", lambda: False)

    rc = main(["doctor", "--json"])

    assert rc == 1
    payload = json.loads(capsys.readouterr().out)
    check = _find(payload["checks"], "sense_extras")
    assert check["passed"] is False
    assert check["severity"] == "warning"
    assert "pip install 'reachy-mini-cli[vision]'" in check["remediation"]
    assert 'uv tool install --force --editable ".[daemon,vision]"' in check["remediation"]


# --------------------------------------------------------------------------- #
# Criterion 2 — cv2 present: sense_extras passes; text mode renders the      #
# [ok] / [FAIL] lines correctly                                              #
# --------------------------------------------------------------------------- #


def test_sense_extras_passes_when_cv2_probe_reports_present() -> None:
    report = _diagnose(cv2_probe=lambda: True)
    check = _find(report["checks"], "sense_extras")

    assert check["passed"] is True
    assert check["remediation"] == ""


def test_doctor_text_renders_ok_line_for_sense_extras_when_cv2_present(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "_cv2_available", lambda: True)

    rc = main(["doctor"])

    out = capsys.readouterr().out
    assert "[ok] sense_extras: [vision] extra (opencv) installed" in out
    # rc still reflects this checkout's OTHER identity checks (prompt file,
    # skills dir) — sense_extras passing does not guarantee overall health.
    assert rc in (0, 1)


def test_doctor_text_renders_fail_line_and_hint_for_sense_extras_when_cv2_absent(
    capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(doctor_module, "_cv2_available", lambda: False)

    rc = main(["doctor"])

    out = capsys.readouterr().out
    assert rc == 1
    assert "[FAIL] sense_extras: [vision] extra (opencv) missing" in out
    assert "  hint: pip install 'reachy-mini-cli[vision]'; on a `uv tool install` deploy" in out
    assert 'uv tool install --force --editable ".[daemon,vision]"' in out


# --------------------------------------------------------------------------- #
# The injection seam itself                                                  #
# --------------------------------------------------------------------------- #


def test_injected_cv2_probe_is_the_one_actually_consulted() -> None:
    calls: list[None] = []

    def fake_probe() -> bool:
        calls.append(None)
        return True

    report = _diagnose(cv2_probe=fake_probe)

    assert calls == [None]
    assert _find(report["checks"], "sense_extras")["passed"] is True


def test_default_cv2_available_probe_never_causes_a_real_cv2_import() -> None:
    """`find_spec` locates a module without executing it — the probe must never
    make `cv2` appear in `sys.modules` as a side effect of merely checking."""
    had_cv2_before = "cv2" in sys.modules

    doctor_module._cv2_available()

    assert ("cv2" in sys.modules) == had_cv2_before


def test_doctor_json_shape_includes_sense_extras_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """No injected probe at all — the real, unmocked default path (this dev
    environment does not install [vision] by default, but either outcome is a
    valid `passed` value; the point is the check is always present)."""
    rc = main(["doctor", "--json"])

    assert rc in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    check = _find(payload["checks"], "sense_extras")
    assert isinstance(check["passed"], bool)
    assert check["severity"] == "warning"


# --------------------------------------------------------------------------- #
# The model-pair check (task t14, spec assumption c47)                        #
#                                                                             #
# Gemma is named in TWO configs and only one of them lives in this repo: the  #
# realtime service's own ``OPENAI_MODEL`` decides which model SPEAKS, and     #
# ``REACHY_EMBODY_SENSES_MODEL`` decides which model answers the layer's      #
# perception questions. They are a documented pair; the worker lane is the    #
# BACKGROUND mind and is expected to be a different model entirely, which is  #
# why it is named but never compared.                                         #
# --------------------------------------------------------------------------- #


def test_model_pair_check_is_present_and_names_both_halves_of_the_pair() -> None:
    report = _diagnose(env={})
    check = _find(report["checks"], "model_pair")

    assert check["severity"] == "warning"
    assert doctor_module.ENV_VOICE_MODEL in str(check["message"])
    assert doctor_module.ENV_SENSES_MODEL in str(check["message"])


def test_model_pair_passes_when_nothing_is_configured() -> None:
    """The state every box is in today: the gateway holds its own OPENAI_MODEL
    and the layer resolves the `senses` ROLE. Additive means additive — a
    healthy box must not become unhealthy just because this check landed."""
    report = _diagnose(env={}, cv2_probe=lambda: True)

    assert _find(report["checks"], "model_pair")["passed"] is True
    assert report["healthy"] is True


def test_model_pair_warns_when_the_voice_and_senses_models_diverge() -> None:
    report = _diagnose(
        env={
            "OPENAI_MODEL": "coolthor/gemma-4-12B-it-NVFP4A16",
            "REACHY_EMBODY_SENSES_MODEL": "qwen3",
        }
    )
    check = _find(report["checks"], "model_pair")

    assert check["passed"] is False
    assert "coolthor/gemma-4-12B-it-NVFP4A16" in str(check["message"])
    assert "qwen3" in str(check["message"])
    assert check["remediation"]


def test_model_pair_passes_when_both_name_the_same_model() -> None:
    report = _diagnose(
        env={
            "OPENAI_MODEL": "coolthor/gemma-4-12B-it-NVFP4A16",
            "REACHY_EMBODY_SENSES_MODEL": "coolthor/gemma-4-12B-it-NVFP4A16",
        }
    )

    assert _find(report["checks"], "model_pair")["passed"] is True


def test_a_role_alias_is_never_read_as_a_divergent_model_name() -> None:
    """`senses` is a ROUTING ALIAS the gateway resolves, not a served model id.
    Comparing it as a string against a served id would warn on the one
    configuration the layer documents as correct."""
    report = _diagnose(
        env={
            "OPENAI_MODEL": "coolthor/gemma-4-12B-it-NVFP4A16",
            "REACHY_EMBODY_SENSES_MODEL": "senses",
        }
    )

    assert _find(report["checks"], "model_pair")["passed"] is True


def test_the_worker_lane_is_named_but_never_compared() -> None:
    """The two-tempo split means the BACKGROUND model differs on purpose
    (Qwen thinks, Gemma speaks). A check that flagged that would be telling
    the operator to undo the architecture."""
    report = _diagnose(
        env={
            "OPENAI_MODEL": "gemma",
            "REACHY_EMBODY_SENSES_MODEL": "gemma",
            "REACHY_EMBODY_WORKER_MODEL": "qwen3",
        }
    )
    check = _find(report["checks"], "model_pair")

    assert check["passed"] is True
    assert "qwen3" in str(check["message"])


def test_model_pair_reads_the_injected_env_not_the_process_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPENAI_MODEL", "from-the-process")
    monkeypatch.setenv("REACHY_EMBODY_SENSES_MODEL", "something-else")

    assert _find(_diagnose(env={})["checks"], "model_pair")["passed"] is True


def test_doctor_json_shape_includes_model_pair_id(
    capsys: pytest.CaptureFixture[str],
) -> None:
    rc = main(["doctor", "--json"])

    assert rc in (0, 1)
    payload = json.loads(capsys.readouterr().out)
    check = _find(payload["checks"], "model_pair")
    assert isinstance(check["passed"], bool)
    assert check["severity"] == "warning"


# --------------------------------------------------------------------------- #
# Drift guard — doctor may not import the layer, so it restates two names     #
# --------------------------------------------------------------------------- #


def test_the_layer_env_names_doctor_restates_match_the_layers_own() -> None:
    """`doctor` is imported by `_build_parser()`, and `reachy.embody.engine`
    reaches `reachy.speech.llm` — so importing the engine here would put the
    LLM client in the import path of `--help`
    (`tests/test_zero_llm_boundary.py::test_building_the_cli_parser_loads_no_
    cognition_module`). The names are therefore restated as literals, and this
    pins them equal to their one real home, the way
    `DEFAULT_PERCEPTION_STALE_AFTER_S` is pinned to `DEFAULT_CLIP_STALE_
    AFTER_S`."""
    from reachy.embody import engine as embody_engine

    assert doctor_module.ENV_SENSES_MODEL == embody_engine.ENV_SENSES_MODEL
    assert doctor_module.ENV_WORKER_MODEL == embody_engine.ENV_WORKER_MODEL
    assert doctor_module.MODEL_ROLE_ALIASES == embody_engine.ROLES


def test_doctor_does_not_import_the_embody_layer_at_module_scope() -> None:
    """The reason the names above are restated at all."""
    import ast
    from pathlib import Path

    tree = ast.parse(Path(doctor_module.__file__).read_text(encoding="utf-8"))
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.col_offset == 0:
            imported.append(node.module or "")
        elif isinstance(node, ast.Import) and node.col_offset == 0:
            imported.extend(alias.name for alias in node.names)

    assert not [name for name in imported if name.startswith("reachy.embody")], imported
