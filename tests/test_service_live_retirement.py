"""Task t23 — ``reachy-live.service`` is RETIRED; ``service`` offers demo | runtime.

``t21`` deleted the ``--live`` flag and ``t22`` deleted the ``listen`` noun, but
:func:`reachy.service.units.live_exec_start` still rendered
``ExecStart=… listen run --live --transcribe --cognition agent --voice-engine
harmonic``. So ``service enable live`` wrote a unit whose command no longer
parses: argparse error, exit 1, ``Restart=on-failure`` + ``RestartSec=5`` — a
box crash-looping every five seconds. That is the hazard these tests close, and
it was inert only because nothing in the arc ran ``service enable``.

The closure reuses ``t4``'s :data:`reachy.service.units.RETIRED_UNITS` migration
verbatim rather than inventing a second mechanism: the name moves OUT of the
canonical-unit catalog and INTO ``RETIRED_UNITS``, which is authoritative — a
retired name is refused as a mode, never re-written by ``enable``/``install``,
purged on every ordinary ``service`` verb, and still probed by ``status()``.

**Everything here is driven through the injected ``run`` / ``unit_dir`` /
``daemon_health`` seams. No real ``systemctl`` is invoked and nothing under a
real ``~/.config/systemd`` is read or written** — the deployed robot's units are
not this suite's to touch.
"""

from __future__ import annotations

import itertools
import json
import subprocess

import pytest

from reachy.cli import main
from reachy.cli._commands import service as service_cmd
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.service import units as units_mod
from reachy.service.manager import RETIRED_MODES, ServiceManager
from reachy.service.units import DAEMON_UNIT, DEMO_UNIT, RETIRED_UNITS, RUNTIME_UNIT

#: The unit name this task retires. Deliberately spelled as a LITERAL here: the
#: whole point of the change is that ``reachy.service.units`` no longer exports a
#: ``LIVE_UNIT`` constant for anything to import.
LIVE_UNIT_NAME = "reachy-live.service"

#: The presence modes that survive. ``service enable`` offers exactly these.
LIVE_MODES = ("demo", "runtime")
MODE_UNIT = {"demo": DEMO_UNIT, "runtime": RUNTIME_UNIT}


# --------------------------------------------------------------------------- #
# Fake systemctl runner — records arg vectors, serves canned query state.
# --------------------------------------------------------------------------- #


