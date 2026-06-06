"""Serial robot-motion subsystem — a queue + one executor, so moves never conflict.

Expressive robot motion (orient toward a sound, a nod, wake/sleep) is driven by the
daemon's interpolated ``goto`` planner, which produces smooth motor trajectories — far
smoother on real hardware than streaming immediate ``set_target`` poses at 50 Hz (which
the motors track jerkily, and which jitters over HTTP). The catch: two interpolated
moves must never run at once (a second ``goto`` interrupts and "resets" the first).

This package serializes motion. Producers submit a :class:`~reachy.motion.queue.MotionAction`
to a :class:`~reachy.motion.queue.MotionQueue`; a single executor
(:func:`~reachy.motion.server.run`) runs them one at a time via the transport's ``goto``,
each finishing before the next starts — so moves can never overlap or conflict. Reactive
producers submit *coalescing* actions (a newer one replaces a still-pending older one of
the same kind), so the robot always heads to the latest intent without a stale backlog.
"""
