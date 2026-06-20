"""End-to-end integration test for the boot-survival presence stack.

This exercises the spec's *success signal* against the **real**
:class:`reachy.service.manager.ServiceManager` and the **real**
:mod:`reachy.service.units` renderers — no monkeypatching of the production
code — through a **stateful** fake ``systemctl --user`` runner. The fake never
touches real systemd: it interprets the exact command vocabulary the manager
emits and maintains an in-memory model of each unit's *enabled* (persists across
a simulated reboot) and *active* (running now) state.

The three things this test proves end to end:

1. **Single-owner switching.** Driving ``enable("live") -> enable("demo") ->
   enable("live")`` leaves exactly ONE presence unit enabled after EVERY enable
   (``reachy-demo-mode.service`` XOR ``reachy-live.service``) plus the daemon.
   The single-SDK-owner model in ``CLAUDE.md`` is the *why*: only one presence
   loop may own the head, never both. A test fails if the two presence units are
   ever both enabled at once — the load-bearing invariant.
2. **Daemon-first ordering.** The rendered presence units declare ``Requires=``
   AND ``After=`` the daemon unit, so on boot the daemon is ordered before the
   presence loop (parsed straight out of the unit text).
3. **Reboot / re-login survival.** A simulated reboot re-evaluates which
   ``WantedBy=default.target`` units the fake has ENABLED (enable state is what
   survives a reboot; active state does not) and "starts" them in dependency
   order (daemon before presence per ``After=``/``Requires=``). After the reboot
   exactly one presence loop is running — the last-enabled mode — the daemon is
   up first, and the other mode is not running.

NOTE: a TRUE machine reboot is a manual on-box check (power-cycle the robot, log
in, confirm `systemctl --user` brings up daemon + one presence). This test
simulates that survival *at the systemctl level* — it models what systemd would
do from the persisted enable state — so the wiring contract is verified in CI
without hardware.
"""

from __future__ import annotations

import subprocess

import pytest

from reachy.service.manager import ServiceManager
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
)

PRESENCE_UNITS = (DEMO_UNIT, LIVE_UNIT)
ALL_UNITS = (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT)


# --------------------------------------------------------------------------- #
# Stateful fake ``systemctl --user`` runner.
#
# Models the SAME command vocabulary the real manager emits (verified by reading
# manager.py):
#   --user daemon-reload
#   --user enable  --now <unit>   -> enabled[unit]=True,  active[unit]=True
#   --user disable --now <unit>   -> enabled[unit]=False, active[unit]=False
#   --user is-enabled <unit>      -> "enabled"/"disabled" (rc 0 / 1) from state
#   --user is-active  <unit>      -> "active"/"inactive"  (rc 0 / 3) from state
#
# Two independent dimensions, exactly like real systemd:
#   * ``enabled`` — persisted "wired to boot" state; SURVIVES a reboot.
#   * ``active``  — "running right now"; does NOT survive a reboot (cleared, then
#     re-established by ``simulate_reboot`` starting the enabled WantedBy units).
# --------------------------------------------------------------------------- #