class FakeSystemctl:
    """Recording fake for the ``systemctl --user ...`` seam.

    Mirrors the fakes in ``test_service_manager.py`` / ``test_service_retired_units.py``:
    read-only queries answer from canned state keyed by ``(verb, unit)``; mutating
    verbs record and succeed unless a failure is seeded. ``enable``/``disable``
    also update the canned ``is-enabled`` answer, so a test can read the
    post-sequence state back exactly as :meth:`ServiceManager.status` would.
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
        if verb == "enable":
            self.set_enabled(unit, "enabled")
            self.set_active(unit, "active")
        elif verb == "disable":
            self.set_enabled(unit, "disabled")
            self.set_active(unit, "inactive")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    # --- read-back helpers -------------------------------------------------

    def is_enabled(self, unit: str) -> bool:
        return self.query_results.get(("is-enabled", unit), ("", 1))[0].startswith("enabled")

    def enabled_units(self) -> list[str]:
        return [c[-1] for c in self.calls if len(c) >= 2 and c[1] == "enable"]

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


@pytest.fixture
def cli(monkeypatch, tmp_path):
    """Drive the real CLI with the production seams replaced by the fake."""
    runner = FakeSystemctl()
    monkeypatch.setenv("XDG_CONFIG_HOME", str(tmp_path / "config"))
    monkeypatch.setattr(service_cmd, "_systemctl_run", runner, raising=True)
    monkeypatch.setattr(service_cmd, "_daemon_health", lambda: True, raising=True)
    return runner


def _cli_unit_dir(tmp_path):
    return tmp_path / "config" / "systemd" / "user"


def _seed_deployed_live_unit(unit_dir):
    """Reproduce the deployed box: ``reachy-live.service`` + its drop-in dir.

    ``spark-f8a9`` carries the unit file plus six hand-authored drop-ins under
    ``reachy-live.service.d/`` (a hardcoded-IP ``panel.conf``, a ``tts.conf``
    routing around the EXPOSE-only chatterbox container, …). A migration that
    unlinks the unit but leaves ``<unit>.d/`` behind leaves systemd carrying
    orphaned overrides forward.
    """
    path = unit_dir / LIVE_UNIT_NAME
    path.write_text(
        "[Unit]\nDescription=Reachy Mini live presence\n\n"
        "[Service]\nExecStart=/x/python -m reachy listen run --live\n"
        "Restart=on-failure\nRestartSec=5\n\n"
        "[Install]\nWantedBy=default.target\n",
        encoding="utf-8",
    )
    dropin = unit_dir / f"{LIVE_UNIT_NAME}.d"
    dropin.mkdir(parents=True, exist_ok=True)
    (dropin / "panel.conf").write_text("[Service]\nExecStart=\n", encoding="utf-8")
    (dropin / "tts.conf").write_text("[Service]\nEnvironment=REACHY_TTS_ROUTE=openai\n", "utf-8")
    return path, dropin


# --------------------------------------------------------------------------- #
# The catalog: LIVE_UNIT is gone; the name is retired; demo|runtime remain.
# --------------------------------------------------------------------------- #


def test_units_module_no_longer_exports_a_live_unit_constant():
    """``LIVE_UNIT`` is removed from the canonical-name contract.

    The constants block is the cross-module contract for units this CLI still
    installs. A retired unit is named ONLY by ``RETIRED_UNITS``, so nothing can
    accidentally import it back into a catalog tuple.
    """
    import reachy.service as service_pkg

    assert not hasattr(units_mod, "LIVE_UNIT")
    # ...and the package re-export is gone too, so nothing can reach it either way.
    assert not hasattr(service_pkg, "LIVE_UNIT")
    assert "LIVE_UNIT" not in service_pkg.__all__


def test_units_module_no_longer_renders_a_live_unit():
    """The renderers that produced the crash-looping ExecStart are gone."""
    import reachy.service as service_pkg

    assert not hasattr(units_mod, "live_unit_text")
    assert not hasattr(units_mod, "live_exec_start")
    assert not hasattr(service_pkg, "live_unit_text")
    assert not hasattr(service_pkg, "live_exec_start")


def test_no_rendered_unit_text_names_the_removed_listen_command():
    """No unit this CLI writes may name a command the CLI no longer provides."""
    for render in (
        units_mod.daemon_unit_text,
        units_mod.demo_unit_text,
        units_mod.runtime_unit_text,
    ):
        text = render()
        assert "listen run" not in text
        assert "--live" not in text


def test_retired_units_carries_the_live_unit_name():
    assert LIVE_UNIT_NAME in RETIRED_UNITS


def test_retired_units_still_carries_the_t4_orphan():
    """``t4``'s negative control is untouched — the June orphan is still listed."""
    assert "reachy-listen.service" in RETIRED_UNITS


def test_retired_modes_maps_live_to_its_retired_unit():
    assert RETIRED_MODES["live"] == LIVE_UNIT_NAME


# --------------------------------------------------------------------------- #
# CENTREPIECE 1 — enable("live") is refused.
# --------------------------------------------------------------------------- #


def test_enable_live_is_refused_as_a_user_error(manager, fake):
    """The hazard, closed: the mode that wrote the crash-looping unit is gone."""
    with pytest.raises(CliError) as excinfo:
        manager.enable("live")

    assert excinfo.value.code == EXIT_USER_ERROR
    message = str(excinfo.value.message).lower()
    assert "retired" in message
    assert LIVE_UNIT_NAME in str(excinfo.value.message)
    # The remediation names the modes that DO exist.
    assert "demo" in str(excinfo.value.remediation)
    assert "runtime" in str(excinfo.value.remediation)


