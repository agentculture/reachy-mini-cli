"""Tests for folding face recognition into the ``listen`` sdk loop (task t9).

``listen`` owns the single-consumer SDK client (and its one camera). A *separate*
face-recognition process would contend for that one media/camera subsystem and get
throttled — the same single-SDK-owner constraint that motivated folding pat (#43)
and vision (t7) into ``listen``'s loop. So face detection is folded the same way:
:class:`~reachy.motion.listen_face.FaceHook` is a per-tick ``on_tick`` hook
``(transport, queue, t, commanded_head) -> None``.

Two design points these tests pin down:

* **No second frame grabber.** ``FaceHook`` never opens a camera or spawns a frame
  grabber of its own — it takes a ``frame_provider`` callable (the shared,
  non-consuming latest-frame peek exposed by
  :class:`~reachy.motion.listen_vision.VisionHook`). The per-tick ``__call__`` only
  publishes the latest frame to a background DETECTION worker and drains completed
  results; the heavy YuNet+SFace detect/embed runs off the tick thread.
* **Bounded cadence + per-name re-announce cooldown.** The worker detects at most
  once per ``detect_interval`` (default 0.5 s, on an injectable clock); a matched
  known face is announced to cognition (``buffer.feed_face(name)``) at most once per
  ``reannounce_cooldown`` (default 30 s, keyed on the loop clock ``t``). Unknown /
  unnamed faces never produce a name cue.

Every hook seam degrades silently (a raising engine / store / buffer / provider
never kills the loop) and :meth:`close` joins the worker under a bounded timeout —
mirroring :class:`~reachy.motion.listen_pat.PatHook` /
:class:`~reachy.motion.listen_vision.VisionHook`.

No robot, no daemon, no network, no real camera, and (crucially) **no cv2** — the
engine and store are injected fakes, so this suite runs green with or without the
``[vision]`` extra installed.
"""

from __future__ import annotations

import logging
import re
import threading
import time

import numpy as np
import pytest

from reachy.motion.listen_face import (
    DEFAULT_DETECT_INTERVAL,
    DEFAULT_REANNOUNCE_COOLDOWN,
    FaceHook,
)
from reachy.vision.face import FaceDetection
from reachy.vision.face_store import FaceMatch

_FRAME = np.zeros((8, 8, 3), dtype=np.uint8)

_SENSE_LOGGER_NAME = "reachy.sense"
_SENSE_LINE_RE = re.compile(
    r"^\[SENSE stage=(?P<stage>\S+) source=(?P<source>\S+) event=(?P<event>\S+)\] "
    r"(?P<detail>.*)$"
)


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


def _detection(embedding: np.ndarray | None = None) -> FaceDetection:
    if embedding is None:
        embedding = np.arange(128, dtype=float)
    return FaceDetection(bbox_norm=(0.1, 0.1, 0.4, 0.4), embedding=embedding)


class _FakeEngine:
    """A stand-in for :class:`~reachy.vision.face.FaceEngine` (no cv2)."""

    def __init__(self, detection: FaceDetection | None = None, raises: bool = False) -> None:
        self._detection = detection
        self._raises = raises
        self.calls = 0

    def detect(self, frame: object) -> FaceDetection | None:  # noqa: ARG002
        self.calls += 1
        if self._raises:
            raise RuntimeError("engine boom")
        return self._detection


class _FakeStore:
    """A stand-in for :class:`~reachy.vision.face_store.FaceStore` (no cv2, no disk)."""

    def __init__(self, match: FaceMatch | None = None, raises: bool = False) -> None:
        self._match = match
        self._raises = raises
        self.match_calls = 0
        self.enrolled: list[tuple[str, np.ndarray]] = []

    def match(self, embedding: np.ndarray):  # noqa: ANN201
        self.match_calls += 1
        if self._raises:
            raise RuntimeError("store boom")
        return self._match

    def enroll(
        self, name: str, embedding: np.ndarray, *, now: float | None = None
    ) -> str:  # noqa: ARG002
        self.enrolled.append((name, embedding))
        return "id123"


class _FakeBuffer:
    """Records every ``feed_face(name)`` call."""

    def __init__(self) -> None:
        self.calls: list[str] = []

    def feed_face(self, name: str) -> None:
        self.calls.append(name)


class _RaisingBuffer:
    def feed_face(self, name: str) -> None:
        raise RuntimeError("cognition buffer exploded")


