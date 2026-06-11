"""End-to-end integration tests for the ``pat`` mode (t7).

Unlike the unit suites (``test_pat_detector.py``, ``test_pat_reaction.py``,
``test_cli_pat.py``), these tests wire the **real** components together — the
:class:`~reachy.motion.pat.PatDetector`, :class:`~reachy.motion.pat_reaction.PatReaction`,
and a real :class:`~reachy.motion.queue.MotionQueue` — and assert on the actual
queued motion, plus drive the CLI through ``reachy.cli.main([...])`` exactly as
an operator would. No mocks of the logic under test, no real robot, no network,
no sleeps: time is injected explicitly via the detector's ``now=`` seam and the
``level2_threshold_fn`` constructor hook.

Coverage:

1. ``pat demo --json`` (CLI entry) produces an *affectionate* lean/snuggle
   reaction — the structured reaction events list enqueued ``lean`` actions, not
   an empty/error result. Exit 0 with no robot.
2. The detector distinguishes scratch (pitch press) from side-nudge (yaw press)
   END-TO-END: a pitch-press impulse train and a yaw-press impulse train are fed
   through ``PatDetector`` → ``PatReaction`` → a real ``MotionQueue`` and the two
   produce DISTINCT queued action sets (scratch → pitch-down lean; side_pat →
   yaw-toward + body_yaw lean).
3. The stubbed ``sdk`` path: a fake transport whose ``head_pose()`` returns a
   deviating pose drives the bounded ``pat run --ticks N`` loop and a reaction
   fires (queued goto with a pat label reaches the transport).
"""

from __future__ import annotations

import json

import pytest

from reachy.cli import main
from reachy.motion.pat import PatDetector
from reachy.motion.pat_reaction import LEAN_PITCH_DOWN, LEAN_YAW_SIDE, SIDE_BODY_YAW, PatReaction
from reachy.motion.queue import MotionAction, MotionQueue

# ---------------------------------------------------------------------------
# Deterministic impulse helpers (mirror tests/test_pat_detector.py)
# ---------------------------------------------------------------------------


def _drive_pitch_press(detector: PatDetector, start: float, *, n: int = 3) -> list:
    """Feed *n* clean pitch-press impulses with an injected clock; return events."""
    events = []
    for i in range(n):
        t_press = start + i * 0.4
        ev = detector.update(0.0, -5.0, now=t_press)  # pressed (head pushed down)
        if ev is not None:
            events.append(ev)
        ev = detector.update(0.0, 0.0, now=t_press + 0.1)  # released
        if ev is not None:
            events.append(ev)
    return events


def _drive_yaw_press(detector: PatDetector, start: float, *, n: int = 3) -> list:
    """Feed *n* clean yaw-press impulses with an injected clock; return events."""
    events = []
    for i in range(n):
        t_press = start + i * 0.4
        ev = detector.update(0.0, 0.0, 0.0, 5.0, now=t_press)  # nudged sideways
        if ev is not None:
            events.append(ev)
        ev = detector.update(0.0, 0.0, 0.0, 0.0, now=t_press + 0.1)  # released
        if ev is not None:
            events.append(ev)
    return events


# ---------------------------------------------------------------------------
# 1. demo path (CLI) — affectionate lean/snuggle reaction, no robot
# ---------------------------------------------------------------------------


