"""Tests for folding periodic scene description into the ``listen`` loop (task t10).

``listen`` owns the single-consumer SDK client (and its one camera). A *separate*
scene-description process would contend for that one media/camera subsystem — the
same single-SDK-owner constraint that folded pat (#43), vision (t7), and face (t9)
into ``listen``'s loop. So :class:`~reachy.motion.listen_scene.SceneHook` is folded
the same way: a per-tick ``on_tick`` hook ``(transport, queue, t, commanded_head)``.

Design points these tests pin down (mirroring FaceHook):

* **No second frame grabber.** ``SceneHook`` never opens a camera or spawns a
  frame grabber of its own — it takes a ``frame_provider`` callable (the shared,
  non-consuming :meth:`~reachy.motion.listen_vision.VisionHook.latest_frame`). The
  per-tick ``__call__`` only publishes the latest frame to a background DESCRIBE
  worker and drains completed results; the heavy VLM ``describe_frame`` runs off the
  tick thread — a hung/slow describe never freezes the loop.
* **Bounded cadence (default 30 s, injectable clock).** The worker describes at most
  once per ``interval``; each result is fed to cognition via
  :meth:`~reachy.speech.events.EventBuffer.feed_scene`.
* **One loud drop per failure episode.** A :class:`~reachy.vision.scene.SceneError`
  logs exactly ONE ``senselog.drop(reason=vlm-unreachable)`` per episode (a run of
  consecutive failures), not per tick, and never stalls or crashes the loop.

No robot, no daemon, no network, no cv2 — ``describe`` is an injected fake.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import pytest

from reachy.motion.listen_scene import DEFAULT_DESCRIBE_INTERVAL, SceneHook
from reachy.vision.scene import SceneError

_FRAME = object()  # opaque frame token — the fakes never inspect it

_SENSE_LOGGER_NAME = "reachy.sense"
_SENSE_LINE_RE = re.compile(
    r"^\[SENSE stage=(?P<stage>\S+) source=(?P<source>\S+) event=(?P<event>\S+)\] "
    r"(?P<detail>.*)$"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _FakeDescribe:
    """A stand-in for ``describe_frame``: records calls, returns a fixed text."""

    def __init__(self, text: str = "a person at a desk") -> None:
        self._text = text
        self.calls = 0

    def __call__(self, frame: object) -> str:  # noqa: ARG002
        self.calls += 1
        return self._text


class _ScriptedDescribe:
    """Returns/raises per call from a script of results (str) or exceptions."""

    def __init__(self, script: list) -> None:
        self._script = list(script)
        self.calls = 0

    def __call__(self, frame: object) -> str:  # noqa: ARG002
        item = self._script[min(self.calls, len(self._script) - 1)]
        self.calls += 1
        if isinstance(item, Exception):
            raise item
        return item


class _FakeBuffer:
    """Records every ``feed_scene(text)`` call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def feed_scene(self, text: str) -> None:
        self.calls.append(text)


class _RaisingBuffer:
    def feed_scene(self, text: str) -> None:
        raise RuntimeError("cognition buffer exploded")


