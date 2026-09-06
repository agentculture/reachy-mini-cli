"""``face`` + ``frame_available`` sense providers for the symbolic behavior runtime.

These tests pin the three acceptance criteria for
:mod:`reachy.behavior.face_sense`:

1. A react rule keyed on ``face`` and a react rule keyed on ``frame_available``
   each FIRE, driven end-to-end through the real seam the runtime uses —
   ``FaceSenseDriver`` -> :func:`reachy.behavior.sense.read_perception` ->
   :class:`reachy.behavior.rule_engine.RuleEngine`. Before this module existed
   both fields were declared on :class:`~reachy.behavior.sense.Sense` with
   nothing feeding them, so such a rule validated and then silently never fired.
2. A missing ``[vision]`` extra (opencv) is ONE logged warning, never a crash:
   the recognizer factory degrades to ``None`` and the driver still runs, still
   reporting ``frame_available`` — only the ``face`` field goes permanently
   quiet.
3. A ``None`` or degenerate frame is SKIPPED, never raised on — the issue #73
   fix shape, where a fresh-client-per-frame path returned ``None`` on every
   read and ``np.asarray(None).shape == ()`` reached the grey/luma conversion
   in :meth:`reachy.vision.motion.MotionDetector._to_grey` and raised
   ``ValueError: Unsupported frame shape: ()``.

Everything here runs WITHOUT the ``[vision]`` extra: cv2 is not installed in
this environment, and none of these tests may require it. The engine/store are
injected fakes, the media client is a fake exposing only ``HeldMediaClient``'s
``frame()`` / ``camera_available`` surface, and the cv2 probe is an injected
seam so the missing-extra path is exercised deterministically either way.
"""

from __future__ import annotations

import ast
import inspect
import logging
import textwrap
import threading

import numpy as np
import pytest

from reachy.behavior import face_sense as FS
from reachy.behavior.face_sense import FaceSenseDriver, build_face_recognition
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import SENSE_FIELDS, RulesConfig
from reachy.behavior.sense import SenseProviders, read_perception

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


def _frame(width: int = 8, height: int = 6) -> np.ndarray:
    """A plausible BGR camera frame (what ``HeldMediaClient.frame()`` returns)."""
    return np.zeros((height, width, 3), dtype=np.uint8)


class _FakeMedia:
    """The narrow ``HeldMediaClient`` surface this driver consumes.

    ``frames`` is a script: each ``frame()`` call pops the next entry (an
    ndarray, ``None``, or an exception INSTANCE to raise), and the last entry
    repeats once the script runs dry — so a test can drive many ticks off a
    one-entry script.
    """

    def __init__(self, frames, *, camera_available: bool = True) -> None:
        self._frames = list(frames)
        self.camera_available = camera_available
        self.calls = 0

    def frame(self):
        self.calls += 1
        value = self._frames[0] if len(self._frames) == 1 else self._frames.pop(0)
        if isinstance(value, BaseException):
            raise value
        return value


class _FakeDetection:
    def __init__(self, embedding) -> None:
        self.embedding = embedding


class _FakeMatch:
    def __init__(self, name) -> None:
        self.name = name


class _FakeEngine:
    """A ``FaceEngine``-shaped detector recording the thread it ran on."""

    def __init__(self, detection=None, *, raises=None) -> None:
        self._detection = detection if detection is not None else _FakeDetection([0.1, 0.2])
        self._raises = raises
        self.frames = []
        self.threads = []
        self.ran = threading.Event()

    def detect(self, frame):
        self.frames.append(frame)
        self.threads.append(threading.get_ident())
        self.ran.set()
        if self._raises is not None:
            raise self._raises
        return self._detection


class _FakeStore:
    """A ``FaceStore``-shaped matcher."""

    def __init__(self, match=None, *, raises=None) -> None:
        self._match = match
        self._raises = raises

    def match(self, embedding):
        if self._raises is not None:
            raise self._raises
        return self._match


