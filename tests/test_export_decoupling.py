"""
Guard tests: reTerminal stays decoupled from reachy-mini-cli.

These tests assert structural properties about the existing codebase — they are
meant to catch regressions where someone accidentally adds a coupling between the
export feed and a specific consumer (e.g. a reTerminal renderer library), or adds
a new base runtime dependency, or adds a network server inside the export package.

Four assertions:
1. Base runtime deps are unchanged: ``["numpy>=1.24"]`` only.
2. No ``import reterminal`` / ``from reterminal`` statement in any ``reachy/**/*.py``.
3. No server/network-library import inside ``reachy/export/*.py``.
4. ``JsonlExporter`` / ``to_jsonl`` are referenced (imported) only from within
   ``reachy/export/`` and the shared CLI wiring ``reachy/cli/_export.py`` — no other
   ``reachy/`` module imports them.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path

# ---------------------------------------------------------------------------
# Repo root — computed relative to this test file (robust to worktrees, etc.)
# ---------------------------------------------------------------------------

REPO_ROOT = Path(__file__).parent.parent
PYPROJECT = REPO_ROOT / "pyproject.toml"
REACHY_PKG = REPO_ROOT / "reachy"
EXPORT_PKG = REACHY_PKG / "export"

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_IMPORT_RE = re.compile(r"^\s*(import|from)\s+", re.MULTILINE)


def _reachy_python_files() -> list[Path]:
    """All *.py files under reachy/."""
    return sorted(REACHY_PKG.rglob("*.py"))


def _export_python_files() -> list[Path]:
    """All *.py files under reachy/export/."""
    return sorted(EXPORT_PKG.glob("*.py"))


def _is_import_line(line: str) -> bool:
    """Return True iff *line* is an import statement (not a docstring mention)."""
    stripped = line.lstrip()
    return stripped.startswith("import ") or stripped.startswith("from ")


# ---------------------------------------------------------------------------
# 1. Base deps unchanged
# ---------------------------------------------------------------------------


def test_base_deps_numpy_only() -> None:
    """[project].dependencies must be exactly ['numpy>=1.24'] — no new base dep added."""
    with PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    deps: list[str] = project["dependencies"]
    assert deps == ["numpy>=1.24"], f"Expected base deps to be ['numpy>=1.24'], got: {deps!r}"


# ---------------------------------------------------------------------------
# 2. No reterminal import coupling
# ---------------------------------------------------------------------------


def test_no_reterminal_import_in_reachy_package() -> None:
    """No reachy/**/*.py file may contain an ``import reterminal`` or ``from reterminal`` statement.

    Docstring *mentions* of 'reTerminal' are intentional and allowed — this test
    only checks actual Python import statements.
    """
    _reterminal_import_re = re.compile(
        r"^\s*(import\s+reterminal|from\s+reterminal\b)",
        re.MULTILINE,
    )
    violations: list[str] = []
    for path in _reachy_python_files():
        source = path.read_text(encoding="utf-8")
        if _reterminal_import_re.search(source):
            violations.append(str(path.relative_to(REPO_ROOT)))
    assert (
        not violations
    ), "Found reterminal import coupling in reachy/ source files:\n" + "\n".join(
        f"  {v}" for v in violations
    )


# ---------------------------------------------------------------------------
# 3. No network/server-library import inside reachy/export/
# ---------------------------------------------------------------------------


def test_export_package_has_no_server_imports() -> None:
    """reachy/export/*.py must not import any server or network-serving library.

    The export feed is stdout-only — it must never start a server or listen on a
    socket.  Allowed network libs: ``urllib.request`` for client-side HTTP (but
    NOT inside export/).
    """
    # Patterns that indicate a server/network-serving dep inside export/
    _server_import_re = re.compile(
        r"^\s*(import|from)\s+(http\.server|socketserver|socket|flask|fastapi|urllib\.request)\b",
        re.MULTILINE,
    )
    violations: list[str] = []
    for path in _export_python_files():
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if _is_import_line(line) and _server_import_re.match(line):
                violations.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()!r}")
    assert not violations, (
        "Found server/network-lib imports inside reachy/export/ (export must be stdout-only):\n"
        + "\n".join(f"  {v}" for v in violations)
    )


# ---------------------------------------------------------------------------
# 4. Single export path: JsonlExporter / to_jsonl only imported from expected places
# ---------------------------------------------------------------------------


def test_jsonl_exporter_and_to_jsonl_imported_only_from_allowed_modules() -> None:
    """JsonlExporter and to_jsonl must be imported only from reachy/export/ and _export.py.

    Any other reachy/ module that imports these symbols would indicate a second,
    unintended structured-export path has been added — this test prevents that. The
    one CLI wiring point ``reachy/cli/_export.py`` builds the sink for *both*
    ``think run`` and ``listen run --live`` so the two feeds can never drift.

    Note: docstring *mentions* (e.g. in cognition.py type-annotation prose) are
    allowed; only Python import statements are checked.
    """
    _symbol_import_re = re.compile(
        r"^\s*(import|from)\s+.*\b(JsonlExporter|to_jsonl)\b",
        re.MULTILINE,
    )

    # Allowed locations: anything inside reachy/export/ or the shared CLI wiring.
    _allowed_rel_parts = (
        ("reachy", "export"),
        ("reachy", "cli", "_export.py"),
    )

    def _is_allowed(path: Path) -> bool:
        parts = path.parts
        for allowed in _allowed_rel_parts:
            # Check if the allowed prefix/suffix appears in the path parts
            if all(a in parts for a in allowed):
                return True
        return False

    violations: list[str] = []
    for path in _reachy_python_files():
        if _is_allowed(path):
            continue
        source = path.read_text(encoding="utf-8")
        for line in source.splitlines():
            if _is_import_line(line) and _symbol_import_re.match(line):
                violations.append(f"{path.relative_to(REPO_ROOT)}: {line.strip()!r}")

    assert not violations, (
        "JsonlExporter / to_jsonl imported from unexpected reachy/ modules "
        "(only reachy/export/ and reachy/cli/_commands/think.py should import these):\n"
        + "\n".join(f"  {v}" for v in violations)
    )
