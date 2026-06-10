"""Tests for reachy.speech.expressions — emoji-keyed expression catalog + loader.

Acceptance criteria:
  1. expressions.toml ships a starter set keyed by emoji; load_catalog() returns an
     ExpressionPose per emoji with the expected head/antenna/body_yaw fields.
  2. An unknown/absent emoji passed to get_pose() returns the NEUTRAL fallback, not an error.
  3. Editing an emoji's pose entry in the data file changes the loaded pose with no code
     change (round-trip: write a custom .toml, load it, assert the values round-trip).
  4. The data file uses stdlib tomllib (no new dependency) and the module is documented.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

from reachy.speech.expressions import (
    NEUTRAL_KEY,
    Catalog,
    ExpressionPose,
    get_pose,
    load_catalog,
)

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_TOML_DIR = Path(__file__).parent.parent / "reachy" / "speech"
_TOML_PATH = _TOML_DIR / "expressions.toml"


# ---------------------------------------------------------------------------
# Acceptance criterion 1 — starter set loaded; ExpressionPose has expected shape
# ---------------------------------------------------------------------------


def test_toml_file_exists() -> None:
    """expressions.toml ships alongside the package."""
    assert _TOML_PATH.exists(), f"Missing data file: {_TOML_PATH}"


def test_load_catalog_returns_dict_of_expression_poses() -> None:
    """load_catalog() returns a dict[str, ExpressionPose] for the default file."""
    catalog = load_catalog()
    assert isinstance(catalog, dict)
    assert len(catalog) >= 5, "Starter set must have at least 5 emoji entries"
    for emoji, pose in catalog.items():
        assert isinstance(emoji, str), f"Key must be str, got {type(emoji)}"
        assert isinstance(pose, ExpressionPose), f"Value must be ExpressionPose, got {type(pose)}"


def test_expression_pose_has_required_fields() -> None:
    """ExpressionPose carries head (x/y/z/roll/pitch/yaw), antennas, body_yaw."""
    catalog = load_catalog()
    # Use the neutral pose as a stable reference
    neutral = catalog[NEUTRAL_KEY]
    # Head fields
    assert hasattr(neutral, "head_x")
    assert hasattr(neutral, "head_y")
    assert hasattr(neutral, "head_z")
    assert hasattr(neutral, "head_roll")
    assert hasattr(neutral, "head_pitch")
    assert hasattr(neutral, "head_yaw")
    # Antenna fields
    assert hasattr(neutral, "antenna_right")
    assert hasattr(neutral, "antenna_left")
    # Body
    assert hasattr(neutral, "body_yaw")


def test_neutral_pose_is_at_zero() -> None:
    """The neutral fallback pose is centred (all zeros)."""
    catalog = load_catalog()
    neutral = catalog[NEUTRAL_KEY]
    assert neutral.head_x == 0.0
    assert neutral.head_y == 0.0
    assert neutral.head_z == 0.0
    assert neutral.head_roll == 0.0
    assert neutral.head_pitch == 0.0
    assert neutral.head_yaw == 0.0
    assert neutral.antenna_right == 0.0
    assert neutral.antenna_left == 0.0
    assert neutral.body_yaw == 0.0


def test_starter_set_has_expected_emoji_keys() -> None:
    """The starter set ships at least a few recognisable emoji keys."""
    catalog = load_catalog()
    keys = set(catalog.keys())
    # These are the committed starter emoji; any subset matching is fine.
    expected_sample = {"🤔", "😮", "🙂"}
    assert (
        expected_sample & keys
    ), f"Starter set missing expected emoji. Got: {keys!r}, expected any of {expected_sample!r}"


def test_expression_pose_is_frozen() -> None:
    """ExpressionPose is a frozen dataclass — immutable after construction."""
    catalog = load_catalog()
    neutral = catalog[NEUTRAL_KEY]
    with pytest.raises((AttributeError, TypeError)):
        neutral.head_x = 99.0  # type: ignore[misc]


def test_head_pose_values_are_float() -> None:
    """All numeric fields in ExpressionPose are floats, not ints."""
    catalog = load_catalog()
    for emoji, pose in catalog.items():
        for field in (
            pose.head_x,
            pose.head_y,
            pose.head_z,
            pose.head_roll,
            pose.head_pitch,
            pose.head_yaw,
            pose.antenna_right,
            pose.antenna_left,
            pose.body_yaw,
        ):
            assert isinstance(
                field, float
            ), f"Pose field for {emoji!r} is {type(field).__name__}, expected float"


# ---------------------------------------------------------------------------
# Acceptance criterion 2 — unknown emoji → NEUTRAL fallback, not an error
# ---------------------------------------------------------------------------


def test_get_pose_unknown_emoji_returns_neutral() -> None:
    """get_pose() with an unknown emoji returns the neutral pose, not an error."""
    neutral = get_pose(NEUTRAL_KEY)
    unknown = get_pose("🦄")  # not in the catalog
    assert unknown == neutral


def test_get_pose_absent_string_returns_neutral() -> None:
    """get_pose() with an arbitrary missing string returns neutral."""
    neutral = get_pose(NEUTRAL_KEY)
    assert get_pose("not-an-emoji") == neutral
    assert get_pose("") == neutral


def test_get_pose_does_not_raise() -> None:
    """get_pose() never raises for any string input."""
    for key in ["🦄", "🎸", "", "bogus", "😀😀😀"]:
        try:
            get_pose(key)
        except Exception as exc:  # noqa: BLE001
            pytest.fail(f"get_pose({key!r}) unexpectedly raised {exc!r}")


# ---------------------------------------------------------------------------
# Acceptance criterion 3 — round-trip: edit data file → changed loaded pose
# ---------------------------------------------------------------------------


def test_round_trip_custom_toml(tmp_path: Path) -> None:
    """Writing a custom .toml and loading it reflects the edited values exactly."""
    custom_toml = tmp_path / "expressions.toml"
    # Write a minimal valid TOML with one emoji and the required neutral entry
    custom_toml.write_text(
        "[neutral]\n"
        "head_x = 0.0\n"
        "head_y = 0.0\n"
        "head_z = 0.0\n"
        "head_roll = 0.0\n"
        "head_pitch = 0.0\n"
        "head_yaw = 0.0\n"
        "antenna_right = 0.0\n"
        "antenna_left = 0.0\n"
        "body_yaw = 0.0\n"
        "\n"
        '["🤔"]\n'
        "head_x = 1.5\n"
        "head_y = 2.5\n"
        "head_z = 3.5\n"
        "head_roll = 4.5\n"
        "head_pitch = 5.5\n"
        "head_yaw = 6.5\n"
        "antenna_right = 7.5\n"
        "antenna_left = 8.5\n"
        "body_yaw = 9.5\n",
        encoding="utf-8",
    )
    catalog = load_catalog(str(custom_toml))
    pose = catalog["🤔"]
    assert pose.head_x == 1.5
    assert pose.head_y == 2.5
    assert pose.head_z == 3.5
    assert pose.head_roll == 4.5
    assert pose.head_pitch == 5.5
    assert pose.head_yaw == 6.5
    assert pose.antenna_right == 7.5
    assert pose.antenna_left == 8.5
    assert pose.body_yaw == 9.5


def test_round_trip_edit_changes_pose(tmp_path: Path) -> None:
    """Editing a value in the .toml and reloading reflects the change (no code change)."""
    custom_toml = tmp_path / "expressions.toml"
    base_toml = (
        "[neutral]\n"
        "head_x = 0.0\n"
        "head_y = 0.0\n"
        "head_z = 0.0\n"
        "head_roll = 0.0\n"
        "head_pitch = 0.0\n"
        "head_yaw = 0.0\n"
        "antenna_right = 0.0\n"
        "antenna_left = 0.0\n"
        "body_yaw = 0.0\n"
        "\n"
        '["😮"]\n'
        "head_x = 0.0\n"
        "head_y = 0.0\n"
        "head_z = 5.0\n"
        "head_roll = 0.0\n"
        "head_pitch = -8.0\n"
        "head_yaw = 0.0\n"
        "antenna_right = 30.0\n"
        "antenna_left = 30.0\n"
        "body_yaw = 0.0\n"
    )
    custom_toml.write_text(base_toml, encoding="utf-8")
    catalog_before = load_catalog(str(custom_toml))
    assert catalog_before["😮"].head_pitch == -8.0

    # Simulate a developer editing the file — change head_pitch to -15.0
    edited_toml = base_toml.replace("head_pitch = -8.0", "head_pitch = -15.0")
    custom_toml.write_text(edited_toml, encoding="utf-8")
    catalog_after = load_catalog(str(custom_toml))
    assert (
        catalog_after["😮"].head_pitch == -15.0
    ), "Editing the .toml should change the loaded pose without any code change"


# ---------------------------------------------------------------------------
# Acceptance criterion 4 — stdlib tomllib only; module is documented
# ---------------------------------------------------------------------------


def test_no_new_runtime_deps() -> None:
    """expressions.py uses only stdlib (tomllib, pathlib, dataclasses) — no new deps."""
    # Collect top-level imports from the module's source
    import inspect

    import reachy.speech.expressions as mod

    source = inspect.getsource(mod)
    forbidden = ["yaml", "toml ", "rtoml", "pytoml", "tomli "]
    for dep in forbidden:
        assert dep not in source, f"Forbidden dependency found in expressions.py: {dep!r}"
    # tomllib must be used (stdlib, Python 3.12+)
    assert "tomllib" in source, "expressions.py should import stdlib tomllib"


def test_module_has_docstring() -> None:
    """reachy.speech.expressions has a module-level docstring."""
    import reachy.speech.expressions as mod

    assert mod.__doc__, "expressions.py must have a module-level docstring"
    # The docstring should mention TOML and editable format
    assert "toml" in mod.__doc__.lower() or "TOML" in mod.__doc__


def test_toml_file_has_header_comment() -> None:
    """expressions.toml has a human-readable header comment explaining the format."""
    content = _TOML_PATH.read_text(encoding="utf-8")
    # The file should start with a # comment block
    assert content.lstrip().startswith(
        "#"
    ), "expressions.toml should begin with a # comment documenting the format"


def test_toml_is_valid_tomllib() -> None:
    """expressions.toml is valid TOML that tomllib can parse without errors."""
    content = _TOML_PATH.read_bytes()
    parsed = tomllib.loads(content.decode("utf-8"))
    assert isinstance(parsed, dict)
    assert len(parsed) >= 5


# ---------------------------------------------------------------------------
# Catalog class API
# ---------------------------------------------------------------------------


def test_catalog_get_known_emoji() -> None:
    """Catalog.get(known_emoji) returns the ExpressionPose for that emoji."""
    catalog = Catalog()
    for key in load_catalog():
        pose = catalog.get(key)
        assert isinstance(pose, ExpressionPose)


def test_catalog_get_unknown_returns_neutral() -> None:
    """Catalog.get(unknown) returns the same pose as get_pose(NEUTRAL_KEY)."""
    catalog = Catalog()
    neutral = catalog.get(NEUTRAL_KEY)
    assert catalog.get("🦄") == neutral


def test_catalog_len() -> None:
    """len(Catalog()) equals the number of entries in the .toml."""
    catalog = Catalog()
    toml_count = len(load_catalog())
    assert len(catalog) == toml_count


def test_catalog_contains() -> None:
    """'emoji' in Catalog() works correctly."""
    catalog = Catalog()
    assert NEUTRAL_KEY in catalog
    assert "🦄🦄🦄" not in catalog


def test_get_pose_uses_default_catalog() -> None:
    """get_pose() is a module-level convenience that uses the default catalog."""
    catalog = Catalog()
    for key in load_catalog():
        assert get_pose(key) == catalog.get(key)
