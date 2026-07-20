"""CLI-level tests for the ``runtime`` presence mode (t10, decisions c19/c20).

Mirrors ``tests/test_cli_service.py``'s style (injected fake ``systemctl``
runner + temp ``XDG_CONFIG_HOME``, driven through ``reachy.cli.main``) for the
new ``service enable runtime`` verb and the now-four-unit ``install``/
``uninstall``. No real systemctl runs and no real systemd unit is ever enabled.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from reachy.cli import main
from reachy.cli._commands import service as service_cmd
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    RETIRED_UNITS,
    RUNTIME_UNIT,
)


class FakeSystemctl:
    """Recording fake for the production ``systemctl --user ...`` runner."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.query_results: dict[tuple[str, str], tuple[str, int]] = {}
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

    def set_enabled(self, unit: str, value: str) -> None:
        self.query_results[("is-enabled", unit)] = (value, 0 if value == "enabled" else 1)

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        rest = args[1:] if args and args[0] == "--user" else list(args)
        verb = rest[0] if rest else ""
        unit = rest[-1] if len(rest) > 1 else ""
        if (verb, unit) in self.fail:
            out, rc = self.fail[(verb, unit)]
            return subprocess.CompletedProcess(args, rc, stdout="", stderr=out)
        if verb in ("is-enabled", "is-active"):
            out, rc = self.query_results.get((verb, unit), ("unknown", 1))
            return subprocess.CompletedProcess(args, rc, stdout=out + "\n", stderr="")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    def verbs_for(self, *verbs: str) -> list[list[str]]:
        wanted = set(verbs)
        return [c for c in self.calls if len(c) > 1 and c[1] in wanted]


@pytest.fixture
def fake(monkeypatch, tmp_path):
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    return runner


def _unit_dir(tmp_path):
    return tmp_path / "config" / "systemd" / "user"


# --------------------------------------------------------------------------- #
# enable runtime
# --------------------------------------------------------------------------- #


def test_enable_runtime_dispatches_through_manager(fake, capsys, tmp_path):
    rc = main(["service", "enable", "runtime"])
    out, err = capsys.readouterr()
    assert rc == 0
    enabled = fake.verbs_for("enable")
    assert ["--user", "enable", "--now", DAEMON_UNIT] in enabled
    assert ["--user", "enable", "--now", RUNTIME_UNIT] in enabled
    # BOTH siblings disabled --now.
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    assert "runtime" in out
    assert err == ""
    assert (_unit_dir(tmp_path) / DAEMON_UNIT).is_file()
    assert (_unit_dir(tmp_path) / RUNTIME_UNIT).is_file()


def test_enable_runtime_json(fake, capsys):
    rc = main(["service", "enable", "runtime", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "enabled"
    assert payload["mode"] == "runtime"
    assert payload["presence_unit"] == RUNTIME_UNIT
    assert set(payload["disabled_siblings"]) == {DEMO_UNIT, LIVE_UNIT}
    assert err == ""


def test_enable_choices_include_runtime(fake, capsys):
    # "runtime" is a valid choice — no parse-time rejection.
    rc = main(["service", "enable", "runtime"])
    assert rc == 0


# --------------------------------------------------------------------------- #
# switching INTO and OUT OF runtime disables the right siblings each time.
# --------------------------------------------------------------------------- #


def test_switch_live_then_runtime_disables_both_others(fake, capsys):
    rc1 = main(["service", "enable", "live"])
    capsys.readouterr()
    assert rc1 == 0
    fake.calls.clear()

    rc2 = main(["service", "enable", "runtime"])
    out, err = capsys.readouterr()
    assert rc2 == 0
    assert ["--user", "enable", "--now", RUNTIME_UNIT] in fake.calls
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert err == ""


def test_switch_runtime_then_demo_disables_both_others(fake, capsys):
    main(["service", "enable", "runtime"])
    capsys.readouterr()
    fake.calls.clear()

    rc = main(["service", "enable", "demo"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert ["--user", "enable", "--now", DEMO_UNIT] in fake.calls
    assert ["--user", "disable", "--now", RUNTIME_UNIT] in fake.calls
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    assert err == ""


# --------------------------------------------------------------------------- #
# status includes the runtime unit
# --------------------------------------------------------------------------- #


def test_status_reports_runtime_mode(fake, capsys):
    fake.set_enabled(DAEMON_UNIT, "enabled")
    fake.set_enabled(RUNTIME_UNIT, "enabled")
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    rc = main(["service", "status", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "runtime"
    assert payload["presence_unit"] == RUNTIME_UNIT
    assert RUNTIME_UNIT in payload["units"]
    assert err == ""


# --------------------------------------------------------------------------- #
# install / uninstall — FOUR units, no enabling.
# --------------------------------------------------------------------------- #


def test_install_writes_all_four_units_without_enabling(fake, capsys, tmp_path):
    rc = main(["service", "install"])
    out, err = capsys.readouterr()
    assert rc == 0
    for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT):
        assert (_unit_dir(tmp_path) / unit).is_file(), f"{unit} not written by install"
    assert ["--user", "daemon-reload"] in fake.calls
    assert fake.verbs_for("enable") == []
    # install's only `disable` is the retired-unit migration (RETIRED_UNITS);
    # no unit in the CURRENT catalog is ever disabled by install.
    disabled = [c[-1] for c in fake.verbs_for("disable")]
    assert set(disabled) <= set(RETIRED_UNITS)
    assert not {DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT} & set(disabled)
    assert err == ""
    assert out != ""


def test_install_json_reports_four_unit_paths(fake, capsys):
    rc = main(["service", "install", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "installed"
    assert set(payload["unit_paths"]) == {DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT}
    assert err == ""


def test_uninstall_removes_all_four_units(fake, capsys, tmp_path):
    main(["service", "install"])
    capsys.readouterr()
    fake.calls.clear()

    rc = main(["service", "uninstall"])
    out, err = capsys.readouterr()
    assert rc == 0
    for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT):
        assert not (_unit_dir(tmp_path) / unit).is_file()
    assert ["--user", "daemon-reload"] in fake.calls
    assert err == ""


def test_uninstall_json_reports_four_removed(fake, capsys):
    main(["service", "install"])
    capsys.readouterr()
    rc = main(["service", "uninstall", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "uninstalled"
    assert set(payload["removed"]) == {DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT}
    assert err == ""


# --------------------------------------------------------------------------- #
# overview mentions the runtime unit.
# --------------------------------------------------------------------------- #


def test_overview_mentions_runtime(fake, capsys):
    rc = main(["service", "overview"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "runtime" in out
    assert RUNTIME_UNIT in out
    assert err == ""
