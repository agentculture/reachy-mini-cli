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

    Duck-types the members the composition uses — ``warm_up`` (t28's setup-thread
    connect), ``connected`` (what the holder keeper polls), ``read`` (the pat
    driver's actual-pose source) and ``close`` (the shutdown teardown) — and
    records them so a test can assert the reader was warmed, consulted and
    released.

    ``warm_up``/``connected`` are here rather than guarded at the call site
    deliberately: the composition warms its holders UNCONDITIONALLY, so a
    ``getattr``-guarded call would let a real holder that silently lost its
    warm-up slip through unnoticed — reintroducing exactly the 425-1213 ms
    startup overrun t28 exists to remove. The fake models the real shape instead.
    """

    def __init__(self, script):
        self._script = list(script)
        self._i = 0
        self.reads = 0
        self.warm_calls = 0
        self.connected = False
        self.closed = False

    def warm_up(self):
        self.warm_calls += 1
        self.connected = True
        return True

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
    # (tests/test_behavior_pat_sense{,_hardware}.py). There is a THIRD real-clock
    # window: ``max_observation_gap_s`` (default 0.2 s,
    # ``DEFAULT_MAX_OBSERVATION_GAP_S`` in ``reachy/behavior/pat_sense.py``).
    # This test drives the full CLI (``main([...])``), so ``ctx.now`` is the
    # engine's REAL ``time.monotonic()``, not an injected test clock — unlike the
    # warmup/stillness windows, this one is not something the test controls the
    # pacing of. Each of the 8 ticks is nominally ~20 ms apart (50 Hz), but on a
    # loaded runner (this suite's own parallel workers included) a single tick can
    # take longer than the 0.2 s gap to come around. When it does,
    # ``_observation_clock_gapped`` reads the stall as the interaction having gone
    # quiet and clears it via ``_blocked_edge`` — dropping the scripted pat before
    # this test ever gets to observe it. Neutralise it the same way as the other
    # two (0.0 disables the check in ``_observation_clock_gapped``), leaving
    # ``hp_tau`` untouched: it is a high-pass TIME CONSTANT, not a pacing window,
    # and CLAUDE.md is explicit that it must never be overridden downward — a
    # sensitivity knob, not a source of this flake.
    from reachy.behavior.pat_sense import PatSenseDriver as _RealDriver

    monkeypatch.setattr(
        "reachy.cli._commands.behavior.PatSenseDriver",
        lambda **kw: _RealDriver(
            **{**kw, "warmup_s": 0.0, "still_hold_s": 0.0, "max_observation_gap_s": 0.0}
        ),
    )

    rc = main(["behavior", "engine", "run", "--no-base-layer", "--max-ticks", "8", "--export", "-"])
    assert rc == 0

    sense_blocks = [b for b in _blocks(capsys.readouterr().out) if b["t"] == "sense"]
    pats = [b["pat"] for b in sense_blocks if b["pat"] is not None]
    assert pats, "no sense block carried a pat_event — the pat stack is not composed into sense"
    assert pats[0] == ["scratch", "level1"]
    detected = next(block for block in sense_blocks if block["pat"] is not None)
    state = detected["pat_state"]
    assert {key: value for key, value in state.items() if not key.endswith("_at")} == {
        "availability": "available",
        "contact": True,
        "touch_type": "scratch",
        "level": "level1",
        "yaw_deg": None,
        "phase": "receptive",
    }
    assert state["phase_started_at"] <= state["last_press_at"]
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
# 4. Boundary: no speech.llm import, no SECOND media client                   #
# --------------------------------------------------------------------------- #


def test_composition_module_does_not_import_speech_llm_or_open_a_second_media_client():
    """The composition stays cognition-free and never opens a SECOND media client.

    This used to grep for the literal ``media_session`` being ABSENT. That proxy
    expired: the runtime's voice now plays through the ONE held media client's
    session (t10), so the composition names it legitimately. The invariant the
    grep stood for was never "the string is absent" — it was **the
    single-SDK-owner model**: pose reads come from the media-FREE held reader,
    and nothing here opens a media client of its own. Both are asserted
    directly below, which is strictly stronger than the old string check:
    ``_open_sdk_media`` is the actual second-client opener
    (``reachy.speech.playback``), and referencing it here is the real defect the
    old assertion was reaching for.
    """
    import reachy.cli._commands.behavior as behavior_mod

    for name in _imported_modules(behavior_mod):
        assert "speech.llm" not in name, f"behavior.py must not import speech.llm ({name!r})"
    src = inspect.getsource(behavior_mod)
    assert "_open_sdk_media" not in src, (
        "the composition must never open a SECOND media client — the voice plays "
        "through the ONE held client injected via media_session_provider"
    )
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


# --------------------------------------------------------------------------- #
# 7. Stillness gate tuning surface — REACHY_PAT_STILL_HOLD_S / _EPS (t2)      #
# --------------------------------------------------------------------------- #
#
# t2 ("no-freeze pat sense") makes the stillness gate's ``still_hold_s`` /
# ``still_eps`` reachable without editing source, so a bench experiment can
# retune them. Three things are pinned here: (a) unset -> today's shipped
# values reach PatSenseDriver's constructor unchanged; (b) an explicit
# override actually reaches the constructor; (c) REACHY_PAT_SENSE's existing
# on/off semantics stay authoritative — setting an override alone must never
# turn the pat stack on.


def _capture_pat_sense_driver(monkeypatch):
    """Monkeypatch ``PatSenseDriver`` to record every construction's kwargs
    while still building a REAL driver underneath it, so the composed seam
    keeps working end to end (mirrors the section-1 test's spy pattern)."""
    from reachy.behavior.pat_sense import PatSenseDriver as _RealDriver

    calls: list[dict] = []

    def _spy(**kwargs):
        calls.append(kwargs)
        return _RealDriver(**kwargs)

    monkeypatch.setattr("reachy.cli._commands.behavior.PatSenseDriver", _spy)
    return calls


def test_compose_run_seam_default_pat_sense_uses_todays_shipped_values(_isolated, monkeypatch):
    """Unset ``REACHY_PAT_STILL_HOLD_S`` / ``REACHY_PAT_STILL_EPS`` -> the composed
    ``PatSenseDriver`` is constructed with exactly today's shipped defaults
    (0.5 s / 0.01 deg), so current behavior stays byte-identical for an operator
    who never sets either var."""
    from reachy.behavior.engine import EngineConfig
    from reachy.behavior.pat_sense import DEFAULT_STILL_EPS, DEFAULT_STILL_HOLD_S
    from reachy.cli._commands import behavior as behavior_mod

    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.delenv("REACHY_PAT_STILL_HOLD_S", raising=False)
    monkeypatch.delenv("REACHY_PAT_STILL_EPS", raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior._make_state_reader",
        lambda: _ScriptedReader([(0.0, 0.0)]),
    )
    calls = _capture_pat_sense_driver(monkeypatch)

    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=False, settle=False)
    _sense_reader, _tick_seam, reader = behavior_mod._compose_run_seam(
        transport, config, None, None
    )
    if reader is not None:
        reader.close()

    assert len(calls) == 1, "PatSenseDriver must be constructed exactly once"
    # The literals are repeated on purpose: this is a tripwire, so a default
    # change has to be acknowledged here rather than sliding through. Moved
    # 0.5/0.01 -> 1.0/0.035 in v0.41.0 when the swing-era gate became shipped.
    assert calls[0]["still_hold_s"] == DEFAULT_STILL_HOLD_S == 1.0
    assert calls[0]["still_eps"] == DEFAULT_STILL_EPS == 0.035


def test_compose_run_seam_env_override_reaches_the_driver(_isolated, monkeypatch):
    """``REACHY_PAT_STILL_HOLD_S`` / ``REACHY_PAT_STILL_EPS`` actually reach the
    composed ``PatSenseDriver``'s constructor — not just a module-level default."""
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod

    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.setenv("REACHY_PAT_STILL_HOLD_S", "1.25")
    monkeypatch.setenv("REACHY_PAT_STILL_EPS", "0.05")
    monkeypatch.setattr(
        "reachy.cli._commands.behavior._make_state_reader",
        lambda: _ScriptedReader([(0.0, 0.0)]),
    )
    calls = _capture_pat_sense_driver(monkeypatch)

    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=False, settle=False)
    _sense_reader, _tick_seam, reader = behavior_mod._compose_run_seam(
        transport, config, None, None
    )
    if reader is not None:
        reader.close()

    assert len(calls) == 1
    assert calls[0]["still_hold_s"] == pytest.approx(1.25)
    assert calls[0]["still_eps"] == pytest.approx(0.05)


