"""The audio tee — the runtime's mic, fanned out to a local socket (spec c19/c29).

The robot has ONE microphone behind ONE single-consumer SDK media session (the
single-SDK-owner model in ``CLAUDE.md``), and the behavior runtime holds it. Any
second process that wants to hear the room — the embodiment layer, a bench
harness, a future panel — cannot simply open its own: the loser of that contest
throttles to ~1 Hz. So the runtime, which already has the audio, hands it out.

This module is the hand-out: a bounded, drop-don't-block fan-out of the chunk
the tick already holds onto a local ``AF_UNIX`` socket under the state dir.

**It is a CONSUMER of the one take, never a taker.**
:meth:`reachy.behavior.audio_pump.AudioPump.take` is a CONSUMING latch swap;
:class:`reachy.cli._commands.behavior._AudioTap` calls it exactly once per tick
and fans the result out to the rms providers and the transcript driver. Calling
it a second time would hand each consumer half the audio — the documented defect
class the tap exists to prevent — so the tee is *offered* that same already-taken
chunk. Structurally: nothing here names ``take`` or ``audio``, and a test in
``tests/test_behavior_audio_tee.py`` asserts exactly that over this file's AST.

Nothing blocks the 20 ms tick
-----------------------------
:meth:`AudioTee.offer` is O(1) and does **no socket I/O at all**: it coerces the
chunk at the audio boundary (:func:`reachy.robot.audio_shape.to_mono` — a
pass-through with no copy for the 1-D float32 the pump produces) and appends it
to a bounded queue. A background daemon thread owns every ``accept``, ``send``
and disconnect. This is the same discipline that removed the measured
425-1213 ms startup tick overruns (21x-61x over a 20 ms budget,
``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3) and the
same one :class:`~reachy.behavior.speech_act.SpeechActuator` follows for the
voice: a wedged consumer, a full queue and a dead socket all resolve to a NAMED
drop, never to backpressure on the tick thread.

Two bounds, because they fail for different reasons:

* the SHARED queue (:data:`DEFAULT_MAX_CHUNKS`) absorbs the tick thread's offers
  and only overflows when the worker itself is starved;
* each consumer's OUTBOX (:data:`DEFAULT_MAX_CLIENT_CHUNKS`) absorbs a slow
  reader, so one wedged consumer can never starve another.

Both drop the OLDEST chunk — freshness wins, exactly as the pump's pending
buffer decides — with one named, counted line per episode rather than one per
chunk. The one exception is a partially-sent head: a stream socket may accept a
PARTIAL write, and dropping the remainder of a chunk already on the wire would
splice the consumer's float32 frame mid-sample and misalign everything after it.
So a drop is always a whole number of samples (:class:`_ChunkQueue`).

The wire
--------
Connection-oriented (``SOCK_STREAM``), one-way, self-describing::

    {"stream":"reachy-audio-tee","version":1,"format":"f32le",
     "channels":1,"samplerate":16000}\\n
    <little-endian float32 samples, contiguous, in production order>

The header is written once per consumer at accept time, terminated by a single
newline, and answers the one question a hearer cannot guess: the mic's REAL
rate (``null`` when a cold media holder cannot report one — announced, never
guessed, because a wrong rate mis-times every server-side VAD decision).
``SOCK_SEQPACKET`` would carry message boundaries for free, but it is a
Linux-only ``AF_UNIX`` mode; a byte stream plus whole-chunk drops gives the same
alignment guarantee everywhere.

Degradation, all named
----------------------
An absent consumer, a departed one, a wedged one, an unusable path, a kill
switch (``REACHY_AUDIO_TEE=0``) and a socket path somebody else is already
serving each resolve to ONE ``[SENSE stage=audio source=tee ...] dropped
reason=<reason>`` line and an inert tee — never an exception on the tick thread
and never a silent no-op. The last of those is deliberate and load-bearing: a
path with a LIVE listener is refused rather than unlinked, because unlinking a
socket somebody is serving on is silent theft (the incumbent keeps its fd, and
nothing can ever reach it again). A stale file left by a crashed runtime — bound
but with nobody accepting — IS reclaimed.

Import boundary: stdlib + numpy + :mod:`reachy.senselog`,
:mod:`reachy.robot.audio_shape` and :func:`reachy.daemon.state_dir`. No SDK, no
``reachy.speech``, no new dependency. Composition
(``reachy/cli/_commands/behavior.py``) owns the ONE instance, starts it after
the media warm-up and closes it at shutdown, like every other worker-owning
runtime piece.
"""

