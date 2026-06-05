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
        kwargs: dict[str, object] = {}
        if head is not None:
            kwargs["head"] = self._create_head_pose(
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
                    "install the sdk extra: pip install 'reachy-cli[sdk]', "
                    "or use --transport http"
                ),
            ) from err
        return ReachyMini, create_head_pose

    # --- device ----------------------------------------------------------
    def robot_state(self) -> object:
        reachy_mini_cls, _ = self._import()
        with reachy_mini_cls() as mini:
            pose = mini.get_current_head_pose()
            antennas = mini.get_present_antenna_joint_positions()
        return {
            "head_pose": pose.tolist() if hasattr(pose, "tolist") else pose,
            "antennas_position": list(antennas) if antennas is not None else None,
        }

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
        kwargs: dict[str, object] = {
            "duration": duration,
            "method": _INTERP_TO_SDK.get(interpolation, "minjerk"),
        }
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
