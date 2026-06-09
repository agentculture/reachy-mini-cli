"""``reachy-mini-cli vision`` — orient the head toward what the robot *sees*.

A visual-reactive loop: it reads the camera frame via the local SDK transport,
runs two pixel-level detectors (:mod:`reachy.vision.motion` for moving objects
and :mod:`reachy.vision.light` for brightness changes) and turns the head toward
the strongest visual event via the daemon's smooth minjerk ``goto`` planner.

Like the ``listen`` noun, this has three faces:

* **run** — the foreground loop (what ``start`` / the process launch run);
* **start** / **stop** / **restart** — manage it as a tracked background process
  (PID + log under the state dir);
* **status** — loop + daemon reachability.
* **specs** — report camera metadata (resolution, name, intrinsics) — remote-safe.

The default transport is ``sdk`` (frames need the local camera); ``vision specs``
may use http (the daemon REST API serves camera metadata without frames).
"""

from __future__ import annotations

import argparse
import os

from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.looputil import install_stop_handlers, restore_stop_handlers
from reachy.robot import add_robot_args, get_transport
from reachy.vision import supervisor
from reachy.vision.producer import VisionParams

_JSON_HELP = "Emit structured JSON."
_CENTER = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

_VERBS = [
    "vision run — run the visual-orienting loop in the foreground",
    "vision start — start the loop in the background (tracked process)",
    "vision stop — stop the loop this CLI started (eases robot to center)",
    "vision restart — restart the background loop (re-reads tuning + code)",
    "vision status — loop process state + daemon reachability",
    "vision specs — report camera metadata (resolution, name, intrinsics)",
    "vision overview — this summary",
]


# --- shared args ----------------------------------------------------------


def _add_tuning_args(parser: argparse.ArgumentParser) -> None:
    """Vision feel knobs (degrees / seconds / deg-per-second); unset ⇒ built-in default."""
    d = VisionParams()
    parser.add_argument("--gain", type=float, default=None, help="head-yaw gain per direction.")
    parser.add_argument(
        "--max-yaw",
        type=float,
        default=None,
        dest="max_yaw",
        help=f"max head yaw toward a visual target (deg, default {d.max_yaw:g}).",
    )
    parser.add_argument(
        "--deadband",
        type=float,
        default=None,
        help=f"ignore targets within this of the current heading (deg, default {d.deadband:g}).",
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
        help=f"turn slew speed (deg/s, default {d.speed:g}).",
    )
    parser.add_argument(
        "--motion-threshold",
        type=float,
        default=None,
        dest="motion_threshold",
        help=f"minimum motion magnitude to trigger a head turn (default {d.motion_threshold:g}).",
    )


def _params_from_args(args: argparse.Namespace) -> VisionParams:
    """A :class:`VisionParams` from CLI flags (each unset flag keeps its default)."""
    p = VisionParams()
    if args.gain is not None:
        p.gain = args.gain
    if args.max_yaw is not None:
        p.max_yaw = args.max_yaw
    if args.deadband is not None:
        p.deadband = args.deadband
    if args.hold is not None:
        p.hold = args.hold
    if args.speed is not None:
        p.speed = args.speed
    if getattr(args, "motion_threshold", None) is not None:
        p.motion_threshold = args.motion_threshold
    return p


# --- overview -------------------------------------------------------------


def cmd_vision_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A pixel-based visual-reactive loop: motion detection (frame differencing) "
                "and light change detection orient the head toward the strongest visual event.",
                "Motion is the primary cue: a moving object in frame drives a head turn. "
                "A significant light change is the fallback cue when no motion fires.",
                "SDK-first by default: frames come via the local camera (in-process); "
                "use --transport http for camera-specs-only queries (no frames over HTTP).",
                "Smooth by construction — drives the daemon's minjerk 'goto' planner, "
                "one move at a time through a serial motion queue (no jerky streaming).",
                "Graceful: no camera / no daemon ⇒ clean exit-2 CliError, no crash.",
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
                "SDK-first by default: frames need the local camera (sdk/daemon extra); "
                "use --transport http for remote specs-only use",
                "feel knobs: --gain / --max-yaw / --deadband / --hold / --speed",
                "detector knob: --motion-threshold",
                "exit codes: 0 ok, 1 user error, 2 environment (daemon/camera unreachable)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli vision",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- run (foreground loop) ------------------------------------------------


