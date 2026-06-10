"""Boundary tests for ``think``'s motion path (t8).

These assert the architectural seams the design demands:

* The cognition engine (:mod:`reachy.speech.cognition`) drives motion **only**
  through the injected ``express`` callback — it must NOT import
  :mod:`reachy.motion` at all, so the producer/queue is the sole motion seam.
* ``think``'s CLI routes expression moves through an
  :class:`~reachy.motion.expression.ExpressionProducer` + a
  :class:`~reachy.motion.queue.MotionQueue` (the serial executor), never via a
  direct ``transport.move_*`` call from the cognition callback.
* ``think`` adds NO vision-driven expression module and NO cross-session
  mood/persistence store.
* ``say`` stays a dumb TTS pipe (mirrors the say-boundary tests).
"""

from __future__ import annotations

import ast
import inspect

import reachy.cli._commands.say as say_mod
import reachy.cli._commands.think as think_mod
import reachy.speech.cognition as cognition_mod


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
# Cognition engine never imports motion — the producer is injected
# ---------------------------------------------------------------------------


def test_cognition_engine_does_not_import_motion() -> None:
    """reachy.speech.cognition must not import reachy.motion at all.

    The only motion seam is the injected ``express`` callback; the engine builds
    no MotionAction, touches no MotionQueue, and never reaches a transport.
    """
    for name in _imported_modules(cognition_mod):
        assert not name.startswith(
            "reachy.motion"
        ), f"cognition.py must not import reachy.motion (got {name!r})"
    assert "motion" not in cognition_mod.__dict__


def test_cognition_engine_drives_motion_only_via_express_callback() -> None:
    """The engine source contains no direct transport.move_* call.

    Motion is enqueued exclusively through the ``express`` callback; the engine
    has no transport handle and issues no move itself.
    """
    src = inspect.getsource(cognition_mod)
    assert "move_goto" not in src
    assert "move_set_target" not in src
    assert ".move_" not in src


# ---------------------------------------------------------------------------
# think routes moves through the producer/queue, not a direct transport.move_*
# ---------------------------------------------------------------------------


def test_think_uses_expression_producer_and_motion_queue() -> None:
    """think.py wires the ExpressionProducer + MotionQueue motion path."""
    imported = _imported_modules(think_mod)
    assert "reachy.motion.expression" in imported
    assert "reachy.motion.queue" in imported
    # The serial executor that drains the queue to the robot.
    assert "reachy.motion.server" in imported


def test_think_cognition_callback_does_not_call_transport_move_directly() -> None:
    """No direct transport.move_* call lives in think's express seam.

    The express callback must hand the emoji to ``ExpressionProducer.express`` —
    the executor (reachy.motion.server.run) is the only thing that calls
    ``transport.move_goto``. So think.py must not itself call ``.move_set_target``
    or ``move_goto`` *as the cognition callback*; the only move I/O is the
    executor's. We assert think.py never references set_target (a streaming move
    API) and that ``express`` flows through the producer.
    """
    src = inspect.getsource(think_mod)
    assert "move_set_target" not in src, "think must not stream set_target moves"
    # The express seam must reference the producer, not a raw transport move.
    assert "producer.express" in src or "ExpressionProducer" in src


def test_think_signal_lifecycle_is_wired() -> None:
    """think.run wraps the loop in the cognition-active signal context manager."""
    # The cognition_signal module is referenced (via `from reachy.speech import
    # cognition_signal` or a fully-dotted import — accept either form).
    assert hasattr(think_mod, "cognition_signal")
    src = inspect.getsource(think_mod.cmd_think_run)
    assert "cognition_active" in src


# ---------------------------------------------------------------------------
# No vision-expression channel, no cross-session mood/persistence store
# ---------------------------------------------------------------------------


def test_think_adds_no_vision_expression_or_mood_store() -> None:
    """think must not import a vision-expression or a persistence/mood module."""
    imported = _imported_modules(think_mod) | _imported_modules(cognition_mod)
    for name in imported:
        assert "vision" not in name, f"think must add no vision-driven expression ({name!r})"
        assert "mood" not in name, f"think must add no mood store ({name!r})"
        # No cross-session persistence layer (db / sqlite / shelve / pickle store).
        assert name not in {"sqlite3", "shelve", "pickle"}, f"no persistence store ({name!r})"


# ---------------------------------------------------------------------------
# say stays a dumb pipe (mirror of the say-boundary tests)
# ---------------------------------------------------------------------------


def test_say_remains_a_dumb_pipe() -> None:
    """say must not import llm / events / motion / cognition — it stays dumb."""
    imported = _imported_modules(say_mod)
    for name in imported:
        assert "speech.llm" not in name, f"say must not import the LLM client ({name!r})"
        assert "speech.events" not in name, f"say must not import the event bus ({name!r})"
        assert "speech.cognition" not in name, f"say must not import cognition ({name!r})"
        assert not name.startswith("reachy.motion"), f"say must not drive motion ({name!r})"
    assert "llm" not in say_mod.__dict__
    assert "events" not in say_mod.__dict__
