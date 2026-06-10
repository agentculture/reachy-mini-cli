"""The pure idle-motion pose generator shared by ``demo-mode`` and ``listen``.

A gentle "alive" pose — a slow breathing oscillation, an occasional glance to a
new gaze target, and a little antenna sway — so a robot that is otherwise idle
looks quietly present rather than frozen. This module is **pure** (only ``math``
and ``random``): no transport, no clock, no ``reachy.cli`` imports, so it can be
shared by :mod:`reachy.alive` (the ``demo-mode`` loop) and
:mod:`reachy.motion.listen` (the always-alive idle layer of ``listen``) without
any import cycle.
"""

from __future__ import annotations

import math
import random
from dataclasses import dataclass


@dataclass
class AliveConfig:
    """Tunables for the idle "alive" motion.

    Amplitudes are in the CLI's friendly units (millimetres / degrees) and are
    all scaled by ``energy`` (a single 0..n liveliness knob). ``interval`` sets
    the tempo (seconds between poses); each ``goto`` is given a duration just
    under ``interval`` so motion glides continuously rather than stepping.
    """

    interval: float = 2.5
    energy: float = 1.0
    breathe_period: float = 5.0
    breathe_z_mm: float = 3.0
    breathe_pitch_deg: float = 2.0
    gaze_yaw_deg: float = 18.0
    gaze_pitch_deg: float = 10.0
    gaze_roll_deg: float = 4.0
    antenna_deg: float = 18.0
    body_yaw_deg: float = 8.0
    glance_probability: float = 0.5
    interpolation: str = "minjerk"
    seed: int | None = None
    # Give up the loop after this many consecutive failed gotos (daemon gone).
    max_errors: int = 5

    def focused(self) -> "AliveConfig":
        """Return a low-energy "focused" variant of this config.

        Stillness is the thinking posture: while the ``think`` cognition loop is
        running, the always-alive idle motion should QUIET DOWN rather than stop.
        The robot keeps *breathing* — vertical/pitch oscillation stays present,
        just smaller — but the ambient gaze / antenna / body wander backs off so
        the body looks calm and focused rather than restlessly looking around.

        Relative to the base config this:

        * keeps a (reduced) breathe — ``breathe_z_mm`` / ``breathe_pitch_deg``
          are scaled down, never zeroed, so it still visibly breathes;
        * sharply cuts the wander amplitudes (gaze yaw/pitch/roll, antenna sway,
          body yaw);
        * lowers the overall ``energy`` multiplier and ``glance_probability`` so
          large reorienting glances become rare.

        All other tunables (``interval``, ``breathe_period``, ``interpolation``,
        ``seed``, ``max_errors``) are preserved so pacing and bookkeeping are
        unchanged — only the *amount* of motion drops.
        """
        from dataclasses import replace

        return replace(
            self,
            energy=self.energy * 0.35,
            breathe_z_mm=self.breathe_z_mm * 0.5,
            breathe_pitch_deg=self.breathe_pitch_deg * 0.5,
            gaze_yaw_deg=self.gaze_yaw_deg * 0.15,
            gaze_pitch_deg=self.gaze_pitch_deg * 0.15,
            gaze_roll_deg=self.gaze_roll_deg * 0.15,
            antenna_deg=self.antenna_deg * 0.2,
            body_yaw_deg=self.body_yaw_deg * 0.15,
            glance_probability=self.glance_probability * 0.2,
        )


def neutral_pose(config: AliveConfig) -> dict[str, object]:
    """The centred rest pose demo-mode settles to when it stops."""
    return {
        "head": {"x": 0.0, "y": 0.0, "z": 0.0, "roll": 0.0, "pitch": 0.0, "yaw": 0.0},
        "antennas": (0.0, 0.0),
        "body_yaw": 0.0,
        "duration": max(0.5, config.interval),
        "interpolation": config.interpolation,
    }


def next_pose(elapsed: float, rng: random.Random, config: AliveConfig) -> dict[str, object]:
    """Compute the next idle pose at time ``elapsed`` seconds into the loop.

    Pure and deterministic given ``elapsed`` and ``rng``: breathing is a function
    of ``elapsed`` (continuous), the glance target is drawn from ``rng``. The
    result maps straight onto :meth:`Transport.move_goto` keyword arguments.
    """
    e = max(0.0, config.energy)
    phase = 2.0 * math.pi * (elapsed / config.breathe_period) if config.breathe_period else 0.0

    # Breathing: a slow vertical + pitch oscillation, always present.
    z = config.breathe_z_mm * e * math.sin(phase)
    breathe_pitch = config.breathe_pitch_deg * e * math.sin(phase)

    # Gaze: now and then look somewhere new; otherwise just micro-drift near centre.
    if rng.random() < config.glance_probability:
        scale = 1.0
        body_yaw = rng.uniform(-config.body_yaw_deg, config.body_yaw_deg) * e
    else:
        scale = 0.2
        body_yaw = 0.0
    yaw = rng.uniform(-config.gaze_yaw_deg, config.gaze_yaw_deg) * e * scale
    gaze_pitch = rng.uniform(-config.gaze_pitch_deg, config.gaze_pitch_deg) * e * scale
    roll = rng.uniform(-config.gaze_roll_deg, config.gaze_roll_deg) * e * scale

    # Antennas: a gentle sway plus a touch of independent jitter.
    sway = config.antenna_deg * e * math.sin(phase * 1.5)
    jitter = rng.uniform(-1.0, 1.0) * config.antenna_deg * 0.3 * e
    right = sway + jitter
    left = -sway + jitter

    return {
        "head": {
            "x": 0.0,
            "y": 0.0,
            "z": z,
            "roll": roll,
            "pitch": breathe_pitch + gaze_pitch,
            "yaw": yaw,
        },
        "antennas": (right, left),
        "body_yaw": body_yaw,
        "duration": max(0.2, config.interval * 0.9),
        "interpolation": config.interpolation,
    }