def test_still_tuning_env_vars_do_not_enable_pat_sense_when_toggle_is_off(_isolated, monkeypatch):
    """``REACHY_PAT_SENSE``'s existing on/off semantics stay authoritative: setting
    the stillness overrides never turns the pat stack on by themselves."""
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod

    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setenv("REACHY_PAT_STILL_HOLD_S", "9.0")
    monkeypatch.setenv("REACHY_PAT_STILL_EPS", "9.0")
    calls = _capture_pat_sense_driver(monkeypatch)

    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=False, settle=False)
    _sense_reader, _tick_seam, resources = behavior_mod._compose_run_seam(
        transport, config, None, None
    )
    resources.close()

    assert calls == [], "PatSenseDriver must not be constructed when REACHY_PAT_SENSE is off"
    # The pose reader is the pat sense's own holder, so it is not opened at all;
    # the media client is a separate, unconditional resource (t28), which is why
    # the seam now returns a resource bundle rather than the bare reader.
    assert resources.pose_reader is None
    assert resources.media is not None


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 1.0, id="absent-default"),
        pytest.param("1.0", 1.0, id="matches-default"),
        pytest.param("2", 2.0, id="override"),
        pytest.param("0", 0.0, id="zero-disables-gate"),
        pytest.param("  1.5  ", 1.5, id="whitespace-trimmed"),
    ],
)
def test_pat_still_hold_s_env_parsing(monkeypatch, raw, expected):
    from reachy.cli._commands.behavior import _pat_still_tuning

    if raw is None:
        monkeypatch.delenv("REACHY_PAT_STILL_HOLD_S", raising=False)
    else:
        monkeypatch.setenv("REACHY_PAT_STILL_HOLD_S", raw)
    monkeypatch.delenv("REACHY_PAT_STILL_EPS", raising=False)

    still_hold_s, _still_eps = _pat_still_tuning()
    assert still_hold_s == pytest.approx(expected)


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        pytest.param(None, 0.035, id="absent-default"),
        pytest.param("0.035", 0.035, id="matches-default"),
        pytest.param("0.25", 0.25, id="override"),
    ],
)
def test_pat_still_eps_env_parsing(monkeypatch, raw, expected):
    from reachy.cli._commands.behavior import _pat_still_tuning

    monkeypatch.delenv("REACHY_PAT_STILL_HOLD_S", raising=False)
    if raw is None:
        monkeypatch.delenv("REACHY_PAT_STILL_EPS", raising=False)
    else:
        monkeypatch.setenv("REACHY_PAT_STILL_EPS", raw)

    _still_hold_s, still_eps = _pat_still_tuning()
    assert still_eps == pytest.approx(expected)


