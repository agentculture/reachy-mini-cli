# agent cognition: tool-use senses + behavior stash

> Reachy's live presence gains an agent cognition layer: the cortex lobe (no thinking mode) drives speech, harmonics, and body poses as tool calls; muse reacts natively to raw perception; hearing stays always-on; the alive base loop is untouched; a background behavior stash (code + explanation + lobes embeddings) lets the agent fetch and adapt body behaviors.
> instruction: build on 'listen run --live': add the agent cognition mode behind a flag; verify on-robot with an addressed utterance producing a spoken reply and/or pose while idle behavior continues beneath

## Audience

- the operator and anyone in the room interacting with Reachy (they get a robot that responds with structured actions), plus downstream export-feed consumers (the reTerminal panel) that keep receiving thinking/message/emotion blocks

## Before → After

- Before: today the live cognition is marker-driven prose from the senses role (Gemma-4-12B): expression + speech are parsed out of *emoji*/quoted text, no tool calls exist anywhere in the client, exactly one voice engine is active per process, poses come only from the fixed TOML catalog, and nothing the robot does is remembered as reusable behavior
- After: Reachy's live presence is an agent: it hears words continuously, decides via the cortex lobe (thinking disabled) through OpenAI tool calls, speaks via lobes TTS or harmonics as tool calls when relevant or asked, applies body poses as tool calls, and maintains a semantic behavior stash (LibraryEntry-shaped code + explanation + gateway embeddings) it fetches and adapts — all while the alive base loop keeps running beneath

## Why it matters

- tool calls make the robot's actions structured, auditable, and extensible (new capabilities = new tools) instead of parsed from prose conventions; the behavior stash lets expressive behaviors accumulate across sessions instead of being rebuilt or hand-coded each time

## Requirements

- the stdlib LLM client (reachy/speech/llm.py) gains OpenAI tool-calling: a 'tools' key in the payload, streamed tool_call delta assembly, and a tool-result turn loop — today _build_request sends content-only payloads and _iter_sse_deltas reads only delta.content, silently dropping tool_calls deltas
  - honesty: a mocked SSE stream carrying tool_call deltas split across chunk boundaries assembles complete calls (name + valid JSON arguments); a live request against cortex with a tools array returns finish_reason=tool_calls
- live cognition switches lobes role senses→cortex: environment.d/10-reachy-llm.conf currently pins REACHY_OPENAI_MODEL_ID=coolthor/gemma-4-12B-it-NVFP4A16 (senses, proxied from orin); cortex = sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP, loaded locally on :8001 with tool_use in its gateway responsibilities and parser qwen3_coder
  - honesty: with the environment pinned to the cortex model ID, a live end-to-end turn completes the full round trip: prompt -> tool_calls -> tool results -> final assistant text
- CognitionEngine grows an agent (tool-use) mode: cognition.py's documented input boundary says 'no tool-use', and the *emoji*/"..." MarkerParser convention is today's expression+speech interface — in agent mode, tool calls (speak, harmonics, pose, stash ops) become the standard interface instead
  - honesty: agent mode executes speak/pose/stash tools and emits the same thinking/message/emotion export block types; with agent mode off, the marker path behaves byte-identically to today (existing tests untouched)
- speech and harmonics become two always-available tools: voice.py's resolve_voice_engine picks ONE exclusive engine per process ({tts, harmonic}); the agent instead sees both as callable tools side by side (speak via TTS, chirp/sing via harmonic)
  - honesty: a single agent turn can invoke both the TTS tool and the harmonics tool; each plays on the SDK playback path at its correct sample rate (24 kHz TTS resampled to device rate, 16 kHz harmonic)
- the speech tool targets TTS on lobes: reachy/speech/tts.py speaks Chatterbox's POST /v1/audio/synthesize at REACHY_TTS_URL (default :9000), while the lobes gateway serves the tts role at /v1/audio/speech (OpenAI-style) on :8001 — the leg needs the gateway route/shape (or a gateway alias for the Chatterbox route)
  - honesty: speech synthesized through the lobes gateway TTS route returns playable PCM on the robot speaker; the live path carries no remaining dependency on the :9000 Chatterbox sidecar