class _Ctx:
    """A duck-typed ``TickContext`` — the driver only reads ``now``."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now


def _drive(driver: FaceSenseDriver, *, ticks: int = 1, start: float = 0.0, dt: float = 0.02):
    """Run *ticks* driver invocations on a fixed-step clock; return the last ctx."""
    ctx = _Ctx(start)
    for i in range(ticks):
        ctx = _Ctx(round(start + i * dt, 10))
        driver(ctx)
    return ctx


def _sense(driver: FaceSenseDriver):
    """Compose the tick's Sense exactly as the runtime's composition root will."""
    return read_perception(
        SenseProviders(
            face=driver.as_face_provider(),
            frame_available=driver.as_frame_available_provider(),
        )
    )


def _react(rule_id: str, field: str, op: str, run: str, *, duration_s: float = 60.0) -> dict:
    return {
        "id": rule_id,
        "when": {"field": field, "op": op},
        "run": run,
        "cooldown_s": 5.0,
        "hysteresis": 0.0,
        "duration_s": duration_s,
    }


class _RecordingCtx:
    """A duck-typed ``TickContext`` recording what the rule engine admitted."""

    def __init__(self, now: float, sense) -> None:
        self.now = now
        self.tick = 1
        self.sense = sense
        self.ownership: dict = {}
        self.admits: list = []
        self.evicts: list = []
        self.events: list = []

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior):
        self.admits.append(behavior)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str):
        self.evicts.append(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set()


# --------------------------------------------------------------------------- #
# Criterion 1 — a rule keyed on face, and one keyed on frame_available, FIRE  #
# --------------------------------------------------------------------------- #


def test_face_is_a_valid_rule_field_and_frame_available_is_too() -> None:
    """Both fields must be addressable by a predicate, or no rule can key on them."""
    assert "face" in SENSE_FIELDS
    assert "frame_available" in SENSE_FIELDS


def test_face_rule_fires_end_to_end_from_the_driver() -> None:
    """Criterion 1a: driver -> read_perception -> RuleEngine admits the behavior."""
    media = _FakeMedia([_frame()])
    engine = _FakeEngine()
    driver = FaceSenseDriver(
        media=media,
        engine=engine,
        store=_FakeStore(match=_FakeMatch("ada")),
        start_worker=False,
    )
    # tick 1: the tick thread publishes the frame for the worker
    _drive(driver, ticks=1, start=0.0)
    driver._worker_tick()  # the heavy leg, run deterministically
    # tick 2: the tick thread drains the worker's result and latches it
    _drive(driver, ticks=1, start=0.02)

    sense = _sense(driver)
    assert sense.face == "ada"

    rules = RulesConfig.from_dict({"react": [_react("greet", "face", "is_true", "nod")]})
    rule_engine = RuleEngine(rules)
    ctx = _RecordingCtx(now=0.02, sense=sense)
    rule_engine.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["nod"]


def test_frame_available_rule_fires_end_to_end_from_the_driver() -> None:
    """Criterion 1b: the same path, keyed on the frame-availability condition."""
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), start_worker=False)
    _drive(driver, ticks=1)

    sense = _sense(driver)
    assert sense.frame_available is True

    rules = RulesConfig.from_dict({"react": [_react("look", "frame_available", "is_true", "nod")]})
    rule_engine = RuleEngine(rules)
    ctx = _RecordingCtx(now=0.0, sense=sense)
    rule_engine.on_tick(ctx)

    assert [b.name for b in ctx.admits] == ["nod"]


def test_frame_available_rule_does_not_fire_without_frames() -> None:
    """The negative half of criterion 1b — no frames means no fire."""
    driver = FaceSenseDriver(media=_FakeMedia([None]), start_worker=False)
    _drive(driver, ticks=3)

    sense = _sense(driver)
    assert sense.frame_available is False

    rules = RulesConfig.from_dict({"react": [_react("look", "frame_available", "is_true", "nod")]})
    ctx = _RecordingCtx(now=0.0, sense=sense)
    RuleEngine(rules).on_tick(ctx)

    assert ctx.admits == []


# --------------------------------------------------------------------------- #
# Criterion 2 — a missing [vision] extra is ONE logged warning, not a crash   #
# --------------------------------------------------------------------------- #


def test_build_face_recognition_without_cv2_returns_none_and_warns_once(
    monkeypatch, caplog
) -> None:
    """Criterion 2: absent opencv degrades to ``None`` after exactly one warning."""
    monkeypatch.setattr(FS, "_VISION_WARNED", False)
    monkeypatch.setattr(FS, "_find_spec", lambda name: None)

    with caplog.at_level(logging.WARNING, logger="reachy.behavior.face_sense"):
        first = build_face_recognition()
        second = build_face_recognition()
        third = build_face_recognition()

    assert first is None
    assert second is None
    assert third is None
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]
    assert "[vision]" in warnings[0].getMessage()


def test_build_face_recognition_on_this_environment_never_raises() -> None:
    """The real probe, unmocked: cv2 is genuinely absent here, and that is fine."""
    result = build_face_recognition()
    assert result is None or (isinstance(result, tuple) and len(result) == 2)


def test_driver_without_a_recognizer_still_reports_frame_available() -> None:
    """A cv2-less box keeps the frame condition; only ``face`` goes quiet."""
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), engine=None, store=None)
    try:
        _drive(driver, ticks=3)
        sense = _sense(driver)
        assert sense.frame_available is True
        assert sense.face is None
    finally:
        driver.close()


def test_driver_without_a_recognizer_starts_no_worker_thread() -> None:
    """No engine means no heavy leg to run — so no thread is spawned at all."""
    before = threading.active_count()
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), engine=None, store=None)
    try:
        assert threading.active_count() == before
        assert driver.worker_alive is False
    finally:
        driver.close()


def test_module_does_not_import_cv2_at_module_scope() -> None:
    """The provider module must stay importable on a bare install."""
    for line in inspect.getsource(FS).splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "cv2" not in stripped, f"face_sense must not import cv2: {line}"


# --------------------------------------------------------------------------- #
# Criterion 3 — a None / degenerate frame is skipped, never raised on (#73)   #
# --------------------------------------------------------------------------- #


def test_none_frame_is_skipped_not_raised() -> None:
    """Criterion 3: the ordinary "nothing ready this instant" case is not a fault."""
    media = _FakeMedia([None])
    engine = _FakeEngine()
    driver = FaceSenseDriver(media=media, engine=engine, store=_FakeStore(), start_worker=False)

    _drive(driver, ticks=5)
    driver._worker_tick()

    assert driver.peek_frame_available() is False
    assert driver.peek_face() is None
    assert engine.frames == []  # a None frame NEVER reaches the heavy leg


@pytest.mark.parametrize(
    "degenerate",
    [
        pytest.param(np.asarray(None), id="zero-d-object-array"),  # the #73 shape exactly
        pytest.param(np.zeros((0, 0, 3), dtype=np.uint8), id="empty"),
        pytest.param(np.zeros((4,), dtype=np.uint8), id="one-d"),
        pytest.param(np.zeros((2, 2, 2, 3), dtype=np.uint8), id="four-d"),
        pytest.param(np.zeros((4, 4, 7), dtype=np.uint8), id="bad-channel-count"),
        pytest.param("not-a-frame", id="not-an-array"),
        pytest.param(object(), id="unconvertible"),
    ],
)
def test_degenerate_frame_is_skipped_not_raised(degenerate) -> None:
    """Criterion 3: anything that is not a usable image is dropped BEFORE any consumer.

    ``np.asarray(None)`` is the concrete issue-#73 value: a 0-d object array
    whose ``.shape`` is ``()``. Reaching a luma conversion with it raises
    ``ValueError: Unsupported frame shape: ()``. It must never get that far.
    """
    engine = _FakeEngine()
    driver = FaceSenseDriver(
        media=_FakeMedia([degenerate]),
        engine=engine,
        store=_FakeStore(),
        start_worker=False,
    )

    _drive(driver, ticks=3)
    driver._worker_tick()

    assert driver.peek_frame_available() is False
    assert engine.frames == []


def test_a_raising_frame_read_degrades_to_no_frame() -> None:
    driver = FaceSenseDriver(media=_FakeMedia([RuntimeError("camera exploded")]))
    try:
        _drive(driver, ticks=3)
        assert driver.peek_frame_available() is False
    finally:
        driver.close()


def test_no_camera_short_circuits_without_reading_frames() -> None:
    media = _FakeMedia([_frame()], camera_available=False)
    driver = FaceSenseDriver(media=media, start_worker=False)

    _drive(driver, ticks=3)

    assert driver.peek_frame_available() is False
    assert media.calls == 0  # the pollable predicate is the cheap negative


def test_a_cold_client_is_never_touched_on_the_tick_thread() -> None:
    """``connected`` is the one FREE probe; the others may block on a lazy connect.

    On a cold ``HeldMediaClient`` both ``camera_available`` and ``frame()`` can
    trigger construction of the full media chain — the 425-1213 ms tick-overrun
    class. So a disconnected client must be skipped without touching either.
    """
    touched: list[str] = []

    class _ColdClient:
        connected = False

        @property
        def camera_available(self):
            touched.append("camera_available")
            return True

        def frame(self):
            touched.append("frame")
            return _frame()

    driver = FaceSenseDriver(media=_ColdClient(), start_worker=False)
    _drive(driver, ticks=5)

    assert driver.peek_frame_available() is False
    assert touched == []


def test_a_reconnected_client_resumes_reporting_frames() -> None:
    class _Client:
        def __init__(self) -> None:
            self.connected = False

        def frame(self):
            return _frame()

    media = _Client()
    driver = FaceSenseDriver(media=media, start_worker=False)
    driver(_Ctx(0.0))
    assert driver.peek_frame_available() is False

    media.connected = True
    driver(_Ctx(0.02))
    assert driver.peek_frame_available() is True


def test_a_raising_media_object_never_breaks_the_tick() -> None:
    class _Exploding:
        @property
        def camera_available(self):
            raise RuntimeError("boom")

        def frame(self):
            raise RuntimeError("boom")

    driver = FaceSenseDriver(media=_Exploding(), start_worker=False)
    _drive(driver, ticks=3)
    assert driver.peek_frame_available() is False


def test_a_missing_media_client_is_a_permanent_no_reading() -> None:
    driver = FaceSenseDriver(media=None, start_worker=False)
    _drive(driver, ticks=3)
    assert driver.peek_frame_available() is False
    assert driver.peek_face() is None


# --------------------------------------------------------------------------- #
# frame_available freshness — a condition, not a per-tick event               #
# --------------------------------------------------------------------------- #


def test_frame_available_holds_across_a_frameless_tick_inside_the_ttl() -> None:
    """The camera runs slower than the 50 Hz tick, so the condition is TTL-held."""
    media = _FakeMedia([_frame(), None, None, None])
    driver = FaceSenseDriver(media=media, frame_ttl_s=1.0, start_worker=False)

    _drive(driver, ticks=4, start=0.0, dt=0.02)

    assert driver.peek_frame_available() is True


def test_frame_available_clears_once_the_ttl_lapses() -> None:
    media = _FakeMedia([_frame(), None])
    driver = FaceSenseDriver(media=media, frame_ttl_s=0.5, start_worker=False)

    driver(_Ctx(0.0))
    assert driver.peek_frame_available() is True
    driver(_Ctx(2.0))
    assert driver.peek_frame_available() is False


# --------------------------------------------------------------------------- #
# face latch semantics — one tick, mirroring PatSenseDriver                   #
# --------------------------------------------------------------------------- #


def test_face_latch_lasts_exactly_one_tick() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(match=_FakeMatch("ada")),
        start_worker=False,
    )
    driver(_Ctx(0.0))
    driver._worker_tick()
    driver(_Ctx(0.02))
    assert driver.peek_face() == "ada"
    # A peek is non-consuming: the same tick reads the same value twice.
    assert driver.peek_face() == "ada"
    driver(_Ctx(0.04))
    assert driver.peek_face() is None


def test_a_lingering_face_is_re_announced_at_most_once_per_cooldown() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(match=_FakeMatch("ada")),
        reannounce_cooldown=30.0,
        start_worker=False,
    )
    seen = []
    for i in range(6):
        driver(_Ctx(round(i * 0.02, 10)))
        driver._worker_tick()
        driver._output.publish("ada")  # the worker keeps matching the same face
        driver(_Ctx(round(i * 0.02 + 0.01, 10)))
        seen.append(driver.peek_face())

    assert seen.count("ada") == 1
    assert driver.events == 1


def test_a_different_face_is_announced_despite_another_names_cooldown() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(),
        reannounce_cooldown=30.0,
        start_worker=False,
    )
    driver._output.publish("ada")
    driver(_Ctx(0.0))
    assert driver.peek_face() == "ada"

    driver._output.publish("bo")
    driver(_Ctx(0.02))
    assert driver.peek_face() == "bo"


def test_an_unnamed_or_unmatched_face_never_becomes_a_cue() -> None:
    for store in (_FakeStore(match=None), _FakeStore(match=_FakeMatch("   "))):
        driver = FaceSenseDriver(
            media=_FakeMedia([_frame()]),
            engine=_FakeEngine(),
            store=store,
            start_worker=False,
        )
        driver(_Ctx(0.0))
        driver._worker_tick()
        driver(_Ctx(0.02))
        assert driver.peek_face() is None


def test_a_raising_engine_or_store_degrades_to_no_match() -> None:
    raising_engine = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(raises=RuntimeError("cv2 blew up")),
        store=_FakeStore(match=_FakeMatch("ada")),
        start_worker=False,
    )
    raising_engine(_Ctx(0.0))
    raising_engine._worker_tick()
    raising_engine(_Ctx(0.02))
    assert raising_engine.peek_face() is None


# --------------------------------------------------------------------------- #
# add_frame_sink — the clip rider's frame-handoff seam (t5)                   #
# --------------------------------------------------------------------------- #


def test_add_frame_sink_receives_every_usable_frame() -> None:
    """A registered sink is pushed each usable frame, in order, once per tick."""
    frames = [_frame(width=4), _frame(width=6), _frame(width=8)]
    # The read CADENCE is a separate property (see the frame-interval tests
    # below); this one is about what a sink receives per read, so it reads
    # every tick.
    driver = FaceSenseDriver(
        media=_FakeMedia(list(frames)), engine=None, store=None, frame_interval_s=0.0
    )
    received: list = []
    driver.add_frame_sink(received.append)
    try:
        _drive(driver, ticks=3, dt=0.02)
    finally:
        driver.close()
    assert len(received) == 3
    for got, expected in zip(received, frames):
        assert got is expected, "the sink must receive the SAME frame object (no copy)"


def test_add_frame_sink_is_never_called_with_a_none_or_degenerate_frame() -> None:
    """No frame this tick means no sink call — never a ``None`` push."""
    driver = FaceSenseDriver(media=_FakeMedia([None]), engine=None, store=None)
    received: list = []
    driver.add_frame_sink(received.append)
    try:
        _drive(driver, ticks=3)
    finally:
        driver.close()
    assert received == []


def test_add_frame_sink_runs_regardless_of_recognizer_readiness() -> None:
    """A cv2-less box (no engine/store) still feeds a registered frame sink.

    Reading a raw camera frame needs no cv2 — only face DETECTION does — so a
    consumer with its own reason to want frames (the clip rider) must not go
    quiet just because face recognition itself is unavailable.
    """
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), engine=None, store=None)
    received: list = []
    driver.add_frame_sink(received.append)
    try:
        _drive(driver, ticks=1)
    finally:
        driver.close()
    assert len(received) == 1


def test_a_raising_frame_sink_does_not_break_the_tick_or_other_sinks() -> None:
    """A misbehaving sink degrades to a dropped frame, never a tick fault."""

    def _boom(_frame):
        raise RuntimeError("consumer exploded")

    well_behaved: list = []
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), engine=None, store=None)
    driver.add_frame_sink(_boom)
    driver.add_frame_sink(well_behaved.append)
    try:
        _drive(driver, ticks=1)  # must not raise
        sense = _sense(driver)
    finally:
        driver.close()
    assert sense.frame_available is True, "a raising sink must not break the rest of the tick"
    assert len(well_behaved) == 1, "a sink registered after a raising one must still be fed"


def test_frame_sink_receives_frames_on_the_tick_thread_synchronously() -> None:
    """The push is inline with ``_update_frame`` — no worker involved for the sink itself."""
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), engine=None, store=None)
    calls: list[int] = []
    driver.add_frame_sink(lambda _frame: calls.append(threading.get_ident()))
    try:
        _drive(driver, ticks=1)
    finally:
        driver.close()
    assert calls == [
        threading.get_ident()
    ], "the sink must run synchronously on the caller's thread"

    raising_store = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(raises=RuntimeError("index corrupt")),
        start_worker=False,
    )
    raising_store(_Ctx(0.0))
    raising_store._worker_tick()
    raising_store(_Ctx(0.02))
    assert raising_store.peek_face() is None


def test_providers_never_raise_and_read_perception_folds_them() -> None:
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), start_worker=False)
    driver(_Ctx(0.0))
    sense = read_perception(
        SenseProviders(
            face=driver.as_face_provider(),
            frame_available=driver.as_frame_available_provider(),
        )
    )
    assert sense.frame_available is True
    assert sense.face is None


# --------------------------------------------------------------------------- #
# Threading — the heavy leg NEVER runs on the tick thread                     #
# --------------------------------------------------------------------------- #


def test_detection_runs_off_the_tick_thread() -> None:
    """A 425-1213 ms tick overrun against a 20 ms budget is what inline detection costs."""
    engine = _FakeEngine()
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=engine,
        store=_FakeStore(match=_FakeMatch("ada")),
        detect_interval=0.0,
    )
    try:
        tick_thread = threading.get_ident()
        for i in range(10):
            driver(_Ctx(round(i * 0.02, 10)))
        assert engine.ran.wait(timeout=5.0), "detection worker never ran"
        assert engine.threads, "no detection recorded"
        assert all(ident != tick_thread for ident in engine.threads)
    finally:
        driver.close()


def test_a_slow_detector_never_blocks_the_tick() -> None:
    """The tick publishes into a latest-wins slot; it never waits for a result."""
    started = threading.Event()
    release = threading.Event()

    class _SlowEngine(_FakeEngine):
        def detect(self, frame):
            started.set()
            release.wait(timeout=5.0)
            return super().detect(frame)

    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SlowEngine(),
        store=_FakeStore(match=_FakeMatch("ada")),
        detect_interval=0.0,
    )
    try:
        driver(_Ctx(0.0))
        assert started.wait(timeout=5.0), "worker never picked up the frame"
        # The detector is parked mid-detection; ticks must still complete.
        for i in range(1, 20):
            driver(_Ctx(round(i * 0.02, 10)))
        assert driver.peek_frame_available() is True
    finally:
        release.set()
        driver.close()


def test_close_is_idempotent_and_stops_the_worker() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(),
    )
    assert driver.worker_alive is True
    driver.close()
    driver.close()
    assert driver.worker_alive is False


def test_ticking_after_close_is_inert_not_an_error() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_FakeEngine(),
        store=_FakeStore(),
    )
    driver.close()
    _drive(driver, ticks=3)
    assert driver.peek_face() is None
    assert driver.peek_frame_available() is False


# --------------------------------------------------------------------------- #
# Composition boundary — this task ships providers, not the wiring            #
# --------------------------------------------------------------------------- #


def test_driver_never_constructs_its_own_media_client() -> None:
    """The media client is the composition root's single owner — always injected."""
    source = inspect.getsource(FS)
    assert "HeldMediaClient(" not in source
    for line in source.splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "media_client" not in stripped, f"face_sense must not import it: {line}"