from __future__ import annotations

import json
import logging
import os
import select
import socket
import threading
from collections import deque
from pathlib import Path
from typing import Any, Callable, Iterator

import numpy as np

from reachy import senselog
from reachy.daemon import state_dir
from reachy.robot.audio_shape import to_mono

logger = logging.getLogger(__name__)

#: ``[SENSE stage=audio source=tee ...]`` — this module's log identity. The
#: stage is shared with :mod:`reachy.behavior.audio_pump` on purpose: one grep
#: (``stage=audio``) shows the whole mic path, producer and fan-out together.
STAGE = "audio"
SOURCE = "tee"

#: Kill switch. Absent -> ON (the deployed ``reachy-runtime.service`` ExecStart
#: carries no flags, so a leg gated behind one would never run on the robot —
#: the same reasoning that composes the nervous-system publisher unconditionally).
ENABLED_ENV = "REACHY_AUDIO_TEE"
#: Explicit socket path, overriding the state-dir default entirely.
SOCKET_ENV = "REACHY_AUDIO_TEE_SOCKET"
#: Default socket name under :func:`reachy.daemon.state_dir`.
DEFAULT_SOCKET_NAME = "audio_tee.sock"

#: Wire identity, carried in the per-consumer header.
WIRE_NAME = "reachy-audio-tee"
WIRE_VERSION = 1
WIRE_FORMAT = "f32le"
#: numpy dtype string for :data:`WIRE_FORMAT` — cite this rather than re-deriving.
SAMPLE_DTYPE = "<f4"
BYTES_PER_SAMPLE = 4
#: The header is one JSON object terminated by exactly one newline.
HEADER_TERMINATOR = b"\n"

#: Shared queue bound — ~2 s at 32 ms/chunk, matching
#: :data:`reachy.behavior.audio_pump.DEFAULT_MAX_CHUNKS`. A worker draining
#: every beat empties this many times per second, so it only bites when the
#: worker is starved.
DEFAULT_MAX_CHUNKS = 64
#: Per-consumer outbox bound. Smaller than the shared queue on purpose: audio
#: that has been waiting behind two seconds of backlog is stale by the time it
#: would arrive, and a realtime hearer wants the present, not the past.
DEFAULT_MAX_CLIENT_CHUNKS = 32
#: Seconds the worker parks in ``select`` when there is nothing to do.
DEFAULT_BEAT_S = 0.02
#: ``listen`` backlog. More than one so a reconnecting consumer is never refused.
DEFAULT_BACKLOG = 4
#: Bound on how long :meth:`AudioTee.close` waits for the worker thread.
DEFAULT_JOIN_TIMEOUT_S = 2.0
#: Seconds the "is somebody already serving this path?" probe may take. A local
#: unix-domain connect either succeeds or is refused immediately; the timeout is
#: only there so a pathological filesystem cannot stall startup.
PROBE_TIMEOUT_S = 0.2

REASON_DISABLED = "disabled"
REASON_BIND_FAILED = "bind-failed"
REASON_SOCKET_IN_USE = "socket-in-use"
REASON_NO_CONSUMER = "no-consumer"
REASON_QUEUE_OVERFLOW = "queue-overflow"
REASON_CONSUMER_SLOW = "consumer-slow"
REASON_HEADER_REFUSED = "header-refused"
REASON_WRITE_FAILED = "write-failed"
REASON_ACCEPT_FAILED = "accept-failed"

#: Explicit falsey tokens for :func:`tee_enabled`; an explicitly EMPTY value is
#: also OFF (see that function).
_FALSEY = {"0", "false", "no", "off"}


# --------------------------------------------------------------------------- #
# Configuration                                                               #
# --------------------------------------------------------------------------- #


