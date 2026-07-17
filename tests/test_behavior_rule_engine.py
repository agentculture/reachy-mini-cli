"""Rule evaluation on the engine tick — react / inhibit / mode, observability, seam.

These tests pin the four acceptance criteria for the rules evaluator:

1. A rules file demonstrably changes robot behavior in a bounded ``max_ticks``
   engine run with an injected clock and ZERO LLM / network calls.
2. A two-rule ping-pong fixture and an every-tick-refire fixture both settle
   under cooldown / hysteresis in deterministic runs.
3. Every fire / inhibition / suppression / cooldown-skip emits a
   ``[SENSE stage=rule ...]`` line with rule id + reason; a log capture
   reconstructs every decision — a silent action is a test failure.
4. The rule engine rides the engine's ONE injected ``tick_seam`` (with goto /
   export as peer riders), never imported by ``engine.py``.

Everything is deterministic: the evaluator is fed an injected clock and scripted
:class:`~reachy.behavior.sense.Sense` snapshots; the integration run uses the
engine's own injectable ``sleep`` / ``now`` / ``max_ticks`` seams and a fake
in-memory streaming sink, so no robot, daemon, network, or LLM is touched.
"""

from __future__ import annotations

import contextlib
import logging
from dataclasses import dataclass, field

import pytest

from reachy.behavior import engine as E
from reachy.behavior import rule_engine as R
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.model import StopClass
from reachy.behavior.rule_engine import RuleEngine, TickBus, compose_rule_seam
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import EMPTY_SENSE, Sense
from reachy.cli._errors import CliError

SENSE_LOGGER = "reachy.sense"


