"""Verb-level tests: ``behavior engine run --export -``.

Task t8's CLI acceptance criterion: the running engine's runtime-event JSONL
feed (perception/rule/intent/motion — see ``reachy.export.runtime``) streams to
stdout, pure (no banners mixed in), while every diagnostic/summary line goes to
stderr — and a broken downstream pipe never kills the loop. This is a SEPARATE
feed from the ``think``/``listen --live`` cognition feed (decision c27); no
``thinking``/``message``/``emotion`` block can ever appear here.
"""

from __future__ import annotations

import contextlib
import json

from reachy.cli import main
from reachy.export.runtime import RUNTIME_BLOCKS

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self) -> None:
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None) -> None:
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


def _patch_transport(monkeypatch, tr=None):
    tr = tr or _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tr


# --------------------------------------------------------------------------- #
# Stdout purity + wiring                                                      #
# --------------------------------------------------------------------------- #


def test_engine_run_export_emits_pure_runtime_jsonl(monkeypatch, capsys) -> None:
    _patch_transport(monkeypatch)
    rc = main(["behavior", "engine", "run", "--export", "-", "--max-ticks", "3"])
    assert rc == 0
    out, err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    assert lines, "the feed must not be empty (a baseline sense event fires tick 1)"
    for ln in lines:
        obj = json.loads(ln)  # every stdout line is pure JSON — no banner text mixed in
        assert obj["t"] in RUNTIME_BLOCKS
        assert obj["t"] not in ("thinking", "message", "emotion")  # decision c27
    # Diagnostics/banners land on stderr, never stdout.
    assert "engine stopped" in err
    assert "export: stdout" in err


def test_engine_run_export_with_json_flag_stdout_still_pure(monkeypatch, capsys) -> None:
    """--json's per-tick ownership summary must NOT leak onto stdout while exporting."""
    _patch_transport(monkeypatch)
    rc = main(["behavior", "engine", "run", "--json", "--export", "-", "--max-ticks", "3"])
    assert rc == 0
    out, _err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    for ln in lines:
        obj = json.loads(ln)
        assert obj["t"] in RUNTIME_BLOCKS  # never the {"tick":.., "ownership":..} shape


def test_engine_run_without_export_flag_is_unaffected(monkeypatch, capsys) -> None:
    """Control: bare 'engine run --json' keeps emitting its existing per-tick summary."""
    _patch_transport(monkeypatch)
    rc = main(["behavior", "engine", "run", "--json", "--max-ticks", "2"])
    assert rc == 0
    out, _err = capsys.readouterr()
    events = [json.loads(ln) for ln in out.splitlines() if ln.strip()]
    assert [e["tick"] for e in events] == [1, 2]
    assert "ownership" in events[0]


def test_export_blocks_filters_the_feed(monkeypatch, capsys) -> None:
    _patch_transport(monkeypatch)
    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--export",
            "-",
            "--export-blocks",
            "rule",
            "--max-ticks",
            "3",
        ]
    )
    assert rc == 0
    out, _err = capsys.readouterr()
    lines = [ln for ln in out.splitlines() if ln.strip()]
    # No rules are loaded in this bare run, so nothing is admitted -> the feed is
    # empty (the baseline "sense" event is filtered out by --export-blocks rule).
    assert lines == []


def test_export_unsupported_target_is_clean_user_error(monkeypatch, capsys) -> None:
    _patch_transport(monkeypatch)
    rc = main(["behavior", "engine", "run", "--export", "/tmp/feed.jsonl", "--max-ticks", "1"])
    assert rc == 1
    _out, err = capsys.readouterr()
    assert "error:" in err and "hint:" in err
    assert "stdout" in err


def test_export_invalid_block_is_clean_user_error(monkeypatch, capsys) -> None:
    _patch_transport(monkeypatch)
    rc = main(
        [
            "behavior",
            "engine",
            "run",
            "--export",
            "-",
            "--export-blocks",
            "thinking",
            "--max-ticks",
            "1",
        ]
    )
    assert rc == 1
    _out, err = capsys.readouterr()
    assert "error:" in err and "hint:" in err


# --------------------------------------------------------------------------- #
# Disconnect-safety at the verb level                                         #
# --------------------------------------------------------------------------- #


def test_broken_pipe_on_export_never_kills_the_loop(monkeypatch, capsys) -> None:
    """A downstream consumer hangup (stdout write raises) must not crash the run."""

    class _BrokenStdout:
        def write(self, *_a, **_kw):
            raise BrokenPipeError("consumer hung up")

        def flush(self, *_a, **_kw):
            pass

    import sys

    _patch_transport(monkeypatch)
    monkeypatch.setattr(sys, "stdout", _BrokenStdout())
    rc = main(["behavior", "engine", "run", "--export", "-", "--max-ticks", "3"])
    assert rc == 0  # the run completed despite every stdout write failing
