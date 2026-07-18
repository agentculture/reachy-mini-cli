"""Composition tests for ``behavior engine run``'s runtime sense/act stack (task t7).

t7 wires every merged runtime piece into ``reachy/cli/_commands/behavior.py``'s
``_compose_run_seam`` / ``cmd_engine_run``: the SDK sense stack (held pose reader
+ ``PatSenseDriver`` + ``LastPoseHolder``) feeding the engine's ``sense`` through
``read_perception``, and the ``GotoLane`` + GOTO intent kind riding the ONE
``TickBus`` alongside the rules + intent drivers. The task-level tests proved each
piece against a hand-built seam; these prove the CLI composition actually stitches
them together, exercising the real ``engine.run`` loop with a fake transport (no
robot, daemon, SDK, or network anywhere):

1. an injected actual-pose reader drives a real ``PatDetector`` through the
   composed sense stack, so a pat surfaces on the perception snapshot and the
   runtime feed publishes the ``sense`` block on change;
2. a goto command submitted to the intents spool reaches the ``GotoLane`` through
   the registered GOTO kind and emits ``goto.admitted`` / ``goto.done``;
3. with the ``[sdk]`` extra absent the composition still succeeds — DoA-only
   sense, no pat events, no exceptions, goto path intact;
4. the composition module never imports ``reachy.speech.llm`` and never touches a
   ``media_session`` (the held reader is media-free by construction);
5. the held pose reader is ``close()``d on engine shutdown — including when the
   engine loop raises — so a real ``no_media`` client never wedges the exit.
"""

from __future__ import annotations

import ast
import contextlib
import inspect
import json

import pytest

from reachy.behavior import control
from reachy.behavior.intents import INTENT_NAMESPACE
from reachy.cli import main

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (a mic-less box)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None  # non-dict -> read_doa -> EMPTY_SENSE (stable, no speech)


class _SpeechTransport(_QuietTransport):
    """A fake transport that always hears speech from the left."""

    def doa(self, timeout=None):
        return {"angle": 0.2, "speech_detected": True}


class _ScriptedReader:
    """A fake HeldStateReader: ``read()`` walks a scripted ``(pitch, yaw)`` list.

    Duck-types the two methods the composition uses — ``read`` (the pat driver's
    actual-pose source) and ``close`` (the shutdown teardown) — and records both
    so a test can assert the reader was consulted and released.
    """

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.reads = 0
        self.closed = False

    def read(self):
        self.reads += 1
        value = self._script[min(self._i, len(self._script) - 1)]
        self._i += 1
        return value

    def close(self):
        self.closed = True


class _StepClock:
    """A deterministic monotonic clock advancing ``dt`` seconds per call."""

    def __init__(self, dt=0.05):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


# --------------------------------------------------------------------------- #
# Fixtures / helpers                                                          #
# --------------------------------------------------------------------------- #


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _QuietTransport()
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _blocks(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def _imported_modules(module) -> set[str]:
    tree = ast.parse(inspect.getsource(module))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module)
        elif isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.name)
    return names


# --------------------------------------------------------------------------- #
# 1. Pat sense flows through the composed stack into the feed                 #
# --------------------------------------------------------------------------- #


