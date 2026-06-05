"""The contention core — who owns each channel, and what an add evicts.

Two pure functions, no I/O, no clock:

* :func:`arbitrate` runs **every tick**: given the live behaviors (in admission
  order, oldest first), it assigns each channel a single owner by
  ``(class priority, recency)``.
* :func:`admit` runs **when a behavior is added**: a ``stopping`` behavior removes
  the ``stoppable`` behaviors it shares a channel with; everything else removes
  nothing. Admission is *total* — every behavior is accepted — so contention a
  newcomer cannot win by removal is simply resolved per tick (it waits, yielding
  the channel to a higher-priority incumbent until that incumbent ends).

This encodes the four-class model directly:

* **passive** — never removes anything and is only ever the per-tick owner of a
  channel no non-passive behavior claims (lowest priority);
* **stoppable** — drives, but is removed by a newly-admitted ``stopping`` on a
  shared channel;
* **unstoppable** — highest priority (owns its channels while alive) and is never
  removed by an add, so it "holds until it finishes itself";
* **stopping** — on admit, evicts the shared ``stoppable`` behaviors and takes
  over.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from reachy.behavior.model import CHANNELS, Behavior, StopClass


def arbitrate(behaviors: list[Behavior]) -> dict[str, Behavior | None]:
    """Assign each channel its owner. ``behaviors`` is oldest-first (recency = later).

    The owner of a channel is the candidate claiming it with the highest class
    priority, ties broken by most-recently-admitted. A channel no behavior claims
    maps to ``None``. A ``passive`` behavior (priority 0) therefore wins a channel
    only when nothing non-passive claims it.
    """
    owners: dict[str, Behavior | None] = dict.fromkeys(CHANNELS)
    indexed = list(enumerate(behaviors))
    for channel in CHANNELS:
        candidates = [(i, b) for i, b in indexed if channel in b.channels]
        if not candidates:
            continue
        _, best = max(candidates, key=lambda ib: (ib[1].stop_class.priority, ib[0]))
        owners[channel] = best
    return owners


@dataclass
class AdmitResult:
    """Outcome of admitting a behavior.

    ``evicted`` are the (stoppable) behaviors a ``stopping`` add removed. ``blocked``
    are the new behavior's channels it will *not* own yet because a higher-priority
    incumbent holds them — informational, not a failure (the newcomer stays active
    and takes the channel once the incumbent ends).
    """

    evicted: list[Behavior] = field(default_factory=list)
    blocked: list[str] = field(default_factory=list)


def admit(new: Behavior, behaviors: list[Behavior]) -> AdmitResult:
    """Decide what admitting ``new`` removes, and which of its channels it must wait for.

    ``behaviors`` is the current live set (oldest-first). A ``passive`` newcomer
    never removes anything and is expected to yield, so its ``blocked`` is left
    empty. Any other newcomer that is ``stopping`` removes the ``stoppable``
    behaviors it shares a channel with; ``blocked`` is then computed against the
    prospective set with ``new`` as the most-recent entry.
    """
    if new.stop_class is StopClass.PASSIVE:
        return AdmitResult(evicted=[], blocked=[])

    evicted: list[Behavior] = []
    if new.stop_class is StopClass.STOPPING:
        evicted = [
            b
            for b in behaviors
            if b.stop_class is StopClass.STOPPABLE and (b.channels & new.channels)
        ]

    evicted_ids = {b.id for b in evicted}
    remaining = [b for b in behaviors if b.id not in evicted_ids]
    owners = arbitrate([*remaining, new])  # new is newest -> wins same-priority ties
    blocked = sorted(
        channel
        for channel in new.channels
        if owners[channel] is None or owners[channel].id != new.id
    )
    return AdmitResult(evicted=evicted, blocked=blocked)
