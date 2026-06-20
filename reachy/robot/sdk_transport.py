"""SDK transport — drive the robot through the optional ``reachy_mini`` package.

This flavor is **partial by design**: it implements the motion/state operations
where the in-process ``ReachyMini`` client adds value, and inherits the base
"not supported on this transport" error for daemon-status and app-management
(which are daemon-side concerns, not part of the client SDK surface — use
``--transport http`` for those).

``reachy_mini`` is imported lazily inside each method so that:

* the default install stays dependency-free (``dependencies = []``); the SDK
  lives behind the ``[sdk]`` optional extra; and
* operations that don't need the SDK never trigger the import.

Adding this third-party runtime import is a deliberate, contained exception to
the zero-runtime-dependency rule in ``CLAUDE.md`` — see
``docs/adr-0001-sdk-transport-extra.md``.
"""

from __future__ import annotations

import contextlib
import math
from typing import Iterator

import numpy as np

from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.robot.transport import TargetSink, Transport

# CLI interpolation name -> SDK ``InterpolationTechnique`` value. The SDK calls
# the eased curve ``ease_in_out``; the daemon (and our CLI) calls it ``ease``.
_INTERP_TO_SDK = {
    "minjerk": "minjerk",
    "linear": "linear",
    "ease": "ease_in_out",
    "cartoon": "cartoon",
}


def _tuple_to_doa_dict(result: tuple[float, bool] | None) -> dict[str, object] | None:
    """Map a ``(angle, speech_detected)`` tuple from ``get_DoA()`` to the canonical dict.

    Returns ``None`` when the SDK returns ``None`` (no reading available).  The
    dict shape ``{"angle": float, "speech_detected": bool}`` matches what the
    HTTP transport returns from ``/api/state/doa`` so :func:`~reachy.behavior.sense.read_doa`
    can consume both transports identically.
    """
    if result is None:
        return None
    angle, speech = result
    return {"angle": float(angle), "speech_detected": bool(speech)}


def _euler_pitch_yaw(pose: "np.ndarray") -> tuple[float, float]:
    """Extract ``(pitch_deg, yaw_deg)`` from a head-pose rotation, in pure numpy.

    ``pose`` is the SDK's head pose as either a 4×4 homogeneous transform (what
    ``ReachyMini.get_current_head_pose()`` returns) or a bare 3×3 rotation; only
    the upper-left 3×3 rotation block is used.

    The convention matches reachy_nova's ``detect_pat``, which reads pitch and
    yaw from ``scipy``'s ``Rotation.from_matrix(R).as_euler("xyz", degrees=True)``
    (intrinsic XYZ → ``[roll, pitch, yaw]``; pitch is index 1, yaw is index 2).
    scipy is deliberately NOT a dependency here, so we close-form the same
    decomposition. For intrinsic ``R = Rx(roll) @ Ry(pitch) @ Rz(yaw)``::

        pitch = asin(R[0, 2])
        yaw   = atan2(-R[0, 1], R[0, 0])

    ``R[0, 2]`` is clamped to ``[-1, 1]`` so floating-point drift past the unit
    range can't make ``asin`` return NaN.
    """
    rot = np.asarray(pose, dtype=float)[:3, :3]
    sin_pitch = float(np.clip(rot[0, 2], -1.0, 1.0))
    pitch = math.degrees(math.asin(sin_pitch))
    yaw = math.degrees(math.atan2(-rot[0, 1], rot[0, 0]))
    return float(pitch), float(yaw)


def _target_kwargs(
    create_head_pose,  # type: ignore[no-untyped-def]
    *,
    head: dict[str, float] | None,
    antennas: tuple[float, float] | None,
    body_yaw: float | None,
) -> dict[str, object]:
    """Build the head / antennas / body_yaw kwargs (friendly mm/deg → SDK units).

    The single source of truth for the friendly→SDK conversion, shared by the
    streaming sink (:meth:`_SdkSink.set_target`) and the goto path
    (:func:`_goto_kwargs`).
    """
    kwargs: dict[str, object] = {}
    if head is not None:
        kwargs["head"] = create_head_pose(
            x=head["x"],
            y=head["y"],
            z=head["z"],
            roll=head["roll"],
            pitch=head["pitch"],
            yaw=head["yaw"],
            mm=True,
            degrees=True,
        )
    if antennas is not None:
        kwargs["antennas"] = [math.radians(antennas[0]), math.radians(antennas[1])]
    if body_yaw is not None:
        kwargs["body_yaw"] = math.radians(body_yaw)
    return kwargs


