# embodiment layer

> Reachy gains an embodiment layer: a detachable realtime harness under the agent noun, served by lobes and run beside the runtime on this box. It hears everything in realtime over ONE duplex session (Reachy mic via a runtime tee; AEC to its own speakers), sees faces plus a rolling video clip, reacts in voice when the robot own rules fire, and speaks through Reachy speaker. It operates the robot only through the direct-operation action set: move the head / antennas / body, make sounds, run sets of movements and sounds, and create new rule-triggered actions. Enable/disable at will; the runtime decision loop and every existing condition stay unchanged; every LLM call (senses gemma / worker qwen) streams.
> instruction: Verify by running the full suite and the teken rubric with the layer absent, then a live side-by-side with it enabled; ship the layer as a new verb beside attach under the agent noun.

## Audience

- The operator (Ori) on this dev box, and any external agent that should puppet Reachy by voice: whoever wants the robot to hold a spoken conversation and take direct-operation commands WITHOUT the runtime changing. The lobes site/ browser harness is the secondary audience as the bench conversation partner.

## Before → After

- Before: Today the robot presence is purely symbolic and mute in conversation: agent attach is turn-based and text-cue-driven, its voice tools are publish-only, and no external surface carries the words the robot hears (the #93 gap) — there is no spoken conversation and no way for an external mind to react in voice to the robot reflexes.
- After: With the layer enabled, Reachy holds an out-loud duplex conversation — hears everything via server VAD, answers through its own speaker — sees faces plus a rolling video clip, reacts IN VOICE when its own rules fire (a scratch draws a spoken response), and can be told to move its head/antennas/body, make sounds, run movement sets, and gain new rule-triggered actions. With the layer disabled, the robot is exactly the symbolic presence it is today.

## Why it matters

- It gives Reachy a voice and a conversational mind that switch on and off like a peripheral: cognition stays external, disposable and model-swappable (worker/senses via lobes) while the reflex runtime stays untouched — and the whole thing is testable on this box with no robot attached.

## Requirements

- A new embodiment-layer harness, shaped like agent attach (feed reader + intents spool writer + publish seams, reachy/cli/`_commands`/agent.py), runs beside the live engine on this box; cognition rides the lobes HTTP /v1/chat/completions lane and its ONLY tools are the direct-operation action set an agent operating the CLI has: move the head / antennas / body (the goto intent), make a sound (say / harmonics), run a set of movements and sounds (`run_behavior` over the library), and create new rule-triggered actions (author the rules overlay + behavior reload) — no arbitrary shell.
  - honesty: Every tool maps 1:1 onto an existing sanctioned surface (goto intent, say/harmonics, `run_behavior`, rules overlay + behavior reload) and the registry refuses anything outside the action set — no shell escape exists anywhere in the layer.
- Rule firings and reactions (e.g. a scratch/pat rule) are harness INPUT so it reacts in voice: the export feed and the MQTT bus already carry rule fire/suppress lines and sense.pat events verbatim (reachy/export/runtime.py; mqtt.py publishes `runtime_to_jsonl` byte-identical), so this input needs zero runtime change.
  - honesty: A rule fire arriving on the bus/feed reaches the layer as a cue and produces a spoken reaction in bench test, with zero runtime code change needed for this input path.
- The layer hears in REALTIME through its own duplex session: server VAD, ungated (no engagement gate for the layer — it hears all speech); Reachy has AEC against its own speakers so it hears while speaking. The #93 transcript-on-feed gap stays an independent gap but is no longer this arc's hearing path.
  - honesty: The layer responds to speech the runtime engagement gate would DROP (proving ungated hearing), and never answers its own voice — AEC or a self-mute seam demonstrably holds in the echo test.
- The harness speaks through the daemon HTTP media route with transport=http passed EXPLICITLY — playback.py resolves `REACHY_TRANSPORT` and falls back to sdk, which would open a second ReachyMini (the forbidden move); the http route is live-verified non-contending with the runtime held media session (docs/operating-reachy.md:1873-1884).
  - honesty: With the engine live and holding the media session, layer audio plays audibly via the daemon http route while camera frames and mic audio keep flowing at rate — no sense degradation, no ~1 Hz throttle.
- Every LLM call the harness makes (gemma senses / qwen worker at thor) uses STREAMING mode to avoid timeouts: lobes relays SSE frame-by-frame on /v1/chat/completions, reachy/speech/llm.py already carries the streaming client, and the model is picked per-request via the body model field (role names like worker are accepted by lobes `resolve_model`; worker = unsloth/Qwen3.6-35B-A3B-NVFP4 on thor) with process-scoped env only — environment.d would re-point the runtime engagement classifier too.
  - honesty: No LLM call from the layer ever waits for a complete response: worker and senses calls both stream (SSE deltas observed on the wire), and a stalled stream resolves as a named timeout drop, never a hang.
- Enable/disable is the per-noun supervisor pattern (detached spawn, pid + log under the state dir, start/stop/status — reachy/behavior/supervisor.py is the template); any systemd unit stays orthogonal to the presence pair.
  - honesty: start/stop/status manage a detached layer process with pid + log under the state dir; stop kills only the layer — the runtime, the daemon and their sessions are untouched.
- The layer converses over ONE lobes /v1/realtime duplex session — the official OpenAI realtime duplex shape as lobes serves it (the site/ Astro harness in lobes-cli is the working end-to-end example): ears = server VAD + transcription events, mouth = response audio deltas played out through Reachy's speaker.
  - instruction: Extend reachy/speech/`realtime_wire.py` primitives for the response.\* event family (cite the lobes site/ harness event flow); test offline against tests/`fake_realtime_server.py` extended with response frames; arm with response.create per the lobes contract.
  - honesty: One duplex session per layer process: VAD/transcription events (ears) and response audio deltas (mouth) are both observed over the SAME socket against the deployed lobes gateway, while the runtime transcription session runs concurrently.
- Seeing = face-name + `frame_available` (already on the bus) PLUS a rolling video clip of the last X seconds handed to the worker model — Qwen3.6-35B-A3B consumes video (user-confirmed); the clip moves out-of-band with a text reference on the bus, per the bus `is_text_reference_only` design.
  - instruction: Implement the clip rider as a `face_sense`-style background worker keeping a rolling frame ring; write clips under the state dir and publish a path reference on the bus; feed the worker model over the streaming HTTP lane.
  - honesty: The worker model (Qwen3.6-35B-A3B) returns a correct description of a rolling clip recorded from the robot camera (webcam in bench), and the clip travels as an out-of-band reference — never embedded on the bus.
- Raw media reaches the layer via a RUNTIME TEE (additive export leg): the AudioPump gains a third consumer that tees mic chunks to a local out-of-band sink (unix socket / FIFO), and a rolling-clip rider keeps the last X seconds of camera frames, publishing a text reference on the bus per its `is_text_reference_only` design. The runtime decision loop stays untouched; both hearings coexist — the runtime transcription session (reflex) and the layer duplex session (conversation).
  - instruction: Implement as a third AudioPump consumer writing mono chunks to a local out-of-band sink (unix socket/FIFO under the state dir); measure tick budget before/after on-box exactly as the t27/t28 baselines did.
  - honesty: With the tee active the runtime senses stay at rate (no tick-budget regression measured on-box) and the layer receives contiguous mono audio; a dead or absent layer never stalls or slows the runtime.
- The layer media endpoints are an INJECTABLE SEAM with two profiles: the robot path (the c19 runtime tee for mic + the daemon http route for the speaker) and the bench path — the dev-box webcam microphone in, monitor speakers out, with browser-style AEC on capture — so the whole layer is testable on this box with no robot attached.
  - instruction: Inject audio source and sink as constructor seams on the layer; bench profile binds dev-box devices, robot profile binds the tee socket + daemon http playback; select by config/env only.
  - honesty: The same layer code runs the bench profile (webcam mic + monitor speakers + browser-style AEC) and the robot profile (tee + daemon route) by configuration only — no forked code paths.
- Layer-authored rules are NAMESPACED (a distinguishing id prefix, e.g. embody-\*) and written ATOMICALLY (temp + rename of the overlay), never rewriting operator- or shipped-layer rules — so they are enumerable and removable as a set, and an operator edit can never be clobbered by a layer write (rules.py merges per rule id; the overlay file itself has no lock).
  - honesty: After any sequence of layer writes, operator-authored rules are byte-identical, and one command (or grep by prefix) enumerates exactly the rules the layer created.
- The layer ships the repo observability contract: the same thinking/message/emotion NDJSON export agent attach has, plus named senselog-style drops for every failure mode (dead session, dead LLM, dead playback, refused rule/action) — no silent no-op anywhere, and a disconnecting export consumer never kills the layer.
  - honesty: Every named failure mode appears as a named drop/log line and on the export feed in bench test; killing the export consumer mid-conversation leaves the layer running.
- The tee fans out the ONE per-tick chunk `_AudioTap` already takes — never a second take() on the AudioPump (the documented half-audio defect class) — and a slow or dead tee consumer DROPS chunks via a bounded queue, never backpressuring the 20 ms tick.
  - honesty: With a wedged tee consumer the runtime tick budget and its own hearing are unaffected (measured on-box), and the audio the layer receives is the same chunk stream `_AudioTap` fanned out.

## Honesty conditions

- With the layer disabled or absent, every existing verb and behavior runs exactly as today (full suite green, zero runtime diff); with it enabled, the runtime process differs only by additive export legs (tee + clip).
- The layer wire client sends only session-config, audio-append and response.create frames over the socket; tool calls appear exclusively in HTTP chat-completions requests — if lobes later ships socket tool-calls, adopting them is a new arc.
- The layer constructs no ReachyMini and opens no media session — a boundary test over the layer module closure asserts no `reachy_mini` import; only the tee, the bus, the spools and the daemon http routes appear in its I/O.
- tests/`test_zero_llm_boundary.py` passes with the layer registered: the parser build loads no cognition module, and the runtime closure gains no import edge — any pin update is documented in the same change.
- `_PRESENCE` remains exactly the demo/runtime pair (its property tests unchanged); any layer unit ships outside the pair, and every enable sequence leaves the runtime enabled.
- The site-harness-to-layer spoken conversation completes at least 3 coherent turns out loud with no self-echo loop, recorded as live evidence.
- The operator starts and stops the layer from this box with one command each way, and the lobes site/ harness serves as the bench conversation partner without modification.
- Each after-state capability appears in the acceptance evidence: a spoken conversation, face + clip seeing, a rule-fire spoken reaction, and at least one execution of each direct-operation action class.
- The before-state is cited, not assumed: on today's main, agent attach has no transcript cue and publish-only voice (reachy/cli/`_commands`/agent.py), and issue #93 is still open — no external surface carries heard words.
- Disabling the layer removes every process, socket, and unit trace, and swapping the worker/senses model needs only configuration; layer-authored embody-\* rules deliberately PERSIST (q6: teaching survives disable) and stay enumerable and removable by prefix.
- A red-team transcript (asking for shell, out-of-range motion, an unbounded loop, an over-long say) shows every attempt refused by the existing validators, with each refusal visible in the export feed.
- A real clip sent as video content parts to model=worker over the deployed gateway returns a correct description BEFORE the clip pipeline is built (a wave-1 probe task in the plan).

## Success signals

- Acceptance: the lobes site/ browser harness (its own realtime session, browser AEC, monitor speakers + webcam mic) holds an out-loud spoken conversation with the embodiment layer realtime session — the realtime API of the website literally speaks with the realtime API of Reachy, end to end, and both sides stay coherent (no self-echo loops) across multiple turns.
  - instruction: Acceptance script: open the lobes site/ harness in Chrome on this box, start the layer in bench profile, hold the conversation; archive the transcript + event log under docs/evidence/.

## Scope / boundaries

- Tool calls and per-session model choice still do not exist on the /v1/realtime socket (explicitly parked upstream, lobes `_conversation.py` + `_session.py`) — so the duplex session is the layer's EARS + MOUTH, and the tool loop rides the streaming HTTP lane beside it.
- The harness never opens an SDK media session or a second ReachyMini (single-SDK-owner model); every sense arrives via the feed, the bus, or the spool result surfaces; it never calls `refuse_if_engine_live` — a live engine is its precondition, not its rival.
- The zero-LLM boundary stays machine-checked: cognition imports in the new command module are function-local (the parser forbidden-set pin, tests/`test_zero_llm_boundary.py`), `_commands`/behavior.py never gains an import edge to the harness, and nothing lands in reachy/behavior/ or reachy/motion/ — under exactly these conditions a second cognition root is legal today.
- reachy/service/manager.py `_PRESENCE` stays the closed demo/runtime pair — adding a harness unit there would disable the very runtime the harness needs; its unit, if any, is orthogonal.
- The blast radius of a hostile or confused utterance is bounded by the action set and the EXISTING fail-closed validators: rules validation (code-smell refusal, bounded lifetimes, 500-char say cap), per-axis goto bounds + the 10 s duration cap, `run_behavior` unbounded-lifetime refusal — and the layer has no shell, filesystem, or network tools to widen it. An ungated ear never widens actuation.

## Non-goals

- No lobes-side work lands in this repo: realtime tool-calling, per-session model selection, and any true socket mode are lobes-cli follow-ups to file upstream (the events-cli issue-3 pattern); this repo consumes what port 8001 serves today.
- No new sense loop noun, no second local VAD, no cognition re-entering the runtime — the retired-surfaces arc stays retired.

## Assumptions

- Without changing Reachy code means: the decision loop and every existing condition stay untouched and runnable; additive export-surface legs (an out-of-band media tee or clip reference) are sanctioned attach points, not violations.
- The Import question resolves: the harness composes by importing reachy modules exactly as agent attach does, and its direct-operation tools call the same in-process seams (intents spool submit, rules overlay write + reload spool, tts + playback) — no subprocess, no shell anywhere in the layer.
- Video-over-API works end-to-end on the deployed stack: the live gateway advertises `video_understanding` as a worker-role responsibility (probed 2026-08-01: proxied to thor, ready=true, loaded=false so the first call pays a model load), but the wire format — video content parts through the chat-completions relay — is unproven until a real clip round-trips.

## Scope exploration

- `s1` — `lobes-cli realtime bridge (lobes/realtime/app.py, _session.py, _conversation.py, _wire.py)`: Full-duplex voice-to-voice shipped upstream (#151): base64 append events in, server VAD + transcription out, response.create arms text+audio out. NO tool calls over the socket, NO session.update, NO per-session model (all explicitly parked); grep finds zero socketmode hits anywhere in lobes — the WS route itself is the closest thing
  - seeds: `c10`, `c14`
- `s2` — `lobes-cli routing + catalog (lobes/gateway/_routing.py resolve_model, lobes/catalog.py, SSE relay spec)`: worker role = unsloth/Qwen3.6-35B-A3B-NVFP4 on thor, senses = gemma-4-12B; the HTTP lane picks a model per-request via the body model field, accepting role names and tier aliases; SSE streaming is relayed frame-by-frame (shipped spec) — the streaming requirement is satisfiable today
  - seeds: `c6`
- `s3` — `reachy/cli/_commands/agent.py (agent attach)`: The composition pattern to copy: NDJSON feed reader (stdin/FIFO), private `_CUE_MAPPERS` (sense/rule/intent/motion to cues), intents-spool tool writes, publish-only voice tools, foreground-only (no supervisor, no unit). Notably it has NO transcript cue and NO `describe_scene` tool
  - seeds: `c2`, `c9`
- `s4` — `reachy/export/runtime.py + docs/export-schema.md`: The feed carries sense/rule/intent/motion lines; rule fire/suppress and sense.pat are present (the react-in-voice-to-a-scratch input exists today); transcript is absent by pinned contract (export-schema.md:231, issue #93) — the one gap between the harness and hearing words
  - seeds: `c3`, `c4`
- `s5` — `reachy/export/mqtt.py + events_client.py (the #128 bus)`: reachy/events/{source}/{type} mirrors `runtime_to_jsonl` byte-identical (so the #93 gap propagates); retained reachy/state/\* carries availability verdicts not sense values; media is structurally barred from the bus (text references only) — richer seeing needs its own arc (q3); no subscriber exists in-repo, the harness would be the first
  - seeds: `c3`, `c4`
- `s6` — `reachy/behavior/speech_act.py + reachy/speech/playback.py + docs/operating-reachy.md:1873-1884`: The daemon http media route is live-verified non-contending with the runtime held media session (ALSA sink is shared; the single-owner model constrains the media session only). BUT `play_audio` and reachy say resolve `REACHY_TRANSPORT` and fall back to sdk — a second ReachyMini — so the harness must pass http explicitly. Side find: CLAUDE.md:428 still documents the pre-7ea6878 http default while `speech_act.py`:155 now defaults sdk-via-held-client (doc drift, issue to file)
  - seeds: `c5`, `c11`
- `s7` — `reachy/behavior/rules.py + reachy/cli/_commands/behavior.py (reload)`: Rules authoring = edit the overlay `state_dir`/behavior/rules.toml then the behavior reload verb (dedicated atomic spool, engine-side RulesLoader.reload never raises, floor = shipped layer); validation is fail-closed (bounded-lifetime, 500-char say cap, code-smell refusal). There is no rules add/edit verb — the harness authors TOML exactly like the operator
  - seeds: `c2`
- `s8` — `reachy/behavior/intents.py + control.py (the spool)`: The sanctioned many-writer external-command surface: atomic temp+rename writes under `state_dir`/behavior/intents, single-reader drain in submission order, five kinds (`run_behavior`/`declare_goal`/`set_mode`/`set_inhibition`/goto), unbounded `run_behavior` refused — any external process may write it by design
  - seeds: `c2`
- `s9` — `tests/test_zero_llm_boundary.py`: A second cognition root is legal today: the parser pin only requires function-local cognition imports in command modules; `test_cognition_survives_only_behind_the_agent_noun` asserts existence, not exclusivity (quoted assertion checked); the one-LLM-edge equality pin bites only if `_commands`/behavior.py gains an import edge to the harness — informs the where-does-it-live question (q1)
  - seeds: `c12`
- `s10` — `reachy/service/units.py + manager.py + the four per-noun supervisors`: `_PRESENCE` is a closed mutually-exclusive pair pinned by property tests — a harness unit there would disable the runtime it needs; the per-noun supervisor (pid+log under state dir, SIGTERM-SIGKILL stop, idempotent start) is the enable/disable shape with zero runtime change
  - seeds: `c7`, `c13`
- `s11` — `reachy/speech/llm.py`: Streaming + non-streaming pure-urllib client already exists; `REACHY_OPENAI_`\* precedence is by presence not truthiness; a process-scoped model override is the clean way to pick worker@thor — environment.d would also re-point the runtime engagement classifier
  - seeds: `c6`
- `s12` — `reachy/behavior/transcript_sense.py (admission)`: Sense.transcript carries only engagement-gate-ADMITTED utterances; drops never publish (the only trace is a 60-char senselog line) — so the #93 export shape inherits admitted-only unless deliberately widened (q4 decides what the harness may hear)
  - seeds: `c4`
- `s13` — `lobes-cli site/ (Astro realtime browser harness)`: The working duplex example the user pointed at: mic in, event stream, audio out against /v1/realtime — local-only, never deployed, CI only builds it; proves the official-OpenAI-shaped duplex loop end-to-end against lobes
  - seeds: `c17`
- `s14` — `challenge pass / adjacent-systems lens: reachy/behavior/speech_act.py mute_until + the engagement gate vs the layer voice`: The runtime self-mute covers only ITS OWN SpeechActuator speech; the layer speaking via the daemon route is invisible to it, so the runtime may transcribe the layer voice and even name-match on it — seeded the double-voice question (q7); AEC coverage of daemon-route playback is unverified (park v5 already tracks AEC sufficiency)
- `s15` — `challenge pass / unstated-assumptions lens: deployed lobes /capabilities (live GET probe 2026-08-01)`: worker advertises `image_understanding` + `video_understanding` (proxied to thor, ready, lazy-loaded); senses is proxied to orin not thor (deployment drift the role-name selection makes harmless); /v1/models returns an empty list keyless — the video capability is advertised but the content-part wire format is unproven, seeding the probe-gated assumption
  - seeds: `c30`
- `s16` — `challenge pass / actors-and-lifecycle lens: rules overlay merge semantics (rules.py per-id merge, tombstones) vs h20 no-residue`: Layer-authored rules PERSIST in the overlay after the layer stops — they run inside the runtime — which contradicts a broad reading of h20 (no residue on disable); seeded the namespacing requirement and the persist-or-sweep question (q6)
  - seeds: `c26`
- `s17` — `challenge pass / security lens: goto_intent bounds, rules fail-closed validation, the tool registry surface`: An ungated ear feeding an LLM with actuation is a prompt-injection surface; existing validators (per-axis bounds, 10 s cap, bounded lifetimes, code-smell refusal, say cap) bound the blast radius and the no-shell tool set closes the rest — seeded the containment boundary and the action-authorization question (q8)
  - seeds: `c28`
- `s18` — `challenge pass / concurrency lens: behavior/audio_pump.py _AudioTap single-take contract; clip rider; motion path`: The tee must fan out the one per-tick chunk (a second take() halves everyone audio — documented defect class), seeding c29; CLEAN on the clip rider (`face_sense` background-worker pattern keeps encoding off the tick) and on motion (layer actions ride the spool into engine arbitration, no MotionQueue contention)
  - seeds: `c29`
- `s19` — `challenge pass / observability-containment lens: agent attach --export precedent + the senselog ethos`: The spec had NO observability requirement for the layer — a conversational robot with an invisible mind is undebuggable and violates the every-drop-named house rule; seeded the export-contract requirement
  - seeds: `c27`
- `s20` — `challenge pass / reversibility-rollback lens: supervisor stop, rules tombstones (enabled=false), set_inhibition, behavior stop`: CLEAN: one-command stop kills the layer; tombstones can disable any rule; inhibition and behavior stop bound motion — the only reversibility gap found is q6 (whether layer rules outlive the layer), already raised

## Decisions

- The embodiment layer lives under the agent noun — a new verb beside attach.
- Layer-authored rules PERSIST after disable — the robot keeps what it was taught; disable removes only process, socket, and unit state (q6).
- Double-voice v1 = accept and observe: the runtime rule-say voice and the layer voice both stay live; coordination becomes a follow-up only if the acceptance evidence shows real collisions (q7).
- Action authorization v1 = anyone may command: containment comes from the c28 boundary, not from speaker identity; face-corroborated authorization is a parked follow-up (q8).

## Open parks

- [unknown_nonblocking] Concurrent /v1/realtime sessions are architecturally isolated but explicitly unvalidated upstream (lobes docs/realtime-pipeline.md), and `TTS_VOICE_CONCURRENCY` defaults to 1 so two conversational sessions serialise synthesis — matters only if the harness later takes the socket mouth leg.
- [unknown_nonblocking] lobes-cli issue 161 — a tool call emitted on a request carrying no tools is parsed and dropped, returning content null — may bite an alternating tools/no-tools harness loop; verify against the deployed gateway before relying on mixed turns.
- [unknown_nonblocking] Clip length X seconds for the rolling video clip is a tunable, undecided.
- [unknown_nonblocking] Whether hardware AEC alone suffices without a self-mute seam: the runtime still wires `mute_until` into its transcript sense despite the AEC channel (reachy/robot/`audio_shape.py` `AEC_CHANNEL` 0) — verify live before dropping self-mute for the layer.
- [unknown_nonblocking] Mouth playback mechanics: the daemon route is upload+play per clip, so v1 plays per-response accumulated audio (utterance-level latency); mid-response chunked streaming and barge-in (unvalidated upstream — lobes 0.54.1 pacing fix never re-validated) are undecided.
- [unknown_nonblocking] Clip retention: the rolling clip under the state dir must be bounded (overwrite-in-place, ring of N, or TTL) — policy undecided, unbounded growth is not an option.
- [unknown_nonblocking] Bench resample chain: webcam mics deliver 44.1/48 kHz while the session negotiates 24 k/16 k `input_sample_rate` — a bench-profile config/resample concern, not a design fork.
- [follow_up] Voice coordination (layer-yields or runtime-yields) — pick one only if the acceptance evidence shows real double-voice collisions (q7 decision).
- [follow_up] Face-corroborated action authorization (operator-only direct-operation commands) — the q8 v1 decision leaves this as the natural hardening step.

## Resolved vagueness

- [unknown_nonblocking] The exact speaking leg is a /think design choice: reachy say subprocess, in-process tts plus `play_audio` http, or realtime audio-out relayed to the daemon play route — all three are feasible today. — resolved: Decided: the mouth is realtime audio-out from the duplex session, played through Reachy speaker via the non-contending daemon http route; say/harmonics remain as deliberate action tools.
