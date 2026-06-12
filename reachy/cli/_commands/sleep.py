"""``reachy-mini-cli sleep`` — drift off when undisturbed, wake on a stimulus.

A graduated-wakefulness loop, the same shape as the ``listen`` / ``think`` /
``pat`` nouns.  An idle timer (:class:`~reachy.sleep.state.SleepStateMachine`)
walks the robot ALERT → DROWSY → ASLEEP the longer it goes undisturbed; any
qualifying *stimulus* (:func:`~reachy.sleep.stimulus.is_stimulus` — speech, a
DoA shift, a loud snap, or a pat) snaps it back to ALERT.  Each wakefulness
state maps to motion through the :class:`~reachy.motion.sleep.SleepProducer`
(full-energy alive idle when ALERT, low-energy when DROWSY, a near-still
"sleep breathe" when ASLEEP), submitted onto the shared serial
:class:`~reachy.motion.queue.MotionQueue` and drained one move at a time by a
background motion executor — exactly as ``listen`` / ``think`` / ``pat`` do.

While the robot is ASLEEP the noun keeps the ``sleep_active.flag`` written (via
:mod:`reachy.motion.sleep_signal`) so other subsystems can quiet themselves; it
is cleared the moment the robot is no longer asleep, and on every exit path.

Verbs (the listen|think|pat scaffold):

* **run** — the foreground decay→sleep→wake loop (SDK-first by default);
* **start** / **stop** / **restart** — manage it as a tracked background process
  (PID + log under the state dir, via :mod:`reachy.sleep.supervisor`);
* **status** — current sleep state + idle seconds + loop process / daemon health;
* **demo** — walk the full ALERT→DROWSY→ASLEEP→wake arc against a synthetic sense
  sequence + a fake clock, with NO robot and NO ``[sdk]`` extra (observable in
  ``--json``);
* **overview** — describe the noun (rubric-required).

Transport: SDK-first by default (real DoA + mic RMS in-process via the media
session); the ``http`` transport polls the daemon's DoA route (no audio source).
A missing ``[sdk]`` extra raises a clean exit-2 :class:`~reachy.cli._errors.CliError`
pointing at the extra — never a traceback.  ``demo`` needs no transport at all.
"""

from __future__ import annotations

import argparse
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import numpy as np

from reachy.behavior.sense import DOA_TIMEOUT, EMPTY_SENSE, DoaPoller, Sense, read_doa
from reachy.cli._commands._robot import emit_payload
from reachy.cli._commands.overview import emit_overview
from reachy.cli._errors import CliError
from reachy.cli._output import emit_diagnostic, emit_result
from reachy.looputil import install_stop_handlers, interruptible_sleep, restore_stop_handlers
from reachy.motion import sleep_signal
from reachy.motion.queue import MotionQueue
from reachy.motion.server import run as run_motion
from reachy.motion.sleep import SleepProducer
from reachy.motion.snap import SnapDetector
from reachy.robot import add_robot_args, get_transport
from reachy.sleep import supervisor
from reachy.sleep.patwake import PatWakeSource
from reachy.sleep.state import SleepState, SleepStateMachine
from reachy.sleep.stimulus import is_stimulus
from reachy.sleep.wake import WakeDetector

_JSON_HELP = "Emit structured JSON."

#: Seconds between sense samples in the foreground loop.
_DEFAULT_TICK = 0.05

#: Default idle-timeout (seconds): the DROWSY→ASLEEP threshold.  The DROWSY
#: threshold is half of it, mirroring the state machine's 75/150 default ratio.
_DEFAULT_IDLE_TIMEOUT = 150.0

#: DoA deadband (radians): a DoA angle move smaller than this is not a "shift".
_DOA_DEADBAND = 0.20

_VERBS = [
    "sleep run — run the decay→sleep→wake loop in the foreground",
    "sleep start — start the loop in the background (tracked process)",
    "sleep stop — stop the loop this CLI started",
    "sleep restart — restart the background loop (re-reads flags + code)",
    "sleep status — current sleep state + idle seconds + process state",
    "sleep demo — walk ALERT→DROWSY→ASLEEP→wake with no robot (synthetic)",
    "sleep overview — this summary",
]


# --- overview -------------------------------------------------------------


