"""Task t3 (plan ``no-freeze-pat-sense``) — pin the sibling pat consumers untouched.

An upcoming task in this plan changes ``reachy/behavior/pat_sense.py``'s
stillness gating (``_commanded_still`` / ``_stillness_open`` /
``_rearm_stillness_hold`` / ``DEFAULT_STILL_EPS`` / ``DEFAULT_STILL_HOLD_S``).
Two sibling pat consumers already sense through motion today, by a completely
DIFFERENT mechanism — expected/current-pose tracking, not a stillness gate —
and this module is the regression guard proving that stays true and that
neither module is coupled to ``pat_sense`` in any way a later task in this
plan could regress:

* :class:`reachy.motion.listen_pat.PatHook` — feeds the detector the
  commanded pose ``listen`` actually dispatched this tick (optionally the
  minjerk-interpolated pose of an in-flight move) as the baseline, so a hand
  reads as external force even while that baseline keeps changing tick to
  tick.
* :class:`reachy.sleep.patwake.PatWakeSource` — feeds the detector the
  *current* (moving) sleep-breathe commanded pose each tick, via an injected
  ``commanded_pose`` provider.

Both simply reuse :class:`reachy.motion.pat.PatDetector` as-is; neither one
has a concept of "commanded pose held still for N seconds" at all.
``reachy.motion.listen_sleep.SleepHook`` (folded ``sleep`` inside ``listen``)
shares the same commanded-baseline mechanism as ``PatHook`` and is included in
the import-boundary check below as a bonus — it was not one of the two
required targets, but the check costs nothing extra.
"""

from __future__ import annotations

import ast
import inspect
import math
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import pytest

import reachy.motion.listen_pat as listen_pat_mod
import reachy.motion.listen_sleep as listen_sleep_mod
import reachy.motion.pat_signal as pat_signal
import reachy.sleep.patwake as patwake_mod
from reachy.motion.listen_pat import PatHook
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionQueue
from reachy.sleep.patwake import PatWakeSource

# ---------------------------------------------------------------------------
# Isolation: pin PatHook's pat-active flag into a throwaway state dir, exactly
# as tests/test_listen_pat.py does — this module writes it too via PatHook.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    pat_signal.clear()
    yield
    pat_signal.clear()


# ---------------------------------------------------------------------------
# A shared moving-pose fixture: a commanded pose that NEVER holds still.
#
# Two independent-frequency sinusoids on pitch/yaw so the combined pose vector
# keeps changing tick to tick throughout the run — the opposite of pat_sense's
# stillness precondition (a commanded pose constant for still_hold_s seconds).
# Both PatHook and PatWakeSource are driven from this SAME trajectory below.
# ---------------------------------------------------------------------------

_STEP_S = 0.05


def _moving_commanded_poses(n: int, *, step: float = _STEP_S) -> list[tuple[float, float]]:
    """``n`` ticks of a continuously-changing commanded (pitch_deg, yaw_deg) pose."""
    poses = []
    for i in range(n):
        t = i * step
        poses.append((3.0 * math.sin(0.9 * t), 4.0 * math.sin(0.4 * t + 1.3)))
    return poses


def test_moving_pose_fixture_never_holds_still() -> None:
    """Pin the fixture's own premise: every consecutive pair actually differs.

    Guards the two detection tests below against a future edit accidentally
    turning the "moving" fixture into a still one (e.g. a bad frequency/step
    choice landing exactly on a shared zero-crossing).
    """
    poses = _moving_commanded_poses(40)
    for (p0, y0), (p1, y1) in zip(poses, poses[1:]):
        assert (
            max(abs(p1 - p0), abs(y1 - y0)) > 1e-3
        ), "the fixture must never hold still tick-to-tick"


# ---------------------------------------------------------------------------
# 1a. PatHook senses a press even though its commanded baseline never stops
#     moving — no stillness gate anywhere in this path.
# ---------------------------------------------------------------------------


class _FollowsMovingCommandTransport:
    """A transport whose head_pose is whatever the test sets it to this tick."""

    name = "sdk"

    def __init__(self) -> None:
        self.next_pose: tuple[float, float] = (0.0, 0.0)

    def head_pose(self) -> tuple[float, float]:
        return self.next_pose


def test_pathook_detects_a_press_through_continuous_commanded_motion() -> None:
    """A hand press layered on the moving-pose fixture still fires through PatHook.

    ``commanded_head`` (the baseline PatHook measures deviation against) is set
    to a DIFFERENT value on every tick from :func:`_moving_commanded_poses` — it
    never holds a single value for two consecutive ticks, let alone for a
    stillness hold window. PatHook has no ``still_hold_s`` / ``still_eps``
    concept: it baselines on whatever the commanded pose *is* this tick, so a
    press deviating from that (still-moving) baseline is detected regardless.
    """
    queue: MotionQueue = MotionQueue()
    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    hook = PatHook(queue, detector=detector)
    transport = _FollowsMovingCommandTransport()

    poses = _moving_commanded_poses(60)
    now = 0.0
    fired = False
    for i, (cmd_pitch, cmd_yaw) in enumerate(poses):
        commanded = {"pitch": cmd_pitch, "yaw": cmd_yaw}
        # Alternate a deep downward press (below the moving baseline) and a
        # release (tracking the moving baseline exactly) to produce distinct
        # press edges, exactly as the existing PatHook tests do.
        pressed = i % 2 == 0
        actual_pitch = cmd_pitch - 20.0 if pressed else cmd_pitch
        transport.next_pose = (actual_pitch, cmd_yaw)
        hook(transport, queue, now, commanded)
        now += 0.4
        if hook.events >= 1:
            fired = True
            break

    assert fired, "PatHook must detect a press while its commanded baseline is continuously moving"
    labels = [a.label for a in queue.pending()]
    assert any(label.startswith("pat_") for label in labels), labels


