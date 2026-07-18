"""Proprioceptive pat sense for the 50 Hz behavior engine.

The runtime process (the behavior engine) has no touch sensor. A pat sense is
therefore *proprioception*: a hand pressing the head makes the ACTUAL head pose
deviate from the COMMANDED head pose, and the vetted
:class:`~reachy.motion.pat.PatDetector` classifies that commanded-vs-actual
deviation into a ``(touch_type, level)`` event (``scratch``/``side_pat`` ×
``level1``/``level2``). This module turns that reading into a
:attr:`reachy.behavior.sense.Sense.pat_event` so a data-only rule — e.g. the
deployed ``when {field=pat, op=is_true} run thoughtful`` — fires on a real pat.

It is built from two cooperating pieces, deliberately split so the *cadence*
(who advances the detector, who reads the result, and when) is explicit:

* :class:`PatSenseDriver` — a :class:`~reachy.behavior.rule_engine.TickBus`
  driver (``callable(ctx) -> None``) that runs at the **END** of each tick. It
  reads the actual pose from an injected reader, takes the commanded pose from
  ``ctx.pose`` (this tick's streamed pose), advances the injected
  :class:`PatDetector`, and **latches** any resulting event.
* :meth:`PatSenseDriver.as_provider` / :meth:`PatSenseDriver.peek` — the
  zero-arg PEEK callable :class:`reachy.behavior.sense.SenseProviders` expects
  for its ``pat_event`` field: it returns the current latch without consuming
  it, so :func:`reachy.behavior.sense.read_perception` folds the pat cue into
  the tick's :class:`~reachy.behavior.sense.Sense` the same way it folds DoA.

--------------------------------------------------------------------------
r2 — cadence + one-tick latch semantics
--------------------------------------------------------------------------
The engine reads sense ONCE per tick at the *start* of the tick (see
:func:`reachy.behavior.engine._read_sense`, called before ``compose_tick``),
and it runs the ``tick_seam`` (the bus of drivers, this class among them) at the
*end* of the tick, after streaming. So within one engine tick the ordering is:

    tick N start:  read sense  -> peek(pat_event)   (the PROVIDER reads)
    tick N end:    tick_seam   -> PatSenseDriver()   (the DRIVER writes)

An event the driver latches at the end of tick ``N`` is therefore first observed
by the sense read at the *start* of tick ``N+1`` — and it must be observed by
**exactly one** sense snapshot. The latch is one-tick: every driver invocation
**clears the latch BEFORE processing**, then re-latches only if the detector
fires this tick. Tracing an event latched at the end of tick ``N``:

    end   N   : clear (was None) -> detect -> latch = (touch_type, level)
    start N+1 : peek -> (touch_type, level)   << the ONE sense that carries it
    end   N+1 : clear -> ... -> latch = None (no new pat)
    start N+2 : peek -> None

Because the provider is a pure peek (it never mutates), **multiple peeks within
the same tick return the identical value** — a rule that reads ``pat`` twice in
one tick sees one consistent reading. The clear-before-process rule holds on
*every* path, including the early returns below, so a stale latch can never leak
into a second tick's sense.

--------------------------------------------------------------------------
Ownership gate — the #66 phantom-pat fix (a hard requirement)
--------------------------------------------------------------------------
While a NON-base behavior owns the head channel (a rule-admitted gesture, a
goto), the driver **suspends** detection entirely: no detector update, no event
latching. Rationale: a deliberate gesture commands fast head motion that the
servos physically lag behind, so ``actual - commanded`` reads as a large
deviation even though nobody touched the robot — a phantom pat. That phantom
would trigger a reaction, which commands more motion, which reads as more
deviation: a self-sustaining oscillation. This is issue #66's false-fire loop
(the folded ``listen`` :class:`~reachy.motion.listen_pat.PatHook` fought the same
demon and gates the same way). Detection is safe only while the head is quiet —
which, at runtime, means owned by the engine's gentle ``feel-alive`` base layer
(slow breathing the servos track) or owned by nobody (a steady neutral pose).

"Base" is identified from ``ctx.ownership["head"]``, which is the owner's **id**
(a string) or ``None``. The engine mints every behavior id as ``f"{name}-{seq}"``
(:meth:`reachy.behavior.engine.Engine._next_id`), so the base layer's id is
``f"{BASE_LAYER_NAME}-{seq}"`` (e.g. ``"feel-alive-1"``); :func:`_is_base_owner`
recovers the library name by stripping the trailing numeric ``-<seq>`` and
compares it to :data:`~reachy.behavior.engine.BASE_LAYER_NAME` — robust to the
base name itself containing a hyphen (``feel-alive``) because only the *last*
segment is stripped, and it requires that last segment to be all digits so a
same-named-but-suffixed behavior can't masquerade as base. ``None`` (no behavior
owns the head this tick) is treated as **not suspended**: the composed head then
falls to a steady neutral pose, which is exactly the quiet state a pat is
detectable against.

On the FIRST tick after ownership returns to base — the suspended -> resumed
edge — the driver calls :meth:`PatDetector.clear_presses` before feeding any
fresh sample: press pairing/edge state from before the gesture is dropped so
edges can never pair across the suspension, while the EMA baselines — the
*learned* commanded-vs-actual frame offset — are KEPT. This is
``clear_presses``'s documented purpose; a full :meth:`PatDetector.reset` here
would wipe the baselines and make the offset read as fresh presses until the
slow EMA re-learns (~13 s) — the post-gesture ghost-fire chain both the folded
``listen`` hook and this driver's first live deployment hit (d1, issue #79).
The initial convergence at boot is instead covered by a one-time warmup mute
(:data:`DEFAULT_WARMUP_S`): the detector updates normally (that is the
learning) but events are not latched until the window passes.

--------------------------------------------------------------------------
r1 — frame / unit mapping (commanded vs actual)
--------------------------------------------------------------------------
The two pose sources are in different frames:

* ``ctx.pose["head"]`` is the engine's composed head offset — a six-axis dict
  ``{x, y, z, roll, pitch, yaw}`` in **degrees** (for rotation axes), and
  **neutral-relative**: behaviors compose offsets *from* the neutral head pose
  (see :mod:`reachy.behavior.model`). We take ``pitch`` and ``yaw``.
* :meth:`reachy.robot.state_reader.HeldStateReader.read` returns the ACTUAL
  ``(pitch_deg, yaw_deg)`` decomposed from the SDK's 4×4 head matrix — degrees,
  but **absolute** (relative to the SDK's own zero), and additionally carrying
  the live head's resting gravity/calibration sag.

So commanded and actual differ by a (roughly constant) frame offset plus sag.
That mismatch does **not** need explicit normalization here, because
:class:`PatDetector` already tracks a slow EMA baseline per axis
(``_baseline_offset += baseline_alpha * (raw_dev - _baseline_offset)``) and
subtracts it, so any *steady-state* offset — a constant frame difference, the
gravity sag — converges out and the corrected deviation is driven to zero. The
detector fires on *transients* (a hand's impulse), not absolute level. This is
verified in the tests by driving an identical relative pat pattern with and
without a constant frame offset and asserting the same event fires (the offset
must first be *learned*: the EMA needs a short warmup to converge, exactly as
the live folded hook warms up before counting events). A static offset is
absorbed; no explicit re-framing is applied.

--------------------------------------------------------------------------
Lag compensation — the d1 live fix (issue #79)
--------------------------------------------------------------------------
The first live deployment falsified an assumption the ownership gate rested
on: that the base layer's own motion is too small/slow to matter. It is not —
``feel-alive`` commands gaze wander of ±7° pitch / ±12° yaw (measured actual
pitch span on the robot: ~13°), and the servos track the streamed pose with a
transport+plant lag ``L``, so ``actual(t) ≈ commanded(t − L)``. Comparing
same-tick commanded vs actual therefore shows a deviation of roughly
``L × d(commanded)/dt`` throughout every wander swing — sustained for seconds,
comfortably past the detector's 1.2° press thresholds, while the detector's
deliberately slow EMA (≈6.7 s at 50 Hz) absorbs only the mean, not the
oscillation. Result: continuous phantom ``scratch``/``side_pat`` fires with
nobody touching the robot (deviation ledger d1).

The fix models the plant: the driver feeds the detector a **first-order
low-passed commanded pose** — ``filtered += dt/(lag_tau+dt) ×
(commanded − filtered)`` — instead of the raw same-tick commanded value.
``lag_tau`` (seconds, default :data:`DEFAULT_LAG_TAU`) approximates the lag of
the real head chasing the streamed target: for *continuous* commanded motion
the filtered commanded now moves like the physical head does, so the
tracking-lag deviation cancels; a real pat — an *external* force the command
stream knows nothing about — still produces a fast actual-vs-filtered spike
and fires exactly as before. The filter seeds at the first commanded sample
(so a static commanded pose behaves identically to the unfiltered driver from
tick one) and re-seeds on every detection-gap edge alongside the detector
re-baseline — the ownership suspended→resumed edge and the stillness
blocked→unblocked edge alike (see "Stillness gate churn fix" below).
``lag_tau=0`` disables the filter (raw passthrough).

--------------------------------------------------------------------------
Stillness gate churn fix — two flags, one re-baseline (PR #83, Qodo #3)
--------------------------------------------------------------------------
The ownership gate (above) and the stillness gate (#80, below) are
independent conditions — one tracks WHO drives the head, the other tracks
WHETHER the commanded pose is currently moving — so they are tracked by two
separate flags, :attr:`PatSenseDriver._suspended` and
:attr:`PatSenseDriver._stillness_blocked`, each owning exactly one
suspended/blocked → resumed/unblocked EDGE.

The first stillness-gate implementation (#80) reused ``_suspended`` for both
concepts. That was wrong in a way the tests didn't catch because the
*output* stayed correct: under continuous idle wander (the ordinary case)
the stillness gate is closed almost every tick, so ``_process`` set
``_suspended = True`` on every blocked tick; on the *very next* tick the
ownership resume block (which only checks "was ``_suspended`` true") saw
that flag set and ran its full re-baseline — ``clear_presses()``, filter
reseed, clock reseed — even though ownership had never changed. That
re-baseline then immediately set ``_suspended = True`` again via the
stillness check. Net effect: the ownership resume path, meant to fire *once*
per genuine suspension, ran on *every* tick of ordinary wander at 50 Hz —
avoidable per-tick work, and a state machine where a real ownership edge was
indistinguishable from this routine thrash.

The fix keeps ``_suspended`` exclusive to the ownership gate and gives the
stillness gate its own ``_stillness_blocked`` flag with its own edge-detected
resume block. Both edges call the same :meth:`PatSenseDriver._rebaseline_after_gap`
helper (``clear_presses`` + filter/high-pass/clock reseed, baselines kept)
because both represent the identical situation from the detector's point of
view — a run of ticks where :meth:`PatDetector.update` was never called; see
that method's own docstring for the full reasoning.

--------------------------------------------------------------------------
Degradation
--------------------------------------------------------------------------
Every failure degrades to "no reading" and never raises out of the driver or
the provider, mirroring :func:`reachy.behavior.sense._peek`'s posture and the
never-raise contract of :class:`reachy.robot.state_reader.HeldStateReader`:

* a reader that returns ``None`` (SDK disconnected / absent) or raises -> the
  tick is skipped, no update, no latch;
* a missing/``None`` ``ctx.pose`` (shouldn't happen once t1 is wired, but
  defended) -> the tick is skipped;
* any other unexpected error inside a tick -> logged once and swallowed; the
  latch stays cleared (``None``) for that tick.

Determinism: the detector, reader, and clock are all injected seams. Time comes
from ``ctx.now`` (the engine's injected monotonic clock), so the driver inherits
the loop's determinism with no extra clock of its own — the same choice
:class:`reachy.motion.listen_pat.PatHook` makes.

Stdlib plus :mod:`reachy.motion.pat` (numpy) and the base-layer name constant
from :mod:`reachy.behavior.engine`; the reader is duck-typed (a zero-arg
``read()``), so this module imports neither ``reachy_mini`` nor the transport.
"""

