"""Held, media-free SDK state reader — one ``ReachyMini(media_backend='no_media')``.

A live probe on the box (issue evidence for this task) established that the two
``ReachyMini`` construction profiles behave very differently for a process that
just wants to poll the ACTUAL head pose at high tick rate:

* The **default** ``ReachyMini()`` constructor brings up a full WebRTC
  bidirectional-audio media chain, and the process's open file descriptors climb
  from ~17 to ~96 over the run — a slow fd leak that eventually starves a
  long-lived loop (the same class of failure as issue #51, which is why
  ``MediaSession`` in :mod:`reachy.robot.sdk_transport` exists for the audio
  path). Opening a **fresh** default client per read (what
  ``SdkTransport.head_pose`` does today, ~line 394) is therefore unusable at
  50 Hz: it is both slow (media-chain bring-up per call) and leaks fds over
  time.
* ``ReachyMini(media_backend='no_media')`` opens with exactly ONE fd, and
  ``get_current_head_pose()`` reads back in ~0.02 ms — a subscription-backed
  local read, not a round trip. Held across many reads, the fd count stays
  flat: no media chain, no leak.
* Regardless of profile, the process **hangs at interpreter exit** unless the
  client is explicitly closed/disconnected first — a bare "let it get
  garbage-collected" teardown does not release the underlying connection. This
  is why :class:`HeldStateReader` exposes an explicit, idempotent
  :meth:`HeldStateReader.close` rather than relying on ``__del__`` or GC.

:class:`HeldStateReader` is the state-side counterpart to
:class:`~reachy.robot.sdk_transport.MediaSession`: where ``MediaSession`` holds
ONE client for the loop's audio/video lifetime, this class holds a SEPARATE,
``no_media`` client for the loop's pose-read lifetime — a runtime process that
needs both a live media session (mic/camera) and a held pose reader constructs
one of each, never sharing the ``no_media`` client with the media one (they are
different ``ReachyMini`` construction profiles).

**Construction BLOCKS — warm it up off the tick thread.** Even the light
``no_media`` profile takes order-of-a-second to come up, and construct-on-first-
read charges that to whichever thread reads first. On the deployed box that is
the 50 Hz tick thread, and it produced a REPRODUCIBLE tick-budget violation on
every single runtime start — the ``[SENSE stage=state]`` "connected" line
immediately followed by an overrun of **424.93, 974.39, 990.61, 1102.92 or
1212.66 ms against a 20 ms budget** (21x-61x over), at tick ~447-453
(``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3).

A tick-thread caller therefore gets two doors, and should use both:

* :meth:`HeldStateReader.warm_up` — the owner constructs at setup (before the
  loop starts) or from a background thread, and checks the result. Once it
  succeeds, no read ever constructs.
* ``allow_inline_connect=False`` — closes the on-thread door outright: reads
  then NEVER construct, only :meth:`HeldStateReader.warm_up` does. This covers
  what warm-up alone cannot. A mid-run read fault drops the client, and the next
  read would otherwise rebuild it inline — reproducing the same stall mid-run
  rather than at start. With the door closed the owner notices via
  :attr:`HeldStateReader.connected` and re-warms off-thread.

Both are ADDITIVE, with a lazy default: an owner that is not on a tick thread
needs no ceremony and every pre-existing caller is unaffected. The class also
stays PASSIVE — it spawns no thread of its own to warm itself, which would
re-introduce the interpreter-exit hazard :meth:`HeldStateReader.close` exists to
avoid. It only stops forcing the caller to construct on whatever thread happens
to read first. This mirrors
:class:`reachy.robot.media_client.HeldMediaClient` point for point, so the two
holders a runtime process owns are warmed and closed the same way.

Pitch/yaw extraction reuses
:func:`reachy.robot.sdk_transport._euler_pitch_yaw` — the exact same
decomposition ``SdkTransport.head_pose`` / ``MediaSession.head_pose`` already
use — rather than duplicating the rotation math here.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Callable

from reachy import senselog
from reachy.robot.sdk_transport import _euler_pitch_yaw

logger = logging.getLogger(__name__)

#: Seconds to wait after a failed construction/read before the next attempt.
#: Injectable via ``HeldStateReader(retry_backoff=...)`` for tests; production
#: code can leave this at the default.
DEFAULT_RETRY_BACKOFF = 5.0

_STAGE = "state"
_SOURCE = "head_pose"


class HeldStateReader:
    """Hold AT MOST ONE ``ReachyMini(media_backend='no_media')`` client, lazily.

    Construction happens once, and the owner chooses where. Call :meth:`warm_up`
    off the tick thread before the loop starts (see the module docstring's
    measured 425-1213 ms startup overruns). By default construction ALSO happens
    lazily on first :meth:`read` — convenient for a non-tick-thread owner, but
    exactly the tick-budget hazard on a 50 Hz loop; pass
    ``allow_inline_connect=False`` to forbid it.

    Once a client is up, every subsequent read reuses it. If construction (or a
    read on an already-open client) fails, the client is dropped and
    reconstruction is not re-attempted until ``retry_backoff`` seconds have
    elapsed (an injected clock — see the ``now`` parameter — makes this
    deterministic in tests, mirroring the pattern in
    :class:`reachy.behavior.sense.DoaPoller` and
    :class:`reachy.sleep.state.SleepStateMachine`). A missing SDK is a special
    case: it degrades to a PERMANENTLY-None reader after exactly one logged
    warning — the extra will not appear mid-process, so there is no point
    retrying, and doing so would just be a retry storm.

    No public method raises: every failure (missing SDK, construction error, a
    raising pose read) degrades to ``None`` / ``False``.

    Thread-use note: this object holds no locks and is **not thread-safe** by
    design, exactly like the serial ``MotionQueue`` / single-SDK-owner objects
    elsewhere in this repo. The supported split is narrow and deliberate, and
    matches :class:`reachy.robot.media_client.HeldMediaClient`: :meth:`warm_up`
    and :meth:`close` on the owner's thread while the loop is NOT running (setup
    and teardown), and every :meth:`read` on the one tick thread. Do not call
    :meth:`warm_up` concurrently with reads.

    :param allow_inline_connect:
        When ``False``, reads never construct — only :meth:`warm_up` does — so a
        tick-thread caller can never stall on a connect, at start OR mid-run
        after a fault. Defaults to ``True`` (lazy, the pre-existing shape).
    """

    def __init__(
        self,
        *,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        now: Callable[[], float] = time.monotonic,
        allow_inline_connect: bool = True,
    ) -> None:
        self._retry_backoff = retry_backoff
        self._now = now
        self._allow_inline_connect = allow_inline_connect
        self._client: object | None = None
        self._closed = False
        self._sdk_absent = False
        self._next_attempt_t: float | None = None

    # ------------------------------------------------------------------
    # the injectable import seam
    # ------------------------------------------------------------------

    @staticmethod
    def _import():  # type: ignore[no-untyped-def]
        """Import ``reachy_mini.ReachyMini``, mirroring ``SdkTransport._import``'s seam.

        Unlike ``SdkTransport._import`` (which raises a ``CliError`` — the CLI
        verb it backs must fail loudly when the SDK is missing), this returns
        ``None`` on ``ImportError``: a state reader is a best-effort background
        input, not a user-invoked command, so "no reading available" is the
        correct degradation, never an exception. Returning ``None`` here (as
        opposed to raising) is what lets :meth:`HeldStateReader._ensure_client`
        latch into the permanently-absent state with a single warning.

        A ``@staticmethod`` so a test can inject a FAKE via
        ``monkeypatch.setattr(HeldStateReader, "_import", ...)`` — the exact
        seam ``tests/test_sdk_transport.py`` uses for ``SdkTransport`` — without
        installing ``reachy_mini``.
        """
        try:
            from reachy_mini import ReachyMini
        except ImportError:
            return None
        return ReachyMini

    # ------------------------------------------------------------------
    # public API — warm-up (call this OFF the tick thread)
    # ------------------------------------------------------------------

    def warm_up(self) -> bool:
        """Construct the client now, on the CALLER's thread. Returns success.

        The affordance that keeps a connect off the tick thread. Bringing up
        even the light ``no_media`` handle blocks for order-of-a-second on real
        hardware (425-1213 ms measured — see the module docstring), so the owner
        calls this at setup or from a background thread and only then starts the
        loop; afterwards no read constructs.

        Idempotent and never raises: returns ``True`` when a live client is held
        (including when one already was), ``False`` for every degradation — the
        reader is closed, the ``[sdk]`` extra is absent, the attempt is inside
        the retry-backoff window, or construction just failed. A ``False`` is
        safe to retry; the backoff throttles a polling owner to the same cadence
        a read would have got, so an off-thread retry loop cannot storm.

        This method spawns NO thread. Which thread warms the reader is the
        owner's decision — see the class docstring's thread-use note.
        """
        if self._closed:
            return False
        if self._client is None:
            self._ensure_client()
        return self._client is not None

    @property
    def connected(self) -> bool:
        """Whether a live client is currently held. Pure predicate, never constructs.

        Free to poll: a supervisor watching for a mid-run drop (so it can
        :meth:`warm_up` again off-thread) reads this every tick at no cost.
        """
        return self._client is not None

    # ------------------------------------------------------------------
    # public API — reads
    # ------------------------------------------------------------------

    def read(self) -> tuple[float, float] | None:
        """Return ``(pitch_deg, yaw_deg)`` from the held client, or ``None``.

        ``None`` covers every "no reading" case: the reader is closed, the SDK
        is absent, construction is in its retry backoff window, construction
        just failed, inline construction is forbidden and :meth:`warm_up` has
        not yet succeeded, or the pose read itself raised. Never raises.

        Non-blocking on the real SDK surface (a subscription-backed local read,
        ~0.02 ms) — EXCEPT a first read that triggers the lazy construction,
        which blocks for order-of-a-second. On a tick thread, :meth:`warm_up`
        first (and ideally construct the reader with
        ``allow_inline_connect=False``).
        """
        if self._closed:
            return None
        if self._client is None and self._allow_inline_connect:
            self._ensure_client()
        if self._client is None:
            return None
        try:
            pose = self._client.get_current_head_pose()  # type: ignore[attr-defined]
        # A read fault must degrade, not raise.
        except Exception as err:  # noqa: BLE001
            self._drop_client(reason=f"read failed ({err})")
            return None
        return _euler_pitch_yaw(pose)

    # ------------------------------------------------------------------
    # public API — lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Release the held client, if any. Idempotent; safe to call repeatedly.

        After ``close()``, :meth:`read` returns ``None``, :meth:`warm_up`
        returns ``False``, and no further construction is ever attempted — the
        reader stays closed for good. This is the method that keeps the process
        from hanging at interpreter exit; it is the owner's job to call it.
        """
        if self._closed:
            return
        self._closed = True
        self._release_client()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_client(self) -> None:
        """Attempt construction at most once per retry window; never per read."""
        if self._sdk_absent:
            return
        now = self._now()
        if self._next_attempt_t is not None and now < self._next_attempt_t:
            return  # still backing off from a prior failure

        reachy_mini_cls = self._import()
        if reachy_mini_cls is None:
            self._sdk_absent = True
            logger.warning(
                "HeldStateReader: reachy_mini SDK not installed; "
                "state reads permanently disabled"
            )
            self._log_transition("sdk-absent: reachy_mini is not installed; state reads disabled")
            return

        try:
            self._client = reachy_mini_cls(media_backend="no_media")
        # A construction fault must degrade, not raise.
        except Exception as err:  # noqa: BLE001
            self._next_attempt_t = now + self._retry_backoff
            logger.warning("HeldStateReader: construction failed (%s); will retry", err)
            self._log_transition(
                f"retrying: construction failed ({err}); next attempt in {self._retry_backoff}s"
            )
            return

        self._next_attempt_t = None
        self._log_transition("connected (media_backend=no_media)")

    def _drop_client(self, *, reason: str) -> None:
        """A live client just failed a read: release it and schedule a retry."""
        self._release_client()
        self._next_attempt_t = self._now() + self._retry_backoff
        logger.warning("HeldStateReader: %s; connection lost, will retry", reason)
        self._log_transition(f"lost: {reason}; will retry")

    def _release_client(self) -> None:
        """Best-effort close/disconnect of the held client, tolerating either being absent."""
        client = self._client
        self._client = None
        if client is None:
            return
        for method_name in ("close", "disconnect"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:  # noqa: BLE001
                # Teardown must never raise — and a raising close() should not
                # stop us trying disconnect() as the fallback (review finding).
                logger.warning("HeldStateReader: %s() raised during release", method_name)
                continue
            break

    def _log_transition(self, detail: str) -> None:
        """Emit exactly one [SENSE] line for a state transition (never per read)."""
        senselog.stage(_STAGE, _SOURCE, uuid.uuid4().hex[:8], detail)
