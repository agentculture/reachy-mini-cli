"""``reachy-mini-cli service`` — boot-persistent presence in exactly one mode.

This is the single CLI surface that makes the robot survive a reboot in *one*
presence mode — either the idle ``demo-mode`` loop or the folded live sense loop
(``listen run --live``), never both. It is the operator-facing front for the
already-built :class:`reachy.service.manager.ServiceManager`, which enforces the
single-presence-owner invariant (enable one mode → the sibling is disabled).

Like ``daemon``, ``service`` does **not** talk to the robot through a transport —
it talks to **systemd** (``systemctl --user``). So it never calls
``_robot.get_transport`` / ``noun_overview``; the ``overview`` is hand-built.

Verbs:

* ``enable {demo|live}`` — write the daemon + chosen presence unit, enable them,
  and disable the sibling (mutual exclusion). Delegates to ``ServiceManager``.
* ``disable`` — disable whichever presence unit is enabled; the daemon is left
  enabled deliberately. Delegates to ``ServiceManager``.
* ``status`` — which presence mode is enabled (or none) + daemon health.
  Delegates to ``ServiceManager``.
* ``install`` — write all three unit files + ``daemon-reload`` WITHOUT enabling
  anything (so a separate ``enable`` chooses the mode).
* ``uninstall`` — remove the unit files + ``daemon-reload``.

Every verb supports ``--json`` with the strict results→stdout /
errors+diagnostics→stderr split. A missing ``systemctl`` on PATH raises a clean
exit-2 :class:`CliError`; an invalid mode is rejected as an exit-1 user error.
"""

from __future__ import annotations

import argparse
import shutil
import subprocess  # nosec B404 - only ever runs the resolved systemctl binary

from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.service.manager import ServiceManager, _default_unit_dir
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
)

_JSON_HELP = "Emit structured JSON."

# The three units this noun manages, with their pure text renderers (t1). Used
# by install/uninstall to write/remove every unit file at once.
_ALL_UNITS = (
    (DAEMON_UNIT, daemon_unit_text),
    (DEMO_UNIT, demo_unit_text),
    (LIVE_UNIT, live_unit_text),
)

_VERBS = [
    "service enable demo — boot-persist the idle demo-mode presence (disables live)",
    "service enable live — boot-persist the folded live sense loop (disables demo)",
    "service disable — disable the enabled presence (daemon left enabled)",
    "service status — which presence mode is enabled (or none) + daemon health",
    "service install — write the unit files + daemon-reload, WITHOUT enabling",
    "service uninstall — remove the unit files + daemon-reload",
    "service overview — this summary",
]


# --- systemctl runner (production seam; monkeypatched in tests) -------------


def _systemctl_run(args: list[str]) -> subprocess.CompletedProcess:
    """Run one ``systemctl <args>`` invocation (the manager prepends ``--user``).

    Resolves ``systemctl`` via :func:`shutil.which` (no bandit B607 partial-path)
    and surfaces a missing binary as a clean exit-2 :class:`CliError` rather than
    a traceback. NEVER adds ``--user`` — the caller (the manager, or this module's
    install/uninstall helpers) already supplies it.
    """
    exe = shutil.which("systemctl")
    if exe is None:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="'systemctl' is not available",
            remediation="this needs a Linux systemd user session to manage boot persistence",
        )
    return subprocess.run(  # nosec B603 - resolved binary, arg list, no shell
        [exe, *args], capture_output=True, text=True, check=False
    )


def _daemon_health() -> bool:
    """Real daemon liveness probe (restart-safe HTTP health check)."""
    from reachy import daemon

    return daemon.is_robot_live()


def _manager() -> ServiceManager:
    """Build the real ServiceManager wired to this module's production seams.

    The runner / health are looked up *by name at call time* (via thin lambdas)
    so a test that monkeypatches ``service._systemctl_run`` / ``_daemon_health``
    is honoured.
    """
    return ServiceManager(
        run=lambda args: _systemctl_run(args),
        daemon_health=lambda: _daemon_health(),
    )


# --- overview --------------------------------------------------------------


