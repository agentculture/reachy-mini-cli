# Reachy Mini's live thinking now hears words: listen run --live can optionally transcribe nearby speech via model-gear's Parakeet STT and feed the actual words into cognition, so the robot reasons about WHAT was said, not just that a sound came from the left

> Reachy Mini's live thinking now hears words: listen run --live can optionally transcribe nearby speech via model-gear's Parakeet STT and feed the actual words into cognition, so the robot reasons about WHAT was said, not just that a sound came from the left

## Audience

- Operators running 'listen run --live' on a real Reachy Mini with model-gear's Parakeet STT reachable (localhost:9002), plus the shared think CognitionEngine that turns perceptions into spoken thought

## Before → After

- Before: Live cognition only receives directional/loudness cues like 'speech from the left' or 'loud sound ahead'. EventBuffer and CognitionEngine BOTH explicitly forbid STT today ('It is NOT transcription'; 'There is intentionally no STT / transcription path'). The robot knows a sound happened and from where, never what was said.
- After: When enabled, transcribed speech enters cognition as a new perception cue (e.g. 'heard someone say: "..."'), so the LLM turn reasons about the actual words. The transcript is visible in the --export thinking-block cues. The robot's spoken/expressed reaction is about content, not just direction.

## Why it matters

- 'Speech from the left' yields a generic musing; the words yield a relevant reaction. Hearing content, not just sound, is the difference between reacting to noise and reacting to meaning — the natural next layer of 'aliveness'.

## Requirements

- The cognition transcript reuses the EXISTING model-gear Parakeet STT leg the wake-word backend already uses (HttpSttBackend's rolling-window WAV/multipart/urllib POST to /v1/audio/transcriptions) — one shared STT client, not a second STT stack.
  - honesty: The Parakeet /v1/audio/transcriptions WAV-multipart leg in HttpSttBackend can be factored into a shared transcribe(audio)->text|None client that both the wake-word matcher and the new cognition transcript build on, with no duplicated STT stack and no new runtime dependency (stdlib urllib + numpy).
- The raw mic chunk the STT needs must ride the shared per-tick SenseSample (extend the loop's single existing _audio read to also retain the raw chunk), NOT a second media session — the single-SDK-owner rule. Today SenseSample carries only RMS, and listen_sleep synthesizes a fake constant-RMS chunk, which transcribes to nothing.
  - honesty: The loop's single per-tick mic read (_audio, which today stashes only RMS) can be extended to also retain the raw float32 chunk on the shared SenseSample, so the STT hook accumulates a rolling window and transcribes REAL audio with zero second media-session reads — single-SDK-owner preserved.
- The transcript path reuses think's self-mute window so the robot never transcribes its own TTS output and feeds its own words back into cognition (a feedback loop).
  - honesty: think's existing self-mute window (the ~2.5s post-speech guard the before_turn sense feed already honours) can gate the transcript path too, so audio captured while the robot is speaking is discarded before transcription — no self-feedback loop.

## Honesty conditions

- An operator can flip one flag on 'listen run --live' and hear the robot react to the actual words spoken near it (visible in the --export thinking feed), with the flag off leaving today's behavior unchanged.
- The audience is real and reachable: 'listen run --live' is the shipped boot-presence loop, and model-gear's Parakeet STT is already running on this box at localhost:9002 (the wake-word backend targets it today).
- Verifiable in-tree: events.py says 'It is NOT transcription' and cognition.py says 'There is intentionally no STT / transcription path', and build_messages renders cues as 'I just perceived: - speech from the left' — direction/loudness only, never words.
- A transcript cue added via a new EventBuffer.feed_transcript lands in build_messages exactly like existing cues, so it appears in the LLM prompt and (when --export is on) in the ThinkingEvent.cues list with no change to the export wire-format.
- The value claim is testable on-robot: with the same nearby utterance, transcribe-off yields a direction-only musing and transcribe-on yields a reaction referencing the spoken content.
- The cognition system prompt / cue wording can present a transcript as 'words you heard' distinct from a raw sensor reading, so the LLM responds to content without the engine growing any dialogue/turn-taking/barge-in machinery.
- With --transcribe off, the live loop issues zero STT POSTs and is observably unchanged; the STT leg is import-light (stdlib urllib + numpy) so a bare 'pip install reachy-mini-cli' is unaffected whether or not the flag exists.

## Success signals

- With the flag ON and someone speaking nearby, the exported 'thinking' block's cues contain the transcript and the robot's reaction is about the words. With the flag OFF, no STT POST is issued and the live loop's behavior + bare-install dependency footprint are byte-identical to today. STT down/unreachable degrades to 'no words this window' and the 20Hz loop never dies.

## Scope / boundaries

- Feeding words is one more PERCEPTION, not a conversation. NON-goals: not a dialogue/chatbot/turn-taking assistant, no barge-in/interrupt of an in-flight thought, not the sleep wake-word path (sleep owns that), no on-box STT model bundled (stays external behind REACHY_STT_URL / Parakeet). OFF by default.

## Assumptions

- model-gear's Parakeet at REACHY_STT_URL returns a usable {"text": "..."} transcript for arbitrary nearby speech, not only the wake phrase — the same endpoint the wake-word backend already depends on.

## Decisions

- Scope this release: --transcribe is wired into the folded 'listen run --live' ThinkHook path ONLY. Standalone 'think run --transcribe' is a parked follow-up (the CognitionEngine is shared, so it is a cheap later add).
