"""Run the ``sleep`` loop as a tracked background process.

The supervisor half of the ``sleep`` noun — a sibling of
:mod:`reachy.vision.supervisor`, but for the sleep loop (``reachy sleep run``)
instead. ``start`` / ``stop`` / ``restart`` / ``status`` manage a detached
background process tracked with a PID + log file under the same per-user state
dir the daemon, demo-mode and vision all use. ``start`` re-invokes this very CLI
(``python -m reachy sleep run``) so the loop keeps running after the launching
command returns.

This module owns its own ``sleep.pid`` / ``sleep.log`` filenames so the loops
can run side-by-side, and it reuses the *generic* process primitives from
:mod:`reachy.daemon` (``state_dir`` / ``is_alive`` — PID-file location and
liveness). The process-management mechanics (PID-file write/read, detached
spawn, signal-based stop, PID-reuse guard) live once in :mod:`reachy.procsup`
and are shared with every sibling supervisor; what stays here is what is
genuinely sleep's — the filenames, the ``sleep run`` argv, the deliberate
ABSENCE of a daemon-health preflight (below), and the wording of every message.
(The ``listen`` supervisor this module was originally a sibling of retired with
its noun; nothing here had to change, which is the point.)

Pure standard library (``subprocess`` / ``signal`` / ``os``, reached through
:mod:`reachy.procsup`): the loop talks to the robot over the existing
transport, so this adds **no** third-party runtime dependency.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reachy import procsup

# Reuse the daemon's generic process primitives + state dir so the sleep loop,
# the vision loop, demo-mode, and the daemon share one bookkeeping
# location.
from reachy.daemon import is_alive, state_dir
from reachy.robot.transport import DEFAULT_BASE_URL, DEFAULT_TIMEOUT

# Seconds to wait after SIGTERM before escalating to SIGKILL. Owned per
# supervisor rather than shared: it is an operator-facing default (the
# ``--timeout`` flag reads it), not a mechanic.
DEFAULT_STOP_TIMEOUT = 10.0

# The exact argv tokens the spawn line below carries — see
# reachy.procsup.has_argv_tokens for why this is a token set and not a substring.
_IDENTITY_TOKENS = ("reachy", "sleep")

_LABELS = procsup.ProcessLabels(
    tracked="sleep",
    launch="sleep",
    exited="sleep",
    reused="a sleep loop",
    signalled="sleep",
)


def pid_file() -> Path:
    return state_dir() / "sleep.pid"


def log_file() -> Path:
    return state_dir() / "sleep.log"


def read_pid() -> int | None:
    """Return the tracked PID, or ``None`` if the file is absent or unparseable."""
    return procsup.read_pid(pid_file())


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses."""
    return procsup.poll_until_gone(pid, timeout, is_alive=is_alive)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually a sleep loop?

    The spawn line is ``<python> -m reachy sleep run ...``, so ``reachy`` and
    ``sleep`` each appear as their OWN argv element — see
    :data:`_IDENTITY_TOKENS` and :func:`reachy.procsup.has_argv_tokens` for the
    exact-token rule. This is the noun the substring form (issue #136) failed
    worst on: ``python -c "import time; time.sleep(60)"`` run from a checkout
    whose path contains ``reachy`` matched BOTH halves of the old test.
    """
    return procsup.has_argv_tokens(pid, _IDENTITY_TOKENS)


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
    consistency with the sibling supervisors' approach.

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

    Unlike the daemon/vision ``start``, there is no HTTP health preflight here:
    the sleep loop surfaces its own clean exit-2 ``CliError`` if unreachable —
    so a spawned loop that can't reach the robot exits during the grace window and
    is reported as ``exited``.

    ``no_audio_wake`` forwards ``--no-audio-wake`` into the spawned ``sleep run``
    command — pat-only / quiet-room mode where speech/snap/DoA stimuli are ignored.
    """
    existing = read_pid()
    if existing is not None and is_alive(existing):
        return procsup.already_running(
            existing, log_path=log_file(), fields={"transport": transport}
        )

    cmd = build_run_command(
        transport=transport,
        base_url=base_url,
        timeout=timeout,
        ticks=ticks,
        idle_timeout=idle_timeout,
        no_audio_wake=no_audio_wake,
    )
    # clear_pid_on_exit: a loop that dies in the grace window (e.g. robot
    # unreachable) must not leave the pid file we just wrote behind, or
    # `status`/`stop` would report a stale pid.
    return procsup.spawn_tracked(
        cmd=cmd,
        pid_path=pid_file(),
        log_path=log_file(),
        labels=_LABELS,
        fields={"transport": transport},
        clear_pid_on_exit=True,
    )


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the sleep loop this CLI started: SIGTERM, then SIGKILL if it lingers.

    Guards against PID reuse (never signals a process that isn't our loop).
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
    """Stop the tracked loop (if any) then start a fresh one (re-reads code/flags)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status() -> dict[str, object]:
    """Report the sleep loop process state (PID + liveness)."""
    pid = read_pid()
    return {
        "process": procsup.process_state(pid, is_alive=is_alive),
        "pid": pid,
        "log": str(log_file()),
    }
