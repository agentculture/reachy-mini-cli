"""Background audio pump — LIVE mic audio with zero audio I/O on the tick thread (#100).

The defect this module closes was found live, by timestamp-correlating the #95
moving-floor gate against rule fires (issue #100). The SDK's audio appsink
(``reachy_mini`` 1.9, ``webrtc_client_gstreamer.py:111-118``) is ``drop=True,
max-buffers=500`` — a FIFO holding up to ~10 s of audio, designed for a consumer
that drains at production rate (the old ``listen`` loop at 50 Hz). The behavior
engine pulled ONE chunk per tick (23-43 Hz achieved), slower than production, so
the queue kept a standing backlog and every read was SECONDS stale:

* every ``moving-floor closed`` was followed within 0-540 ms by a
  ``look-toward-sound`` fire or a NOISE tier open — in a silent room, invariant
  to the gate's release-tail length. The mic reading the instant the gate opened
  was always loud, because it was replaying the robot's own past motion;
* STT transcribed a real word (``"Yeah."``) mid-silence — it transcribed the past;
* ``utterance start`` events fired continuously with nobody speaking.

Bonus defect: the SDK's ``get_sample`` BLOCKS up to 20 ms
(``try_pull_sample(20_000_000)``) when the queue is empty — audio I/O on the tick
thread, the prime suspect for the measured ~20.7 ms tick work (#97's residual).

The fix, following the transcript/face background-worker precedent
(:mod:`reachy.behavior.transcript_sense`, :mod:`reachy.behavior.face_sense`):
:class:`AudioPump` owns ALL audio acquisition on one background daemon thread —

* **Continuous drain.** The loop reads ``media.audio()`` back to back while
  chunks flow (drains are fast while the appsink queue is non-empty; only
  production pace refills it), so any standing backlog empties within ~1 s of
  connection. Chunks read BEFORE the first empty read are the backlog — stale
  by definition — and are DISCARDED, never latched: the pump announces itself
  live only once it has caught up, so a consumer polling at any rate never
  reads the past. A mid-run client loss re-runs the same drain-then-live
  sequence on return, because a reconnect's standing chunks are just as stale.
* **Beat-paced when empty.** An empty read sleeps one short beat
  (:data:`DEFAULT_BEAT_S`) before retrying. ``None`` can mean "queue empty"
  (the SDK pull already blocked 20 ms) OR "client down" (returns instantly);
  the beat prevents a spin loop in the down case and costs nothing in the
  empty case. A defensive yield (:data:`DEFAULT_YIELD_EVERY`) also beats once
  per long unbroken run of chunks, so a pathological source that never reports
  empty cannot pin a core.
* **The tick side is a latch swap.** :meth:`take` atomically swaps out the
  pending chunks (lock held for the swap only — O(1), no I/O) and returns them
  concatenated as ONE float32 array, or ``None``. The pending buffer is
  bounded (:data:`DEFAULT_MAX_CHUNKS`, ~2 s at 32 ms/chunk); overflow drops
  the OLDEST chunk so freshness always wins, and the drops are reported once
  per episode with a count — never per chunk.
* **Named transitions, per :mod:`reachy.senselog`'s grammar.** One
  ``[SENSE stage=audio source=pump ...]`` line per state transition — started,
  live (naming the discarded backlog), client-lost, closed — and one
  per-episode overflow drop line. Never a per-chunk line.

Import boundary: this module never imports ``reachy_mini`` — its only non-stdlib
imports are numpy and :mod:`reachy.robot.audio_shape` (pure numpy, the shared
"what shape is one mic chunk" answer), and it only calls the injected source's
``audio()`` (duck-typed on
:class:`reachy.robot.media_client.HeldMediaClient`: ``None`` covers every
"no audio" case and reads never raise, though the pump guards anyway) and peeks
its free ``connected`` predicate to tell "empty" from "down". numpy is a base
dependency, so the concatenation adds nothing. Composition
(``reachy/cli/_commands/behavior.py``) owns the ONE pump instance, starts it
after the media warm-up, and closes it at shutdown — like every other
worker-owning runtime piece, an unclosed thread must never hang the exit
(:meth:`close` joins with a bounded timeout, and the thread is a daemon so a
timed-out join still cannot wedge the interpreter).
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Any, Callable

import numpy as np

from reachy import senselog
from reachy.robot.audio_shape import to_mono

logger = logging.getLogger(__name__)

_STAGE = "audio"
_SOURCE = "pump"

#: Bound on chunks pending a :meth:`AudioPump.take` — ~2 s at 32 ms/chunk. A
#: consumer ticking at 20-50 Hz swaps this out many times per second, so the
#: bound only bites when the engine has stalled; freshness wins (drop-oldest).
DEFAULT_MAX_CHUNKS = 64

#: Seconds slept after an empty read (see the module docstring's beat note).
DEFAULT_BEAT_S = 0.02

#: Defensive spin guard: beat once per this many back-to-back non-empty reads.
#: The real appsink cannot sustain more than production rate (~31 chunks/s), so
#: a run this long only happens while draining a standing backlog (500 chunks =
#: at most two beats added) or against a source that never reports empty.
DEFAULT_YIELD_EVERY = 256

#: Bound on how long :meth:`AudioPump.close` waits for the pump thread.
DEFAULT_JOIN_TIMEOUT_S = 2.0

# Pump states (module-private): draining a stale backlog, live, or client-down.
_DRAINING = "draining"
_LIVE = "live"
_LOST = "lost"


class AudioPump:
    """Own ALL mic acquisition on a background daemon thread; latch for the tick.

    Construct one over the process's single held media client, :meth:`start` it
    once the client has been warmed (the drain should measure a real backlog,
    not a cold holder), read :meth:`take` from the tick thread, and
    :meth:`close` at shutdown. Every public method degrades rather than raises.

    Threading contract: the pump thread is the ONLY caller of
    ``media.audio()``; :meth:`take` may be called from exactly one consumer
    thread (the tick thread) and touches only the lock-guarded pending buffer;
    :meth:`start`/:meth:`close` belong to the owner's setup/teardown thread.

    :param media: duck-typed audio source — ``audio()`` returning a float32
        chunk or ``None``, plus an optional free ``connected`` predicate
        (absent means "assume live").
    :param max_chunks: pending-buffer bound (drop-oldest on overflow).
    :param beat_s: seconds slept after an empty read.
    :param sleep: injectable beat seam for tests (``sleep(seconds)``). The
        default sleeps on the internal stop event, so :meth:`close` interrupts
        a beat immediately.
    :param yield_every: beat once per this many back-to-back non-empty reads.
    :param join_timeout_s: bound on :meth:`close`'s thread join.
    """

    def __init__(
        self,
        media: Any,
        *,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        beat_s: float = DEFAULT_BEAT_S,
        sleep: Callable[[float], None] | None = None,
        yield_every: int = DEFAULT_YIELD_EVERY,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
    ) -> None:
        self._media = media
        self._max_chunks = max(1, int(max_chunks))
        self._beat_s = max(0.0, float(beat_s))
        self._sleep = sleep
        self._yield_every = max(1, int(yield_every))
        self._join_timeout_s = max(0.0, float(join_timeout_s))

        self._lock = threading.Lock()
        self._pending: deque = deque()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._closed = False

        # Pump-thread-only state (never touched by take()).
        self._state = _DRAINING
        self._episode_drained = 0
        self._overflow_run = 0

        #: Diagnostics / tests: total reads, backlog chunks discarded while
        #: draining, chunks dropped to the overflow bound.
        self.reads = 0
        self.drained = 0
        self.dropped = 0

    # ------------------------------------------------------------------
    # lifecycle (owner's setup/teardown thread)
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Spawn the pump thread. Idempotent; refused after :meth:`close`."""
        if self._thread is not None or self._closed:
            return
        self._thread = threading.Thread(target=self._loop, name="behavior-audio-pump", daemon=True)
        senselog.stage(
            _STAGE,
            _SOURCE,
            "started",
            f"background mic pump up (max_chunks={self._max_chunks} beat={self._beat_s}s)",
        )
        self._thread.start()

    def close(self) -> None:
        """Stop the pump with a bounded join. Idempotent, never raises.

        A dead/absent media client can never prevent exit: the loop's reads are
        guarded, the beat is interruptible, and the thread is a daemon — a join
        that times out leaves a thread that dies with the process.
        """
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._join_timeout_s)
        self._flush_overflow_episode()
        senselog.stage(
            _STAGE,
            _SOURCE,
            "closed",
            f"stopped (reads={self.reads} drained={self.drained} dropped={self.dropped})",
        )

    def __enter__(self) -> "AudioPump":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # the consumer side (tick thread)
    # ------------------------------------------------------------------

    def take(self) -> np.ndarray | None:
        """Swap out ALL pending chunks as ONE float32 array, or ``None``.

        A pure latch swap: the lock is held only to exchange the pending deque
        (O(1)), and no call into the media source ever happens here — the whole
        point of #100 is that the tick thread does zero audio I/O. Each chunk
        is delivered exactly once, in production order.
        """
        with self._lock:
            if not self._pending:
                return None
            chunks = self._pending
            self._pending = deque()
        if len(chunks) == 1:
            return chunks[0]
        return np.concatenate(tuple(chunks))

    # ------------------------------------------------------------------
    # observability
    # ------------------------------------------------------------------

    @property
    def live(self) -> bool:
        """Whether the pump has caught up with the source (backlog discarded)."""
        return self._state is _LIVE

    @property
    def closed(self) -> bool:
        return self._closed

    @property
    def pending(self) -> int:
        """Chunks currently awaiting a :meth:`take` (a free diagnostic peek)."""
        with self._lock:
            return len(self._pending)

    # ------------------------------------------------------------------
    # the pump loop (background thread)
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        streak = 0
        while True:
            beat = False
            try:
                chunk = self._read()
                if chunk is None:
                    streak = 0
                    self._on_empty()
                    beat = True
                else:
                    streak += 1
                    self._on_chunk(chunk)
                    if streak >= self._yield_every:
                        streak = 0
                        beat = True
            except Exception:  # noqa: BLE001 — the pump must outlive any one read
                logger.warning("AudioPump: loop iteration raised; continuing", exc_info=True)
                beat = True
            if self._stop.is_set():
                return
            if beat:
                self._beat()

    def _read(self) -> np.ndarray | None:
        """One guarded source read, coerced to a 1-D float32 chunk or ``None``.

        The coercion is :func:`reachy.robot.audio_shape.to_mono`, not a bare
        ``.reshape(-1)``: a multi-channel read must have a channel SELECTED, or
        flattening interleaves both channels into one double-length stream.
        """
        self.reads += 1
        try:
            raw = self._media.audio()
        except Exception:  # noqa: BLE001 — a raising source is "no audio"
            logger.debug("AudioPump: media read raised; no audio", exc_info=True)
            return None
        chunk = to_mono(raw)
        if chunk is None or chunk.size == 0:
            return None
        return chunk

    def _on_chunk(self, chunk: np.ndarray) -> None:
        if self._state is not _LIVE:
            # Draining (fresh start, or first data back after a loss): this
            # chunk was sitting in the appsink queue — stale by definition.
            if self._state is _LOST:
                self._state = _DRAINING
                self._episode_drained = 0
            self._episode_drained += 1
            self.drained += 1
            return
        ended_overflow = 0
        with self._lock:
            if len(self._pending) >= self._max_chunks:
                self._pending.popleft()  # freshness wins: drop the OLDEST
                self.dropped += 1
                self._overflow_run += 1
            elif self._overflow_run:
                ended_overflow = self._overflow_run  # space is free again
                self._overflow_run = 0
            self._pending.append(chunk)
        if ended_overflow:
            senselog.drop(_STAGE, _SOURCE, "overflow", f"deque-overflow count={ended_overflow}")

    def _on_empty(self) -> None:
        if self._state is _DRAINING:
            if self._connected() is False:
                self._state = _LOST
                senselog.stage(
                    _STAGE,
                    _SOURCE,
                    "client-lost",
                    "media client down; audio paused (will re-drain on return)",
                )
            else:
                self._state = _LIVE
                senselog.stage(
                    _STAGE,
                    _SOURCE,
                    "live",
                    f"live after discarding {self._episode_drained} stale chunk(s)",
                )
        elif self._state is _LIVE and self._connected() is False:
            self._state = _LOST
            self._flush_overflow_episode()
            senselog.stage(
                _STAGE,
                _SOURCE,
                "client-lost",
                "media client down; audio paused (will re-drain on return)",
            )
        # _LOST stays quiet: one line per episode, never one per beat.

    def _connected(self) -> bool:
        """The source's free liveness predicate; unknown/raising means live."""
        try:
            return bool(getattr(self._media, "connected", True))
        except Exception:  # noqa: BLE001 — a raising probe is not a verdict
            return True

    def _flush_overflow_episode(self) -> None:
        with self._lock:
            run = self._overflow_run
            self._overflow_run = 0
        if run:
            senselog.drop(_STAGE, _SOURCE, "overflow", f"deque-overflow count={run}")

    def _beat(self) -> None:
        if self._beat_s <= 0.0:
            return
        if self._sleep is not None:
            try:
                self._sleep(self._beat_s)
            except Exception:  # noqa: BLE001 — an injected beat must not kill the pump
                logger.debug("AudioPump: injected sleep raised", exc_info=True)
            return
        # Event-based by default so close() interrupts a beat immediately.
        self._stop.wait(self._beat_s)
