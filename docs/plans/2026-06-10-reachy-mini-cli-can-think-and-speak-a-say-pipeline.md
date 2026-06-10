# Build Plan — reachy-mini-cli can think and speak: a 'say' pipeline streams an OpenAI-compatible LLM's thought sentence-by-sentence into text-to-speech and plays it aloud through the robot, so Reachy Mini reasons about a prompt and voices the answer in its own voice.

slug: `reachy-mini-cli-can-think-and-speak-a-say-pipeline` · status: `exported` · from frame: `reachy-mini-cli-can-think-and-speak-a-say-pipeline`

> reachy-mini-cli can think and speak: a 'say' pipeline streams an OpenAI-compatible LLM's thought sentence-by-sentence into text-to-speech and plays it aloud through the robot, so Reachy Mini reasons about a prompt and voices the answer in its own voice.

## Tasks

### t1 — TTS synth client: reachy/speech/tts.py — POST cleaned text to a Magpie-style HTTP /v1/audio/synthesize over stdlib urllib, return PCM16; text cleaning + sentence-split cited from realtime-api

- covers: c10, h2
- acceptance:
  - given a stub HTTP synth, synthesize() returns non-empty PCM bytes for a sentence
  - markdown/emoji are stripped and a multi-sentence paragraph splits into ordered sentences
  - an unreachable TTS URL raises CliError exit 2 with a hint line (no traceback)

### t2 — LLM streaming client: reachy/speech/llm.py — stream OpenAI-compatible /v1/chat/completions (stream=true) over stdlib urllib, parse SSE deltas, yield complete sentences early (stream_sentences port)

- covers: c9, h1
- acceptance:
  - given a stubbed SSE byte stream, stream_sentences() yields complete sentences before the stream ends
  - config reads REACHY_LLM_BASE_URL/_API_KEY/_MODEL with --base-url/--model overrides
  - an unreachable LLM URL raises CliError exit 2 with a hint (no traceback)

### t3 — Audio playback: reachy/speech/playback.py — sdk path streams PCM via reachy_mini media push_audio_sample()/start_playing(); http path uploads WAV to daemon /media/sounds/upload then POSTs /media/play_sound

- covers: c11, h3
- acceptance:
  - sdk playback feeds PCM to a fake media session via push_audio_sample
  - http playback uploads a WAV and calls /media/play_sound against a stub daemon
  - selecting the sdk path without the [sdk] extra raises CliError exit 2 pointing at the [sdk] install

### t4 — Sense-event buffer: reachy/speech/events.py — poll existing behavior.sense DoA/RMS + vision motion/light into a rolling timestamped cue buffer ('speech from the left', 'motion right', 'brightening'); thread-safe snapshot-and-clear

- covers: c17, h11
- acceptance:
  - feeding DoA/RMS + vision samples yields human-readable directional cue strings
  - snapshot() returns buffered cues and atomically clears them; concurrent producer/consumer loses no cues
  - reads the listen/vision sense primitives without holding an exclusive lock (no new sensor code)

### t5 — say noun: reachy/cli/_commands/say.py — 'reachy say "<text>"' (+ stdin '-'), --voice/--speed/--json, run+overview; pipes text -> tts -> playback; never touches the LLM or senses

- depends on: t1, t3
- covers: c13, h9
- acceptance:
  - 'reachy say' with text synthesizes and plays it via the tts+playback modules
  - '-' reads text from stdin; --voice/--speed are forwarded to synth; --json emits a structured result
  - say imports neither the llm nor events modules (a test asserts the dumb-pipe boundary)

### t6 — think engine: reachy/speech/cognition.py — event-buffer -> serialized single LLM turn -> sentence-stream into TTS+playback with speech overlapping generation; mid-turn events buffer for the next turn

- depends on: t1, t2, t3, t4
- covers: c15, c19, h10, h12
- acceptance:
  - only one LLM turn runs at a time; cues arriving during a turn are consumed only by the next turn
  - early sentences reach playback while later sentences are still being generated (parallel pipeline)
  - the engine consumes only event cues — no STT/transcription, tool-use, or barge-in path exists

### t7 — think noun: reachy/cli/_commands/think.py — managed-loop verbs mirroring listen (run + start/stop/restart/status + overview), --json, CliError contract; drives the cognition engine over a live sense feed

- depends on: t6
- covers: c6, h8
- acceptance:
  - think exposes run + start/stop/restart/status + overview as a tracked background process (pid/log under the state dir), like listen
  - from a sense context the loop produces a sentence-streamed spoken answer (first audio before generation completes)
  - an unreachable LLM or TTS endpoint exits 2 with a CliError hint line and no traceback

### t8 — Wire + document: register say & think in reachy/cli/__init__._build_parser, add explain/catalog ENTRIES for both, document REACHY_LLM_*/REACHY_TTS_* env + verbs in README/CLAUDE.md, bump version

- depends on: t5, t7
- covers: c1, c2, c4, c5, h5, h7
- acceptance:
  - 'reachy say' and 'reachy think' resolve through reachy.cli.main; teken cli doctor . --strict stays green
  - explain/catalog has resolving ENTRIES for the say and think command paths
  - README/CLAUDE.md document the two nouns + env vars; pyproject version is bumped (version-check passes)

### t9 — Integration + E2E tests: tests/test_say_think.py — end-to-end say and think against stubbed LLM/TTS/media, proving the spoken-response loop and say/think independence

- depends on: t8
- covers: h4, h6
- acceptance:
  - an E2E test drives 'think' with stubbed LLM+TTS+media and asserts coherent ordered audio reaches playback
  - a test proves say works with no LLM/senses and think runs the full loop, each independently invokable
  - the suite runs under pytest -n auto and keeps coverage >= the CI gate (60%)

## Risks

- [unknown_nonblocking] think's background process mgmt may require generalizing the listen-bound reachy/motion/supervisor.py + server.py (shared files) — refactor/merge-overlap risk between t7 and listen (task t7)
- [unknown_nonblocking] porting realtime-api's async httpx SSE streaming to stdlib urllib sync chunked reads (LLM stream + per-sentence TTS) is non-trivial and may need careful incremental buffering (task t2)