def test_demo_cli_produces_affectionate_lean_reactions(
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``pat demo --json`` (via main) emits structured lean reactions, exit 0, no robot."""
    rc = main(["pat", "demo", "--json"])
    assert rc == 0

    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"

    reactions = payload["reactions"]
    # Not an empty/error result: the scripted demo plays its full event sequence.
    assert isinstance(reactions, list) and len(reactions) >= 1

    for r in reactions:
        assert r["touch_type"] in {"scratch", "side_pat"}
        assert r["level"] in {"level1", "level2"}
        actions = r["actions"]
        assert isinstance(actions, list) and len(actions) >= 1
        # The affectionate reaction leans in: the first action is a lean, and a
        # nuzzle/settle snuggle follows — never an empty set, never an error tremor.
        assert actions[0].endswith("_lean"), actions
        assert any("lean" in a for a in actions)
        assert any("nuzzle" in a for a in actions)
        assert any("settle" in a for a in actions)
        # No error-tremor / error label leaked into the reaction sequence.
        assert not any("error" in a or "tremor" in a for a in actions), actions


# ---------------------------------------------------------------------------
# 2. scratch vs side_pat distinctness — detector → reaction → real queue
# ---------------------------------------------------------------------------


def _labels(actions: list[MotionAction]) -> list[str]:
    return [a.label for a in actions]


def test_scratch_and_side_pat_enqueue_distinct_actions_end_to_end() -> None:
    """A pitch-press and a yaw-press flow through the real wiring into DISTINCT actions.

    Wires ``PatDetector`` → ``PatReaction`` → a real ``MotionQueue`` for each axis
    and asserts the two queued action sets differ in both label and pose: scratch
    leans pitch-DOWN with no body_yaw; side_pat yaws TOWARD with a matching body_yaw.
    """
    # --- scratch: pitch-press impulses ---
    scratch_det = PatDetector(level2_threshold_fn=lambda: 6.0)
    scratch_queue: MotionQueue = MotionQueue()
    scratch_reaction = PatReaction(queue=scratch_queue)

    scratch_events = _drive_pitch_press(scratch_det, start=1000.0, n=3)
    assert scratch_events, "expected at least one scratch detection"
    s_level, s_type = scratch_events[0]
    assert (s_level, s_type) == ("level1", "scratch")
    scratch_reaction.react(s_type, s_level)
    scratch_actions = scratch_queue.pending()

    # --- side_pat: yaw-press impulses ---
    side_det = PatDetector(level2_threshold_fn=lambda: 6.0)
    side_queue: MotionQueue = MotionQueue()
    side_reaction = PatReaction(queue=side_queue)

    side_events = _drive_yaw_press(side_det, start=2000.0, n=3)
    assert side_events, "expected at least one side_pat detection"
    p_level, p_type = side_events[0]
    assert (p_level, p_type) == ("level1", "side_pat")
    side_reaction.react(p_type, p_level)
    side_actions = side_queue.pending()

    # The detector classified the two touches differently end-to-end.
    assert s_type != p_type

    # The queued action *labels* are distinct.
    scratch_labels = _labels(scratch_actions)
    side_labels = _labels(side_actions)
    assert scratch_labels == ["pat_scratch_lean", "pat_scratch_nuzzle", "pat_scratch_settle"]
    assert side_labels == ["pat_side_lean", "pat_side_nuzzle", "pat_side_settle"]
    assert set(scratch_labels).isdisjoint(set(side_labels))

    # The queued *poses* are distinct: scratch = pitch-down lean, no body_yaw.
    scratch_lean = scratch_actions[0]
    assert scratch_lean.head is not None
    assert scratch_lean.head["pitch"] == pytest.approx(-LEAN_PITCH_DOWN)
    assert scratch_lean.head["yaw"] == pytest.approx(0.0)
    assert scratch_lean.body_yaw is None

    # side_pat = yaw-toward lean + matching body_yaw, no pitch.
    side_lean = side_actions[0]
    assert side_lean.head is not None
    assert abs(side_lean.head["yaw"]) == pytest.approx(LEAN_YAW_SIDE)
    assert side_lean.head["pitch"] == pytest.approx(0.0)
    assert side_lean.body_yaw is not None
    assert abs(side_lean.body_yaw) == pytest.approx(SIDE_BODY_YAW)


# ---------------------------------------------------------------------------
# 3. stubbed sdk path — bounded `pat run --ticks N` loop fires a reaction
# ---------------------------------------------------------------------------


class _PatTransport:
    """A fake sdk transport whose head_pose returns a scripted deviating pose.

    Reuses the established ``tests/test_cli_pat.py::_FakeTransport`` seam (the
    noun's ``get_transport`` is monkeypatched to return this and ``move_goto``
    records every command), but scripts the read-back to *alternate* between a
    deep press (head pushed down, far below the commanded pitch) and a release.
    A constant deviation would only register one edge-triggered press; alternating
    yields several distinct presses so the detector clears ``min_presses`` and
    fires a scratch end-to-end inside the bounded loop.
    """

    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []
        self._tick = 0

    def head_pose(self) -> tuple[float, float]:
        self._tick += 1
        pressed = self._tick % 2 == 1  # alternate pressed / released each read
        return (-20.0, 0.0) if pressed else (0.0, 0.0)

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_run_sdk_bounded_loop_fires_reaction(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    """The bounded `pat run` loop over a stubbed sdk transport detects a pat and leans.

    Reuses the exact ``get_transport`` injection seam + ``--ticks`` bound from
    ``tests/test_cli_pat.py``. Enough ticks to clear ``min_presses`` so a
    detection fires; assert exit 0, the reported tick count, and that a pat-labelled
    lean goto actually reached the transport.
    """
    import reachy.cli._commands.pat as pat_mod

    transport = _PatTransport()
    monkeypatch.setattr(pat_mod, "get_transport", lambda args: transport)

    rc = main(["pat", "run", "--transport", "sdk", "--ticks", "8", "--json"])
    assert rc == 0

    out = capsys.readouterr().out
    last = [line for line in out.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["status"] == "ok"
    assert payload["ticks"] == 8
    # A pat was detected during the bounded run (events reported and a lean issued).
    assert payload["events"] >= 1

    # The lean reaction reached the transport: at least one goto carries a pat
    # lean pose (a head dict with a non-zero pitch or yaw beyond the held baseline).
    leaned = [
        g
        for g in transport.gotos
        if isinstance(g.get("head"), dict)
        and (abs(g["head"].get("pitch", 0.0)) > 1.0 or abs(g["head"].get("yaw", 0.0)) > 1.0)
    ]
    assert leaned, "expected at least one lean goto to reach the transport"
