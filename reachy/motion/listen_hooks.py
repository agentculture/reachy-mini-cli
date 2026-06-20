"""Compose multiple per-tick sense hooks onto ``listen``'s single ``on_tick`` seam.

``listen`` owns the one in-process SDK client and drives the serial
:class:`~reachy.motion.queue.MotionQueue` through :func:`reachy.motion.server.run`.
That loop exposes exactly one ``on_tick`` callback — the
``(transport, queue, t, commanded_head) -> None`` seam fired once per tick before
the producer is consulted (see :class:`reachy.motion.server.LoopHooks`). The #43
:class:`~reachy.motion.listen_pat.PatHook` folds head-pat detection into that one
loop precisely because a *second* process would contend for the single-consumer
SDK client and throttle to ~1 Hz (see the single-SDK-owner model in
``CLAUDE.md``). The same argument applies to every other sense — think, vision,
sleep: they all want to ride the one loop, not spawn rival processes.

But the loop has room for only one ``on_tick`` callable. :class:`HookChain`
resolves that by *being* a single ``on_tick`` callable that fans a list of hooks
out across the seam. A ``HookChain`` instance is a drop-in ``on_tick`` — its
``__call__`` has the exact ``(transport, queue, t, commanded_head)`` signature —
so :func:`reachy.motion.server.run`'s contract is unchanged; ``listen`` just
passes ``on_tick=HookChain([...])`` instead of ``on_tick=pat_hook``.

Two robustness rules, both matching how the rest of the motion stack already
degrades silently (a transport drop must never kill the loop):

* **Per-tick isolation.** Each hook's per-tick call is wrapped in
  ``try/except Exception``: a raising hook is logged (stdlib :mod:`logging`,
  ``WARNING``) and swallowed, and the remaining hooks still run *that* tick. One
  misbehaving sense never silences the others.
* **Per-hook close.** :meth:`HookChain.close` fans out to every hook's ``close``
  (if it has one), each guarded the same way — a hook whose ``close`` raises does
  not block its neighbours' cleanup. The ``listen`` loop calls this in its
  ``finally`` so no sense leaks its ``*_active`` flag past the run.

**Priority convention.** Hooks run in the order given, so callers pass them in
descending priority — the established idle-interrupt order is
``sleep > pat > think`` (sleep yields the head entirely, pat pauses the idle
wander, think drops to a focused breathe). The hooks themselves arrive in later
tasks; this module is only the composition mechanism. An empty chain is a fully
valid no-op (call and close are both safe), which is how ``listen`` represents
"no sense hooks active" without a ``None`` special case.

Pure standard library — no new runtime dependency.
"""

from __future__ import annotations

import logging
from typing import Callable

logger = logging.getLogger(__name__)

#: The shared ``on_tick`` signature every hook in a chain must accept:
#: ``(transport, queue, t, commanded_head) -> None`` (see
#: :class:`reachy.motion.server.LoopHooks`).
Hook = Callable[..., None]


class HookChain:
    """A composite ``on_tick`` that fans a list of per-tick hooks out in order.

    Construct one with the hooks in **descending priority order** (the convention
    is ``sleep > pat > think``; see the module docstring) and pass the instance as
    ``on_tick=`` to :func:`reachy.motion.server.run`. Each hook must accept the
    loop's ``on_tick`` signature ``(transport, queue, t, commanded_head)``. Call
    :meth:`close` in the loop's ``finally`` so every hook gets to clean up.

    Parameters
    ----------
    hooks:
        The per-tick hooks, in the order they should run each tick. An empty list
        makes the chain a no-op (a valid ``on_tick`` that does nothing).
    """

    def __init__(self, hooks: list[Hook]) -> None:
        #: The hooks, kept in caller (priority) order; never reordered.
        self.hooks: list[Hook] = list(hooks)

    def __call__(
        self,
        transport: object,
        queue: object,
        t: float,
        commanded_head: dict[str, float] | None = None,
    ) -> None:
        """One tick: run every hook in order, isolating each from the others.

        Mirrors the bare-:class:`~reachy.motion.listen_pat.PatHook` ``on_tick``
        contract exactly, then fans it out. Each hook is invoked inside a
        ``try/except Exception`` so a raising hook is logged and swallowed and the
        remaining hooks still run this tick — a single misbehaving sense never
        silences the loop. ``queue`` and ``commanded_head`` are forwarded verbatim
        (the loop hands in the live queue and the ``{"pitch", "yaw"}`` pose it last
        dispatched).
        """
        for hook in self.hooks:
            # Degrade silently: one bad hook must not kill the loop or its peers.
            try:
                hook(transport, queue, t, commanded_head)
            except Exception:  # noqa: BLE001
                logger.warning("on_tick hook %r raised; skipping it this tick", hook, exc_info=True)

    def close(self) -> None:
        """Fan out to every hook's ``close`` (if present), guarded per-hook.

        A hook without a ``close`` attribute is skipped. A hook whose ``close``
        raises is logged and swallowed so it never blocks the cleanup of the
        hooks after it — the ``listen`` loop calls this in its ``finally`` and
        must always reach every hook so no sense leaks its ``*_active`` flag.
        Safe to call on an empty chain.
        """
        for hook in self.hooks:
            close = getattr(hook, "close", None)
            if close is None:
                continue
            try:
                close()
            except Exception:  # noqa: BLE001
                logger.warning("on_tick hook %r close() raised; skipping it", hook, exc_info=True)
