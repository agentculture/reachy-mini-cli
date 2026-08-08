"""Machine-check the stdlib-only property of ``reachy/discover/`` (task t8).

The whole wireless-discovery feature (spec claim c12, honesty condition h22)
was specified to add **zero** new base dependencies: it is the HTTP remote
profile's own feature, and it must work on a bare ``pip install
reachy-mini-cli`` with neither the ``[sdk]`` nor the ``[daemon]`` extra
installed. ``CLAUDE.md``'s "Hard constraints" section pins exactly **three**
base runtime dependencies today — ``numpy``, ``harmonics-cli``,
``events-cli`` — each with a recorded justification; a stray
``import requests`` or the natural pull toward ``zeroconf`` for mDNS would
widen that set silently. This module turns "reachy/discover/ is stdlib-only"
into something CI enforces, mirroring the AST-based style of
:mod:`tests.test_zero_llm_boundary` (its docstring explains why an AST walk,
not a grep, is the right tool: it also catches function-local and
``TYPE_CHECKING``-guarded imports).

What is checked
================

1. Every module under ``reachy/discover/`` imports only the standard library
   at any scope — module level, function level, class body — or a first-party
   ``reachy.*`` module. "Is stdlib" is decided with
   :data:`sys.stdlib_module_names` (Python ≥ 3.10; this repo requires ≥ 3.12),
   never a hand-maintained name list.
2. The set of ``reachy.*`` modules OUTSIDE ``reachy.discover`` itself that the
   package reaches is pinned **by equality** against
   :data:`_EXPECTED_FIRST_PARTY_EDGES` — so both a new edge and the quiet
   disappearance of an existing one are deliberate, reviewed decisions, not
   something a future refactor drifts into unnoticed. A companion test fails
   on a DEAD entry (one nothing actually uses any more), matching
   ``test_zero_llm_boundary.py``'s ``test_the_speech_allow_list_has_no_dead_entries``.
3. ``pyproject.toml``'s base dependency list still holds exactly the three
   pinned entries :mod:`tests.test_dep_freeze` asserts — this module does not
   modify that file or that test, and re-derives the same check locally so a
   stray new base dependency fails loudly from *this* file's own story
   ("discovery added no dependency") too, not only from the sibling module
   that owns the invariant generally.
4. A named, documented rejection of the obvious future breach: no module
   under ``reachy/discover/`` may import ``zeroconf``, ``netifaces``,
   ``psutil``, ``requests``, ``httpx`` or ``reachy_mini`` — each with the
   reason it is refused, so a future reader does not have to re-derive why.
   This is strictly implied by check 1 (none of these are stdlib), but it is
   asserted directly and by name because it is the one path a future PR is
   actually tempted to take (the mDNS accelerator is explicitly parked, plan
   risk v2), and a direct assertion names the temptation instead of leaving it
   as an emergent consequence of a more general rule.
"""

from __future__ import annotations

import ast
import sys
import tomllib
from pathlib import Path

_REPO_ROOT = Path(__file__).resolve().parent.parent
_PKG_ROOT = _REPO_ROOT / "reachy"
_DISCOVER_DIR = _PKG_ROOT / "discover"
_PYPROJECT = _REPO_ROOT / "pyproject.toml"

#: Robust "is this the standard library?" — never a hand-maintained list.
_STDLIB_TOP_NAMES = set(sys.stdlib_module_names)

#: ``reachy.*`` modules OUTSIDE ``reachy.discover`` the package legitimately
#: reaches today, and why each is not a new dependency. Pinned by equality in
#: :func:`test_the_first_party_edges_are_exactly_the_documented_two` below.
_EXPECTED_FIRST_PARTY_EDGES = {
    "reachy.cli._errors": (
        "the CliError/exit-code contract every discover CliError-raising "
        "helper (hosts.py's rollback refusals, resolve.py's ambiguity error, "
        "ssh.py's argv validation) raises through"
    ),
    "reachy.daemon": (
        "registry.py resolves units.json under state_dir(), the same "
        "per-user state directory reachy/daemon.py itself and reachy/stash/ "
        "already use — not a new storage location or dependency"
    ),
}

