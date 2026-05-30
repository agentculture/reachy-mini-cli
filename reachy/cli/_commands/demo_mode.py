"""``reachy-mini-cli demo-mode`` — make the robot *feel alive*, as a managed service.

A continuously-running behaviour: a loop drives the Reachy Mini with gentle idle
motion (breathing, the occasional glance, antenna sway) so an otherwise idle
robot looks present rather than frozen. It is meant to run always-on and to be
improved over time, so it has three layers:

* **process** — ``start`` / ``stop`` / ``status`` / ``restart`` manage a tracked
  background loop (PID + log under the state dir), like the ``daemon`` noun;
* **config** — ``config`` reads/writes the persisted tuning (``demo-mode.json``)
  that ``run``/``start`` read; CLI flags override it per-invocation;
* **service** — ``install`` / ``enable`` / ``disable`` / ``uninstall`` manage a
  systemd ``--user`` unit so the loop starts on boot and restarts on crash.

``restart`` applies an update: edit the config (or the motion code), then restart.
If the systemd service is active it is restarted; otherwise the background loop is
relaunched. The loop drives the robot through the shared transport, so it needs a
running daemon — bring one up with ``reachy daemon start``.
"""

from __future__ import annotations

import argparse

from reachy import alive
from reachy import demo_config as dconf
from reachy import demo_service as dservice
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.robot import INTERPOLATIONS, TRANSPORTS, get_transport

_JSON_HELP = "Emit structured JSON."

_VERBS = [
    "demo-mode start — start the feel-alive loop in the background",
    "demo-mode stop — stop the loop this CLI started (eases robot to neutral)",
    "demo-mode restart — apply an update: restart the service, else the background loop",
    "demo-mode status — loop + service state and daemon reachability",
    "demo-mode run — run the loop in the foreground (what start/the service launch)",
    "demo-mode config — show / --init / --set the persisted tuning",
    "demo-mode install — write the systemd --user unit",
    "demo-mode enable — enable + start the service on boot (with linger)",
    "demo-mode disable — disable + stop the service",
    "demo-mode uninstall — remove the systemd unit",
    "demo-mode overview — this summary",
]


# --- shared args ----------------------------------------------------------


def _add_connection_and_tuning_args(parser: argparse.ArgumentParser) -> None:
    """Per-invocation overrides for ``run`` / ``start`` (all default to config)."""
    parser.add_argument("--json", action="store_true", help=_JSON_HELP)
    parser.add_argument(
        "--config", default=None, help="Path to the demo-mode config file (default: XDG location)."
    )
    parser.add_argument(
        "--transport", choices=TRANSPORTS, default=None, help="Override the transport flavor."
    )
    parser.add_argument("--base-url", default=None, help="Override the daemon base URL.")
    parser.add_argument("--timeout", type=float, default=None, help="Override the request timeout.")
    parser.add_argument(
        "--interval", type=float, default=None, help="Override seconds between poses."
    )
    parser.add_argument(
        "--energy", type=float, default=None, help="Override the liveliness multiplier (0..n)."
    )
    parser.add_argument(
        "--interpolation", choices=INTERPOLATIONS, default=None, help="Override the curve."
    )
    parser.add_argument("--seed", type=int, default=None, help="Override the motion RNG seed.")
    parser.add_argument(
        "--no-wake", action="store_true", help="Skip the wake animation on start (preflight only)."
    )
    parser.add_argument(
        "--no-settle", action="store_true", help="Do not ease the robot to neutral on stop."
    )


def _resolve_config(args: argparse.Namespace) -> dconf.DemoConfig:
    """Config file merged with explicit CLI flags (flags win); then validated."""
    cfg = dconf.load(getattr(args, "config", None))
    for key in ("transport", "base_url", "timeout", "interval", "energy", "interpolation", "seed"):
        val = getattr(args, key, None)
        if val is not None:
            setattr(cfg, key, val)
    if getattr(args, "no_wake", False):
        cfg.wake = False
    if getattr(args, "no_settle", False):
        cfg.settle = False
    errors = dconf.validate(cfg)
    if errors:
        raise CliError(
            code=EXIT_USER_ERROR,
            message="invalid demo-mode config: " + "; ".join(errors),
            remediation="fix it with 'reachy-mini-cli demo-mode config --set key=value'",
        )
    return cfg


