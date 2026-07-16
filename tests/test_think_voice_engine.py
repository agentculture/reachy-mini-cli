"""Tests for wiring the harmonic/tts voice engine into ``think`` (t4).

Mirrors the harness style of ``tests/test_think.py`` (isolated parser built from
``think_mod.register``, ``REACHY_STATE_DIR`` pinned to a tmp dir, LLM/TTS/playback
collaborators faked via monkeypatch) rather than importing from it, so this file
stays self-contained.

Covers:

* ``think run`` / ``think demo`` / ``think start`` gain ``--voice-engine {tts,harmonic}``.
* Engine selection precedence: explicit flag > ``REACHY_VOICE_ENGINE`` env > "tts".
* Engine ``tts`` is byte-identical to before this feature existed (module-alias
  wiring + no ``samplerate`` in playback kwargs is preserved).
* Engine ``harmonic`` wires :func:`reachy.speech.harmonic.synthesize`, empty
  ``tts_kwargs``, and ``playback_kwargs["samplerate"] == 16000`` into
  :class:`~reachy.speech.cognition.CognitionEngine` (and the demo's direct
  synthesize call).
* The startup banner names the active voice engine.
* ``think start``/``restart`` forward an explicit ``--voice-engine`` to the
  spawned ``think run`` via the inherited environment (``build_run_command``
  is not touched — see ``_voice_engine_env`` in think.py).
* ``think status --json`` gains a ``voice_engine`` field, sourced from a
  ``think.voice`` sidecar written for the run's lifetime; ``null`` when the
  loop is not running.
* The self-mute window's clip-duration math uses the ACTIVE engine's
  samplerate (regression: a harmonic-length clip must not be mis-measured
  against tts's 24 kHz rate).
"""

from __future__ import annotations

import argparse
import json
import os

import pytest

from reachy.cli._commands import think as think_mod
from reachy.cli._errors import EXIT_ENV_ERROR, CliError
from reachy.speech.voice import VOICE_ENGINE_ENV


@pytest.fixture(autouse=True)
def _isolate_state(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.delenv(VOICE_ENGINE_ENV, raising=False)


def _build_think_parser() -> argparse.ArgumentParser:
    """A standalone parser with only the ``think`` noun registered (mirrors
    ``tests/test_think.py``'s helper of the same name)."""
    parser = argparse.ArgumentParser(prog="reachy-mini-cli")
    sub = parser.add_subparsers(dest="command")
    think_mod.register(sub)
    return parser


def _run(argv: list[str]) -> int:
    """Parse + dispatch a ``think ...`` argv, translating a raised CliError to
    its exit code (the real top-level ``main()`` does this)."""
    args = _build_think_parser().parse_args(argv)
    try:
        return args.func(args)
    except CliError as err:
        return err.code


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------


class _Recorder:
    """Records synth/play calls so a spoken answer can be asserted (mirrors
    tests/test_think.py's private helper of the same name)."""

    def __init__(self) -> None:
        self.synth_texts: list[str] = []
        self.played_texts: list[str] = []

    def synth(self, text: str, **_kw) -> bytes:
        self.synth_texts.append(text)
        return ("pcm:" + text).encode("utf-8")

    def play(self, pcm: bytes, **_kw) -> None:
        self.played_texts.append(pcm.decode("utf-8").removeprefix("pcm:"))


class _FakeCognitionEngine:
    """A stand-in for ``CognitionEngine`` that records its constructor kwargs and
    never actually runs a turn — used to inspect exactly how ``cmd_think_run``
    wires the active voice engine without needing a full cognition loop."""

    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs

    def run(self, *, max_turns=None, stop=None, before_turn=None) -> int:
        return 0


def _capture_engine_factory():
    """Return ``(factory, captured)``: ``factory`` builds a ``_FakeCognitionEngine``
    and stashes its kwargs into ``captured["kwargs"]`` for the test to inspect."""
    captured: dict = {}

    def factory(**kwargs):
        captured["kwargs"] = kwargs
        return _FakeCognitionEngine(**kwargs)

    return factory, captured


# ---------------------------------------------------------------------------
# think run — engine selection wires CognitionEngine's constructor kwargs
# ---------------------------------------------------------------------------


def test_run_bare_defaults_to_tts_engine_kwargs_unchanged(monkeypatch) -> None:
    """Bare ``think run`` (no flag, no env) wires the tts engine exactly as
    before this feature: the module-level ``_synthesize`` alias, the existing
    ``_tts_kwargs(args)`` dict, and NO ``samplerate`` key in playback_kwargs."""
    factory, captured = _capture_engine_factory()
    monkeypatch.setattr(think_mod, "CognitionEngine", factory)
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)

    rc = _run(["think", "run", "--max-ticks", "0"])
    assert rc == 0
    kwargs = captured["kwargs"]
    assert kwargs["synthesize"] is think_mod._synthesize
    assert kwargs["tts_kwargs"] == {}
    assert "samplerate" not in kwargs["playback_kwargs"]