# --------------------------------------------------------------------------- #
# The frame-read interval (#137 / #145)                                        #
# --------------------------------------------------------------------------- #


def test_the_frame_read_is_gated_by_the_interval_not_the_tick() -> None:
    """The read runs at its own cadence, not once per 50 Hz tick.

    Reading every tick sustained the whole 20 ms budget ~5% over for as long as
    frames flowed, measured on the deployed box
    (``docs/evidence/2026-08-02-t8-tick-overrun-attribution.md``). No consumer
    needs faster than 8 fps.
    """
    media = _FakeMedia([_frame() for _ in range(20)])
    driver = FaceSenseDriver(media=media, start_worker=False, frame_interval_s=0.1)
    try:
        # 10 ticks of a 50 Hz loop = 0.2 s of wall time -> 3 reads at 10 Hz
        # (t=0.00, 0.10, 0.20), never 10.
        for index in range(11):
            driver(_Ctx(index * 0.02))
    finally:
        driver.close()
    assert media.calls == 3, f"read {media.calls} times in 0.2 s; expected 3 at 10 Hz"


def test_frame_available_is_held_across_the_gap_between_reads() -> None:
    """A skipped read holds the condition on its TTL rather than dropping it.

    The interval (0.1 s) is an order of magnitude inside the TTL (1.0 s), so a
    tick that declines to read can never make the condition flap.
    """
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame() for _ in range(5)]),
        start_worker=False,
        frame_interval_s=0.1,
        frame_ttl_s=1.0,
    )
    try:
        driver(_Ctx(0.0))
        assert driver.peek_frame_available() is True
        for index in range(1, 5):  # every one of these is inside the interval
            driver(_Ctx(index * 0.02))
            assert driver.peek_frame_available() is True, "the TTL hold broke at a skipped read"
    finally:
        driver.close()