from __future__ import annotations

import logging
from typing import Callable

from reachy.behavior.engine import BASE_LAYER_NAME
from reachy.motion.pat import PatDetector

logger = logging.getLogger(__name__)

#: A zero-arg reader returning the ACTUAL head pose as ``(pitch_deg, yaw_deg)``,
#: or ``None`` when there is no reading. Duck-typed exactly like
#: :meth:`reachy.robot.state_reader.HeldStateReader.read` — this module never
#: names the concrete class, so it needs no ``reachy_mini`` / transport import.
PoseReader = Callable[[], "tuple[float, float] | None"]

#: The pat-event peek the provider hands back: ``(touch_type, level)`` or ``None``.
PatEvent = "tuple[str, str] | None"

#: Default commanded-pose low-pass time constant (seconds) — the lag-compensation
#: filter's ``tau`` (see "Lag compensation" in the module docstring). Approximates
#: the daemon+servo lag of the real head chasing the 50 Hz streamed target;
#: measured live: wander-induced phantom deviations vanish at ~0.3 s while a real
#: pat's external impulse (much faster than tau) still spikes through. ``0``
#: disables the filter.
DEFAULT_LAG_TAU = 0.3

#: Deviation high-pass time constant (seconds). Removes the offset/sag and the
#: slow wander residual from the deviation; chosen — together with the tuned
#: thresholds below — by an offline replay grid over a 2250-tick
#: commanded-vs-actual recording from the real robot (see issue #79): the
#: viable band was hp_tau 0.5-0.8 x press 1.8-2.2, and (0.8, 2.0) sits centred
#: in it (zero ghost fires on the recording, 6/6 synthetic pats detected).
DEFAULT_HP_TAU = 0.8

