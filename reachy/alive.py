"""``demo-mode`` — make the Reachy Mini *feel alive* + manage that as a process.

Two halves, mirroring :mod:`reachy.daemon`:

* **The engine** — a pure idle-motion generator (:func:`next_pose`) and the
  foreground loop (:func:`run_loop`) that drives a :class:`~reachy.robot.transport.Transport`.
  Each tick it sends a gentle "alive" pose — a slow breathing oscillation, an
  occasional glance to a new gaze target, and a little antenna sway — so a robot
  that is otherwise idle looks like it is quietly present rather than frozen.
* **The supervisor** — :func:`start` / :func:`stop` / :func:`status` run that loop
  as a detached background process, tracked with a PID file + log file under the
  same per-user state dir the daemon uses. ``start`` re-invokes this very CLI
  (``python -m reachy demo-mode run``) so the loop keeps running after the
  launching command returns.

Pure standard library (``random`` / ``math`` / ``subprocess`` / ``signal`` /
``os``): the "feel alive" behaviour is just a stream of ``move goto`` calls over
the existing transport, so this adds **no** third-party runtime dependency and
the slim base install keeps its zero-runtime-deps property. The motion only
needs *something to talk to* — a running daemon (``reachy daemon start``) for the
http transport, or the ``[sdk]`` extra for ``--transport sdk``.
"""

from __future__ import annotations

import math
import os
import random
import signal
import subprocess  # nosec B404 - only ever re-spawns this trusted CLI (sys.executable -m reachy)
import sys
import time
from dataclasses import dataclass
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# Reuse the daemon's generic process primitives + state dir so demo-mode and the
# daemon share one bookkeeping location and one definition of "is this pid alive".
from reachy.daemon import health_ok, is_alive, state_dir
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT, Transport

# Grace window after spawning before we trust the loop came up (vs crashed).
_START_GRACE = 0.4
# Seconds to wait after SIGTERM before escalating to SIGKILL.
DEFAULT_STOP_TIMEOUT = 10.0
# How finely the loop slices its inter-tick sleep so a stop signal lands fast.
_SLEEP_SLICE = 0.25
_STATUS_NOT_RUNNING = "not running"


# --------------------------------------------------------------------------- #
# Engine: the "feel alive" motion generator                                   #
# --------------------------------------------------------------------------- #


@dataclass
class AliveConfig:
    """Tunables for the idle "alive" motion.

    Amplitudes are in the CLI's friendly units (millimetres / degrees) and are
    all scaled by ``energy`` (a single 0..n liveliness knob). ``interval`` sets
    the tempo (seconds between poses); each ``goto`` is given a duration just
    under ``interval`` so motion glides continuously rather than stepping.
    """

    interval: float = 2.5
    energy: float = 1.0
    breathe_period: float = 5.0
    breathe_z_mm: float = 3.0
    breathe_pitch_deg: float = 2.0
    gaze_yaw_deg: float = 18.0
    gaze_pitch_deg: float = 10.0
    gaze_roll_deg: float = 4.0
    antenna_deg: float = 18.0
    body_yaw_deg: float = 8.0
    glance_probability: float = 0.5
    interpolation: str = "minjerk"
    seed: int | None = None
    # Give up the loop after this many consecutive failed gotos (daemon gone).
    max_errors: int = 5


def neutral_pose(config: AliveConfig) -> dict[str, object]:
    """The centred rest pose demo-mode settles to when it stops."""
    return {
        "head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "antennas": (0.0, 0.0),
        "body_yaw": 0.0,
        "duration": max(0.5, config.interval),
        "interpolation": config.interpolation,
    }


