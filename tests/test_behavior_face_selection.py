"""Face POSITION and face SELECTION reaching :class:`~reachy.behavior.sense.Sense` (t2).

Before this task the camera sense threw away everything about a face except
its name: :meth:`reachy.behavior.face_sense.FaceSenseDriver._detect_once`
returned ``match.name`` and dropped the detection, so an UNMATCHED face — a
stranger looking straight at the robot — produced no reading at all, even
though :attr:`reachy.vision.face.FaceDetection.bbox_norm` was already computed
one layer down. A gaze behavior needs the POSITION of the best face, whether or
not the store knows its name.

These tests pin the two acceptance criteria:

1. **Position reaches the snapshot.** ``Sense.face_bbox`` carries
   ``(x, y, w, h)`` normalised to the frame and ``Sense.face_age_s`` how long
   ago that detection landed. An unmatched detection still yields a bbox while
   ``Sense.face`` stays ``None`` — the name cue keeps its "named, matched face
   only" contract untouched.
2. **Selection is explicit.** With several faces in one frame the biggest wins;
   when two are within :data:`~reachy.behavior.face_sense.AREA_TIE_RATIO` of
   each other in area, a recognised face beats an unknown one. The rule lives
   in the pure :func:`~reachy.behavior.face_sense.select_face`, so it is
   unit-tested over plain candidate lists as well as through the driver.

Everything here runs WITHOUT the ``[vision]`` extra — same discipline as
``tests/test_behavior_face_sense.py``: injected fakes, no cv2, no robot.
"""

from __future__ import annotations

import numpy as np
import pytest

from reachy.behavior.face_sense import (
    AREA_TIE_RATIO,
    FaceCandidate,
    FaceSenseDriver,
    select_face,
)
from reachy.behavior.sense import EMPTY_SENSE, Sense, SenseProviders, read_perception

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


def _frame(width: int = 8, height: int = 6) -> np.ndarray:
    return np.zeros((height, width, 3), dtype=np.uint8)


class _Ctx:
    """A duck-typed ``TickContext`` — the driver only reads ``now``."""

    def __init__(self, now: float = 0.0) -> None:
        self.now = now


class _FakeMedia:
    def __init__(self, frames, *, camera_available: bool = True) -> None:
        self._frames = list(frames)
        self.camera_available = camera_available

    def frame(self):
        return self._frames[0] if len(self._frames) == 1 else self._frames.pop(0)


class _Detection:
    """A ``FaceDetection``-shaped record: corner bbox + an embedding."""

    def __init__(self, bbox_norm, embedding) -> None:
        self.bbox_norm = bbox_norm
        self.embedding = embedding


class _MultiEngine:
    """A detector exposing ``detect_all`` (and ``detect`` as the largest)."""

    def __init__(self, detections) -> None:
        self._detections = list(detections)

    def detect_all(self, frame):
        return list(self._detections)

    def detect(self, frame):
        return self._detections[0] if self._detections else None


class _SingleEngine:
    """A detector exposing ONLY ``detect`` — the shape shipped before t2."""

    def __init__(self, detection) -> None:
        self._detection = detection

    def detect(self, frame):
        return self._detection


class _NameStore:
    """A ``FaceStore``-shaped matcher keyed on the embedding's first element."""

    def __init__(self, names: dict) -> None:
        self._names = names

    def match(self, embedding):
        key = float(np.asarray(embedding).ravel()[0])
        name = self._names.get(key)
        if name is None:
            return None
        return type("_M", (), {"name": name})()


def _xyxy(x: float, y: float, w: float, h: float) -> tuple:
    """Corner-form bbox (what the engine produces) from an x/y/w/h box."""
    return (x, y, x + w, y + h)


def _run(driver: FaceSenseDriver, *, now: float = 0.0) -> None:
    """One tick, one worker iteration, one draining tick — the usual dance."""
    driver(_Ctx(now))
    driver._worker_tick()
    driver(_Ctx(now + 0.02))


# --------------------------------------------------------------------------- #
# Criterion 1 — the snapshot carries position and age                         #
# --------------------------------------------------------------------------- #


def test_sense_declares_face_bbox_and_face_age_defaulting_to_no_reading() -> None:
    assert EMPTY_SENSE.face_bbox is None
    assert EMPTY_SENSE.face_age_s is None
    s = Sense(face_bbox=(0.1, 0.2, 0.3, 0.4), face_age_s=0.5)
    assert s.face_bbox == (0.1, 0.2, 0.3, 0.4)
    assert s.face_age_s == 0.5