def test_pat_sense_flows_into_the_feed_via_the_composed_stack(_isolated, monkeypatch, capsys):
    """An injected actual-pose reader drives the REAL PatDetector through the
    composed sense stack; the pat surfaces on the perception snapshot and the
    runtime feed publishes a ``sense`` block carrying it.

    ``--no-base-layer`` keeps the commanded head at steady neutral, so the
    scripted actual-pose dip -> release -> dip reproduces the pat_sense unit
    scenario deterministically (a pitch-dominated ``scratch``)."""
    script = [(-3.0, 0.0), (0.0, 0.0), (-3.0, 0.0)] + [(0.0, 0.0)] * 8
    reader = _ScriptedReader(script)
    monkeypatch.setenv("REACHY_PAT_SENSE", "1")  # explicit; ON by default since #80
    monkeypatch.setattr("reachy.cli._commands.behavior._make_state_reader", lambda: reader)
    # The composed driver's boot warmup and stillness gate are both real-clock
    # windows (15 s and 0.5 s); this 8-tick run spans 0.4 s of injected clock, so
    # neutralise both test-side — each has its own dedicated coverage elsewhere
    # (tests/test_behavior_pat_sense{,_hardware}.py).
    from reachy.behavior.pat_sense import PatSenseDriver as _RealDriver

    monkeypatch.setattr(
        "reachy.cli._commands.behavior.PatSenseDriver",
        lambda **kw: _RealDriver(**{**kw, "warmup_s": 0.0, "still_hold_s": 0.0}),
    )

    rc = main(["behavior", "engine", "run", "--no-base-layer", "--max-ticks", "8", "--export", "-"])
    assert rc == 0

    sense_blocks = [b for b in _blocks(capsys.readouterr().out) if b["t"] == "sense"]
    pats = [b["pat"] for b in sense_blocks if b["pat"] is not None]
    assert pats, "no sense block carried a pat_event — the pat stack is not composed into sense"
    assert pats[0] == ["scratch", "level1"]
    assert reader.reads > 0, "the injected reader was never consulted — pat driver did not run"


# --------------------------------------------------------------------------- #
# 2. A spool goto reaches the lane through the registered GOTO kind           #
# --------------------------------------------------------------------------- #


def test_goto_command_reaches_the_lane_and_emits_admitted_and_done(_isolated, monkeypatch):
    """A goto dropped in the intents spool routes through the registered GOTO kind
    to the ``GotoLane`` and emits ``goto.admitted`` then ``goto.done`` on the bus.

    Driven through ``_compose_run_seam`` + ``engine.run`` with an injected clock so
    the 0.1 s goto completes deterministically (real wall-clock would not advance
    with a no-op sleep). Also forces the SDK-absent path, doubling as proof the
    goto path needs no SDK."""
    from reachy.behavior import engine as engine_mod
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod
    from reachy.robot.state_reader import HeldStateReader

    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: None))
    control.submit("goto", namespace=INTENT_NAMESPACE, head={"yaw": 1.0}, duration=0.1)

    events: list[dict] = []
    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    sense_reader, tick_seam, reader = behavior_mod._compose_run_seam(
        transport, config, None, events.append
    )

    try:
        engine_mod.run(
            transport,
            config,
            sleep=lambda *_: None,
            now=_StepClock(dt=0.05),
            max_ticks=8,
            control=control.CommandSpool(),
            sense=sense_reader,
            tick_seam=tick_seam,
        )
    finally:
        if reader is not None:  # None when the opt-in pat stack is off
            reader.close()

    raw_types = [e.get("type") for e in events if isinstance(e, dict)]
    assert "goto.admitted" in raw_types, f"goto never admitted through the lane (types={raw_types})"
    assert "goto.done" in raw_types, f"goto never completed (types={raw_types})"


def test_goto_admitted_through_the_full_cli_export(_isolated, capsys):
    """The full CLI path: a spool goto surfaces as a ``motion`` block with
    ``action=goto`` / ``phase=admitted`` in ``cmd_engine_run``'s export feed."""
    control.submit("goto", namespace=INTENT_NAMESPACE, head={"yaw": 1.0}, duration=5.0)
    rc = main(["behavior", "engine", "run", "--max-ticks", "6", "--export", "-"])
    assert rc == 0

    motion = [b for b in _blocks(capsys.readouterr().out) if b["t"] == "motion"]
    phases = {m["detail"].get("phase") for m in motion if m["action"] == "goto"}
    assert "admitted" in phases, f"goto not admitted through the CLI (motion blocks={motion})"


# --------------------------------------------------------------------------- #
# 3. Degrade: no [sdk] extra -> DoA-only sense, no pat, no crash              #
# --------------------------------------------------------------------------- #


