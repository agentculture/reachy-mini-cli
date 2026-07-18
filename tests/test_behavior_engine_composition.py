"""Live-composition tests for ``behavior engine run`` — the gaps t13's audit found.

The task-level tests proved each piece (rules, intents, sense, metrics, export)
against a hand-built seam; these prove the CLI composition in
``reachy/cli/_commands/behavior.py`` actually wires them together:

1. a live sense source (the transport's DoA route) feeds the rules, so a
   ``speech is_true`` rule fires in a real ``behavior engine run``;
2. the IntentDriver drains the intents spool inside the run, so a submitted
   intent resolves ``ok=true`` (no timeout);
3. ``install_logging`` runs, so ``[SENSE stage=rule]`` lines reach stderr;
4. the intent/goto emit names map into the runtime feed's block types.
"""

from __future__ import annotations

import contextlib
import json
import logging

import pytest

from reachy.behavior import control
from reachy.cli import main
from reachy.export.runtime import to_runtime_event

pytestmark = pytest.mark.offline


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _DoaTransport:
    """A fake transport whose DoA route always hears speech from the left."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return {"angle": 0.2, "speech_detected": True}


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setattr("reachy.cli._commands.behavior.get_transport", lambda args: _DoaTransport())
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _write_rules(tmp_path, text: str) -> None:
    d = tmp_path / "behavior"
    d.mkdir(parents=True, exist_ok=True)
    (d / "rules.toml").write_text(text, encoding="utf-8")


SPEECH_RULE = """
[[react]]
id = "hear-speech"
when = { field = "speech", op = "is_true" }
run = "nod"
cooldown_s = 0.0
"""


def test_speech_rule_fires_through_the_cli_composition(_isolated, capsys, caplog) -> None:
    _write_rules(_isolated, SPEECH_RULE)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--json"])
    assert rc == 0
    fired = [r.message for r in caplog.records if "hear-speech" in r.message]
    assert fired, "the speech rule never fired — live sense is not reaching the rules"


def test_intent_spool_resolves_ok_inside_the_run(_isolated) -> None:
    from reachy.behavior.intents import INTENT_NAMESPACE

    # nod is a looping-default entry (looping=True, duration=None) — an explicit
    # bounded duration is required so run_behavior doesn't refuse an unbounded
    # admission (see reachy/behavior/intents.py _validated_lifetime).
    cmd_id = control.submit(
        "run_behavior",
        namespace=INTENT_NAMESPACE,
        name="nod",
        lifetime={"duration": 5},
    )
    rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--json"])
    assert rc == 0
    result = control.await_result(cmd_id, namespace=INTENT_NAMESPACE, timeout=0.0)
    assert (
        result is not None and result.get("ok") is True
    ), "intent did not resolve ok=true — IntentDriver is not composed into the run"


def test_sense_logging_handler_installed_by_engine_run(_isolated) -> None:
    _write_rules(_isolated, SPEECH_RULE)
    rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--json"])
    assert rc == 0
    handlers = logging.getLogger("reachy").handlers
    assert handlers, "no handler on the reachy logger — install_logging is not wired"


def test_export_carries_rule_blocks_from_the_live_composition(_isolated, capsys) -> None:
    _write_rules(_isolated, SPEECH_RULE)
    rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--export", "-"])
    assert rc == 0
    out = capsys.readouterr().out
    types = {json.loads(line)["t"] for line in out.splitlines() if line.strip()}
    assert "rule" in types and types <= {"sense", "rule", "intent", "motion"}


def test_intent_status_emits_map_into_the_feed() -> None:
    ev = to_runtime_event(
        {"type": "intent.applied", "kind": "run_behavior", "cmd_id": "c1", "ts": 1.0, "tick": 3}
    )
    assert ev is not None and ev.action == "applied" and ev.name == "run_behavior"
    blocked = to_runtime_event(
        {"type": "intent.blocked", "kind": "set_mode", "reason": "unknown mode", "ts": 1.0}
    )
    assert blocked is not None and blocked.action == "blocked"


def test_goto_lifecycle_emits_map_into_the_feed() -> None:
    for phase in ("admitted", "done", "cancelled"):
        ev = to_runtime_event(
            {"type": f"goto.{phase}", "id": "g1", "label": "look-left", "ts": 2.0, "tick": 5}
        )
        assert ev is not None, f"goto.{phase} did not map"
        assert ev.action == "goto" and ev.detail["phase"] == phase
