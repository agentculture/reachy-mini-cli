# agent senses + poses + durable speech (issues 66/67/68)

> Reachy's agent cognition closes its three post-0.32 gaps: touch reaches the brain (a pat becomes a sense cue the agent can answer), the pose catalog is advertised to the model (enum + a contentment pose, so tool choices stop silently no-oping), and speech survives deployment (gateway-route TTS reachable out of the box, an audio latch that recovers, and a mute state an operator can see)
> instruction: build as two PRs (A: issues 66+67 agent senses, B: issue 68 speech durability); verify each on the live robot: a real pat produces an agent response with zero false-fires at idle, an unknown pose key self-corrects from the error tool-result, and a TTS stop/start round-trip recovers speech without a service restart

## Audience

- the operator who pats and talks to the robot (touch gets acknowledged, speech survives outages), the agent model itself (a truthful, complete tool schema), and any operator of a standard model-gear box (TTS reachable with only the existing REACHY_OPENAI_* env set)

## Before → After

- Before: a pat is a hardwired reflex the LLM never hears about — and the deployed detector false-fires at saturation (147 detections in 51 untouched minutes) while real pats get masked by reaction windows; 5 of 8 catalog keys are invisible to the model and wrong guesses silently no-op to neutral; two consecutive TTS failures mute the boot presence for the life of the process, and the default TTS URL is unreachable on the reference deployment in the first place
- After: a pat produces the reflex lean AND a sense cue the agent answers in its next turn, and touch cues are trustworthy because false-fires are gone; the model sees the full pose catalog (including a contentment pose) and self-corrects an unknown key from the error tool-result; TTS defaults to the lobes gateway, a TTS outage self-heals via backoff retry, and the mute state is visible in status --json plus periodic log warnings

## Why it matters

- 0.32 gave the robot agency through tool calls, but an agent is only as good as what it can perceive, do, and keep doing — today it senses only words, guesses at its own body vocabulary, and loses its voice permanently to one transient outage

## Requirements

- issue 66: EventBuffer (reachy/speech/events.py) gains a feed_pat alongside feed_transcript — a thread-safe cue append carrying kind + intensity (scratch/side_pat, level1/level2), e.g. 'felt a gentle scratch on the head'; PatHook (reachy/motion/listen_pat.py) calls it on each detection, keeping the reflex (PatReaction lean stays instant, never waits on the LLM)
  - instruction: add EventBuffer.feed_pat(kind, level) following feed_transcript's shape (cue text like: felt a gentle scratch on the head); call it from PatHook._sense_and_maybe_react right after reaction.react(); TDD in test_speech_events + test_listen_pat
  - honesty: with a fake detector forcing one detection inside the live loop, exactly one felt-a-pat cue naming kind and intensity lands in the shared EventBuffer, and the PatReaction lean is enqueued without waiting on any LLM work
- issue 66 wiring: _build_pat_hook (reachy/cli/_commands/listen.py:437-457) gains the shared-buffer seam and the live composition threads the SAME EventBuffer it already shares with ThinkHook/AgentTurnEngine/TranscribeHook (listen.py:510-522, 577-597) into PatHook
  - instruction: add a buffer parameter to _build_pat_hook and pass the shared buf the --live composition already builds (same object handed to _build_think_hook/_build_agent_think_hook and TranscribeHook); assert object identity in test_listen_cognition_agent
  - honesty: a composition test asserts the SAME EventBuffer object (identity) is shared by PatHook, ThinkHook, and TranscribeHook under listen run --live --cognition agent
- issue 66 rate limit: at most one cue per reaction cycle — PatHook already pauses sensing for reaction_duration (~3.5s) via _reacting_until (listen_pat.py:108-113), a built-in floor; sustained stroking should coalesce rather than stream repeated cues
  - instruction: feed the cue at detection time only — PatHook already pauses sensing during the reaction window, giving one cue per cycle; add a coalescing guard only if the fidelity fix (c20) still lets bursts through; fake-clock test in test_listen_pat
  - honesty: driving the detector through a continuous-stroke sequence on a fake clock yields at most one cue per reaction window