def test_run_voice_engine_harmonic_wires_cognition_engine(monkeypatch) -> None:
    """``--voice-engine harmonic`` wires the harmonic synthesize, empty
    tts_kwargs, and playback samplerate pinned to 16000."""
    factory, captured = _capture_engine_factory()
    monkeypatch.setattr(think_mod, "CognitionEngine", factory)
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)

    rc = _run(["think", "run", "--voice-engine", "harmonic", "--max-ticks", "0"])
    assert rc == 0
    kwargs = captured["kwargs"]
    assert kwargs["synthesize"] is think_mod._harmonic_synthesize
    assert kwargs["tts_kwargs"] == {}
    assert kwargs["playback_kwargs"]["samplerate"] == 16000
    # transport/base_url are still carried alongside the pinned samplerate.
    assert "transport" in kwargs["playback_kwargs"]
    assert "base_url" in kwargs["playback_kwargs"]


def test_env_var_selects_harmonic_engine(monkeypatch) -> None:
    """REACHY_VOICE_ENGINE=harmonic selects harmonic with no explicit flag."""
    factory, captured = _capture_engine_factory()
    monkeypatch.setattr(think_mod, "CognitionEngine", factory)
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")

    rc = _run(["think", "run", "--max-ticks", "0"])
    assert rc == 0
    kwargs = captured["kwargs"]
    assert kwargs["synthesize"] is think_mod._harmonic_synthesize
    assert kwargs["playback_kwargs"]["samplerate"] == 16000


def test_explicit_voice_engine_flag_overrides_env(monkeypatch) -> None:
    """--voice-engine tts wins over REACHY_VOICE_ENGINE=harmonic."""
    factory, captured = _capture_engine_factory()
    monkeypatch.setattr(think_mod, "CognitionEngine", factory)
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")

    rc = _run(["think", "run", "--voice-engine", "tts", "--max-ticks", "0"])
    assert rc == 0
    kwargs = captured["kwargs"]
    assert kwargs["synthesize"] is think_mod._synthesize
    assert "samplerate" not in kwargs["playback_kwargs"]


# ---------------------------------------------------------------------------
# think run — end-to-end speaking behaviour (real CognitionEngine)
# ---------------------------------------------------------------------------


def test_run_bare_tts_speaks_via_tts_synth_end_to_end(monkeypatch) -> None:
    """Full run with the real CognitionEngine: the default engine speaks
    through the tts alias and never touches the harmonic one."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "Testing tts voice."

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    def _forbid_harmonic(text, **_kw):
        raise AssertionError("harmonic synthesize must not be called for the default tts engine")

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)
    monkeypatch.setattr(think_mod, "_harmonic_synthesize", _forbid_harmonic)

    rc = _run(["think", "run", "--max-turns", "1"])
    assert rc == 0
    assert rec.played_texts == ["Testing tts voice."]


def test_run_harmonic_speaks_via_harmonic_synth_end_to_end(monkeypatch) -> None:
    """Full run with the real CognitionEngine: --voice-engine harmonic speaks
    through the harmonic alias and never touches the tts one."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "Testing harmonic voice."

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    def _forbid_tts(text, **_kw):
        raise AssertionError("tts synthesize must not be called when engine=harmonic")

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_harmonic_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)
    monkeypatch.setattr(think_mod, "_synthesize", _forbid_tts)

    rc = _run(["think", "run", "--voice-engine", "harmonic", "--max-turns", "1"])
    assert rc == 0
    assert rec.played_texts == ["Testing harmonic voice."]


