"""The behavior runtime's speech actuator — background worker, never the tick thread.

Task t6. These tests are the acceptance contract for
:mod:`reachy.behavior.speech_act`:

1. **Nothing synthesizes or plays on the calling (tick) thread.** Proven two
   ways: the worker's thread identity is compared against the submitting
   thread's, and a blocking synthesize is shown NOT to block ``say()``.
2. **The tick budget holds.** A real bounded engine run, wrapped in the same
   :class:`~reachy.behavior.tick_metrics.TickMetrics` the runtime composes,
   with speech dispatched on EVERY tick and a synth/playback leg that is
   deliberately slower than the whole budget — zero overruns.
3. **A wedged or unreachable TTS degrades to silence and never stalls a tick.**
4. **The DEFAULT voice is the in-process harmonic synth**, exercised in the
   ``offline`` lane with every endpoint unreachable and real sockets blocked.
5. **TTS is a configurable alternative**, never required.
"""

from __future__ import annotations

import contextlib
import threading
import time

import pytest

from reachy.behavior.engine import EngineConfig
from reachy.behavior.engine import run as engine_run
from reachy.behavior.rule_engine import TickBus
from reachy.behavior.speech_act import (
    RUNTIME_DEFAULT_TRANSPORT,
    RUNTIME_DEFAULT_VOICE_ENGINE,
    SPEECH_TRANSPORT_ENV,
    SpeechActuator,
    resolve_playback_transport,
    resolve_runtime_voice_engine,
)
from reachy.behavior.tick_metrics import TickMetrics, budget_from_hz
from reachy.cli._errors import CliError

# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def __init__(self):
        self.calls = 0

    def set_target(self, *, head=None, antennas=None, body_yaw=None):
        self.calls += 1
        return {"status": "ok"}


class _FakeTransport:
    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

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


class _Recorder:
    """Records the thread each leg ran on, plus what it was handed."""

    def __init__(self, *, synth_delay=0.0, play_delay=0.0):
        self.synth_threads: list[int] = []
        self.play_threads: list[int] = []
        self.texts: list[str] = []
        self.played: list[tuple[bytes, int]] = []
        self._synth_delay = synth_delay
        self._play_delay = play_delay

    def synthesize(self, text, **_kw):
        self.synth_threads.append(threading.get_ident())
        self.texts.append(text)
        if self._synth_delay:
            time.sleep(self._synth_delay)
        return b"\x01\x00" * 400

    def play(self, pcm, *, samplerate):
        self.play_threads.append(threading.get_ident())
        if self._play_delay:
            time.sleep(self._play_delay)
        self.played.append((pcm, samplerate))


def _drain(actuator, *, timeout=5.0):
    """Block until the actuator has finished everything queued (test-only)."""
    assert actuator.join_idle(timeout=timeout), "speech worker did not drain in time"


# --------------------------------------------------------------------------- #
# Criterion 1 — nothing synthesizes or plays on the calling (tick) thread     #
# --------------------------------------------------------------------------- #


def test_synthesis_and_playback_run_on_a_background_thread_not_the_caller():
    rec = _Recorder()
    actuator = SpeechActuator(synthesize=rec.synthesize, play=rec.play, samplerate=16000)
    try:
        actuator.start()
        assert actuator.say("hello there") is True
        _drain(actuator)
    finally:
        actuator.close()

    caller = threading.get_ident()
    assert rec.synth_threads == rec.play_threads
    assert rec.synth_threads and rec.synth_threads[0] != caller
    assert rec.played and rec.played[0][1] == 16000
    assert rec.texts == ["hello there"]


def test_say_returns_immediately_even_while_the_worker_is_blocked_mid_synthesis():
    """``say`` is O(1) hand-off: a synthesize wedged forever must not hold it."""
    entered = threading.Event()
    release = threading.Event()

    def slow_synthesize(text, **_kw):
        entered.set()
        release.wait(5.0)
        return b"\x00\x00" * 100

    actuator = SpeechActuator(synthesize=slow_synthesize, play=lambda pcm, *, samplerate: None)
    try:
        actuator.start()
        actuator.say("first")
        assert entered.wait(2.0), "worker never picked the utterance up"
        # The worker is now wedged inside synthesize. A second say must still
        # return promptly (accepted or dropped — never blocked).
        started = time.perf_counter()
        actuator.say("second")
        assert time.perf_counter() - started < 0.05
    finally:
        release.set()
        actuator.close()


