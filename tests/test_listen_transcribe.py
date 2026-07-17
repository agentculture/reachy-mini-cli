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

import logging
from dataclasses import fields as _dc_fields

import numpy as np

from reachy.motion.listen_transcribe import TranscribeHook, TranscribeTuning
from reachy.motion.queue import MotionQueue
from reachy.motion.sense_sample import SenseSample

#: Field names of TranscribeTuning — used to split tuning kwargs from seam kwargs
#: at the test helpers below (S107 split: the constructor now takes one grouped
#: ``tuning=`` object instead of nine individual numeric parameters).
_TUNING_FIELDS = {f.name for f in _dc_fields(TranscribeTuning)}


def _pop_tuning(kwargs: dict) -> TranscribeTuning:
    """Pop any TranscribeTuning-field keys out of *kwargs* and build a TranscribeTuning."""
    return TranscribeTuning(**{k: kwargs.pop(k) for k in list(kwargs) if k in _TUNING_FIELDS})


# ---------------------------------------------------------------------------
# Fakes: a recording event buffer + a recording transcriber
# ---------------------------------------------------------------------------


class _RecordingBuffer:
    """A minimal :class:`EventBuffer` look-alike recording fed transcripts."""

    def __init__(self) -> None:
        self.transcripts: list[str] = []
        self.directions: list[str | None] = []

    def feed_transcript(self, text: str, *, direction: str | None = None) -> None:
        self.transcripts.append(text)
        self.directions.append(direction)


class _FakeTranscriber:
    """A stand-in for :class:`~reachy.speech.stt.Transcriber`.

    Records every ``transcribe`` call (so a test can assert how many POSTs would
    have happened) and returns a canned, per-call result list.
    """

    def __init__(self, results: list | None = None) -> None:
        self.calls: list[np.ndarray] = []
        self.once_calls: list[np.ndarray] = []
        self._results = list(results or [])

    def transcribe(self, audio: np.ndarray):
        self.calls.append(audio)
        if self._results:
            return self._results.pop(0)
        return None

    def transcribe_once(self, audio: np.ndarray):
        self.once_calls.append(audio)
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
    tuning = _pop_tuning(kwargs)
    hook = TranscribeHook(provider, buffer=buffer, transcriber=transcriber, tuning=tuning, **kwargs)
    return hook, buffer, transcriber


def _make_driven_hook(*, transcriber=None, buffer=None, **kwargs):
    """A hook reading a *mutable* holder so a test can feed speech then a pause.

    Defaults ``min_utterance_s=0`` so even a small test chunk flushes (tests that
    care about the minimum-duration gate set it explicitly). Returns the holder so
    the test can swap the sample between ticks.
    """
    kwargs.setdefault("min_utterance_s", 0.0)
    holder: dict = {"s": None}
    hook, buf, tr = _make_hook(
        lambda: holder["s"], transcriber=transcriber, buffer=buffer, **kwargs
    )
    return hook, buf, tr, holder


def _utterance(hook, holder, *, t_speech=0.0, t_pause=1.0, doa=10.0, audio=None) -> None:
    """Drive ONE utterance: a speech tick, then a pause tick (> silence_hold) that flushes."""
    chunk = _audio() if audio is None else audio
    holder["s"] = SenseSample(rms=0.1, doa=doa, speech=True, ts=t_speech, audio=chunk)
    hook(object(), MotionQueue(), t_speech, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=doa, speech=False, ts=t_pause, audio=None)
    hook(object(), MotionQueue(), t_pause, {"pitch": 0.0, "yaw": 0.0})


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


def test_utterance_transcribes_once_on_pause_and_feeds() -> None:
    """A whole utterance is transcribed ONCE on the pause and fed (name → engages)."""
    transcriber = _FakeTranscriber(results=["reachy hello there"])  # name → engagement
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)

    # Speech tick: accumulate only — no STT POST mid-utterance.
    holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=_audio())
    hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls == [], "no transcription mid-utterance"
    assert buffer.transcripts == []

    # Pause tick (> silence_hold) → flush the whole utterance once and feed it.
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert len(transcriber.once_calls) == 1, "the pause flushes exactly one POST"
    assert buffer.transcripts == ["reachy hello there"]
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