def _transport_for(cfg: dconf.DemoConfig):
    shim = argparse.Namespace(transport=cfg.transport, base_url=cfg.base_url, timeout=cfg.timeout)
    return get_transport(shim)


# --- overview -------------------------------------------------------------


def cmd_demo_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A continuously-running loop that streams gentle idle motion to the "
                "robot so it 'feels alive' — breathing, the occasional glance, antenna sway.",
                "Run it ad-hoc (start/stop) or always-on as a systemd --user service.",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Config",
            "items": [
                f"file: {dconf.config_path()}",
                "keys: " + ", ".join(dconf.FIELDS),
                "precedence: CLI flag > config file > built-in default",
            ],
        },
        {
            "title": "State",
            "items": [
                f"pid file: {alive.pid_file()}",
                f"log file: {alive.log_file()}",
                f"unit: {dservice.unit_path()}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "edit config or motion code, then 'restart' to apply the update",
                "the service auto-restarts on crash and starts on boot (enable + linger)",
                "exit codes: 0 ok, 1 user error, 2 environment (daemon/systemctl missing)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli demo-mode",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- run (foreground loop) ------------------------------------------------


def cmd_demo_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    cfg = _resolve_config(args)
    transport = _transport_for(cfg)
    alive_cfg = cfg.to_alive_config()

    def _on_start() -> None:
        # Emitted only after the preflight succeeds, so a failed preflight yields
        # exactly the two-line error:/hint: contract (no stray startup line).
        if not json_mode:
            emit_diagnostic(
                f"[demo-mode] feeling alive: interval={alive_cfg.interval:g}s "
                f"energy={alive_cfg.energy:g} via {transport.name}; Ctrl-C to stop"
            )

    def _emit(event: dict) -> None:
        if json_mode:
            emit_result(event, json_mode=True)
        elif not event["ok"]:
            emit_diagnostic(f"[demo-mode] tick {event['tick']}: send failed ({event['error']})")

    ticks = alive.run_loop(
        transport,
        alive_cfg,
        on_start=_on_start,
        emit=_emit,
        max_ticks=args.max_ticks,
        wake=cfg.wake,
        settle=cfg.settle,
    )
    if not json_mode:
        emit_diagnostic(f"[demo-mode] stopped after {ticks} tick(s)")
    return 0


# --- start / stop / restart / status --------------------------------------


def _start_kwargs(cfg: dconf.DemoConfig) -> dict:
    return {
        "transport": cfg.transport,
        "base_url": cfg.base_url,
        "timeout": cfg.timeout,
        "interval": cfg.interval,
        "energy": cfg.energy,
        "interpolation": cfg.interpolation,
        "seed": cfg.seed,
        "wake": cfg.wake,
        "settle": cfg.settle,
    }


def cmd_demo_start(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    data = alive.start(**_start_kwargs(cfg))
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_demo_stop(args: argparse.Namespace) -> int:
    data = alive.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_demo_restart(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    # If the systemd service is running, restarting it is the right "apply update"
    # path (it re-execs ExecStart -> new code + config). Otherwise relaunch the
    # tracked background loop.
    if dservice.is_active():
        data = dservice.restart()
        data["mode"] = "service"
    else:
        cfg = _resolve_config(args)
        data = alive.restart(**_start_kwargs(cfg))
        data["mode"] = "process"
    emit_payload(data, json_mode=json_mode)
    return 0


def cmd_demo_status(args: argparse.Namespace) -> int:
    cfg = _resolve_config(args)
    data = alive.status(base_url=cfg.base_url, timeout=cfg.timeout)
    data["service"] = dservice.status()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


# --- config ---------------------------------------------------------------


def cmd_demo_config(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    path = getattr(args, "config", None)
    if args.init or args.set:
        cfg = dconf.load(path)
        if args.set:
            dconf.apply_set(cfg, args.set)
        errors = dconf.validate(cfg)
        if errors:
            raise CliError(
                code=EXIT_USER_ERROR,
                message="refusing to write invalid config: " + "; ".join(errors),
                remediation="correct the value(s) and retry",
            )
        written = dconf.save(cfg, path)
        data = {"status": "written", "path": str(written), "config": cfg.to_dict()}
    else:
        cfg = dconf.load(path)
        data = {"path": str(path or dconf.config_path()), "config": cfg.to_dict()}
    emit_payload(data, json_mode=json_mode)
    return 0


# --- service verbs --------------------------------------------------------


def cmd_demo_install(args: argparse.Namespace) -> int:
    # Ensure the config file the unit will read actually exists — whether or not
    # --config was given — so the service never points at a missing file.
    cfg_file = str(dconf.ensure(getattr(args, "config", None)))
    data = dservice.install(config_file=cfg_file)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_demo_enable(args: argparse.Namespace) -> int:
    data = dservice.enable(linger=not args.no_linger)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_demo_disable(args: argparse.Namespace) -> int:
    data = dservice.disable()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_demo_uninstall(args: argparse.Namespace) -> int:
    data = dservice.uninstall()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_demo_overview(args)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the feel-alive loop in the foreground.")
    _add_connection_and_tuning_args(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many poses (default: run until signalled).",
    )
    run.set_defaults(func=cmd_demo_run)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the feel-alive loop in the background.")
    _add_connection_and_tuning_args(start)
    start.set_defaults(func=cmd_demo_start)

    restart = noun_sub.add_parser(
        "restart", help="Apply an update: restart the service, else the background loop."
    )
    _add_connection_and_tuning_args(restart)
    restart.set_defaults(func=cmd_demo_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=alive.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {alive.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_demo_stop)

    st = noun_sub.add_parser("status", help="Report demo-mode process + service + daemon state.")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.add_argument("--config", default=None, help="Config file (for base-url/timeout).")
    st.add_argument("--base-url", default=None, help="Override the daemon base URL.")
    st.add_argument("--timeout", type=float, default=None, help="Override the request timeout.")
    st.set_defaults(func=cmd_demo_status)


def _register_config_verb(noun_sub: argparse._SubParsersAction) -> None:
    cfg = noun_sub.add_parser("config", help="Show / scaffold / set the persisted tuning.")
    cfg.add_argument("--json", action="store_true", help=_JSON_HELP)
    cfg.add_argument("--config", default=None, help="Config file path (default: XDG location).")
    cfg.add_argument(
        "--init", action="store_true", help="Write a default config file if keys are unset."
    )
    cfg.add_argument(
        "--set",
        nargs="*",
        default=None,
        metavar="KEY=VALUE",
        help="Set one or more config keys (e.g. energy=0.8 interval=3).",
    )
    cfg.set_defaults(func=cmd_demo_config)


def _register_service_verbs(noun_sub: argparse._SubParsersAction) -> None:
    install = noun_sub.add_parser("install", help="Write the systemd --user unit.")
    install.add_argument("--json", action="store_true", help=_JSON_HELP)
    install.add_argument("--config", default=None, help="Config file the unit should read.")
    install.set_defaults(func=cmd_demo_install)

    enable = noun_sub.add_parser("enable", help="Enable + start the service on boot.")
    enable.add_argument("--json", action="store_true", help=_JSON_HELP)
    enable.add_argument(
        "--no-linger",
        action="store_true",
        help="Do not enable linger (service then stops at logout).",
    )
    enable.set_defaults(func=cmd_demo_enable)

    disable = noun_sub.add_parser("disable", help="Disable + stop the service.")
    disable.add_argument("--json", action="store_true", help=_JSON_HELP)
    disable.set_defaults(func=cmd_demo_disable)

    uninstall = noun_sub.add_parser("uninstall", help="Remove the systemd unit.")
    uninstall.add_argument("--json", action="store_true", help=_JSON_HELP)
    uninstall.set_defaults(func=cmd_demo_uninstall)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "demo-mode",
        help="Make the robot feel alive (see 'reachy-mini-cli demo-mode overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="demo_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the demo-mode noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_demo_overview)

    _register_run(noun_sub)
    _register_process_verbs(noun_sub)
    _register_config_verb(noun_sub)
    _register_service_verbs(noun_sub)
