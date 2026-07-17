"""Single-session composition proof for ``listen run --live`` (task t11).

By the time task t10 landed, ``listen run --live --transcribe --cognition agent``
folds SEVEN things onto one ``on_tick`` seam (:class:`~reachy.motion.listen_hooks.HookChain`):
``TranscribeHook`` (STT pre-roll), ``PatHook``, ``VisionHook`` (the one frame
grabber), ``FaceHook`` (shared frame provider, detection worker), ``SceneHook``
(shared frame provider, describe worker), the folded ``ThinkHook`` (cognition —
marker or agent engine), and ``SleepHook``. The single-SDK-owner invariant
(``CLAUDE.md``) says this whole tree must still look, from the hardware's point of
view, like exactly ONE media session and ONE frame grabber, with every heavy
worker (SFace detect, VLM describe) running off the ~20 Hz tick thread on a
bounded-join background thread.

This suite drives the REAL CLI composition path (``listen run --live ...``
through :func:`reachy.cli.main`) against a fake sdk transport — mirroring the
harnesses already established in ``tests/test_listen_cognition_agent.py`` — and
proves, from the outside, that composition still obeys that invariant:

1. Exactly ONE media session is opened for the loop's lifetime, however many
   sense hooks fold in (test A).
2. No folded hook reads the mic itself — every audio-consuming hook rides the
   loop's ONE shared :class:`~reachy.motion.sense_sample.SenseSample` tap; the
   ``get_audio_sample`` call count equals the tick count, never more (test B).
3. ``FaceHook`` and ``SceneHook`` share ``VisionHook``'s ONE frame provider —
   object identity, not two grabbers (test C; ``[vision]``/cv2-gated).
4. ``PatHook``, ``VisionHook``, ``FaceHook``, ``SceneHook``, ``TranscribeHook``,
   and the think engine (behind ``ThinkHook``) all consume the exact SAME
   :class:`~reachy.speech.events.EventBuffer` object — one shared cognition
   sink, not six independent ones (test D; ``[vision]``/cv2-gated).
5. A deliberately HUNG detect/describe seam (blocked on a ``threading.Event``
   that is never set) never blocks the tick path or an unbounded ``close()`` —
   the whole CLI call still returns promptly, and any worker thread ``close()``
   could not reap is a daemon, so it can never block interpreter shutdown
   (test E; ``[vision]``/cv2-gated).

No robot, no daemon, no network, no real LLM/STT/TTS/VLM/cv2 model: the LLM turn
functions are patched to safe fakes, and the fake sdk session never triggers a
real STT/VLM call (no speech, and the describe/detect seams under test are
patched fakes).
"""

from __future__ import annotations

import contextlib
import io
import sys
import threading
import time

import numpy as np
import pytest

import reachy.motion.pat_signal as ps
import reachy.motion.sleep_signal as ss
import reachy.speech.cognition_signal as cs
from reachy.cli import main
from reachy.motion.listen_face import FaceHook
from reachy.motion.listen_pat import PatHook
from reachy.motion.listen_scene import SceneHook
from reachy.motion.listen_think import ThinkHook
from reachy.motion.listen_transcribe import TranscribeHook
from reachy.motion.listen_vision import VisionHook
from reachy.speech.llm import TurnResult

_FRAME = np.zeros((8, 8, 3), dtype=np.uint8)

# ---------------------------------------------------------------------------
# Isolation: pin flags into a throwaway state dir, no env leakage, and — crucially
# — patch the LLM turn functions so no background cognition worker can ever
# network (mirrors tests/test_listen_cognition_agent.py's fixture exactly).
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate(monkeypatch, tmp_path):
    monkeypatch.setenv("REACHY_STATE_DIR", str(tmp_path))
    monkeypatch.delenv("XDG_STATE_HOME", raising=False)
    monkeypatch.delenv("REACHY_COGNITION", raising=False)
    monkeypatch.delenv("REACHY_VOICE_ENGINE", raising=False)
    monkeypatch.delenv("REACHY_ENGAGE_HEURISTIC", raising=False)
    monkeypatch.setattr("time.sleep", lambda *_: None)

    def _fake_turn(messages, **kwargs):  # noqa: ARG001
        return TurnResult(content="", tool_calls=[])

    # The agent engine's default turn_fn (resolved at construction).
    monkeypatch.setattr("reachy.speech.llm.stream_turn", _fake_turn)
    # The marker engine's default streamer — a no-op iterator, so a marker
    # worker (built as a fallback if agent construction ever failed) never
    # networks either.
    monkeypatch.setattr("reachy.speech.llm.stream_sentences", lambda *a, **k: iter(()))

    for sig in (ps, ss, cs):
        sig.clear()
    yield
    for sig in (ps, ss, cs):
        sig.clear()


