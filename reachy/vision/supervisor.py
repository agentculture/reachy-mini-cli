"""Run the ``vision`` loop as a tracked background process.

The vision supervisor manages the visual-orienting
:class:`~reachy.vision.producer.VisionProducer` loop as a detached background
process tracked with a PID + log file under the same per-user state dir the
daemon, demo-mode, and sleep supervisors share. (It was originally written to
mirror the ``listen`` loop's ``reachy.motion.supervisor`` exactly; that module
retired with its noun, and this one is unchanged by the removal.)
``start`` / ``stop`` / ``restart`` / ``status`` are the public API; ``start``
re-invokes this very CLI (``python -m reachy vision run``) so the loop keeps
running after the launching command returns.

Pure standard library (``subprocess`` / ``signal`` / ``os``, reached through
:mod:`reachy.procsup`): the loop talks to the robot through the existing
transport, so this adds no third-party runtime dependency. A running daemon
(``reachy daemon start``) is required for the http transport; the ``[sdk]``
extra is required for ``--transport sdk`` (frames need the local camera).

The process-management mechanics (PID-file write/read, detached spawn,
signal-based stop, PID-reuse guard) live once in :mod:`reachy.procsup` and are
shared with every sibling supervisor. What is genuinely vision's stays here: the
``vision.pid`` / ``vision.log`` filenames, the ``vision run`` argv, the
daemon-health preflight for the ``http`` transport, the ``transport`` / ``url``
result fields, and the wording of every message.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reachy import procsup

# Reuse the daemon's generic process primitives + state dir.
from reachy.daemon import health_ok, is_alive, state_dir
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT
from reachy.vision.producer import VisionParams

# Seconds to wait after SIGTERM before escalating to SIGKILL. Owned per
# supervisor rather than shared: it is an operator-facing default (the
# ``--timeout`` flag reads it), not a mechanic.
DEFAULT_STOP_TIMEOUT = 10.0

# The exact argv tokens the spawn line below carries — see
# reachy.procsup.has_argv_tokens for why this is a token set and not a substring.
_IDENTITY_TOKENS = ("reachy", "vision")

_LABELS = procsup.ProcessLabels(
    tracked="vision",
    launch="vision",
    exited="vision",
    reused="a vision loop",
    signalled="vision",
)


def pid_file() -> Path:
    return state_dir() / "vision.pid"


def log_file() -> Path:
    return state_dir() / "vision.log"


def read_pid() -> int | None:
    """Return the tracked PID, or ``None`` if the file is absent or unparseable."""
    return procsup.read_pid(pid_file())


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses."""
    return procsup.poll_until_gone(pid, timeout, is_alive=is_alive)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a vision loop?

    The spawn line is ``<python> -m reachy vision run ...``, so ``reachy`` and
    ``vision`` each appear as their OWN argv element — see
    :data:`_IDENTITY_TOKENS` and :func:`reachy.procsup.has_argv_tokens` for the
    exact-token rule and why the substring scan this used to run (issue #136)
    could be satisfied by a checkout directory name alone.
    """
    return procsup.has_argv_tokens(pid, _IDENTITY_TOKENS)


def build_run_command(
    *,
    transport: str,
    base_url: str,
    timeout: float,
    params: VisionParams,
) -> list[str]:
    """The argv the background process runs: ``python -m reachy vision run``."""
    return [
        sys.executable,
        "-m",
        "reachy",
        "vision",
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
        "--hold",
        str(params.hold),
        "--speed",
        str(params.speed),
        "--motion-threshold",
        str(params.motion_threshold),
    ]


def start(
    *,
    transport: str = "sdk",
    base_url: str = DEFAULT_BASE_URL,
    timeout: float = DEFAULT_TIMEOUT,
    params: VisionParams | None = None,
) -> dict[str, object]:
    """Start the vision loop in the background (idempotent).

    If a tracked loop is already alive, report ``already-running``. For the http
    transport, preflight the daemon's health route so we don't spawn a loop with
    nothing to talk to. Then spawn the loop detached, record its PID + log path,
    and give it a short grace window to confirm it didn't crash on startup.
    """
    params = params if params is not None else VisionParams()
    existing = read_pid()
    if existing is not None and is_alive(existing):
        return procsup.already_running(
            existing, log_path=log_file(), fields={"transport": transport}
        )

    if transport == "http":
        procsup.require_daemon_health(base_url, timeout, health_ok=health_ok)

    cmd = build_run_command(transport=transport, base_url=base_url, timeout=timeout, params=params)
    fields: dict[str, object] = {"transport": transport}
    if transport == "http":
        fields["url"] = base_url
    # clear_pid_on_exit=False: unlike sleep/embody, a vision loop that dies in
    # the grace window (e.g. no camera available) leaves its pid file for the
    # stale-pid path to clear on the next status/stop.
    return procsup.spawn_tracked(
        cmd=cmd,
        pid_path=pid_file(),
        log_path=log_file(),
        labels=_LABELS,
        fields=fields,
        clear_pid_on_exit=False,
    )


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the vision loop this CLI started: SIGTERM, then SIGKILL if it lingers.

    SIGTERM lets the loop ease the robot back to center before it exits. Guards
    against PID reuse (never signals a process that isn't our loop).
    """
    return procsup.stop_tracked(
        pid_path=pid_file(),
        labels=_LABELS,
        timeout=timeout,
        is_alive=is_alive,
        is_ours=_is_our_process,
        wait_gone=_wait_gone,
    )


def restart(**start_kwargs) -> dict[str, object]:
    """Stop the tracked loop (if any) then start a fresh one (re-reads code/params)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Report the vision process state and whether its target daemon answers."""
    pid = read_pid()
    return {
        "process": procsup.process_state(pid, is_alive=is_alive),
        "pid": pid,
        "daemon": "healthy" if health_ok(base_url, timeout) else "unreachable",
        "url": base_url,
        "log": str(log_file()),
    }
