"""Tests for the ``reachy-runtime.service`` unit renderer (t10, decision c19).

Mirrors ``tests/test_service_units.py``'s style (field-by-field parsing, fixed
injected paths) for the new AI-agnostic runtime presence unit. The load-bearing
assertion is acceptance criterion 1: the rendered ExecStart runs the
deterministic ``behavior engine run`` loop — no LLM flag, no ``REACHY_OPENAI``
reference anywhere in the unit text.
"""

from __future__ import annotations

import sys

from reachy.service import units

# Fixed, injected value so assertions are exact regardless of the host.
PY = "/opt/venv/bin/python3"


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """Parse systemd unit text into ``{section: {key: [values...]}}``.

    Same parser as ``test_service_units.py`` (duplicated intentionally — new
    test files stay self-contained rather than importing fixtures across test
    modules).
    """
    sections: dict[str, dict[str, list[str]]] = {}
    current: str | None = None
    for raw in text.splitlines():
        line = raw.rstrip("\n")
        if not line.strip():
            continue
        if line.startswith("[") and line.endswith("]"):
            current = line[1:-1]
            sections.setdefault(current, {})
            continue
        assert current is not None, f"directive before any section: {line!r}"
        assert "=" in line, f"not a directive: {line!r}"
        key, _, value = line.partition("=")
        sections[current].setdefault(key, []).append(value)
    return sections


# --------------------------------------------------------------------------- #
# Canonical unit name.
# --------------------------------------------------------------------------- #


def test_runtime_unit_name_constant():
    assert units.RUNTIME_UNIT == "reachy-runtime.service"


# --------------------------------------------------------------------------- #
# Criterion 1: ExecStart runs the AI-agnostic behavior runtime, no LLM/agent
# flag or REACHY_OPENAI reference ANYWHERE in the unit text.
# --------------------------------------------------------------------------- #


def test_runtime_exec_runs_behavior_engine():
    text = units.runtime_unit_text(python=PY)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    assert exec_start == f'"{PY}" -m reachy behavior engine run'


def test_runtime_exec_start_helper_matches_unit_text():
    assert units.runtime_exec_start(python=PY) == f'"{PY}" -m reachy behavior engine run'


def test_runtime_unit_text_has_no_llm_or_openai_reference():
    text = units.runtime_unit_text(python=PY)
    forbidden = (
        "REACHY_OPENAI",
        "--cognition",
        "--transcribe",
        "--voice-engine",
        "listen",
        "agent",
        "harmonic",
        "llm",
        "LLM",
    )
    for token in forbidden:
        assert token not in text, f"unexpected {token!r} in runtime unit text:\n{text}"


def test_runtime_exec_start_has_no_llm_or_openai_reference():
    # Belt-and-suspenders: check the raw ExecStart line alone too, not just the
    # full rendered unit (in case a future Description= ever mentions a tool).
    exec_start = units.runtime_exec_start(python=PY)
    for token in ("REACHY_OPENAI", "--cognition", "--transcribe", "--voice-engine"):
        assert token not in exec_start


def test_runtime_default_python_is_running_interpreter():
    text = units.runtime_unit_text()
    sec = parse_unit(text)
    assert sys.executable in sec["Service"]["ExecStart"][0]


# --------------------------------------------------------------------------- #
# Shared shape (mirrors test_service_units.py::test_common_shape).
# --------------------------------------------------------------------------- #


def test_runtime_common_shape():
    text = units.runtime_unit_text(python=PY)
    sec = parse_unit(text)
    assert set(sec) == {"Unit", "Service", "Install"}
    assert sec["Unit"]["Description"], "missing Description="
    assert sec["Unit"]["Description"][0].strip()
    assert any("network-online.target" in v for v in sec["Unit"]["After"])
    assert sec["Service"]["Type"] == ["simple"]
    assert sec["Service"]["Restart"] == ["on-failure"]
    assert sec["Service"]["RestartSec"] == ["5"]
    assert "ExecStart" in sec["Service"]
    assert sec["Install"]["WantedBy"] == ["default.target"]


# --------------------------------------------------------------------------- #
# Boot dependency: Requires=/After= the daemon unit, like the sibling presences.
# --------------------------------------------------------------------------- #


def test_runtime_requires_and_after_daemon():
    text = units.runtime_unit_text(python=PY)
    sec = parse_unit(text)
    assert sec["Unit"]["Requires"] == [units.DAEMON_UNIT]
    after_values = " ".join(sec["Unit"]["After"])
    assert units.DAEMON_UNIT in after_values
    assert "network-online.target" in after_values


# --------------------------------------------------------------------------- #
# Quoting/escaping matches the sibling renderers (shared _unit_arg helper).
# --------------------------------------------------------------------------- #


def test_runtime_exec_args_are_quoted_and_escaped():
    weird = "/path with space/py%thon"
    text = units.runtime_unit_text(python=weird)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    assert exec_start.startswith('"/path with space/py%%thon"')
