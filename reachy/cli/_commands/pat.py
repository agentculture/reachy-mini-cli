"""``reachy-mini-cli pat`` — feel a head pat and lean into it.

A proprioceptive reactive loop, the same shape as the ``listen`` / ``think``
nouns: a foreground loop eases the head to a baseline pose once (through the
serial motion executor) and then reads the *actual* head pose back from the robot
via :meth:`~reachy.robot.transport.Transport.head_pose`, feeding the
commanded-vs-actual deviation to a
:class:`~reachy.motion.pat.PatDetector`. When the detector recognises a pat
(``"scratch"`` head-press or ``"side_pat"`` side-nudge) it fires an event, and
:class:`~reachy.motion.pat_reaction.PatReaction` enqueues a calm lean→nuzzle→settle
gesture onto the shared serial :class:`~reachy.motion.queue.MotionQueue`. That
queue is drained to the robot one move at a time by the motion executor
(:func:`reachy.motion.server.run`) on a background thread — exactly as ``listen``
and ``think`` do.

Three verbs:

* **run** — the foreground proprioceptive loop (SDK-first by default);
* **demo** — synthesize pat events on a timer with NO robot and run them through
  :class:`PatReaction`, emitting the enqueued actions as a structured reaction
  event (for verifying the lean wiring without hardware or the ``[sdk]`` extra);
* **overview** — describe the noun (rubric-required).

Transport: SDK-first by default (``head_pose`` is an SDK-only read-back); the
``http`` transport is available via ``--transport http`` / ``REACHY_TRANSPORT=http``
but raises a clean "not supported on this transport" error for ``head_pose``.
Running the ``sdk`` path with the ``[sdk]`` extra absent raises a clean exit-2
:class:`~reachy.cli._errors.CliError` pointing at the extra — never a traceback.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from typing import Callable

from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.looputil import install_stop_handlers, interruptible_sleep, restore_stop_handlers
from reachy.motion import pat_signal
from reachy.motion.pat import PatDetector
from reachy.motion.pat_reaction import PatReaction, reaction_duration
from reachy.motion.queue import MotionAction, MotionQueue
from reachy.motion.server import run as run_motion
from reachy.robot import add_robot_args, get_transport

_JSON_HELP = "Emit structured JSON."

#: The baseline (neutral) head pose. The loop eases the head here once at start
#: (through the serial executor), then compares the actual pose read-back against
#: this commanded pitch/yaw — a deviation (someone pressing the head) triggers a pat.
_BASELINE = {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0}

#: Seconds between proprioceptive samples in the foreground loop.
_DEFAULT_TICK = 0.05

#: The scripted pat events ``pat demo`` plays through PatReaction (no robot).
DEMO_EVENTS: list[tuple[str, str]] = [
    ("level1", "scratch"),
    ("level1", "side_pat"),
    ("level2", "scratch"),
]

_VERBS = [
    "pat run — run the proprioceptive pat-reaction loop in the foreground",
    "pat demo — synthesize pat events with no robot and show the lean reaction",
    "pat overview — this summary",
]


# --- overview -------------------------------------------------------------


def cmd_pat_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A proprioceptive reactive loop: hold a baseline head pose, read the "
                "actual pose back, and detect a pat from the commanded-vs-actual deviation.",
                "On a detected pat the robot leans into it — a calm lean→nuzzle→settle "
                "gesture (a head dip for a 'scratch', a yaw-toward for a 'side_pat').",
                "Two touch types: scratch (head pressed down) and side_pat (head nudged "
                "sideways); two intensities: level1 (light) and level2 (sustained).",
                "SDK-first by default: head_pose read-back is an SDK-only capability; "
                "use --transport http only for non-pose ops (it cannot read head_pose).",
                "Smooth by construction — leans drive the serial MotionQueue, one move at "
                "a time through the motion executor (no jerky overlap).",
                "Graceful: a transport drop degrades motion to silent without killing the "
                "loop; a missing [sdk] extra raises a clean exit-2 error.",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "SDK-first by default; head_pose requires the [sdk] extra",
                "bound a run for testing/ops with --ticks N",
                "demo needs NO robot and NO [sdk] extra (synthetic events)",
                "exit codes: 0 ok, 1 user error, 2 environment (missing [sdk]/daemon)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli pat",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- motion executor (shared with the think/listen pattern) ---------------


class _NullProducer:
    """A producer that never originates a move — the queue is filled externally.

    ``pat``'s lean gestures are submitted onto the :class:`MotionQueue` from the
    foreground loop via :meth:`PatReaction.react`. The motion executor
    (:func:`reachy.motion.server.run`) still owns *draining* that queue to the
    robot one move at a time, but originates nothing itself — so it is handed
    this no-op producer.
    """

    def update(self, *_a: object, **_kw: object) -> None:
        return None


class _MotionExecutor:
    """Background thread draining the lean queue to the robot, degrade-safe.

    Mirrors ``think``'s executor: wraps :func:`reachy.motion.server.run` on its
    own thread, draining the shared :class:`MotionQueue` (which :class:`PatReaction`
    fills) to ``transport.move_goto``. A :class:`CliError` inside the executor (the
    transport went away mid-run) is captured, **not** raised on the loop thread —
    motion degrades to silent while the pat loop keeps sensing.
    """

    def __init__(self, transport: object) -> None:
        self.transport = transport
        self.queue = MotionQueue()
        self._stop = {"flag": False}
        self._thread: threading.Thread | None = None
        self._error: list[BaseException] = []

    def _drive(self) -> None:
        try:
            run_motion(
                self.transport,
                _NullProducer(),
                queue=self.queue,
                stop=self._stop,
                max_errors=10**9,
            )
        # Degrade, never crash the pat loop: capture any transport error from this
        # background thread. The loop owns SIGINT/SIGTERM, so catch Exception (not
        # BaseException) to let KeyboardInterrupt/SystemExit propagate correctly.
        except Exception as exc:  # noqa: BLE001
            self._error.append(exc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drive, name="reachy-pat-motion", daemon=True)
        self._thread.start()

    def drain(self) -> None:
        """Flush any pending leans the executor hasn't issued yet (best effort)."""
        try:
            while True:
                action = self.queue.pop()
                if action is None:
                    return
                self.transport.move_goto(  # type: ignore[attr-defined]
                    head=action.head,
                    antennas=action.antennas,
                    body_yaw=action.body_yaw,
                    duration=action.duration,
                    interpolation=action.interpolation,
                )
        except CliError:
            return

    def stop(self) -> None:
        self._stop["flag"] = True
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.drain()


