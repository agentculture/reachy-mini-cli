"""Wireless discovery — find a Reachy Mini unit on the local network.

Finds a Reachy Mini daemon on the LAN, remembers which unit is the operator's
across IP changes, pins a stable ``/etc/hosts`` alias, and opens a login shell
— all reachable from the ``reachy wireless`` noun. This package is
**stdlib-only** (mirroring ``reachy/daemon.py``'s own constraint): no
``reachy_mini`` import, no third-party HTTP client, no new dependency — a
later test walks its AST to enforce this.

Public API so far::

    from reachy.discover.probe import UnitRecord, probe
    from reachy.discover.sweep import Interface, SweepResult, enumerate_hosts, sweep

:func:`~reachy.discover.probe.probe` is the one building block every later
piece in this package composes on top of: a single, side-effect-free
``GET /api/daemon/status`` that never raises.
:func:`~reachy.discover.sweep.sweep` fans it out over the local ``/24``-or-
narrower subnets under a hard worker cap and one overall deadline — see that
module's docstring for the seven-Docker-bridge hazard it exists to make
impossible, and for :func:`~reachy.discover.sweep.read_interfaces`, the one
seam that touches the real box. Later modules layer a per-user remembered-unit
registry (``registry.py``), a recoverable ``/etc/hosts`` pin (``hosts.py``),
and the SSH login/authorize path (``ssh.py``) on top of them — none of those
exist yet in this package.

**Do NOT re-export ``sweep`` (or any future submodule-named symbol) here.**
``sweep.py`` holds a function also called ``sweep``, so
``from reachy.discover.sweep import sweep`` in this file would REBIND the
package attribute ``reachy.discover.sweep`` from the MODULE to the FUNCTION.
That breaks the injection seam outright: ``import reachy.discover.sweep as m``
resolves via ``getattr(package, "sweep")`` before it ever consults
``sys.modules``, so every caller — including task t6's autouse guard, whose
whole job is ``monkeypatch.setattr(<the module>, "read_interfaces", ...)`` —
would silently receive the function and fail with ``AttributeError``. It was
written that way once and caught by the suite; ``tests/test_discover_sweep.py``
now pins the module identity so it cannot come back quietly. Import the sweep
API from its own module.
"""

from __future__ import annotations

from reachy.discover.probe import UnitRecord, probe

__all__ = [
    "UnitRecord",
    "probe",
]
