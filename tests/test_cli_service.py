"""Tests for the ``reachy service`` noun — the single boot-persistence surface.

Every test drives the CLI through ``reachy.cli.main`` with INJECTED seams so no
real ``systemctl`` runs and no real systemd unit is ever enabled:

* the production ``systemctl`` runner inside ``service.py`` is monkeypatched to a
  recording fake (records the exact arg vectors, serves canned query state);
* unit files are written into a temp dir (``XDG_CONFIG_HOME``), never the real
  ``~/.config/systemd/user``;
* the daemon-health probe is stubbed.

The output contract (results→stdout, errors+diagnostics→stderr, never mixed; the
two-line ``error:``/``hint:`` text shape; ``--json`` on every verb) is asserted
in both text and JSON modes via ``capsys``.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from reachy.cli import main
from reachy.cli._commands import service as service_cmd
from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR
from reachy.service.units import DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT

# --------------------------------------------------------------------------- #
# Fake systemctl runner — records arg vectors, serves canned query state.
# --------------------------------------------------------------------------- #


class FakeSystemctl:
    """Recording fake for the production ``systemctl --user ...`` runner.

    The manager prepends ``--user`` before calling, so every recorded call
    begins with ``--user``. Read-only queries (``is-enabled`` / ``is-active``)
    return canned stdout keyed by ``(verb, unit)``; mutating verbs return rc 0
    unless a failure is seeded.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.query_results: dict[tuple[str, str], tuple[str, int]] = {}
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

    def set_enabled(self, unit: str, value: str) -> None:
        self.query_results[("is-enabled", unit)] = (value, 0 if value == "enabled" else 1)

    def set_active(self, unit: str, value: str) -> None:
        self.query_results[("is-active", unit)] = (value, 0 if value == "active" else 3)

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
    """Inject the fake runner + a temp unit dir into the service command module.

    Returns the ``FakeSystemctl`` so a test can seed canned state and assert the
    exact dispatched command sequence. No real systemd is ever touched.
    """
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    # Replace the production runner so the manager shells out to the fake, and
    # stub the daemon-health probe so status() never hits the real daemon.
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    return runner


def _unit_dir(tmp_path):
    return tmp_path / "config" / "systemd" / "user"


# --------------------------------------------------------------------------- #
# overview
# --------------------------------------------------------------------------- #


