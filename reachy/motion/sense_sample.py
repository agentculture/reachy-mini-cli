"""Per-tick sense sample shared by the folded ``listen``-loop sense hooks.

The ``listen`` loop already computes direction-of-arrival, loudness, and a
speech flag once per tick (to drive the Tier-1 antenna lean and the Tier-2
turn). The folded audio-sense hooks — ``think`` (:mod:`reachy.motion.listen_think`)
and ``sleep`` (:mod:`reachy.motion.listen_sleep`) — must consume *that* sample
rather than opening a second, single-consumer media session: the hardware has
one SDK media subsystem and it is single-consumer, so a second reader would
contend and throttle to ~1 Hz (see the single-SDK-owner model in ``CLAUDE.md``
and the #43 ``PatHook`` fold-in rationale).

This module defines the small read-only value type those hooks share and the
provider callable the composition layer (``listen run --live``) supplies. The
``on_tick`` hook signature stays ``(transport, queue, t, commanded_head)``
unchanged; hooks that need audio cues take a :data:`SampleProvider` at
construction and read the latest sample inside their tick. A provider returning
``None`` means "no fresh sample this tick" and the hook must degrade silently.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Optional

import numpy as np


@dataclass(frozen=True)
class SenseSample:
    """One tick's audio sense cues, shared read-only with the sense hooks.

    Fields mirror what the ``listen`` loop already derives per tick:

    - ``rms`` — loudness of the current mic chunk (the loudness detector's value).
    - ``doa`` — direction of arrival in degrees, or ``None`` when not available.
    - ``speech`` — whether speech was detected this tick.
    - ``ts`` — a monotonic timestamp for the sample (seconds).
    - ``audio`` — the raw mic chunk for this tick (float32 ndarray), or ``None``
      when not captured — consumed by the optional STT transcribe path.
    """

    rms: float = 0.0
    doa: Optional[float] = None
    speech: bool = False
    ts: float = 0.0
    audio: np.ndarray | None = None


#: A hook reads the latest sample via this callable. ``None`` means "no fresh
#: sample this tick"; the hook must degrade silently (do nothing) in that case.
SampleProvider = Callable[[], Optional[SenseSample]]
