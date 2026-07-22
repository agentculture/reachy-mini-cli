"""Realtime hearing, composed into the runtime seam (task t5).

Tasks t1-t4 built the wire, the fake server, the session client and the driver
that consumes it — but t4 deliberately left the driver's ``realtime`` argument
uninjected, so the deployed runtime was DEAF and said so once
(``no-realtime-session``). This file pins the composition step that closes that
gap, and pins it where it can actually go wrong:

1. **The audio hand-off is the ONE fanned-out chunk.** The streamer receives the
   per-tick chunk :class:`~reachy.cli._commands.behavior._AudioTap` already
   swapped off the pump — it never opens a second ``media.audio()`` read (#100).
   Asserted structurally over the AST, in the repo's existing idiom
   (``tests/test_zero_llm_boundary.py``), because the defect it prevents is a
   refactor that "just reads the mic where it needs it".
2. **The session config carries the mic's REAL rate.** ``input_sample_rate``
   rides the connect URL and the server resamples from it, so a hard-coded 16000
   against a 48 kHz mic mis-times every VAD decision. Proved end to end against
   :class:`tests.fake_realtime_server.FakeRealtimeServer`, which reports the rate
   it was handed on the handshake — plus the fallback path when a cold media
   holder cannot report one, which must be ANNOUNCED, not silent.
3. **The session is released at shutdown.** It owns a socket and a worker
   thread; an unclosed one leaks the thread and can hang the process at
   interpreter exit — the same failure class the two held SDK clients have.
4. **A bare box still runs.** No ``[sdk]`` extra, no gateway: composition is
   unconditional, the transcript field stays permanently ``None``, and the
   down state is announced ONCE, not fifty times a second (#99).
5. **Startup stays off the tick thread.** Construction and ``start()`` happen
   during composition, before the first tick — the same discipline that removed
   the measured 425-1213 ms startup overruns (21x-61x over a 20 ms budget).
   Asserted structurally *and* by observation, never by a timing threshold that
   would flake under ``-n auto``.
"""

from __future__ import annotations

import ast
import inspect
import logging
import socket
import threading
import time
from pathlib import Path

import pytest

from reachy.behavior.engine import EngineConfig
from reachy.cli._commands import behavior as behavior_mod
from tests.fake_realtime_server import FakeRealtimeServer, Scenario

_REPO_ROOT = Path(__file__).resolve().parent.parent
_BEHAVIOR_MODULE = _REPO_ROOT / "reachy" / "cli" / "_commands" / "behavior.py"

#: How long a bounded wait may spend on a background thread before failing.
_TIMEOUT = 5.0


# --------------------------------------------------------------------------- #
# Fakes                                                                       #
# --------------------------------------------------------------------------- #


class _FakeSink:
    def set_target(self, *a, **k):
        return {"ok": True}


class _QuietTransport:
    """A fake transport whose DoA route has no reading (a mic-less box)."""

    name = "fake"

    def __init__(self):
        self.sink = _FakeSink()

    def streaming(self):
        from contextlib import nullcontext

        return nullcontext(self.sink)

    def doa(self, timeout=None):
        return None


class _FakeMedia:
    """A held-media-client stand-in: warms, reports a rate, hands out no audio.

    Deliberately silent (``audio()`` -> ``None``): every claim in this file is
    about CONFIGURATION and LIFECYCLE, and audio content is covered end to end by
    ``tests/test_behavior_transcript_realtime.py``. ``samplerate`` is the one
    interesting knob — ``None`` models a cold holder that the daemon has not
    brought up yet.
    """

    def __init__(self, samplerate: int | None = 16000) -> None:
        self.samplerate = samplerate
        self.channels = 1
        self.camera_available = False
        self.connected = False
        self.closed = False
        self.warm_calls = 0

    def warm_up(self) -> bool:
        self.warm_calls += 1
        self.connected = True
        return True

    def audio(self):
        return None

    def frame(self):
        return None

    def close(self) -> None:
        self.closed = True