def test_a_clockless_tick_still_reads() -> None:
    """Without ``ctx.now`` there is no interval to measure, so the read happens.

    Declining instead would silence the sense entirely on a clock-less engine.
    """
    media = _FakeMedia([_frame() for _ in range(3)])
    driver = FaceSenseDriver(media=media, start_worker=False, frame_interval_s=0.1)
    try:
        for _ in range(3):
            driver(_Ctx(None))
    finally:
        driver.close()
    assert media.calls == 3


def test_a_disconnected_tick_does_not_consume_the_interval() -> None:
    """A client that comes back reads at once instead of waiting an interval out."""

    class _Client:
        def __init__(self) -> None:
            self.connected = False
            self.calls = 0

        def frame(self):
            self.calls += 1
            return _frame()

    media = _Client()
    driver = FaceSenseDriver(media=media, start_worker=False, frame_interval_s=0.1)
    try:
        driver(_Ctx(0.0))
        assert media.calls == 0
        media.connected = True
        driver(_Ctx(0.02))  # well inside the interval, but nothing was read yet
        assert media.calls == 1
        assert driver.peek_frame_available() is True
    finally:
        driver.close()


# --------------------------------------------------------------------------- #
# Camera-stream-ended staleness (issue #138) — DETECT ONLY                    #
# --------------------------------------------------------------------------- #
#
# Live evidence, 2026-08-02: the daemon's pipeline EOS'd and no camera frame
# arrived again for 1h45m while the runtime stayed healthy. ``connected``
# stays true across a dead pipeline (see ``_FakeMedia``, which has no
# ``connected`` attribute at all — the driver's own docstring says a missing
# probe is ASSUMED live, exactly what a real ``HeldMediaClient`` reports here),
# so the only honest signal is how long ago the last USABLE frame arrived.


