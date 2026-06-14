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
import os
from typing import Callable

import numpy as np

from reachy.behavior.sense import DOA_TIMEOUT, DoaPoller, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.motion import supervisor
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.listen_pat import PatHook
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionQueue
from reachy.motion.server import LoopHooks
from reachy.motion.server import run as run_loop
from reachy.motion.snap import SnapDetector
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
        help="silence grace before the head/body start drifting home "
        f"(s, default {d.recenter_after:g}).",
    )
    parser.add_argument(
        "--idle-energy",
        type=float,
        default=None,
        dest="idle_energy",
        help="liveliness of the always-alive idle motion; 0 holds still between sounds "
        f"(default {d.idle_energy:g}).",
    )
    parser.add_argument(
        "--drift-speed",
        type=float,
        default=None,
        dest="drift_speed",
        help="speed the head/body drift home after silence (deg/s, " f"default {d.drift_speed:g}).",
    )
    parser.add_argument(
        "--speech-only",
        action="store_true",
        dest="speech_only",
        help="react only to detected speech (default: any sound).",
    )
    parser.add_argument(
        "--antenna-gain",
        type=float,
        default=None,
        dest="antenna_gain",
        help=f"scales Tier-1 antenna lean magnitude (default {d.antenna_gain:g}).",
    )
    parser.add_argument(
        "--antenna-max",
        type=float,
        default=None,
        dest="antenna_max",
        help=f"maximum near-side antenna deflection (deg, default {d.antenna_max:g}).",
    )
    parser.add_argument(
        "--body-yaw-max",
        type=float,
        default=None,
        dest="body_yaw_max",
        help=f"max body yaw for Tier-2 head/body escalation (deg, default {d.body_yaw_max:g}).",
    )
    parser.add_argument(
        "--body-speed",
        type=float,
        default=None,
        dest="body_speed",
        help=f"body turn slew speed for Tier-2 escalation (deg/s, default {d.body_speed:g}).",
    )
    parser.add_argument(
        "--head-only-band",
        type=float,
        default=None,
        dest="head_only_band",
        help=f"|desired| <= this uses head-only; beyond triggers body escalation "
        f"(deg, default {d.head_only_band:g}).",
    )
    parser.add_argument(
        "--snap-ratio",
        type=float,
        default=None,
        dest="snap_ratio",
        help="RMS snap detector: loudness ratio over rolling average to fire (default 5.0).",
    )
    parser.add_argument(
        "--snap-floor",
        type=float,
        default=None,
        dest="snap_floor",
        help="RMS snap detector: absolute RMS floor below which chunks are ignored (default 0.02).",
    )


def _add_pat_args(parser: argparse.ArgumentParser) -> None:
    """Head-pat detection toggle + tuning (SDK transport only; on by default).

    ``--pat`` / ``--no-pat`` fold proprioceptive head-pat detection into the SDK
    loop (the loop owns the single SDK client, so its head-pose read-backs are
    fast enough to detect a pat). The tuning knobs mirror the standalone ``pat``
    noun; unset ⇒ the detector's built-in default.
    """
    parser.add_argument(
        "--pat",
        action="store_true",
        dest="pat",
        default=True,
        help="detect head pats inside the sdk loop and lean into them (default: on).",
    )
    parser.add_argument(
        "--no-pat",
        action="store_false",
        dest="pat",
        help="do not detect head pats (sound-orienting only).",
    )
    parser.add_argument(
        "--press-threshold",
        type=float,
        default=None,
        dest="press_threshold",
        help="pat: pitch deviation (deg) past which a head-press counts (default 1.2).",
    )
    parser.add_argument(
        "--min-presses",
        type=int,
        default=None,
        dest="min_presses",
        help="pat: presses within the window needed to trigger a pat (default 2).",
    )


# 1:1 ``(arg attr, ListenParams attr)`` flags: an unset CLI flag (``None``) keeps
# the param's default. The genuinely special cases (--speed sets two fields,
# --speech-only is a bool flag, --pat is a default-True toggle) are handled apart.
_SIMPLE_PARAM_MAP: tuple[tuple[str, str], ...] = (
    ("gain", "gain"),
    ("max_yaw", "max_yaw"),
    ("deadband", "deadband"),
    ("dwell", "dwell"),
    ("hold", "hold"),
    ("recenter_after", "recenter_after"),
    ("idle_energy", "idle_energy"),
    ("drift_speed", "drift_speed"),
    ("antenna_gain", "antenna_gain"),
    ("antenna_max", "antenna_max"),
    ("body_yaw_max", "body_yaw_max"),
    ("body_speed", "body_speed"),
    ("head_only_band", "head_only_band"),
)


def _params_from_args(args: argparse.Namespace) -> ListenParams:
    """A :class:`ListenParams` from CLI flags (each unset flag keeps its default)."""
    p = ListenParams()
    for arg_name, attr in _SIMPLE_PARAM_MAP:
        value = getattr(args, arg_name, None)
        if value is not None:
            setattr(p, attr, value)
    # Special cases: --speed drives both slew speeds; --speech-only / --no-pat are
    # bool toggles, not value flags.
    if getattr(args, "speed", None) is not None:
        p.alert_speed = p.relax_speed = args.speed
    if getattr(args, "speech_only", False):
        p.speech_only = True
    if getattr(args, "pat", True) is False:
        p.pat = False
    return p


# --- overview -------------------------------------------------------------