def cmd_sleep_overview(args: argparse.Namespace) -> int:
    sections: list[dict[str, object]] = [
        {
            "title": "What",
            "items": [
                "A graduated-wakefulness loop: an idle timer walks the robot "
                "ALERT → DROWSY → ASLEEP the longer it goes undisturbed.",
                "Any stimulus — detected speech, a DoA shift, a loud snap, or a pat — "
                "snaps it back to ALERT with a single re-engagement gesture.",
                "Quiet-room / audio-off deployment: --no-audio-wake (alias --wake pat) makes "
                "it pat-only — speech/DoA/snap are ignored and only a head pat wakes it "
                "(needs the sdk head-pose read-back; not available on --transport http).",
                "Each state maps to motion: full alive idle (ALERT), low-energy idle "
                "(DROWSY), a near-still sleep-breathe (ASLEEP).",
                "While ASLEEP it writes the sleep-active flag so other subsystems can "
                "quiet themselves; the flag is cleared the moment it wakes.",
                "SDK-first by default: real DoA + mic loudness in-process; "
                "use --transport http to poll the daemon's DoA route instead.",
                "Graceful: a transport drop degrades motion to silent without killing "
                "the loop; a missing [sdk] extra raises a clean exit-2 error.",
            ],
        },
        {"title": "Verbs", "items": list(_VERBS)},
        {
            "title": "State",
            "items": [
                f"pid file: {supervisor.pid_file()}",
                f"log file: {supervisor.log_file()}",
                f"sleep flag: {sleep_signal.sleep_flag_path()}",
            ],
        },
        {
            "title": "Conventions",
            "items": [
                "every command supports --json",
                "SDK-first by default; the sdk sense feed requires the [sdk] extra",
                "wake: --no-audio-wake / --wake pat → pat-only (quiet-room / audio-off); "
                "default --wake audio keeps sound wake; --wake-word adds phrase wake",
                "pacing: --idle-timeout (seconds of quiet before sleep)",
                "bound a run for testing/ops with --ticks N",
                "demo needs NO robot and NO [sdk] extra (synthetic sense + fake clock)",
                "exit codes: 0 ok, 1 user error, 2 environment (missing [sdk]/daemon)",
            ],
        },
    ]
    emit_overview(
        "reachy-mini-cli sleep",
        sections,
        json_mode=bool(getattr(args, "json", False)),
    )
    return 0


# --- motion executor (shared with the listen/think/pat pattern) -----------


class _NullProducer:
    """A producer that never originates a move — the queue is filled externally."""

    def update(self, *_a: object, **_kw: object) -> None:
        return None


class _MotionExecutor:
    """Background thread draining the sleep queue to the robot, degrade-safe.

    Mirrors ``pat`` / ``think``'s executor: wraps :func:`reachy.motion.server.run`
    on its own thread, draining the shared :class:`MotionQueue` (which the
    :class:`SleepProducer` fills) to ``transport.move_goto``.  A
    :class:`CliError` inside the executor (the transport went away mid-run) is
    captured, **not** raised on the loop thread — motion degrades to silent while
    the sleep loop keeps sensing.
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
        except Exception as exc:  # noqa: BLE001
            self._error.append(exc)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._drive, name="reachy-sleep-motion", daemon=True)
        self._thread.start()

    def drain(self) -> None:
        """Flush any pending moves the executor hasn't issued yet (best effort)."""
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


# --- the pure arc driver (shared by run + demo + the arc unit test) -------


def _call_bool(fn: Callable[[], bool] | None) -> bool:
    """Invoke an optional bool source, defaulting to ``False`` when absent."""
    return bool(fn()) if fn is not None else False


def _call_float(fn: Callable[[], float] | None) -> float:
    """Invoke an optional float source, defaulting to ``0.0`` when absent."""
    return float(fn()) if fn is not None else 0.0


def _doa_shifted(curr: float | None, prev: float | None) -> bool:
    """True when the DoA angle moved past the deadband since the last reading."""
    return curr is not None and prev is not None and abs(curr - prev) > _DOA_DEADBAND


def _commanded_head_pose(producer: SleepProducer) -> tuple[float, float]:
    """Best-effort read of the producer's CURRENT commanded head ``(pitch, yaw)``.

    The :class:`SleepProducer` submits its sleep-breathe / idle pose onto the
    shared queue under :data:`~reachy.motion.sleep.SLEEP_COALESCE_KEY` (and idle
    moves under ``IDLE_KEY``); the latest pending action with a ``head`` dict is
    the pose the robot was just told to hold this tick.  Inspect the queue's
    public snapshot — newest-last — and return its head pitch/yaw, defaulting to
    neutral ``(0.0, 0.0)`` when no head pose is pending yet.
    """
    for action in reversed(producer.queue.pending()):
        head = action.head
        if head is not None:
            return float(head.get("pitch", 0.0)), float(head.get("yaw", 0.0))
    return 0.0, 0.0


def _advance(
    machine: SleepStateMachine,
    producer: SleepProducer,
    wake_detector: WakeDetector,
    t: float,
    *,
    stimulated: bool,
) -> bool:
    """Advance the FSM + producer for one tick; return ``True`` if a wake fired.

    The acoustic wake-word leg is consulted in :func:`run_sleep_arc` before this
    call; here ``wake_detector`` is only reset on a wake so its snap history does
    not carry stale state across a re-engagement.
    """
    if stimulated:
        machine.reset(now=t)
        producer.wake()
        wake_detector.reset()
        woke = True
    else:
        machine.update(now=t)
        woke = False
    producer.state = machine.state
    producer.update(t)
    return woke


