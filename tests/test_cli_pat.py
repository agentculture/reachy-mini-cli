"""Tests for the ``reachy pat`` noun (t4): run / demo / overview.

Covers the three acceptance criteria:

1. ``pat demo --json`` exits 0 with no robot attached and emits a structured
   reaction event (touch_type + enqueued actions).
2. ``pat overview --json`` lists the verbs.
3. The missing-SDK ``sdk run`` path exits 2 with ``error:`` / ``hint:`` lines in
   text mode, and structured JSON when ``--json``.

Tests drive the CLI through ``reachy.cli.main([...])`` exactly like the other
``test_cli_*`` suites, monkeypatching ``get_transport`` (or the SDK import) so no
real robot / ``[sdk]`` extra is required.
"""

from __future__ import annotations

import itertools
import json

import pytest

from reachy.cli import main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError

# --- overview -------------------------------------------------------------


def test_pat_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["pat", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "demo" in out
    assert "overview" in out


def test_pat_overview_json_lists_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["pat", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert "pat run" in blob
    assert "pat demo" in blob
    assert "pat overview" in blob


def test_pat_bare_shows_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["pat"])
    assert rc == 0
    assert "Verbs" in capsys.readouterr().out or "run" in capsys.readouterr().out


def test_pat_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["pat", "--help"])
    assert exc.value.code == 0


# --- demo (no robot) ------------------------------------------------------


def test_pat_demo_json_no_robot_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    """``pat demo --json`` runs with no robot and emits a structured reaction event."""
    rc = main(["pat", "demo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    # A structured reaction event: each reacted event carries a touch_type, a
    # level, and the enqueued action labels.
    reactions = payload["reactions"]
    assert isinstance(reactions, list) and len(reactions) >= 1
    first = reactions[0]
    assert first["touch_type"] in {"scratch", "side_pat"}
    assert first["level"] in {"level1", "level2"}
    assert isinstance(first["actions"], list) and len(first["actions"]) >= 1
    # The PatReaction enqueues a lean as the first action.
    assert any("lean" in label for label in first["actions"])


def test_pat_demo_text_no_robot_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["pat", "demo"])
    assert rc == 0
    # text mode: result to stdout, diagnostics to stderr — never a traceback.
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_pat_demo_custom_count(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["pat", "demo", "--json", "--count", "1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert len(payload["reactions"]) == 1


# --- run: missing-SDK path ------------------------------------------------


class _NoSdkTransport:
    """An sdk-flavor transport whose head_pose raises the missing-[sdk] CliError."""

    name = "sdk"

    def head_pose(self) -> tuple[float, float]:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the reachy_mini SDK is not installed",
            remediation="install the sdk extra: pip install 'reachy-mini-cli[sdk]'",
        )

    def move_goto(self, **kwargs: object) -> None:  # pragma: no cover - guard
        return None


def test_pat_run_sdk_missing_exits_2_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import reachy.cli._commands.pat as pat_mod

    monkeypatch.setattr(pat_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["pat", "run", "--transport", "sdk", "--ticks", "1"])
    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_pat_run_sdk_missing_exits_2_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import reachy.cli._commands.pat as pat_mod

    monkeypatch.setattr(pat_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["pat", "run", "--transport", "sdk", "--ticks", "1", "--json"])
    assert rc == EXIT_ENV_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == EXIT_ENV_ERROR
    assert "message" in payload and "remediation" in payload


# --- run: bounded loop with a fake transport ------------------------------


class _FakeTransport:
    """A fake head-pose source: returns a pat-shaped deviation so the detector fires."""

    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []

    def head_pose(self) -> tuple[float, float]:
        # actual pitch far below commanded (head pushed down) → a "press".
        return (-20.0, 0.0)

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_pat_run_bounded_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import reachy.cli._commands.pat as pat_mod

    fake = _FakeTransport()
    monkeypatch.setattr(pat_mod, "get_transport", lambda args: fake)
    rc = main(["pat", "run", "--transport", "sdk", "--ticks", "5", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    # The final JSON result line carries a status + tick count.
    last = [line for line in out.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["status"] == "ok"
    assert payload["ticks"] == 5


class _AlternatingTransport:
    """A fake transport that presses then releases on alternating ``head_pose``
    reads, so two press edges accumulate and the detector fires a pat."""

    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []
        self.pose_calls = 0

    def head_pose(self) -> tuple[float, float]:
        self.pose_calls += 1
        pressed = (self.pose_calls % 2) == 1  # press on the 1st, 3rd, … read
        return (-20.0 if pressed else 0.0, 0.0)

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_pat_run_reaction_window_holds_flag_and_pauses_sensing(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """Regression for PR #34 qodo bugs 6 & 7: once a pat fires, the pat-active
    signal stays up for the WHOLE reaction (not just the instantaneous enqueue),
    and the loop stops sensing while the robot executes its own lean — so the
    deliberate motion can never self-trigger a second pat."""
    import reachy.cli._commands.pat as pat_mod
    from reachy.motion import pat_signal
    from reachy.motion.pat import PatDetector
    from reachy.motion.pat_reaction import PatReaction
    from reachy.motion.queue import MotionQueue

    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr(pat_mod.time, "sleep", lambda *_a, **_k: None)  # no real waits

    fake = _AlternatingTransport()
    # Fire on two presses with no inter-pat cooldown so the event lands early and
    # the reaction window (~3.5 s) is observed across the remaining ticks.
    detector = PatDetector(min_presses=2, pat_cooldown=0.0)
    reaction = PatReaction(queue=MotionQueue())

    # A clock advancing 0.5 s/tick: the reaction window spans several ticks.
    # Record the pat-active flag at the top of each tick.
    state = {"i": 0, "active": []}

    def clock() -> float:
        state["active"].append(pat_signal.is_active())
        t = 0.5 * state["i"]
        state["i"] += 1
        return t

    ticks, events = pat_mod._proprioceptive_loop(
        transport=fake,
        detector=detector,
        reaction=reaction,
        commanded_pitch=0.0,
        commanded_yaw=0.0,
        max_ticks=9,
        clock=clock,
    )

    active = state["active"]
    # Exactly one pat despite presses continuing — the window suppresses re-fire.
    assert events == 1
    # The flag is held up across a contiguous run of >= 2 in-window ticks
    # (bug 6: not cleared the instant react() enqueues).
    longest_run = max(
        (len(list(g)) for k, g in itertools.groupby(active) if k),
        default=0,
    )
    assert longest_run >= 2
    assert active[0] is False
    # Sensing is paused during the window: head_pose is NOT read every tick
    # (bug 7: the robot's own lean is never fed back to the detector).
    assert fake.pose_calls < ticks
    # The flag never leaks past the loop.
    assert pat_signal.is_active() is False