def _drop_messages(caplog, reason: str) -> list:
    """Every senselog line naming *reason* as its drop reason."""
    return [
        record.getMessage()
        for record in caplog.records
        if f"dropped reason={reason}" in record.getMessage()
    ]


def test_stream_ended_drop_fires_once_after_frames_stop_while_connected(caplog) -> None:
    """The acceptance criterion's positive case: one latched drop within the window."""
    media = _FakeMedia([_frame(), None])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver(_Ctx(0.0))  # a real frame anchors `_last_frame_at`
        for now in (0.5, 1.0, 1.5, 3.0, 10.0):  # well past the 1.0s staleness window
            driver(_Ctx(now))

    drops = _drop_messages(caplog, FS.REASON_STREAM_ENDED)
    assert len(drops) == 1, drops
    assert "stage=vision source=face" in drops[0]


def test_a_camera_that_never_existed_produces_no_stream_ended_drop(caplog) -> None:
    """The acceptance criterion's negative case: no frame ever arrived, so no drop."""
    media = _FakeMedia([None])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        for now in (0.0, 1.0, 5.0, 50.0):  # far past the staleness window, forever
            driver(_Ctx(now))

    assert _drop_messages(caplog, FS.REASON_STREAM_ENDED) == []


def test_no_stream_ended_drop_when_camera_reports_unavailable(caplog) -> None:
    """A camera-absent robot (#120's case) is a structural absence, not #138's staleness."""
    media = _FakeMedia([_frame()], camera_available=False)
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        for now in (0.0, 5.0, 50.0):
            driver(_Ctx(now))

    assert _drop_messages(caplog, FS.REASON_STREAM_ENDED) == []


def test_stream_ended_drop_does_not_fire_inside_the_staleness_window() -> None:
    """A camera slower than 1 fps, but not dead, must not be misdiagnosed."""
    media = _FakeMedia([_frame(), None, None, None])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    driver(_Ctx(0.0))
    for now in (0.2, 0.5, 0.9):  # inside the 1.0s window the whole time
        driver(_Ctx(now))
    assert driver._stream_ended_logged is False


def test_stream_ended_latch_resets_after_frames_resume_and_can_fire_again(caplog) -> None:
    """A LATER silent episode is reported again — the latch is per-episode, not process-wide."""
    media = _FakeMedia([_frame(), None, _frame(), None])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver(_Ctx(0.0))  # frame: anchors `_last_frame_at`
        driver(_Ctx(2.0))  # None, 2.0s later: past the window -> drop #1
        driver(_Ctx(2.02))  # frame resumes: clears the latch
        driver(_Ctx(4.1))  # None, 2.08s later: past the window again -> drop #2

    assert len(_drop_messages(caplog, FS.REASON_STREAM_ENDED)) == 2


def test_staleness_detection_never_constructs_rebuilds_or_restarts_anything() -> None:
    """Grep proof (#138 acceptance): DETECT ONLY — no reconnect, rebuild, warm-up, restart.

    Checked against the method's CODE only — its own docstring explains, in
    prose, exactly what it must NOT do, which would trip a plain substring
    grep against the raw source.
    """
    source = textwrap.dedent(inspect.getsource(FaceSenseDriver._check_stream_staleness))
    (func,) = ast.parse(source).body
    body = func.body
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant):
        body = body[1:]  # drop the docstring node — prose is not code
    code = "\n".join(ast.get_source_segment(source, node) or "" for node in body)

    forbidden = (
        "warm_up",
        "ReachyMini",
        "HeldMediaClient",
        "reconnect",
        "rebuild",
        "restart",
        "connect(",
        "_ensure_",
    )
    for token in forbidden:
        assert token not in code, f"{token!r} must not appear in the staleness check's CODE: {code}"


def test_stream_ended_fires_when_the_camera_goes_unavailable_after_streaming(caplog) -> None:
    """The failure mode #138 actually produces, measured on the deployed box.

    The daemon reports ``camera_available`` FALSE once its GStreamer pipeline
    EOSes — not the believed-present-but-silent shape the first detector
    watched, which is why that detector stayed silent through three real camera
    deaths in one afternoon. A camera that streamed and then vanished is a
    stream that ENDED.
    """
    media = _FakeMedia([_frame()])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver(_Ctx(0.0))  # a real frame anchors `_last_frame_at`
        assert driver.peek_frame_available() is True
        media.camera_available = False  # the pipeline dies
        driver(_Ctx(0.5))
        assert not _drop_messages(caplog, FS.REASON_STREAM_ENDED), "fired inside the window"
        for now in (2.0, 5.0, 20.0):
            driver(_Ctx(now))

    drops = _drop_messages(caplog, FS.REASON_STREAM_ENDED)
    assert len(drops) == 1, f"a died pipeline was not named exactly once: {drops}"


def test_a_camera_that_never_streamed_and_is_unavailable_produces_no_drop(caplog) -> None:
    """The never-existed exemption survives the fix: no frame ever, no stream."""
    media = _FakeMedia([_frame()], camera_available=False)
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        for tick in range(4):
            driver(_Ctx(tick * 30.0))
    assert not _drop_messages(caplog, FS.REASON_STREAM_ENDED)


def test_a_disconnected_client_is_never_reported_as_a_dead_stream(caplog) -> None:
    """A dropped or unwarmed client is not a camera whose stream ended.

    ``HeldMediaClient.connected`` is false whenever no live client is HELD —
    not warmed yet, dropped, mid-backoff. Sharing the ``camera-stream-ended``
    name with that state would report a dead camera on every reconnect window,
    which is the misleading-diagnosis class #138 exists to remove.
    """

    class _Dropping:
        def __init__(self) -> None:
            self.connected = True
            self.camera_available = True

        def frame(self):
            return _frame()

    media = _Dropping()
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver(_Ctx(0.0))  # a real frame anchors `_last_frame_at`
        assert driver.peek_frame_available() is True
        media.connected = False  # the client drops — NOT a dead pipeline
        for now in (2.0, 10.0, 60.0):  # far past the staleness window
            driver(_Ctx(now))
        assert driver.peek_frame_available() is False

    assert not _drop_messages(
        caplog, FS.REASON_STREAM_ENDED
    ), "a merely disconnected client was mislabelled as a dead camera stream"


# --------------------------------------------------------------------------- #
# REACHY_FACE_DETECT_INTERVAL / REACHY_FACE_DETECT_MAX_WIDTH                  #
# --------------------------------------------------------------------------- #