def _sync_sleep_flag(*, asleep: bool, flag_up: bool) -> bool:
    """Write/clear the ASLEEP flag so it matches state; return the new flag_up."""
    if asleep and not flag_up:
        sleep_signal.write()
        return True
    if flag_up and not asleep:
        sleep_signal.clear()
        return False
    return flag_up


def _default_wake_detector() -> WakeDetector:
    """The arc's default wake-word detector: Tier-2 wake-word disabled.

    Tier-1 (speech flag / snap) is already handled by ``is_stimulus``; this
    detector only adds the optional wake-*word* leg, which is off by default.
    Callers wire a wake-word-enabled detector via ``wake_detector_factory``.
    """
    return WakeDetector(wake_word_enabled=False)


#: A one-sample silent buffer fed to the wake-word backend each tick (the real
#: mic audio is the snap detector's concern; the wake-word leg only needs a
#: non-None array here).
_SILENT_AUDIO = np.zeros(1, dtype=np.float32)


@dataclass(frozen=True)
class WakeWord:
    """Tier-2 wake-WORD wiring for the arc: the detector factory + its audio feed.

    Bundled because both pieces serve *only* the wake-word leg — ``audio`` is
    consumed nowhere else — and to keep :func:`run_sleep_arc`'s parameter list
    within bounds. Both default to ``None`` (Tier-2 off; a silent buffer feeds a
    null detector).
    """

    factory: Callable[[], WakeDetector] | None = None
    audio: Callable[[], np.ndarray] | None = None


def _process_tick(
    *,
    t: float,
    snapshot: Sense,
    prev_doa: float | None,
    machine: SleepStateMachine,
    producer: SleepProducer,
    wake_detector: WakeDetector,
    snap: Callable[[], bool] | None,
    audio: Callable[[], np.ndarray] | None,
    pat: Callable[[], bool] | None,
    mute_until: Callable[[], float] | None,
    audio_wake: bool,
    commanded_pose_sink: Callable[[tuple[float, float]], None] | None,
    flag_up: bool,
) -> tuple[float | None, bool, bool]:
    """Run one arc tick. Returns ``(new_prev_doa, woke_this_tick, new_flag_up)``."""
    doa_shift = _doa_shifted(snapshot.doa_angle, prev_doa)
    new_prev = snapshot.doa_angle if snapshot.doa_angle is not None else prev_doa

    stimulated = is_stimulus(
        snapshot,
        doa_shift=doa_shift,
        snap=_call_bool(snap),
        pat=_call_bool(pat),
        now=t,
        mute_until=_call_float(mute_until),
        audio_wake=audio_wake,
    )
    # Tier-2 wake-WORD: only when audio is on (pat-only never listens).  Feed the
    # real mic chunk (the sdk feed's latest sample); a silent buffer when absent.
    if audio_wake and not stimulated:
        chunk = audio() if audio is not None else _SILENT_AUDIO
        stimulated = wake_detector.update(snapshot, chunk)

    woke = _advance(machine, producer, wake_detector, t, stimulated=stimulated)
    # Publish the commanded sleep pose so a pat-wake source can compare the
    # read-back against the MOVING target (not a static baseline).
    if commanded_pose_sink is not None:
        commanded_pose_sink(_commanded_head_pose(producer))
    new_flag_up = _sync_sleep_flag(asleep=machine.state is SleepState.ASLEEP, flag_up=flag_up)
    return new_prev, woke, new_flag_up


