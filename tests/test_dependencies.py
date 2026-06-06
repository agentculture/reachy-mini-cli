"""
Verify the SDK-first-but-installable dependency split.

`numpy` (the RMS loudness detector) is a base dependency — a pure wheel that
installs everywhere. `reachy-mini` (the SDK) is the default ``listen`` transport
but stays an *extra* (``[sdk]`` / ``[daemon]``), because its transitive stack
(pycairo / gstreamer) needs system libraries absent on a bare box / in CI, so a
hard base dep would break ``uv sync``.
"""

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"


def _project() -> dict:
    with PYPROJECT.open("rb") as fh:
        return tomllib.load(fh)["project"]


def _base_deps() -> list[str]:
    return _project()["dependencies"]


def test_numpy_is_base_dep():
    """numpy must appear in [project.dependencies] (pure wheel, installs everywhere)."""
    deps = _base_deps()
    assert any(d.startswith("numpy") for d in deps), f"numpy not in base dependencies: {deps}"


def test_reachy_mini_is_not_a_base_dep():
    """reachy-mini must NOT be a base dep — its cairo/gstreamer stack breaks bare installs/CI."""
    deps = _base_deps()
    assert not any(
        d.startswith("reachy-mini") for d in deps
    ), f"reachy-mini must stay an extra, not base: {deps}"


def test_reachy_mini_is_in_sdk_and_daemon_extras():
    """reachy-mini must remain available via the [sdk] and [daemon] extras."""
    extras = _project()["optional-dependencies"]
    for name in ("sdk", "daemon"):
        assert any(
            d.startswith("reachy-mini") for d in extras.get(name, [])
        ), f"reachy-mini not found in the [{name}] extra: {extras.get(name)}"
