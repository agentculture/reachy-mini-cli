"""Tests for reachy.speech.events — sense-event cue buffer.

Tests are written test-first: they define the acceptance criteria before the
implementation.  Run with:  uv run pytest tests/test_speech_events.py -q
"""

from __future__ import annotations

import math
import threading
import time
from typing import List

import pytest

from reachy.speech.events import EventBuffer, SenseCue

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_buffer(maxlen: int = 256, clock=None) -> EventBuffer:
    """Construct a buffer; inject a fixed clock if provided."""
    if clock is not None:
        return EventBuffer(maxlen=maxlen, clock=clock)
    return EventBuffer(maxlen=maxlen)


# ---------------------------------------------------------------------------
# AC1 — DoA/RMS samples → human-readable directional cue strings
# ---------------------------------------------------------------------------


class TestFeedDoa:
    """feed_doa(angle_rad, rms, is_speech) → correct directional cues."""

    # DoA convention from reachy/behavior/sense.py:
    #   angle 0       = left
    #   angle pi/2    = front / ahead
    #   angle pi      = right

    def test_speech_from_left(self):
        buf = _make_buffer()
        buf.feed_doa(angle_rad=0.0, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "speech" in cues[0].text
        assert "left" in cues[0].text

    def test_speech_from_right(self):
        buf = _make_buffer()
        buf.feed_doa(angle_rad=math.pi, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "speech" in cues[0].text
        assert "right" in cues[0].text

    def test_speech_from_ahead(self):
        buf = _make_buffer()
        buf.feed_doa(angle_rad=math.pi / 2, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "speech" in cues[0].text
        assert "ahead" in cues[0].text

    def test_loud_sound_from_left(self):
        """A non-speech loud RMS from the left → 'loud sound ... left'."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=0.3, rms=0.5, is_speech=False)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "loud" in cues[0].text
        assert "left" in cues[0].text

    def test_loud_sound_from_right(self):
        """A non-speech loud RMS from the right → 'loud sound ... right'."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=math.pi - 0.3, rms=0.5, is_speech=False)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "loud" in cues[0].text
        assert "right" in cues[0].text

    def test_loud_sound_ahead(self):
        """A loud RMS near-zero offset from front → 'loud sound ahead'."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=math.pi / 2 + 0.1, rms=0.5, is_speech=False)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "ahead" in cues[0].text

    def test_quiet_sound_generates_cue(self):
        """Any DoA with is_speech=True emits a cue regardless of RMS level."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=0.0, rms=0.001, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 1

    def test_quiet_non_speech_no_cue(self):
        """Quiet, non-speech sound below the loud threshold → no cue (ambient noise)."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=0.0, rms=0.001, is_speech=False)
        cues = buf.snapshot()
        assert len(cues) == 0

    def test_none_angle_no_cue(self):
        """No DoA reading (angle_rad=None) → no cue even if RMS is present."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=None, rms=0.5, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 0

    def test_direction_boundary_left(self):
        """Angle just below pi/2 minus threshold → 'left'."""
        buf = _make_buffer()
        # angle slightly less than pi/2 → left of centre
        buf.feed_doa(angle_rad=math.pi / 2 - 0.4, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert "left" in cues[0].text

    def test_direction_boundary_right(self):
        """Angle just above pi/2 plus threshold → 'right'."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=math.pi / 2 + 0.4, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert "right" in cues[0].text

    def test_cue_has_timestamp(self):
        """Each cue carries a monotonic timestamp."""
        tick = [0.0]

        def clock():
            tick[0] += 1.0
            return tick[0]

        buf = _make_buffer(clock=clock)
        buf.feed_doa(angle_rad=0.0, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert cues[0].timestamp == pytest.approx(1.0)


# ---------------------------------------------------------------------------
# AC1 — Vision samples → human-readable cue strings
# ---------------------------------------------------------------------------


class TestFeedVision:
    """feed_vision(motion_direction, brightness_delta) → correct cues."""

    # motion_direction: MotionResult.direction in [-1, 1]
    #   -1 = far left, +1 = far right, 0 = centre
    # brightness_delta: positive = brighter, negative = darker

    def test_motion_on_left(self):
        buf = _make_buffer()
        buf.feed_vision(motion_direction=-0.7, brightness_delta=0.0)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "motion" in cues[0].text
        assert "left" in cues[0].text

    def test_motion_on_right(self):
        buf = _make_buffer()
        buf.feed_vision(motion_direction=0.7, brightness_delta=0.0)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "motion" in cues[0].text
        assert "right" in cues[0].text

    def test_motion_ahead(self):
        """Direction near 0 → 'ahead'."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=0.1, brightness_delta=0.0)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "ahead" in cues[0].text

    def test_no_motion_no_cue(self):
        """None motion_direction → no motion cue (caller already filtered no-motion)."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=None, brightness_delta=0.0)
        cues = buf.snapshot()
        assert len(cues) == 0

    def test_light_brightened(self):
        """Positive brightness delta → 'brightened'."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=None, brightness_delta=15.0)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "bright" in cues[0].text

    def test_light_dimmed(self):
        """Negative brightness delta → 'dimmed'."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=None, brightness_delta=-15.0)
        cues = buf.snapshot()
        assert len(cues) == 1
        assert "dim" in cues[0].text

    def test_small_brightness_delta_no_cue(self):
        """Tiny brightness delta (noise) → no cue."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=None, brightness_delta=1.0)
        cues = buf.snapshot()
        assert len(cues) == 0

    def test_motion_and_brightness_both_emit(self):
        """Both motion and significant brightness change in one feed → two cues."""
        buf = _make_buffer()
        buf.feed_vision(motion_direction=-0.5, brightness_delta=20.0)
        cues = buf.snapshot()
        assert len(cues) == 2

    def test_vision_cue_has_timestamp(self):
        t = [10.0]

        def clock():
            t[0] += 0.5
            return t[0]

        buf = _make_buffer(clock=clock)
        buf.feed_vision(motion_direction=0.8, brightness_delta=0.0)
        cues = buf.snapshot()
        assert cues[0].timestamp == pytest.approx(10.5)


# ---------------------------------------------------------------------------
# AC2 — snapshot() atomically clears; no cues lost
# ---------------------------------------------------------------------------


class TestSnapshot:
    def test_snapshot_clears_buffer(self):
        """After snapshot(), the buffer is empty."""
        buf = _make_buffer()
        buf.feed_doa(angle_rad=0.0, rms=0.05, is_speech=True)
        buf.snapshot()  # clears
        assert buf.snapshot() == []

    def test_snapshot_returns_list(self):
        buf = _make_buffer()
        assert isinstance(buf.snapshot(), list)

    def test_snapshot_empty_returns_empty_list(self):
        buf = _make_buffer()
        assert buf.snapshot() == []

    def test_maxlen_rolling_window(self):
        """Oldest cues are evicted when the buffer is full (rolling window)."""
        buf = _make_buffer(maxlen=3)
        for _ in range(5):
            buf.feed_doa(angle_rad=0.0, rms=0.05, is_speech=True)
        cues = buf.snapshot()
        assert len(cues) == 3  # capped at maxlen


class TestSnapshotAtomic:
    """snapshot() clears atomically — concurrent producer/consumer loses no cues."""

    def test_no_cues_lost_under_concurrency(self):
        """Two producer threads feed cues; a consumer threads snapshots repeatedly.

        Invariant: total_produced == total_consumed (no cue double-counted or lost).
        Because maxlen caps the buffer, we must snapshot fast enough that the
        rolling window does not evict before we read.  We use a large maxlen and
        slow producers to stay comfortably below the cap.
        """
        PRODUCE_COUNT = 200  # cues each producer emits
        MAXLEN = PRODUCE_COUNT * 4  # well above what producers can outpace

        buf = _make_buffer(maxlen=MAXLEN)
        consumed: List[SenseCue] = []
        stop_event = threading.Event()

        def producer_doa():
            for _ in range(PRODUCE_COUNT):
                buf.feed_doa(angle_rad=0.0, rms=0.05, is_speech=True)

        def producer_vision():
            for _ in range(PRODUCE_COUNT):
                buf.feed_vision(motion_direction=0.8, brightness_delta=0.0)

        def consumer():
            while not stop_event.is_set():
                consumed.extend(buf.snapshot())
                time.sleep(0.0001)
            # Final drain after stop
            consumed.extend(buf.snapshot())

        t_prod1 = threading.Thread(target=producer_doa)
        t_prod2 = threading.Thread(target=producer_vision)
        t_cons = threading.Thread(target=consumer)

        t_cons.start()
        t_prod1.start()
        t_prod2.start()

        t_prod1.join()
        t_prod2.join()
        stop_event.set()
        t_cons.join()

        # Each producer emits exactly PRODUCE_COUNT cues (1 per feed call for doa
        # with is_speech=True; 1 per feed call for motion_direction non-None).
        assert len(consumed) == PRODUCE_COUNT * 2

    def test_snapshot_is_consistent_snapshot(self):
        """A snapshot taken during concurrent feeds contains only whole-cue entries
        (no partial / half-written cue object).  Verified by checking each cue
        has non-empty text and a numeric timestamp.
        """
        buf = _make_buffer(maxlen=1024)
        results: List[List[SenseCue]] = []

        def producer():
            for _ in range(100):
                buf.feed_doa(angle_rad=math.pi / 2, rms=0.05, is_speech=True)
                buf.feed_vision(motion_direction=-0.3, brightness_delta=0.0)

        t = threading.Thread(target=producer)
        t.start()
        for _ in range(50):
            snap = buf.snapshot()
            for cue in snap:
                assert isinstance(cue.text, str) and cue.text
                assert isinstance(cue.timestamp, float)
            results.append(snap)
        t.join()
        buf.snapshot()  # final drain (just ensure no exception)


# ---------------------------------------------------------------------------
# AC3 — Buffer only accepts already-read values; no hardware / I/O
# ---------------------------------------------------------------------------


class TestNoHardwareAccess:
    """The buffer must be constructable and usable with no external resources."""

    def test_instantiates_without_hardware(self):
        """EventBuffer can be constructed and used in a plain test environment."""
        buf = EventBuffer()
        buf.feed_doa(angle_rad=0.0, rms=0.1, is_speech=True)
        buf.feed_vision(motion_direction=0.5, brightness_delta=10.0)
        cues = buf.snapshot()
        assert len(cues) >= 1  # at least something was emitted

    def test_feed_methods_accept_scalar_values(self):
        """feed_doa and feed_vision take plain Python scalars, not sensor objects."""
        buf = EventBuffer()
        # These must NOT raise even without any robot / daemon connection
        buf.feed_doa(angle_rad=1.2, rms=0.03, is_speech=False)
        buf.feed_vision(motion_direction=-1.0, brightness_delta=-12.0)


# ---------------------------------------------------------------------------
# SenseCue dataclass
# ---------------------------------------------------------------------------


class TestSenseCue:
    def test_fields(self):
        """SenseCue has text and timestamp fields."""
        cue = SenseCue(text="speech from the left", timestamp=1.0)
        assert cue.text == "speech from the left"
        assert cue.timestamp == 1.0

    def test_repr_contains_text(self):
        cue = SenseCue(text="motion on the right", timestamp=2.5)
        assert "motion" in repr(cue)