class _FakeClock:
    """A mutable clock; ``clock.now`` is the value returned by calling it."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _stopped_hook(**kwargs) -> SceneHook:
    """A SceneHook with its worker stopped, for deterministic synchronous driving."""
    hook = SceneHook(**kwargs)
    hook.close()
    return hook


# ---------------------------------------------------------------------------
# 1. describe core: text / empty / faults
# ---------------------------------------------------------------------------


def test_describe_once_returns_stripped_text() -> None:
    hook = _stopped_hook(describe=lambda f: "  a red mug  ", frame_provider=lambda: None)
    assert hook._describe_once(_FRAME) == "a red mug"


def test_describe_once_empty_result_returns_none() -> None:
    hook = _stopped_hook(describe=lambda f: "   ", frame_provider=lambda: None)
    assert hook._describe_once(_FRAME) is None


def test_describe_once_scene_error_degrades_to_none() -> None:
    def _down(_frame):
        raise SceneError("vlm down")

    hook = _stopped_hook(describe=_down, frame_provider=lambda: None)
    assert hook._describe_once(_FRAME) is None  # no raise


def test_describe_once_arbitrary_error_degrades_to_none() -> None:
    def _boom(_frame):
        raise RuntimeError("unexpected")

    hook = _stopped_hook(describe=_boom, frame_provider=lambda: None)
    assert hook._describe_once(_FRAME) is None  # no raise


# ---------------------------------------------------------------------------
# 2. bounded describe cadence (default 30 s, injectable clock)
# ---------------------------------------------------------------------------


def test_default_interval_is_thirty_seconds() -> None:
    assert DEFAULT_DESCRIBE_INTERVAL == pytest.approx(30.0)


def test_worker_describes_at_most_once_per_interval() -> None:
    clock = _FakeClock(100.0)
    describe = _FakeDescribe("scene")
    hook = _stopped_hook(describe=describe, frame_provider=lambda: None, clock=clock)

    hook._input.publish(_FRAME)
    hook._worker_tick()  # first describe
    assert describe.calls == 1

    hook._input.publish(_FRAME)
    hook._worker_tick()  # same clock -> cadence not elapsed -> no describe
    assert describe.calls == 1

    clock.now = 100.0 + DEFAULT_DESCRIBE_INTERVAL + 0.01  # past the interval
    hook._input.publish(_FRAME)
    hook._worker_tick()  # cadence elapsed -> a fresh describe
    assert describe.calls == 2


def test_worker_tick_with_no_frame_is_a_noop() -> None:
    describe = _FakeDescribe("scene")
    hook = _stopped_hook(describe=describe, frame_provider=lambda: None, interval=0.0)
    hook._worker_tick()  # nothing published -> no describe
    assert describe.calls == 0


# ---------------------------------------------------------------------------
# 3. per-tick __call__ publishes the shared frame + drains a cue
# ---------------------------------------------------------------------------


def test_call_publishes_the_shared_frame_to_the_worker() -> None:
    hook = _stopped_hook(
        describe=_FakeDescribe("scene"), frame_provider=lambda: _FRAME, interval=0.0
    )
    hook(None, None, t=0.0)  # publishes the shared frame
    assert hook._input.take() is _FRAME


def test_call_drains_a_worker_result_into_feed_scene() -> None:
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        describe=_FakeDescribe("scene"), buffer=buffer, frame_provider=lambda: None
    )
    hook._output.publish("a cat on the couch")
    hook(None, None, t=0.0)
    assert buffer.calls == ["a cat on the couch"]


def test_call_with_no_worker_result_is_a_noop() -> None:
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        describe=_FakeDescribe("scene"), buffer=buffer, frame_provider=lambda: None
    )
    hook(None, None, t=0.0)  # nothing published -> no feed
    assert buffer.calls == []


# ---------------------------------------------------------------------------
# 4. one loud drop per failure episode (not per tick); resets on success
# ---------------------------------------------------------------------------


def _drop_records(caplog) -> list:
    return [
        r
        for r in caplog.records
        if r.name == _SENSE_LOGGER_NAME and "dropped reason=vlm-unreachable" in r.getMessage()
    ]


def test_scene_error_logs_one_drop_per_failure_episode(caplog) -> None:
    def _down(_frame):
        raise SceneError("cannot reach the vlm")

    hook = _stopped_hook(describe=_down, frame_provider=lambda: None, interval=0.0)
    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        hook._input.publish(_FRAME)
        hook._worker_tick()  # fail 1 -> one loud drop
        hook._input.publish(_FRAME)
        hook._worker_tick()  # fail 2 (same episode) -> NO new drop
        hook._input.publish(_FRAME)
        hook._worker_tick()  # fail 3 (same episode) -> NO new drop

    assert len(_drop_records(caplog)) == 1


def test_drop_line_names_the_scene_source_and_reason(caplog) -> None:
    def _down(_frame):
        raise SceneError("cannot reach the vlm")

    hook = _stopped_hook(describe=_down, frame_provider=lambda: None, interval=0.0)
    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        hook._input.publish(_FRAME)
        hook._worker_tick()

    records = _drop_records(caplog)
    assert len(records) == 1
    match = _SENSE_LINE_RE.match(records[0].getMessage())
    assert match is not None
    assert match.group("source") == "scene"
    assert "vlm-unreachable" in match.group("detail")


def test_failure_episode_resets_on_success(caplog) -> None:
    describe = _ScriptedDescribe(
        [SceneError("down"), "a recovered scene", SceneError("down again")]
    )
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        describe=describe, buffer=buffer, frame_provider=lambda: None, interval=0.0
    )
    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        hook._input.publish(_FRAME)
        hook._worker_tick()  # episode 1 fail -> drop
        hook._input.publish(_FRAME)
        hook._worker_tick()  # success -> resets the episode latch (+ publishes a cue)
        hook._input.publish(_FRAME)
        hook._worker_tick()  # episode 2 fail -> a NEW drop

    assert len(_drop_records(caplog)) == 2


# ---------------------------------------------------------------------------
# 5. fault isolation + on_tick contract + frame_provider requirement
# ---------------------------------------------------------------------------


def test_raising_buffer_does_not_break_call() -> None:
    hook = _stopped_hook(
        describe=_FakeDescribe("scene"), buffer=_RaisingBuffer(), frame_provider=lambda: None
    )
    hook._output.publish("a scene")
    hook(None, None, t=0.0)  # feed_scene raises -> swallowed, no traceback


def test_raising_frame_provider_does_not_break_call() -> None:
    def _boom():
        raise RuntimeError("provider exploded")

    hook = _stopped_hook(describe=_FakeDescribe("scene"), frame_provider=_boom)
    hook(None, None, t=0.0)  # provider raises -> swallowed


def test_no_buffer_is_a_silent_noop() -> None:
    hook = _stopped_hook(describe=_FakeDescribe("scene"), buffer=None, frame_provider=lambda: None)
    hook._output.publish("a scene")
    hook(None, None, t=0.0)  # no buffer -> nothing to feed, no crash


def test_on_tick_signature_accepts_commanded_head() -> None:
    hook = _stopped_hook(describe=_FakeDescribe("scene"), frame_provider=lambda: None)
    hook(None, None, 0.0, {"pitch": 0.0, "yaw": 0.0})  # 4-arg on_tick contract, no raise


def test_frame_provider_is_required() -> None:
    describe = _FakeDescribe("scene")
    with pytest.raises(ValueError):
        SceneHook(describe=describe, frame_provider=None)


# ---------------------------------------------------------------------------
# 6. hung describe never blocks the tick thread
# ---------------------------------------------------------------------------


def test_hung_describe_does_not_block_the_tick() -> None:
    release = threading.Event()
    started = threading.Event()

    def _hang(_frame):
        started.set()
        release.wait(2.0)
        return "late"

    buffer = _FakeBuffer()
    hook = SceneHook(describe=_hang, buffer=buffer, frame_provider=lambda: _FRAME, interval=0.0)
    try:
        # The tick keeps returning promptly even while the worker is stuck in _hang.
        for _ in range(5):
            hook(None, None, t=0.0)
            time.sleep(0.01)
        assert started.wait(1.0), "the worker must have entered the (blocking) describe"
        # While describe hangs, no cue is fed — the tick path is unaffected.
        assert buffer.calls == []
    finally:
        release.set()
        hook.close()


# ---------------------------------------------------------------------------
# 7. end-to-end through the real background worker thread
# ---------------------------------------------------------------------------


def test_frame_flows_through_worker_to_a_cue() -> None:
    describe = _FakeDescribe("a person at a desk")
    buffer = _FakeBuffer()
    hook = SceneHook(describe=describe, buffer=buffer, frame_provider=lambda: _FRAME, interval=0.0)
    try:
        assert _wait_until(lambda: (hook(None, None, t=0.0) or True) and bool(buffer.calls))
    finally:
        hook.close()
    assert buffer.calls == ["a person at a desk"]


def test_close_joins_worker_and_is_idempotent() -> None:
    hook = SceneHook(describe=_FakeDescribe("scene"), frame_provider=lambda: None)
    assert hook._worker.is_alive()
    hook.close()
    assert _wait_until(lambda: not hook._worker.is_alive())
    hook.close()  # second close is a safe no-op


def test_hook_holds_no_transport_and_spawns_exactly_one_thread() -> None:
    """SceneHook reuses VisionHook's frames — it opens no camera / second grabber."""
    before = threading.active_count()
    hook = SceneHook(describe=_FakeDescribe("scene"), frame_provider=lambda: None)
    try:
        assert threading.active_count() == before + 1  # exactly ONE new thread (the worker)
        assert not hasattr(hook, "_transport")
    finally:
        hook.close()


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-v"]))
