"""Tests for relocating ``Event``/``MarkerEvent`` out of ``reachy.speech.markers``
(task t2, "retire the old AI-first flow").

Why this exists
----------------
``reachy/speech/markers.py`` hosted the streaming ``MarkerParser`` of the
in-loop LLM cognition path, and also DEFINED ``Event``/``MarkerEvent``.  But
:mod:`reachy.motion.expression` — part of the ``apply_pose`` tool path that
SURVIVES (used by ``reachy agent attach``) — needs those types.  Task t2
therefore moved them into :mod:`reachy.speech.marker_events`, so that deleting
``markers.py`` could not break the surviving ``expression``/``tools`` import
chain.

Task t21 then deleted ``markers.py`` for real, together with the
``--live`` composition root and the ``CognitionEngine`` it fed.  So the
criteria below are no longer simulated — they are the live state of the tree:

1. Importing ``reachy.speech.tools`` and ``reachy.motion.expression`` succeeds
   with ``markers.py`` genuinely absent (and neither pulls it in).
2. ``Event``/``MarkerEvent`` keep their shape in their new home; ``expression.py``
   consumers are unchanged.

Criterion 1 is proved with a **subprocess** probe rather than an in-process
``sys.modules`` eviction — this repo's own lessons (see
``reachy-sleep-mode-spec`` memory / ``test_speech_tools.py``) note that
in-process module eviction pollutes sibling tests.
"""

from __future__ import annotations

import ast
import inspect
import subprocess  # nosec B404 — fixed-arg subprocess for an import-boundary probe
import sys

import reachy.motion.expression as expression_mod
import reachy.speech.marker_events as marker_events_mod
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
# Criterion 1b — dynamic: a fresh interpreter imports reachy.speech.tools and
# reachy.motion.expression with markers.py genuinely deleted, and neither drags
# it back in.
# ---------------------------------------------------------------------------

_IMPORT_PROBE = """
import sys

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
    """Dynamic proof of criterion 1: a fresh subprocess interpreter imports
    reachy.motion.expression and reachy.speech.tools successfully with
    markers.py deleted, and never pulls the deleted module in."""
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [sys.executable, "-c", _IMPORT_PROBE],
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, (
        f"import chain broke with markers.py absent:\n"
        f"stdout={proc.stdout!r}\nstderr={proc.stderr}"
    )
    assert proc.stdout.strip() == "ok"


def test_markers_module_is_gone() -> None:
    """Guard on the probe above: markers.py really is deleted, so the green
    result is meaningful and not a probe that would pass either way."""
    proc = subprocess.run(  # nosec B603 — fixed args, sys.executable, no shell
        [
            sys.executable,
            "-c",
            (
                "import importlib.util;"
                "assert importlib.util.find_spec('reachy.speech.markers') is None, "
                "'reachy.speech.markers must be gone';"
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
# Criterion 2 — shape/identity preserved: marker_events.py holds the SAME
# class objects the surviving consumers use (not redefinitions), so isinstance
# checks across import paths keep working.
# ---------------------------------------------------------------------------


def test_marker_event_identity_preserved_in_new_home() -> None:
    """marker_events.MarkerEvent/SpeechEvent/Event must be the exact objects
    expression.py (and any other surviving consumer) uses — one definition,
    not a parallel redefinition."""
    assert marker_events_mod.MarkerEvent is expression_mod.MarkerEvent
    # Event is a typing.Union alias; compare structurally as well as by
    # identity where the module chooses to bind the same alias object.
    assert marker_events_mod.Event == expression_mod.Event


def test_marker_event_shape_unchanged() -> None:
    """MarkerEvent keeps its current frozen-dataclass shape: emoji + kind."""
    event = marker_events_mod.MarkerEvent(emoji="🤔")
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
    event = marker_events_mod.SpeechEvent(text="hello")
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
        marker_events_mod.MarkerEvent(emoji="🤔"),
        marker_events_mod.SpeechEvent(text="hello"),
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
