"""Tests for ``reachy.behavior.clip_rider`` — the rolling video-clip rider (t5).

Task t5 of ``docs/plans/2026-08-01-embodiment-layer.md`` (spec claim c18):
"Seeing = face-name + ``frame_available`` (already on the bus) PLUS a rolling
video clip of the last X seconds handed to the worker model ... the clip moves
out-of-band with a text reference on the bus, per the bus
``is_text_reference_only`` design."

The two acceptance criteria, and the tests that carry them:

1. A ``face_sense``-style background worker keeps the ring — ZERO encoding on
   the tick thread (:func:`test_offer_never_calls_the_encoder_synchronously`,
   :func:`test_encoding_happens_on_a_different_thread_than_offer`); X
   (``clip_seconds``) is configurable with a shipped default
   (:func:`test_default_clip_seconds_is_positive`,
   :func:`test_clip_seconds_is_configurable_via_constructor_and_env`);
   retention is bounded, both in memory (:func:`test_ring_evicts_frames_older_
   than_clip_seconds`, :func:`test_ring_is_hard_capped_independent_of_time_
   eviction`) and on disk (:func:`test_repeated_successful_encodes_never_grow_
   the_clip_directory`).
2. The bus carries ONLY a path reference
   (:func:`test_the_published_block_is_always_a_text_reference_only_payload`,
   :func:`test_a_successful_encode_reaches_state_json_as_a_text_reference`); a
   missing ``[vision]`` extra degrades to one logged warning and a
   permanently-quiet rider, never a crash
   (:func:`test_build_clip_encoder_without_cv2_returns_none_and_warns_once`,
   :func:`test_a_rider_without_an_encoder_spawns_no_worker_thread`,
   :func:`test_a_rider_without_an_encoder_never_raises_and_names_its_reason`).

Everything here runs WITHOUT the ``[vision]`` extra: cv2 is not installed in
this environment, and none of these tests may require it — the encoder is
always an injected fake (mirroring ``tests/test_behavior_face_sense.py``'s
engine/store fakes), except the two tests that exercise the real,
cv2-less :func:`~reachy.behavior.clip_rider.build_clip_encoder` probe directly.
"""

from __future__ import annotations

import inspect
import json
import logging
import threading
import time

import numpy as np
import pytest

from reachy.behavior import clip_rider as CR
from reachy.behavior import control as control_mod
from reachy.behavior.clip_rider import ClipRider, build_clip_encoder, clip_unavailable_reason
from reachy.export.mqtt import is_text_reference_only

# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _restore_vision_latch():
    """Save/restore the process-wide ``_VISION_WARNED`` latch around each test."""
    saved = CR._VISION_WARNED
    yield
    CR._VISION_WARNED = saved


def _frame(width: int = 4, height: int = 3) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class _StepClock:
    """A deterministic, manually-advanced clock (never wall time)."""

    def __init__(self, start: float = 0.0) -> None:
        self.t = start

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> float:
        self.t += dt
        return self.t


class _FakeEncoder:
    """A ``build_clip_encoder()``-shaped callable recording every invocation.

    ``result`` may be ``True`` / ``False`` / an exception INSTANCE to raise.
    When ``write_bytes`` is set, a successful call actually writes that many
    bytes to *path* — so retention tests can observe real files on disk without
    needing cv2.
    """

    def __init__(self, result: object = True, *, write_bytes: int | None = 16) -> None:
        self.result = result
        self.write_bytes = write_bytes
        self.calls: list[tuple[int, float]] = []  # (frame_count, fps)
        self.threads: list[int] = []
        self.ran = threading.Event()

    def __call__(self, frames, fps, path):
        self.threads.append(threading.get_ident())
        self.calls.append((len(frames), fps))
        self.ran.set()
        if isinstance(self.result, BaseException):
            raise self.result
        if self.result and self.write_bytes is not None:
            path.write_bytes(b"\x00" * self.write_bytes)
        return bool(self.result)


def _rider(tmp_path, *, encoder=None, start_worker: bool = False, **kwargs) -> ClipRider:
    return ClipRider(
        encoder=encoder,
        root=tmp_path,
        start_worker=start_worker,
        **kwargs,
    )


def _wait(predicate, *, timeout: float = 5.0, interval: float = 0.005) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# 1a. offer() is O(1) and never encodes on the caller's thread                #
# --------------------------------------------------------------------------- #


def test_offer_never_calls_the_encoder_synchronously(tmp_path) -> None:
    """Criterion 1: the tick-thread handoff never touches the encoder itself."""
    encoder = _FakeEncoder()
    rider = _rider(tmp_path, encoder=encoder, start_worker=False)
    try:
        for _ in range(50):
            rider.offer(_frame())
        assert encoder.calls == [], "offer() must never encode — that is the worker's job"
    finally:
        rider.close()


