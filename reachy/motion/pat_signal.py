"""Pat-active file flag.

Publishes a simple file-system flag that signals whether a ``pat`` reaction is
currently in progress.  The flag lives under the same per-user state directory
that every other piece of bookkeeping in this project uses (daemon PID file,
listen/think supervisor PID files, the cognition-active flag, …).

This mirrors :mod:`reachy.speech.cognition_signal` *exactly* in shape — only the
flag file name and the symbol names differ.  The :func:`pat_active` context
manager is the canonical way to set and clear the flag; the lower-level
:func:`write` / :func:`clear` / :func:`is_active` functions are exposed for
callers (e.g. the ``listen`` idle producer) that only need to *read* the signal.

While the flag is present the always-alive ``listen`` idle wander pauses
entirely so the pat lean/snuggle reaction owns the motion — a scratch breaks
stillness.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# Reuse the single source-of-truth state-dir resolver so every subsystem (the
# daemon, the listen supervisor, the think supervisor, the cognition flag, and
# now this flag) all land in the same directory.
from reachy.daemon import state_dir

# Name of the flag file inside the state dir.
_FLAG_NAME = "pat_active.flag"


def pat_flag_path() -> Path:
    """Return the path of the pat-active flag file.

    The parent directory is resolved (and created) by :func:`reachy.daemon.state_dir`,
    which honours the following precedence — *exactly* as the rest of the repo does:

    1. ``$REACHY_STATE_DIR`` (tests inject this for isolation)
    2. ``$XDG_STATE_HOME/reachy``
    3. ``~/.local/state/reachy``
    """
    return state_dir() / _FLAG_NAME


def write() -> None:
    """Write (or overwrite) the pat-active flag.

    Idempotent: calling this when the flag already exists is safe.  The parent
    directory is created automatically if it does not exist yet.
    """
    path = pat_flag_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("1", encoding="utf-8")


def clear() -> None:
    """Remove the pat-active flag.

    Idempotent: if the flag file is absent (never written, already cleared, or
    removed by an external process) this is a no-op and does **not** raise.
    """
    try:
        pat_flag_path().unlink()
    except FileNotFoundError:
        pass


def is_active() -> bool:
    """Return ``True`` if the pat-active flag file currently exists."""
    return pat_flag_path().exists()


@contextlib.contextmanager
def pat_active() -> Generator[None, None, None]:
    """Context manager: set the pat-active flag on enter, clear it on exit.

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
