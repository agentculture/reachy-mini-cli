"""t5 — the runtime answers to the names the LIVE loader holds (#177).

Tasks t1-t4 made ``names`` a validated, extend-only rules table
(:data:`reachy.behavior.rules.RulesConfig.names`), taught
:class:`~reachy.behavior.transcript_sense.TranscriptSenseDriver` a
``names_provider=`` seam resolved per utterance, and gave the runtime a
``name_mentioned`` sense field. None of that reaches the deployed robot until
COMPOSITION binds the provider to the loader the ``ReloadDriver`` keeps live —
which is what this file pins, and pins where it can actually go wrong:

1. **The binding is to the LIVE loader, never a snapshot.** ``names_provider``
   is a closure over ``rules_driver.loader``, so a ``behavior reload`` of an
   edited ``names`` table changes who the robot answers to BETWEEN TICKS with
   no driver rebuild. A ``names=`` snapshot (or a provider capturing
   ``loader.current`` at composition) would look identical on day one and then
   quietly ignore every later reload — the exact defect class the seam exists
   to prevent.
2. **``name_mentioned`` is the DRIVER's peek**, not a second latch: the
   provider wired onto :class:`~reachy.behavior.sense.SenseProviders` is
   ``transcript_driver.peek_name_mentioned`` itself.
3. **An operator can SEE the names.** ``behavior rules list`` and ``rules
   check`` report the merged tuple from the file on disk; ``behavior engine
   status`` reports what the RUNNING engine actually answers to, read off the
   ``names`` key the runtime publishes onto ``state.json`` — and it says which
   of the two it read, because "the file says X" and "the robot answers to X"
   are different questions the moment a reload has not been pushed.

``nova`` appears here only as a CONFIGURED value in a test overlay — the robot
learns no peer's name from this repo.
"""

from __future__ import annotations

import json

import pytest

from reachy.behavior import control
from reachy.behavior import rules as rules_mod
from reachy.behavior.engine import EngineConfig
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_mod

CONFIGURED = "nova"

#: The dedicated senselog logger (mirrors ``tests/test_behavior_reload.py``).
SENSE_LOGGER = "reachy.sense"


# --------------------------------------------------------------------------- #
# Fixtures / fakes                                                            #
# --------------------------------------------------------------------------- #


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    return tmp_path


def _write_overlay(*names: str) -> None:
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    body = "" if not names else "names = [" + ", ".join(repr(n) for n in names) + "]\n"
    path.write_text(body, encoding="utf-8")


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A mic-less, headless transport stand-in (mirrors the realtime-composition file)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    def streaming(self):
        from contextlib import nullcontext

        return nullcontext(self.sink)

    def doa(self, timeout=None):
        return None


class _FakeMedia:
    def __init__(self) -> None:
        self.samplerate = 16000
        self.channels = 1
        self.camera_available = False
        self.connected = False

    def warm_up(self) -> bool:
        self.connected = True
        return True

    def audio(self):
        return None

    def frame(self):
        return None

    def close(self) -> None:
        pass


class _QuietSession:
    """A ``RealtimeTranscriber`` stand-in that never hears anything."""

    def __init__(self, sample_rate: int = 16000) -> None:
        self.sample_rate = sample_rate

    def start(self) -> None:
        pass

    def close(self) -> None:
        pass

    def submit_audio(self, audio) -> bool:
        return True

    def take_utterance(self):
        return None

    def set_sample_rate(self, rate: int) -> None:
        self.sample_rate = int(rate)


def _compose(monkeypatch, rules_driver):
    """Run the real ``_compose_run_seam`` with fake hardware; return its triple."""
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setattr(behavior_mod, "_make_media_client", _FakeMedia)
    monkeypatch.setattr(behavior_mod, "_make_realtime_client", lambda rate: _QuietSession(rate))
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    return behavior_mod._compose_run_seam(_QuietTransport(), config, rules_driver, None)


# --------------------------------------------------------------------------- #
# 1 — the transcript driver is bound to the LIVE loader                       #
# --------------------------------------------------------------------------- #


def test_composition_binds_the_transcript_driver_to_the_live_loader(monkeypatch) -> None:
    """A reload changes who the SAME driver answers to — no rebuild."""
    _write_overlay(CONFIGURED)
    rules_driver = behavior_mod._boot_tick_seam()
    assert rules_driver is not None

    seen: dict = {}
    real = behavior_mod.TranscriptSenseDriver

    def _capture(**kwargs):
        driver = real(**kwargs)
        seen["driver"] = driver
        seen["kwargs"] = kwargs
        return driver

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", _capture)
    _sense_reader, _metrics, resources = _compose(monkeypatch, rules_driver)
    try:
        provider = seen["kwargs"]["names_provider"]
        assert provider is not None, "composition passed no names_provider"
        assert tuple(provider()) == (*rules_mod.SHIPPED_NAMES, CONFIGURED)

        driver_before = seen["driver"]
        # An operator edits the table and pushes a reload. The DRIVER is not
        # rebuilt — only the loader's `current` moves.
        _write_overlay("mimi")
        rules_driver.loader.reload()
        assert rules_driver.loader.last_error is None
        assert tuple(provider()) == (*rules_mod.SHIPPED_NAMES, "mimi")
        assert seen["driver"] is driver_before, "the driver was rebuilt — the seam is a snapshot"
    finally:
        resources.close()


