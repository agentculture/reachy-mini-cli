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

Process-management mechanics (PID-file write/read, detached spawn,
signal-based stop, PID-reuse guard) are NOT re-implemented here: they live once
in :mod:`reachy.procsup`, which every sibling supervisor also cites. What stays
in this module is only what is genuinely the layer's — its PID/log filenames,
its spawn argv, its wording, and the two structural choices called out above.
(This module was where the exact-argv-token PID guard was first written; issue
#136 was the other three supervisors still carrying the substring form, which
is what moving it into :mod:`reachy.procsup` fixes for good.)

Pure standard library (``subprocess`` / ``signal`` / ``os``, the latter two via
:mod:`reachy.procsup`). This is the one module under :mod:`reachy.embody` that
is EXEMPT from the layer's own "no shell reachable" claim
(``tests/test_embody_redteam.py``): it is the OPERATOR's own control plane for
the layer PROCESS (what a human runs from a terminal), never part of the
tool-dispatch action surface an utterance can reach — nothing in
:mod:`reachy.embody.tools` / ``.engine`` / ``.cues`` / ``.media`` imports this
module, and the redteam suite pins that unreachability by name. Reusing
:func:`reachy.daemon.state_dir` / :func:`reachy.daemon.is_alive` (exactly as
every sibling supervisor does) also means this module names
:mod:`reachy.daemon` directly, the other half of that same exemption.
"""

from __future__ import annotations

import sys
from pathlib import Path

from reachy import procsup
from reachy.daemon import is_alive, state_dir

# Seconds to wait after SIGTERM before escalating to SIGKILL. Owned per
# supervisor (and mirrored by VALUE in reachy.cli._commands.agent) rather than
# shared: it is an operator-facing default, not a mechanic.
DEFAULT_STOP_TIMEOUT = 10.0

# The exact argv tokens the spawn line below carries — see
# reachy.procsup.has_argv_tokens for why this is a token set and not a substring.
_IDENTITY_TOKENS = ("reachy", "embody")

_LABELS = procsup.ProcessLabels(
    tracked="embody",
    launch="the embodiment layer",
    exited="embody",
    reused="an embody layer",
    signalled="embody",
)

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
    return procsup.read_pid(pid_file())


def _wait_gone(pid: int, timeout: float) -> bool:
    """Poll until ``pid`` is gone or ``timeout`` elapses."""
    return procsup.poll_until_gone(pid, timeout, is_alive=is_alive)


def _is_our_process(pid: int) -> bool:
    """Best-effort guard against PID reuse: is ``pid`` actually an embody layer?

    The spawn line is ``<python> -m reachy agent embody ...``, so ``reachy`` and
    ``embody`` each appear as their OWN argv element — see
    :data:`_IDENTITY_TOKENS` and :func:`reachy.procsup.has_argv_tokens` for the
    exact-token rule and why a substring scan (issue #136) could never do this
    job.
    """
    return procsup.has_argv_tokens(pid, _IDENTITY_TOKENS)


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
        return procsup.already_running(existing, log_path=log_file())

    cmd = build_run_command(
        feed=feed,
        media_profile=media_profile,
        spool_dir=spool_dir,
        await_timeout=await_timeout,
        turn_interval=turn_interval,
        mute_during_playback=mute_during_playback,
    )
    # clear_pid_on_exit: a layer that dies in the grace window (a bad flag, an
    # unreadable --feed path) must not leave a pid file behind for status/stop
    # to report as stale.
    return procsup.spawn_tracked(
        cmd=cmd,
        pid_path=pid_file(),
        log_path=log_file(),
        labels=_LABELS,
        clear_pid_on_exit=True,
    )


def stop(*, timeout: float = DEFAULT_STOP_TIMEOUT) -> dict[str, object]:
    """Stop the layer this CLI started: SIGTERM, then SIGKILL if it lingers.

    Guards against PID reuse (never signals a process that isn't our layer) —
    the pid file is the ONLY authority this function consults; it never scans
    for a process by name or signals a process group, so a sibling
    runtime/daemon process (or anything else on the box) is untouched by
    construction, not merely by convention.
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
    """Stop the tracked layer (if any) then start a fresh one (re-reads code/flags)."""
    before = stop()
    result = start(**start_kwargs)
    result["restarted_from"] = before.get("status", "unknown")
    return result


def status() -> dict[str, object]:
    """Report the embody layer's process state (PID + liveness)."""
    pid = read_pid()
    return {
        "process": procsup.process_state(pid, is_alive=is_alive),
        "pid": pid,
        "log": str(log_file()),
    }
