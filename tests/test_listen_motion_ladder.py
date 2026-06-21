"""Tests for the 3-tier ``listen`` motion ladder (noise / speech / engaged).

These pin the graduated, perception-tiered response added on top of the existing
two-tier ``ListenProducer`` (Tier-1 antenna lean / Tier-2 head→body turn). Under
the words-only ``--transcribe`` configuration (``turn_enabled=False``) the head no
longer swings toward every sound; instead the response is graduated by *what was
perceived*:

* **noise** — ambient sound only: Tier-1 antenna lean toward the DoA, no head/body
  turn (today's transcribe behaviour, pinned here as a regression guard).
* **speech** — detected speech: a LARGER orienting move toward the speaker (a
  bounded, head-only nudge), smaller than the full escalate turn, never a body
  rotation.
* **engaged** — the engagement gate decided the utterance is addressed to the
  robot: a deliberate head/body turn toward the utterance DoA (the full Tier-2
  escalate path), with a duration clamp that can never feed the SDK ``goto``
  planner a value that trips ``ValueError("time value is out of range [0,1]")``.

The ``engaged`` / ``speech`` perception level is signalled into ``update(...)``
either as a keyword argument or via the :meth:`ListenProducer.set_engaged` latch
(consumed on the next tick) — the seam task t7 drives from the engagement gate.
"""

from __future__ import annotations

import math

from reachy.behavior.sense import Sense, doa_angle_to_yaw
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.motion.queue import ANTENNA_KEY, LOOK_KEY

# A DoA angle clearly off-axis on the LEFT (0 rad = left, pi/2 = front, pi = right).
# With the default gain=0.6 this maps to a raw desired yaw of +54°, beyond the
# default head_only_band (30°) — i.e. an angle large enough to escalate to the body.
_LEFT = 0.0
# A DoA angle off-axis but inside head_only_band: ~pi/2 - 0.6 rad ≈ front-left.
_NEAR_LEFT = math.pi / 2.0 - math.radians(40.0) / 0.6  # raw desired ≈ +40°


def _transcribe_params(**overrides) -> ListenParams:
    """ListenParams in the words-only ``--transcribe`` configuration.

    ``turn_enabled=False`` + ``idle_energy=0`` so we observe the reactive tier
    without the always-alive idle layer competing for the return value.
    """
    base = dict(turn_enabled=False, idle_energy=0.0, deadband=0.0, hold=0.0)
    base.update(overrides)
    return ListenParams(**base)


def _sense(angle: float | None, *, speech: bool = False) -> Sense:
    return Sense(doa_angle=angle, speech_detected=speech)


# ---------------------------------------------------------------------------
# Criterion 3 — NO engaged signal under transcribe → antenna-only (no barge-in)
# ---------------------------------------------------------------------------


def test_noise_under_transcribe_is_antenna_only() -> None:
    """Ambient noise (no speech, no engaged) → Tier-1 antenna lean only.

    Reproduces today's transcribe behaviour: ``turn_enabled=False`` and no
    engaged/speech signal must never produce a head/body turn on ambient sound.
    """
    p = _transcribe_params()
    prod = ListenProducer(p)
    action = prod.update(0.1, _sense(_LEFT), sound_present=True)
    assert action is not None
    # Tier-1 antenna lean: no head, no body, antenna pair set, coalesces as antenna.
    assert action.head is None, f"ambient noise must not turn the head; got {action}"
    assert action.body_yaw is None, f"ambient noise must not rotate the body; got {action}"
    assert action.antennas is not None
    assert action.coalesce_key == ANTENNA_KEY
    # The committed head heading must remain untouched (no turn committed).
    assert prod.committed == 0.0


def test_speech_without_engaged_under_transcribe_is_still_not_a_full_turn() -> None:
    """Speech but not engaged → a LARGER orienting move, not the full escalate turn.

    The escalate path rotates the body (body_yaw set, |body| up to body_yaw_max).
    The speech tier must stay head-only (no body rotation) and bounded.
    """
    p = _transcribe_params()
    prod = ListenProducer(p)
    action = prod.update(0.1, _sense(_LEFT, speech=True), sound_present=True)
    assert action is not None
    # A larger orienting move: the head IS driven (unlike pure noise) ...
    assert action.head is not None, f"detected speech should orient the head; got {action}"
    # ... but it must NOT rotate the body (that is the engaged escalate tier only).
    assert action.body_yaw is None, f"speech tier must not rotate the body; got {action}"
    # And the head yaw is bounded within max_yaw and points toward the source (+).
    head_yaw = action.head["yaw"]
    assert 0.0 < head_yaw <= p.max_yaw + 1e-9
    assert action.coalesce_key == LOOK_KEY


