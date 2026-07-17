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

**Expected-trajectory sensing (the false-fire fix).** ``commanded_head`` is the
*target* of the last dispatched ``goto``, but a minjerk move takes >1 s in
transit — so measured against the target, the actual pose lags by construction
and ``actual − target`` reads as an external press even though nobody touched
the robot (this false-fired 147 phantom pats in 51 minutes on the live loop, in
wall-to-wall bursts: each reaction's resume move re-triggered the detector, a
self-sustaining loop). Gating variants failed live one after another: the
always-alive idle keeps a move in flight ~90 % of wall time, active wander
phases dispatch large yaw drifts nearly back-to-back, and the breathe/gaze
layers command multi-degree *pitch* jumps too (``breathe_pitch_deg=2`` +
``gaze_pitch_deg=10``) — every whole-move or per-axis mask ended up starving
real pats for minutes.

So the hook does not gate at all — it senses **against where the head should
be**. Using the previous tick's commanded pose (the move's start), the new
commanded pose (its target), the dispatch tick, and the loop's published
``busy_until`` horizon (the ``busy_horizon`` seam — ``() -> float``, see
:func:`reachy.motion.server.run`'s ``busy`` argument, wired in
:func:`reachy.cli._commands.listen._run_sdk_loop`), it evaluates the minjerk
profile at ``now`` and feeds the *expected* pose to the detector as the
commanded baseline. A head tracking its plan reads ≈ 0 deviation on both axes
at every instant of every move; a hand reads as pure external force — even
mid-move. The head pose is read and sensed every tick, so detection is never
starved. Only the very first tick (no previous commanded pose, an in-flight
move of unknown start) rides to the horizon unsensed. The ``on_tick`` contract
``(transport, queue, t, commanded_head)`` is unchanged — the seam is a
constructor argument, so the other folded hooks and
:class:`~reachy.motion.listen_hooks.HookChain` need no change.

**Re-baseline on resume.** After a reaction window closes, the first sensing
pass calls :meth:`PatDetector.reset` before feeding the fresh reading, so the
settled pose seeds a clean zero-deviation baseline. The post-reaction resume
move (idle wander) is an ordinary tracked dispatch — its transit reads ≈ 0
against the expected profile — so the resume move can never re-trigger a pat.

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


#: Commanded-pose jump (deg, per axis) above which an observed dispatch also RESETS
#: the detector's press accumulation. Live autopsies showed phantom fires landing
#: exactly on dispatch-observation ticks (t0 == fire tick, head already at the new
#: target while the expectation still pointed at the start) — boundary artifacts.
#: A press edge counted before a large dispatch must not pair with one counted
#: after it; a real hand re-accumulates its edges within a second of quiet, while
#: a boundary artifact never survives its own dispatch tick. Sub-degree breathe
#: dispatches don't reset, so pats on a calmly-breathing robot detect instantly.
LARGE_MOVE_THRESHOLD_DEG: float = 1.0

#: Cold-start warmup (seconds): once per process the EMA baselines must learn the
#: resting gravity/calibration sag (a live head rests several degrees off its
#: commanded neutral) — events fired while they are still learning are discarded.
WARMUP_SECONDS: float = 3.0

#: Live-fold detector sensitivity ("we can't have it that sensitive" — operator,
#: 2026-07-17): the folded hook defaults to firmer press thresholds than the
#: standalone ``pat`` noun, because the live loop's own motion + gravity leave
#: more residual deviation than a bench setup. A real scratch measures ~20°, so
#: the margin stays enormous. Override per-run via ``--press-threshold``.
LIVE_PRESS_THRESHOLD_DEG: float = 2.5
LIVE_YAW_PRESS_THRESHOLD_DEG: float = 6.0

#: Post-horizon landing grace (seconds): the live SDK's blocking goto returns at
#: ~plan duration with the head still ≈20 % short on large swings (measured ~5°
#: at return on 25° yaw moves), closing over the next half-second. The expectation
#: profile is stretched by this margin so the physical close-out reads as transit,
#: not as an external press.
LANDING_GRACE_SECONDS: float = 0.8


