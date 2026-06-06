"""Local ``reachy-mini-daemon`` process lifecycle (start / stop / status).

The robot verbs (``device`` / ``app`` / ``move``) are *clients* of a running
daemon; this module is the missing other half — it brings the daemon up. It
spawns ``reachy-mini-daemon`` as a detached background process, tracks it via a
PID file + log file under an XDG state dir, and reconciles the OS process state
with the daemon's HTTP health endpoint.

Pure standard library (``subprocess`` / ``signal`` / ``os`` / ``urllib``):
managing a local process adds **no** third-party runtime dependency, so the slim
base install keeps its zero-runtime-deps property. The daemon binary itself
ships in the ``[daemon]`` extra (``pip install 'reachy-cli[daemon]'``); a missing
binary is reported as a clean environment error pointing at that install.
"""

from __future__ import annotations

import os
import shlex
import shutil
import signal
import subprocess  # nosec B404 - only ever spawns the trusted reachy-mini-daemon binary
import time
import urllib.parse
import urllib.request
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

DAEMON_BINARY = "reachy-mini-daemon"
# Health route the running daemon serves; the http transport hits the same one.
HEALTH_PATH = "/api/daemon/status"
# Seconds to wait after SIGTERM before escalating to SIGKILL.
DEFAULT_STOP_TIMEOUT = 10.0
# Seconds ``start`` polls the health endpoint before giving up the wait.
DEFAULT_WAIT_TIMEOUT = 30.0
_POLL_INTERVAL = 0.25
# Shared status literal (one definition; avoids Sonar S1192 duplicate-string).
_STATUS_NOT_RUNNING = "not running"


def state_dir() -> Path:
    """Return (and create) the per-user state dir for daemon bookkeeping.

    ``$REACHY_STATE_DIR`` overrides everything (tests use it for isolation);
    otherwise ``$XDG_STATE_HOME/reachy`` or ``~/.local/state/reachy``.
    """
    override = os.environ.get("REACHY_STATE_DIR")
    if override:
        base = Path(override)
    else:
        xdg = os.environ.get("XDG_STATE_HOME")
        base = Path(xdg) / "reachy" if xdg else Path.home() / ".local" / "state" / "reachy"
    base.mkdir(parents=True, exist_ok=True)
    return base


def pid_file() -> Path:
    return state_dir() / "daemon.pid"


def log_file() -> Path:
    return state_dir() / "daemon.log"


def resolve_daemon_cmd(override: str | None = None) -> list[str]:
    """Resolve the daemon launch command, or raise with an install hint.

    Precedence: explicit ``override`` / ``$REACHY_DAEMON_CMD`` (shell-split) →
    ``reachy-mini-daemon`` on PATH. A missing binary almost always means the user
    installed the slim/remote profile, so the remediation says how to fix it.
    """
    raw = override or os.environ.get("REACHY_DAEMON_CMD")
    if raw:
        parts = shlex.split(raw)
        if not parts:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="empty daemon command",
                remediation="pass a real command via --daemon-cmd or REACHY_DAEMON_CMD",
            )
        # Resolve the program against PATH but keep caller-supplied args.
        program = shutil.which(parts[0]) or parts[0]
        return [program, *parts[1:]]
    found = shutil.which(DAEMON_BINARY)
    if found:
        return [found]
    raise CliError(
        code=EXIT_ENV_ERROR,
        message=f"{DAEMON_BINARY!r} not found on PATH",
        remediation=(
            "install the daemon build: pip install 'reachy-cli[daemon]' (the bare/remote "
            "install is HTTP-only). Or point --daemon-cmd / REACHY_DAEMON_CMD at the binary, "
            "or run the daemon elsewhere and target it with --base-url / REACHY_BASE_URL."
        ),
    )


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


def is_alive(pid: int) -> bool:
    """True if a process with ``pid`` currently exists."""
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        # Exists but owned by another user — still "alive" for our purposes.
        return True
    return True


