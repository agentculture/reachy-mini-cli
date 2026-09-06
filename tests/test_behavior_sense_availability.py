"""Tests for ``reachy.behavior.sense_availability`` — per-sense availability in state.json.

Task t3 of the ``reachy-nervous-system`` plan (issue #120b). The defect this
covers: on the deployed robot the ``[vision]`` extra was missing, so
:func:`reachy.behavior.face_sense.build_face_recognition` logged ONE latched
warning at startup and the ``face`` / ``frame_available`` cues were never
populated again. Six hours of journal held zero face events, and nothing
distinguished "no face has been in view" from "this sense has been dead since
boot". The once-only warning is the right LOG policy; the defect was that it was
the only copy of the fact.

The three acceptance criteria, and the tests that carry them:

1. no ``[vision]`` -> ``senses.face.available is False`` with
   ``reason == "vision-extra-absent"``; with the extra ->
   ``available is True`` / ``reason is None``, from the SAME code driven only
   through the availability seam
   (:func:`test_the_face_probe_reports_vision_extra_absent_without_the_extra`,
   :func:`test_the_face_probe_reports_available_when_the_seams_say_both_extras_are_there`);
2. the block covers every :data:`reachy.behavior.sense._COMPOSED_PROVIDER_FIELDS`
   entry plus ``pat``, and a composed-but-dead sense always names its reason
   (:func:`test_the_declared_sense_set_is_the_composed_provider_fields_plus_pat`,
   :func:`test_every_sense_in_a_bare_box_block_names_a_reason`);
3. ``face_sense`` keeps its once-only ``_VISION_WARNED`` latch while exposing the
   reason string (:func:`test_the_vision_warning_stays_once_only`,
   :func:`test_face_sense_exposes_the_named_vision_extra_absent_reason`).

Task t7 (issue #176, c37/h27) adds a SEPARATE fact beside structural
``available``: per-sense ``live`` / ``last_frame_at``. See the tests under
"6. Liveness" below — the change gate and the senselog report must key on
``(available, reason, live)`` only, never on ``last_frame_at`` (which advances
every tick a frame arrives), or the rider would write and log every tick and
defeat the whole write-on-change discipline this module exists for. The
composition-level mirror test
(``tests/test_behavior_nervous_composition.py::test_the_retained_state_tree_equals_state_json_including_rider_keys``)
already asserts the retained ``reachy/state/senses`` tree equals ``state.json``
byte-for-byte via generic dict equality, so it needs no change to cover the two
new keys — but that composition seam does not wire a ``frame_liveness``
provider (task t12), so it only ever proves ``live``/``last_frame_at`` are
``None`` on both surfaces alike. The MEANINGFUL mirror check — the SAME dict
(all four keys, with a real ``live``/``last_frame_at`` pair) reaching both
``state.json`` and the block a retained-topic publisher would read — is
therefore a unit-level test here instead
(:func:`test_the_block_returned_for_the_state_write_is_the_same_dict_a_retained_mirror_would_publish`).
"""

from __future__ import annotations

import json
from dataclasses import FrozenInstanceError

import pytest

