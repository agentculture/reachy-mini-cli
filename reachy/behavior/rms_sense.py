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
from typing import Any, Callable

from reachy import senselog
from reachy.behavior.sense import RmsProvider, SelfMovingProvider
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
        # A raising peek degrades to "not moving": the measured rms passes
        # through unchanged — exactly the pre-#95 behavior, never a crash.
        try:
            return bool(self._moving())
        except Exception:  # noqa: BLE001
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
    except Exception:  # noqa: BLE001
        return None


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
    """
    gate = _MovingFloorGate(moving, moving_floor) if moving is not None else None

    def _provider() -> float | None:
        try:
            chunk = audio()
        except Exception:  # noqa: BLE001
            return None
        measured = rms_from_chunk(chunk)
        return measured if gate is None else gate.apply(measured)

    return _provider
