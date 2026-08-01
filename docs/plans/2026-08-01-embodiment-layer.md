# Build Plan — embodiment layer

slug: `embodiment-layer` · status: `exported` · from frame: `embodiment-layer`

> Reachy gains an embodiment layer: a detachable realtime harness under the agent noun, served by lobes and run beside the runtime on this box. It hears everything in realtime over ONE duplex session (Reachy mic via a runtime tee; AEC to its own speakers), sees faces plus a rolling video clip, reacts in voice when the robot own rules fire, and speaks through Reachy speaker. It operates the robot only through the direct-operation action set: move the head / antennas / body, make sounds, run sets of movements and sounds, and create new rule-triggered actions. Enable/disable at will; the runtime decision loop and every existing condition stay unchanged; every LLM call (senses gemma / worker qwen) streams.

## Tasks

### t1 — PROBE concurrent sessions: hold a transcription-only session and an armed conversational session against the deployed lobes /v1/realtime simultaneously (scratch script; evidence to docs/evidence/)

- acceptance:
  - Two sessions coexist for at least 60 s — one transcription-only, one armed with response.create; both receive their correct event families with no cross-talk; transcript + event log archived under docs/evidence/
  - A failure or refusal is written up in the same evidence file and triggers /deviate on the media plan — never silently worked around

### t2 — PROBE video wire-format: send a real short clip as video content parts to model=worker over the deployed gateway /v1/chat/completions (streaming)

- acceptance:
  - The worker model returns a recognizably correct description of the clip; the exact request shape (content-part schema, encoding, size limits) is archived under docs/evidence/
  - On failure the clip pipeline is NOT built — the finding routes through /deviate to re-scope seeing (this probe gates t5/t10 clip work by policy, enforced at the workforce gate)

### t3 — Realtime wire response.\* family: extend reachy/speech/`realtime_wire.py` codec + tests/`fake_realtime_server.py` with response.created / response.text.done / response.audio.delta / response.done / response.interrupted and response.create arming

- covers: c10
- acceptance:
  - Codec round-trips every response.\* event against the fake server, base64 PCM deltas decode to contiguous audio, and malformed events resolve to named errors — offline, stdlib-only, safe under pytest -n auto
  - A test asserts the client-side send surface is EXACTLY session-config, `input_audio_buffer`.append, and response.create — no other frame type exists in the wire module

### t4 — Audio tee: third AudioPump consumer fanning the ONE per-tick chunk to a local unix-socket sink (reachy/behavior/`audio_tee.py` + composition in `_commands`/behavior.py)

- covers: c19, c29, h24
- acceptance:
  - The tee receives the same chunk stream `_AudioTap` fans out — a test proves no second take() call exists (AST or call-count fake) and chunks arrive contiguous, mono, in order
  - A wedged or absent consumer never blocks: bounded queue drops with a named senselog drop, tick-thread work stays O(1) per tick (test with a never-reading socket)
  - With no consumer connected the runtime behaves byte-identically to today (composition test); the socket lives under the state dir and is removed on shutdown

### t5 — Clip rider: rolling last-X-seconds frame ring + bounded clip files under the state dir + text reference published on the bus (reachy/behavior/`clip_rider.py`, composed after the tee lands)

- depends on: t4
- covers: c18
- acceptance:
  - A `face_sense`-style background worker keeps the ring — zero encoding on the tick thread (test: tick callback does only a frame handoff); X is configurable with a shipped default; retention is bounded (overwrite-in-place or ring of N, test proves no unbounded growth)
  - The bus carries ONLY a path reference (`is_text_reference_only` holds — test); a missing \[vision\] extra degrades to one logged warning and a permanently-quiet rider, never a crash

### t6 — Embody media profiles: injectable audio source/sink seams with bench (dev-box webcam mic + monitor speakers + AEC-on-capture) and robot (tee socket reader + daemon http playback) profiles (new reachy/embody/media.py)

- covers: c20, h11, c5
- acceptance:
  - Profile selection is config/env only and both profiles run the SAME code paths against fakes (test parametrized over profiles — no isinstance forks)
  - The robot sink calls `play_audio` with transport http EXPLICITLY — a test asserts the sdk fallback is unreachable from the layer
  - No new base dependency: bench capture binds through an optional extra or stdlib route, and pyproject base deps are unchanged (test or diff review)

