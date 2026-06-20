"""Tests for :mod:`reachy.service.manager` — the presence-service manager.

These tests drive the manager entirely through INJECTED seams: a fake
``systemctl`` runner callable (records the exact command sequence and returns
canned results), a temp ``unit_dir`` (so file writes never touch the real
``~/.config/systemd/user``), and a fake ``daemon_health`` callable. No real
``systemctl`` is ever invoked and no real systemd unit is ever enabled.
"""

from __future__ import annotations

import subprocess

import pytest

from reachy.cli._errors import CliError
from reachy.service.manager import ServiceManager
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
)

# --------------------------------------------------------------------------- #
# Fake systemctl runner — records the exact arg vectors, returns canned state.
# --------------------------------------------------------------------------- #


class FakeSystemctl:
    """Records every ``systemctl --user ...`` invocation; serves canned queries.

    A canned ``is-enabled`` / ``is-active`` answer is keyed by ``(verb, unit)``
    so a test can assert what ``status()`` reports without any real systemd.
    Mutating verbs (enable/disable/daemon-reload) are recorded and return rc 0
    unless the test seeds a failure.
    """

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        # (verb, unit) -> (stdout, returncode)
        self.query_results: dict[tuple[str, str], tuple[str, int]] = {}
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

    def set_enabled(self, unit: str, value: str) -> None:
        self.query_results[("is-enabled", unit)] = (value, 0 if value == "enabled" else 1)

    def set_active(self, unit: str, value: str) -> None:
        self.query_results[("is-active", unit)] = (value, 0 if value == "active" else 3)

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        # args always begin with "--user".
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

    # --- assertion helpers -------------------------------------------------

    def mutating_calls(self) -> list[list[str]]:
        verbs = {"daemon-reload", "enable", "disable", "restart", "start", "stop"}
        return [c for c in self.calls if len(c) > 1 and c[1] in verbs]

    def enabled_units(self) -> list[str]:
        """Units passed to an ``enable --now`` call, in order."""
        out = []
        for c in self.calls:
            if len(c) >= 2 and c[1] == "enable":
                out.append(c[-1])
        return out

    def disabled_units(self) -> list[str]:
        out = []
        for c in self.calls:
            if len(c) >= 2 and c[1] == "disable":
                out.append(c[-1])
        return out


@pytest.fixture
def unit_dir(tmp_path):
    d = tmp_path / "systemd" / "user"
    return d


@pytest.fixture
def make_manager(unit_dir):
    def _make(run=None, daemon_health=None):
        return ServiceManager(
            run=run if run is not None else FakeSystemctl(),
            unit_dir=unit_dir,
            daemon_health=daemon_health if daemon_health is not None else (lambda: True),
        )

    return _make


# --------------------------------------------------------------------------- #
# enable() — writes units, enables daemon + chosen presence, disables sibling.
# --------------------------------------------------------------------------- #


def test_enable_live_writes_daemon_and_live_units(make_manager, unit_dir):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("live")

    daemon_path = unit_dir / DAEMON_UNIT
    live_path = unit_dir / LIVE_UNIT
    assert daemon_path.is_file()
    assert live_path.is_file()
    # Text comes from t1's renderers verbatim — no re-rendering in the manager.
    assert daemon_path.read_text(encoding="utf-8") == daemon_unit_text()
    assert live_path.read_text(encoding="utf-8") == live_unit_text()


def test_enable_demo_writes_daemon_and_demo_units(make_manager, unit_dir):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("demo")

    assert (unit_dir / DAEMON_UNIT).is_file()
    demo_path = unit_dir / DEMO_UNIT
    assert demo_path.is_file()
    assert demo_path.read_text(encoding="utf-8") == demo_unit_text()


