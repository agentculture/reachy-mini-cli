"""Composition tests for the runtime's full sense stack + held-client warm-up (t28).

t28 is the keystone of the "retire the old AI-first flow" arc: it makes five
already-built modules actually live by wiring them into the ONE composition root
(``reachy/cli/_commands/behavior.py``'s ``_compose_run_seam``). Two things are
proved here, and they are inseparable:

**The providers.** ``transcript`` (t11), ``rms`` (t12) and ``face`` /
``frame_available`` (t13) each had a declared ``Sense`` field, a declared
``SenseProviders`` slot and a valid ``rules.toml`` predicate name — and nothing
feeding them, so a rule keyed on one validated cleanly and then silently never
fired. These tests drive a rule keyed on each of the four fields end to end
through the composed seam and assert it actually fires.

**The warm-up.** Both held SDK clients (the ``no_media`` pose reader, t27, and
the media client, t10) construct lazily, and construction BLOCKS for
425-1213 ms — measured on the deployed box, on every single runtime start, as a
21x-61x tick-budget overrun immediately after the holder's ``connected`` line
(``docs/verification/2026-07-20-retire-old-flow-baseline.md`` section 3). t27
only made the fix possible by adding ``warm_up()`` / ``allow_inline_connect``;
the fix itself is here, and it is a PAIR: the flag alone (reads never construct)
without the warm-up silently disables the sense, and the warm-up alone leaves the
mid-run-fault door open for the same stall to reappear later.

Nothing here touches a robot, daemon, SDK or network: both holder seams are
injected, and the real holders degrade to "SDK absent" in this environment.
"""

from __future__ import annotations

import contextlib
import threading
import time

import numpy as np
import pytest

from reachy.behavior.engine import EngineConfig
from reachy.behavior.rule_engine import RuleEngine
from reachy.behavior.rules import RulesConfig
from reachy.cli import main
from reachy.cli._commands import behavior as behavior_mod

pytestmark = pytest.mark.offline


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

    @contextlib.contextmanager
    def streaming(self):
        yield self.sink

    def doa(self, timeout=None):
        return None


class _Recorder:
    """An ordered, thread-safe log of composition/runtime events."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.events: list[str] = []

    def add(self, event: str) -> None:
        with self._lock:
            self.events.append(event)

    def index(self, event: str) -> int:
        with self._lock:
            return self.events.index(event)

    def first(self, prefix: str) -> int:
        with self._lock:
            for i, event in enumerate(self.events):
                if event.startswith(prefix):
                    return i
        raise AssertionError(f"no recorded event starts with {prefix!r}: {self.events}")

    def has(self, prefix: str) -> bool:
        with self._lock:
            return any(event.startswith(prefix) for event in self.events)


class _FakePoseReader:
    """A ``HeldStateReader`` stand-in with t27's full shape (warm_up + connected)."""

    def __init__(self, *, recorder: _Recorder | None = None, warms_to: bool = True) -> None:
        self._recorder = recorder
        self._warms_to = warms_to
        self.connected = False
        self.warm_calls = 0
        self.warm_threads: list[int] = []
        self.reads = 0
        self.closed = False

    def warm_up(self) -> bool:
        self.warm_calls += 1
        self.warm_threads.append(threading.get_ident())
        self.connected = self._warms_to
        if self._recorder is not None:
            self._recorder.add("warm:pose")
        return self._warms_to

    def read(self):
        self.reads += 1
        if self._recorder is not None:
            self._recorder.add("read:pose")
        return (0.0, 0.0) if self.connected else None

    def close(self) -> None:
        self.closed = True


