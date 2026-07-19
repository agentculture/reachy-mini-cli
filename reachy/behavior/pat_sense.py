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
Complete-command gate — the #66 phantom-pat fix (a hard requirement)
--------------------------------------------------------------------------
Detection is safe while the robot holds a complete commanded pose, not while a
specially named behavior happens to own the head. The stillness vector includes
all six head axes, body yaw, and both antenna positions. A change in any value
blocks before the actual-pose reader, and sensing resumes only after the whole
vector has remained constant for ``still_hold_s``. A reaction can therefore
keep sensing while it holds a receptive pose.

Owner ids are never allowlisted. An ownership edge still invalidates temporal
pairing even if the numeric pose is unchanged: it ends the ordinary interaction
immediately, publishes blocked idle state, and re-arms the quiet hold. Filter
conditioning is reseeded when the complete-command gate next opens.

Opening a gap calls :meth:`PatDetector.clear_interaction`: press pairing,
evidence, and the interaction FSM/level clock are dropped immediately while
learned EMA baselines and the event cooldown are kept. A full reset would make the
calibration offset read as fresh presses until the slow EMA relearns. Initial
convergence at boot is instead covered by the one-time warmup mute.

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
The first live deployment showed that the base layer's own motion is not too
small or slow to matter:
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
tick one) and re-seeds when every detection gap recovers — ownership and
complete-command gaps alike.
``lag_tau=0`` disables the filter (raw passthrough).

--------------------------------------------------------------------------
Gap-edge re-baselining
--------------------------------------------------------------------------
Command motion, ownership changes, malformed pose data, and missing actual-pose
readings are detection gaps. A logical observation interval over 0.2 seconds is
also a gap; true wall-clock tick overruns live outside this driver and remain an
integration responsibility. The first blocked/unavailable edge ends the
ordinary interaction immediately, preserving detector calibration and event
cooldown; repeated unsafe ticks do not churn that reset. Recovery only reseeds
the lag/high-pass state. ``PatState`` keeps ``blocked`` distinct from
``unavailable`` and publishes idle/no-contact throughout either gap.

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
import math
import random
from dataclasses import replace
from typing import Callable

from reachy.behavior.sense import UNAVAILABLE_PAT_STATE, PatState
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

#: Longest interval between logical observation ticks that can preserve an
#: interaction. The 50 Hz engine normally supplies 0.02 s; ten missed ticks is
#: an input discontinuity, so the ordinary detector must re-earn stillness and
#: begin a fresh interaction. This is deliberately separate from wall-clock
#: ``TickMetrics`` overruns, which this driver cannot observe.
DEFAULT_MAX_OBSERVATION_GAP_S = 0.2

#: Nominal tick period (seconds) used for the filter step when ``ctx.now`` is
#: unavailable — the engine's 50 Hz default.
_NOMINAL_DT = 0.02

#: BOOT-ONLY warmup (seconds) during which classification is MUTED while the
#: detector's EMA baseline first converges. The EMA (``baseline_alpha`` 0.003
#: at 50 Hz) has a ~6.7 s time constant; learning the several-degree
#: commanded-vs-actual frame offset down past the release threshold takes ~2x
#: that, and until then offset + wander edges read as presses — the fire at
#: boot observed live (d1, issue #79). The detector keeps UPDATING through
#: warmup (that update IS the convergence), while both the event latch and
#: persistent PatState remain idle. Exit clears the learned interaction once;
#: later gap edges keep the baselines, so there is no post-gesture deadzone.
DEFAULT_WARMUP_S = 15.0

CONTENTMENT_AFTER_S = 4.0
WARNING_AFTER_S = 8.0
ENOUGH_MAX_S = 12.0
RELEASE_AFTER_S = 1.0
ENOUGH_COOLDOWN_S = 5.0
_HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")