def run_sleep_arc(
    *,
    queue: MotionQueue,
    now: Callable[[], float],
    sense: Callable[[], Sense],
    on_tick: Callable[[], None] | None = None,
    ticks: int,
    idle_timeout: float,
    snap: Callable[[], bool] | None = None,
    pat: Callable[[], bool] | None = None,
    mute_until: Callable[[], float] | None = None,
    audio_wake: bool = True,
    wake_word: WakeWord | None = None,
    commanded_pose_sink: Callable[[tuple[float, float]], None] | None = None,
    stop: dict | None = None,
) -> dict[str, object]:
    """Drive the decay→sleep→wake state arc for ``ticks`` iterations.

    Pure of any robot motion *transport* — it only submits actions onto ``queue``
    (the caller owns draining it).  ``now`` and ``sense`` are injected so the arc
    is fully deterministic in tests (a fake clock + a synthetic sense sequence,
    zero wall-clock wait, zero robot).

    Each tick: read the sense, compute :func:`~reachy.sleep.stimulus.is_stimulus`;
    on a stimulus reset the state machine + call ``producer.wake()`` + reset the
    wake detector; otherwise advance the machine.  Mirror the machine's state to
    ``producer.state`` and call ``producer.update(...)`` to enqueue motion.  While
    ASLEEP keep the ``sleep_active.flag`` written; clear it otherwise.

    ``audio_wake`` gates the acoustic wake paths.  When ``True`` (default) the
    three acoustic stimuli (DoA shift, speech flag, snap) all qualify *and* a
    wake-*word* backend (from ``wake_detector_factory``) is consulted on the
    audio each tick — a detected phrase also wakes it.  When ``False`` the loop is
    **pat-only**: ``is_stimulus(audio_wake=False)`` ignores all acoustic cues and
    the wake-word backend is never consulted, so only an injected ``pat`` wakes it
    — the quiet-room / mic-off deployment.

    ``commanded_pose_sink``, when supplied, is called once per tick with the
    producer's current commanded head ``(pitch_deg, yaw_deg)`` *after*
    ``producer.update`` — so a pat-wake source can compare the read-back against
    the MOVING sleep-breathe target.

    Returns ``{"states": [...], "woke": bool, "idle_seconds": float}`` — the
    observed :class:`SleepState` name per tick, whether a wake fired, and the
    final idle seconds.
    """
    # asleep_after == idle_timeout; drowsy_after is half of it (75/150 ratio).
    machine = SleepStateMachine(drowsy_after=idle_timeout / 2.0, asleep_after=idle_timeout)
    producer = SleepProducer(queue=queue, state=SleepState.ALERT)
    ww = wake_word if wake_word is not None else WakeWord()
    factory = ww.factory if ww.factory is not None else _default_wake_detector
    wake_detector = factory()

    states: list[str] = []
    woke = False
    flag_up = False
    prev_doa: float | None = None

    try:
        for _ in range(ticks):
            if stop is not None and stop.get("flag"):
                break
            t = now()
            snapshot = sense()
            prev_doa, tick_woke, flag_up = _process_tick(
                t=t,
                snapshot=snapshot,
                prev_doa=prev_doa,
                machine=machine,
                producer=producer,
                wake_detector=wake_detector,
                snap=snap,
                audio=ww.audio,
                pat=pat,
                mute_until=mute_until,
                audio_wake=audio_wake,
                commanded_pose_sink=commanded_pose_sink,
                flag_up=flag_up,
            )
            woke = woke or tick_woke
            states.append(machine.state.name)
            if on_tick is not None:
                on_tick()
    finally:
        if flag_up or sleep_signal.is_active():
            sleep_signal.clear()

    return {"states": states, "woke": woke, "idle_seconds": machine.idle_seconds}


# --- sense feed (DoA / RMS / snap → Sense) --------------------------------


def _make_sdk_feed(
    transport: object,
) -> tuple[Callable[[], Sense], Callable[[], bool], Callable[[], np.ndarray], Callable[[], None]]:
    """Open the SDK media session and return ``(sense, snap, audio, close)``.

    The session is entered eagerly so a missing ``[sdk]`` / dead daemon raises its
    clean CliError before the loop starts.  ``sense()`` returns the latest DoA
    snapshot; ``snap()`` returns whether the latest mic chunk was a loud transient;
    ``audio()`` returns that same latest mic chunk (so the wake-WORD backend sees
    real audio, not silence).
    """
    session = transport.media_session()  # type: ignore[attr-defined]
    cm = session if (hasattr(session, "__enter__") and hasattr(session, "__exit__")) else None
    if cm is not None:
        session = cm.__enter__()
    poller = DoaPoller(read=lambda: read_doa(session, timeout=DOA_TIMEOUT))
    detector = SnapDetector()
    last_snap = {"v": False}
    last_audio: dict[str, np.ndarray] = {"v": _SILENT_AUDIO}

    def _sense() -> Sense:
        snapshot = poller()
        sample = session.get_audio_sample()
        if sample is not None:
            last_snap["v"] = detector.feed(sample)
            last_audio["v"] = np.asarray(sample, dtype=np.float32)
        else:
            last_snap["v"] = False
            last_audio["v"] = _SILENT_AUDIO
        return snapshot

    def _snap() -> bool:
        return last_snap["v"]

    def _audio() -> np.ndarray:
        return last_audio["v"]

    def _close() -> None:
        if cm is not None:
            cm.__exit__(None, None, None)

    return _sense, _snap, _audio, _close


def _make_http_feed(
    transport: object,
) -> tuple[Callable[[], Sense], Callable[[], bool], Callable[[], np.ndarray], Callable[[], None]]:
    """Poll the daemon's DoA route — no audio source, so ``snap`` is always False
    and ``audio`` is always a silent buffer (no mic on the http transport)."""
    poller = DoaPoller(read=lambda: read_doa(transport, timeout=DOA_TIMEOUT))

    def _sense() -> Sense:
        return poller()

    def _snap() -> bool:
        return False

    def _audio() -> np.ndarray:
        return _SILENT_AUDIO

    def _close() -> None:
        return None

    return _sense, _snap, _audio, _close