class _FakeMedia:
    """A ``HeldMediaClient`` stand-in: audio + frames + the warm-up shape."""

    def __init__(
        self,
        *,
        recorder: _Recorder | None = None,
        chunk=None,
        frame=None,
        camera: bool = True,
        warms_to: bool = True,
    ) -> None:
        self._recorder = recorder
        self._chunk = chunk
        self._frame = frame
        self._camera = camera
        self._warms_to = warms_to
        self.connected = False
        self.warm_calls = 0
        self.warm_threads: list[int] = []
        self.audio_calls = 0
        self.audio_threads: list[int] = []
        self.frame_calls = 0
        self.closed = False
        self.samplerate = 16000
        self.channels = 1

    # -- lifecycle ----------------------------------------------------------
    def warm_up(self) -> bool:
        self.warm_calls += 1
        self.warm_threads.append(threading.get_ident())
        self.connected = self._warms_to
        if self._recorder is not None:
            self._recorder.add("warm:media")
        return self._warms_to

    def close(self) -> None:
        self.closed = True

    # -- reads --------------------------------------------------------------
    def audio(self):
        self.audio_calls += 1
        self.audio_threads.append(threading.get_ident())
        if self._recorder is not None:
            self._recorder.add("read:media")
        if not self.connected:
            return None
        chunk = self._chunk
        return chunk(self.audio_calls) if callable(chunk) else chunk

    def frame(self):
        self.frame_calls += 1
        if not self.connected:
            return None
        return self._frame

    @property
    def camera_available(self) -> bool:
        return self.connected and self._camera


class _StepClock:
    def __init__(self, dt: float = 0.02) -> None:
        self.t = 0.0
        self.dt = dt

    def __call__(self) -> float:
        self.t += self.dt
        return self.t


class _StaticRules:
    """A one-rule real ``RuleEngine`` in the shape ``_compose_run_seam`` expects.

    Mirrors ``cmd_engine_run``'s ``ReloadDriver``: callable as a tick driver, plus
    the ``set_active_mode`` / ``known_modes`` members the intent driver reads.
    """

    def __init__(self, field: str, *, run: str = "pet-reaction") -> None:
        self.field = field
        self._engine = RuleEngine(
            RulesConfig.from_dict(
                {
                    "react": [
                        {
                            "id": f"{field}-probe",
                            "when": {"field": field, "op": "is_true"},
                            "run": run,
                            "cooldown_s": 60.0,
                        }
                    ]
                }
            )
        )

    def __call__(self, ctx) -> None:
        self._engine(ctx)

    def set_active_mode(self, name: str | None) -> None:
        self._engine.set_active_mode(name)

    @staticmethod
    def known_modes() -> tuple[str, ...]:
        return ()


def _rules_driver(field: str, *, run: str = "pet-reaction") -> _StaticRules:
    """A one-rule rules driver keyed on *field* — the real rules path."""
    return _StaticRules(field, run=run)


@pytest.fixture()
def _isolated(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("REACHY_BASE_URL", raising=False)
    monkeypatch.delenv("REACHY_TRANSPORT", raising=False)
    monkeypatch.setattr(
        "reachy.cli._commands.behavior.get_transport", lambda args: _QuietTransport()
    )
    monkeypatch.setattr("time.sleep", lambda *_: None)
    return tmp_path


def _inject_holders(monkeypatch, pose, media) -> None:
    monkeypatch.setattr(behavior_mod, "_make_state_reader", lambda: pose)
    monkeypatch.setattr(behavior_mod, "_make_media_client", lambda: media)


def _run_seam(rules_driver, *, events: list[dict], max_ticks: int, sleep=None, dt: float = 0.02):
    """Compose the REAL run seam and drive it through the real engine loop."""
    from reachy.behavior import engine as engine_mod

    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=True, settle=False)
    sense_reader, tick_seam, resources = behavior_mod._compose_run_seam(
        transport, config, rules_driver, events.append
    )
    try:
        engine_mod.run(
            transport,
            config,
            now=_StepClock(dt=dt),
            sleep=sleep if sleep is not None else (lambda *_: None),
            max_ticks=max_ticks,
            sense=sense_reader,
            tick_seam=tick_seam,
            emit=lambda _event: None,
        )
    finally:
        if resources is not None:
            resources.close()
    return resources


