"""Passive held/unheld observations on the behavior engine's existing tick.

The driver in this module never commands motion and never owns a robot client.
It observes ``TickContext.pose`` only after proving that the same canonical CLI
``feel-alive`` behavior owns head, antennas, and body yaw, then samples through
the engine process's existing held pose reader.  Any missing or changed
precondition terminates the capture before pose or actual-pose inspection.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable

SCHEMA = "reachy.excited-motion-probe/v1"
MODES = ("unheld", "held")
CUES = {"unheld": "HANDS OFF", "held": "START HOLD"}
SETTLED_EDGE_S = 0.5
ARM_TIMEOUT_S = 18.0
MOTION_TIMEOUT_S = 13.0
MAX_TICK_GAP_S = 0.1
HEAD_AXES = ("x", "y", "z", "roll", "pitch", "yaw")
CHANNELS = ("head", "antennas", "body_yaw")

_UNSET = object()


class SharedPoseReader:
    """Cache the existing reader's value once per engine tick.

    The probe calls :meth:`begin_tick` before every other sense driver.  During
    capture it calls :meth:`read`; a later consumer on that same tick receives
    the cached value instead of performing a second underlying state read.
    """

    def __init__(self, source: Callable[[], tuple[float, float] | None]) -> None:
        self._source = source
        self._tick: object = _UNSET
        self._value: object = _UNSET

    def begin_tick(self, tick: object) -> None:
        if tick != self._tick:
            self._tick = tick
            self._value = _UNSET

    def read(self) -> tuple[float, float] | None:
        if self._value is _UNSET:
            try:
                self._value = self._source()
            except Exception:  # noqa: BLE001 - a read-only sense tap degrades
                self._value = None
        value = self._value
        if value is None:
            return None
        try:
            pitch, yaw = value  # type: ignore[misc]
            result = (float(pitch), float(yaw))
        except (TypeError, ValueError):
            return None
        return result if all(math.isfinite(axis) for axis in result) else None


def _feel_alive_owner(ctx) -> tuple[str | None, str | None]:  # type: ignore[no-untyped-def]
    """Return ``(owner, refusal)`` without inspecting command or actual pose."""
    ownership = getattr(ctx, "ownership", None)
    if not isinstance(ownership, dict):
        return None, "all three channel owners must be available"
    owners = tuple(ownership.get(channel) for channel in CHANNELS)
    if any(not isinstance(owner, str) for owner in owners) or len(set(owners)) != 1:
        return None, "all three channels must have the same CLI feel-alive owner"
    owner = owners[0]
    if not isinstance(owner, str):
        return None, "all three channel owners must be available"
    # Engine-assigned ids are ``<library-name>-<sequence>``. Match the complete
    # canonical name, not a loose prefix such as ``feel-alive-experimental``.
    canonical = owner == "feel-alive" or (
        owner.startswith("feel-alive-") and owner.removeprefix("feel-alive-").isdigit()
    )
    if not canonical:
        return None, "owner is not the canonical CLI feel-alive behavior"
    try:
        active = ctx.active_names()
    except Exception:  # noqa: BLE001 - absence fails closed
        return None, "active behavior identity is unavailable"
    if not isinstance(active, set) or "feel-alive" not in active:
        return None, "canonical CLI feel-alive is not active"
    return owner, None


def _complete_pose(pose: object) -> dict | None:
    if not isinstance(pose, dict):
        return None
    head = pose.get("head")
    antennas = pose.get("antennas")
    if not isinstance(head, dict) or not isinstance(antennas, (tuple, list)):
        return None
    if len(antennas) != 2 or "body_yaw" not in pose or any(axis not in head for axis in HEAD_AXES):
        return None
    try:
        head_values = {axis: float(head[axis]) for axis in HEAD_AXES}
        right, left = (float(antennas[0]), float(antennas[1]))
        body_yaw = float(pose["body_yaw"])
    except (TypeError, ValueError):
        return None
    values = (*head_values.values(), right, left, body_yaw)
    if not all(math.isfinite(value) for value in values):
        return None
    return {
        "head": head_values,
        "antennas": {"right": right, "left": left},
        "body_yaw": body_yaw,
    }


def _command_vector(command: dict) -> tuple[float, ...]:
    return (
        *(command["head"][axis] for axis in HEAD_AXES),
        command["antennas"]["right"],
        command["antennas"]["left"],
        command["body_yaw"],
    )


class ProbeDriver:
    """A bounded read-only ``TickBus`` driver; one call observes one engine tick."""

    def __init__(
        self,
        mode: str,
        reader: SharedPoseReader,
        *,
        emit: Callable[[dict], None],
        wall_now: Callable[[], float] = time.monotonic,
        max_tick_gap_s: float = MAX_TICK_GAP_S,
    ) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of: {', '.join(MODES)}")
        self._mode = mode
        self._reader = reader
        self._emit = emit
        self._wall_now = wall_now
        self._max_tick_gap_s = max(0.0, float(max_tick_gap_s))
        self._last_tick_timestamp: float | None = None
        self._observation_start: float | None = None
        self._motion_start: float | None = None
        self._last_command: tuple[float, ...] | None = None
        self._stable_since: float | None = None
        self._armed = False
        self._previous: float | None = None
        self._samples = 0
        self._terminal = False

    def __call__(self, ctx) -> None:  # type: ignore[no-untyped-def]
        self._reader.begin_tick(getattr(ctx, "tick", None))
        if self._terminal:
            return
        timestamp = getattr(ctx, "now", None)
        if not isinstance(timestamp, (int, float)) or not math.isfinite(timestamp):
            self._refuse(0.0, "engine tick timestamp is unavailable")
            return
        timestamp = float(timestamp)
        if self._last_tick_timestamp is not None:
            gap = timestamp - self._last_tick_timestamp
            if gap <= 0.0:
                self._refuse(timestamp, "backward or duplicate engine tick timestamp")
                return
            if gap > self._max_tick_gap_s + 1e-9:
                self._refuse(timestamp, f"excessive engine tick gap ({gap:.6f}s)")
                return
        self._last_tick_timestamp = timestamp

        owner, refusal = _feel_alive_owner(ctx)
        if refusal is not None:
            self._refuse(timestamp, refusal)
            return

        command = _complete_pose(getattr(ctx, "pose", None))
        if command is None:
            self._refuse(timestamp, "complete engine command vector is unavailable")
            return
        vector = _command_vector(command)

        if self._observation_start is None:
            self._observation_start = timestamp
            self._last_command = vector
            self._stable_since = float(timestamp)
            self._emit(
                {
                    "schema": SCHEMA,
                    "type": "probe_start",
                    "label": self._mode,
                    "phase": "armed",
                    "cue": CUES[self._mode],
                    "timestamp_s": timestamp,
                    "arm_timeout_s": ARM_TIMEOUT_S,
                    "settled_edge_s": SETTLED_EDGE_S,
                    "owner": owner,
                }
            )
            return

        onset = False
        if self._motion_start is None:
            if timestamp - self._observation_start > ARM_TIMEOUT_S:
                self._refuse(timestamp, "observation timeout waiting for motion onset")
                return
            if not self._armed:
                if vector != self._last_command:
                    self._last_command = vector
                    self._stable_since = timestamp
                    return
                if (
                    self._stable_since is not None
                    and timestamp - self._stable_since >= SETTLED_EDGE_S
                ):
                    self._armed = True
                    self._emit(
                        {
                            "schema": SCHEMA,
                            "type": "probe_armed",
                            "label": self._mode,
                            "phase": "armed",
                            "timestamp_s": timestamp,
                            "owner": owner,
                        }
                    )
                return
            if vector == self._last_command:
                return
            self._motion_start = timestamp
            self._last_command = vector
            self._stable_since = None
            onset = True

        elapsed = timestamp - self._motion_start
        if elapsed > MOTION_TIMEOUT_S:
            self._refuse(timestamp, "motion episode timeout before settled edge")
            return

        if onset or vector != self._last_command:
            self._last_command = vector
            self._stable_since = None
            phase = "excited_motion"
        else:
            if self._stable_since is None:
                self._stable_since = timestamp
            phase = "settled"

        read_started = self._wall_now()
        actual = self._reader.read()
        read_lag = self._wall_now() - read_started
        pitch, yaw = actual if actual is not None else (None, None)
        actual_record = {
            "availability": "available" if actual is not None else "unavailable",
            "pitch": pitch,
            "yaw": yaw,
        }
        self._emit(
            {
                "schema": SCHEMA,
                "type": "sample",
                "label": self._mode,
                "phase": phase,
                "timestamp_s": timestamp,
                "elapsed_s": elapsed,
                "owner": owner,
                "commanded": command,
                "actual": actual_record,
                "timing": {
                    "sample_gap_s": (
                        None if self._previous is None else timestamp - self._previous
                    ),
                    "read_lag_s": read_lag,
                },
            }
        )
        self._previous = timestamp
        self._samples += 1

        if (
            phase == "settled"
            and self._stable_since is not None
            and timestamp - self._stable_since >= SETTLED_EDGE_S
        ):
            self._emit(
                {
                    "schema": SCHEMA,
                    "type": "probe_end",
                    "label": self._mode,
                    "phase": "settled",
                    "timestamp_s": timestamp,
                    "elapsed_s": elapsed,
                    "samples": self._samples,
                }
            )
            self._terminal = True

    def _refuse(self, timestamp: float, reason: str) -> None:
        self._emit(
            {
                "schema": SCHEMA,
                "type": "probe_refused",
                "label": self._mode,
                "phase": "refused",
                "timestamp_s": timestamp,
                "reason": reason,
            }
        )
        self._terminal = True


__all__ = [
    "ARM_TIMEOUT_S",
    "CUES",
    "HEAD_AXES",
    "MAX_TICK_GAP_S",
    "MODES",
    "MOTION_TIMEOUT_S",
    "ProbeDriver",
    "SCHEMA",
    "SETTLED_EDGE_S",
    "SharedPoseReader",
]
