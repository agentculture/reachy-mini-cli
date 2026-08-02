"""Shared mechanics for this CLI's tracked background processes.

Four nouns run a foreground loop as a detached background process tracked by a
PID + log file: ``sleep`` (:mod:`reachy.sleep.supervisor`), ``vision``
(:mod:`reachy.vision.supervisor`), the ``behavior`` engine
(:mod:`reachy.behavior.supervisor`) and the ``embody`` layer
(:mod:`reachy.embody.supervisor`). Each of them used to own a verbatim copy of
the same machinery — read/clear a PID file, poll a PID until it is gone, decide
whether a tracked PID is still OURS, spawn detached with a startup grace window,
escalate SIGTERM→SIGKILL — and that copy-paste is exactly why issue #136 existed
in four places at once: the PID-identity guard was fixed in ONE supervisor
(``embody``) and stayed broken in the other three, because there was nothing
shared to fix.

This module is the single owner of that machinery. It is deliberately
**parameterised, not flattened** — every genuine per-noun difference stays in
the noun's own supervisor and is passed in here:

* **Identity tokens.** Each noun names the exact argv tokens its spawn line
  carries (see :func:`has_argv_tokens`); nothing here guesses them.
* **Wording.** :class:`ProcessLabels` carries the five operator-facing strings
  the messages differ by, so ``sleep`` still says "no tracked sleep pid" and the
  engine still says "no tracked engine pid".
* **Liveness / identity / wait seams.** ``is_alive`` / ``is_ours`` /
  ``wait_gone`` are *injected callables*, never imported here. That keeps
  :mod:`reachy.daemon` out of this module's imports, and — just as important —
  keeps each supervisor's module-level ``is_alive`` / ``_is_our_process`` /
  ``_wait_gone`` the name a test monkeypatches, because the supervisor resolves
  them at call time and hands them over.
* **Everything else** — the daemon-health preflight (``vision`` and the
  ``behavior`` engine do it, ``sleep`` and ``embody`` deliberately do not), the
  extra result fields (``transport`` / ``url``), whether a failed start clears
  the PID file, and whether the noun has ``restart`` at all — stays in the
  supervisor. This module offers pieces; it does not impose a lifecycle.

Pure standard library (``subprocess`` / ``signal`` / ``os``), like every
supervisor it serves. It knows nothing about transports, robots or systemd.
"""

from __future__ import annotations

import os
import signal
import subprocess  # nosec B404 - only ever spawns the argv its caller built (this trusted CLI)
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from reachy.cli._errors import EXIT_ENV_ERROR, CliError

#: Grace window after spawning before we trust the loop came up (vs crashed).
START_GRACE = 0.4
#: How finely :func:`poll_until_gone` polls for the process to exit.
SLEEP_SLICE = 0.25
#: Seconds to keep polling after SIGKILL before declaring the stop a failure.
KILL_GRACE = 2.0
#: Shared status literal (one definition; avoids Sonar S1192 duplicate-string).
STATUS_NOT_RUNNING = "not running"

#: Remediation shared by every "the spawn itself failed" error.
_LAUNCH_REMEDIATION = "check the Python interpreter is usable and the state dir is writable"
#: Remediation shared by every "the daemon isn't there" preflight refusal.
_DAEMON_REMEDIATION = (
    "start it first with 'reachy daemon start', or point --base-url / "
    "REACHY_BASE_URL at a running daemon (use --transport sdk to drive "
    "the robot in-process instead)"
)


@dataclass(frozen=True)
class ProcessLabels:
    """The operator-facing wording one supervised process differs by.

    Five strings, because the four supervisors genuinely word their messages
    five different ways and flattening them would change what an operator reads:

    * ``tracked`` — "no tracked **sleep** pid" / "no tracked **engine** pid".
    * ``launch`` — "failed to launch **vision** (…)" / "failed to launch **the
      behavior engine** (…)".
    * ``exited`` — "**embody** exited during startup; see …".
    * ``reused`` — "tracked pid is no longer **a sleep loop** (reused); left
      untouched" (includes its own article, since it is "a vision loop" but
      "an embody layer").
    * ``signalled`` — "not permitted to stop **behavior engine** pid 42".
    """

    tracked: str
    launch: str
    exited: str
    reused: str
    signalled: str