def test_overview_text_describes_the_noun(fake, capsys):
    rc = main(["service", "overview"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert "service" in out.lower()
    # The verb surface is described.
    for verb in ("enable", "disable", "status", "install", "uninstall"):
        assert verb in out
    assert err == ""


def test_overview_json(fake, capsys):
    rc = main(["service", "overview", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["subject"] == "reachy-mini-cli service"
    assert isinstance(payload["sections"], list)
    assert err == ""


def test_bare_noun_prints_overview(fake, capsys):
    rc = main(["service"])
    out, _ = capsys.readouterr()
    assert rc == 0
    assert "service" in out.lower()


# --------------------------------------------------------------------------- #
# enable
# --------------------------------------------------------------------------- #


def test_enable_demo_dispatches_through_manager(fake, capsys, tmp_path):
    rc = main(["service", "enable", "demo"])
    out, err = capsys.readouterr()
    assert rc == 0
    # The chosen presence + daemon are enabled; the sibling (live) is disabled.
    enabled = [c for c in fake.verbs_for("enable")]
    assert ["--user", "enable", "--now", DAEMON_UNIT] in enabled
    assert ["--user", "enable", "--now", DEMO_UNIT] in enabled
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    # Result on stdout, nothing on stderr.
    assert "demo" in out
    assert err == ""
    # Unit files were written into the temp dir, not the real user-unit dir.
    assert (_unit_dir(tmp_path) / DAEMON_UNIT).is_file()
    assert (_unit_dir(tmp_path) / DEMO_UNIT).is_file()


def test_enable_live_dispatches_through_manager(fake, capsys):
    rc = main(["service", "enable", "live"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert ["--user", "enable", "--now", LIVE_UNIT] in fake.calls
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert "live" in out
    assert err == ""


def test_enable_json(fake, capsys):
    rc = main(["service", "enable", "demo", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "enabled"
    assert payload["mode"] == "demo"
    assert payload["presence_unit"] == DEMO_UNIT
    assert payload["disabled_sibling"] == LIVE_UNIT
    assert err == ""


def test_enable_invalid_mode_is_exit_1_user_error(fake, capsys):
    # An invalid mode is rejected at parse time (choices) — a structured user
    # error raised as SystemExit, never a traceback.
    with pytest.raises(SystemExit) as exc:
        main(["service", "enable", "bogus"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_enable_invalid_mode_json_is_structured(fake, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["service", "enable", "bogus", "--json"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    payload = json.loads(err)
    assert payload["code"] == EXIT_USER_ERROR
    assert "message" in payload


# --------------------------------------------------------------------------- #
# disable
# --------------------------------------------------------------------------- #


def test_disable_dispatches_through_manager(fake, capsys):
    fake.set_enabled(DEMO_UNIT, "enabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    rc = main(["service", "disable"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert err == ""
    assert "disabled" in out


def test_disable_json(fake, capsys):
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    rc = main(["service", "disable", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "disabled"
    # No presence enabled → nothing disabled, daemon left enabled.
    assert payload["disabled"] is None
    assert payload["daemon"] == "left-enabled"
    assert err == ""


# --------------------------------------------------------------------------- #
# status
# --------------------------------------------------------------------------- #


def test_status_reports_enabled_mode(fake, capsys):
    fake.set_enabled(DAEMON_UNIT, "enabled")
    fake.set_active(DAEMON_UNIT, "active")
    fake.set_enabled(LIVE_UNIT, "enabled")
    fake.set_active(LIVE_UNIT, "active")
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_active(DEMO_UNIT, "inactive")
    rc = main(["service", "status", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] == "live"
    assert payload["presence_unit"] == LIVE_UNIT
    assert payload["daemon_healthy"] is True
    assert payload["units"][DAEMON_UNIT]["enabled"] == "enabled"
    assert err == ""


def test_status_text(fake, capsys):
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    rc = main(["service", "status"])
    out, err = capsys.readouterr()
    assert rc == 0
    assert out != ""
    assert err == ""


# --------------------------------------------------------------------------- #
# install / uninstall — write/remove units WITHOUT enabling
# --------------------------------------------------------------------------- #


def test_install_writes_units_and_reloads_without_enabling(fake, capsys, tmp_path):
    rc = main(["service", "install"])
    out, err = capsys.readouterr()
    assert rc == 0
    # Both presence units + the daemon unit are written.
    for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT):
        assert (_unit_dir(tmp_path) / unit).is_file()
    # daemon-reload happened, but NO enable/disable.
    assert ["--user", "daemon-reload"] in fake.calls
    assert fake.verbs_for("enable") == []
    assert fake.verbs_for("disable") == []
    assert err == ""
    assert out != ""


def test_install_json(fake, capsys):
    rc = main(["service", "install", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] == "installed"
    assert err == ""


def test_uninstall_removes_units_and_reloads(fake, capsys, tmp_path):
    # Pre-create the units so uninstall has something to remove.
    main(["service", "install"])
    capsys.readouterr()
    fake.calls.clear()
    rc = main(["service", "uninstall"])
    out, err = capsys.readouterr()
    assert rc == 0
    for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT):
        assert not (_unit_dir(tmp_path) / unit).is_file()
    assert ["--user", "daemon-reload"] in fake.calls
    assert err == ""
    assert out != ""


def test_uninstall_json(fake, capsys):
    main(["service", "install"])
    capsys.readouterr()
    rc = main(["service", "uninstall", "--json"])
    out, err = capsys.readouterr()
    assert rc == 0
    payload = json.loads(out)
    assert payload["status"] in ("uninstalled", "not-installed")
    assert err == ""


# --------------------------------------------------------------------------- #
# error contract: missing systemctl, nested parse errors
# --------------------------------------------------------------------------- #


def test_missing_systemctl_is_clean_exit_2(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd.shutil, "which", lambda name: None)
    rc = main(["service", "enable", "demo"])
    out, err = capsys.readouterr()
    assert rc == EXIT_ENV_ERROR
    assert out == ""
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_missing_systemctl_json(monkeypatch, tmp_path, capsys):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd.shutil, "which", lambda name: None)
    rc = main(["service", "enable", "demo", "--json"])
    out, err = capsys.readouterr()
    assert rc == EXIT_ENV_ERROR
    assert out == ""
    payload = json.loads(err)
    assert payload["code"] == EXIT_ENV_ERROR
    assert "Traceback" not in err


def test_unknown_subverb_is_structured_error(fake, capsys):
    # Nested parse errors keep the structured CliError contract (not argparse's
    # default stderr/exit-2) because the noun group passes parser_class=type(p):
    # exit 1, two-line error:/hint:, no traceback.
    with pytest.raises(SystemExit) as exc:
        main(["service", "frobnicate"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_unknown_subverb_json(fake, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["service", "frobnicate", "--json"])
    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    payload = json.loads(err)
    assert "message" in payload


def test_systemctl_failure_surfaces_as_env_error(fake, capsys):
    # Seed a non-zero rc on the daemon-reload during enable.
    fake.fail[("daemon-reload", "")] = ("boom", 1)
    rc = main(["service", "enable", "demo"])
    out, err = capsys.readouterr()
    assert rc == EXIT_ENV_ERROR
    assert out == ""
    assert err.startswith("error:")
    assert "Traceback" not in err
