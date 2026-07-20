"""Held SDK media client — ONE mic + camera owner for the runtime process.

The SDK's media subsystem is **single-consumer**: two processes (or two clients
in one process) reading it contend, and the loser throttles to ~1 Hz. That
constraint — the "single-SDK-owner model" in ``CLAUDE.md`` — is why every live
sense was folded into one loop in #43 rather than run as sibling processes.

:class:`HeldMediaClient` is the media-side counterpart to
:class:`~reachy.robot.state_reader.HeldStateReader`, and is deliberately built
to the shape ``state_reader``'s module docstring (lines 26-32) already
sanctions: a runtime process that needs BOTH a live media session (mic/camera)
and a held pose reader constructs **one of each**, never sharing the
``no_media`` client with the media one — they are different ``ReachyMini``
construction profiles:

* ``ReachyMini(media_backend='no_media')`` — the pose reader's profile: one fd,
  no media chain. Owned by :class:`HeldStateReader`, not by this class.
* ``ReachyMini()`` — the DEFAULT profile, which brings up the full media chain
  (mic + camera + speaker). Owned here, once, for the process lifetime.

Discipline this class inherits from :class:`HeldStateReader`, point for point:

* **Construct on first use**, never per read: opening the media chain per tick
  is both slow and leaks file descriptors (issue #51, the fd-leak crash-loop).
* **Lazy retry with backoff** behind an injected clock, so a daemon that is not
  up yet is a degradation, not a crash, and not a retry storm.
* **Explicit, idempotent** :meth:`close`. Regardless of profile, the process
  **hangs at interpreter exit** unless the client is explicitly
  closed/disconnected first — ``__del__``/GC teardown does not release the
  underlying connection. This class therefore defines **no** ``__del__``, starts
  no threads and registers no ``atexit`` hook; the owner closes it (or uses it
  as a context manager, which closes it even when the body raises).
* **Degrades, never raises.** A missing ``[sdk]`` extra latches into a
  permanently-``None`` reader after exactly one logged warning, so
  ``reachy/behavior/`` stays importable and runnable on a bare box.

The reads it exposes are the ones the runtime's senses need — :meth:`audio`
(transcript + rms) and :meth:`frame` (face + frame-available) — plus the mic
:attr:`samplerate` / :attr:`channels` an STT leg needs for a WAV header, and
:attr:`camera_available` for a sense that wants to know whether frames can
exist at all. Both reads are non-blocking on the real SDK surface (a local
subscription read, ``None`` when nothing is ready this instant), so they are
safe to call at tick rate.

Wiring note: this module is a **holder only**. It is composed by the runtime's
composition root, which is what guarantees a single instance exists; nothing in
``reachy/behavior/`` imports it directly.
"""

from __future__ import annotations

import logging
import time
import uuid
from typing import Any, Callable

from reachy import senselog

logger = logging.getLogger(__name__)

#: Seconds to wait after a failed construction/read before the next attempt.
#: Injectable via ``HeldMediaClient(retry_backoff=...)`` for tests; production
#: code can leave this at the default. Matches ``state_reader``'s default so the
#: two holders in one process back off on the same cadence.
DEFAULT_RETRY_BACKOFF = 5.0

_STAGE = "media"
_SOURCE = "held_client"


