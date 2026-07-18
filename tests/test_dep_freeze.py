"""Dep-freeze check for the offline CI lane (task t12).

CLAUDE.md's "Hard constraints" section pins exactly two base runtime
dependencies — ``numpy`` (the RMS loudness detector) and ``harmonics-cli`` (the
harmonic voice backend) — both pure wheels with no system-library transitive
deps, so a bare ``pip install reachy-mini-cli`` works everywhere (including a
fully offline CI runner). Every other engine package (``reachy-mini``, the
``[cpu]``/``[gpu]`` wake-word backends, ``opencv-python-headless``) MUST stay
behind an extra. This module asserts that invariant directly against
``pyproject.toml`` so a stray new base dependency fails the offline lane loudly,
rather than only being caught by a human reviewing a diff.

``tests/test_dependencies.py`` already covers this same invariant (and more,
per-extra) from its own angle; this is a deliberately thin, ``offline``-marked
companion that drives the identical seam (parse ``pyproject.toml`` with
``tomllib``) so the invariant is *also* proven inside the offline lane, without
editing a file another task owns.
"""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.offline

_PYPROJECT = Path(__file__).parent.parent / "pyproject.toml"

#: The exact base dependency NAME set — matched on name only (split on the
#: first version specifier), so a version-pin bump (e.g. numpy>=1.24 ->
#: numpy>=1.26) never false-fails this test; only a package being ADDED or
#: REMOVED from [project.dependencies] does.
_EXPECTED_BASE_DEP_NAMES = {"numpy", "harmonics-cli"}


def _base_dependency_names() -> set[str]:
    with _PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    deps = project["dependencies"]
    names = set()
    for spec in deps:
        # Split on the first version-specifier character; a bare name (no pin)
        # is also handled since none of ">=<!~" appear in it.
        name = spec
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<"):
            if sep in name:
                name = name.split(sep, 1)[0]
        names.add(name.strip())
    return names


def test_base_dependencies_are_exactly_numpy_and_harmonics_cli() -> None:
    names = _base_dependency_names()
    assert names == _EXPECTED_BASE_DEP_NAMES, (
        f"[project.dependencies] must stay exactly {_EXPECTED_BASE_DEP_NAMES} "
        f"(SDK-first, installability-aware — see CLAUDE.md's Hard constraints); "
        f"got {names}"
    )
