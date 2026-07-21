"""Pat-active file flag — now bench-local bookkeeping, not a coordination channel.

Publishes a simple file-system flag that signals whether a ``pat`` reaction is
currently in progress.  The flag lives under the same per-user state directory
that every other piece of bookkeeping in this project uses (daemon PID file,
the sleep-active flag, the ``sleep`` supervisor's PID file, …).

This mirrors :mod:`reachy.motion.sleep_signal` *exactly* in shape — only the
flag file name and the symbol names differ.  The :func:`pat_active` context
manager is the canonical way to set and clear the flag; the lower-level
:func:`write` / :func:`clear` / :func:`is_active` functions are exposed for
callers that only need to *read* the signal.

**Scope, after task t22.**  The flag's one cross-process reader was the
always-alive ``listen`` idle wander, which paused entirely while a pat reaction
owned the motion.  That reader went with the ``listen`` NOUN, so **no other
process consults this flag any more**: its only remaining read is ``pat run``'s
own idempotent cleanup, in the very process that wrote it.  The module is kept
rather than deleted because that writer and reader are both live code in a
surviving noun; treat it as per-noun bookkeeping, not as arbitration.

Nothing regressed by that loss.  Arbitration against a running behavior engine
— the case that actually matters on the robot — is not advisory and never was
this flag's job: ``pat run`` calls
:func:`reachy.behavior.liveness.refuse_if_engine_live` before it constructs a
transport, so it *refuses to start* beside a live engine rather than yielding to
it.  Live patting reaches the robot through the engine's own pat sense
(:mod:`reachy.behavior.pat_sense`), which never read this flag; standalone
``pat run`` is the isolated bench check.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import contextlib
from pathlib import Path
from typing import Generator

# Reuse the single source-of-truth state-dir resolver so every subsystem (the
# daemon, the sleep supervisor, the sleep flag, and this flag) all land in the
# same directory.
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