# ---------------------------------------------------------------------------
# 1b. PatWakeSource senses a press even though the commanded sleep pose it is
#     measured against never stops moving — no stillness gate anywhere here
#     either.
# ---------------------------------------------------------------------------


def test_patwake_source_detects_a_press_through_continuous_commanded_motion() -> None:
    """A hand press layered on the SAME moving-pose fixture still fires through PatWakeSource.

    ``commanded_pose`` (PatWakeSource's injected provider) walks the identical
    :func:`_moving_commanded_poses` trajectory used above for PatHook — the
    sleep-breathe pose it models is moving by design (see the module
    docstring). PatWakeSource has no stillness concept either: it simply feeds
    ``actual - commanded`` to the reused :class:`PatDetector` every tick.

    ``min_presses=2`` with an alternating press/release pattern (mirroring how
    the PatHook tests above and in ``tests/test_listen_pat.py`` drive
    :class:`PatDetector`) is deliberate: it forces detection to survive across
    SEVERAL consecutive ticks of a continuously-changing commanded baseline,
    not just a single lucky first sample — a much stronger proof that nothing
    here waits for the pose to stop moving.
    """
    poses = _moving_commanded_poses(60)
    # Alternate a deep press (20 deg below whatever the moving commanded pitch
    # is this tick) with a release that tracks the moving baseline exactly —
    # distinct press edges spread across ticks where the baseline itself never
    # repeats a value.
    actuals = [
        (pitch - 20.0, yaw) if i % 2 == 0 else (pitch, yaw) for i, (pitch, yaw) in enumerate(poses)
    ]

    cmd_it = iter(poses)
    act_it = iter(actuals)

    def commanded_pose() -> tuple[float, float]:
        return next(cmd_it)

    def read_head_pose() -> tuple[float, float]:
        return next(act_it)

    detector = PatDetector(min_presses=2, pat_cooldown=0.0, level2_threshold_fn=lambda: 6.0)
    src = PatWakeSource(
        read_head_pose=read_head_pose, commanded_pose=commanded_pose, detector=detector
    )

    now = 0.0
    fired = False
    for _ in poses:
        if src.poll(now=now):
            fired = True
            break
        now += 0.4  # inside PatDetector's default pat_window (3.0 s) so presses accumulate

    assert fired, "PatWakeSource must detect a press while the commanded sleep pose keeps moving"


# ---------------------------------------------------------------------------
# 2. Boundary assertion: neither module imports pat_sense (module-level AST
#    check + a subprocess sys.modules check), and neither exposes pat_sense's
#    private stillness-gate names in its own namespace. Mirrors the pattern in
#    tests/test_speech_tools.py / tests/test_agent_turn.py (the forge / llm /
#    events import-boundary tests).
# ---------------------------------------------------------------------------

#: pat_sense's stillness-gate helpers/constants (issue #80) — private to that
#: module today, with zero external importers. Pinned here so a later task in
#: this plan cannot quietly reach them into a sibling consumer.
_STILLNESS_NAMES = frozenset(
    {
        "_commanded_still",
        "_stillness_open",
        "_rearm_stillness_hold",
        "DEFAULT_STILL_EPS",
        "DEFAULT_STILL_HOLD_S",
    }
)

#: The two required targets (acceptance criterion 2), plus listen_sleep.py as
#: a bonus — it shares PatHook's commanded-baseline mechanism and was called
#: out as relevant, though not a required target, in the task brief.
_TARGET_MODULES = (listen_pat_mod, patwake_mod, listen_sleep_mod)


def _imported_module_names(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


@pytest.mark.parametrize("module", _TARGET_MODULES, ids=lambda m: m.__name__)
def test_module_does_not_import_pat_sense(module) -> None:
    """No ``reachy.behavior.pat_sense`` import anywhere in the module source."""
    for name in _imported_module_names(module):
        assert (
            "behavior.pat_sense" not in name
        ), f"{module.__name__} must not import pat_sense ({name!r})"


@pytest.mark.parametrize("module", _TARGET_MODULES, ids=lambda m: m.__name__)
def test_module_does_not_expose_pat_sense_stillness_names(module) -> None:
    """None of pat_sense's private stillness-gate names leak into the module's namespace.

    Defensive on top of the import check above: catches a hypothetical
    ``from reachy.behavior.pat_sense import _commanded_still``-style import
    that would deposit the name directly into the module's ``__dict__``.
    """
    leaked = _STILLNESS_NAMES & set(module.__dict__)
    assert not leaked, f"{module.__name__} must not expose pat_sense stillness names: {leaked}"


@pytest.mark.parametrize(
    "module_name",
    ["reachy.motion.listen_pat", "reachy.sleep.patwake", "reachy.motion.listen_sleep"],
)
def test_importing_module_does_not_pull_pat_sense_into_sys_modules(module_name: str) -> None:
    """A fresh interpreter importing the module must not transitively import pat_sense.

    Stronger than the AST check: it would also catch an indirect/transitive
    import path the source-level scan can't see.
    """
    code = (
        f"import sys, {module_name};"
        "assert 'reachy.behavior.pat_sense' not in sys.modules, 'pat_sense leaked';"
        "print('ok')"
    )
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [sys.executable, "-c", code],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))
