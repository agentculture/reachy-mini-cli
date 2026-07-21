"""Tests for the retired-unit cleanup migration (``RETIRED_UNITS``).

A unit name that leaves the catalog does NOT leave the deployed box: nothing
writes or removes unit files on ``pip upgrade``, and every install/enable path
only ever touches units still *in* the hardcoded catalog tuples. So a robot with
a since-retired unit enabled keeps a unit whose ``ExecStart`` names a deleted
subcommand — and with ``Restart=on-failure`` + ``RestartSec=5`` that is a
5-second crash loop, while ``service status`` cheerfully reports ``mode=None``
because the stale unit is no longer iterated. The operator's first diagnostic
lies to them.

These tests pin the three contract points that close that hole:

1. a box with a retired unit enabled ends with it **disabled**, its unit file
   **unlinked**, and its ``.d/`` drop-in directory **removed**;
2. ``status()`` never reports ``mode=None`` while a presence unit — retired or
   not — is still enabled;
3. all of it is driven through the injected ``run`` / ``unit_dir`` seams, so no
   real ``systemctl`` runs and no real ``~/.config/systemd/user`` is touched.

The scenario is parameterised on the retired NAME rather than hardcoding the
catalog, because the whole point of ``RETIRED_UNITS`` is that adding a name to
it is the only step a later removal needs. :data:`SYNTHETIC_RETIRED` — a name
in no catalog and on no box — is the injected retirement candidate for the
generic mechanism, so these tests keep proving the MECHANISM rather than any one
retirement. (They were originally written against ``LIVE_UNIT`` as a stand-in
for "a unit about to be retired"; ``t23`` actually retired it, and its specific
migration is covered in ``tests/test_service_live_retirement.py``.)

Tests that need the retired name to ALSO be a live catalog entry — proving
``RETIRED_UNITS`` wins over a lingering catalog tuple — inject ``DEMO_UNIT``
instead, since a synthetic name has no catalog entry to lose to.
"""

from __future__ import annotations

import json
import subprocess

import pytest

from reachy.cli import main
from reachy.cli._commands import service as service_cmd
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.service import units as units_mod
from reachy.service.manager import ServiceManager
from reachy.service.units import DEMO_UNIT, RETIRED_UNITS, RUNTIME_UNIT

#: A retirement candidate in NO catalog and on no real box — these tests prove
#: the migration MECHANISM, not any particular retirement.
SYNTHETIC_RETIRED = "reachy-legacy-presence.service"

# --------------------------------------------------------------------------- #
# Fake systemctl runner — records arg vectors, serves canned query state.
# --------------------------------------------------------------------------- #