from reachy.behavior import control as control_mod
from reachy.behavior import face_sense, sense_availability
from reachy.behavior.face_sense import DEFAULT_STREAM_STALE_S
from reachy.behavior.sense import _COMPOSED_PROVIDER_FIELDS
from reachy.behavior.sense_availability import (
    AVAILABILITY_SENSES,
    AVAILABLE,
    PROBE_ERROR,
    SDK_EXTRA_ABSENT,
    STATE_KEY,
    SenseAvailability,
    SenseAvailabilityDriver,
    runtime_probes,
    sdk_unavailable_reason,
    unavailable,
)

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _spool(tmp_path, monkeypatch):
    """An isolated main command spool (the file the engine's state write targets)."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    return control_mod.CommandSpool()


@pytest.fixture(autouse=True)
def _restore_vision_latch():
    """Save/restore the process-wide ``_VISION_WARNED`` latch around each test.

    It is module state by design (the extra cannot appear mid-run), so a test
    that trips it would otherwise silently disarm a sibling's assertion.
    """
    saved = face_sense._VISION_WARNED
    yield
    face_sense._VISION_WARNED = saved


class _Ctx:
    """The only part of ``TickContext`` this driver reads: nothing."""

    now = 1.0


def _probes(**overrides):
    """A full, all-available probe map, with per-sense overrides."""
    probes = {name: (lambda: AVAILABLE) for name in AVAILABILITY_SENSES}
    probes.update(overrides)
    return probes


# --------------------------------------------------------------------------- #
# 1. SenseAvailability — a dead sense always NAMES its reason                  #
# --------------------------------------------------------------------------- #


def test_an_unavailable_sense_must_name_a_reason():
    with pytest.raises(ValueError, match="reason"):
        SenseAvailability(available=False, reason=None)
    with pytest.raises(ValueError, match="reason"):
        SenseAvailability(available=False, reason="   ")


def test_an_available_sense_must_not_carry_a_reason():
    with pytest.raises(ValueError, match="reason"):
        SenseAvailability(available=True, reason="vision-extra-absent")


def test_available_and_unavailable_render_the_wire_shape():
    assert AVAILABLE.as_dict() == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }
    assert unavailable("sdk-extra-absent").as_dict() == {
        "available": False,
        "reason": "sdk-extra-absent",
        "live": None,
        "last_frame_at": None,
    }


def test_sense_availability_is_frozen():
    # A LOCAL instance, not the module-level AVAILABLE: the assignment is meant
    # to fail, but a reader cannot tell that from the call site, and neither can
    # a linter — so do not appear to mutate a shared constant.
    probe = SenseAvailability(available=True, reason=None)
    with pytest.raises(FrozenInstanceError):
        probe.available = False  # type: ignore[misc]


# --------------------------------------------------------------------------- #
# 2. Coverage — the declared set is the ONE source of truth plus pat           #
# --------------------------------------------------------------------------- #


def test_the_declared_sense_set_is_the_composed_provider_fields_plus_pat():
    """Criterion 2, pinned by EQUALITY so it fails in both directions.

    ``_COMPOSED_PROVIDER_FIELDS`` is the one declared truth ``behavior rules
    check`` already lints against; a provider added there without an
    availability probe must fail here rather than silently vanish from the
    block.
    """
    assert AVAILABILITY_SENSES == frozenset(_COMPOSED_PROVIDER_FIELDS) | {"pat"}


def test_the_block_keys_are_exactly_the_declared_senses(_spool):
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    assert set(driver.block()) == set(AVAILABILITY_SENSES)


def test_a_probe_map_missing_a_declared_sense_is_refused(_spool):
    probes = _probes()
    probes.pop("face")
    with pytest.raises(ValueError, match="face"):
        SenseAvailabilityDriver(probes, main_control=_spool)


def test_a_probe_map_naming_an_unknown_sense_is_refused(_spool):
    probes = _probes()
    probes["telepathy"] = lambda: AVAILABLE
    with pytest.raises(ValueError, match="telepathy"):
        SenseAvailabilityDriver(probes, main_control=_spool)


def test_a_raising_probe_reports_unavailable_with_a_named_reason(_spool):
    def boom():
        raise RuntimeError("nope")

    driver = SenseAvailabilityDriver(_probes(rms=boom), main_control=_spool)
    assert driver.block()["rms"] == {
        "available": False,
        "reason": PROBE_ERROR,
        "live": None,
        "last_frame_at": None,
    }


def test_a_probe_returning_a_non_availability_reports_a_named_reason(_spool):
    driver = SenseAvailabilityDriver(_probes(rms=lambda: "yes"), main_control=_spool)
    assert driver.block()["rms"] == {
        "available": False,
        "reason": PROBE_ERROR,
        "live": None,
        "last_frame_at": None,
    }


# --------------------------------------------------------------------------- #
# 3. The seam-rider write into state.json                                      #
# --------------------------------------------------------------------------- #


def test_the_block_lands_under_the_senses_key(_spool):
    driver = SenseAvailabilityDriver(
        _probes(face=lambda: unavailable("vision-extra-absent")), main_control=_spool
    )
    driver(_Ctx())
    state = _spool.read_state()
    assert state is not None
    assert state[STATE_KEY]["face"] == {
        "available": False,
        "reason": "vision-extra-absent",
        "live": None,
        "last_frame_at": None,
    }


def test_publishing_preserves_every_other_state_key(_spool):
    """The rider is ADDITIVE: it merges, exactly like the IntentDriver's view."""
    _spool.write_state({"updated": 12.5, "active": [], "intents": {"goal": None}})
    SenseAvailabilityDriver(_probes(), main_control=_spool)(_Ctx())
    state = _spool.read_state()
    assert state["updated"] == 12.5
    assert state["active"] == []
    assert state["intents"] == {"goal": None}
    assert set(state[STATE_KEY]) == set(AVAILABILITY_SENSES)


