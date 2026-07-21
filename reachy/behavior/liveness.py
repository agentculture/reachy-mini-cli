"""Is a behavior engine currently driving the head? — and the refusal built on it.

Why this module exists (the decision)
=====================================
The head is a single resource. Three ``*_active.flag`` files
(``reachy.speech.cognition_signal``, :mod:`reachy.motion.pat_signal`,
:mod:`reachy.motion.sleep_signal`) used to coordinate it across processes, but
they were only ever *read* across processes by the ``listen`` idle layer, which
yielded the motion channel by priority ``sleep > pat > think``.
``think_active.flag`` is already gone — task t21 deleted its writer (the folded
``listen --live`` cognition hook), its reader and its module in one pass. The
other two outlived that layer: task t22 retired the ``listen`` NOUN, so
``pat_active.flag`` / ``sleep_active.flag`` now have **no cross-process reader
at all**. Their modules stay because each is still noun-local state its own
writer reads back (``pat run`` clears its flag idempotently;
``sleep status`` reports ASLEEP from ``sleep_active.flag``), but nothing outside
the writing process consults them any more. Nothing in :mod:`reachy.behavior`
ever read any of the three. So a foreground ``reachy pat run`` and the
``reachy-runtime.service`` behavior engine would drive the head with **no mutual
awareness at all**.

Two options were defensible: the runtime could read the flags and yield the head
(preserving today's soft-guard semantics), or the foreground verbs could refuse
to start beside a live runtime. **This module implements the refusal.** Four
things decided it:

1. **The repo already answered this exact question, this exact way.** The probe
   path in ``reachy/cli/_commands/behavior.py``
   (``_refuse_bad_probe_request`` / ``_probe_engine_is_fresh``) faces the same
   hazard — a second head-owner starting beside a live engine — and refuses with
   a :class:`~reachy.cli._errors.CliError` rather than yielding. Adopting the
   opposite policy for ``pat`` / ``sleep`` would leave two contradictory
   arbitration rules for one resource in one codebase. That probe logic now
   delegates *here*, so there is exactly one definition of "an engine is live".

2. **Yielding would produce a silently useless process.** The engine owns motion
   exclusively and streams the head at 50 Hz through its own channel arbitration.
   A foreground ``pat run`` that yielded would still sense, still detect a pat,
   and never be able to react — a process that looks like it is working and is
   not. A refusal that names the fix is strictly more honest than a no-op loop.

3. **A flag file cannot expire; a heartbeat can.** A ``SIGKILL``ed writer leaves
   its ``*_active.flag`` on disk forever, and there is no stale-flag guard
   anywhere today. Building *new* load-bearing arbitration on that primitive
   would make the failure mode worse, not better. The engine's ``state.json``
   heartbeat is self-expiring (:data:`ENGINE_HEARTBEAT_TTL_S`) and handles the
   reboot / monotonic-reset case explicitly, so a crashed engine can never lock
   an operator out of their own robot.

4. **A foreground verb joining a running runtime is an operator error**, not a
   supported composition. The single-presence invariant is already enforced
   between the *units* (:class:`reachy.service.manager.ServiceManager` disables
   the siblings on every ``enable``); this closes the same invariant against the
   foreground CLI verbs an operator types by hand, which no unit-level check can
   see.

The flag modules themselves are deliberately left in place: :mod:`reachy.motion.
pat_signal` and :mod:`reachy.motion.sleep_signal` each still have a live writer
AND a live reader *inside the owning noun* — unlike ``think_active.flag``, whose
writer and reader both went in one pass, so it could be deleted outright. What
they no longer have is a reader in any *other* process, which is exactly the
arbitration role this module took over. Treat them as per-noun bookkeeping, not
as a cross-process channel: nothing arbitrates the head on them.

Why a heartbeat and not ``systemctl --user is-active``
======================================================
The hazard is "an engine is streaming the head right now", regardless of who
launched it — ``reachy-runtime.service``, ``behavior engine start``, or a bare
foreground ``behavior engine run`` in another terminal. A systemd query would
see only the first of the three, and would drag a systemd dependency into two
nouns that have none. Reading the engine's own heartbeat catches all three, is
trivially testable, and works on a box with no systemd at all.

Pure standard library.
"""

