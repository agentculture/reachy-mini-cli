"""Per-transition suppression logging — the #99 drop-line flood fix.

The rule engine's gating (cooldown / hysteresis / already-active / inhibited)
is measured CORRECT on the live robot and is out of scope here (spec decision
c46). What was wrong is the emission cadence: a ``dropped reason=...`` senselog
line was emitted on EVERY tick a rule's predicate held while gated — at the
engine's ~23 Hz tick rate, one deployed rule produced 6722 drop lines in a 3 h
journal window (5414 ``already-active`` + 1308 ``cooldown``) against only 42
genuine fires (#99).

These tests pin the per-EPISODE cadence that replaces it. A "suppression
episode" (a gated streak) is a run of consecutive suppressed evaluations of one
rule; it ends when the rule fires or its predicate stops matching. The cadence:

* ONE ``dropped reason=<reason>`` line at streak entry;
* ONE further line only when the reason CHANGES mid-streak (e.g. ``cooldown``
  giving way to ``already-active``);
* ONE summary line when the streak ends, naming the reason(s) and the streak
  length in ticks (``... suppressed 214 ticks``) — emitted BEFORE the fire
  line when the streak ends in a fire, so the log stays chronological;
* a genuine FIRE still logs every time, unchanged;
* the :mod:`reachy.senselog` grammar is preserved — a drop always names its
  reason, just once per transition instead of once per tick.

The ``ctx.emit`` event stream (the export feed's ``rule.suppress`` blocks)
follows the SAME transition cadence — flooding the JSONL feed at 23 Hz is the
same defect as flooding the journal — and the summary event carries the episode
length as a structured ``ticks`` field.

Everything is deterministic: an injected fixed-step clock and scripted
:class:`~reachy.behavior.sense.Sense` snapshots; no robot, network, or LLM.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field

from reachy.behavior import rule_engine as R
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig
from reachy.behavior.sense import EMPTY_SENSE, Sense

SENSE_LOGGER = "reachy.sense"


# --------------------------------------------------------------------------- #
# Harness (mirrors test_behavior_rule_engine.py's fakes)                      #
# --------------------------------------------------------------------------- #


@dataclass
class _RecordingCtx:
    """A duck-typed ``TickContext`` recording every seam interaction."""

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


def _react(rule_id, run="nod", *, cooldown_s=5.0, hysteresis=0.0, duration_s=60.0):
    return {
        "id": rule_id,
        "when": {"field": "speech", "op": "is_true"},
        "run": run,
        "cooldown_s": cooldown_s,
        "hysteresis": hysteresis,
        "duration_s": duration_s,
    }


# --------------------------------------------------------------------------- #
# Streak entry — one line, not one per tick                                   #
# --------------------------------------------------------------------------- #


def test_gated_streak_emits_one_entry_line_not_one_per_tick(caplog) -> None:
    """The #99 flood shape: a predicate holding for 50 gated ticks used to log
    50 drop lines; now it logs exactly one at streak entry."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=100.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re_, [Sense(speech_detected=True)] * 51, keep_active=False)
    lines = _sense_lines(caplog)
    assert len(ctx.admits) == 1  # the gating DECISION is unchanged (c46)
    assert len(lines) == 2  # one fire + one streak-entry drop, nothing per-tick
    assert lines[0].endswith("] fired kind=react run=nod")
    assert lines[1].endswith("] dropped reason=cooldown")


def test_entry_line_keeps_the_plain_drop_shape(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=100.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, [Sense(speech_detected=True)] * 3, keep_active=False)
    entry = _sense_lines(caplog)[1]
    assert entry == "[SENSE stage=rule source=speech event=hear] dropped reason=cooldown"


# --------------------------------------------------------------------------- #
# Streak end — one summary naming the reason(s) and the length                #
# --------------------------------------------------------------------------- #


def test_streak_end_by_predicate_release_emits_one_summary(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=100.0)]})
    re_ = RuleEngine(cfg)
    senses = [Sense(speech_detected=True)] * 4 + [Sense(speech_detected=False)]
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, senses, keep_active=False)
    lines = _sense_lines(caplog)
    # fire, streak entry, then the release summary on the first non-matching tick
    assert len(lines) == 3
    assert "reason=cooldown suppressed 3 ticks" in lines[2]


def test_streak_end_by_refire_emits_summary_before_the_fire(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=1.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re_, [Sense(speech_detected=True)] * 5, keep_active=False)
    lines = _sense_lines(caplog)
    # dt=0.25, cd=1.0 -> fire t=0.25; gated ticks 2-4; refire t=1.25 closes the streak.
    assert len(ctx.admits) == 2  # the cooldown DECISION is unchanged (c46)
    assert len(lines) == 4
    assert lines[0].endswith("fired kind=react run=nod")
    assert lines[1].endswith("dropped reason=cooldown")
    assert "reason=cooldown suppressed 3 ticks" in lines[2]  # summary precedes...
    assert lines[3].endswith("fired kind=react run=nod")  # ...the fire that ends it