def _fired(events: list[dict], field: str) -> bool:
    return any(
        event.get("type") == "rule.fire" and event.get("field") == field
        for event in events
        if isinstance(event, dict)
    )


# --------------------------------------------------------------------------- #
# 1 + 9. Both holders are warmed during SETUP, before the first tick          #
# --------------------------------------------------------------------------- #


def test_both_holders_are_warmed_before_the_first_tick(_isolated, monkeypatch):
    """Criterion 1/9: the pose reader AND the media client are both constructed
    (warmed) during composition — before a single tick runs.

    This is what actually fixes the measured 425-1213 ms startup overrun: the
    blocking connect is charged to setup, where there is no tick budget, instead
    of to whichever thread reads first (the 50 Hz tick thread). Warming
    *sequentially on a background thread after ticking starts* only relocates the
    stall, so the assertion is specifically about ORDER against the first read.
    """
    recorder = _Recorder()
    pose = _FakePoseReader(recorder=recorder)
    media = _FakeMedia(recorder=recorder, chunk=np.zeros(320, dtype=np.float32))
    _inject_holders(monkeypatch, pose, media)

    rc = main(["behavior", "engine", "run", "--max-ticks", "5", "--json"])
    assert rc == 0

    assert pose.warm_calls >= 1, "the pose reader was never warmed"
    assert media.warm_calls >= 1, "the media client was never warmed"
    # The mic is read every tick, so its first read marks where ticking began.
    first_read = recorder.first("read:")
    assert recorder.index("warm:pose") < first_read, "the pose holder warmed after ticking started"
    assert (
        recorder.index("warm:media") < first_read
    ), "the media holder warmed after ticking started"


def test_both_holders_are_closed_on_shutdown(_isolated, monkeypatch):
    """Both held clients are released at teardown — an unclosed client hangs the
    process at interpreter exit regardless of profile."""
    pose = _FakePoseReader()
    media = _FakeMedia()
    _inject_holders(monkeypatch, pose, media)

    assert main(["behavior", "engine", "run", "--max-ticks", "3", "--json"]) == 0
    assert pose.closed, "the pose reader was not closed"
    assert media.closed, "the media client was not closed"


def test_both_holders_are_closed_even_when_the_engine_loop_raises(_isolated, monkeypatch):
    pose = _FakePoseReader()
    media = _FakeMedia()
    _inject_holders(monkeypatch, pose, media)

    def _boom(*a, **k):
        raise RuntimeError("engine exploded")

    monkeypatch.setattr(behavior_mod, "engine_run", _boom)

    assert main(["behavior", "engine", "run", "--max-ticks", "3", "--json"]) != 0
    assert pose.closed and media.closed


def test_both_holders_are_closed_when_composition_itself_raises(_isolated, monkeypatch):
    """A raise PART-WAY through composition must still release what was opened.

    ``cmd_engine_run`` can only close what it was returned, so a mid-composition
    fault would otherwise strand two held clients — and an unclosed client hangs
    the process at interpreter exit, turning a clean structured failure into a
    wedged unit that ``Restart=on-failure`` never restarts.
    """
    pose = _FakePoseReader()
    media = _FakeMedia()
    _inject_holders(monkeypatch, pose, media)

    def _boom(*_a, **_k):
        raise RuntimeError("goto lane exploded")

    # Fails after BOTH holders and both sense drivers are already constructed.
    monkeypatch.setattr(behavior_mod, "GotoLane", _boom)

    assert main(["behavior", "engine", "run", "--max-ticks", "3", "--json"]) != 0
    assert pose.closed, "a mid-composition fault stranded the pose reader"
    assert media.closed, "a mid-composition fault stranded the media client"


# --------------------------------------------------------------------------- #
# 3 + 6. Reads can never construct: allow_inline_connect=False at BOTH sites   #
# --------------------------------------------------------------------------- #


