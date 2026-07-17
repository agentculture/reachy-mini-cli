"""``behavior reload`` — hot-swap the running engine's rules config between ticks.

The engine's command spool (:mod:`reachy.behavior.control`) is drained straight
into :meth:`reachy.behavior.engine.Engine.apply`, which only understands
``add``/``stop``/``list`` — and ``engine.py`` is frozen for this feature (no new
op can be taught to it here). So a ``reload`` request needs its OWN spool, one a
per-tick rider drains directly via the engine's ONE ``tick_seam`` integration
point (:class:`reachy.behavior.engine.TickContext`), never touching
``Engine.apply`` at all.

This module provides both halves:

* :func:`submit_reload` / :func:`await_result` — the CLI-side writer, an
  atomic-rename spool mirroring :mod:`reachy.behavior.control`'s idiom (a temp
  file in the same directory, then ``os.replace``) but rooted at its own
  ``behavior/reload/`` subtree so it can never be drained by the engine's main
  command loop.
* :class:`ReloadDriver` — the engine-side rider. Wraps a
  :class:`~reachy.behavior.rules.RulesLoader` plus whichever
  :class:`~reachy.behavior.rule_engine.RuleEngine` currently applies. Each tick
  it first drains its own reload spool and, for every pending command, calls
  ``loader.reload()`` (which never raises — see
  :meth:`reachy.behavior.rules.RulesLoader.reload`): an ACCEPTED reload swaps in
  a fresh ``RuleEngine`` built over the new config; a REJECTED reload leaves the
  previous ``RuleEngine`` running untouched and logs why via
  :mod:`reachy.senselog`. It then delegates the tick itself to whichever
  ``RuleEngine`` currently applies, so a ``ReloadDriver`` is a drop-in
  ``tick_seam`` on its own (see :mod:`reachy.cli._commands.behavior`'s
  composition of ``behavior engine run``).

Pure standard library (``json``/``pathlib``/``uuid``/``time``) plus in-package
imports; nothing here touches ``reachy_mini`` or a network.
"""

from __future__ import annotations

import json
import logging
import os
import time
import uuid
from pathlib import Path

from reachy import senselog
from reachy.behavior.rule_engine import STAGE, RuleEngine
from reachy.behavior.rules import RulesLoader
from reachy.daemon import state_dir

logger = logging.getLogger(__name__)

#: The senselog ``source`` this module logs under — distinct from a rule's own
#: ``field`` source (see ``reachy.behavior.rule_engine``), since a reload event
#: isn't about any one sense field, it's about the rules config itself.
SOURCE = "rules"


