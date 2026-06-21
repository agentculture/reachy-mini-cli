# Build Plan — Reachy Mini's live thinking now hears words: listen run --live can optionally transcribe nearby speech via model-gear's Parakeet STT and feed the actual words into cognition, so the robot reasons about WHAT was said, not just that a sound came from the left

slug: `reachy-mini-s-live-thinking-now-hears-words-listen` · status: `exported` · from frame: `reachy-mini-s-live-thinking-now-hears-words-listen`

> Reachy Mini's live thinking now hears words: listen run --live can optionally transcribe nearby speech via model-gear's Parakeet STT and feed the actual words into cognition, so the robot reasons about WHAT was said, not just that a sound came from the left

## Tasks

### t1 — Shared STT transcriber: factor the Parakeet /v1/audio/transcriptions WAV-multipart leg into a new reachy/speech/stt.py returning the transcript text

- covers: h1, c8
- acceptance:
  - New reachy/speech/stt.py exposes a Transcriber that POSTs a PCM16-mono WAV multipart form to {REACHY_STT_URL}/v1/audio/transcriptions and returns the response 'text' (str) or None
  - An unreachable host, HTTP>=400, empty body, or non-JSON response returns None and never raises
  - Accumulates a rolling window and throttles to <=1 POST per min_interval (both injectable); a sub-window chunk returns None until the window fills
  - No new runtime dependency: stdlib urllib + numpy only; importing the module pulls in no requests/openai

### t2 — SenseSample carries the raw mic chunk: add an optional frozen audio field to reachy/motion/sense_sample.py

- covers: c9, h2
- acceptance:
  - SenseSample gains an optional frozen field audio: np.ndarray|None = None (default None) so every existing SenseSample(...) construction is unchanged
  - test_sense_sample asserts the field defaults to None and round-trips an ndarray when set

### t3 — Transcript cue into cognition input: add EventBuffer.feed_transcript to reachy/speech/events.py and update the 'NOT transcription' docstring

- covers: c4, h9, h5, h8
- acceptance:
  - EventBuffer.feed_transcript(text) appends a SenseCue worded as words heard (e.g. 'heard someone say: "..."'); empty/whitespace text appends no cue
  - A fed transcript cue is returned by snapshot() and rendered by build_messages exactly like existing cues, so it appears in the export ThinkingEvent.cues with no wire-format change
  - The module docstring no longer claims 'It is NOT transcription' unconditionally; it documents feed_transcript while noting the CognitionEngine engine itself still has no STT (transcription is upstream in the hook)

### t4 — Refactor wakeword HttpSttBackend onto the shared transcriber (reachy/sleep/wakeword.py)

- depends on: t1
- covers: c8
- acceptance:
  - HttpSttBackend obtains its transcript text from reachy/speech/stt, with no second WAV/multipart/urllib implementation left in wakeword.py
  - All existing wakeword tests pass unchanged: substring phrase match, explicit 'detected' boolean, and phrase-echo equality behaviour are byte-identical

### t5 — TranscribeHook: per-tick listen-loop hook that transcribes the shared sample's audio and feeds words to cognition (new reachy/motion/listen_transcribe.py)

- depends on: t1, t2, t3
- covers: c10, h3, c7
- acceptance:
  - TranscribeHook has the on_tick signature (transport, queue, t, commanded_head); it reads the shared SenseSample and only when speech is present AND outside think's self-mute window accumulates the raw audio into the shared Transcriber
  - On a returned transcript it calls feed_transcript on the SAME EventBuffer the cognition engine consumes; a None transcript feeds nothing
  - Audio captured inside the self-mute window is discarded before transcription (test injects a mute-until in the future and asserts zero POSTs)
  - Any provider/transcriber/feed fault is swallowed (logged) so a tick degrades to 'no words' and never raises out of the hook

### t6 — Wire --transcribe into listen run --live: flag + stash the raw chunk in _build_sample_tap + compose TranscribeHook sharing the cognition buffer (reachy/cli/_commands/listen.py)

- depends on: t1, t2, t3, t5
- covers: c1, c2, h6, h4, h2, c9, h10
- acceptance:
  - listen run gains --transcribe (off by default); it requires --live and the sdk transport, erroring cleanly (exit 1) otherwise, mirroring --export
  - _build_sample_tap stashes the SAME per-tick chunk's raw samples onto SenseSample.audio with no second get_audio_sample call (test asserts one read per tick)
  - With --transcribe set, the live HookChain includes a TranscribeHook wired to the same EventBuffer as the ThinkHook engine
  - With --transcribe OFF, no TranscribeHook is built and zero STT POSTs are issued; a test asserts the live loop is observably unchanged from today

### t7 — Docs: document --transcribe across CLAUDE.md, README.md, docs/operating-reachy.md, and the explain catalog

- depends on: t6
- covers: c3, c5, c6, h7, h8
- acceptance:
  - CLAUDE.md listen-noun section + README + docs/operating-reachy.md describe --transcribe: what it does, off-by-default, the single-SDK-owner shared-chunk path, self-mute reuse, and the non-goals (not dialogue/barge-in/wake-word; STT stays external)
  - The explain catalog text for 'listen run' mentions --transcribe and test_every_catalog_path_resolves stays green
  - Docs state the STT requires model-gear Parakeet at REACHY_STT_URL (default localhost:9002) and degrades cleanly when absent

### t8 — Version bump + CHANGELOG entry for the hear-words feature (pyproject.toml, CHANGELOG.md)

- depends on: t7
- acceptance:
  - pyproject.toml version is bumped (minor) and CHANGELOG.md gains a Keep-a-Changelog entry; the version-check CI job would pass against origin/main

## Risks

- [follow_up] Flag surface: --transcribe is the working name; --hear-words / --stt are alternatives. Resolve at build time; whichever, it implies/requires --live like --export.
- [unknown_nonblocking] Gate policy: transcribe only when sample.speech is true (skip silence, save the STT server) vs any audio window — a throttling/cost tuning choice settled in t5.
- [unknown_nonblocking] Parakeet quality/latency for arbitrary full-sentence speech (not just the wake phrase) is unproven; the wake-word path only needs a substring. Heavier transcription may lag the rolling window or return poor text.