def test_state_reader_seam_forbids_inline_connect():
    """Criterion 6: ``_make_state_reader`` passes ``allow_inline_connect=False``.

    The flag and the warm-up are a PAIR — the flag alone silently disables the
    pat sense (reads return ``None`` forever), the warm-up alone leaves a mid-run
    fault free to rebuild the client inline and reproduce the stall mid-run.
    """
    reader = behavior_mod._make_state_reader()
    try:
        assert reader._allow_inline_connect is False
    finally:
        reader.close()


def test_media_client_seam_forbids_inline_connect():
    """Criterion 3, the media half of the same pair."""
    media = behavior_mod._make_media_client()
    try:
        assert media._allow_inline_connect is False
    finally:
        media.close()


def test_a_cold_state_reader_never_constructs_from_a_read(monkeypatch):
    """The behavioural consequence of the flag: a read on a cold holder does not
    construct — it just reports "no reading" and leaves warming to the owner."""
    from reachy.robot.state_reader import HeldStateReader

    constructed: list[int] = []

    class _Client:
        def __init__(self, **_kw):
            constructed.append(1)

        def get_current_head_pose(self):
            raise AssertionError("unreachable")

        def close(self):
            pass

    # monkeypatch (not a bare class assignment) so the SDK import seam is
    # restored for every sibling test in this worker.
    monkeypatch.setattr(HeldStateReader, "_import", staticmethod(lambda: _Client))

    reader = behavior_mod._make_state_reader()
    try:
        assert reader.read() is None
        assert constructed == [], "a tick-thread read constructed the SDK client inline"
        assert reader.warm_up() is True
        assert constructed == [1], "warm_up did not construct the client"
    finally:
        reader.close()


# --------------------------------------------------------------------------- #
# 2 + 8. A mid-run drop is recovered OFF the tick thread (the retry policy)    #
# --------------------------------------------------------------------------- #


def test_the_keeper_rewarms_a_dropped_holder_off_the_tick_thread():
    """Criterion 2/8: the composition's declared retry policy is "poll
    ``connected`` off-thread and re-warm", not "accept a dead sense for the run".

    With ``allow_inline_connect=False`` a dropped client stays dead until someone
    re-warms it, and the tick thread is forbidden from doing so. So the owner
    polls the free ``connected`` predicate from a background keeper and re-warms
    there — the holder's own retry backoff throttles the actual attempts.
    """
    pose = _FakePoseReader()
    media = _FakeMedia()
    keeper = behavior_mod._HolderKeeper([("state", pose), ("media", media)], period=0.005)
    tick_thread = threading.get_ident()
    keeper.start()
    try:
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (pose.connected and media.connected):
            time.sleep(0.005)
        assert pose.connected and media.connected, "the keeper never warmed a cold holder"

        # A mid-run drop: the holder goes cold with no read able to fix it.
        pose.connected = False
        media.connected = False
        before = (pose.warm_calls, media.warm_calls)
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and not (pose.connected and media.connected):
            time.sleep(0.005)
        assert pose.connected and media.connected, "a mid-run drop was never recovered"
        assert pose.warm_calls > before[0] and media.warm_calls > before[1]
    finally:
        keeper.stop()

    assert tick_thread not in pose.warm_threads, "re-warm ran on the calling (tick) thread"
    assert tick_thread not in media.warm_threads


def test_the_keeper_does_not_rewarm_a_live_holder():
    """``connected`` is polled precisely so a healthy holder is left alone — the
    keeper must not call ``warm_up`` concurrently with reads for no reason."""
    pose = _FakePoseReader()
    pose.connected = True
    keeper = behavior_mod._HolderKeeper([("state", pose)], period=0.002)
    keeper.start()
    try:
        time.sleep(0.05)
    finally:
        keeper.stop()
    assert pose.warm_calls == 0


