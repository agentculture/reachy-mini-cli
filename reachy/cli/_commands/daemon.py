"""``reachy-mini-cli daemon`` — local daemon process lifecycle.

The robot verbs (``device`` / ``app`` / ``move``) talk *to* a running daemon;
this noun is the other half — it brings the local ``reachy-mini-daemon`` process
up and down. ``start`` spawns it in the background and waits for its health
route, ``stop`` terminates it, ``status`` reconciles the tracked process with the
HTTP health check. The daemon binary ships in the ``[daemon]`` extra; a missing
binary yields a clean exit-2 hint pointing at the install.

Note: ``reachy-mini-daemon`` defaults to ``--wake-up-on-start``, so ``daemon
start`` already wakes the robot. Forward ``-- --no-wake-up-on-start`` to skip it.
"""

from __future__ import annotations

import argparse
import os

from reachy import daemon as _daemon
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

_JSON_HELP = "Emit structured JSON."

_VERBS = [
    "daemon start — start the local reachy-mini-daemon in the background",
    "daemon stop — stop the daemon this CLI started",
    "daemon status — process + HTTP-health state of the daemon",
    "daemon overview — this summary",
]


def _add_health_args(parser: argparse.ArgumentParser) -> None:
    """Flags shared by verbs that talk to the daemon's health route."""
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)
    parser.add_argument(
        "--base-url",
        default=os.environ.get("REACHY_BASE_URL", DEFAULT_BASE_URL),
        help="Daemon base URL for the health check (env REACHY_BASE_URL).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Health-check request timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )


def cmd_daemon_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "State",
            "items": [
                f"pid file: {_daemon.pid_file()}",
                f"log file: {_daemon.log_file()}",
                f"health route: {DEFAULT_BASE_URL}{_daemon.HEALTH_PATH}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "start spawns reachy-mini-daemon detached, then polls the health route",
                "the daemon ships in the [daemon] extra: pip install 'reachy-cli[daemon]'",
                "override the binary with --daemon-cmd or REACHY_DAEMON_CMD",
                "forward daemon args after '--' (e.g. -- --sim --fastapi-port 9000)",
                "exit codes: 0 ok, 1 user error, 2 environment (binary/daemon missing)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli daemon",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_daemon_start(args: argparse.Namespace) -> int:
    data = _daemon.start(
        base_url=args.base_url,
        wait=not args.no_wait,
        wait_timeout=args.wait_timeout,
        poll_timeout=args.timeout,
        daemon_cmd=args.daemon_cmd,
        extra_args=list(args.daemon_args or []),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_daemon_stop(args: argparse.Namespace) -> int:
    data = _daemon.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_daemon_status(args: argparse.Namespace) -> int:
    data = _daemon.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_daemon_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "daemon",
        help="Local daemon process lifecycle (see 'reachy-mini-cli daemon overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="daemon_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the daemon noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_daemon_overview)

    start = noun_sub.add_parser("start", help="Start the local reachy-mini-daemon.")
    _add_health_args(start)
    start.add_argument(
        "--daemon-cmd",
        default=None,
        help="Override the daemon launch command (env REACHY_DAEMON_CMD).",
    )
    start.add_argument(
        "--no-wait",
        action="store_true",
        help="Return immediately after spawning, without polling the health route.",
    )
    start.add_argument(
        "--wait-timeout",
        type=float,
        default=_daemon.DEFAULT_WAIT_TIMEOUT,
        help="Seconds to wait for the daemon to become healthy "
        f"(default: {_daemon.DEFAULT_WAIT_TIMEOUT:g}).",
    )
    start.add_argument(
        "daemon_args",
        nargs="*",
        default=[],
        metavar="-- ARGS",
        help="Args after '--' are forwarded to reachy-mini-daemon "
        "(e.g. -- --sim --fastapi-port 9000).",
    )
    start.set_defaults(func=cmd_daemon_start)

    stop = noun_sub.add_parser("stop", help="Stop the daemon this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=_daemon.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {_daemon.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_daemon_stop)

    st = noun_sub.add_parser("status", help="Report daemon process + HTTP-health state.")
    _add_health_args(st)
    st.set_defaults(func=cmd_daemon_status)