def next_pose(elapsed: float, rng: random.Random, config: AliveConfig) -> dict[str, object]:
    """Compute the next idle pose at time ``elapsed`` seconds into the loop.

    Pure and deterministic given ``elapsed`` and ``rng``: breathing is a function
    of ``elapsed`` (continuous), the glance target is drawn from ``rng``. The
    result maps straight onto :meth:`Transport.move_goto` keyword arguments.
    """
    e = max(0.0, config.energy)
    phase = 2.0 * math.pi * (elapsed / config.breathe_period) if config.breathe_period else 0.0

    # Breathing: a slow vertical + pitch oscillation, always present.
    z = config.breathe_z_mm * e * math.sin(phase)
    breathe_pitch = config.breathe_pitch_deg * e * math.sin(phase)

    # Gaze: now and then look somewhere new; otherwise just micro-drift near centre.
    if rng.random() < config.glance_probability:
        scale = 1.0
        body_yaw = rng.uniform(-config.body_yaw_deg, config.body_yaw_deg) * e
    else:
        scale = 0.2
        body_yaw = 0.0
    yaw = rng.uniform(-config.gaze_yaw_deg, config.gaze_yaw_deg) * e * scale
    gaze_pitch = rng.uniform(-config.gaze_pitch_deg, config.gaze_pitch_deg) * e * scale
    roll = rng.uniform(-config.gaze_roll_deg, config.gaze_roll_deg) * e * scale

    # Antennas: a gentle sway plus a touch of independent jitter.
    sway = config.antenna_deg * e * math.sin(phase * 1.5)
    jitter = rng.uniform(-1.0, 1.0) * config.antenna_deg * 0.3 * e
    right = sway + jitter
    left = -sway + jitter

    return {
        "head": {
            "x": 0.0,
            "y": 0.0,
            "z": z,
            "roll": roll,
            "pitch": breathe_pitch + gaze_pitch,
            "yaw": yaw,
        },
        "antennas": (right, left),
        "body_yaw": body_yaw,
        "duration": max(0.2, config.interval * 0.9),
        "interpolation": config.interpolation,
    }


def _send_pose(transport: Transport, pose: dict[str, object]) -> object:
    return transport.move_goto(
        head=pose["head"],  # type: ignore[arg-type]
        antennas=pose["antennas"],  # type: ignore[arg-type]
        body_yaw=pose["body_yaw"],  # type: ignore[arg-type]
        duration=pose["duration"],  # type: ignore[arg-type]
        interpolation=pose["interpolation"],  # type: ignore[arg-type]
    )


def _install_stop_handlers(stop: dict):
    """Install SIGTERM/SIGINT handlers that flip ``stop['flag']``; return the olds.

    ``signal.signal`` only works in the main thread; under a test runner / worker
    thread it raises ``ValueError`` — in that case we run without graceful stop.
    """

    def _handler(_signum, _frame):
        stop["flag"] = True

    try:
        return (
            signal.signal(signal.SIGTERM, _handler),
            signal.signal(signal.SIGINT, _handler),
        )
    except ValueError:
        return None


def _restore_stop_handlers(handlers) -> None:
    if handlers is not None:
        signal.signal(signal.SIGTERM, handlers[0])
        signal.signal(signal.SIGINT, handlers[1])


def _interruptible_sleep(seconds: float, stop: dict, sleep) -> None:
    """Sleep up to ``seconds`` in small slices, waking early if ``stop`` is set."""
    if seconds <= 0:
        return
    slept = 0.0
    while slept < seconds and not stop["flag"]:
        sleep(_SLEEP_SLICE)
        slept += _SLEEP_SLICE


def _preflight(transport: Transport, config: AliveConfig, wake: bool) -> None:
    """First robot call — validates the transport. A dead daemon raises CliError."""
    if wake:
        transport.wake()
    else:
        _send_pose(transport, neutral_pose(config))


def _settle(transport: Transport, config: AliveConfig) -> None:
    """Best-effort ease back to neutral on stop (a dead daemon can't be settled)."""
    try:
        _send_pose(transport, neutral_pose(config))
    except CliError:
        pass


