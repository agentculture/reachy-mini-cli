"""``reachy-mini-cli behavior`` — compose robot behaviors on a 50 Hz loop.

A persistent engine runs a 50 Hz loop and holds a set of active behaviors; you
push one-shot or looping behaviors onto it from separate invocations, and a
per-channel contention model (``passive`` / ``stoppable`` / ``unstoppable`` /
``stopping``) decides who drives ``head`` / ``antennas`` / ``body_yaw`` when they
conflict. ``feel-alive`` runs as a passive base layer so the robot stays alive on
any channel nothing else claims.

Most behaviors are pure motion; ``listen`` is *sensor-driven* — it reads the sound
Direction of Arrival from the daemon and orients the head (optionally the body)
toward it (``behavior run listen``), yielding back to ``feel-alive`` when there is
no sound (or no mic).

* ``behavior list`` — the built-in behavior catalog (no robot needed).
* ``behavior run`` / ``stop`` / ``status`` — drive the running engine (auto-starts
  it) through the command spool.
* ``behavior engine start|stop|status|run`` — manage the 50 Hz engine process.

The engine streams immediate ``set_target`` poses, so it owns motion exclusively
while running — don't drive the robot with ``move goto`` / ``demo-mode`` at the
same time.
"""

from __future__ import annotations

import argparse

from reachy.behavior import control, library, supervisor
from reachy.behavior.engine import EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.model import CHANNELS, StopClass
from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, read_doa
from reachy.cli._commands._robot import add_robot_args, emit_payload, get_transport, noun_overview
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.robot import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

_JSON_HELP = "Emit structured JSON."
_CLASSES = tuple(c.value for c in StopClass)

_VERBS = [
    "behavior list — the built-in behavior catalog (names, channels, class, params)",
    "behavior run <name> — push a behavior onto the running engine (auto-starts it)",
    "behavior stop <id|name|all> — stop a running behavior (all = keep the idle base)",
    "behavior status — active behaviors + per-channel ownership + engine/daemon state",
    "behavior engine start — start the 50 Hz engine in the background",
    "behavior engine stop — stop the engine (eases the robot to neutral)",
    "behavior engine status — engine process + daemon reachability",
    "behavior engine run — run the engine in the foreground (what start launches)",
    "behavior overview — this summary",
]


# --------------------------------------------------------------------------- #
# helpers                                                                     #
# --------------------------------------------------------------------------- #


def _parse_set(pairs: list[str] | None) -> dict[str, str]:
    """Parse ``key=value`` tokens into a dict, rejecting malformed ones."""
    out: dict[str, str] = {}
    for token in pairs or []:
        if "=" not in token:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"bad --set token {token!r} (expected key=value)",
                remediation="e.g. --set amp=20 period=0.5",
            )
        key, raw = token.split("=", 1)
        out[key.strip()] = raw.strip()
    return out


def _resolve_channels(names: list[str] | None) -> list[str] | None:
    if not names:
        return None
    bad = [n for n in names if n not in CHANNELS]
    if bad:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown channel(s): {', '.join(bad)}",
            remediation=f"valid channels: {', '.join(CHANNELS)}",
        )
    return list(names)


def _engine_config(args: argparse.Namespace) -> EngineConfig:
    return EngineConfig(
        compose_hz=args.compose_hz,
        base_layer=not args.no_base_layer,
        energy=args.energy,
        settle=not args.no_settle,
    )


# --------------------------------------------------------------------------- #
# overview / list                                                             #
# --------------------------------------------------------------------------- #