def test_speech_orienting_move_is_larger_than_noise_lean() -> None:
    """The speech tier's orienting move is materially larger than the noise lean.

    The noise lean only moves antennas (head stays put); the speech tier turns the
    head toward the source. "Larger" = the head actually moves toward the DoA.
    """
    p = _transcribe_params()
    noise_prod = ListenProducer(p)
    speech_prod = ListenProducer(p)
    noise = noise_prod.update(0.1, _sense(_LEFT), sound_present=True)
    speech = speech_prod.update(0.1, _sense(_LEFT, speech=True), sound_present=True)
    # Noise: head untouched. Speech: head committed toward the source.
    assert noise is not None and noise.head is None
    assert speech is not None and speech.head is not None
    assert speech.head["yaw"] > 0.0


# ---------------------------------------------------------------------------
# Criterion 1 — engaged → deliberate head/body turn toward the utterance DoA
# ---------------------------------------------------------------------------


def test_engaged_drives_deliberate_head_body_turn() -> None:
    """An engaged signal drives the full deliberate turn toward the DoA.

    For a far off-axis DoA (raw desired beyond head_only_band) the engaged turn
    escalates to the body — body_yaw is driven toward the source.
    """
    p = _transcribe_params()
    prod = ListenProducer(p)
    action = prod.update(0.1, _sense(_LEFT, speech=True), sound_present=True, engaged=True)
    assert action is not None
    # Engaged + far off-axis → escalate to body: both head and body driven, toward +.
    assert action.head is not None
    assert action.body_yaw is not None, f"engaged far-off-axis must rotate the body; got {action}"
    assert action.body_yaw > 0.0, "left-side source → +body_yaw (left)"
    assert action.coalesce_key == LOOK_KEY


def test_engaged_near_axis_is_head_only_turn() -> None:
    """Engaged but inside head_only_band → a head-only deliberate turn (no body)."""
    p = _transcribe_params()
    prod = ListenProducer(p)
    # raw desired ≈ +40° is beyond head_only_band(30) by default — shrink the band so
    # this angle is head-only, exercising the non-escalate engaged branch.
    p.head_only_band = 60.0
    action = prod.update(0.1, _sense(_NEAR_LEFT, speech=True), sound_present=True, engaged=True)
    assert action is not None
    assert action.head is not None
    assert action.body_yaw is None, f"near-axis engaged turn must be head-only; got {action}"
    assert action.head["yaw"] > 0.0


def test_set_engaged_latch_consumed_on_next_tick() -> None:
    """``set_engaged()`` latches an engaged signal consumed on the next ``update``.

    This is the ergonomic seam t7 uses from the gate: latch once, the next tick
    performs the deliberate turn, and a subsequent tick (no re-latch) does not.
    """
    p = _transcribe_params()
    prod = ListenProducer(p)
    prod.set_engaged()
    a1 = prod.update(0.1, _sense(_LEFT, speech=True), sound_present=True)
    assert a1 is not None and a1.body_yaw is not None, "latched engaged → deliberate turn"
    # Latch consumed: the next tick (no re-latch, no kwarg) is not an engaged turn.
    # Reset committed/body + clear hold so the only difference is the missing latch.
    prod.committed = 0.0
    prod.body = 0.0
    prod._hold_until = 0.0
    a2 = prod.update(0.5, _sense(_LEFT, speech=True), sound_present=True)
    assert a2 is not None
    assert a2.body_yaw is None, f"latch must be one-shot; second tick not engaged: {a2}"


def test_engaged_latch_survives_none_angle_tick() -> None:
    """A ``doa_angle is None`` tick must NOT consume the engaged latch (Qodo #3 regression).

    ``set_engaged()`` arms a one-shot deliberate turn. If the latch were cleared on a
    tick where the DoA is unavailable — silence right after the addressed utterance, or a
    degraded/exception DoA read that surfaces as ``doa_angle=None`` — the engaged turn
    would be silently lost. The latch must stay armed until a tick carries a usable angle,
    then fire exactly once.
    """
    p = _transcribe_params()
    prod = ListenProducer(p)
    prod.set_engaged()
    # Tick 1: no DoA available (silence / degraded read). The latch must survive and no
    # engaged body turn may fire (there is nothing to turn toward).
    a1 = prod.update(0.1, _sense(None), sound_present=False)
    assert prod._engaged_latch is True, "latch must survive a None-angle tick"
    assert a1 is None or a1.body_yaw is None, "no engaged body turn without a usable DoA"
    # Tick 2: a real DoA arrives → the deliberate engaged turn fires, latch consumed once.
    prod._hold_until = 0.0
    a2 = prod.update(0.5, _sense(_LEFT, speech=True), sound_present=True)
    assert a2 is not None and a2.body_yaw is not None, "engaged turn fires once an angle arrives"
    assert prod._engaged_latch is False, "latch consumed exactly when the turn fires"


