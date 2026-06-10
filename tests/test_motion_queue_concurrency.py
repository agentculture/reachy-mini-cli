"""Thread-safety of :class:`MotionQueue` — the race ``think`` introduced.

``think`` runs the motion executor on a background thread while the cognition
thread submits expression gestures, so ``submit`` races ``peek``/``pop``. These
tests pin :meth:`MotionQueue.pop_if` (the atomic, identity-checked removal that
closes the peek→dispatch→remove window) and a concurrent submit/drain stress.
"""

from __future__ import annotations

import threading

from reachy.motion.queue import EXPRESSION_KEY, MotionAction, MotionQueue


def _gesture(label: str, key: str | None = EXPRESSION_KEY) -> MotionAction:
    return MotionAction(label=label, coalesce_key=key)


def test_pop_if_removes_matching_head():
    q = MotionQueue()
    a = _gesture("A")
    q.submit(a)
    assert q.pop_if(a) is a
    assert len(q) == 0


def test_pop_if_empty_queue_returns_none():
    assert MotionQueue().pop_if(_gesture("x")) is None


def test_pop_if_does_not_drop_a_coalesced_replacement():
    """The core regression: a blind pop() would drop the newer gesture.

    Executor peeks A and starts its (slow) move.  Mid-dispatch the cognition
    thread submits B, which coalesces A away (same EXPRESSION_KEY) — the head is
    now B.  When the executor finishes A and removes it, it must NOT pop B.
    """
    q = MotionQueue()
    a = _gesture("A")
    q.submit(a)

    nxt = q.peek()  # executor peeks A
    assert nxt is a

    b = _gesture("B")
    q.submit(b)  # concurrent submit: B evicts pending A -> _pending == [B]

    removed = q.pop_if(nxt)  # executor done with A; head is B, so nothing removed
    assert removed is None
    assert q.peek() is b  # B preserved — it will execute next, not be dropped
    assert len(q) == 1


def test_concurrent_submit_and_drain_loses_no_action():
    """Hammer submit() from one thread while another drains; nothing dropped/duped.

    One-shot gestures (``coalesce_key=None``) never coalesce, so every submitted
    action must be dispatched exactly once, in FIFO order — even under concurrent
    submit/drain. A blind, unlocked pop() would corrupt the list or mis-order.
    """
    q = MotionQueue()
    executed: list[str] = []
    state = {"stop": False}

    def drain() -> None:
        while not state["stop"] or len(q):
            nxt = q.peek()
            if nxt is None:
                continue
            if q.pop_if(nxt) is not None:  # simulate accepted dispatch
                executed.append(nxt.label)

    worker = threading.Thread(target=drain, name="drain")
    worker.start()

    n = 500
    for i in range(n):
        q.submit(_gesture(f"g{i}", key=None))  # one-shot: never coalesced

    state["stop"] = True
    worker.join(timeout=5.0)
    assert not worker.is_alive()

    assert executed == [f"g{i}" for i in range(n)]
