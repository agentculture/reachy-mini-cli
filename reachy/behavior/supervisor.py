"""Run the behavior engine as a tracked background process.

Mirrors :mod:`reachy.alive`'s supervisor half (and the ``daemon`` noun): spawn
``python -m reachy behavior engine run`` detached, track it with a PID file + log
under ``state_dir()/behavior``, and reconcile the OS process with the daemon's
health route. One long-lived engine owns motion; ``behavior run`` auto-starts it
if absent, and all controllers talk to it through the command spool.

Pure standard library (``subprocess`` / ``signal`` / ``os``); the same
PID-reuse guarding and SIGTERM→SIGKILL escalation as the daemon supervisor.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only ever re-spawns this trusted CLI (sys.executable -m reachy)
import sys
import time
from pathlib import Path

from reachy.behavior.control import behavior_dir
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.daemon import health_ok, is_alive
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

_START_GRACE = 0.4
DEFAULT_STOP_TIMEOUT = 10.0
_SLEEP_SLICE = 0.25
_STATUS_NOT_RUNNING = "not running"


def pid_file() -> Path:
    return behavior_dir() / "engine.pid"


def log_file() -> Path:
    return behavior_dir() / "engine.log"


def read_pid() -> int | None:
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
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(_SLEEP_SLICE)
    return not is_alive(pid)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a behavior engine?"""
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    # The engine's spawn line is `... -m reachy behavior engine run`; require BOTH
    # tokens so a bare `reachy behavior <verb>` CLI call — or any unrelated process
    # that merely contains "behavior" — is never signalled under PID reuse.
    return "behavior" in cmdline and "engine" in cmdline


def build_run_command(
    *,
    transport: str,
    base_url: str,
    timeout: float,
    compose_hz: float,
    energy: float,
    base_layer: bool,
    settle: bool,
) -> list[str]:
    """The argv the background process runs: ``python -m reachy behavior engine run``."""
    cmd = [
        sys.executable,
        "-m",
        "reachy",
        "behavior",
        "engine",
        "run",
        "--transport",
        transport,
        "--base-url",
        base_url,
        "--timeout",
        str(timeout),
        "--compose-hz",
        str(compose_hz),
        "--energy",
        str(energy),
    ]
    if not base_layer:
        cmd.append("--no-base-layer")
    if not settle:
        cmd.append("--no-settle")
    return cmd


def start(
    *,
    transport: str = "http",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    compose_hz: float = 50.0,
    energy: float = 1.0,
    base_layer: bool = True,
    settle: bool = True,
) -> dict[str, object]:
    """Start the engine in the background (idempotent).

    If a tracked engine is alive, report ``already-running``. For the http
    transport, preflight the daemon health route so we never spawn an engine with
    nothing to drive. Then spawn detached, record the PID + log, and grace-check it.
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
        compose_hz=compose_hz,
        energy=energy,
        base_layer=base_layer,
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
            message=f"failed to launch the behavior engine ({cmd[0]}): {err}",
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
        result["status"] = "exited"
        result["exit_code"] = proc.returncode
        result["note"] = f"engine exited during startup; see {log_path}"
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the engine this CLI started: SIGTERM (so it settles), then SIGKILL if it lingers."""
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked engine pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_process(pid):
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer a behavior engine (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop behavior engine pid {pid}",
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
            message=f"failed to stop behavior engine pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Report the engine process state and whether its target daemon answers."""
    pid = read_pid()
    if pid is None:
        process = "stopped"
    elif is_alive(pid):
        process = "running"
    else:
        process = "stale"
    return {
        "process": process,
        "pid": pid,
        "daemon": "healthy" if health_ok(base_url, timeout) else "unreachable",
        "url": base_url,
        "log": str(log_file()),
    }


def ensure_running(**start_kwargs) -> dict[str, object]:
    """Start the engine if it isn't already tracked-alive (idempotent helper)."""
    pid = read_pid()
    if pid is not None and is_alive(pid):
        return {"status": "already-running", "pid": pid}
    return start(**start_kwargs)