class _RecordingSession:
    """A ``RealtimeTranscriber`` stand-in recording its lifecycle calls.

    Duck-types the whole surface composition and the driver touch: the two O(1)
    tick methods, the rate push, and the two lifecycle calls this file is about.
    """

    def __init__(self, sample_rate: int) -> None:
        self.sample_rate = sample_rate
        self.start_calls = 0
        self.close_calls = 0
        self.chunks: list[object] = []
        self.rates: list[int] = []
        #: How many ticks had already run when ``start()`` was first called,
        #: and which thread called it — the two observations that say "setup
        #: paid for this, not the 20 ms tick".
        self.ticks_at_start: int | None = None
        self.start_thread: int | None = None
        self.ticks = 0

    def start(self) -> None:
        if self.ticks_at_start is None:
            self.ticks_at_start = self.ticks
            self.start_thread = threading.get_ident()
        self.start_calls += 1

    def close(self) -> None:
        self.close_calls += 1

    def submit_audio(self, audio) -> bool:
        self.chunks.append(audio)
        return True

    def take_utterance(self):
        return None

    def set_sample_rate(self, rate: int) -> None:
        self.rates.append(int(rate))
        self.sample_rate = int(rate)


# --------------------------------------------------------------------------- #
# Helpers                                                                     #
# --------------------------------------------------------------------------- #


