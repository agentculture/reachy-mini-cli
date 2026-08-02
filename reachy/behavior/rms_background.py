"""Rolling background estimate for the mic — the denominator of a RELATIVE
admission (issue #102, task t36).

Why an absolute floor could never work
=====================================
The runtime's sound admission was an absolute number in two places: the shipped
``look-toward-sound`` rule (``rms >= 0.02``) and
:attr:`reachy.behavior.orient.OrientParams`'s own ``sound_present`` floor. Both
were calibrated against ONE measurement — a still robot in a quiet room, 1459
samples, max 0.00953 — and both held until the room changed. Hands-on
measurement on the deployed robot, 2026-07-21
(``docs/verification/2026-07-21-live-verification-night.md`` section 4):

.. code-block:: text

    condition                                   still-room rms         >= 0.02
    daytime baseline                            p50 0.004, max 0.0095    0 %
    night, no streaming                         p50 0.0207, p90 0.053   51.7 %
    night, 50 Hz target streaming (NORMAL)      p50 0.034,  p99 0.085   99.1 %
    post-motion settle, 0-4 s after stop        p50 0.07-0.13          100 %

The background drifts **~25x** across conditions the same robot lives in within
24 h. So ``0.02`` sits UNDER the night background — every empty-room admission
the operator saw was the background, honestly reported — while any value above
the night state deafens the daytime robot. **No absolute value is right in both
rooms**, and picking one is choosing which half of the day to be broken in.

The fix restores the shape the ``0.02`` was lifted FROM. It began life as
:attr:`reachy.motion.snap.SnapDetector.min_rms` — a floor *inside* a
``rms > ratio * rolling_avg`` test (cited from ``reachy_nova``) — and was
transplanted into the behavior runtime as a bare absolute threshold, leaving the
ratio behind. This module is the ratio's other half: a rolling estimate of what
the room currently sounds like, so "loud" can be a COMPARISON again rather than
a number. It is deliberately NOT coupled to ``SnapDetector`` itself: that
detector is edge-triggered over whole mic chunks, while the runtime needs a
per-tick sense field a one-predicate rule can key on.

Why the median, and not a mean
==============================
The post-motion settle row above is the argument: ``p50 0.07-0.13``, 100 % over
the floor, *never decaying* within the 4 s observed. A mean chases exactly that
kind of transient and would drag the estimate up for as long as the window
holds, then back down — the estimate would be a smoothed copy of the signal
instead of an estimate of its floor. A median is indifferent to a minority of
arbitrarily loud samples: an utterance, a clap or a settle tail can occupy
almost half the window without moving it at all.

The median is maintained INCREMENTALLY (:mod:`bisect` into a sorted list beside
the time-ordered deque), so the per-tick cost is one insort and one delete on a
~500-element list of floats rather than a sort per tick. Measured on this box
against a full 10 s window at 50 Hz: **1.06 us per** :meth:`RmsBackground.observe`,
0.005 % of the 20 ms tick budget. That mattered enough to measure — the loop
already runs at ~20.7 ms against that budget, so a new per-tick cost had to be
shown small, not assumed small.

The ratio, and why 5.0
======================
:data:`DEFAULT_RATIO` is 5.0 — :class:`reachy.motion.snap.SnapDetector`'s own
default ratio, i.e. the companion the ``0.02`` was separated from. It also lands
the arithmetic where the deployed robot already was: ``5 x 0.004`` (the measured
DAYTIME p50) ``= 0.020``, the retired absolute floor exactly. So this change
leaves the daytime robot's sensitivity untouched and repairs only the night
robot's, which is the smallest honest change that closes #102.

Stated plainly, the cost of a relative gate: in the night-streaming room the
admission point is ``5 x 0.034 = 0.17``, so a quiet voice across that room will
not admit. That is not a regression this change introduces — it is the physics
of a mic whose own noise floor moved 8x. A relative gate makes it visible
instead of pretending an absolute number still means something.

What the estimator must NOT learn
=================================
Two exclusions, both load-bearing:

* **The robot's own noise.** The ``moving`` latch
  (:class:`reachy.behavior.self_motion.SelfMotionDriver`, #95) already marks the
  windows where the engine is commanding motion. A self-noise-inflated
  background would mask real sound; and with the SHIPPED infinite moving floor
  (:data:`reachy.behavior.rms_sense.DEFAULT_MOVING_FLOOR`) those windows report
  ``0.0``, so learning from them would drag the estimate onto the silence guard
  and make the first real reading afterwards read as a hundredfold spike — a
  phantom fire manufactured by the fix for the previous phantom fire.
* **An admitted episode.** While a reading stands at or above
  :data:`DEFAULT_RATIO` the estimator FREEZES — no learning AND no eviction — so
  a person talking cannot train the robot deaf to themselves mid-sentence. The
  freeze holds through gaps for ``episode_hold_s`` and is bounded by
  ``episode_max_s``, which is one whole shipped orienting window (12 s, the
  ``look-toward-sound`` ``duration_s``) plus margin: a source that outlasts an
  entire reaction cycle is not an episode, it is the new room, and the estimator
  adopts it rather than freezing forever.

Frozen means frozen: eviction is suspended too, so an arbitrarily long
exclusion preserves the estimate exactly rather than draining the window into a
cold, ratio-less sense. When learning resumes, samples genuinely older than the
window are evicted normally — an estimate from ten minutes ago is not the
current room — which can leave the sense briefly cold. Cold is fail-closed: no
level means no ratio means no admission.

The silence guard
=================
:data:`DEFAULT_SILENCE_FLOOR` (``1e-3``) is the denominator's lower clamp, and
it exists only so digital silence cannot manufacture a ratio. It is chosen to be
**inert in every measured room**: the quietest condition ever measured on this
robot has p50 0.004, four times above it, so the operative denominator is always
the real estimate — this is not an absolute floor wearing a disguise. It is
still ~30x a 16-bit dither LSB (~3e-5), so a muted or digitally-silent stream
resolves to a ratio far below admission instead of dividing by nearly zero.

Pure standard library, no numpy, no transport, no ``reachy_mini`` — a
dependency-free leaf like the rest of :mod:`reachy.behavior`, and never raises
out of :meth:`RmsBackground.observe`.
"""