- issue 66: DEFAULT_AGENT_SYSTEM_PROMPT (agent_turn.py:96-105) must name touch as a perception — today it says only microphone and camera, so the model has no frame for a felt-a-pat cue
  - instruction: extend DEFAULT_AGENT_SYSTEM_PROMPT's perception sentence to include touch/being patted; string assertion in test_agent_turn
  - honesty: a test asserts DEFAULT_AGENT_SYSTEM_PROMPT names touch/pat among the perceptions
- issue 67: the apply_pose parameters schema advertises the catalog — an enum generated from the loaded catalog keys (Catalog.keys() exists, expressions.py:233-234) instead of the current free string naming 3 of 8 keys as examples (tools.py:219-237); adding a TOML entry then reaches the model with no code change
  - instruction: thread catalog keys into ToolRegistry (parameter defaulting to reachy.speech.expressions catalog keys) and emit them as the emoji property's enum in _apply_pose_tool; test in test_speech_tools with a temp TOML proving a new key reaches the schema with no code change
  - honesty: the published apply_pose parameters carry an enum equal to the loaded catalog keys, and a test loading a temp TOML with an extra key sees that key appear in the published schema with no code change
- issue 67: a contentment/enjoy pose (proposed key: smiling-face emoji) lands in expressions.toml within the AMPLITUDE GUIDE ranges; think expressions check (find_too_similar) must pass, especially distance from the slightly-smiling entry
  - instruction: add the contentment entry to expressions.toml within the AMPLITUDE GUIDE ranges (antennas gently forward, slight head_z lift, small chin-up); run reachy think expressions check and tune until no too-similar pair
  - honesty: the new TOML entry stays within the AMPLITUDE GUIDE ranges and think expressions check exits ok with no too-similar pair involving it
- issue 67: an unknown emoji becomes observable to the MODEL — the apply_pose handler validates membership and returns an error tool-result naming valid keys (the model can self-correct within the same turn), replacing the silent neutral no-op (get_pose never raises, expressions.py:182-194)
  - instruction: in _make_pose_handler, validate the emoji against the injected keys before express(); on miss raise ValueError naming the valid keys (dispatch already converts it to the error tool-result the model reads); test in test_speech_tools
  - honesty: dispatching apply_pose with an emoji absent from the catalog returns an error tool-result that names the valid keys and enqueues no motion
- issue 68: the one-way audio latch becomes recoverable in BOTH engines — cognition.py (_note_audio_failure 464-484; guard at 435 makes the streak-reset at 456 unreachable once muted) AND its duplicate in agent_turn.py (388-414; _dispatch_audio 360-386 skips dispatch when muted); the deployed boot path runs the agent engine, so fixing only cognition.py fixes the wrong latch; retry with backoff, clear the latch on success
  - instruction: replace the boolean _audio_muted latch in BOTH cognition.py and agent_turn.py with a retry-after policy (muted_until via injected clock, exponential backoff with a cap); a successful synth clears streak and mute; fake-clock tests in test_cognition_audio_optional + test_agent_turn
  - honesty: in BOTH engines' suites (test_cognition_audio_optional and test_agent_turn): consecutive failures latch mute at the threshold; after the synth seam recovers, a backoff-scheduled retry succeeds, clears the latch, and speech resumes — fake clock, no real TTS, no process restart
- issue 68: the muted state becomes operator-visible — surfaced in status --json via the sidecar pattern (think.voice precedent, _commands/think.py:394-424, 700-702) and warned periodically (not once) while muted
  - instruction: write an audio-mute sidecar (pattern: the think.voice sidecar in _commands/think.py) while muted, clear on recovery; report it in think status --json; repeat the mute warning periodically instead of once
  - honesty: while muted, status --json reports the muted state via the sidecar and the log carries repeated periodic mute warnings; on recovery the sidecar clears