def test_enable_live_writes_nothing_and_runs_no_enable(manager, fake, unit_dir):
    """A refused mode must not touch disk and must not enable anything."""
    with pytest.raises(CliError):
        manager.enable("live")

    assert not (unit_dir / LIVE_UNIT_NAME).exists()
    assert fake.enabled_units() == []
    assert fake.disabled_units() == []


def test_cli_enable_live_is_rejected_without_writing_a_unit(cli, capsys, tmp_path):
    """``service enable live`` is refused at the CLI, structured, exit 1."""
    with pytest.raises(SystemExit) as exc:
        main(["service", "enable", "live"])

    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err
    assert not (_cli_unit_dir(tmp_path) / LIVE_UNIT_NAME).exists()
    assert cli.enabled_units() == []


def test_cli_enable_live_json_is_structured(cli, capsys):
    with pytest.raises(SystemExit) as exc:
        main(["service", "enable", "live", "--json"])

    assert exc.value.code == EXIT_USER_ERROR
    out, err = capsys.readouterr()
    assert out == ""
    payload = json.loads(err)
    assert payload["code"] == EXIT_USER_ERROR


def test_cli_enable_offers_exactly_demo_and_runtime(cli, capsys):
    """The advertised choice set is exactly ``demo`` | ``runtime``."""
    with pytest.raises(SystemExit):
        main(["service", "enable", "--help"])
    out, _ = capsys.readouterr()
    assert "demo" in out
    assert "runtime" in out
    assert "live" not in out


# --------------------------------------------------------------------------- #
# CENTREPIECE 2 — a previously-ENABLED reachy-live.service is stopped+disabled.
# --------------------------------------------------------------------------- #


def test_migration_stops_and_disables_a_previously_enabled_live_unit(manager, fake, unit_dir):
    """'Present and enabled' — the crash-looping box. It ends fully purged."""
    path, dropin = _seed_deployed_live_unit(unit_dir)
    fake.set_enabled(LIVE_UNIT_NAME, "enabled")
    fake.set_active(LIVE_UNIT_NAME, "activating")

    result = manager.enable("runtime")

    # Stopped AND disabled — ``disable --now`` is both halves in one call.
    assert ["--user", "disable", "--now", LIVE_UNIT_NAME] in fake.calls
    assert fake.is_enabled(LIVE_UNIT_NAME) is False
    # On-disk artifacts gone: the unit file AND its drop-in directory.
    assert not path.exists()
    assert not dropin.exists()
    assert result["retired_removed"] == [LIVE_UNIT_NAME]
    # ...and the chosen presence still comes up.
    assert result["mode"] == "runtime"
    assert (unit_dir / RUNTIME_UNIT).is_file()
    assert RUNTIME_UNIT in fake.enabled_units()


def test_migration_handles_present_but_disabled(manager, fake, unit_dir):
    """'Present but disabled' — the deployed box today (``spark-f8a9``).

    ``disable --now`` is UNCONDITIONAL, so the already-disabled case is a no-op
    success rather than a skipped branch, and the on-disk artifacts are still
    removed. Both starting states converge on the same clean end state.
    """
    path, dropin = _seed_deployed_live_unit(unit_dir)
    fake.set_enabled(LIVE_UNIT_NAME, "disabled")
    fake.set_active(LIVE_UNIT_NAME, "inactive")

    result = manager.enable("runtime")

    assert ["--user", "disable", "--now", LIVE_UNIT_NAME] in fake.calls
    assert not path.exists()
    assert not dropin.exists()
    assert result["retired_removed"] == [LIVE_UNIT_NAME]
    assert result["mode"] == "runtime"


@pytest.mark.parametrize("starting_state", ("enabled", "disabled"))
@pytest.mark.parametrize("mode", LIVE_MODES)
def test_both_starting_states_converge_for_both_modes(fake, unit_dir, starting_state, mode):
    """The migration is independent of which surviving mode the operator picks."""
    manager = ServiceManager(run=fake, unit_dir=unit_dir, daemon_health=lambda: True)
    path, dropin = _seed_deployed_live_unit(unit_dir)
    fake.set_enabled(LIVE_UNIT_NAME, starting_state)

    manager.enable(mode)

    assert fake.is_enabled(LIVE_UNIT_NAME) is False
    assert not path.exists()
    assert not dropin.exists()
    assert fake.is_enabled(MODE_UNIT[mode]) is True


