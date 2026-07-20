"""Boot resilience + ``behavior reload`` — t4 of the symbolic-runtime-70 rules plan.

Two acceptance criteria under test:

1. A deliberately broken ``rules.toml`` driven through the real ``behavior
   engine run`` composition (``reachy.cli._commands.behavior``) yields running
   base presence (``feel-alive``) plus a ``[SENSE]`` rejection naming every
   reason — the process keeps going (exit-0), no tick_seam is installed at all.
   Since t15 that "no tick_seam at all" outcome is the floor of LAST resort, not
   what a real robot does: the release now ships default rules, so a broken
   overlay degrades to THOSE. This module therefore blanks the shipped layer
   (see ``_no_shipped_rules``) to keep testing the nothing-left-to-run branch;
   the production branch lives in ``tests/test_behavior_default_rules.py``.
2. ``behavior reload`` (``reachy.behavior.reload_driver``) swaps the rules
   config at a deterministic between-ticks point; a rejected reload keeps the
   last-good config and reports the rejection; the CLI verb itself still obeys
   the standard clean-error contract.

No real robot, daemon, or background process: the engine runs against a fake
in-memory streaming sink with injectable ``sleep`` / ``now`` / ``max_ticks``,
exactly like ``tests/test_behavior.py`` and ``tests/test_behavior_rule_engine.py``.
"""

from __future__ import annotations

import contextlib
import json
import logging
from dataclasses import dataclass, field

import pytest

from reachy.behavior import reload_driver
from reachy.behavior import rules as rules_mod
from reachy.behavior.engine import Engine, EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.reload_driver import ReloadDriver
from reachy.behavior.rule_engine import STAGE as RULE_STAGE
from reachy.behavior.rules import RulesLoader
from reachy.behavior.sense import EMPTY_SENSE
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_cmd


@pytest.fixture(autouse=True)
def _no_shipped_rules(monkeypatch):
    """Blank the SHIPPED rules layer for this module.

    These tests exercise the box-local OVERLAY and the loader/CLI mechanics
    around it, not the product decision of what the release ships. Pinning them
    to whatever ``reachy/behavior/default_rules.toml`` happens to contain would
    churn them on every change to the shipped defaults while testing nothing
    about the mechanism. The real shipped content is asserted in
    ``tests/test_behavior_default_rules.py``; the two-layer merge itself in
    ``tests/test_behavior_rules_layering.py``.
    """
    monkeypatch.setattr(rules_mod, "shipped_rules_text", lambda: None)


SENSE_LOGGER = "reachy.sense"

# A predicate that is true on the very FIRST tick regardless of live sense data:
# ``absent_for`` seeds every SENSE_FIELDS' last-seen clock to the tick's own
# ``now`` before evaluating, so "absent for >= 0s" holds immediately. Lets these
# tests prove the rules seam is really wired into the engine composition without
# needing a live sense source (out of scope for this task).
GOOD1_TOML = """\
[[react]]
id = "r1"
when = { field = "doa", op = "absent_for", value = 0 }
run = "nod"
cooldown_s = 0
duration_s = 30.0
"""

GOOD2_TOML = """\
[[react]]
id = "r2"
when = { field = "rms", op = "absent_for", value = 0 }
run = "gaze-hold"
cooldown_s = 0
"""

# Deliberately broken: two unknown top-level fields in one file, so the single
# raised CliError message names BOTH reasons at once ("naming every reason").
BROKEN_TOML = """\
mystery = 1
another_bad = 2
"""


