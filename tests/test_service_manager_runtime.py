"""Tests for the three-way single-presence-owner invariant (t10, decisions c19/c20).

Extends the ``ServiceManager`` coverage in ``tests/test_service_manager.py`` (not
edited — this file mirrors its ``FakeSystemctl`` recorder style) to the new
``runtime`` mode: after ANY sequence of ``enable(mode)`` calls across
``{demo, live, runtime}``, exactly the chosen unit is enabled and BOTH siblings
are disabled --now'd. Every side effect goes through the injected ``run`` /
``unit_dir`` / ``daemon_health`` seams — no real systemctl, no real unit dir.
"""

from __future__ import annotations

import itertools
import subprocess

import pytest

from reachy.cli._errors import CliError
from reachy.service.manager import ServiceManager
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    RUNTIME_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
    runtime_unit_text,
)

PRESENCE_UNITS = (DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT)
MODE_UNIT = {"demo": DEMO_UNIT, "live": LIVE_UNIT, "runtime": RUNTIME_UNIT}


# --------------------------------------------------------------------------- #
# Fake systemctl runner — records call vectors, serves canned query state.
# --------------------------------------------------------------------------- #


class FakeSystemctl:
    """Records every ``systemctl --user ...`` invocation; serves canned queries."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.query_results: dict[tuple[str, str], tuple[str, int]] = {}
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

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

    def enabled_units(self) -> list[str]:
        return [c[-1] for c in self.calls if len(c) >= 2 and c[1] == "enable"]

    def disabled_units(self) -> list[str]:
        return [c[-1] for c in self.calls if len(c) >= 2 and c[1] == "disable"]


@pytest.fixture
def unit_dir(tmp_path):
    return tmp_path / "systemd" / "user"


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
# RUNTIME_UNIT joins the mode table / renderer set.
# --------------------------------------------------------------------------- #


def test_enable_runtime_writes_daemon_and_runtime_units(make_manager, unit_dir):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("runtime")

    daemon_path = unit_dir / DAEMON_UNIT
    runtime_path = unit_dir / RUNTIME_UNIT
    assert daemon_path.is_file()
    assert runtime_path.is_file()
    assert daemon_path.read_text(encoding="utf-8") == daemon_unit_text()
    assert runtime_path.read_text(encoding="utf-8") == runtime_unit_text()


def test_enable_runtime_writes_all_four_unit_files(make_manager, unit_dir):
    """A fresh enable("runtime") writes every sibling too (safe disable target)."""
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("runtime")

    for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT):
        assert (unit_dir / unit).is_file(), f"{unit} not written"
    assert (unit_dir / DEMO_UNIT).read_text(encoding="utf-8") == demo_unit_text()
    assert (unit_dir / LIVE_UNIT).read_text(encoding="utf-8") == live_unit_text()


def test_enable_runtime_enables_daemon_plus_runtime_disables_both_siblings(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("runtime")

    assert ["--user", "daemon-reload"] in fake.calls
    assert DAEMON_UNIT in fake.enabled_units()
    assert RUNTIME_UNIT in fake.enabled_units()
    assert DEMO_UNIT in fake.disabled_units()
    assert LIVE_UNIT in fake.disabled_units()
    assert DEMO_UNIT not in fake.enabled_units()
    assert LIVE_UNIT not in fake.enabled_units()
    assert ["--user", "enable", "--now", RUNTIME_UNIT] in fake.calls
    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls


def test_enable_demo_now_disables_both_live_and_runtime(make_manager):
    """enable("demo") must disable BOTH other presences, not just live."""
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("demo")

    assert ["--user", "disable", "--now", LIVE_UNIT] in fake.calls
    assert ["--user", "disable", "--now", RUNTIME_UNIT] in fake.calls


def test_enable_live_now_disables_both_demo_and_runtime(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    mgr.enable("live")

    assert ["--user", "disable", "--now", DEMO_UNIT] in fake.calls
    assert ["--user", "disable", "--now", RUNTIME_UNIT] in fake.calls


def test_enable_result_reports_runtime_mode(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    result = mgr.enable("runtime")
    assert result["mode"] == "runtime"
    assert result["status"] == "enabled"
    assert result["presence_unit"] == RUNTIME_UNIT
    # Both siblings are reported, not just one.
    assert set(result["disabled_siblings"]) == {DEMO_UNIT, LIVE_UNIT}


# --------------------------------------------------------------------------- #
# The three-way single-presence-owner invariant — the load-bearing tests.
# --------------------------------------------------------------------------- #


def test_invariant_holds_for_every_pairwise_transition(make_manager):
    """For every (X, Y) in {demo, live, runtime}^2, enable(X) then enable(Y)
    leaves exactly Y's unit enabled and BOTH siblings disable --now'd.

    This is the exclusion-matrix acceptance criterion: not just adjacent
    demo<->live switches, but every ordered pair including runtime and the
    X == Y (re-enable the same mode) case.
    """
    for mode_x, mode_y in itertools.product(MODE_UNIT, repeat=2):
        fake = FakeSystemctl()
        mgr = make_manager(run=fake)

        mgr.enable(mode_x)
        fake.calls.clear()  # only inspect the second enable's effects
        mgr.enable(mode_y)

        enabled_unit = MODE_UNIT[mode_y]
        sibling_units = [u for m, u in MODE_UNIT.items() if m != mode_y]

        assert (
            enabled_unit in fake.enabled_units()
        ), f"enable({mode_x!r}) then enable({mode_y!r}): {enabled_unit} was not enabled"
        for sibling in sibling_units:
            assert ["--user", "disable", "--now", sibling] in fake.calls, (
                f"enable({mode_x!r}) then enable({mode_y!r}): "
                f"{sibling} was not disabled --now'd"
            )
            assert (
                sibling not in fake.enabled_units()
            ), f"enable({mode_x!r}) then enable({mode_y!r}): {sibling} was (re-)enabled"


def test_invariant_at_most_one_presence_enabled_after_any_sequence_of_three_modes(make_manager):
    """After ANY sequence of enables across all three modes, at most one is enabled.

    Mirrors test_service_manager.py's two-way version, extended with runtime and
    a tracking wrapper so we can read back the post-sequence enabled/disabled
    state exactly as status() would.
    """
    fake = FakeSystemctl()
    state: dict[str, bool] = {u: False for u in (DAEMON_UNIT, *PRESENCE_UNITS)}

    def tracking_run(args):
        result = fake(args)
        rest = args[1:] if args and args[0] == "--user" else list(args)
        if rest and rest[0] == "enable":
            state[rest[-1]] = True
        elif rest and rest[0] == "disable":
            state[rest[-1]] = False
        return result

    mgr = make_manager(run=tracking_run)

    sequence = ["demo", "live", "runtime", "runtime", "demo", "runtime", "live", "demo"]
    for mode in sequence:
        mgr.enable(mode)
        enabled_presence = [u for u in PRESENCE_UNITS if state[u]]
        assert (
            len(enabled_presence) <= 1
        ), f"after enable({mode!r}): two+ presence units enabled: {enabled_presence}"

    last = sequence[-1]
    for mode, unit in MODE_UNIT.items():
        assert state[unit] is (mode == last)


def test_enable_rejects_unknown_mode_still_rejects_with_runtime_present(make_manager):
    mgr = make_manager()
    with pytest.raises(CliError) as ei:
        mgr.enable("sleep")
    assert ei.value.code != 0


# --------------------------------------------------------------------------- #
# status() covers all four units, including runtime.
# --------------------------------------------------------------------------- #


def test_status_reports_enabled_runtime_mode(make_manager):
    fake = FakeSystemctl()
    fake.query_results[("is-enabled", RUNTIME_UNIT)] = ("enabled", 0)
    fake.query_results[("is-active", RUNTIME_UNIT)] = ("active", 0)
    fake.query_results[("is-enabled", DEMO_UNIT)] = ("disabled", 1)
    fake.query_results[("is-enabled", LIVE_UNIT)] = ("disabled", 1)
    fake.query_results[("is-enabled", DAEMON_UNIT)] = ("enabled", 0)
    mgr = make_manager(run=fake, daemon_health=lambda: True)

    st = mgr.status()
    assert st["mode"] == "runtime"
    assert st["presence_unit"] == RUNTIME_UNIT
    assert st["units"][RUNTIME_UNIT]["enabled"] == "enabled"
    assert st["units"][RUNTIME_UNIT]["active"] == "active"


def test_status_units_dict_has_all_four_keys(make_manager):
    fake = FakeSystemctl()
    mgr = make_manager(run=fake)
    st = mgr.status()
    assert set(st["units"]) == {DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT}


def test_status_reports_none_when_no_presence_enabled_among_three(make_manager):
    fake = FakeSystemctl()
    for unit in PRESENCE_UNITS:
        fake.query_results[("is-enabled", unit)] = ("disabled", 1)
    mgr = make_manager(run=fake)
    st = mgr.status()
    assert st["mode"] is None
    assert st["presence_unit"] is None


# --------------------------------------------------------------------------- #
# disable() works the same regardless of which of the three is enabled.
# --------------------------------------------------------------------------- #


def test_disable_stops_enabled_runtime_unit(make_manager):
    fake = FakeSystemctl()
    fake.query_results[("is-enabled", RUNTIME_UNIT)] = ("enabled", 0)
    fake.query_results[("is-enabled", DEMO_UNIT)] = ("disabled", 1)
    fake.query_results[("is-enabled", LIVE_UNIT)] = ("disabled", 1)
    mgr = make_manager(run=fake)
    result = mgr.disable()

    assert ["--user", "disable", "--now", RUNTIME_UNIT] in fake.calls
    assert result["disabled"] == RUNTIME_UNIT
    assert result["daemon"] == "left-enabled"
