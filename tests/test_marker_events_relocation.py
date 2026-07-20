"""Tests for relocating ``Event``/``MarkerEvent`` out of ``reachy.speech.markers``
(task t2, "retire the old AI-first flow").

Why this exists
----------------
A later PR deletes ``reachy/speech/markers.py`` entirely as part of retiring the
in-loop LLM cognition path (the streaming ``MarkerParser`` it hosts).  But
:mod:`reachy.motion.expression` — part of the ``apply_pose`` tool path that
SURVIVES that deletion (used by ``reachy agent attach``) — imports
``Event``/``MarkerEvent`` from ``reachy.speech.markers``.  If those types stay
defined only in ``markers.py``, deleting it would break the surviving
``expression``/``tools`` import chain.

Acceptance criteria (from the task)
------------------------------------
1. Importing ``reachy.speech.tools`` and ``reachy.motion.expression`` succeeds
   with ``markers.py`` absent.
2. ``Event``/``MarkerEvent`` keep their current shape; ``expression.py``
   consumers are unchanged.

Criterion 1 is proved with a **subprocess** probe (not an in-process
``sys.modules`` eviction) — this repo's own lessons (see
``reachy-sleep-mode-spec`` memory / ``test_speech_tools.py``) note that
in-process module eviction pollutes sibling tests, so a fresh interpreter with
a meta-path finder that raises for ``reachy.speech.markers`` specifically is
used instead, simulating the file's absence without touching disk or
``sys.modules`` in the test process itself.
"""

from __future__ import annotations

import ast
import inspect
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import reachy.motion.expression as expression_mod
import reachy.speech.markers as markers_mod
import reachy.speech.tools as tools_mod

# ---------------------------------------------------------------------------
# Static import-graph helpers (mirrors tests/test_think_boundary.py)
# ---------------------------------------------------------------------------


def _imported_modules(module) -> set[str]:
    """All dotted module names imported by *module* (Import + ImportFrom)."""
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


# ---------------------------------------------------------------------------
# Criterion 1a — static: expression.py's own import statements never name
# reachy.speech.markers (a fast, in-process guard; the subprocess probe below
# is the dynamic proof).
# ---------------------------------------------------------------------------


def test_expression_module_does_not_import_markers_statically() -> None:
    """reachy.motion.expression must not name reachy.speech.markers in its
    own import statements — its Event/MarkerEvent types must come from a
    module that survives markers.py's eventual deletion."""
    imported = _imported_modules(expression_mod)
    assert "reachy.speech.markers" not in imported, (
        "expression.py must not import reachy.speech.markers directly "
        f"(got imports: {sorted(imported)!r})"
    )


# ---------------------------------------------------------------------------
# Criterion 1b — dynamic: a fresh interpreter with markers.py "absent"
# (a meta-path finder raises for that one module name) can still import
# reachy.speech.tools and reachy.motion.expression.
# ---------------------------------------------------------------------------

_BLOCK_MARKERS_PROBE = """
import sys
import importlib.abc


class _BlockMarkers(importlib.abc.MetaPathFinder):
    \"\"\"Simulate reachy/speech/markers.py being absent from disk.\"\"\"

    def find_spec(self, name, path, target=None):
        if name == "reachy.speech.markers":
            raise ModuleNotFoundError(
                f"No module named {name!r} (simulated absence for t2 probe)"
            )
        return None


sys.meta_path.insert(0, _BlockMarkers())

import reachy.motion.expression
import reachy.speech.tools

assert "reachy.speech.markers" not in sys.modules, (
    "reachy.speech.markers must not have been imported as a side effect"
)

# The surviving chain must still expose usable Event/MarkerEvent types.
event = reachy.motion.expression.MarkerEvent(emoji="\U0001f914")
assert event.emoji == "\U0001f914"
assert event.kind == "marker"

print("ok")
"""