def test_muted_speech_is_not_accumulated_only_outside_mute() -> None:
    """Speech inside the mute window is dropped; only post-mute speech is transcribed."""
    transcriber = _FakeTranscriber(results=["reachy after the mute"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber, mute_until=lambda: 2.5)

    # Speech while muted (t < 2.5) → discarded, never accumulated.
    holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=1.0, audio=_audio())
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls == []

    # Speech once the mute clears (t >= 2.5), then a pause → flush.
    _utterance(hook, holder, t_speech=3.0, t_pause=4.0)
    assert len(transcriber.once_calls) == 1
    assert buffer.transcripts == ["reachy after the mute"]


def test_transcript_carries_doa_direction() -> None:
    """The hook derives the speaker's direction from the utterance's DoA and feeds it.

    doa=10° (near the left in the 0=left/90=front/180=right convention) → "left".
    """
    transcriber = _FakeTranscriber(results=["reachy hello"])  # name → engages
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)

    _utterance(hook, holder, doa=10.0)

    assert buffer.transcripts == ["reachy hello"]
    assert buffer.directions == ["left"], "the words' DoA direction must be passed through"


def test_transcript_direction_none_when_no_doa() -> None:
    """An utterance with no DoA reading feeds the words with direction=None (plain cue)."""
    transcriber = _FakeTranscriber(results=["reachy no direction"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)

    _utterance(hook, holder, doa=None)

    assert buffer.transcripts == ["reachy no direction"]
    assert buffer.directions == [None]


# ---------------------------------------------------------------------------
# 3b. Engagement gate — clear sentences, addressed by name or ongoing conversation
# ---------------------------------------------------------------------------


def test_name_engages_even_when_idle() -> None:
    """An utterance naming the robot is fed even with no prior conversation."""
    transcriber = _FakeTranscriber(results=["robot what time is it"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)
    assert buffer.transcripts == ["robot what time is it"]


def test_unaddressed_coherent_sentence_is_ignored_when_idle() -> None:
    """A coherent sentence with no name and no ongoing conversation is ignored."""
    transcriber = _FakeTranscriber(results=["the weather is nice today"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)
    assert buffer.transcripts == [], "ambient speech not addressed to the robot is ignored"


def test_name_match_is_whole_word_not_substring() -> None:
    """ "robotic" (contains 'robot' as a substring) must NOT trigger the name gate."""
    transcriber = _FakeTranscriber(results=["the robotic arm is interesting"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)
    assert buffer.transcripts == [], "a substring of the name must not engage when idle"


def test_short_fragment_is_ignored() -> None:
    """A 1-2 word fragment (no name) is below the coherence floor → ignored."""
    transcriber = _FakeTranscriber(results=["uh yeah"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)
    assert buffer.transcripts == [], "a short fragment is not a clear sentence"


def test_coherent_followup_engages_within_conversation_window() -> None:
    """After a name-addressed turn, a coherent follow-up (no name) is accepted in-window."""
    transcriber = _FakeTranscriber(results=["reachy hello", "what is the weather like"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber, engage_window_s=20.0)

    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)  # name → engages, opens window
    _utterance(hook, holder, t_speech=5.0, t_pause=6.0)  # follow-up within 20s → accepted
    assert buffer.transcripts == ["reachy hello", "what is the weather like"]


def test_followup_ignored_after_conversation_window_expires() -> None:
    """A coherent follow-up (no name) after the window expires is ignored again."""
    transcriber = _FakeTranscriber(results=["reachy hello", "what is the weather like"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber, engage_window_s=10.0)

    _utterance(hook, holder, t_speech=0.0, t_pause=1.0)  # engages until ~11.0
    _utterance(hook, holder, t_speech=30.0, t_pause=31.0)  # well past the window → ignored
    assert buffer.transcripts == ["reachy hello"]


def test_too_short_utterance_is_not_transcribed() -> None:
    """An utterance below min_utterance_s never reaches the STT (no POST)."""
    transcriber = _FakeTranscriber(results=["reachy hello there"])
    # Default min_utterance_s=0.3s @16k = 4800 samples; a 1000-sample chunk is too short.
    holder: dict = {"s": None}
    hook, buffer, _t = _make_hook(lambda: holder["s"], transcriber=transcriber, sample_rate=16000)
    holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=_audio(1000))
    hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls == [], "a sub-min-duration blip is not transcribed"
    assert buffer.transcripts == []


# ---------------------------------------------------------------------------
# 4. Empty / None transcript feeds nothing
# ---------------------------------------------------------------------------


def test_none_transcript_feeds_nothing() -> None:
    """A ``None`` transcript (STT failure) feeds nothing even after a flush."""
    transcriber = _FakeTranscriber(results=[None])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)

    _utterance(hook, holder)

    assert len(transcriber.once_calls) == 1, "we still attempt a transcription on the pause"
    assert buffer.transcripts == [], "a None transcript feeds nothing"
    assert hook.transcripts == 0, "a None transcript does not advance the counter"


def test_empty_transcript_feeds_nothing() -> None:
    """An empty-string transcript feeds nothing."""
    transcriber = _FakeTranscriber(results=[""])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)

    _utterance(hook, holder)

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
    """A transcriber that raises on flush is swallowed; the tick returns, no feed."""

    class _BadTranscriber:
        def __init__(self) -> None:
            self.calls = 0

        def transcribe_once(self, audio):  # noqa: ARG002
            self.calls += 1
            raise RuntimeError("STT blew up")

    bad = _BadTranscriber()
    hook, buffer, _t, holder = _make_driven_hook(transcriber=bad)

    # The transcription fault on the flush must not escape the tick.
    _utterance(hook, holder)
    assert bad.calls == 1, "the hook did attempt a transcription on the pause"
    assert buffer.transcripts == [], "a failed transcription feeds nothing"


def test_faulty_feed_degrades_silently() -> None:
    """A buffer whose feed_transcript raises is swallowed; the tick returns."""

    class _BadBuffer(_RecordingBuffer):
        def feed_transcript(self, text, *, direction=None):  # noqa: ARG002
            raise RuntimeError("buffer fault")

    transcriber = _FakeTranscriber(results=["reachy words"])  # name → reaches the feed
    hook, _b, _t, holder = _make_driven_hook(transcriber=transcriber, buffer=_BadBuffer())

    # The feed fault must not escape the tick.
    _utterance(hook, holder)
    assert len(transcriber.once_calls) == 1


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

    transcriber = _FakeTranscriber(results=["reachy hi there"])  # name → engages
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    xport = _ExplodingTransport()

    # Drive a full utterance through a transport whose media_session explodes —
    # proving the hook rides only the provider and never opens a session.
    holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=_audio())
    hook(xport, MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
    hook(xport, MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert buffer.transcripts == ["reachy hi there"]


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


# ---------------------------------------------------------------------------
# 7. Pre-roll ring buffer + measured onset (t3)
# ---------------------------------------------------------------------------


def _sense_lines(caplog) -> str:
    """Join every ``reachy.sense`` [SENSE] log line captured by ``caplog``."""
    return "\n".join(r.getMessage() for r in caplog.records if r.name == "reachy.sense")


def test_preroll_includes_pre_flag_samples() -> None:
    """The measured-onset pre-roll prepends audio captured BEFORE the speech flag.

    The SDK speech flag is polled at ~5 Hz and lags, so the leading words of an
    utterance arrive on ticks where ``sample.speech`` is still False. With the
    rolling ring buffer + measured onset, those pre-flag chunks are still buffered
    when the flag rises and are prepended to the transcribed clip — the leading
    words are no longer lost (the bug this task fixes).
    """
    rate = 1000
    transcriber = _FakeTranscriber(results=["reachy hello there"])
    buffer = _RecordingBuffer()
    holder: dict = {"s": None}
    hook = TranscribeHook(
        lambda: holder["s"],
        buffer=buffer,
        transcriber=transcriber,
        sample_rate=rate,
        tuning=TranscribeTuning(min_utterance_s=0.0, pre_roll_s=2.0),
    )

    pre = np.full(100, 0.3, dtype=np.float32)  # leading words, speech flag still False
    speech = np.full(100, 0.5, dtype=np.float32)  # the flag finally flips here

    # Three PRE-FLAG ticks: audio present + energetic, but speech flag has NOT flipped.
    for ti in (0.0, 0.1, 0.2):
        holder["s"] = SenseSample(rms=0.3, doa=10.0, speech=False, ts=ti, audio=pre)
        hook(object(), MotionQueue(), ti, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls == [], "nothing is transcribed before the flag rises"

    # Speech flag rises: the rising edge measures onset and seeds the pre-roll.
    holder["s"] = SenseSample(rms=0.5, doa=10.0, speech=True, ts=0.3, audio=speech)
    hook(object(), MotionQueue(), 0.3, {"pitch": 0.0, "yaw": 0.0})

    # Pause (> silence_hold) → flush the whole utterance (pre-roll + speech) in one POST.
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.5, audio=None)
    hook(object(), MotionQueue(), 1.5, {"pitch": 0.0, "yaw": 0.0})

    assert len(transcriber.once_calls) == 1, "the pause flushes exactly one POST"
    clip = transcriber.once_calls[0]
    # 3 pre-flag chunks (300 samples) + the speech chunk (100 samples) = 400 samples.
    assert clip.size == 400, "the emitted clip MUST include the pre-flag samples"
    assert np.allclose(clip[:300], 0.3), "the clip starts with the pre-flag lead-in"
    assert np.allclose(clip[300:], 0.5), "and ends with the flagged-speech audio"
    assert buffer.transcripts == ["reachy hello there"]


def test_ring_buffer_fed_every_tick_before_speech_flag() -> None:
    """The ring is fed on non-speech ticks too (before the speech-flag gate)."""
    rate = 1000
    hook = TranscribeHook(lambda: None, buffer=_RecordingBuffer(), sample_rate=rate)
    # Feed straight into the tick path with speech=False — the ring must still grow.
    sample = SenseSample(rms=0.1, doa=None, speech=False, ts=0.0, audio=np.full(100, 0.1, "f4"))
    hook._maybe_transcribe(sample, 0.0)
    hook._maybe_transcribe(sample, 0.1)
    assert hook._ring_samples == 200, "non-speech ticks with audio still feed the ring"


def test_ring_buffer_trimmed_by_total_samples_not_chunk_count() -> None:
    """The pre-roll ring is bounded by TOTAL samples (~ring_seconds), not chunk count."""
    rate = 1000
    hook = TranscribeHook(
        lambda: None,
        buffer=_RecordingBuffer(),
        sample_rate=rate,
        tuning=TranscribeTuning(ring_seconds=1.0),
    )  # 1000-sample cap
    for _ in range(30):  # 30 * 100 = 3000 samples, far over the cap
        hook._push_ring(np.full(100, 0.1, dtype=np.float32))
    assert hook._ring_samples == 1000, "the ring is trimmed to ~ring_seconds of samples"
    assert hook._ring_total == 3000, "the absolute sample counter keeps counting"


def test_onset_measured_skips_leading_silence() -> None:
    """Onset is MEASURED by an RMS scan (10ms windows), not an assumed fixed offset."""
    rate = 1000
    hook = TranscribeHook(
        lambda: None,
        buffer=_RecordingBuffer(),
        sample_rate=rate,
        tuning=TranscribeTuning(onset_threshold=0.02),
    )
    snapshot = np.concatenate(
        [np.zeros(200, dtype=np.float32), np.full(100, 0.3, dtype=np.float32)]
    )
    # The first 10ms (10-sample) window clearing 0.02 RMS begins at sample 200.
    assert hook._measure_onset(snapshot) == 200
    # All-silence → falls back to 0 (pre-roll still applies conservatively).
    assert hook._measure_onset(np.zeros(300, dtype=np.float32)) == 0


def test_preroll_clamped_to_ring_start() -> None:
    """When pre_roll exceeds the buffered audio, the clip clamps to the ring start."""
    rate = 1000
    transcriber = _FakeTranscriber(results=["reachy hi there"])
    holder: dict = {"s": None}
    hook = TranscribeHook(
        lambda: holder["s"],
        buffer=_RecordingBuffer(),
        transcriber=transcriber,
        sample_rate=rate,
        # pre_roll_s=5.0 -> 5000 samples — far more than the buffered audio.
        tuning=TranscribeTuning(min_utterance_s=0.0, pre_roll_s=5.0),
    )
    chunk = np.full(100, 0.3, dtype=np.float32)
    holder["s"] = SenseSample(rms=0.3, doa=10.0, speech=True, ts=0.0, audio=chunk)
    hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
    hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls[0].size == 100, "clip clamps to what is buffered"


def test_preroll_does_not_pad_a_blip_past_the_min_gate() -> None:
    """Pre-roll must NOT let a sub-min-duration blip clear the min-utterance gate.

    The min-utterance gate measures the *speech* samples (excludes pre-roll), so a
    short burst of speech is still dropped even when preceded by ambient audio that
    the ring captured — the gate's original semantics are preserved.
    """
    rate = 16000
    transcriber = _FakeTranscriber(results=["reachy hello there"])
    holder: dict = {"s": None}
    hook = TranscribeHook(
        lambda: holder["s"],
        buffer=_RecordingBuffer(),
        transcriber=transcriber,
        sample_rate=rate,
        tuning=TranscribeTuning(min_utterance_s=0.3),  # 4800 samples
    )
    # 5 pre-flag ambient ticks (energetic) build a big ring, then a tiny speech blip.
    for ti in (0.0, 0.05, 0.1, 0.15, 0.2):
        holder["s"] = SenseSample(rms=0.3, doa=10.0, speech=False, ts=ti, audio=_audio(2000))
        hook(object(), MotionQueue(), ti, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.3, doa=10.0, speech=True, ts=0.25, audio=_audio(1000))
    hook(object(), MotionQueue(), 0.25, {"pitch": 0.0, "yaw": 0.0})
    holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.5, audio=None)
    hook(object(), MotionQueue(), 1.5, {"pitch": 0.0, "yaw": 0.0})
    assert transcriber.once_calls == [], "only 1000 speech samples (< 4800) → dropped"


def test_capture_and_onset_senselog_lines(caplog) -> None:
    """A rising edge emits [SENSE] stage=capture (pre-roll + buffered) + stage=onset."""
    transcriber = _FakeTranscriber(results=["reachy hello there"])
    hook, buffer, _t, holder = _make_driven_hook(transcriber=transcriber)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        _utterance(hook, holder)
    text = _sense_lines(caplog)
    assert "stage=capture" in text
    assert "pre_roll=" in text
    assert "buffered=" in text
    assert "stage=onset" in text
    assert "offset=" in text


def test_min_utterance_drop_emits_senselog_drop(caplog) -> None:
    """A sub-min-duration blip is dropped via ``senselog.drop`` (greppable reason)."""
    transcriber = _FakeTranscriber(results=["reachy hello there"])
    holder: dict = {"s": None}
    hook, _b, _t = _make_hook(lambda: holder["s"], transcriber=transcriber, sample_rate=16000)
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=_audio(1000))
        hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
        holder["s"] = SenseSample(rms=0.0, doa=10.0, speech=False, ts=1.0, audio=None)
        hook(object(), MotionQueue(), 1.0, {"pitch": 0.0, "yaw": 0.0})
    assert "dropped reason=min-utterance" in _sense_lines(caplog)
    assert transcriber.once_calls == []


def test_self_mute_discard_emits_senselog_drop(caplog) -> None:
    """A muted tick mid-utterance discards the partial via ``senselog.drop``."""
    transcriber = _FakeTranscriber(results=["reachy hi"])
    mute = {"until": 0.0}
    holder: dict = {"s": None}
    hook, buffer, _t = _make_hook(
        lambda: holder["s"],
        transcriber=transcriber,
        sample_rate=16000,
        mute_until=lambda: mute["until"],
        min_utterance_s=0.0,
    )
    with caplog.at_level(logging.INFO, logger="reachy.sense"):
        # Accumulate a partial utterance (not muted yet).
        holder["s"] = SenseSample(rms=0.1, doa=10.0, speech=True, ts=0.0, audio=_audio())
        hook(object(), MotionQueue(), 0.0, {"pitch": 0.0, "yaw": 0.0})
        # Robot starts speaking → the next tick falls inside the self-mute window.
        mute["until"] = 100.0
        holder["s"] = SenseSample(rms=0.5, doa=10.0, speech=True, ts=0.1, audio=_audio())
        hook(object(), MotionQueue(), 0.1, {"pitch": 0.0, "yaw": 0.0})
    assert "dropped reason=self-mute" in _sense_lines(caplog)
    assert transcriber.once_calls == [], "a muted mid-utterance discard never transcribes"
    assert buffer.transcripts == []