def read_pid(path: Path) -> int | None:
    """Return the PID recorded in *path*, or ``None`` if absent/unparseable."""
    try:
        text = path.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def clear_pid(path: Path) -> None:
    """Remove the PID file at *path*; a missing file is already the goal."""
    try:
        path.unlink()
    except FileNotFoundError:
        pass


def poll_until_gone(
    pid: int,
    timeout: float,
    *,
    is_alive: Callable[[int], bool],
    slice_seconds: float = SLEEP_SLICE,
) -> bool:
    """Poll until *pid* is gone or *timeout* elapses.

    ``is_alive`` is injected rather than imported so the caller's own
    module-level liveness function (which tests monkeypatch) stays authoritative.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if not is_alive(pid):
            return True
        time.sleep(slice_seconds)
    return not is_alive(pid)


def has_argv_tokens(pid: int, tokens: Iterable[str]) -> bool:
    """Is *pid*'s command line one of ours — does its argv carry every token?

    The one guard that makes "signal ONLY our own process" true under PID reuse.
    On Linux it reads ``/proc/<pid>/cmdline``; if ``/proc`` is unavailable we
    cannot verify, so we trust the PID file and return ``True``. If ``/proc``
    exists but the process is gone or clearly isn't ours, we return ``False`` so
    a caller never signals an unrelated PID.

    **Matched as EXACT argv tokens (issue #136), never as substrings of the
    joined command line.** ``/proc/<pid>/cmdline`` is NUL-separated, so splitting
    it recovers the real argv. Three of the four supervisors used to flatten it
    and run substring tests instead — which also scan the interpreter PATH and
    every argument, so ANY process launched from a checkout whose directory name
    contains the noun already matched::

        /home/…/git/reachy-mini-cli/.venv/bin/python3 -c "import time; time.sleep(60)"

    contains ``reachy`` (from the path) and ``sleep`` (from the code), and so
    read as "this is the sleep loop" to ``sleep``'s guard. A guard that exists
    *solely* to stop ``stop`` from SIGKILLing an unrelated process after PID
    reuse must not be satisfiable by a directory name. Splitting on NUL costs
    nothing and makes the guard mean what it says.

    Note the direction of the residual risk: exact-token matching can only be
    *stricter* than the substring form, so its failure mode is refusing to kill
    a process that was ours (reported as ``reused``, PID file cleared, operator
    told) — never killing one that was not.
    """
    if not Path("/proc").is_dir():
        return True
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return False
    argv = {token.decode("utf-8", "replace") for token in raw.split(b"\x00") if token}
    return all(token in argv for token in tokens)


def process_state(pid: int | None, *, is_alive: Callable[[int], bool]) -> str:
    """``stopped`` (no PID file) / ``running`` / ``stale`` (PID file, dead PID)."""
    if pid is None:
        return "stopped"
    return "running" if is_alive(pid) else "stale"


def already_running(
    pid: int, *, log_path: Path, fields: dict[str, object] | None = None
) -> dict[str, object]:
    """The idempotent ``start`` result for a loop that is already up."""
    return {"status": "already-running", "pid": pid, **(fields or {}), "log": str(log_path)}


def require_daemon_health(
    base_url: str, timeout: float, *, health_ok: Callable[[str, float], bool]
) -> None:
    """Refuse to spawn when the daemon's health route does not answer.

    Only ``vision`` and the ``behavior`` engine call this: their ``http``
    transport has nothing to talk to otherwise. ``sleep`` and ``embody``
    deliberately do NOT — see their own module docstrings.
    """
    if health_ok(base_url, timeout):
        return
    raise CliError(
        code=EXIT_ENV_ERROR,
        message=f"no Reachy daemon reachable at {base_url}",
        remediation=_DAEMON_REMEDIATION,
    )


def spawn_tracked(
    *,
    cmd: list[str],
    pid_path: Path,
    log_path: Path,
    labels: ProcessLabels,
    fields: dict[str, object] | None = None,
    clear_pid_on_exit: bool,
    grace: float = START_GRACE,
) -> dict[str, object]:
    """Spawn *cmd* detached, record its PID, and grace-check that it survived.

    Output goes to *log_path* (appended), the child gets its own session so it
    outlives the launching CLI, and ``stdin`` is closed. After ``grace`` seconds
    a child that has already exited is reported as ``exited`` with its return
    code — a failed startup, not a running loop.

    ``clear_pid_on_exit`` is a genuine per-noun difference, not a default worth
    unifying here: ``sleep`` / ``embody`` clear the PID file they just wrote so
    ``status`` / ``stop`` do not then report a stale PID, while ``vision`` /
    the ``behavior`` engine leave it for the stale-PID path to clean up.
    """
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
            message=f"failed to launch {labels.launch} ({cmd[0]}): {err}",
            remediation=_LAUNCH_REMEDIATION,
        ) from err
    pid_path.write_text(str(proc.pid), encoding="utf-8")

    time.sleep(grace)
    result: dict[str, object] = {
        "status": "started",
        "pid": proc.pid,
        **(fields or {}),
        "log": str(log_path),
    }
    if proc.poll() is not None:
        if clear_pid_on_exit:
            clear_pid(pid_path)
        result["status"] = "exited"
        result["exit_code"] = proc.returncode
        result["note"] = f"{labels.exited} exited during startup; see {log_path}"
    return result


def stop_tracked(
    *,
    pid_path: Path,
    labels: ProcessLabels,
    timeout: float,
    is_alive: Callable[[int], bool],
    is_ours: Callable[[int], bool],
    wait_gone: Callable[[int, float], bool],
) -> dict[str, object]:
    """SIGTERM the tracked process, escalating to SIGKILL if it lingers.

    The PID file is the ONLY authority: this never scans for a process by name
    and never signals a process group, so a sibling loop (or anything else on
    the box) is untouched by construction rather than by convention. ``is_ours``
    is the PID-reuse guard — normally the noun's ``_is_our_process``, which
    delegates to :func:`has_argv_tokens`.

    Never claims success it cannot confirm: a process that survives SIGKILL
    raises a :class:`CliError` rather than reporting ``stopped``.
    """
    pid = read_pid(pid_path)
    if pid is None:
        return {"status": STATUS_NOT_RUNNING, "note": f"no tracked {labels.tracked} pid"}
    if not is_alive(pid):
        clear_pid(pid_path)
        return {"status": STATUS_NOT_RUNNING, "pid": pid, "note": "stale pid cleared"}
    if not is_ours(pid):
        # The recorded pid was recycled by an unrelated process — do NOT signal it.
        clear_pid(pid_path)
        return {
            "status": STATUS_NOT_RUNNING,
            "pid": pid,
            "note": f"tracked pid is no longer {labels.reused} (reused); left untouched",
        }
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        clear_pid(pid_path)
        return {"status": STATUS_NOT_RUNNING, "pid": pid, "note": "process already gone"}
    except PermissionError as err:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"not permitted to stop {labels.signalled} pid {pid}",
            remediation="stop it as the owning user",
        ) from err
    signaled = "SIGTERM"
    gone = wait_gone(pid, timeout)
    if not gone:
        try:
            os.kill(pid, signal.SIGKILL)
            signaled = "SIGKILL"
        except ProcessLookupError:
            gone = True
        if not gone:
            gone = wait_gone(pid, KILL_GRACE)
    if not gone:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message=f"failed to stop {labels.signalled} pid {pid}: still alive after SIGKILL",
            remediation="inspect and terminate the process manually",
        )
    clear_pid(pid_path)
    return {"status": "stopped", "pid": pid, "signal": signaled}
