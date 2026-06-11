"""Tests for reachy.sleep.stimulus — qualifying-stimulation classifier.

The classifier decides whether a sensor sample should reset the sleep idle timer.
Tests cover all four positive stimulus kinds and the self-mute exclusion window.
"""

from __future__ import annotations

import math

from reachy.behavior.sense import Sense
from reachy.sleep.stimulus import is_stimulus

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_NO_MUTE: float = 0.0  # mute_until in the past → window not active
_NOW: float = 100.0  # arbitrary "current" monotonic time


def _sense(*, doa_angle: float | None = None, speech_detected: bool = False) -> Sense:
    return Sense(doa_angle=doa_angle, speech_detected=speech_detected)


# ---------------------------------------------------------------------------
# Positive stimulus tests (each event type should return True)
# ---------------------------------------------------------------------------


class TestPositiveStimuli:
    """Each qualifying event type independently triggers a True return."""

    def test_doa_shift_is_stimulus(self) -> None:
        """A DoA angle shift (new sound direction) qualifies as a stimulus."""
        sense = _sense(doa_angle=math.pi / 4)
        assert is_stimulus(
            sense, doa_shift=True, snap=False, pat=False, now=_NOW, mute_until=_NO_MUTE
        )

    def test_speech_detected_is_stimulus(self) -> None:
        """speech_detected=True on the Sense qualifies as a stimulus."""
        sense = _sense(doa_angle=math.pi / 2, speech_detected=True)
        assert is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=_NO_MUTE
        )

    def test_snap_is_stimulus(self) -> None:
        """A SnapDetector-detected loud transient qualifies as a stimulus."""
        sense = _sense(doa_angle=None)
        assert is_stimulus(
            sense, doa_shift=False, snap=True, pat=False, now=_NOW, mute_until=_NO_MUTE
        )

    def test_pat_is_stimulus(self) -> None:
        """A pat deviation event qualifies as a stimulus."""
        sense = _sense()
        assert is_stimulus(
            sense, doa_shift=False, snap=False, pat=True, now=_NOW, mute_until=_NO_MUTE
        )

    def test_multiple_events_still_stimulus(self) -> None:
        """Multiple simultaneous qualifying events still return True."""
        sense = _sense(doa_angle=math.pi / 3, speech_detected=True)
        assert is_stimulus(
            sense, doa_shift=True, snap=True, pat=False, now=_NOW, mute_until=_NO_MUTE
        )


# ---------------------------------------------------------------------------
# Self-mute window exclusion tests
# ---------------------------------------------------------------------------


class TestSelfMuteExclusion:
    """Samples captured while now < mute_until must NOT qualify as stimulus."""

    def test_inside_mute_window_doa_shift_suppressed(self) -> None:
        """DoA shift inside self-mute window → NOT a stimulus."""
        mute_until = _NOW + 2.5  # mute expires 2.5 s from now
        sense = _sense(doa_angle=math.pi / 4)
        assert not is_stimulus(
            sense, doa_shift=True, snap=False, pat=False, now=_NOW, mute_until=mute_until
        )

    def test_inside_mute_window_speech_suppressed(self) -> None:
        """speech_detected inside self-mute window → NOT a stimulus."""
        mute_until = _NOW + 1.0
        sense = _sense(speech_detected=True)
        assert not is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=mute_until
        )

    def test_inside_mute_window_snap_suppressed(self) -> None:
        """Snap inside self-mute window → NOT a stimulus."""
        mute_until = _NOW + 0.5
        sense = _sense()
        assert not is_stimulus(
            sense, doa_shift=False, snap=True, pat=False, now=_NOW, mute_until=mute_until
        )

    def test_inside_mute_window_pat_suppressed(self) -> None:
        """Pat inside self-mute window → NOT a stimulus.

        The robot cannot keep itself awake through physical resonance from its
        own speaker either — pat is suppressed the same as acoustic cues.
        """
        mute_until = _NOW + 1.0
        sense = _sense()
        assert not is_stimulus(
            sense, doa_shift=False, snap=False, pat=True, now=_NOW, mute_until=mute_until
        )

    def test_exactly_at_mute_boundary_is_not_muted(self) -> None:
        """now == mute_until: window has expired, sample qualifies."""
        mute_until = _NOW  # boundary: not strictly inside the window
        sense = _sense(speech_detected=True)
        assert is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=mute_until
        )

    def test_after_mute_window_qualifies(self) -> None:
        """now > mute_until: window is over, qualifying event resumes."""
        mute_until = _NOW - 1.0  # expired 1 s ago
        sense = _sense(speech_detected=True)
        assert is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=mute_until
        )

    def test_mute_until_zero_qualifies(self) -> None:
        """mute_until=0.0 (default sentinel) never suppresses at realistic now."""
        sense = _sense(speech_detected=True)
        assert is_stimulus(sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=0.0)


# ---------------------------------------------------------------------------
# No-stimulus (all-false) baseline
# ---------------------------------------------------------------------------


class TestNoStimulus:
    """When no qualifying event is present the classifier returns False."""

    def test_empty_sense_no_events_is_not_stimulus(self) -> None:
        """An empty sample with no events is not a stimulus."""
        sense = _sense()
        assert not is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=_NO_MUTE
        )

    def test_doa_present_but_no_shift_no_speech_is_not_stimulus(self) -> None:
        """A stable (non-shifting) DoA with no speech is not a stimulus."""
        sense = _sense(doa_angle=math.pi / 2)  # angle present but no shift flagged
        assert not is_stimulus(
            sense, doa_shift=False, snap=False, pat=False, now=_NOW, mute_until=_NO_MUTE
        )