def test_the_shipped_fallback_provider_is_the_shipped_pair(monkeypatch) -> None:
    """`_boot_tick_seam` returns None on a box with nothing left to run; the
    robot must still know its own shipped name — nameless is never legal."""
    seen: dict = {}
    real = behavior_mod.TranscriptSenseDriver

    def _capture(**kwargs):
        seen["kwargs"] = kwargs
        return real(**kwargs)

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", _capture)
    _sense_reader, _metrics, resources = _compose(monkeypatch, None)
    try:
        assert tuple(seen["kwargs"]["names_provider"]()) == rules_mod.SHIPPED_NAMES
    finally:
        resources.close()


# --------------------------------------------------------------------------- #
# 2 — name_mentioned is the driver's own peek                                 #
# --------------------------------------------------------------------------- #


def test_name_mentioned_provider_is_the_transcript_drivers_peek(monkeypatch) -> None:
    seen: dict = {}
    real_driver = behavior_mod.TranscriptSenseDriver
    real_providers = behavior_mod.SenseProviders

    def _capture_driver(**kwargs):
        driver = real_driver(**kwargs)
        seen["driver"] = driver
        return driver

    def _capture_providers(**kwargs):
        seen["providers"] = kwargs
        return real_providers(**kwargs)

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", _capture_driver)
    monkeypatch.setattr(behavior_mod, "SenseProviders", _capture_providers)
    _sense_reader, _metrics, resources = _compose(monkeypatch, None)
    try:
        provider = seen["providers"].get("name_mentioned")
        assert provider is not None, "SenseProviders was built with no name_mentioned"
        assert provider == seen["driver"].peek_name_mentioned
    finally:
        resources.close()


# --------------------------------------------------------------------------- #
# 3 — the operator surfaces                                                   #
# --------------------------------------------------------------------------- #