- the pose tool rides the existing expression seam: expressions.py get_pose (emoji → 9-axis ExpressionPose) + motion/expression.py ExpressionProducer.express already enqueue calm one-shot moves onto the serial MotionQueue; the tool maps catalog emoji (and optionally a raw 9-axis pose) onto that path
  - honesty: an apply_pose tool call with a catalog emoji enqueues the identical MotionQueue action the *emoji* marker path produces today (same pose, same one-shot move shape)
- a NEW behavior-stash subsystem: repo-wide grep finds zero embedding code today; the gateway embedder role (Qwen/Qwen3-Embedding-0.6B, loaded) serves /v1/embeddings on :8001; stash records = code snippet + natural-language explanation + embedding vector, fetched by semantic similarity, adapted, then applied
  - honesty: a stash record (LibraryEntry-shaped code + natural-language explanation) receives an embedding via the gateway /v1/embeddings; a semantically related query retrieves it; an adapted copy runs on the live loop's motion queue
- the live boot unit carries the agent mode: service/units.py LIVE_UNIT ExecStart (listen run --live --transcribe --voice-engine harmonic) gains the agent-cognition flag(s) once they exist, so the presence survives reboot in the new mode
  - honesty: the rendered LIVE_UNIT ExecStart carries the agent-mode flag(s), and after 'reachy service enable live' the presence comes back in agent mode across a unit restart
- cortex is the main agent for tool-use cognition: live probe returned structured tool_calls with finish_reason=tool_calls and clean JSON arguments (emoji intact) from sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP via the gateway; muse is deferred to a follow-up adoption once agentculture/lobes-cli#139 (tool parser on thor + audio tower / honest catalog) lands
  - instruction: pin REACHY_OPENAI_MODEL_ID=sakamakismile/Qwen3.6-27B-Text-NVFP4-MTP (the cortex model) via environment.d; keep chat_template_kwargs enable_thinking:false as llm.py already sends; verify finish_reason=tool_calls on a tool-warranting prompt
  - honesty: after the client gains the tools parameter, live turns against cortex keep returning structured tool_calls (the 2026-07-17 probe reproduced as an automated integration check, skipped when the gateway is absent)
- stashed behaviors are LibraryEntry-shaped only: pure fn(t_local, params, sense) -> Contribution with typed params, channels, stop-class, lifetime — no free-form Python (user decision: dangerous/unstable); execution adapts the entry onto the live loop's serial MotionQueue goto path
  - instruction: reuse reachy/behavior/library.py's LibraryEntry as the stash record schema; adapt fetched entries onto the live loop's serial MotionQueue goto path (q3 decision), not the separate behavior-engine process
  - honesty: the stash loader accepts only LibraryEntry-shaped records (typed params, declared channels/stop-class/lifetime) and refuses anything else — no exec/eval of free-form code anywhere in the stash path

## Honesty conditions

- an addressed utterance on the robot produces at least one executed tool call (speak or pose) from a cortex turn, while the alive base loop keeps running (idle presence visible before and after the turn)
- the agent layer runs inside the existing live process: no new OS process, no second media_session call anywhere in the diff; bare 'listen run' and the idle/alive base behave byte-identically (existing tests pass untouched)
- say's import-boundary test still asserts reachy.speech.llm and reachy.speech.events are absent from say's import graph, and stays green
- pyproject base dependencies remain exactly numpy + harmonics-cli; every new network leg (tools LLM, gateway TTS, embeddings) uses stdlib urllib only
- under agent mode an utterance reaches cognition only on an ENGAGE verdict (name fast-path or classifier YES); ambient human-to-human chatter produces zero LLM turns
- an agent-mode turn emits thinking/message/emotion JSONL blocks that validate against docs/export-schema.md, and the reTerminal bridge renders them without changes
- the feature is exercised by voice alone — the operator addresses the robot and observes the structured response; no new client tooling is required to use it
- each after-state capability is demonstrable on the robot in one session: hear → cortex tool-call decision → speak and/or pose → stash + semantic fetch of a behavior — with idle presence running throughout
- the before-state describes current main: no 'tools' key in llm.py, exactly one voice engine resolved per process, zero embedding code in reachy/
- every robot action in agent mode is visible as a structured tool call in the export/log stream, and adding a new capability requires only registering one new tool definition + handler
- the success checks are runnable as: on-robot addressed-utterance demo, a cross-session stash fetch demo, 'pytest -n auto' green, 'teken cli doctor . --strict' green