def _send_tick(
    transport: Transport, pose: dict, tick: int, elapsed: float, consecutive: int, max_errors: int
) -> tuple[dict, int]:
    """Send one pose; return ``(event, consecutive_errors)``.

    Re-raises the :class:`CliError` once ``max_errors`` consecutive sends have
    failed, so a sustained daemon outage ends the loop cleanly.
    """
    base = {"tick": tick, "elapsed": round(elapsed, 3)}
    try:
        _send_pose(transport, pose)
    except CliError as exc:
        consecutive += 1
        if consecutive >= max_errors:
            raise
        return {**base, "ok": False, "error": exc.message}, consecutive
    return {**base, "ok": True, "error": None}, 0


def run_loop(
    transport: Transport,
    config: AliveConfig,
    *,
    sleep=time.sleep,
    now=time.monotonic,
    on_start=None,
    emit=None,
    max_ticks: int | None = None,
    wake: bool = True,
    settle: bool = True,
    rng: random.Random | None = None,
) -> int:
    """Drive the robot with idle "alive" poses until stopped. Returns ticks run.

    Connectivity is validated up front (the opening ``wake`` — or, with
    ``wake=False``, a single neutral ``goto``); if the robot can't be reached the
    underlying :class:`CliError` propagates so the caller exits cleanly. The
    optional ``on_start`` callback runs *after* that preflight succeeds — so a
    caller can emit a "starting" line only once the loop is truly live, never
    polluting the error output of a failed preflight. Once running, transient send
    failures are tolerated up to ``config.max_errors`` consecutive misses before
    giving up. On stop the robot is eased back to neutral (best effort).
    """
    # nosec B311 - the RNG only shapes idle robot motion; not security-sensitive.
    rng = rng if rng is not None else random.Random(config.seed)  # nosec B311
    stop = {"flag": False}
    handlers = _install_stop_handlers(stop)

    _preflight(transport, config, wake)
    if on_start is not None:
        on_start()

    start_t = now()
    ticks = 0
    consecutive = 0
    try:
        while not stop["flag"]:
            elapsed = now() - start_t
            pose = next_pose(elapsed, rng, config)
            event, consecutive = _send_tick(
                transport, pose, ticks + 1, elapsed, consecutive, config.max_errors
            )
            ticks += 1
            if emit is not None:
                emit(event)
            if max_ticks is not None and ticks >= max_ticks:
                break
            _interruptible_sleep(config.interval, stop, sleep)
    finally:
        _restore_stop_handlers(handlers)
        if settle:
            _settle(transport, config)
    return ticks


# --------------------------------------------------------------------------- #
# Supervisor: run the loop as a tracked background process                     #
# --------------------------------------------------------------------------- #


def pid_file() -> Path:
    return state_dir() / "demo-mode.pid"


def log_file() -> Path:
    return state_dir() / "demo-mode.log"


def read_pid() -> int | None:
    """Return the tracked PID, or ``None`` if the file is absent or unparseable."""
    try:
        text = pid_file().read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _clear_pid() -> None:
    try:
        pid_file().unlink()
    except FileNotFoundError:
        pass


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses. Uses this module's
    :func:`is_alive` so tests can pin liveness via ``reachy.alive.is_alive``.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(_SLEEP_SLICE)
    return not is_alive(pid)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a demo-mode loop?

    Reads ``/proc/<pid>/cmdline`` on Linux (the spawn line contains
    ``demo-mode``). If ``/proc`` is unavailable we cannot verify, so we trust the
    pid file. If ``/proc`` exists but the process is gone or clearly isn't ours,
    return False so :func:`stop` never signals an unrelated recycled pid.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return "demo-mode" in cmdline or "demo_mode" in cmdline


def build_run_command(
    *,
    transport: str,
    base_url: str,
    timeout: float,
    interval: float,
    energy: float,
    interpolation: str,
    seed: int | None,
    wake: bool,
    settle: bool,
) -> list[str]:
    """The argv that the background process runs: ``python -m reachy demo-mode run``."""
    cmd = [
        sys.executable,
        "-m",
        "reachy",
        "demo-mode",
        "run",
        "--transport",
        transport,
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
        "--interval",
        str(interval),
        "--energy",
        str(energy),
        "--interpolation",
        interpolation,
    ]
    if seed is not None:
        cmd += ["--seed", str(seed)]
    if not wake:
        cmd.append("--no-wake")
    if not settle:
        cmd.append("--no-settle")
    return cmd