def test_rules_list_json_carries_the_merged_names(capsys) -> None:
    _write_overlay(CONFIGURED)
    assert main(["behavior", "rules", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == [*rules_mod.SHIPPED_NAMES, CONFIGURED]


def test_rules_list_text_mode_shows_the_names(capsys) -> None:
    _write_overlay(CONFIGURED)
    assert main(["behavior", "rules", "list"]) == 0
    out = capsys.readouterr().out
    assert "names" in out
    assert CONFIGURED in out


def test_rules_list_with_no_overlay_still_reports_the_shipped_names(capsys) -> None:
    assert main(["behavior", "rules", "list", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == list(rules_mod.SHIPPED_NAMES)


def test_rules_check_summary_names_the_names_in_force(capsys) -> None:
    _write_overlay(CONFIGURED)
    assert main(["behavior", "rules", "check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == [*rules_mod.SHIPPED_NAMES, CONFIGURED]
    assert CONFIGURED in payload["summary"]
    for name in rules_mod.SHIPPED_NAMES:
        assert name in payload["summary"]


def test_rules_check_text_mode_surfaces_the_summary(capsys) -> None:
    _write_overlay(CONFIGURED)
    assert main(["behavior", "rules", "check"]) == 0
    assert CONFIGURED in capsys.readouterr().out


def test_rules_check_on_a_malformed_file_still_reports_the_names_in_force(capsys) -> None:
    """A rejected overlay leaves the SHIPPED names in force — say so, don't go quiet."""
    path = rules_mod.default_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("mystery = 1\n", encoding="utf-8")
    assert main(["behavior", "rules", "check", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is False
    assert payload["names"] == list(rules_mod.SHIPPED_NAMES)


# --------------------------------------------------------------------------- #
# 3b — engine status reads the RUNNING engine's names                         #
# --------------------------------------------------------------------------- #


def _tick_names_publisher(rules_driver):
    """Compose the state rider the runtime composes, and tick it once."""
    rider = behavior_mod._NamesPublisher(lambda: rules_driver.loader.current.names)
    rider(None)
    return rider


def _engine_process_is_alive(monkeypatch) -> None:
    """Make the supervisor report a LIVE engine process (no real process needed)."""
    monkeypatch.setattr("reachy.behavior.supervisor.read_pid", lambda: 4242)
    monkeypatch.setattr("reachy.behavior.supervisor.is_alive", lambda pid: True)


def test_engine_status_json_reports_the_running_engines_names(monkeypatch, capsys) -> None:
    _write_overlay(CONFIGURED)
    rules_driver = behavior_mod._boot_tick_seam()
    rider = _tick_names_publisher(rules_driver)

    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    _engine_process_is_alive(monkeypatch)
    assert main(["behavior", "engine", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == [*rules_mod.SHIPPED_NAMES, CONFIGURED]
    assert payload["names_source"] == behavior_mod.NAMES_FROM_ENGINE

    # ... and a reload of an edited table moves it, with no restart.
    _write_overlay("mimi")
    rules_driver.loader.reload()
    rider(None)
    assert main(["behavior", "engine", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == [*rules_mod.SHIPPED_NAMES, "mimi"]


def test_a_stopped_engines_leftover_state_is_not_reported_as_live_names(
    monkeypatch, capsys
) -> None:
    """state.json outlives a stopped/crashed engine until the next run resets the
    spool — a published list is proof of nothing unless the process is alive."""
    _write_overlay(CONFIGURED)
    rules_driver = behavior_mod._boot_tick_seam()
    _tick_names_publisher(rules_driver)(None)  # publishes names into state.json
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    # No pid file / dead pid: the supervisor says "stopped" — the default here.
    assert main(["behavior", "engine", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] != "running"
    assert payload["names_source"] == behavior_mod.NAMES_FROM_DISK


def test_the_engagement_classifier_is_built_on_the_live_names_provider(monkeypatch) -> None:
    """Review finding: the fast-path used live names but the classifier's prompt
    listed only the shipped pair, so nameless follow-ups were judged blind."""
    monkeypatch.delenv("REACHY_ENGAGE_HEURISTIC", raising=False)
    names = [*rules_mod.SHIPPED_NAMES]
    classifier = behavior_mod._engagement_classifier(names=lambda: tuple(names))
    assert classifier is not None
    assert CONFIGURED not in classifier.system_prompt
    names.append(CONFIGURED)
    assert CONFIGURED in classifier.system_prompt  # re-rendered from the live provider


def test_engine_status_falls_back_to_the_file_and_says_so(monkeypatch, capsys) -> None:
    """No live engine publishing names: report the DISK loader, labelled."""
    _write_overlay(CONFIGURED)
    monkeypatch.setattr("reachy.behavior.supervisor.health_ok", lambda *a, **k: True)
    assert main(["behavior", "engine", "status", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["names"] == [*rules_mod.SHIPPED_NAMES, CONFIGURED]
    assert payload["names_source"] == behavior_mod.NAMES_FROM_DISK

    assert main(["behavior", "engine", "status"]) == 0
    out = capsys.readouterr().out
    assert behavior_mod.NAMES_FROM_DISK in out, "the text output must say the engine wasn't asked"


def test_composition_logs_exactly_one_sense_line_naming_the_names(monkeypatch, caplog) -> None:
    """The journal says who the robot answers to from the first line of a run —
    once, not once per tick, and not on the ``source=rules`` channel a reload
    already owns."""
    import logging

    _write_overlay(CONFIGURED)
    rules_driver = behavior_mod._boot_tick_seam()
    # `install_logging` is not in play here, so the sense logger still
    # propagates to caplog's root handler — no second handler to attach (which
    # would double-count every record and hide exactly the repetition this test
    # is about).
    with caplog.at_level(logging.INFO, logger=SENSE_LOGGER):
        _sense_reader, _metrics, resources = _compose(monkeypatch, rules_driver)
        resources.close()

    lines = [r.getMessage() for r in caplog.records if r.name == SENSE_LOGGER]
    named = [ln for ln in lines if f"source={behavior_mod.NAMES_STATE_KEY}" in ln]
    assert len(named) == 1, named
    assert CONFIGURED in named[0]
    # The reload/boot vocabulary is untouched — an operator's `source=rules`
    # grep must not start matching a per-run names line.
    assert f"source={behavior_mod.NAMES_STATE_KEY}" not in "".join(
        ln for ln in lines if "source=rules" in ln
    )


def test_the_names_publisher_never_clobbers_a_sibling_state_key() -> None:
    """It read-modify-writes, exactly as the availability/intents riders do."""
    spool = control.CommandSpool()
    spool.write_state({"active": [], "intents": {"mode": "calm"}})
    rider = behavior_mod._NamesPublisher(lambda: ("reachy", CONFIGURED), main_control=spool)
    rider(None)
    state = spool.read_state()
    assert state["names"] == ["reachy", CONFIGURED]
    assert state["intents"] == {"mode": "calm"}


def test_the_names_publisher_never_raises_out_of_a_tick() -> None:
    """A sense tap must never crash the loop (the availability rider's contract)."""

    def _boom():
        raise RuntimeError("no loader")

    behavior_mod._NamesPublisher(_boom)(None)  # must not raise
