"""Fold proprioceptive head-pat detection into the ``listen`` motion loop.

``listen`` already owns the single SDK media session and drives the serial
:class:`~reachy.motion.queue.MotionQueue` through :func:`reachy.motion.server.run`.
That loop reads the mic each tick *in-process*, so a head-pose read-back issued
from inside it is fast — fast enough to detect a pat. A *separate* ``pat`` process
cannot read the pose quickly: it contends with ``listen`` for the single-consumer
SDK client and gets throttled to roughly 1 Hz, far too slow for the
:class:`~reachy.motion.pat.PatDetector`. The two also fight over the head.

This module resolves both problems by providing :class:`PatHook` — a per-tick
hook (``(transport, queue, t, commanded_head) -> None``) that mirrors ``pat``'s
``_sense_and_maybe_react`` / ``_proprioceptive_loop`` logic exactly, but runs
*inside* ``listen``'s loop via :func:`reachy.motion.server.run`'s ``on_tick``
seam. On every tick it:

* reads the actual head pose back via ``transport.head_pose()`` (a
  :class:`~reachy.cli._errors.CliError` is treated as no deviation, never raised),
* feeds the commanded-vs-actual deviation to a :class:`PatDetector`, using the
  **actual commanded head pose** the loop last dispatched (handed in as
  ``commanded_head`` by the ``on_tick`` seam) as the commanded baseline — so
  ``listen``'s own non-neutral idle pose and sound-orienting turns read as zero
  deviation (the detector measures *external* force, ``actual − commanded``) and
  never false-fire a pat, and
* on a detection enqueues a calm lean→nuzzle→settle gesture via
  :class:`~reachy.motion.pat_reaction.PatReaction` onto the *same* queue the loop
  drives, writes the ``pat_active`` flag (so the ``listen`` idle wander yields for
  the whole reaction), and opens a **reaction window** of
  :func:`~reachy.motion.pat_reaction.reaction_duration` seconds during which it
  keeps the flag up and **stops sensing** — so the robot's own deliberate lean is
  never mistaken for a fresh pat.

The flag is always cleared on the way out (see :meth:`PatHook.close`), even if the
loop is interrupted mid-reaction. ``now`` is taken straight from the loop's clock,
so the hook inherits the loop's determinism with no extra clock seam.
"""

from __future__ import annotations

from reachy.cli._errors import CliError
from reachy.motion import pat_signal
from reachy.motion.pat import PatDetector
from reachy.motion.pat_reaction import PatReaction, reaction_duration
from reachy.motion.queue import MotionQueue

#: The pre-first-action commanded head pose ``listen`` rests at before it has
#: dispatched any move. The loop hands the *actual* last-dispatched head pose to
#: the hook each tick (see :meth:`PatHook.__call__`); this neutral default only
#: applies before the first move and as the no-deviation fallback when a head-pose
#: read-back raises.
_NEUTRAL_HEAD: dict[str, float] = {"pitch": 0.0, "yaw": 0.0}


class PatHook:
    """A per-tick ``on_tick`` hook detecting head pats inside ``listen``'s loop.

    Construct one with the :class:`~reachy.motion.queue.MotionQueue` the loop's
    executor drains, then pass :meth:`__call__` as ``on_tick=`` to
    :func:`reachy.motion.server.run`. Call :meth:`close` in the loop's ``finally``
    so the ``pat_active`` flag never leaks past the run.

    Parameters
    ----------
    queue:
        The shared serial queue the lean gesture is enqueued onto (the same one
        ``listen``'s producer submits sound-orient moves to).
    detector:
        An optional pre-built :class:`PatDetector` (tests inject one with an
        explicit ``level2_threshold_fn`` / tuned thresholds); a default detector
        is built when omitted.
    """

    def __init__(
        self,
        queue: MotionQueue,
        *,
        detector: PatDetector | None = None,
    ) -> None:
        self.queue = queue
        self.detector = detector if detector is not None else PatDetector()
        self.reaction = PatReaction(queue=queue)
        #: Wall-clock (loop-clock) time until which sensing is paused and the flag held.
        self._reacting_until = 0.0
        #: Whether the ``pat_active`` flag is currently raised by this hook.
        self._flag_up = False
        #: Count of pats detected this run (for diagnostics / tests).
        self.events = 0

    def __call__(
        self,
        transport: object,
        queue: MotionQueue,
        t: float,
        commanded_head: dict[str, float] | None = None,
    ) -> None:
        """One tick: clear an expired window, then sense + maybe react.

        While ``t`` is inside the reaction window the robot is executing its own
        lean — keep the ``pat_active`` flag up and do **not** read the head pose
        (avoid self-trigger). Once the window has elapsed, clear the flag and run
        one sensing pass. ``queue`` is the live loop queue (identical to the one
        this hook was constructed with); the parameter keeps the ``on_tick``
        contract self-describing. ``commanded_head`` is the
        ``{"pitch": float, "yaw": float}`` head pose the loop last dispatched — the
        baseline the detected deviation is measured against (defaults to neutral
        before the loop has commanded any move).
        """
        if t < self._reacting_until:
            return
        if self._flag_up:
            pat_signal.clear()
            self._flag_up = False
        self._sense_and_maybe_react(transport, t, commanded_head or _NEUTRAL_HEAD)

    def _sense_and_maybe_react(
        self, transport: object, now: float, commanded_head: dict[str, float]
    ) -> None:
        """Read the head pose, feed the detector, and react on a detection.

        Mirrors :func:`reachy.cli._commands.pat._sense_and_maybe_react`: a
        :class:`CliError` from ``head_pose`` is swallowed and treated as no
        deviation (the actual pose is taken to equal the commanded pose), so a
        transient transport drop degrades to "no pat" rather than killing the loop.
        The commanded baseline is ``commanded_head`` — the pose ``listen`` actually
        dispatched — so the detector measures only *external* force (``actual −
        commanded``) and ``listen``'s own idle/orient motion never false-fires. On
        an event it enqueues the lean, resets the detector, raises the
        ``pat_active`` flag, and opens the reaction window.
        """
        commanded_pitch = float(commanded_head.get("pitch", 0.0))
        commanded_yaw = float(commanded_head.get("yaw", 0.0))
        try:
            actual_pitch, actual_yaw = transport.head_pose()  # type: ignore[attr-defined]
        except CliError:
            actual_pitch, actual_yaw = commanded_pitch, commanded_yaw
        event = self.detector.update(
            commanded_pitch, actual_pitch, commanded_yaw, actual_yaw, now=now
        )
        if event is None:
            return
        level, touch_type = event
        self.reaction.react(touch_type, level)
        self.detector.reset()
        pat_signal.write()
        self._flag_up = True
        self._reacting_until = now + reaction_duration(level)
        self.events += 1

    def close(self) -> None:
        """Clear the ``pat_active`` flag if this hook still holds it (idempotent).

        Always safe to call: :func:`reachy.motion.pat_signal.clear` is a no-op
        when the flag is already absent. The ``listen`` loop calls this in its
        ``finally`` so an interrupt mid-reaction never leaks the flag.
        """
        if self._flag_up or pat_signal.is_active():
            pat_signal.clear()
        self._flag_up = False
