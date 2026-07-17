"""The stash apply adapter — turn a fetched :class:`StashRecord` into motion.

This is the bridge from the *declarative* stash world (a record naming a
:data:`reachy.behavior.library.LIBRARY` generator + typed params, no code) onto
the live loop's **serial** :class:`~reachy.motion.queue.MotionQueue` ``goto``
path — the same path :class:`reachy.motion.expression.ExpressionProducer` drives
for ``think``'s expression markers. It deliberately does **not** run the record
through the 50 Hz :mod:`reachy.behavior.engine` process: a fetched stash record is
adapted into a short, bounded gesture on the one shared motion queue, not a new
long-lived behavior-engine loop.

The only callables involved are the vetted, in-repo
:data:`reachy.behavior.library.LIBRARY` generator functions — resolved by name via
:func:`reachy.behavior.library.build`. There is no ``exec``/``eval`` of any string
anywhere in this module, and none is possible: a :class:`StashRecord` is pure
declarative data (see :mod:`reachy.stash.record`), so the *only* source of a live
callable is the library registry itself.

Sampling design
----------------
A behavior's contribution function is *continuous* — it can be sampled at any
``t_local``. To turn it into a **bounded** sequence of
:class:`~reachy.motion.queue.MotionAction` goto keyframes:

1. Resolve the record's effective duration (:func:`_effective_duration`): the
   record's own finite ``lifetime.duration`` when set, else
   :data:`DEFAULT_INFINITE_DURATION` seconds — a sensible bounded preview window
   for a looping-forever behavior (``lifetime.duration is None``), since an
   adapted stash gesture is a short one-off move, never an unbounded loop.
2. Compute an evenly-spaced keyframe count from the target spacing
   (:data:`DEFAULT_KEYFRAME_INTERVAL`, in the 0.5-1s "one keyframe every so
   often" range) and the effective duration, then **cap** it at
   :data:`DEFAULT_MAX_KEYFRAMES` (:func:`_keyframe_times`) — this is the hard
   bound: no adapted record ever enqueues more than ``max_keyframes`` actions,
   regardless of how long its lifetime claims to run.
3. Re-space the (possibly capped) keyframe count *evenly* across
   ``[0, effective_duration]`` rather than walking fixed-size steps and
   dropping the remainder — so the **last** keyframe always lands exactly at
   the effective duration (the gesture always finishes where it means to,
   never mid-stride) and every inter-keyframe gap is identical and strictly
   positive (the ``MotionAction.duration`` the executor plays each leg over).
4. Sample the built :class:`~reachy.behavior.model.Behavior`'s pure ``fn`` at
   each keyframe time with :data:`reachy.behavior.sense.EMPTY_SENSE` — a
   stashed record plays back the same way every time, independent of live
   sensing, even for a ``wants_sense=True`` generator (there is no live
   :class:`~reachy.behavior.sense.Sense` reader on this path).
5. Map each :class:`~reachy.behavior.model.Contribution` onto the
   :class:`~reachy.motion.queue.MotionAction` fields, **skipping** any channel
   the behavior does not (or no longer) claim, or that the contribution
   abstains from this instant (leaves ``None``) — the same abstention
   semantics :mod:`reachy.behavior.engine` uses, just resolved once per
   keyframe instead of by per-tick arbitration (there is no contention here:
   an adapted record is the only claimant of its own gesture).

Each keyframe action carries ``coalesce_key=None`` — the keyframes form one
ordered sequence that must all play in order (unlike a reactive coalescing
key, a later keyframe must never evict an earlier, not-yet-executed one).
"""

from __future__ import annotations

import dataclasses

from reachy.behavior import library
from reachy.behavior.model import CHANNELS, Behavior, Contribution, Lifetime
from reachy.behavior.sense import EMPTY_SENSE
from reachy.cli._errors import EXIT_USER_ERROR, CliError
from reachy.motion.queue import MotionAction, MotionQueue
from reachy.stash.record import StashRecord
from reachy.stash.store import ScoredRecord

#: Target spacing between sampled keyframes, seconds — the "every 0.5-1s" design
#: point from the task's sampling notes. The actual per-gap duration may differ
#: slightly once the (capped) keyframe count is re-spaced evenly across the
#: effective duration (see :func:`_keyframe_times`).
DEFAULT_KEYFRAME_INTERVAL: float = 0.75

#: Hard cap on the number of MotionAction keyframes one apply_record() call ever
#: enqueues, regardless of the record's lifetime — keeps a single stash apply a
#: bounded, short gesture rather than an unbounded backlog on the motion queue.
DEFAULT_MAX_KEYFRAMES: int = 8

#: Bounded preview duration (seconds) substituted for a looping-forever record
#: (``lifetime.duration is None``) — an adapted stash gesture is always a short,
#: finite move on the goto path, never an unbounded loop.
DEFAULT_INFINITE_DURATION: float = 4.0

#: The standard smooth interpolation the executor expects (matches
#: ``ExpressionProducer`` and every other MotionAction producer in this repo).
_INTERPOLATION: str = "minjerk"


def _as_record(record: StashRecord | ScoredRecord) -> StashRecord:
    """Unwrap a :class:`ScoredRecord` (a search hit) to its plain :class:`StashRecord`."""
    if isinstance(record, ScoredRecord):
        return record.record
    if isinstance(record, StashRecord):
        return record
    raise CliError(
        code=EXIT_USER_ERROR,
        message=f"expected a StashRecord or ScoredRecord (got {type(record).__name__!r})",
        remediation="pass a reachy.stash.record.StashRecord or a stash.store.ScoredRecord",
    )