def minjerk_progress(tau: float) -> float:
    """The minimum-jerk position profile ``s(τ) = 10τ³ − 15τ⁴ + 6τ⁵`` on [0, 1].

    The same smooth profile the SDK's ``goto`` planner interpolates with — used
    to compute where a dispatched move *should* have the head at a given moment,
    so deviation is measured against the plan rather than the final target.
    Clamped: ``τ ≤ 0 → 0``, ``τ ≥ 1 → 1``.
    """
    if tau <= 0.0:
        return 0.0
    if tau >= 1.0:
        return 1.0
    return tau * tau * tau * (10.0 + tau * (-15.0 + 6.0 * tau))


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
        ``busy`` argument). When the hook observes the commanded pose *change*
        between ticks (a dispatch), it records the move — start (the previous
        commanded pose), target, dispatch tick, and this horizon — and from then
        on feeds the detector the **expected** pose along the minjerk profile
        instead of the raw target, so clean transit reads ≈ 0 deviation on both
        axes and a hand reads as pure external force even mid-move. On the very
        first tick (no previous pose; an in-flight move of unknown start) sensing
        rides to the horizon unfed. ``None`` (the default, used by the
        direct-seam unit tests) feeds the raw commanded pose as before.
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
        buffer: object | None = None,
        warmup: float = 0.0,
    ) -> None:
        self.queue = queue
        self.detector = detector if detector is not None else PatDetector()
        self.reaction = PatReaction(queue=queue)
        #: Optional seam: the loop's busy_until horizon for the move currently in flight.
        self._busy_horizon = busy_horizon
        #: The commanded head pose seen last tick (None before the first tick).
        self._prev_commanded: dict[str, float] | None = None
        #: The tracked in-flight move: start pose, target pose, dispatch tick, horizon.
        self._move_start: dict[str, float] | None = None
        self._move_target: dict[str, float] | None = None
        self._move_t0 = 0.0
        self._move_end = 0.0
        #: Loop-clock time until which sensing is skipped because the in-flight
        #: move's start is unknown (only ever the pre-first-tick condition).
        self._unknown_move_until = 0.0
        #: Optional duck-typed cognition sink: ``feed_pat(kind, level) -> None``.
        self._buffer = buffer
        #: Wall-clock (loop-clock) time until which sensing is paused and the flag held.
        self._reacting_until = 0.0
        #: Whether the ``pat_active`` flag is currently raised by this hook.
        self._flag_up = False
        #: Set whenever sensing is suspended (reaction window / unknown move); the
        #: next sensing pass clears press accumulation so edges never pair across
        #: the suspension. The detector's EMA baselines are NEVER cleared — they
        #: hold the learned gravity/calibration sag (see PatDetector.clear_presses).
        self._needs_rebaseline = False
        #: Cold-start warmup duration (seconds; 0 disables). The live composition
        #: passes WARMUP_SECONDS so the EMA baselines learn the resting sag before
        #: events count; direct/bench constructions default to no warmup.
        self._warmup = warmup
        #: End of the cold-start warmup, stamped on the first sensed tick.
        self._warmup_until: float | None = None
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
        (avoid self-trigger). Outside it the head pose is read and sensed every
        tick: a commanded-pose *change* between ticks records the dispatched move
        (start = the previous commanded pose, target, dispatch tick, the
        published ``busy_horizon``), and deviation is measured against the
        **expected** pose along the minjerk profile — clean transit reads ≈ 0, a
        hand reads as external force even mid-move. Only the very first tick
        with a move already in flight (start unknown) rides to the horizon
        unsensed. The reaction window arms a re-baseline: the first sensing pass
        after it resets the detector so the settled pose reads as zero
        deviation. ``queue`` is the live loop queue (identical to the one this
        hook was constructed with); the parameter keeps the ``on_tick`` contract
        self-describing. ``commanded_head`` is the ``{"pitch": float, "yaw":
        float}`` head pose the loop last dispatched (defaults to neutral before
        the loop has commanded any move).
        """
        cmd = commanded_head or _NEUTRAL_HEAD
        # Dispatch tracking runs on EVERY tick, including inside the reaction window —
        # the reaction's own lean/nuzzle/settle moves and the idle-resume move must
        # keep the previous-commanded / tracked-move state fresh, or the first
        # post-window expectation interpolates from a stale start and its bogus
        # deviation re-seeds a phantom reaction chain.
        self._note_dispatch(cmd, t)
        if t < self._reacting_until:
            # Executing our own reaction lean — hold the flag, do not sense, and mark
            # that the detector must re-baseline once sensing resumes.
            self._needs_rebaseline = True
            return
        if self._flag_up:
            pat_signal.clear()
            self._flag_up = False
        if t < self._unknown_move_until:
            # A move dispatched before our first tick is in flight and we do not know
            # where it started — ride it out, then re-baseline.
            self._needs_rebaseline = True
            return
        self._sense_and_maybe_react(transport, t, cmd)

    def _note_dispatch(self, cmd: dict[str, float], t: float) -> None:
        """Track the commanded pose across ticks; record a dispatch as a move.

        Any commanded change between ticks means the loop dispatched a ``goto``
        whose start is the previous commanded pose and whose flight ends at the
        published ``busy_horizon`` — everything needed to evaluate the expected
        minjerk pose at later ticks. The first-ever tick has no previous pose: if
        a move is in flight then, its start is unknown, so sensing rides to the
        horizon instead (the only unsensed window). Without a ``busy_horizon``
        seam (the direct-seam unit tests) no move is ever tracked and the raw
        commanded pose is fed, as before.
        """
        prev = self._prev_commanded
        current = {
            "pitch": float(cmd.get("pitch", 0.0)),
            "yaw": float(cmd.get("yaw", 0.0)),
        }
        self._prev_commanded = current
        if self._busy_horizon is None:
            return
        if prev is None:
            self._unknown_move_until = max(self._unknown_move_until, self._busy_horizon())
            return
        if current["pitch"] != prev["pitch"] or current["yaw"] != prev["yaw"]:
            self._move_start = prev
            self._move_target = current
            self._move_t0 = t
            # Stretch the expectation past the published horizon: the blocking
            # goto returns with the head still closing the last stretch of large
            # swings (see LANDING_GRACE_SECONDS) — that close-out is transit too.
            self._move_end = self._busy_horizon() + LANDING_GRACE_SECONDS
            # Tick-level ground truth for the phantom hunt (info-level): what the
            # hook observed at each dispatch, incl. the horizon it was handed.
            logger.info(
                "dispatch observed: t=%.2f cmd %s->%s horizon=%.2f (+%.2fs)",
                t,
                prev,
                current,
                self._move_end,
                self._move_end - t,
            )
            if (
                abs(current["pitch"] - prev["pitch"]) > LARGE_MOVE_THRESHOLD_DEG
                or abs(current["yaw"] - prev["yaw"]) > LARGE_MOVE_THRESHOLD_DEG
            ):
                # A large move is starting: press edges counted before it must not
                # pair with edges counted after — dispatch-boundary deviations are
                # artifacts of the commanded/actual handoff, not a hand. Clear the
                # accumulation (KEEPING the learned gravity/sag baselines); a real
                # press re-earns its edges within a second.
                self.detector.clear_presses()

    def _expected_head(self, now: float, cmd: dict[str, float]) -> tuple[float, float]:
        """Where the head *should* be at ``now``: the tracked move's minjerk pose.

        Falls back to the raw commanded pose when no move is tracked or the
        tracked move has landed. Public-ish for tests: scripting an actual pose
        that follows this value is exactly "a head tracking its plan".
        """
        start, target = self._move_start, self._move_target
        if start is None or target is None or now >= self._move_end:
            return (float(cmd.get("pitch", 0.0)), float(cmd.get("yaw", 0.0)))
        span = self._move_end - self._move_t0
        s = minjerk_progress((now - self._move_t0) / span) if span > 0 else 1.0
        return (
            start["pitch"] + (target["pitch"] - start["pitch"]) * s,
            start["yaw"] + (target["yaw"] - start["yaw"]) * s,
        )

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
        # The baseline is the EXPECTED pose along the in-flight move's minjerk
        # profile (== the raw commanded pose when nothing is in flight), so clean
        # transit reads ≈ 0 deviation and a hand reads as external force mid-move.
        commanded_pitch, commanded_yaw = self._expected_head(now, commanded_head)
        try:
            actual_pitch, actual_yaw = transport.head_pose()  # type: ignore[attr-defined]
        except CliError:
            actual_pitch, actual_yaw = commanded_pitch, commanded_yaw
        if self._needs_rebaseline:
            # First sensing pass after a suspension: clear stale press state so edges
            # never pair across it — but KEEP the EMA baselines (the learned gravity
            # sag); wiping them made the sag read as a fresh press until re-learned,
            # which was the phantom chains' true fuel.
            self.detector.clear_presses()
            self._needs_rebaseline = False
        event = self.detector.update(
            commanded_pitch, actual_pitch, commanded_yaw, actual_yaw, now=now
        )
        if self._warmup_until is None:
            # First sensed tick of the process: the EMA baselines start at zero and
            # need a few seconds to learn the resting gravity/calibration sag. Keep
            # feeding the detector (that IS the learning) but discard any event.
            self._warmup_until = now + self._warmup
        if now < self._warmup_until:
            if event is not None:
                self.detector.clear_presses()
            return
        if event is None:
            return
        level, touch_type = event
        # Detection autopsy (info-level; silent unless logging is configured): the
        # full expectation state at fire time, so a phantom detection in a live
        # journal explains itself instead of needing a reproduction.
        logger.info(
            "pat fire: %s/%s t=%.2f expected=(%.2f,%.2f) actual=(%.2f,%.2f) "
            "raw_cmd=(%.2f,%.2f) move=%s->%s t0=%.2f end=%.2f",
            touch_type,
            level,
            now,
            commanded_pitch,
            commanded_yaw,
            actual_pitch,
            actual_yaw,
            float(commanded_head.get("pitch", 0.0)),
            float(commanded_head.get("yaw", 0.0)),
            self._move_start,
            self._move_target,
            self._move_t0,
            self._move_end,
        )
        self.reaction.react(touch_type, level)
        if self._buffer is not None:
            try:
                self._buffer.feed_pat(touch_type, level)  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 — a raising buffer must never break the reflex
                logger.warning("PatHook buffer feed raised; cue dropped", exc_info=True)
        self.detector.clear_presses()
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