from __future__ import annotations

import math
import time

from reachy.behavior import control
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.service.units import RUNTIME_UNIT

__all__ = [
    "ENGINE_HEARTBEAT_TTL_S",
    "ENGINE_HEARTBEAT_SKEW_S",
    "RUNTIME_UNIT",
    "engine_is_live",
    "refuse_if_engine_live",
]

#: How old the engine's published heartbeat may be and still count as live.
#: The engine republishes ``state.json`` at ``compose_hz / 2`` (25 Hz at the
#: default 50 Hz compose rate), so 2 s is many hundreds of missed beats — long
#: enough that a briefly-stalled engine is not mistaken for a dead one, short
#: enough that a killed engine stops blocking the operator almost immediately.
ENGINE_HEARTBEAT_TTL_S = 2.0

#: How far into the future a heartbeat may sit and still count as live.
#: ``Engine.state()`` writes ``round(now, 3)`` and can round UP, so a genuinely
#: live engine can read back a hair ahead of us; this window absorbs that (and
#: small cross-process jitter) without letting a monotonic-clock reset (a reboot)
#: masquerade as a live engine forever.
ENGINE_HEARTBEAT_SKEW_S = 1.0


def engine_is_live() -> bool:
    """Whether ``state.json`` proves a behavior engine heartbeat is still live.

    Fail-OPEN on anything unproven — a missing, corrupt, or unusable state file
    means "no evidence of an engine", not "refuse". The guard exists to stop a
    *known* collision, never to strand an operator on ambiguous bookkeeping.

    Fail-CLOSED only on the one genuine ambiguity: a heartbeat a hair in the
    future, which a live engine's millisecond rounding really does produce. A
    stamp *far* in the future is instead a ``state.json`` left over from before a
    monotonic-clock reset, which is stale rather than live.
    """
    state = control.read_state()
    updated = state.get("updated") if isinstance(state, dict) else None
    # bool is an int subclass — exclude it explicitly so ``"updated": true``
    # cannot be read as the timestamp 1.0.
    if isinstance(updated, bool) or not isinstance(updated, (int, float)):
        return False
    updated = float(updated)
    if not math.isfinite(updated):
        return False
    age = time.monotonic() - updated
    if age < 0.0:
        return age >= -ENGINE_HEARTBEAT_SKEW_S
    return age <= ENGINE_HEARTBEAT_TTL_S


def refuse_if_engine_live(verb: str) -> None:
    """Refuse *verb* while a behavior engine is driving the head; else no-op.

    Call this FIRST in a foreground command — before the transport is
    constructed — so a refused verb never opens the single-consumer SDK media
    session it would have contended for, exactly as the probe refusal precedes
    transport construction on the ``behavior`` side.

    :param verb: the command path being refused (e.g. ``"pat run"``), quoted back
        to the operator so the message names what they actually typed.
    :raises CliError: exit-1 (user error) with a remediation covering all three
        ways an engine can be running.
    """
    if not engine_is_live():
        return
    raise CliError(
        code=EXIT_USER_ERROR,
        message=(
            f"'{verb}' refused: a behavior engine is already driving the head "
            "(fresh heartbeat in the engine's state.json)"
        ),
        remediation=(
            f"the engine owns motion exclusively — stop it first: "
            f"'systemctl --user stop {RUNTIME_UNIT}' if it runs as the boot presence, "
            "'reachy behavior engine stop' if it was launched with "
            "'reachy behavior engine start', or press Ctrl-C in the terminal owning a "
            "foreground 'reachy behavior engine run'"
        ),
    )