from __future__ import annotations

import bisect
import math
from collections import deque

from reachy import senselog

#: Rolling window over which the background is estimated, in seconds. Two orders
#: of magnitude below the drift being tracked (hours) and several times longer
#: than the transients that must not become the estimate (an utterance or a clap
#: is 0.1-3 s), so the median sees a floor with a minority of loud samples.
DEFAULT_WINDOW_S = 10.0

#: How many times the rolling background a reading must stand to count as live
#: sound. :class:`reachy.motion.snap.SnapDetector`'s own default ratio — the
#: companion the retired ``0.02`` absolute floor was separated from — and, at
#: the measured daytime background (p50 0.004), arithmetically the same
#: admission point that floor expressed (``5 x 0.004 = 0.02``).
DEFAULT_RATIO = 5.0

#: How many times the rolling background a reading must stand to count as LOUD
#: — the ratio that promotes the orienting ladder from its antenna lean to a
#: head/body turn without waiting for the sound to prove itself ongoing (see
#: :class:`reachy.behavior.orient.CorroboratedGate`). Three times
#: :data:`DEFAULT_RATIO`, i.e. an unmistakable step above "audible": the measured
#: still-room distributions top out at ~2.5x their OWN median, so nothing a room
#: does by itself reaches even the tier-1 ratio, let alone this. Stated here
#: rather than in :mod:`reachy.behavior.orient` so the two ratios — the same
#: family, both "times the rolling background" — sit together and cannot drift.
DEFAULT_LOUD_RATIO = 15.0

#: Lower clamp on the denominator. Inert in every measured room (the quietest
#: measured p50 is 0.004, 4x this) and ~30x a 16-bit dither LSB, so it can only
#: ever bite on a silent/muted stream. See "The silence guard" above.
DEFAULT_SILENCE_FLOOR = 1e-3

#: Retained samples required before the estimator reports a level at all. 25
#: samples is 0.5 s at the runtime's 50 Hz — long enough that one transient
#: cannot be the whole estimate, short enough that a fresh start (or a resume
#: after a long freeze) is deaf only briefly. Below it :attr:`RmsBackground.level`
#: is ``None`` and every ratio is ``None``: fail-closed, since with no background
#: there is nothing for a reading to be loud *against*.
DEFAULT_MIN_SAMPLES = 25

#: Continuous quiet (seconds) before an episode's freeze releases. Long enough
#: to ride the gaps inside one utterance, short enough that the estimator is
#: adapting again well within a single orienting window.
DEFAULT_EPISODE_HOLD_S = 2.0