@pytest.mark.parametrize("raw", ["banana", "", "  ", "1.2.3", "1,5"])
def test_pat_still_hold_s_malformed_value_raises_clean_error(monkeypatch, raw):
    from reachy.cli._commands.behavior import _pat_still_tuning
    from reachy.cli._errors import EXIT_USER_ERROR, CliError

    monkeypatch.setenv("REACHY_PAT_STILL_HOLD_S", raw)
    monkeypatch.delenv("REACHY_PAT_STILL_EPS", raising=False)

    with pytest.raises(CliError) as excinfo:
        _pat_still_tuning()
    assert excinfo.value.code == EXIT_USER_ERROR
    assert "REACHY_PAT_STILL_HOLD_S" in excinfo.value.message


@pytest.mark.parametrize("raw", ["banana", "", "1e", "1_2_3xyz"])
def test_pat_still_eps_malformed_value_raises_clean_error(monkeypatch, raw):
    from reachy.cli._commands.behavior import _pat_still_tuning
    from reachy.cli._errors import EXIT_USER_ERROR, CliError

    monkeypatch.delenv("REACHY_PAT_STILL_HOLD_S", raising=False)
    monkeypatch.setenv("REACHY_PAT_STILL_EPS", raw)

    with pytest.raises(CliError) as excinfo:
        _pat_still_tuning()
    assert excinfo.value.code == EXIT_USER_ERROR
    assert "REACHY_PAT_STILL_EPS" in excinfo.value.message


def test_engine_run_cli_reports_clean_error_on_malformed_still_tuning_env(_isolated, monkeypatch):
    """The malformed-value error surfaces through the full CLI path (never a
    raw traceback) — ``main()``'s ``_dispatch`` wraps the raised ``CliError``."""
    monkeypatch.setenv("REACHY_PAT_SENSE", "1")
    monkeypatch.setenv("REACHY_PAT_STILL_HOLD_S", "not-a-number")

    rc = main(["behavior", "engine", "run", "--max-ticks", "1", "--json"])
    assert rc == 1


def test_pat_env_rejects_non_finite_and_negative_values(monkeypatch) -> None:
    """'nan'/'inf' parse as floats but are never valid tuning.

    Left unchecked they propagate into the conditioning filters and thresholds
    and silently disable sensing, rather than reporting the operator's mistake —
    which is exactly what the tuning surface's error contract promises not to do.
    """
    from reachy.cli._commands.behavior import _pat_float_env
    from reachy.cli._errors import EXIT_USER_ERROR, CliError

    for raw in ("nan", "inf", "-inf", "NaN", "Infinity"):
        monkeypatch.setenv("REACHY_PAT_HP_TAU", raw)
        with pytest.raises(CliError) as excinfo:
            _pat_float_env("REACHY_PAT_HP_TAU", 0.8)
        assert excinfo.value.code == EXIT_USER_ERROR
        assert "finite" in str(excinfo.value.message)

    monkeypatch.setenv("REACHY_PAT_HP_TAU", "-0.5")
    with pytest.raises(CliError) as excinfo:
        _pat_float_env("REACHY_PAT_HP_TAU", 0.8)
    assert "non-negative" in str(excinfo.value.message)

    # Valid values still pass straight through, including an explicit zero.
    for raw, expected in (("0", 0.0), ("0.08", 0.08), ("2.5", 2.5)):
        monkeypatch.setenv("REACHY_PAT_HP_TAU", raw)
        assert _pat_float_env("REACHY_PAT_HP_TAU", 0.8) == expected
