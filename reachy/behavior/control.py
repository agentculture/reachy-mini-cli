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

Namespaced spools (additive)
-----------------------------
Every directory helper (:func:`commands_dir` / :func:`results_dir` /
:func:`state_file`) and :class:`CommandSpool` itself now take an optional
``namespace`` — a subdirectory under ``behavior/`` that gives a second consumer
(e.g. the intent spool, :mod:`reachy.behavior.intents`) its OWN commands/results
inbox that the base engine's ``_apply_commands`` drain never touches (it only
ever drains the unnamespaced ``behavior/commands/``), and an optional ``root``
that overrides :func:`~reachy.daemon.state_dir` entirely (mainly so a caller —
e.g. an agent tool registration — can point a spool at an isolated directory
without mutating ``REACHY_STATE_DIR``). Calling every helper with no arguments
resolves to EXACTLY the paths this module has always used, so every existing
caller (the engine's own command drain, ``behavior`` CLI commands) is unaffected.

:class:`KindRegistry` is the generic ``kind -> handler`` extension point a
per-tick driver (:class:`~reachy.behavior.engine.TickContext` consumer) uses to
apply namespaced spool commands without this module ever knowing what those
kinds mean — a new command kind (this wave's four intent kinds, a future goto
lane's kinds, ...) registers a handler into a registry instance wherever it is
composed. This module stays a domain-free mechanism: no behavior/intent/goto
semantics live here, only the spool + the registry shape.
"""

from __future__ import annotations

import json
import os
import time
import uuid
from pathlib import Path
from typing import Callable

from reachy.cli._errors import CliError
from reachy.daemon import state_dir

#: A command-kind handler: ``handler(payload, ctx) -> outcome_dict``. ``ctx`` is
#: passed through unopened by :class:`KindRegistry` — typically an engine
#: ``TickContext``-like object exposing ``admit``/``evict``/``active_names``/....
KindHandler = Callable[[dict, object], dict]


def behavior_dir(root: Path | None = None) -> Path:
    """``state_dir()/behavior`` — the engine's bookkeeping root (created on access).

    ``root`` overrides :func:`~reachy.daemon.state_dir` entirely when given
    (e.g. a test or an isolated agent-tool spool); ``None`` (the default)
    resolves exactly as before.
    """
    base = root if root is not None else state_dir()
    d = base / "behavior"
    d.mkdir(parents=True, exist_ok=True)
    return d


def commands_dir(namespace: str = "", *, root: Path | None = None) -> Path:
    base = behavior_dir(root)
    d = (base / namespace / "commands") if namespace else (base / "commands")
    d.mkdir(parents=True, exist_ok=True)
    return d


def results_dir(namespace: str = "", *, root: Path | None = None) -> Path:
    base = behavior_dir(root)
    d = (base / namespace / "results") if namespace else (base / "results")
    d.mkdir(parents=True, exist_ok=True)
    return d


def state_file(namespace: str = "", *, root: Path | None = None) -> Path:
    base = behavior_dir(root)
    return (base / namespace / "state.json") if namespace else (base / "state.json")


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


def submit(op: str, *, namespace: str = "", root: Path | None = None, **fields: object) -> str:
    """Drop a command for the engine; return its ``cmd_id`` for :func:`await_result`."""
    cmd_id = uuid.uuid4().hex
    payload = {"cmd_id": cmd_id, "op": op, **fields}
    # Time-ns prefix keeps the spool in submission order under sorted() drain.
    name = f"{time.time_ns()}-{cmd_id}.json"
    _atomic_write(commands_dir(namespace, root=root) / name, json.dumps(payload))
    return cmd_id


def read_state(*, namespace: str = "", root: Path | None = None) -> dict | None:
    """The engine's last-published state, or ``None`` if it hasn't written one."""
    try:
        return json.loads(state_file(namespace, root=root).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None


def await_result(
    cmd_id: str,
    *,
    namespace: str = "",
    root: Path | None = None,
    timeout: float = 1.0,
    poll: float = 0.02,
    sleep=time.sleep,
) -> dict | None:
    """Poll for the engine's result for ``cmd_id`` until it lands or ``timeout``.

    Returns the result dict (and removes the result file), or ``None`` if the
    engine didn't answer in time (e.g. it isn't running).
    """
    deadline = time.monotonic() + timeout
    path = results_dir(namespace, root=root) / f"{cmd_id}.json"
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
    """The engine's view of the spool: drain commands, publish results + state.

    ``namespace`` / ``root`` are the same overrides :func:`commands_dir` /
    :func:`results_dir` / :func:`state_file` take — a bare ``CommandSpool()``
    resolves to exactly the paths this class has always used (the base engine's
    command spool). Passing ``namespace="intents"`` (or any other name) gives a
    second consumer its own inbox that this spool's ``drain()`` never sees and
    vice versa — two independent spools sharing nothing but the same
    ``behavior/`` root and the same ``state.json`` write target when
    ``namespace=""``.
    """

    def __init__(self, *, namespace: str = "", root: Path | None = None) -> None:
        self._namespace = namespace
        self._root = root

    def _commands_dir(self) -> Path:
        return commands_dir(self._namespace, root=self._root)

    def _results_dir(self) -> Path:
        return results_dir(self._namespace, root=self._root)

    def _state_file(self) -> Path:
        return state_file(self._namespace, root=self._root)

    def reset(self) -> None:
        """Clear stale commands/results from a prior run (called on engine start)."""
        # Unlinking each entry right after it is yielded is safe (scandir keeps its
        # position past already-returned entries), so no need to materialise first.
        for d in (self._commands_dir(), self._results_dir()):
            for p in d.iterdir():
                _safe_unlink(p)
        _safe_unlink(self._state_file())

    def drain(self) -> list[dict]:
        """Read, delete, and return all pending commands in submission order.

        Unreadable or half-written files are removed and skipped — a torn write is
        impossible (atomic rename) so this only guards against genuine garbage.
        """
        try:
            files = sorted(p for p in self._commands_dir().iterdir() if p.suffix == ".json")
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
        _atomic_write(self._results_dir() / f"{cmd_id}.json", json.dumps(result))

    def write_state(self, state: dict) -> None:
        _atomic_write(self._state_file(), json.dumps(state))

    def read_state(self) -> dict | None:
        """This spool's last-published state, or ``None`` if it hasn't written one.

        The instance-method mirror of the free :func:`read_state` function —
        added so a driver holding a :class:`CommandSpool` (e.g.
        :class:`~reachy.behavior.intents.IntentDriver`'s reference to the MAIN,
        unnamespaced spool) can read-modify-write additively through one object.
        """
        try:
            return json.loads(self._state_file().read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return None


# --------------------------------------------------------------------------- #
# KindRegistry — the extension point for namespaced-spool command kinds       #
# --------------------------------------------------------------------------- #


class KindRegistry:
    """An extensible ``kind -> handler`` mapping for engine-side command drivers.

    A generic, domain-free registry: register a handler for a command ``kind``
    (its ``op`` field) at composition time, then :meth:`dispatch` a drained
    command to it. New command kinds register themselves into a registry
    instance wherever they are composed — never by editing this module. A
    handler is ``handler(payload, ctx) -> dict`` (an outcome dict, echoed back
    as the spool result); ``ctx`` is whatever the driver holding this registry
    passes through, typically an engine ``TickContext``-like object exposing
    ``admit``/``evict``/``active_names``/....

    :meth:`dispatch` never raises out — an unknown kind, a :class:`CliError`
    from a handler (a clean validation rejection), or any other handler
    exception all resolve to an ``{"ok": False, ...}`` outcome dict, mirroring
    :meth:`reachy.behavior.engine.Engine.apply`'s defensive shape so a single
    bad command can never break a tick.
    """

    def __init__(self) -> None:
        self._handlers: dict[str, KindHandler] = {}

    def register(self, kind: str, handler: KindHandler) -> "KindRegistry":
        """Register *handler* for *kind* (replacing any prior one); returns self."""
        self._handlers[kind] = handler
        return self

    def kinds(self) -> list[str]:
        """The registered kind names, in registration order."""
        return list(self._handlers)

    def dispatch(self, cmd: dict, ctx: object) -> dict:
        """Look up ``cmd``'s kind (its ``op`` field) and apply it defensively."""
        kind = cmd.get("op")
        handler = self._handlers.get(kind)
        if handler is None:
            return {"ok": False, "op": kind, "error": f"unknown kind {kind!r}"}
        try:
            return handler(cmd, ctx)
        except CliError as err:
            detail = err.message if not err.remediation else f"{err.message} ({err.remediation})"
            return {"ok": False, "op": kind, "error": detail}
        except Exception as err:  # noqa: BLE001 - a bad handler never breaks the drain
            return {"ok": False, "op": kind, "error": f"{type(err).__name__}: {err}"}
