"""Explicit, shape-agnostic mic-channel selection (:mod:`reachy.robot.audio_shape`).

A latent portability hazard found alongside issue #108's root cause. The SDK
documents ``AudioBase.get_audio_sample()`` as returning shape ``(N, 2)``
(``reachy_mini`` 1.9 ``media/audio_base.py:117-131``, ``CHANNELS = 2``), and
every consumer in this repo used to flatten it with a bare ``.reshape(-1)`` —
which does not *pick* a channel, it **interleaves both into one stream**. On a
truly stereo source that halves the apparent pitch and hands STT twice as many
samples as the WAV header claims.

It is not currently firing. Measured read-only over 829 archived STT uploads
from the deployed box: within-pair vs across-pair ``|diff|`` ratio 0.984
(a duplicated-channel interleave scores 0.000), lag-1 autocorrelation +0.782
against lag-2 +0.641 (a two-different-channel interleave scores +0.00 / +0.90),
and every clip length a multiple of the 256-sample ALSA period rather than of
512. Since ``.reshape(-1)`` was the only transform applied, that rules out both
stereo shapes and says the array delivered on that box is 1-D (or ``(N, 1)``).
So this is a hazard, not the cause — but a future SDK or ALSA change must not be
able to silently re-introduce it.

:func:`reachy.robot.audio_shape.to_mono` is therefore explicit about the channel
it wants (``reachy_nova``'s ``audio_pipeline.py:21-34`` picks channel 0, the AEC
channel) and total over the shapes a mic can hand out. These tests pin both
halves: today's shape passes through untouched, and a stereo shape is SELECTED
from, never interleaved.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy.behavior.audio_pump import AudioPump
from reachy.behavior.transcript_sense import TranscriptSenseDriver
from reachy.robot.audio_shape import AEC_CHANNEL, to_mono


def _stereo(n: int = 64) -> np.ndarray:
    """A stereo chunk whose two channels are trivially distinguishable."""
    left = np.arange(n, dtype=np.float32)
    right = -np.arange(n, dtype=np.float32)
    return np.stack([left, right], axis=1)


# --------------------------------------------------------------------------- #
# to_mono                                                                     #
# --------------------------------------------------------------------------- #


def test_a_one_dimensional_chunk_passes_through_unchanged() -> None:
    """Today's live shape: byte-identical, so the fix changes nothing on the box."""
    chunk = np.linspace(-1.0, 1.0, 128, dtype=np.float32)
    assert np.array_equal(to_mono(chunk), chunk)


def test_a_single_channel_column_is_flattened_not_reshaped_away() -> None:
    chunk = np.linspace(-1.0, 1.0, 128, dtype=np.float32).reshape(-1, 1)
    out = to_mono(chunk)
    assert out.ndim == 1
    assert np.array_equal(out, chunk.reshape(-1))


def test_a_stereo_chunk_selects_one_channel_and_never_interleaves() -> None:
    """The hazard, closed: N frames in, N samples out — the AEC channel's."""
    stereo = _stereo()
    out = to_mono(stereo)
    assert out.ndim == 1
    assert out.size == stereo.shape[0]  # NOT 2 * shape[0], which is the interleave
    assert np.array_equal(out, stereo[:, AEC_CHANNEL])
    assert not np.array_equal(out, stereo.reshape(-1)[: stereo.shape[0]])


def test_the_selected_channel_is_the_aec_channel_by_default() -> None:
    """``reachy_nova`` picks channel 0 — the AEC-processed one — and so do we."""
    assert AEC_CHANNEL == 0
    stereo = _stereo()
    assert np.array_equal(to_mono(stereo), to_mono(stereo, channel=0))


def test_an_explicit_channel_can_be_asked_for() -> None:
    stereo = _stereo()
    assert np.array_equal(to_mono(stereo, channel=1), stereo[:, 1])


def test_an_out_of_range_channel_falls_back_to_the_channel_mean() -> None:
    """A guess is worse than an average: never index past the real channel count."""
    stereo = _stereo()
    out = to_mono(stereo, channel=7)
    assert out.ndim == 1
    assert np.allclose(out, stereo.mean(axis=1))


def test_a_chunk_is_always_returned_as_contiguous_float32() -> None:
    stereo = _stereo().astype(np.float64)
    out = to_mono(stereo)
    assert out.dtype == np.float32
    assert out.flags["C_CONTIGUOUS"]


@pytest.mark.parametrize(
    "raw",
    [
        None,
        "not audio",
        object(),
        np.zeros((2, 2, 2), dtype=np.float32),  # a shape no mic produces
    ],
)
def test_an_unusable_input_degrades_to_no_audio(raw) -> None:
    """A sense tap must never raise on a hostile chunk — it reports "no audio"."""
    assert to_mono(raw) is None


def test_an_empty_chunk_stays_empty_rather_than_becoming_none() -> None:
    """Emptiness is the caller's "nothing this tick", not a coercion failure."""
    out = to_mono(np.zeros(0, dtype=np.float32))
    assert out is not None
    assert out.size == 0


# --------------------------------------------------------------------------- #
# Both audio consumers use it                                                 #
# --------------------------------------------------------------------------- #


class _StereoSource:
    """A media client handing out the SDK 1.9 documented ``(N, 2)`` shape."""

    def __init__(self, chunk: np.ndarray) -> None:
        self.chunk = chunk
        self.samplerate = 16000
        self.channels = 2
        self.connected = True
        self.served = 0

    def audio(self):
        self.served += 1
        return self.chunk


def test_the_audio_pump_selects_a_channel_rather_than_interleaving() -> None:
    stereo = _stereo(32)
    pump = AudioPump(_StereoSource(stereo))
    got = pump._read()
    assert got is not None
    assert np.array_equal(got, stereo[:, AEC_CHANNEL])


def test_the_transcript_driver_selects_a_channel_rather_than_interleaving() -> None:
    stereo = _stereo(32)
    driver = TranscriptSenseDriver(media=_StereoSource(stereo))
    try:
        got = driver._read_audio()
        assert got is not None
        assert np.array_equal(got, stereo[:, AEC_CHANNEL])
    finally:
        driver.close()


def test_the_held_media_client_selects_a_channel_rather_than_interleaving(monkeypatch) -> None:
    """The coercion also happens at the SDK boundary, where the shape is born."""
    from reachy.robot import media_client as mc

    stereo = _stereo(32)

    class _FakeMedia:
        def start_recording(self):
            return None

        def get_input_audio_samplerate(self):
            return 16000

        def get_input_channels(self):
            return 2

        def get_audio_sample(self):
            return stereo

    class _FakeMini:
        def __init__(self) -> None:
            self.media = _FakeMedia()

        def close(self):
            return None

    monkeypatch.setattr(mc.HeldMediaClient, "_import", staticmethod(lambda: _FakeMini))
    holder = mc.HeldMediaClient(base_url="")
    try:
        got = holder.audio()
        assert got is not None
        assert np.array_equal(got, stereo[:, AEC_CHANNEL])
    finally:
        holder.close()
