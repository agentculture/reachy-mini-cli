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

The moving floor (issue #95)
----------------------------
The deployed rms-driven ``look-toward-sound`` rule self-sustained: the robot's
OWN actuator noise (head + antennas) cleared the rule's 0.02 admission floor,
admitted orienting, which made more noise. Measured: a still robot in a quiet
room NEVER crosses 0.02 (1459 samples, max 0.00953); with the runtime running
in the same room the rule genuinely fired 42x in 3 h. The fix — decided with
the operator — is a MOTION-CONDITIONED floor, not a global threshold raise and
not a binary inhibit rule: while an injected ``moving`` peek (a
:class:`reachy.behavior.self_motion.SelfMotionDriver` latch) reports the
engine is commanding motion AND the measured rms is below ``moving_floor``
(env ``REACHY_RMS_FLOOR_MOVING``, default INFINITY), the provider reports
QUIET (``0.0``). While still — where the 0.02 floor is measured correct — or
when a finite moving floor is exceeded (the future measured-floor mode), the
measured rms passes through unchanged.

Two distinct "nothing to hear" values, kept deliberately separate:

* ``None`` — NO READING (no chunk, a raising source): unchanged semantics on
  both sides of the gate, and never produced by suppression.
* ``0.0`` — a reading was taken and the moving floor judged it self-noise.

Suppression is observable per TRANSITION, never per tick: the gate emits one
``[SENSE stage=gate source=rms event=moving-floor]`` line when it opens
(naming the reason and the active floor) and one when it closes (naming how
many suppressed reads it held), per :mod:`reachy.senselog`'s "a drop always
names its reason" discipline.

The derived RELATIVE reading (issue #102)
-----------------------------------------
``rms`` above is a raw loudness and stays one — it is an honest measurement and
other consumers read it. What ADMISSION keys on is now
:class:`RmsSense`'s second, derived field: ``rms_ratio``, this tick's loudness
over a rolling estimate of the room's own background
(:class:`reachy.behavior.rms_background.RmsBackground`). The measured reason is
in that module's docstring — the mic background drifts ~25x across conditions
the same robot lives in within 24 h, so an absolute floor is either under the
night background or above the daytime signal, never both.

The comparison has to be made HERE, upstream of the predicate, because a
:class:`reachy.behavior.rules.Rule` carries exactly ONE ``when`` predicate:
"loud relative to the background" cannot be written as a conjunction in a rules
file, so it has to arrive as a sense field that already means it.

:class:`RmsSense` is what fans ONE mic read out to both: :meth:`RmsSense.pull`
takes the reading once per tick (idempotent on the tick's clock value, the same
contract ``_AudioTap`` gives the audio itself) and :meth:`RmsSense.rms` /
:meth:`RmsSense.ratio` are plain latch peeks. Feeding the estimator twice for
one tick would double-weight that tick in the background; recomputing the ratio
from a stale latch would decouple the two fields of one snapshot.

Degradation
-----------
Every failure mode — no client, a closed/absent holder, a raising read, a
``None``/empty/degenerate chunk — resolves to ``None`` (i.e. ``Sense.rms``
stays unset), never an exception. This mirrors
:func:`reachy.behavior.sense._peek`'s own never-raise contract and
:class:`reachy.behavior.sense.DoaPoller`'s failure handling: a provider must
never crash the 50 Hz tick it is peeked from. A raising ``moving`` peek
degrades to "not moving" — i.e. to the measured rms passing through unchanged,
exactly the pre-#95 behavior.
"""

from __future__ import annotations

import math
import time
from typing import Any, Callable

from reachy import senselog
from reachy.behavior.rms_background import RmsBackground
from reachy.behavior.sense import RmsProvider, RmsRatioProvider, SelfMovingProvider
from reachy.motion.rms import compute_rms

#: Zero-arg callable returning the latest raw mic chunk (an ndarray-like), or
#: ``None`` when there is no reading this tick. The exact shape of
#: ``reachy.robot.media_client.HeldMediaClient.audio`` (bound method), or any
#: injected fake in tests / other callers.
AudioChunkProvider = Callable[[], Any | None]

#: The moving floor's shipped default: INFINITY — while the engine commands
#: motion, EVERY measured rms reads quiet, because #95 measured the self-noise
#: class as inseparable from real sound by amplitude alone (42 genuine rule
#: fires in 3 h with nobody making a sound). A finite value (set via
#: :data:`MOVING_FLOOR_ENV`) is the future measured-floor mode: once the
#: in-motion self-noise ceiling is measured, sound ABOVE it passes through even
#: mid-motion.
DEFAULT_MOVING_FLOOR = math.inf

#: Env override for the moving floor. Resolved at COMPOSITION time (see
#: ``reachy.cli._commands.behavior._rms_moving_floor``, which accepts the
#: string ``"inf"`` — infinity IS the shipped default); this module never reads
#: the environment itself.
MOVING_FLOOR_ENV = "REACHY_RMS_FLOOR_MOVING"

_GATE_STAGE = "gate"
_GATE_SOURCE = "rms"
_GATE_EVENT = "moving-floor"


class _MovingFloorGate:
    """Per-provider suppression state for the moving rms floor (#95).

    ``apply`` maps one measured reading through the gate. Transition logging
    lives here so it fires once per OPEN and once per CLOSE, never per tick;
    the held count is the number of suppressed reads (one per tick in the
    wired engine, whose ``sense_reader`` reads perception once a tick). A
    ``None`` measurement is "no reading" — it neither opens nor closes the
    gate, so an intermittent mic cannot churn transition lines.
    """

    def __init__(self, moving: SelfMovingProvider, floor: float) -> None:
        self._moving = moving
        self._floor = float(floor)
        self._suppressing = False
        self._held = 0

    def apply(self, measured: float | None) -> float | None:
        if measured is None:
            return None
        if self._is_moving() and measured < self._floor:
            if not self._suppressing:
                self._suppressing = True
                self._held = 0
                senselog.stage(
                    _GATE_STAGE,
                    _GATE_SOURCE,
                    _GATE_EVENT,
                    f"opened reason=self-moving floor={self._floor}",
                )
            self._held += 1
            return 0.0
        if self._suppressing:
            self._suppressing = False
            senselog.stage(
                _GATE_STAGE,
                _GATE_SOURCE,
                _GATE_EVENT,
                f"closed held_ticks={self._held}",
            )
        return measured

    def _is_moving(self) -> bool:
        return peek_moving(self._moving)


def peek_moving(moving: SelfMovingProvider | None) -> bool:
    """The self-motion latch's value, tolerating absence and any failure.

    A raising or missing peek degrades to "not moving": for the moving floor
    that means the measured rms passes through unchanged (exactly the pre-#95
    behavior), and for the background estimator it means the sample is learned.
    Both are the pre-existing behavior, and neither is ever a crash.
    """
    if moving is None:
        return False
    try:
        return bool(moving())
    except Exception:
        return False


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
    except Exception:
        return None


class RmsSense:
    """ONE mic read per tick, fanned out to the raw and the relative field.

    :meth:`pull` performs the tick's single read: it takes the injected chunk,
    maps it to a loudness, passes it through the #95 moving floor, and (when an
    estimator is wired) folds it into the rolling background and latches the
    resulting ratio. :meth:`rms` and :meth:`ratio` are then plain peeks of that
    latch, directly usable as
    ``SenseProviders(rms=sense.rms, rms_ratio=sense.ratio)``.

    The split matters in both directions. Reading audio inside each provider
    would consume the tick's chunk twice (the ``_AudioTap`` contract); folding
    the sample into the background inside each provider would double-weight the
    tick in the estimate; and deriving the ratio anywhere but from the SAME read
    would let the two fields of one :class:`reachy.behavior.sense.Sense`
    disagree. So the read happens once, here, at the top of the tick — the same
    shape ``_AudioTap`` uses for the audio itself, restated one layer up.

    *background* is optional: with none wired, :meth:`ratio` is permanently
    ``None`` and nothing is estimated — the pre-#102 behavior, byte for byte.
    *moving* is the self-motion latch peek, consulted twice per tick for two
    different jobs: it gates the raw reading (#95) and it EXCLUDES the sample
    from the background (#102, see :class:`RmsBackground`).

    Never raises: an unreachable/raising audio source, a hostile chunk and a
    raising ``moving`` peek all resolve to "no reading" for the tick.
    """

    def __init__(
        self,
        audio: AudioChunkProvider,
        *,
        moving: SelfMovingProvider | None = None,
        moving_floor: float = DEFAULT_MOVING_FLOOR,
        background: RmsBackground | None = None,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._audio = audio
        self._moving = moving
        self._gate = _MovingFloorGate(moving, moving_floor) if moving is not None else None
        self._background = background
        self._now = now
        self._rms: float | None = None
        self._ratio: float | None = None
        self._pulled_at: float | None = None

    @property
    def background(self) -> RmsBackground | None:
        """The rolling background estimator, or ``None`` when none is wired."""
        return self._background

    def pull(self, t: float | None = None) -> None:
        """Take this tick's reading once. Idempotent on *t*; never raises.

        Called with the engine's tick clock a repeated call within the same
        tick is a no-op, so a second ``read_perception`` for the same tick
        cannot feed the estimator twice. Called with no *t* (the standalone
        provider path) every call is its own read.
        """
        if t is not None and t == self._pulled_at:
            return
        self._pulled_at = t
        try:
            chunk = self._audio()
        except Exception:  # an unreachable source is "no reading"
            chunk = None
        measured = rms_from_chunk(chunk)
        self._rms = measured if self._gate is None else self._gate.apply(measured)
        if self._background is None:
            return
        self._ratio = self._background.observe(
            self._rms,
            t if t is not None else self._clock(),
            excluded=peek_moving(self._moving),
        )

    def rms(self) -> float | None:
        """This tick's raw (moving-floor gated) loudness — the ``rms`` provider."""
        return self._rms

    def ratio(self) -> float | None:
        """This tick's loudness over the rolling background — the ``rms_ratio`` provider."""
        return self._ratio

    def _clock(self) -> float:
        try:
            return float(self._now())
        except Exception:  # a broken clock must not crash the tick
            return 0.0


def make_rms_provider(
    audio: AudioChunkProvider,
    *,
    moving: SelfMovingProvider | None = None,
    moving_floor: float = DEFAULT_MOVING_FLOOR,
) -> RmsProvider:
    """Adapt an injected mic-chunk source into a ``Sense.rms``-shaped provider.

    Returns a zero-arg callable: on each call it reads *audio* once and maps
    the result through :func:`rms_from_chunk`. Any exception raised by *audio*
    itself (an unreachable/closed media client) degrades to ``None`` exactly
    like a ``None`` return would — this provider never raises, so a caller can
    wire it straight into
    ``SenseProviders(rms=make_rms_provider(media_client.audio))`` with no
    additional guarding.

    *moving* is the optional self-motion latch peek (the #95 moving floor —
    see the module docstring): while it reports ``True`` and the measured rms
    is below *moving_floor*, the reading is reported QUIET (``0.0``, never
    ``None``). With no *moving* seam wired the provider is byte-identical to
    the pre-#95 one — the gate is not even constructed.

    This is the RAW-ONLY front: no background is estimated and no ratio is
    derived. The engine composition builds a :class:`RmsSense` instead, because
    it needs both fields off one read; this factory stays for callers (and
    tests) that only want loudness.
    """
    sense = RmsSense(audio, moving=moving, moving_floor=moving_floor)

    def _provider() -> float | None:
        sense.pull()
        return sense.rms()

    return _provider


def make_rms_providers(
    audio: AudioChunkProvider,
    *,
    moving: SelfMovingProvider | None = None,
    moving_floor: float = DEFAULT_MOVING_FLOOR,
    background: RmsBackground | None = None,
) -> tuple[RmsSense, RmsProvider, RmsRatioProvider]:
    """Build the tick-coherent pair: ``(sense, rms_provider, ratio_provider)``.

    The composition helper. The caller must drive ``sense.pull(t)`` once per
    tick, at the top of its sense read and right after the audio tap's own
    pull — the providers are latch peeks and read nothing on their own.
    """
    sense = RmsSense(audio, moving=moving, moving_floor=moving_floor, background=background)
    return sense, sense.rms, sense.ratio