def test_an_engine_heartbeat_write_is_repaired_on_the_next_tick(_spool):
    """The engine writes the un-augmented base shape; the rider restores its key.

    This is why the rider re-checks every tick instead of publishing once: the
    engine's periodic ``control.write_state(engine.state(...))`` heartbeat has no
    idea this module exists and clobbers the whole file.
    """
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    driver(_Ctx())
    assert STATE_KEY in _spool.read_state()

    _spool.write_state({"updated": 99.0})  # the engine's own heartbeat write
    assert STATE_KEY not in _spool.read_state()

    driver(_Ctx())
    state = _spool.read_state()
    assert state["updated"] == 99.0
    assert set(state[STATE_KEY]) == set(AVAILABILITY_SENSES)


def test_an_unchanged_block_is_not_rewritten(_spool, monkeypatch):
    """Availability is structural, so the steady state costs a read, not a write."""
    writes: list[dict] = []
    real_write = _spool.write_state
    monkeypatch.setattr(
        _spool, "write_state", lambda state: (writes.append(state), real_write(state))[1]
    )
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    for _ in range(10):
        driver(_Ctx())
    assert len(writes) == 1


def test_a_changed_block_is_republished(_spool):
    flag = {"dead": False}
    driver = SenseAvailabilityDriver(
        _probes(transcript=lambda: unavailable("session-down") if flag["dead"] else AVAILABLE),
        main_control=_spool,
    )
    driver(_Ctx())
    assert _spool.read_state()[STATE_KEY]["transcript"]["available"] is True
    flag["dead"] = True
    driver(_Ctx())
    assert _spool.read_state()[STATE_KEY]["transcript"] == {
        "available": False,
        "reason": "session-down",
        "live": None,
        "last_frame_at": None,
    }


def test_a_failing_state_write_never_raises_out_of_the_tick(_spool, monkeypatch):
    """A sense tap must never crash the 50 Hz loop — the whole-repo discipline."""

    def boom(_state):
        raise OSError("read-only filesystem")

    monkeypatch.setattr(_spool, "write_state", boom)
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    driver(_Ctx())  # must not raise


def test_a_corrupt_state_file_is_treated_as_empty(_spool, tmp_path):
    control_mod.state_file().write_text("{ not json", encoding="utf-8")
    SenseAvailabilityDriver(_probes(), main_control=_spool)(_Ctx())
    state = json.loads(control_mod.state_file().read_text(encoding="utf-8"))
    assert set(state[STATE_KEY]) == set(AVAILABILITY_SENSES)


def test_the_block_is_json_serialisable(_spool):
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    assert json.loads(json.dumps(driver.block())) == driver.block()


# --------------------------------------------------------------------------- #
# 4. runtime_probes — the composition's structural verdicts                    #
# --------------------------------------------------------------------------- #