def test_read_perception_peeks_the_face_bbox_and_age_providers() -> None:
    snap = read_perception(
        SenseProviders(face_bbox=lambda: (0.1, 0.2, 0.3, 0.4), face_age_s=lambda: 1.25)
    )
    assert snap.face_bbox == (0.1, 0.2, 0.3, 0.4)
    assert snap.face_age_s == 1.25


def test_a_raising_or_malformed_bbox_provider_degrades_to_no_reading() -> None:
    def _boom():
        raise RuntimeError("nope")

    assert read_perception(SenseProviders(face_bbox=_boom, face_age_s=_boom)).face_bbox is None
    for bad in ((0.1, 0.2), "0.1,0.2,0.3,0.4", (0.1, 0.2, 0.3, "x")):
        snap = read_perception(SenseProviders(face_bbox=lambda bad=bad: bad))
        assert snap.face_bbox is None, bad


def test_an_unmatched_face_still_yields_a_bbox_while_the_name_stays_none() -> None:
    """The headline of criterion 1: a single UNKNOWN face is a position reading."""
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_Detection(_xyxy(0.2, 0.3, 0.4, 0.2), [1.0])),
        store=_NameStore({}),  # nobody is known
        start_worker=False,
    )
    _run(driver)

    assert driver.peek_face() is None
    bbox = driver.peek_face_bbox()
    assert bbox is not None
    assert bbox == pytest.approx((0.2, 0.3, 0.4, 0.2))

    snap = read_perception(
        SenseProviders(
            face=driver.as_face_provider(),
            face_bbox=driver.as_face_bbox_provider(),
            face_age_s=driver.as_face_age_provider(),
        )
    )
    assert snap.face is None
    assert snap.face_bbox == pytest.approx((0.2, 0.3, 0.4, 0.2))
    assert snap.face_age_s is not None and snap.face_age_s >= 0.0


def test_a_recognised_face_yields_both_the_name_and_the_bbox() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_Detection(_xyxy(0.1, 0.1, 0.5, 0.5), [7.0])),
        store=_NameStore({7.0: "ada"}),
        start_worker=False,
    )
    _run(driver)
    assert driver.peek_face() == "ada"
    assert driver.peek_face_bbox() == pytest.approx((0.1, 0.1, 0.5, 0.5))


def test_the_face_age_grows_with_the_tick_clock() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_Detection(_xyxy(0.2, 0.2, 0.2, 0.2), [1.0])),
        store=_NameStore({}),
        start_worker=False,
        face_bbox_ttl_s=10.0,
    )
    _run(driver, now=0.0)
    # `_run` drains the observation on its second tick, at now=0.02 — that is
    # the anchor the age counts from (the tick clock, never the worker's).
    assert driver.peek_face_age_s() == pytest.approx(0.0, abs=1e-9)
    driver(_Ctx(1.5))
    assert driver.peek_face_age_s() == pytest.approx(1.48)
    assert driver.peek_face_bbox() is not None


def test_the_bbox_survives_the_per_name_reannounce_cooldown() -> None:
    """The name cue is cooled down; the POSITION must keep refreshing anyway.

    Otherwise a face that simply lingers — the ordinary case for a gaze
    behavior — would have a position for one tick and then nothing for the
    whole 30 s cooldown.
    """
    detection = _Detection(_xyxy(0.0, 0.0, 0.4, 0.4), [7.0])
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(detection),
        store=_NameStore({7.0: "ada"}),
        reannounce_cooldown=30.0,
        detect_interval=0.0,
        start_worker=False,
        face_bbox_ttl_s=10.0,
    )
    _run(driver, now=0.0)
    assert driver.peek_face() == "ada"

    detection.bbox_norm = _xyxy(0.5, 0.5, 0.2, 0.2)  # she moved
    _run(driver, now=1.0)
    assert driver.peek_face() is None  # still cooled down — unchanged contract
    assert driver.peek_face_bbox() == pytest.approx((0.5, 0.5, 0.2, 0.2))
    assert driver.peek_face_age_s() == pytest.approx(0.0, abs=1e-9)


