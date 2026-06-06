"""Run the ``listen`` motion loop as a tracked background process.

The supervisor half of :mod:`reachy.motion` — the mirror of :mod:`reachy.alive`'s
process management, but for the sound-orienting :func:`reachy.motion.server.run`
loop instead of the feel-alive loop. ``start`` / ``stop`` / ``restart`` /
``status`` manage a detached background process tracked with a PID + log file
under the same per-user state dir the daemon and demo-mode use. ``start``
re-invokes this very CLI (``python -m reachy listen run``) so the loop keeps
running after the launching command returns.

Pure standard library (``subprocess`` / ``signal`` / ``os``): the loop just talks
to the daemon over the existing transport, so this adds **no** third-party runtime
dependency. It needs *something to talk to* — a running daemon
(``reachy daemon start``) for the http transport, or the ``[sdk]`` extra for
``--transport sdk``.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only ever re-spawns this trusted CLI (sys.executable -m reachy)
import sys
import time
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# Reuse the daemon's generic process primitives + state dir so the listen loop,
# demo-mode, and the daemon share one bookkeeping location.
from reachy.daemon import health_ok, is_alive, state_dir
from reachy.motion.listen import ListenParams
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

# Grace window after spawning before we trust the loop came up (vs crashed).
_START_GRACE = 0.4
# Seconds to wait after SIGTERM before escalating to SIGKILL.
DEFAULT_STOP_TIMEOUT = 10.0
# How finely _wait_gone polls for the process to exit.
_SLEEP_SLICE = 0.25
_STATUS_NOT_RUNNING = "not running"


def pid_file() -> Path:
    return state_dir() / "listen.pid"


def log_file() -> Path:
    return state_dir() / "listen.log"


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
    """Best-effort guard against PID reuse: is ``pid`` actually a listen loop?

    Reads ``/proc/<pid>/cmdline`` on Linux (the spawn line contains both
    ``reachy`` and ``listen``). If ``/proc`` is unavailable we cannot verify, so
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
    return "reachy" in cmdline and "listen" in cmdline


def build_run_command(
    *,
    transport: str,
    base_url: str,
    timeout: float,
    params: ListenParams,
) -> list[str]:
    """The argv the background process runs: ``python -m reachy listen run``."""
    cmd = [
        sys.executable,
        "-m",
        "reachy",
        "listen",
        "run",
        "--transport",
        transport,
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
        "--gain",
        str(params.gain),
        "--max-yaw",
        str(params.max_yaw),
        "--deadband",
        str(params.deadband),
        "--dwell",
        str(params.dwell),
        "--hold",
        str(params.hold),
        "--speed",
        str(params.alert_speed),
        "--recenter-after",
        str(params.recenter_after),
        "--idle-energy",
        str(params.idle_energy),
        "--drift-speed",
        str(params.drift_speed),
    ]
    if params.speech_only:
        cmd.append("--speech-only")
    return cmd


def start(
    *,
    transport: str = "http",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    params: ListenParams | None = None,
) -> dict[str, object]:
    """Start the listen loop in the background (idempotent).

    If a tracked loop is already alive, report ``already-running``. For the http
    transport, preflight the daemon's health route so we don't spawn a loop with
    nothing to talk to. Then spawn the loop detached, record its PID + log path,
    and give it a short grace window to confirm it didn't crash on startup.
    """
    params = params if params is not None else ListenParams()
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

    cmd = build_run_command(transport=transport, base_url=base_url, timeout=timeout, params=params)
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
            message=f"failed to launch listen ({cmd[0]}): {err}",
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
        result["note"] = f"listen exited during startup; see {log_path}"
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the listen loop this CLI started: SIGTERM, then SIGKILL if it lingers.

    SIGTERM lets the loop ease the robot back to center before it exits. Guards
    against PID reuse (never signals a process that isn't our loop).
    """
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked listen pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_process(pid):
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer a listen loop (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop listen pid {pid}",
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
            message=f"failed to stop listen pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def restart(**start_kwargs) -> dict[str, object]:
    """Stop the tracked loop (if any) then start a fresh one (re-reads code/params)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Report the listen process state and whether its target daemon answers."""
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
