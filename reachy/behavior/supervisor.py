"""Run the behavior engine as a tracked background process.

Mirrors :mod:`reachy.alive`'s supervisor half (and the ``daemon`` noun): spawn
``python -m reachy behavior engine run`` detached, track it with a PID file + log
under ``state_dir()/behavior``, and reconcile the OS process with the daemon's
health route. One long-lived engine owns motion; ``behavior run`` auto-starts it
if absent, and all controllers talk to it through the command spool.

Pure standard library (``subprocess`` / ``signal`` / ``os``, reached through
:mod:`reachy.procsup`); the same PID-reuse guarding and SIGTERM→SIGKILL
escalation as the daemon supervisor.

The mechanics themselves (PID-file write/read, detached spawn, signal-based
stop, PID-reuse guard) live once in :mod:`reachy.procsup` and are shared with
every sibling supervisor. What is genuinely the engine's stays here: the
``behavior/engine.pid`` / ``engine.log`` paths under ``behavior_dir()`` rather
than the plain state dir, the ``behavior engine run`` argv, the daemon-health
preflight for the ``http`` transport, the ``http`` DEFAULT (every sibling
defaults to ``sdk``), :func:`ensure_running`, and the deliberate ABSENCE of a
``restart`` verb.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reachy import procsup
from reachy.behavior.control import behavior_dir
from reachy.daemon import health_ok, is_alive
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

# Seconds to wait after SIGTERM before escalating to SIGKILL. Owned per
# supervisor rather than shared: it is an operator-facing default (the
# ``--timeout`` flag reads it), not a mechanic.
DEFAULT_STOP_TIMEOUT = 10.0

# The exact argv tokens the spawn line below carries. Unlike the sibling
# supervisors this pair deliberately does NOT include ``reachy``: requiring
# ``behavior`` AND ``engine`` already excludes a bare ``reachy behavior <verb>``
# CLI call, and it keeps the guard correct for an engine launched through the
# ``reachy`` console script too (there argv[0] is a PATH, not the bare token).
# See reachy.procsup.has_argv_tokens for the exact-token rule.
_IDENTITY_TOKENS = ("behavior", "engine")

_LABELS = procsup.ProcessLabels(
    tracked="engine",
    launch="the behavior engine",
    exited="engine",
    reused="a behavior engine",
    signalled="behavior engine",
)


def pid_file() -> Path:
    return behavior_dir() / "engine.pid"


def log_file() -> Path:
    return behavior_dir() / "engine.log"


def read_pid() -> int | None:
    return procsup.read_pid(pid_file())


def _wait_gone(pid: int, timeout: float) -> bool:
    return procsup.poll_until_gone(pid, timeout, is_alive=is_alive)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a behavior engine?

    The engine's spawn line is ``... -m reachy behavior engine run``; both
    :data:`_IDENTITY_TOKENS` must appear as their OWN argv elements, so a bare
    ``reachy behavior <verb>`` CLI call — or any unrelated process whose command
    line merely CONTAINS ``behavior`` (issue #136) — is never signalled under
    PID reuse.
    """
    return procsup.has_argv_tokens(pid, _IDENTITY_TOKENS)


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
        return procsup.already_running(
            existing, log_path=log_file(), fields={"transport": transport}
        )

    if transport == "http":
        procsup.require_daemon_health(base_url, timeout, health_ok=health_ok)

    cmd = build_run_command(
        transport=transport,
        base_url=base_url,
        timeout=timeout,
        compose_hz=compose_hz,
        energy=energy,
        base_layer=base_layer,
        settle=settle,
    )
    fields: dict[str, object] = {"transport": transport}
    if transport == "http":
        fields["url"] = base_url
    # clear_pid_on_exit=False: unlike sleep/embody, an engine that dies in the
    # grace window leaves its pid file for the stale-pid path to clear on the
    # next status/stop.
    return procsup.spawn_tracked(
        cmd=cmd,
        pid_path=pid_file(),
        log_path=log_file(),
        labels=_LABELS,
        fields=fields,
        clear_pid_on_exit=False,
    )


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the engine this CLI started: SIGTERM (so it settles), then SIGKILL if it lingers."""
    return procsup.stop_tracked(
        pid_path=pid_file(),
        labels=_LABELS,
        timeout=timeout,
        is_alive=is_alive,
        is_ours=_is_our_process,
        wait_gone=_wait_gone,
    )


def status(
    *, base_url: str = DEFAULT_BASE_URL, timeout: float = DEFAULT_TIMEOUT
) -> dict[str, object]:
    """Report the engine process state and whether its target daemon answers."""
    pid = read_pid()
    return {
        "process": procsup.process_state(pid, is_alive=is_alive),
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