# --- run (foreground loop) ------------------------------------------------


def _resolve_idle_timeout(args: argparse.Namespace) -> float:
    value = getattr(args, "idle_timeout", None)
    return _DEFAULT_IDLE_TIMEOUT if value is None else max(0.001, float(value))


def _resolve_audio_wake(args: argparse.Namespace) -> bool:
    """Fold ``--no-audio-wake`` / ``--wake {audio,pat}`` into a single bool.

    ``--no-audio-wake`` and ``--wake pat`` mean the same thing (pat-only); either
    yields ``False``.  ``--wake audio`` (the default) yields ``True``.  When both
    forms are supplied they must agree on "audio off" — ``--no-audio-wake`` is the
    floor, so any audio-off request wins.
    """
    no_audio = bool(getattr(args, "no_audio_wake", False))
    wake = getattr(args, "wake", None)  # None | "audio" | "pat"
    if wake == "pat":
        no_audio = True
    return not no_audio


def _make_wake_detector_factory(args: argparse.Namespace) -> Callable[[], WakeDetector]:
    """Build the arc's wake-WORD detector factory from the run flags.

    Tier-2 wake-word is opt-in via ``--wake-word``; the resolved backend kind
    (``http`` STT default, or ``openwakeword`` on the ``[cpu]``/``[gpu]`` extra)
    and an optional phrase override are threaded to :class:`WakeDetector`, which
    resolves the t2 backend.  Disabled by default → a null backend that never
    fires (Tier-1 speech/snap still works via ``is_stimulus``).
    """
    enabled = bool(getattr(args, "wake_word", False))
    kind = getattr(args, "wake_word_kind", None) or "http"
    phrase = getattr(args, "wake_phrase", None)

    def _factory() -> WakeDetector:
        kw: dict[str, object] = {"wake_word_enabled": enabled, "wake_word_kind": kind}
        if phrase:
            kw["phrase"] = phrase
        return WakeDetector(**kw)  # type: ignore[arg-type]

    return _factory


def _require_pat_capable(transport: object) -> None:
    """Guard: pat-only wake needs the SDK ``head_pose`` read-back.

    The ``http`` transport (no ``media_session``) cannot read the head pose back,
    so a pat-only request there can never detect a touch.  Raise a clean exit-2
    :class:`CliError` (two lines, no traceback) pointing at the SDK requirement.
    """
    if not hasattr(transport, "media_session"):
        raise CliError(
            code=2,
            message="pat-only wake (--no-audio-wake / --wake pat) needs the SDK head-pose "
            "read-back, which the http transport cannot provide",
            remediation="run on the sdk transport (the default): drop --transport http, "
            "and install the extra with: pip install 'reachy-mini-cli[sdk]'",
        )


def _build_pat_wake(
    transport: object,
) -> tuple[Callable[[], bool], Callable[[tuple[float, float]], None]]:
    """Wire a :class:`PatWakeSource` against the MOVING commanded sleep pose.

    Returns ``(pat, commanded_pose_sink)``:

    * ``commanded_pose_sink`` — passed to :func:`run_sleep_arc`; called each tick
      with the producer's current commanded head ``(pitch, yaw)`` and stashed.
    * ``pat`` — polled by the arc; the :class:`PatWakeSource` reads the actual head
      pose via ``transport.head_pose`` and compares it to the stashed commanded
      pose, so the robot's own sleep-breathe motion is not read as a press.
    """
    commanded: dict[str, tuple[float, float]] = {"pose": (0.0, 0.0)}

    def _sink(pose: tuple[float, float]) -> None:
        commanded["pose"] = pose

    source = PatWakeSource(
        read_head_pose=transport.head_pose,  # type: ignore[attr-defined]
        commanded_pose=lambda: commanded["pose"],
    )

    def _pat() -> bool:
        try:
            return source.poll()
        except CliError:
            # head_pose unavailable mid-run (transport dropped) → no pat this tick.
            return False

    return _pat, _sink


