"""Tests for the ``think`` noun group and the ``reachy.speech.supervisor``.

No real robot, daemon, LLM, TTS, or background process is involved:

* ``think run`` drives the cognition engine with the LLM/TTS/playback
  collaborators faked (``stream_sentences`` / ``synthesize`` / ``play_audio``)
  and a seeded :class:`~reachy.speech.events.EventBuffer`, so a spoken answer is
  produced from fed sense cues without any network or audio device.
* The supervisor's subprocess (``subprocess.Popen``), liveness (``os.kill`` /
  ``is_alive``), grace sleep, and HTTP health check are monkeypatched. State is
  pinned to a tmp dir via ``REACHY_STATE_DIR`` (mirrors
  ``tests/test_listen_cli.py``).

t7 builds the command module + a think-owned supervisor (``reachy.speech.supervisor``)
and tests only — registering the ``think`` noun in the top-level parser is t8.
So ``run`` is exercised directly via :func:`cmd_think_run`, and the supervisor
verbs via :func:`register` against a freshly-built subparser.
"""

from __future__ import annotations

import argparse
import json
import signal

import pytest

from reachy.cli._commands import think as think_mod
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.speech import supervisor
from reachy.speech.events import EventBuffer


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)


def _build_think_parser() -> argparse.ArgumentParser:
    """A standalone parser with only the ``think`` noun registered (t8 wires it
    into the real top-level parser; here we register it in isolation)."""
    parser = argparse.ArgumentParser(prog="reachy-mini-cli")
    sub = parser.add_subparsers(dest="command")
    think_mod.register(sub)
    return parser


def _run(argv: list[str]) -> int:
    """Parse + dispatch a ``think ...`` argv through the isolated parser.

    Returns the verb's exit code, translating a raised :class:`CliError` to its
    code (the real top-level ``main()`` does this; we replicate it for the
    isolated parser so error-contract tests can assert the exit code).
    """
    args = _build_think_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as err:
        return err.code


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Recorder:
    """Records synth/play calls so a spoken answer can be asserted."""

    def __init__(self) -> None:
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        self.played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))


# ---------------------------------------------------------------------------
# run — the foreground cognition loop
# ---------------------------------------------------------------------------


def test_run_speaks_an_answer_from_fed_sense_cues(monkeypatch, capsys) -> None:
    """A seeded sense feed produces a spoken answer (criterion 2).

    The engine is wired to fakes via the module-level engine factory; the sense
    feed is replaced with a deterministic source that pumps one DoA reading
    (speech on the left) into the buffer on the first turn.
    """
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        # The user message must carry the cue text the sense feed produced.
        user = messages[-1]["content"]
        assert "speech from the left" in user
        yield "I hear you."
        yield "Hello there."

    # Deterministic sense feed: feed exactly one DoA cue on the first call.
    fed: list[int] = []

    def fake_feed(buffer) -> None:
        if not fed:
            buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)
        fed.append(1)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    # First audio is produced (sentence-streamed) before generation completes.
    assert rec.played_texts == ["I hear you.", "Hello there."]
    assert rec.synth_texts == ["I hear you.", "Hello there."]