def test_migration_is_idempotent(manager, fake, unit_dir):
    """A second ``enable`` on an already-migrated box reports nothing removed."""
    _seed_deployed_live_unit(unit_dir)

    assert manager.enable("runtime")["retired_removed"] == [LIVE_UNIT_NAME]
    assert manager.enable("runtime")["retired_removed"] == []


def test_migration_survives_a_systemctl_failure_on_the_missing_unit(manager, fake, unit_dir):
    """A box that never had the unit: ``disable`` genuinely exits non-zero.

    That must not abort the operator's real work — the migration is best-effort
    by construction (``_systemctl``, not ``_require``).
    """
    path, dropin = _seed_deployed_live_unit(unit_dir)
    fake.fail_verb("disable", LIVE_UNIT_NAME, "Failed to disable: Unit file does not exist.")

    result = manager.enable("runtime")

    assert result["mode"] == "runtime"
    assert not path.exists()
    assert not dropin.exists()


def test_enable_never_rewrites_the_live_unit(manager, fake, unit_dir):
    """``enable`` writes every SURVIVING presence unit — never the retired one.

    ``enable`` writes each sibling so step 4's ``disable --now <sibling>`` always
    targets an installed unit. If the retired name were still in that write set,
    step 1 would resurrect the file step 0 just deleted.
    """
    _seed_deployed_live_unit(unit_dir)

    result = manager.enable("runtime")

    assert not (unit_dir / LIVE_UNIT_NAME).exists()
    assert not (unit_dir / f"{LIVE_UNIT_NAME}.d").exists()
    assert LIVE_UNIT_NAME not in result["disabled_siblings"]
    assert LIVE_UNIT_NAME not in result["unit_paths"]


def test_cli_install_purges_and_never_writes_the_live_unit(cli, capsys, tmp_path):
    unit_dir = _cli_unit_dir(tmp_path)
    unit_dir.mkdir(parents=True, exist_ok=True)
    path, dropin = _seed_deployed_live_unit(unit_dir)

    rc = main(["service", "install", "--json"])
    out, err = capsys.readouterr()

    assert rc == 0
    payload = json.loads(out)
    assert payload["retired_removed"] == [LIVE_UNIT_NAME]
    assert LIVE_UNIT_NAME not in payload["unit_paths"]
    assert set(payload["unit_paths"]) == {DAEMON_UNIT, DEMO_UNIT, RUNTIME_UNIT}
    assert not path.exists()
    assert not dropin.exists()
    assert err == ""


def test_cli_uninstall_purges_the_live_unit(cli, capsys, tmp_path):
    unit_dir = _cli_unit_dir(tmp_path)
    unit_dir.mkdir(parents=True, exist_ok=True)
    path, dropin = _seed_deployed_live_unit(unit_dir)

    rc = main(["service", "uninstall", "--json"])
    out, _ = capsys.readouterr()

    assert rc == 0
    assert json.loads(out)["retired_removed"] == [LIVE_UNIT_NAME]
    assert not path.exists()
    assert not dropin.exists()


# --------------------------------------------------------------------------- #
# status() still tells the truth about a live unit left enabled.
# --------------------------------------------------------------------------- #


def test_status_reports_an_enabled_live_unit_rather_than_none(manager, fake):
    """A box that upgraded but never ran ``enable`` still gets a true answer.

    Reporting ``mode=None`` while ``reachy-live.service`` crash-loops is the
    diagnostic lie ``t4``'s ``status()`` change exists to close; retiring the
    name must keep that closed, not reopen it.
    """
    fake.set_enabled(LIVE_UNIT_NAME, "enabled")
    fake.set_active(LIVE_UNIT_NAME, "activating")
    for unit in (DEMO_UNIT, RUNTIME_UNIT):
        fake.set_enabled(unit, "disabled")

    data = manager.status()

    assert data["mode"] is not None
    assert data["presence_unit"] == LIVE_UNIT_NAME
    assert data["retired_enabled"] == [LIVE_UNIT_NAME]
    assert LIVE_UNIT_NAME in data["units"]
    assert LIVE_UNIT_NAME in str(data["warning"])