#: Runtime-tuned press/release thresholds (degrees) for the driver's DEFAULT
#: detector. The stock PatDetector thresholds (1.2/0.5) were tuned for listen's
#: STATIC commanded pose; under the runtime's continuous feel-alive wander the
#: measured plant (~0.28 s lag, 1.1-1.2x underdamped overshoot) leaves 100-400 ms
#: conditioned-residual transients up to ~1.8 deg — overshoot RINGING at wander
#: reversals, spectrally inside the pat band, so amplitude is the remaining
#: discriminator. Cost, stated plainly: very gentle pats (<~2 deg deflection)
#: are missed; the hands-on tuning pass (issue #79 follow-up) refines with real
#: pat data. Injected detectors (tests, listen-era callers) are unaffected.
DEFAULT_PRESS_THRESHOLD = 0.5
DEFAULT_RELEASE_THRESHOLD = 0.2

#: The STILLNESS GATE (issue #80). Detection runs only while the COMMANDED head
#: pose has been constant for :data:`DEFAULT_STILL_HOLD_S` seconds, judged with a
#: :data:`DEFAULT_STILL_EPS` tolerance per axis. Measured on the real robot (four
#: 30-50 s recordings, untouched vs petted, all six DOF):
#:
#:   head HELD STILL : untouched residual p99 0.07-0.11 deg, petting p90 0.85-1.90
#:                     -> 12-20x separation on EVERY axis; the shipped detector
#:                        scores 0 false fires / 30 s and 8-10 detections / 50 s
#:                        at any threshold from 0.3 to 2.0.
#:   head WANDERING  : untouched residual p99 3.3-4.0 deg, petting p90 2.4-3.1
#:                     -> 0.7-2.0x separation; pats are NOT separable by amplitude
#:                        on any axis, including the uncommanded ones (roll/x/y get
#:                        dragged ~11x noisier by mechanical coupling).
#:
#: The plant is quiet only when it is not tracking a moving target (servo hunting,
#: not lag — a fitted 40-tap FIR plant model bought just 1.1x, and the residual is
#: uncorrelated with commanded velocity). So stillness is a PRECONDITION for the
#: sense, not a tuning knob: gating on it makes ghost fires structurally impossible
#: while leaving a still robot fully pettable.
#: Tolerance for "the commanded pose did not change" (degrees, per tick, per
#: axis). A genuinely still command is EXACTLY constant, so this only needs to
#: absorb float noise — it must stay well under the per-tick change of the idle
#: wander (~0.03-0.06 deg/tick at 50 Hz), or the gate creeps open at the wander's
#: turning points where velocity momentarily crosses zero while the plant is
#: still ringing from the preceding swing.
DEFAULT_STILL_EPS = 0.01
DEFAULT_STILL_HOLD_S = 0.5  # commanded must be quiet this long before sensing

