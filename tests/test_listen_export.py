"""Tests for ``listen run --live --export -`` — stream the cognition feed.

``listen --live`` folds ``think`` into the loop, so the export JSONL feed
(``thinking`` / ``message`` / ``emotion`` blocks) that ``think run --export -``
produces is wired here too — letting the boot-persistent live loop stream what the
robot is thinking to any subscriber (a reTerminal panel, a log, an audio renderer).

What is proven here (the new seams, deterministically — no real LLM/threads):

1. ``_build_think_hook(provider, export=hook)`` builds the engine with that export
   hook **and** ``audio_optional=True`` (the live engine must never die on a dead
   TTS).
2. ``_build_live_hooks(..., export=hook)`` threads the hook through to the engine.
3. The CLI guards: ``--export`` needs ``--live`` and the sdk transport — both clean
   exit-1 user errors.
"""

from __future__ import annotations

import io
import sys

import reachy.cli._commands.listen as listen_mod
from reachy.cli import main
from reachy.export.exporter import ExportHook


class _FakeEngine:
    """Captures the kwargs ``_build_think_hook`` constructs the engine with."""

    last_kwargs: dict = {}

    def __init__(self, **kwargs):
        _FakeEngine.last_kwargs = kwargs
        self.buffer = kwargs.get("buffer")

    def run(self, *_a, **_k):  # never driven in these tests
        return 0


def _patch_engine(monkeypatch):
    _FakeEngine.last_kwargs = {}
    monkeypatch.setattr("reachy.speech.cognition.CognitionEngine", _FakeEngine)


# ---------------------------------------------------------------------------
# 1 + 2. Composition: export + audio_optional reach the engine
# ---------------------------------------------------------------------------


def test_build_think_hook_threads_export_and_audio_optional(monkeypatch):
    _patch_engine(monkeypatch)
    sentinel = ExportHook(emit=lambda _e: None)

    hook = listen_mod._build_think_hook(lambda: None, export=sentinel)

    assert hook is not None
    assert _FakeEngine.last_kwargs.get("export") is sentinel
    assert _FakeEngine.last_kwargs.get("audio_optional") is True


def test_build_think_hook_defaults_audio_optional_without_export(monkeypatch):
    _patch_engine(monkeypatch)

    listen_mod._build_think_hook(lambda: None)

    assert _FakeEngine.last_kwargs.get("export") is None
    # Even without an export sink the folded live engine is resilient to a dead TTS.
    assert _FakeEngine.last_kwargs.get("audio_optional") is True


def test_build_live_hooks_passes_export_through(monkeypatch):
    _patch_engine(monkeypatch)
    sentinel = ExportHook(emit=lambda _e: None)

    class _Tp:
        def get_frame(self):  # VisionHook reads frames off the transport
            return None

    listen_mod._build_live_hooks(
        transport=_Tp(), queue=object(), provider=lambda: None, pat_hook=None, export=sentinel
    )

    assert _FakeEngine.last_kwargs.get("export") is sentinel


# ---------------------------------------------------------------------------
# 3. CLI guards
# ---------------------------------------------------------------------------


def _run(monkeypatch, argv, *, transport=None):
    """Run ``reachy <argv>``; return (rc, stdout, stderr)."""
    if transport is not None:
        monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _a: transport)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def test_export_requires_live(monkeypatch):
    rc, _out, err = _run(monkeypatch, ["listen", "run", "--export", "-", "--max-ticks", "1"])
    assert rc == 1
    assert "--export needs --live" in err
    assert "hint:" in err


def test_export_requires_sdk_transport(monkeypatch):
    class _HttpTransport:
        name = "http"  # no media_session attribute → http profile

        def move_goto(self, **_k):
            return None

    rc, _out, err = _run(
        monkeypatch,
        ["listen", "run", "--live", "--export", "-", "--max-ticks", "1"],
        transport=_HttpTransport(),
    )
    assert rc == 1
    assert "sdk transport" in err
