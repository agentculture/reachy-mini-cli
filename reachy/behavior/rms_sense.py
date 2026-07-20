"""RMS (loudness) sense provider for the symbolic behavior runtime.

:class:`~reachy.behavior.sense.Sense` already declares an ``rms`` field and
:class:`~reachy.behavior.sense.SenseProviders` already declares an ``rms``
slot, and :data:`reachy.behavior.rules.SENSE_FIELDS` already accepts ``rms`` as
a valid rule predicate field — but nothing has ever fed it: a rule keyed on
``rms`` validates cleanly and then silently never fires. This module (task
t12) is the missing feed: :func:`make_rms_provider` adapts an injected
mic-chunk source into the zero-arg
:data:`~reachy.behavior.sense.RmsProvider` shape
:func:`~reachy.behavior.sense.read_perception` expects.

Audio source
------------
The single shared media owner is
:class:`reachy.robot.media_client.HeldMediaClient` (task t10); its ``audio()``
bound method returns one mic chunk (a ``np.float32`` ndarray) or ``None``. This
module never constructs a ``HeldMediaClient`` and never opens a second reader
— per the single-SDK-owner model (``CLAUDE.md``), a sibling sense (t11's
transcript provider) reads the SAME underlying source, so composition — not
this module — owns wiring the ONE shared audio read to every consumer.
Accordingly :data:`AudioChunkProvider` is a duck-typed, zero-arg callable
(mirroring :data:`reachy.behavior.pat_sense.PoseReader`'s injected-reader
pattern): this module never imports ``reachy_mini`` or
``reachy.robot.media_client``, so it stays a dependency-free leaf like the
rest of :mod:`reachy.behavior`.

Loudness maths
--------------
The loudness computation itself is not reinvented here: :func:`rms_from_chunk`
delegates to :func:`reachy.motion.rms.compute_rms` — the SAME
``sqrt(mean(chunk**2))`` formula :class:`reachy.motion.snap.SnapDetector`
already uses (task t12 extracted it into that shared helper so there is
exactly one definition of "loudness" in this repo).

Degradation
-----------
Every failure mode — no client, a closed/absent holder, a raising read, a
``None``/empty/degenerate chunk — resolves to ``None`` (i.e. ``Sense.rms``
stays unset), never an exception. This mirrors
:func:`reachy.behavior.sense._peek`'s own never-raise contract and
:class:`reachy.behavior.sense.DoaPoller`'s failure handling: a provider must
never crash the 50 Hz tick it is peeked from.
"""

from __future__ import annotations

from typing import Any, Callable

from reachy.behavior.sense import RmsProvider
from reachy.motion.rms import compute_rms

#: Zero-arg callable returning the latest raw mic chunk (an ndarray-like), or
#: ``None`` when there is no reading this tick. The exact shape of
#: ``reachy.robot.media_client.HeldMediaClient.audio`` (bound method), or any
#: injected fake in tests / other callers.
AudioChunkProvider = Callable[[], Any | None]


def rms_from_chunk(chunk: Any) -> float | None:
    """Loudness for one mic chunk, or ``None`` for no usable chunk.

    ``None`` and empty/degenerate input are "no reading", never an error —
    this is the boundary that lets :func:`make_rms_provider` stay a pure
    pass-through onto :func:`reachy.motion.rms.compute_rms`, which itself
    assumes non-empty numeric input.
    """
    if chunk is None:
        return None
    try:
        if len(chunk) == 0:
            return None
    except TypeError:
        return None
    try:
        return compute_rms(chunk)
    except Exception:  # noqa: BLE001
        return None


def make_rms_provider(audio: AudioChunkProvider) -> RmsProvider:
    """Adapt an injected mic-chunk source into a ``Sense.rms``-shaped provider.

    Returns a zero-arg callable: on each call it reads *audio* once and maps
    the result through :func:`rms_from_chunk`. Any exception raised by *audio*
    itself (an unreachable/closed media client) degrades to ``None`` exactly
    like a ``None`` return would — this provider never raises, so a caller can
    wire it straight into
    ``SenseProviders(rms=make_rms_provider(media_client.audio))`` with no
    additional guarding.
    """

    def _provider() -> float | None:
        try:
            chunk = audio()
        except Exception:  # noqa: BLE001
            return None
        return rms_from_chunk(chunk)

    return _provider
