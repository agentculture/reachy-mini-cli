"""The runtime's voice is a fan-out leg of the ONE held media client (task t10).

Before this task ``_make_speech_actuator`` built a BARE
:class:`~reachy.behavior.speech_act.SpeechActuator`, so the ``sdk`` playback path
had no session to use and :func:`reachy.speech.playback._open_sdk_media` opened a
**SECOND** ``ReachyMini`` — the exact thing the single-SDK-owner model forbids,
and the reason #122 had to re-enable the daemon ``http`` route as a quick fix.
This file is the acceptance contract for the durable fix (spec claim c16): the
held client the runtime already owns is injected into the play seam.

**The shape is DIRECT injection, decided by a live probe (deviation d2), not by
preference.** On spark-f8a9, 2026-07-24, pushing a TTS clip through
``playback._play_sdk`` from a WORKER thread while a separate reader thread
drained ``client.audio()`` concurrently produced 198 clean reads, ZERO read
errors, and no reader stall — the SDK's input-read and output-push paths do not
contend. ``push_audio_sample`` buffers and returns in ~8 ms for a 5.76 s clip, so
it never blocks. The clip was confirmed audible from Reachy's own speaker. That
is why the hand-off is a plain injected callable rather than a pump-style output
seam mirroring :class:`~reachy.behavior.audio_pump.AudioPump`.

The four criteria proved here:

1. the play seam accepts a held-media-session provider and the ``sdk`` path
   pushes through THAT session — ``_open_sdk_media`` is never reached;
2. the push happens on the speech WORKER thread (never the caller's), and a
   wedged or dead held session still resolves to a named drop rather than
   backpressure onto the 20 ms tick budget;
3. a ``None``/unwarmed session (a cold boot, or a box with no ``[sdk]`` extra)
   falls back to the daemon ``http`` route and STILL never opens a second
   client — so a bare box keeps a working voice;
4. the module docstring no longer states the retired #94 "sdk is unconstructable"
   premise.

Every condition is INJECTED — a fake session, a fake provider, a stubbed
``_import`` for the absent-SDK case. Nothing here sniffs the ambient interpreter
for an installed extra, reaches a socket, or needs a robot.
"""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest

from reachy.behavior import speech_act
from reachy.behavior.speech_act import (
    RUNTIME_DEFAULT_TRANSPORT,
    SPEECH_TRANSPORT_ENV,
    SpeechActuator,
    make_default_play,
    resolve_playback_transport,
)
from reachy.robot.media_client import HeldMediaClient
from reachy.speech import playback as playback_mod

_PCM = (np.zeros(1024, dtype=np.int16)).tobytes()


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSpeaker:
    """A stand-in for the held client's media manager, on the OUTPUT side.

    Mirrors the surface :func:`reachy.speech.playback._play_sdk` actually uses:
    ``get_output_audio_samplerate`` / ``start_playing`` / ``push_audio_sample``.
    Records which thread pushed, so the worker-thread contract is checkable.
    """

    def __init__(self, *, output_samplerate: int = 16000, push_delay: float = 0.0, boom=None):
        self._output_samplerate = output_samplerate
        self._push_delay = push_delay
        self._boom = boom
        self.started = False
        self.push_calls: list = []
        self.push_threads: set = set()

    def get_output_audio_samplerate(self) -> int:
        return self._output_samplerate

    def start_playing(self) -> None:
        self.started = True

    def push_audio_sample(self, data) -> None:
        self.push_threads.add(threading.get_ident())
        if self._boom is not None:
            raise self._boom
        if self._push_delay:
            time.sleep(self._push_delay)
        self.push_calls.append(data)


class _Boom:
    """A stand-in for ``_open_sdk_media`` that fails loudly if ever reached.

    Opening a second media client is the defect this whole task removes, so the
    guard raises rather than merely counting: a regression cannot be silently
    tolerated by a later assertion that forgot to check.
    """

    def __init__(self):
        self.calls = 0

    def __call__(self):
        self.calls += 1
        raise AssertionError(
            "a SECOND SDK media client was opened — the runtime's voice must play "
            "through the ONE held client (spec claim c16)"
        )


