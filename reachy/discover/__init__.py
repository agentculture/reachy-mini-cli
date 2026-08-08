"""Wireless discovery — find a Reachy Mini unit on the local network.

Finds a Reachy Mini daemon on the LAN, remembers which unit is the operator's
across IP changes, pins a stable ``/etc/hosts`` alias, and opens a login shell
— all reachable from the ``reachy wireless`` noun. This package is
**stdlib-only** (mirroring ``reachy/daemon.py``'s own constraint): no
``reachy_mini`` import, no third-party HTTP client, no new dependency — a
later test walks its AST to enforce this.

Public API so far::

    from reachy.discover.probe import UnitRecord, probe
    from reachy.discover.registry import RegistryRecord, UnitRegistry, lookup_mac

:func:`~reachy.discover.probe.probe` is the one building block every later
piece in this package composes on top of: a single, side-effect-free
``GET /api/daemon/status`` that never raises. :class:`~reachy.discover.registry.UnitRegistry`
persists probed units to a per-user ``state_dir()/units.json``, keyed by
``hardware_id``, with the same never-raise degrade-to-empty discipline as
:class:`reachy.stash.store.StashStore`. Later modules layer bounded concurrent
LAN sweeping (``sweep.py``), a recoverable ``/etc/hosts`` pin (``hosts.py``),
and the SSH login/authorize path (``ssh.py``) on top of these — none of them
exist yet in this package.
"""

from __future__ import annotations

from reachy.discover.probe import UnitRecord, probe
from reachy.discover.registry import RegistryRecord, UnitRegistry, lookup_mac

__all__ = [
    "UnitRecord",
    "probe",
    "RegistryRecord",
    "UnitRegistry",
    "lookup_mac",
]
