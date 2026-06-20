"""Tests for the composite :class:`~reachy.motion.listen_hooks.HookChain`.

``listen``'s motion loop (:func:`reachy.motion.server.run`) exposes a single
``on_tick`` seam — ``(transport, queue, t, commanded_head) -> None`` — fired once
per tick before the producer is consulted. Today only one hook (the #43
:class:`~reachy.motion.listen_pat.PatHook`) rides that seam. To let pat + think +
vision + sleep hooks (built by later tasks) coexist in the one loop that owns the
single SDK client, :class:`HookChain` fans a list of hooks out across the *same*
seam: a ``HookChain`` instance is itself a valid ``on_tick`` callable, so the
loop's contract is unchanged.

Coverage (mirrors the acceptance criteria):

1. The chain runs N hooks per tick, in the given (priority) order, with the
   exact ``on_tick`` signature ``(transport, queue, t, commanded_head)``.
2. An exception from ONE hook is swallowed (logged, not raised) and the
   remaining hooks still run that tick.
3. An empty chain is a no-op (call and close both safe).
4. ``close()`` fans out to every hook's ``close()``; a per-hook ``close()`` that
   raises is swallowed so one bad close does not block the rest. A hook without a
   ``close`` attribute is skipped cleanly.
5. A single real :class:`PatHook` wrapped in a one-element ``HookChain`` behaves
   identically to driving the ``PatHook`` directly (regression).

No robot, no daemon, no network, no real sleeps; clocks/state dir are injected.
"""

from __future__ import annotations

import logging

import pytest

import reachy.motion.pat_signal as ps
from reachy.motion.listen_hooks import HookChain
from reachy.motion.listen_pat import PatHook
from reachy.motion.pat import PatDetector
from reachy.motion.queue import MotionQueue

