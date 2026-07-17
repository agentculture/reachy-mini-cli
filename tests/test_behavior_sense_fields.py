"""Tests for the Sense perception-snapshot extension and its provider seam.

TASK t1: :class:`~reachy.behavior.sense.Sense` gains four new None-safe fields
(``rms``, ``pat_event``, ``face``, ``frame_available``) plus a small, stdlib-only,
duck-typed provider-seam type (:class:`~reachy.behavior.sense.SenseProviders` /
:func:`~reachy.behavior.sense.read_perception`) so a future engine composition can
feed pat/vision/face cues into the same :class:`Sense` a sensor-driven behavior
already reads for DoA today. No ``engine.py`` wiring here — this is the snapshot +
provider scaffolding only; every existing ``Sense``/``DoaPoller`` call site (see
``tests/test_behavior.py``) must keep working unchanged.
"""

from __future__ import annotations

import inspect

import pytest

from reachy.behavior.sense import (
    EMPTY_SENSE,
    NO_PROVIDERS,
    Sense,
    SenseProviders,
    read_perception,
)

# --------------------------------------------------------------------------- #
# 1. Sense field extension — None-safe defaults, existing constructors intact #
# --------------------------------------------------------------------------- #


def test_sense_new_fields_default_none_safe() -> None:
    s = Sense()
    assert s.rms is None
    assert s.pat_event is None
    assert s.face is None
    assert s.frame_available is False


def test_empty_sense_carries_the_same_none_safe_defaults() -> None:
    assert EMPTY_SENSE.rms is None
    assert EMPTY_SENSE.pat_event is None
    assert EMPTY_SENSE.face is None
    assert EMPTY_SENSE.frame_available is False


def test_existing_doa_only_constructor_still_works() -> None:
    # The exact call shape every existing call site (tests/test_behavior.py, the
    # listen/vision/pat/sleep sense feeds) already uses — must keep constructing
    # with no code change.
    s = Sense(doa_angle=0.5, speech_detected=True)
    assert s.doa_angle == 0.5
    assert s.speech_detected is True
    assert s.rms is None and s.pat_event is None and s.face is None
    assert s.frame_available is False


def test_sense_accepts_new_fields_explicitly() -> None:
    s = Sense(
        doa_angle=1.0,
        speech_detected=True,
        rms=0.42,
        pat_event=("scratch", "level1"),
        face="Ada",
        frame_available=True,
    )
    assert s.rms == 0.42
    assert s.pat_event == ("scratch", "level1")
    assert s.face == "Ada"
    assert s.frame_available is True


def test_sense_is_still_frozen() -> None:
    s = Sense()
    with pytest.raises(Exception):  # dataclasses.FrozenInstanceError
        s.rms = 1.0  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 2. Provider seam — SenseProviders / read_perception                        #
# --------------------------------------------------------------------------- #


def test_no_providers_reads_back_to_the_field_defaults() -> None:
    assert read_perception() == EMPTY_SENSE
    assert read_perception(NO_PROVIDERS) == EMPTY_SENSE


def test_read_perception_peeks_each_configured_provider() -> None:
    providers = SenseProviders(
        rms=lambda: 0.9,
        pat_event=lambda: ("side_pat", "level2"),
        face=lambda: "Bob",
        frame_available=lambda: True,
    )
    snap = read_perception(providers)
    assert snap.rms == 0.9
    assert snap.pat_event == ("side_pat", "level2")
    assert snap.face == "Bob"
    assert snap.frame_available is True


def test_read_perception_preserves_base_doa_fields() -> None:
    base = Sense(doa_angle=1.2, speech_detected=True)
    snap = read_perception(SenseProviders(rms=lambda: 0.1), base=base)
    assert snap.doa_angle == 1.2
    assert snap.speech_detected is True
    assert snap.rms == 0.1


def test_read_perception_swallows_every_provider_error() -> None:
    def _boom():
        raise RuntimeError("camera not available")

    providers = SenseProviders(rms=_boom, pat_event=_boom, face=_boom, frame_available=_boom)
    snap = read_perception(providers)  # must never raise
    assert snap.rms is None
    assert snap.pat_event is None
    assert snap.face is None
    assert snap.frame_available is False


