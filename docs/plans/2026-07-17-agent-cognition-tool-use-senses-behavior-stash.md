# Build Plan — agent cognition: tool-use senses + behavior stash

slug: `agent-cognition-tool-use-senses-behavior-stash` · status: `exported` · from frame: `agent-cognition-tool-use-senses-behavior-stash`

> Reachy's live presence gains an agent cognition layer: the cortex lobe (no thinking mode) drives speech, harmonics, and body poses as tool calls; muse reacts natively to raw perception; hearing stays always-on; the alive base loop is untouched; a background behavior stash (code + explanation + lobes embeddings) lets the agent fetch and adapt body behaviors.

## Tasks

### t1 — LLM client tool-calling (reachy/speech/llm.py)

- covers: c2, h2, c17, h10
- acceptance:
  - tools= kwarg is serialized into the request payload for stream_chat_completion and complete; when absent the payload is byte-identical to today (existing tests unmodified)
  - a mocked SSE stream carrying tool_call deltas split across chunk boundaries assembles complete calls (name + valid parsed JSON arguments); content-only streams behave exactly as before
  - an integration test (auto-skipped when the gateway is unreachable) sends a tool-warranting prompt to the cortex model and asserts finish_reason=tool_calls, with chat_template_kwargs enable_thinking:false still present in the payload

### t2 — Gateway TTS leg — OpenAI-style /v1/audio/speech (reachy/speech/tts.py)

- covers: c7, h6
- acceptance:
  - synthesize can target the lobes gateway OpenAI-style route (env-selectable, e.g. REACHY_TTS_ROUTE or URL-based detection), returning playable PCM16; the Chatterbox /v1/audio/synthesize route keeps working unchanged
  - unit tests fake both routes and cover response-shape handling (bare PCM pass-through, WAV unwrap) plus the error contract (CliError exit-2 on unreachable/HTTP failure)

### t3 — Agent tool registry — speak/harmonics/apply_pose definitions + dispatch (new reachy/speech/tools.py)

- covers: c6, h5, c8, h7, c22, h20
- acceptance:
  - the registry defines speak (TTS), harmonics, and apply_pose tools with JSON schemas and a dispatch that executes a handler and returns an OpenAI tool-result message
  - apply_pose with a catalog emoji enqueues the identical MotionQueue action the *emoji* marker path produces (asserted by comparing enqueued actions)
  - one turn can invoke both speak and harmonics; each synthesizes at its own sample rate (24 kHz TTS resampled to device rate, 16 kHz harmonic) through the injected playback seam
  - adding a capability requires only one new tool definition + handler (demonstrated by a test registering a fake tool and seeing it dispatched)

### t4 — Behavior stash store — records, embeddings, semantic fetch (new reachy/stash/ package)

- depends on: t3
- covers: c9, c14, h14, c18
- acceptance:
  - stash records are LibraryEntry-shaped declarative data (name, summary/explanation, typed params, channels, stop-class, lifetime, generator reference) with schema validation; malformed or free-code records are refused with a clean error
  - embeddings are fetched from the gateway /v1/embeddings with stdlib urllib; the index persists under the state dir; a cosine top-k query (numpy only) returns the semantically nearest record
  - pyproject base dependencies are unchanged (numpy + harmonics-cli only) — asserted by a test reading pyproject.toml

### t5 — Stash apply adapter — fetched entry onto the live MotionQueue goto path (reachy/stash/apply.py)

- depends on: t4
- covers: h8, h11, c18
- acceptance:
  - a fetched entry is built via the library build() path (no exec/eval of strings anywhere in the stash path) and sampled into a bounded series of MotionQueue goto actions
  - end-to-end test: stash a record, fetch it by a semantically related query, and the adapted copy enqueues on a fake queue with the expected pose sequence

### t6 — Agent turn engine — tool loop + export blocks (new reachy/speech/agent_turn.py + cognition seam)

- depends on: t1, t3
- covers: c5, h4, c16, h16
- acceptance:
  - agent mode runs the full tool loop (LLM -> tool_calls -> execute via registry -> tool results -> final text) and emits thinking/message/emotion export blocks that validate against docs/export-schema.md
  - with agent mode off, the marker path is byte-identical to today (every existing cognition/marker test passes unmodified)
  - audio_optional degradation carries over: a dead TTS skips speech but the turn completes and expression + export sinks still fire

### t7 — Live wiring — agent flag on listen --live, engagement gate unchanged (reachy/cli/_commands/listen.py + reachy/motion/listen_think.py)

- depends on: t6
- covers: c1, h1, c12, h12, c15, h15
- acceptance:
  - a --live-only agent-mode flag selects the agent engine behind the existing ThinkHook seam; the diff introduces no new OS process and no second media_session call
  - utterances reach agent cognition only on ENGAGE verdicts — the existing layered gate is wired unchanged; a fake-classifier test shows ambient chatter drives zero LLM turns
  - bare 'listen run' and non-agent '--live' behave identically to main (existing listen tests pass unmodified)

### t8 — Cortex role switch — config + end-to-end round trip (docs + gateway-gated integration test)

- depends on: t1, t6
- covers: c4, h3
- acceptance:
  - with REACHY_OPENAI_MODEL_ID pinned to the cortex model (environment.d documented), a gateway-gated integration test completes the full round trip: prompt -> tool_calls -> tool results -> final assistant text
  - the operating guide documents the senses->cortex switch and that think/say defaults are unaffected

### t9 — Boot unit + rollout docs + verification sweep (reachy/service/units.py, docs, CLAUDE.md)

- depends on: t7, t8
- covers: c11, h9, c13, h13, c19, h17, c20, h18, c21, h19, c23, h21
- acceptance:
  - LIVE_UNIT ExecStart carries the agent-mode flag; unit renderer tests updated; 'reachy service enable live' brings the presence back in agent mode across a restart (on-box check documented)
  - say's import-boundary test stays green; full 'pytest -n auto' green; 'teken cli doctor . --strict' green
  - the operating guide documents the voice-only usage story, the before/after states as shipped, and the two on-robot demos (addressed utterance -> tool-use turn; stash in one session -> semantic fetch in a later one)

### t10 — Muse verification + agent-model move option (deviation d1)

- depends on: t8, t9
- acceptance:
  - the gateway-gated tool round-trip integration test runs against BOTH cortex and muse (parametrized over model IDs; muse = nvidia/Gemma-4-31B-IT-NVFP4): tool_calls returned, tool-result flow completes, final text arrives; each model's case skips independently when unavailable
  - the round trip records wall-clock turn latency per model and asserts a generous usability bound for muse through the thor proxy (document the measured numbers in the test docstring or report)
  - docs/operating-reachy.md's cortex-switch section becomes a model-choice section: cortex (local, default fallback) vs muse (proxied from thor, now tool-capable; audio-in still absent per lobes-cli#139), with the exact REACHY_OPENAI_MODEL_ID values and the note that the switch is pure environment.d config

## Risks

- [unknown_nonblocking] gateway /v1/audio/speech response shape (bare PCM vs WAV vs compressed) is unverified — cheap probe before t2 lands decides the decode path
- [unknown_nonblocking] cortex tool-call STREAMING via the gateway is unverified — the 2026-07-17 probe validated non-streaming only; t1's delta-assembly integration test resolves it (fallback: non-streaming tool turns, speech pipeline unaffected)
- [follow_up] declarative-only stash records may prove too restrictive for interesting behaviors — v2 candidate: a vetted mini-DSL or reviewed-generator flow
- [follow_up] muse adoption (reaction duty / divergent second opinion) when agentculture/lobes-cli#139 lands
