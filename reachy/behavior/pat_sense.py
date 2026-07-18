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
edge — the driver calls :meth:`PatDetector.reset` before feeding any fresh
sample, re-baselining the detector so the just-settled pose seeds a clean
zero-deviation baseline instead of the gesture's stale press state. A steady
deviation that persists across the resume (e.g. the head still parked a couple
of degrees off from the ended gesture) cannot fire spuriously: a *sustained*
offset registers at most ONE press edge (the detector needs a release then a
re-press to count a second), so it never reaches ``min_presses`` — only a
genuine, oscillating pat re-accumulates enough edges to fire.

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
tick one) and re-seeds on every suspended→resumed edge alongside the detector
re-baseline. ``lag_tau=0`` disables the filter (raw passthrough).

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

#: Nominal tick period (seconds) used for the filter step when ``ctx.now`` is
#: unavailable — the engine's 50 Hz default.
_NOMINAL_DT = 0.02


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
    ) -> None:
        self._reader = reader
        self.detector = detector if detector is not None else PatDetector()
        #: Commanded-pose low-pass time constant (s); ``0`` = raw passthrough.
        self._lag_tau = max(0.0, float(lag_tau))
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
        except Exception:  # noqa: BLE001 — a sense tap must never crash the loop
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
            # First tick back on the base layer: re-baseline so the settled pose
            # seeds a clean zero-deviation baseline (never a stale phantom press),
            # and re-seed the lag filter (the gesture invalidated its state).
            self.detector.reset()
            self._filtered = None
            self._suspended = False

        # --- commanded pose (this tick's streamed head offset, r1) -----
        commanded = self._commanded_pitch_yaw(ctx)
        if commanded is None:
            return  # a missing/malformed ctx.pose -> skip this tick

        # --- actual pose (proprioception) ------------------------------
        actual = self._read_actual()
        if actual is None:
            return  # reader disconnected / absent / raised -> no reading

        now = self._now(ctx)
        commanded_pitch, commanded_yaw = self._lag_filtered(commanded, now)
        actual_pitch, actual_yaw = actual
        event = self.detector.update(
            commanded_pitch,
            actual_pitch,
            commanded_yaw,
            actual_yaw,
            now=now,
        )
        if event is None:
            return
        # PatDetector yields ``(level, touch_type)``; Sense.pat_event is
        # ``(touch_type, level)`` (matching EventBuffer.feed_pat(kind, level)).
        level, touch_type = event
        self._latch = (touch_type, level)
        self.events += 1

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
        except Exception:  # noqa: BLE001 — a raising reader degrades, never propagates
            logger.debug("PatSenseDriver reader raised; treating as no reading", exc_info=True)
            return None
        if reading is None:
            return None
        try:
            pitch, yaw = reading
            return (float(pitch), float(yaw))
        except (TypeError, ValueError):
            return None
