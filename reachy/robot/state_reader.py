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

    Construction is attempted on first :meth:`read`, never per call: once a
    client is up, every subsequent read reuses it. If construction (or a read
    on an already-open client) fails, the client is dropped and reconstruction
    is not re-attempted until ``retry_backoff`` seconds have elapsed (an
    injected clock — see the ``now`` parameter — makes this deterministic in
    tests, mirroring the pattern in :class:`reachy.behavior.sense.DoaPoller`
    and :class:`reachy.sleep.state.SleepStateMachine`). A missing SDK is a
    special case: it degrades to a PERMANENTLY-None reader after exactly one
    logged warning — the extra will not appear mid-process, so there is no
    point retrying, and doing so would just be a retry storm.

    :meth:`read` never raises: every failure (missing SDK, construction error,
    a raising pose read) degrades to ``None``.

    Thread-use note: this class is touched only from the owning engine's tick
    thread (construct-on-first-read, read, close — all on that one thread). It
    holds no locks and is **not thread-safe** by design, exactly like the
    serial ``MotionQueue`` / single-SDK-owner objects elsewhere in this repo —
    do not share one instance across threads.
    """

    def __init__(
        self,
        *,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retry_backoff = retry_backoff
        self._now = now
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
    # public API
    # ------------------------------------------------------------------

    def read(self) -> tuple[float, float] | None:
        """Return ``(pitch_deg, yaw_deg)`` from the held client, or ``None``.

        ``None`` covers every "no reading" case: the reader is closed, the SDK
        is absent, construction is in its retry backoff window, construction
        just failed, or the pose read itself raised. Never raises.
        """
        if self._closed:
            return None
        if self._client is None:
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

    def close(self) -> None:
        """Release the held client, if any. Idempotent; safe to call repeatedly.

        After ``close()``, :meth:`read` always returns ``None`` and no further
        construction is ever attempted — the reader stays closed for good.
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