def cmd_service_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "Makes the robot boot-persistent in EXACTLY ONE presence mode — the "
                "idle demo-mode loop or the folded live sense loop — never both.",
                "Backed by systemd --user units; enable one mode and the sibling is "
                "disabled (the single-presence-owner invariant).",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Units",
            "items": [
                f"daemon: {DAEMON_UNIT}",
                f"demo presence: {DEMO_UNIT}",
                f"live presence: {LIVE_UNIT}",
                f"unit dir: {_default_unit_dir()}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "results to stdout, errors/diagnostics to stderr (never mixed)",
                "talks to systemd, not the robot — no --transport flag",
                "install writes units without enabling; enable {demo|live} chooses the mode",
                "exit codes: 0 ok, 1 user error, 2 environment (systemctl missing)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli service",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- enable / disable / status (delegate to the manager) -------------------


def cmd_service_enable(args: argparse.Namespace) -> int:
    data = _manager().enable(args.mode)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_service_disable(args: argparse.Namespace) -> int:
    data = _manager().disable()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_service_status(args: argparse.Namespace) -> int:
    data = _manager().status()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


# --- install / uninstall (write/remove units, no enabling) -----------------


def _require(args: list[str], action: str) -> None:
    """Run a mutating ``systemctl`` command; raise a clean CliError on failure."""
    result = _systemctl_run(args)
    rc = getattr(result, "returncode", 0)
    if rc != 0:
        # Collapse systemctl's (possibly multi-line) output to ONE line — text CLI
        # errors must stay exactly two lines (error: / hint:).
        detail = " ".join(
            (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").split()
        )
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"{action} failed: {detail}" if detail else f"{action} failed",
            remediation="inspect 'systemctl --user status reachy-*.service' on the robot",
        )


def cmd_service_install(args: argparse.Namespace) -> int:
    """Write all three unit files + daemon-reload, WITHOUT enabling anything."""
    unit_dir = _default_unit_dir()
    unit_dir.mkdir(parents=True, exist_ok=True)
    written: dict[str, str] = {}
    for unit, render in _ALL_UNITS:
        path = unit_dir / unit
        path.write_text(render(), encoding="utf-8")
        written[unit] = str(path)
    _require(["--user", "daemon-reload"], "reload the systemd user manager")
    data = {"status": "installed", "unit_paths": written}
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_service_uninstall(args: argparse.Namespace) -> int:
    """Remove all three unit files + daemon-reload (best-effort, idempotent)."""
    unit_dir = _default_unit_dir()
    removed: list[str] = []
    for unit, _render in _ALL_UNITS:
        path = unit_dir / unit
        if path.is_file():
            path.unlink()
            removed.append(unit)
    _require(["--user", "daemon-reload"], "reload the systemd user manager")
    data = {
        "status": "uninstalled" if removed else "not-installed",
        "removed": removed,
    }
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_service_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "service",
        help="Boot-persistent presence in one mode (see 'reachy-mini-cli service overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    # parser_class=type(p) so nested parse errors keep the structured CliError
    # contract instead of argparse's default stderr/exit-2.
    noun_sub = p.add_subparsers(dest="service_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the service noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_service_overview)

    enable = noun_sub.add_parser("enable", help="Boot-persist exactly one presence mode.")
    enable.add_argument(
        "mode",
        choices=("demo", "live"),
        help="Which presence to boot-persist (the sibling is disabled).",
    )
    enable.add_argument("--json", action="store_true", help=_JSON_HELP)
    enable.set_defaults(func=cmd_service_enable)

    disable = noun_sub.add_parser("disable", help="Disable the enabled presence (daemon left up).")
    disable.add_argument("--json", action="store_true", help=_JSON_HELP)
    disable.set_defaults(func=cmd_service_disable)

    st = noun_sub.add_parser("status", help="Report the enabled presence mode + daemon health.")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.set_defaults(func=cmd_service_status)

    install = noun_sub.add_parser(
        "install", help="Write the unit files + daemon-reload (no enable)."
    )
    install.add_argument("--json", action="store_true", help=_JSON_HELP)
    install.set_defaults(func=cmd_service_install)

    uninstall = noun_sub.add_parser("uninstall", help="Remove the unit files + daemon-reload.")
    uninstall.add_argument("--json", action="store_true", help=_JSON_HELP)
    uninstall.set_defaults(func=cmd_service_uninstall)