def test_an_unclosed_streak_at_run_end_emits_no_summary(caplog) -> None:
    """A streak still open when the loop stops has no release tick to log on —
    the entry line is the record of it."""
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=100.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, [Sense(speech_detected=True)] * 10, keep_active=False)
    lines = _sense_lines(caplog)
    assert len(lines) == 2  # fire + entry; no dangling summary
    assert not any("suppressed" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# Mid-streak reason change — one line per change, all reasons in the summary  #
# --------------------------------------------------------------------------- #


def test_mid_streak_reason_change_emits_one_line_per_change(caplog) -> None:
    # cd=1.0 with keep_active=True: gated ticks 2-4 are 'cooldown'; from tick 5
    # the cooldown has elapsed but the behavior is still active -> the reason
    # CHANGES to 'already-active' mid-streak. Exactly one line per transition.
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=1.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, [Sense(speech_detected=True)] * 8, keep_active=True)
    lines = _sense_lines(caplog)
    assert len(lines) == 3
    assert lines[0].endswith("fired kind=react run=nod")
    assert lines[1].endswith("dropped reason=cooldown")
    assert lines[2].endswith("dropped reason=already-active")


def test_summary_names_every_reason_in_the_episode(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=1.0)]})
    re_ = RuleEngine(cfg)
    senses = [Sense(speech_detected=True)] * 8 + [Sense(speech_detected=False)]
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, senses, keep_active=True)
    lines = _sense_lines(caplog)
    # ticks 2-8 were suppressed (3x cooldown then 4x already-active) = 7 ticks.
    assert len(lines) == 4
    assert "reason=cooldown,already-active suppressed 7 ticks" in lines[3]


# --------------------------------------------------------------------------- #
# Fires are untouched — every fire still logs (c46)                           #
# --------------------------------------------------------------------------- #


def test_fires_still_log_every_time(caplog) -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=0.0)]})
    re_ = RuleEngine(cfg)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re_, [Sense(speech_detected=True)] * 6, keep_active=False)
    lines = _sense_lines(caplog)
    assert len(ctx.admits) == 6
    assert len(lines) == 6
    assert all("fired" in ln for ln in lines)
    assert not any("dropped" in ln for ln in lines)


# --------------------------------------------------------------------------- #
# The inhibit path shares the episode cadence                                 #
# --------------------------------------------------------------------------- #


def test_inhibit_rule_gated_streak_emits_per_transition(caplog) -> None:
    cfg = RulesConfig.from_dict(
        {
            "inhibit": [
                {
                    "id": "hush",
                    "when": {"field": "pat", "op": "is_true"},
                    "disable": ["nod"],
                    "cooldown_s": 1.0,
                }
            ]
        }
    )
    re_ = RuleEngine(cfg)
    senses = [Sense(pat_event=("scratch", "level1"))] * 5
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        ctx = _drive_ticks(re_, senses, keep_active=False)
    lines = _sense_lines(caplog)
    # fire t=0.25, gated ticks 2-4 (one entry), summary + refire at t=1.25.
    assert ctx.evicts == ["nod", "nod"]  # eviction DECISIONS unchanged (c46)
    assert len(lines) == 4
    assert lines[1].endswith("dropped reason=cooldown")
    assert "reason=cooldown suppressed 3 ticks" in lines[2]
    assert lines[3].startswith("[SENSE stage=rule source=pat event=hush] fired")


# --------------------------------------------------------------------------- #
# The event stream follows the same cadence; summary carries ticks            #
# --------------------------------------------------------------------------- #


def test_suppress_events_follow_the_same_transition_cadence() -> None:
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=1.0)]})
    re_ = RuleEngine(cfg)
    ctx = _drive_ticks(re_, [Sense(speech_detected=True)] * 5, keep_active=False)
    fires = [e for e in ctx.events if e["type"] == R.EVENT_FIRE]
    suppresses = [e for e in ctx.events if e["type"] == R.EVENT_SUPPRESS]
    assert len(fires) == 2
    assert len(suppresses) == 2  # entry + summary, not one per gated tick
    assert suppresses[0]["reason"] == "cooldown"
    assert "ticks" not in suppresses[0]
    assert suppresses[1]["reason"] == "cooldown suppressed 3 ticks"
    assert suppresses[1]["ticks"] == 3


# --------------------------------------------------------------------------- #
# Grammar — a drop still always names its reason                              #
# --------------------------------------------------------------------------- #


def test_drop_lines_keep_the_sense_grammar(caplog) -> None:
    shape = re.compile(r"^\[SENSE stage=rule source=[\w-]+ event=[\w-]+\] dropped reason=\S.*$")
    cfg = RulesConfig.from_dict({"react": [_react("hear", cooldown_s=1.0)]})
    re_ = RuleEngine(cfg)
    senses = [Sense(speech_detected=True)] * 8 + [Sense(speech_detected=False)]
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _drive_ticks(re_, senses, keep_active=True)
    dropped = [ln for ln in _sense_lines(caplog) if "dropped" in ln]
    assert dropped, "the scenario must produce drop lines"
    for line in dropped:
        assert shape.match(line), line