# --- run (foreground proprioceptive loop) ---------------------------------


def _detector_from_args(args: argparse.Namespace) -> PatDetector:
    """Build a :class:`PatDetector`; each unset flag keeps its built-in default."""
    kw: dict[str, float] = {}
    if getattr(args, "press_threshold", None) is not None:
        kw["press_threshold"] = args.press_threshold
    if getattr(args, "min_presses", None) is not None:
        kw["min_presses"] = args.min_presses
    return PatDetector(**kw)


def cmd_pat_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    detector = _detector_from_args(args)

    # Preflight read: validates the transport and surfaces a missing [sdk] extra
    # as a clean exit-2 CliError *before* the loop / executor thread start, never
    # a traceback (head_pose is an SDK-only capability).
    transport.head_pose()  # type: ignore[attr-defined]

    motion = _MotionExecutor(transport)
    reaction = PatReaction(queue=motion.queue)

    # All robot motion flows through the single serial executor (which drains the
    # queue one move at a time) — the loop never calls ``move_goto`` itself, so the
    # executor's non-overlap guarantee holds. Ease the head to neutral once at the
    # start by submitting one baseline move onto that same queue; thereafter the
    # loop only *reads* the pose.
    motion.queue.submit(MotionAction(label="pat baseline", head=dict(_BASELINE), duration=1.0))

    # The robot rests at the commanded baseline whenever the loop is sensing (the
    # loop pauses sensing while a reaction plays — see ``_proprioceptive_loop``), so
    # the commanded pose stays at neutral and the detector never sees the robot's
    # own deliberate lean as a press.
    commanded_pitch = _BASELINE["pitch"]
    commanded_yaw = _BASELINE["yaw"]

    if not json_mode:
        emit_diagnostic(
            f"[pat] feeling for a head pat via {getattr(transport, 'name', 'sdk')}; "
            "lean into it on a pat; Ctrl-C to stop"
        )

    max_ticks = getattr(args, "ticks", None)

    motion.start()
    try:
        ran, events = _proprioceptive_loop(
            transport=transport,
            detector=detector,
            reaction=reaction,
            commanded_pitch=commanded_pitch,
            commanded_yaw=commanded_yaw,
            max_ticks=max_ticks,
        )
    finally:
        motion.stop()

    if json_mode:
        emit_result({"status": "ok", "ticks": ran, "events": events}, json_mode=True)
    else:
        emit_diagnostic(f"[pat] stopped after {ran} tick(s), {events} pat(s)")
    return 0