#: Hard bound on how long a single episode may freeze the estimate. One whole
#: shipped orienting window (``look-toward-sound``'s ``duration_s = 12``) plus
#: margin, so a freeze always outlives ONE admitted reaction — and a source that
#: outlasts a whole reaction cycle (a vacuum, music, a fan spinning up) is
#: adopted as the new room rather than freezing the sense indefinitely.
DEFAULT_EPISODE_MAX_S = 14.0

#: Env overrides, read at COMPOSITION time (``reachy.cli._commands.behavior``
#: resolves them, mirroring the ``REACHY_SELF_MOVING_*`` / ``REACHY_PAT_*``
#: pattern) — this module never reads the environment, so tests stay
#: deterministic.
WINDOW_S_ENV = "REACHY_RMS_BACKGROUND_S"
SILENCE_FLOOR_ENV = "REACHY_RMS_SILENCE_FLOOR"

#: A background estimate is logged when it first warms and thereafter only when
#: it has moved by this factor in either direction since the last line. The
#: quantity being tracked drifts 25x over a day, so a per-transition trace at
#: this granularity is a handful of lines per session — enough for an operator
#: to answer "what does the robot think this room sounds like?" without burying
#: the journal.
_LOG_STEP = 2.0

_STAGE = "gate"
_SOURCE = "rms"
_EVENT = "background"