class _HttpRecorder:
    """Captures the daemon-route fallback without touching a socket."""

    def __init__(self):
        self.calls: list = []

    def __call__(self, pcm, *, samplerate, base_url, timeout=10.0):
        self.calls.append((pcm, samplerate, base_url))


@pytest.fixture()
def no_second_client(monkeypatch):
    """Make opening a second media client an immediate, loud failure."""
    boom = _Boom()
    monkeypatch.setattr(playback_mod, "_open_sdk_media", boom)
    return boom


@pytest.fixture()
def http_route(monkeypatch):
    """Replace the daemon http leg with a recorder (no network)."""
    recorder = _HttpRecorder()
    monkeypatch.setattr(playback_mod, "_play_http", recorder)
    return recorder


def _drain(actuator, *, timeout=5.0):
    assert actuator.join_idle(timeout=timeout), "speech worker did not drain in time"


# --------------------------------------------------------------------------- #
# Criterion 1 — the sdk path reaches the HELD session, never a second client  #
# --------------------------------------------------------------------------- #


def test_the_play_seam_pushes_through_the_injected_held_session(no_second_client):
    """``make_default_play`` given a provider plays through THAT session."""
    speaker = _FakeSpeaker()
    play = make_default_play(transport="sdk", media_session_provider=lambda: speaker)

    play(_PCM, samplerate=16000)

    assert speaker.started, "the held session was never started for playback"
    assert speaker.push_calls, "no PCM reached the held session"
    assert no_second_client.calls == 0


def test_the_actuator_default_play_reaches_the_held_session(no_second_client):
    """End of the seam: a ``say`` renders into the held client's speaker."""
    speaker = _FakeSpeaker()
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: _PCM,
        samplerate=16000,
        media_session_provider=lambda: speaker,
    )
    try:
        actuator.start()
        assert actuator.say("hello there") is True
        _drain(actuator)
    finally:
        actuator.close()

    assert speaker.push_calls, "the utterance never reached the held session"
    assert actuator.spoken == 1
    assert no_second_client.calls == 0


def test_the_provider_is_resolved_per_utterance_not_captured_once(monkeypatch, no_second_client):
    """A LATE-BOUND provider is what lets composition build the voice FIRST.

    The composition root constructs the actuator before the media client exists
    (deliberate: a malformed ``REACHY_VOICE_ENGINE`` must fail at setup). So the
    seam may not read the session at build time — it must ask the provider again
    for every clip, which is also what lets a mid-run reconnect be picked up.
    """
    first, second = _FakeSpeaker(), _FakeSpeaker()
    sessions = [None, first, second]
    play = make_default_play(transport="sdk", media_session_provider=lambda: sessions.pop(0))

    # 1st clip: the holder is not warm yet — no session, so the daemon route.
    recorder = _HttpRecorder()
    monkeypatch.setattr(playback_mod, "_play_http", recorder)
    play(_PCM, samplerate=16000)
    assert len(recorder.calls) == 1

    play(_PCM, samplerate=16000)  # 2nd: warmed
    play(_PCM, samplerate=16000)  # 3rd: a different session after a reconnect

    assert first.push_calls, "the warmed session was never used"
    assert second.push_calls, "a re-warmed session was not picked up"
    assert len(recorder.calls) == 1, "a warmed session must not fall back"
    assert no_second_client.calls == 0


def test_the_shipped_default_transport_is_the_held_client_sdk_path(monkeypatch):
    """Decision record (t10): the runtime voice defaults to the HELD sdk path.

    c16's honesty condition is that a rule's ``say`` plays "without the daemon
    http route in the path", which is only the runtime's real behaviour if the
    held-client path is the DEFAULT. ``http`` stays one env var away.
    """
    monkeypatch.delenv(SPEECH_TRANSPORT_ENV, raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)

    assert RUNTIME_DEFAULT_TRANSPORT == "sdk"
    assert resolve_playback_transport() == "sdk"

    monkeypatch.setenv(SPEECH_TRANSPORT_ENV, "http")
    assert resolve_playback_transport() == "http", "http must stay env-selectable"