def test_offer_pushes_into_a_bounded_inbox_dropping_oldest_when_full(tmp_path) -> None:
    encoder = _FakeEncoder()
    rider = _rider(tmp_path, encoder=encoder, start_worker=False, inbox_size=4)
    try:
        for _ in range(10):
            rider.offer(_frame())
        assert rider.inbox_dropped == 6, rider.inbox_dropped
        assert len(rider._inbox) == 4
    finally:
        rider.close()


def test_worker_tick_drains_the_inbox_into_the_ring(tmp_path) -> None:
    clock = _StepClock()
    encoder = _FakeEncoder()
    rider = _rider(tmp_path, encoder=encoder, start_worker=False, clock=clock)
    try:
        for _ in range(5):
            rider.offer(_frame())
        rider._drain_inbox()
        assert rider.ring_size == 5
        assert len(rider._inbox) == 0
    finally:
        rider.close()


def test_encoding_happens_on_a_different_thread_than_offer(tmp_path) -> None:
    """The heavy leg runs on the background worker, never the calling thread."""
    encoder = _FakeEncoder()
    rider = _rider(
        tmp_path, encoder=encoder, start_worker=True, encode_interval_s=0.0, clip_seconds=10.0
    )
    caller_thread = threading.get_ident()
    try:
        for i in range(3):
            rider.offer(_frame(width=4 + i))
            time.sleep(0.01)
        assert _wait(lambda: encoder.ran.is_set()), "the encoder was never invoked"
    finally:
        rider.close()
    assert encoder.threads, "no encode call was recorded"
    assert all(
        t != caller_thread for t in encoder.threads
    ), "the encoder ran on the offer()-calling thread — encoding leaked onto the tick thread"


# --------------------------------------------------------------------------- #
# 1b. X (clip_seconds) — configurable, with a shipped default                 #
# --------------------------------------------------------------------------- #


def test_default_clip_seconds_is_positive() -> None:
    assert CR.DEFAULT_CLIP_SECONDS > 0


def test_clip_seconds_is_configurable_via_constructor_and_env(tmp_path, monkeypatch) -> None:
    rider = _rider(tmp_path, encoder=_FakeEncoder(), clip_seconds=2.5)
    try:
        assert rider._clip_seconds == 2.5
    finally:
        rider.close()

    monkeypatch.delenv(CR.CLIP_SECONDS_ENV, raising=False)
    assert CR.clip_seconds_from_env() == CR.DEFAULT_CLIP_SECONDS

    monkeypatch.setenv(CR.CLIP_SECONDS_ENV, "9.5")
    assert CR.clip_seconds_from_env() == 9.5

    monkeypatch.setenv(CR.CLIP_SECONDS_ENV, "not-a-number")
    assert CR.clip_seconds_from_env() == CR.DEFAULT_CLIP_SECONDS

    monkeypatch.setenv(CR.CLIP_SECONDS_ENV, "-3")
    assert CR.clip_seconds_from_env() == CR.DEFAULT_CLIP_SECONDS


# --------------------------------------------------------------------------- #
# 1c. Bounded retention — the ring (memory)                                   #
# --------------------------------------------------------------------------- #


def test_ring_evicts_frames_older_than_clip_seconds(tmp_path) -> None:
    clock = _StepClock()
    rider = _rider(tmp_path, encoder=_FakeEncoder(), clip_seconds=1.0, clock=clock)
    try:
        rider.offer(_frame())  # t=0.0
        clock.advance(0.5)
        rider.offer(_frame())  # t=0.5
        clock.advance(0.6)  # now=1.1: the t=0.0 frame is 1.1s old, outside the 1.0s window
        rider._drain_inbox()
        rider._evict_old(clock())
        assert rider.ring_size == 1, "the stale frame was not evicted"
    finally:
        rider.close()


def test_ring_is_hard_capped_independent_of_time_eviction(tmp_path) -> None:
    """A burst of same-timestamp frames must not grow the ring unboundedly."""
    clock = _StepClock()
    rider = _rider(
        tmp_path, encoder=_FakeEncoder(), clip_seconds=100.0, max_ring_frames=8, clock=clock
    )
    try:
        for _ in range(500):
            rider.offer(_frame())
            rider._drain_inbox()  # drain promptly so the bounded inbox never masks this
        assert rider.ring_size <= 8, rider.ring_size
    finally:
        rider.close()


# --------------------------------------------------------------------------- #
# 1d. Bounded retention — the clip FILE (disk)                                #
# --------------------------------------------------------------------------- #


