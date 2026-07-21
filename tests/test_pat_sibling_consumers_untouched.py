"""Task t3 (plan ``no-freeze-pat-sense``) — pin the sibling pat consumer untouched.

An upcoming task in this plan changes ``reachy/behavior/pat_sense.py``'s
stillness gating (``_commanded_still`` / ``_stillness_open`` /
``_rearm_stillness_hold`` / ``DEFAULT_STILL_EPS`` / ``DEFAULT_STILL_HOLD_S``).
A sibling pat consumer already senses through motion today, by a completely
DIFFERENT mechanism — expected/current-pose tracking, not a stillness gate —
and this module is the regression guard proving that stays true and that the
module is not coupled to ``pat_sense`` in any way a later task in this plan
could regress:

* :class:`reachy.sleep.patwake.PatWakeSource` — feeds the detector the
  *current* (moving) sleep-breathe commanded pose each tick, via an injected
  ``commanded_pose`` provider.

It simply reuses :class:`reachy.motion.pat.PatDetector` as-is; it has no
concept of "commanded pose held still for N seconds" at all.

Two sibling consumers used to ride along here and no longer exist. Both retired
with the old AI-first flow, not with any change to the pat sense:
``reachy.motion.listen_sleep.SleepHook`` (the folded ``sleep`` inside ``listen
--live``) went with the ``--live`` composition root, and
``reachy.motion.listen_pat.PatHook`` — which fed the detector the commanded
pose ``listen`` dispatched each tick — went with the ``listen`` NOUN itself
(task t22, the old flow's retirement), leaving it with no caller. The property
under guard is unchanged: ``PatWakeSource`` rides the SAME
:func:`_moving_commanded_poses` trajectory ``PatHook`` did, so only the second
witness is gone, not the proof.
"""

from __future__ import annotations

import ast
import inspect
import math
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import pytest

import reachy.sleep.patwake as patwake_mod
from reachy.motion.pat import PatDetector
from reachy.sleep.patwake import PatWakeSource

# ---------------------------------------------------------------------------
# Isolation: keep every state-dir consumer pointed at a throwaway dir so this
# module can never read the real one.
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


# ---------------------------------------------------------------------------
# A shared moving-pose fixture: a commanded pose that NEVER holds still.
#
# Two independent-frequency sinusoids on pitch/yaw so the combined pose vector
# keeps changing tick to tick throughout the run — the opposite of pat_sense's
# stillness precondition (a commanded pose constant for still_hold_s seconds).
# PatWakeSource is driven from this trajectory below.
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

    Guards the detection test below against a future edit accidentally
    turning the "moving" fixture into a still one (e.g. a bad frequency/step
    choice landing exactly on a shared zero-crossing).
    """
    poses = _moving_commanded_poses(40)
    for (p0, y0), (p1, y1) in zip(poses, poses[1:]):
        assert (
            max(abs(p1 - p0), abs(y1 - y0)) > 1e-3
        ), "the fixture must never hold still tick-to-tick"


# ---------------------------------------------------------------------------
# 1. PatWakeSource senses a press even though the commanded sleep pose it is
#     measured against never stops moving — no stillness gate anywhere here
#     either.
# ---------------------------------------------------------------------------


def test_patwake_source_detects_a_press_through_continuous_commanded_motion() -> None:
    """A hand press layered on the SAME moving-pose fixture still fires through PatWakeSource.

    ``commanded_pose`` (PatWakeSource's injected provider) walks the
    :func:`_moving_commanded_poses` trajectory — the sleep-breathe pose it
    models is moving by design (see the module docstring). PatWakeSource has no
    stillness concept: it simply feeds ``actual - commanded`` to the reused
    :class:`PatDetector` every tick.

    ``min_presses=2`` with an alternating press/release pattern is deliberate:
    it forces detection to survive across
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
#: this plan cannot quietly reach them into the sibling consumer.
_STILLNESS_NAMES = frozenset(
    {
        "_commanded_still",
        "_stillness_open",
        "_rearm_stillness_hold",
        "DEFAULT_STILL_EPS",
        "DEFAULT_STILL_HOLD_S",
    }
)

#: The surviving required target (acceptance criterion 2).
_TARGET_MODULES = (patwake_mod,)


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


@pytest.mark.parametrize("module_name", ["reachy.sleep.patwake"])
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