def cmd_vision_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    params = _params_from_args(args)

    from reachy.vision.producer import VisionProducer

    producer = VisionProducer(transport=transport, params=params)

    # Preflight: ease to center. Validates the transport (a dead daemon raises a
    # clean CliError → tidy exit) and gives the loop a known starting pose.
    transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
    if not json_mode:
        emit_diagnostic(
            f"[vision] orienting to visual cues via {transport.name}: "
            f"deadband={params.deadband:g}deg hold={params.hold:g}s "
            f"speed={params.speed:g}deg/s; Ctrl-C to stop"
        )

    def _on_action(action) -> None:
        yaw = action.head.get("yaw") if action.head else None
        if json_mode:
            emit_result(
                {"action": action.label, "yaw": yaw, "duration": round(action.duration, 3)},
                json_mode=True,
            )
        else:
            emit_diagnostic(f"[vision] {action.label} ({action.duration:.1f}s)")

    # Install SIGTERM/SIGINT handlers so `vision stop` and Ctrl-C flip the stop
    # flag and let the loop exit cleanly — otherwise the signal kills the process
    # mid-loop and the settle-to-center below never runs, leaving the head off
    # center despite the supervisor's "eases back to center" contract.
    stop = {"flag": False}
    handlers = install_stop_handlers(stop)
    ticks = 0
    try:
        ticks = producer.run(max_ticks=args.max_ticks, on_action=_on_action, stop=stop)
    finally:
        restore_stop_handlers(handlers)
        # Settle: ease back to center (best effort — a dead daemon can't be settled).
        try:
            transport.move_goto(head=dict(_CENTER), duration=0.8, interpolation="minjerk")
        except CliError:
            pass
    if not json_mode:
        emit_diagnostic(f"[vision] stopped after {ticks} tick(s)")
    return 0


# --- specs ----------------------------------------------------------------


def cmd_vision_specs(args: argparse.Namespace) -> int:
    transport = get_transport(args)
    data = transport.camera_specs()
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


# --- start / stop / restart / status --------------------------------------


def cmd_vision_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_vision_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_vision_restart(args: argparse.Namespace) -> int:
    data = supervisor.restart(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        params=_params_from_args(args),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_vision_status(args: argparse.Namespace) -> int:
    data = supervisor.status(base_url=args.base_url, timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_vision_overview(args)


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the visual-orienting loop in the foreground.")
    add_robot_args(run)
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(run)
    run.add_argument(
        "--max-ticks",
        type=int,
        default=None,
        dest="max_ticks",
        help="Stop after this many loop ticks (default: run until signalled).",
    )
    run.set_defaults(func=cmd_vision_run)


def _register_specs(noun_sub: argparse._SubParsersAction) -> None:
    sp = noun_sub.add_parser("specs", help="Report camera metadata (resolution, name, intrinsics).")
    add_robot_args(sp)
    sp.set_defaults(func=cmd_vision_specs)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the visual-orienting loop in the background.")
    add_robot_args(start)
    start.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(start)
    start.set_defaults(func=cmd_vision_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads tuning).")
    add_robot_args(restart)
    restart.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(restart)
    restart.set_defaults(func=cmd_vision_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_vision_stop)

    st = noun_sub.add_parser("status", help="Report vision process + daemon state.")
    add_robot_args(st)
    st.set_defaults(func=cmd_vision_status)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "vision",
        help="Orient the head toward visual cues (see 'reachy-mini-cli vision overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="vision_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the vision noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_vision_overview)

    _register_run(noun_sub)
    _register_specs(noun_sub)
    _register_process_verbs(noun_sub)