def cmd_sleep_run(args: argparse.Namespace) -> int:
    json_mode = bool(getattr(args, "json", False))
    transport = get_transport(args)
    idle_timeout = _resolve_idle_timeout(args)
    max_ticks = getattr(args, "ticks", None)
    audio_wake = _resolve_audio_wake(args)

    # Pat-only wake needs the SDK head-pose read-back: reject the http transport
    # up front with a clean exit-2 CliError (never a traceback) before any session.
    if not audio_wake:
        _require_pat_capable(transport)

    # Build the sense feed for the selected transport.  The sdk feed opens the
    # media session eagerly, so a missing [sdk] extra raises a clean exit-2
    # CliError here — never a traceback.
    if hasattr(transport, "media_session"):
        sense, snap, audio, close = _make_sdk_feed(transport)
    else:
        sense, snap, audio, close = _make_http_feed(transport)

    # Pat-only mode wires a PatWakeSource off the SDK head-pose read-back; audio
    # mode leaves it absent (None → the arc's pat source defaults to "no pat").
    pat, pose_sink = _build_pat_wake(transport) if not audio_wake else (None, None)

    motion = _MotionExecutor(transport)

    if not json_mode:
        emit_diagnostic(_run_banner(transport, idle_timeout=idle_timeout, audio_wake=audio_wake))

    stop = {"flag": False}
    handlers = install_stop_handlers(stop)
    motion.start()
    try:
        result = _run_foreground(
            queue=motion.queue,
            sense=sense,
            snap=snap,
            audio=audio,
            idle_timeout=idle_timeout,
            max_ticks=max_ticks,
            stop=stop,
            audio_wake=audio_wake,
            wake_detector_factory=_make_wake_detector_factory(args),
            pat=pat,
            commanded_pose_sink=pose_sink,
        )
    finally:
        motion.stop()
        close()
        restore_stop_handlers(handlers)

    ran = len(result["states"])  # type: ignore[arg-type]
    if json_mode:
        emit_result(
            {
                "status": "ok",
                "ticks": ran,
                "final_state": result["states"][-1] if result["states"] else "ALERT",
                "woke": result["woke"],
            },
            json_mode=True,
        )
    else:
        emit_diagnostic(f"[sleep] stopped after {ran} tick(s)")
    return 0


def _run_banner(transport: object, *, idle_timeout: float, audio_wake: bool) -> str:
    """The one-line text diagnostic shown when a foreground run starts."""
    wake_by = "a sound or touch wakes it" if audio_wake else "pat-only (mic off); a touch wakes it"
    return (
        f"[sleep] drifting off when undisturbed via {getattr(transport, 'name', 'sdk')}; "
        f"idle-timeout={idle_timeout:g}s; {wake_by}; Ctrl-C to stop"
    )


def _run_foreground(
    *,
    queue: MotionQueue,
    sense: Callable[[], Sense],
    snap: Callable[[], bool],
    audio: Callable[[], np.ndarray] | None = None,
    idle_timeout: float,
    max_ticks: int | None,
    stop: dict,
    audio_wake: bool = True,
    wake_detector_factory: Callable[[], WakeDetector] | None = None,
    pat: Callable[[], bool] | None = None,
    commanded_pose_sink: Callable[[tuple[float, float]], None] | None = None,
) -> dict[str, object]:
    """The live foreground loop: an unbounded (or ``max_ticks``-bounded) arc.

    Mute window: a placeholder ``{"until": 0.0}`` is honored so the noun is
    structurally ready for self-mute parity with ``think`` — the robot's own
    sleep-breathe makes no sound today, so nothing stamps it yet, but the seam
    exists.  Reuses :func:`run_sleep_arc` with a real monotonic clock and a real
    sleep between ticks; an unbounded run loops until ``stop['flag']``.

    ``audio_wake`` gates the acoustic wake paths (see :func:`run_sleep_arc`); in
    pat-only mode the caller supplies a ``pat`` source + ``commanded_pose_sink``
    so the pat detector compares the read-back against the moving sleep pose.
    """
    mute = {"until": 0.0}
    ticks = max_ticks if max_ticks is not None else 10**12

    def _between_ticks() -> None:
        interruptible_sleep(_DEFAULT_TICK, stop, time.sleep)

    return run_sleep_arc(
        queue=queue,
        now=time.monotonic,
        sense=sense,
        snap=snap,
        pat=pat,
        mute_until=lambda: mute["until"],
        audio_wake=audio_wake,
        wake_word=WakeWord(factory=wake_detector_factory, audio=audio),
        commanded_pose_sink=commanded_pose_sink,
        on_tick=_between_ticks,
        ticks=ticks,
        idle_timeout=idle_timeout,
        stop=stop,
    )


# --- start / stop / restart / status --------------------------------------