def tee_enabled(env: dict | None = None) -> bool:
    """Whether the tee binds at all. Default ON, with a four-way reading.

    The same reading ``REACHY_PAT_SENSE`` uses, and for the same reason: ABSENT
    means the shipped default (ON), an explicit falsey token means OFF, and an
    explicitly EMPTY value means OFF too — an operator setting a variable to
    nothing means "unset this", never "turn it on".
    """
    source = os.environ if env is None else env
    raw = source.get(ENABLED_ENV)
    if raw is None:
        return True
    value = raw.strip().lower()
    if not value:
        return False
    return value not in _FALSEY


def socket_path() -> Path:
    """Where the tee listens: :data:`SOCKET_ENV`, else under the state dir.

    The state dir is where every other cross-process runtime artefact lives
    (``state.json``, the spools, the pid/log pairs), so a consumer finds the
    socket the same way it finds those. The env override exists for a bench run
    — and for the test suite, which must never bind (or unlink) the path a
    deployed runtime is serving on.
    """
    override = os.environ.get(SOCKET_ENV)
    if override:
        return Path(override)
    return state_dir() / DEFAULT_SOCKET_NAME


def header_bytes(samplerate: object, channels: int = 1) -> bytes:
    """The one self-describing header line a consumer receives on connect."""
    rate: int | None
    try:
        rate = int(samplerate) if samplerate is not None else None
    except (TypeError, ValueError):
        rate = None
    if rate is not None and rate <= 0:
        rate = None
    payload = {
        "stream": WIRE_NAME,
        "version": WIRE_VERSION,
        "format": WIRE_FORMAT,
        "channels": int(channels),
        "samplerate": rate,
    }
    return json.dumps(payload, separators=(",", ":")).encode("utf-8") + HEADER_TERMINATOR


# --------------------------------------------------------------------------- #
# The bounded queue (one policy, both bounds)                                 #
# --------------------------------------------------------------------------- #


class _ChunkQueue:
    """A bounded FIFO whose overflow drops the OLDEST WHOLE chunk.

    Used for both bounds — the shared tick-side queue and each consumer's
    outbox — so there is exactly one definition of "what happens when audio
    outruns its reader". Freshness wins (drop-oldest), matching
    :class:`reachy.behavior.audio_pump.AudioPump`'s pending buffer.

    ``protect_head`` is the stream socket's one wrinkle: a ``send`` may accept
    only part of a chunk, leaving the head IN FLIGHT. Dropping that remainder
    would splice the consumer's float32 frame mid-sample and misalign every
    sample after it, so the head is kept and the next-oldest chunk goes instead
    — and when the head is all there is, the NEW chunk is refused. Either way a
    drop is always a whole number of samples: a time gap, never a corrupt one.
    """

    def __init__(self, max_chunks: int) -> None:
        self._max = max(1, int(max_chunks))
        self._items: deque = deque()

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator:
        return iter(self._items)

    def push(self, item: Any, *, protect_head: bool = False) -> int:
        """Append *item*, dropping as needed. Returns how many chunks were lost."""
        dropped = 0
        while len(self._items) >= self._max:
            index = 1 if protect_head else 0
            if index >= len(self._items):
                # Only the in-flight head remains: the NEW chunk is what goes.
                return dropped + 1
            del self._items[index]
            dropped += 1
        self._items.append(item)
        return dropped

    def peek(self) -> Any:
        return self._items[0]

    def popleft(self) -> Any:
        return self._items.popleft()

    def replace_head(self, item: Any) -> None:
        self._items[0] = item

    def swap(self) -> deque:
        """Exchange the backing deque for an empty one — O(1), for the drain."""
        items = self._items
        self._items = deque()
        return items


# --------------------------------------------------------------------------- #
# One connected consumer                                                      #
# --------------------------------------------------------------------------- #