# --------------------------------------------------------------------------- #
# Criterion 2 — worker thread, and no backpressure onto the tick              #
# --------------------------------------------------------------------------- #


def test_the_held_session_push_happens_on_the_worker_thread(no_second_client):
    """The probe's regime: the push runs on the speech worker, not the caller.

    This is the property the live probe (d2) validated against a concurrently
    draining reader — so the test pins the threading contract the injection
    relies on, not merely that audio arrived.
    """
    speaker = _FakeSpeaker()
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: _PCM,
        samplerate=16000,
        media_session_provider=lambda: speaker,
    )
    caller = threading.get_ident()
    try:
        actuator.start()
        worker_ident = actuator.worker.ident
        actuator.say("hello there")
        _drain(actuator)
    finally:
        actuator.close()

    assert speaker.push_threads, "nothing was ever pushed"
    assert caller not in speaker.push_threads, "playback ran on the calling (tick) thread"
    assert speaker.push_threads == {worker_ident}


def test_a_dead_held_session_is_a_named_drop_never_an_exception(no_second_client, caplog):
    """A session that raises on push degrades to silence with a named reason."""
    speaker = _FakeSpeaker(boom=RuntimeError("speaker gone"))
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: _PCM,
        samplerate=16000,
        media_session_provider=lambda: speaker,
    )
    try:
        with caplog.at_level("INFO", logger="reachy.sense"):
            actuator.start()
            assert actuator.say("hello") is True  # accepted; the failure is downstream
            _drain(actuator)
        text = caplog.text
    finally:
        actuator.close()

    assert "stage=speech" in text
    assert "reason=playback-failed" in text
    assert actuator.failures == 1
    assert no_second_client.calls == 0


def test_a_wedged_held_session_never_backpressures_the_tick_thread(no_second_client, caplog):
    """The bounded-queue property still holds with the held session in the path.

    ``say()`` must stay O(1) even while the worker is stuck inside
    ``push_audio_sample``; the overflow is a NAMED ``queue-full`` drop, which is
    the whole reason the hand-off queue is bounded and every put non-blocking.
    """
    speaker = _FakeSpeaker(push_delay=0.4)
    actuator = SpeechActuator(
        synthesize=lambda text, **_kw: _PCM,
        samplerate=16000,
        media_session_provider=lambda: speaker,
        queue_maxsize=1,
    )
    try:
        with caplog.at_level("INFO", logger="reachy.sense"):
            actuator.start()
            started = time.monotonic()
            for _ in range(8):
                actuator.say("hello")
            elapsed = time.monotonic() - started
        text = caplog.text
    finally:
        actuator.close()

    assert elapsed < 0.2, f"say() blocked on the wedged speaker ({elapsed:.3f}s)"
    assert "reason=queue-full" in text
    assert actuator.dropped >= 1
    assert no_second_client.calls == 0


# --------------------------------------------------------------------------- #
# Criterion 3 — a None session falls back, and still opens no second client   #
# --------------------------------------------------------------------------- #


def test_a_none_session_falls_back_to_the_daemon_route(no_second_client, http_route):
    """Decision record (t10): an unwarmed/absent session falls back to ``http``.

    Never to ``_open_sdk_media`` — avoiding that second client is the entire
    point — and never to a bare drop, because the daemon route reaches the SAME
    physical speaker through the shared ALSA sink, so a box whose held client is
    not up (or has no ``[sdk]`` extra at all) keeps a working voice.
    """
    play = make_default_play(
        transport="sdk", base_url="http://box:8000", media_session_provider=lambda: None
    )

    play(_PCM, samplerate=16000)

    assert len(http_route.calls) == 1
    _pcm, samplerate, base_url = http_route.calls[0]
    assert samplerate == 16000
    assert base_url == "http://box:8000"
    assert no_second_client.calls == 0


