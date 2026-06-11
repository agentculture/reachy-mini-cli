"""Tests for :mod:`reachy.motion.sleep_signal`.

Governing principle under test: the sleep flag signals that the robot is in a
sleep/rest state. While the robot is asleep, other nouns can check ``is_active()``
to suppress or modify their behaviour accordingly.

Two acceptance criteria are exercised:

* :func:`asleep` writes ``sleep_active.flag`` on enter and removes it on exit,
  including on exception.
* :func:`is_active` is a pure :meth:`Path.exists` check.

The flag is structurally identical to :mod:`reachy.motion.pat_signal` and
:mod:`reachy.speech.cognition_signal` — only the flag file name and symbol names
differ.
"""

from __future__ import annotations

import contextlib

import pytest

import reachy.motion.sleep_signal as ss
from reachy.motion import pat_signal as ps
from reachy.speech import cognition_signal


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ss.clear()
    yield
    ss.clear()


# ---------------------------------------------------------------------------
# 1a. Path resolution — flag file lives under REACHY_STATE_DIR
# ---------------------------------------------------------------------------


def test_flag_path_under_state_dir(tmp_path):
    path = ss.sleep_flag_path()
    assert path.parent == tmp_path, "flag must live directly under $REACHY_STATE_DIR"
    assert path.name == "sleep_active.flag"


def test_flag_path_uses_xdg_state_home_when_no_override(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
    path = ss.sleep_flag_path()
    assert path.parent == tmp_path / "reachy"


def test_flag_path_falls_back_to_home_local_state(monkeypatch, tmp_path):
    monkeypatch.delenv("REACHY_STATE_DIR", raising=False)
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.setattr("pathlib.Path.home", lambda: tmp_path)
    path = ss.sleep_flag_path()
    assert path.parent == tmp_path / ".local" / "state" / "reachy"


# ---------------------------------------------------------------------------
# 1b. write / clear / is_active basic semantics
# ---------------------------------------------------------------------------


def test_initially_not_active():
    assert ss.is_active() is False


def test_write_makes_active():
    ss.write()
    assert ss.is_active() is True
    assert ss.sleep_flag_path().exists()


def test_clear_removes_flag():
    ss.write()
    ss.clear()
    assert ss.is_active() is False
    assert not ss.sleep_flag_path().exists()


def test_clear_on_absent_flag_does_not_raise():
    ss.clear()  # must not raise
    assert ss.is_active() is False


def test_write_is_idempotent():
    ss.write()
    ss.write()  # second write on an existing flag must not raise
    assert ss.is_active() is True


def test_state_dir_created_if_missing(monkeypatch, tmp_path):
    nested = tmp_path / "deep" / "nested"
    monkeypatch.setenv("REACHY_STATE_DIR", str(nested))
    ss.write()
    assert nested.exists()
    assert ss.is_active() is True


# ---------------------------------------------------------------------------
# 2. Context manager behaviour
# ---------------------------------------------------------------------------


def test_context_manager_writes_on_enter_and_clears_on_exit():
    with ss.asleep():
        assert ss.is_active() is True
    assert ss.is_active() is False


def test_context_manager_clears_on_exception():
    with contextlib.suppress(RuntimeError):
        with ss.asleep():
            assert ss.is_active() is True
            raise RuntimeError("simulated crash")
    assert ss.is_active() is False


def test_context_manager_tolerates_stale_flag():
    """A stale flag left by a prior crash is overwritten; a clean exit clears it."""
    ss.write()
    assert ss.is_active() is True
    with ss.asleep():
        assert ss.is_active() is True
    assert ss.is_active() is False


# ---------------------------------------------------------------------------
# 3. Sleep flag is independent of the cognition (think) and pat flags
# ---------------------------------------------------------------------------


def test_sleep_and_cognition_flags_are_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    ss.write()
    assert ss.is_active() is True
    assert cognition_signal.is_active() is False
    cognition_signal.write()
    ss.clear()
    assert ss.is_active() is False
    assert cognition_signal.is_active() is True
    cognition_signal.clear()


def test_sleep_and_pat_flags_are_distinct(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    ss.write()
    assert ss.is_active() is True
    assert ps.is_active() is False
    ps.write()
    ss.clear()
    assert ss.is_active() is False
    assert ps.is_active() is True
    ps.clear()