def test_the_face_probe_reports_vision_extra_absent_without_the_extra(monkeypatch):
    """Criterion 1, first half — and the #120 scenario verbatim.

    The deployed box HAD the ``[sdk]`` extra and lacked ``[vision]``; the reason
    reported must be the one an operator can act on, so the vision leg is checked
    FIRST.
    """
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)  # no cv2
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: object())  # sdk present
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=False)
    assert probes["face"]().as_dict() == {
        "available": False,
        "reason": face_sense.VISION_EXTRA_ABSENT,
        "live": None,
        "last_frame_at": None,
    }


def test_the_face_probe_reports_available_when_the_seams_say_both_extras_are_there(monkeypatch):
    """Criterion 1, second half — SAME code, only the availability seam moved."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())  # cv2 present
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: object())  # sdk present
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=True)
    assert probes["face"]().as_dict() == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }


def test_the_face_probe_names_a_broken_vision_stack_separately(monkeypatch):
    """cv2 importable but the recognizer failed to build — a DIFFERENT named fact."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: object())
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=False)
    assert probes["face"]().as_dict() == {
        "available": False,
        "reason": face_sense.VISION_STACK_UNAVAILABLE,
        "live": None,
        "last_frame_at": None,
    }


def test_the_face_probe_falls_through_to_the_sdk_leg(monkeypatch):
    """With vision present but no SDK there are no frames — face is still dead."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())
    monkeypatch.setattr(sense_availability, "_find_spec", lambda name: None)
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=True)
    assert probes["face"]().as_dict() == {
        "available": False,
        "reason": SDK_EXTRA_ABSENT,
        "live": None,
        "last_frame_at": None,
    }


@pytest.mark.parametrize("name", ["rms", "rms_ratio", "transcript", "frame_available", "pat"])
def test_the_media_backed_senses_report_sdk_extra_absent_on_a_bare_box(monkeypatch, name):
    monkeypatch.setattr(sense_availability, "_find_spec", lambda module: None)
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=False)
    assert probes[name]().as_dict() == {
        "available": False,
        "reason": SDK_EXTRA_ABSENT,
        "live": None,
        "last_frame_at": None,
    }


def test_pat_and_pat_event_are_the_same_verdict_under_two_vocabularies(monkeypatch):
    monkeypatch.setattr(sense_availability, "_find_spec", lambda module: None)
    probes = runtime_probes(pat_composed=True, face_recognizer_ready=False)
    assert probes["pat"]().as_dict() == probes["pat_event"]().as_dict()


def test_an_uncomposed_pat_stack_is_named_as_such(monkeypatch):
    """``REACHY_PAT_SENSE=0`` is a composition switch, not a missing extra."""
    monkeypatch.setattr(sense_availability, "_find_spec", lambda module: object())
    probes = runtime_probes(pat_composed=False, face_recognizer_ready=True)
    assert probes["pat"]().as_dict() == {
        "available": False,
        "reason": sense_availability.PAT_SENSE_DISABLED,
        "live": None,
        "last_frame_at": None,
    }


def test_self_moving_is_available_on_the_barest_box(monkeypatch):
    """It reads only ``ctx.pose`` — no extra, no hardware, so never structurally dead."""
    monkeypatch.setattr(sense_availability, "_find_spec", lambda module: None)
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    probes = runtime_probes(pat_composed=False, face_recognizer_ready=False)
    assert probes["self_moving"]().as_dict() == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }


def test_every_sense_in_a_bare_box_block_names_a_reason(monkeypatch, _spool):
    """Criterion 2's second half: composed-but-dead always carries a NAMED reason."""
    monkeypatch.setattr(sense_availability, "_find_spec", lambda module: None)
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    driver = SenseAvailabilityDriver(
        runtime_probes(pat_composed=True, face_recognizer_ready=False), main_control=_spool
    )
    block = driver.block()
    assert set(block) == set(AVAILABILITY_SENSES)
    for name, entry in block.items():
        if entry["available"]:
            assert entry["reason"] is None, name
        else:
            assert isinstance(entry["reason"], str) and entry["reason"].strip(), name