def _sense_and_maybe_react(
    *,
    transport: object,
    detector: PatDetector,
    reaction: PatReaction,
    commanded_pitch: float,
    commanded_yaw: float,
    now: float,
) -> tuple[float, str] | None:
    """One sensing pass: read the actual head pose, feed the deviation to the
    detector, and on a detection enqueue the lean and open the reaction window.

    Returns ``(reacting_until, level)`` when a pat fired this tick (the caller
    raises the ``pat_active`` flag and stops sensing until ``reacting_until``),
    or ``None`` when nothing fired.
    """
    try:
        actual_pitch, actual_yaw = transport.head_pose()  # type: ignore[attr-defined]
    except CliError:
        actual_pitch, actual_yaw = commanded_pitch, commanded_yaw
    event = detector.update(commanded_pitch, actual_pitch, commanded_yaw, actual_yaw, now=now)
    if event is None:
        return None
    level, touch_type = event
    reaction.react(touch_type, level)
    # Clear the detector's accumulated press history and open the reaction
    # window: the caller holds pat_active up and stops sensing until the
    # lean→nuzzle→settle gesture has finished playing.
    detector.reset()
    pat_signal.write()
    emit_diagnostic(f"[pat] {level} {touch_type} — leaning in")
    return now + reaction_duration(level), level


def _proprioceptive_loop(
    *,
    transport: object,
    detector: PatDetector,
    reaction: PatReaction,
    commanded_pitch: float,
    commanded_yaw: float,
    max_ticks: int | None,
    clock: Callable[[], float] = time.monotonic,
) -> tuple[int, int]:
    """Run the sense→detect→react loop until ``max_ticks`` (or signalled).

    Each tick reads the actual head pose back and feeds the commanded-vs-actual
    deviation to the detector; on a detection event it enqueues a lean via
    :class:`PatReaction`. The loop itself never commands motion — the executor
    owns the robot (one move at a time), so the lean is never fought.

    **Reaction window.** A pat moves the head deliberately (the lean→nuzzle→settle
    gesture), which the detector would otherwise read as fresh presses. So on a
    pat the loop opens a window of :func:`reaction_duration` seconds during which
    it (a) holds the ``pat_active`` signal up — so a co-running ``listen`` idle
    wander yields for the *whole* reaction, not just the instant of enqueue — and
    (b) pauses its own sensing, so the robot's own motion can't self-trigger.
    ``clock`` is injectable for deterministic tests. Returns ``(ticks, events)``.
    """
    stop = {"flag": False}
    handlers = install_stop_handlers(stop)
    ticks = 0
    events = 0
    reacting_until = 0.0
    flag_up = False
    try:
        while not stop["flag"]:
            now = clock()
            # Mid-reaction: the robot is executing its own lean. Keep the
            # pat-active flag up and do NOT sense (avoid self-trigger).
            if now >= reacting_until:
                if flag_up:
                    pat_signal.clear()
                    flag_up = False
                fired = _sense_and_maybe_react(
                    transport=transport,
                    detector=detector,
                    reaction=reaction,
                    commanded_pitch=commanded_pitch,
                    commanded_yaw=commanded_yaw,
                    now=now,
                )
                if fired is not None:
                    reacting_until, _level = fired
                    flag_up = True
                    events += 1
            ticks += 1
            if max_ticks is not None and ticks >= max_ticks:
                break
            interruptible_sleep(_DEFAULT_TICK, stop, time.sleep)
    finally:
        # Never leak the flag (e.g. Ctrl-C mid-reaction): always clear on exit.
        if flag_up or pat_signal.is_active():
            pat_signal.clear()
        restore_stop_handlers(handlers)
    return ticks, events


