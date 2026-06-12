"""Pat-wake source — detect a head pat *while the sleep-breathe motion moves*.

The ``pat`` noun (:mod:`reachy.cli._commands.pat`) detects a pat by holding a
**static** baseline head pose and feeding the commanded-vs-actual deviation to a
:class:`~reachy.motion.pat.PatDetector`. That works because, while sensing, the
``pat`` loop deliberately keeps the commanded pose pinned at neutral.

In *sleep* there is no such pin: while ASLEEP the robot runs the slow
sleep-breathe motion, so the **commanded** head pose is constantly **moving**. A
static-baseline comparison would read the robot's own breathing as a stream of
presses and wake spuriously. So this source measures the read-back deviation
against the **current** commanded sleep pose at each tick — supplied by an
injected provider — rather than a fixed baseline.

It does *not* reimplement detection: it reuses :class:`PatDetector` exactly,
calling its :meth:`~reachy.motion.pat.PatDetector.update` with the moving
commanded pitch/yaw and the read-back actual pitch/yaw. When the actual pose
tracks the moving commanded pose exactly (zero deviation), the detector never
fires — only a genuine press *relative to where the head was told to be* counts.

Determinism seams (all injected — no robot, no SDK import at module top level):

* ``read_head_pose`` — a zero-arg callable returning the *actual* head pose as
  ``(pitch_deg, yaw_deg)`` (in production: ``transport.head_pose``).
* ``commanded_pose`` — a zero-arg callable returning the *current commanded*
  sleep pose as ``(pitch_deg, yaw_deg)`` (in production t4: the SleepProducer's
  current commanded head pose this tick).
* ``now`` — passed into :meth:`poll`; forwarded to ``PatDetector.update(now=…)``.

This module is importable without the ``[sdk]`` extra: the read-back arrives as a
plain callable, so there is no hard ``reachy_mini`` import here.
"""

from __future__ import annotations

from collections.abc import Callable

from reachy.motion.pat import PatDetector

#: A zero-arg pose provider returning ``(pitch_deg, yaw_deg)``.
PoseProvider = Callable[[], "tuple[float, float]"]


class PatWakeSource:
    """Feed a :class:`PatDetector` from a read-back vs the *moving* commanded pose.

    On each :meth:`poll`, reads the actual head pose, asks the commanded-pose
    provider for the head pose the robot was told to hold *this tick* (which moves
    with the sleep-breathe motion), and feeds the commanded-vs-actual deviation to
    the reused detector. Returns whether a pat fired this tick.

    Parameters
    ----------
    read_head_pose:
        Zero-arg callable returning the *actual* head pose as
        ``(pitch_deg, yaw_deg)``. In production this is ``transport.head_pose``
        (an SDK-only read-back); in tests, a fake.
    commanded_pose:
        Zero-arg callable returning the *current commanded* head pose as
        ``(pitch_deg, yaw_deg)`` — the MOVING sleep-breathe target this tick.
    detector:
        The :class:`PatDetector` to drive. Defaults to a fresh ``PatDetector()``
        with library defaults. Reused, never reimplemented.
    """

    def __init__(
        self,
        *,
        read_head_pose: PoseProvider,
        commanded_pose: PoseProvider,
        detector: PatDetector | None = None,
    ) -> None:
        self._read_head_pose = read_head_pose
        self._commanded_pose = commanded_pose
        self.detector = detector if detector is not None else PatDetector()
        #: The last detection event ``(level, touch_type)``, or ``None``.
        self.last_event: tuple[str, str] | None = None

    def poll(self, *, now: float | None = None) -> bool:
        """Read one proprioceptive sample and feed the detector.

        Reads the actual head pose and the *current* commanded sleep pose, then
        feeds the commanded-vs-actual deviation to :meth:`PatDetector.update`. The
        deviation is taken against the **moving** commanded pose — so a read-back
        that tracks the commanded pose exactly (zero deviation) does not fire.

        Parameters
        ----------
        now:
            Current time in seconds (monotonic). Forwarded to the detector for
            deterministic tests; omit in production to use the detector's own
            ``time.monotonic()`` default.

        Returns
        -------
        bool
            ``True`` if a pat fired this tick (a wake stimulus), else ``False``.
        """
        actual_pitch, actual_yaw = self._read_head_pose()
        commanded_pitch, commanded_yaw = self._commanded_pose()
        event = self.detector.update(
            commanded_pitch,
            actual_pitch,
            commanded_yaw,
            actual_yaw,
            now=now,
        )
        self.last_event = event
        return event is not None

    def __call__(self, *, now: float | None = None) -> bool:
        """Alias for :meth:`poll` so the source is usable as a plain callable."""
        return self.poll(now=now)

    def reset(self) -> None:
        """Reset the underlying detector (e.g. after a wake)."""
        self.detector.reset()
        self.last_event = None
