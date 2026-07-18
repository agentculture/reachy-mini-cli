"""``behavior goto`` — the CLI operator surface for the live goto path (task t8).

``behavior goto`` submits a GOTO command into the SAME namespaced intents spool
(``reachy.behavior.control``, namespace ``reachy.behavior.intents.INTENT_NAMESPACE``)
a live tool-use agent's ``run_behavior``/``declare_goal``/... tools write into
(``reachy.speech.intent_tools._submit_and_await``) — this verb exercises exactly
that submission path, nothing bespoke. Validation is layered:

* "no channel at all" is checked CLI-side, synchronously, before any spool write
  (so it fails fast even with no engine running to hand back a confirmation);
* everything else (out-of-range axes, a runaway duration, an unknown field) is the
  KIND's job (``reachy.behavior.goto_intent.make_goto_handler``) — a LIVE
  confirmed rejection is surfaced here as a clean exit-1 CliError, never a
  silently-``ok:false`` JSON blob;
* no confirmation in time degrades to a ``submitted``/unconfirmed report (exit 0)
  — the command persists on disk for a later-started engine.

No real robot/daemon/SDK anywhere: spool tests touch only the filesystem under an
isolated ``REACHY_STATE_DIR``; the one engine-application test drives the real
``behavior engine run --max-ticks N`` loop against a fake transport, mirroring
``tests/test_behavior_runtime_composition.py``'s existing spool-goto pattern.
"""

from __future__ import annotations

import contextlib
import json

import pytest

from reachy.behavior import control
from reachy.behavior.intents import INTENT_NAMESPACE
from reachy.cli import main
from reachy.explain import known_paths


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)


def _submitted_commands() -> list[dict]:
    """Read (without draining) every command currently sitting in the intents spool."""
    return [
        json.loads(p.read_text(encoding="utf-8"))
        for p in sorted(control.commands_dir(INTENT_NAMESPACE).iterdir())
        if p.suffix == ".json"
    ]


# --------------------------------------------------------------------------- #
# Client-side validation — exit-1, error:/hint:, no traceback                 #
# --------------------------------------------------------------------------- #