### t7 — Embody tool registry: the direct-operation action set with containment (new reachy/embody/tools.py) — goto via intents spool, sound via say/harmonics seams, `run_behavior` via spool, create-rule via embody-\* prefixed atomic overlay write + reload spool

- covers: c2, h2, c26, h21, c28, h23
- acceptance:
  - Every tool maps 1:1 onto its sanctioned surface and the registry refuses anything outside the set; an AST test proves no subprocess/os.system/shell reachable from the layer package
  - Rule authoring enforces the embody- prefix, writes temp+rename, and never touches non-embody rules — after any write sequence operator rules are byte-identical (test)
  - Red-team suite: a shell request, an out-of-range goto, an unbounded loop, and a 501-char say are each refused by the existing validators with a named, exported refusal

### t8 — Embody cue intake: bus/feed consumer mapping rule fire/suppress, pat, face, intent and motion lines to cues (new reachy/embody/cues.py; events-cli subscribe with feed-tail fallback)

- covers: c3
- acceptance:
  - A table test maps every runtime line type to its cue text (rule fires included — the react-in-voice input); unknown lines are skipped with a named drop
  - An absent broker degrades to one named drop and the feed-tail fallback; no runtime code change is needed (consumes existing topics/lines only)

### t9 — Duplex session client: ONE lobes /v1/realtime session per process — streams tee/bench audio in, surfaces server-VAD utterances and response audio deltas out, arms with response.create (new reachy/speech/`realtime_duplex.py`)

- depends on: t3
- covers: c4, c17, h13
- acceptance:
  - Against the extended fake server: append-only audio in, utterances + audio deltas out over the same socket; the send surface is pinned to the three legal frame kinds (h13 test)
  - UNGATED by construction: no engagement/name-match import anywhere in the module (boundary test); every failure (refused handshake, mid-stream close, named server error) is a named drop + backoff reconnect, never an exception on the caller thread
  - An optional mute-during-playback seam exists but defaults OFF (the AEC decision), so the self-mute fallback is one config flip if live AEC proves insufficient

### t10 — Embody turn engine: streaming cognition loop — cues + utterances in, streaming HTTP chat-completions turns out (model per request: worker/senses), tool dispatch over the embody registry (new reachy/embody/engine.py, reusing AgentTurnEngine seams where they fit)

- depends on: t8, t9, t7
- covers: c6, h6
- acceptance:
  - Every LLM call streams: a fake SSE server asserts stream=true and that deltas are consumed incrementally; a stalled stream resolves as a named timeout drop and the loop continues
  - The model field is per-request (worker for turns, senses where chosen) resolved from process-scoped env only — no environment.d read, no global mutation (test)
  - Tool results and refusals flow back into the conversation context; the thinking/message/emotion export contract is emitted per turn

### t11 — agent embody verb: the composition root — duplex client + media profile + cue intake + turn engine + export hook wired beside attach (reachy/cli/`_commands`/agent.py + explain catalog entry)

- depends on: t6, t7, t10
- covers: c1, c11, h14, c12, h15, c27, h22
- acceptance:
  - All cognition imports are function-local: `test_zero_llm_boundary` passes with embody registered, parser build loads no cognition module, runtime closure gains no edge (h15)
  - A boundary test over the embody package closure asserts no `reachy_mini` import — I/O is exactly tee socket, bus, spools, daemon http (h14)
  - Every named failure mode reaches the export feed and a named log line; killing the export consumer mid-run leaves the layer alive (h22); the explain catalog entry resolves

### t12 — Embody supervisor: start/stop/status/restart with pid + log under the state dir (new reachy/embody/supervisor.py + verbs in `_commands`/agent.py)

- depends on: t11
- covers: c7, h7, h17, c13, h16, h26
- acceptance:
  - start spawns a detached embody process (pid + log under state dir), repeated start is idempotent, stop escalates SIGTERM-to-SIGKILL and kills ONLY the layer — a test proves runtime/daemon processes and sessions are untouched (h7)
  - One command each way for the operator (h17); no systemd unit ships in v1 and the `_PRESENCE` property tests are untouched (h16)
  - After stop: no process, socket, or unit trace remains, while embody-\* rules persist in the overlay and are enumerable by prefix (h26)

### t13 — Runtime-equivalence proof: with the layer absent/disabled nothing changed — full suite + rubric green, runtime diff limited to the additive tee/clip legs; before-state citation refreshed on merge day

