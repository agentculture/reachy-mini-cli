"""Tests for :mod:`reachy.motion.pat_signal` and the idle pat-suppression hook.

Governing principle under test: A SCRATCH BREAKS STILLNESS. While the robot is
being patted, the always-alive ``listen`` idle wander must pause entirely so the
pat lean/snuggle reaction is not fought by idle motion.

Two layers are exercised:

* :mod:`reachy.motion.pat_signal` — a stdlib file flag mirroring
  :mod:`reachy.speech.cognition_signal` exactly in shape (write / clear /
  is_active / context manager / flag-path helper), resolved under
  ``$REACHY_STATE_DIR``.
* :meth:`ListenProducer._idle` — reads ``pat_signal.is_active()`` per tick and
  returns ``None`` (no idle pose at all) while the pat flag is present.
"""

from __future__ import annotations

import contextlib
import random

import pytest

import reachy.motion.pat_signal as ps
from reachy.motion.listen import ListenParams, ListenProducer
from reachy.speech import cognition_signal


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ps.clear()
    cognition_signal.clear()
    yield
    ps.clear()
    cognition_signal.clear()


# ---------------------------------------------------------------------------
# 1a. Path resolution — flag file lives under REACHY_STATE_DIR
# ---------------------------------------------------------------------------


def test_flag_path_under_state_dir(tmp_path):
    path = ps.pat_flag_path()
    assert path.parent == tmp_path, "flag must live directly under $REACHY_STATE_DIR"
    assert path.name, "flag filename must not be empty"


def test_flag_path_uses_xdg_state_home_when_no_override(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = ps.pat_flag_path()
    assert path.parent == tmp_path / "reachy"


def test_flag_path_falls_back_to_home_local_state(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = ps.pat_flag_path()
    assert path.parent == tmp_path / ".local" / "state" / "reachy"


# ---------------------------------------------------------------------------
# 1b. write / clear / is_active basic semantics
# ---------------------------------------------------------------------------


def test_initially_not_active():
    assert ps.is_active() is False


def test_write_makes_active():
    ps.write()
    assert ps.is_active() is True
    assert ps.pat_flag_path().exists()


def test_clear_removes_flag():
    ps.write()
    ps.clear()
    assert ps.is_active() is False
    assert not ps.pat_flag_path().exists()


def test_clear_on_absent_flag_does_not_raise():
    ps.clear()  # must not raise
    assert ps.is_active() is False


def test_write_is_idempotent():
    ps.write()
    ps.write()  # second write on an existing flag must not raise
    assert ps.is_active() is True


def test_state_dir_created_if_missing(monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setenv("REACHY_STATE_DIR", str(nested))
    ps.write()
    assert nested.exists()
    assert ps.is_active() is True


# ---------------------------------------------------------------------------
# 2. Context manager behaviour
# ---------------------------------------------------------------------------


def test_context_manager_writes_on_enter_and_clears_on_exit():
    with ps.pat_active():
        assert ps.is_active() is True
    assert ps.is_active() is False


def test_context_manager_clears_on_exception():
    with contextlib.suppress(RuntimeError):
        with ps.pat_active():
            assert ps.is_active() is True
            raise RuntimeError("simulated crash")
    assert ps.is_active() is False


def test_context_manager_tolerates_stale_flag():
    """A stale flag left by a prior crash is overwritten; a clean exit clears it."""
    ps.write()
    assert ps.is_active() is True
    with ps.pat_active():
        assert ps.is_active() is True
    assert ps.is_active() is False


# ---------------------------------------------------------------------------
# 3. Pat flag is independent of the cognition (think) flag
# ---------------------------------------------------------------------------


def test_pat_and_cognition_flags_are_distinct():
    ps.write()
    assert ps.is_active() is True
    assert cognition_signal.is_active() is False
    cognition_signal.write()
    ps.clear()
    assert ps.is_active() is False
    assert cognition_signal.is_active() is True


# ---------------------------------------------------------------------------
# 4. ListenProducer._idle — pat suppression
# ---------------------------------------------------------------------------


def _fresh_producer(seed: int) -> ListenProducer:
    prod = ListenProducer(ListenParams(idle_energy=1.0))
    prod._rng = random.Random(seed)
    return prod


def test_idle_returns_none_while_pat_active():
    """While the pat flag is present, the idle producer emits NO pose at all."""
    prod = _fresh_producer(99)
    ps.write()
    # Across a window of ticks, every idle call must yield None.
    for i in range(20):
        assert prod._idle(i * 2.5, live=False) is None


def test_idle_resumes_when_pat_cleared():
    """After the pat flag clears, the idle producer emits poses again."""
    prod = _fresh_producer(99)
    ps.write()
    assert prod._idle(0.0, live=False) is None
    ps.clear()
    # At least one of the next ticks emits an actual idle pose.
    emitted = [prod._idle(i * 2.5, live=False) for i in range(1, 10)]
    assert any(a is not None for a in emitted), "idle must resume once pat clears"


def test_pat_suppression_beats_cognition_focused():
    """If BOTH flags are active, pat wins: the idle producer returns None."""
    prod = _fresh_producer(99)
    cognition_signal.write()
    ps.write()
    for i in range(10):
        assert prod._idle(i * 2.5, live=False) is None


def test_idle_emits_normally_with_no_flags():
    """Sanity: with neither flag set the idle layer emits poses (not all None)."""
    prod = _fresh_producer(99)
    emitted = [prod._idle(i * 2.5, live=False) for i in range(10)]
    assert any(a is not None for a in emitted)