def _goto_kwargs(
    create_head_pose,  # type: ignore[no-untyped-def]
    *,
    head: dict[str, float] | None,
    antennas: tuple[float, float] | None,
    body_yaw: float | None,
    duration: float,
    interpolation: str,
) -> dict[str, object]:
    """Build ``goto_target`` kwargs (target kwargs + duration/method).

    Shared by :meth:`SdkTransport.move_goto` (opens a client per call) and
    :meth:`MediaSession.move_goto` (reuses the loop's one open client).
    """
    kwargs = _target_kwargs(create_head_pose, head=head, antennas=antennas, body_yaw=body_yaw)
    kwargs["duration"] = duration
    kwargs["method"] = _INTERP_TO_SDK.get(interpolation, "minjerk")
    return kwargs


class MediaSession:
    """A live session open against the one in-process ``ReachyMini`` client.

    Obtained exclusively through :meth:`SdkTransport.media_session` — do not
    instantiate directly.  Audio + DoA reads, the head-pose read-back, moves, and
    camera frames ALL happen through this one held client, so the loop pays the
    ``ReachyMini`` open/close cost exactly once (not per tick / per move / per
    frame).  Routing every per-tick read here is what keeps the loop from leaking
    file descriptors through the SDK's ``GStreamerAudio`` teardown (issue #51).

    The AEC (acoustic-echo-cancelled) channel is the recorder's default
    (channel 0) — ``start_recording()`` activates it automatically.
    """

    def __init__(self, mini, create_head_pose) -> None:  # type: ignore[no-untyped-def]
        self._mini = mini
        self._media = mini.media
        self._create_head_pose = create_head_pose
        self.samplerate: int = mini.media.get_input_audio_samplerate()
        self.channels: int = mini.media.get_input_channels()
        self._camera: object | None = None
        self._camera_resolved = False

    def doa(self, **_kwargs: object) -> object:
        """Read the sound Direction of Arrival.

        Returns ``{"angle": float, "speech_detected": bool}`` (angle in radians,
        ``0``=left, ``pi/2``=front, ``pi``=right), or ``None`` when the SDK has
        no reading available. Accepts and ignores transport-style keyword args
        (notably ``timeout``) so it is duck-compatible with ``Transport.doa`` —
        ``read_doa`` always passes ``timeout`` and the SDK read is non-blocking.
        """
        return _tuple_to_doa_dict(self._media.get_DoA())

    def get_audio_sample(self) -> "np.ndarray | None":
        """Return one mic chunk (``np.float32`` ndarray) or ``None`` when unavailable."""
        return self._media.get_audio_sample()  # type: ignore[return-value]

    def head_pose(self) -> tuple[float, float]:
        """Read the ACTUAL head pose as ``(pitch_deg, yaw_deg)`` through the one open client.

        The loop reads the pose back every tick (pat detection). Serving it from
        this already-open ``ReachyMini`` — instead of ``SdkTransport.head_pose``,
        which opens a fresh client per call — is what stops the loop leaking file
        descriptors via the SDK's ``GStreamerAudio`` teardown (issue #51).
        """
        return _euler_pitch_yaw(self._mini.get_current_head_pose())

    def move_goto(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
        duration: float,
        interpolation: str,
    ) -> object:
        """Stream a goto through the one open client (no per-move session open)."""
        self._mini.goto_target(
            **_goto_kwargs(
                self._create_head_pose,
                head=head,
                antennas=antennas,
                body_yaw=body_yaw,
                duration=duration,
                interpolation=interpolation,
            )
        )
        return {"status": "ok", "transport": "sdk", "action": "goto"}

    def get_frame(self) -> "np.ndarray":
        """Capture one camera frame through the one open client (no per-frame session open).

        Resolves the local camera once (lazily) off the held ``ReachyMini`` and
        reuses it — unlike ``SdkTransport.get_frame``, which builds a fresh client
        per frame and never closes it.
        """
        if not self._camera_resolved:
            available = bool(self._mini.is_local_camera_available())
            self._camera = self._mini.media_manager.camera if available else None
            self._camera_resolved = True
        if self._camera is None:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="no local camera is available on this Reachy Mini",
                remediation=(
                    "check the camera is connected and run on the robot itself "
                    "(local camera frames need connection_mode 'localhost_only')"
                ),
            )
        return self._camera.get_frame()


