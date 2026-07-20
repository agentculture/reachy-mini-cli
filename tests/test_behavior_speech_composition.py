"""The speech actuator wired into ``behavior engine run``'s composition (task t6).

The unit tests in ``test_behavior_speech_act.py`` prove the actuator; the ones
in ``test_behavior_speech_rules.py`` prove ``say`` reaches a speech seam. These
prove ``reachy.cli._commands.behavior._compose_run_seam`` actually stitches the
two together on the real CLI path:

1. an actuator is built, started, and handed to the rules driver;
2. it is owned by ``_RuntimeResources`` and therefore ``close()``d at shutdown;
3. its self-mute window is wired into the transcript driver, so the runtime
   cannot hear — and answer — its own voice;
4. end to end: a ``rules.toml`` carrying ``say`` makes the composed runtime
   render audio, off the tick thread, with no robot and no network.
"""

from __future__ import annotations

import contextlib
import threading

import pytest

from reachy.cli import main

pytestmark = pytest.mark.offline


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _SpeechTransport:
    """A fake transport that always hears speech (fires a speech-keyed rule)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return {"angle": 0.2, "speech_detected": True}


class _RecordingPlay:
    """A stand-in speaker: records what it was handed, and on which thread."""

    def __init__(self):
        self.clips: list[tuple[bytes, int]] = []
        self.threads: set[int] = set()

    def __call__(self, pcm, *, samplerate):
        self.threads.add(threading.get_ident())
        self.clips.append((pcm, samplerate))


SAY_RULE = "\n".join(
    [
        "[[react]]",
        'id = "greet-on-sound"',
        'run = "speak"',
        "duration_s = 1.5",
        "cooldown_s = 0.0",
        'say = "hello there"',
        "[react.when]",
        'field = "speech"',
        'op = "is_true"',
    ]
)


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.delenv("REACHY_SPEECH_TRANSPORT", raising=False)
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _SpeechTransport()
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _write_rules(state_dir, body: str):
    path = state_dir / "behavior" / "rules.toml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(body, encoding="utf-8")
    return path


# --------------------------------------------------------------------------- #
# 1 + 2 — the actuator is composed, wired, and released                       #
# --------------------------------------------------------------------------- #


def test_compose_run_seam_builds_starts_and_owns_a_speech_actuator(_isolated, monkeypatch):
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod

    built: list = []
    real_factory = behavior_mod._make_speech_actuator

    def factory():
        actuator = real_factory()
        actuator._play = lambda pcm, *, samplerate: None
        built.append(actuator)
        return actuator

    monkeypatch.setattr(behavior_mod, "_make_speech_actuator", factory)

    _, _, resources = behavior_mod._compose_run_seam(
        _SpeechTransport(), EngineConfig(compose_hz=50), None, None
    )
    assert len(built) == 1, "composition did not build a speech actuator"
    actuator = built[0]
    assert (
        actuator.worker is not None and actuator.worker.is_alive()
    ), "the worker must be started at SETUP, so no tick ever pays for thread creation"

    resources.close()
    assert not actuator.worker.is_alive(), "the actuator was not closed at shutdown"


def test_the_speech_actuator_is_wired_into_the_rules_driver(_isolated, monkeypatch):
    """A rules driver composed by the CLI can actually speak."""
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod

    _write_rules(_isolated, SAY_RULE)
    rules_driver = behavior_mod._boot_tick_seam()
    assert rules_driver is not None

    spoken: list[str] = []
    monkeypatch.setattr(
        behavior_mod,
        "_make_speech_actuator",
        lambda: type(
            "_Stub",
            (),
            {
                "say": staticmethod(spoken.append),
                "start": lambda self: None,
                "close": lambda self: None,
                "mute_until": staticmethod(lambda: 0.0),
            },
        )(),
    )
    _, _, resources = behavior_mod._compose_run_seam(
        _SpeechTransport(), EngineConfig(compose_hz=50), rules_driver, None
    )
    try:
        assert rules_driver._speech is not None
        rules_driver._engine._speech("wired")
        assert spoken == ["wired"]
    finally:
        resources.close()


def test_the_actuator_self_mute_is_wired_into_the_transcript_driver(_isolated, monkeypatch):
    """The robot must not transcribe its own voice and answer itself."""
    from reachy.behavior.engine import EngineConfig
    from reachy.cli._commands import behavior as behavior_mod

    built: list = []
    real_factory = behavior_mod._make_speech_actuator

    def factory():
        actuator = real_factory()
        actuator._play = lambda pcm, *, samplerate: None
        built.append(actuator)
        return actuator

    monkeypatch.setattr(behavior_mod, "_make_speech_actuator", factory)

    captured: list = []
    real_driver = behavior_mod.TranscriptSenseDriver

    def spy(**kwargs):
        captured.append(kwargs)
        return real_driver(**kwargs)

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", spy)

    _, _, resources = behavior_mod._compose_run_seam(
        _SpeechTransport(), EngineConfig(compose_hz=50), None, None
    )
    try:
        assert captured, "the transcript driver was not composed"
        mute_until = captured[0].get("mute_until")
        assert mute_until is not None, "transcript driver composed with no self-mute seam"
        assert mute_until == built[0].mute_until
    finally:
        resources.close()


# --------------------------------------------------------------------------- #
# 4 — end to end: a rules.toml `say` makes the runtime render audio           #
# --------------------------------------------------------------------------- #


def test_a_say_rule_makes_the_composed_runtime_speak_off_the_tick_thread(
    _isolated, monkeypatch, capsys
):
    """The whole arc in one run: rules.toml -> rule fires -> actuator -> PCM.

    The voice is the SHIPPED default (in-process harmonic) under the ``offline``
    marker, so this also re-proves criterion 4 through the real CLI: no endpoint
    is reachable and the robot still produces sound. Only the speaker itself is
    a stand-in — it also records which thread rendered, so an actuator that
    regressed onto the tick thread fails here too.
    """
    from reachy.behavior.speech_act import SpeechActuator
    from reachy.cli._commands import behavior as behavior_mod

    _write_rules(_isolated, SAY_RULE)
    speaker = _RecordingPlay()
    actuators: list[SpeechActuator] = []

    def factory():
        actuator = SpeechActuator(play=speaker)
        actuators.append(actuator)
        return actuator

    monkeypatch.setattr(behavior_mod, "_make_speech_actuator", factory)

    caller = threading.get_ident()
    rc = main(["behavior", "engine", "run", "--max-ticks", "6"])
    assert rc == 0

    assert actuators, "no actuator was composed"
    assert speaker.clips, "the say rule fired but nothing was ever rendered"
    pcm, samplerate = speaker.clips[0]
    assert samplerate == 16000  # the harmonic engine's rate
    assert len(pcm) > 1000, "no real PCM — the default offline voice did not render"
    assert caller not in speaker.threads, "rendering happened on the tick/caller thread"
    assert actuators[0].spoken >= 1
