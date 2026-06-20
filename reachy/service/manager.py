"""Single-presence-owner service manager for the boot-survival presence stack.

The robot has exactly **one** presence at a time (the single-SDK-owner model in
``CLAUDE.md``): either the idle ``demo-mode`` loop or the folded ``listen --live``
loop may own the head, never both. This manager makes that invariant true across
reboots via systemd ``--user`` units: ``enable(mode)`` installs + enables the
daemon and the *chosen* presence unit and **always disables the sibling**, so any
sequence of enables leaves at most one presence unit enabled.

It generalizes the pattern already proven in
:mod:`reachy.demo_service` (write unit text → ``daemon-reload`` → ``enable --now``
/ ``disable --now``) to three coordinated units — the daemon plus the two
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

The daemon unit is enabled for **both** modes — the presence units ``Requires=``
/ ``After=`` it (see :mod:`reachy.service.units`). ``disable()`` stops the enabled
presence unit only and **leaves the daemon enabled** — that decision is explicit
(reported as ``daemon="left-enabled"``) rather than silent, because tearing the
daemon down would also break any non-presence client of the robot.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from reachy.cli._errors import EXIT_ENV_ERROR, EXIT_USER_ERROR, CliError
from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    LIVE_UNIT,
    daemon_unit_text,
    demo_unit_text,
    live_unit_text,
)

# mode name -> (presence unit name, sibling unit name, unit-text renderer)
_PRESENCE = {
    "demo": (DEMO_UNIT, LIVE_UNIT, demo_unit_text),
    "live": (LIVE_UNIT, DEMO_UNIT, live_unit_text),
}
_MODES = tuple(_PRESENCE)

# Map a presence unit name back to its mode, for status() read-back.
_UNIT_TO_MODE = {DEMO_UNIT: "demo", LIVE_UNIT: "live"}


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

    # --- public API --------------------------------------------------------

    def enable(self, mode: str) -> dict[str, object]:
        """Enable exactly one presence mode (the daemon + that presence unit).

        Writes the daemon and chosen presence unit text, reloads the user
        manager, ``enable --now`` the daemon and chosen presence, and
        ``disable --now`` the sibling presence (idempotent — fine if it was
        already disabled). The sibling disable is what keeps the single-owner
        invariant true after any sequence of ``enable`` calls.
        """
        if mode not in _PRESENCE:
            raise CliError(
                code=EXIT_USER_ERROR,
                message=f"unknown presence mode: {mode!r}",
                remediation=f"choose one of: {', '.join(_MODES)}",
            )
        presence_unit, sibling_unit, _ = _PRESENCE[mode]

        # 1. Write the daemon + BOTH presence unit files (t1's renderers). Writing
        #    the sibling too means step 4's `disable --now <sibling>` always targets
        #    an installed unit — a first-time enable has no sibling on disk yet, and
        #    `systemctl disable` on a missing unit fails and would abort the enable.
        daemon_path = self._write_unit(DAEMON_UNIT, daemon_unit_text())
        written = {
            unit: self._write_unit(unit, render_fn())
            for unit, _sib, render_fn in _PRESENCE.values()
        }
        presence_path = written[presence_unit]

        # 2. Reload the user manager so it sees the freshly-written units.
        self._require(["daemon-reload"], "reload the systemd user manager")

        # 3. Enable + start the daemon and the chosen presence.
        self._require(["enable", "--now", DAEMON_UNIT], f"enable {DAEMON_UNIT}")
        self._require(["enable", "--now", presence_unit], f"enable {presence_unit}")

        # 4. Disable + stop the sibling presence — the single-owner invariant.
        #    Idempotent: disabling an already-disabled unit is a no-op success.
        self._require(["disable", "--now", sibling_unit], f"disable {sibling_unit}")

        return {
            "status": "enabled",
            "mode": mode,
            "presence_unit": presence_unit,
            "disabled_sibling": sibling_unit,
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

        Queries ``is-enabled`` / ``is-active`` for the daemon and both presence
        units through the injected runner (no mutation), folds the injected
        daemon-health probe, and returns a structured dict. ``mode`` is the one
        enabled presence mode or ``None``.
        """
        units: dict[str, dict[str, str]] = {}
        for unit in (DAEMON_UNIT, DEMO_UNIT, LIVE_UNIT):
            units[unit] = {
                "enabled": self._query("is-enabled", unit),
                "active": self._query("is-active", unit),
            }
        enabled = self._enabled_presence_unit(units=units)
        return {
            "mode": _UNIT_TO_MODE.get(enabled) if enabled else None,
            "presence_unit": enabled,
            "daemon_healthy": bool(self.daemon_health()),
            "units": units,
        }

    # --- internals ---------------------------------------------------------

    def _enabled_presence_unit(
        self, units: Optional[dict[str, dict[str, str]]] = None
    ) -> Optional[str]:
        """Return the one enabled presence unit name, or None.

        If two were somehow both reported enabled (a corrupt external state),
        prefer the first in catalog order — the next ``enable`` will repair the
        invariant by disabling the sibling.
        """
        for unit in (DEMO_UNIT, LIVE_UNIT):
            if units is not None:
                value = units[unit]["enabled"]
            else:
                value = self._query("is-enabled", unit)
            if value == "enabled":
                return unit
        return None
