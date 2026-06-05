"""Composable robot behaviors on a 50 Hz control loop.

The :mod:`reachy.behavior` package turns the Reachy Mini's three motion channels
(``head`` / ``antennas`` / ``body_yaw``) into something you can *layer*: a
persistent 50 Hz engine holds a set of active behaviors, each a pure function of
time producing a per-channel :class:`~reachy.behavior.model.Contribution`, and
arbitrates per channel using each behavior's contention class
(``passive`` / ``stoppable`` / ``unstoppable`` / ``stopping``). The winners are
composed into one complete pose every tick and streamed to the robot.

The pieces:

* :mod:`~reachy.behavior.model` — the pure data model (channels, classes,
  lifetimes, the :class:`Behavior` value object).
* :mod:`~reachy.behavior.arbitration` — the pure contention algorithm
  (:func:`arbitrate` per tick, :func:`admit` on add).
* :mod:`~reachy.behavior.library` — the built-in parametric behaviors.
* :mod:`~reachy.behavior.engine` — the 50 Hz compose loop.
* :mod:`~reachy.behavior.control` — the command-spool / state-file IPC.
* :mod:`~reachy.behavior.supervisor` — run the engine as a tracked process.

Pure standard library throughout; nothing here imports ``reachy_mini``.
"""

from __future__ import annotations

from reachy.behavior.model import CHANNELS, Behavior, Contribution, Lifetime, StopClass

__all__ = ["CHANNELS", "Behavior", "Contribution", "Lifetime", "StopClass"]
