"""Run the embodiment layer (``agent embody``) as a tracked background process.

The supervisor half of the ``embody`` verb — a sibling of
:mod:`reachy.sleep.supervisor` (the closest model; see that module's own
docstring), rebuilt here for the layer instead of the sleep loop.
``start`` / ``stop`` / ``restart`` / ``status`` manage a detached background
process tracked with a PID + log file under the same per-user state dir the
daemon, demo-mode, sleep and vision all use. ``start`` re-invokes this very CLI
(``python -m reachy agent embody``) so the layer keeps running after the
launching command returns.

Two things this module deliberately does NOT do, both spelled out because a
sibling supervisor's shape almost suggests them:

* **No daemon-health preflight.** :mod:`reachy.vision.supervisor` and
  :mod:`reachy.behavior.supervisor` probe the daemon's HTTP health route before
  spawning, because their ``http`` transport has nothing to talk to otherwise.
  The layer has no ``--transport`` at all (spec: "a live engine is its
  precondition, not its rival" — it never calls ``refuse_if_engine_live``
  either) — it degrades a dead session/gateway to a named drop and keeps
  running, exactly like :mod:`reachy.sleep.supervisor`'s own loop
  self-reports rather than being preflighted here.
* **No ``restart`` skipped.** Unlike :mod:`reachy.behavior.supervisor` (which
  has none), this mirrors :mod:`reachy.sleep.supervisor` /
  :mod:`reachy.vision.supervisor`: ``restart`` stops the tracked process (if
  any) then starts a fresh one, so an operator picks up new code/flags with
  one command.

Own process-management mechanics (PID-file write/read, detached spawn,
signal-based stop, PID-reuse guard) are kept self-contained here, exactly as
every sibling supervisor keeps its own — a change to one noun's supervisor can
never reach another's.

Pure standard library (``subprocess`` / ``signal`` / ``os``). This is the one
module under :mod:`reachy.embody` that is EXEMPT from the layer's own
"no shell reachable" claim (``tests/test_embody_redteam.py``): it is the
OPERATOR's own control plane for the layer PROCESS (what a human runs from a
terminal), never part of the tool-dispatch action surface an utterance can
reach — nothing in :mod:`reachy.embody.tools` / ``.engine`` / ``.cues`` /
``.media`` imports this module, and the redteam suite pins that unreachability
by name. Reusing :func:`reachy.daemon.state_dir` / :func:`reachy.daemon.is_alive`
(exactly as every sibling supervisor does) also means this module names
:mod:`reachy.daemon` directly, the other half of that same exemption.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only ever re-spawns this trusted CLI (sys.executable -m reachy)
import sys
import time
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.daemon import is_alive, state_dir

# Grace window after spawning before we trust the layer came up (vs crashed).
_START_GRACE = 0.4
# Seconds to wait after SIGTERM before escalating to SIGKILL.
DEFAULT_STOP_TIMEOUT = 10.0
# How finely _wait_gone polls for the process to exit.
_SLEEP_SLICE = 0.25
_STATUS_NOT_RUNNING = "not running"

# Mirrors reachy.cli._commands.agent.DEFAULT_TURN_INTERVAL by VALUE, not by
# import: a library module under reachy/embody/ must never import a CLI
# command module (the dependency runs the other way), and
# reachy.embody.__init__'s own contract keeps every reachy.embody import inside
# a command module's FUNCTION bodies — never at its module scope — so the two
# constants are independently owned, exactly like the sibling supervisors'
# DEFAULT_STOP_TIMEOUT is independently defined three times over already.
DEFAULT_TURN_INTERVAL = 0.5


def pid_file() -> Path:
    return state_dir() / "embody.pid"


def log_file() -> Path:
    return state_dir() / "embody.log"


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
    """Best-effort guard against PID reuse: is ``pid`` actually an embody layer?

    Reads ``/proc/<pid>/cmdline`` on Linux (the spawn line is ``<python> -m
    reachy agent embody ...``, so ``reachy`` and ``embody`` each appear as
    their OWN argv element). If ``/proc`` is unavailable we cannot verify, so
    we trust the pid file. If ``/proc`` exists but the process is gone or
    clearly isn't ours, return False so :func:`stop` never signals an
    unrelated pid — this is the ONE guard that makes "kill ONLY the layer"
    true under PID reuse.

    Matched as EXACT argv tokens, not a substring scan over the raw joined
    cmdline (what the sibling sleep/vision/behavior supervisors do): the
    interpreter path itself (argv[0]) commonly contains substrings like
    ``reachy`` or ``embody`` merely because of where the checkout/venv lives —
    this very worktree is ``.../embody-t12/.venv/bin/python3`` — which a
    substring scan would misread as "this is an embody layer" for ANY process
    that interpreter runs, not only a real one. Exact-token matching only
    a real ``... agent embody ...`` spawn line.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    tokens = {tok.decode("utf-8", "replace") for tok in raw.split(b"\x00") if tok}
    return "reachy" in tokens and "embody" in tokens