class PatSenseDriver:
    """A ``TickBus`` driver that turns proprioceptive pats into a ``pat_event`` cue.

    Construct one with the actual-pose ``reader`` (a zero-arg ``read()``
    returning ``(pitch_deg, yaw_deg) | None``), then register :meth:`__call__`
    as a driver on the engine's ``tick_seam`` and wire :meth:`as_provider` (or
    :meth:`peek` directly) as ``SenseProviders(pat_event=...)``. Every tick the
    driver reads the actual pose, takes the commanded head pose from
    ``ctx.pose``, advances the detector, and latches any event for the NEXT
    tick's single sense read (see the module docstring for the full cadence,
    complete-command gate, frame mapping, and degradation contract).

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
        max_observation_gap_s: float = DEFAULT_MAX_OBSERVATION_GAP_S,
        enough_after_fn: Callable[[], float] | None = None,
        release_after_s: float = RELEASE_AFTER_S,
        cooldown_s: float = ENOUGH_COOLDOWN_S,
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
        self._max_observation_gap_s = max(0.0, float(max_observation_gap_s))
        self._last_observation_at: float | None = None
        #: Last commanded (pitch, yaw) and the clock reading when it last moved.
        self._last_cmd: tuple[float, ...] | None = None
        self._last_motion_t: float | None = None
        self._last_owners: tuple[object, object, object] | None = None
        #: Boot calibration-mute window (s); ``0`` disables (see d1 fix).
        self._warmup_s = max(0.0, float(warmup_s))
        #: Mute classification until this clock reading (armed once at boot);
        #: ``None`` = not armed / warmup disabled.
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
        #: True while the commanded pose is not yet still long enough to sense
        #: (the complete-command stillness gate, #80). The blocked -> unblocked
        #: edge triggers one conditioning re-seed via :meth:`_reseed_after_gap`.
        self._stillness_blocked = False
        self._input_gap = False
        #: One contiguous unsafe observation interval. Interaction state is
        #: cleared exactly once when this opens; recovery only reseeds filters.
        self._gap_active = False
        self._state = UNAVAILABLE_PAT_STATE
        self._seen_press_at: float | None = None
        self._active_contact_s = 0.0
        self._last_available_at: float | None = None
        self._no_fresh_since: float | None = None
        self._enough_after_fn = (
            enough_after_fn
            if enough_after_fn is not None
            else lambda: random.uniform(WARNING_AFTER_S, ENOUGH_MAX_S)  # nosec B311
        )
        self._enough_after_s = ENOUGH_MAX_S
        self._release_after_s = max(0.0, float(release_after_s))
        self._cooldown_s = max(0.0, float(cooldown_s))
        self._cooldown_until: float | None = None
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
        now = self._now(ctx)
        reseeded = False
        # --- logical observation-clock edge ---------------------------
        # A long or backwards clock jump means samples are missing. It is the
        # only overrun-like edge visible inside this driver; real wall-clock
        # TickMetrics overruns are observed outside this seam.
        if self._observation_clock_gapped(now):
            self._begin_gap("blocked", now)
            self._rearm_stillness_hold()
            if self._still_hold_s <= 0.0:
                self._reseed_after_gap()
                reseeded = True

        # --- ownership edge --------------------------------------------
        if self._ownership_changed(ctx):
            self._begin_gap("blocked", now)
            self._rearm_stillness_hold()
            if self._still_hold_s <= 0.0:
                if not reseeded:
                    self._reseed_after_gap()
                reseeded = True

        # --- commanded pose (this tick's streamed head offset, r1) -----
        commanded_full = self._commanded_pose(ctx)
        if commanded_full is None:
            if not self._stillness_blocked:
                self._rearm_stillness_hold()
            self._stillness_blocked = True
            self._begin_gap("blocked", now)
            return
        commanded = (commanded_full[4], commanded_full[5])

        # --- stillness gate (#80) --------------------------------------
        # The plant is only quiet while it is NOT tracking a moving target, so
        # sensing is confined to commanded-still windows. This makes the wander
        # ghost class structurally impossible rather than threshold-managed.
        #
        # `_stillness_blocked` owns one blocked -> unblocked edge, so ordinary
        # wander cannot rerun interaction clearing on every blocked tick.
        if not self._commanded_still(commanded_full, now):
            self._stillness_blocked = True
            self._begin_gap("blocked", now)
            return
        if self._stillness_blocked:
            # First tick back on a still commanded pose (a genuine
            # blocked -> unblocked edge). `detector.update()` was never called
            # for the whole blocked stretch (this method returned above, every
            # tick), so from the detector's point of view this is the same
            # situation as the ownership edge: reseed conditioning once before
            # accepting another physical sample.
            if not reseeded:
                self._reseed_after_gap()
                reseeded = True
            self._stillness_blocked = False

        # --- actual pose (proprioception) ------------------------------
        actual = self._read_actual()
        if actual is None:
            self._input_gap = True
            self._begin_gap("unavailable", now)
            return
        if self._input_gap:
            if not reseeded:
                self._reseed_after_gap()
                reseeded = True
            self._input_gap = False

        # The complete command and actual reading are both safe. The gap has
        # ended, but its interaction was already cleared at entry.
        recovered_gap = self._gap_active
        self._gap_active = False

        # Policy time only advances across successful observations. A blocked
        # command or unavailable reader must never masquerade as contact time,
        # release time, or cooldown time.
        self._advance_policy_clock(now)
        if self._state.phase == "cooldown":
            self._state = replace(self._state, availability="available")
            self._last_available_at = now
            return

        if self._first_update:
            # Boot: the deviation conditioning below needs ~3x its time constants
            # to converge before events mean anything — mute latching meanwhile.
            self._first_update = False
            self._arm_warmup(now)
        warmup_was_armed = self._warmup_until is not None
        warming_up = self._in_warmup(now)
        if warmup_was_armed and not warming_up and not recovered_gap:
            # Drop every edge and FSM transition learned while calibration was
            # converging before accepting the first post-warmup sample.
            self._end_interaction(now, availability="available")
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
        if warming_up:
            # Baselines continue learning, but muted boot evidence is not an
            # ordinary interaction and must never leak through PatState.
            self._publish_warmup_idle(now)
            return
        self._update_pat_state(now)
        if event is None:
            return
        # PatDetector yields ``(level, touch_type)``; Sense.pat_event is
        # ``(touch_type, level)`` (matching EventBuffer.feed_pat(kind, level)).
        level, touch_type = event
        self._latch = (touch_type, level)
        self.events += 1

    def _rearm_stillness_hold(self) -> None:
        """Force the stillness gate to re-earn its quiet window.

        See :meth:`_reseed_after_gap` for the matching conditioning reset.
        """
        self._last_cmd = None
        self._last_motion_t = None

    def _reseed_after_gap(self) -> None:
        """Reseed conditioning when a detection gap becomes safe again.

        Interaction state was already cleared when the gap opened. Recovery
        only resets lag/high-pass conditioning so a pose step across the gap
        cannot replay as a physical press. Learned detector EMA baselines and
        event cooldown remain untouched. Boot warmup is not re-armed.
        """
        self._filtered = None
        self._dev_lp = None
        self._last_now = None
        self._hp_last_now = None

    def _end_interaction(self, now: float | None, *, availability: str) -> None:
        """Clear detector and persistent policy state at one interaction edge."""
        self.detector.clear_interaction()
        self._seen_press_at = None
        self._active_contact_s = 0.0
        self._last_available_at = None
        self._no_fresh_since = None
        self._cooldown_until = None
        self._state = PatState(
            availability=availability,
            phase="idle",
            phase_started_at=now,
        )

    def _begin_gap(self, availability: str, now: float | None) -> None:
        """Open or update one unsafe interval without repeated clear churn."""
        if not self._gap_active:
            self._end_interaction(now, availability=availability)
            self._gap_active = True
            return
        self._state = PatState(
            availability=availability,
            phase="idle",
            phase_started_at=self._state.phase_started_at,
        )
        self._last_available_at = None
        self._no_fresh_since = None

    def _publish_warmup_idle(self, now: float | None) -> None:
        """Expose calibration as available idle, never as physical contact."""
        phase_started_at = (
            self._state.phase_started_at
            if self._state.availability == "available" and self._state.phase == "idle"
            else now
        )
        self._state = PatState(
            availability="available",
            phase="idle",
            phase_started_at=phase_started_at,
        )

    def _advance_policy_clock(self, now: float | None) -> None:
        """Advance enough into cooldown, and cooldown into a clean idle state."""
        if now is None:
            return
        if self._state.phase == "enough":
            self.detector.clear_presses()
            self._seen_press_at = None
            self._active_contact_s = 0.0
            self._last_available_at = None
            self._no_fresh_since = None
            self._cooldown_until = now + self._cooldown_s
            self._state = PatState(
                availability=self._state.availability,
                contact=False,
                phase="cooldown",
                phase_started_at=now,
            )
        if (
            self._state.phase == "cooldown"
            and self._cooldown_until is not None
            and now >= self._cooldown_until
        ):
            self._cooldown_until = None
            self.detector.clear_presses()
            self._seen_press_at = None
            self._state = PatState(
                availability=self._state.availability,
                phase="idle",
                phase_started_at=now,
            )

    def _update_pat_state(self, now: float | None) -> None:
        """Fold immutable detector evidence into the persistent interaction ladder."""
        if now is None:
            self._state = replace(self._state, availability="available")
            return
        evidence = self.detector.snapshot()
        fresh = evidence.last_press_at is not None and evidence.last_press_at != self._seen_press_at
        previous_available = self._last_available_at
        self._last_available_at = now
        dt = max(0.0, now - previous_available) if previous_available is not None else 0.0

        if fresh:
            self._seen_press_at = evidence.last_press_at
            # Release is budgeted from the persisted press anchor, not from
            # whichever later quiet sample happens to be observed first.
            self._no_fresh_since = evidence.last_press_at
            if self._state.phase in ("idle", "released"):
                self._active_contact_s = 0.0
                self._enough_after_s = self._draw_enough_after()
                phase = "receptive"
                phase_started_at = now
            else:
                phase = self._state.phase
                phase_started_at = self._state.phase_started_at
            self._state = PatState(
                availability="available",
                contact=True,
                touch_type=evidence.touch_type,
                level=evidence.level,
                yaw_deg=evidence.yaw_deg,
                phase=phase,
                phase_started_at=phase_started_at,
                last_press_at=evidence.last_press_at,
            )
        else:
            self._state = replace(self._state, availability="available")

        if not self._state.contact:
            return

        if not fresh and self._no_fresh_since is None:
            self._no_fresh_since = now
        if self._no_fresh_since is not None and now - self._no_fresh_since >= self._release_after_s:
            self._state = replace(
                self._state,
                contact=False,
                phase="released",
                phase_started_at=now,
            )
            self._last_available_at = now
            return

        self._active_contact_s += dt
        if self._active_contact_s >= self._enough_after_s:
            self._state = replace(self._state, phase="enough", phase_started_at=now)
        elif self._active_contact_s >= WARNING_AFTER_S and self._state.phase != "warning":
            self._state = replace(self._state, phase="warning", phase_started_at=now)
        elif self._active_contact_s >= CONTENTMENT_AFTER_S and self._state.phase == "receptive":
            self._state = replace(
                self._state,
                phase="contentment",
                phase_started_at=now,
            )

    def _draw_enough_after(self) -> float:
        try:
            value = float(self._enough_after_fn())
        except Exception:  # noqa: BLE001
            value = ENOUGH_MAX_S
        if not math.isfinite(value):
            value = ENOUGH_MAX_S
        return max(WARNING_AFTER_S, min(ENOUGH_MAX_S, value))

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

    def _commanded_still(self, commanded: tuple[float, ...], now: float | None) -> bool:
        """Whether the COMMANDED pose has been constant long enough to sense.

        Any per-axis change beyond ``still_eps`` restamps the motion clock; the
        gate opens only once ``still_hold_s`` has elapsed with no such change.
        Disabled (always open) when ``still_hold_s <= 0``. With no clock the gate
        stays open — conditioning and ownership-edge re-baselining still apply.
        """
        if self._still_hold_s <= 0.0:
            return True
        prev = self._last_cmd
        self._last_cmd = commanded
        if now is None:
            return True
        if (
            prev is None
            or max(abs(current - previous) for current, previous in zip(commanded, prev))
            > self._still_eps
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

    def _observation_clock_gapped(self, now: float | None) -> bool:
        """Whether logical samples are discontinuous enough to end interaction."""
        previous = self._last_observation_at
        self._last_observation_at = now
        if now is None or previous is None or self._max_observation_gap_s <= 0.0:
            return False
        delta = now - previous
        return delta < 0.0 or delta > self._max_observation_gap_s

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

    def peek_state(self) -> PatState:
        """The current persistent pat interaction snapshot, without consuming it."""
        return self._state

    def as_state_provider(self) -> Callable[[], PatState]:
        """The zero-arg ``pat_state`` provider callable."""
        return self.peek_state

    # ------------------------------------------------------------------
    # Defensive readers of the (duck-typed) TickContext + reader
    # ------------------------------------------------------------------

    def _ownership_changed(self, ctx) -> bool:  # type: ignore[no-untyped-def]
        ownership = getattr(ctx, "ownership", None)
        current: tuple[object, object, object]
        if isinstance(ownership, dict):
            current = (
                ownership.get("head"),
                ownership.get("antennas"),
                ownership.get("body_yaw"),
            )
        else:
            current = (None, None, None)
        previous = self._last_owners
        self._last_owners = current
        return previous is not None and current != previous

    @staticmethod
    def _commanded_pose(ctx) -> tuple[float, ...] | None:  # type: ignore[no-untyped-def]
        pose = getattr(ctx, "pose", None)
        if not isinstance(pose, dict):
            return None
        head = pose.get("head")
        antennas = pose.get("antennas")
        if not isinstance(head, dict) or not isinstance(antennas, (tuple, list)):
            return None
        if (
            len(antennas) != 2
            or "body_yaw" not in pose
            or any(axis not in head for axis in _HEAD_AXES)
        ):
            return None
        try:
            values = tuple(float(head[axis]) for axis in _HEAD_AXES) + (
                float(pose["body_yaw"]),
                float(antennas[0]),
                float(antennas[1]),
            )
        except (TypeError, ValueError):
            return None
        return values if all(math.isfinite(value) for value in values) else None

    @staticmethod
    def _now(ctx) -> float | None:  # type: ignore[no-untyped-def]
        """The engine's injected monotonic clock for this tick (``ctx.now``).

        ``None`` when unavailable, which forwards to :meth:`PatDetector.update`'s
        own ``time.monotonic()`` default — but in the wired engine ``ctx.now`` is
        always the deterministic injected clock.
        """
        now = getattr(ctx, "now", None)
        if not isinstance(now, (int, float)):
            return None
        value = float(now)
        return value if math.isfinite(value) else None

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