def test_say_is_bounded_work_and_never_blocks_when_the_queue_is_full():
    release = threading.Event()
    entered = threading.Event()

    def wedged(text, **_kw):
        entered.set()
        release.wait(5.0)
        return b""

    actuator = SpeechActuator(
        synthesize=wedged, play=lambda pcm, *, samplerate: None, queue_maxsize=1
    )
    try:
        actuator.start()
        assert actuator.say("one") is True
        assert entered.wait(2.0)
        assert actuator.say("two") is True  # fills the depth-1 queue
        started = time.perf_counter()
        assert actuator.say("three") is False  # dropped, not blocked
        assert time.perf_counter() - started < 0.05
        assert actuator.dropped == 1
    finally:
        release.set()
        actuator.close()


# --------------------------------------------------------------------------- #
# Criterion 2 — the tick budget holds across a sustained run                  #
# --------------------------------------------------------------------------- #


def test_engine_run_with_speech_on_every_tick_has_zero_tick_overruns():
    """A full engine run, speech dispatched every tick, synth+playback slower
    than the whole 20 ms budget — and not one tick overruns.

    This is the measurement that matters: :class:`TickMetrics` times the REAL
    wall-clock duration of the seam (``time.perf_counter``, not the engine's
    injected logical clock), so an actuator that did any of its work inline
    would show up here immediately.
    """
    ticks_to_run = 250
    rec = _Recorder(synth_delay=0.03, play_delay=0.03)  # 3x the 20 ms budget, each
    actuator = SpeechActuator(
        synthesize=rec.synthesize,
        play=rec.play,
        samplerate=16000,
        queue_maxsize=2,
    )

    def speech_driver(ctx):
        actuator.say(f"utterance {ctx.tick}")

    bus = TickBus(drivers=[speech_driver])
    metrics = TickMetrics(bus, budget_s=budget_from_hz(50.0))
    try:
        actuator.start()
        ran = engine_run(
            _FakeTransport(),
            EngineConfig(compose_hz=50, base_layer=True, settle=False),
            sleep=lambda *_: None,
            now=_Clock(),
            max_ticks=ticks_to_run,
            tick_seam=metrics,
        )
    finally:
        actuator.close()

    assert ran == ticks_to_run
    assert metrics.overruns == 0
    # The point of the bounded queue: the slow leg drops work rather than
    # stalling the tick, so far fewer utterances render than were offered.
    assert actuator.dropped > 0
    assert rec.synth_threads, "the worker rendered nothing at all"
    assert set(rec.synth_threads) != {threading.get_ident()}


# --------------------------------------------------------------------------- #
# Criterion 3 — a wedged/unreachable backend degrades to silence              #
# --------------------------------------------------------------------------- #


def test_unreachable_synthesis_degrades_to_silence_and_never_raises():
    def unreachable(text, **_kw):
        raise CliError(code=2, message="cannot reach the TTS endpoint", remediation="start it")

    played: list = []
    actuator = SpeechActuator(
        synthesize=unreachable, play=lambda pcm, *, samplerate: played.append(pcm)
    )
    try:
        actuator.start()
        assert actuator.say("anything") is True
        _drain(actuator)
        assert played == []
        assert actuator.failures == 1
        assert actuator.worker is not None and actuator.worker.is_alive()
    finally:
        actuator.close()


def test_unreachable_playback_degrades_to_silence_and_keeps_the_worker_alive():
    def unreachable_play(pcm, *, samplerate):
        raise ConnectionRefusedError("[Errno 111] Connection refused")

    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: b"\x01\x00" * 100, play=unreachable_play
    )
    try:
        actuator.start()
        actuator.say("hello")
        _drain(actuator)
        assert actuator.failures == 1
        assert actuator.worker is not None and actuator.worker.is_alive()
    finally:
        actuator.close()