def test_the_keeper_survives_a_raising_holder():
    """A holder whose ``connected``/``warm_up`` raises must not kill the keeper."""

    class _Hostile:
        def __init__(self) -> None:
            self.calls = 0

        @property
        def connected(self) -> bool:
            self.calls += 1
            raise RuntimeError("probe exploded")

        def warm_up(self) -> bool:
            raise RuntimeError("warm exploded")

    hostile = _Hostile()
    keeper = behavior_mod._HolderKeeper([("state", hostile)], period=0.002)
    keeper.start()
    try:
        deadline = time.monotonic() + 2.0
        while time.monotonic() < deadline and hostile.calls < 3:
            time.sleep(0.002)
    finally:
        keeper.stop()
    assert hostile.calls >= 3, "the keeper died on the first raising probe"


def test_a_failed_setup_warm_is_not_a_fault_and_the_run_continues(_isolated, monkeypatch):
    """Criterion 8: ``warm_up() -> False`` is the ORDINARY daemon-not-up-yet
    outcome (systemd orders the daemon before the presence unit but does not wait
    for its readiness), never a fault. The run proceeds and the keeper keeps
    trying — accepting a dead sense for the whole run would leave a rebooted
    robot deaf and blind until someone restarted it by hand."""
    pose = _FakePoseReader(warms_to=False)
    media = _FakeMedia(warms_to=False)
    _inject_holders(monkeypatch, pose, media)

    assert main(["behavior", "engine", "run", "--max-ticks", "4", "--json"]) == 0
    assert pose.warm_calls >= 1 and media.warm_calls >= 1
    assert pose.closed and media.closed


def test_the_run_composes_a_started_keeper_over_both_holders(_isolated, monkeypatch):
    """The composed run actually installs the keeper over BOTH holders and stops
    it at teardown (the unit tests above pin its behaviour; this pins the wiring)."""
    built: list[tuple] = []
    real_keeper = behavior_mod._HolderKeeper

    class _SpyKeeper(real_keeper):  # type: ignore[misc,valid-type]
        def __init__(self, holders, **kwargs):
            built.append(tuple(label for label, _holder in holders))
            super().__init__(holders, **kwargs)
            self.started = 0
            self.stopped = 0

        def start(self):
            self.started += 1
            super().start()

        def stop(self):
            self.stopped += 1
            super().stop()

    monkeypatch.setattr(behavior_mod, "_HolderKeeper", _SpyKeeper)
    _inject_holders(monkeypatch, _FakePoseReader(), _FakeMedia())

    assert main(["behavior", "engine", "run", "--max-ticks", "3", "--json"]) == 0
    assert built, "no keeper was composed"
    assert set(built[0]) == {"state", "media"}


# --------------------------------------------------------------------------- #
# 4. All four providers are composed into _compose_run_seam                    #
# --------------------------------------------------------------------------- #


def test_compose_run_seam_wires_every_sense_provider(_isolated, monkeypatch):
    """Criterion 4: ``transcript``, ``rms``, ``face`` and ``frame_available`` are
    all passed a live provider callable by the ONE composition root."""
    wired: list[dict] = []
    real_providers = behavior_mod.SenseProviders

    def _spy(**kwargs):
        wired.append(kwargs)
        return real_providers(**kwargs)

    monkeypatch.setattr(behavior_mod, "SenseProviders", _spy)
    _inject_holders(monkeypatch, _FakePoseReader(), _FakeMedia())

    transport = _QuietTransport()
    config = EngineConfig(compose_hz=50, base_layer=False, settle=False)
    _sense, _seam, resources = behavior_mod._compose_run_seam(transport, config, None, None)
    resources.close()

    assert wired, "SenseProviders was never constructed"
    fields = wired[-1]
    for name in ("rms", "face", "frame_available", "transcript", "pat_event", "pat_state"):
        assert fields.get(name) is not None, f"no provider wired for {name!r}"