def test_goto_no_channel_is_a_clean_user_error(capsys) -> None:
    rc = main(["behavior", "goto"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "at least one channel" in err
    # nothing was ever written to the spool — validated before submission
    assert _submitted_commands() == []


def test_goto_no_channel_is_a_clean_user_error_json(capsys) -> None:
    rc = main(["behavior", "goto", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert "at least one channel" in payload["message"]
    assert payload["remediation"]


def test_goto_bad_number_is_a_clean_user_error_no_traceback(capsys) -> None:
    # A non-numeric --yaw fails argparse's type=float parsing, which main()'s
    # _CliArgumentParser.error() already turns into the structured error:/hint:
    # contract (raised as SystemExit(1), not a Python traceback) — see
    # reachy/cli/__init__.py.
    with pytest.raises(SystemExit) as exc:
        main(["behavior", "goto", "--yaw", "not-a-number"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "Traceback" not in err
    assert _submitted_commands() == []


def test_goto_bad_duration_is_a_clean_user_error(capsys) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["behavior", "goto", "--yaw", "5", "--duration", "nope"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err


# --------------------------------------------------------------------------- #
# Submission — only the passed channels end up in the payload                #
# --------------------------------------------------------------------------- #


def test_goto_submits_only_the_passed_head_axis(capsys) -> None:
    rc = main(
        ["behavior", "goto", "--yaw", "10", "--duration", "2", "--await-timeout", "0", "--json"]
    )
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is None
    assert "submitted" in payload and payload["submitted"]

    commands = _submitted_commands()
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd["op"] == "goto"
    assert cmd["cmd_id"] == payload["submitted"]
    assert cmd["head"] == {"yaw": 10.0}
    assert cmd["duration"] == 2.0
    assert "antennas" not in cmd
    assert "body_yaw" not in cmd
    assert "label" not in cmd  # not passed -> kind applies its own default


def test_goto_submits_antennas_and_body_yaw(capsys) -> None:
    rc = main(
        [
            "behavior",
            "goto",
            "--antennas",
            "15",
            "-15",
            "--body-yaw",
            "8",
            "--label",
            "wave",
            "--await-timeout",
            "0",
        ]
    )
    assert rc == 0
    commands = _submitted_commands()
    assert len(commands) == 1
    cmd = commands[0]
    assert cmd["antennas"] == [15.0, -15.0]
    assert cmd["body_yaw"] == 8.0
    assert cmd["label"] == "wave"
    assert "head" not in cmd


def test_goto_default_duration_is_one_second() -> None:
    rc = main(["behavior", "goto", "--pitch", "3", "--await-timeout", "0", "--json"])
    assert rc == 0
    cmd = _submitted_commands()[0]
    assert cmd["duration"] == 1.0
    assert cmd["interpolation"] == "minjerk"


def test_goto_reports_unconfirmed_when_no_engine_is_running(capsys) -> None:
    rc = main(["behavior", "goto", "--x", "2", "--await-timeout", "0"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "submitted" in out


# --------------------------------------------------------------------------- #
# Confirmed results — success and a live rejection                            #
# --------------------------------------------------------------------------- #


def test_goto_reports_confirmed_admission(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {
            "ok": True,
            "op": "goto",
            "id": "goto-1",
            "label": "goto",
            "channels": ["head"],
            "duration": 2.0,
        },
    )
    rc = main(["behavior", "goto", "--yaw", "10", "--duration", "2", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["ok"] is True
    assert payload["id"] == "goto-1"
    assert payload["channels"] == ["head"]


def test_goto_confirmed_rejection_becomes_a_clean_user_error(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {
            "ok": False,
            "op": "goto",
            "error": "goto: head.yaw out of range: 999.0 (allowed [-20.0, 20.0] deg)",
        },
    )
    rc = main(["behavior", "goto", "--yaw", "999", "--duration", "2"])
    assert rc == 1
    err = capsys.readouterr().err
    assert err.startswith("error:") and "hint:" in err
    assert "out of range" in err


def test_goto_confirmed_rejection_becomes_a_clean_user_error_json(monkeypatch, capsys) -> None:
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.control.await_result",
        lambda cid, **k: {"ok": False, "op": "goto", "error": "goto: boom"},
    )
    rc = main(["behavior", "goto", "--yaw", "5", "--json"])
    assert rc == 1
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == 1
    assert "boom" in payload["message"]


# --------------------------------------------------------------------------- #
# End-to-end: a bounded foreground engine run applies the spooled goto        #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (mirrors the composition
    tests' fake — no robot, daemon, SDK, or network anywhere)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


def _blocks(out: str) -> list[dict]:
    return [json.loads(line) for line in out.splitlines() if line.strip()]


def test_goto_cli_submission_is_applied_by_a_bounded_engine_run(monkeypatch, capsys) -> None:
    """The full spool-to-engine path: ``behavior goto`` writes a command that a
    bounded foreground ``behavior engine run --max-ticks N`` then drains and
    admits — the exact composition ``tests/test_behavior_runtime_composition.py``
    already proves for a directly-submitted command, here reached through the
    CLI verb itself.
    """
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _QuietTransport()
    )

    rc = main(
        ["behavior", "goto", "--yaw", "10", "--duration", "5", "--await-timeout", "0", "--json"]
    )
    assert rc == 0
    submitted = json.loads(capsys.readouterr().out)
    assert submitted["ok"] is None  # no engine was running yet to confirm it

    rc = main(["behavior", "engine", "run", "--max-ticks", "6", "--export", "-"])
    assert rc == 0

    motion = [b for b in _blocks(capsys.readouterr().out) if b["t"] == "motion"]
    phases = {m["detail"].get("phase") for m in motion if m["action"] == "goto"}
    assert "admitted" in phases, f"goto not admitted through the CLI (motion blocks={motion})"


# --------------------------------------------------------------------------- #
# explain catalog + overview                                                  #
# --------------------------------------------------------------------------- #


def test_goto_catalog_path_is_registered() -> None:
    assert ("behavior", "goto") in known_paths()


def test_goto_catalog_entry_resolves(capsys) -> None:
    rc = main(["explain", "behavior", "goto"])
    assert rc == 0
    assert "goto" in capsys.readouterr().out.lower()


def test_behavior_overview_lists_goto(capsys) -> None:
    rc = main(["behavior", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    verbs = next(s["items"] for s in payload["sections"] if s["title"] == "Verbs")
    assert any(item.startswith("behavior goto") for item in verbs)