def _dead_port() -> int:
    """An ephemeral port nothing listens on — connect is refused, immediately."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = int(sock.getsockname()[1])
    sock.close()
    return port


def _wait_for(predicate, *, timeout: float = _TIMEOUT) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


def _compose(monkeypatch, *, media: _FakeMedia, session_factory=None):
    """Run the real ``_compose_run_seam`` with fake hardware; return its triple."""
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")  # no held pose reader to fake
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: media)
    if session_factory is not None:
        monkeypatch.setattr(behavior_mod, "_make_realtime_client", session_factory)
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    return behavior_mod._compose_run_seam(_QuietTransport(), config, None, None)


@pytest.fixture()
def _state_dir(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    return tmp_path


# --------------------------------------------------------------------------- #
# AST helpers                                                                 #
# --------------------------------------------------------------------------- #


def _module_tree(path: Path) -> ast.Module:
    return ast.parse(path.read_text(encoding="utf-8"))


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == name:
            return node
    raise AssertionError(f"{name}() not found — this scan is blind")


def _calls(node: ast.AST, name: str) -> list[ast.Call]:
    """Every call to a bare name or an attribute ending in *name*."""
    found: list[ast.Call] = []
    for child in ast.walk(node):
        if not isinstance(child, ast.Call):
            continue
        func = child.func
        if (isinstance(func, ast.Name) and func.id == name) or (
            isinstance(func, ast.Attribute) and func.attr == name
        ):
            found.append(child)
    return found


def _kwarg(call: ast.Call, name: str) -> ast.AST | None:
    for keyword in call.keywords:
        if keyword.arg == name:
            return keyword.value
    return None


def _audio_call_sites() -> set[str]:
    """Every ``<something>.audio()`` CALL in ``reachy/``, as repo-relative paths.

    Attribute calls only: an ``audio()`` method DEFINITION (``_AudioTap.audio``)
    is not a read, and neither is a callable passed by reference
    (``audio_tap.audio`` handed to the rms providers).
    """
    sites: set[str] = set()
    for path in sorted((_REPO_ROOT / "reachy").rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Attribute)
                and node.func.attr == "audio"
                and not node.args
                and not node.keywords
            ):
                sites.add(str(path.relative_to(_REPO_ROOT)))
    return sites


# --------------------------------------------------------------------------- #
# 1 — the audio hand-off: ONE consuming read, fanned out                      #
# --------------------------------------------------------------------------- #


#: Pinned by EQUALITY so the suite fails in BOTH directions: a new module that
#: reads the mic fails it, and so does one of these two losing its read.
_EXPECTED_AUDIO_READERS = {
    # The ONE background thread that owns every media.audio() call (#100).
    "reachy/behavior/audio_pump.py",
    # Reads its INJECTED source once per tick — which composition binds to the
    # _AudioTap, i.e. the pump's already-taken chunk, never a second SDK read.
    "reachy/behavior/transcript_sense.py",
}


def test_the_audio_pump_is_still_the_only_thing_that_reads_the_mic() -> None:
    """Criterion: wiring the streamer added no second ``media.audio()`` consumer."""
    assert _audio_call_sites() == _EXPECTED_AUDIO_READERS, (
        "the set of modules calling `.audio()` changed. The AudioPump is the ONE "
        "owner of mic acquisition (#100); the transcript driver reads the "
        "injected _AudioTap. A new reader here means two consumers of a "
        "CONSUMING read, and each would get half the audio."
    )


def test_the_streamer_receives_the_very_chunk_the_tap_fanned_out() -> None:
    """The behavioural half of criterion 1: ONE take, two consumers, same audio.

    ``AudioPump.take()`` is a CONSUMING swap. The sense reader takes it once at
    the top of the tick (what the rms providers read), and the transcript driver
    — which runs at the END of the same tick — must see that identical chunk via
    the tap's non-consuming peek. A second take here would hand each consumer
    half the audio, which is exactly what streaming to a server-side VAD must
    never do: the gaps would read as endpoints.
    """
    from types import SimpleNamespace

    import numpy as np

    from reachy.behavior.sense import Sense
    from reachy.behavior.transcript_sense import TranscriptSenseDriver

    chunk = np.linspace(-0.5, 0.5, 64, dtype=np.float32)

    class _Pump:
        def __init__(self) -> None:
            self.takes = 0

        def take(self):
            self.takes += 1
            return chunk if self.takes == 1 else None

    pump = _Pump()
    tap = behavior_mod._AudioTap(pump, _FakeMedia(samplerate=48000))
    session = _RecordingSession(48000)
    driver = TranscriptSenseDriver(media=tap, realtime=session)
    try:
        tap.pull(0.0)  # the sense reader's ONE swap, at the top of the tick
        assert tap.audio() is chunk, "the rms provider's view is not the pump's chunk"
        driver(SimpleNamespace(now=0.0, tick=0, sense=Sense()))  # end of the SAME tick
        assert pump.takes == 1, "the tick consumed the pump's latch twice"
        assert len(session.chunks) == 1, f"the streamer got {len(session.chunks)} chunks, not 1"
        np.testing.assert_allclose(session.chunks[0], chunk)
        # And the real rate reached the session off that same first read.
        assert session.rates == [48000]
    finally:
        driver.close()


def test_the_session_client_never_reads_audio_itself() -> None:
    """The streamer is HANDED chunks; it owns no source and no media import."""
    client = _REPO_ROOT / "reachy" / "speech" / "realtime.py"
    tree = ast.parse(client.read_text(encoding="utf-8"))
    assert not _calls(tree, "audio"), "the session client now reads audio itself"
    imported: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module)
    assert not {name for name in imported if "media" in name or name == "reachy_mini"}


def test_composition_hands_the_streamer_the_tap_not_a_second_mic_read() -> None:
    """The wiring itself, read off ``_compose_run_seam``'s AST.

    Three facts, and their JOINT truth is the criterion: the pump wraps the ONE
    media client, the tap wraps that pump, and the transcript driver — the only
    thing that calls ``submit_audio`` — is given the TAP as its media source.
    Swap that last argument back to ``media`` and the driver would open a second
    consuming read against the holder, which is exactly the #100 defect.
    """
    seam = _function(_module_tree(_BEHAVIOR_MODULE), "_compose_run_seam")

    pumps = _calls(seam, "AudioPump")
    taps = _calls(seam, "_AudioTap")
    drivers = _calls(seam, "TranscriptSenseDriver")
    assert len(pumps) == len(taps) == len(drivers) == 1, (
        "expected exactly one AudioPump, one _AudioTap and one "
        f"TranscriptSenseDriver in the seam (got {len(pumps)}/{len(taps)}/{len(drivers)})"
    )

    media_name = pumps[0].args[0].id  # AudioPump(media)
    tap_args = [arg.id for arg in taps[0].args if isinstance(arg, ast.Name)]
    assert media_name in tap_args, f"_AudioTap does not wrap {media_name!r}"

    driver_media = _kwarg(drivers[0], "media")
    assert isinstance(driver_media, ast.Name), "the driver's media source is no longer a name"
    assert driver_media.id != media_name, (
        "the transcript driver is reading the held media client directly again — "
        "that is a SECOND consuming mic read (#100); it must take the _AudioTap."
    )

    realtime = _kwarg(drivers[0], "realtime")
    assert isinstance(realtime, ast.Name), (
        "the transcript driver is composed without a `realtime=` session client — "
        "the runtime would be deaf and log one `no-realtime-session` drop."
    )


# --------------------------------------------------------------------------- #
# 2 — the session config carries the mic's REAL rate                          #
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("reported", "expected"),
    [
        pytest.param(48000, 48000, id="real-48k"),
        pytest.param(16000, 16000, id="real-16k"),
        pytest.param(None, behavior_mod.DEFAULT_MIC_SAMPLE_RATE, id="cold-holder-none"),
        pytest.param(0, behavior_mod.DEFAULT_MIC_SAMPLE_RATE, id="zero"),
        pytest.param("nonsense", behavior_mod.DEFAULT_MIC_SAMPLE_RATE, id="unparseable"),
    ],
)
def test_mic_sample_rate_resolution(reported, expected) -> None:
    assert behavior_mod._mic_sample_rate(_FakeMedia(samplerate=reported)) == expected


def test_a_raising_samplerate_probe_is_the_fallback_not_a_crash() -> None:
    class _Raising:
        @property
        def samplerate(self):
            raise RuntimeError("holder exploded")

    assert behavior_mod._mic_sample_rate(_Raising()) == behavior_mod.DEFAULT_MIC_SAMPLE_RATE


def test_an_unknown_mic_rate_is_announced_on_both_channels(caplog) -> None:
    """The fallback must never be silent — see ``DEFAULT_MIC_SAMPLE_RATE``.

    A session quietly declaring 16 kHz for a 48 kHz mic mis-times every
    server-side VAD decision, and presents to an operator as "hearing is just
    bad". So it says so twice: once for a human (the module logger) and once for
    the journal (a named ``senselog`` drop).
    """
    with caplog.at_level(logging.INFO):
        assert behavior_mod._mic_sample_rate(_FakeMedia(samplerate=None)) == 16000
    text = caplog.text
    assert "mic sample rate unknown" in text
    assert "mic-rate-unknown" in text
    assert "stage=warmup" in text and "source=realtime" in text


def test_a_known_mic_rate_is_carried_into_the_constructed_session(_state_dir, monkeypatch) -> None:
    """48 kHz in, 48 kHz configured — not the 16000 the client refuses to default."""
    made: list[_RecordingSession] = []

    def factory(sample_rate: int) -> _RecordingSession:
        session = _RecordingSession(sample_rate)
        made.append(session)
        return session

    _, _, resources = _compose(
        monkeypatch, media=_FakeMedia(samplerate=48000), session_factory=factory
    )
    try:
        assert [s.sample_rate for s in made] == [48000]
    finally:
        resources.close()


def test_a_cold_media_holder_falls_back_to_the_documented_default(
    _state_dir, monkeypatch, caplog
) -> None:
    made: list[_RecordingSession] = []

    def factory(sample_rate: int) -> _RecordingSession:
        session = _RecordingSession(sample_rate)
        made.append(session)
        return session

    with caplog.at_level(logging.INFO):
        _, _, resources = _compose(
            monkeypatch, media=_FakeMedia(samplerate=None), session_factory=factory
        )
    try:
        assert [s.sample_rate for s in made] == [behavior_mod.DEFAULT_MIC_SAMPLE_RATE]
        assert "mic-rate-unknown" in caplog.text
    finally:
        resources.close()


def test_the_rate_reaches_the_wire_on_the_handshake(_state_dir, monkeypatch) -> None:
    """End to end through the PRODUCTION factory: the server sees 48000.

    The rate rides the connect URL's ``input_sample_rate`` query — there is no
    follow-up ``session.update`` on this wire — so this is the only place the
    server can learn it, and the fake server reports what it was handed.
    """
    with FakeRealtimeServer(Scenario.HAPPY_PATH) as server:
        monkeypatch.setenv("REACHY_REALTIME_URL", server.url)
        _, _, resources = _compose(monkeypatch, media=_FakeMedia(samplerate=48000))
        try:
            assert _wait_for(lambda: server.last_input_sample_rate is not None), (
                "the composed session never reached the server "
                f"(connections={server.connections_accepted})"
            )
            assert server.last_input_sample_rate == 48000
        finally:
            resources.close()


# --------------------------------------------------------------------------- #
# 3 — the session is released at shutdown                                     #
# --------------------------------------------------------------------------- #


def test_runtime_resources_closes_the_session_client() -> None:
    session = _RecordingSession(16000)
    behavior_mod._RuntimeResources(realtime=session).close()
    assert session.close_calls == 1


def test_a_raising_session_close_does_not_skip_the_remaining_teardown() -> None:
    """One failing teardown step must not strand a held SDK client."""

    class _Boom:
        def close(self):
            raise RuntimeError("session close exploded")

    media = _FakeMedia()
    behavior_mod._RuntimeResources(media=media, realtime=_Boom()).close()
    assert media.closed, "the media client was skipped because the session raised"


def test_composition_registers_the_session_for_teardown(_state_dir, monkeypatch) -> None:
    made: list[_RecordingSession] = []
    _, _, resources = _compose(
        monkeypatch,
        media=_FakeMedia(),
        session_factory=lambda rate: made.append(_RecordingSession(rate)) or made[-1],
    )
    assert resources.realtime is made[0]
    resources.close()
    assert made[0].close_calls == 1
    resources.close()  # idempotent
    assert made[0].close_calls == 1


def test_a_session_that_never_connected_leaves_no_thread_behind(_state_dir, monkeypatch) -> None:
    """Criterion 3's real teeth: a REAL client against a dead port, then close.

    An unclosed session leaks its worker thread and can hang the process at
    interpreter exit — the same failure class the two held SDK clients have.
    ``threading.active_count()`` must return to the pre-composition baseline.
    """
    monkeypatch.setenv("REACHY_REALTIME_URL", f"ws://127.0.0.1:{_dead_port()}/v1/realtime")
    baseline = threading.active_count()

    _, _, resources = _compose(monkeypatch, media=_FakeMedia())
    assert resources.realtime is not None
    assert _wait_for(lambda: threading.active_count() > baseline), "composition spawned no worker"
    resources.close()

    assert _wait_for(lambda: threading.active_count() <= baseline), (
        "a thread outlived the runtime teardown "
        f"(baseline={baseline}, now={threading.active_count()}, "
        f"alive={[t.name for t in threading.enumerate()]})"
    )


# --------------------------------------------------------------------------- #
# 4 — a bare box: unconditional, quiet, and latched                           #
# --------------------------------------------------------------------------- #


def test_a_bare_box_composes_the_session_and_stays_quiet(_state_dir, monkeypatch, caplog) -> None:
    """No ``[sdk]`` extra, no gateway: it runs, hears nothing, complains ONCE.

    Two claims in one run, because they are one behaviour: the composition is
    unconditional (a session client exists even though nothing about this box
    can hear), and the resulting down state is a LATCHED transition — one
    ``session-down`` line for the whole run, not one per failed attempt and
    certainly not one per 20 ms tick (#99).

    The client is REAL (only its backoff is tightened, so several attempts fit
    in a bounded wait); the latch under test is its own.
    """
    from reachy.robot.media_client import HeldMediaClient
    from reachy.robot.state_reader import HeldStateReader
    from reachy.speech.realtime import RealtimeTranscriber

    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(lambda: None))
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: None))
    url = f"ws://127.0.0.1:{_dead_port()}/v1/realtime"
    made: list[RealtimeTranscriber] = []

    def factory(sample_rate: int) -> RealtimeTranscriber:
        client = RealtimeTranscriber(
            sample_rate=sample_rate, url=url, backoff_initial_s=0.01, backoff_max_s=0.02
        )
        made.append(client)
        return client

    monkeypatch.setattr(behavior_mod, "_make_media_client", HeldMediaClient)
    monkeypatch.setenv("REACHY_PAT_SENSE", "0")
    monkeypatch.setattr(behavior_mod, "_make_realtime_client", factory)

    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    with caplog.at_level(logging.INFO):
        sense_reader, _seam, resources = behavior_mod._compose_run_seam(
            _QuietTransport(), config, None, None
        )
        try:
            client = made[0]
            assert _wait_for(lambda: client.connect_failures >= 4), (
                f"the dead gateway was retried only {client.connect_failures} times; "
                "the latch claim below would be vacuous"
            )
            for tick in range(10):
                assert sense_reader(tick * 0.02).transcript is None
        finally:
            resources.close()

    downs = [line for line in caplog.text.splitlines() if "reason=session-down" in line]
    assert len(downs) == 1, (
        f"{len(downs)} session-down lines for {client.connect_failures} failed attempts — "
        "the down state must be a LATCHED transition, not a per-attempt complaint (#99)"
    )


def test_the_full_cli_run_still_exits_clean_with_no_gateway(
    _state_dir, monkeypatch, capsys
) -> None:
    """The whole ``behavior engine run`` path, bare box, unreachable gateway.

    The transcript field itself is asserted at the seam above — the runtime feed
    does not carry it — so what this adds is the end-to-end shape: the CLI still
    exits 0, perception still publishes, and the export feed on stdout stays
    pure JSONL (the session client logs to stderr, never into the feed).
    """
    import json

    from reachy.cli import main
    from reachy.robot.media_client import HeldMediaClient
    from reachy.robot.state_reader import HeldStateReader

    monkeypatch.setattr(HeldMediaClient, "_import", staticmethod(lambda: None))
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: None))
    monkeypatch.setattr(behavior_mod, "get_transport", lambda args: _QuietTransport())
    monkeypatch.setenv("REACHY_REALTIME_URL", f"ws://127.0.0.1:{_dead_port()}/v1/realtime")
    monkeypatch.setattr("time.sleep", lambda *_: None)

    rc = main(["behavior", "engine", "run", "--max-ticks", "8", "--export", "-"])
    assert rc == 0

    captured = capsys.readouterr()
    blocks = [json.loads(line) for line in captured.out.splitlines() if line.strip()]
    assert [b for b in blocks if b["t"] == "sense"], "perception stopped publishing"
    assert "Traceback" not in captured.err, captured.err


# --------------------------------------------------------------------------- #
# 5 — startup stays off the tick thread                                       #
# --------------------------------------------------------------------------- #


def test_the_session_is_started_during_composition_before_any_tick(_state_dir, monkeypatch) -> None:
    """Observed, not timed: ``start()`` has already happened when the seam returns.

    A timing threshold would be the flaky way to say this under ``-n auto``. The
    honest way is that the tick counter the fake keeps is still zero when
    ``start()`` lands — i.e. no tick could have paid for it.
    """
    made: list[_RecordingSession] = []

    def factory(sample_rate: int) -> _RecordingSession:
        session = _RecordingSession(sample_rate)
        made.append(session)
        return session

    sense_reader, _seam, resources = _compose(
        monkeypatch, media=_FakeMedia(), session_factory=factory
    )
    try:
        session = made[0]
        assert session.start_calls == 1, "the session was not started at composition"
        assert session.ticks_at_start == 0, "start() landed after ticking had begun"
        assert session.start_thread == threading.get_ident(), (
            "the session was started off the composing thread — the whole point "
            "is that SETUP pays for it, on the thread that has no 20 ms budget"
        )

        # And running ticks does not start it again: a composition that re-armed
        # the session per tick would be the same defect wearing a loop.
        for tick in range(5):
            session.ticks += 1
            sense_reader(tick * 0.02)
        assert session.start_calls == 1
    finally:
        resources.close()


def test_no_session_startup_is_reachable_from_the_tick_seam() -> None:
    """Structural half: ``start()`` is called in the seam BODY, never in a tick path.

    ``sense_reader`` is the one closure ``_compose_run_seam`` hands back to run
    on the 20 ms tick; a ``start()`` (or a constructor call) inside it would put
    thread creation and a blocking connect straight onto the budget — the defect
    class that produced the deployed box's measured 425-1213 ms overruns.
    """
    seam = _function(_module_tree(_BEHAVIOR_MODULE), "_compose_run_seam")
    nested = {node.name for node in ast.walk(seam) if isinstance(node, ast.FunctionDef)} - {
        "_compose_run_seam"
    }
    assert "sense_reader" in nested, "the tick closure was renamed — this scan is blind"

    tick_closure = _function(seam, "sense_reader")
    for banned in ("start", "_make_realtime_client"):
        assert not _calls(tick_closure, banned), (
            f"sense_reader() now calls {banned}() — session startup belongs to "
            "composition, not to the 20 ms tick"
        )

    # ... and positively: composition DOES build and start it, exactly once.
    built = _calls(seam, "_make_realtime_client")
    assert len(built) == 1, f"expected one _make_realtime_client() call, found {len(built)}"


def test_the_factory_is_a_bare_constructor_that_bakes_the_rate_into_the_url() -> None:
    """The seam itself: no env parsing here, and the rate reaches the connect URL.

    Constructed but never ``start()``ed, so this test spawns no thread and opens
    no socket.
    """
    client = behavior_mod._make_realtime_client(48000)
    assert "input_sample_rate=48000" in client.connect_url
    assert client.sample_rate == 48000
    assert client.worker is None, "the factory must not start the worker; composition does"
    assert "RealtimeTranscriber(" in inspect.getsource(behavior_mod._make_realtime_client)
