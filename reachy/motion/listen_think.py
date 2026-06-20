"""Fold ``think``'s cognition trigger into the ``listen`` motion loop.

``listen`` already owns the *one* in-process SDK media session and derives a
single per-tick :class:`~reachy.motion.sense_sample.SenseSample` (direction of
arrival, mic loudness, speech flag) to drive its Tier-1 antenna lean and Tier-2
turn. :class:`ThinkHook` rides that *same* sample: it is a per-tick ``on_tick``
hook (``(transport, queue, t, commanded_head) -> None``) that feeds the loop's
shared cues into ``think``'s :class:`~reachy.speech.cognition.CognitionEngine`
and reflects cognition activity into the ``think_active`` file flag.

Why a folded hook rather than a second process
----------------------------------------------
The robot has one single-consumer SDK media subsystem. A standalone ``think``
process opening its *own* media session would contend with ``listen`` for that
one client and throttle to ~1 Hz (the same constraint that motivated folding
``pat`` in via :class:`~reachy.motion.listen_pat.PatHook`, #43; see the
single-SDK-owner model in ``CLAUDE.md``). So ``ThinkHook`` opens **no** audio of
its own — it never imports/constructs a ``ReachyMini`` client and never calls
``media_session``. Its only sense input is the injected
:data:`~reachy.motion.sense_sample.SampleProvider`, which hands it the loop's
already-computed sample. When the provider returns ``None`` (no fresh sample this
tick) the tick is a silent no-op.

Off-tick cognition (never block the loop)
-----------------------------------------
The LLM turn is slow and must never stall the 20 Hz motion loop. So the per-tick
:meth:`__call__` does only cheap work — translate the sample's cues into the
:class:`~reachy.speech.events.EventBuffer` and update the flag — and the actual
:meth:`CognitionEngine.run` loop runs on a **start-once background worker**
(mirroring how ``think run`` already runs cognition off-thread, see
:mod:`reachy.cli._commands.think`). The worker is spawned the first time a sample
arrives and consumes the buffer the hook fills; subsequent ticks only top up the
buffer. The ``spawn`` seam defaults to a real daemon thread but is injectable so
tests run the worker synchronously with no real threads.

Flag + cleanup
--------------
While the worker is running, the ``think_active`` flag (``think_active.flag``
under the state dir, see :mod:`reachy.speech.cognition_signal`) is raised — the
``listen`` idle layer reads it and drops to a quiet "focused breathe". The flag
is always cleared on the way out: :meth:`close` stops the worker (best-effort)
and clears the flag, and the ``listen`` loop calls it in its ``finally`` so the
flag never leaks past the run. Every step is guarded — a provider/engine/feed
fault degrades to "no thought this tick" and never propagates out of the tick,
because a raising hook must never kill the loop (the
:class:`~reachy.motion.listen_hooks.HookChain` also isolates hooks, but the hook
defends itself too, exactly like ``PatHook``).

Pure standard library + the existing speech engine — no new runtime dependency.
"""

from __future__ import annotations

import logging
import math
import threading
from typing import Callable, Optional

from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SampleProvider, SenseSample
from reachy.speech import cognition_signal
from reachy.speech.events import EventBuffer

logger = logging.getLogger(__name__)


def _default_spawn(target: Callable[[], None], *, name: str | None = None) -> threading.Thread:
    """Default worker spawner: a daemon :class:`threading.Thread` started at once.

    Tests inject a synchronous spawner so the cognition ``run`` loop executes
    inline (no real threads); production uses this real daemon thread so the LLM
    turn never blocks the motion loop.
    """
    thread = threading.Thread(target=target, name=name, daemon=True)
    thread.start()
    return thread