class _FakeDetectAllEngine:
    """A ``detect_all``-shaped engine recording the frame's shape it saw."""

    def __init__(self, bbox_norm=(0.1, 0.2, 0.6, 0.8)) -> None:
        self._bbox_norm = bbox_norm
        self.frames = []

    def detect_all(self, frame):
        self.frames.append(frame)
        detection = _FakeDetection([0.1, 0.2])
        detection.bbox_norm = self._bbox_norm
        return [detection]


def test_detect_interval_from_env_defaults_when_unset() -> None:
    assert FS.detect_interval_from_env(None) == FS.DEFAULT_DETECT_INTERVAL


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("1", 1.0),
        ("0.25", 0.25),
        ("  2.5  ", 2.5),
    ],
)
def test_detect_interval_from_env_parses_a_positive_float(raw, expected) -> None:
    assert FS.detect_interval_from_env(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "0", "", "   ", "nan", "inf"])
def test_detect_interval_from_env_falls_back_to_default_on_garbage(raw) -> None:
    assert FS.detect_interval_from_env(raw) == FS.DEFAULT_DETECT_INTERVAL


def test_detect_interval_from_env_reads_the_real_os_environ_when_unset(monkeypatch) -> None:
    monkeypatch.delenv("REACHY_FACE_DETECT_INTERVAL", raising=False)
    assert FS.detect_interval_from_env() == FS.DEFAULT_DETECT_INTERVAL
    monkeypatch.setenv("REACHY_FACE_DETECT_INTERVAL", "1.0")
    assert FS.detect_interval_from_env() == 1.0


def test_detect_max_width_from_env_defaults_when_unset() -> None:
    assert FS.detect_max_width_from_env(None) == FS.DEFAULT_DETECT_MAX_WIDTH == 0


@pytest.mark.parametrize("raw,expected", [("640", 640), ("1280", 1280), ("0", 0)])
def test_detect_max_width_from_env_parses_a_non_negative_int(raw, expected) -> None:
    assert FS.detect_max_width_from_env(raw) == expected


@pytest.mark.parametrize("raw", ["abc", "-1", "", "   ", "3.5"])
def test_detect_max_width_from_env_falls_back_to_default_on_garbage(raw) -> None:
    assert FS.detect_max_width_from_env(raw) == FS.DEFAULT_DETECT_MAX_WIDTH


def test_downscale_for_detection_resizes_a_wide_frame() -> None:
    pytest.importorskip("cv2")
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    scaled = FS.downscale_for_detection(frame, 640)
    assert scaled.shape == (360, 640, 3)


@pytest.mark.parametrize("max_width", [0, None])
def test_downscale_for_detection_is_identity_when_off(max_width) -> None:
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    result = FS.downscale_for_detection(frame, max_width)
    assert result is frame


def test_downscale_for_detection_is_identity_when_already_narrow_enough() -> None:
    frame = np.zeros((480, 640, 3), dtype=np.uint8)
    result = FS.downscale_for_detection(frame, 640)
    assert result is frame
    result_wider_cap = FS.downscale_for_detection(frame, 1280)
    assert result_wider_cap is frame


def test_downscale_for_detection_without_cv2_passes_through_unscaled(monkeypatch) -> None:
    """If cv2 is missing, the frame passes through unscaled — never a raise.

    The helper loads OpenCV through ``importlib.import_module`` (not a bare
    ``import``), so THAT is the seam the simulated absence must hit — patching
    ``builtins.__import__`` never applied, and with the ``[vision]`` extra
    installed the frame was resized and this test failed (review finding).
    """
    import importlib

    real_import_module = importlib.import_module

    def _no_cv2(name, *args, **kwargs):
        if name == "cv2":
            raise ImportError("no module named cv2")
        return real_import_module(name, *args, **kwargs)

    monkeypatch.setattr(importlib, "import_module", _no_cv2)
    frame = np.zeros((720, 1280, 3), dtype=np.uint8)
    result = FS.downscale_for_detection(frame, 640)
    assert result is frame


def test_the_no_cv2_test_seam_is_the_one_the_helper_uses() -> None:
    """Vacuity guard: the helper must reach cv2 via importlib.import_module."""
    import inspect

    source = inspect.getsource(FS.downscale_for_detection)
    assert 'importlib.import_module("cv2")' in source


def test_driver_with_detect_max_width_hands_the_engine_a_downscaled_frame() -> None:
    """The engine sees a 640-wide frame, and the fake's normalised bbox survives unchanged."""
    pytest.importorskip("cv2")
    media = _FakeMedia([np.zeros((720, 1280, 3), dtype=np.uint8)])
    engine = _FakeDetectAllEngine(bbox_norm=(0.1, 0.2, 0.6, 0.8))
    driver = FaceSenseDriver(
        media=media,
        engine=engine,
        store=_FakeStore(match=_FakeMatch("ada")),
        detect_max_width=640,
        start_worker=False,
    )
    _drive(driver, ticks=1, start=0.0)
    driver._worker_tick()
    _drive(driver, ticks=1, start=0.02)

    assert engine.frames, "the engine never saw a frame"
    assert engine.frames[0].shape == (360, 640, 3)
    # bbox_norm is normalised to whatever frame the engine saw, so it is
    # published unchanged — no rescaling back to the original 1280-wide frame.
    assert driver.peek_face_bbox() == pytest.approx((0.1, 0.2, 0.5, 0.6))


def test_driver_without_detect_max_width_hands_the_engine_the_full_frame() -> None:
    """No knob set -> the engine sees exactly the frame read off the media client."""
    media = _FakeMedia([np.zeros((720, 1280, 3), dtype=np.uint8)])
    engine = _FakeDetectAllEngine()
    driver = FaceSenseDriver(
        media=media,
        engine=engine,
        store=_FakeStore(match=_FakeMatch("ada")),
        start_worker=False,
    )
    _drive(driver, ticks=1, start=0.0)
    driver._worker_tick()

    assert engine.frames
    assert engine.frames[0].shape == (720, 1280, 3)


def test_behavior_command_passes_both_knobs_from_env(monkeypatch) -> None:
    """The composition seam: both env knobs land on the constructed driver."""
    monkeypatch.setenv("REACHY_FACE_DETECT_INTERVAL", "1.0")
    monkeypatch.setenv("REACHY_FACE_DETECT_MAX_WIDTH", "640")
    from reachy.behavior.face_sense import detect_interval_from_env, detect_max_width_from_env

    assert detect_interval_from_env() == 1.0
    assert detect_max_width_from_env() == 640

    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        detect_interval=detect_interval_from_env(),
        detect_max_width=detect_max_width_from_env(),
        start_worker=False,
    )
    assert driver._detect_interval == 1.0
    assert driver._detect_max_width == 640


def test_behavior_command_construction_site_passes_both_knobs() -> None:
    """Grep-assert the call site in behavior.py wires both knobs through."""
    import inspect

    from reachy.cli._commands import behavior as behavior_module

    source = inspect.getsource(behavior_module)
    assert "detect_interval=detect_interval_from_env()" in source
    assert "detect_max_width=detect_max_width_from_env()" in source