def test_a_persistently_dead_backend_latches_the_sink_off_then_retries_later():
    """A hard-down backend must not be re-dialled on every utterance forever.

    Mirrors ``CognitionEngine(audio_optional=True)``'s latch, but time-bounded:
    a transient daemon restart must not leave a boot-persistent robot mute for
    the rest of its uptime.
    """
    clock = {"t": 100.0}
    attempts: list[str] = []

    def dead(text, **_kw):
        attempts.append(text)
        raise OSError("down")

    actuator = SpeechActuator(
        synthesize=dead,
        play=lambda pcm, *, samplerate: None,
        failure_latch=2,
        retry_after_s=30.0,
        clock=lambda: clock["t"],
    )
    try:
        actuator.start()
        for i in range(2):
            actuator.say(f"try {i}")
            _drain(actuator)
        assert len(attempts) == 2
        assert actuator.muted is True

        actuator.say("while latched")
        _drain(actuator)
        assert len(attempts) == 2  # not even attempted
        assert actuator.dropped >= 1

        clock["t"] += 31.0
        assert actuator.muted is False
        actuator.say("after the retry window")
        _drain(actuator)
        assert len(attempts) == 3
    finally:
        actuator.close()


def test_empty_and_oversized_text_are_dropped_with_a_named_reason(caplog):
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: b"\x00\x00", play=lambda pcm, *, samplerate: None
    )
    try:
        with caplog.at_level("INFO", logger="reachy.sense"):
            assert actuator.say("") is False
            assert actuator.say("   ") is False
            assert actuator.say("x" * 10_000) is False
        text = caplog.text
        assert "stage=speech" in text
        assert "reason=empty-text" in text
        assert "reason=too-long" in text
        assert actuator.dropped == 3
    finally:
        actuator.close()


def test_close_is_idempotent_and_joins_the_worker():
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: b"\x00\x00", play=lambda pcm, *, samplerate: None
    )
    actuator.start()
    actuator.say("bye")
    worker = actuator.worker
    actuator.close()
    actuator.close()
    assert worker is not None and not worker.is_alive()
    assert actuator.say("after close") is False


def test_say_after_close_never_starts_a_new_worker():
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: b"\x00\x00", play=lambda pcm, *, samplerate: None
    )
    actuator.close()
    assert actuator.say("nope") is False
    assert actuator.worker is None


# --------------------------------------------------------------------------- #
# Self-mute — the actuator publishes the window its own voice occupies        #
# --------------------------------------------------------------------------- #


def test_playback_stamps_a_self_mute_window_covering_the_whole_clip():
    """``mute_until`` is what the transcript driver reads so the robot never
    transcribes its own voice (its ``mute_until`` seam, wired at composition)."""
    clock = {"t": 500.0}
    samplerate = 16000
    one_second_pcm = b"\x00\x00" * samplerate

    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: one_second_pcm,
        play=lambda pcm, *, samplerate: None,
        samplerate=samplerate,
        mute_margin_s=0.5,
        clock=lambda: clock["t"],
    )
    try:
        assert actuator.mute_until() == 0.0
        actuator.start()
        actuator.say("a one second utterance")
        _drain(actuator)
        # 1.0 s of audio + the 0.5 s margin, measured from the injected clock.
        assert actuator.mute_until() == pytest.approx(501.5)
    finally:
        actuator.close()


def test_mute_until_is_unchanged_when_synthesis_produced_no_audio():
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: b"", play=lambda pcm, *, samplerate: None
    )
    try:
        actuator.start()
        actuator.say("silent")
        _drain(actuator)
        assert actuator.mute_until() == 0.0
        assert actuator.spoken == 0
    finally:
        actuator.close()


# --------------------------------------------------------------------------- #
# Criteria 4 & 5 — harmonic is the default; TTS is the configurable alternate #
# --------------------------------------------------------------------------- #


