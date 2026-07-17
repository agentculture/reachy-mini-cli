"""Tests for the stash apply adapter (:mod:`reachy.stash.apply`).

Covers task t5's acceptance criteria:

1. A fetched :class:`~reachy.stash.record.StashRecord` is realized via the
   library ``build()`` path (no exec/eval of strings anywhere) and sampled into
   a bounded series of :class:`~reachy.motion.queue.MotionAction` goto actions.
2. End-to-end: stash a record (fake embedder, no network), fetch it via
   :class:`~reachy.stash.store.StashStore` semantic search, hand the resulting
   :class:`~reachy.stash.store.ScoredRecord` to the adapter, and assert it
   enqueues the expected bounded pose sequence on a queue double.
"""

from __future__ import annotations

import math

import pytest

from reachy.behavior.model import neutral_head
from reachy.cli._errors import CliError
from reachy.motion.queue import MotionAction, MotionQueue
from reachy.stash.apply import (
    DEFAULT_INFINITE_DURATION,
    DEFAULT_KEYFRAME_INTERVAL,
    DEFAULT_MAX_KEYFRAMES,
    apply_record,
    plan_keyframes,
)
from reachy.stash.record import StashRecord
from reachy.stash.store import ScoredRecord, StashStore

# ---------------------------------------------------------------------------
# Fixture records
# ---------------------------------------------------------------------------

# "thoughtful" is a one-shot, finite-duration (3.0s) generator whose contribution
# is NOT constant in t (an ease-in ramp) -- ideal for asserting a distinguishable
# first vs. last sampled pose.
_THOUGHTFUL = {
    "name": "pondering-tilt",
    "explanation": "ease into a thoughtful tilted gaze while pondering a sound",
    "generator": "thoughtful",
    "params": {
        "pitch": {"default": 8.0, "unit": "deg", "help": "upward/forward tilt"},
        "yaw": {"default": 10.0, "unit": "deg", "help": "gaze-aside angle"},
        "roll": {"default": 5.0, "unit": "deg", "help": "head roll"},
        "rise": {"default": 0.6, "unit": "s", "help": "ease-in time"},
    },
    "channels": ["head"],
    "stop_class": "stoppable",
    "lifetime": {"looping": False, "duration": 3.0},
}

# "no-shake" reuses the fixture from test_stash_store.py's naming -- a looping,
# infinite-duration (duration=None) generator, for exercising the infinite-lifetime
# cap.
_SHAKE = {
    "name": "no-shake",
    "explanation": "shake the head side to side to say no",
    "generator": "shake",
    "params": {
        "amp": {"default": 15.0, "unit": "deg", "help": "shake amplitude"},
        "period": {"default": 0.7, "unit": "s", "help": "shake cycle"},
    },
    "channels": ["head"],
    "stop_class": "stoppable",
    "lifetime": {"looping": True, "duration": None},
}

# "feel-alive" drives all three channels; used to test that a record's channel
# override RESTRICTS which axes get driven even though the raw contribution would
# otherwise populate them all.
_FEEL_ALIVE_HEAD_ONLY = {
    "name": "quiet-breathe",
    "explanation": "a gentle idle breathing motion, head only",
    "generator": "feel-alive",
    "params": {},
    "channels": ["head"],
    "stop_class": "passive",
    "lifetime": {"looping": True, "duration": None},
}


class _FakeQueue:
    """A minimal MotionQueue double: only the duck-typed ``submit`` the adapter needs."""

    def __init__(self) -> None:
        self.submitted: list[MotionAction] = []

    def submit(self, action: MotionAction) -> None:
        self.submitted.append(action)


class _FakeEmbedder:
    """A deterministic fake embedder keyed by substring (no network, ever)."""

    def __init__(self, table: dict[str, list[float]]) -> None:
        self._table = table

    def __call__(self, text: str) -> list[float]:
        for key, vector in self._table.items():
            if key in text:
                return list(vector)
        return [0.0, 0.0]


def _embedder() -> _FakeEmbedder:
    return _FakeEmbedder(
        {
            "thoughtful gaze": [1.0, 0.0],
            "shake the head": [0.0, 1.0],
        }
    )