def reload_dir() -> Path:
    """``state_dir()/behavior/reload`` — the reload spool's root (created on access).

    Deliberately separate from :func:`reachy.behavior.control.behavior_dir`'s
    ``commands``/``results`` subtrees, which the engine's main spool loop drains
    straight into ``Engine.apply`` — a ``reload`` command dropped there would be
    read as an unknown op and rejected before this module ever saw it.
    """
    d = state_dir() / "behavior" / "reload"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _commands_dir() -> Path:
    d = reload_dir() / "commands"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _results_dir() -> Path:
    d = reload_dir() / "results"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _atomic_write(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` atomically — mirrors ``control.py``'s temp+replace idiom."""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}.{uuid.uuid4().hex}")
    tmp.write_text(text, encoding="utf-8")
    os.replace(tmp, path)  # atomic on the same filesystem


def _safe_unlink(path: Path) -> None:
    try:
        path.unlink()
    except FileNotFoundError:
        pass


# --------------------------------------------------------------------------- #
# Writer side — used by the CLI (``behavior reload``)                         #
# --------------------------------------------------------------------------- #


def submit_reload() -> str:
    """Drop a reload command for the running engine; returns its ``cmd_id``.

    Picked up by whichever :class:`ReloadDriver` the running ``behavior engine
    run`` installed, on its next tick — a deterministic between-ticks
    application point, never mid-composition.
    """
    cmd_id = uuid.uuid4().hex
    payload = {"cmd_id": cmd_id}
    # Time-ns prefix keeps the spool in submission order under sorted() drain,
    # matching reachy.behavior.control.submit.
    name = f"{time.time_ns()}-{cmd_id}.json"
    _atomic_write(_commands_dir() / name, json.dumps(payload))
    return cmd_id


def await_result(
    cmd_id: str, *, timeout: float = 1.0, poll: float = 0.02, sleep=time.sleep
) -> dict | None:
    """Poll for the driver's result for ``cmd_id`` until it lands or ``timeout``.

    Returns the result dict (and removes the result file), or ``None`` if no
    running engine answered in time (e.g. ``behavior engine`` isn't up, or is
    running without a ``ReloadDriver`` installed).
    """
    deadline = time.monotonic() + timeout
    path = _results_dir() / f"{cmd_id}.json"
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
# Reader side — the engine-side tick_seam rider                               #
# --------------------------------------------------------------------------- #


class ReloadDriver:
    """A per-tick ``tick_seam`` rider: apply pending reloads, then run the rules.

    Usable directly as ``engine.run(tick_seam=driver)`` — it needs no
    :class:`~reachy.behavior.rule_engine.TickBus` wrapping, since it already
    delegates each tick to its own internal ``RuleEngine``. Construct it once at
    ``behavior engine run`` composition time over a :class:`RulesLoader` whose
    first :meth:`~reachy.behavior.rules.RulesLoader.reload` has already
    succeeded (see :func:`reachy.cli._commands.behavior._boot_tick_seam`) —
    this class only handles *live* reloads, not the initial boot load.
    """

    def __init__(self, loader: RulesLoader, *, id_prefix: str = "rule", lib=None) -> None:
        self._loader = loader
        self._id_prefix = id_prefix
        self._lib = lib
        self._engine = RuleEngine(loader.current, id_prefix=id_prefix, lib=lib)

    @property
    def loader(self) -> RulesLoader:
        """The wrapped :class:`RulesLoader` (exposes ``.path`` / ``.current`` / ``.last_error``)."""
        return self._loader

    def set_active_mode(self, name: str | None) -> None:
        """Swap the active mode on the LIVE rule engine (the intent seam's mode_setter).

        Delegates to the *current* :class:`RuleEngine` — after a live reload the
        engine is replaced and the reload's ``active_mode`` wins, so a mode set
        through this seam does not survive a subsequent reload (documented).
        """
        self._engine.set_active_mode(name)

    def known_modes(self) -> tuple[str, ...]:
        """Mode names the current config declares (the intent seam's validator)."""
        return tuple(self._loader.current.modes)

    def __call__(self, ctx) -> None:
        """The ``tick_seam`` entry point: apply pending reloads, then evaluate rules."""
        for cmd in self._drain():
            self._apply(cmd, ctx.now)
        self._engine(ctx)

    # -- reload spool ------------------------------------------------------- #

    def _drain(self) -> list[dict]:
        try:
            files = sorted(p for p in _commands_dir().iterdir() if p.suffix == ".json")
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

    def _apply(self, cmd: dict, now: float) -> None:
        cmd_id = cmd.get("cmd_id")
        event = str(cmd_id) if cmd_id else "reload"
        self._loader.reload()
        error = self._loader.last_error
        if error is None:
            current = self._loader.current
            self._engine = RuleEngine(current, id_prefix=self._id_prefix, lib=self._lib)
            senselog.stage(
                STAGE,
                SOURCE,
                event,
                f"reload applied path={self._loader.path} react={len(current.react)} "
                f"inhibit={len(current.inhibit)}",
            )
            result = {
                "ok": True,
                "cmd_id": cmd_id,
                "path": str(self._loader.path),
                "react": len(current.react),
                "inhibit": len(current.inhibit),
                "ts": now,
            }
        else:
            senselog.drop(STAGE, SOURCE, event, error)
            result = {
                "ok": False,
                "cmd_id": cmd_id,
                "path": str(self._loader.path),
                "error": error,
                "ts": now,
            }
        if cmd_id:
            _atomic_write(_results_dir() / f"{cmd_id}.json", json.dumps(result))