def test_frame_available_provider_result_is_coerced_to_bool() -> None:
    snap = read_perception(SenseProviders(frame_available=lambda: "yes-a-frame"))
    assert snap.frame_available is True
    snap2 = read_perception(SenseProviders(frame_available=lambda: None))
    assert snap2.frame_available is False


def test_partial_providers_leave_the_rest_at_safe_defaults() -> None:
    snap = read_perception(SenseProviders(face=lambda: "Ada"))
    assert snap.face == "Ada"
    assert snap.rms is None
    assert snap.pat_event is None
    assert snap.frame_available is False


# --------------------------------------------------------------------------- #
# 3. Non-consuming ("peek") semantics — two consumers, one tick, one source   #
# --------------------------------------------------------------------------- #


class _PeekTakeHolder:
    """Stand-in for the shared per-tick holder the real folded hooks use (mirrors
    ``reachy.motion.listen_vision._FrameHolder``: ``peek()`` never clears the
    value, ``take()`` hands it out at most once). Sense's provider seam must be
    wired to something that behaves like ``peek`` — this fake exposes both so the
    contrast is explicit in the assertions below, without importing the real
    (numpy/threading-heavy) module from a stdlib-only test.
    """

    def __init__(self, value):
        self._value = value
        self._taken = False

    def peek(self):
        return self._value

    def take(self):
        if self._taken:
            return None
        self._taken = True
        return self._value


def test_two_consumers_reading_the_same_provider_see_the_same_tick_sample() -> None:
    """The core non-consuming-tap acceptance test.

    ONE shared holder (standing in for the one background grabber / one media
    sample) backs a SINGLE provider callable. Two independent "consumers" (e.g. a
    behavior and a cognition sink) each build their own Sense snapshot from that
    SAME provider for the SAME tick — proving a peek-style provider never starves
    a second reader, and that nothing here stood up a second holder/grabber (there
    is exactly one holder instance, shared by both calls below).
    """
    holder = _PeekTakeHolder(0.77)
    providers = SenseProviders(rms=holder.peek, frame_available=lambda: holder.peek() is not None)

    consumer_a = read_perception(providers)
    consumer_b = read_perception(providers)

    assert consumer_a.rms == 0.77
    assert consumer_b.rms == 0.77  # unchanged — a peek, not a take
    assert consumer_a.frame_available is True
    assert consumer_b.frame_available is True


def test_a_consuming_take_style_provider_would_starve_the_second_consumer() -> None:
    """Negative control: proves *why* providers must be peeks, not takes.

    Wiring the provider to a consuming ``take()`` (the anti-pattern this module's
    contract forbids) starves every consumer after the first — the second
    "reader" sees ``None`` even though nothing about the tick changed. Contrasting
    this against the previous test demonstrates the provider seam actually relies
    on peek semantics, rather than happening to pass only because a test calls it
    once.
    """
    holder = _PeekTakeHolder(0.5)
    providers = SenseProviders(rms=holder.take)

    consumer_a = read_perception(providers)
    consumer_b = read_perception(providers)

    assert consumer_a.rms == 0.5
    assert consumer_b.rms is None  # take() consumed it — proves take != peek


def test_read_perception_never_mutates_the_provider_bundle() -> None:
    calls = {"n": 0}

    def _rms():
        calls["n"] += 1
        return 1.0

    providers = SenseProviders(rms=_rms)
    for _ in range(5):
        read_perception(providers)
    assert calls["n"] == 5  # each read is independent; no caching/consuming state
    assert providers.rms is _rms  # the bundle itself is untouched (frozen)


# --------------------------------------------------------------------------- #
# 4. Dependency-free leaf — stdlib only, no reachy_mini / cv2 / numpy import  #
# --------------------------------------------------------------------------- #


def test_sense_module_stays_stdlib_only() -> None:
    """AST-check actual import statements (not prose) — sense.py's own docstring
    now *names* ``reachy_mini``/``cv2`` when explaining the provider contract, so
    a naive substring scan over the whole source would false-positive on that
    prose. Only real ``import``/``from ... import`` statements count.
    """
    import ast

    import reachy.behavior.sense as sense_mod

    tree = ast.parse(inspect.getsource(sense_mod))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".")[0])

    forbidden = {"reachy_mini", "cv2", "numpy"}  # stdlib-only per CLAUDE.md
    assert not (
        imported_roots & forbidden
    ), f"sense.py must stay a dependency-free leaf, found: {imported_roots & forbidden}"