class RmsBackground:
    """Rolling median of the mic's own floor, and the ratio a reading stands at.

    One call per tick: :meth:`observe` takes this tick's (already moving-floor
    gated) loudness and the engine's clock, returns the reading's ratio over the
    background *as it stood when the reading arrived*, and folds the reading
    into the estimate unless it must not be learned. :attr:`level` is the
    non-consuming peek at the current estimate.

    Stateful in a time-ordered deque plus a sorted mirror of the same values, so
    both the eviction and the median are O(1) amortised per tick. Never raises:
    a hostile reading, a hostile or rewinding clock, and an empty window all
    resolve to ``None`` — the same "any failure means no reading" contract
    :class:`reachy.behavior.sense.DoaPoller` sets for every sense in this
    package.
    """

    def __init__(
        self,
        *,
        window_s: float = DEFAULT_WINDOW_S,
        silence_floor: float = DEFAULT_SILENCE_FLOOR,
        min_samples: int = DEFAULT_MIN_SAMPLES,
        episode_ratio: float = DEFAULT_RATIO,
        episode_hold_s: float = DEFAULT_EPISODE_HOLD_S,
        episode_max_s: float = DEFAULT_EPISODE_MAX_S,
    ) -> None:
        self._window_s = max(0.0, float(window_s))
        self._silence_floor = max(0.0, float(silence_floor))
        self._min_samples = max(1, int(min_samples))
        self._episode_ratio = float(episode_ratio)
        self._episode_hold_s = max(0.0, float(episode_hold_s))
        self._episode_max_s = max(0.0, float(episode_max_s))
        #: Retained samples in arrival order — the eviction queue.
        self._window: deque[tuple[float, float]] = deque()
        #: The SAME values kept sorted, so the median is an index lookup.
        self._sorted: list[float] = []
        self._last_t = 0.0
        self._hot_since: float | None = None
        self._hot_until = 0.0
        self._logged: float | None = None

    # ------------------------------------------------------------------
    # Peeks
    # ------------------------------------------------------------------

    @property
    def level(self) -> float | None:
        """The current background estimate, or ``None`` while the window is cold."""
        count = len(self._sorted)
        if count < self._min_samples:
            return None
        mid = count // 2
        if count % 2:
            return self._sorted[mid]
        return 0.5 * (self._sorted[mid - 1] + self._sorted[mid])

    @property
    def samples(self) -> int:
        """How many readings the window currently retains."""
        return len(self._window)

    @property
    def frozen(self) -> bool:
        """Whether an episode is currently suspending learning."""
        return self._is_frozen(self._last_t)

    # ------------------------------------------------------------------
    # The per-tick entry point
    # ------------------------------------------------------------------

    def observe(self, rms, now, *, excluded: bool = False) -> float | None:
        """Fold one tick's loudness in; return its ratio over the background.

        *rms* is this tick's reading (already through
        :mod:`reachy.behavior.rms_sense`'s moving-floor gate) or ``None`` for no
        reading; *now* is the engine's clock. *excluded* marks a sample the
        estimator must not learn from — wired to the same ``self_moving`` latch
        the moving floor consults.

        ``None`` in means ``None`` out and NOTHING changes: "no reading" is
        distinct from a measured quiet (``0.0``), and an intermittent mic must
        not be able to churn the estimate or an episode's hold. ``None`` out
        also covers a cold window: with no background there is nothing to be
        loud against, which is fail-closed by construction.
        """
        try:
            return self._observe(rms, now, excluded)
        except Exception:  # a sense tap must never crash the tick
            return None

    def _observe(self, rms, now, excluded: bool) -> float | None:
        moment = self._clock(now)
        value = self._reading(rms)
        if value is None:
            return None
        # Trim first, so the reading is judged against a window that ends
        # `now` — but only while learning, because a frozen estimate must be
        # preserved wholesale, not drained one tick at a time.
        if not (excluded or self._is_frozen(moment)):
            self._evict(moment)
        level = self.level
        ratio = None if level is None else value / max(level, self._silence_floor)
        self._arm_episode(ratio, moment)
        # Re-checked AFTER arming: a reading loud enough to BE an episode is
        # never itself learned, or the estimate would chase the very sound the
        # freeze exists to protect.
        if not (excluded or self._is_frozen(moment)):
            self._append(moment, value)
        return ratio

    def reset(self) -> None:
        """Forget everything — a fresh room (used by callers that re-seat the mic)."""
        self._window.clear()
        self._sorted.clear()
        self._hot_since = None
        self._hot_until = 0.0
        self._logged = None

    # ------------------------------------------------------------------
    # Window maintenance
    # ------------------------------------------------------------------

    def _append(self, now: float, value: float) -> None:
        self._window.append((now, value))
        bisect.insort(self._sorted, value)
        self._observe_level()

    def _evict(self, now: float) -> None:
        horizon = now - self._window_s
        while self._window and self._window[0][0] < horizon:
            _, value = self._window.popleft()
            index = bisect.bisect_left(self._sorted, value)
            if index < len(self._sorted) and self._sorted[index] == value:
                del self._sorted[index]

    def _observe_level(self) -> None:
        """Log the estimate on the first warm and on every ``_LOG_STEP`` move."""
        level = self.level
        if level is None:
            return
        previous = self._logged
        if previous is not None:
            span = max(level, self._silence_floor) / max(previous, self._silence_floor)
            if 1.0 / _LOG_STEP < span < _LOG_STEP:
                return
        self._logged = level
        senselog.stage(
            _STAGE,
            _SOURCE,
            _EVENT,
            f"level={level:.5f} samples={len(self._window)} "
            f"admits_at={level * self._episode_ratio:.5f}",
        )

    # ------------------------------------------------------------------
    # Episode tracking — the freeze
    # ------------------------------------------------------------------

    def _arm_episode(self, ratio: float | None, now: float) -> None:
        if ratio is None or ratio < self._episode_ratio:
            return
        if self._hot_since is None or now >= self._hot_until:
            self._hot_since = now  # a fresh episode, not a continuation
        self._hot_until = now + self._episode_hold_s

    def _is_frozen(self, now: float) -> bool:
        if self._hot_since is None:
            return False
        if now >= self._hot_until:
            self._hot_since = None  # the episode ended: quiet outlasted the hold
            return False
        # Past the bound this stopped being an episode and became the room.
        return (now - self._hot_since) < self._episode_max_s

    # ------------------------------------------------------------------
    # Defensive readers
    # ------------------------------------------------------------------

    def _clock(self, now) -> float:
        """A monotonic, finite clock — a hostile or rewinding one simply holds."""
        try:
            moment = float(now)
        except (TypeError, ValueError):
            return self._last_t
        if not math.isfinite(moment):
            return self._last_t
        self._last_t = max(self._last_t, moment)
        return self._last_t

    @staticmethod
    def _reading(rms) -> float | None:
        """A finite, non-negative loudness, or ``None`` for "no reading"."""
        if rms is None or isinstance(rms, bool):
            return None
        try:
            value = float(rms)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(value) or value < 0.0:
            return None
        return value


__all__ = [
    "DEFAULT_EPISODE_HOLD_S",
    "DEFAULT_EPISODE_MAX_S",
    "DEFAULT_LOUD_RATIO",
    "DEFAULT_MIN_SAMPLES",
    "DEFAULT_RATIO",
    "DEFAULT_SILENCE_FLOOR",
    "DEFAULT_WINDOW_S",
    "RmsBackground",
    "SILENCE_FLOOR_ENV",
    "WINDOW_S_ENV",
]