class ThinkHook:
    """A per-tick ``on_tick`` hook driving ``think`` cognition from the shared sample.

    Construct one with the loop's :data:`SampleProvider` and the
    :class:`~reachy.speech.cognition.CognitionEngine` to drive (the composition
    layer wires the *same* :class:`~reachy.speech.events.EventBuffer` into both the
    engine and this hook, so cues fed here are consumed by that engine). Pass
    :meth:`__call__` as ``on_tick=`` to :func:`reachy.motion.server.run` (usually
    inside a :class:`~reachy.motion.listen_hooks.HookChain`), and call
    :meth:`close` in the loop's ``finally`` so the ``think_active`` flag never
    leaks.

    Parameters
    ----------
    sample_provider:
        Zero-arg callable returning the loop's latest
        :class:`~reachy.motion.sense_sample.SenseSample`, or ``None`` for "no fresh
        sample this tick" (then the tick is a silent no-op). This is the hook's
        **only** sense input — it never opens audio itself.
    engine:
        The cognition engine to drive. Its :meth:`run` is invoked once on the
        background worker; cues are fed into ``buffer`` (defaulting to the engine's
        own ``buffer`` when it exposes one). Tests inject a fake.
    buffer:
        The :class:`~reachy.speech.events.EventBuffer` the sample's cues are fed
        into. Defaults to the engine's ``buffer`` attribute when present, otherwise
        a fresh buffer — but in production the composition layer passes the *same*
        buffer the engine consumes.
    spawn:
        Worker spawner ``(target, *, name) -> handle`` (the handle needs a
        ``join(timeout=...)``). Defaults to a real daemon thread; tests inject a
        synchronous spawner for determinism.
    clock:
        Injectable ``() -> float`` (unused by the core logic today; reserved so the
        hook can stamp activity deterministically in future). Defaults to
        :func:`time.monotonic`.
    """

    def __init__(
        self,
        sample_provider: SampleProvider,
        *,
        engine: object,
        buffer: Optional[EventBuffer] = None,
        spawn: Callable[..., object] | None = None,
        clock: Callable[[], float] | None = None,
    ) -> None:
        self._provider = sample_provider
        self._engine = engine
        # Feed cues into the engine's own buffer when it exposes one (the real
        # CognitionEngine and the test fake both do via `.buffer`); otherwise build
        # a fresh buffer. The composition layer passes a shared buffer explicitly.
        if buffer is not None:
            self._buffer = buffer
        else:
            self._buffer = getattr(engine, "buffer", None) or EventBuffer()
        self._spawn = spawn if spawn is not None else _default_spawn
        if clock is not None:
            self._clock = clock
        else:
            import time

            self._clock = time.monotonic

        #: The background cognition worker handle, started once on first sample.
        self._worker: object | None = None
        #: Cooperative stop flag the worker's ``stop`` predicate reads.
        self._stop = False
        #: Whether this hook currently holds the ``think_active`` flag.
        self._flag_up = False
        #: Count of samples fed (diagnostics / tests).
        self.events = 0

    # ------------------------------------------------------------------
    # Per-tick entry point
    # ------------------------------------------------------------------

    def __call__(
        self,
        transport: object,
        queue: MotionQueue,
        t: float,
        commanded_head: dict[str, float] | None = None,
    ) -> None:
        """One tick: feed the shared sample's cues + ensure the worker runs.

        Reads the loop's latest sample via the provider; a ``None`` sample is a
        silent no-op. Otherwise the sample's DoA / loudness / speech cues are
        pushed into the cognition :class:`~reachy.speech.events.EventBuffer`, the
        background cognition worker is started once, and the ``think_active`` flag
        is raised. This returns **promptly** — the LLM turn runs on the worker, not
        here. ``transport`` / ``queue`` / ``commanded_head`` are part of the shared
        ``on_tick`` contract but unused: ``ThinkHook`` drives no motion (the engine
        enqueues its own expression moves) and reads no audio off the transport.

        Every step is guarded: a provider, feed, or spawn fault is logged and
        swallowed so a transient fault degrades to "no thought this tick" and never
        kills the loop.
        """
        try:
            sample = self._provider()
        except Exception:  # noqa: BLE001
            logger.warning("ThinkHook sample provider raised; skipping tick", exc_info=True)
            return
        if sample is None:
            return
        try:
            self._feed(sample)
            self._ensure_worker()
        except Exception:  # noqa: BLE001
            logger.warning("ThinkHook tick degraded (feed/spawn fault)", exc_info=True)

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _feed(self, sample: SenseSample) -> None:
        """Translate one shared sample into a cue on the cognition event buffer.

        Mirrors ``think``'s ``_feed_doa``: hand the DoA / RMS / speech cues to
        :meth:`EventBuffer.feed_doa`. The sample's ``doa`` is in **degrees** (the
        shared :class:`SenseSample` contract); the buffer wants **radians**, so it
        is converted here (``None`` stays ``None`` — "no reading"). The buffer's
        own thresholds decide whether the reading is notable enough to become a cue.
        """
        angle_rad = None if sample.doa is None else math.radians(sample.doa)
        self._buffer.feed_doa(  # type: ignore[attr-defined]
            angle_rad=angle_rad,
            rms=float(sample.rms),
            is_speech=bool(sample.speech),
        )
        self.events += 1

    def _ensure_worker(self) -> None:
        """Start the cognition worker once and raise the ``think_active`` flag.

        Idempotent: after the first call the worker is already running, so this
        only no-ops (the buffer it consumes keeps being topped up by :meth:`_feed`).
        The flag is raised here — when cognition begins producing — and cleared in
        :meth:`close`.
        """
        if self._worker is not None:
            return
        if not self._flag_up:
            cognition_signal.write()
            self._flag_up = True
        self._worker = self._spawn(self._run_cognition, name="reachy-listen-think")

    def _run_cognition(self) -> None:
        """The background worker body: drive the engine's bounded ``run`` loop.

        Runs :meth:`CognitionEngine.run` with a ``stop`` predicate wired to this
        hook's cooperative stop flag, so :meth:`close` ends the loop. Any error from
        the cognition stack is captured here (it must not escape the worker thread,
        and motion/`listen` must keep running); the flag is still cleared by
        :meth:`close`.
        """
        try:
            self._engine.run(stop=lambda: self._stop)  # type: ignore[attr-defined]
        except Exception:  # noqa: BLE001
            logger.warning("ThinkHook cognition worker raised; cognition stopped", exc_info=True)

    def close(self) -> None:
        """Stop the worker (best-effort) and clear the ``think_active`` flag.

        Always safe and idempotent: signals the worker to stop, joins it briefly,
        and clears the flag if this hook holds it (or if it is lingering on disk).
        The ``listen`` loop calls this in its ``finally`` so an interrupt mid-thought
        never leaks the flag. A join/clear fault is swallowed — cleanup is
        best-effort by contract.
        """
        self._stop = True
        worker = self._worker
        self._worker = None
        if worker is not None:
            try:
                join = getattr(worker, "join", None)
                if join is not None:
                    join(timeout=5.0)
            except Exception:  # noqa: BLE001
                logger.warning("ThinkHook worker join failed", exc_info=True)
        if self._flag_up or cognition_signal.is_active():
            cognition_signal.clear()
        self._flag_up = False