def start(
    *,
    transport: str = "http",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    interval: float = AliveConfig.interval,
    energy: float = AliveConfig.energy,
    interpolation: str = AliveConfig.interpolation,
    seed: int | None = None,
    wake: bool = True,
    settle: bool = True,
) -> dict[str, object]:
    """Start the "feel alive" loop in the background (idempotent).

    If a tracked loop is already alive, report ``already-running``. For the http
    transport, preflight the daemon's health route so we don't spawn a loop with
    nothing to talk to (a dead daemon is reported as a clean environment error).
    Then spawn the loop detached, record its PID + log path, and give it a short
    grace window to confirm it didn't crash on startup.
    """
    existing = read_pid()
    if existing is not None and is_alive(existing):
        return {
            "status": "already-running",
            "pid": existing,
            "transport": transport,
            "log": str(log_file()),
        }

    if transport == "http" and not health_ok(base_url, timeout):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"no Reachy daemon reachable at {base_url}",
            remediation=(
                "start it first with 'reachy daemon start', or point --base-url / "
                "REACHY_BASE_URL at a running daemon (use --transport sdk to drive "
                "the robot in-process instead)"
            ),
        )

    cmd = build_run_command(
        transport=transport,
        base_url=base_url,
        timeout=timeout,
        interval=interval,
        energy=energy,
        interpolation=interpolation,
        seed=seed,
        wake=wake,
        settle=settle,
    )
    log_path = log_file()
    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(  # nosec B603 - trusted argv (this CLI), no shell
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"failed to launch demo-mode ({cmd[0]}): {err}",
            remediation="check the Python interpreter is usable and the state dir is writable",
        ) from err
    pid_file().write_text(str(proc.pid), encoding="utf-8")

    time.sleep(_START_GRACE)
    result: dict[str, object] = {
        "status": "started",
        "pid": proc.pid,
        "transport": transport,
        "log": str(log_path),
    }
    if transport == "http":
        result["url"] = base_url
    if proc.poll() is not None:
        # Exited within the grace window — startup failed (e.g. daemon vanished).
        result["status"] = "exited"
        result["exit_code"] = proc.returncode
        result["note"] = f"demo-mode exited during startup; see {log_path}"
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the demo-mode loop this CLI started: SIGTERM, then SIGKILL if it lingers.

    SIGTERM lets the loop ease the robot back to neutral before it exits. Guards
    against PID reuse (never signals a process that isn't our loop) and never
    claims success it can't confirm.
    """
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked demo-mode pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_process(pid):
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer a demo-mode loop (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop demo-mode pid {pid}",
            remediation="stop it as the owning user",
        ) from err
    signaled = "SIGTERM"
    gone = _wait_gone(pid, timeout)
    if not gone:
        try:
            os.kill(pid, signal.SIGKILL)
            signaled = "SIGKILL"
        except ProcessLookupError:
            gone = True
        if not gone:
            gone = _wait_gone(pid, 2.0)
    if not gone:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"failed to stop demo-mode pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def restart(**start_kwargs) -> dict[str, object]:
    """Stop the tracked loop (if any) then start a fresh one.

    The new process re-imports the latest motion code and re-reads config, so
    this is how an update is applied in the ad-hoc (non-service) mode. ``stop``
    is best-effort — a not-running loop is fine; only a genuinely unkillable
    process raises (propagated from :func:`stop`).
    """
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Report the demo-mode process state and whether its target daemon answers."""
    pid = read_pid()
    if pid is None:
        process = "stopped"
    elif is_alive(pid):
        process = "running"
    else:
        process = "stale"  # pid file points at a dead process
    return {
        "process": process,
        "pid": pid,
        "daemon": "healthy" if health_ok(base_url, timeout) else "unreachable",
        "url": base_url,
        "log": str(log_file()),
    }