def _effective_duration(lifetime: dict) -> float:
    """The bounded duration to sample across: the record's own, or the infinite default."""
    duration = lifetime.get("duration")
    return float(duration) if duration is not None else DEFAULT_INFINITE_DURATION


def _keyframe_times(
    total_duration: float, keyframe_interval: float, max_keyframes: int
) -> list[float]:
    """The evenly-spaced sample times in ``[0, total_duration]`` (always >= 2, <= max_keyframes).

    The *count* is derived from ``total_duration / keyframe_interval`` (so a longer
    gesture gets more keyframes, up to the cap) but the times themselves are then
    re-spaced evenly across the full span, so the last keyframe always lands
    exactly on ``total_duration`` and every gap is identical.
    """
    if total_duration <= 0.0:
        return [0.0, 0.0]
    raw_count = int(total_duration // keyframe_interval) + 1
    count = max(2, min(raw_count, max_keyframes))
    step = total_duration / (count - 1)
    return [i * step for i in range(count)]


def _resolve_behavior(record: StashRecord) -> Behavior:
    """Realize the record via the vetted library ``build()`` path — no exec/eval anywhere.

    The only callable ever wired into the returned :class:`Behavior` is
    ``LIBRARY[record.generator].fn`` (or ``make_fn()``'s result) — the record itself
    supplies no code, only the name and typed params :func:`resolve_params` merges
    onto the library entry's defaults.
    """
    entry = library.get(record.generator)
    params = library.resolve_params(
        entry, {name: str(p.default) for name, p in record.params.items()}
    )
    stop_class = library.resolve_class(entry, record.stop_class)
    lifetime = Lifetime(
        looping=record.lifetime.get("looping", entry.looping),
        duration=record.lifetime.get("duration"),
    )
    behavior = library.build(
        record.generator,
        params,
        stop_class,
        lifetime,
        f"stash-{record.name}",
    )
    if record.channels:
        behavior = dataclasses.replace(behavior, channels=frozenset(record.channels))
    return behavior


def _pose_for(behavior: Behavior, contribution: Contribution) -> dict[str, object]:
    """Map one :class:`Contribution` onto MotionAction-shaped fields.

    A channel is driven only when the behavior still claims it (``channel in
    behavior.channels`` — honours a record's channel-restricting override the same
    way :meth:`reachy.behavior.engine.Engine.add` does) **and** the contribution
    does not abstain from it this instant (``channel(name) is not None``) — the
    same abstention semantics the 50 Hz engine's arbitration uses.
    """
    pose: dict[str, object] = {}
    for channel in CHANNELS:
        value = contribution.channel(channel)
        if channel in behavior.channels and value is not None:
            pose[channel] = dict(value) if isinstance(value, dict) else value
        else:
            pose[channel] = None
    return pose


def plan_keyframes(
    record: StashRecord | ScoredRecord,
    *,
    keyframe_interval: float = DEFAULT_KEYFRAME_INTERVAL,
    max_keyframes: int = DEFAULT_MAX_KEYFRAMES,
) -> list[MotionAction]:
    """Sample a fetched stash record into a bounded, ordered list of MotionActions.

    Pure — builds no queue, performs no I/O. Accepts either a plain
    :class:`StashRecord` or a :class:`~reachy.stash.store.ScoredRecord` (a
    ``StashStore.search`` hit), so a caller can hand a search result straight in.
    See the module docstring for the sampling design (keyframe count, spacing,
    infinite-lifetime handling, channel/abstention skipping).
    """
    if keyframe_interval <= 0.0:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"keyframe_interval must be > 0 (got {keyframe_interval!r})",
            remediation="pass a positive number of seconds, e.g. keyframe_interval=0.75",
        )
    if max_keyframes < 2:
        raise CliError(
            code=EXIT_USER_ERROR,
            message=f"max_keyframes must be >= 2 (got {max_keyframes!r})",
            remediation="a gesture needs at least a start and an end keyframe",
        )

    stash_record = _as_record(record)
    behavior = _resolve_behavior(stash_record)
    total_duration = _effective_duration(stash_record.lifetime)
    times = _keyframe_times(total_duration, keyframe_interval, max_keyframes)
    step = times[1] - times[0] if len(times) > 1 else keyframe_interval

    actions: list[MotionAction] = []
    for i, t in enumerate(times):
        contribution = behavior.contribution(t, EMPTY_SENSE)
        pose = _pose_for(behavior, contribution)
        actions.append(
            MotionAction(
                label=f"stash {stash_record.name} [{i + 1}/{len(times)}]",
                head=pose["head"],
                antennas=pose["antennas"],
                body_yaw=pose["body_yaw"],
                duration=step,
                interpolation=_INTERPOLATION,
                coalesce_key=None,
            )
        )
    return actions


def apply_record(
    record: StashRecord | ScoredRecord,
    queue: MotionQueue,
    *,
    keyframe_interval: float = DEFAULT_KEYFRAME_INTERVAL,
    max_keyframes: int = DEFAULT_MAX_KEYFRAMES,
) -> list[MotionAction]:
    """Plan *record* into keyframes and submit each, in order, onto *queue*.

    ``queue`` needs only a ``submit(action)`` method (the shape
    :class:`~reachy.motion.queue.MotionQueue` and any test double both provide).
    Returns the enqueued actions (the same list :func:`plan_keyframes` produced),
    so a caller/test can inspect exactly what was submitted.
    """
    actions = plan_keyframes(
        record, keyframe_interval=keyframe_interval, max_keyframes=max_keyframes
    )
    for action in actions:
        queue.submit(action)
    return actions