# ---------------------------------------------------------------------------
# plan_keyframes -- pure sampling (no queue)
# ---------------------------------------------------------------------------


def test_plan_keyframes_is_bounded_by_max_keyframes() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    assert 2 <= len(actions) <= DEFAULT_MAX_KEYFRAMES


def test_plan_keyframes_first_pose_is_neutral_before_the_ease_in() -> None:
    # thoughtful's ease-in ramp is smoothstep(t / rise); at t=0 the ramp is 0, so
    # the first sampled pose must be the all-zero neutral head.
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    assert actions[0].head == neutral_head()


def test_plan_keyframes_last_pose_is_the_full_amplitude_at_duration_end() -> None:
    # At t=duration=3.0 >> rise=0.6, the ease-in ramp has fully saturated to 1.0,
    # so the last sampled pose is the full pitch/yaw/roll amplitude.
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    last = actions[-1].head
    assert last["yaw"] == pytest.approx(10.0)
    assert last["pitch"] == pytest.approx(8.0)
    assert last["roll"] == pytest.approx(5.0)
    assert last["z"] == 0.0


def test_plan_keyframes_durations_are_all_positive() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    assert all(a.duration > 0.0 for a in actions)


def test_plan_keyframes_spacing_covers_the_effective_duration_exactly() -> None:
    # Every gap is the same "step"; (n - 1) gaps span exactly the effective
    # duration end-to-end (the n-th keyframe's own duration is the ease-in move
    # from wherever the robot currently is into the first sampled pose, so it is
    # not itself one of the (n - 1) inter-sample gaps).
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    step = actions[0].duration
    assert all(a.duration == pytest.approx(step) for a in actions)
    assert (len(actions) - 1) * step == pytest.approx(3.0)


def test_plan_keyframes_uses_minjerk_interpolation_and_no_coalesce_key() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    for a in actions:
        assert a.interpolation == "minjerk"
        # Keyframes form one ordered sequence -- a later one must never evict an
        # earlier, not-yet-executed keyframe, so none may carry a coalesce key.
        assert a.coalesce_key is None


def test_plan_keyframes_only_drives_the_channels_the_generator_claims() -> None:
    # "thoughtful" only ever contributes to the head channel.
    record = StashRecord.from_dict(_THOUGHTFUL)
    actions = plan_keyframes(record)
    for a in actions:
        assert a.antennas is None
        assert a.body_yaw is None
        assert a.head is not None


def test_plan_keyframes_infinite_lifetime_is_capped_at_default_duration() -> None:
    record = StashRecord.from_dict(_SHAKE)  # looping, duration=None
    actions = plan_keyframes(record)
    assert 2 <= len(actions) <= DEFAULT_MAX_KEYFRAMES
    step = actions[0].duration
    assert (len(actions) - 1) * step == pytest.approx(DEFAULT_INFINITE_DURATION)


def test_plan_keyframes_channel_override_restricts_driven_axes() -> None:
    # feel-alive's raw contribution always populates head + antennas + body_yaw,
    # but the record restricts channels to head-only -- the adapter must honour
    # that restriction (mirrors Engine.add's channels override), NOT the raw
    # contribution's full set.
    record = StashRecord.from_dict(_FEEL_ALIVE_HEAD_ONLY)
    actions = plan_keyframes(record)
    assert len(actions) >= 2
    for a in actions:
        assert a.antennas is None
        assert a.body_yaw is None


def test_plan_keyframes_respects_a_smaller_max_keyframes_cap() -> None:
    record = StashRecord.from_dict(_SHAKE)
    actions = plan_keyframes(record, max_keyframes=3)
    assert len(actions) == 3
    step = actions[0].duration
    assert (len(actions) - 1) * step == pytest.approx(DEFAULT_INFINITE_DURATION)


def test_plan_keyframes_respects_a_wider_keyframe_interval() -> None:
    # A coarser interval on the same finite duration yields fewer keyframes.
    record = StashRecord.from_dict(_THOUGHTFUL)
    coarse = plan_keyframes(record, keyframe_interval=2.0)
    fine = plan_keyframes(record, keyframe_interval=0.25)
    assert len(coarse) < len(fine)