#: The obvious future breach, named and refused. None of these is a
#: dependency of this project (``tests/test_dep_freeze.py`` pins the full
#: base list); listing them here is the "documented rejection" this task asks
#: for, not merely an emergent consequence of the stdlib-only rule above.
_FORBIDDEN_PACKAGES = {
    "zeroconf": (
        "the mDNS accelerator was evaluated and explicitly PARKED (plan risk "
        "v2: resolving the unit's own TXT record timed out twice live while "
        "the HTTP probe answered on the first attempt) — the sweep stays a "
        "plain HTTP fan-out, never an mDNS client"
    ),
    "netifaces": (
        "reachy/discover/sweep.py enumerates interfaces with stdlib "
        "socket/fcntl + /proc/net parsing precisely so this feature never "
        "needs a compiled interface-introspection library"
    ),
    "psutil": (
        "no process or system introspection is needed to enumerate hosts or "
        "probe a daemon; socket + ipaddress + urllib cover the whole feature"
    ),
    "requests": (
        "reachy/discover/probe.py is a single stdlib urllib.request GET "
        "against /api/daemon/status (cited from reachy/robot/http_transport.py's "
        "URL shape); no third-party HTTP client belongs in the base install"
    ),
    "httpx": ("same reasoning as requests — urllib only, no HTTP client dependency"),
    "reachy_mini": (
        "discovery is specified for the bare HTTP remote profile with "
        "neither [sdk] nor [daemon] installed; the SDK client stays entirely "
        "out of reachy/discover, which only ever speaks plain HTTP to the "
        "daemon's status route"
    ),
}

#: The exact base dependency NAME set (mirrors tests/test_dep_freeze.py,
#: re-derived here — not imported from it — so this file's own story, "the
#: discovery feature added zero new dependencies", stands on its own).
_EXPECTED_BASE_DEP_NAMES = {"numpy", "harmonics-cli", "events-cli"}


# --------------------------------------------------------------------------- #
# AST helpers — never grep. `import x.y`, `import x.y as z`, `from x import   #
# y`, function-local and nested-scope forms all count.                        #
# --------------------------------------------------------------------------- #


def _module_name(path: Path) -> str:
    """``reachy/discover/probe.py`` -> ``reachy.discover.probe``."""
    parts = list(path.relative_to(_REPO_ROOT).with_suffix("").parts)
    if parts[-1] == "__init__":
        parts = parts[:-1]
    return ".".join(parts)


def _discover_modules() -> dict[str, Path]:
    """Every ``.py`` file under ``reachy/discover/``, by dotted module name."""
    return {_module_name(p): p for p in sorted(_DISCOVER_DIR.rglob("*.py"))}


def _all_reachy_modules() -> dict[str, Path]:
    return {_module_name(p): p for p in sorted(_PKG_ROOT.rglob("*.py"))}


def _imported_names(path: Path, dotted: str) -> set[str]:
    """Every dotted name *path* imports, in any syntactic form, at any scope.

    ``ast.walk`` covers nested (function-local, class-body,
    ``if TYPE_CHECKING``) imports as well as module scope. ``from a.b import
    c`` contributes BOTH ``a.b`` and ``a.b.c``, so a boundary check written
    against either the submodule's or the symbol's own name still catches it.
    Relative imports are resolved against *dotted*'s own package.
    """
    names: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
        elif isinstance(node, ast.ImportFrom):
            if node.level:  # relative: `from . import x` / `from ..pkg import y`
                base = dotted.split(".")[: -node.level] or []
                module = ".".join([*base, *([node.module] if node.module else [])])
            else:
                module = node.module or ""
            if not module:
                continue
            names.add(module)
            for alias in node.names:
                names.add(f"{module}.{alias.name}")
    return names