def cmd_overview(args: argparse.Namespace) -> int:
    noun_overview(
        "reachy-mini-cli behavior",
        _VERBS,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    entries = []
    for entry in library.LIBRARY.values():
        entries.append(
            {
                "name": entry.name,
                "summary": entry.summary,
                "channels": sorted(entry.channels),
                "default_class": entry.default_class.value,
                "kind": "looping" if entry.looping else "one-shot",
                "default_duration": entry.default_duration,
                "params": {
                    k: {"default": p.default, "unit": p.unit, "help": p.help}
                    for k, p in entry.params.items()
                },
            }
        )
    if json_mode:
        emit_result({"behaviors": entries}, json_mode=True)
    else:
        lines: list[str] = ["# behaviors", ""]
        for e in entries:
            dur = (
                "until stopped" if e["default_duration"] is None else f"{e['default_duration']:g}s"
            )
            lines.append(
                f"- {e['name']} [{e['kind']}, {e['default_class']}, {dur}] — {e['summary']}"
            )
            lines.append(f"    channels: {', '.join(e['channels'])}")
            if e["params"]:
                params = ", ".join(
                    f"{k}={p['default']:g}{p['unit']}" for k, p in e["params"].items()
                )
                lines.append(f"    params: {params}")
        emit_result("\n".join(lines), json_mode=False)
    return 0


# --------------------------------------------------------------------------- #
# run / stop / status (talk to the running engine via the spool)              #
# --------------------------------------------------------------------------- #


def cmd_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    entry = library.get(args.name)
    params = library.resolve_params(entry, _parse_set(args.set))
    stop_class = library.resolve_class(entry, args.behavior_class)
    lifetime = library.resolve_lifetime(
        entry, once=args.once, loop=args.loop, duration=args.duration
    )
    channels = _resolve_channels(args.channels)

    if not args.no_ensure_engine:
        supervisor.ensure_running(
            transport=args.transport,
            base_url=args.base_url,
            timeout=args.timeout,
            compose_hz=args.compose_hz,
            energy=args.energy,
            base_layer=not args.no_base_layer,
            settle=not args.no_settle,
        )

    cmd_id = control.submit(
        "add",
        name=args.name,
        params=params,
        lifetime={"looping": lifetime.looping, "duration": lifetime.duration},
        channels=channels,
        **{"class": stop_class.value},
    )
    result = control.await_result(cmd_id, timeout=args.await_timeout)
    if result is None:
        result = {
            "ok": False,
            "submitted": cmd_id,
            "note": "engine did not confirm in time — is 'behavior engine' running?",
        }
    emit_payload(result, json_mode=json_mode, empty="(submitted)")
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    cmd_id = control.submit("stop", target=args.target)
    result = control.await_result(cmd_id, timeout=args.await_timeout)
    if result is None:
        result = {"ok": False, "submitted": cmd_id, "note": "engine did not confirm in time"}
    emit_payload(result, json_mode=json_mode, empty="(submitted)")
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    engine_state = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    data: dict[str, object] = {"engine": engine_state}
    published = control.read_state()
    if published is None:
        data["active"] = []
        data["ownership"] = dict.fromkeys(CHANNELS)
        data["note"] = "engine has not published state (not running, or just started)"
    else:
        data["active"] = published.get("active", [])
        data["ownership"] = published.get("ownership", {})
        data["compose_hz"] = published.get("compose_hz")
        if "doa" in published:
            data["doa"] = published["doa"]
    emit_payload(data, json_mode=json_mode)
    return 0


# --------------------------------------------------------------------------- #
# engine sub-noun (the 50 Hz process)                                         #
# --------------------------------------------------------------------------- #


def cmd_engine_overview(args: argparse.Namespace) -> int:
    emit_overview(
        "reachy-mini-cli behavior engine",
        [
            {
                "title": "What",
                "items": [
                    "The persistent 50 Hz loop that composes active behaviors and "
                    "streams one immediate pose per tick to the robot.",
                    "It owns motion exclusively while running — don't also use "
                    "'move goto' / 'demo-mode'.",
                ],
            },
            {
                "title": "Verbs",
                "items": [
                    "engine start — spawn the loop in the background",
                    "engine stop — stop it (eases the robot to neutral)",
                    "engine status — process + daemon reachability",
                    "engine run — run it in the foreground (what start launches)",
                    "engine overview — this summary",
                ],
            },
            {
                "title": "State",
                "items": [
                    f"pid file: {supervisor.pid_file()}",
                    f"log file: {supervisor.log_file()}",
                    f"control spool: {control.behavior_dir()}",
                ],
            },
        ],
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


def cmd_engine_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        compose_hz=args.compose_hz,
        energy=args.energy,
        base_layer=not args.no_base_layer,
        settle=not args.no_settle,
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_engine_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_engine_status(args: argparse.Namespace) -> int:
    data = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_engine_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    config = _engine_config(args)
    spool = control.CommandSpool()
    # Sound Direction-of-Arrival source for sensor-driven behaviors (e.g. listen).
    # Polled only while such a behavior is active, throttled, with a short timeout;
    # any failure (no mic, unsupported transport) degrades to "no reading".
    sense = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))

    def _on_start() -> None:
        if not json_mode:
            emit_diagnostic(
                f"[behavior] engine live: {config.compose_hz:g} Hz via {transport.name}"
                f"{' + base layer' if config.base_layer else ''}; Ctrl-C to stop"
            )

    def _emit(event: dict) -> None:
        if json_mode:
            emit_result(event, json_mode=True)

    ticks = engine_run(
        transport,
        config,
        on_start=_on_start,
        emit=_emit,
        max_ticks=args.max_ticks,
        control=spool,
        sense=sense,
    )
    if not json_mode:
        emit_diagnostic(f"[behavior] engine stopped after {ticks} tick(s)")
    return 0


