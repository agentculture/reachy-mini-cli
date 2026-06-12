"""Tests for the ``reachy sleep`` noun (t8): run / start / stop / restart /
status / demo / overview.

Covers the acceptance criteria:

1. ``sleep demo --json`` (injected synthetic sense + a fake clock, no robot)
   walks ALERT -> DROWSY -> ASLEEP then back to ALERT on a wake event, observable
   in the structured output (c1/h14).
2. ``sleep status --json`` reports the process/daemon health; an arc unit test
   drives the full ALERT -> DROWSY -> ASLEEP -> wake using a fake clock + injected
   sense with zero robot and zero real wall-clock wait (c7/h15).
3. The noun follows the listen|think|pat scaffold (run / start / stop / restart /
   status / demo / overview, every verb ``--json``, the CliError two-line
   contract).

Tests drive the CLI through ``reachy.cli.main([...])`` exactly like the other
``test_cli_*`` / ``test_*_cli`` suites, monkeypatching ``get_transport`` (or the
SDK import) so no real robot / ``[sdk]`` extra is required. ``demo`` needs no
transport at all.
"""

from __future__ import annotations

import json

import pytest

from reachy.cli import main
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.sleep.state import SleepState

# --- overview -------------------------------------------------------------


def test_sleep_overview_text(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "overview"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "run" in out
    assert "demo" in out
    assert "overview" in out


def test_sleep_overview_json_lists_verbs(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "overview", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    blob = json.dumps(payload)
    assert "sleep run" in blob
    assert "sleep demo" in blob
    assert "sleep overview" in blob


def test_sleep_bare_shows_overview(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep"])
    assert rc == 0
    assert "Verbs" in capsys.readouterr().out


def test_sleep_help_exits_zero() -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sleep", "--help"])
    assert exc.value.code == 0


# --- demo (no robot) ------------------------------------------------------


def test_sleep_demo_json_walks_full_arc(capsys: pytest.CaptureFixture[str]) -> None:
    """``sleep demo --json`` walks ALERT -> DROWSY -> ASLEEP then back to ALERT
    on a wake event, all observable in the structured output. No robot, no SDK."""
    rc = main(["sleep", "demo", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["status"] == "ok"
    states = payload["states"]
    assert isinstance(states, list) and len(states) >= 4
    # The arc must contain each level in order, and end back at ALERT after wake.
    assert states[0] == "ALERT"
    assert "DROWSY" in states
    assert "ASLEEP" in states
    # ASLEEP must come before the final ALERT (the wake).
    assert states.index("ASLEEP") < (len(states) - 1)
    assert states[-1] == "ALERT"
    # The wake event is reported explicitly.
    assert payload["woke"] is True


def test_sleep_demo_text_no_robot_exits_zero(capsys: pytest.CaptureFixture[str]) -> None:
    rc = main(["sleep", "demo"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "Traceback" not in err


def test_sleep_demo_does_not_need_reachy_mini(monkeypatch: pytest.MonkeyPatch) -> None:
    """Importing the noun + running demo must not require the reachy_mini SDK."""
    import builtins

    real_import = builtins.__import__

    def _guard(name: str, *args, **kwargs):
        if name == "reachy_mini" or name.startswith("reachy_mini."):
            raise ImportError("reachy_mini is not installed (simulated)")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", _guard)
    rc = main(["sleep", "demo", "--json"])
    assert rc == 0


# --- status ---------------------------------------------------------------


def test_sleep_status_json_shape(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``sleep status --json`` reports a process state + the cross-process sleep
    state; ``idle_seconds`` is ``null`` (the live timer is not readable across
    processes)."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    rc = main(["sleep", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    # Process health (delegated to the supervisor).
    assert payload["process"] in {"running", "stopped", "stale"}
    # Cross-process sleep state (from the flag) is reported.
    assert payload["state"] in {"ALERT", "DROWSY", "ASLEEP"}
    # idle_seconds is present but null — the live timer lives in the loop process.
    assert "idle_seconds" in payload
    assert payload["idle_seconds"] is None


def test_sleep_status_reports_asleep_when_flag_set(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """When the sleep-active flag is present, status reports the ASLEEP state."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    from reachy.motion import sleep_signal

    sleep_signal.write()
    try:
        rc = main(["sleep", "status", "--json"])
        assert rc == 0
        payload = json.loads(capsys.readouterr().out)
        assert payload["state"] == "ASLEEP"
    finally:
        sleep_signal.clear()


# --- run: missing-SDK path ------------------------------------------------


class _NoSdkTransport:
    """An sdk-flavor transport whose media_session raises the missing-[sdk] error."""

    name = "sdk"

    def media_session(self) -> object:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="the reachy_mini SDK is not installed",
            remediation="install the sdk extra: pip install 'reachy-mini-cli[sdk]'",
        )

    def move_goto(self, **kwargs: object) -> None:  # pragma: no cover - guard
        return None


def test_sleep_run_sdk_missing_exits_2_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "1"])
    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_sleep_run_sdk_missing_exits_2_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _NoSdkTransport())
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "1", "--json"])
    assert rc == EXIT_ENV_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == EXIT_ENV_ERROR
    assert "message" in payload and "remediation" in payload


def test_sleep_unknown_verb_exits_one(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as exc:
        main(["sleep", "bogus-verb"])
    assert exc.value.code == 1
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


# --- run: bounded loop with a fake transport ------------------------------


class _SilentSession:
    """A media session (context manager) that yields no DoA and no audio — so the
    sleep loop senses 'nothing' every tick and the idle clock runs uninterrupted."""

    def __enter__(self) -> "_SilentSession":
        return self

    def __exit__(self, *exc: object) -> bool:
        return False

    def doa(self, *, timeout: float | None = None) -> object:
        return None  # no reading → EMPTY_SENSE

    def get_audio_sample(self) -> object:
        return None


class _FakeSdkTransport:
    """An sdk-flavor transport with a silent media session and a goto sink."""

    name = "sdk"

    def __init__(self) -> None:
        self.gotos: list[dict] = []

    def media_session(self) -> _SilentSession:
        return _SilentSession()

    def move_goto(self, **kwargs: object) -> None:
        self.gotos.append(kwargs)


def test_sleep_run_bounded_exits_zero(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    fake = _FakeSdkTransport()
    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: fake)
    monkeypatch.setattr(sleep_mod.time, "sleep", lambda *_a, **_k: None)
    rc = main(["sleep", "run", "--transport", "sdk", "--ticks", "5", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    last = [line for line in out.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["status"] == "ok"
    assert payload["ticks"] == 5


# --- arc unit test (fake clock + injected sense, zero robot/wall-clock) ----


def test_sleep_arc_alert_drowsy_asleep_wake_no_wallclock() -> None:
    """Drive the decay->sleep->wake arc with an injected fake clock + injected
    synthetic sense feed and a bounded tick count, asserting the observed state
    sequence — with zero robot and zero real wall-clock wait."""
    from reachy.cli._commands.sleep import run_sleep_arc
    from reachy.motion.queue import MotionQueue

    # Fake clock: jumps forward by a big step each tick so a tiny idle-timeout
    # drives ALERT -> DROWSY -> ASLEEP across just a few ticks.
    clock_state = {"t": 0.0}

    def fake_now() -> float:
        return clock_state["t"]

    # A sense feed: returns 'no stimulus' until the final tick, then a wake event.
    from reachy.behavior.sense import EMPTY_SENSE, Sense

    senses = [EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, Sense(speech_detected=True)]
    feed_state = {"i": 0}

    def fake_sense() -> Sense:
        s = senses[min(feed_state["i"], len(senses) - 1)]
        return s

    # Advance the clock between ticks via on_tick: 10 s per tick.
    def advance() -> None:
        feed_state["i"] += 1
        clock_state["t"] += 10.0

    queue = MotionQueue()
    result = run_sleep_arc(
        queue=queue,
        now=fake_now,
        sense=fake_sense,
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,  # drowsy_after=7.5, asleep_after=15 → asleep by tick ~2-3
    )
    states = result["states"]
    assert states[0] == SleepState.ALERT.name
    assert SleepState.DROWSY.name in states
    assert SleepState.ASLEEP.name in states
    # The final tick had a speech stimulus → wake → back to ALERT.
    assert states[-1] == SleepState.ALERT.name
    assert result["woke"] is True


# --- t4: audio-wake toggle (pat-only / quiet-room) ------------------------


def _idle_then(events, *, ticks):
    """Build a (now, sense, advance) seam: silent until the last tick, then the
    provided final ``Sense``.  The clock jumps 10s/tick so a small idle-timeout
    walks the machine to ASLEEP within a handful of ticks."""
    from reachy.behavior.sense import EMPTY_SENSE

    clock = {"t": 0.0}
    feed = {"i": 0}

    def now() -> float:
        return clock["t"]

    def sense():
        return events[min(feed["i"], len(events) - 1)]

    def advance() -> None:
        feed["i"] += 1
        clock["t"] += 10.0

    _ = ticks  # documentation only — caller passes ticks to run_sleep_arc
    _ = EMPTY_SENSE
    return now, sense, advance


def test_sleep_arc_pat_only_ignores_speech_and_snap() -> None:
    """``run_sleep_arc(audio_wake=False)`` is pat-only: an injected speech flag
    AND an injected snap both stay ASLEEP (audio ignored) — only a pat wakes."""
    from reachy.behavior.sense import EMPTY_SENSE, Sense
    from reachy.cli._commands.sleep import run_sleep_arc
    from reachy.motion.queue import MotionQueue

    # Every tick carries an active speech flag; a snap source fires every tick too.
    senses = [Sense(speech_detected=True)] * 5
    now, sense, advance = _idle_then(senses, ticks=5)
    _ = EMPTY_SENSE

    result = run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        snap=lambda: True,  # loud transient every tick
        pat=lambda: False,  # no touch
        audio_wake=False,
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,
    )
    # Audio is ignored: speech + snap never wake it; it decays to ASLEEP and stays.
    assert SleepState.ASLEEP.name in result["states"]
    assert result["states"][-1] != SleepState.ALERT.name
    assert result["woke"] is False


def test_sleep_arc_pat_only_wakes_on_pat() -> None:
    """``run_sleep_arc(audio_wake=False)`` wakes when the injected ``pat`` source
    fires on the final tick — pat is the only path."""
    from reachy.behavior.sense import EMPTY_SENSE
    from reachy.cli._commands.sleep import run_sleep_arc
    from reachy.motion.queue import MotionQueue

    senses = [EMPTY_SENSE] * 5
    now, sense, advance = _idle_then(senses, ticks=5)
    pat_state = {"i": 0}

    def pat_source() -> bool:
        pat_state["i"] += 1
        return pat_state["i"] >= 5  # fire on the 5th poll (the last tick)

    result = run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        snap=lambda: False,
        pat=pat_source,
        audio_wake=False,
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,
    )
    assert SleepState.ASLEEP.name in result["states"]
    assert result["states"][-1] == SleepState.ALERT.name
    assert result["woke"] is True


def test_sleep_arc_feeds_real_audio_to_wake_word() -> None:
    """run_sleep_arc forwards the audio() chunk (not a silent buffer) to the
    wake-word backend — regression for the silent-audio bug (Qodo #1, PR #37)."""
    import numpy as np

    from reachy.behavior.sense import EMPTY_SENSE
    from reachy.cli._commands.sleep import WakeWord, run_sleep_arc
    from reachy.motion.queue import MotionQueue

    received: list = []

    class _RecordingDetector:
        def update(self, sense, audio):
            received.append(audio)
            return False

        def reset(self) -> None:
            return None

    real_chunk = np.full(64, 0.5, dtype=np.float32)
    senses = [EMPTY_SENSE] * 3
    now, sense, advance = _idle_then(senses, ticks=3)

    run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        snap=lambda: False,
        audio_wake=True,
        wake_word=WakeWord(factory=lambda: _RecordingDetector(), audio=lambda: real_chunk),
        on_tick=advance,
        ticks=3,
        idle_timeout=15.0,
    )
    assert received, "wake-word backend must be consulted when audio_wake is on"
    # It got the REAL chunk, not a zero/silent buffer.
    assert any(np.array_equal(a, real_chunk) for a in received)
    assert all(float(np.max(np.abs(a))) > 0.0 for a in received)


def test_sleep_arc_default_keeps_audio_wake() -> None:
    """The default (``audio_wake`` omitted) keeps audio wake: a speech flag on the
    final tick wakes it."""
    from reachy.behavior.sense import EMPTY_SENSE, Sense
    from reachy.cli._commands.sleep import run_sleep_arc
    from reachy.motion.queue import MotionQueue

    senses = [EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, EMPTY_SENSE, Sense(speech_detected=True)]
    now, sense, advance = _idle_then(senses, ticks=5)

    result = run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,
    )
    assert SleepState.ASLEEP.name in result["states"]
    assert result["states"][-1] == SleepState.ALERT.name
    assert result["woke"] is True


def test_sleep_arc_wake_word_wakes_when_audio_on() -> None:
    """With ``audio_wake=True`` a detected wake-WORD (via the injected wake_detector
    factory) wakes it even with no speech flag / snap."""
    from reachy.behavior.sense import EMPTY_SENSE
    from reachy.cli._commands.sleep import WakeWord, run_sleep_arc
    from reachy.motion.queue import MotionQueue

    senses = [EMPTY_SENSE] * 5
    now, sense, advance = _idle_then(senses, ticks=5)

    fired = {"i": 0}

    class _WakeWordDetector:
        def update(self, sense, audio) -> bool:  # noqa: ANN001
            fired["i"] += 1
            return fired["i"] >= 5

        def reset(self) -> None:
            return None

    result = run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        audio_wake=True,
        wake_word=WakeWord(factory=lambda: _WakeWordDetector()),
        on_tick=advance,
        ticks=5,
        idle_timeout=15.0,
    )
    assert SleepState.ASLEEP.name in result["states"]
    assert result["states"][-1] == SleepState.ALERT.name
    assert result["woke"] is True


def test_sleep_arc_pat_only_does_not_consult_wake_word() -> None:
    """With ``audio_wake=False`` the audio wake-word backend is never consulted —
    its ``update`` must not be called."""
    from reachy.behavior.sense import EMPTY_SENSE
    from reachy.cli._commands.sleep import WakeWord, run_sleep_arc
    from reachy.motion.queue import MotionQueue

    senses = [EMPTY_SENSE] * 3
    now, sense, advance = _idle_then(senses, ticks=3)
    calls = {"n": 0}

    class _SpyDetector:
        def update(self, sense, audio) -> bool:  # noqa: ANN001
            calls["n"] += 1
            return False

        def reset(self) -> None:
            return None

    run_sleep_arc(
        queue=MotionQueue(),
        now=now,
        sense=sense,
        audio_wake=False,
        wake_word=WakeWord(factory=lambda: _SpyDetector()),
        on_tick=advance,
        ticks=3,
        idle_timeout=15.0,
    )
    assert calls["n"] == 0


# --- t4: CLI surface for --no-audio-wake / --wake pat ----------------------


def test_sleep_run_wake_pat_on_http_exits_2_text(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``sleep run --wake pat`` on the http transport (no head_pose read-back)
    raises a clean exit-2 CliError — two lines, no traceback."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    class _HttpTransport:
        name = "http"

        def move_goto(self, **kwargs: object) -> None:
            return None

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _HttpTransport())
    rc = main(["sleep", "run", "--transport", "http", "--wake", "pat", "--ticks", "1"])
    assert rc == EXIT_ENV_ERROR
    err = capsys.readouterr().err
    assert err.startswith("error:")
    assert "hint:" in err
    assert "Traceback" not in err


def test_sleep_run_no_audio_wake_on_http_exits_2_json(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``sleep run --no-audio-wake --json`` on http raises a structured exit-2."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    class _HttpTransport:
        name = "http"

        def move_goto(self, **kwargs: object) -> None:
            return None

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: _HttpTransport())
    rc = main(["sleep", "run", "--transport", "http", "--no-audio-wake", "--ticks", "1", "--json"])
    assert rc == EXIT_ENV_ERROR
    payload = json.loads(capsys.readouterr().err)
    assert payload["code"] == EXIT_ENV_ERROR
    assert "message" in payload and "remediation" in payload


def test_sleep_run_no_audio_wake_sdk_pat_only_bounded(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], tmp_path
) -> None:
    """``sleep run --no-audio-wake`` on the sdk transport wires a pat source from
    ``head_pose`` and runs the bounded loop to completion (no robot, no wake)."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    import reachy.cli._commands.sleep as sleep_mod

    class _PatSdkTransport(_FakeSdkTransport):
        def __init__(self) -> None:
            super().__init__()
            self.head_pose_reads = 0

        def head_pose(self) -> tuple[float, float]:
            self.head_pose_reads += 1
            return (0.0, 0.0)

    fake = _PatSdkTransport()

    # Stub the pat-wake SOURCE so the test exercises the pat-only WIRING (the
    # source is constructed with the SDK head-pose read-back and polled each tick)
    # without coupling to the PatDetector's press dynamics — a quiet run, no pat.
    polled = {"n": 0}

    class _QuietPatWake:
        def __init__(
            self, *, read_head_pose, commanded_pose, detector=None
        ) -> None:  # noqa: ANN001
            # Prove the wiring: the SDK head-pose read-back is the source.
            assert getattr(read_head_pose, "__self__", None) is fake
            self._commanded = commanded_pose

        def poll(self, *, now=None) -> bool:  # noqa: ANN001
            polled["n"] += 1
            self._commanded()  # reads the moving commanded pose (must not raise)
            return False

    monkeypatch.setattr(sleep_mod, "get_transport", lambda args: fake)
    monkeypatch.setattr(sleep_mod, "PatWakeSource", _QuietPatWake)
    monkeypatch.setattr(sleep_mod.time, "sleep", lambda *_a, **_k: None)
    rc = main(["sleep", "run", "--transport", "sdk", "--no-audio-wake", "--ticks", "4", "--json"])
    assert rc == 0
    out = capsys.readouterr().out
    last = [line for line in out.splitlines() if line.strip()][-1]
    payload = json.loads(last)
    assert payload["status"] == "ok"
    assert payload["ticks"] == 4
    assert payload["woke"] is False
    # The pat source was actually polled (pat-only path executed).
    assert polled["n"] >= 1


def test_sleep_run_help_names_audio_off_use(capsys: pytest.CaptureFixture[str]) -> None:
    """The run help text names the pat-only / quiet-room / audio-off deployment."""
    with pytest.raises(SystemExit) as exc:
        main(["sleep", "run", "--help"])
    assert exc.value.code == 0
    out = capsys.readouterr().out.lower()
    assert "--no-audio-wake" in out or "no-audio-wake" in out
    assert "--wake" in out
    assert "pat" in out


def test_sleep_overview_names_audio_off_use(capsys: pytest.CaptureFixture[str]) -> None:
    """The overview names the quiet-room / audio-off / pat-only deployment."""
    rc = main(["sleep", "overview"])
    assert rc == 0
    out = capsys.readouterr().out.lower()
    assert "quiet" in out or "audio-off" in out or "pat-only" in out


# --- t5: sleep start/restart forward --no-audio-wake ----------------------


def test_sleep_start_no_audio_wake_spawns_flag(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``sleep start --no-audio-wake`` builds a spawned command containing
    ``--no-audio-wake`` — verifying the plumb from subparser → cmd_sleep_start
    → supervisor.start → build_run_command."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    from reachy.sleep import supervisor as sup

    captured: list[list[str]] = []

    class _SpyPopen:
        returncode = None
        pid = 5555

        def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001
            captured.append(list(cmd))

        def poll(self) -> None:
            return None

    monkeypatch.setattr("subprocess.Popen", _SpyPopen)
    monkeypatch.setattr(sup, "is_alive", lambda pid: False)

    rc = main(["sleep", "start", "--no-audio-wake"])
    assert rc == 0
    assert len(captured) == 1
    assert "--no-audio-wake" in captured[0]


def test_sleep_start_no_audio_wake_absent_by_default(
    monkeypatch: pytest.MonkeyPatch, tmp_path
) -> None:
    """``sleep start`` (no flag) spawns a command WITHOUT ``--no-audio-wake``."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    from reachy.sleep import supervisor as sup

    captured: list[list[str]] = []

    class _SpyPopen:
        returncode = None
        pid = 5556

        def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001
            captured.append(list(cmd))

        def poll(self) -> None:
            return None

    monkeypatch.setattr("subprocess.Popen", _SpyPopen)
    monkeypatch.setattr(sup, "is_alive", lambda pid: False)

    rc = main(["sleep", "start"])
    assert rc == 0
    assert len(captured) == 1
    assert "--no-audio-wake" not in captured[0]


def test_sleep_restart_no_audio_wake_spawns_flag(monkeypatch: pytest.MonkeyPatch, tmp_path) -> None:
    """``sleep restart --no-audio-wake`` plumbs through cmd_sleep_restart →
    supervisor.restart → build_run_command: spawned argv carries the flag."""
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.setattr("time.sleep", lambda *_: None)

    from reachy.sleep import supervisor as sup

    captured: list[list[str]] = []

    class _SpyPopen:
        returncode = None
        pid = 5557

        def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001
            captured.append(list(cmd))

        def poll(self) -> None:
            return None

    monkeypatch.setattr("subprocess.Popen", _SpyPopen)
    monkeypatch.setattr(sup, "is_alive", lambda pid: False)

    rc = main(["sleep", "restart", "--no-audio-wake"])
    assert rc == 0
    assert len(captured) == 1
    assert "--no-audio-wake" in captured[0]