# ---------------------------------------------------------------------------
# A minimal fake sdk media session + transport (mirrors tests/test_listen_live.py
# and tests/test_listen_cognition_agent.py).
# ---------------------------------------------------------------------------


class _Session:
    """The ONE open client for the loop: audio + DoA + pose + move + frame."""

    _SAMPLE = np.full(512, 0.001, dtype=np.float32)  # below min_rms -> no snap

    def __init__(self, *, frame: object | None = None):
        self._frame = frame
        #: Every real call this session's get_audio_sample received — the crux
        #: of test B: only the loop's own per-tick tap may ever call this.
        self.get_audio_sample_calls = 0

    def doa(self, *, timeout=None):  # noqa: ARG002
        return {"angle": np.pi / 2, "speech_detected": False}  # front, no speech

    def get_audio_sample(self):
        self.get_audio_sample_calls += 1
        return self._SAMPLE

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)  # flat: no pat

    def get_frame(self):
        return self._frame

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    @property
    def samplerate(self):
        return 16000

    @property
    def channels(self):
        return 1


class _LiveSdkTransport:
    """A fake sdk transport with one open media session (counts opens)."""

    name = "sdk-live"

    def __init__(self, *, frame: object | None = None):
        self.media_opens = 0
        self._session = _Session(frame=frame)

    def head_pose(self) -> tuple[float, float]:
        return (0.0, 0.0)

    def get_frame(self):
        return self._session._frame

    def move_goto(self, *, head=None, antennas=None, body_yaw=None, duration, interpolation):
        return {"uuid": "fake"}

    @contextlib.contextmanager
    def media_session(self):
        self.media_opens += 1
        yield self._session


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _run_capture(monkeypatch, argv, *, transport):
    """Run ``reachy <argv>`` against ``transport``; return (rc, stdout, stderr)."""
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _a: transport)
    out, err = io.StringIO(), io.StringIO()
    old_out, old_err = sys.stdout, sys.stderr
    sys.stdout, sys.stderr = out, err
    try:
        rc = main(argv)
    finally:
        sys.stdout, sys.stderr = old_out, old_err
    return rc, out.getvalue(), err.getvalue()


def _live_argv(*extra: str) -> list[str]:
    return [
        "listen",
        "run",
        "--live",
        "--transport",
        "sdk",
        "--deadband",
        "0",
        "--idle-energy",
        "0",
        "--max-ticks",
        "2",
        *extra,
    ]


def _spy_init(monkeypatch, cls):
    """Capture every ``cls(...)`` construction's args/kwargs, and the instance.

    Generic across the six hook classes under test: some take their sample
    provider / queue positionally, all thread ``buffer=`` (and, for the two
    vision-frame consumers, ``frame_provider=``) as a keyword — see each
    class's ``_build_*`` call site in ``reachy/cli/_commands/listen.py``.
    """
    captured: dict = {"kwargs": None, "args": None, "instance": None}
    real_init = cls.__init__

    def _init(self, *a, **kw):
        captured["args"] = a
        captured["kwargs"] = kw
        captured["instance"] = self
        return real_init(self, *a, **kw)

    monkeypatch.setattr(cls, "__init__", _init)
    return captured


# ---------------------------------------------------------------------------
# A. Exactly ONE media session, however many sense hooks fold in.
# ---------------------------------------------------------------------------


def test_exactly_one_media_session_for_the_full_live_composition(monkeypatch) -> None:
    """The loop's WHOLE lifetime opens exactly one media session.

    Runs the heaviest composition (``--live --transcribe --cognition agent``,
    which additionally folds vision/face/scene when the ``[vision]`` extra is
    importable) end to end and counts session opens on the fake transport.
    """
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--transcribe", "--max-ticks", "5"),
        transport=transport,
    )

    assert rc == 0
    assert transport.media_opens == 1, "the whole live composition must open exactly ONE session"


# ---------------------------------------------------------------------------
# B. Every audio-consuming hook rides the shared SenseSample tap — none of them
#    reads the mic itself.
# ---------------------------------------------------------------------------


