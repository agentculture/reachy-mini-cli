"""Single-presence-owner service manager for the boot-survival presence stack.

The robot has exactly **one** presence at a time (the single-SDK-owner model in
``CLAUDE.md``): the idle ``demo-mode`` loop, the retiring ``listen --live`` loop,
or the AI-agnostic symbolic runtime (``behavior engine run``, decision c19) may
own the head, never more than one. This manager makes that invariant true across
reboots via systemd ``--user`` units: ``enable(mode)`` installs + enables the
daemon and the *chosen* presence unit and **always disables BOTH siblings**, so
any sequence of enables leaves at most one presence unit enabled.

It generalizes the pattern already proven in
:mod:`reachy.demo_service` (write unit text → ``daemon-reload`` → ``enable --now``
/ ``disable --now``) to four coordinated units — the daemon plus the three
mutually-exclusive presence units — and reuses the *pure* unit-text renderers and
canonical names from :mod:`reachy.service.units` verbatim (it never re-derives a
unit name or re-renders text).

Every side effect goes through **injected seams** so it is exhaustively testable
without touching real systemd or the real ``~/.config/systemd/user``:

* ``run`` — a callable ``(args: list[str]) -> CompletedProcess-ish`` that runs
  one ``systemctl --user <args>`` invocation (the manager prepends ``--user``);
* ``unit_dir`` — the directory unit files are written into (defaults to the real
  XDG user-unit dir);
* ``daemon_health`` — a ``() -> bool`` daemon liveness probe (defaults to the
  real :func:`reachy.daemon.is_robot_live`).

The daemon unit is enabled for **every** mode — the presence units ``Requires=``
/ ``After=`` it (see :mod:`reachy.service.units`). ``disable()`` stops the enabled
presence unit only and **leaves the daemon enabled** — that decision is explicit
(reported as ``daemon="left-enabled"``) rather than silent, because tearing the
daemon down would also break any non-presence client of the robot.
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path
from typing import Callable, Optional, Sequence

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.service import units as _units
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    RUNTIME_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
    runtime_unit_text,
)

# mode name -> (presence unit name, sibling unit names, unit-text renderer).
# Sibling order is deterministic (catalog order, self excluded) so the FIRST
# sibling is stable across calls — kept as ``disabled_sibling`` in enable()'s
# result for backward compatibility, alongside the full ``disabled_siblings``.
_PRESENCE = {
    "demo": (DEMO_UNIT, (LIVE_UNIT, RUNTIME_UNIT), demo_unit_text),
    "live": (LIVE_UNIT, (DEMO_UNIT, RUNTIME_UNIT), live_unit_text),
    "runtime": (RUNTIME_UNIT, (DEMO_UNIT, LIVE_UNIT), runtime_unit_text),
}
_MODES = tuple(_PRESENCE)

# Map a presence unit name back to its mode, for status() read-back.
_UNIT_TO_MODE = {DEMO_UNIT: "demo", LIVE_UNIT: "live", RUNTIME_UNIT: "runtime"}

# All presence units, in catalog order — every non-daemon unit the manager
# coordinates as mutually exclusive.
_PRESENCE_UNITS = (DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT)

# ``mode`` reported by status() when the unit that owns presence is a RETIRED
# one. It is deliberately NOT None: a retired unit still enabled is exactly the
# crash-looping box, and reporting None there is the diagnostic lie this whole
# migration exists to close.
RETIRED_MODE = "retired"

_log = logging.getLogger(__name__)


def _is_enabled(value: str) -> bool:
    """Does an ``is-enabled`` answer mean the unit will start at boot?

    ``systemctl is-enabled`` has a wider vocabulary than the literal
    ``"enabled"``: a unit enabled for this boot only reports ``enabled-runtime``.
    Matching the literal string alone reported ``mode=None`` for such a box — the
    same class of lie as an un-iterated retired unit — so match the family.
    """
    return value.startswith("enabled")


def _default_unit_dir() -> Path:
    """The real XDG user-unit directory (``$XDG_CONFIG_HOME/systemd/user``)."""
    # Imported lazily and reused from demo_service to stay consistent with the
    # existing installer (same dir the demo-mode unit lives in).
    from reachy.demo_service import xdg_config_home

    return xdg_config_home() / "systemd" / "user"


def _default_daemon_health() -> bool:
    """Real daemon liveness probe (restart-safe HTTP health check)."""
    from reachy import daemon

    return daemon.is_robot_live()


class ServiceManager:
    """Enable/disable/status the presence stack with the single-owner invariant.

    See the module docstring for the seam contract. All three public methods are
    deterministic given the injected ``run`` / ``unit_dir`` / ``daemon_health``.
    """

    def __init__(
        self,
        *,
        run: Callable[[list[str]], object],
        unit_dir: Optional[Path] = None,
        daemon_health: Optional[Callable[[], bool]] = None,
    ) -> None:
        self.run = run
        self.unit_dir = Path(unit_dir) if unit_dir is not None else _default_unit_dir()
        self.daemon_health = daemon_health if daemon_health is not None else _default_daemon_health

    # --- systemctl seam helpers -------------------------------------------

    def _systemctl(self, args: list[str]) -> object:
        """Run one ``systemctl --user <args>`` through the injected runner."""
        return self.run(["--user", *args])

    def _require(self, args: list[str], action: str) -> object:
        """Run a mutating systemctl command; raise a clean CliError on failure."""
        result = self._systemctl(args)
        rc = getattr(result, "returncode", 0)
        if rc != 0:
            # Collapse systemctl's (possibly multi-line) output to ONE line — text
            # CLI errors must stay exactly two lines (error: / hint:).
            detail = " ".join(
                (getattr(result, "stderr", "") or getattr(result, "stdout", "") or "").split()
            )
            raise CliError(
                code=EXIT_ENV_ERROR,
                message=f"{action} failed: {detail}" if detail else f"{action} failed",
                remediation="inspect 'systemctl --user status reachy-*.service' on the robot",
            )
        return result

    def _query(self, verb: str, unit: str) -> str:
        """Read-only ``systemctl --user <verb> <unit>``; 'unknown' if unusable.

        ``is-enabled`` / ``is-active`` exit non-zero for the negative answer
        ("disabled" → rc 1, "inactive" → rc 3) while still printing the state on
        stdout, so we read stdout regardless of returncode.
        """
        result = self._systemctl([verb, unit])
        out = (getattr(result, "stdout", "") or "").strip()
        return out or "unknown"

    # --- unit-file writing -------------------------------------------------

    def _write_unit(self, unit: str, text: str) -> Path:
        """Write one unit file into ``unit_dir`` (creating the dir if needed)."""
        self.unit_dir.mkdir(parents=True, exist_ok=True)
        path = self.unit_dir / unit
        path.write_text(text, encoding="utf-8")
        return path

    # --- retired-unit migration -------------------------------------------

    def cleanup_retired_units(self, retired: Optional[Sequence[str]] = None) -> list[str]:
        """Purge every unit name in :data:`reachy.service.units.RETIRED_UNITS`.

        For each retired name, unconditionally:

        1. ``systemctl --user disable --now <unit>`` — kills a running instance
           and drops the ``default.target.wants`` symlink. Unconditional because
           the unit may have been enabled from a path this process cannot see,
           and because a retired unit is by definition a crash loop
           (``Restart=on-failure`` + ``RestartSec=5``) if left running;
        2. unlink ``<unit_dir>/<unit>``;
        3. remove the ``<unit_dir>/<unit>.d/`` drop-in directory — a deployed box
           carries several drop-ins, and unlinking only the unit would leave
           systemd carrying orphaned overrides forward.

        **Best-effort: a cleanup failure never aborts the caller's real work.**
        ``systemctl disable`` on a unit that does not exist genuinely exits
        non-zero (which is why this uses :meth:`_systemctl`, not
        :meth:`_require`), and an unwritable unit dir is not a reason to abort
        the ``enable`` that called us — those are logged and swallowed.

        The ONE exception deliberately allowed through is :class:`CliError`:
        that means the environment itself is unusable (no ``systemctl`` on
        PATH), which every caller must surface as a clean exit-2 rather than
        have masked by a best-effort migration running first.

        Callers run this BEFORE their ``daemon-reload`` so systemd picks up the
        removals in the same pass.

        Returns the retired unit names whose on-disk artifacts were actually
        removed (empty when the box was already clean — the idempotent case).
        """
        names = tuple(_units.RETIRED_UNITS if retired is None else retired)
        removed: list[str] = []
        for unit in names:
            try:
                self._systemctl(["disable", "--now", unit])
            except CliError:
                # The environment is unusable (no systemctl) — never mask that.
                raise
            except Exception as exc:  # pragma: no cover - defensive
                _log.warning("retired-unit cleanup: disabling %s failed: %s", unit, exc)
            touched = False
            path = self.unit_dir / unit
            try:
                if path.is_file() or path.is_symlink():
                    path.unlink()
                    touched = True
            except OSError as exc:
                _log.warning("retired-unit cleanup: removing %s failed: %s", path, exc)
            dropin = self.unit_dir / f"{unit}.d"
            try:
                if dropin.is_dir():
                    shutil.rmtree(dropin)
                    touched = True
            except OSError as exc:
                _log.warning("retired-unit cleanup: removing %s failed: %s", dropin, exc)
            if touched:
                _log.info("retired-unit cleanup: removed %s", unit)
                removed.append(unit)
        return removed

    # --- public API --------------------------------------------------------

    def enable(self, mode: str) -> dict[str, object]:
        """Enable exactly one presence mode (the daemon + that presence unit).

        Writes the daemon and ALL presence unit text, reloads the user manager,
        ``enable --now`` the daemon and chosen presence, and ``disable --now``
        EVERY sibling presence (idempotent — fine if one was already disabled).
        Disabling every sibling is what keeps the three-way single-owner
        invariant true after any sequence of ``enable`` calls (demo/live/runtime
        — at most one enabled, ever).
        """
        if mode not in _PRESENCE:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown presence mode: {mode!r}",
                remediation=f"choose one of: {', '.join(_MODES)}",
            )
        presence_unit, sibling_units, _ = _PRESENCE[mode]

        # RETIRED_UNITS is AUTHORITATIVE over the catalog: a name listed there is
        # gone even if a catalog entry for it lingers. Enabling it is refused
        # outright (better a clean user error than quietly booting a unit whose
        # ExecStart names a removed command), and it is filtered out of both the
        # write set and the sibling-disable set below — otherwise step 1 would
        # resurrect the very file step 0 just deleted.
        retired = frozenset(_units.RETIRED_UNITS)
        if presence_unit in retired:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"presence mode {mode!r} is retired ({presence_unit} no longer exists)",
                remediation=(
                    "choose one of: "
                    f"{', '.join(m for m in _MODES if _PRESENCE[m][0] not in retired)}"
                ),
            )
        sibling_units = tuple(unit for unit in sibling_units if unit not in retired)

        # 0. Purge any since-retired unit BEFORE the daemon-reload below, so the
        #    reload in step 2 publishes the removals in the same pass. An upgrade
        #    never rewrites units on its own, so an ordinary `service enable` is
        #    the migration's real trigger on a deployed box.
        retired_removed = self.cleanup_retired_units()

        # 1. Write the daemon + ALL (non-retired) presence unit files.
        #    Writing every sibling too means step 4's `disable --now <sibling>`
        #    always targets an installed unit — a first-time enable has no
        #    sibling on disk yet, and `systemctl disable` on a missing unit
        #    fails and would abort the enable.
        daemon_path = self._write_unit(DAEMON_UNIT, daemon_unit_text())
        written = {
            unit: self._write_unit(unit, render_fn())
            for unit, _sib, render_fn in _PRESENCE.values()
            if unit not in retired
        }
        presence_path = written[presence_unit]

        # 2. Reload the user manager so it sees the freshly-written units.
        self._require(["daemon-reload"], "reload the systemd user manager")

        # 3. Enable + start the daemon and the chosen presence.
        self._require(["enable", "--now", DAEMON_UNIT], f"enable {DAEMON_UNIT}")
        self._require(["enable", "--now", presence_unit], f"enable {presence_unit}")

        # 4. Disable + stop EVERY sibling presence — the single-owner invariant.
        #    Idempotent: disabling an already-disabled unit is a no-op success.
        for sibling_unit in sibling_units:
            self._require(["disable", "--now", sibling_unit], f"disable {sibling_unit}")

        return {
            "status": "enabled",
            "mode": mode,
            "presence_unit": presence_unit,
            # Kept singular for backward compatibility (the FIRST sibling, in
            # stable catalog order); disabled_siblings carries the full set.
            "disabled_sibling": sibling_units[0] if sibling_units else None,
            "disabled_siblings": list(sibling_units),
            "retired_removed": retired_removed,
            "unit_paths": {
                DAEMON_UNIT: str(daemon_path),
                presence_unit: str(presence_path),
            },
        }

    def disable(self) -> dict[str, object]:
        """Stop/disable whichever presence unit is enabled; leave the daemon up.

        Reads which presence unit (if any) is currently enabled and
        ``disable --now`` it. The daemon decision is **explicit**: the daemon is
        deliberately left enabled (reported as ``daemon="left-enabled"``) because
        other clients of the robot depend on it.
        """
        enabled = self._enabled_presence_unit()
        if enabled is None:
            return {"status": "disabled", "disabled": None, "daemon": "left-enabled"}
        self._require(["disable", "--now", enabled], f"disable {enabled}")
        return {"status": "disabled", "disabled": enabled, "daemon": "left-enabled"}

    def status(self) -> dict[str, object]:
        """Report the single enabled presence mode (or none) + daemon health.

        Queries ``is-enabled`` / ``is-active`` for the daemon, all three presence
        units, **and every retired unit name** through the injected runner (no
        mutation), folds the injected daemon-health probe, and returns a
        structured dict.

        Retired units are queried precisely because they are the dangerous case:
        a retired unit still enabled is a 5-second crash loop, and a status that
        iterated only the current catalog would answer ``mode=None`` for exactly
        the box most in need of a true answer. So ``mode`` is ``None`` **only**
        when nothing at all is enabled; a retired owner reports
        :data:`RETIRED_MODE` plus a ``warning`` naming the unit and the fix.
        """
        retired = tuple(_units.RETIRED_UNITS)
        probe: list[str] = [DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT, RUNTIME_UNIT]
        probe += [unit for unit in retired if unit not in probe]
        units: dict[str, dict[str, str]] = {}
        for unit in probe:
            units[unit] = {
                "enabled": self._query("is-enabled", unit),
                "active": self._query("is-active", unit),
            }
        enabled = self._enabled_presence_unit(units=units)
        retired_enabled = [
            unit for unit in retired if _is_enabled(units.get(unit, {}).get("enabled", ""))
        ]
        warning = None
        if retired_enabled:
            warning = (
                f"retired unit(s) still enabled: {', '.join(retired_enabled)} — this "
                "unit's ExecStart names a command this version no longer provides, so "
                "it restarts every 5s; run 'reachy-mini-cli service enable <mode>' to "
                "purge it"
            )
        return {
            "mode": _UNIT_TO_MODE.get(enabled, RETIRED_MODE) if enabled else None,
            "presence_unit": enabled,
            "daemon_healthy": bool(self.daemon_health()),
            "retired_enabled": retired_enabled,
            "warning": warning,
            "units": units,
        }

    # --- internals ---------------------------------------------------------

    def _enabled_presence_unit(
        self, units: Optional[dict[str, dict[str, str]]] = None
    ) -> Optional[str]:
        """Return the one enabled presence unit name, or None.

        Scans the current catalog FIRST, then any retired unit name — so a box
        whose only enabled presence is a since-retired unit still gets a truthful
        non-``None`` answer instead of a silent "nothing is running" while it
        crash-loops. Catalog-first ordering means a normal box is unaffected.

        If more than one were somehow reported enabled (a corrupt external
        state), prefer the first in that order — the next ``enable`` will repair
        the invariant by disabling every sibling and purging every retired unit.
        """
        candidates = list(_PRESENCE_UNITS)
        candidates += [unit for unit in _units.RETIRED_UNITS if unit not in candidates]
        for unit in candidates:
            if units is not None:
                value = units.get(unit, {}).get("enabled", "")
            else:
                value = self._query("is-enabled", unit)
            if _is_enabled(value):
                return unit
        return None