- issue 66 prerequisite: eliminate the pat false-fire loop. Live journal evidence (2026-07-17): 147 detections in 51 minutes, sustained bursts of 10-14/min at the reaction-window saturation rate while nobody touched the robot (11:23-11:29 and 11:47-11:51, still firing at measurement ~7s apart). Mechanism: commanded_head is the TARGET of the last dispatched goto, but a minjerk move takes over a second in transit, so commanded-vs-actual deviation reads the robot's own motion as external force; post-reaction idle-resume moves re-trigger immediately, and real pats get masked because sensing pauses during reaction windows. Fix direction: suppress sensing while a commanded move is in flight and/or re-baseline against the actual pose when the reaction window closes
  - instruction: gate PatHook sensing on motion-in-flight: skip detector updates while a commanded move is executing (queue busy / within the move's duration) and re-baseline the detector when the reaction window closes; verify on-robot: 30 untouched idle minutes yield zero detections and one real pat still detects
  - honesty: on the live robot: 30 untouched idle minutes produce zero pat detections in the journal, and a deliberate pat in the same session still detects and reacts

## Honesty conditions

- all three headline capabilities are demonstrated on the robot: touch reaches cognition (a pat yields an agent response), the advertised catalog matches the TOML (enum equals keys), and speech survives a TTS outage (mute then self-recovery, no restart)
- an integration test with a fake turn_fn shows a lone pat cue (no words in the buffer) firing an agent turn
- no change lands in reachy/speech/engagement.py or the TranscribeHook gate path; pat cues reach the buffer without passing decide_engagement
- the tools.py import-boundary check still holds: no import of reachy.speech.llm, reachy.speech.events, or reachy.motion in reachy/speech/tools.py
- the diffs of both PRs contain no change to reachy/cli/_commands/say.py and say's existing boundary tests stay green
- each audience benefit is observable in the on-robot verify: the operator gets a touch response and durable speech, the model's published schema equals the catalog, and a box with only REACHY_OPENAI_* env set reaches TTS
- every after-state capability is demonstrated in one on-robot session: pat to cue to agent reply or pose with no false-fires; unknown emoji corrected in-turn; TTS stop/start shows mute in status --json then speech resumes
- the before-state is evidenced by the repo and journal as read on 2026-07-17: no feed_pat call exists, the schema has no enum, the latch has no reset path, and the journal shows the 147-detection hour
- the three gaps are exactly the ones the deployed boot configuration exhibits: agent engine with transcripts as its only sense, guessed pose keys, and a chatterbox default that cannot reach the reference box
- each signal is checked and recorded in the delivery summary: a journal excerpt for the untouched idle window, session transcript for the pat and TTS round-trips, and CI links for suite plus rubric

## Success signals

- on-robot acceptance: 30 untouched idle minutes log zero pat detections, then one real pat yields both the reflex lean and an agent-turn response (speech or pose); stopping TTS mutes the robot (visible in status --json), restarting TTS un-mutes it with no service restart; think expressions check passes with the new pose; full suite + teken rubric green; two merged PRs each with a version bump

## Scope / boundaries

- the layered engagement gate is untouched: it lives inside TranscribeHook and gates transcripts only (_decide at listen_transcribe.py:328 precedes feed_transcript at 338); pat cues land on the buffer directly and never pass through decide_engagement
- tools.py keeps its documented import boundary (never imports reachy.speech.llm, reachy.speech.events, or reachy.motion — tools.py:43-51); catalog keys arrive via the peer module reachy.speech.expressions or by injection at the composition layer
- the say noun is untouched: the route default lives in reachy/speech/tts.py behind synthesize (DEFAULT_TTS_ROUTE at tts.py:92), which say already calls through — no change to _commands/say.py or its no-llm/no-events import contract

## Non-goals

- feed_vision wiring stays issue 32 — it has no live caller today (only the docstring example at events.py:168) and this work does not add one
- the live robot-does-not-speak runtime diagnosis (why almost no utterances get transcribed: STT reachability / utterance endpointing) is a separate investigation — these three fixes do not claim to resolve it
- no new export block type: the docs/export-schema.md wire contract (exactly thinking/message/emotion) is unchanged this pass — mute surfacing rides status --json and logs, not the JSONL feed

## Assumptions

- issue 66: feeding a pat cue automatically makes a pat turn-worthy with ZERO engine change — AgentTurnEngine.run_turn is cue-gated (agent_turn.py:279-281: any cue in the snapshot fires a turn); touch bypasses the engagement gate by design because a pat is inherently addressed
- every change lands in the existing per-module test suites (test_listen_pat, test_speech_events, test_speech_tools, test_agent_turn, test_cognition_audio_optional, test_speech_tts, test_service_units, test_listen_cognition_agent, test_expressions all exist in tests/); no new test infrastructure

## Scope exploration

- `s1` — `reachy/motion/listen_pat.py`: PatHook is reflex-only: imports only CliError/pat_signal/PatDetector/PatReaction/MotionQueue, no EventBuffer, no feed_* call; self.events (lines 87, 147) is a plain counter; the reaction window (_reacting_until, lines 108-113) already pauses sensing for reaction_duration per detection — a natural one-cue-per-cycle rate floor
  - seeds: `c2`, `c5`
- `s2` — `reachy/speech/events.py`: EventBuffer has exactly three feeds — feed_doa (189), feed_vision (234), feed_transcript (277) — all funnel through the thread-safe _append (333); feed_transcript is the template shape for a feed_pat; feed_vision has no live caller (only the docstring example at 168), which is issue 32
  - seeds: `c2`, `c15`
- `s3` — `reachy/speech/agent_turn.py`: run_turn is cue-gated: snapshot then any-cues-fires-a-turn (279-281), so a fed pat cue is turn-worthy with no engine change; the audio latch is DUPLICATED here (_audio_muted ctor-only False at 246, _note_audio_failure 388-414, _dispatch_audio 360-386 skips dispatch when muted) and the deployed boot unit runs THIS engine; DEFAULT_AGENT_SYSTEM_PROMPT (96-105) names microphone and camera only — no touch
  - seeds: `c4`, `c6`, `c10`
- `s4` — `reachy/cli/_commands/listen.py`: _build_pat_hook (437-457) takes no buffer — PatHook is the only live-sense hook NOT handed the shared EventBuffer; _build_think_hook (460-527) and _build_agent_think_hook (530-603) both thread one shared buf, and the agent path composes ToolRegistry at 578-590 with injected seams (express/speak_engine/harmonic_engine/play) — the natural injection point for catalog keys
  - seeds: `c3`, `c7`
- `s5` — `reachy/speech/tools.py`: _apply_pose_tool (219-237) publishes a free-form string with no enum, naming 3 of 8 keys as examples; the module docstring (43-51) bounds imports away from llm/events/motion but NOT from the peer reachy.speech.expressions; dispatch (321-354) never raises and returns error tool-results the model reads — the right channel for an unknown-emoji correction
  - seeds: `c7`, `c9`, `c13`
- `s6` — `reachy/speech/expressions.py + expressions.toml`: Catalog exposes keys() (233-234) so the enum has a ready data source; get_pose silently falls back to neutral and never raises (182-194) — the silent no-op; the TOML holds 8 keys (neutral + 7 emoji) with no contentment entry, and its AMPLITUDE GUIDE header gives the safe per-axis ranges a new pose must respect
  - seeds: `c7`, `c8`
- `s7` — `reachy/speech/cognition.py`: the original one-way latch: DEFAULT_AUDIO_MUTE_THRESHOLD=2 (99); once muted the worker guard (435) skips _speak_clip so the streak-reset (456) is unreachable; _note_audio_failure (464-484) logs on the first failure of a streak and once at latch — nothing repeats afterwards, so an operator sees a healthy service that simply never speaks
  - seeds: `c10`, `c11`
- `s8` — `reachy/speech/tts.py`: DEFAULT_TTS_ROUTE=chatterbox (92) with DEFAULT_TTS_URL=localhost:9000 (72), which the standard model-gear box never publishes (EXPOSE-only container); the openai route (/v1/audio/speech) already exists and shares REACHY_OPENAI_URL_BASE + Bearer key with the LLM by documented convention (94-101), live-verified 200/PCM16 on 2026-07-17 — the route flip is a tts.py-level decision, say composition untouched
  - seeds: `c14`, `c18`
- `s9` — `reachy/service/units.py`: pure unit-text renderers; the generated live unit passes opt-in flags (--transcribe --cognition agent --voice-engine harmonic, live_exec_start 91-113) but sets NO environment — the dev box needed a hand-made tts.conf drop-in to reach TTS; the unit already carries deployment-default choices, so an Environment= line has precedent
  - seeds: `c18`
- `s10` — `reachy/cli/_commands/think.py`: the think.voice sidecar (constants 77-81, read/write/clear 394-424, bracketed around the run at 700-702) is the established pattern for exposing a running loop's in-process state through status --json — the same mechanism fits an audio-muted flag
  - seeds: `c11`
- `s11` — `docs/export-schema.md`: the wire contract defines exactly three block types (thinking/message/emotion); adding a mute/status block is a versioned wire-format change affecting every subscriber — kept out of this pass
  - seeds: `c17`
- `s12` — `reachy/motion/listen_transcribe.py`: the engagement gate lives inside TranscribeHook and gates transcripts only: _decide (328) runs before feed_transcript (338); a PatHook feeding the buffer directly is structurally outside the gate — no gate change needed or wanted for touch
  - seeds: `c12`
- `s13` — `tests/`: per-module suites already exist for every touched surface: test_listen_pat, test_speech_events, test_speech_tools, test_agent_turn (+cortex integration), test_cognition_audio_optional, test_speech_tts, test_service_units, test_listen_cognition_agent, test_expressions — extensions, not new infrastructure
  - seeds: `c19`
- `s14` — `reachy-live.service journal (2026-07-17 11:00-11:51)`: 147 pat detections in 51 minutes, in bursts of 10-14/min — the reaction-window saturation rate — across two windows (11:23-11:29, 11:47-11:51) while nobody touched the robot, still firing ~7s apart at measurement; resolves the fidelity unknown: the detector false-fires on the loop's own goto transit, and real pats are masked by the resulting wall-to-wall reaction windows
  - seeds: `c20`, `c23`

## Decisions

- TTS route default flips globally: DEFAULT_TTS_ROUTE = openai in reachy/speech/tts.py; chatterbox stays selectable via REACHY_TTS_ROUTE=chatterbox; no Environment= line in the generated live unit (user decision resolving q1)
- delivery is two PRs: PR A = issues 66+67 (agent senses: fidelity fix + feed_pat + catalog enum + contentment pose), PR B = issue 68 (speech durability: route default + recoverable latch in both engines + visible mute); each with its own version bump (user decision resolving q2)

## Open / follow-up

- pat detection fidelity is unverified: the 2026-07-17 live capture (11:19-11:20) showed six full reaction cycles in ~80s of casual interaction — if PatDetector false-fires on servo lag during or after its own reaction, feed_pat would stream false touch cues to the LLM; must verify on-robot (at rest, while speaking, during idle wander) before touch cues ship [RESOLVED 2026-07-17, user-approved hand-edit — devague has no park-resolution verb yet (a fix supporting this is already underway upstream, per the user): the unknown was answered with live journal evidence (147 detections in 51 untouched minutes at saturation rate) and converted into confirmed requirement c20 + scope entry s14; the fidelity fix is now in scope, verified by honesty h15]