class HeldMediaClient:
    """Hold AT MOST ONE default-profile ``ReachyMini`` client (mic + camera), lazily.

    Construction is attempted on first use (a read, or a
    :attr:`samplerate`/:attr:`channels`/:attr:`camera_available` query), never
    per call: once the client is up, every subsequent read reuses it. A failure
    drops the client and suppresses reconstruction until ``retry_backoff``
    seconds have elapsed on the injected clock. A missing SDK is the one
    permanent case: it latches to a disabled holder after exactly one logged
    warning (the extra cannot appear mid-process, so retrying would only be a
    storm).

    No public method raises: every failure path degrades to ``None`` / ``False``.

    Thread-use note: like :class:`~reachy.robot.state_reader.HeldStateReader` and
    the serial ``MotionQueue``, this object is touched only from the owning
    engine's tick thread. It holds no locks and is **not thread-safe** by
    design — do not share one instance across threads.
    """

    def __init__(
        self,
        *,
        retry_backoff: float = DEFAULT_RETRY_BACKOFF,
        now: Callable[[], float] = time.monotonic,
    ) -> None:
        self._retry_backoff = retry_backoff
        self._now = now
        self._client: Any | None = None
        self._media: Any | None = None
        self._samplerate: int | None = None
        self._channels: int | None = None
        self._camera: Any | None = None
        self._camera_resolved = False
        self._camera_warned = False
        self._closed = False
        self._sdk_absent = False
        self._next_attempt_t: float | None = None

    # ------------------------------------------------------------------
    # the injectable import seam
    # ------------------------------------------------------------------

    @staticmethod
    def _import():  # type: ignore[no-untyped-def]
        """Import ``reachy_mini.ReachyMini``, mirroring ``HeldStateReader._import``.

        Returns ``None`` on ``ImportError`` rather than raising a ``CliError``:
        a held media client is a best-effort background input, not a
        user-invoked command, so "no media available" is the correct
        degradation. Returning ``None`` (as opposed to raising) is what lets
        :meth:`_ensure_client` latch into the permanently-absent state with a
        single warning.

        A ``@staticmethod`` so a test can inject a FAKE via
        ``monkeypatch.setattr(HeldMediaClient, "_import", ...)`` — the seam
        ``tests/test_sdk_transport.py`` and ``tests/test_robot_state_reader.py``
        already use — without installing ``reachy_mini``.
        """
        try:
            from reachy_mini import ReachyMini
        except ImportError:
            return None
        return ReachyMini

    # ------------------------------------------------------------------
    # public API — reads
    # ------------------------------------------------------------------

    def audio(self) -> Any | None:
        """Return one mic chunk (a ``np.float32`` ndarray), or ``None``.

        ``None`` covers every "no audio" case: closed holder, absent SDK, inside
        the retry backoff, construction just failed, or the read itself raised
        (which drops the client and schedules a retry). Non-blocking on the real
        SDK surface — safe at tick rate. Never raises.
        """
        media = self._ensure_media()
        if media is None:
            return None
        try:
            return media.get_audio_sample()
        # A read fault must degrade, not raise.
        except Exception as err:  # noqa: BLE001
            self._drop_client(reason=f"audio read failed ({err})")
            return None

    def frame(self) -> Any | None:
        """Return one camera frame (a BGR ndarray), or ``None``.

        ``None`` means either "no frame ready this instant" — the ordinary case a
        grabber simply skips, which does NOT drop the client — or one of the
        "no media" cases :meth:`audio` documents, or that this robot has no
        camera at all (a latched, once-warned degradation; see
        :attr:`camera_available`). A read that *raises* drops the client and
        schedules a retry. Never raises.
        """
        media = self._ensure_media()
        if media is None:
            return None
        if not self._ensure_camera():
            return None
        try:
            return self._media.get_frame()  # type: ignore[union-attr]
        # A read fault must degrade, not raise.
        except Exception as err:  # noqa: BLE001
            self._drop_client(reason=f"frame read failed ({err})")
            return None

    # ------------------------------------------------------------------
    # public API — properties
    # ------------------------------------------------------------------

    @property
    def samplerate(self) -> int | None:
        """Mic input sample rate in Hz, or ``None`` when there is no live client.

        Read off the held client at construction (an STT leg needs it for the WAV
        header). Touching this may trigger the lazy construction, exactly like a
        read does; it never opens a second client.
        """
        self._ensure_media()
        return self._samplerate

    @property
    def channels(self) -> int | None:
        """Mic input channel count, or ``None`` when there is no live client."""
        self._ensure_media()
        return self._channels

    @property
    def camera_available(self) -> bool:
        """Whether this robot exposes a camera on the held client.

        ``False`` when there is no live client, or when the client has no camera
        (``media.camera is None`` — the real 1.9.x availability surface). A
        genuinely absent camera is reported once and then latched, so a
        frame-available sense can poll it every tick without log spam.
        """
        if self._ensure_media() is None:
            return False
        return self._ensure_camera()

    # ------------------------------------------------------------------
    # public API — lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Stop recording and release the held client. Idempotent.

        After ``close()`` every read returns ``None`` and no further construction
        is ever attempted — the holder stays closed for good. This is the method
        that keeps the process from hanging at interpreter exit; it is the
        owner's job to call it (or to use the holder as a context manager).
        """
        if self._closed:
            return
        self._closed = True
        self._release_client()

    def __enter__(self) -> "HeldMediaClient":
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    # ------------------------------------------------------------------
    # internals
    # ------------------------------------------------------------------

    def _ensure_media(self) -> Any | None:
        """Return the live media manager, constructing the client if needed."""
        if self._closed:
            return None
        if self._client is None:
            self._ensure_client()
        return self._media

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
                "HeldMediaClient: reachy_mini SDK not installed; media permanently disabled"
            )
            self._log_transition("sdk-absent: reachy_mini is not installed; media disabled")
            return

        client = None
        try:
            client = reachy_mini_cls()
            media = client.media
            # Activate the AEC mic recorder (channel 0 is the recorder default),
            # exactly as ``SdkTransport.media_session`` does on enter. A client
            # that comes up but will not record is NOT a usable media owner, so
            # it is released rather than held half-open.
            media.start_recording()
            samplerate = int(media.get_input_audio_samplerate())
            channels = int(media.get_input_channels())
        # A construction fault must degrade, not raise.
        except Exception as err:  # noqa: BLE001
            self._release_raw(client)
            self._next_attempt_t = now + self._retry_backoff
            logger.warning("HeldMediaClient: construction failed (%s); will retry", err)
            self._log_transition(
                f"retrying: construction failed ({err}); next attempt in {self._retry_backoff}s"
            )
            return

        self._client = client
        self._media = media
        self._samplerate = samplerate
        self._channels = channels
        self._next_attempt_t = None
        self._log_transition("connected (default media profile, recording)")

    def _ensure_camera(self) -> bool:
        """Resolve the camera once per client, honoring ``acquire_media``.

        Uses only the real 1.9.x surface, mirroring
        :meth:`reachy.robot.sdk_transport.MediaSession._ensure_camera`: if the
        daemon released media for direct device access (``media_released``
        truthy), ``acquire_media()`` is called once to bring the camera pipeline
        back (the SDK re-creates the manager on acquire, so the cached ``media``
        handle is refreshed). Availability is ``media.camera is not None``.

        Unlike ``MediaSession``, a genuinely absent camera is NOT a ``CliError``
        here — a background sense degrades, it does not abort the runtime — so
        this returns ``False`` after one logged warning.
        """
        if not self._camera_resolved:
            try:
                if getattr(self._client, "media_released", False):
                    acquire = getattr(self._client, "acquire_media", None)
                    if acquire is not None:
                        acquire()
                        self._media = self._client.media  # acquire re-creates the manager
                self._camera = getattr(self._media, "camera", None)
            # Camera resolution must degrade, not raise.
            except Exception as err:  # noqa: BLE001
                logger.warning("HeldMediaClient: camera resolution failed (%s)", err)
                self._camera = None
            self._camera_resolved = True
            if self._camera is None and not self._camera_warned:
                self._camera_warned = True
                logger.warning("HeldMediaClient: no camera on this robot; frames disabled")
                self._log_transition("camera-absent: no camera available; frames disabled")
        return self._camera is not None

    def _drop_client(self, *, reason: str) -> None:
        """A live client just failed a read: release it and schedule a retry."""
        self._release_client()
        self._next_attempt_t = self._now() + self._retry_backoff
        logger.warning("HeldMediaClient: %s; connection lost, will retry", reason)
        self._log_transition(f"lost: {reason}; will retry")

    def _release_client(self) -> None:
        """Stop the recorder, then close/disconnect the held client. Never raises."""
        client = self._client
        media = self._media
        self._client = None
        self._media = None
        self._samplerate = None
        self._channels = None
        self._camera = None
        self._camera_resolved = False
        if media is not None:
            stop = getattr(media, "stop_recording", None)
            if stop is not None:
                try:
                    stop()
                except Exception:  # noqa: BLE001
                    # Teardown must never raise — and a wedged recorder must not
                    # stop us releasing the client itself (the part that hangs
                    # interpreter exit if left open).
                    logger.warning("HeldMediaClient: stop_recording() raised during release")
        self._release_raw(client)

    @staticmethod
    def _release_raw(client: Any | None) -> None:
        """Best-effort close/disconnect of *client*, tolerating either being absent."""
        if client is None:
            return
        for method_name in ("close", "disconnect"):
            method = getattr(client, method_name, None)
            if method is None:
                continue
            try:
                method()
            except Exception:  # noqa: BLE001
                # A raising close() must not stop us trying disconnect().
                logger.warning("HeldMediaClient: %s() raised during release", method_name)
                continue
            break

    def _log_transition(self, detail: str) -> None:
        """Emit exactly one [SENSE] line for a state transition (never per read)."""
        senselog.stage(_STAGE, _SOURCE, uuid.uuid4().hex[:8], detail)