def test_only_the_loops_own_tap_calls_get_audio_sample(monkeypatch) -> None:
    """``get_audio_sample`` is called exactly once per tick — never more.

    The loop's own ``_audio`` closure (``reachy/cli/_commands/listen.py``,
    ``_run_sdk_loop``) is the ONLY caller of ``session.get_audio_sample()``; every
    audio-consuming hook (``ThinkHook``, ``SleepHook``, ``TranscribeHook``) reads
    the shared per-tick :class:`~reachy.motion.sense_sample.SenseSample` instead
    (via ``holder.provider``). If any hook opened a second read, the call count
    would exceed the tick count — this proves it does not, across the full
    ``--transcribe --cognition agent`` composition (the mode that folds the most
    audio consumers).
    """
    ticks = 7
    transport = _LiveSdkTransport()

    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--transcribe", "--max-ticks", str(ticks)),
        transport=transport,
    )

    assert rc == 0
    assert transport._session.get_audio_sample_calls == ticks, (
        "get_audio_sample must be called exactly once per tick — by the loop's own "
        "tap, and by nothing else"
    )


# ---------------------------------------------------------------------------
# C. FaceHook and SceneHook share VisionHook's ONE frame provider (no second
#    grabber). Gated on cv2: FaceHook/SceneHook are only built when the
#    [vision] extra is importable — see _build_face_hook / _build_scene_hook.
# ---------------------------------------------------------------------------


def test_face_and_scene_hooks_share_visionhooks_one_frame_provider(monkeypatch) -> None:
    pytest.importorskip("cv2")
    vision_captured = _spy_init(monkeypatch, VisionHook)
    face_captured = _spy_init(monkeypatch, FaceHook)
    scene_captured = _spy_init(monkeypatch, SceneHook)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch, _live_argv("--cognition", "agent", "--max-ticks", "3"), transport=transport
    )
    assert rc == 0

    vision_instance = vision_captured["instance"]
    assert vision_instance is not None, "VisionHook must have been constructed"

    face_provider = face_captured["kwargs"].get("frame_provider")
    scene_provider = scene_captured["kwargs"].get("frame_provider")
    assert face_provider is not None, "FaceHook must receive a frame_provider"
    assert scene_provider is not None, "SceneHook must receive a frame_provider"

    # Object identity: both providers are bound methods of the SAME VisionHook
    # instance — the ONE grabber, never a second one.
    assert (
        getattr(face_provider, "__self__", None) is vision_instance
    ), "FaceHook must reuse VisionHook's own grabber, not open a second one"
    assert (
        getattr(scene_provider, "__self__", None) is vision_instance
    ), "SceneHook must reuse VisionHook's own grabber, not open a second one"
    assert face_provider == vision_instance.latest_frame
    assert scene_provider == vision_instance.latest_frame


# ---------------------------------------------------------------------------
# D. PatHook, VisionHook, FaceHook, SceneHook, TranscribeHook, and the think
#    engine (behind ThinkHook) all share the exact SAME EventBuffer object.
# ---------------------------------------------------------------------------


def test_all_six_folded_sense_hooks_share_one_event_buffer(monkeypatch) -> None:
    """One identity assertion across all six live-cognition consumers.

    Individual pairwise proofs already exist elsewhere (PatHook<->ThinkHook,
    VisionHook<->ThinkHook, FaceHook<->VisionHook, SceneHook<->VisionHook, in
    ``tests/test_listen_cognition_agent.py``); this test proves the full set at
    once — the composition layer builds exactly ONE cognition EventBuffer and
    threads the SAME object into every consumer, never six independent ones.
    """
    pytest.importorskip("cv2")
    pat = _spy_init(monkeypatch, PatHook)
    vision = _spy_init(monkeypatch, VisionHook)
    face = _spy_init(monkeypatch, FaceHook)
    scene = _spy_init(monkeypatch, SceneHook)
    transcribe = _spy_init(monkeypatch, TranscribeHook)
    think = _spy_init(monkeypatch, ThinkHook)

    transport = _LiveSdkTransport()
    rc, _out, _err = _run_capture(
        monkeypatch,
        _live_argv("--cognition", "agent", "--transcribe", "--max-ticks", "3"),
        transport=transport,
    )
    assert rc == 0

    buffers = {
        "pat": pat["kwargs"].get("buffer"),
        "vision": vision["kwargs"].get("buffer"),
        "face": face["kwargs"].get("buffer"),
        "scene": scene["kwargs"].get("buffer"),
        "transcribe": transcribe["kwargs"].get("buffer"),
        "think": think["kwargs"].get("buffer"),
    }
    shared = buffers["pat"]
    assert shared is not None, "PatHook must receive a shared cognition buffer under --live"
    assert all(
        buf is shared for buf in buffers.values()
    ), f"all six hooks must share the exact same EventBuffer object: {buffers}"


# ---------------------------------------------------------------------------
# E. Background-worker proof: a HUNG detect/describe seam never blocks the
#    tick path, and close() returns within its bounded join timeouts. Any
#    worker thread close() could not reap must be a daemon (never blocks
#    interpreter shutdown).
# ---------------------------------------------------------------------------