# --------------------------------------------------------------------------- #
# t4 #181: FaceObservation.captured_at — the age anchor is capture time,      #
# not publish/drain time                                                      #
# --------------------------------------------------------------------------- #


class _DetectionWithBBox:
    """A detection carrying a real bbox, so the position leg is exercised too."""

    embedding = [0.1, 0.2]
    bbox_norm = (0.1, 0.1, 0.3, 0.3)


def test_captured_at_is_stamped_before_detection_and_ages_the_snapshot() -> None:
    """t4 #181 acceptance (a): face_age_s == now - captured_at, not now - drain time.

    The fake engine's ``detect`` advances a shared clock by 300 ms to simulate
    a slow CM4 detection — the exact gap the honesty condition (h32) names.
    ``_worker_tick`` is driven synchronously (``start_worker=False``), so there
    is no thread race: the clock only moves where this test moves it.
    """
    clock_time = [0.0]

    class _SlowEngine:
        def detect(self, frame):
            clock_time[0] += 0.3  # simulated 300 ms detection cost
            return _DetectionWithBBox()

    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SlowEngine(),
        store=_FakeStore(match=_FakeMatch("ada")),
        detect_interval=0.0,
        clock=lambda: clock_time[0],
        start_worker=False,
    )
    # tick at t=0.0: the tick thread publishes the frame for the worker to take.
    driver(_Ctx(0.0))
    assert clock_time[0] == 0.0

    driver._worker_tick()  # the heavy leg — clock advances mid-detection
    assert clock_time[0] == pytest.approx(0.3)

    # tick at t=0.3: the tick thread drains the worker's result and latches it.
    driver(_Ctx(0.3))
    # captured_at was stamped at 0.0 (before detection), so the anchor is 0.0
    # and the age at this drain is 0.3 — never 0.0, which is what anchoring on
    # the drain/publish time would have produced.
    assert driver._face_bbox_at == pytest.approx(0.0)
    assert driver.peek_face_age_s() == pytest.approx(0.3)

    # a later tick with no new detection keeps ageing from the SAME anchor.
    driver(_Ctx(0.5))
    assert driver.peek_face_age_s() == pytest.approx(0.5)


def test_face_observation_without_captured_at_falls_back_to_publish_time() -> None:
    """t4 #181 acceptance (b): an older producer/test fake with no ``captured_at``
    still works — the age anchors on the drain (publish) tick, exactly as before.
    """
    driver = FaceSenseDriver(media=_FakeMedia([_frame()]), start_worker=False)
    driver(_Ctx(0.0))
    driver._output.publish(FS.FaceObservation(name="ada", bbox=(0.1, 0.1, 0.2, 0.2)))

    driver(_Ctx(1.0))  # drain tick — no captured_at on the observation
    assert driver._face_bbox_at == pytest.approx(1.0)
    assert driver.peek_face_age_s() == pytest.approx(0.0)

    driver(_Ctx(1.2))
    assert driver.peek_face_age_s() == pytest.approx(0.2)


def test_bbox_ttl_expiry_anchors_on_captured_at_not_drain_time() -> None:
    """t4 #181 acceptance (c): the bbox TTL expiry uses the captured_at anchor.

    Without the fix, the anchor would be the 0.5 s drain tick and the position
    would still be held at t=1.4 (only 0.9 s past that anchor); anchored on the
    true capture time (0.0), it is 1.4 s old — past a 1.0 s TTL — and expires.
    """
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        face_bbox_ttl_s=1.0,
        start_worker=False,
    )
    driver(_Ctx(0.0))
    driver._output.publish(
        FS.FaceObservation(name="ada", bbox=(0.1, 0.1, 0.2, 0.2), captured_at=0.0)
    )

    driver(_Ctx(0.5))  # drain tick, well after the real capture time
    assert driver.peek_face_bbox() is not None

    driver(_Ctx(1.4))  # now - captured_at(0.0) = 1.4 > ttl(1.0): expired
    assert driver.peek_face_bbox() is None
    assert driver.peek_face_age_s() is None


def test_face_observation_captured_at_defaults_to_none() -> None:
    """The dataclass default keeps every existing bare construction working."""
    assert FS.FaceObservation(name="ada").captured_at is None
    assert FS.FaceObservation(name="ada", bbox=(0.0, 0.0, 0.1, 0.1)).captured_at is None


def test_worker_tick_stamps_captured_at_on_the_published_observation() -> None:
    """A direct check of the publish contract: captured_at is the pre-detect clock."""
    clock_time = [5.0]

    class _Engine:
        def detect(self, frame):
            clock_time[0] += 0.05
            return _DetectionWithBBox()

    published: list = []

    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_Engine(),
        store=_FakeStore(match=_FakeMatch("ada")),
        detect_interval=0.0,
        clock=lambda: clock_time[0],
        start_worker=False,
    )
    driver._output.publish = lambda value: published.append(value) or None

    driver(_Ctx(0.0))
    driver._worker_tick()

    assert len(published) == 1
    observation = published[0]
    assert isinstance(observation, FS.FaceObservation)
    assert observation.captured_at == pytest.approx(5.0)


# --------------------------------------------------------------------------- #
# Issue #179 — still-only detection: the self-motion gate on the worker        #
# --------------------------------------------------------------------------- #
#
# On the CM4 a detection submitted mid-slew costs the tick rate (50 -> 7 Hz)
# AND produces a blurred bbox in a head frame that has already moved on. The
# gate is the same shape as ``rms_sense``'s moving floor: an injected
# ``SelfMovingProvider`` peek, one senselog line per transition (never per
# tick), and a raising/missing peek that degrades to "not moving".


class _GateDriver:
    """A recognizer-ready driver whose detections are counted, on a fake clock."""

    def __init__(self, clock_box, **kwargs):
        self.detections = 0
        engine = _FakeEngine()
        self.driver = FaceSenseDriver(
            media=_FakeMedia([_frame()]),
            engine=engine,
            store=_FakeStore(match=_FakeMatch("ada")),
            detect_interval=0.5,
            frame_interval_s=0.0,
            clock=lambda: clock_box[0],
            start_worker=False,
            **kwargs,
        )
        self._engine = engine

    def worker_tick(self) -> None:
        before = len(self._engine.frames)
        self.driver._worker_tick()
        if len(self._engine.frames) > before:
            self.detections += 1


def _gate_lines(caplog) -> list:
    """Every senselog line this module's self-motion gate emitted."""
    return [
        record.getMessage()
        for record in caplog.records
        if "stage=gate source=face" in record.getMessage()
    ]


