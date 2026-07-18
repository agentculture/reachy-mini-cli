"""A live-pose feed — bridge the engine's composed pose back into the seam.

The 50 Hz engine composes and streams a complete pose every tick, but no seam
rider could previously read it: :class:`~reachy.behavior.engine.TickContext`
exposed ownership and perception, not the pose itself (see
:mod:`reachy.behavior.goto_lane`'s "Start pose" docstring section, which
documents the resulting limitation — a goto with no injected start-pose
provider must interpolate from neutral, snapping any already-offset channel to
zero at ``t=0``). ``TickContext.pose`` closes that gap: it is now the exact
dict the engine streamed THIS tick, populated after streaming (see
:class:`reachy.behavior.engine.TickContext`'s docstring).

This module is the seam-side consumer of that field:

* :class:`LastPoseHolder` — a :class:`~reachy.behavior.rule_engine.TickBus`
  driver that stashes ``ctx.pose`` each tick so a later rider can read "the
  robot's current composed pose" without re-deriving it from ownership +
  contributions. Mirrors :class:`reachy.export.runtime.SenseSnapshotDriver`'s
  "stash on ``__call__``, read via a getter" shape and
  :class:`reachy.behavior.sense.DoaPoller`'s "any failure degrades to the last
  known value, never raises" contract.
* :meth:`LastPoseHolder.as_start_pose_provider` — the adapter closing the loop
  with :class:`reachy.behavior.goto_lane.GotoLane`: it converts the holder's
  stashed pose dict into the :class:`~reachy.behavior.model.Contribution` shape
  ``GotoLane``'s ``start_pose_provider`` expects, so composition code wires

    ``GotoLane(start_pose_provider=holder.as_start_pose_provider())``

  and a goto submitted while some behavior already holds a channel off-neutral
  interpolates from THAT live pose instead of snapping to neutral at ``t=0``.

Pure standard library, and it imports only :mod:`reachy.behavior.model` (for
:class:`~reachy.behavior.model.Contribution`) — no transport, no clock, no
network, so it stays a dependency-free leaf like its sibling seam modules.
"""

from __future__ import annotations

from typing import Callable

from reachy.behavior.model import Contribution


class LastPoseHolder:
    """Stash each tick's composed pose so a later seam rider can read it live.

    Usable directly as a :class:`~reachy.behavior.rule_engine.TickBus` driver
    (``driver(ctx) -> None``) — pass it straight to
    ``TickBus(drivers=[holder, ...])`` or even bare as
    ``engine.run(tick_seam=holder)``. Every call stashes ``ctx.pose`` — the
    complete dict the engine streamed THIS tick (see
    :class:`reachy.behavior.engine.TickContext`'s ``pose`` field) — so
    :meth:`peek` always answers with the most recently stashed pose relative to
    whichever consumer calls it afterward (typically the previous tick's pose,
    when the consumer is a driver registered later on the same
    :class:`~reachy.behavior.rule_engine.TickBus` reading it on the NEXT tick).

    Never raises. A missing ``ctx.pose`` (an older/foreign ``TickContext``-
    shaped object that predates this field, or any other attribute-access
    failure) degrades to a no-op stash — :meth:`peek` simply keeps returning
    whatever it last held (``None`` if nothing has stashed yet) — mirroring
    :class:`reachy.behavior.sense.DoaPoller`'s "any failure -> keep the last
    known value" contract. A tick seam rider reading :meth:`peek` before any
    tick has run (or immediately after a degrade) must treat ``None`` as "no
    live pose yet" and fall back to its own honest default (see
    :meth:`as_start_pose_provider` for the pattern).
    """

    def __init__(self) -> None:
        self._last: dict | None = None

    def __call__(self, ctx) -> None:
        """``TickBus`` driver entry point — stash ``ctx.pose`` for :meth:`peek`."""
        try:
            pose = ctx.pose
        except AttributeError:
            return
        if pose is None:
            return
        self._last = pose

    def peek(self) -> dict | None:
        """The last stashed pose dict, or ``None`` before any tick has stashed one."""
        return self._last

    def as_start_pose_provider(self) -> Callable[[], Contribution]:
        """A ``() -> Contribution`` adapter for :class:`GotoLane`'s ``start_pose_provider``.

        Converts :meth:`peek`'s pose dict into the :class:`Contribution` shape
        :class:`reachy.behavior.goto_lane.GotoLane` samples at admit time to
        seed a goto's minjerk start pose:
        ``GotoLane(start_pose_provider=holder.as_start_pose_provider())``.

        Before any tick has stashed a pose the returned provider answers a
        fresh, all-``None`` :class:`Contribution` — the SAME honest
        neutral-relative default ``GotoLane`` already falls back to when no
        provider is wired at all (see its module docstring's "Start pose"
        section), so an early goto behaves identically whether or not a
        provider was wired. Never raises: the provider itself degrades to that
        same default on any unexpected shape from :meth:`peek` (defensive, to
        match this module's "peek, never raise" contract even though a
        well-formed engine pose can't actually trigger it).
        """

        def provider() -> Contribution:
            pose = self.peek()
            if pose is None:
                return Contribution()
            try:
                head = pose.get("head")
                return Contribution(
                    head=dict(head) if head is not None else None,
                    antennas=pose.get("antennas"),
                    body_yaw=pose.get("body_yaw"),
                )
            except AttributeError:
                return Contribution()

        return provider