#: Nominal tick period (seconds) used for the filter step when ``ctx.now`` is
#: unavailable — the engine's 50 Hz default.
_NOMINAL_DT = 0.02

#: BOOT-ONLY warmup (seconds) during which detected events are MUTED while the
#: detector's EMA baseline first converges. The EMA (``baseline_alpha`` 0.003
#: at 50 Hz) has a ~6.7 s time constant; learning the several-degree
#: commanded-vs-actual frame offset down past the release threshold takes ~2x
#: that, and until then offset + wander edges read as presses — the fire at
#: boot observed live (d1, issue #79). The detector keeps UPDATING through
#: warmup (that update IS the convergence); only latching is muted. Resume
#: edges do NOT re-arm this: they call ``clear_presses()``, which keeps the
#: learned baselines, so there is no post-gesture deadzone.
DEFAULT_WARMUP_S = 15.0


def _is_base_owner(owner_id: str | None) -> bool:
    """Whether ``owner_id`` names the engine's seeded base layer (``feel-alive``).

    Ids are minted as ``f"{name}-{seq}"`` by
    :meth:`reachy.behavior.engine.Engine._next_id`, so the base layer's id is
    ``f"{BASE_LAYER_NAME}-{seq}"`` (e.g. ``"feel-alive-1"``). The library name is
    recovered by stripping the trailing numeric ``-<seq>`` and compared to
    :data:`BASE_LAYER_NAME`. ``None`` (no owner this tick) is NOT the base layer.
    The digit guard means a differently-suffixed behavior (id ``"feel-alive"``
    with no numeric seq, or ``"feel-alive-foo-2"``) is not mistaken for base.
    """
    if owner_id is None:
        return False
    name, _, seq = owner_id.rpartition("-")
    return bool(seq) and seq.isdigit() and name == BASE_LAYER_NAME