class _Consumer:
    """One accepted socket plus its bounded outbox. Worker-thread only.

    Everything here runs on the tee's worker thread: the tick thread never sees
    a socket. ``send`` is non-blocking by construction, so a reader that stops
    reading fills the kernel buffer, then this outbox, and then loses the OLDEST
    audio with a named drop — it never slows anybody down.
    """

    def __init__(self, sock: socket.socket, *, max_chunks: int) -> None:
        self.sock = sock
        self.queue = _ChunkQueue(max_chunks)
        self.partial = False
        self.dropped_run = 0
        self.sent_bytes = 0

    def fileno(self) -> int:
        return self.sock.fileno()

    def enqueue(self, payload: bytes) -> int:
        dropped = self.queue.push(payload, protect_head=self.partial)
        self.dropped_run += dropped
        return dropped

    def flush(self) -> None:
        """Write what the socket will accept. Raises ``OSError`` for a dead peer."""
        while len(self.queue):
            head = self.queue.peek()
            try:
                sent = self.sock.send(head)
            except (BlockingIOError, InterruptedError):
                return  # the kernel buffer is full: the outbox holds the rest
            if sent >= len(head):
                self.queue.popleft()
                self.partial = False
            else:
                self.queue.replace_head(head[sent:])
                self.partial = True
            self.sent_bytes += sent
            if self.partial:
                return

    def close(self) -> None:
        try:
            self.sock.close()
        except OSError:  # a socket that refuses to close is already gone
            logger.debug("AudioTee: consumer socket close raised", exc_info=True)


# --------------------------------------------------------------------------- #
# The tee                                                                     #
# --------------------------------------------------------------------------- #