# --------------------------------------------------------------------------- #
# Fakes / harness                                                             #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed :class:`TickContext` that records every seam interaction.

    ``keep_active`` models whether an admitted behavior lingers in the active
    set (a looping behavior) or is treated as already gone next tick (a one-shot
    that expired) — the latter lets cooldown, not the already-active dedup, be
    the thing under test.
    """

    now: float = 0.0
    tick: int = 0
    sense: Sense = EMPTY_SENSE
    ownership: dict = field(default_factory=dict)
    keep_active: bool = True
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        if self.keep_active:
            self._active.add(behavior.name)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        self._active.discard(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set(self._active)


def _drive_ticks(engine: RuleEngine, senses, *, dt=0.25, start=0.25, keep_active=False):
    """Feed *senses* to *engine* one tick at a time on a fixed-step clock."""
    ctx = _RecordingCtx(keep_active=keep_active)
    t = start
    for i, sense in enumerate(senses):
        ctx.now = round(t, 10)
        ctx.tick = i + 1
        ctx.sense = sense
        engine.on_tick(ctx)
        t += dt
    return ctx


def _sense_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


def _react(rule_id, field, op, run, *, value=None, cooldown_s=5.0, hysteresis=0.0, params=None):
    when = {"field": field, "op": op}
    if value is not None:
        when["value"] = value
    rule = {
        "id": rule_id,
        "when": when,
        "run": run,
        "cooldown_s": cooldown_s,
        "hysteresis": hysteresis,
    }
    if params:
        rule["params"] = params
    return rule


def _inhibit(rule_id, field, op, disable, *, value=None, cooldown_s=5.0, hysteresis=0.0):
    when = {"field": field, "op": op}
    if value is not None:
        when["value"] = value
    return {
        "id": rule_id,
        "when": when,
        "disable": list(disable),
        "cooldown_s": cooldown_s,
        "hysteresis": hysteresis,
    }


# --------------------------------------------------------------------------- #
# Import boundary (criterion 4) — engine.py never imports the rule engine     #
# --------------------------------------------------------------------------- #


def test_engine_does_not_import_rule_engine() -> None:
    import inspect

    import reachy.behavior.engine as eng_mod

    # No runtime dependency: the module object is not bound in engine's namespace.
    assert not isinstance(eng_mod.__dict__.get("rule_engine"), type(R))
    # And no import statement pulls it in (a docstring usage-hint is fine).
    for line in inspect.getsource(eng_mod).splitlines():
        stripped = line.strip()
        if stripped.startswith(("import ", "from ")):
            assert "rule_engine" not in stripped, f"engine.py must not import rule_engine: {line}"


def test_rule_engine_is_callable_seam_and_exposes_no_engine_dependency() -> None:
    cfg = RulesConfig.from_dict({"react": [_react("r", "speech", "is_true", "nod")]})
    re = RuleEngine(cfg)
    assert callable(re)  # usable directly as tick_seam=re


# --------------------------------------------------------------------------- #
# Criterion 3 — every decision is logged with rule id + reason                #
# --------------------------------------------------------------------------- #


def test_fire_emits_senselog_stage_line_with_rule_id(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(speech_detected=True)])
    lines = _sense_lines(caplog)
    assert len(lines) == 1
    assert lines[0].startswith("[SENSE stage=rule source=speech event=hear]")
    assert "fired" in lines[0] and "run=nod" in lines[0]
    assert len(ctx.admits) == 1 and ctx.admits[0].name == "nod"


def test_no_match_tick_is_silent(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(speech_detected=False)] * 5)
    assert _sense_lines(caplog) == []  # no-match is NOT logged per tick
    assert ctx.admits == []


def test_cooldown_skip_is_logged_with_reason(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("hear", "speech", "is_true", "nod", cooldown_s=1.0)]}
    )
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        # true every tick, dt=0.25 -> fires t=0.25, then cooldown until t>=1.25
        _drive_ticks(re, [Sense(speech_detected=True)] * 3)
    lines = _sense_lines(caplog)
    assert lines[0].endswith("] fired kind=react run=nod")
    assert lines[1].endswith("] dropped reason=cooldown")
    assert lines[2].endswith("] dropped reason=cooldown")
    for ln in lines:
        assert "event=hear" in ln  # rule id present on every decision


def test_log_capture_reconstructs_every_decision(caplog) -> None:
    """Every ctx side effect (admit) has a matching [SENSE] line — nothing silent."""
    cfg = RulesConfig.from_dict(
        {"react": [_react("hear", "speech", "is_true", "nod", cooldown_s=1.0)]}
    )
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(speech_detected=True)] * 8)
    fired = [ln for ln in _sense_lines(caplog) if "fired" in ln]
    # t=0.25..2.0 (dt=0.25, cd=1.0) -> fires at 0.25, 1.25; each admit has a fired line.
    assert len(fired) == len(ctx.admits) == 2


# --------------------------------------------------------------------------- #
# Criterion 2 — refire fixture settles under cooldown                         #
# --------------------------------------------------------------------------- #


def test_every_tick_refire_settles_under_cooldown(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("loud", "rms", "gt", "nod", value=0.05, cooldown_s=1.0)]}
    )
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(rms=0.1)] * 20, keep_active=False)
    lines = _sense_lines(caplog)
    fires = [ln for ln in lines if "fired" in ln]
    cooldowns = [ln for ln in lines if "reason=cooldown" in ln]
    # dt=0.25, cd=1.0 over t=0.25..5.0 -> fires at 0.25,1.25,2.25,3.25,4.25
    assert len(fires) == 5
    assert len(cooldowns) == 15
    assert len(lines) == 20  # every matching tick emits exactly one decision
    assert len(ctx.admits) == 5


def test_refire_without_cooldown_fires_every_tick() -> None:
    """Control: with cooldown 0 (and no hysteresis) it fires every matching tick."""
    cfg = RulesConfig.from_dict(
        {"react": [_react("loud", "rms", "gt", "nod", value=0.05, cooldown_s=0.0)]}
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(rms=0.1)] * 6, keep_active=False)
    assert len(ctx.admits) == 6  # unbounded without cooldown -> the thing cooldown tames


# --------------------------------------------------------------------------- #
# Criterion 2 — two-rule ping-pong settles under cooldown                     #
# --------------------------------------------------------------------------- #


def test_two_rule_ping_pong_settles_under_cooldown(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [
                _react("A", "speech", "is_true", "nod", cooldown_s=1.0),
                _react("B", "speech", "is_false", "shake", cooldown_s=1.0),
            ]
        }
    )
    re = RuleEngine(cfg)
    # speech alternates every tick -> A and B would each fire every other tick
    # without cooldown (10 each); cooldown=1.0 (dt=0.25) settles them to 5 each.
    senses = [Sense(speech_detected=(i % 2 == 0)) for i in range(20)]
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, senses, keep_active=False)
    lines = _sense_lines(caplog)
    a_fires = [ln for ln in lines if "event=A]" in ln and "fired" in ln]
    b_fires = [ln for ln in lines if "event=B]" in ln and "fired" in ln]
    a_cool = [ln for ln in lines if "event=A]" in ln and "reason=cooldown" in ln]
    b_cool = [ln for ln in lines if "event=B]" in ln and "reason=cooldown" in ln]
    assert len(a_fires) == 5 and len(b_fires) == 5  # settled, not 10 each
    assert len(a_cool) == 5 and len(b_cool) == 5
    assert len(lines) == 20  # exactly one decision per tick (the non-matching rule is silent)
    names = [b.name for b in ctx.admits]
    assert names.count("nod") == 5 and names.count("shake") == 5


# --------------------------------------------------------------------------- #
# Hysteresis semantics                                                        #
# --------------------------------------------------------------------------- #


def test_hysteresis_requires_continuous_false_before_refire(caplog) -> None:
    # cooldown 0 so ONLY hysteresis governs re-arming.
    cfg = RulesConfig.from_dict(
        {"react": [_react("h", "speech", "is_true", "nod", cooldown_s=0.0, hysteresis=1.0)]}
    )
    re = RuleEngine(cfg)
    # T,T,T (fire once, then rearming), then F for >=1.0s, then T (fires again)
    seq = [True, True, True, False, False, False, False, False, True]
    senses = [Sense(speech_detected=v) for v in seq]
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, senses, keep_active=False)
    lines = _sense_lines(caplog)
    fires = [ln for ln in lines if "fired" in ln]
    rearming = [ln for ln in lines if "reason=rearming" in ln]
    assert len(fires) == 2  # t=0.25 and after the >=1.0s false window
    assert len(rearming) == 2  # the two held-true ticks before predicate went false
    assert "reason=cooldown" not in "\n".join(lines)
    assert len(ctx.admits) == 2


def test_hysteresis_false_run_interrupted_resets_rearm() -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("h", "speech", "is_true", "nod", cooldown_s=0.0, hysteresis=1.0)]}
    )
    re = RuleEngine(cfg)
    # fire, then a false run that never reaches 1.0s continuously (interrupted by True)
    # so it never re-arms -> only the first fire.
    seq = [True, False, False, True, False, False, True, False, False, True]
    ctx = _drive_ticks(re, [Sense(speech_detected=v) for v in seq], keep_active=False)
    assert len(ctx.admits) == 1  # never re-armed


def test_hysteresis_zero_means_cooldown_alone_governs() -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("h", "speech", "is_true", "nod", cooldown_s=1.0, hysteresis=0.0)]}
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(speech_detected=True)] * 8, keep_active=False)
    assert len(ctx.admits) == 2  # t=0.25, 1.25 — cooldown only, no rearm gate


# --------------------------------------------------------------------------- #
# absent_for semantics                                                        #
# --------------------------------------------------------------------------- #


def test_absent_for_fires_after_field_absent_for_duration(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("lonely", "face", "absent_for", "nod", value=1.0, cooldown_s=100.0)]}
    )
    re = RuleEngine(cfg)
    # face present at t=0.5, then absent; dt=0.5 -> absent>=1.0s first true at t=2.0
    senses = [Sense(face="Ada")] + [Sense(face=None)] * 5
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, senses, dt=0.5, start=0.5, keep_active=False)
    fires = [ln for ln in _sense_lines(caplog) if "fired" in ln]
    assert len(fires) == 1
    assert len(ctx.admits) == 1


def test_absent_for_not_yet_reached_does_not_fire() -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("lonely", "face", "absent_for", "nod", value=10.0)]}
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(face=None)] * 5, dt=0.5, start=0.5, keep_active=False)
    assert ctx.admits == []  # only 2.5s absent, threshold 10s


# --------------------------------------------------------------------------- #
# Inhibit semantics                                                           #
# --------------------------------------------------------------------------- #


def test_inhibit_blocks_react_and_logs_inhibited(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [_react("hear", "speech", "is_true", "nod")],
            "inhibit": [_inhibit("hush", "pat", "is_true", ["nod"])],
        }
    )
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(speech_detected=True, pat_event=("scratch", "level1"))])
    lines = _sense_lines(caplog)
    assert any("event=hear" in ln and "reason=inhibited" in ln for ln in lines)
    assert any("event=hush" in ln and "fired" in ln for ln in lines)
    assert ctx.admits == []  # react suppressed
    assert "nod" in ctx.evicts  # inhibit evicts the named behavior


def test_inhibit_lifts_when_predicate_clears(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [_react("hear", "speech", "is_true", "nod")],
            "inhibit": [_inhibit("hush", "pat", "is_true", ["nod"])],
        }
    )
    re = RuleEngine(cfg)
    senses = [
        Sense(speech_detected=True, pat_event=("scratch", "level1")),  # inhibited
        Sense(speech_detected=True, pat_event=None),  # inhibit lifts -> react fires
    ]
    ctx = _drive_ticks(re, senses, keep_active=True)
    assert len(ctx.admits) == 1 and ctx.admits[0].name == "nod"


# --------------------------------------------------------------------------- #
# Mode semantics — parameter-set swap                                         #
# --------------------------------------------------------------------------- #


def test_active_mode_params_swap_into_react_behavior() -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [_react("hear", "speech", "is_true", "nod")],
            "modes": {"calm": {"amp": 3.0}, "wild": {"amp": 30.0}},
            "active_mode": "calm",
        }
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(speech_detected=True)])
    assert ctx.admits[0].params["amp"] == 3.0  # 'calm' mode swapped in (nod default is 12.0)


def test_rule_params_override_mode_params() -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [_react("hear", "speech", "is_true", "nod", params={"amp": 7.0})],
            "modes": {"calm": {"amp": 3.0}},
            "active_mode": "calm",
        }
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(speech_detected=True)])
    assert ctx.admits[0].params["amp"] == 7.0  # rule params win over mode


def test_mode_param_not_a_behavior_param_is_ignored() -> None:
    cfg = RulesConfig.from_dict(
        {
            "react": [_react("hear", "speech", "is_true", "nod")],
            "modes": {"m": {"unrelated": 9.0}},
            "active_mode": "m",
        }
    )
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(speech_detected=True)])
    assert "unrelated" not in ctx.admits[0].params  # unknown key does not leak in


# --------------------------------------------------------------------------- #
# already-active dedup                                                        #
# --------------------------------------------------------------------------- #


def test_already_active_behavior_is_not_readmitted(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {"react": [_react("hear", "speech", "is_true", "nod", cooldown_s=0.0)]}
    )
    re = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re, [Sense(speech_detected=True)] * 4, keep_active=True)
    assert len(ctx.admits) == 1  # admitted once; then already active
    assert sum("reason=already-active" in ln for ln in _sense_lines(caplog)) == 3


# --------------------------------------------------------------------------- #
# emit fan-out via TickBus (criterion 4 — export/goto ride the same seam)     #
# --------------------------------------------------------------------------- #


def test_emit_events_fan_out_to_bus_consumers() -> None:
    received = []
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    bus = compose_rule_seam(cfg, consumers=[received.append])
    ctx = _RecordingCtx(now=0.25, tick=1, sense=Sense(speech_detected=True))
    # ctx.emit must route to the bus's registered consumers
    ctx.emit = bus.emit
    bus(ctx)
    fires = [e for e in received if e["type"] == R.EVENT_FIRE]
    assert len(fires) == 1
    assert fires[0]["rule"] == "hear" and fires[0]["behavior"] == "nod"
    assert fires[0]["kind"] == "react"


def test_bus_isolates_a_raising_driver_and_consumer() -> None:
    def boom_driver(ctx):
        raise RuntimeError("driver boom")

    def boom_consumer(event):
        raise RuntimeError("consumer boom")

    seen = []
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    bus = TickBus(
        drivers=[boom_driver, RuleEngine(cfg)],
        consumers=[boom_consumer, seen.append],
    )
    ctx = _RecordingCtx(now=0.25, tick=1, sense=Sense(speech_detected=True))
    ctx.emit = bus.emit
    bus(ctx)  # must not raise despite the boom driver
    assert len(ctx.admits) == 1  # RuleEngine still ran after boom_driver
    assert any(e["type"] == R.EVENT_FIRE for e in seen)  # seen consumer still got the event


# --------------------------------------------------------------------------- #
# Criterion 1 — a rules file changes behavior in a real bounded engine run    #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.poses = []
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        self.poses.append({"head": head, "antennas": antennas, "body_yaw": body_yaw})
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self, sink=None):
        self.sink = sink or _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink


class _Clock:
    def __init__(self, dt=0.02):
        self.t = 0.0
        self.dt = dt

    def __call__(self):
        self.t += self.dt
        return self.t


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def test_rules_file_changes_robot_behavior_in_bounded_run(caplog) -> None:
    """A speech->nod rule flips head ownership from feel-alive to nod; no network."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    eng = Engine()
    bus = compose_rule_seam(cfg)

    calls = {"n": 0}

    def sense(_t):  # scripted perception — no daemon, no network
        calls["n"] += 1
        return Sense(speech_detected=True)

    tr = _FakeTransport()
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ticks = E.run(
            tr,
            EngineConfig(compose_hz=50, base_layer=True, settle=False),
            sleep=lambda *_: None,
            now=_Clock(),
            max_ticks=3,
            engine=eng,
            sense=sense,
            tick_seam=bus,
        )
    assert ticks == 3
    # The seam forced an ungated perception read every tick (no wants_sense behavior).
    assert calls["n"] == 3
    # The rule admitted a nod, which (stoppable) beats the passive feel-alive on head.
    active_names = {ab.behavior.name for ab in eng.active}
    assert "nod" in active_names
    assert eng._last_ownership["head"].startswith("rule:hear:")
    assert any("event=hear" in ln and "fired" in ln for ln in _sense_lines(caplog))


