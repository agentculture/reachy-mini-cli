"""Control channel between the CLI and a running engine — a command spool.

The behavior engine runs as a single long-lived process; the CLI adds and stops
behaviors from *separate* invocations. They talk through the filesystem under the
shared state dir — no socket, no port, no thread, so the engine stays
single-threaded and signal handling stays in the main thread (matching the daemon
/ demo-mode idiom):

* ``behavior/commands/`` — the CLI drops one ``<ns>-<id>.json`` command file per
  request (written to a temp name then ``os.replace``-d in, so it is never read
  half-written). The engine drains + deletes them each tick, in submission order.
* ``behavior/results/`` — the engine writes ``<cmd_id>.json`` after applying a
  command, so the CLI can confirm the outcome (admitted / evicted / blocked).
* ``behavior/state.json`` — the engine writes the active set + per-channel
  ownership, which ``behavior status`` reads.

Single reader (the engine), many independent writers (CLI invocations), an
append-only spool with atomic renames — so no locking is needed.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path

from reachy.daemon import state_dir


def behavior_dir() -> Path:
    """``state_dir()/behavior`` — the engine's bookkeeping root (created on access)."""
    d = state_dir() / "behavior"
    d.mkdir(parents=True, exist_ok=True)
    return d


def commands_dir() -> Path:
    d = behavior_dir() / "commands"
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_dir() -> Path:
    d = behavior_dir() / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file() -> Path:
    return behavior_dir() / "state.json"


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically (temp in the same dir, then replace)."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same filesystem


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Writer side — used by the CLI                                               #
# --------------------------------------------------------------------------- #


def submit(op: str, **fields: object) -> str:
    """Drop a command for the engine; return its ``cmd_id`` for :func:`await_result`."""
    cmd_id = uuid.uuid4().hex
    payload = {"cmd_id": cmd_id, "op": op, **fields}
    # Time-ns prefix keeps the spool in submission order under sorted() drain.
    name = f"{time.time_ns()}-{cmd_id}.json"
    _atomic_write(commands_dir() / name, json.dumps(payload))
    return cmd_id


def read_state() -> dict | None:
    """The engine's last-published state, or ``None`` if it hasn't written one."""
    try:
        return json.loads(state_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def await_result(
    cmd_id: str, *, timeout: float = 1.0, poll: float = 0.02, sleep=time.sleep
) -> dict | None:
    """Poll for the engine's result for ``cmd_id`` until it lands or ``timeout``.

    Returns the result dict (and removes the result file), or ``None`` if the
    engine didn't answer in time (e.g. it isn't running).
    """
    deadline = time.monotonic() + timeout
    path = results_dir() / f"{cmd_id}.json"
    while True:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            data = None
        if data is not None:
            _safe_unlink(path)
            return data
        if time.monotonic() >= deadline:
            return None
        sleep(poll)


# --------------------------------------------------------------------------- #
# Reader side — used by the engine                                            #
# --------------------------------------------------------------------------- #


class CommandSpool:
    """The engine's view of the spool: drain commands, publish results + state."""

    def reset(self) -> None:
        """Clear stale commands/results from a prior run (called on engine start)."""
        # Unlinking each entry right after it is yielded is safe (scandir keeps its
        # position past already-returned entries), so no need to materialise first.
        for d in (commands_dir(), results_dir()):
            for p in d.iterdir():
                _safe_unlink(p)
        _safe_unlink(state_file())

    def drain(self) -> list[dict]:
        """Read, delete, and return all pending commands in submission order.

        Unreadable or half-written files are removed and skipped — a torn write is
        impossible (atomic rename) so this only guards against genuine garbage.
        """
        try:
            files = sorted(p for p in commands_dir().iterdir() if p.suffix == ".json")
        except OSError:
            return []
        commands: list[dict] = []
        for path in files:
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                _safe_unlink(path)
                continue
            _safe_unlink(path)
            if isinstance(data, dict):
                commands.append(data)
        return commands

    def write_result(self, cmd_id: str | None, result: dict) -> None:
        if not cmd_id:
            return
        _atomic_write(results_dir() / f"{cmd_id}.json", json.dumps(result))

    def write_state(self, state: dict) -> None:
        _atomic_write(state_file(), json.dumps(state))