def cmd_listen_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A two-tier sound-reactive loop using real mic-array DoA + RMS loudness "
                "(SDK-first by default).",
                "Tier-1 (near-side antenna lean): on any live sound that does not trigger a "
                "head turn, the antenna facing the source deflects gently toward it — "
                "a subtle 'I hear you' cue.",
                "Tier-2 (head→body 'turn to see'): on detected speech OR a loud RMS snap "
                "transient, the head turns toward the source; when the angle exceeds "
                "head-only-band the body rotates too (head re-centres on the residual) "
                "so the whole robot faces the sound.",
                "Always-alive idle: between sounds the robot keeps gently moving "
                "(breathing, slow gaze wander, antenna sway) around its current heading — "
                "if it turned toward a sound it stays rotated and keeps moving there, "
                "then drifts slowly back to front after silence (never frozen, never a "
                "hard snap). Tune with --idle-energy / --drift-speed (--idle-energy 0 "
                "restores hold-still).",
                "Head pats too (sdk only): the loop reads the head pose back in-process "
                "each tick, so a downward press or sideways nudge is detected as a pat and "
                "the robot leans into it (lean→nuzzle→settle) while still reacting to sound. "
                "On by default; --no-pat turns it off.",
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
                "SDK-first by default: real DoA + mic loudness in-process; "
                "use --transport http for the remote/daemon profile",
                "Tier-1 knobs: --antenna-gain / --antenna-max",
                "Tier-2 knobs: --head-only-band / --body-yaw-max / --body-speed",
                "idle knobs: --idle-energy / --drift-speed / --recenter-after",
                "feel knobs: --dwell / --hold / --speed / --deadband / --gain",
                "head-pat (sdk only): --pat / --no-pat (default on), "
                "--press-threshold / --min-presses",
                "snap detector: --snap-ratio / --snap-floor (SDK profile only)",
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


def _build_pat_hook(args: argparse.Namespace, transport: object, queue) -> PatHook | None:
    """A :class:`PatHook` bound to the loop's queue, or ``None`` when pat is off.

    Pat detection is only meaningful on the SDK transport (``head_pose`` is an
    SDK-only read-back) and is on by default; ``--no-pat`` (``args.pat`` False)
    suppresses it, as does a transport that cannot read the head pose back. The
    hook reads the head pose back each tick *inside* the loop that owns the single
    SDK client, so the read-backs are fast enough to detect a pat — a separate
    ``pat`` process would be throttled by SDK contention.
    """
    if not getattr(args, "pat", True):
        return None
    if not hasattr(transport, "head_pose"):
        return None
    kw: dict[str, float] = {}
    if getattr(args, "press_threshold", None) is not None:
        kw["press_threshold"] = args.press_threshold
    if getattr(args, "min_presses", None) is not None:
        kw["min_presses"] = args.min_presses
    detector = PatDetector(**kw) if kw else None
    return PatHook(queue, detector=detector)


def _run_sdk_loop(
    transport: object,
    producer: ListenProducer,
    args: argparse.Namespace,
    on_action: Callable[[object], None],
) -> int:
    """Drive the loop over an open SDK media session (real DoA + mic-audio loudness).

    The loop also folds in proprioceptive head-pat detection (unless ``--no-pat``):
    a :class:`~reachy.motion.listen_pat.PatHook` runs once per tick via the
    executor's ``on_tick`` seam, reading the head pose back through the *same* SDK
    client the loop owns. On a detected pat it enqueues a lean→nuzzle→settle
    gesture onto the loop's queue and raises the ``pat_active`` flag (so the idle
    wander yields) — so ``listen`` reacts to both sound and touch at once.
    """
    snap_kwargs: dict[str, float] = {}
    if getattr(args, "snap_ratio", None) is not None:
        snap_kwargs["ratio"] = args.snap_ratio
    if getattr(args, "snap_floor", None) is not None:
        snap_kwargs["min_rms"] = args.snap_floor
    queue = MotionQueue()
    pat_hook = _build_pat_hook(args, transport, queue)
    with transport.media_session() as session:  # type: ignore[attr-defined]
        poller = DoaPoller(read=lambda: read_doa(session, timeout=DOA_TIMEOUT))
        detector = SnapDetector(**snap_kwargs)

        def _audio(_t: float) -> tuple[bool, bool | None]:
            sample = session.get_audio_sample()
            if sample is None:
                return (False, None)
            rms = float(np.sqrt(np.mean(sample**2)))
            return (detector.feed(sample), rms > detector.min_rms)

        try:
            return run_loop(
                transport,
                producer,
                hooks=LoopHooks(sense=poller, audio=_audio, on_action=on_action, on_tick=pat_hook),
                queue=queue,
                max_ticks=args.max_ticks,
            )
        finally:
            if pat_hook is not None:
                pat_hook.close()


def _run_http_loop(
    transport: object,
    producer: ListenProducer,
    args: argparse.Namespace,
    on_action: Callable[[object], None],
) -> int:
    """Drive the loop over the HTTP transport's DoA (no audio source / loudness)."""
    poller = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))
    return run_loop(
        transport,
        producer,
        hooks=LoopHooks(sense=poller, on_action=on_action),
        max_ticks=args.max_ticks,
    )


def cmd_listen_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    params = _params_from_args(args)
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

    # SDK profile streams real DoA + mic loudness through a media session; the HTTP/remote
    # profile polls transport.doa() with no audio source.
    if hasattr(transport, "media_session"):
        ticks = _run_sdk_loop(transport, producer, args, _on_action)
    else:
        ticks = _run_http_loop(transport, producer, args, _on_action)

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
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(run)
    _add_pat_args(run)
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
    start.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(start)
    _add_pat_args(start)
    start.set_defaults(func=cmd_listen_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads tuning).")
    add_robot_args(restart)
    restart.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_tuning_args(restart)
    _add_pat_args(restart)
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