- depends on: t5, t12
- covers: h1, h19
- acceptance:
  - Full pytest -n auto and teken cli doctor --strict green on the merged tree with no embody process running; a reviewed diff of reachy/behavior/ shows only the tee + clip additive legs (h1)
  - On merge day: agent attach still has no transcript cue and publish-only voice, and issue #93 is still open — cited in the PR description (h19)

### t14 — Bench acceptance: the lobes site/ harness converses out loud with embody in bench profile — the two realtime APIs literally speak; every after-state capability demonstrated and archived

- depends on: t12, t2
- covers: c21, h12, c22, c23, h18, h4, h3, h9
- acceptance:
  - At least 3 coherent turns out loud with no self-echo loop, site/ harness unmodified; transcript + export feed + event log archived under docs/evidence/ (h12, c21)
  - Capability demos archived: a rule-fire line (live or replayed) produces a spoken reaction (h3); a clip is described correctly by worker (h9); embody answers speech the runtime gate would drop (h4 ungated half); at least one execution of each direct-operation action class (h18)
  - The echo test passes: embody never answers its own voice under bench AEC (h4 second half)

### t15 — On-box robot-path verification: tee tick-budget measurement, daemon-route playback under a live engine, dual sessions concurrent — the live halves of h5/h8/h10

- depends on: t4, t12
- covers: h5, h8, h10
- acceptance:
  - Tick budget measured before/after the tee exactly as the t27/t28 baselines did — no regression with an active AND a wedged consumer (h10)
  - Layer audio plays audibly via the daemon route while the engine holds media and senses stay at rate — no ~1 Hz throttle (h5)
  - The runtime transcription session and the embody duplex session run concurrently against deployed lobes for at least 5 minutes, both healthy (h8)

### t16 — Docs: operating-guide embodiment-layer section, CLAUDE.md noun catalog entry, README; fold in the issue #131 speech-transport drift fix

- depends on: t14, t15
- covers: c24, c25
- acceptance:
  - The operating guide gains an embodiment-layer section (channels, profiles, lifecycle, the persist-on-disable rule contract) and CLAUDE.md noun catalog + internals are updated; markdownlint green
  - The before/after story and the peripheral on/off + model-swap property are documented with links to the acceptance evidence (c24, c25)
  - CLAUDE.md:428 and docs/operating-reachy.md:1864 now describe `speech_act.py`'s real sdk-via-held-client default with http one variable away (#131 closed by this change)

## Risks

- [unknown_nonblocking] Concurrent /v1/realtime sessions unvalidated upstream (`TTS_VOICE_CONCURRENCY`=1 serialises two conversational sessions — only one here is conversational); t1 probes before the build leans on it (task t1)
- [unknown_nonblocking] Video content-part wire format through the gateway relay is unproven (capability advertised, probed 2026-08-01; model lazy-loads on thor so the first call pays load latency); t2 gates the clip pipeline by policy (task t2)
- [unknown_nonblocking] Robot-path AEC sufficiency: the runtime still self-mutes despite the AEC channel; if live AEC proves insufficient the duplex client's mute-during-playback seam is the one-flip fallback (park v5) (task t15)
- [unknown_nonblocking] Mouth latency and barge-in: v1 plays per-response accumulated audio via upload+play; barge-in is unvalidated upstream (lobes 0.54.1 pacing fix never re-validated) — utterance-level latency is accepted v1 (task t14)
- [unknown_nonblocking] Clip length X default and retention policy decided inside t5 (bounded is the requirement; the exact numbers are tunables) (task t5)
- [unknown_nonblocking] lobes-cli#161: a tool call emitted on a no-tools request returns content null — the turn engine alternates tools/no-tools turns; verify against the deployed gateway inside t10 (task t10)
- [unknown_nonblocking] Bench mic capture binding (webcam at 44.1/48 kHz vs session 24k/16k; capture library choice under the keep-base-light rule) resolved inside t6 (task t6)
- [follow_up] Double-voice collisions (runtime rule-say vs layer voice): accept-and-observe per q7 — coordination becomes a follow-up only if t14/t15 evidence shows real collisions (task t14)
- [follow_up] Face-corroborated action authorization (operator-only commands) — the q8 v1 decision leaves this as the natural hardening follow-up arc