def _resolve(dotted: str, modules: dict[str, Path]) -> str | None:
    """Map an imported dotted name onto the repo module that provides it.

    ``reachy.cli._errors.CliError`` -> ``reachy.cli._errors`` (walk up until a
    real module is found); a name with no matching repo module -> ``None``.
    """
    candidate = dotted
    while candidate and candidate not in modules:
        candidate = candidate.rsplit(".", 1)[0] if "." in candidate else ""
    return candidate or None


# --------------------------------------------------------------------------- #
# 0. Vacuity guard — a test that passes because it scanned nothing is worse   #
#    than no test at all.                                                     #
# --------------------------------------------------------------------------- #


def test_the_scan_actually_has_modules_and_imports_to_walk() -> None:
    """Fail loudly if a refactor empties the sets every check below iterates."""
    modules = _discover_modules()
    assert len(modules) >= 5, f"only {len(modules)} reachy/discover/ modules found — scan is broken"

    all_imports: set[str] = set()
    for name, path in modules.items():
        all_imports |= _imported_names(path, name)
    assert len(all_imports) >= 10, (
        f"only {len(all_imports)} distinct imported names collected across "
        f"{sorted(modules)} — the AST walk is not exercising the modules it "
        "claims to"
    )
    # And the walk must actually see BOTH stdlib and first-party edges, or the
    # partition logic below is running over a degenerate input.
    tops = {dotted.split(".", 1)[0] for dotted in all_imports}
    assert "reachy" in tops, "no reachy.* edge was ever seen — the scan is vacuous"
    assert tops - {"reachy"}, "no non-reachy (stdlib) edge was ever seen — the scan is vacuous"


# --------------------------------------------------------------------------- #
# 1. Every reachy/discover/ module imports only stdlib or first-party reachy  #
# --------------------------------------------------------------------------- #


def test_every_discover_module_imports_only_stdlib_or_first_party() -> None:
    """Criterion 1: the whole-package stdlib-or-reachy partition.

    Determinism note: ``sys.stdlib_module_names`` is the documented,
    version-correct source of truth (Python 3.10+); this repo requires
    Python >= 3.12, so it is always available. No hardcoded name list.
    """
    offences: list[str] = []
    for name, path in _discover_modules().items():
        for imported in sorted(_imported_names(path, name)):
            top = imported.split(".", 1)[0]
            if top == "reachy":
                continue  # first-party — checked for NAMED edges separately
            if top in _STDLIB_TOP_NAMES:
                continue
            offences.append(f"{name} imports {imported}")
    assert not offences, (
        "reachy/discover/ must stay stdlib-only (spec c12/h22 — the wireless "
        "discovery feature adds zero new base dependencies):\n  "
        + "\n  ".join(offences)
        + "\nIf this is genuinely needed, it does not belong in "
        "reachy/discover/ — that package is specified to work on a bare "
        "`pip install reachy-mini-cli` with neither [sdk] nor [daemon]."
    )


# --------------------------------------------------------------------------- #
# 2. First-party edges: allowed, but named and pinned by equality             #
# --------------------------------------------------------------------------- #


def test_the_first_party_edges_are_exactly_the_documented_two() -> None:
    """Criterion 2, pinned by EQUALITY — fails in both directions.

    A new ``reachy.*`` edge out of ``reachy/discover/`` (to
    ``reachy.behavior``, say, or a wider surface of ``reachy.robot``) fails
    this test and must be added to :data:`_EXPECTED_FIRST_PARTY_EDGES` with a
    reason, in the same change. So does the quiet disappearance of an
    existing edge — at which point the right move is to shrink the expected
    set here, not to leave a stale, unused entry (see the companion
    dead-entry test below).

    Intra-package edges (``reachy.discover.probe`` importing from
    ``reachy.discover.registry``, say) are internal composition, not a
    dependency-boundary crossing, and are deliberately excluded from the pin.
    """
    all_modules = _all_reachy_modules()
    discover_modules = _discover_modules()
    edges: set[str] = set()
    for name, path in discover_modules.items():
        for imported in sorted(_imported_names(path, name)):
            if not imported.startswith("reachy."):
                continue
            resolved = _resolve(imported, all_modules)
            if resolved is None:
                continue
            if resolved == name or resolved.startswith("reachy.discover"):
                continue  # intra-package, not a boundary crossing
            edges.add(resolved)
    assert edges == set(_EXPECTED_FIRST_PARTY_EDGES), (
        "the set of reachy.* modules reachy/discover/ reaches OUTSIDE itself "
        "changed.\n"
        f"  expected: {sorted(_EXPECTED_FIRST_PARTY_EDGES)}\n"
        f"  actual:   {sorted(edges)}\n"
        "A new edge must be added to _EXPECTED_FIRST_PARTY_EDGES with its "
        "reason, in the same change; a removed edge must be deleted from "
        "there, not left stale."
    )