def test_only_one_audio_read_happens_per_tick(_isolated, monkeypatch):
    """The rms provider and the transcript driver are two consumers of ONE mic
    read. ``media.audio()`` is a CONSUMING read of the shared single-consumer
    media session, so composition taps it once per tick and fans the chunk out —
    reading it twice would hand each consumer half the audio."""
    media = _FakeMedia(chunk=np.zeros(320, dtype=np.float32))
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    assert main(["behavior", "engine", "run", "--max-ticks", "10", "--json"]) == 0
    assert media.audio_calls == 10, f"expected one mic read per tick, got {media.audio_calls}"


def test_no_sense_work_runs_on_the_tick_thread_that_needs_a_socket(_isolated, monkeypatch):
    """The threading contract at the composition layer: the tick thread reads the
    already-held client and nothing else. Any SDK construction, STT round trip or
    face detection belongs to setup or a worker thread."""
    media = _FakeMedia(chunk=np.zeros(320, dtype=np.float32))
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    assert main(["behavior", "engine", "run", "--max-ticks", "6", "--json"]) == 0
    assert media.warm_threads, "the media client was never warmed"
    # Every per-tick read happened on ONE thread, and warming happened before it.
    assert len(set(media.audio_threads)) == 1


# --------------------------------------------------------------------------- #
# 5. A rule keyed on each newly-fed field fires through the composed seam      #
# --------------------------------------------------------------------------- #


def test_a_rule_keyed_on_rms_fires_through_the_composed_seam(_isolated, monkeypatch):
    """t12's provider, live: a mic chunk becomes ``Sense.rms`` and a rule keyed on
    ``rms`` fires. Before t28 this rule validated cleanly and never fired."""
    media = _FakeMedia(chunk=np.full(320, 0.25, dtype=np.float32))
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    events: list[dict] = []
    _run_seam(_rules_driver("rms"), events=events, max_ticks=6)

    assert _fired(events, "rms"), f"no rule.fire for rms (events={[e.get('type') for e in events]})"


def test_a_rule_keyed_on_frame_available_fires_through_the_composed_seam(_isolated, monkeypatch):
    """t13's condition half, live: a usable camera frame holds
    ``Sense.frame_available`` true and a rule keyed on it fires."""
    frame = np.zeros((8, 8, 3), dtype=np.uint8)
    media = _FakeMedia(frame=frame, camera=True)
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    events: list[dict] = []
    _run_seam(_rules_driver("frame_available"), events=events, max_ticks=6)

    assert _fired(events, "frame_available"), "no rule.fire for frame_available"


def test_a_rule_keyed_on_face_fires_through_the_composed_seam(_isolated, monkeypatch):
    """t13's event half, live: a recognised, named face latches into ``Sense.face``
    for exactly one tick and a rule keyed on ``face`` fires.

    The recognizer pair is faked (opencv is not installed here) and the driver's
    own ``start_worker=False`` seam is used to run its detection step
    synchronously — the threading split itself is covered by
    ``tests/test_behavior_face_sense.py``; what is under test here is that the
    composition root builds the recognizer, injects the ONE media client, and
    wires the provider.
    """
    from reachy.behavior.face_sense import FaceSenseDriver as _RealFaceDriver

    class _Detection:
        embedding = (1.0, 0.0)

    class _Engine:
        def detect(self, _frame):
            return _Detection()

    class _Match:
        name = "ori"

    class _Store:
        def match(self, _embedding):
            return _Match()

    class _PumpedFaceDriver(_RealFaceDriver):
        """The real driver with its worker driven inline by the test's own loop."""

        def __init__(self, **kwargs):
            super().__init__(**{**kwargs, "start_worker": False, "detect_interval": 0.0})

        def __call__(self, ctx):
            super().__call__(ctx)
            self._worker_tick()

    monkeypatch.setattr(behavior_mod, "build_face_recognition", lambda: (_Engine(), _Store()))
    monkeypatch.setattr(behavior_mod, "FaceSenseDriver", _PumpedFaceDriver)
    media = _FakeMedia(frame=np.zeros((8, 8, 3), dtype=np.uint8), camera=True)
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    events: list[dict] = []
    _run_seam(_rules_driver("face"), events=events, max_ticks=10)

    assert _fired(events, "face"), "no rule.fire for face"


