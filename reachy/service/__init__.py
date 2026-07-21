"""``reachy.service`` — systemd ``--user`` supervision for the presence stack.

This package owns the unit text and (in sibling tasks) the install/enable/order
machinery for the daemon + presence units. The canonical unit names and the pure
unit-text renderers are re-exported here so callers can do
``from reachy.service import DAEMON_UNIT, daemon_unit_text``.

``LIVE_UNIT`` / ``live_exec_start`` / ``live_unit_text`` are deliberately ABSENT
since ``t23``: ``reachy-live.service`` ran ``listen run --live``, a command
``t21``/``t22`` removed, so the unit it rendered crash-looped. The name survives
only in :data:`reachy.service.units.RETIRED_UNITS`, which is what purges it from
a deployed box.
"""

from __future__ import annotations

from reachy.service.units import (
    DAEMON_UNIT,
    DEMO_UNIT,
    RETIRED_UNITS,
    RUNTIME_UNIT,
    daemon_exec_start,
    daemon_unit_text,
    demo_exec_start,
    demo_unit_text,
    runtime_exec_start,
    runtime_unit_text,
)

__all__ = [
    "DAEMON_UNIT",
    "DEMO_UNIT",
    "RETIRED_UNITS",
    "RUNTIME_UNIT",
    "daemon_exec_start",
    "demo_exec_start",
    "runtime_exec_start",
    "daemon_unit_text",
    "demo_unit_text",
    "runtime_unit_text",
]
