"""``reachy.sleep`` — graduated sleep/wake state for Reachy Mini.

The package models a robot that naturally grows sleepy after an idle period and
wakes immediately on stimulation.  Sub-modules:

* :mod:`reachy.sleep.state` — pure :class:`SleepStateMachine` (no I/O, no
  threads; clock injected via ``now=`` parameters).
"""