def test_a_rule_keyed_on_transcript_fires_through_the_composed_seam(_isolated, monkeypatch):
    """t11's provider, live: an addressed utterance is captured, endpointed,
    transcribed on the worker thread and latched into ``Sense.transcript``, and a
    rule keyed on ``transcript`` fires.

    The STT leg is faked (no network); everything else — the energy VAD, the
    pre-roll ring, endpointing on a pause, the worker handoff, the engagement
    gate's name fast-path — is the real driver running on the real composed seam.
    The tick loop is synchronised to the worker through the driver's own
    ``submitted``/``transcripts`` counters rather than a bare sleep, so the
    handoff is awaited deterministically instead of raced.
    """
    from reachy.behavior.transcript_sense import TranscriptSenseDriver as _RealDriver

    class _FakeTranscriber:
        def transcribe_once(self, *_args, **_kwargs):
            return "hey reachy are you there"

    built: list[_RealDriver] = []

    def _make(**kwargs):
        driver = _RealDriver(**{**kwargs, "transcriber": _FakeTranscriber()})
        built.append(driver)
        return driver

    monkeypatch.setattr(behavior_mod, "TranscriptSenseDriver", _make)

    loud = np.full(320, 0.3, dtype=np.float32)
    quiet = np.zeros(320, dtype=np.float32)
    # 50 ticks of speech (1.0 s, clearing the 0.3 s floor) then silence, which
    # endpoints the utterance after the 0.7 s hold.
    media = _FakeMedia(chunk=lambda n: loud if n <= 50 else quiet)
    _inject_holders(monkeypatch, _FakePoseReader(), media)

    def _await_worker(_seconds):
        """Between ticks, let any submitted utterance finish its round trip."""
        if not built:
            return
        driver = built[0]
        deadline = time.monotonic() + 5.0
        while time.monotonic() < deadline and driver.submitted > driver.transcripts:
            time.sleep(0.001)

    events: list[dict] = []
    _run_seam(_rules_driver("transcript"), events=events, max_ticks=140, sleep=_await_worker)

    assert built, "the composition never built a transcript driver"
    assert built[0].submitted >= 1, "no utterance was endpointed and submitted"
    assert _fired(events, "transcript"), "no rule.fire for transcript"


# --------------------------------------------------------------------------- #
# The linter's declared truth must move with the wiring (t16's contract)       #
# --------------------------------------------------------------------------- #


def test_every_newly_wired_field_is_declared_fed():
    """t16's ``FED_SENSE_FIELDS`` is the ONE declared source of truth
    ``behavior rules check`` reads. Wiring a provider without updating it makes
    the linter lie in the opposite direction — warning that a rule "can never
    fire" when it now can."""
    from reachy.behavior.sense import FED_SENSE_FIELDS

    assert {"rms", "face", "frame_available", "transcript"} <= FED_SENSE_FIELDS


def test_rules_check_no_longer_warns_about_the_newly_fed_fields(_isolated, capsys):
    """The operator-visible half: ``behavior rules check`` stops warning about a
    rule keyed on a now-fed field."""
    from reachy.behavior.rules import overlay_rules_path

    path = overlay_rules_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        "\n".join(
            [
                "[[react]]",
                'id = "hears-words"',
                'when = { field = "transcript", op = "is_true" }',
                'run = "pet-reaction"',
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    assert main(["behavior", "rules", "check", "--json"]) == 0
    out = capsys.readouterr().out
    assert "can never fire" not in out