def test_enable_live_enables_daemon_plus_live_disables_demo(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("live")

    # daemon-reload happens before enabling.
    assert ["--user", "daemon-reload"] in fake.calls
    # daemon AND the chosen presence get enabled --now.
    assert DAEMON_UNIT in fake.enabled_units()
    assert LIVE_UNIT in fake.enabled_units()
    # the sibling presence is disabled --now.
    assert DEMO_UNIT in fake.disabled_units()
    # the sibling is NOT enabled.
    assert DEMO_UNIT not in fake.enabled_units()


def test_enable_demo_enables_daemon_plus_demo_disables_live(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("demo")

    assert DAEMON_UNIT in fake.enabled_units()
    assert DEMO_UNIT in fake.enabled_units()
    assert LIVE_UNIT in fake.disabled_units()
    assert LIVE_UNIT not in fake.enabled_units()


def test_enable_uses_now_flag_for_presence(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("live")
    # presence enable is enable --now (start it immediately).
    assert ["--user", "enable", "--now", LIVE_UNIT] in fake.calls
    # daemon is enabled --now too.
    assert ["--user", "enable", "--now", DAEMON_UNIT] in fake.calls
    # sibling disable is disable --now.
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls


def test_enable_rejects_unknown_mode(make_manager):
    mgr = make_manager()
    with pytest.raises(CliError) as ei:
        mgr.enable("sleep")
    assert ei.value.code != 0


def test_enable_result_reports_mode(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    result = mgr.enable("live")
    assert result["mode"] == "live"
    assert result["status"] == "enabled"


# --------------------------------------------------------------------------- #
# The single-presence-owner invariant — the load-bearing test.
# --------------------------------------------------------------------------- #


def test_invariant_at_most_one_presence_enabled_after_any_sequence(make_manager):
    """After ANY sequence of enables, at most one presence unit is enabled.

    The fake runner mirrors each enable/disable into its is-enabled answer so we
    can read back the post-sequence state exactly as ``status()`` would.
    """
    fake = FakeSystemctl()

    # A runner wrapper that tracks enable/disable -> is-enabled so we can read
    # the post-sequence state exactly as ``status()`` would. (We wrap a plain
    # callable rather than reassigning ``__call__`` on the instance, because
    # Python resolves dunders on the type, not the instance.)
    state: dict[str, bool] = {DEMO_UNIT: False, LIVE_UNIT: False, DAEMON_UNIT: False}

    def tracking_run(args):
        result = fake(args)
        rest = args[1:] if args and args[0] == "--user" else list(args)
        if rest and rest[0] == "enable":
            state[rest[-1]] = True
        elif rest and rest[0] == "disable":
            state[rest[-1]] = False
        return result

    mgr = make_manager(run=tracking_run)

    for mode in ("demo", "live", "live", "demo", "live"):
        mgr.enable(mode)
        enabled_presence = [u for u in (DEMO_UNIT, LIVE_UNIT) if state[u]]
        assert len(enabled_presence) <= 1, f"two presence units enabled: {enabled_presence}"

    # End state: only the last mode (live) presence is enabled.
    assert state[LIVE_UNIT] is True
    assert state[DEMO_UNIT] is False


# --------------------------------------------------------------------------- #
# status() — single enabled presence (or none) + daemon health.
# --------------------------------------------------------------------------- #


def test_status_reports_enabled_live_mode(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(LIVE_UNIT, "enabled")
    fake.set_active(LIVE_UNIT, "active")
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_active(DEMO_UNIT, "inactive")
    fake.set_enabled(DAEMON_UNIT, "enabled")
    fake.set_active(DAEMON_UNIT, "active")
    mgr = make_manager(run=fake, daemon_health=lambda: True)

    st = mgr.status()
    assert st["mode"] == "live"
    assert st["daemon_healthy"] is True


def test_status_reports_enabled_demo_mode(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(DEMO_UNIT, "enabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    mgr = make_manager(run=fake)
    st = mgr.status()
    assert st["mode"] == "demo"


def test_status_reports_none_when_no_presence_enabled(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    mgr = make_manager(run=fake)
    st = mgr.status()
    assert st["mode"] is None


def test_status_folds_daemon_health_false(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(LIVE_UNIT, "enabled")
    mgr = make_manager(run=fake, daemon_health=lambda: False)
    st = mgr.status()
    assert st["daemon_healthy"] is False


def test_status_reports_per_unit_enabled_active(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(LIVE_UNIT, "enabled")
    fake.set_active(LIVE_UNIT, "active")
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_active(DEMO_UNIT, "inactive")
    fake.set_enabled(DAEMON_UNIT, "enabled")
    fake.set_active(DAEMON_UNIT, "active")
    mgr = make_manager(run=fake)
    st = mgr.status()
    units = st["units"]
    assert units[LIVE_UNIT]["enabled"] == "enabled"
    assert units[LIVE_UNIT]["active"] == "active"
    assert units[DEMO_UNIT]["enabled"] == "disabled"
    assert units[DAEMON_UNIT]["enabled"] == "enabled"


def test_status_does_no_mutating_calls(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(LIVE_UNIT, "enabled")
    mgr = make_manager(run=fake)
    mgr.status()
    assert fake.mutating_calls() == []


# --------------------------------------------------------------------------- #
# disable() — stops/disables the enabled presence; daemon decision is explicit.
# --------------------------------------------------------------------------- #


def test_disable_stops_enabled_presence_unit(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(LIVE_UNIT, "enabled")
    fake.set_enabled(DEMO_UNIT, "disabled")
    mgr = make_manager(run=fake)
    result = mgr.disable()

    # the enabled presence is disabled --now (stop + disable).
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    assert result["disabled"] == LIVE_UNIT


def test_disable_leaves_daemon_enabled(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(DEMO_UNIT, "enabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    mgr = make_manager(run=fake)
    result = mgr.disable()

    # daemon is NOT disabled — the daemon decision is explicit/documented.
    assert DAEMON_UNIT not in fake.disabled_units()
    assert result["daemon"] == "left-enabled"


def test_disable_with_no_presence_enabled_is_noop(make_manager):
    fake = FakeSystemctl()
    fake.set_enabled(DEMO_UNIT, "disabled")
    fake.set_enabled(LIVE_UNIT, "disabled")
    mgr = make_manager(run=fake)
    result = mgr.disable()
    assert result["disabled"] is None
    # nothing was disabled.
    assert fake.disabled_units() == []


# --------------------------------------------------------------------------- #
# Failure surfacing — a non-zero systemctl rc becomes a clean CliError.
# --------------------------------------------------------------------------- #


def test_enable_surfaces_systemctl_failure_as_clierror(make_manager):
    fake = FakeSystemctl()
    fake.fail[("enable", DAEMON_UNIT)] = ("Failed to enable", 1)
    mgr = make_manager(run=fake)
    with pytest.raises(CliError) as ei:
        mgr.enable("live")
    assert ei.value.code != 0


# --------------------------------------------------------------------------- #
# Default daemon_health wiring — defaults to reachy.daemon, no real call here.
# --------------------------------------------------------------------------- #


def test_default_daemon_health_is_callable(unit_dir):
    """A manager built without an explicit daemon_health still has a callable one."""
    mgr = ServiceManager(run=FakeSystemctl(), unit_dir=unit_dir)
    assert callable(mgr.daemon_health)


# --------------------------------------------------------------------------- #
# Qodo PR #50 fixes: single-line errors + sibling-disable safety
# --------------------------------------------------------------------------- #


def test_systemctl_failure_message_is_single_line(make_manager):
    """A multi-line systemctl failure collapses to ONE line.

    Text CLI errors must stay exactly two lines (``error:`` then ``hint:``); a
    raw multi-line systemctl message embedded in ``CliError.message`` would break
    that contract (Qodo PR #50, comment 1).
    """
    fake = FakeSystemctl()
    fake.fail[("enable", DAEMON_UNIT)] = ("Failed to enable unit:\nUnit not found\nsee logs", 1)
    mgr = make_manager(run=fake)

    with pytest.raises(CliError) as ei:
        mgr.enable("live")

    assert "\n" not in ei.value.message
    assert "Unit not found" in ei.value.message  # detail preserved, just flattened


def test_enable_writes_both_presence_units(make_manager, unit_dir):
    """enable() writes BOTH presence unit files, not just the chosen one.

    The sibling-disable in step 4 (``disable --now <sibling>``) must target an
    installed unit; a first-time enable has no sibling on disk yet, and
    ``systemctl disable`` on a missing unit fails and would abort the enable
    (Qodo PR #50, comment 5).
    """
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)

    mgr.enable("live")  # fresh enable — nothing pre-installed

    assert (unit_dir / LIVE_UNIT).is_file()
    assert (unit_dir / DEMO_UNIT).is_file()  # sibling written too -> disable is safe