def test_repeated_successful_encodes_never_grow_the_clip_directory(tmp_path) -> None:
    """Overwrite-in-place: N successful encode cycles leave exactly ONE file."""
    clock = _StepClock()
    encoder = _FakeEncoder(write_bytes=32)
    rider = _rider(
        tmp_path,
        encoder=encoder,
        start_worker=False,
        clock=clock,
        clip_seconds=5.0,
        encode_interval_s=1.0,
    )
    clip_dir = control_mod.behavior_dir(tmp_path)
    try:
        for cycle in range(6):
            for _ in range(3):
                rider.offer(_frame())
            clock.advance(1.1)  # clear the encode-interval cadence gate each cycle
            rider._worker_tick()
            files = sorted(p.name for p in clip_dir.iterdir() if not p.is_dir())
            assert files == [CR.DEFAULT_CLIP_FILENAME], (cycle, files)
        assert len(encoder.calls) == 6, "the encoder was not invoked once per cycle"
    finally:
        rider.close()


def test_a_failing_encode_leaves_no_temp_file_and_does_not_crash(tmp_path, caplog) -> None:
    encoder = _FakeEncoder(result=False)
    rider = _rider(tmp_path, encoder=encoder, start_worker=False, encode_interval_s=0.0)
    clip_dir = control_mod.behavior_dir(tmp_path)
    try:
        rider.offer(_frame())
        rider.offer(_frame())
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            rider._worker_tick()  # must not raise
        assert list(clip_dir.iterdir()) == [], "a refused encode must leave nothing behind"
        drops = [r.getMessage() for r in caplog.records if "dropped reason=" in r.getMessage()]
        assert any("encode-refused" in line for line in drops), drops
    finally:
        rider.close()


def test_an_encoder_that_raises_is_caught_named_and_does_not_crash(tmp_path, caplog) -> None:
    encoder = _FakeEncoder(result=RuntimeError("cv2 blew up"))
    rider = _rider(tmp_path, encoder=encoder, start_worker=False, encode_interval_s=0.0)
    clip_dir = control_mod.behavior_dir(tmp_path)
    try:
        rider.offer(_frame())
        rider.offer(_frame())
        with caplog.at_level(logging.INFO, logger="reachy.sense"):
            rider._worker_tick()  # must not raise
        assert list(clip_dir.iterdir()) == [], "a raising encode must leave nothing behind"
        drops = [r.getMessage() for r in caplog.records if "dropped reason=" in r.getMessage()]
        assert any("encode-raised" in line for line in drops), drops
    finally:
        rider.close()


def test_encode_is_cadence_gated(tmp_path) -> None:
    clock = _StepClock()
    encoder = _FakeEncoder()
    rider = _rider(
        tmp_path, encoder=encoder, start_worker=False, clock=clock, encode_interval_s=5.0
    )
    try:
        rider.offer(_frame())
        rider.offer(_frame())
        rider._worker_tick()
        assert len(encoder.calls) == 1
        clock.advance(1.0)  # under the 5s cadence
        rider.offer(_frame())
        rider._worker_tick()
        assert len(encoder.calls) == 1, "re-encoded before the cadence interval elapsed"
        clock.advance(5.0)
        rider._worker_tick()
        assert len(encoder.calls) == 2
    finally:
        rider.close()


def test_no_encode_attempt_with_too_few_frames(tmp_path) -> None:
    encoder = _FakeEncoder()
    rider = _rider(tmp_path, encoder=encoder, start_worker=False, encode_interval_s=0.0)
    try:
        rider.offer(_frame())  # exactly one frame: no duration, nothing to encode
        rider._worker_tick()
        assert encoder.calls == []
    finally:
        rider.close()


# --------------------------------------------------------------------------- #
# 2a. The bus carries ONLY a path reference                                   #
# --------------------------------------------------------------------------- #


def test_the_published_block_is_always_a_text_reference_only_payload(tmp_path) -> None:
    for encoder in (None, _FakeEncoder()):
        rider = _rider(tmp_path, encoder=encoder, start_worker=False)
        try:
            assert is_text_reference_only(rider.block())
        finally:
            rider.close()


def test_a_successful_encode_reaches_state_json_as_a_text_reference(tmp_path) -> None:
    clock = _StepClock()
    encoder = _FakeEncoder(write_bytes=8)
    rider = _rider(
        tmp_path, encoder=encoder, start_worker=False, clock=clock, encode_interval_s=0.0
    )
    try:
        rider.offer(_frame())
        rider.offer(_frame())
        rider._worker_tick()
        rider(None)  # the TickBus driver entry: publish into state.json

        state = json.loads(control_mod.state_file(root=tmp_path).read_text(encoding="utf-8"))
        clip_block = state[CR.STATE_KEY]
        assert clip_block["available"] is True
        assert clip_block["reason"] is None
        assert clip_block["path"].endswith(CR.DEFAULT_CLIP_FILENAME)
        assert clip_block["frame_count"] == 2
        assert is_text_reference_only(clip_block)
        assert is_text_reference_only(state)
    finally:
        rider.close()