class _SdkSink:
    """Streaming sink over an *already-open* ``ReachyMini`` session.

    Holds the session for the loop's lifetime so a 50 Hz stream pays the
    open/close cost once, not per pose. Converts friendly units (mm / degrees) to
    the SDK's metres / radians via ``create_head_pose``.
    """

    def __init__(self, mini, create_head_pose) -> None:  # type: ignore[no-untyped-def]
        self._mini = mini
        self._create_head_pose = create_head_pose

    def set_target(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
    ) -> object:
        kwargs = _target_kwargs(
            self._create_head_pose, head=head, antennas=antennas, body_yaw=body_yaw
        )
        self._mini.set_target(**kwargs)
        return {"status": "ok", "transport": "sdk", "action": "set_target"}


class SdkTransport(Transport):
    """Drive the robot through the in-process ``ReachyMini`` client."""

    name = "sdk"

    @staticmethod
    def _import():  # type: ignore[no-untyped-def]
        try:
            from reachy_mini import ReachyMini
            from reachy_mini.utils import create_head_pose
        except ImportError as err:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="the reachy_mini SDK is not installed",
                remediation=(
                    "install the sdk extra: pip install 'reachy-mini-cli[sdk]', "
                    "or use --transport http"
                ),
            ) from err
        return ReachyMini, create_head_pose

    # --- device ----------------------------------------------------------
    def doa(self, **_kwargs: object) -> object:
        """Read the sound Direction of Arrival via the SDK media subsystem.

        Accepts and ignores transport-style keyword args (notably ``timeout``)
        for duck-compatibility with ``Transport.doa``.

        Opens a short-lived ``ReachyMini`` session, calls
        ``mini.media.get_DoA()``, and maps the ``(angle, speech_detected)``
        tuple to ``{"angle": float, "speech_detected": bool}``.  Returns
        ``None`` when the SDK has no reading available.

        The ``timeout`` parameter is accepted for interface compatibility with
        :meth:`~reachy.robot.http_transport.HttpTransport.doa` but is unused
        here (the in-process SDK call is synchronous and does not block on I/O).
        """
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            return _tuple_to_doa_dict(mini.media.get_DoA())

    # --- camera ----------------------------------------------------------
    @staticmethod
    def _import_camera():  # type: ignore[no-untyped-def]
        """Lazily resolve the local camera handle: ``(available, camera)``.

        Kept tiny and ``@staticmethod`` so a test can inject a FAKE via
        ``monkeypatch.setattr(SdkTransport, "_import_camera", ...)`` — exactly the
        seam ``_import`` uses for the rest of the SDK surface — without installing
        ``reachy_mini``.

        PARKED ASSUMPTION about the SDK camera API (mirrors the audio path's
        ``mini.media.get_DoA()`` style): a local camera is exposed through the
        media manager — ``is_local_camera_available()`` gates it, and the handle
        lives at ``media_manager.camera`` (frames via ``camera.get_frame()``,
        connection_mode == 'localhost_only'). Isolated here so a wrong guess is a
        one-line fix, not a scatter across methods.
        """
        try:
            from reachy_mini import ReachyMini
        except ImportError as err:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="the reachy_mini SDK is not installed",
                remediation=(
                    "install the sdk extra: pip install 'reachy-mini-cli[sdk]', "
                    "or use --transport http"
                ),
            ) from err
        mini = ReachyMini()
        available = bool(mini.is_local_camera_available())
        camera = mini.media_manager.camera if available else None
        return available, camera

    def get_frame(self) -> "np.ndarray":
        """Capture one frame from the local camera as a ``numpy.ndarray`` (H x W x 3).

        Frames are a *local-profile* capability (issue #22): the daemon HTTP API
        serves camera metadata only, so this exists on the ``sdk`` flavor alone.
        Raises :class:`CliError` (exit 2) when the SDK is missing or the local
        camera is unavailable — never a traceback.
        """
        available, camera = self._import_camera()
        if not available or camera is None:
            raise CliError(
                code=EXIT_ENV_ERROR,
                message="no local camera is available on this Reachy Mini",
                remediation=(
                    "check the camera is connected and run on the robot itself "
                    "(local camera frames need connection_mode 'localhost_only')"
                ),
            )
        return camera.get_frame()

    def robot_state(self) -> object:
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            pose = mini.get_current_head_pose()
            antennas = mini.get_present_antenna_joint_positions()
        return {
            "head_pose": pose.tolist() if hasattr(pose, "tolist") else pose,
            "antennas_position": list(antennas) if antennas is not None else None,
        }

    def head_pose(self) -> tuple[float, float]:
        """Read the ACTUAL current head pose as ``(pitch_deg, yaw_deg)``.

        Opens a short-lived ``ReachyMini`` session, calls
        ``get_current_head_pose()`` (a 4×4 homogeneous transform matrix), and
        normalizes its rotation block to degrees via :func:`_euler_pitch_yaw`.
        Raises a clean :class:`CliError` (exit 2) when the SDK extra is missing —
        never a traceback.
        """
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            pose = mini.get_current_head_pose()
        return _euler_pitch_yaw(pose)

    # --- move ------------------------------------------------------------
    def move_goto(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
        duration: float,
        interpolation: str,
    ) -> object:
        reachy_mini_cls, create_head_pose = self._import()
        kwargs = _goto_kwargs(
            create_head_pose,
            head=head,
            antennas=antennas,
            body_yaw=body_yaw,
            duration=duration,
            interpolation=interpolation,
        )
        with reachy_mini_cls() as mini:
            mini.goto_target(**kwargs)
        return {"status": "ok", "transport": self.name, "action": "goto"}

    def wake(self) -> object:
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            mini.wake_up()
        return {"status": "ok", "transport": self.name, "action": "wake"}

    def sleep(self) -> object:
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            mini.goto_sleep()
        return {"status": "ok", "transport": self.name, "action": "sleep"}

    # --- streaming / immediate target ------------------------------------
    def set_target(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
    ) -> object:
        # One-off immediate target (opens a session per call). The engine never
        # uses this path — it streams through ``streaming()`` to keep one session.
        with self.streaming() as sink:
            return sink.set_target(head=head, antennas=antennas, body_yaw=body_yaw)

    @contextlib.contextmanager
    def streaming(self) -> Iterator[TargetSink]:
        reachy_mini_cls, create_head_pose = self._import()
        with reachy_mini_cls() as mini:  # opened ONCE for the loop's lifetime
            yield _SdkSink(mini, create_head_pose)

    @contextlib.contextmanager
    def media_session(self) -> Iterator[MediaSession]:
        """Open a persistent audio + DoA session for a streaming listen loop.

        On enter: opens a ``ReachyMini`` context and calls
        ``mini.media.start_recording()`` to activate the AEC mic recorder.
        Yields a :class:`MediaSession` that exposes ``.doa()``,
        ``.get_audio_sample()``, ``.samplerate``, and ``.channels``.
        On exit: calls ``stop_recording()`` then closes the ``ReachyMini``
        context — even if the loop body raises.

        Use this instead of per-tick :meth:`doa` calls when the listen behavior
        is running so the SDK session and the mic recorder are opened once for
        the loop's lifetime rather than per read.
        """
        reachy_mini_cls, create_head_pose = self._import()
        with reachy_mini_cls() as mini:
            mini.media.start_recording()
            try:
                yield MediaSession(mini, create_head_pose)
            finally:
                mini.media.stop_recording()
