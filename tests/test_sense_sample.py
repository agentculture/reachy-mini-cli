"""Tests for the shared per-tick :class:`SenseSample` value type."""

import numpy as np

from reachy.motion.sense_sample import SenseSample


def test_defaults_are_quiet_and_empty():
    s = SenseSample()
    assert s.rms == 0.0
    assert s.doa is None
    assert s.speech is False
    assert s.ts == 0.0


def test_fields_round_trip():
    s = SenseSample(rms=0.42, doa=30.0, speech=True, ts=12.5)
    assert (s.rms, s.doa, s.speech, s.ts) == (0.42, 30.0, True, 12.5)


def test_is_frozen():
    s = SenseSample()
    try:
        s.rms = 1.0  # type: ignore[misc]
    except Exception:  # FrozenInstanceError is a dataclasses exception
        return
    raise AssertionError("SenseSample should be immutable (frozen)")


def test_default_audio_is_none():
    # The raw-audio field is opt-in; default construction stays audio-free so
    # every existing SenseSample(...) call is unchanged.
    assert SenseSample().audio is None


def test_audio_round_trips():
    chunk = np.array([0.1, 0.2, 0.3], dtype=np.float32)
    s = SenseSample(audio=chunk)
    assert s.audio is chunk
    assert np.array_equal(s.audio, chunk)