def _write_rules(text: str):
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def _sense_lines(caplog):
    return [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


# --------------------------------------------------------------------------- #
# Fakes (mirrors tests/test_behavior.py)                                      #
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


@dataclass
class _Ctx:
    """A minimal duck-typed TickContext for driving a ReloadDriver directly."""

    now: float = 0.0
    tick: int = 1
    sense: object = EMPTY_SENSE
    ownership: dict = field(default_factory=dict)
    admits: list = field(default_factory=list)
    evicts: list = field(default_factory=list)
    events: list = field(default_factory=list)
    _active: set = field(default_factory=set)

    def emit(self, event: dict) -> None:
        self.events.append(event)

    def admit(self, behavior) -> dict:
        self.admits.append(behavior)
        self._active.add(behavior.name)
        return {"ok": True, "op": "add", "id": behavior.id, "name": behavior.name}

    def evict(self, name: str) -> dict:
        self.evicts.append(name)
        self._active.discard(name)
        return {"ok": True, "op": "stop", "target": name}

    def active_names(self) -> set:
        return set(self._active)


# --------------------------------------------------------------------------- #
# reload_driver: the atomic-rename spool                                      #
# --------------------------------------------------------------------------- #


def test_submit_reload_writes_a_command_file() -> None:
    cmd_id = reload_driver.submit_reload()
    files = list((reload_driver.reload_dir() / "commands").glob("*.json"))
    assert len(files) == 1
    payload = json.loads(files[0].read_text(encoding="utf-8"))
    assert payload["cmd_id"] == cmd_id


def test_reload_spool_is_separate_from_the_engine_command_spool() -> None:
    """A reload command must never land where Engine.apply() would drain it."""
    from reachy.behavior import control

    reload_driver.submit_reload()
    assert control.CommandSpool().drain() == []  # nothing there for Engine.apply to choke on


def test_await_result_times_out_when_nothing_answers() -> None:
    cmd_id = reload_driver.submit_reload()
    assert reload_driver.await_result(cmd_id, timeout=0, sleep=lambda *_: None) is None


# --------------------------------------------------------------------------- #
# ReloadDriver: accept / reject semantics                                     #
# --------------------------------------------------------------------------- #


def test_driver_evaluates_rules_with_no_pending_reload() -> None:
    _write_rules(GOOD1_TOML)
    loader = RulesLoader()
    loader.reload()
    driver = ReloadDriver(loader)

    ctx = _Ctx(now=0.25, tick=1)
    driver(ctx)
    assert [b.name for b in ctx.admits] == ["nod"]


def test_accepted_reload_swaps_config_and_reports_ok(caplog) -> None:
    _write_rules(GOOD1_TOML)
    loader = RulesLoader()
    loader.reload()
    driver = ReloadDriver(loader)

    # One persistent ctx across ticks (like the real engine's active set, which
    # survives across TickContext instances) so "already-active" dedup behaves
    # exactly as it would in a real run.
    ctx = _Ctx(now=0.25, tick=1)
    driver(ctx)  # tick 1: GOOD1 fires, admits nod

    # edit the file + submit a reload command
    _write_rules(GOOD2_TOML)
    cmd_id = reload_driver.submit_reload()

    ctx.tick, ctx.now = 2, 0.5
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        driver(ctx)

    # the NEW config's rule (GOOD2 -> gaze-hold) fired on its first evaluation,
    # on top of the nod admitted under the old config on tick 1.
    assert [b.name for b in ctx.admits] == ["nod", "gaze-hold"]
    assert driver.loader.last_error is None
    assert driver.loader.current.react[0].id == "r2"

    lines = _sense_lines(caplog)
    assert any(
        ln.startswith(f"[SENSE stage={RULE_STAGE} source=rules") and "reload applied" in ln
        for ln in lines
    )

    result = reload_driver.await_result(cmd_id, timeout=1.0, sleep=lambda *_: None)
    assert result["ok"] is True
    assert result["react"] == 1


def test_rejected_reload_keeps_last_good_and_reports_rejection(caplog) -> None:
    _write_rules(GOOD1_TOML)
    loader = RulesLoader()
    loader.reload()
    driver = ReloadDriver(loader)

    ctx = _Ctx(now=0.25, tick=1)
    driver(ctx)  # GOOD1 active: nod admitted

    _write_rules(BROKEN_TOML)
    cmd_id = reload_driver.submit_reload()

    ctx.tick, ctx.now = 2, 0.5
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        driver(ctx)

    # last-good (GOOD1) config is still what's running -> its rule (already
    # active from tick 1) is now suppressed as already-active, NOT re-admitted,
    # and definitely not replaced by anything from the broken candidate.
    assert [b.name for b in ctx.admits] == ["nod"]  # unchanged since tick 1
    assert driver.loader.last_error is not None
    assert "mystery" in driver.loader.last_error
    assert "another_bad" in driver.loader.last_error
    assert driver.loader.current.react[0].id == "r1"  # unchanged: still GOOD1

    lines = _sense_lines(caplog)
    rejection = [ln for ln in lines if "dropped reason=" in ln and "source=rules" in ln]
    assert len(rejection) == 1
    assert "mystery" in rejection[0] and "another_bad" in rejection[0]

    result = reload_driver.await_result(cmd_id, timeout=1.0, sleep=lambda *_: None)
    assert result["ok"] is False
    assert "mystery" in result["error"] and "another_bad" in result["error"]


def test_reload_with_no_pending_command_is_a_pure_passthrough() -> None:
    _write_rules(GOOD1_TOML)
    loader = RulesLoader()
    loader.reload()
    driver = ReloadDriver(loader)
    driver(_Ctx(now=0.25, tick=1))
    engine_before = driver._engine  # identity check (test-only reach into internals)
    driver(_Ctx(now=0.5, tick=2))  # no reload submitted -> same RuleEngine instance
    assert driver._engine is engine_before


# --------------------------------------------------------------------------- #
# End-to-end: a real E.run() loop, reload applied at a deterministic tick     #
# --------------------------------------------------------------------------- #


def test_reload_swaps_config_mid_run_via_real_engine_loop() -> None:
    _write_rules(GOOD1_TOML)
    loader = RulesLoader()
    loader.reload()
    driver = ReloadDriver(loader)

    eng = Engine()
    tr = _FakeTransport()
    state = {"edited": False}

    def _sleep_seam(*_a, **_k):
        # Fires once between tick 1 and tick 2 (engine.run's injected sleep
        # seam) -- a deterministic "between ticks" point to drop a reload.
        if not state["edited"]:
            _write_rules(GOOD2_TOML)
            reload_driver.submit_reload()
            state["edited"] = True

    ticks = engine_run(
        tr,
        EngineConfig(compose_hz=50, base_layer=True, settle=False),
        sleep=_sleep_seam,
        now=_Clock(),
        max_ticks=2,
        engine=eng,
        tick_seam=driver,
    )
    assert ticks == 2
    names = {ab.behavior.name for ab in eng.active}
    assert "nod" in names  # admitted from GOOD1 on tick 1
    assert "gaze-hold" in names  # admitted from GOOD2 after the mid-run reload


# --------------------------------------------------------------------------- #
# Boot resilience: reachy.cli._commands.behavior._boot_tick_seam              #
# --------------------------------------------------------------------------- #
#
# NOTE the module-level ``_no_shipped_rules`` fixture: everything below runs
# with the SHIPPED layer blanked, so these cover the "there is genuinely
# nothing left to fall back to" branch — bare base presence as the floor of
# LAST resort. The complementary branch, which since t15 is the one a real
# robot takes (a malformed overlay degrading to the shipped rules rather than
# to nothing), is covered against the real package resource in
# ``tests/test_behavior_default_rules.py`` and with an injected shipped layer
# in ``tests/test_behavior_rules_layering.py``.


def test_boot_tick_seam_missing_rules_file_is_not_a_rejection(caplog) -> None:
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        seam = behavior_cmd._boot_tick_seam()
    assert isinstance(seam, ReloadDriver)
    assert seam.loader.current.react == ()
    assert seam.loader.last_error is None
    assert _sense_lines(caplog) == []  # "no rules yet" is not logged as a rejection


def test_boot_tick_seam_broken_rules_file_logs_and_returns_none(caplog) -> None:
    _write_rules(BROKEN_TOML)
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        seam = behavior_cmd._boot_tick_seam()
    assert seam is None
    lines = _sense_lines(caplog)
    rejection = [ln for ln in lines if ln.startswith(f"[SENSE stage={RULE_STAGE} source=rules")]
    assert len(rejection) == 1
    assert "event=boot" in rejection[0]
    assert "mystery" in rejection[0] and "another_bad" in rejection[0]


def test_boot_tick_seam_good_rules_file_is_installed() -> None:
    _write_rules(GOOD1_TOML)
    seam = behavior_cmd._boot_tick_seam()
    assert isinstance(seam, ReloadDriver)
    assert [r.id for r in seam.loader.current.react] == ["r1"]


# --------------------------------------------------------------------------- #
# CLI: behavior engine run with a broken rules file -- exit-0, base presence  #
# --------------------------------------------------------------------------- #


def test_engine_run_survives_a_broken_rules_file(monkeypatch, capsys, caplog) -> None:
    _write_rules(BROKEN_TOML)
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        rc = main(["behavior", "engine", "run", "--json", "--max-ticks", "3"])

    assert rc == 0  # exit-0: no crash, no exception ever raised out of the loop
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    assert [e["tick"] for e in events] == [1, 2, 3]
    # base presence: feel-alive still owns every channel every tick
    for e in events:
        assert e["ownership"]["head"].startswith("feel-alive")

    lines = _sense_lines(caplog)
    rejection = [ln for ln in lines if ln.startswith(f"[SENSE stage={RULE_STAGE} source=rules")]
    assert len(rejection) == 1
    assert "event=boot" in rejection[0]
    assert "mystery" in rejection[0] and "another_bad" in rejection[0]


def test_engine_run_with_good_rules_admits_via_the_seam(monkeypatch, capsys) -> None:
    _write_rules(GOOD1_TOML)
    tr = _FakeTransport()
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: tr)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    rc = main(["behavior", "engine", "run", "--json", "--max-ticks", "3"])
    assert rc == 0
    events = [json.loads(ln) for ln in capsys.readouterr().out.splitlines() if ln.strip()]
    # tick 1's emitted ownership reflects the state BEFORE tick 1's own seam call
    # (still feel-alive); by tick 3 the admit from tick 1 has landed.
    assert events[0]["ownership"]["head"].startswith("feel-alive")
    assert events[-1]["ownership"]["head"].startswith("rule:r1:")


# --------------------------------------------------------------------------- #
# CLI: behavior reload                                                        #
# --------------------------------------------------------------------------- #


def test_reload_verb_submits_and_reports(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.reload_driver.await_result",
        lambda cid, **k: {"ok": True, "cmd_id": cid, "path": "x", "react": 1, "inhibit": 0},
    )
    rc = main(["behavior", "reload", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True and payload["react"] == 1
    # cmd_reload() really called submit_reload() -- a command file landed in
    # the spool (await_result is faked above, so nothing drained it).
    files = list((reload_driver.reload_dir() / "commands").glob("*.json"))
    assert len(files) == 1


def test_reload_no_engine_reports_unconfirmed(capsys) -> None:
    rc = main(["behavior", "reload", "--await-timeout", "0"])
    assert rc == 0
    assert "did not confirm" in capsys.readouterr().out


def test_reload_bad_flag_is_a_clean_cli_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["behavior", "reload", "--bogus"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


def test_reload_overview_reachable_via_behavior_overview(capsys) -> None:
    assert main(["behavior", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = "\n".join(payload["sections"][0]["items"])
    assert "behavior reload" in verbs