def test_runtime_probes_covers_exactly_the_declared_senses():
    assert set(runtime_probes(pat_composed=True, face_recognizer_ready=True)) == set(
        AVAILABILITY_SENSES
    )


def test_the_sdk_probe_is_injectable_as_well_as_monkeypatchable():
    assert sdk_unavailable_reason(find_spec=lambda name: None) == SDK_EXTRA_ABSENT
    assert sdk_unavailable_reason(find_spec=lambda name: object()) is None


def test_the_sdk_probe_asks_about_the_sdk_package():
    asked: list[str] = []
    sdk_unavailable_reason(find_spec=lambda name: asked.append(name))
    assert asked == ["reachy_mini"]


# --------------------------------------------------------------------------- #
# 5. face_sense keeps its latch AND exposes the fact (criterion 3)             #
# --------------------------------------------------------------------------- #


def test_face_sense_exposes_the_named_vision_extra_absent_reason(monkeypatch):
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    assert face_sense.vision_unavailable_reason() == "vision-extra-absent"
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())
    assert face_sense.vision_unavailable_reason() is None


def test_the_vision_reason_probe_asks_about_cv2():
    asked: list[str] = []
    face_sense.vision_unavailable_reason(find_spec=lambda name: asked.append(name))
    assert asked == ["cv2"]


def test_face_recognition_reason_prefers_the_missing_extra_over_a_broken_stack(monkeypatch):
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    assert face_sense.face_recognition_unavailable_reason(False) == face_sense.VISION_EXTRA_ABSENT
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: object())
    assert (
        face_sense.face_recognition_unavailable_reason(False) == face_sense.VISION_STACK_UNAVAILABLE
    )
    assert face_sense.face_recognition_unavailable_reason(True) is None


def test_the_vision_warning_stays_once_only(monkeypatch, caplog):
    """Criterion 3: the log latch is UNCHANGED — the block is a second copy, not a swap."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    monkeypatch.setattr(face_sense, "_VISION_WARNED", False)
    with caplog.at_level("WARNING", logger="reachy.behavior.face_sense"):
        assert face_sense.build_face_recognition() is None
        assert face_sense.build_face_recognition() is None
        assert face_sense.build_face_recognition() is None
    warnings = [r for r in caplog.records if r.levelname == "WARNING"]
    assert len(warnings) == 1
    assert "[vision]" in warnings[0].getMessage()


def test_the_reason_survives_the_spent_warning_latch(monkeypatch):
    """The whole point of #120b: after the one boot line, the fact is still readable."""
    monkeypatch.setattr(face_sense, "_find_spec", lambda name: None)
    monkeypatch.setattr(face_sense, "_VISION_WARNED", True)  # the boot line is long gone
    assert face_sense.vision_unavailable_reason() == face_sense.VISION_EXTRA_ABSENT


# --------------------------------------------------------------------------- #
# 6. Liveness (t7, issue #176) — a SEPARATE fact, keyed apart from last_frame_at #
# --------------------------------------------------------------------------- #


def test_live_true_requires_a_last_frame_at():
    with pytest.raises(ValueError, match="last_frame_at"):
        SenseAvailability(available=True, reason=None, live=True, last_frame_at=None)


def test_live_false_or_none_never_requires_a_last_frame_at():
    # None of these raise.
    SenseAvailability(available=True, reason=None, live=False, last_frame_at=None)
    SenseAvailability(available=True, reason=None, live=None, last_frame_at=None)
    SenseAvailability(available=False, reason="sdk-extra-absent", live=None, last_frame_at=None)