class _FakeClock:
    """A mutable clock; ``clock.now`` is the value returned by calling it."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now

    def __call__(self) -> float:
        return self.now


def _match(name: str) -> FaceMatch:
    return FaceMatch(face_id="fid", name=name, score=0.9)


def _wait_until(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.005)
    return predicate()


def _stopped_hook(**kwargs) -> FaceHook:
    """A FaceHook with its worker stopped, for deterministic synchronous driving.

    Constructing a hook starts the detection worker thread. The synchronous
    unit tests (``_detect_once`` / ``_worker_tick`` / ``__call__`` cooldown) drive
    those methods directly, so we stop the worker first to remove any concurrency.
    With no frame published before ``close()`` the worker never touched state, so
    the hook is a clean, fully-deterministic object afterward.
    """
    hook = FaceHook(**kwargs)
    hook.close()
    return hook


# ---------------------------------------------------------------------------
# 1. detection core: known / unknown / unnamed / no-face / faults
# ---------------------------------------------------------------------------


def test_detect_once_returns_name_for_known_face():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) == "Ada"


def test_detect_once_unknown_face_returns_none():
    """A face with no store match is never announced by name."""
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()), store=_FakeStore(match=None), frame_provider=lambda: None
    )
    assert hook._detect_once(_FRAME) is None


def test_detect_once_no_face_in_frame_returns_none():
    hook = _stopped_hook(
        engine=_FakeEngine(detection=None),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) is None


def test_detect_once_empty_name_match_returns_none():
    """A match whose name is empty/whitespace is not a name cue."""
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("   ")),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) is None


def test_detect_once_strips_name():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("  Ada  ")),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) == "Ada"


def test_raising_engine_degrades_to_none():
    hook = _stopped_hook(
        engine=_FakeEngine(raises=True),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) is None  # no raise


def test_raising_store_degrades_to_none():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(raises=True),
        frame_provider=lambda: None,
    )
    assert hook._detect_once(_FRAME) is None  # no raise


# ---------------------------------------------------------------------------
# 2. bounded detection cadence (default 0.5 s, injectable clock)
# ---------------------------------------------------------------------------


def test_worker_detects_at_most_once_per_interval():
    clock = _FakeClock(100.0)
    engine = _FakeEngine(_detection())
    hook = _stopped_hook(
        engine=engine, store=_FakeStore(_match("Ada")), frame_provider=lambda: None, clock=clock
    )

    hook._input.publish(_FRAME)
    hook._worker_tick()  # first detection
    assert engine.calls == 1

    hook._input.publish(_FRAME)
    hook._worker_tick()  # same clock -> cadence not elapsed -> no detection
    assert engine.calls == 1

    clock.now = 100.0 + DEFAULT_DETECT_INTERVAL + 0.01  # past the interval
    hook._input.publish(_FRAME)
    hook._worker_tick()  # cadence elapsed -> a fresh detection
    assert engine.calls == 2


def test_worker_tick_with_no_frame_is_a_noop():
    engine = _FakeEngine(_detection())
    hook = _stopped_hook(
        engine=engine, store=_FakeStore(_match("Ada")), frame_provider=lambda: None
    )
    hook._worker_tick()  # nothing published -> no detection
    assert engine.calls == 0


# ---------------------------------------------------------------------------
# 3. per-name re-announce cooldown (default 30 s, keyed on the loop clock t)
# ---------------------------------------------------------------------------


def test_reannounce_cooldown_suppresses_repeat_within_window():
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        buffer=buffer,
        frame_provider=lambda: None,
    )

    hook._output.publish("Ada")
    hook(None, None, t=0.0)  # first announce
    assert buffer.calls == ["Ada"]

    hook._output.publish("Ada")
    hook(None, None, t=10.0)  # inside the 30 s window -> suppressed
    assert buffer.calls == ["Ada"]

    hook._output.publish("Ada")
    hook(None, None, t=DEFAULT_REANNOUNCE_COOLDOWN + 1.0)  # window elapsed -> announce again
    assert buffer.calls == ["Ada", "Ada"]


def test_reannounce_cooldown_is_per_name():
    """A different name is not blocked by another name's cooldown window."""
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        buffer=buffer,
        frame_provider=lambda: None,
    )

    hook._output.publish("Ada")
    hook(None, None, t=0.0)
    hook._output.publish("Bo")
    hook(None, None, t=1.0)  # different name, no prior -> announced immediately
    assert buffer.calls == ["Ada", "Bo"]


def test_cooldown_drop_emits_senselog_line(caplog):
    buffer = _FakeBuffer()
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        buffer=buffer,
        frame_provider=lambda: None,
    )
    hook._output.publish("Ada")
    hook(None, None, t=0.0)
    with caplog.at_level(logging.INFO, logger=_SENSE_LOGGER_NAME):
        hook._output.publish("Ada")
        hook(None, None, t=5.0)  # suppressed -> a drop line
    records = [r for r in caplog.records if r.name == _SENSE_LOGGER_NAME]
    assert len(records) == 1
    match = _SENSE_LINE_RE.match(records[0].getMessage())
    assert match is not None
    assert match.group("source") == "face"
    assert "cooldown" in match.group("detail")