def cmd_sleep_start(args: argparse.Namespace) -> int:
    data = supervisor.start(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        idle_timeout=getattr(args, "idle_timeout", None),
        no_audio_wake=getattr(args, "no_audio_wake", False),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_sleep_stop(args: argparse.Namespace) -> int:
    data = supervisor.stop(timeout=args.timeout)
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_sleep_restart(args: argparse.Namespace) -> int:
    data = supervisor.restart(
        transport=args.transport,
        base_url=args.base_url,
        timeout=args.timeout,
        idle_timeout=getattr(args, "idle_timeout", None),
        no_audio_wake=getattr(args, "no_audio_wake", False),
    )
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def cmd_sleep_status(args: argparse.Namespace) -> int:
    """Report the loop's process state plus the cross-process ASLEEP signal.

    The live state machine (and its idle timer) lives inside the running loop's
    *own* process; the only thing observable from here is the persisted
    ``sleep_active.flag`` it raises while ASLEEP. So this verb reports the
    process health (running / stale / not-running) and ASLEEP-vs-ALERT from the
    flag. ``idle_seconds`` is reported as ``null`` because the live timer is not
    readable across processes — drive ``sleep demo`` (or read the running loop's
    own ``--json`` output) to observe the full alert->drowsy->asleep arc.
    """
    data = supervisor.status()
    asleep = sleep_signal.is_active()
    data["state"] = SleepState.ASLEEP.name if asleep else SleepState.ALERT.name
    # The idle timer is owned by the loop process; it is not observable here.
    data["idle_seconds"] = None
    emit_payload(data, json_mode=bool(getattr(args, "json", False)))
    return 0


def _no_verb(args: argparse.Namespace) -> int:
    return cmd_sleep_overview(args)


# --- demo (synthetic sense + fake clock, NO robot) ------------------------

#: The scripted idle gap (seconds) the demo's fake clock jumps each tick — big
#: enough that a small ``--idle-timeout`` walks the machine through every level.
_DEMO_TICK_SECONDS = 10.0

#: How many idle ticks the demo runs before injecting the wake stimulus.
_DEMO_IDLE_TICKS = 5


def cmd_sleep_demo(args: argparse.Namespace) -> int:
    """Walk the full ALERT→DROWSY→ASLEEP→wake arc with no robot, no [sdk] extra.

    Drives :func:`run_sleep_arc` against a synthetic sense sequence (silence,
    then a final speech stimulus) and a fake clock that jumps
    :data:`_DEMO_TICK_SECONDS` per tick, so a tiny idle-timeout walks the machine
    through every wakefulness level and then wakes.  Requires NO transport — the
    arc only fills a throwaway :class:`MotionQueue`.
    """
    json_mode = bool(getattr(args, "json", False))
    from reachy.motion.queue import MotionQueue as _MQ

    clock = {"t": 0.0}

    def _now() -> float:
        return clock["t"]

    # Silence for the idle ticks, then a speech stimulus to wake.
    feed = {"i": 0}
    total = _DEMO_IDLE_TICKS + 1

    def _sense() -> Sense:
        is_last = feed["i"] >= _DEMO_IDLE_TICKS
        return Sense(speech_detected=True) if is_last else EMPTY_SENSE

    def _advance() -> None:
        feed["i"] += 1
        clock["t"] += _DEMO_TICK_SECONDS

    # idle-timeout small enough that ASLEEP is reached within the idle ticks.
    idle_timeout = getattr(args, "idle_timeout", None)
    if idle_timeout is None:
        idle_timeout = _DEMO_TICK_SECONDS * 2.5  # asleep by ~tick 3

    result = run_sleep_arc(
        queue=_MQ(),
        now=_now,
        sense=_sense,
        on_tick=_advance,
        ticks=total,
        idle_timeout=float(idle_timeout),
    )

    if json_mode:
        emit_result(
            {"status": "ok", "states": result["states"], "woke": result["woke"]},
            json_mode=True,
        )
    else:
        emit_result(" → ".join(result["states"]), json_mode=False)  # type: ignore[arg-type]
        emit_diagnostic(
            f"[sleep demo] done — walked {len(result['states'])} tick(s), "
            f"woke={result['woke']}, no robot"
        )
    return 0


# --- registration ---------------------------------------------------------


def _add_idle_timeout(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--idle-timeout",
        type=float,
        default=None,
        dest="idle_timeout",
        help="Seconds of quiet before the robot falls asleep "
        f"(default {_DEFAULT_IDLE_TIMEOUT:g}; the drowsy threshold is half of it).",
    )


def _add_wake_flags(parser: argparse.ArgumentParser) -> None:
    """Add the audio-wake toggle + optional wake-word backend flags.

    ``--no-audio-wake`` and ``--wake pat`` are aliases meaning the same thing:
    pat-only / quiet-room — the mic is off, so speech/snap/DoA are ignored and
    only a physical head pat wakes it (needs the SDK head-pose read-back).
    ``--wake audio`` (the default) keeps the acoustic wake paths on.
    """
    parser.add_argument(
        "--no-audio-wake",
        action="store_true",
        dest="no_audio_wake",
        help="Pat-only / quiet-room mode (audio-off): ignore speech, DoA shifts and "
        "snaps — only a physical head pat wakes it (needs the sdk head-pose read-back). "
        "Same as --wake pat.",
    )
    parser.add_argument(
        "--wake",
        choices=("audio", "pat"),
        default=None,
        dest="wake",
        help="Which stimulus wakes the robot: 'audio' (default — sound or touch) or "
        "'pat' (quiet-room / audio-off, touch only; alias for --no-audio-wake).",
    )
    parser.add_argument(
        "--wake-word",
        action="store_true",
        dest="wake_word",
        help="Also wake on a spoken wake-WORD phrase (Tier-2; audio mode only). "
        "Resolves the STT backend; off by default.",
    )
    parser.add_argument(
        "--wake-word-kind",
        choices=("http", "openwakeword"),
        default=None,
        dest="wake_word_kind",
        help="Wake-word backend when --wake-word is set: 'http' STT (default) or "
        "'openwakeword' (the [cpu]/[gpu] extra).",
    )
    parser.add_argument(
        "--wake-phrase",
        default=None,
        dest="wake_phrase",
        help="Wake-word phrase override (default 'hey reachy' / REACHY_STT_PHRASE).",
    )


def _register_run(noun_sub: argparse._SubParsersAction) -> None:
    run = noun_sub.add_parser(
        "run",
        help="Run the decay→sleep→wake loop in the foreground "
        "(--no-audio-wake / --wake pat for the quiet-room / audio-off deployment).",
    )
    add_robot_args(run)
    # sleep is SDK-first (real DoA + mic RMS) — default to sdk.
    run.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_idle_timeout(run)
    _add_wake_flags(run)
    run.add_argument(
        "--ticks",
        type=int,
        default=None,
        help="Stop after this many loop ticks (default: run until signalled).",
    )
    run.set_defaults(func=cmd_sleep_run)


def _register_process_verbs(noun_sub: argparse._SubParsersAction) -> None:
    start = noun_sub.add_parser("start", help="Start the sleep loop in the background.")
    add_robot_args(start)
    start.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_idle_timeout(start)
    start.add_argument(
        "--no-audio-wake",
        action="store_true",
        dest="no_audio_wake",
        help="Pat-only / quiet-room mode: ignore speech, DoA shifts and snaps — "
        "only a physical head pat wakes the robot. Forwarded to the background "
        "sleep run process. Same as --wake pat on the run verb.",
    )
    start.set_defaults(func=cmd_sleep_start)

    restart = noun_sub.add_parser("restart", help="Restart the background loop (re-reads flags).")
    add_robot_args(restart)
    restart.set_defaults(transport=os.environ.get("REACHY_TRANSPORT", "sdk"))
    _add_idle_timeout(restart)
    restart.add_argument(
        "--no-audio-wake",
        action="store_true",
        dest="no_audio_wake",
        help="Pat-only / quiet-room mode: ignore speech, DoA shifts and snaps — "
        "only a physical head pat wakes the robot. Forwarded to the background "
        "sleep run process. Same as --wake pat on the run verb.",
    )
    restart.set_defaults(func=cmd_sleep_restart)

    stop = noun_sub.add_parser("stop", help="Stop the loop this CLI started.")
    stop.add_argument("--json", action="store_true", help=_JSON_HELP)
    stop.add_argument(
        "--timeout",
        type=float,
        default=supervisor.DEFAULT_STOP_TIMEOUT,
        help="Seconds to wait after SIGTERM before SIGKILL "
        f"(default: {supervisor.DEFAULT_STOP_TIMEOUT:g}).",
    )
    stop.set_defaults(func=cmd_sleep_stop)

    st = noun_sub.add_parser(
        "status", help="Report loop process state + the cross-process ALERT/ASLEEP signal."
    )
    st.add_argument("--json", action="store_true", help=_JSON_HELP)
    st.set_defaults(func=cmd_sleep_status)


def _register_demo(noun_sub: argparse._SubParsersAction) -> None:
    demo = noun_sub.add_parser(
        "demo",
        help="Walk ALERT→DROWSY→ASLEEP→wake with no robot (synthetic sense + fake clock).",
    )
    demo.add_argument("--json", action="store_true", help=_JSON_HELP)
    _add_idle_timeout(demo)
    demo.set_defaults(func=cmd_sleep_demo)


def register(sub: argparse._SubParsersAction) -> None:
    p = sub.add_parser(
        "sleep",
        help="Drift off when undisturbed, wake on a stimulus "
        "(see 'reachy-mini-cli sleep overview').",
    )
    p.add_argument("--json", action="store_true", help=_JSON_HELP)
    p.set_defaults(func=_no_verb, json=False)
    noun_sub = p.add_subparsers(dest="sleep_command", parser_class=type(p))

    ov = noun_sub.add_parser("overview", help="Describe the sleep noun group.")
    ov.add_argument("--json", action="store_true", help=_JSON_HELP)
    ov.set_defaults(func=cmd_sleep_overview)

    _register_run(noun_sub)
    _register_process_verbs(noun_sub)
    _register_demo(noun_sub)
