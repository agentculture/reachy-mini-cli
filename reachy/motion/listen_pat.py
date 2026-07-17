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
  never mistaken for a fresh pat, and
* optionally feeds the same detection to cognition — one cue per reaction cycle —
  via an injected duck-typed ``buffer`` (see :class:`PatHook`'s ``buffer``
  parameter and :meth:`~reachy.speech.events.EventBuffer.feed_pat`).

**Large-move gating (the false-fire fix, amplitude-aware).** ``commanded_head``
is the *target* of the last dispatched ``goto``, but a minjerk move takes >1 s in
transit — so during a commanded move the actual pose lags the target by
construction and ``actual − commanded`` reads as an external press even though
nobody touched the robot (this false-fired 147 phantom pats in 51 minutes on the
live loop, in wall-to-wall bursts: each reaction's resume move re-triggered the
detector, a self-sustaining loop). The gate must be **amplitude-aware**, not
binary: the always-alive idle layer keeps a move in flight ~90 % of wall time
(back-to-back 2.2 s holds/breaths), so "skip sensing whenever busy" silently
disables pat detection altogether (a real head scratch produced nothing on the
live robot). But only *large* jumps can false-fire — a commanded delta below the
detector's press threshold cannot generate transit deviation above it, and the
dominant idle dispatches are holds (delta 0) and sub-degree breaths.

So :class:`PatHook` takes an optional ``busy_horizon`` seam — ``() -> float``,
the loop's published ``busy_until`` for the move currently in flight (see
:func:`reachy.motion.server.run`'s ``busy`` argument, wired at the construction
site in :func:`reachy.cli._commands.listen._run_sdk_loop`) — and tracks the
per-tick ``commanded_head`` delta itself. When the commanded pose *jumps* by more
than :data:`LARGE_MOVE_THRESHOLD_DEG` (a look/turn/reaction-scale move was just
dispatched), sensing is suspended until **that move's** horizon passes; holds and
small breaths dispatched afterwards do not extend the suspension, so sensing
rides straight through the idle cadence. On the very first tick (no previous
commanded pose to diff against) an in-flight move of unknown size is ridden out
the same way. The ``on_tick`` contract ``(transport, queue, t, commanded_head)``
is unchanged — the seam is a constructor argument, so the other folded hooks and
:class:`~reachy.motion.listen_hooks.HookChain` need no change.

**Re-baseline on resume.** Whenever sensing is suspended — inside a reaction
window or while a large commanded move is in flight — the *first* sensing pass
once it resumes calls :meth:`PatDetector.reset` before feeding the fresh reading,
so the settled pose seeds a clean zero-deviation baseline. The post-reaction
resume move (idle wander) is a large jump itself, so its transit is skipped and
the re-baseline lands only once the head has actually settled — the resume move
can never re-trigger a pat.

The flag is always cleared on the way out (see :meth:`PatHook.close`), even if the
loop is interrupted mid-reaction. ``now`` is taken straight from the loop's clock,
so the hook inherits the loop's determinism with no extra clock seam.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from reachy.cli._errors import CliError
from reachy.motion import pat_signal
from reachy.motion.pat import PatDetector
from reachy.motion.pat_reaction import PatReaction, reaction_duration
from reachy.motion.queue import MotionQueue

logger = logging.getLogger(__name__)

#: The pre-first-action commanded head pose ``listen`` rests at before it has
#: dispatched any move. The loop hands the *actual* last-dispatched head pose to
#: the hook each tick (see :meth:`PatHook.__call__`); this neutral default only
#: applies before the first move and as the no-deviation fallback when a head-pose
#: read-back raises.
_NEUTRAL_HEAD: dict[str, float] = {"pitch": 0.0, "yaw": 0.0}