class StatefulSystemctl:
    """A stateful in-memory ``systemctl --user`` that records and answers."""

    def __init__(self) -> None:
        self.calls: list[list[str]] = []
        self.enabled: dict[str, bool] = {u: False for u in ALL_UNITS}
        self.active: dict[str, bool] = {u: False for u in ALL_UNITS}
        # Seeded by a failing-verb test if needed; (verb, unit) -> (stderr, rc).
        self.fail: dict[tuple[str, str], tuple[str, int]] = {}

    # --- the callable seam the manager drives ------------------------------ #

    def __call__(self, args: list[str]) -> subprocess.CompletedProcess:
        self.calls.append(list(args))
        # The manager always prepends "--user".
        rest = args[1:] if args and args[0] == "--user" else list(args)
        verb = rest[0] if rest else ""
        unit = rest[-1] if len(rest) > 1 else ""

        if (verb, unit) in self.fail:
            out, rc = self.fail[(verb, unit)]
            return subprocess.CompletedProcess(args, rc, stdout="", stderr=out)

        if verb == "daemon-reload":
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        if verb == "enable":
            # "enable --now" => persist enabled AND start it now.
            self.enabled[unit] = True
            if "--now" in rest:
                self.active[unit] = True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        if verb == "disable":
            # "disable --now" => clear enabled AND stop it now (idempotent).
            self.enabled[unit] = False
            if "--now" in rest:
                self.active[unit] = False
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        if verb == "is-enabled":
            on = self.enabled.get(unit, False)
            return subprocess.CompletedProcess(
                args, 0 if on else 1, stdout=("enabled" if on else "disabled") + "\n"
            )

        if verb == "is-active":
            on = self.active.get(unit, False)
            return subprocess.CompletedProcess(
                args, 0 if on else 3, stdout=("active" if on else "inactive") + "\n"
            )

        # start/stop/restart (not used by the manager today) — still modeled.
        if verb in ("start", "restart"):
            self.active[unit] = True
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")
        if verb == "stop":
            self.active[unit] = False
            return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    # --- read-back helpers (the post-state, as systemd would report it) ----- #

    def enabled_presence_units(self) -> list[str]:
        return [u for u in PRESENCE_UNITS if self.enabled[u]]

    def active_presence_units(self) -> list[str]:
        return [u for u in PRESENCE_UNITS if self.active[u]]

    def simulate_reboot(self, unit_dir) -> list[str]:
        """Model a power-cycle + re-login at the ``systemctl --user`` level.

        Real systemd on boot: (1) the persisted ``enabled`` state survives, the
        ``active`` state does not; (2) the user manager starts every enabled
        unit whose ``[Install] WantedBy=`` names the reached target
        (``default.target``); (3) it honours ``After=`` / ``Requires=`` ordering
        so the daemon comes up before any presence loop that requires it.

        We reproduce exactly that: clear ``active`` (nothing is running right
        after power-on), read the *written unit files* to learn each enabled
        unit's ``WantedBy`` + ``After`` (the real files the manager wrote — not a
        re-derivation), then start the enabled+wanted units in topological order.

        Returns the unit start order, so a test can assert daemon-before-presence.
        """
        # 1. A reboot stops everything that was running.
        for unit in ALL_UNITS:
            self.active[unit] = False

        # 2. Discover the boot set from the real on-disk unit files (the ones
        #    the manager actually wrote) — only units that are still enabled and
        #    whose [Install] section wants default.target are started at boot.
        boot_set = []
        for unit in ALL_UNITS:
            if not self.enabled[unit]:
                continue
            path = unit_dir / unit
            if not path.is_file():
                # Enabled but no unit file written -> systemd would not start it.
                continue
            text = path.read_text(encoding="utf-8")
            if "WantedBy=default.target" in text:
                boot_set.append(unit)

        # 3. Order by dependency: a unit that names another in After=/Requires=
        #    must start after it. The daemon names nothing, presence units name
        #    the daemon, so this puts the daemon first.
        def depends_on(unit: str, other: str) -> bool:
            text = (unit_dir / unit).read_text(encoding="utf-8")
            after_line = next((ln for ln in text.splitlines() if ln.startswith("After=")), "")
            requires_line = next((ln for ln in text.splitlines() if ln.startswith("Requires=")), "")
            return other in after_line or other in requires_line

        ordered: list[str] = []
        remaining = list(boot_set)
        # Stable topological pass: repeatedly take units whose deps (within the
        # boot set) are already started.
        while remaining:
            progressed = False
            for unit in list(remaining):
                if all(not depends_on(unit, other) for other in remaining if other != unit):
                    ordered.append(unit)
                    remaining.remove(unit)
                    progressed = True
            if not progressed:  # pragma: no cover - cycle guard, not expected
                ordered.extend(remaining)
                break

        # 4. "Start" them in order — this re-establishes ``active`` post-boot.
        for unit in ordered:
            self(["--user", "start", unit])
        return ordered


