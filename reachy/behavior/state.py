"""State snapshot — joints + head pose read through the one SDK client seam.

Mirrors :mod:`reachy.behavior.sense`'s duck-typing idiom: this module holds no
reference to ``reachy_mini`` (or any transport class) at all. A
:class:`StateReader` is constructed with plain injected callables — typically
bound methods on an already-open SDK session such as
:class:`reachy.robot.sdk_transport.MediaSession` (``get_current_joint_positions``
and ``head_pose``) — so reading state never opens a second SDK client and is
fully mockable in tests with bare functions/lambdas.

Stdlib only, and this is a dependency-free leaf: no imports from
``reachy.robot`` or ``reachy_mini`` anywhere in this file.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Callable


@dataclass(frozen=True)
class StateSnapshot:
    """One state read: joint positions + head pose, or ``None`` when unavailable.

    ``joints`` is whatever the injected ``joints_fn`` returns (e.g. the SDK's
    ``get_current_joint_positions()`` result — a ``(head_joints, antenna_joints)``
    tuple). ``head_pose`` is whatever the injected ``head_pose_fn`` returns (e.g.
    a pitch/yaw pose mapping, matching ``head_pose()`` on the transports in
    ``reachy/robot/``). ``ts`` is the snapshot's timestamp, from the reader's
    injected clock.

    Either of ``joints`` / ``head_pose`` degrades to ``None`` when its reader
    was not supplied, raised, or itself returned ``None`` — a snapshot never
    raises.
    """

    joints: object | None = None
    head_pose: object | None = None
    ts: float = 0.0


def _safe_call(fn: Callable[[], object] | None) -> object | None:
    """Call ``fn`` and return its result, degrading any failure to ``None``.

    A missing reader (``fn is None``), a raising reader, or a reader that
    itself returns ``None`` all resolve the same way: ``None``. This is the
    "degrade, never crash" idiom shared with :class:`reachy.behavior.sense.DoaPoller`.
    """
    if fn is None:
        return None
    try:
        return fn()
    except Exception:
        return None


class StateReader:
    """Read a :class:`StateSnapshot` through injected reader callables.

    Callable as ``reader()`` (mirrors :class:`reachy.behavior.sense.DoaPoller`).
    ``joints_fn`` and ``head_pose_fn`` are zero-argument callables the caller
    binds to whatever client/session it already holds open — this module never
    opens one itself, so it opens no *second* SDK client no matter how the
    caller wires it. ``now`` is an injectable clock (default
    :func:`time.monotonic`) so tests can pin the timestamp deterministically.

    Any reader may be omitted (``None``), may raise, or may itself return
    ``None`` — every case degrades that one field to ``None`` rather than
    propagating an exception, so a dead/unavailable SDK surface never crashes
    a caller polling state.
    """

    def __init__(
        self,
        *,
        joints_fn: Callable[[], object] | None = None,
        head_pose_fn: Callable[[], object] | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._joints_fn = joints_fn
        self._head_pose_fn = head_pose_fn
        self._now = now

    def __call__(self) -> StateSnapshot:
        """Return one :class:`StateSnapshot`, degrading any reader failure to ``None``."""
        return StateSnapshot(
            joints=_safe_call(self._joints_fn),
            head_pose=_safe_call(self._head_pose_fn),
            ts=self._now(),
        )