def _is_our_daemon(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a reachy daemon?

    Reads ``/proc/<pid>/cmdline`` on Linux. If ``/proc`` is unavailable (non-Linux)
    we cannot verify, so we trust the pid file and return True. If ``/proc`` exists
    but the process is gone or clearly isn't a reachy daemon, return False so
    :func:`stop` never signals an unrelated process that recycled the pid.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    cmdline = raw.replace(b"\x00", b" ").decode("utf-8", "replace")
    return DAEMON_BINARY in cmdline or "reachy_mini" in cmdline


def health_ok(base_url: str, timeout: float) -> bool:
    """True if the daemon answers its health route with a 2xx. Never raises."""
    url = f"{base_url.rstrip('/')}{HEALTH_PATH}"
    if urllib.parse.urlsplit(url).scheme not in ("http", "https"):
        return False
    req = urllib.request.Request(url, method="GET")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # nosec B310
            status = getattr(resp, "status", None) or resp.getcode()
            return 200 <= int(status) < 300
    except (OSError, ValueError):
        # urllib.error.URLError / HTTPError both subclass OSError.
        return False


def _clear_pid() -> None:
    try:
        pid_file().unlink()
    except FileNotFoundError:
        pass


def _poll_until_healthy(
    base_url: str, poll_timeout: float, wait_timeout: float, *, alive=None
) -> bool:
    """Poll the health route until it answers, the deadline passes, or ``alive()`` fails.

    ``alive`` is an optional predicate (e.g. "is the process we spawned still
    running?"); when it returns False the poll gives up early.
    """
    deadline = time.monotonic() + wait_timeout
    while time.monotonic() < deadline:
        if alive is not None and not alive():
            return False
        if health_ok(base_url, poll_timeout):
            return True
        time.sleep(_POLL_INTERVAL)
    return health_ok(base_url, poll_timeout)


def _wait_for_health(
    proc: subprocess.Popen, base_url: str, poll_timeout: float, wait_timeout: float
) -> bool:
    """Poll the health route until it answers, the deadline passes, or ``proc`` dies."""
    return _poll_until_healthy(
        base_url, poll_timeout, wait_timeout, alive=lambda: proc.poll() is None
    )


def _wait_gone(pid: int, timeout: float) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(_POLL_INTERVAL)
    return not is_alive(pid)


def start(
    *,
    base_url: str = DEFAULT_BASE_URL,
    wait: bool = True,
    wait_timeout: float = DEFAULT_WAIT_TIMEOUT,
    poll_timeout: float = DEFAULT_TIMEOUT,
    daemon_cmd: str | None = None,
    extra_args: list[str] | None = None,
) -> dict[str, object]:
    """Start the daemon in the background (idempotent).

    If a tracked process is already alive, or the health endpoint already
    answers, report ``already-running`` rather than spawn a second daemon.
    Otherwise spawn ``reachy-mini-daemon`` detached, record its PID + log path,
    and (unless ``wait`` is False) poll the health route until it answers or
    ``wait_timeout`` elapses. ``extra_args`` are forwarded to the daemon verbatim.
    """
    existing = read_pid()
    if existing is not None and is_alive(existing):
        healthy = health_ok(base_url, poll_timeout)
        if wait and not healthy:
            # The tracked daemon is up but not answering yet — honour wait here too.
            healthy = _poll_until_healthy(
                base_url, poll_timeout, wait_timeout, alive=lambda: is_alive(existing)
            )
        return {
            "status": "already-running",
            "pid": existing,
            "url": base_url,
            "healthy": healthy,
            "log": str(log_file()),
        }
    if health_ok(base_url, poll_timeout):
        # A daemon we don't track (foreign / service-managed / remote) already answers.
        return {
            "status": "already-running",
            "pid": None,
            "url": base_url,
            "healthy": True,
            "log": str(log_file()),
            "note": "a daemon already answers at this url (not started by this CLI)",
        }

    cmd = resolve_daemon_cmd(daemon_cmd)
    if extra_args:
        cmd = [*cmd, *extra_args]
    log_path = log_file()
    # Detach into its own session so the daemon outlives this CLI process, and
    # tee its stdout+stderr to the log file for ``daemon status`` to point at.
    try:
        with open(log_path, "ab") as logf:
            proc = subprocess.Popen(  # nosec B603 - trusted binary, arg list, no shell
                cmd,
                stdout=logf,
                stderr=subprocess.STDOUT,
                stdin=subprocess.DEVNULL,
                start_new_session=True,
            )
    except OSError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"failed to launch the daemon ({cmd[0]}): {err}",
            remediation=(
                "check the daemon binary is executable, or set --daemon-cmd / "
                "REACHY_DAEMON_CMD to a working command"
            ),
        ) from err
    pid_file().write_text(str(proc.pid), encoding="utf-8")

    healthy = _wait_for_health(proc, base_url, poll_timeout, wait_timeout) if wait else False
    result: dict[str, object] = {
        "status": "started",
        "pid": proc.pid,
        "url": base_url,
        "healthy": healthy,
        "log": str(log_path),
    }
    if wait and not healthy:
        if proc.poll() is not None:
            # Crashed on startup (e.g. motors busy, port taken) — surface it.
            result["status"] = "exited"
            result["exit_code"] = proc.returncode
            result["note"] = f"daemon exited during startup; see {log_path}"
        else:
            result["note"] = (
                f"started but health route not ready within {wait_timeout:g}s; "
                f"check 'reachy daemon status' and {log_path}"
            )
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the daemon this CLI started: SIGTERM, then SIGKILL if it lingers.

    Guards against PID reuse (never signals a process that isn't our daemon) and
    never reports success it can't confirm — if the process survives SIGKILL it
    raises a :class:`CliError` rather than claiming ``stopped``.
    """
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked daemon pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_daemon(pid):
        # The recorded pid was recycled by an unrelated process — do NOT signal it.
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer a reachy daemon (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop daemon pid {pid}",
            remediation="stop it as the owning user, or via your service manager",
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
            message=f"failed to stop daemon pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def is_robot_live(*, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT) -> bool:
    """Return True if the robot/daemon is actually reachable *right now*.

    This is an **always-fresh** liveness probe: it re-checks the HTTP health
    endpoint on every call and deliberately ignores any PID file state. The PID
    file can be stale across a restart (a new process may have already claimed the
    port while the old PID file still exists), so trusting it would produce false
    negatives immediately after a restart and false positives when the daemon
    crashed without cleaning up its PID file.

    Correct restart semantics: call this function twice in succession — the first
    call correctly reports ``False`` while the daemon is down; the second call
    correctly reports ``True`` once the new process is listening, without any
    in-process caching in the way.

    The function is **additive**: it does not replace the existing ``health_ok``
    helper (which the start/stop/status machinery uses internally) but provides a
    stable, named, restart-safe public surface for callers that only need to know
    "can I talk to the robot right now?".
    """
    return health_ok(base_url, timeout)


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Reconcile the tracked process state with the HTTP health endpoint."""
    pid = read_pid()
    if pid is None:
        process = "stopped"
    elif is_alive(pid):
        process = "running"
    else:
        process = "stale"  # pid file points at a dead process
    live = is_robot_live(base_url=base_url, timeout=timeout)
    return {
        "process": process,
        "pid": pid,
        "http": "healthy" if live else "unreachable",
        "live": live,
        "url": base_url,
        "log": str(log_file()),
    }
