"""Wireless discovery — find a Reachy Mini unit on the local network.

Finds a Reachy Mini daemon on the LAN, remembers which unit is the operator's
across IP changes, pins a stable ``/etc/hosts`` alias, and opens a login shell
— all reachable from the ``reachy wireless`` noun. This package is
**stdlib-only** (mirroring ``reachy/daemon.py``'s own constraint): no
``reachy_mini`` import, no third-party HTTP client, no new dependency — a
later test walks its AST to enforce this.

Public API::

    from reachy.discover.probe import UnitRecord, probe
    from reachy.discover.registry import RegistryRecord, UnitRegistry, lookup_mac
    from reachy.discover.sweep import Interface, SweepResult, enumerate_hosts, sweep
    from reachy.discover import hosts, ssh

:func:`~reachy.discover.probe.probe` is the one building block every later
piece composes on top of: a single, side-effect-free ``GET
/api/daemon/status`` that never raises.
:func:`~reachy.discover.sweep.sweep` fans it out over the local ``/24``-or-
narrower subnets under a hard worker cap and one overall deadline — see that
module's docstring for the seven-Docker-bridge hazard it exists to make
impossible, and for :func:`~reachy.discover.sweep.read_interfaces`, the one
seam that touches the real box. :class:`~reachy.discover.registry.UnitRegistry`
persists probed units to a per-user ``state_dir()/units.json`` keyed by
``hardware_id``, with the same never-raise degrade-to-empty discipline as
:class:`reachy.stash.store.StashStore`. ``hosts.py`` owns the recoverable
``/etc/hosts`` pin and ``ssh.py`` the login/authorize path.

**Never re-export a symbol whose name matches a submodule of this package.**
``probe.py`` holds a function also called ``probe``; ``sweep.py`` holds one
called ``sweep``. Re-exporting either here REBINDS the package attribute from
the MODULE to the FUNCTION, because ``import reachy.discover.probe as m``
resolves via ``getattr(package, "probe")`` before it ever consults
``sys.modules``. Every module-level injection seam then breaks: a caller doing
``monkeypatch.setattr(<the module>, "read_interfaces", ...)`` — task t6's
autouse guard does exactly this — receives the function and dies with
``AttributeError``. Both spellings were written that way once and both were
caught; ``tests/test_discover_sweep.py`` and ``tests/test_discover_probe.py``
pin the module identity so neither can come back quietly. Import those two
callables from their own modules.
"""

from __future__ import annotations

from reachy.discover.probe import UnitRecord
from reachy.discover.registry import RegistryRecord, UnitRegistry, lookup_mac

__all__ = [
    "UnitRecord",
    "RegistryRecord",
    "UnitRegistry",
    "lookup_mac",
]
