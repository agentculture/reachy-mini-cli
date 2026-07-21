"""Direction invariants regression suite (t5).

Pins the direction half of the ``--transcribe`` words-only cognition design
(``docs/plans/2026-07-17-event-based-senses-pipeline.md``, task t5):

**Direction rides transcripts.** With raw per-tick DoA cues off, a transcribed
utterance's DoA is still translated into a direction word and carried on the
transcript cue itself
(:meth:`~reachy.speech.events.EventBuffer.feed_transcript`) — so direction is
not lost, only decoupled from the noisy per-tick raw feed. This pins the exact
cue wording, which is what an LLM prompt is built from.

A second section documents (but does NOT implement in production) the rate-limit
contract a FUTURE standalone ``audio_direction`` sense event must honour, per
the same plan doc: "one direction event per 2s unless the bearing jumps 15
degrees", ported from ``reachy_nova``'s
``tracking.py::TrackingManager._maybe_fire_audio_direction``. No such event
exists in ``reachy/speech/events.py`` yet (direction rides transcripts only,
per the invariant above) — the reference implementation here is TEST-ONLY, so
the contract is proven by a fake-clock test ahead of any production code
landing.

Two sections retired with the folded ``listen --live`` composition root (t21):
the ``feed_doa_cues=False`` composition pins (there is no folded ThinkHook to
compose any more — the symbolic runtime's transcript sense is word-driven by
construction), and the end-to-end ``TranscribeHook`` -> ``EventBuffer`` cue
test. The surviving path's equivalent is
``tests/test_behavior_transcript_sense.py``'s
``test_the_direction_of_the_utterance_is_latched_alongside_the_words``.

No robot, no daemon, no network, no real LLM/STT/TTS, no real threads, no real
sleeps.
"""

from __future__ import annotations

import time

from reachy.speech.events import EventBuffer

# ---------------------------------------------------------------------------
# 1. Direction rides transcripts
# ---------------------------------------------------------------------------


def test_feed_transcript_direction_cue_wording_pinned() -> None:
    """Pin :meth:`EventBuffer.feed_transcript`'s exact direction-tagged wording.

    A regression here would silently change what cognition "reads" as the
    speaker's direction — this is the wording the LLM prompt is built from.
    """
    buf = EventBuffer()

    buf.feed_transcript("hello there", direction="left")

    cues = buf.snapshot()
    assert len(cues) == 1
    assert cues[0].text == 'heard someone say (from the left): "hello there"'


# ---------------------------------------------------------------------------
# 2. FUTURE direction-event rate-limit contract (test-local reference spec)
#
# No standalone "audio_direction" sense event exists in production yet —
# direction rides transcripts only (section 1 above). This class documents the
# CONTRACT any future such event must honour, ported from reachy_nova's
# tracking.py::TrackingManager._maybe_fire_audio_direction (the same proven
# algorithm this repo already cites for other rate-limited detectors). It is
# NOT wired to any production code path — do not import it outside this file.
# ---------------------------------------------------------------------------


class _DirectionEventRateLimiter:
    """TEST-ONLY reference spec for a FUTURE ``audio_direction`` sense event.

    Ported from ``reachy_nova``'s
    ``tracking.py::TrackingManager._maybe_fire_audio_direction`` (see
    ``docs/plans/2026-07-17-event-based-senses-pipeline.md`` t5's acceptance
    criteria: "a fake-clock test documents the nova rate-limit contract — one
    direction event per 2s unless the bearing jumps 15 degrees — for any future
    direction event"). This class exists ONLY so a fake-clock test can pin that
    contract ahead of any real ``audio_direction`` event landing in
    ``reachy/speech/events.py`` (tracked as t9-t11 in the same plan). It has no
    caller in production code.

    Contract
    --------
    :meth:`should_emit` returns ``True`` (and latches the new emit time +
    bearing) when EITHER:

    * at least ``rate_limit_s`` (default 2.0s) has elapsed since the last
      emitted event, OR
    * the bearing has moved at least ``bearing_jump_deg`` (default 15.0
      degrees) since the last emitted event's bearing —

    and latches immediately in either case, resetting the window. The very
    first call always emits (there is no "last" event yet). A tiny epsilon
    absorbs float round-trip noise so a jump of exactly the threshold reliably
    counts as "15+ degrees" rather than landing a hair under it (mirrors
    nova's own comment on this exact edge case).
    """

    def __init__(
        self,
        *,
        rate_limit_s: float = 2.0,
        bearing_jump_deg: float = 15.0,
        clock=None,
    ) -> None:
        self._rate_limit_s = rate_limit_s
        self._bearing_jump_deg = bearing_jump_deg
        self._clock = clock if clock is not None else time.monotonic
        self._last_emit_t: float | None = None
        self._last_bearing: float | None = None

    def should_emit(self, bearing_deg: float) -> bool:
        now = self._clock()
        first_emit = self._last_emit_t is None
        window_elapsed = first_emit or (now - self._last_emit_t) >= self._rate_limit_s
        bearing_jumped = (
            self._last_bearing is not None
            and abs(bearing_deg - self._last_bearing) >= self._bearing_jump_deg - 1e-9
        )
        if not (window_elapsed or bearing_jumped):
            return False
        self._last_emit_t = now
        self._last_bearing = bearing_deg
        return True


class _FakeClock:
    """A manually-advanced clock for deterministic rate-limit tests."""

    def __init__(self, t: float = 0.0) -> None:
        self._t = t

    def advance(self, dt: float) -> None:
        self._t += dt

    def __call__(self) -> float:
        return self._t


def test_direction_rate_limiter_first_call_always_emits() -> None:
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)

    assert limiter.should_emit(0.0) is True


def test_direction_rate_limiter_suppresses_within_window_same_bearing() -> None:
    """No further emission before 2.0s elapses, with the bearing unchanged."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.5)
    assert limiter.should_emit(0.0) is False

    clock.advance(1.4)  # total 1.9s since the last emit — still inside the window
    assert limiter.should_emit(0.0) is False


def test_direction_rate_limiter_emits_once_window_elapses() -> None:
    """At >= 2.0s elapsed the window re-opens even with an unchanged bearing."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(2.0)  # exactly the rate limit — the boundary counts as elapsed
    assert limiter.should_emit(0.0) is True


def test_direction_rate_limiter_bearing_jump_emits_immediately() -> None:
    """A bearing jump of exactly 15 degrees emits despite the window not elapsing."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)  # well inside the 2.0s window
    assert limiter.should_emit(15.0) is True, "a >=15 degree jump must emit immediately"


def test_direction_rate_limiter_bearing_jump_below_threshold_is_suppressed() -> None:
    """A jump just under 15 degrees does NOT bypass the rate limit."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)
    assert limiter.should_emit(14.9) is False


def test_direction_rate_limiter_jump_resets_the_window() -> None:
    """After a jump-triggered emit, the next call re-measures from THAT emit."""
    clock = _FakeClock(0.0)
    limiter = _DirectionEventRateLimiter(clock=clock)
    assert limiter.should_emit(0.0) is True

    clock.advance(0.1)
    assert limiter.should_emit(15.0) is True  # jump -> emits, resets window + bearing

    clock.advance(0.1)  # only 0.1s since the jump-emit, and no further jump
    assert limiter.should_emit(15.0) is False

    clock.advance(2.0)  # now >= 2.0s since the jump-emit -> window re-opens
    assert limiter.should_emit(15.0) is True
