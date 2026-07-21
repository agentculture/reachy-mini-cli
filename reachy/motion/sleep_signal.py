"""Sleep-active file flag — the only way to observe a parked robot.

Publishes a simple file-system flag that signals whether the robot is currently
in a sleep/rest state.  The flag lives under the same per-user state directory
that every other piece of bookkeeping in this project uses (daemon PID file,
the ``sleep`` supervisor's PID file, the pat-active flag, …).

This mirrors :mod:`reachy.motion.pat_signal` *exactly* in shape — only the
flag file name and the symbol names differ.  The :func:`asleep` context
manager is the canonical way to set and clear the flag; the lower-level
:func:`write` / :func:`clear` / :func:`is_active` functions are exposed for
callers that only need to *read* the signal.

**Scope, after task t22 — and why this one is load-bearing.**  Unlike its
:mod:`~reachy.motion.pat_signal` twin, this flag keeps a genuine CROSS-PROCESS
reader: ``sleep status`` runs in a different process from ``sleep run`` and the
live state machine is not readable across that boundary, so this flag is the
*only* thing that tells an operator their robot is parked
(``cmd_sleep_status``).  Parking a robot with ``sleep run`` is a wanted
capability, not a test path, so neither this flag nor its writer is vestigial.

What *did* go with the ``listen`` NOUN is the other half: the idle layer that
read this flag as its strongest interrupt and yielded the motion channel to a
sleeping robot.  No shipped process performs that yield any more.  Nothing
regressed in practice — the behavior engine and ``sleep run`` cannot coexist
regardless (they contend for the single-consumer SDK media session long before
an advisory flag would matter, and ``sleep run`` calls
:func:`reachy.behavior.liveness.refuse_if_engine_live` at entry, refusing to
start beside a live engine) — but it does mean parking is reachable only by
stopping the engine and running ``sleep run`` as a separate process, never by
the engine standing down in place.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# Reuse the single source-of-truth state-dir resolver so every subsystem (the
# daemon, the sleep supervisor, the pat flag, and this flag) all land in the
# same directory.
from reachy.daemon import state_dir

# Name of the flag file inside the state dir.
_FLAG_NAME = "sleep_active.flag"


def sleep_flag_path() -> Path:
    """Return the path of the sleep-active flag file.

    The parent directory is resolved (and created) by :func:`reachy.daemon.state_dir`,
    which honours the following precedence — *exactly* as the rest of the repo does:

    1. ``$REACHY_STATE_DIR`` (tests inject this for isolation)
    2. ``$XDG_STATE_HOME/reachy``
    3. ``~/.local/state/reachy``
    """
    return state_dir() / _FLAG_NAME


def write() -> None:
    """Write (or overwrite) the sleep-active flag.

    Idempotent: calling this when the flag already exists is safe.  The parent
    directory is created automatically if it does not exist yet.
    """
    path = sleep_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def clear() -> None:
    """Remove the sleep-active flag.

    Idempotent: if the flag file is absent (never written, already cleared, or
    removed by an external process) this is a no-op and does **not** raise.
    """
    try:
        sleep_flag_path().unlink()
    except FileNotFoundError:
        pass


def is_active() -> bool:
    """Return ``True`` if the sleep-active flag file currently exists."""
    return sleep_flag_path().exists()


@contextlib.contextmanager
def asleep() -> Generator[None, None, None]:
    """Context manager: set the sleep-active flag on enter, clear it on exit.

    Tolerates a stale flag left by a prior crash — ``write()`` is idempotent so
    entering when the flag already exists is fine.  The ``finally`` block ensures
    ``clear()`` is always called, even if the body raises; ``clear()`` itself is
    safe when the file is already absent.
    """
    write()
    try:
        yield
    finally:
        clear()