# ---------------------------------------------------------------------------
# 4. fault isolation + on_tick contract
# ---------------------------------------------------------------------------


def test_raising_buffer_does_not_break_call():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        buffer=_RaisingBuffer(),
        frame_provider=lambda: None,
    )
    hook._output.publish("Ada")
    hook(None, None, t=0.0)  # feed_face raises -> swallowed, no traceback


def test_raising_frame_provider_does_not_break_call():
    def _boom():
        raise RuntimeError("provider exploded")

    hook = _stopped_hook(
        engine=_FakeEngine(_detection()), store=_FakeStore(_match("Ada")), frame_provider=_boom
    )
    hook(None, None, t=0.0)  # provider raises -> swallowed


def test_no_buffer_is_a_silent_noop():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        buffer=None,
        frame_provider=lambda: None,
    )
    hook._output.publish("Ada")
    hook(None, None, t=0.0)  # no buffer -> nothing to feed, no crash


def test_on_tick_signature_accepts_commanded_head():
    hook = _stopped_hook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    hook(None, None, 0.0, {"pitch": 0.0, "yaw": 0.0})  # 4-arg on_tick contract, no raise


def test_frame_provider_is_required():
    engine = _FakeEngine()
    store = _FakeStore()
    with pytest.raises(ValueError):
        FaceHook(engine=engine, store=store, frame_provider=None)


# ---------------------------------------------------------------------------
# 5. end-to-end through the real background worker thread
# ---------------------------------------------------------------------------


def test_frame_flows_through_worker_to_a_cue():
    """A shared frame → worker detect → match → a single ``feed_face`` cue.

    Drives the REAL worker thread (bounded ``detect_interval=0`` so it detects on
    every poll). ``__call__`` publishes the shared frame and drains the worker's
    result; the cooldown at the fixed ``t=0`` keeps it to one cue.
    """
    engine = _FakeEngine(_detection())
    store = _FakeStore(_match("Ada"))
    buffer = _FakeBuffer()
    hook = FaceHook(
        engine=engine,
        store=store,
        buffer=buffer,
        frame_provider=lambda: _FRAME,
        detect_interval=0.0,
    )
    try:
        assert _wait_until(lambda: (hook(None, None, t=0.0) or True) and bool(buffer.calls))
    finally:
        hook.close()
    assert buffer.calls == ["Ada"]


def test_close_joins_worker_and_is_idempotent():
    hook = FaceHook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    assert hook._worker.is_alive()
    hook.close()
    assert _wait_until(lambda: not hook._worker.is_alive())
    hook.close()  # second close is a safe no-op


def test_worker_never_calls_transport_or_opens_a_grabber():
    """FaceHook holds no transport and spawns exactly one (detection) thread.

    The whole point of the shared ``frame_provider`` is that FaceHook never opens a
    camera / second grabber — it reuses VisionHook's frames. So it keeps no
    transport reference and its only thread is the detection worker.
    """
    before = threading.active_count()
    hook = FaceHook(
        engine=_FakeEngine(_detection()),
        store=_FakeStore(_match("Ada")),
        frame_provider=lambda: None,
    )
    try:
        assert threading.active_count() == before + 1  # exactly ONE new thread (the worker)
        assert not hasattr(hook, "_transport")
    finally:
        hook.close()


# ---------------------------------------------------------------------------
# 6. enrollment seam
# ---------------------------------------------------------------------------


def test_enroll_from_frame_enrolls_the_detected_embedding():
    embedding = np.arange(128, dtype=float)
    engine = _FakeEngine(_detection(embedding))
    store = _FakeStore()
    hook = _stopped_hook(engine=engine, store=store, frame_provider=lambda: _FRAME)
    face_id = hook.enroll_from_frame("Ada")
    assert face_id == "id123"
    assert len(store.enrolled) == 1
    name, emb = store.enrolled[0]
    assert name == "Ada"
    assert np.array_equal(emb, embedding)


def test_enroll_from_frame_uses_provider_when_no_frame_given():
    engine = _FakeEngine(_detection())
    store = _FakeStore()
    hook = _stopped_hook(engine=engine, store=store, frame_provider=lambda: _FRAME)
    hook.enroll_from_frame("Ada")
    assert engine.calls == 1  # the provider frame was detected


def test_enroll_from_frame_no_face_returns_none():
    engine = _FakeEngine(detection=None)
    store = _FakeStore()
    hook = _stopped_hook(engine=engine, store=store, frame_provider=lambda: _FRAME)
    assert hook.enroll_from_frame("Ada") is None
    assert store.enrolled == []