def test_the_bbox_expires_once_its_ttl_lapses() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_Detection(_xyxy(0.2, 0.2, 0.2, 0.2), [1.0])),
        store=_NameStore({}),
        start_worker=False,
        face_bbox_ttl_s=2.0,
    )
    _run(driver, now=0.0)
    driver(_Ctx(1.9))
    assert driver.peek_face_bbox() is not None
    driver(_Ctx(2.5))
    assert driver.peek_face_bbox() is None
    assert driver.peek_face_age_s() is None


def test_close_clears_the_position_reading() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_Detection(_xyxy(0.2, 0.2, 0.2, 0.2), [1.0])),
        store=_NameStore({}),
        start_worker=False,
    )
    _run(driver)
    assert driver.peek_face_bbox() is not None
    driver.close()
    assert driver.peek_face_bbox() is None
    assert driver.peek_face_age_s() is None


def test_a_detection_without_a_bbox_is_still_a_name_cue() -> None:
    """Backwards compatibility: a detector that yields no box still names faces."""

    class _NoBbox:
        embedding = [7.0]

    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_SingleEngine(_NoBbox()),
        store=_NameStore({7.0: "ada"}),
        start_worker=False,
    )
    _run(driver)
    assert driver.peek_face() == "ada"
    assert driver.peek_face_bbox() is None


# --------------------------------------------------------------------------- #
# Criterion 2 — selection among several faces in one frame                    #
# --------------------------------------------------------------------------- #


def test_select_face_over_an_empty_list_is_no_reading() -> None:
    assert select_face([]) is None


def test_the_biggest_face_wins_when_the_gap_is_wide() -> None:
    """A larger UNKNOWN face beats a much smaller KNOWN one."""
    big_unknown = FaceCandidate(bbox=(0.0, 0.0, 0.6, 0.6), name=None)
    small_known = FaceCandidate(bbox=(0.7, 0.7, 0.1, 0.1), name="ada")
    assert select_face([small_known, big_unknown]) is big_unknown


def test_a_recognised_face_wins_a_near_tie_in_area() -> None:
    """Within ~15% of area, a name breaks the tie."""
    known = FaceCandidate(bbox=(0.0, 0.0, 0.4, 0.4), name="ada")  # area 0.16
    unknown = FaceCandidate(bbox=(0.5, 0.0, 0.42, 0.4), name=None)  # area 0.168
    ratio = (0.4 * 0.4) / (0.42 * 0.4)
    assert 1.0 - ratio < AREA_TIE_RATIO  # the tie is genuinely inside the band
    assert select_face([unknown, known]) is known
    assert select_face([known, unknown]) is known


def test_the_biggest_of_two_recognised_faces_wins() -> None:
    small = FaceCandidate(bbox=(0.0, 0.0, 0.4, 0.4), name="ada")
    big = FaceCandidate(bbox=(0.5, 0.0, 0.42, 0.4), name="bo")
    assert select_face([small, big]) is big


def test_a_boxless_candidate_never_beats_one_with_a_box() -> None:
    boxless = FaceCandidate(bbox=None, name="ada")
    boxed = FaceCandidate(bbox=(0.0, 0.0, 0.3, 0.3), name=None)
    assert select_face([boxless, boxed]) is boxed


def test_the_driver_selects_the_bigger_unknown_over_a_smaller_known_face() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_MultiEngine(
            [
                _Detection(_xyxy(0.7, 0.7, 0.1, 0.1), [7.0]),  # small, known
                _Detection(_xyxy(0.0, 0.0, 0.6, 0.6), [1.0]),  # big, unknown
            ]
        ),
        store=_NameStore({7.0: "ada"}),
        start_worker=False,
    )
    _run(driver)
    assert driver.peek_face_bbox() == pytest.approx((0.0, 0.0, 0.6, 0.6))
    assert driver.peek_face() is None  # the CHOSEN face is nameless


def test_the_driver_prefers_the_known_face_in_a_near_tie() -> None:
    driver = FaceSenseDriver(
        media=_FakeMedia([_frame()]),
        engine=_MultiEngine(
            [
                _Detection(_xyxy(0.5, 0.0, 0.42, 0.4), [1.0]),  # marginally bigger, unknown
                _Detection(_xyxy(0.0, 0.0, 0.4, 0.4), [7.0]),  # known
            ]
        ),
        store=_NameStore({7.0: "ada"}),
        start_worker=False,
    )
    _run(driver)
    assert driver.peek_face() == "ada"
    assert driver.peek_face_bbox() == pytest.approx((0.0, 0.0, 0.4, 0.4))