def test_plan_keyframes_accepts_a_scored_record_directly() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    scored = ScoredRecord(record=record, score=0.9)
    actions_from_scored = plan_keyframes(scored)
    actions_from_plain = plan_keyframes(record)
    assert [a.head for a in actions_from_scored] == [a.head for a in actions_from_plain]


def test_plan_keyframes_rejects_a_non_positive_keyframe_interval() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    with pytest.raises(CliError):
        plan_keyframes(record, keyframe_interval=0.0)
    with pytest.raises(CliError):
        plan_keyframes(record, keyframe_interval=-1.0)


def test_plan_keyframes_rejects_too_small_a_max_keyframes() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    with pytest.raises(CliError):
        plan_keyframes(record, max_keyframes=1)


def test_plan_keyframes_rejects_a_non_record_input() -> None:
    with pytest.raises(CliError):
        plan_keyframes({"not": "a record"})  # type: ignore[arg-type]


def test_default_keyframe_interval_is_in_the_half_to_one_second_design_range() -> None:
    assert 0.5 <= DEFAULT_KEYFRAME_INTERVAL <= 1.0


# ---------------------------------------------------------------------------
# apply_record -- enqueues the planned actions onto a queue, in order
# ---------------------------------------------------------------------------


def test_apply_record_enqueues_the_planned_actions_in_order_onto_a_fake_queue() -> None:
    record = StashRecord.from_dict(_THOUGHTFUL)
    queue = _FakeQueue()
    returned = apply_record(record, queue)
    assert queue.submitted == returned
    assert 2 <= len(queue.submitted) <= DEFAULT_MAX_KEYFRAMES
    assert queue.submitted[0].head == neutral_head()
    assert queue.submitted[-1].head["yaw"] == pytest.approx(10.0)
    assert all(a.duration > 0.0 for a in queue.submitted)


def test_apply_record_works_against_a_real_motion_queue() -> None:
    # Sanity: the adapter's only requirement of "queue" is a duck-typed submit(),
    # which the real MotionQueue satisfies -- and since every keyframe carries
    # coalesce_key=None, none of them evict each other.
    record = StashRecord.from_dict(_THOUGHTFUL)
    queue = MotionQueue()
    actions = apply_record(record, queue)
    assert queue.pending() == actions


def test_apply_record_rejects_a_non_record_input() -> None:
    queue = _FakeQueue()
    with pytest.raises(CliError):
        apply_record(object(), queue)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# End-to-end -- stash a record, fetch it by semantic search, apply it
# ---------------------------------------------------------------------------


def test_end_to_end_stash_fetch_and_apply(tmp_path) -> None:
    embed = _embedder()
    store = StashStore(path=tmp_path / "stash" / "index.json", embed=embed)
    store.add(StashRecord.from_dict(_THOUGHTFUL))
    store.add(StashRecord.from_dict(_SHAKE))

    # A semantically related query should surface the thoughtful-gaze record first
    # (the fake embedder makes this deterministic by keying on the "thoughtful
    # gaze" substring -- see test_stash_store.py for the same pattern).
    results = store.search("please give me a thoughtful gaze", k=1)
    assert len(results) == 1
    hit = results[0]
    assert isinstance(hit, ScoredRecord)
    assert hit.record.name == "pondering-tilt"

    queue = _FakeQueue()
    actions = apply_record(hit, queue)

    assert queue.submitted == actions
    assert 2 <= len(actions) <= DEFAULT_MAX_KEYFRAMES
    assert actions[0].head == neutral_head()
    last = actions[-1].head
    assert last["yaw"] == pytest.approx(10.0)
    assert last["pitch"] == pytest.approx(8.0)
    assert last["roll"] == pytest.approx(5.0)
    assert all(a.duration > 0.0 for a in actions)
    assert all(a.antennas is None and a.body_yaw is None for a in actions)
    step = actions[0].duration
    assert math.isclose((len(actions) - 1) * step, 3.0)


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