class FakeSystemctl:
    """Recording fake for the ``systemctl --user ...`` seam.

    Mirrors the fakes in ``test_service_manager.py`` / ``test_cli_service.py``:
    read-only queries return canned stdout keyed by ``(verb, unit)``; mutating
    verbs record and succeed unless a failure is seeded. ``systemctl disable``
    on a unit that does not exist really does exit non-zero, so
    ``fail_unknown_units`` reproduces that — the migration must survive it.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.query_results: dict[tuple[str, str], tuple[str, int]] = {}
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

    def set_enabled(self, unit: str, value: str) -> None:
        self.query_results[("is-enabled", unit)] = (value, 0 if value == "enabled" else 1)

    def set_active(self, unit: str, value: str) -> None:
        self.query_results[("is-active", unit)] = (value, 0 if value == "active" else 3)

    def fail_verb(self, verb: str, unit: str, message: str = "Unit not loaded.") -> None:
        self.fail[(verb, unit)] = (message, 1)

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

    def disabled_units(self) -> list[str]:
        return [c[-1] for c in self.calls if len(c) >= 2 and c[1] == "disable"]


@pytest.fixture
def unit_dir(tmp_path):
    d = tmp_path / "systemd" / "user"
    d.mkdir(parents=True, exist_ok=True)
    return d


@pytest.fixture
def fake():
    return FakeSystemctl()


@pytest.fixture
def manager(fake, unit_dir):
    return ServiceManager(run=fake, unit_dir=unit_dir, daemon_health=lambda: True)


def _seed_deployed_unit(unit_dir, unit: str) -> tuple:
    """Reproduce a deployed box: a unit file PLUS its ``.d/`` drop-in dir.

    The deployed robot carries several drop-ins (a hardcoded-IP panel.conf, a
    load-bearing pat-sense.conf); a migration that unlinks the unit but leaves
    ``<unit>.d/`` behind leaves systemd carrying orphaned overrides forward.
    """
    path = unit_dir / unit
    path.write_text("[Unit]\nDescription=stale\n", encoding="utf-8")
    dropin = unit_dir / f"{unit}.d"
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "panel.conf").write_text("[Service]\nEnvironment=X=1\n", encoding="utf-8")
    return path, dropin


# --------------------------------------------------------------------------- #
# The constant itself.
# --------------------------------------------------------------------------- #


def test_retired_units_is_a_tuple_of_unit_names():
    """``RETIRED_UNITS`` is the single list a later removal appends one name to."""
    assert isinstance(RETIRED_UNITS, tuple)
    assert all(isinstance(name, str) and name.endswith(".service") for name in RETIRED_UNITS)


def test_retired_units_carries_the_known_orphan():
    """The hand-authored ``reachy-listen.service`` is genuinely retired.

    It was superseded by the CLI-generated ``reachy-live.service`` and still
    sits, enabled and in no catalog, on the deployed box — the real negative
    control for this whole migration.
    """
    assert "reachy-listen.service" in RETIRED_UNITS


def test_retired_units_excludes_units_still_in_the_catalog():
    """A surviving presence unit must never be listed as retired."""
    for still_live in (DEMO_UNIT, RUNTIME_UNIT):
        assert still_live not in RETIRED_UNITS


# --------------------------------------------------------------------------- #
# Criterion 1 — disabled, unlinked, and the .d/ directory removed.
# --------------------------------------------------------------------------- #


def test_cleanup_disables_unlinks_and_removes_the_dropin_dir(manager, fake, unit_dir):
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)
    fake.set_enabled(SYNTHETIC_RETIRED, "enabled")

    removed = manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,))

    assert SYNTHETIC_RETIRED in removed
    assert ["--user", "disable", "--now", SYNTHETIC_RETIRED] in fake.calls
    assert not path.exists(), "the retired unit file must be unlinked"
    assert not dropin.exists(), "the retired unit's .d/ drop-in dir must be removed"


def test_cleanup_disable_is_unconditional_even_with_no_file_on_disk(manager, fake, unit_dir):
    """``disable --now`` fires even when nothing is on disk.

    The unit may have been enabled from a package-managed path; the symlink in
    ``…/default.target.wants/`` outlives the file we can see.
    """
    removed = manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,))

    assert ["--user", "disable", "--now", SYNTHETIC_RETIRED] in fake.calls
    assert removed == []


def test_cleanup_survives_systemctl_failure_on_an_unknown_unit(manager, fake, unit_dir):
    """A real ``disable`` of a nonexistent unit exits non-zero — never fatal here."""
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)
    fake.fail_verb("disable", SYNTHETIC_RETIRED, "Failed to disable: Unit file does not exist.")

    removed = manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,))

    assert removed == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()


def test_cleanup_is_idempotent(manager, fake, unit_dir):
    _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)
    assert manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,)) == [SYNTHETIC_RETIRED]
    assert manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,)) == []


def test_cleanup_only_touches_retired_names(manager, fake, unit_dir):
    """The negative control: a still-catalogued unit is left completely alone."""
    keep, keep_dropin = _seed_deployed_unit(unit_dir, RUNTIME_UNIT)
    _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    manager.cleanup_retired_units(retired=(SYNTHETIC_RETIRED,))

    assert keep.exists()
    assert keep_dropin.exists()
    assert RUNTIME_UNIT not in fake.disabled_units()


def test_cleanup_defaults_to_the_module_constant(manager, fake, unit_dir, monkeypatch):
    """No argument → the real ``RETIRED_UNITS`` drives the migration."""
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    assert manager.cleanup_retired_units() == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()


# --------------------------------------------------------------------------- #
# Criterion 1 (wiring) — the migration runs from the ordinary service verbs.
# --------------------------------------------------------------------------- #


def test_enable_runs_the_retired_cleanup(manager, fake, unit_dir, monkeypatch):
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    result = manager.enable("runtime")

    assert result["retired_removed"] == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()
    # The chosen presence still comes up.
    assert result["mode"] == "runtime"
    assert (unit_dir / RUNTIME_UNIT).is_file()


def test_enable_cleanup_precedes_the_daemon_reload(manager, fake, unit_dir, monkeypatch):
    """Removal must land BEFORE ``daemon-reload`` so systemd sees the new truth."""
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    manager.enable("runtime")

    verbs = [c[1] for c in fake.calls if len(c) > 1]
    assert "disable" in verbs and "daemon-reload" in verbs
    assert verbs.index("disable") < verbs.index("daemon-reload")


def test_cli_install_runs_the_retired_cleanup(monkeypatch, tmp_path, capsys):
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    rc = main(["service", "install", "--json"])
    out, err = capsys.readouterr()

    assert rc == 0
    payload = json.loads(out)
    assert payload["retired_removed"] == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()
    assert err == ""


def test_cli_enable_runs_the_retired_cleanup(monkeypatch, tmp_path, capsys):
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    rc = main(["service", "enable", "runtime", "--json"])
    out, _ = capsys.readouterr()

    assert rc == 0
    assert json.loads(out)["retired_removed"] == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()


def test_cli_uninstall_runs_the_retired_cleanup(monkeypatch, tmp_path, capsys):
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    path, dropin = _seed_deployed_unit(unit_dir, SYNTHETIC_RETIRED)

    rc = main(["service", "uninstall", "--json"])
    out, _ = capsys.readouterr()

    assert rc == 0
    assert json.loads(out)["retired_removed"] == [SYNTHETIC_RETIRED]
    assert not path.exists()
    assert not dropin.exists()


# --------------------------------------------------------------------------- #
# RETIRED_UNITS is authoritative — a retired name is never re-written.
# --------------------------------------------------------------------------- #


def test_enable_never_rewrites_a_retired_unit(manager, fake, unit_dir, monkeypatch):
    """Retirement must win over a lingering catalog entry.

    ``enable`` writes EVERY presence unit (so the sibling-disable always targets
    an installed unit). If a retired name were still in that catalog, the write
    would resurrect the file the migration just deleted — one line out of order
    and the crash loop comes back. ``RETIRED_UNITS`` is the source of truth.

    ``DEMO_UNIT`` is injected as the retirement candidate precisely BECAUSE it
    is still catalogued: a synthetic name has no catalog entry for retirement to
    have to beat.
    """
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (DEMO_UNIT,), raising=True)
    _seed_deployed_unit(unit_dir, DEMO_UNIT)

    manager.enable("runtime")

    assert not (unit_dir / DEMO_UNIT).exists()
    assert not (unit_dir / f"{DEMO_UNIT}.d").exists()
    assert DEMO_UNIT not in manager.enable("runtime")["disabled_siblings"]


def test_enabling_a_retired_mode_is_a_user_error(manager, fake, monkeypatch):
    """A mode whose unit is retired is refused, not silently written."""
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (DEMO_UNIT,), raising=True)
    with pytest.raises(CliError) as excinfo:
        manager.enable("demo")
    assert excinfo.value.code == EXIT_USER_ERROR
    assert "retired" in str(excinfo.value.message).lower()


def test_install_never_rewrites_a_retired_unit(monkeypatch, tmp_path, capsys):
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (DEMO_UNIT,), raising=True)
    unit_dir = tmp_path / "config" / "systemd" / "user"
    unit_dir.mkdir(parents=True, exist_ok=True)
    _seed_deployed_unit(unit_dir, DEMO_UNIT)

    rc = main(["service", "install", "--json"])
    out, _ = capsys.readouterr()

    assert rc == 0
    payload = json.loads(out)
    assert DEMO_UNIT not in payload["unit_paths"]
    assert not (unit_dir / DEMO_UNIT).exists()
    assert not (unit_dir / f"{DEMO_UNIT}.d").exists()
    # The still-catalogued units are installed as usual.
    assert RUNTIME_UNIT in payload["unit_paths"]


# --------------------------------------------------------------------------- #
# Criterion 2 — status never lies with mode=None while something is enabled.
# --------------------------------------------------------------------------- #


def test_status_reports_a_retired_unit_that_is_still_enabled(manager, fake, monkeypatch):
    """The crash-loop case: a retired unit is enabled; status must NOT say None."""
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    fake.set_enabled(SYNTHETIC_RETIRED, "enabled")
    fake.set_active(SYNTHETIC_RETIRED, "activating")
    for unit in (DEMO_UNIT, RUNTIME_UNIT):
        fake.set_enabled(unit, "disabled")

    data = manager.status()

    assert data["mode"] is not None, "status must not report mode=None while a unit is enabled"
    assert data["presence_unit"] == SYNTHETIC_RETIRED
    assert data["retired_enabled"] == [SYNTHETIC_RETIRED]
    assert SYNTHETIC_RETIRED in data["units"]
    assert data["warning"]
    assert SYNTHETIC_RETIRED in str(data["warning"])


def test_status_is_clean_when_nothing_retired_is_enabled(manager, fake, monkeypatch):
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    fake.set_enabled(RUNTIME_UNIT, "enabled")
    for unit in (DEMO_UNIT, SYNTHETIC_RETIRED):
        fake.set_enabled(unit, "disabled")

    data = manager.status()

    assert data["mode"] == "runtime"
    assert data["presence_unit"] == RUNTIME_UNIT
    assert data["retired_enabled"] == []
    assert data["warning"] is None


def test_status_mode_none_only_when_truly_nothing_is_enabled(manager, fake, monkeypatch):
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)
    for unit in (DEMO_UNIT, SYNTHETIC_RETIRED, RUNTIME_UNIT):
        fake.set_enabled(unit, "disabled")

    data = manager.status()

    assert data["mode"] is None
    assert data["presence_unit"] is None
    assert data["retired_enabled"] == []


def test_status_honours_enabled_runtime_as_enabled(manager, fake):
    """``systemctl enable --runtime`` reports ``enabled-runtime`` — still enabled.

    A literal ``== "enabled"`` match reported ``mode=None`` for such a box, the
    same class of lie the retired-unit hole creates.
    """
    fake.query_results[("is-enabled", RUNTIME_UNIT)] = ("enabled-runtime", 0)
    fake.set_enabled(DEMO_UNIT, "disabled")

    data = manager.status()

    assert data["mode"] == "runtime"
    assert data["presence_unit"] == RUNTIME_UNIT


def test_cli_status_surfaces_the_retired_warning(monkeypatch, tmp_path, capsys):
    runner = FakeSystemctl()
    runner.set_enabled(SYNTHETIC_RETIRED, "enabled")
    for unit in (DEMO_UNIT, RUNTIME_UNIT):
        runner.set_enabled(unit, "disabled")
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    monkeypatch.setattr(units_mod, "RETIRED_UNITS", (SYNTHETIC_RETIRED,), raising=True)

    rc = main(["service", "status", "--json"])
    out, err = capsys.readouterr()

    assert rc == 0
    payload = json.loads(out)
    assert payload["mode"] is not None
    assert payload["retired_enabled"] == [SYNTHETIC_RETIRED]
    assert err == ""