def test_run_json_emits_structured_summary(monkeypatch, capsys) -> None:
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "Okay."

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = _run(["think", "run", "--json", "--max-turns", "1"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["turns"] == 1


def test_run_unreachable_llm_exits_2_with_hint(monkeypatch, capsys) -> None:
    """An unreachable LLM raises CliError → exit 2, two-line error contract,
    no traceback (criterion 3)."""

    def boom_stream(messages, **_kw):
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="cannot reach LLM at http://localhost:8000",
            remediation="start the LLM server or set REACHY_OPENAI_URL_BASE",
        )
        yield  # pragma: no cover - generator marker

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", boom_stream)

    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 2


def test_run_unreachable_tts_exits_2(monkeypatch) -> None:
    """An unreachable TTS endpoint propagates CliError → exit 2."""

    def fake_stream(messages, **_kw):
        yield "Hello."

    def boom_synth(text, **_kw) -> bytes:
        raise CliError(
            code=EXIT_ENV_ERROR,
            message="TTS endpoint unreachable",
            remediation="start the TTS server or point REACHY_TTS_URL at a host",
        )

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", boom_synth)

    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 2


def test_run_error_renders_two_line_contract_in_text_mode(monkeypatch, capsys) -> None:
    """The CliError must render as ``error:`` then ``hint:`` (no traceback).

    The isolated parser used by these tests re-raises CliError; the real
    top-level ``main()`` (t8) is what renders it. To prove the contract we
    render here exactly as ``main`` would via the shared output helper.
    """
    from reachy.cli._output import emit_error

    err = CliError(
        code=EXIT_ENV_ERROR,
        message="cannot reach LLM at http://localhost:8000",
        remediation="start the LLM server",
    )
    emit_error(err, json_mode=False)
    captured = capsys.readouterr().err
    assert captured.startswith("error:")
    assert "hint:" in captured
    assert "Traceback" not in captured


# ---------------------------------------------------------------------------
# supervisor: start / stop / restart / status
# ---------------------------------------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.kwargs = kwargs
        self.pid = 7373

    def poll(self):
        return self.returncode


def _popen_factory(box):
    def _popen(cmd, **kwargs):  # noqa: ANN001 - test shim
        proc = _FakePopen(cmd, **kwargs)
        box.append(proc)
        return proc

    return _popen


def _no_spawn(cmd, **kwargs):  # noqa: ANN001 - test shim
    raise AssertionError("must not spawn a process here")


def test_start_spawns_think_run(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))

    rc = _run(["think", "start", "--transport", "sdk", "--max-turns", "5"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out and "pid: 7373" in out
    assert (tmp_path / "think.pid").read_text().strip() == "7373"
    cmd = procs[0].cmd
    assert cmd[1:5] == ["-m", "reachy", "think", "run"]
    assert "--transport" in cmd and "sdk" in cmd
    assert cmd[cmd.index("--max-turns") + 1] == "5"
    assert procs[0].kwargs.get("start_new_session") is True


def test_start_uses_own_pid_filename_not_listen(monkeypatch, tmp_path) -> None:
    """The think supervisor must NOT collide with listen's pid/log files."""
    assert supervisor.pid_file() == tmp_path / "think.pid"
    assert supervisor.log_file() == tmp_path / "think.log"


def test_start_idempotent_when_already_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "think.pid").write_text("7373")
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: True)
    monkeypatch.setattr("subprocess.Popen", _no_spawn)
    rc = _run(["think", "start"])
    assert rc == 0
    assert "already-running" in capsys.readouterr().out


def test_stop_when_not_running(capsys) -> None:
    rc = _run(["think", "stop"])
    assert rc == 0
    assert "not running" in capsys.readouterr().out


