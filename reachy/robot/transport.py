"""Transport layer for reachy-mini-cli robot operations.

Two interchangeable *flavors* speak to the same Reachy Mini robot:

* ``http`` (default) — talks to the Reachy daemon's REST API using only the
  Python standard library, so the CLI keeps its zero-runtime-dependency
  property. This mirrors how the sibling ``reachy-mini-mcp`` server works.
* ``sdk`` — lazily imports the optional ``reachy_mini`` package (the ``[sdk]``
  extra) and drives the robot through the in-process ``ReachyMini`` client.

A :class:`Transport` exposes one method per high-level operation. The base
class raises a structured :class:`CliError` for any operation a given flavor
does not implement, so a flavor only provides what it genuinely supports.

Operation methods take **friendly units** — millimetres for translation,
degrees for rotation. Each transport converts to whatever its target expects
(the daemon wants metres + radians; the SDK's ``create_head_pose`` takes the
``mm``/``degrees`` flags directly).
"""

from __future__ import annotations

import argparse
import os
from typing import Iterator, Protocol

from reachy.cli._errors import EXIT_USER_ERROR, CliError

DEFAULT_BASE_URL = "http://localhost:8000"
DEFAULT_TIMEOUT = 10.0
TRANSPORTS = ("http", "sdk")
# CLI-facing interpolation names (the daemon's ``InterpolationMode`` values).
INTERPOLATIONS = ("minjerk", "linear", "ease", "cartoon")


class TargetSink(Protocol):
    """A streaming target sink: an open session a high-rate loop pushes poses to.

    The behavior engine composes a complete pose every tick and calls
    :meth:`set_target` on a sink obtained from :meth:`Transport.streaming`, so the
    underlying robot session is opened once for the loop's lifetime rather than
    per call. Units are the CLI's friendly ones (mm / degrees).
    """

    def set_target(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
    ) -> object: ...


class Transport:
    """Base interface for a robot transport flavor.

    Subclasses override the operations they support; everything else falls
    through to a structured "not supported on this transport" error.
    """

    name = "base"

    # --- device ----------------------------------------------------------
    def daemon_status(self) -> object:
        raise self._unsupported("device status")

    def robot_state(self) -> object:
        raise self._unsupported("device state")

    def doa(self, *, timeout: float | None = None) -> object:
        """Read the sound Direction of Arrival (``{angle, speech_detected}`` or null).

        Polled at a low rate by the behavior engine's sense source; ``timeout``
        overrides the transport's default so a slow daemon can't stall the loop.
        """
        raise self._unsupported("state doa")

    # --- apps ------------------------------------------------------------
    def apps_list(self) -> object:
        raise self._unsupported("app list")

    def app_status(self) -> object:
        raise self._unsupported("app status")

    def app_start(self, name: str) -> object:
        raise self._unsupported("app start")

    def app_stop(self) -> object:
        raise self._unsupported("app stop")

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
        raise self._unsupported("move goto")

    def wake(self) -> object:
        raise self._unsupported("move wake")

    def sleep(self) -> object:
        raise self._unsupported("move sleep")

    # --- streaming / immediate target ------------------------------------
    def set_target(
        self,
        *,
        head: dict[str, float] | None = None,
        antennas: tuple[float, float] | None = None,
        body_yaw: float | None = None,
    ) -> object:
        """Set an immediate (non-interpolated) target. Friendly units in."""
        raise self._unsupported("move set_target")

    def streaming(self) -> Iterator[TargetSink]:
        """Open one session for a high-rate loop, yielding a :class:`TargetSink`.

        Flavors that support streaming override this (as a context manager); the
        base just raises the standard "not supported on this transport" error.
        """
        raise self._unsupported("move streaming")

    # --- helpers ---------------------------------------------------------
    def _unsupported(self, op: str) -> CliError:
        return CliError(
            code=EXIT_USER_ERROR,
            message=f"'{op}' is not supported on the '{self.name}' transport",
            remediation="retry with --transport http",
        )


def add_robot_args(parser: argparse.ArgumentParser) -> None:
    """Add the flags every robot verb shares: --json, transport selection, etc.

    Env defaults: ``REACHY_TRANSPORT`` (flavor) and ``REACHY_BASE_URL`` (daemon
    URL) so an operator can set them once for a session.
    """
    parser.add_argument("--json", action="store_true", help="Emit structured JSON.")
    parser.add_argument(
        "--transport",
        choices=TRANSPORTS,
        default=os.environ.get("REACHY_TRANSPORT", "http"),
        help="Which robot transport flavor to use (default: http; env REACHY_TRANSPORT).",
    )
    parser.add_argument(
        "--base-url",
        default=os.environ.get("REACHY_BASE_URL", DEFAULT_BASE_URL),
        help="Daemon base URL for the http transport (env REACHY_BASE_URL).",
    )
    parser.add_argument(
        "--timeout",
        type=float,
        default=DEFAULT_TIMEOUT,
        help=f"Request timeout in seconds (default: {DEFAULT_TIMEOUT}).",
    )


def get_transport(args: argparse.Namespace) -> Transport:
    """Build the transport selected by ``args`` (see :func:`add_robot_args`)."""
    transport = getattr(args, "transport", "http")
    # argparse ``choices`` only validate CLI tokens, not the env-var default, so
    # re-check here to fail loud on e.g. REACHY_TRANSPORT=sdk2 instead of
    # silently falling through to http.
    if transport not in TRANSPORTS:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"unknown transport {transport!r}",
            remediation=f"set --transport / REACHY_TRANSPORT to one of: {', '.join(TRANSPORTS)}",
        )
    if transport == "sdk":
        from reachy.robot.sdk_transport import SdkTransport

        return SdkTransport()
    from reachy.robot.http_transport import HttpTransport

    return HttpTransport(
        base_url=getattr(args, "base_url", DEFAULT_BASE_URL),
        timeout=getattr(args, "timeout", DEFAULT_TIMEOUT),
    )