# ---------------------------------------------------------------------------
# Isolation: pin the pat-active flag into a throwaway state dir
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    """Pin bookkeeping into a throwaway dir; never touch the real state dir."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    ps.clear()
    yield
    ps.clear()


# ---------------------------------------------------------------------------
# Stub hooks: record their calls; mirror the on_tick contract exactly
# ---------------------------------------------------------------------------


class _RecordingHook:
    """A stub ``on_tick`` hook that records every call and close.

    Its ``__call__`` matches the loop contract
    ``(transport, queue, t, commanded_head)`` exactly. ``calls`` accumulates the
    ``(name, t)`` of each tick so test order can be asserted on the shared log.
    """

    def __init__(self, name: str, log: list, *, fail: bool = False):
        self.name = name
        self.log = log
        self.fail = fail
        self.ticks: list[tuple[object, object, float, object]] = []
        self.closed = 0

    def __call__(self, transport, queue, t, commanded_head=None):
        self.log.append((self.name, t))
        self.ticks.append((transport, queue, t, commanded_head))
        if self.fail:
            raise RuntimeError(f"{self.name} boom")

    def close(self):
        self.closed += 1

    def __repr__(self):
        return f"<_RecordingHook {self.name}>"


class _BadCloseHook(_RecordingHook):
    """Records ticks like :class:`_RecordingHook` but raises from ``close``."""

    def close(self):
        super().close()
        raise RuntimeError(f"{self.name} close boom")


class _NoCloseHook:
    """A callable hook with NO ``close`` attribute (close fan-out must skip it)."""

    def __init__(self, name: str, log: list):
        self.name = name
        self.log = log

    def __call__(self, transport, queue, t, commanded_head=None):
        self.log.append((self.name, t))


# ---------------------------------------------------------------------------
# 1. ordered fan-out across the on_tick seam
# ---------------------------------------------------------------------------


def test_runs_all_hooks_in_given_order():
    log: list = []
    a = _RecordingHook("a", log)
    b = _RecordingHook("b", log)
    c = _RecordingHook("c", log)
    chain = HookChain([a, b, c])

    chain("T", "Q", 1.5, {"pitch": 0.0, "yaw": 0.0})

    assert log == [("a", 1.5), ("b", 1.5), ("c", 1.5)]


def test_forwards_the_on_tick_arguments_verbatim():
    log: list = []
    a = _RecordingHook("a", log)
    chain = HookChain([a])
    transport, queue = object(), MotionQueue()
    commanded = {"pitch": 3.0, "yaw": -2.0}

    chain(transport, queue, 7.0, commanded)

    assert a.ticks == [(transport, queue, 7.0, commanded)]


def test_is_a_drop_in_on_tick_callable():
    """A ``HookChain`` instance must itself be callable as ``on_tick``."""
    chain = HookChain([_RecordingHook("a", [])])
    assert callable(chain)


# ---------------------------------------------------------------------------
# 2. one failing hook is swallowed; the rest still run
# ---------------------------------------------------------------------------


def test_one_failing_hook_does_not_stop_the_others(caplog):
    log: list = []
    a = _RecordingHook("a", log)
    boom = _RecordingHook("boom", log, fail=True)
    c = _RecordingHook("c", log)
    chain = HookChain([a, boom, c])

    with caplog.at_level(logging.WARNING):
        chain("T", "Q", 2.0, None)  # must NOT raise

    # every hook ran this tick, in order, despite the middle one raising
    assert log == [("a", 2.0), ("boom", 2.0), ("c", 2.0)]
    # the failure was logged, not raised
    assert any("boom" in rec.getMessage() for rec in caplog.records)


def test_failure_is_isolated_per_tick():
    """A hook that always fails never prevents later ticks of the others."""
    log: list = []
    boom = _RecordingHook("boom", log, fail=True)
    c = _RecordingHook("c", log)
    chain = HookChain([boom, c])

    for t in (1.0, 2.0):
        chain("T", "Q", t, None)

    assert ("c", 1.0) in log and ("c", 2.0) in log


# ---------------------------------------------------------------------------
# 3. empty chain is a no-op
# ---------------------------------------------------------------------------


def test_empty_chain_call_is_a_noop():
    chain = HookChain([])
    chain("T", "Q", 0.0, None)  # must not raise


def test_empty_chain_close_is_a_noop():
    HookChain([]).close()  # must not raise


# ---------------------------------------------------------------------------
# 4. close() fans out, guarded per-hook
# ---------------------------------------------------------------------------


def test_close_fans_out_to_every_hook():
    a = _RecordingHook("a", [])
    b = _RecordingHook("b", [])
    chain = HookChain([a, b])

    chain.close()

    assert a.closed == 1 and b.closed == 1


def test_one_bad_close_does_not_block_the_rest(caplog):
    a = _RecordingHook("a", [])
    bad = _BadCloseHook("bad", [])
    c = _RecordingHook("c", [])
    chain = HookChain([a, bad, c])

    with caplog.at_level(logging.WARNING):
        chain.close()  # must NOT raise

    assert a.closed == 1 and c.closed == 1  # neighbours still closed
    assert bad.closed == 1  # the bad one was attempted
    assert any("bad" in rec.getMessage() for rec in caplog.records)


def test_close_skips_hooks_without_a_close():
    a = _RecordingHook("a", [])
    plain = _NoCloseHook("plain", [])
    chain = HookChain([plain, a])

    chain.close()  # must not raise on the close-less hook

    assert a.closed == 1


# ---------------------------------------------------------------------------
# 5. regression: a one-element chain == the bare PatHook
# ---------------------------------------------------------------------------


def _deviating_transport(pitch: float = -0.5, yaw: float = 0.0):
    """A fake transport whose head_pose reads a steady downward press."""

    class _T:
        def head_pose(self):
            return (pitch, yaw)

    return _T()


def _drive(hook, transport, queue, ticks):
    """Tick a hook directly (the bare-PatHook control path)."""
    for i in range(ticks):
        hook(transport, queue, float(i), {"pitch": 0.0, "yaw": 0.0})


def test_single_pathook_in_chain_matches_bare_pathook():
    transport = _deviating_transport()

    # control: drive a bare PatHook
    q_bare = MotionQueue()
    bare = PatHook(q_bare, detector=PatDetector())
    _drive(bare, transport, q_bare, ticks=12)
    bare_events = bare.events
    bare_queue_len = len(q_bare)
    bare_flag = ps.is_active()
    bare.close()

    # subject: the same PatHook wrapped in a one-element HookChain
    q_chain = MotionQueue()
    wrapped = PatHook(q_chain, detector=PatDetector())
    chain = HookChain([wrapped])
    _drive(chain, transport, q_chain, ticks=12)

    assert wrapped.events == bare_events
    assert len(q_chain) == bare_queue_len
    assert ps.is_active() == bare_flag

    chain.close()
    # close fans through to the wrapped PatHook → flag cleared, like bare.close()
    assert not ps.is_active()