## Success signals

- on the robot: an addressed utterance produces a cortex tool-use turn that speaks (lobes TTS or harmonic) and/or applies a pose, with the export feed still rendering on the reTerminal panel; a behavior stashed in one session is fetched by meaning and re-applied in a later session; the full pytest suite and the teken rubric gate stay green

## Scope / boundaries

- the live loop stays the base and the single-SDK-owner model holds: the agent layer rides the folded ThinkHook on_tick seam (listen_think.py opens no media session of its own; HookChain isolates hooks; priority sleep > pat > think) — no second process, no second media session, and bare 'listen run' plus the alive/idle base behavior are untouched (user's clarification: it builds on live mode)
- say stays a dumb TTS pipe: it must not import reachy.speech.llm or reachy.speech.events (a test-asserted import boundary) — tool-use cognition lives in the think/live engine only, never in say
- no new base runtime dependency: embedder, reranker, TTS, and muse are all reached over the lobes gateway with stdlib urllib; numpy (already base) covers cosine similarity for the stash index — no openai SDK, no vector-db package (the base-deps hard constraint allows only numpy + harmonics-cli)
- 'talk when relevant or asked' = the existing layered engagement gate stays the speech gate (self-mute → fuzzy name fast-path → single-shot LLM classifier → heuristic DEGRADE fallback, REACHY_ENGAGE_HEURISTIC escape hatch); the agent layer consumes its ENGAGE verdicts rather than bypassing it
- the export feed contract survives the interface change: cognition.py's export hook emits MessageEvent per spoken item and EmotionEvent per expression in stream order (docs/export-schema.md; the reTerminal bridge consumes it) — tool-use speak/pose calls must keep emitting the same three block types

## Non-goals

- muse-native reaction is OUT of v1 — deferred until agentculture/lobes-cli#139 lands (probe 2026-07-17: no tool parser on thor serving, served checkpoint has no audio tower); also out: barge-in conversation, free-form Python behavior execution, and any change to bare 'listen run' or the alive/idle base

## Assumptions

- 'without thinking' is already satisfied client-side: llm.py:260 hardcodes chat_template_kwargs {enable_thinking: false} on every request, and cortex (Qwen3.6-27B, same hybrid-thinking family) honours the same kwarg
- reachy/behavior/library.py is the natural substrate for stashed behaviors: LibraryEntry already models named parametric motion generators fn(t_local, params, sense) -> Contribution with channels/stop-class/lifetime, a registry, and build(); behavior/control.py already provides a file-spool control channel into a running engine

## Scope exploration

- `s1` — `reachy/speech/llm.py`: content-only client: _build_request has no tools key, _iter_sse_deltas reads only delta.content (tool_calls deltas dropped); but line 260 already hardcodes chat_template_kwargs {enable_thinking: false} — 'without thinking' is done, tool-calling is the whole gap
  - seeds: `c2`, `c3`
- `s2` — `lobes gateway :8001 /capabilities (live probe)`: role map confirmed live: cortex Qwen3.6-27B loaded locally with tool_use responsibility + parser qwen3_coder; muse Gemma-4-31B unified multimodal proxied from thor (loaded:false, forbids final_decision); embedder /v1/embeddings loaded; reranker /v1/rerank loaded; stt /v1/audio/transcriptions; tts /v1/audio/speech
  - seeds: `c4`, `c9`
- `s3` — `~/.config/environment.d/10-reachy-llm.conf + systemctl --user cat reachy-live.service`: live cognition today runs the senses role (REACHY_OPENAI_MODEL_ID=coolthor/gemma-4-12B-it-NVFP4A16) via an EnvironmentFile drop-in; ExecStart is 'listen run --live --transcribe --voice-engine harmonic' — the role switch is config, the flags are code
  - seeds: `c4`, `c11`
- `s4` — `reachy/speech/cognition.py + reachy/speech/markers.py`: the engine's documented input boundary (module docstring) is 'no tool-use'; expression + speech flow through the *emoji*/quoted MarkerParser convention; the export hook emits MessageEvent/EmotionEvent per work item in stream order and ThinkingEvent at turn end — agent mode replaces the marker interface but must keep the export block contract
  - seeds: `c5`, `c16`
- `s5` — `reachy/speech/voice.py`: resolve_voice_engine picks exactly one engine per process from {tts, harmonic} (env REACHY_VOICE_ENGINE); 'tool use for both as standard' dissolves the either/or into two tools available in the same turn
  - seeds: `c6`
- `s6` — `reachy/speech/tts.py`: the TTS leg posts Chatterbox JSON {text, voice} to {REACHY_TTS_URL}/v1/audio/synthesize (default :9000), expects bare PCM16 @ 24 kHz; the lobes gateway instead exposes tts as OpenAI-style /v1/audio/speech — route + response-shape gap for 'TTS on lobes'
  - seeds: `c7`
- `s7` — `reachy/speech/expressions.py + reachy/motion/expression.py`: get_pose (emoji-keyed TOML catalog → 9-axis ExpressionPose) and ExpressionProducer.express (enqueue onto the serial MotionQueue) already form the apply-a-pose path; a pose tool is a thin adapter over this seam
  - seeds: `c8`
- `s8` — `reachy/motion/listen_think.py + reachy/motion/listen_hooks.py + reachy/motion/sense_sample.py`: the folded seam is ready to carry the agent layer: ThinkHook opens no media session (single-SDK-owner), feeds the shared per-tick SenseSample (which already retains raw audio for STT) into the engine's EventBuffer off-thread, and HookChain isolates per-tick faults; the engine swap happens behind this seam without touching the loop
  - seeds: `c12`
- `s9` — `repo-wide grep for embedding/embedder/stash`: zero hits in reachy/ — the behavior stash and its embedding index are a genuinely new subsystem, not an extension of existing retrieval code
  - seeds: `c9`
- `s10` — `reachy/behavior/library.py + reachy/behavior/control.py + reachy/behavior/arbitration.py`: a behavior substrate already exists: LibraryEntry (named, parametric, pure fn(t_local, params, sense) -> Contribution, channels + stop-class + lifetime), a registry with build(), a file-spool control channel (commands/ results/ state.json), and per-tick channel arbitration — but it drives 50 Hz set_target through its own engine process, not the live loop's MotionQueue goto path
  - seeds: `c10`
- `s11` — `reachy/service/units.py`: pure unit-text renderers; LIVE_UNIT ExecStart is the one line where the deployed presence picks up new flags — the repo's renderer and the dev box's local drop-ins (llm.conf, panel.conf) both exist, and issue #62 already tracks the daemon-unit ExecStart gap
  - seeds: `c11`
- `s12` — `CLAUDE.md hard-constraints + say import-boundary tests + engagement gate (reachy/speech/engagement.py)`: three standing boundaries the idea must respect: base deps stay numpy + harmonics-cli only (everything new rides the gateway over stdlib urllib); say never imports llm/events; the layered engagement gate remains the talk-when-relevant mechanism
  - seeds: `c13`, `c14`, `c15`
- `s13` — `live probes against gateway :8001 (muse completion, muse tool-use, muse audio-in, cortex tool-use)`: muse responds (thor proxy healthy, tolerates enable_thinking kwarg) but emits tool calls as plain text (no serving-side parser) and rejects input_audio with 400 'no audio tower' — the served NVFP4 export lacks audio_config despite the catalog's text+image+audio claim; cortex returns structured tool_calls correctly; gaps filed as agentculture/lobes-cli#139
  - seeds: `c17`, `c18`

## Open / follow-up

- whether the lobes gateway muse route accepts OpenAI audio content parts (input_audio) for Gemma-4 audio-in, and what the thor→spark proxy latency budget allows for a 'native reaction' path — unverified; needs a live probe against the gateway before the muse leg can be designed — RESOLVED by live probe 2026-07-17: gateway rejects input_audio for muse (400: served NVFP4 checkpoint has no audio tower) and thor serving has no tool parser; muse leg deferred out of v1 scope pending agentculture/lobes-cli#139 (see q1 decision + scope s13). Hand-edited per devague#55/#60 (no unpark verb in 0.19.0).