class PatSenseDriver:
    """A ``TickBus`` driver that turns proprioceptive pats into a ``pat_event`` cue.

    Construct one with the actual-pose ``reader`` (a zero-arg ``read()``
    returning ``(pitch_deg, yaw_deg) | None``), then register :meth:`__call__`
    as a driver on the engine's ``tick_seam`` and wire :meth:`as_provider` (or
    :meth:`peek` directly) as ``SenseProviders(pat_event=...)``. Every tick the
    driver reads the actual pose, takes the commanded head pose from
    ``ctx.pose``, advances the detector, and latches any event for the NEXT
    tick's single sense read (see the module docstring for the full cadence,
    ownership gate, frame mapping, and degradation contract).

    Parameters
    ----------
    reader:
        Zero-arg actual-pose reader (``() -> (pitch_deg, yaw_deg) | None``). In
        production a :class:`reachy.robot.state_reader.HeldStateReader`'s
        ``read``; in tests, a fake. A ``None`` return or any raise degrades to
        "no reading" for that tick.
    detector:
        The :class:`PatDetector` to advance. Defaults to a fresh
        ``PatDetector()``; inject one (with a fixed ``level2_threshold_fn`` /
        tuned thresholds) for deterministic tests. Reused, never reimplemented.
    """

    def __init__(
        self,
        *,
        reader: PoseReader,
        detector: PatDetector | None = None,
        lag_tau: float = DEFAULT_LAG_TAU,
        hp_tau: float = DEFAULT_HP_TAU,
        warmup_s: float = DEFAULT_WARMUP_S,
        still_eps: float = DEFAULT_STILL_EPS,
        still_hold_s: float = DEFAULT_STILL_HOLD_S,
    ) -> None:
        self._reader = reader
        self.detector = (
            detector
            if detector is not None
            else PatDetector(
                press_threshold=DEFAULT_PRESS_THRESHOLD,
                release_threshold=DEFAULT_RELEASE_THRESHOLD,
                yaw_press_threshold=DEFAULT_PRESS_THRESHOLD,
                yaw_release_threshold=DEFAULT_RELEASE_THRESHOLD,
            )
        )
        #: Commanded-pose low-pass time constant (s); ``0`` = raw passthrough.
        self._lag_tau = max(0.0, float(lag_tau))
        #: Deviation high-pass time constant (s); ``0`` = raw deviation through.
        self._hp_tau = max(0.0, float(hp_tau))
        #: The high-pass state: low-passed deviation ``(pitch, yaw)``. Starts at
        #: zero — the boot warmup covers its convergence onto the real offset —
        #: and goes ``None`` on a resume re-seed (seed at the next sample, so a
        #: settled post-gesture offset can never read as a step).
        self._dev_lp: tuple[float, float] | None = (0.0, 0.0)
        self._hp_last_now: float | None = None
        #: Stillness gate (issue #80): commanded-change tolerance and the quiet
        #: hold required before sensing resumes. ``still_hold_s <= 0`` disables.
        self._still_eps = max(0.0, float(still_eps))
        self._still_hold_s = max(0.0, float(still_hold_s))
        #: Last commanded (pitch, yaw) and the clock reading when it last moved.
        self._last_cmd: tuple[float, float] | None = None
        self._last_motion_t: float | None = None
        #: Post-(re)baseline event-mute window (s); ``0`` disables (see d1 fix).
        self._warmup_s = max(0.0, float(warmup_s))
        #: Mute latching until this clock reading (armed at first update + on
        #: every resume re-baseline); ``None`` = not armed / warmup disabled.
        self._warmup_until: float | None = None
        #: True until the first detector update arms the initial warmup.
        self._first_update = True
        #: The filter state: low-passed commanded ``(pitch, yaw)``, or ``None``
        #: before the first sample / after a resume re-seed (see d1 fix).
        self._filtered: tuple[float, float] | None = None
        #: The previous tick's clock reading, for the filter's ``dt``.
        self._last_now: float | None = None
        #: The one-tick latch: ``(touch_type, level)`` or ``None`` (see r2).
        self._latch: tuple[str, str] | None = None
        #: True while a non-base behavior owns the head (detection suspended); the
        #: suspended -> resumed edge triggers a detector re-baseline (see #66 gate).
        self._suspended = False
        #: True while the commanded pose is not yet still long enough to sense
        #: (the stillness gate, #80). Deliberately a SEPARATE flag from
        #: ``_suspended``: ownership and stillness open/close on independent
        #: conditions, and under ordinary continuous wander this flag flips
        #: open/closed every tick while ownership never changes at all —
        #: collapsing the two into one flag made the ownership resume block
        #: run on every such tick instead of once per real edge (the churn
        #: fixed by PR #83 / Qodo #3; see the module docstring). The blocked
        #: -> unblocked edge triggers the SAME re-baseline as the ownership
        #: edge, via :meth:`_rebaseline_after_gap`.
        self._stillness_blocked = False
        #: Count of pats latched this run (diagnostics / tests).
        self.events = 0

    # ------------------------------------------------------------------
    # TickBus driver entry point
    # ------------------------------------------------------------------

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """One tick (END of tick): clear the latch, gate, sense, and maybe re-latch.

        Never raises: the latch is cleared first (a plain assignment that cannot
        fail, preserving the one-tick contract on every path), then the sensing
        body runs under a broad guard so a misbehaving reader / detector degrades
        to "no pat this tick" rather than propagating (the engine's ``TickBus``
        also isolates drivers, but the never-raise guarantee is the driver's own).
        """
        # Clear-before-process: an event latched last tick has already been read
        # by this tick's start-of-tick sense; drop it so it can never leak into a
        # second sense snapshot. Holds on EVERY path below, including early exits.
        self._latch = None
        try:
            self._process(ctx)
        # A sense tap must never crash the loop.
        except Exception:  # noqa: BLE001
            logger.warning("PatSenseDriver tick raised; pat cue dropped", exc_info=True)

    def _process(self, ctx) -> None:  # type: ignore[no-untyped-def]
        """The gated sensing body, split out so :meth:`__call__` stays a thin guard."""
        # --- ownership gate (#66) --------------------------------------
        owner = self._head_owner(ctx)
        if owner is not None and not _is_base_owner(owner):
            # A non-base behavior drives the head: its commanded motion lags in
            # the servos and reads as force. Suspend detection entirely.
            self._suspended = True
            return
        if self._suspended:
            # First tick back on the base layer (a genuine ownership edge —
            # this branch only runs once per suspension; see the module
            # docstring's "Stillness gate churn fix" for why that is now
            # guaranteed). Re-baseline: clear press pairing/edge state, keep
            # the learned EMA baselines, re-seed the lag filter + high-pass +
            # their clocks. See :meth:`_rebaseline_after_gap` for the full
            # reasoning (shared verbatim with the stillness edge below).
            self._rebaseline_after_gap()
            self._suspended = False

        # --- commanded pose (this tick's streamed head offset, r1) -----
        commanded = self._commanded_pitch_yaw(ctx)
        if commanded is None:
            return  # a missing/malformed ctx.pose -> skip this tick

        # --- stillness gate (#80) --------------------------------------
        # The plant is only quiet while it is NOT tracking a moving target, so
        # sensing is confined to commanded-still windows. This makes the wander
        # ghost class structurally impossible rather than threshold-managed.
        #
        # `_stillness_blocked` is tracked SEPARATELY from `_suspended` (PR #83,
        # Qodo #3): reusing `_suspended` here made the ownership resume block
        # above run every tick during ordinary wander (the gate closes and
        # reopens every tick while ownership never changes) instead of once per
        # real ownership edge. See the module docstring for the full story.
        if not self._commanded_still(commanded, self._now(ctx)):
            self._stillness_blocked = True
            return
        if self._stillness_blocked:
            # First tick back on a still commanded pose (a genuine
            # blocked -> unblocked edge). `detector.update()` was never called
            # for the whole blocked stretch (this method returned above, every
            # tick), so from the detector's point of view this is the same
            # situation as the ownership edge: re-baseline once, via the same
            # helper, for the same reasons.
            self._rebaseline_after_gap()
            self._stillness_blocked = False

        # --- actual pose (proprioception) ------------------------------
        actual = self._read_actual()
        if actual is None:
            return  # reader disconnected / absent / raised -> no reading

        now = self._now(ctx)
        if self._first_update:
            # Boot: the deviation conditioning below needs ~3x its time constants
            # to converge before events mean anything — mute latching meanwhile.
            self._first_update = False
            self._arm_warmup(now)
        commanded_pitch, commanded_yaw = self._lag_filtered(commanded, now)
        actual_pitch, actual_yaw = actual
        # Deviation conditioning (see "Lag compensation + wander rejection"):
        # high-pass the lag-compensated deviation so the wander band (offset,
        # slow gain/lag residual) vanishes while a pat's fast presses pass.
        dev_pitch, dev_yaw = self._highpassed(
            actual_pitch - commanded_pitch, actual_yaw - commanded_yaw, now
        )
        event = self.detector.update(
            0.0,
            dev_pitch,
            0.0,
            dev_yaw,
            now=now,
        )
        if event is None:
            return
        if self._in_warmup(now):
            # The detector keeps updating through warmup (that IS the baseline
            # convergence) — only the event is dropped, never silently: name it.
            logger.debug("pat event dropped: warmup (baseline converging)")
            return
        # PatDetector yields ``(level, touch_type)``; Sense.pat_event is
        # ``(touch_type, level)`` (matching EventBuffer.feed_pat(kind, level)).
        level, touch_type = event
        self._latch = (touch_type, level)
        self.events += 1

    def _rebaseline_after_gap(self) -> None:
        """Re-baseline after a detection gap — shared by BOTH resume edges.

        The ownership suspended → resumed edge and the stillness blocked →
        unblocked edge look different from the outside (one is about who
        drives the head, the other about whether the commanded pose is
        moving) but they are the identical situation from the detector's
        point of view: a run of ticks — long or short — during which
        :meth:`PatDetector.update` was never called, because :meth:`_process`
        returned early on every one of them. Whatever the detector's
        press/edge state and the driver's own filters were doing at the
        moment the gap opened is now stale relative to whatever comes next,
        so both edges perform the same reset:

        * :meth:`PatDetector.clear_presses` — drop press pairing/edge state
          so it can never pair across the gap, but KEEP the learned EMA
          baselines. This is deliberate and load-bearing (not a mistake to
          "improve" into a full :meth:`PatDetector.reset`): a hand can be
          mid-press at the exact tick a gap opens on EITHER edge — a gesture
          can grab the head mid-pat, and a pat happens on ACTUAL while the
          stillness gate reacts only to COMMANDED motion, so the two are
          uncorrelated and a pat straddling a stillness-gate close/open is
          equally possible. ``pat.py``'s ``clear_presses`` docstring explains
          why ``reset`` must not be used here: wiping the baselines makes the
          learned frame offset read as a fresh press until the slow EMA
          (~6.7 s time constant) reconverges — the exact post-gesture ghost
          chain issue #79 hit live.
        * ``_filtered = None`` / ``_dev_lp = None`` — re-seed the lag filter
          and the deviation high-pass at the very next sample instead of
          slewing toward it from a stale value. Both integrate their input
          over time, and a gap can span a large commanded-pose excursion (a
          gesture, or several seconds of idle wander), so resuming without a
          reseed would replay a spurious step transient into the very
          detector the #79 lag-compensation fix exists to protect.
        * ``_last_now = None`` / ``_hp_last_now = None`` — keep the ``dt``
          clocks consistent with the filter/high-pass state they time: left
          stale, the first post-gap step would compute ``dt`` against a
          pre-gap timestamp (clamped to 0.2 s and therefore harmless, but
          not predictable — a colleague review finding on the original
          ownership edge, carried over here for the identical reason).

        Boot warmup (:data:`DEFAULT_WARMUP_S`) is deliberately NOT re-armed
        here, on either edge: it is a one-time convergence window for the
        EMA baselines, which this method never touches.
        """
        self.detector.clear_presses()
        self._filtered = None
        self._dev_lp = None
        self._last_now = None
        self._hp_last_now = None

    def _highpassed(
        self, dev_pitch: float, dev_yaw: float, now: float | None
    ) -> tuple[float, float]:
        """High-pass the per-axis deviation: subtract its own first-order low-pass.

        The slow low-pass tracks everything in the wander band — the frame
        offset, gravity sag, the plant's gain error and residual lag — so what
        remains is the fast band a hand's presses live in. ``hp_tau == 0``
        disables (raw deviation through). Seeds at the first sample (hp = 0)
        and re-seeds after a resume (the gesture invalidated the state).
        """
        if self._hp_tau <= 0.0:
            return (dev_pitch, dev_yaw)
        if now is not None and self._hp_last_now is not None:
            dt = min(max(now - self._hp_last_now, 0.0), 0.2)
        else:
            dt = _NOMINAL_DT
        if now is not None:
            self._hp_last_now = now
        if self._dev_lp is None:  # resume re-seed: adopt the settled deviation
            self._dev_lp = (dev_pitch, dev_yaw)
            return (0.0, 0.0)
        k = dt / (self._hp_tau + dt)
        lp_pitch = self._dev_lp[0] + k * (dev_pitch - self._dev_lp[0])
        lp_yaw = self._dev_lp[1] + k * (dev_yaw - self._dev_lp[1])
        self._dev_lp = (lp_pitch, lp_yaw)
        return (dev_pitch - lp_pitch, dev_yaw - lp_yaw)

    def _commanded_still(self, commanded: tuple[float, float], now: float | None) -> bool:
        """Whether the COMMANDED pose has been constant long enough to sense.

        Any per-axis change beyond ``still_eps`` restamps the motion clock; the
        gate opens only once ``still_hold_s`` has elapsed with no such change.
        Disabled (always open) when ``still_hold_s <= 0``. With no clock the gate
        stays open — the ownership gate and conditioning still apply.
        """
        if self._still_hold_s <= 0.0:
            return True
        prev = self._last_cmd
        self._last_cmd = commanded
        if now is None:
            return True
        if (
            prev is None
            or max(abs(commanded[0] - prev[0]), abs(commanded[1] - prev[1])) > self._still_eps
        ):
            self._last_motion_t = now
            return False
        if self._last_motion_t is None:
            self._last_motion_t = now
            return False
        return (now - self._last_motion_t) >= self._still_hold_s

    def _arm_warmup(self, now: float | None) -> None:
        """Mute event latching for ``warmup_s`` from *now* (no-op when disabled)."""
        if self._warmup_s > 0.0 and now is not None:
            self._warmup_until = now + self._warmup_s

    def _in_warmup(self, now: float | None) -> bool:
        """Whether the post-(re)baseline mute window is still open."""
        if self._warmup_until is None:
            return False
        if now is None:
            return True  # armed but clockless: stay muted rather than ghost-fire
        if now >= self._warmup_until:
            self._warmup_until = None
            return False
        return True

    # ------------------------------------------------------------------
    # Lag compensation (the d1 live fix — see the module docstring)
    # ------------------------------------------------------------------

    def _lag_filtered(
        self, commanded: tuple[float, float], now: float | None
    ) -> tuple[float, float]:
        """Low-pass *commanded* toward where the physical head plausibly is.

        First-order filter ``filtered += dt/(tau+dt) × (commanded − filtered)``,
        seeded at the first commanded sample (a static commanded pose is
        therefore passed through unchanged from the first tick). ``dt`` comes
        from consecutive ``ctx.now`` readings, clamped to ``[0, 0.2]`` s so a
        clock hiccup cannot snap the filter; a missing clock uses the nominal
        50 Hz period. ``lag_tau == 0`` short-circuits to raw passthrough.
        """
        if self._lag_tau <= 0.0:
            return commanded
        if now is not None and self._last_now is not None:
            dt = min(max(now - self._last_now, 0.0), 0.2)
        else:
            dt = _NOMINAL_DT
        if now is not None:
            self._last_now = now
        if self._filtered is None:
            self._filtered = commanded
            return commanded
        k = dt / (self._lag_tau + dt)
        f_pitch = self._filtered[0] + k * (commanded[0] - self._filtered[0])
        f_yaw = self._filtered[1] + k * (commanded[1] - self._filtered[1])
        self._filtered = (f_pitch, f_yaw)
        return self._filtered

    # ------------------------------------------------------------------
    # Provider seam
    # ------------------------------------------------------------------

    def peek(self) -> tuple[str, str] | None:
        """The current latch — a non-consuming PEEK, safe to call many times a tick.

        Directly usable as ``SenseProviders(pat_event=driver.peek)``. Returns the
        ``(touch_type, level)`` latched by the most recent :meth:`__call__`, or
        ``None``. Never raises.
        """
        return self._latch

    def as_provider(self) -> Callable[[], tuple[str, str] | None]:
        """The zero-arg ``pat_event`` provider callable (an alias for :meth:`peek`).

        Mirrors :meth:`reachy.behavior.pose_feed.LastPoseHolder.as_start_pose_provider`
        so composition reads
        ``SenseProviders(pat_event=driver.as_provider())`` symmetrically with the
        other seam adapters.
        """
        return self.peek

    # ------------------------------------------------------------------
    # Defensive readers of the (duck-typed) TickContext + reader
    # ------------------------------------------------------------------

    @staticmethod
    def _head_owner(ctx) -> str | None:  # type: ignore[no-untyped-def]
        """``ctx.ownership["head"]`` (owner id or ``None``), tolerating any shape."""
        ownership = getattr(ctx, "ownership", None)
        if not isinstance(ownership, dict):
            return None
        return ownership.get("head")

    @staticmethod
    def _commanded_pitch_yaw(ctx) -> tuple[float, float] | None:  # type: ignore[no-untyped-def]
        """Extract ``(pitch, yaw)`` degrees from ``ctx.pose["head"]``, or ``None``.

        Degrees, neutral-relative (see r1). Any missing field or unexpected shape
        degrades to ``None`` (skip the tick) rather than raising.
        """
        pose = getattr(ctx, "pose", None)
        if not isinstance(pose, dict):
            return None
        head = pose.get("head")
        if not isinstance(head, dict):
            return None
        try:
            return (float(head.get("pitch", 0.0)), float(head.get("yaw", 0.0)))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _now(ctx) -> float | None:  # type: ignore[no-untyped-def]
        """The engine's injected monotonic clock for this tick (``ctx.now``).

        ``None`` when unavailable, which forwards to :meth:`PatDetector.update`'s
        own ``time.monotonic()`` default — but in the wired engine ``ctx.now`` is
        always the deterministic injected clock.
        """
        now = getattr(ctx, "now", None)
        return now if isinstance(now, (int, float)) else None

    def _read_actual(self) -> tuple[float, float] | None:
        """Call the injected reader, degrading a raise/``None`` to no reading."""
        try:
            reading = self._reader()
        # A raising reader degrades, never propagates.
        except Exception:  # noqa: BLE001
            logger.debug("PatSenseDriver reader raised; treating as no reading", exc_info=True)
            return None
        if reading is None:
            return None
        try:
            pitch, yaw = reading
            return (float(pitch), float(yaw))
        except (TypeError, ValueError):
            return None