# --------------------------------------------------------------------------- #
# registration                                                                #
# --------------------------------------------------------------------------- #


def _add_engine_tuning(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--compose-hz",
        type=float,
        default=50.0,
        dest="compose_hz",
        help="Engine tick rate in Hz (default: 50).",
    )
    parser.add_argument(
        "--energy", type=float, default=1.0, help="Base-layer liveliness multiplier (default: 1.0)."
    )
    parser.add_argument(
        "--no-base-layer",
        action="store_true",
        dest="no_base_layer",
        help="Do not seed the passive feel-alive base layer.",
    )
    parser.add_argument(
        "--no-settle",
        action="store_true",
        dest="no_settle",
        help="Do not ease the robot to neutral on stop.",
    )


def _register_list(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("list", help="List the built-in behaviors.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_list)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("run", help="Push a behavior onto the running engine.")
    p.add_argument("name", help="Behavior name (see 'behavior list').")
    p.add_argument(
        "--set",
        nargs="*",
        default=None,
        metavar="KEY=VALUE",
        help="Override behavior parameters (e.g. --set amp=20 period=0.5).",
    )
    p.add_argument(
        "--class",
        dest="behavior_class",
        choices=_CLASSES,
        default=None,
        help="Contention class (default: the behavior's own).",
    )
    p.add_argument(
        "--channels",
        nargs="*",
        default=None,
        metavar="CHANNEL",
        help=f"Override claimed channels ({', '.join(CHANNELS)}).",
    )
    group = p.add_mutually_exclusive_group()
    group.add_argument("--once", action="store_true", help="Run once (one-shot).")
    group.add_argument("--loop", action="store_true", help="Run looping until stopped.")
    p.add_argument(
        "--duration", type=float, default=None, help="Lifetime in seconds (default: per behavior)."
    )
    p.add_argument(
        "--no-ensure-engine",
        action="store_true",
        dest="no_ensure_engine",
        help="Do not auto-start the engine if it is not running.",
    )
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help="Seconds to wait for the engine to confirm (default: 1.0).",
    )
    _add_engine_tuning(p)  # forwarded to an auto-start
    add_robot_args(p)
    p.set_defaults(func=cmd_run)


def _register_stop(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("stop", help="Stop a running behavior (id | name | all).")
    p.add_argument("target", help="Behavior id, name, or 'all' (keeps the idle base layer).")
    p.add_argument(
        "--await-timeout",
        type=float,
        default=1.0,
        dest="await_timeout",
        help="Seconds to wait for the engine to confirm (default: 1.0).",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=cmd_stop)


def _register_status(noun_sub: argparse._SubParsersAction) -> None:
    p = noun_sub.add_parser("status", help="Active behaviors + channel ownership + engine state.")
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Daemon base URL.")
    p.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout.")
    p.set_defaults(func=cmd_status)


def _register_engine(noun_sub: argparse._SubParsersAction) -> None:
    eng = noun_sub.add_parser("engine", help="Manage the 50 Hz engine process.")
    eng.add_argument("--json", action="store_true", help=_JSON_HELP)
    eng.set_defaults(func=cmd_engine_overview, json=False)
    eng_sub = eng.add_subparsers(dest="engine_command", parser_class=type(eng))

    ov = eng_sub.add_parser("overview", help="Describe the engine sub-noun.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_engine_overview)

    start = eng_sub.add_parser("start", help="Start the engine in the background.")
    _add_engine_tuning(start)
    add_robot_args(start)
    start.set_defaults(func=cmd_engine_start)

    run = eng_sub.add_parser("run", help="Run the engine in the foreground.")
    _add_engine_tuning(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many ticks (default: run until signalled).",
    )
    add_robot_args(run)
    run.set_defaults(func=cmd_engine_run)

    stop = eng_sub.add_parser("stop", help="Stop the engine.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_engine_stop)

    st = eng_sub.add_parser("status", help="Engine process + daemon reachability.")
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.add_argument("--base-url", default=DEFAULT_BASE_URL, help="Daemon base URL.")
    st.add_argument("--timeout", type=float, default=DEFAULT_TIMEOUT, help="Request timeout.")
    st.set_defaults(func=cmd_engine_status)


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_overview(args)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "behavior",
        help="Compose robot behaviors on a 50 Hz loop (see 'reachy-mini-cli behavior overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="behavior_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the behavior noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_overview)

    _register_list(noun_sub)
    _register_run(noun_sub)
    _register_stop(noun_sub)
    _register_status(noun_sub)
    _register_engine(noun_sub)