def _run_gate(gate, clock_box, *, seconds: float, step: float = 0.1, ctx_start: float = 0.0):
    """Advance the fake clock, publishing a frame and running the worker each step."""
    ticks = int(round(seconds / step))
    for i in range(ticks):
        clock_box[0] = round(clock_box[0] + step, 6)
        gate.driver(_Ctx(round(ctx_start + (i + 1) * step, 6)))
        gate.worker_tick()


def test_a_moving_head_submits_zero_detections() -> None:
    """Criterion (a), first half: 2 s of commanded motion -> nothing detected."""
    clock = [0.0]
    gate = _GateDriver(clock, moving=lambda: True)
    _run_gate(gate, clock, seconds=2.0)
    assert gate.detections == 0


def test_detection_resumes_once_the_settle_has_elapsed_after_motion_stops() -> None:
    """Criterion (a), second half: within ``still_settle_s`` of stillness it detects again."""
    clock = [0.0]
    moving = {"v": True}
    gate = _GateDriver(clock, moving=lambda: moving["v"], still_settle_s=0.5)
    _run_gate(gate, clock, seconds=2.0)
    assert gate.detections == 0

    moving["v"] = False
    # Inside the settle: still nothing.
    _run_gate(gate, clock, seconds=0.4, ctx_start=2.0)
    assert gate.detections == 0
    # Past the settle: a detection lands.
    _run_gate(gate, clock, seconds=0.4, ctx_start=2.4)
    assert gate.detections >= 1


def test_a_held_lock_degrades_the_cadence_instead_of_silencing_it() -> None:
    """Criterion (b): while a lock is held, motion slows detection — it never stops it."""
    clock = [0.0]
    gate = _GateDriver(
        clock,
        moving=lambda: True,
        lock_held=lambda: True,
        held_detect_interval=1.5,
    )
    _run_gate(gate, clock, seconds=6.0)
    # 6 s at a 1.5 s held cadence: a handful, not zero and not the free-running rate.
    assert 3 <= gate.detections <= 5


def test_the_gate_emits_exactly_one_open_and_one_close_line_per_motion_episode(caplog) -> None:
    """Criterion (e): transitions are observable; ticks are not."""
    clock = [0.0]
    moving = {"v": False}
    gate = _GateDriver(clock, moving=lambda: moving["v"], still_settle_s=0.5)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        _run_gate(gate, clock, seconds=0.5)
        moving["v"] = True
        _run_gate(gate, clock, seconds=2.0, ctx_start=0.5)
        moving["v"] = False
        _run_gate(gate, clock, seconds=1.5, ctx_start=2.5)

    lines = _gate_lines(caplog)
    assert len(lines) == 2, lines
    assert "opened" in lines[0] and "self-moving" in lines[0]
    assert "held=" in lines[0]
    assert "closed" in lines[1]


def test_a_raising_moving_peek_degrades_to_not_moving() -> None:
    """Criterion (f): a broken peek is never a crash and never a silenced sense."""

    def _boom() -> bool:
        raise RuntimeError("no self-motion driver")

    clock = [0.0]
    gate = _GateDriver(clock, moving=_boom)
    _run_gate(gate, clock, seconds=2.0)
    assert gate.detections >= 1


def test_without_a_moving_peek_the_cadence_is_exactly_todays() -> None:
    """No injected provider means no gate at all — the pre-#179 behaviour."""
    clock = [0.0]
    gate = _GateDriver(clock)
    _run_gate(gate, clock, seconds=2.0)
    assert gate.detections >= 1


# --------------------------------------------------------------------------- #
# Issue #179 / #176 — the ``on_stale`` callback seam                          #
# --------------------------------------------------------------------------- #
#
# ``_check_stream_staleness`` stays DETECT ONLY here: it calls an injected
# callback and nothing else. Composition (a later task) binds it to
# ``HeldMediaClient.drop``; this module imports neither.


def test_on_stale_fires_once_per_silent_episode() -> None:
    calls: list = []
    media = _FakeMedia([_frame(), None, _frame(), None])
    driver = FaceSenseDriver(
        media=media,
        start_worker=False,
        frame_interval_s=0.0,
        stream_stale_s=1.0,
        on_stale=calls.append,
    )
    driver(_Ctx(0.0))  # a frame anchors `_last_frame_at`
    driver(_Ctx(2.0))  # silent past the window -> episode 1
    driver(_Ctx(2.5))  # still silent -> no second call
    assert calls == [FS.REASON_STREAM_ENDED]

    driver(_Ctx(2.6))  # frames resume: the latch re-arms
    driver(_Ctx(5.0))  # silent again -> episode 2
    assert calls == [FS.REASON_STREAM_ENDED, FS.REASON_STREAM_ENDED]


def test_on_stale_never_fires_when_no_frame_ever_arrived() -> None:
    calls: list = []
    driver = FaceSenseDriver(
        media=_FakeMedia([None]),
        start_worker=False,
        frame_interval_s=0.0,
        stream_stale_s=1.0,
        on_stale=calls.append,
    )
    for now in (0.0, 1.0, 5.0, 50.0):
        driver(_Ctx(now))
    assert calls == []


def test_on_stale_never_fires_when_the_camera_reports_unavailable() -> None:
    """A died pipeline still LOGS the drop; it must not ask for a re-acquire."""
    calls: list = []
    media = _FakeMedia([_frame()])
    driver = FaceSenseDriver(
        media=media,
        start_worker=False,
        frame_interval_s=0.0,
        stream_stale_s=1.0,
        on_stale=calls.append,
    )
    driver(_Ctx(0.0))
    media.camera_available = False
    for now in (2.0, 5.0, 20.0):
        driver(_Ctx(now))
    assert calls == []


def test_a_raising_on_stale_is_contained_and_logged_once(caplog) -> None:
    def _boom(reason: str) -> None:
        raise RuntimeError("holder is gone")

    media = _FakeMedia([_frame(), None, _frame(), None])
    driver = FaceSenseDriver(
        media=media,
        start_worker=False,
        frame_interval_s=0.0,
        stream_stale_s=1.0,
        on_stale=_boom,
    )
    with caplog.at_level(logging.WARNING, logger="reachy.behavior.face_sense"):
        driver(_Ctx(0.0))
        driver(_Ctx(2.0))
        driver(_Ctx(2.6))
        driver(_Ctx(5.0))

    assert driver.peek_frame_available() is False  # no crash reached the tick body
    warnings = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert len(warnings) == 1, [r.getMessage() for r in warnings]


def test_without_a_callback_the_staleness_path_is_exactly_todays(caplog) -> None:
    media = _FakeMedia([_frame(), None])
    driver = FaceSenseDriver(
        media=media, start_worker=False, frame_interval_s=0.0, stream_stale_s=1.0
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        driver(_Ctx(0.0))
        for now in (2.0, 5.0):
            driver(_Ctx(now))
    assert len(_drop_messages(caplog, FS.REASON_STREAM_ENDED)) == 1
