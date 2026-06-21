"""Tests for ``reachy.service.units`` — pure systemd ``--user`` unit rendering.

These assert the rendered unit text field-by-field (parse the INI-ish unit text
into sections/directives and check each one) so the contract is exact, with
fixed injected python / daemon-binary paths rather than a machine-absolute path.
"""

from __future__ import annotations

import sys

import pytest

from reachy.service import units

# Fixed, injected values so assertions are exact regardless of the host.
PY = "/opt/venv/bin/python3"
DAEMON_BIN = "/opt/venv/bin/reachy-mini-daemon"
CFG = "/home/op/.config/reachy/demo-mode.json"


def parse_unit(text: str) -> dict[str, dict[str, list[str]]]:
    """Parse systemd unit text into ``{section: {key: [values...]}}``.

    Repeated keys (legal in systemd, e.g. multiple ``After=``) accumulate.
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
# Canonical unit-name constants (cross-task contract).
# --------------------------------------------------------------------------- #


def test_unit_name_constants():
    assert units.DAEMON_UNIT == "reachy-daemon.service"
    assert units.DEMO_UNIT == "reachy-demo-mode.service"
    assert units.LIVE_UNIT == "reachy-live.service"


# --------------------------------------------------------------------------- #
# Shared shape every unit must satisfy (criterion 1).
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "text",
    [
        units.daemon_unit_text(daemon_cmd=DAEMON_BIN),
        units.demo_unit_text(python=PY, config_file=CFG),
        units.live_unit_text(python=PY),
    ],
)
def test_common_shape(text):
    sec = parse_unit(text)
    assert set(sec) == {"Unit", "Service", "Install"}
    # Description present and non-empty.
    assert sec["Unit"]["Description"], "missing Description="
    assert sec["Unit"]["Description"][0].strip()
    # network-online ordering.
    assert any("network-online.target" in v for v in sec["Unit"]["After"])
    # Service block invariants.
    assert sec["Service"]["Type"] == ["simple"]
    assert sec["Service"]["Restart"] == ["on-failure"]
    assert sec["Service"]["RestartSec"] == ["5"]
    assert "ExecStart" in sec["Service"]
    # Install block.
    assert sec["Install"]["WantedBy"] == ["default.target"]


# --------------------------------------------------------------------------- #
# Daemon unit (criterion 2: runs the reachy-mini-daemon binary).
# --------------------------------------------------------------------------- #


def test_daemon_exec_runs_binary():
    text = units.daemon_unit_text(daemon_cmd=DAEMON_BIN)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    assert exec_start == f'"{DAEMON_BIN}"'
    # The daemon unit does NOT depend on itself.
    assert "Requires" not in sec["Unit"]


def test_daemon_default_resolves_binary():
    # With no injected path, the default is derived (PATH lookup or bare name),
    # and the binary name appears in the rendered ExecStart.
    text = units.daemon_unit_text()
    sec = parse_unit(text)
    assert "reachy-mini-daemon" in sec["Service"]["ExecStart"][0]


# --------------------------------------------------------------------------- #
# Demo presence unit (criterion 2: depends on daemon; runs demo-mode run).
# --------------------------------------------------------------------------- #


def test_demo_exec_runs_module():
    text = units.demo_unit_text(python=PY, config_file=CFG)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    assert exec_start == f'"{PY}" -m reachy demo-mode run --config "{CFG}"'


def test_demo_requires_and_after_daemon():
    text = units.demo_unit_text(python=PY, config_file=CFG)
    sec = parse_unit(text)
    assert sec["Unit"]["Requires"] == [units.DAEMON_UNIT]
    after_values = " ".join(sec["Unit"]["After"])
    assert units.DAEMON_UNIT in after_values
    assert "network-online.target" in after_values


def test_demo_default_python_is_running_interpreter():
    text = units.demo_unit_text(config_file=CFG)
    sec = parse_unit(text)
    assert sys.executable in sec["Service"]["ExecStart"][0]


# --------------------------------------------------------------------------- #
# Live presence unit (criterion 2: folded live loop; depends on daemon).
# --------------------------------------------------------------------------- #


def test_live_exec_runs_listen_live():
    text = units.live_unit_text(python=PY)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    # The deployed boot presence opts into --transcribe so on-robot it hears words
    # (the CLI default stays off; the unit opts in).
    assert exec_start == f'"{PY}" -m reachy listen run --live --transcribe'


def test_live_requires_and_after_daemon():
    text = units.live_unit_text(python=PY)
    sec = parse_unit(text)
    assert sec["Unit"]["Requires"] == [units.DAEMON_UNIT]
    after_values = " ".join(sec["Unit"]["After"])
    assert units.DAEMON_UNIT in after_values
    assert "network-online.target" in after_values


# --------------------------------------------------------------------------- #
# Quoting / escaping (mirror demo_service grammar): spaces, %, ", backslash.
# --------------------------------------------------------------------------- #


def test_exec_args_are_quoted_and_escaped():
    weird = "/path with space/py%thon"
    text = units.live_unit_text(python=weird)
    sec = parse_unit(text)
    exec_start = sec["Service"]["ExecStart"][0]
    # space preserved inside quotes, % doubled for systemd specifier grammar.
    assert exec_start.startswith('"/path with space/py%%thon"')