def test_degrades_to_doa_only_when_sdk_absent(_isolated, monkeypatch, capsys):
    """With the SDK extra absent the composition still runs: the DoA/speech leg
    keeps flowing, no pat ever surfaces, and nothing raises."""
    from reachy.robot.state_reader import HeldStateReader

    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _SpeechTransport()
    )
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: None))

    rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--export", "-"])
    assert rc == 0

    sense_blocks = [b for b in _blocks(capsys.readouterr().out) if b["t"] == "sense"]
    assert sense_blocks, "no sense blocks — DoA-only perception stopped publishing with no SDK"
    assert any(b["speech"] for b in sense_blocks), "the DoA/speech leg stopped flowing"
    assert all(b["pat"] is None for b in sense_blocks), "a pat surfaced with no SDK reader"


# --------------------------------------------------------------------------- #
# 4. Boundary: no speech.llm import, no media_session call                    #
# --------------------------------------------------------------------------- #


def test_composition_module_does_not_import_speech_llm_or_touch_media_session():
    import reachy.cli._commands.behavior as behavior_mod

    for name in _imported_modules(behavior_mod):
        assert "speech.llm" not in name, f"behavior.py must not import speech.llm ({name!r})"
    src = inspect.getsource(behavior_mod)
    assert "media_session" not in src, "the runtime composition must never touch a media_session"
    # The pose source is the media-free held reader, positively (not a media session).
    assert "HeldStateReader" in src


# --------------------------------------------------------------------------- #
# 5. reader.close() on engine shutdown (normal + on a raising loop)           #
# --------------------------------------------------------------------------- #


def test_reader_close_invoked_on_engine_stop(_isolated, monkeypatch):
    reader = _ScriptedReader([(0.0, 0.0)])
    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.setattr("reachy.cli._commands.behavior._make_state_reader", lambda: reader)

    rc = main(["behavior", "engine", "run", "--max-ticks", "3", "--json"])
    assert rc == 0
    assert reader.closed, "reader.close() was not called on engine shutdown"


def test_reader_close_invoked_even_when_engine_loop_raises(_isolated, monkeypatch):
    reader = _ScriptedReader([(0.0, 0.0)])
    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.setattr("reachy.cli._commands.behavior._make_state_reader", lambda: reader)

    def _boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr("reachy.cli._commands.behavior.engine_run", _boom)

    rc = main(["behavior", "engine", "run", "--max-ticks", "3", "--json"])
    # main() converts the unexpected exception into a non-zero exit via _dispatch;
    # this test pins the finally-block teardown, not the exit code.
    assert rc != 0
    assert reader.closed, "reader.close() must run even when the engine loop raises"


# --------------------------------------------------------------------------- #
# 6. REACHY_PAT_SENSE parsing — absent vs empty vs falsey vs truthy (Qodo #4)  #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, True, id="absent"),
        pytest.param("", False, id="empty"),
        pytest.param("   ", False, id="blank-whitespace"),
        pytest.param("0", False, id="zero"),
        pytest.param("false", False, id="false"),
        pytest.param("no", False, id="no"),
        pytest.param("OFF", False, id="OFF-case-insensitive"),
        pytest.param("  off  ", False, id="off-with-whitespace"),
        pytest.param("1", True, id="one"),
        pytest.param("true", True, id="true"),
        pytest.param("yes", True, id="yes"),
        pytest.param("on", True, id="on"),
        pytest.param("banana", True, id="arbitrary-string"),
    ],
)
def test_pat_sense_enabled_env_parsing(monkeypatch, raw, expected):
    """``REACHY_PAT_SENSE`` absent -> ON (default since issue #80); an explicit but
    empty/blank value -> OFF rather than silently falling through to the default
    (Qodo review finding #4 on PR #83 — the old denylist check let
    ``REACHY_PAT_SENSE=`` slip past every falsey token and enable the sense); any
    other explicit falsey token (``0``/``false``/``no``/``off``, case/whitespace
    insensitive) -> OFF; anything else (``1``/``true``/``yes``/``on``, or any other
    non-blank string) -> ON."""
    from reachy.cli._commands.behavior import _pat_sense_enabled

    if raw is None:
        monkeypatch.delenv("REACHY_PAT_SENSE", raising=False)
    else:
        monkeypatch.setenv("REACHY_PAT_SENSE", raw)

    assert _pat_sense_enabled() is expected
