"""Cognition-active file flag.

Publishes a simple file-system flag that signals whether the ``think``
cognition loop is currently running.  The flag lives under the same per-user
state directory that every other piece of bookkeeping in this project uses
(daemon PID file, listen/think supervisor PID files, …).

The :func:`cognition_active` context manager is the canonical way to set and
clear the flag; the lower-level :func:`write` / :func:`clear` / :func:`is_active`
functions are exposed for callers (e.g. t6 idle-reduction) that only need to
*read* the signal.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# Reuse the single source-of-truth state-dir resolver so every subsystem (the
# daemon, the listen supervisor, the think supervisor, and now this flag) all
# land in the same directory.
from reachy.daemon import state_dir

# Name of the flag file inside the state dir.
_FLAG_NAME = "think_active.flag"


def cognition_flag_path() -> Path:
    """Return the path of the cognition-active flag file.

    The parent directory is resolved (and created) by :func:`reachy.daemon.state_dir`,
    which honours the following precedence — *exactly* as the rest of the repo does:

    1. ``$REACHY_STATE_DIR`` (tests inject this for isolation)
    2. ``$XDG_STATE_HOME/reachy``
    3. ``~/.local/state/reachy``
    """
    return state_dir() / _FLAG_NAME


def write() -> None:
    """Write (or overwrite) the cognition-active flag.

    Idempotent: calling this when the flag already exists is safe.  The parent
    directory is created automatically if it does not exist yet.
    """
    path = cognition_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def clear() -> None:
    """Remove the cognition-active flag.

    Idempotent: if the flag file is absent (never written, already cleared, or
    removed by an external process) this is a no-op and does **not** raise.
    """
    try:
        cognition_flag_path().unlink()
    except FileNotFoundError:
        pass


def is_active() -> bool:
    """Return ``True`` if the cognition-active flag file currently exists."""
    return cognition_flag_path().exists()


@contextlib.contextmanager
def cognition_active() -> Generator[None, None, None]:
    """Context manager: set the cognition-active flag on enter, clear it on exit.

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
