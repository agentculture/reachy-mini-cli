"""Sleep-active file flag.

Publishes a simple file-system flag that signals whether the robot is currently
in a sleep/rest state.  The flag lives under the same per-user state directory
that every other piece of bookkeeping in this project uses (daemon PID file,
listen/think supervisor PID files, the cognition-active flag, the pat-active
flag, …).

This mirrors :mod:`reachy.motion.pat_signal` *exactly* in shape — only the
flag file name and the symbol names differ.  The :func:`asleep` context
manager is the canonical way to set and clear the flag; the lower-level
:func:`write` / :func:`clear` / :func:`is_active` functions are exposed for
callers that only need to *read* the signal.

While the flag is present, other nouns can check :func:`is_active` to suppress
or modify their behaviour, keeping the robot quiescent during sleep.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# Reuse the single source-of-truth state-dir resolver so every subsystem (the
# daemon, the listen supervisor, the think supervisor, the cognition flag, the
# pat flag, and now this flag) all land in the same directory.
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
