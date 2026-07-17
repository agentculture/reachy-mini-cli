"""Tests for ``reachy.behavior.state`` — the joints + head-pose snapshot seam.

Two concerns, per task t6's acceptance criteria:

1. ``StateReader`` reads joints/head-pose through *injected* callables only
   (no ``reachy_mini`` import anywhere, no second SDK client opened) and
   degrades a missing/raising/``None``-returning reader to ``None`` rather
   than crashing.
2. A repo-wide grep test: the word "battery" must not appear anywhere under
   ``reachy/`` (source, schema, or doc example) — this module is
   battery-free by construction and the repo stays that way.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from reachy.behavior.state import StateReader, StateSnapshot

REPO_ROOT = Path(__file__).parent.parent
REACHY_PKG = REPO_ROOT / "reachy"


# --------------------------------------------------------------------------- #
# StateSnapshot / StateReader                                                 #
# --------------------------------------------------------------------------- #


def test_state_snapshot_is_frozen_dataclass():
    snap = StateSnapshot(joints=(1.0, 2.0), head_pose={"pitch": 1.0, "yaw": 2.0}, ts=5.0)
    assert snap.joints == (1.0, 2.0)
    assert snap.head_pose == {"pitch": 1.0, "yaw": 2.0}
    assert snap.ts == 5.0
    with pytest.raises(AttributeError):
        snap.ts = 6.0  # type: ignore[misc]


def test_state_snapshot_defaults_to_none_and_zero():
    snap = StateSnapshot()
    assert snap.joints is None
    assert snap.head_pose is None
    assert snap.ts == 0.0


def test_state_reader_reads_joints_and_head_pose_via_injected_callables():
    joints = ([1.0, 2.0, 3.0], [0.1, 0.2])
    pose = (12.5, -3.0)
    reader = StateReader(
        joints_fn=lambda: joints,
        head_pose_fn=lambda: pose,
        now=lambda: 42.0,
    )
    snap = reader()
    assert snap.joints == joints
    assert snap.head_pose == pose
    assert snap.ts == 42.0


def test_state_reader_call_is_the_read_method():
    """StateReader is callable (mirrors DoaPoller's callable idiom)."""
    reader = StateReader(joints_fn=lambda: (1,), head_pose_fn=lambda: (2,), now=lambda: 1.0)
    snap = reader()
    assert isinstance(snap, StateSnapshot)


def test_state_reader_degrades_missing_readers_to_none():
    """No readers injected at all — every field degrades to None, no crash."""
    reader = StateReader(now=lambda: 7.0)
    snap = reader()
    assert snap.joints is None
    assert snap.head_pose is None
    assert snap.ts == 7.0


def test_state_reader_degrades_raising_joints_fn_to_none():
    def _boom():
        raise RuntimeError("no SDK client")

    reader = StateReader(joints_fn=_boom, head_pose_fn=lambda: (1.0, 2.0), now=lambda: 1.0)
    snap = reader()
    assert snap.joints is None
    assert snap.head_pose == (1.0, 2.0)


def test_state_reader_degrades_raising_head_pose_fn_to_none():
    def _boom():
        raise ConnectionError("daemon unreachable")

    reader = StateReader(joints_fn=lambda: ([1.0], [2.0]), head_pose_fn=_boom, now=lambda: 1.0)
    snap = reader()
    assert snap.joints == ([1.0], [2.0])
    assert snap.head_pose is None


def test_state_reader_degrades_reader_returning_none():
    """A reader that itself returns None (e.g. 'no reading yet') stays None, no crash."""
    reader = StateReader(joints_fn=lambda: None, head_pose_fn=lambda: None, now=lambda: 3.0)
    snap = reader()
    assert snap.joints is None
    assert snap.head_pose is None


def test_state_reader_both_raise_still_returns_a_snapshot_with_ts():
    def _boom():
        raise Exception("dead")  # noqa: TRY002

    reader = StateReader(joints_fn=_boom, head_pose_fn=_boom, now=lambda: 99.0)
    snap = reader()
    assert snap.joints is None
    assert snap.head_pose is None
    assert snap.ts == 99.0


def test_state_reader_uses_injected_clock_not_wall_clock():
    calls = {"n": 0}

    def _now():
        calls["n"] += 1
        return 123.456

    reader = StateReader(joints_fn=lambda: (), head_pose_fn=lambda: (), now=_now)
    snap = reader()
    assert snap.ts == 123.456
    assert calls["n"] == 1


def test_state_reader_default_clock_is_time_monotonic():
    import time

    reader = StateReader(joints_fn=lambda: (), head_pose_fn=lambda: ())
    before = time.monotonic()
    snap = reader()
    after = time.monotonic()
    assert before <= snap.ts <= after


def test_state_reader_takes_no_transport_or_client_argument():
    """Fully mockable: construction needs only plain callables, no client/transport object."""
    reader = StateReader(joints_fn=lambda: "joints", head_pose_fn=lambda: "pose")
    assert callable(reader)
    snap = reader()
    assert snap.joints == "joints"
    assert snap.head_pose == "pose"


def test_state_module_imports_no_reachy_mini_or_transport():
    """The module is a dependency-free leaf: no reachy_mini import, no second SDK client.

    Mentioning ``reachy_mini``/``reachy.robot`` in prose (docstrings pointing at
    the real surface being duck-typed) is fine and expected; an actual ``import``
    statement pulling either in is not — that would make this a non-leaf module.
    """
    import ast

    import reachy.behavior.state as state_mod

    source = Path(state_mod.__file__).read_text(encoding="utf-8")
    tree = ast.parse(source)
    imported_modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_modules.add(node.module)

    assert not any(
        m == "reachy_mini" or m.startswith("reachy_mini.") for m in imported_modules
    ), f"module must not import reachy_mini, found: {imported_modules}"
    assert not any(
        m == "reachy.robot" or m.startswith("reachy.robot.") for m in imported_modules
    ), f"module must not import reachy.robot (the transport), found: {imported_modules}"


# --------------------------------------------------------------------------- #
# Repo-wide "battery-free by construction" grep test                          #
# --------------------------------------------------------------------------- #


def _reachy_source_files() -> list[Path]:
    """Every .py / .toml / .md file under reachy/ (source, schema, doc examples)."""
    files: list[Path] = []
    for pattern in ("*.py", "*.toml", "*.md"):
        files.extend(REACHY_PKG.rglob(pattern))
    return sorted(files)


def test_reachy_source_files_exist_for_the_grep_scan():
    """Sanity check the scan itself isn't silently empty (a vacuous pass is not a pass)."""
    files = _reachy_source_files()
    assert len(files) > 50, f"expected many files under reachy/, found {len(files)}"


@pytest.mark.parametrize(
    "path", _reachy_source_files(), ids=lambda p: str(p.relative_to(REACHY_PKG))
)
def test_no_battery_word_under_reachy(path: Path):
    """The word 'battery' must appear in no source file, schema, or doc example under reachy/.

    Reachy Mini has no battery-level API; this is a repo-wide guard so no future
    module/doc reintroduces a "battery" concept that doesn't exist on the device.
    """
    text = path.read_text(encoding="utf-8", errors="replace")
    assert "battery" not in text.lower(), f"found 'battery' in {path}"