def test_without_the_rule_behavior_is_unchanged() -> None:
    """Control: no seam -> perception is gated off and feel-alive keeps the head."""
    eng = Engine()
    calls = {"n": 0}

    def sense(_t):
        calls["n"] += 1
        return Sense(speech_detected=True)

    tr = _FakeTransport()
    E.run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=lambda *_: None,
        now=_Clock(),
        max_ticks=3,
        engine=eng,
        sense=sense,  # supplied but no tick_seam -> gated (no wants_sense behavior)
    )
    assert calls["n"] == 0  # gated: never polled
    assert {ab.behavior.name for ab in eng.active} == {"feel-alive"}
    assert eng._last_ownership["head"].startswith("feel-alive")


def test_react_stop_class_and_lifetime_come_from_library_entry() -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "gaze-hold")]})
    re = RuleEngine(cfg)
    ctx = _drive_ticks(re, [Sense(speech_detected=True)])
    beh = ctx.admits[0]
    assert beh.stop_class is StopClass.STOPPABLE  # gaze-hold's default class
    assert beh.lifetime.duration == 5.0  # gaze-hold's default duration


def test_a_raising_rule_build_never_escapes_on_tick(monkeypatch) -> None:
    """Defensive: a per-rule failure is isolated, not propagated to the loop."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", "speech", "is_true", "nod")]})
    re = RuleEngine(cfg)

    def boom(*_a, **_k):
        raise CliError(code=1, message="boom", remediation="")

    monkeypatch.setattr(R.behavior_library, "build", boom)
    ctx = _RecordingCtx(now=0.25, tick=1, sense=Sense(speech_detected=True))
    re.on_tick(ctx)  # must not raise
    assert ctx.admits == []