class AudioTee:
    """Fan the tick's ONE audio chunk out to local consumers, dropping if slow.

    Construct one during composition, :meth:`start` it after the media warm-up,
    :meth:`offer` this tick's already-taken chunk from the tick thread, and
    :meth:`close` at shutdown. Every public method is total: it returns normally
    for any input and any consumer state, reporting faults as named senselog
    drops.

    Threading contract: :meth:`offer` belongs to exactly one producer thread
    (the tick) and touches only the lock-guarded shared queue and two flags;
    the worker thread owns every socket and every log line about them;
    :meth:`start`/:meth:`close` belong to the owner's setup/teardown thread.

    :param path: socket path; defaults to :func:`socket_path`.
    :param samplerate_provider: zero-arg peek at the mic's real rate, read once
        per accepted consumer for its header. A ``None``/raising probe is a
        ``null`` rate — announced, never guessed.
    :param channels: channel count announced in the header. The wire is mono
        (:func:`~reachy.robot.audio_shape.to_mono` coerces every offer), so this
        is 1; it exists so the header states it rather than implying it.
    :param max_chunks: shared queue bound (drop-oldest).
    :param max_client_chunks: per-consumer outbox bound (drop-oldest).
    :param beat_s: seconds the worker parks in ``select`` with nothing to do.
    :param backlog: ``listen`` backlog.
    :param sndbuf: ``SO_SNDBUF`` for accepted sockets. A test seam: pinning it
        small lets a wedged consumer be reproduced in milliseconds instead of
        megabytes. ``None`` (production) leaves the OS default alone.
    :param join_timeout_s: bound on :meth:`close`'s worker join.
    :param enabled: override the :data:`ENABLED_ENV` reading (tests/bench).
    """

    def __init__(
        self,
        path: Path | str | None = None,
        *,
        samplerate_provider: Callable[[], object] | None = None,
        channels: int = 1,
        max_chunks: int = DEFAULT_MAX_CHUNKS,
        max_client_chunks: int = DEFAULT_MAX_CLIENT_CHUNKS,
        beat_s: float = DEFAULT_BEAT_S,
        backlog: int = DEFAULT_BACKLOG,
        sndbuf: int | None = None,
        join_timeout_s: float = DEFAULT_JOIN_TIMEOUT_S,
        enabled: bool | None = None,
    ) -> None:
        self._path = Path(path) if path is not None else None
        self._samplerate_provider = samplerate_provider
        self._channels = max(1, int(channels))
        self._max_client_chunks = max(1, int(max_client_chunks))
        self._beat_s = max(0.0, float(beat_s))
        self._backlog = max(1, int(backlog))
        self._sndbuf = sndbuf
        self._join_timeout_s = max(0.0, float(join_timeout_s))
        self._enabled = enabled

        self._lock = threading.Lock()
        self._pending = _ChunkQueue(max_chunks)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._listener: socket.socket | None = None
        self._consumers: list[_Consumer] = []
        self._owns_path = False
        self._closed = False
        self._active = False

        #: Read by :meth:`offer` (producer thread), written by the worker.
        #: A plain bool store/load is atomic under the GIL, which is all the
        #: fast path needs — it must never take a lock to learn "is anybody
        #: listening?".
        self._has_consumers = False
        #: Set by :meth:`offer` when audio was discarded for want of a consumer;
        #: the WORKER turns it into one named line per episode, so the tick
        #: thread never logs.
        self._discarded = False
        self._no_consumer_reported = False
        self._overflow_run = 0

        #: Diagnostics / tests (also the seam an on-box tick-budget measurement
        #: reads): fan-out attempts seen by the tick, chunks queued, chunks lost
        #: to either bound, consumers accepted.
        self.offers = 0
        self.queued = 0
        self.dropped = 0
        self.connections = 0
        self._sent_bytes_detached = 0

    # ------------------------------------------------------------------
    # introspection
    # ------------------------------------------------------------------

    @property
    def path(self) -> Path:
        """The socket path this tee serves (resolved lazily, like the state dir)."""
        if self._path is None:
            self._path = socket_path()
        return self._path

    @property
    def active(self) -> bool:
        """Whether the socket is bound and the worker is running."""
        return self._active

    @property
    def clients(self) -> int:
        """How many consumers are connected right now (a free diagnostic peek)."""
        return len(self._consumers)

    @property
    def sent_bytes(self) -> int:
        """Audio bytes actually written, live consumers included."""
        return self._sent_bytes_detached + sum(c.sent_bytes for c in self._consumers)

    # ------------------------------------------------------------------
    # lifecycle (owner's setup/teardown thread)
    # ------------------------------------------------------------------

    def start(self) -> bool:
        """Bind, listen and spawn the worker. Returns whether the tee is live.

        Every failure is a NORMAL outcome, not a fault: a disabled tee, an
        unusable path and a path somebody else already serves each disable this
        instance with one named drop and leave the runtime untouched.
        """
        if self._active or self._closed:
            return self._active
        if self._enabled is False or (self._enabled is None and not tee_enabled()):
            self._drop(REASON_DISABLED, f"({ENABLED_ENV} is off; no socket bound)")
            return False

        listener = self._bind()
        if listener is None:
            return False
        self._listener = listener
        self._active = True
        self._thread = threading.Thread(target=self._loop, name="behavior-audio-tee", daemon=True)
        senselog.stage(STAGE, SOURCE, "started", f"audio tee listening on {self.path}")
        self._thread.start()
        return True

    def close(self) -> None:
        """Stop the worker, close every socket, remove the socket file.

        Idempotent and total. The worker is a daemon thread parked in a
        ``select`` bounded by ``beat_s``, so the join cannot outlast one beat by
        much — and a timed-out join still cannot wedge interpreter exit.
        """
        if self._closed:
            return
        self._closed = True
        was_active = self._active
        self._active = False
        self._stop.set()
        thread = self._thread
        self._thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=self._join_timeout_s)
        for consumer in self._consumers:
            self._sent_bytes_detached += consumer.sent_bytes
            consumer.close()
        self._consumers = []
        self._has_consumers = False
        if self._listener is not None:
            try:
                self._listener.close()
            except OSError:
                logger.debug("AudioTee: listener close raised", exc_info=True)
            self._listener = None
        if self._owns_path:
            # Only ever OUR socket file: a refused start left somebody else's
            # in place and must not remove it on the way out.
            try:
                self.path.unlink(missing_ok=True)
            except OSError:
                logger.debug("AudioTee: socket unlink raised", exc_info=True)
            self._owns_path = False
        if was_active:
            senselog.stage(
                STAGE,
                SOURCE,
                "closed",
                f"stopped (offers={self.offers} queued={self.queued} "
                f"dropped={self.dropped} consumers={self.connections})",
            )

    def __enter__(self) -> "AudioTee":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # the producer side (tick thread) — O(1), no I/O, no logging
    # ------------------------------------------------------------------

    def offer(self, chunk: object) -> None:
        """Hand this tick's already-taken chunk to the fan-out. **O(1).**

        Never raises, never blocks, never touches a socket and never logs: the
        whole point is that a wedged consumer costs the 20 ms tick nothing. With
        nobody connected it is a bare flag store; the worker turns that into one
        named ``no-consumer`` line per episode.

        The chunk is coerced through :func:`reachy.robot.audio_shape.to_mono` at
        this boundary rather than flattened — a multi-channel read must have a
        channel SELECTED, or the wire carries both channels interleaved into one
        double-length stream the header then mislabels. For the 1-D float32 the
        pump produces this is a pass-through with no copy.
        """
        if not self._active or self._closed:
            return
        self.offers += 1
        if not self._has_consumers:
            self._discarded = True
            return
        mono = to_mono(chunk)
        if mono is None or mono.size == 0:
            return
        # The counters ride the SAME lock as the queue they describe: both
        # threads touch them, and ``+=`` on an int is a read-modify-write, not
        # an atomic. Uncontended acquisition is nanoseconds and never becomes a
        # wait — the worker only ever holds this lock for an O(1) deque swap.
        with self._lock:
            dropped = self._pending.push(mono)
            self.queued += 1
            if dropped:
                self.dropped += dropped
                self._overflow_run += dropped

    # ------------------------------------------------------------------
    # the worker (background thread) — every socket, every log line
    # ------------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                self._serve_once()
            except Exception:  # the tee must outlive any one sweep
                logger.warning("AudioTee: worker sweep raised; continuing", exc_info=True)
                self._stop.wait(self._beat_s)

    def _serve_once(self) -> None:
        self._poll()
        self._report_discarded()
        chunks = self._drain()
        if chunks:
            self._fan_out(chunks)
        self._flush_all()
        self._report_overflow()

    def _poll(self) -> None:
        """Park until something happens: a connect, a hang-up, or free space."""
        listener = self._listener
        if listener is None:
            self._stop.wait(self._beat_s)
            return
        readers: list[Any] = [listener, *self._consumers]
        writers: list[Any] = [c for c in self._consumers if len(c.queue)]
        try:
            ready_r, _ready_w, _ = select.select(readers, writers, [], self._beat_s)
        except (OSError, ValueError):
            # A consumer's fd died between building the list and selecting on
            # it; the reap below notices on the next sweep.
            self._stop.wait(self._beat_s)
            return
        if listener in ready_r:
            self._accept()
        for consumer in [c for c in self._consumers if c in ready_r]:
            self._reap_if_closed(consumer)

    def _accept(self) -> None:
        listener = self._listener
        if listener is None:
            return
        try:
            sock, _addr = listener.accept()
        except (BlockingIOError, InterruptedError):
            return
        except OSError as err:
            self._drop(REASON_ACCEPT_FAILED, f"({type(err).__name__}: {err})")
            return
        try:
            sock.setblocking(False)
            if self._sndbuf is not None:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_SNDBUF, int(self._sndbuf))
            header = header_bytes(self._samplerate(), self._channels)
            if sock.send(header) != len(header):
                raise BlockingIOError("the header did not fit the socket buffer")
        except OSError as err:
            self._drop(REASON_HEADER_REFUSED, f"({type(err).__name__}: {err})")
            try:
                sock.close()
            except OSError:
                logger.debug("AudioTee: refused consumer close raised", exc_info=True)
            return
        self._consumers.append(_Consumer(sock, max_chunks=self._max_client_chunks))
        self._has_consumers = True
        self._no_consumer_reported = False  # a new episode earns a fresh report
        self.connections += 1
        senselog.stage(
            STAGE, SOURCE, "consumer", f"consumer attached (clients={len(self._consumers)})"
        )

    def _reap_if_closed(self, consumer: _Consumer) -> None:
        """A readable consumer is either chatting at us (ignored) or gone."""
        try:
            data = consumer.sock.recv(4096)
        except (BlockingIOError, InterruptedError):
            return
        except OSError:
            data = b""
        if data:
            return  # the tee is one-way; anything sent to it is discarded
        self._detach(consumer, "consumer detached")

    def _detach(self, consumer: _Consumer, why: str) -> None:
        if consumer in self._consumers:
            self._consumers.remove(consumer)
        self._has_consumers = bool(self._consumers)
        self._sent_bytes_detached += consumer.sent_bytes
        self._report_consumer_drops(consumer)
        consumer.close()
        senselog.stage(STAGE, SOURCE, "consumer", f"{why} (clients={len(self._consumers)})")

    def _drain(self) -> list:
        """Swap out everything the tick thread queued — O(1) under the lock."""
        with self._lock:
            if not len(self._pending):
                return []
            return list(self._pending.swap())

    def _fan_out(self, chunks: list) -> None:
        if not self._consumers:
            # The one-sweep race: a consumer detached after the tick queued
            # these. Discarding them is right (they are nobody's audio now), so
            # the only requirement is that it is NAMED like every other drop.
            self._discarded = True
            return
        for chunk in chunks:
            payload = np.ascontiguousarray(chunk, dtype=SAMPLE_DTYPE).tobytes()
            for consumer in self._consumers:
                consumer.enqueue(payload)

    def _flush_all(self) -> None:
        # Iterate a SNAPSHOT: `_detach` below removes from `self._consumers`
        # mid-loop, so dropping the `list()` would mutate the sequence being
        # iterated. Static analysis reads this as a redundant copy (Sonar
        # S7504); it is not, and removing it turns a write error into skipped
        # consumers.
        for consumer in list(self._consumers):
            try:
                consumer.flush()
            except OSError as err:
                self._drop(REASON_WRITE_FAILED, f"({type(err).__name__}: {err})")
                self._detach(consumer, "consumer dropped after a write error")
                continue
            self._report_consumer_drops(consumer)

    # ------------------------------------------------------------------
    # named reporting (worker thread only, one line per episode)
    # ------------------------------------------------------------------

    def _report_discarded(self) -> None:
        if not self._discarded:
            return
        self._discarded = False
        if self._has_consumers or self._no_consumer_reported:
            return
        self._no_consumer_reported = True
        self._drop(REASON_NO_CONSUMER, "(nothing attached to the audio tee)")

    def _report_overflow(self) -> None:
        with self._lock:
            run = self._overflow_run
            self._overflow_run = 0
        if not run:
            return
        self._drop(REASON_QUEUE_OVERFLOW, f"count={run} (the tee worker fell behind the tick)")

    def _report_consumer_drops(self, consumer: _Consumer) -> None:
        run = consumer.dropped_run
        if not run:
            return
        consumer.dropped_run = 0
        with self._lock:
            self.dropped += run
        self._drop(REASON_CONSUMER_SLOW, f"count={run} (a consumer is not reading)")

    # ------------------------------------------------------------------
    # setup helpers
    # ------------------------------------------------------------------

    def _bind(self) -> socket.socket | None:
        """Bind + listen, or name why not. Never raises."""
        path = self.path
        if path.exists():
            if self._path_is_served(path):
                self._drop(REASON_SOCKET_IN_USE, f"path={path} (another tee is serving it)")
                return None
            try:
                path.unlink()  # a stale file from a crashed runtime: reclaim it
            except OSError as err:
                self._drop(REASON_BIND_FAILED, f"path={path} ({type(err).__name__}: {err})")
                return None
        listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            listener.setblocking(False)
            listener.bind(str(path))
            listener.listen(self._backlog)
        except OSError as err:
            self._drop(REASON_BIND_FAILED, f"path={path} ({type(err).__name__}: {err})")
            listener.close()
            return None
        self._owns_path = True
        return listener

    @staticmethod
    def _path_is_served(path: Path) -> bool:
        """Is somebody ACCEPTING on *path* right now?

        A refused connect means a stale file (the common case after a crash);
        an accepted one means a live incumbent whose socket must not be
        unlinked. Any other outcome — including a test lane that blocks
        ``socket.connect`` outright — is read as "cannot prove a listener", so a
        stale file never permanently disables the tee.
        """
        probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            probe.settimeout(PROBE_TIMEOUT_S)
            probe.connect(str(path))
        except Exception:  # anything but a connect is "not served"
            return False
        finally:
            probe.close()
        return True

    def _samplerate(self) -> object:
        provider = self._samplerate_provider
        if provider is None:
            return None
        try:
            return provider()
        except Exception:  # a cold holder is a null rate, not a crash
            logger.debug("AudioTee: samplerate probe raised", exc_info=True)
            return None

    @staticmethod
    def _drop(reason: str, extra: str = "") -> None:
        senselog.drop(STAGE, SOURCE, "tee", f"{reason} {extra}".strip())
