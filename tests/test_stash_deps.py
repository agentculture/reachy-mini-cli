"""Task t4 acceptance criterion 3: the stash package adds no new base dependency.

Reads ``pyproject.toml`` directly and asserts ``[project.dependencies]`` is
EXACTLY ``numpy`` + ``harmonics-cli`` — the stash's embeddings client (stdlib
``urllib``) and cosine search (the already-base ``numpy``) introduce nothing new.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

_EXPECTED_BASE_DEPS = {"numpy>=1.24", "harmonics-cli>=0.8"}


def _base_deps() -> list[str]:
    with PYPROJECT.open("rb") as fh:
        data = tomllib.load(fh)
    return data["project"]["dependencies"]


def test_base_dependencies_are_exactly_numpy_and_harmonics_cli():
    deps = _base_deps()
    assert set(deps) == _EXPECTED_BASE_DEPS, (
        f"base [project.dependencies] changed: {deps!r} — the behavior stash "
        "(reachy/stash/) must ride stdlib urllib + the already-base numpy only"
    )
    assert len(deps) == len(_EXPECTED_BASE_DEPS), f"unexpected duplicate/base dep entries: {deps!r}"


if __name__ == "__main__":  # pragma: no cover
    import pytest

    raise SystemExit(pytest.main([__file__, "-v"]))