def test_the_runtime_default_voice_engine_is_harmonic(monkeypatch):
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    engine = resolve_runtime_voice_engine()
    assert RUNTIME_DEFAULT_VOICE_ENGINE == "harmonic"
    assert engine.name == "harmonic"
    assert engine.samplerate == 16000


def test_tts_is_selectable_as_the_alternative_backend(monkeypatch):
    monkeypatch.setenv("REACHY_VOICE_ENGINE", "tts")
    assert resolve_runtime_voice_engine().name == "tts"
    # An explicit argument still beats the environment.
    assert resolve_runtime_voice_engine("harmonic").name == "harmonic"


def test_an_unknown_voice_engine_is_a_clean_user_error(monkeypatch):
    monkeypatch.setenv("REACHY_VOICE_ENGINE", "kazoo")
    with pytest.raises(CliError) as excinfo:
        resolve_runtime_voice_engine()
    assert excinfo.value.code == 1


def test_the_default_playback_transport_is_http_and_is_overridable(monkeypatch):
    monkeypatch.delenv(SPEECH_TRANSPORT_ENV, raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    assert RUNTIME_DEFAULT_TRANSPORT == "http"
    assert resolve_playback_transport() == "http"

    monkeypatch.setenv("REACHY_TRANSPORT", "sdk")
    assert resolve_playback_transport() == "sdk"
    monkeypatch.setenv(SPEECH_TRANSPORT_ENV, "http")
    assert resolve_playback_transport() == "http"  # the specific var wins
    assert resolve_playback_transport("sdk") == "sdk"  # explicit beats both


def test_an_unknown_playback_transport_is_a_clean_user_error(monkeypatch):
    monkeypatch.setenv(SPEECH_TRANSPORT_ENV, "carrier-pigeon")
    with pytest.raises(CliError) as excinfo:
        resolve_playback_transport()
    assert excinfo.value.code == 1


def test_the_actuator_defaults_to_the_harmonic_engines_sample_rate(monkeypatch):
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    played: list = []
    actuator = SpeechActuator(play=lambda pcm, *, samplerate: played.append(samplerate))
    try:
        assert actuator.voice.name == "harmonic"
        actuator.start()
        actuator.say("hello robot")
        _drain(actuator)
        assert played == [16000]
    finally:
        actuator.close()


@pytest.mark.offline
def test_the_default_voice_renders_real_audio_with_every_endpoint_unreachable(monkeypatch):
    """Criterion 4's real proof: no env pointed anywhere live, sockets blocked
    outright by the ``offline`` guard — and the robot still produces audio.

    Only the PLAYBACK seam is injected (it has to reach a speaker somehow); the
    whole meaning -> notes -> PCM leg is the shipped default and runs in-process.
    """
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    played: list = []
    actuator = SpeechActuator(play=lambda pcm, *, samplerate: played.append((pcm, samplerate)))
    try:
        assert actuator.voice.name == "harmonic"
        actuator.start()
        assert actuator.say("hello, is anybody there") is True
        _drain(actuator)
    finally:
        actuator.close()

    assert len(played) == 1
    pcm, samplerate = played[0]
    assert samplerate == 16000
    assert len(pcm) > 1000  # real rendered PCM16, not an empty stub
    assert len(pcm) % 2 == 0
    assert actuator.spoken == 1
    assert actuator.failures == 0


@pytest.mark.offline
def test_the_whole_default_path_degrades_to_silence_when_playback_is_also_down():
    """The default synth is offline-safe; the default PLAYBACK leg is not (it
    reaches the daemon). With sockets blocked, the actuator must still fall
    silent without raising and without wedging — criterion 3 on the real,
    un-injected default playback binding."""
    actuator = SpeechActuator()  # both legs at their shipped defaults
    try:
        actuator.start()
        assert actuator.say("nobody can hear this") is True
        _drain(actuator)
        assert actuator.failures == 1
        assert actuator.spoken == 0
        assert actuator.worker is not None and actuator.worker.is_alive()
    finally:
        actuator.close()
