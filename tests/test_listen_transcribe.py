"""Tests for folding STT transcription into the ``listen`` loop.

``listen`` already owns the one in-process SDK media session and derives a single
per-tick :class:`~reachy.motion.sense_sample.SenseSample` (DoA / RMS / speech /
raw ``audio``).  :class:`~reachy.motion.listen_transcribe.TranscribeHook` is the
per-tick ``on_tick`` hook that transcribes *that* shared sample's audio and feeds
the recognised WORDS into the *same* ``think`` :class:`~reachy.speech.events.EventBuffer`
the cognition engine consumes — it never opens a second media session (which
would contend for the single-consumer SDK client and throttle to ~1 Hz, see the
single-SDK-owner model in ``CLAUDE.md`` and the #43 ``PatHook`` fold-in).

These tests exercise the seam directly with fakes — no robot, no daemon, no
network, no real STT, no real threads, no real sleeps.  Everything (provider,
transcriber, buffer, self-mute window) is injected.

Coverage (mirrors the acceptance criteria):

1. The hook reads the loop's shared sample via an injected ``SampleProvider``;
   a ``None`` sample is a silent no-op (no transcribe, no feed).
2. It transcribes ONLY when the sample has ``speech`` True AND ``audio`` is not
   ``None`` AND the tick is outside the self-mute window.
3. SELF-MUTE: with an injected ``mute_until`` in the future, the tick discards the
   audio BEFORE transcription — ``transcriber.transcribe`` is called ZERO times.
4. A non-empty transcript is fed via ``feed_transcript`` on the shared buffer; a
   ``None`` / empty transcript feeds nothing.
5. Every step is guarded: a faulty provider / transcriber / feed never propagates
   out of the tick.  ``close()`` is safe + idempotent.
6. The hook never opens a media session (single-SDK-owner invariant).
"""

from __future__ import annotations

import numpy as np

from reachy.motion.listen_transcribe import TranscribeHook
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample

# ---------------------------------------------------------------------------
# Fakes: a recording event buffer + a recording transcriber
# ---------------------------------------------------------------------------