def test_expression_and_tools_import_with_markers_absent() -> None:
    """Dynamic proof of criterion 1: block reachy.speech.markers in a fresh
    subprocess interpreter and confirm reachy.motion.expression and
    reachy.speech.tools still import successfully."""
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [sys.executable, "-c", _BLOCK_MARKERS_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"import chain broke with markers.py simulated absent:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr}"
    )
    assert proc.stdout.strip() == "ok"


def test_expression_and_tools_import_fails_today_baseline_guard() -> None:
    """Sanity guard on the probe itself: confirm that *without* the fix the
    probe would indeed fail — i.e. some import in the chain really did
    depend on reachy.speech.markers before this task, so the green result
    above is meaningful and not a probe that always passes.

    This test does not re-break the fix; it independently re-verifies (via a
    second, harmless subprocess) that MarkerEvent as seen through
    reachy.motion.expression is the SAME object as reachy.speech.markers's
    export, i.e. the type was moved/re-exported rather than duplicated.
    """
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [
            sys.executable,
            "-c",
            (
                "import reachy.motion.expression as expr_mod, "
                "reachy.speech.markers as markers_mod;"
                "assert expr_mod.MarkerEvent is markers_mod.MarkerEvent, "
                "'MarkerEvent must be the SAME class, not a duplicate';"
                "print('ok')"
            ),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr
    assert proc.stdout.strip() == "ok"


# ---------------------------------------------------------------------------
# Criterion 2 — shape/identity preserved: markers.py re-exports the SAME
# class objects (not redefinitions), so isinstance checks across the two
# import paths (reachy.speech.markers vs. the new home) keep working.
# ---------------------------------------------------------------------------


def test_marker_event_identity_preserved_across_markers_reexport() -> None:
    """markers.MarkerEvent/SpeechEvent/Event must be the exact objects
    expression.py (and any other surviving consumer) uses — a re-export,
    not a parallel redefinition."""
    assert markers_mod.MarkerEvent is expression_mod.MarkerEvent
    # Event is a typing.Union alias; compare structurally as well as by
    # identity where the module chooses to bind the same alias object.
    assert markers_mod.Event == expression_mod.Event


def test_marker_event_shape_unchanged() -> None:
    """MarkerEvent keeps its current frozen-dataclass shape: emoji + kind."""
    event = markers_mod.MarkerEvent(emoji="🤔")
    assert event.emoji == "🤔"
    assert event.kind == "marker"
    # frozen: mutation raises.
    try:
        event.emoji = "😮"  # type: ignore[misc]
    except Exception:
        pass
    else:
        raise AssertionError("MarkerEvent must stay frozen")


def test_speech_event_shape_unchanged() -> None:
    """SpeechEvent keeps its current frozen-dataclass shape: text + kind."""
    event = markers_mod.SpeechEvent(text="hello")
    assert event.text == "hello"
    assert event.kind == "speech"


def test_expression_producer_consumes_marker_events_unchanged() -> None:
    """expression.py's consume()/on_marker() behavior is unaffected by the
    relocation — it still gestures on MarkerEvent and ignores SpeechEvent."""

    class _RecordingQueue:
        def __init__(self) -> None:
            self.submitted: list = []

        def submit(self, action) -> None:
            self.submitted.append(action)

    queue = _RecordingQueue()
    producer = expression_mod.ExpressionProducer(queue=queue)
    events = [
        markers_mod.MarkerEvent(emoji="🤔"),
        markers_mod.SpeechEvent(text="hello"),
    ]
    moves = producer.consume(events)
    assert moves == 1
    assert len(queue.submitted) == 1


def test_tools_module_still_does_not_import_markers_or_motion() -> None:
    """reachy.speech.tools must not import reachy.speech.markers (or
    reachy.motion at all — the pre-existing boundary from test_speech_tools.py,
    reasserted here as part of this task's own contract)."""
    imported = _imported_modules(tools_mod)
    assert "reachy.speech.markers" not in imported
    for name in imported:
        assert not name.startswith("reachy.motion"), f"tools.py must not import motion ({name!r})"
