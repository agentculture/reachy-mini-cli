"""Tests for the ``listen`` idle layer yielding to the sleep signal.

The idle producer layers two file-flag interrupts:

* :mod:`reachy.motion.pat_signal` — full suppression (return ``None``)
* :mod:`reachy.motion.sleep_signal` — full suppression that OUTRANKS pat.
  When ``sleep_active.flag`` is present the idle producer goes still (returns
  ``None``) and that decision is taken *before* the pat check, so sleep wins
  even if the pat flag is also set.

Regression note: before task t7 the idle path had no rest/decay state above pat
— the strongest interrupt was pat (full suppression). These tests assert the
branch ordering (sleep > pat > alive) so both demo and real listen sessions go
still while the sleep flag is present.

A third flag, ``think_active.flag``, used to sit between the two (low-energy
"focused breathe" rather than suppression). It retired with the folded
``listen --live`` cognition loop that wrote it — the two precedence tests that
paired it with sleep went with it; the pat pairing below covers the ordering
that survives.

Mirrors ``tests/test_pat_signal.py``'s idle-suppression section.
"""

from __future__ import annotations

import random

import pytest

import reachy.motion.pat_signal as ps
from reachy.motion import sleep_signal as ss
from reachy.motion.listen import ListenParams, ListenProducer


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Isolate every flag file under a temp state dir and start clean."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ss.clear()
    ps.clear()
    yield
    ss.clear()
    ps.clear()


def _fresh_producer(seed: int) -> ListenProducer:
    prod = ListenProducer(ListenParams(idle_energy=1.0))
    prod._rng = random.Random(seed)
    return prod


# ---------------------------------------------------------------------------
# 1. Sleep full-suppression
# ---------------------------------------------------------------------------


def test_idle_returns_none_while_sleep_active():
    """While the sleep flag is present, the idle producer emits NO pose."""
    prod = _fresh_producer(99)
    ss.write()
    for i in range(20):
        assert prod._idle(i * 2.5, live=False) is None


def test_idle_resumes_when_sleep_cleared():
    """After the sleep flag clears, the idle producer emits poses again."""
    prod = _fresh_producer(99)
    ss.write()
    assert prod._idle(0.0, live=False) is None
    ss.clear()
    emitted = [prod._idle(i * 2.5, live=False) for i in range(1, 10)]
    assert any(a is not None for a in emitted), "idle must resume once sleep clears"


def test_sleep_signal_read_via_monkeypatch(monkeypatch):
    """Monkeypatching ``sleep_signal.is_active`` to True forces stillness."""
    prod = _fresh_producer(99)
    monkeypatch.setattr(ss, "is_active", lambda: True)
    for i in range(10):
        assert prod._idle(i * 2.5, live=False) is None


# ---------------------------------------------------------------------------
# 2. Precedence — sleep outranks pat
# ---------------------------------------------------------------------------


def test_sleep_beats_pat():
    """Both sleep + pat active: sleep wins, idle returns None."""
    prod = _fresh_producer(99)
    ps.write()
    ss.write()
    for i in range(10):
        assert prod._idle(i * 2.5, live=False) is None


def test_sleep_checked_before_pat_branch():
    """The sleep branch precedes the pat branch.

    With sleep active, the producer must return None *without* consulting the
    pat signal: monkeypatching ``pat_signal.is_active`` to raise proves the
    sleep check short-circuits first.
    """
    prod = _fresh_producer(99)
    ss.write()

    def _boom() -> bool:  # pragma: no cover - must never be called
        raise AssertionError("pat_signal.is_active consulted after sleep branch")

    import reachy.motion.listen as listen_mod

    # The pat check lives behind ``pat_signal.is_active`` as imported into the
    # listen module; patch it there to prove ordering.
    original = listen_mod.pat_signal.is_active
    listen_mod.pat_signal.is_active = _boom  # type: ignore[assignment]
    try:
        assert prod._idle(0.0, live=False) is None
    finally:
        listen_mod.pat_signal.is_active = original  # type: ignore[assignment]


# ---------------------------------------------------------------------------
# 3. Sanity — no flags emits poses
# ---------------------------------------------------------------------------


def test_idle_emits_normally_with_no_flags():
    prod = _fresh_producer(99)
    emitted = [prod._idle(i * 2.5, live=False) for i in range(10)]
    assert any(a is not None for a in emitted)
