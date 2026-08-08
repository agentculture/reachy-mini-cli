"""The lookup the CLI actually calls: registry + probe + sweep, composed.

:mod:`reachy.discover.probe` answers "is there a Reachy daemon at *this*
address?". :mod:`reachy.discover.sweep` answers "which addresses on this LAN
are worth asking?". :mod:`reachy.discover.registry` remembers what was found
last time. This module is the fourth piece that turns those three into the
one question a verb like ``wireless ssh`` / ``wireless authorize`` / ``wireless
pin`` actually needs answered: **which single unit do you mean, and where is
it right now?**

The resolution order (spec requirement, ``docs/specs/...#The remembered unit``)
-------------------------------------------------------------------------------

1. **Fast path.** A remembered unit is tried FIRST at its ``last_ip``, with a
   short, bounded timeout (:data:`FAST_PATH_TIMEOUT` — reused from
   :data:`reachy.discover.probe.DEFAULT_TIMEOUT` rather than a second number
   that could drift from it). A dark IP therefore costs exactly ONE bounded
   probe, never a full per-host connect timeout and never a retry loop.
2. **Verify identity.** A response whose ``hardware_id`` matches the
   remembered record is returned immediately — the sweep function is never
   even called on this path.
3. **Escalate on mismatch or miss.** A remembered IP that answers as a
   DIFFERENT ``hardware_id`` (DHCP handed the address to another device) is
   REJECTED, not returned, and falls through to the sweep exactly like a dark
   IP does. Once the sweep re-locates the same ``hardware_id`` at its new
   address, the registry record is RE-PINNED (``last_ip``/``name``/``model``/
   ``wireless``/``last_seen`` refreshed; ``mac``/``alias`` carried forward
   unchanged) — staleness is actively corrected, never merely tolerated.
4. **Ambiguity is refused, never guessed.** More than one known unit matching
   (or, with no known units at all, more than one unit the sweep finds) raises
   a :class:`~reachy.cli._errors.CliError` naming every candidate — this
   operator's box has a Lite on localhost AND a Wireless on the LAN, both
   reporting ``robot_name=reachy_mini``, so "more than one match" is the
   NORMAL case here, not a corner case. ``--unit <hardware_id-or-alias>``
   (the ``selector`` parameter) and an optional registry-level ``default``
   both pick among known units; neither is ever silently overridden by a
   sweep result.

What this module deliberately does NOT do: it never enumerates interfaces or
opens a socket itself — every network fact flows through the injected
``probe_fn`` / ``sweep_fn`` seams (both default to the real
:func:`reachy.discover.probe.probe` / :func:`reachy.discover.sweep.sweep`,
resolved at CALL time so a test can swap either without touching a real NIC),
mirroring :func:`reachy.discover.sweep.sweep`'s own "resolve the real thing
only if nothing was injected" convention.

Stdlib only, plus the CLI's own :class:`~reachy.cli._errors.CliError`
contract.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone

from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.discover.probe import DEFAULT_PORT, DEFAULT_TIMEOUT, UnitRecord
from reachy.discover.probe import probe as _real_probe
from reachy.discover.registry import RegistryRecord, UnitRegistry
from reachy.discover.sweep import SweepResult
from reachy.discover.sweep import sweep as _real_sweep

#: The ONE probe against a remembered IP is bounded to this timeout — reused
#: from :data:`reachy.discover.probe.DEFAULT_TIMEOUT` (a deliberately short,
#: bounded value) rather than a second constant that could drift from it, and
#: NOT the system's default (effectively unbounded) TCP connect timeout.
FAST_PATH_TIMEOUT = DEFAULT_TIMEOUT

#: The signature :func:`resolve` calls the probe seam through.
ProbeFn = Callable[[str, int, float], "UnitRecord | None"]

#: The signature :func:`resolve` calls the sweep seam through. Deliberately
#: loose (``sweep()`` carries many keyword-only tuning knobs) so a test double
#: only needs to accept ``**kwargs``.
SweepFn = Callable[..., SweepResult]

#: A zero-argument callable yielding an ISO-8601 UTC timestamp — the injected
#: clock, so ``last_seen`` is deterministic in tests.
Clock = Callable[[], str]

# --------------------------------------------------------------------------- #
# Named outcomes — every resolution names how it got there                    #
# --------------------------------------------------------------------------- #

#: The remembered IP answered with a matching hardware_id on the first try.
REASON_FAST_PATH = "fast-path"

#: The remembered IP was dark (no response within FAST_PATH_TIMEOUT); the
#: sweep re-located the same hardware_id elsewhere.
REASON_ESCALATED_MISS = "escalated-miss"

#: The remembered IP answered as a DIFFERENT hardware_id (DHCP reassignment);
#: the sweep re-located the real unit elsewhere.
REASON_ESCALATED_MISMATCH = "escalated-mismatch"

#: No unit was known at all; the sweep found exactly one and it was
#: registered for the first time.
REASON_FIRST_SIGHTING = "first-sighting"

REASONS = frozenset(
    {
        REASON_FAST_PATH,
        REASON_ESCALATED_MISS,
        REASON_ESCALATED_MISMATCH,
        REASON_FIRST_SIGHTING,
    }
)


@dataclass(frozen=True)
class ResolvedUnit:
    """The single unit :func:`resolve` decided on, and how it got there.

    ``unit`` is the freshly-probed :class:`~reachy.discover.probe.UnitRecord`
    (never a stale registry-only view); ``registry_record`` is what is now
    persisted for it (already written to disk by the time this is returned);
    ``reason`` is one of :data:`REASONS`, so a caller can tell an operator
    "this unit's address changed" without re-deriving it.
    """

    unit: UnitRecord
    registry_record: RegistryRecord
    reason: str


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _display(hardware_id: str, label: str, address: str, model: str) -> str:
    return f"{label} ({hardware_id}) at {address} [{model}]"


def _ambiguous_registry_error(records: Sequence[RegistryRecord]) -> CliError:
    candidates = ", ".join(
        _display(r.hardware_id, r.alias or r.name, r.last_ip, r.model) for r in records
    )
    return CliError(
        EXIT_USER_ERROR,
        "more than one known unit matches; refusing to pick one",
        f"pass --unit <hardware_id-or-alias> to choose: {candidates}",
    )


def _ambiguous_sweep_error(units: Sequence[UnitRecord]) -> CliError:
    candidates = ", ".join(_display(u.hardware_id, u.robot_name, u.address, u.model) for u in units)
    return CliError(
        EXIT_USER_ERROR,
        "more than one unit answered and none is known yet; refusing to pick one",
        f"run 'reachy wireless find' to see every candidate, then choose with "
        f"--unit <hardware_id>: {candidates}",
    )


def _unknown_selector_error(selector: str) -> CliError:
    return CliError(
        EXIT_USER_ERROR,
        f"no known unit matches --unit {selector!r}",
        "run 'reachy wireless list' to see known units, or "
        "'reachy wireless find' to discover new ones",
    )


def _not_found_error(hardware_id: str) -> CliError:
    return CliError(
        EXIT_USER_ERROR,
        f"unit {hardware_id} not found on the network",
        "check it is powered on and connected to this LAN, or run "
        "'reachy wireless find' to rediscover units",
    )


def _no_units_found_error() -> CliError:
    return CliError(
        EXIT_USER_ERROR,
        "no Reachy Mini unit found on the network",
        "check the unit is powered on and connected to this LAN, or pass an "
        "explicit --base-url/address",
    )


def _touch(existing: RegistryRecord, probed: UnitRecord, clock: Clock) -> RegistryRecord:
    """Re-pin *existing* to what *probed* just answered.

    Only the volatile facts move (``last_ip``/``name``/``model``/``wireless``/
    ``last_seen``); ``mac`` and ``alias`` are carried forward unchanged — a
    re-pin is a location update, not a fresh sighting.
    """
    return RegistryRecord(
        hardware_id=probed.hardware_id,
        mac=existing.mac,
        last_ip=probed.address,
        name=probed.robot_name,
        model=probed.model,
        wireless=probed.wireless,
        last_seen=clock(),
        alias=existing.alias,
    )


def _first_sighting(probed: UnitRecord, clock: Clock) -> RegistryRecord:
    """A brand-new record for a unit that was never in the registry before."""
    return RegistryRecord(
        hardware_id=probed.hardware_id,
        mac=None,
        last_ip=probed.address,
        name=probed.robot_name,
        model=probed.model,
        wireless=probed.wireless,
        last_seen=clock(),
        alias=None,
    )


def _select(
    selector: str | None, default: str | None, records: Sequence[RegistryRecord]
) -> RegistryRecord | None:
    """Resolve *selector* (falling back to *default*) against known records.

    Returns ``None`` when no selector/default was given at all (the caller
    then falls through to the "how many known units are there" logic).
    Raises :class:`CliError` when a selector WAS given but matches nothing.
    """
    chosen = selector.strip() if selector and selector.strip() else None
    if chosen is None:
        chosen = default.strip() if default and default.strip() else None
    if chosen is None:
        return None
    matches = [r for r in records if r.hardware_id == chosen or r.alias == chosen]
    if not matches:
        raise _unknown_selector_error(chosen)
    if len(matches) > 1:
        raise _ambiguous_registry_error(matches)
    return matches[0]


def _resolve_known(
    record: RegistryRecord,
    *,
    registry: UnitRegistry,
    prober: ProbeFn,
    sweeper: SweepFn,
    port: int,
    fast_timeout: float,
    sweep_kwargs: Mapping[str, object],
    clock: Clock,
) -> ResolvedUnit:
    """Fast-path *record*, escalating to a sweep on a miss or an identity mismatch."""
    seen = prober(record.last_ip, port, fast_timeout)
    if seen is not None and seen.hardware_id == record.hardware_id:
        updated = _touch(record, seen, clock)
        registry.upsert(updated)
        return ResolvedUnit(unit=seen, registry_record=updated, reason=REASON_FAST_PATH)

    mismatched = seen is not None  # answered, but as a different unit

    sweep_result = sweeper(port=port, probe_fn=prober, **dict(sweep_kwargs))
    found = next((u for u in sweep_result.units if u.hardware_id == record.hardware_id), None)
    if found is None:
        raise _not_found_error(record.hardware_id)

    updated = _touch(record, found, clock)
    registry.upsert(updated)
    reason = REASON_ESCALATED_MISMATCH if mismatched else REASON_ESCALATED_MISS
    return ResolvedUnit(unit=found, registry_record=updated, reason=reason)


def resolve(
    selector: str | None = None,
    *,
    default: str | None = None,
    registry: UnitRegistry | None = None,
    probe_fn: ProbeFn | None = None,
    sweep_fn: SweepFn | None = None,
    port: int = DEFAULT_PORT,
    fast_timeout: float = FAST_PATH_TIMEOUT,
    sweep_kwargs: Mapping[str, object] | None = None,
    now: Clock | None = None,
) -> ResolvedUnit:
    """Resolve exactly ONE unit — the fast path, escalation, and refusal rules above.

    ``selector`` is the operator's ``--unit <hardware_id-or-alias>``; ``default``
    is an optional registry-level fallback a caller may configure (e.g. an env
    var) and is consulted only when ``selector`` is not given. Both are matched
    against a known record's ``hardware_id`` OR its ``alias``.

    ``probe_fn`` / ``sweep_fn`` default to the real
    :func:`reachy.discover.probe.probe` / :func:`reachy.discover.sweep.sweep`,
    resolved at CALL time (not as a default-argument binding) so a caller can
    inject either without needing to patch a module attribute — mirroring
    :func:`reachy.discover.sweep.sweep`'s own ``probe_fn`` convention.

    Raises :class:`~reachy.cli._errors.CliError` (never any other exception)
    on every refusal: an unknown selector, an ambiguous match, a unit that
    cannot be found even after a sweep, or no unit answering at all.
    """
    reg = registry if registry is not None else UnitRegistry()
    prober: ProbeFn = probe_fn if probe_fn is not None else _real_probe
    sweeper: SweepFn = sweep_fn if sweep_fn is not None else _real_sweep
    clock: Clock = now if now is not None else _now_iso
    kwargs = sweep_kwargs if sweep_kwargs is not None else {}

    records = reg.all()

    picked = _select(selector, default, records)
    if picked is not None:
        return _resolve_known(
            picked,
            registry=reg,
            prober=prober,
            sweeper=sweeper,
            port=port,
            fast_timeout=fast_timeout,
            sweep_kwargs=kwargs,
            clock=clock,
        )

    if len(records) > 1:
        raise _ambiguous_registry_error(records)

    if len(records) == 1:
        return _resolve_known(
            records[0],
            registry=reg,
            prober=prober,
            sweeper=sweeper,
            port=port,
            fast_timeout=fast_timeout,
            sweep_kwargs=kwargs,
            clock=clock,
        )

    # No known units at all: nothing to fast-path against, so this is a
    # first-ever discovery. A sweep is the only option.
    sweep_result = sweeper(port=port, probe_fn=prober, **dict(kwargs))
    units = sweep_result.units
    if not units:
        raise _no_units_found_error()
    if len(units) > 1:
        raise _ambiguous_sweep_error(units)

    found = units[0]
    new_record = _first_sighting(found, clock)
    reg.upsert(new_record)
    return ResolvedUnit(unit=found, registry_record=new_record, reason=REASON_FIRST_SIGHTING)
