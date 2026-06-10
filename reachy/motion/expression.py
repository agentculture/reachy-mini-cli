"""The ``expression`` producer: one calm gesture per LLM expression marker.

This is the motion-integration core for ``think``'s expressive movement.  While
the robot is thinking, **stillness is the posture** — so the producer emits a
move *only* on an expression marker (``*🤔*`` in the LLM stream), never per
sentence, and each move is calm and low-amplitude so the rare gesture stands out
against the surrounding stillness.

It deliberately reuses the **existing** serial goto/minjerk motion path:

* the catalog pose (a :class:`~reachy.speech.expressions.ExpressionPose`, in the
  CLI's friendly mm/deg units) is mapped 1-to-1 onto a single
  :class:`~reachy.motion.queue.MotionAction`, and
* that action is submitted to the same :class:`~reachy.motion.queue.MotionQueue`
  the ``listen`` executor already drains one move at a time.

There is **no** new motion channel and **no** direct ``transport.move_*`` call —
the producer only builds and enqueues actions; the existing
:func:`reachy.motion.server.run` executor does the I/O.

Coalesce key — :data:`~reachy.motion.queue.EXPRESSION_KEY`
---------------------------------------------------------
Expression gestures carry :data:`~reachy.motion.queue.EXPRESSION_KEY`, which:

* **supersedes a pending idle pose** (``IDLE_KEY``) — a deliberate expression
  always wins over background idle motion, the same way a reactive look/lean does;
  the gesture is the thinking robot's deliberate "tell".
* **coalesces with itself** — a burst of markers queued before the executor
  drains collapses to the *latest* pending expression move, which keeps motion
  sparse (≤ one expression move per marker) without a stale backlog.
* is **independent of** ``LOOK_KEY`` / ``ANTENNA_KEY`` — an expression is not a
  reactive look-at turn, so a committed turn or lean neither evicts a pending
  expression nor is evicted by one; they queue alongside in order.

Amplitude
---------
The catalog poses are already low-amplitude (see the amplitude guide in
``expressions.toml``); the producer uses them **as-is** — it never scales them
up.  The only timing knob is :data:`EXPRESSION_DURATION` (a calm default,
overridable per producer), on the same gentle scale as ``listen``'s ``min_dur``.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable, Optional

from reachy.motion.queue import EXPRESSION_KEY, MotionAction, MotionQueue
from reachy.speech.expressions import Catalog, ExpressionPose
from reachy.speech.markers import Event, MarkerEvent

#: Calm default duration (seconds) for an expression gesture — deliberate, not
#: snappy, on the same gentle scale as ``listen``'s ``min_dur`` (1.5 s).
EXPRESSION_DURATION: float = 1.5

#: Interpolation mode — the standard smooth minjerk goto the executor expects.
_INTERPOLATION: str = "minjerk"


@dataclass
class ExpressionProducer:
    """Map LLM expression markers to sparse, calm gestures on the motion queue.

    Construct with the target :class:`~reachy.motion.queue.MotionQueue` the
    executor drains; optionally inject a :class:`~reachy.speech.expressions.Catalog`
    (default: the bundled ``expressions.toml``) and override the gesture
    ``duration``.

    Usage (how ``think``'s cognition loop drives it)::

        producer = ExpressionProducer(queue=motion_queue)
        for event in marker_parser.feed(chunk):
            producer.on_marker(event)   # ignores SpeechEvent, gestures on MarkerEvent

    or, draining a whole list of parsed events at once::

        producer.consume(events)        # one move per MarkerEvent, speech ignored
    """

    queue: MotionQueue
    catalog: Catalog = field(default_factory=Catalog)
    duration: float = EXPRESSION_DURATION

    def _action_for(self, emoji: str) -> MotionAction:
        """Build the single calm :class:`MotionAction` for *emoji* (unknown → neutral).

        The catalog pose is mapped 1-to-1 onto the action's friendly-unit fields
        (``head`` mm/deg dict, ``antennas`` ``(right, left)`` deg tuple, ``body_yaw``
        deg scalar) **verbatim** — never amplified — with the calm default duration
        and the standard minjerk interpolation.
        """
        pose: ExpressionPose = self.catalog.get(emoji)
        return MotionAction(
            label=f"express {emoji}",
            head=pose.as_head_dict(),
            antennas=pose.as_antennas_tuple(),
            body_yaw=pose.body_yaw,
            duration=self.duration,
            interpolation=_INTERPOLATION,
            coalesce_key=EXPRESSION_KEY,
        )

    def express(self, emoji: str) -> MotionAction:
        """Enqueue **exactly one** calm gesture for *emoji* and return it.

        Builds the catalog-pose action (unknown emoji → the neutral fallback) and
        submits it to the motion queue under :data:`EXPRESSION_KEY`.  Returns the
        enqueued action so callers can inspect it (e.g. for logging / tests).
        """
        action = self._action_for(emoji)
        self.queue.submit(action)
        return action

    def on_marker(self, event: MarkerEvent) -> MotionAction:
        """Gesture for a :class:`~reachy.speech.markers.MarkerEvent` (alias of :meth:`express`)."""
        return self.express(event.emoji)

    def consume(self, events: Iterable[Event]) -> int:
        """Enqueue one move per :class:`MarkerEvent`; ignore every :class:`SpeechEvent`.

        This is the sparse driver: speech produces **no** motion, so a marked
        stream with ``N`` expression markers enqueues at most ``N`` expression
        moves — never one per sentence.  Returns the number of moves enqueued
        (i.e. the number of markers seen).
        """
        moves = 0
        for event in events:
            if isinstance(event, MarkerEvent):
                self.express(event.emoji)
                moves += 1
            # SpeechEvent (and anything else) is silently ignored — stillness.
        return moves


def build(
    queue: MotionQueue,
    *,
    catalog: Optional[Catalog] = None,
    duration: float = EXPRESSION_DURATION,
) -> ExpressionProducer:
    """Convenience constructor mirroring the other motion producers' factory style."""
    return ExpressionProducer(
        queue=queue,
        catalog=catalog if catalog is not None else Catalog(),
        duration=duration,
    )
