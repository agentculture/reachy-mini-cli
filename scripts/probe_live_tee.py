"""Read the LIVE runtime's audio tee with the layer's OWN consumer. Passive only."""

import os
import time

import numpy as np

os.environ.setdefault("REACHY_EMBODY_MEDIA_PROFILE", "robot")
from reachy.embody import media  # noqa: E402

src = media.build_media().source
print(f"source sample_rate={src.sample_rate}")
chunks, samples, peak, t0 = 0, 0, 0.0, time.monotonic()
while time.monotonic() - t0 < 6.0:
    got = src.read()
    if got is None or got.size == 0:
        time.sleep(0.01)
        continue
    chunks += 1
    samples += got.size
    peak = max(peak, float(np.abs(got).max()))
    if chunks == 1:
        print(
            f"first chunk: dtype={got.dtype} shape={got.shape} "
            f"min={got.min():.4f} max={got.max():.4f}"
        )
elapsed = time.monotonic() - t0
print(f"chunks={chunks} samples={samples} peak_abs={peak:.4f} elapsed={elapsed:.1f}s")
print(f"implied rate={samples/elapsed:.0f} Hz vs declared {src.sample_rate} Hz")
print(f"amplitude in [-1,1]: {peak <= 1.0}")
src.close()