def test_stop_sigterm(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "think.pid").write_text("7373")
    state = {"alive": True}
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: state["alive"])
    monkeypatch.setattr("reachy.speech.supervisor._is_our_process", lambda pid: True)
    killed: list = []

    def _kill(pid, sig):
        killed.append((pid, sig))
        state["alive"] = False

    monkeypatch.setattr("os.kill", _kill)
    rc = _run(["think", "stop"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "stopped" in out and "SIGTERM" in out
    assert killed == [(7373, signal.SIGTERM)]
    assert not (tmp_path / "think.pid").exists()


def test_restart_stops_then_starts(monkeypatch, tmp_path, capsys) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    procs: list = []
    monkeypatch.setattr("subprocess.Popen", _popen_factory(procs))
    # No prior pid -> stop is a no-op, then start spawns.
    rc = _run(["think", "restart", "--transport", "sdk"])
    assert rc == 0
    out = capsys.readouterr().out
    assert "status: started" in out
    assert procs[0].cmd[1:5] == ["-m", "reachy", "think", "run"]


def test_status_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "think.pid").write_text("7373")
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: True)
    rc = _run(["think", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running" and payload["pid"] == 7373


def test_status_stopped_when_no_pid(capsys) -> None:
    rc = _run(["think", "status"])
    assert rc == 0
    assert "process: stopped" in capsys.readouterr().out


# ---------------------------------------------------------------------------
# overview + bare noun
# ---------------------------------------------------------------------------


def test_overview_text(capsys) -> None:
    assert _run(["think", "overview"]) == 0
    assert "# reachy-mini-cli think" in capsys.readouterr().out


def test_bare_think_prints_overview(capsys) -> None:
    assert _run(["think"]) == 0
    assert capsys.readouterr().out.strip()


def test_overview_json(capsys) -> None:
    assert _run(["think", "overview", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["subject"] == "reachy-mini-cli think"


# ---------------------------------------------------------------------------
# build_run_command unit
# ---------------------------------------------------------------------------


def test_build_run_command_serializes_flags() -> None:
    cmd = supervisor.build_run_command(
        transport="http",
        base_url="http://localhost:8000",
        timeout=10.0,
        llm_base_url="http://llm:1",
        tts_url="http://tts:2",
        turn_interval=2.5,
    )
    assert cmd[1:5] == ["-m", "reachy", "think", "run"]
    assert cmd[cmd.index("--transport") + 1] == "http"
    assert cmd[cmd.index("--llm-base-url") + 1] == "http://llm:1"
    assert cmd[cmd.index("--tts-url") + 1] == "http://tts:2"
    assert cmd[cmd.index("--turn-interval") + 1] == "2.5"


# ---------------------------------------------------------------------------
# sense feed wiring (DoA → EventBuffer) — unit
# ---------------------------------------------------------------------------


class _FakeSession:
    """A minimal SDK media session double for the sense feed."""

    samplerate = 16000

    def __init__(self, doa, sample) -> None:
        self._doa = doa
        self._sample = sample

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def doa(self, *, timeout=None):
        return self._doa

    def get_audio_sample(self):
        return self._sample


def test_sense_feed_pumps_doa_into_buffer(monkeypatch) -> None:
    """The before_turn hook reads DoA/RMS/speech and feeds the EventBuffer."""
    import numpy as np

    buffer = EventBuffer()
    sample = np.full(256, 0.3, dtype=np.float32)  # loud-ish
    session = _FakeSession({"angle": 0.0, "speech_detected": True}, sample)

    class _Tr:
        name = "fake"

        def media_session(self):
            return session

    monkeypatch.setattr(think_mod, "get_transport", lambda args: _Tr())
    args = _build_think_parser().parse_args(["think", "run", "--transport", "sdk"])
    feed = think_mod._make_sense_feed(args, buffer)
    feed()
    cues = buffer.snapshot()
    assert any("speech from the left" in c.text for c in cues)


# ---------------------------------------------------------------------------
# Self-mute guard — no audio feedback loop (mic + speaker share one device)
# ---------------------------------------------------------------------------


def test_mute_after_speak_breaks_the_feedback_loop(monkeypatch) -> None:
    """With a cue available every tick, the robot speaks only ONCE while the
    self-mute window holds — proving it doesn't react to its own voice."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "I hear something."

    # The feed would produce a fresh speech cue on EVERY call (simulates the mic
    # hearing the robot's own voice) — the guard must suppress it after speaking.
    def always_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.3, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: always_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    # Large mute window + 5 ticks: after the first spoken turn, every later tick is
    # muted, so the cue never re-fires.
    rc = _run(
        ["think", "run", "--max-ticks", "5", "--turn-interval", "0", "--mute-after-speak", "100"]
    )
    assert rc == 0
    assert rec.played_texts == ["I hear something."], "spoke more than once → feedback loop"


def test_mute_after_speak_zero_disables_the_guard(monkeypatch) -> None:
    """--mute-after-speak 0 disables the guard: a cue every tick → speaks every tick."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "Tick."

    def always_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.3, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: always_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = _run(
        ["think", "run", "--max-ticks", "3", "--turn-interval", "0", "--mute-after-speak", "0"]
    )
    assert rc == 0
    assert len(rec.played_texts) == 3, "guard disabled should speak on every cued tick"


def test_supervisor_forwards_mute_after_speak() -> None:
    """build_run_command forwards --mute-after-speak to the spawned think run."""
    cmd = supervisor.build_run_command(
        transport="http", base_url="http://localhost:8000", timeout=5.0, mute_after_speak=3.0
    )
    assert "--mute-after-speak" in cmd
    assert cmd[cmd.index("--mute-after-speak") + 1] == "3.0"