# ---------------------------------------------------------------------------
# Startup banner names the active voice engine
# ---------------------------------------------------------------------------


def test_run_banner_names_the_active_voice_engine(monkeypatch, capsys) -> None:
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)

    rc = _run(["think", "run", "--voice-engine", "harmonic", "--max-ticks", "0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "voice engine: harmonic" in err


def test_run_banner_names_tts_by_default(monkeypatch, capsys) -> None:
    monkeypatch.setattr(think_mod, "_make_sense_feed", lambda args, buffer: lambda: None)

    rc = _run(["think", "run", "--max-ticks", "0"])
    assert rc == 0
    err = capsys.readouterr().err
    assert "voice engine: tts" in err


# ---------------------------------------------------------------------------
# think demo --voice-engine harmonic
# ---------------------------------------------------------------------------


class _FakeTransport:
    """Records move_goto calls; touches no real hardware or network.

    Faking ``get_transport`` (not just ``_play_audio``) means the expression
    queue's synchronous drain (``_MotionExecutor.drain``, called from
    ``motion.stop()``) never attempts a real HTTP/SDK call either — so a
    forbidden-``urlopen`` guard cleanly isolates the voice-engine's own
    network behaviour from the pre-existing (and unrelated) motion path.
    """

    def move_goto(
        self,
        *,
        head=None,
        antennas=None,
        body_yaw=None,
        duration=1.0,
        interpolation="minjerk",
    ) -> None:
        return None


def test_demo_voice_engine_harmonic_uses_real_harmonic_synth_no_network(monkeypatch) -> None:
    """The demo's 3 quoted phrases render through the real (fast, in-process,
    deterministic) harmonic synthesizer — no LLM, no TTS HTTP call — and
    playback receives samplerate=16000. Network access is forbidden outright."""
    played: list[dict] = []

    def fake_play(pcm: bytes, **kwargs) -> None:
        played.append({"pcm": pcm, **kwargs})

    def _forbid_urlopen(*_a, **_kw):
        raise AssertionError("think demo --voice-engine harmonic must not touch the network")

    monkeypatch.setattr(think_mod, "get_transport", lambda args: _FakeTransport())
    monkeypatch.setattr(think_mod, "_play_audio", fake_play)
    monkeypatch.setattr("urllib.request.urlopen", _forbid_urlopen)

    rc = _run(["think", "demo", "--voice-engine", "harmonic"])
    assert rc == 0
    assert len(played) == 3  # DEMO_SCRIPT has 3 quoted phrases
    for call in played:
        assert call["samplerate"] == 16000
        assert len(call["pcm"]) > 0


def test_demo_voice_engine_harmonic_never_calls_tts_synth(monkeypatch) -> None:
    def _forbid_tts(text, **_kw):
        raise AssertionError("tts synthesize must not be called under --voice-engine harmonic")

    monkeypatch.setattr(think_mod, "get_transport", lambda args: _FakeTransport())
    monkeypatch.setattr(think_mod, "_synthesize", _forbid_tts)
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)

    rc = _run(["think", "demo", "--voice-engine", "harmonic"])
    assert rc == 0


# ---------------------------------------------------------------------------
# think start / restart — --voice-engine forwarded via the environment
# ---------------------------------------------------------------------------


class _FakePopen:
    returncode = None

    def __init__(self, cmd, **kwargs) -> None:  # noqa: ANN001 - test shim
        self.cmd = list(cmd)
        self.pid = 4242

    def poll(self):
        return self.returncode


def test_start_forwards_explicit_voice_engine_via_env(monkeypatch) -> None:
    """--voice-engine harmonic on `start` is visible to the spawned Popen call
    via REACHY_VOICE_ENGINE, and the env var is restored (removed) after."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    seen_env: list[str | None] = []

    def _popen(cmd, **kwargs):
        seen_env.append(os.environ.get(VOICE_ENGINE_ENV))
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr("subprocess.Popen", _popen)

    rc = _run(["think", "start", "--voice-engine", "harmonic"])
    assert rc == 0
    assert seen_env == ["harmonic"]
    assert os.environ.get(VOICE_ENGINE_ENV) is None


def test_start_without_voice_engine_flag_leaves_env_untouched(monkeypatch) -> None:
    """No --voice-engine flag: the spawn inherits whatever REACHY_VOICE_ENGINE
    the operator already has set (or unset), unchanged from before this feature."""
    monkeypatch.setattr("time.sleep", lambda *_: None)
    monkeypatch.setenv(VOICE_ENGINE_ENV, "harmonic")
    seen_env: list[str | None] = []

    def _popen(cmd, **kwargs):
        seen_env.append(os.environ.get(VOICE_ENGINE_ENV))
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr("subprocess.Popen", _popen)

    rc = _run(["think", "start"])
    assert rc == 0
    assert seen_env == ["harmonic"]  # inherited as-is, not touched by the flag path
    assert os.environ.get(VOICE_ENGINE_ENV) == "harmonic"  # still set after (monkeypatch's own)


def test_restart_forwards_explicit_voice_engine_via_env(monkeypatch) -> None:
    monkeypatch.setattr("time.sleep", lambda *_: None)
    seen_env: list[str | None] = []

    def _popen(cmd, **kwargs):
        seen_env.append(os.environ.get(VOICE_ENGINE_ENV))
        return _FakePopen(cmd, **kwargs)

    monkeypatch.setattr("subprocess.Popen", _popen)

    rc = _run(["think", "restart", "--voice-engine", "harmonic"])
    assert rc == 0
    assert seen_env == ["harmonic"]
    assert os.environ.get(VOICE_ENGINE_ENV) is None


# ---------------------------------------------------------------------------
# think status --json voice_engine field
# ---------------------------------------------------------------------------


def test_status_json_voice_engine_present_when_running(monkeypatch, tmp_path, capsys) -> None:
    """Documented choice: while the tracked pid is alive, voice_engine reports
    whatever name the think.voice sidecar holds."""
    (tmp_path / "think.pid").write_text("7373")
    (tmp_path / "think.voice").write_text("harmonic")
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: True)

    rc = _run(["think", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "running"
    assert payload["voice_engine"] == "harmonic"


def test_status_text_voice_engine_present_when_running(monkeypatch, tmp_path, capsys) -> None:
    (tmp_path / "think.pid").write_text("7373")
    (tmp_path / "think.voice").write_text("tts")
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: True)

    rc = _run(["think", "status"])
    assert rc == 0
    assert "voice_engine: tts" in capsys.readouterr().out


def test_status_json_voice_engine_null_when_no_pid(capsys) -> None:
    """Documented choice: no tracked process -> voice_engine is null (never a
    leftover name from a previous run)."""
    rc = _run(["think", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "stopped"
    assert payload["voice_engine"] is None


def test_status_json_voice_engine_null_when_stale_pid(monkeypatch, tmp_path, capsys) -> None:
    """A dead-but-tracked pid (stale) also reports voice_engine null, even if a
    sidecar happens to still be present (e.g. a crash that skipped cleanup)."""
    (tmp_path / "think.pid").write_text("7373")
    (tmp_path / "think.voice").write_text("harmonic")
    monkeypatch.setattr("reachy.speech.supervisor.is_alive", lambda pid: False)

    rc = _run(["think", "status", "--json"])
    assert rc == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["process"] == "stale"
    assert payload["voice_engine"] is None


# ---------------------------------------------------------------------------
# The think.voice sidecar's write/clear lifecycle (the mechanism status reads)
# ---------------------------------------------------------------------------


def test_run_sidecar_exists_during_run_and_is_cleared_after(monkeypatch, tmp_path) -> None:
    seen: dict[str, str] = {}

    def fake_stream(messages, **_kw):
        yield "Ok."

    def fake_feed(buffer) -> None:
        seen["during"] = (tmp_path / "think.voice").read_text().strip()
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_harmonic_synthesize", lambda text, **kw: b"")
    monkeypatch.setattr(think_mod, "_play_audio", lambda pcm, **kw: None)

    rc = _run(["think", "run", "--voice-engine", "harmonic", "--max-turns", "1"])
    assert rc == 0
    assert seen["during"] == "harmonic"
    assert not (tmp_path / "think.voice").exists()


def test_run_sidecar_cleared_even_on_unreachable_llm_error(monkeypatch, tmp_path) -> None:
    """The sidecar must not survive a crashed run (a CliError mid-loop)."""

    def boom_stream(messages, **_kw):
        raise CliError(code=EXIT_ENV_ERROR, message="boom", remediation="n/a")
        yield  # pragma: no cover - generator marker

    def fake_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.2, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: fake_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", boom_stream)

    rc = _run(["think", "run", "--voice-engine", "harmonic", "--max-turns", "1"])
    assert rc == 2
    assert not (tmp_path / "think.voice").exists()


# ---------------------------------------------------------------------------
# Self-mute window regression: clip duration must use the ACTIVE samplerate
# ---------------------------------------------------------------------------


def test_mute_window_covers_harmonic_clip_duration() -> None:
    """For a harmonic-length clip, mute['until'] >= now + len(pcm)/2/16000."""
    pcm = b"\x00\x01" * 20000  # 40000 bytes of PCM16 @ 16 kHz -> 1.25s of audio
    now = 1000.0
    mute_after = 2.5

    until = think_mod._mute_window(pcm, 16000, mute_after, now=now)

    assert until >= now + len(pcm) / 2 / 16000


def test_mute_window_formula_is_duration_plus_margin() -> None:
    """Exact formula check: until == now + clip_seconds + mute_after."""
    pcm = b"\x00\x01" * 20000
    now = 1000.0
    mute_after = 2.5
    expected = now + (len(pcm) / 2 / 16000) + mute_after

    assert think_mod._mute_window(pcm, 16000, mute_after, now=now) == pytest.approx(expected)


def test_mute_window_uses_engine_samplerate_not_a_hardcoded_tts_rate() -> None:
    """The same clip measured at tts's 24kHz vs harmonic's 16kHz yields a
    different (longer, for 16kHz) duration — proving the rate is a parameter,
    not a hardcoded assumption baked into the function."""
    pcm = b"\x00\x01" * 20000
    now = 0.0
    mute_after = 0.0  # isolate the duration term

    at_tts_rate = think_mod._mute_window(pcm, 24000, mute_after, now=now)
    at_harmonic_rate = think_mod._mute_window(pcm, 16000, mute_after, now=now)

    assert at_harmonic_rate > at_tts_rate


def test_run_guarded_play_uses_active_engine_samplerate_for_mute_window(monkeypatch) -> None:
    """End-to-end: --voice-engine harmonic's self-mute guard measures clip
    duration at 16kHz (not tts's 24kHz), so a long harmonic clip still
    suppresses the very next tick's cue (mirrors test_think.py's
    test_mute_after_speak_breaks_the_feedback_loop, but for the harmonic engine)."""
    rec = _Recorder()

    def fake_stream(messages, **_kw):
        yield "I hear something."

    # A cue is available on every tick; only the self-mute guard should keep
    # the robot from reacting to its own harmonic voice.
    def always_feed(buffer) -> None:
        buffer.feed_doa(angle_rad=0.0, rms=0.3, is_speech=True)

    monkeypatch.setattr(
        think_mod, "_make_sense_feed", lambda args, buffer: lambda: always_feed(buffer)
    )
    monkeypatch.setattr(think_mod, "_stream_sentences", fake_stream)
    monkeypatch.setattr(think_mod, "_harmonic_synthesize", rec.synth)
    monkeypatch.setattr(think_mod, "_play_audio", rec.play)

    rc = _run(
        [
            "think",
            "run",
            "--voice-engine",
            "harmonic",
            "--max-ticks",
            "5",
            "--turn-interval",
            "0",
            "--mute-after-speak",
            "100",
        ]
    )
    assert rc == 0
    assert rec.played_texts == ["I hear something."], "spoke more than once -> feedback loop"