#: Commanded-pose jump (deg, max of |Δpitch| / |Δyaw| between ticks) above which the
#: just-dispatched move counts as *large* and its transit is ridden out unsensed.
#: Chosen just below the detector's default press threshold (1.2°): a commanded
#: delta smaller than this cannot generate transit deviation that clears the press
#: threshold, so sensing through those moves is false-fire-safe — and they are the
#: dominant idle dispatches (holds and sub-degree breaths), which is what keeps pat
#: detection alive under the always-alive idle cadence.
LARGE_MOVE_THRESHOLD_DEG: float = 1.0


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
    busy_horizon:
        An optional ``() -> float`` seam returning the loop's published
        ``busy_until`` — the wall-clock horizon (dispatch + duration + settle) the
        move currently in flight runs until (see :func:`reachy.motion.server.run`'s
        ``busy`` argument). The hook reads it only when it observes a *large*
        commanded jump (> ``large_move_threshold``) between ticks — or on the very
        first tick, when the in-flight move's size is unknown — and suspends
        sensing until that horizon passes. Holds and small breaths dispatched
        afterwards do not extend the suspension, so sensing rides through the
        always-alive idle cadence instead of being starved by it. ``None`` (the
        default, used by the direct-seam unit tests) senses every tick as before.
    large_move_threshold:
        Commanded-jump size (deg) above which a dispatch counts as large. Default
        :data:`LARGE_MOVE_THRESHOLD_DEG`.
    buffer:
        An optional duck-typed cognition sink exposing ``feed_pat(kind, level)``
        (the shape of :meth:`~reachy.speech.events.EventBuffer.feed_pat`) — kept
        loose rather than typed as ``EventBuffer`` so this module does not need to
        import ``reachy.speech.events`` (mirrors how ``transport`` above is typed
        as ``object``). On every detection the hook calls
        ``buffer.feed_pat(touch_type, level)`` **once**, right alongside the
        reflex — the same reaction-window suppression that already limits
        detections to one per cycle naturally caps the cue to one per cycle too.
        The feed is fault-isolated: a raising buffer is logged and swallowed (see
        :meth:`_sense_and_maybe_react`), so a broken cognition sink can never stop
        the lean from being enqueued or the ``pat_active`` window from opening.
        ``None`` (the default) keeps this hook byte-identical to before — no cue,
        no buffer call, no behavior change.
    """

    def __init__(
        self,
        queue: MotionQueue,
        *,
        detector: PatDetector | None = None,
        busy_horizon: Callable[[], float] | None = None,
        large_move_threshold: float = LARGE_MOVE_THRESHOLD_DEG,
        buffer: object | None = None,
    ) -> None:
        self.queue = queue
        self.detector = detector if detector is not None else PatDetector()
        self.reaction = PatReaction(queue=queue)
        #: Optional seam: the loop's busy_until horizon for the move currently in flight.
        self._busy_horizon = busy_horizon
        self._large_move_threshold = large_move_threshold
        #: The commanded head pose seen last tick (None before the first tick).
        self._prev_commanded: dict[str, float] | None = None
        #: Loop-clock time until which sensing is suspended for a large move's transit.
        self._suppress_until = 0.0
        #: Optional duck-typed cognition sink: ``feed_pat(kind, level) -> None``.
        self._buffer = buffer
        #: Wall-clock (loop-clock) time until which sensing is paused and the flag held.
        self._reacting_until = 0.0
        #: Whether the ``pat_active`` flag is currently raised by this hook.
        self._flag_up = False
        #: Set whenever sensing is suspended (reaction window or motion-in-flight); the
        #: next sensing pass re-baselines the detector so the settled pose reads zero.
        self._needs_rebaseline = False
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
        (avoid self-trigger). Likewise, while a *large* commanded move is in
        flight (a jump > ``large_move_threshold`` between this tick's
        ``commanded_head`` and the last — or an in-flight move of unknown size on
        the very first tick), the actual pose lags the target by construction, so
        the hook skips the head-pose read until that move's ``busy_horizon``
        passes (transit is not a hand). Holds and small breaths never suspend
        sensing — their transit cannot clear the press threshold, and they are
        what the always-alive idle dispatches ~90 % of the time. Both suspensions
        arm a re-baseline: the first sensing pass once they lift resets the
        detector so the settled pose reads as zero deviation. ``queue`` is the
        live loop queue (identical to the one this hook was constructed with); the
        parameter keeps the ``on_tick`` contract self-describing. ``commanded_head``
        is the ``{"pitch": float, "yaw": float}`` head pose the loop last dispatched
        — the baseline the detected deviation is measured against (defaults to
        neutral before the loop has commanded any move).
        """
        if t < self._reacting_until:
            # Executing our own reaction lean — hold the flag, do not sense, and mark
            # that the detector must re-baseline once sensing resumes.
            self._needs_rebaseline = True
            return
        if self._flag_up:
            pat_signal.clear()
            self._flag_up = False
        cmd = commanded_head or _NEUTRAL_HEAD
        self._note_commanded_jump(cmd)
        if t < self._suppress_until:
            # A large move's transit is in flight: any deviation is lag, not a hand.
            # Skip the head-pose read and re-baseline once the move settles.
            self._needs_rebaseline = True
            return
        self._sense_and_maybe_react(transport, t, cmd)

    def _note_commanded_jump(self, cmd: dict[str, float]) -> None:
        """Track the commanded pose across ticks; arm suppression on a large jump.

        A jump larger than ``large_move_threshold`` means a look/turn/reaction-scale
        move was just dispatched — its transit would read as a phantom press, so
        sensing is suspended until the loop's ``busy_horizon`` for *that* move. The
        first-ever tick has no previous pose to diff against: if a move is in
        flight then, its size is unknown, so it is ridden out the same way. Small
        jumps (holds, breaths) never suspend. Without a ``busy_horizon`` seam (the
        direct-seam unit tests) nothing is ever suspended, as before.
        """
        prev = self._prev_commanded
        self._prev_commanded = {
            "pitch": float(cmd.get("pitch", 0.0)),
            "yaw": float(cmd.get("yaw", 0.0)),
        }
        if self._busy_horizon is None:
            return
        if prev is None:
            self._suppress_until = max(self._suppress_until, self._busy_horizon())
            return
        jump = max(
            abs(self._prev_commanded["pitch"] - prev["pitch"]),
            abs(self._prev_commanded["yaw"] - prev["yaw"]),
        )
        if jump > self._large_move_threshold:
            self._suppress_until = max(self._suppress_until, self._busy_horizon())

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
        commanded``) and ``listen``'s own idle/orient motion never false-fires. When
        a re-baseline is armed (this is the first sensing pass after a suspension)
        the detector is reset first, so the freshly-read settled pose seeds a clean
        zero-deviation baseline. On an event it enqueues the lean (the reflex,
        unconditional), then — if a ``buffer`` was injected — feeds the same
        ``(touch_type, level)`` as a cue via ``buffer.feed_pat``, wrapped in its own
        ``try/except`` so a raising buffer degrades to "no cue" and never prevents
        the reflex or the reaction window that follows. Finally it resets the
        detector, raises the ``pat_active`` flag, and opens the reaction window.
        """
        commanded_pitch = float(commanded_head.get("pitch", 0.0))
        commanded_yaw = float(commanded_head.get("yaw", 0.0))
        try:
            actual_pitch, actual_yaw = transport.head_pose()  # type: ignore[attr-defined]
        except CliError:
            actual_pitch, actual_yaw = commanded_pitch, commanded_yaw
        if self._needs_rebaseline:
            # First sensing pass after a suspension: clear stale detector state so the
            # settled pose seeds a fresh zero-deviation baseline (no self-trigger).
            self.detector.reset()
            self._needs_rebaseline = False
        event = self.detector.update(
            commanded_pitch, actual_pitch, commanded_yaw, actual_yaw, now=now
        )
        if event is None:
            return
        level, touch_type = event
        self.reaction.react(touch_type, level)
        if self._buffer is not None:
            try:
                self._buffer.feed_pat(touch_type, level)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — a raising buffer must never break the reflex
                logger.warning("PatHook buffer feed raised; cue dropped", exc_info=True)
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
