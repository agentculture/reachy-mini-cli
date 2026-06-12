"""Run the ``sleep`` loop as a tracked background process.

The supervisor half of the ``sleep`` noun — a sibling of
:mod:`reachy.motion.supervisor` (the ``listen`` loop's supervisor) and
:mod:`reachy.speech.supervisor` (the ``think`` loop's supervisor), but for the
sleep loop (``reachy sleep run``) instead. ``start`` / ``stop`` / ``restart`` /
``status`` manage a detached background process tracked with a PID + log file
under the same per-user state dir the daemon, demo-mode, listen, and think all
use. ``start`` re-invokes this very CLI (``python -m reachy sleep run``) so the
loop keeps running after the launching command returns.

This module deliberately does **not** import or reuse the listen or think
supervisors: it owns its own ``sleep.pid`` / ``sleep.log`` filenames so the
loops can run side-by-side, and it reuses only the *generic* process primitives
from :mod:`reachy.daemon` (``state_dir`` / ``is_alive`` — PID-file location
and liveness). The process-management mechanics (PID-file write/read, detached
spawn, signal-based stop, PID-reuse guard) are kept self-contained here so
editing the listen-owned or think-owned supervisors is never required.

Pure standard library (``subprocess`` / ``signal`` / ``os``): the loop talks to
the robot over the existing transport, so this adds **no** third-party runtime
dependency.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only ever re-spawns this trusted CLI (sys.executable -m reachy)
import sys
import time
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# Reuse the daemon's generic process primitives + state dir so the sleep loop,
# the listen loop, think loop, demo-mode, and the daemon share one bookkeeping
# location.
from reachy.daemon import is_alive, state_dir
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

# Grace window after spawning before we trust the loop came up (vs crashed).
_START_GRACE = 0.4
# Seconds to wait after SIGTERM before escalating to SIGKILL.
DEFAULT_STOP_TIMEOUT = 10.0
# How finely _wait_gone polls for the process to exit.
_SLEEP_SLICE = 0.25
_STATUS_NOT_RUNNING = "not running"


def pid_file() -> Path:
    return state_dir() / "sleep.pid"


def log_file() -> Path:
    return state_dir() / "sleep.log"


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
    """Poll until ``pid`` is gone or ``timeout`` elapses."""
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(_SLEEP_SLICE)
    return not is_alive(pid)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a sleep loop?

    Reads ``/proc/<pid>/cmdline`` on Linux (the spawn line contains both
    ``reachy`` and ``sleep``). If ``/proc`` is unavailable we cannot verify, so
    we trust the pid file. If ``/proc`` exists but the process is gone or clearly
    isn't ours, return False so :func:`stop` never signals an unrelated pid.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return "reachy" in cmdline and "sleep" in cmdline


def build_run_command(
    *,
    transport: str,
    base_url: str,
    timeout: float,
    ticks: int | None = None,
    idle_timeout: float | None = None,
    no_audio_wake: bool = False,
) -> list[str]:
    """The argv the background process runs: ``python -m reachy sleep run``.

    Only flags with a concrete value are forwarded; unset optional flags fall
    through to the engine's own env/default resolution in the child. The noun
    task (t8) will finalize which additional flags are accepted; placeholders for
    ``--ticks`` and ``--idle-timeout`` are included here for passthrough
    consistency with the motion supervisor's approach.

    ``no_audio_wake`` forwards ``--no-audio-wake`` when ``True`` — pat-only /
    quiet-room mode where speech/snap/DoA stimuli are ignored.
    """
    cmd = [
        sys.executable,
        "-m",
        "reachy",
        "sleep",
        "run",
        "--transport",
        transport,
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
    ]
    if ticks is not None:
        cmd += ["--ticks", str(ticks)]
    if idle_timeout is not None:
        cmd += ["--idle-timeout", str(idle_timeout)]
    if no_audio_wake:
        cmd += ["--no-audio-wake"]
    return cmd


def start(
    *,
    transport: str = "sdk",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    ticks: int | None = None,
    idle_timeout: float | None = None,
    no_audio_wake: bool = False,
) -> dict[str, object]:
    """Start the sleep loop in the background (idempotent).

    If a tracked loop is already alive, report ``already-running``. Otherwise
    spawn the loop detached, record its PID + log path, and give it a short grace
    window to confirm it didn't crash on startup.

    Unlike the daemon/listen ``start``, there is no HTTP health preflight here:
    the sleep loop surfaces its own clean exit-2 ``CliError`` if unreachable —
    so a spawned loop that can't reach the robot exits during the grace window and
    is reported as ``exited``.

    ``no_audio_wake`` forwards ``--no-audio-wake`` into the spawned ``sleep run``
    command — pat-only / quiet-room mode where speech/snap/DoA stimuli are ignored.
    """
    existing = read_pid()
    if existing is not None and is_alive(existing):
        return {
            "status": "already-running",
            "pid": existing,
            "transport": transport,
            "log": str(log_file()),
        }

    cmd = build_run_command(
        transport=transport,
        base_url=base_url,
        timeout=timeout,
        ticks=ticks,
        idle_timeout=idle_timeout,
        no_audio_wake=no_audio_wake,
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
            message=f"failed to launch sleep ({cmd[0]}): {err}",
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
    if proc.poll() is not None:
        # Exited within the grace window — startup failed (e.g. robot unreachable).
        # Clear the pid file we just wrote so `status`/`stop` don't report a stale pid.
        _clear_pid()
        result["status"] = "exited"
        result["exit_code"] = proc.returncode
        result["note"] = f"sleep exited during startup; see {log_path}"
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the sleep loop this CLI started: SIGTERM, then SIGKILL if it lingers.

    Guards against PID reuse (never signals a process that isn't our loop).
    """
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked sleep pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_process(pid):
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer a sleep loop (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop sleep pid {pid}",
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
            message=f"failed to stop sleep pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def restart(**start_kwargs) -> dict[str, object]:
    """Stop the tracked loop (if any) then start a fresh one (re-reads code/flags)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status() -> dict[str, object]:
    """Report the sleep loop process state (PID + liveness)."""
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
        "log": str(log_file()),
    }