def test_block_before_any_encode_names_no_clip_yet(tmp_path) -> None:
    rider = _rider(tmp_path, encoder=_FakeEncoder())
    try:
        block = rider.block()
        assert block["available"] is False
        assert block["reason"] == CR.REASON_NO_CLIP_YET
        assert block["path"] is None
    finally:
        rider.close()


def test_state_is_written_only_on_change(tmp_path) -> None:
    """Mirrors ``SenseAvailabilityDriver``'s write-only-on-change discipline."""
    rider = _rider(tmp_path, encoder=_FakeEncoder())
    spool = control_mod.CommandSpool(root=tmp_path)
    writes = []
    original = spool.write_state

    def _counting_write(state):
        writes.append(state)
        original(state)

    monkeypatch_target = rider._main
    monkeypatch_target.write_state = _counting_write
    try:
        rider(None)
        rider(None)
        rider(None)
        assert len(writes) == 1, "republished with nothing changed"
    finally:
        rider.close()


# --------------------------------------------------------------------------- #
# 2b. Missing [vision] extra: one warning, permanently quiet, never a crash   #
# --------------------------------------------------------------------------- #


def test_build_clip_encoder_without_cv2_returns_none_and_warns_once(monkeypatch, caplog) -> None:
    monkeypatch.setattr(CR, "_VISION_WARNED", False)
    monkeypatch.setattr(CR.face_sense, "_find_spec", lambda name: None)

    with caplog.at_level(logging.WARNING, logger="reachy.behavior.clip_rider"):
        first = build_clip_encoder()
        second = build_clip_encoder()

    assert first is None and second is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "[vision]" in warnings[0].getMessage()


def test_build_clip_encoder_on_this_environment_never_raises() -> None:
    """The real probe, unmocked: cv2 is genuinely absent here, and that is fine."""
    result = build_clip_encoder()
    assert result is None or callable(result)


def test_clip_unavailable_reason_precedence(monkeypatch) -> None:
    assert (
        clip_unavailable_reason(False, find_spec=lambda n: None)
        == CR.face_sense.VISION_EXTRA_ABSENT
    )
    assert (
        clip_unavailable_reason(False, find_spec=lambda n: object())
        == CR.face_sense.VISION_STACK_UNAVAILABLE
    )
    assert clip_unavailable_reason(True, find_spec=lambda n: object()) is None


def test_a_rider_without_an_encoder_spawns_no_worker_thread(tmp_path) -> None:
    before = threading.active_count()
    rider = _rider(tmp_path, encoder=None, start_worker=True)
    try:
        assert threading.active_count() == before
        assert rider.worker_alive is False
    finally:
        rider.close()


def test_a_rider_without_an_encoder_never_raises_and_names_its_reason(
    tmp_path, monkeypatch
) -> None:
    monkeypatch.setattr(CR.face_sense, "_find_spec", lambda name: None)
    rider = _rider(tmp_path, encoder=None, start_worker=True)
    try:
        for _ in range(5):
            rider.offer(_frame())  # must be a harmless no-op
            rider(None)  # the tick driver entry — must never raise
        block = rider.block()
        assert block["available"] is False
        assert block["reason"] == CR.face_sense.VISION_EXTRA_ABSENT
        assert rider.inbox_dropped == 0, "a disabled rider must not even queue frames"
    finally:
        rider.close()


def test_close_is_idempotent_and_stops_the_worker(tmp_path) -> None:
    encoder = _FakeEncoder()
    rider = _rider(tmp_path, encoder=encoder, start_worker=True)
    rider.offer(_frame())
    rider.close()
    rider.close()  # must not raise
    assert rider.worker_alive is False


def test_module_does_not_import_cv2_or_reachy_mini_at_module_scope() -> None:
    """cv2 IS imported, but only lazily inside ``build_clip_encoder`` — never
    at column 0, which is what would make the module unimportable without the
    ``[vision]`` extra."""
    for line in inspect.getsource(CR).splitlines():
        if line.startswith(("import ", "from ")):  # unindented: true module scope
            assert "cv2" not in line, f"clip_rider must not import cv2 at module scope: {line}"
            assert (
                "reachy_mini" not in line
            ), f"clip_rider must not import reachy_mini at module scope: {line}"