class _RecordingBuffer:
    """A minimal :class:`EventBuffer` look-alike recording fed transcripts."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []

    def feed_transcript(self, text: str) -> None:
        self.transcripts.append(text)


class _FakeTranscriber:
    """A stand-in for :class:`~reachy.speech.stt.Transcriber`.

    Records every ``transcribe`` call (so a test can assert how many POSTs would
    have happened) and returns a canned, per-call result list.
    """

    def __init__(self, results: list | None = None) -> None:
        self.calls: list[np.ndarray] = []
        self._results = list(results or [])

    def transcribe(self, audio: np.ndarray):
        self.calls.append(audio)
        if self._results:
            return self._results.pop(0)
        return None


def _audio(n: int = 256) -> np.ndarray:
    """A non-empty float32 mic chunk."""
    return np.full(n, 0.05, dtype=np.float32)


def _make_hook(provider, **kwargs):
    """Build a TranscribeHook with a recording buffer + transcriber unless given."""
    buffer = kwargs.pop("buffer", None) or _RecordingBuffer()
    transcriber = kwargs.pop("transcriber", None) or _FakeTranscriber()
    hook = TranscribeHook(provider, buffer=buffer, transcriber=transcriber, **kwargs)
    return hook, buffer, transcriber


# ---------------------------------------------------------------------------
# 1. None sample → silent no-op
# ---------------------------------------------------------------------------


def test_none_sample_is_silent_no_op() -> None:
    """A provider returning ``None`` means no transcribe, no feed."""
    hook, buffer, transcriber = _make_hook(lambda: None)

    queue = MotionQueue()
    for i in range(5):
        hook(object(), queue, 0.1 * i, {"pitch": 0.0, "yaw": 0.0})

    assert transcriber.calls == [], "a None sample must not transcribe"
    assert buffer.transcripts == [], "a None sample must feed nothing"
    hook.close()
    assert buffer.transcripts == []


# ---------------------------------------------------------------------------
# 2. Transcribe gate: only speech + audio + not muted
# ---------------------------------------------------------------------------


def test_no_speech_does_not_transcribe() -> None:
    """A sample with ``speech`` False is not transcribed even with audio present."""
    sample = SenseSample(rms=0.04, doa=5.0, speech=False, ts=1.0, audio=_audio())
    hook, buffer, transcriber = _make_hook(lambda: sample)

    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert transcriber.calls == [], "no speech this tick → no transcription"
    assert buffer.transcripts == []


def test_speech_but_no_audio_does_not_transcribe() -> None:
    """A speech sample with ``audio is None`` is not transcribed (nothing to send)."""
    sample = SenseSample(rms=0.08, doa=5.0, speech=True, ts=1.0, audio=None)
    hook, buffer, transcriber = _make_hook(lambda: sample)

    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert transcriber.calls == [], "speech but no audio → no transcription"
    assert buffer.transcripts == []


def test_speech_and_audio_not_muted_transcribes_and_feeds() -> None:
    """speech + audio + not muted → transcribe is called and a hit is fed."""
    audio = _audio()
    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=audio)
    transcriber = _FakeTranscriber(results=["hello there"])
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=transcriber)

    # mute_until defaults to never-muted (0.0), so this tick is eligible.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert len(transcriber.calls) == 1, "an eligible tick must transcribe exactly once"
    # The hook fed the shared sample's raw audio (not a reconstruction).
    assert transcriber.calls[0] is audio
    assert buffer.transcripts == ["hello there"], "a non-empty transcript is fed to cognition"
    # Diagnostics counter advanced.
    assert hook.transcripts == 1


# ---------------------------------------------------------------------------
# 3. Self-mute: inside the mute window → ZERO transcribe calls
# ---------------------------------------------------------------------------


def test_self_mute_window_discards_audio_before_transcription() -> None:
    """Inside the self-mute window the audio is dropped BEFORE STT (zero calls)."""
    audio = _audio()
    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=audio)
    # mute_until in the future relative to the tick's t → muted.
    hook, buffer, transcriber = _make_hook(lambda: sample, mute_until=lambda: 100.0)

    # t == 0.1 < mute_until() == 100.0 → muted; must NOT call transcribe at all.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert transcriber.calls == [], "inside the mute window NO STT POST may happen"
    assert buffer.transcripts == [], "muted tick feeds nothing"


def test_outside_mute_window_transcribes() -> None:
    """Once ``t`` reaches/passes ``mute_until`` the tick transcribes again."""
    audio = _audio()
    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=audio)
    transcriber = _FakeTranscriber(results=["after the mute"])
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=transcriber, mute_until=lambda: 2.5)

    # t below the mute deadline → discarded.
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.calls == []

    # t at/after the mute deadline → eligible.
    hook(object(), MotionQueue(), 3.0, {"pitch": 0.0, "yaw": 0.0})
    assert len(transcriber.calls) == 1
    assert buffer.transcripts == ["after the mute"]


# ---------------------------------------------------------------------------
# 4. Empty / None transcript feeds nothing
# ---------------------------------------------------------------------------


def test_none_transcript_feeds_nothing() -> None:
    """A ``None`` transcript (not enough audio / throttled) feeds nothing."""
    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=_audio())
    transcriber = _FakeTranscriber(results=[None])
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=transcriber)

    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert len(transcriber.calls) == 1, "we still attempt a transcription"
    assert buffer.transcripts == [], "a None transcript feeds nothing"
    assert hook.transcripts == 0, "a None transcript does not advance the counter"


def test_empty_transcript_feeds_nothing() -> None:
    """An empty-string transcript feeds nothing."""
    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=_audio())
    transcriber = _FakeTranscriber(results=[""])
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=transcriber)

    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})

    assert buffer.transcripts == [], "an empty transcript feeds nothing"


# ---------------------------------------------------------------------------
# 5. on_tick signature + silent degradation + close()
# ---------------------------------------------------------------------------


def test_on_tick_signature_matches_pat_hook() -> None:
    """TranscribeHook.__call__ accepts (transport, queue, t, commanded_head)."""
    hook, _b, _t = _make_hook(lambda: None)
    queue = MotionQueue()
    # Positional, exactly like HookChain forwards to PatHook.
    hook(object(), queue, 0.5, {"pitch": 1.0, "yaw": 2.0})
    # commanded_head is optional (the seam may omit it) — must not raise.
    hook(object(), queue, 0.6)


def test_faulty_provider_degrades_silently() -> None:
    """A provider that raises must not propagate out of the tick (loop survives)."""

    def _boom():
        raise RuntimeError("sensor blew up")

    hook, buffer, transcriber = _make_hook(_boom)
    # Must NOT raise — the loop must never die from a hook fault.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.calls == [], "a faulty provider must not transcribe"
    assert buffer.transcripts == []


def test_faulty_transcriber_degrades_silently() -> None:
    """A transcriber that raises is swallowed; the tick returns, no feed."""

    class _BadTranscriber:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe(self, audio):  # noqa: ARG002
            self.calls += 1
            raise RuntimeError("STT blew up")

    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=_audio())
    bad = _BadTranscriber()
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=bad)

    # The transcription fault must not escape the tick.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert bad.calls == 1, "the hook did attempt a transcription"
    assert buffer.transcripts == [], "a failed transcription feeds nothing"


def test_faulty_feed_degrades_silently() -> None:
    """A buffer whose feed_transcript raises is swallowed; the tick returns."""

    class _BadBuffer(_RecordingBuffer):
        def feed_transcript(self, text):  # noqa: ARG002
            raise RuntimeError("buffer fault")

    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=_audio())
    transcriber = _FakeTranscriber(results=["words"])
    hook, _b, _t = _make_hook(lambda: sample, buffer=_BadBuffer(), transcriber=transcriber)

    # The feed fault must not escape the tick.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert len(transcriber.calls) == 1


def test_close_is_idempotent() -> None:
    """close() is safe to call repeatedly / when never fired."""
    hook, _b, _t = _make_hook(lambda: None)
    hook.close()
    hook.close()  # second close must be a safe no-op


# ---------------------------------------------------------------------------
# 6. The hook never opens a media session (single-SDK-owner invariant)
# ---------------------------------------------------------------------------


def test_hook_never_opens_a_media_session() -> None:
    """The hook reads cues ONLY via the provider — never transport.media_session."""

    class _ExplodingTransport:
        name = "sdk"

        def media_session(self):  # pragma: no cover - must never be called
            raise AssertionError("TranscribeHook must NOT open a media session")

        def head_pose(self):  # pragma: no cover
            raise AssertionError("TranscribeHook must not read head_pose either")

    sample = SenseSample(rms=0.09, doa=10.0, speech=True, ts=1.0, audio=_audio())
    transcriber = _FakeTranscriber(results=["hi"])
    hook, buffer, _t = _make_hook(lambda: sample, transcriber=transcriber)

    # Passing a transport whose media_session explodes proves it is never called.
    hook(_ExplodingTransport(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert buffer.transcripts == ["hi"]


def test_module_does_not_import_reachy_mini_or_media_session() -> None:
    """Static guard: the module's *code* must not call media_session / build ReachyMini.

    Prose (docstrings/comments) is allowed to *name* these to explain what the hook
    deliberately does NOT do, so we walk the executable AST and assert no
    ``ReachyMini`` name, no ``.media_session`` attribute access, and no
    ``reachy_mini`` import.
    """
    import ast
    import inspect

    import reachy.motion.listen_transcribe as mod

    tree = ast.parse(inspect.getsource(mod))
    names = {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    attrs = {a.attr for a in ast.walk(tree) if isinstance(a, ast.Attribute)}
    aliases = {
        alias.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.Import, ast.ImportFrom))
        for alias in node.names
    }
    assert "media_session" not in attrs, "TranscribeHook must not call media_session"
    assert "ReachyMini" not in names, "TranscribeHook must not reference a ReachyMini client"
    assert not any(
        "reachy_mini" in a for a in aliases
    ), "TranscribeHook must not import reachy_mini"


# ---------------------------------------------------------------------------
# Default construction (real Transcriber) — no network on construction
# ---------------------------------------------------------------------------


def test_default_transcriber_is_constructed_without_network() -> None:
    """Omitting ``transcriber`` builds a real :class:`Transcriber` (no I/O on init)."""
    hook = TranscribeHook(lambda: None, buffer=_RecordingBuffer())
    # The default mute_until never mutes.
    assert hook is not None
    # A None-sample tick is still a no-op even with the real transcriber wired.
    hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    hook.close()


def test_sample_rate_threads_into_default_transcriber() -> None:
    """The session's real mic rate must label the WAV sent to STT.

    A WAV header that lies about the rate makes STT mis-decode (the gap
    live-testing probed for); the hook builds its default Transcriber with the
    real ``session.samplerate``, mirroring ``sleep``'s wake-word STT. An explicit
    ``transcriber`` still wins; omitting the rate keeps the 16 kHz default.
    """
    rated = TranscribeHook(lambda: None, buffer=_RecordingBuffer(), sample_rate=48000)
    assert rated._transcriber._sample_rate == 48000

    default = TranscribeHook(lambda: None, buffer=_RecordingBuffer())
    assert default._transcriber._sample_rate == 16000

    explicit = _FakeTranscriber(results=[])
    won = TranscribeHook(
        lambda: None, buffer=_RecordingBuffer(), transcriber=explicit, sample_rate=48000
    )
    assert won._transcriber is explicit