# --- demo (synthetic pat events, NO robot) --------------------------------


def cmd_pat_demo(args: argparse.Namespace) -> int:
    """Drive a scripted sequence of pat events through PatReaction (no robot).

    Synthesizes ``(level, touch_type)`` events and runs each through
    :class:`PatReaction` against a fresh :class:`MotionQueue`, draining the
    enqueued :class:`~reachy.motion.queue.MotionAction` labels into a structured
    reaction event. Requires NO transport and NO ``[sdk]`` extra — exercises the
    lean planner end to end so a human (or CI) can verify the wiring.
    """
    json_mode = bool(getattr(args, "json", False))
    count = getattr(args, "count", None)
    events = DEMO_EVENTS if count is None else DEMO_EVENTS[: max(0, int(count))]

    reactions: list[dict[str, object]] = []
    for level, touch_type in events:
        queue = MotionQueue()
        # Mirror the live loop: signal the pat-active flag while the reaction is
        # enqueued (cleared afterward, including on error) so demo exercises the
        # same idle-suppression wiring as `run`.
        with pat_signal.pat_active():
            PatReaction(queue=queue).react(touch_type, level)
        actions = [a.label for a in queue.pending()]
        reactions.append({"touch_type": touch_type, "level": level, "actions": actions})

    if json_mode:
        emit_result({"status": "ok", "reactions": reactions}, json_mode=True)
    else:
        lines = [
            f"{r['level']} {r['touch_type']}: " + " → ".join(r["actions"])  # type: ignore[arg-type]
            for r in reactions
        ]
        emit_result("\n".join(lines), json_mode=False)
        emit_diagnostic(f"[pat demo] done — {len(reactions)} reaction(s), no robot")
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_pat_overview(args)


# --- registration ---------------------------------------------------------


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser("run", help="Run the proprioceptive pat-reaction loop.")
    add_robot_args(run)
    # pat is SDK-first (head_pose is an SDK-only read-back) — default to sdk.
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    run.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="Stop after this many loop ticks (default: run until signalled).",
    )
    run.add_argument(
        "--press-threshold",
        type=float,
        default=None,
        dest="press_threshold",
        help="Pitch deviation (deg) past which a head-press counts (default 1.2).",
    )
    run.add_argument(
        "--min-presses",
        type=int,
        default=None,
        dest="min_presses",
        help="Presses within the window needed to trigger a pat (default 2).",
    )
    run.set_defaults(func=cmd_pat_run)


def _register_demo(noun_sub: argparse._SubParsersAction) -> None:
    demo = noun_sub.add_parser(
        "demo",
        help="Synthesize pat events with no robot and show the lean reaction.",
    )
    demo.add_argument("--json", action="store_true", help=_JSON_HELP)
    demo.add_argument(
        "--count",
        type=int,
        default=None,
        help="Number of scripted pat events to play (default: the full sequence).",
    )
    demo.set_defaults(func=cmd_pat_demo)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "pat",
        help="Feel a head pat and lean into it (see 'reachy-mini-cli pat overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="pat_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the pat noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_pat_overview)

    _register_run(noun_sub)
    _register_demo(noun_sub)