def test_as_dict_renders_all_four_keys():
    assert AVAILABLE.as_dict() == {
        "available": True,
        "reason": None,
        "live": None,
        "last_frame_at": None,
    }
    verdict = SenseAvailability(available=True, reason=None, live=True, last_frame_at=42.0)
    assert verdict.as_dict() == {
        "available": True,
        "reason": None,
        "live": True,
        "last_frame_at": 42.0,
    }


def test_a_sense_with_no_liveness_provider_renders_live_null(_spool):
    """No ``frame_liveness`` injected (today's composition, task t12 not yet done)."""
    driver = SenseAvailabilityDriver(_probes(), main_control=_spool)
    block = driver.block()
    for name, entry in block.items():
        assert entry["live"] is None, name
        assert entry["last_frame_at"] is None, name


def test_the_camera_derived_senses_report_live_from_the_injected_provider(_spool):
    driver = SenseAvailabilityDriver(
        _probes(),
        main_control=_spool,
        frame_liveness=lambda: 500.0,
        clock=lambda: 500.5,
    )
    block = driver.block()
    for name in ("frame_available", "face"):
        assert block[name]["live"] is True, name
        assert block[name]["last_frame_at"] == 500.0, name


def test_a_stream_older_than_the_stale_threshold_reports_dead_not_none(_spool):
    """The threshold is IMPORTED from face_sense, never restated."""
    driver = SenseAvailabilityDriver(
        _probes(),
        main_control=_spool,
        frame_liveness=lambda: 0.0,
        clock=lambda: DEFAULT_STREAM_STALE_S + 0.001,
    )
    block = driver.block()
    assert block["frame_available"]["live"] is False
    assert block["frame_available"]["last_frame_at"] == 0.0  # the stamp is kept, not erased


def test_a_stream_exactly_at_the_stale_threshold_still_counts_as_live(_spool):
    driver = SenseAvailabilityDriver(
        _probes(),
        main_control=_spool,
        frame_liveness=lambda: 0.0,
        clock=lambda: DEFAULT_STREAM_STALE_S,
    )
    assert driver.block()["frame_available"]["live"] is True


def test_no_frame_yet_reports_live_false_not_none(_spool):
    """A provider IS injected but has nothing to report yet — a known ``False``,
    never the "no provider at all" ``None``."""
    driver = SenseAvailabilityDriver(
        _probes(), main_control=_spool, frame_liveness=lambda: None, clock=lambda: 10.0
    )
    block = driver.block()
    assert block["frame_available"]["live"] is False
    assert block["frame_available"]["last_frame_at"] is None


def test_an_unavailable_camera_sense_still_renders_live_none(_spool):
    """Structural unavailability wins — liveness is meaningless with nothing behind it."""
    driver = SenseAvailabilityDriver(
        _probes(face=lambda: unavailable("vision-extra-absent")),
        main_control=_spool,
        frame_liveness=lambda: 10.0,
        clock=lambda: 10.0,
    )
    block = driver.block()
    assert block["face"] == {
        "available": False,
        "reason": "vision-extra-absent",
        "live": None,
        "last_frame_at": None,
    }


def test_a_non_camera_sense_ignores_the_frame_liveness_provider(_spool):
    driver = SenseAvailabilityDriver(
        _probes(), main_control=_spool, frame_liveness=lambda: 10.0, clock=lambda: 10.0
    )
    block = driver.block()
    assert block["rms"]["live"] is None
    assert block["rms"]["last_frame_at"] is None