def test_the_first_party_edge_pin_has_no_dead_entries() -> None:
    """An allow-list entry nothing uses any more quietly re-widens the boundary."""
    all_modules = _all_reachy_modules()
    discover_modules = _discover_modules()
    used: set[str] = set()
    for name, path in discover_modules.items():
        for imported in _imported_names(path, name):
            resolved = _resolve(imported, all_modules)
            if resolved in _EXPECTED_FIRST_PARTY_EDGES:
                used.add(resolved)
    dead = sorted(set(_EXPECTED_FIRST_PARTY_EDGES) - used)
    assert not dead, (
        f"_EXPECTED_FIRST_PARTY_EDGES permits {dead} but nothing in "
        "reachy/discover/ imports them any more — delete the entries so the "
        "pin stays a description of the code, not a wish about it."
    )


# --------------------------------------------------------------------------- #
# 3. pyproject.toml's base dependency list is untouched by this feature       #
# --------------------------------------------------------------------------- #


def test_pyproject_still_holds_exactly_the_three_base_dependencies() -> None:
    """Criterion 3 — re-derived locally so this file's story stands alone.

    ``tests/test_dep_freeze.py`` is the canonical owner of this invariant and
    is left unmodified (a hard constraint of this task); this is a
    deliberately thin companion proving the wireless feature specifically
    added no base dependency, driven over the identical
    ``[project.dependencies]`` seam.
    """
    with _PYPROJECT.open("rb") as fh:
        project = tomllib.load(fh)["project"]
    names: set[str] = set()
    for spec in project["dependencies"]:
        base = spec
        for sep in (">=", "<=", "==", "!=", "~=", ">", "<"):
            if sep in base:
                base = base.split(sep, 1)[0]
        names.add(base.strip())
    assert names == _EXPECTED_BASE_DEP_NAMES, (
        f"[project.dependencies] must stay exactly {_EXPECTED_BASE_DEP_NAMES} "
        f"— the wireless discovery feature (c12/h22) is specified to add "
        f"none; got {names}"
    )


# --------------------------------------------------------------------------- #
# 4. The documented rejection: the obvious future breach, named              #
# --------------------------------------------------------------------------- #


def test_no_discover_module_imports_a_forbidden_accelerator_or_the_sdk() -> None:
    """Criterion 4 — a direct, named guard against the specific temptation.

    Strictly implied by criterion 1 (none of these are stdlib), but asserted
    by name so a future reader sees WHY each package is refused without
    having to reconstruct the reasoning from a general rule.
    """
    offences: list[str] = []
    for name, path in _discover_modules().items():
        for imported in sorted(_imported_names(path, name)):
            top = imported.split(".", 1)[0]
            if top in _FORBIDDEN_PACKAGES:
                offences.append(f"{name} imports {imported} ({top}: {_FORBIDDEN_PACKAGES[top]})")
    assert not offences, "a forbidden accelerator/SDK import appeared:\n  " + "\n  ".join(offences)


def test_every_forbidden_package_has_a_named_reason() -> None:
    """Guard the guard: an entry with no reason string is not a documented rejection."""
    for package, reason in _FORBIDDEN_PACKAGES.items():
        assert reason and isinstance(reason, str), package
        assert len(reason) > 20, f"{package}'s reason reads as a placeholder: {reason!r}"