# ---------------------------------------------------------------------------
# Criterion 2 — duration clamp keeps t/duration in [0, 1] at the LARGEST angle
# ---------------------------------------------------------------------------


def test_engaged_largest_angle_duration_is_sane_positive() -> None:
    """The LARGEST escalate angle yields a sane, well-floored positive duration.

    The SDK's ``time_trajectory(t/duration)`` raises when ``t/duration > 1`` if a
    turn's duration came out too small relative to elapsed wall-clock. The engaged
    turn must therefore floor its duration to a real positive value (well above any
    floor, never zero/negative/NaN) at the most extreme angle.
    """
    # Pick params that would, without a floor, drive the duration toward zero:
    # a tiny body_delta path AND extreme angle. We also crank body_speed huge so
    # body_delta/body_speed → ~0, proving the floor (not the divide) sets duration.
    p = _transcribe_params(body_speed=1e9, min_dur=1.5, max_dur=4.0)
    prod = ListenProducer(p)
    # The largest possible DoA→yaw magnitude across the whole acoustic span:
    # angle in [0, pi] → raw desired in [+gain*90, -gain*90]; the extreme is angle=0.
    action = prod.update(0.0, _sense(_LEFT, speech=True), sound_present=True, engaged=True)
    assert action is not None
    dur = action.duration
    # Sane positive, finite, not NaN, and floored to the deliberate minimum.
    assert isinstance(dur, float)
    assert math.isfinite(dur), f"duration must be finite; got {dur!r}"
    assert dur > 0.0, f"duration must be strictly positive; got {dur!r}"
    assert dur >= p.min_dur - 1e-9, f"duration must be floored to min_dur; got {dur!r}"
    assert dur <= p.max_dur + 1e-9, f"duration must be capped to max_dur; got {dur!r}"
    # The crash condition: for ANY elapsed t within the move, t/duration <= 1.
    # With a sane floor, even the whole move's wall-clock stays in range.
    assert dur >= 1.0, "duration floor must keep t/duration in [0,1] for a real move"


def test_engaged_zero_body_speed_does_not_divide_by_zero() -> None:
    """A degenerate ``body_speed=0`` must not produce a 0/NaN/inf duration."""
    p = _transcribe_params(body_speed=0.0)
    prod = ListenProducer(p)
    action = prod.update(0.0, _sense(_LEFT, speech=True), sound_present=True, engaged=True)
    assert action is not None
    dur = action.duration
    assert math.isfinite(dur) and dur > 0.0, f"degenerate body_speed → bad duration {dur!r}"
    assert dur >= p.min_dur - 1e-9


def test_engaged_turn_points_toward_doa() -> None:
    """The engaged turn faces the utterance DoA (sign matches doa_angle_to_yaw)."""
    p = _transcribe_params()
    # A right-side source (angle near pi) → negative yaw.
    right_prod = ListenProducer(p)
    raw = doa_angle_to_yaw(math.pi, p.gain)  # ≈ -54°
    assert raw < 0.0  # sanity on the fixture
    action = right_prod.update(0.0, _sense(math.pi, speech=True), sound_present=True, engaged=True)
    assert action is not None
    # Right-side source → body and/or head go negative (toward the right).
    target = action.body_yaw if action.body_yaw is not None else action.head["yaw"]
    assert target < 0.0, f"right-side engaged turn must face right (-); got {target}"


# ---------------------------------------------------------------------------
# Backward-compat — turn_enabled=True (non-transcribe) is unchanged by tiers
# ---------------------------------------------------------------------------


def test_turn_enabled_true_unchanged_without_engaged() -> None:
    """With turn_enabled=True (normal listen), behaviour is unchanged by the ladder.

    A speech trigger far off-axis still escalates to the body exactly as before —
    the new tiers are gated on the transcribe configuration / engaged seam and do
    not alter the legacy turn path.
    """
    p = ListenParams(turn_enabled=True, idle_energy=0.0, deadband=0.0, hold=0.0)
    prod = ListenProducer(p)
    action = prod.update(0.1, _sense(_LEFT, speech=True), sound_present=True)
    assert action is not None
    # Legacy Tier-2 escalate: body driven toward the source.
    assert action.body_yaw is not None and action.body_yaw > 0.0
    assert action.coalesce_key == LOOK_KEY