def test_a_steady_live_stream_costs_one_write_and_one_log_pass(_spool, monkeypatch):
    """60 s of fake ticks with ``last_frame_at`` advancing every tick: after the
    first publish, zero further ``state.json`` writes and zero further log
    lines — the change gate must ignore ``last_frame_at`` (which advances every
    tick a frame arrives), or the rider would write and log every tick and
    defeat the whole write-on-change discipline this module exists for.
    """
    now = {"t": 1000.0}

    def clock():
        return now["t"]

    def frame_liveness():
        return now["t"]  # a fresh frame every tick

    writes: list[dict] = []
    real_write = _spool.write_state
    monkeypatch.setattr(
        _spool, "write_state", lambda state: (writes.append(state), real_write(state))[1]
    )
    logged: list[tuple] = []
    monkeypatch.setattr(
        sense_availability.senselog, "stage", lambda *a: logged.append(("stage", a))
    )
    monkeypatch.setattr(sense_availability.senselog, "drop", lambda *a: logged.append(("drop", a)))

    driver = SenseAvailabilityDriver(
        _probes(), main_control=_spool, frame_liveness=frame_liveness, clock=clock
    )
    driver(_Ctx())
    assert len(writes) == 1
    assert len(logged) > 0  # the first publish reports every sense once

    logged.clear()
    for _ in range(60):
        now["t"] += 1.0
        driver(_Ctx())

    assert len(writes) == 1, "last_frame_at advancing every tick must not trigger a rewrite"
    assert logged == [], "last_frame_at advancing every tick must not log a line"


def test_a_live_flip_writes_once_and_logs_exactly_one_line(_spool, monkeypatch):
    now = {"t": 1000.0}
    frame = {"at": 1000.0}

    def clock():
        return now["t"]

    def frame_liveness():
        return frame["at"]

    # Isolate the flip to ONE sense: face stays structurally unavailable
    # throughout, so only frame_available's liveness can change.
    probes = _probes(face=lambda: unavailable("vision-extra-absent"))
    driver = SenseAvailabilityDriver(
        probes, main_control=_spool, frame_liveness=frame_liveness, clock=clock
    )
    driver(_Ctx())  # settle the first publish

    writes: list[dict] = []
    real_write = _spool.write_state
    monkeypatch.setattr(
        _spool, "write_state", lambda state: (writes.append(state), real_write(state))[1]
    )
    logged: list[tuple] = []
    monkeypatch.setattr(
        sense_availability.senselog, "stage", lambda *a: logged.append(("stage", a))
    )
    monkeypatch.setattr(sense_availability.senselog, "drop", lambda *a: logged.append(("drop", a)))

    # the stream goes stale: no new frame, the clock advances past the threshold
    now["t"] += DEFAULT_STREAM_STALE_S + 1.0
    driver(_Ctx())

    assert len(writes) == 1
    assert len(logged) == 1
    kind, args = logged[0]
    assert kind == "drop"
    state = _spool.read_state()
    assert state[STATE_KEY]["frame_available"]["live"] is False


def test_the_block_returned_for_the_state_write_is_the_same_dict_a_retained_mirror_would_publish(
    _spool,
):
    """The unit-level stand-in for the (unwired) composition-level mirror check.

    ``NervousPublisher.state_writer`` wraps whatever writes ``state.json``, so
    ANY mirror simply republishes whatever the ONE writer wrote — there is only
    ever one builder. Pin that here at the point the two new keys are actually
    populated (today's composition wires no ``frame_liveness`` provider at all,
    task t12), so the property is proven directly rather than merely inherited
    from the pre-existing composition test's generic (and, for these two keys,
    all-``None``) dict equality.
    """
    driver = SenseAvailabilityDriver(
        _probes(),
        main_control=_spool,
        frame_liveness=lambda: 500.0,
        clock=lambda: 500.5,
    )
    driver(_Ctx())
    block = driver.block()
    state = _spool.read_state()
    assert state[STATE_KEY] == block
    assert block["frame_available"]["live"] is True
    assert block["frame_available"]["last_frame_at"] == 500.0


def test_a_pre_upgrade_senses_block_without_liveness_keys_is_compared_not_crashed():
    """PR #187 review: a state.json persisted by an older runtime carries only
    available/reason; the key must tolerate it or liveness is never written."""
    from reachy.behavior.sense_availability import SenseAvailabilityDriver

    key = SenseAvailabilityDriver._key(
        {"face": {"available": True, "reason": None}, "junk": "not-a-dict"}
    )
    assert key == {"face": (True, None, None)}