def build_run_command(
    *,
    feed: str = "-",
    media_profile: str | None = None,
    spool_dir: str | None = None,
    await_timeout: float = 1.0,
    turn_interval: float = DEFAULT_TURN_INTERVAL,
    mute_during_playback: bool = False,
) -> list[str]:
    """The argv the background process runs: ``python -m reachy agent embody``.

    ``embody`` has no ``run`` sub-verb (unlike ``sleep``/``vision``/``behavior
    engine``) — the bare verb IS the foreground loop
    (:func:`reachy.cli._commands.agent.cmd_agent_embody`), so this supervisor
    re-invokes it directly rather than a nested ``... agent embody run``. Only
    the layer's own OPERATING flags are forwarded — never ``--max-turns`` /
    ``--max-events`` (bounded-run test flags, meaningless for a persistent
    background service, exactly why ``behavior engine start`` does not forward
    ``--max-ticks`` either) and never ``--export`` / ``--log-level`` (the
    background process's stdout/stderr already go to :func:`log_file`, so
    piping a JSONL export feed there would bury it in the log rather than
    serve it to a live consumer).
    """
    cmd = [
        sys.executable,
        "-m",
        "reachy",
        "agent",
        "embody",
        "--feed",
        feed,
        "--await-timeout",
        str(await_timeout),
        "--turn-interval",
        str(turn_interval),
    ]
    if media_profile:
        cmd += ["--media-profile", media_profile]
    if spool_dir:
        cmd += ["--spool-dir", str(spool_dir)]
    if mute_during_playback:
        cmd.append("--mute-during-playback")
    return cmd


def start(
    *,
    feed: str = "-",
    media_profile: str | None = None,
    spool_dir: str | None = None,
    await_timeout: float = 1.0,
    turn_interval: float = DEFAULT_TURN_INTERVAL,
    mute_during_playback: bool = False,
) -> dict[str, object]:
    """Start the embodiment layer in the background (idempotent).

    If a tracked layer is already alive, report ``already-running``. Otherwise
    spawn the layer detached, record its PID + log path, and give it a short
    grace window to confirm it didn't crash on startup.

    No HTTP health preflight here (see the module docstring): the layer
    surfaces its own named drops for a dead session/gateway and keeps running,
    so a spawned layer that cannot reach lobes yet is NOT reported as
    ``exited`` — only an actual early process exit is.
    """
    existing = read_pid()
    if existing is not None and is_alive(existing):
        return {
            "status": "already-running",
            "pid": existing,
            "log": str(log_file()),
        }

    cmd = build_run_command(
        feed=feed,
        media_profile=media_profile,
        spool_dir=spool_dir,
        await_timeout=await_timeout,
        turn_interval=turn_interval,
        mute_during_playback=mute_during_playback,
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
            message=f"failed to launch the embodiment layer ({cmd[0]}): {err}",
            remediation="check the Python interpreter is usable and the state dir is writable",
        ) from err
    pid_file().write_text(str(proc.pid), encoding="utf-8")

    time.sleep(_START_GRACE)
    result: dict[str, object] = {
        "status": "started",
        "pid": proc.pid,
        "log": str(log_path),
    }
    if proc.poll() is not None:
        # Exited within the grace window — startup failed (e.g. a bad flag, an
        # unreadable --feed path). Clear the pid file we just wrote so
        # status/stop don't report a stale pid.
        _clear_pid()
        result["status"] = "exited"
        result["exit_code"] = proc.returncode
        result["note"] = f"embody exited during startup; see {log_path}"
    return result


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the layer this CLI started: SIGTERM, then SIGKILL if it lingers.

    Guards against PID reuse (never signals a process that isn't our layer) —
    the pid file is the ONLY authority this function consults; it never scans
    for a process by name or signals a process group, so a sibling
    runtime/daemon process (or anything else on the box) is untouched by
    construction, not merely by convention.
    """
    pid = read_pid()
    if pid is None:
        return {"status": _STATUS_NOT_RUNNING, "note": "no tracked embody pid"}
    if not is_alive(pid):
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not _is_our_process(pid):
        _clear_pid()
        return {
            "status": _STATUS_NOT_RUNNING,
            "pid": pid,
            "note": "tracked pid is no longer an embody layer (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        _clear_pid()
        return {"status": _STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop embody pid {pid}",
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
            message=f"failed to stop embody pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    _clear_pid()
    return {"status": "stopped", "pid": pid, "signal": signaled}


def restart(**start_kwargs) -> dict[str, object]:
    """Stop the tracked layer (if any) then start a fresh one (re-reads code/flags)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status() -> dict[str, object]:
    """Report the embody layer's process state (PID + liveness)."""
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