def test_hung_face_and_scene_workers_never_block_tick_or_close(monkeypatch) -> None:
    """A permanently-blocked ``FaceEngine.detect`` / ``describe_frame`` seam.

    ``FaceHook`` / ``SceneHook`` each run their heavy call (SFace detect / VLM
    describe) on a bounded-join background worker specifically so a hang there
    can never freeze the ~20 Hz tick or an orderly shutdown (see the module
    docstrings of ``reachy/motion/listen_face.py`` /
    ``reachy/motion/listen_scene.py``). This test injects a seam that blocks
    forever (a ``threading.Event`` that is never set within the test) and
    proves, from the *outside* — driving the real CLI entry point on a
    background thread with a generous bounded join as a safety net — that:

    * the whole CLI call (N ticks + the loop's ``finally: close()``) still
      returns promptly, well inside its bounded join timeouts, and
    * any worker thread ``close()`` could not reap (because it is genuinely
      stuck inside the hung call) is a daemon thread, so it can never block
      interpreter shutdown even though it was never joined.
    """
    pytest.importorskip("cv2")
    block = threading.Event()  # deliberately never set during the test body
    face_entered = threading.Event()  # set the instant the hung seam is reached
    scene_entered = threading.Event()

    class _HungFaceEngine:
        def detect(self, frame):  # noqa: ARG002
            face_entered.set()
            block.wait()
            return None

    def _hung_describe(frame):  # noqa: ARG002
        scene_entered.set()
        block.wait()
        return ""

    monkeypatch.setattr("reachy.vision.face.FaceEngine", _HungFaceEngine)
    monkeypatch.setattr("reachy.vision.scene.describe_frame", _hung_describe)

    # A REAL (non-None) frame so the grabber publishes something for FaceHook /
    # SceneHook's workers to pick up and hang on — a None frame would make both
    # workers no-ops, proving nothing about the hang path.
    transport = _LiveSdkTransport(frame=_FRAME)
    monkeypatch.setattr("reachy.cli._commands.listen.get_transport", lambda _a: transport)

    before_idents = {t.ident for t in threading.enumerate()}
    result: dict = {}

    def _target() -> None:
        start = time.monotonic()
        out, err = io.StringIO(), io.StringIO()
        old_out, old_err = sys.stdout, sys.stderr
        sys.stdout, sys.stderr = out, err
        try:
            # Generous tick count: real wall-clock runway (the loop's own inter-tick
            # pacing) for the background grabber + worker threads to actually get
            # scheduled and reach the hung call at least once — after which they stay
            # hung for the rest of the run regardless of tick count.
            result["rc"] = main(_live_argv("--cognition", "agent", "--max-ticks", "40"))
        except BaseException as exc:  # noqa: BLE001 — captured for the assertion, not swallowed
            result["exc"] = exc
        finally:
            sys.stdout, sys.stderr = old_out, old_err
        result["elapsed"] = time.monotonic() - start

    # Run the CLI call on its own thread with a generous bounded join: this is a
    # test-harness safety net, not part of the invariant under test — if the
    # composition ever regressed to an unbounded join, this test fails cleanly
    # instead of hanging the whole suite forever.
    runner = threading.Thread(target=_target, name="cli-under-test", daemon=True)
    runner.start()
    runner.join(timeout=15.0)

    try:
        assert not runner.is_alive(), "the CLI call must return even with hung detect/describe"
        assert "exc" not in result, result.get("exc")
        assert result.get("rc") == 0, result
        assert result["elapsed"] < 10.0, (
            "the tick path plus the bounded close() must stay prompt despite two "
            f"permanently-hung workers (took {result.get('elapsed')}s)"
        )
        assert face_entered.is_set() and scene_entered.is_set(), (
            "the hung detect/describe seams were never reached — the background "
            "workers never got a frame in time"
        )

        # The hung face-worker / scene-worker threads could not be joined (they are
        # genuinely still blocked in block.wait()) — but close() must give up on
        # them within its bounded timeout rather than waiting forever, and they
        # must be daemons so they can never block interpreter shutdown.
        leaked = [t for t in threading.enumerate() if t.ident not in before_idents]
        assert leaked, "expected the hung face/scene worker threads to still be alive"
        daemon_flags = [(t.name, t.daemon) for t in leaked]
        assert all(
            t.daemon for t in leaked
        ), f"every un-joinable worker must be a daemon: {daemon_flags}"
    finally:
        # Release the hung workers so they don't linger past this test.
        block.set()
        for t in list(threading.enumerate()):
            if t.ident not in before_idents:
                t.join(timeout=2.0)
