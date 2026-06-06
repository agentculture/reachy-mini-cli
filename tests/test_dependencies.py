"""
Verify that reachy-mini and numpy are base (non-optional) runtime dependencies.

These must live in [project.dependencies], not only in [project.optional-dependencies],
because the SDK transport and the numpy-based RMS detector are now the default path.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _load_base_deps() -> list[str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["dependencies"]


def test_reachy_mini_is_base_dep():
    """reachy-mini must appear in [project.dependencies]."""
    deps = _load_base_deps()
    assert any(
        d.startswith("reachy-mini") for d in deps
    ), f"reachy-mini not found in base dependencies: {deps}"


def test_numpy_is_base_dep():
    """numpy must appear in [project.dependencies]."""
    deps = _load_base_deps()
    assert any(d.startswith("numpy") for d in deps), f"numpy not found in base dependencies: {deps}"
