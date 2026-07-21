"""One place that decides what "one mic chunk" means, whatever shape it arrives in.

Every audio consumer in this process — :class:`reachy.robot.media_client.
HeldMediaClient` at the SDK boundary, :class:`reachy.behavior.audio_pump.
AudioPump` on its background thread, and
:class:`reachy.behavior.transcript_sense.TranscriptSenseDriver` on the tick —
needs a 1-D float32 stream. They used to get one with a bare ``.reshape(-1)``,
which is not a channel selection: **it interleaves**.

Why that matters
----------------
``reachy_mini`` 1.9 documents ``AudioBase.get_audio_sample()`` as returning
shape ``(N, 2)`` (``media/audio_base.py:117-131``; ``CHANNELS = 2``, and the
GStreamer appsink caps pin ``channels=2, layout=interleaved``). Flattening an
``(N, 2)`` array yields ``[l0, r0, l1, r1, ...]`` — ``2N`` samples that are then
written into a WAV header claiming the *mic's* rate. The result plays at half
speed and an octave down, and STT reads it as slurred nonsense. ``reachy_nova``,
the working precedent on this same hardware, never does this: its
``audio_pipeline.preprocess_mic_audio`` (``:21-34``) selects channel 0 — the AEC
channel — explicitly, and averages only when asked for no particular channel.

Is it firing today? No — measured, read-only, over 829 archived STT uploads from
the deployed box (issue #108): within-pair vs across-pair ``|diff|`` ratio 0.984
where a duplicated-channel interleave scores 0.000; lag-1 autocorrelation +0.782
against lag-2 +0.641 where a two-different-channel interleave scores +0.00 /
+0.90; and every clip length a multiple of the 256-sample ALSA period rather
than of 512. Since ``.reshape(-1)`` was the only transform applied to that
audio, both stereo shapes are ruled out and the array that box delivers must be
1-D (or ``(N, 1)``). The live shape could not be read directly — the SDK media
session is single-consumer and the deployed runtime holds it — so the code is
written to be **total over the shapes a microphone can hand out** rather than to
assume either one:

* ``(N,)`` — passed through untouched, so today's box is byte-identical.
* ``(N, 1)`` — flattened.
* ``(N, C)`` with ``C > 1`` — the requested channel is SELECTED (default
  :data:`AEC_CHANNEL`), or the channel mean when that index does not exist.
* anything else (``None``, a non-array, 3-D) — ``None``, i.e. "no audio this
  tick", the degradation every caller already handles.

Pure numpy and stdlib: this module imports no SDK and opens nothing.
"""

from __future__ import annotations

from typing import Any

import numpy as np

#: The channel a multi-channel mic chunk is read from. Channel 0 is the
#: AEC-processed capture on this hardware — ``reachy_nova``'s
#: ``preprocess_mic_audio(aec_channel=0)`` default, cited rather than guessed.
AEC_CHANNEL = 0


def to_mono(raw: Any, *, channel: int = AEC_CHANNEL) -> np.ndarray | None:
    """Coerce one mic read to a contiguous 1-D float32 chunk, or ``None``.

    ``None`` means "no usable audio this tick" — a ``None`` read, a non-numeric
    object, or a rank the mic cannot have produced. It is never an error: every
    caller treats it as silence. An EMPTY array is returned as an empty array,
    because emptiness is the caller's own "nothing arrived", not a failure to
    interpret what did.

    :param raw: whatever the audio source returned.
    :param channel: which channel to take from a multi-channel chunk. An index
        past the real channel count falls back to the channel MEAN — averaging
        is a defensible reading of an unknown layout; indexing past the end, or
        silently interleaving, is not.
    """
    if raw is None:
        return None
    try:
        chunk = np.asarray(raw, dtype=np.float32)
    # A hostile chunk is "no audio", never an exception on a sense tap.
    except (TypeError, ValueError):
        return None
    if chunk.ndim == 1:
        return chunk
    if chunk.ndim != 2:
        # No microphone produces a rank-0 or rank-3+ read; refuse to guess.
        return None
    if chunk.shape[1] <= 1:
        return np.ascontiguousarray(chunk.reshape(-1))
    if 0 <= int(channel) < chunk.shape[1]:
        return np.ascontiguousarray(chunk[:, int(channel)])
    return np.ascontiguousarray(chunk.mean(axis=1, dtype=np.float32))
