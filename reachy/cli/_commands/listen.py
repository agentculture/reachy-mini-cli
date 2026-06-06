"""``reachy-mini-cli listen`` — orient the head toward the direction of sound.

A sound-reactive loop: it reads the mic array's Direction of Arrival (DoA) from
the daemon and turns the head toward a *sustained, off-axis* sound, then holds
there before reconsidering, easing back to center after silence. Unlike the
behavior engine (which streams immediate ``set_target`` poses at 50 Hz), this
loop drives the robot with the daemon's smooth minjerk ``goto`` planner and runs
moves strictly one at a time through a serial motion queue — so reorienting turns
are soft and never conflict.

Three faces, like the ``daemon`` / ``demo-mode`` nouns:

* **run** — the foreground loop (what ``start`` / the process launch run);
* **start** / **stop** / **restart** — manage it as a tracked background process
  (PID + log under the state dir);
* **status** — loop + daemon reachability.

It degrades gracefully: no mic / no daemon DoA ⇒ no reaction, no crash. The loop
drives the robot through the shared transport, so it needs a running daemon —
bring one up with ``reachy daemon start``.
"""

from __future__ import annotations

import argparse

from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.motion import supervisor
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.server import run as run_loop
from reachy.robot import add_robot_args, get_transport

_JSON_HELP = "Emit structured JSON."
_CENTER = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

_VERBS = [
    "listen run — run the sound-orienting loop in the foreground",
    "listen start — start the loop in the background (tracked process)",
    "listen stop — stop the loop this CLI started (eases robot to center)",
    "listen restart — restart the background loop (re-reads tuning + code)",
    "listen status — loop process state + daemon reachability",
    "listen overview — this summary",
]


# --- shared args ----------------------------------------------------------


def _add_tuning_args(parser: argparse.ArgumentParser) -> None:
    """Listen feel knobs (degrees / seconds / deg-per-second); unset ⇒ built-in default."""
    d = ListenParams()
    parser.add_argument("--gain", type=float, default=None, help="head-yaw gain per DoA angle.")
    parser.add_argument(
        "--max-yaw",
        type=float,
        default=None,
        dest="max_yaw",
        help=f"max head yaw toward sound (deg, default {d.max_yaw:g}).",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=None,
        help=f"ignore sound within this of the current heading (deg, default {d.deadband:g}).",
    )
    parser.add_argument(
        "--dwell",
        type=float,
        default=None,
        help=f"a direction must persist this long before turning (s, default {d.dwell:g}).",
    )
    parser.add_argument(
        "--hold",
        type=float,
        default=None,
        help=f"after turning, stay this long before reconsidering (s, default {d.hold:g}).",
    )
    parser.add_argument(
        "--speed",
        type=float,
        default=None,
        help=f"turn/return slew speed (deg/s, default {d.alert_speed:g}).",
    )
    parser.add_argument(
        "--recenter-after",
        type=float,
        default=None,
        dest="recenter_after",
        help=f"ease to center after this long with no sound (s, default {d.recenter_after:g}).",
    )
    parser.add_argument(
        "--speech-only",
        action="store_true",
        dest="speech_only",
        help="react only to detected speech (default: any sound).",
    )


def _params_from_args(args: argparse.Namespace) -> ListenParams:
    """A :class:`ListenParams` from CLI flags (each unset flag keeps its default)."""
    p = ListenParams()
    if args.gain is not None:
        p.gain = args.gain
    if args.max_yaw is not None:
        p.max_yaw = args.max_yaw
    if args.deadband is not None:
        p.deadband = args.deadband
    if args.dwell is not None:
        p.dwell = args.dwell
    if args.hold is not None:
        p.hold = args.hold
    if args.speed is not None:
        p.alert_speed = p.relax_speed = args.speed
    if args.recenter_after is not None:
        p.recenter_after = args.recenter_after
    if getattr(args, "speech_only", False):
        p.speech_only = True
    return p


# --- overview -------------------------------------------------------------


def cmd_listen_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A sound-reactive loop: turn the head toward a sustained, off-axis "
                "sound (mic-array DoA), hold there, then ease back to center after silence.",
                "Smooth by construction — drives the daemon's minjerk 'goto' planner, "
                "one move at a time through a serial motion queue (no jerky streaming).",
                "Graceful: no mic / no daemon DoA ⇒ no reaction, no crash.",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "State",
            "items": [
                f"pid file: {supervisor.pid_file()}",
                f"log file: {supervisor.log_file()}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "tune the feel with --dwell / --hold / --speed / --deadband / --gain",
                "needs a running daemon (reachy daemon start) for the http transport",
                "exit codes: 0 ok, 1 user error, 2 environment (daemon unreachable)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli listen",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- run (foreground loop) ------------------------------------------------


def cmd_listen_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    params = _params_from_args(args)
    poller = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))
    producer = ListenProducer(params)

    # Preflight: ease to center. Validates the transport (a dead daemon raises a
    # clean CliError → tidy exit) and gives the loop a known starting pose.
    transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
    if not json_mode:
        emit_diagnostic(
            f"[listen] orienting to sound via {transport.name}: dwell={params.dwell:g}s "
            f"hold={params.hold:g}s speed={params.alert_speed:g}deg/s"
            f"{' (speech only)' if params.speech_only else ''}; Ctrl-C to stop"
        )

    def _on_action(action) -> None:
        yaw = action.head.get("yaw") if action.head else None
        if json_mode:
            emit_result(
                {"action": action.label, "yaw": yaw, "duration": round(action.duration, 3)},
                json_mode=True,
            )
        else:
            emit_diagnostic(f"[listen] {action.label} ({action.duration:.1f}s)")

    ticks = run_loop(
        transport, producer, sense=poller, on_action=_on_action, max_ticks=args.max_ticks
    )

    # Settle: ease back to center (best effort — a dead daemon can't be settled).
    try:
        transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
    except CliError:
        pass
    if not json_mode:
        emit_diagnostic(f"[listen] stopped after {ticks} tick(s)")
    return 0


# --- start / stop / restart / status --------------------------------------


def cmd_listen_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_restart(args: argparse.Namespace) -> int:
    data = supervisor.restart(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_listen_status(args: argparse.Namespace) -> int:
    data = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_listen_overview(args)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the sound-orienting loop in the foreground.")
    add_robot_args(run)
    _add_tuning_args(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many loop ticks (default: run until signalled).",
    )
    run.set_defaults(func=cmd_listen_run)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the sound-orienting loop in the background.")
    add_robot_args(start)
    _add_tuning_args(start)
    start.set_defaults(func=cmd_listen_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads tuning).")
    add_robot_args(restart)
    _add_tuning_args(restart)
    restart.set_defaults(func=cmd_listen_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_listen_stop)

    st = noun_sub.add_parser("status", help="Report listen process + daemon state.")
    add_robot_args(st)
    st.set_defaults(func=cmd_listen_status)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "listen",
        help="Orient the head toward sound (see 'reachy-mini-cli listen overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="listen_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the listen noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_listen_overview)

    _register_run(noun_sub)
    _register_process_verbs(noun_sub)