def test_no_provider_at_all_behaves_like_an_absent_session(no_second_client, http_route):
    """A stand-alone actuator (no composition root) must not open a client either."""
    play = make_default_play(transport="sdk", base_url="http://box:8000")

    play(_PCM, samplerate=16000)

    assert len(http_route.calls) == 1
    assert no_second_client.calls == 0


def test_a_raising_provider_degrades_to_the_fallback(no_second_client, http_route):
    """A broken provider is a degradation, not a lost utterance."""

    def broken():
        raise RuntimeError("holder exploded")

    play = make_default_play(
        transport="sdk", base_url="http://box:8000", media_session_provider=broken
    )

    play(_PCM, samplerate=16000)

    assert len(http_route.calls) == 1
    assert no_second_client.calls == 0


def test_an_explicit_http_transport_never_consults_the_provider(no_second_client, http_route):
    """``REACHY_SPEECH_TRANSPORT=http`` keeps the old route, untouched."""
    asked = []

    def provider():
        asked.append(1)
        return _FakeSpeaker()

    play = make_default_play(
        transport="http", base_url="http://box:8000", media_session_provider=provider
    )

    play(_PCM, samplerate=16000)

    assert asked == [], "the http route must not touch the held client at all"
    assert len(http_route.calls) == 1
    assert no_second_client.calls == 0


@pytest.mark.offline
def test_a_box_without_the_sdk_extra_still_has_a_working_voice(
    monkeypatch, no_second_client, http_route
):
    """Criterion 3's real shape: an absent ``[sdk]`` extra, INJECTED not sniffed.

    The absence is expressed where it actually lives — ``HeldMediaClient._import``
    returning ``None``, the seam the holder's own suite uses — so this pins the
    bare-box regime on CI *and* on a dev box that happens to have the extra
    installed. The voice is the SHIPPED default (in-process harmonic), so the
    whole text -> PCM leg is real with every endpoint unreachable.
    """
    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(lambda: None))
    holder = HeldMediaClient(base_url=None, allow_inline_connect=False)
    actuator = SpeechActuator(
        media_session_provider=lambda: holder.media_session,
        base_url="http://box:8000",
    )
    try:
        assert holder.warm_up() is False, "a bare box must not come up"
        assert holder.media_session is None

        actuator.start()
        assert actuator.say("hello robot") is True
        _drain(actuator)
    finally:
        actuator.close()
        holder.close()

    assert actuator.spoken == 1, "the bare box lost its voice"
    assert len(http_route.calls) == 1
    pcm, samplerate, _base = http_route.calls[0]
    assert samplerate == 16000, "the harmonic engine's rate did not reach playback"
    assert len(pcm) > 1000, "no real PCM — the offline default voice did not render"
    assert no_second_client.calls == 0


# --------------------------------------------------------------------------- #
# Criterion 4 — the docstring no longer carries the retired #94 premise       #
# --------------------------------------------------------------------------- #


def test_the_module_docstring_drops_the_retired_unconstructable_premise():
    """#94 is CLOSED; the docstring must not still justify a default with it.

    The stale text claimed a media-profile ``ReachyMini`` cannot be constructed
    on the deployed robot, which is what made ``http`` look like the only
    possible default. It is measurably false (the runtime warms that very client
    on every boot), and leaving it in place would argue against the injection
    this task ships.
    """
    doc = speech_act.__doc__ or ""
    lowered = doc.lower()

    assert "is dead on the deployed robot" not in lowered
    assert "connectionrefusederror" not in lowered
    assert "a default nothing can currently exercise is not a default" not in lowered
    # And it must positively describe what replaced it.
    assert "held" in lowered, "the docstring never mentions the held media client"
    assert "alsa" in lowered, "the ALSA-sharing fact (t5) is missing"