def test_status_probes_the_live_unit_without_mutating_anything(manager, fake):
    manager.status()

    assert LIVE_UNIT_NAME in [c[-1] for c in fake.calls if len(c) > 1 and c[1] == "is-enabled"]
    mutating = {"enable", "disable", "daemon-reload", "start", "stop", "restart"}
    assert [c for c in fake.calls if len(c) > 1 and c[1] in mutating] == []


def test_status_is_clean_once_the_live_unit_is_gone(manager, fake):
    fake.set_enabled(RUNTIME_UNIT, "enabled")
    for unit in (DEMO_UNIT, LIVE_UNIT_NAME):
        fake.set_enabled(unit, "disabled")

    data = manager.status()

    assert data["mode"] == "runtime"
    assert data["retired_enabled"] == []
    assert data["warning"] is None


# --------------------------------------------------------------------------- #
# The single-presence invariant survives, across ARBITRARY enable sequences.
# --------------------------------------------------------------------------- #


def test_invariant_holds_for_every_ordered_pair_of_surviving_modes(fake, unit_dir):
    """For every (X, Y) in {demo, runtime}², enable(X) then enable(Y) leaves Y alone."""
    for mode_x, mode_y in itertools.product(LIVE_MODES, repeat=2):
        runner = FakeSystemctl()
        manager = ServiceManager(run=runner, unit_dir=unit_dir, daemon_health=lambda: True)

        manager.enable(mode_x)
        manager.enable(mode_y)

        enabled = [u for u in MODE_UNIT.values() if runner.is_enabled(u)]
        assert enabled == [MODE_UNIT[mode_y]], f"enable({mode_x!r}) then enable({mode_y!r})"
        assert runner.is_enabled(DAEMON_UNIT) is True


@pytest.mark.parametrize(
    "sequence",
    [list(seq) for length in (1, 2, 3, 4) for seq in itertools.product(LIVE_MODES, repeat=length)],
)
def test_invariant_at_most_one_presence_after_any_sequence(fake, unit_dir, sequence):
    """EXHAUSTIVE over every enable sequence up to length 4.

    ``t23`` changes the presence catalog, so the invariant is re-proven on the
    new catalog rather than assumed to survive: after EVERY step of EVERY
    sequence, at most one presence unit is enabled — and at the end, exactly the
    last-named one.
    """
    manager = ServiceManager(run=fake, unit_dir=unit_dir, daemon_health=lambda: True)

    for mode in sequence:
        manager.enable(mode)
        enabled = [u for u in MODE_UNIT.values() if fake.is_enabled(u)]
        assert len(enabled) <= 1, f"two presence units enabled after enable({mode!r}): {enabled}"

    assert [u for u in MODE_UNIT.values() if fake.is_enabled(u)] == [MODE_UNIT[sequence[-1]]]
    assert fake.is_enabled(DAEMON_UNIT) is True


def test_a_refused_live_enable_never_perturbs_the_invariant(fake, unit_dir):
    """An operator following a stale runbook does not break the running box."""
    manager = ServiceManager(run=fake, unit_dir=unit_dir, daemon_health=lambda: True)
    manager.enable("runtime")

    with pytest.raises(CliError):
        manager.enable("live")

    assert fake.is_enabled(RUNTIME_UNIT) is True
    assert fake.is_enabled(DEMO_UNIT) is False
    assert fake.is_enabled(LIVE_UNIT_NAME) is False


def test_disable_still_leaves_the_daemon_enabled(fake, unit_dir):
    """The explicit daemon decision is unchanged by the retirement."""
    manager = ServiceManager(run=fake, unit_dir=unit_dir, daemon_health=lambda: True)
    manager.enable("runtime")

    result = manager.disable()

    assert result["disabled"] == RUNTIME_UNIT
    assert result["daemon"] == "left-enabled"
    assert DAEMON_UNIT not in fake.disabled_units()
    assert fake.is_enabled(DAEMON_UNIT) is True
