"""Focused registration contracts for the stateful presence generators."""

from __future__ import annotations

import ast
import inspect

import pytest

from reachy.behavior import library
from reachy.behavior.engine import Engine
from reachy.behavior.feel_alive import make_feel_alive
from reachy.behavior.model import Lifetime, StopClass
from reachy.behavior.pet_reaction import (
    DONE_GESTURE_S,
    MAX_CONTACT_S,
    NOMINAL_TICK_S,
    make_pet_reaction,
)
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import PatState, Sense
from reachy.cli._errors import CliError

ALL_CHANNELS = frozenset({"head", "antennas", "body_yaw"})


def test_feel_alive_keeps_its_public_contract_and_uses_fresh_factory() -> None:
    entry = library.get("feel-alive")

    assert entry.name == "feel-alive"
    assert entry.channels == ALL_CHANNELS
    assert entry.default_class is StopClass.PASSIVE
    assert entry.looping is True
    assert entry.default_duration is None
    assert entry.wants_sense is False
    assert entry.fn is None
    assert entry.make_fn is make_feel_alive
    assert entry.default_params() == {
        "energy": 1.0,
        "breathe_period": 5.0,
        "breathe_z": 3.0,
        "breathe_pitch": 2.0,
        "gaze_yaw": 12.0,
        "gaze_pitch": 7.0,
        "antenna": 12.0,
        "antenna_period": 6.0,
        "body_yaw": 6.0,
    }


def test_two_built_feel_alive_behaviors_have_distinct_cadence_state() -> None:
    entry = library.get("feel-alive")
    lifetime = Lifetime(looping=True, duration=None)
    first = library.build(
        entry.name, entry.default_params(), StopClass.PASSIVE, lifetime, "feel-alive-1"
    )
    second = library.build(
        entry.name, entry.default_params(), StopClass.PASSIVE, lifetime, "feel-alive-2"
    )

    assert first.fn is not second.fn
    before = second.contribution(0.0)
    first.contribution(100.0)  # grow only the first instance's private schedule
    assert second.contribution(0.0) == before


def test_pet_reaction_registration_is_sensor_driven_stoppable_and_bounded() -> None:
    entry = library.get("pet-reaction")

    assert entry.channels == ALL_CHANNELS
    assert entry.default_class is StopClass.STOPPABLE
    assert entry.looping is False
    assert entry.default_duration is not None
    assert entry.default_duration > MAX_CONTACT_S + DONE_GESTURE_S
    assert entry.wants_sense is True
    assert entry.params == {}
    assert entry.fn is None
    assert entry.make_fn is make_pet_reaction

    lifetime = library.resolve_lifetime(entry, once=False, loop=False, duration=None)
    assert lifetime == Lifetime(looping=False, duration=entry.default_duration)
    assert lifetime.errors() == []

    behavior = library.build(entry.name, {}, StopClass.STOPPABLE, lifetime, "pet-reaction-1")
    contact = Sense(
        pat_state=PatState(
            availability="available",
            contact=True,
            touch_type="scratch",
            level="level1",
            phase="receptive",
            phase_started_at=0.0,
            last_press_at=MAX_CONTACT_S,
        )
    )
    behavior.contribution(MAX_CONTACT_S, contact)
    completion_t = MAX_CONTACT_S + DONE_GESTURE_S + NOMINAL_TICK_S
    assert behavior.is_expired(completion_t) is False
    assert behavior.contribution(completion_t, contact).done is True


def test_pet_reaction_default_is_accepted_by_data_only_rules_without_duration() -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [
                {
                    "id": "pat-to-pet-reaction",
                    "when": {"field": "pat", "op": "is_true"},
                    "run": "pet-reaction",
                }
            ]
        }
    )

    assert cfg.react[0].behavior == "pet-reaction"
    assert cfg.react[0].duration_s is None


def test_two_built_pet_reactions_do_not_share_touch_state() -> None:
    entry = library.get("pet-reaction")
    lifetime = library.resolve_lifetime(entry, once=False, loop=False, duration=None)
    first = library.build(entry.name, {}, StopClass.STOPPABLE, lifetime, "pet-reaction-1")
    second = library.build(entry.name, {}, StopClass.STOPPABLE, lifetime, "pet-reaction-2")

    assert first.fn is not second.fn
    first.contribution(
        0.0,
        Sense(
            pat_state=PatState(
                availability="available",
                contact=True,
                touch_type="side_pat",
                level="level1",
                yaw_deg=3.0,
                phase="warning",
                phase_started_at=0.0,
                last_press_at=0.0,
            )
        ),
    )

    assert getattr(first.fn, "phase") == "warning"
    assert getattr(second.fn, "phase") is None


def test_base_seeding_remains_passive_and_mints_fresh_presence_instances() -> None:
    first_engine = Engine()
    second_engine = Engine()

    first_engine.seed_base_layer(now=0.0, energy=0.4)
    second_engine.seed_base_layer(now=0.0, energy=0.4)
    first = first_engine.active[0].behavior
    second = second_engine.active[0].behavior

    assert first.name == second.name == "feel-alive"
    assert first.stop_class is second.stop_class is StopClass.PASSIVE
    assert first.channels == second.channels == ALL_CHANNELS
    assert first.params["energy"] == second.params["energy"] == 0.4
    assert first.fn is not second.fn


def test_pet_reaction_parameter_validation_and_existing_catalog_stay_compatible() -> None:
    entry = library.get("pet-reaction")
    assert library.resolve_params(entry, {}) == {}
    with pytest.raises(CliError):
        library.resolve_params(entry, {"dynamic_code": "1"})

    existing = {
        "feel-alive",
        "gaze-hold",
        "nod",
        "shake",
        "speak",
        "thoughtful",
        "antenna-sway",
        "body-turn-hold",
    }
    assert existing < set(library.LIBRARY)
    assert "pet-reaction" in library.LIBRARY


def test_library_imports_no_legacy_reaction_or_motion_queue() -> None:
    tree = ast.parse(inspect.getsource(library))
    imported_modules: set[str] = set()
    imported_names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom):
            if node.module:
                imported_modules.add(node.module)
            imported_names.update(alias.name for alias in node.names)

    assert "reachy.motion.queue" not in imported_modules
    assert "reachy.motion.pat_reaction" not in imported_modules
    assert "MotionQueue" not in imported_names
    assert "PatReaction" not in imported_names