@pytest.fixture
def unit_dir(tmp_path):
    return tmp_path / "systemd" / "user"


def _make_manager(run, unit_dir):
    return ServiceManager(run=run, unit_dir=unit_dir, daemon_health=lambda: True)


# --------------------------------------------------------------------------- #
# 1. Single-owner switching: exactly one presence enabled after every enable.
# --------------------------------------------------------------------------- #


def test_single_owner_switching_live_demo_live(unit_dir):
    """live -> demo -> live: one presence + daemon enabled after EVERY enable."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    for mode, expected_unit in (
        ("live", LIVE_UNIT),
        ("demo", DEMO_UNIT),
        ("live", LIVE_UNIT),
    ):
        mgr.enable(mode)

        enabled_presence = sysd.enabled_presence_units()
        # Exactly ONE presence unit enabled (XOR) — the load-bearing invariant.
        assert enabled_presence == [expected_unit], (
            f"after enable({mode!r}) expected only {expected_unit} enabled, "
            f"got {enabled_presence}"
        )
        # ...and it is never BOTH presence units.
        assert len(enabled_presence) == 1
        # The daemon is enabled for both modes.
        assert sysd.enabled[DAEMON_UNIT] is True


def test_both_presence_units_enabled_is_a_failure(unit_dir):
    """A corrupt 'both enabled' state must be detectable as a FAILURE.

    This guards the guard: it proves the invariant assertion above would catch a
    double-enable, by forcing the fake into that corrupt state directly and
    confirming the same check fails. (We never reach this through the manager —
    the manager always disables the sibling — but if a future change let both
    units be enabled, the single-owner assertion must turn red.)
    """
    sysd = StatefulSystemctl()
    sysd.enabled[DEMO_UNIT] = True
    sysd.enabled[LIVE_UNIT] = True

    enabled_presence = sysd.enabled_presence_units()
    # The exact predicate the single-owner test relies on — it MUST fail here.
    assert len(enabled_presence) == 2  # corrupt state is observable
    with pytest.raises(AssertionError):
        assert len(enabled_presence) <= 1, "two presence units enabled simultaneously"


def test_single_owner_holds_over_a_long_random_looking_sequence(unit_dir):
    """A longer enable sequence never leaves two presence units enabled."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    sequence = ["demo", "live", "live", "demo", "demo", "live", "demo"]
    for mode in sequence:
        mgr.enable(mode)
        assert len(sysd.enabled_presence_units()) <= 1
        assert sysd.enabled[DAEMON_UNIT] is True

    # End state matches the last mode.
    last = sequence[-1]
    expected = DEMO_UNIT if last == "demo" else LIVE_UNIT
    assert sysd.enabled_presence_units() == [expected]


# --------------------------------------------------------------------------- #
# 2. Daemon-first ordering: presence units Require + order After the daemon.
# --------------------------------------------------------------------------- #


def _unit_field_lines(text: str, prefix: str) -> list[str]:
    return [ln for ln in text.splitlines() if ln.startswith(prefix)]


@pytest.mark.parametrize("render", (demo_unit_text, live_unit_text))
def test_presence_unit_requires_and_orders_after_daemon(render):
    """Both presence units declare Requires= AND After= the daemon unit."""
    text = render()

    requires = _unit_field_lines(text, "Requires=")
    after = _unit_field_lines(text, "After=")

    assert len(requires) == 1, f"expected exactly one Requires= line, got {requires}"
    assert len(after) == 1, f"expected exactly one After= line, got {after}"

    # Requires= names the daemon unit (hard dependency).
    assert DAEMON_UNIT in requires[0]
    # After= orders the daemon BEFORE this presence unit (boot ordering).
    assert DAEMON_UNIT in after[0]


