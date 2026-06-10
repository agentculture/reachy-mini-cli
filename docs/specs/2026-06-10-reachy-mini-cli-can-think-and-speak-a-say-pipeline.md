# reachy-mini-cli can think and speak: a 'say' pipeline streams an OpenAI-compatible LLM's thought sentence-by-sentence into text-to-speech and plays it aloud through the robot, so Reachy Mini reasons about a prompt and voices the answer in its own voice.

> reachy-mini-cli can think and speak: a 'say' pipeline streams an OpenAI-compatible LLM's thought sentence-by-sentence into text-to-speech and plays it aloud through the robot, so Reachy Mini reasons about a prompt and voices the answer in its own voice.

## Audience

- Ori operating Reachy Mini from the CLI, plus the mesh agent itself — anyone who wants the robot to answer a prompt out loud instead of just on screen.

## Before → After

- Before: Today the CLI can orient to sound (listen) and react to motion (vision) but has no language or audio-out path at all — the robot cannot form or voice a single sentence.
- After: Two nouns: 'reachy say "<text>"' is pure text-to-speech (no LLM) that voices given text through the robot; 'reachy think' is a continuous mode (like listen/vision) that accumulates live sense EVENTS, reasons over them with an OpenAI-compatible LLM, and speaks the result by reusing say's synth+playback.

## Why it matters

- Listen/vision/motion already let Reachy sense and move; a voice closes the loop so the robot can actually respond — the difference between a reactive ornament and something you can talk with.

## Requirements

- Thinking uses an OpenAI-compatible chat-completions endpoint configured by env + flags (REACHY_LLM_BASE_URL / _API_KEY / _MODEL, --base-url/--model), streamed and split into sentences exactly like realtime-api's stream_sentences, so first speech starts early.
  - honesty: An OpenAI-compatible /v1/chat/completions with stream=true is reachable in Ori's setup (the realtime-api vLLM/Nemotron or any OpenAI endpoint), and sentence-splitting yields first speakable text within ~1-2 sentences of generation.
- Speech uses a configurable TTS endpoint (default the Magpie-style HTTP synth from realtime-api: REACHY_TTS_URL / _VOICE), each streamed sentence synthesized as it arrives and the PCM played back through the robot.
  - honesty: A TTS HTTP endpoint that returns playable PCM/WAV for a sentence is available (Magpie NIM or equivalent), and per-sentence synth latency is low enough that speech keeps up with the LLM stream.
- Audio plays out through the robot's own speaker via the reachy-mini SDK media session (consistent with listen's sdk transport), NOT the host's aplay; SDK stays an extra so a bare install still parses.
  - honesty: The reachy-mini SDK media session can PLAY audio out to the robot speaker (not only capture mic in); if it cannot, speech-out needs another path (host aplay / daemon endpoint).
- think sources sense EVENTS by polling the same primitives listen/vision already use (behavior.sense DoA/RMS + vision motion/light), turning them into timestamped cues in a rolling buffer — no new sensor code, reuse existing senses.
  - honesty: The listen/vision sense primitives are readable by think concurrently (no exclusive hardware lock that the existing loops hold) so think can poll DoA/motion while idle.
- Parallel think+speak: the LLM response streams sentence-by-sentence into TTS (reusing say) so early sentences play through the speaker while later sentences are still being generated — the realtime-api stream_sentences pattern.
  - honesty: Per-sentence synth+playback keeps up with LLM generation closely enough that speech is continuous/gap-tolerant (proven by realtime-api at 125% rate).

## Honesty conditions

- End-to-end LLM->TTS->robot-speaker produces coherent spoken output from a prompt/sense-context in Ori's setup (vLLM/Nemotron + Magpie reachable).
- The operator (and the mesh agent) can invoke say/think from the CLI and hear output with no extra GUI tooling.
- Spoken output is qualitatively more useful to the operator than on-screen text — the robot actually 'responds'.
- Today's CLI has no language or audio-out verb (verifiable: no say/think/tts in reachy/cli/_commands).
- On an unreachable LLM or TTS endpoint the verb exits 2 with a CliError (hint line, no traceback); on success speech is sentence-streamed (first audio before generation completes).
- say works with no LLM and no senses; think runs the full sense->reason->speak loop; each is independently invokable.
- v1 delivers value from event-cue reasoning alone — shipping without STT, barge-in, or tool-use is still worth using.

## Success signals

- From a bare prompt, the robot speaks a coherent spoken answer through its own speaker, sentence-streamed (talking begins before the LLM finishes), and degrades to a clean exit-2 CliError when the LLM or TTS endpoint is unreachable — never a traceback.

## Scope / boundaries

- v1 think fuses EVENT-level cues only (built from listen/vision state: 'speech from the left', 'loud sound', 'motion right', 'brightening') — NOT transcribed words (STT is a follow-up), NOT a full-duplex barge-in conversation, NOT a tool-using agent. say stays a dumb TTS pipe with no senses and no LLM.

## Decisions

- Keep the base runtime light: the LLM/TTS HTTP clients use stdlib urllib (like reachy/daemon.py), adding NO new base dependency beyond numpy — httpx stays out of the base profile.
- say and think are separate nouns. say = TTS primitive (text in -> speech out). think = autonomous sense->reason->speak mode that depends on say for its speech leg. Only think touches the LLM and the environment; say never does.
- think is a managed-loop noun mirroring listen: run (foreground) + start/stop/restart/status (tracked background process under the state dir) + overview, --json everywhere, the CliError contract, an explain/catalog entry. Copy listen's scaffold.
- Serialized cognition: a think turn snapshots the event buffer and runs ONE LLM generation at a time; events arriving mid-turn accumulate and are only consumed by the next turn (only after a thought finishes does the next start).
- A think turn fires when unprocessed events exist and the robot is idle (not mid think/speak), with a tunable min inter-turn interval to avoid chatter; silence => no speech.
- say verb: 'reachy say "<text>"' (also stdin '-'), --voice/--speed/--json, run+overview; reuses the exact same TTS synth + SDK-streaming/HTTP-fallback playback that think uses for its speech leg.

## Open / follow-up

- Expressive motion while speaking (antenna/head sync to speech) — desirable but a follow-up, not v1.
