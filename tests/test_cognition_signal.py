"""Tests for :mod:`reachy.speech.cognition_signal`.

Acceptance criteria:

1. Exposes ``cognition_flag_path`` / ``write`` / ``clear`` / ``is_active`` over a
   stdlib file flag under ``$REACHY_STATE_DIR`` (same resolution precedence as
   :func:`reachy.daemon.state_dir`). No new dependency.
2. A context manager ``cognition_active()`` writes the flag on enter and clears it
   on exit — including on exception. A stale flag left by a prior crash is
   tolerated and overwritten on next start; a later clean exit clears it.
"""

from __future__ import annotations

import contextlib

import pytest

import reachy.speech.cognition_signal as cs


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)


# ---------------------------------------------------------------------------
# 1a. Path resolution — flag file lives under REACHY_STATE_DIR
# ---------------------------------------------------------------------------


def test_flag_path_under_state_dir(tmp_path):
    path = cs.cognition_flag_path()
    assert path.parent == tmp_path, "flag must live directly under $REACHY_STATE_DIR"
    assert path.name, "flag filename must not be empty"


def test_flag_path_uses_xdg_state_home_when_no_override(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = cs.cognition_flag_path()
    assert path.parent == tmp_path / "reachy"


def test_flag_path_falls_back_to_home_local_state(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = cs.cognition_flag_path()
    assert path.parent == tmp_path / ".local" / "state" / "reachy"


# ---------------------------------------------------------------------------
# 1b. write / clear / is_active basic semantics
# ---------------------------------------------------------------------------


def test_initially_not_active(tmp_path):
    assert cs.is_active() is False


def test_write_makes_active(tmp_path):
    cs.write()
    assert cs.is_active() is True
    assert cs.cognition_flag_path().exists()


def test_clear_removes_flag(tmp_path):
    cs.write()
    cs.clear()
    assert cs.is_active() is False
    assert not cs.cognition_flag_path().exists()


def test_clear_on_absent_flag_does_not_raise(tmp_path):
    # Flag was never written — clear must be idempotent.
    cs.clear()  # must not raise
    assert cs.is_active() is False


def test_write_is_idempotent(tmp_path):
    cs.write()
    cs.write()  # second write on an existing flag must not raise
    assert cs.is_active() is True


def test_state_dir_created_if_missing(monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setenv("REACHY_STATE_DIR", str(nested))
    # The directory does not exist yet — write must create it.
    cs.write()
    assert nested.exists()
    assert cs.is_active() is True


# ---------------------------------------------------------------------------
# 2. Context manager behaviour
# ---------------------------------------------------------------------------


def test_context_manager_writes_on_enter_and_clears_on_exit(tmp_path):
    with cs.cognition_active():
        assert cs.is_active() is True
    assert cs.is_active() is False


def test_context_manager_clears_on_exception(tmp_path):
    with contextlib.suppress(RuntimeError):
        with cs.cognition_active():
            assert cs.is_active() is True
            raise RuntimeError("simulated crash")
    assert cs.is_active() is False


def test_context_manager_tolerates_stale_flag(tmp_path):
    """A stale flag left by a prior crash is overwritten; a clean exit clears it."""
    # Simulate a crash: write flag without using the context manager (stale file).
    cs.write()
    assert cs.is_active() is True

    # Re-entering with context manager must succeed (overwrite) and clear on exit.
    with cs.cognition_active():
        assert cs.is_active() is True
    assert cs.is_active() is False


def test_context_manager_nested_outer_wins(tmp_path):
    """Entering twice: the flag stays active until the outer context exits."""
    with cs.cognition_active():
        with cs.cognition_active():
            assert cs.is_active() is True
        # Inner exit cleared it — outer's cleanup must not blow up even if
        # flag is already gone (clear is idempotent).
    # After both exits the flag must be gone.
    assert cs.is_active() is False