def test_daemon_unit_does_not_depend_on_presence():
    """The daemon must NOT require/After a presence unit (no dependency cycle)."""
    text = daemon_unit_text()
    assert DEMO_UNIT not in text
    assert LIVE_UNIT not in text
    # Daemon has no Requires= line at all.
    assert _unit_field_lines(text, "Requires=") == []


# --------------------------------------------------------------------------- #
# 3. Reboot / re-login simulation: the last-enabled mode comes back, alone.
#
#    A TRUE machine reboot is a manual on-box check. This simulates it at the
#    systemctl level: persisted enable state -> what systemd starts at boot.
# --------------------------------------------------------------------------- #


def test_reboot_brings_back_last_enabled_mode_only_live(unit_dir):
    """After enabling live then rebooting, only the live presence runs."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    mgr.enable("demo")  # earlier mode
    mgr.enable("live")  # the last-enabled mode wins

    order = sysd.simulate_reboot(unit_dir)

    # Daemon comes up first, before any presence loop.
    assert order[0] == DAEMON_UNIT
    assert order.index(DAEMON_UNIT) < order.index(LIVE_UNIT)

    # Exactly one presence loop is running, and it is the last-enabled one.
    assert sysd.active_presence_units() == [LIVE_UNIT]
    # The other mode is NOT running.
    assert sysd.active[DEMO_UNIT] is False
    # The daemon is up.
    assert sysd.active[DAEMON_UNIT] is True


def test_reboot_brings_back_last_enabled_mode_only_demo(unit_dir):
    """After enabling demo last and rebooting, only the demo presence runs."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    mgr.enable("live")
    mgr.enable("demo")  # last-enabled wins

    order = sysd.simulate_reboot(unit_dir)

    assert order[0] == DAEMON_UNIT
    assert order.index(DAEMON_UNIT) < order.index(DEMO_UNIT)
    assert sysd.active_presence_units() == [DEMO_UNIT]
    assert sysd.active[LIVE_UNIT] is False
    assert sysd.active[DAEMON_UNIT] is True


def test_reboot_after_disable_runs_no_presence_but_keeps_daemon(unit_dir):
    """disable() leaves the daemon enabled; after a reboot only the daemon runs."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    mgr.enable("live")
    mgr.disable()  # stops/disables the presence; daemon left enabled

    # Disable cleared the presence enable but kept the daemon enabled.
    assert sysd.enabled_presence_units() == []
    assert sysd.enabled[DAEMON_UNIT] is True

    order = sysd.simulate_reboot(unit_dir)

    # Daemon boots; no presence loop comes back.
    assert order == [DAEMON_UNIT]
    assert sysd.active_presence_units() == []
    assert sysd.active[DAEMON_UNIT] is True


def test_reboot_is_idempotent_repeated_boots_keep_single_owner(unit_dir):
    """Rebooting twice keeps exactly one presence running (enable state stable)."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    mgr.enable("live")

    sysd.simulate_reboot(unit_dir)
    assert sysd.active_presence_units() == [LIVE_UNIT]

    # A second boot from the same persisted enable state is identical.
    sysd.simulate_reboot(unit_dir)
    assert sysd.active_presence_units() == [LIVE_UNIT]
    assert sysd.active[DEMO_UNIT] is False


def test_status_after_reboot_reports_the_surviving_mode(unit_dir):
    """The real manager.status() reads the post-reboot state as the live mode."""
    sysd = StatefulSystemctl()
    mgr = _make_manager(sysd, unit_dir)

    mgr.enable("demo")
    mgr.enable("live")
    sysd.simulate_reboot(unit_dir)

    # status() queries is-enabled/is-active through the same stateful fake.
    st = mgr.status()
    assert st["mode"] == "live"
    assert st["presence_unit"] == LIVE_UNIT
    assert st["units"][LIVE_UNIT]["active"] == "active"
    assert st["units"][DEMO_UNIT]["enabled"] == "disabled"
    assert st["units"][DAEMON_UNIT]["active"] == "active"
    assert st["daemon_healthy"] is True
