# symbolic-runtime-70

> reachy-mini-cli is a symbolic runtime for Reachy: a deterministic on-box heartbeat runtime owns state, reactions, and scheduled behaviors with zero external AI services required; humans, scripts, and AI agents all operate it by declaring symbolic goals and rules — AI chooses intentions, the runtime sustains them, and lobes/model-gear become optional plug-ins (issue #70)

## Audience

- operators, scripts, symbolic systems, and AI agents equally — anyone driving Reachy through the CLI/runtime; AI agents are one client class among several (issue #70: agents consume capabilities, not define them)

## Before → After

- After: Reachy boots into a self-sufficient symbolic runtime: the heartbeat loop sustains presence, reflexes, and declarative rules with zero external AI services; agents attach at runtime through data-out/act-in seams to add cognition, and detaching them changes nothing about the loop

## Why it matters

- today deterministic embodiment burns LLM tokens, adds latency, blocks offline operation, and couples testing to live services (issue #70's drawback list); an AI-agnostic runtime makes behavior reproducible, testable, and cheap, with AI spent only on intentions

## Requirements

- offline-first invariant: every reflex/presence behavior (listen tiers, pat detect+react, sleep decay/Tier-1 wake, idle alive, demo-mode, behavior engine, harmonic voice, pixel vision, local face recognition) runs correctly with zero reachable AI services — this already holds de facto and the runtime makes it an asserted, tested property
  - honesty: CI gains an explicit offline lane: reflex/presence tests run with every service endpoint unreachable and stay green
- a declarative rule/config layer lands on top of the behavior engine: when-perception -> run-behavior reactions, inhibitions, and modes as data-only config (TOML/YAML), validated like stash records (from_dict single gate, refuses anything code-shaped, no exec/eval), compiled onto existing arbitration priorities
  - honesty: a rules file (react/when/run + inhibit + mode) demonstrably changes robot behavior in a bounded --ticks deterministic run with injected clock and zero LLM calls
- a unified perception surface feeds the runtime: generalize behavior/sense.py Sense (today DoA-only via daemon HTTP) to carry what the live loop's SenseSample + hooks already produce (speech flag, RMS, pat events, face, frame/scene availability), with senselog [SENSE] lines as the shared observability layer
  - honesty: one perception snapshot type serves both the behavior engine and the folded hooks without opening a second media session or camera grabber — the single-SDK-owner model holds
- AI moves from executor to orchestrator: the agent ToolRegistry gains runtime intents (declare goal, run behavior, set mode, set inhibition) so an agent's choice persists in the runtime instead of being re-driven per turn; the existing speak/harmonics/apply_pose tools stay as immediate acts
  - honesty: an intent an agent declares in one turn is still being sustained by the runtime many ticks later with no further agent calls, observable via a status/export surface
- the boot-default presence becomes the AI-agnostic runtime loop: the deployed service unit runs the runtime (collect data, run rules, sustain goals), and agent cognition attaches as an external client through the loop's seams (export feed out, intents/tools in) instead of being folded in via --cognition agent (operationalizes c11 against reachy/service/units.py)
  - honesty: the shipped live unit's ExecStart no longer wires an LLM into the loop; a box with no REACHY_OPENAI_* endpoint boots to full presence, and attaching the agent later requires no loop restart
- the service noun's single-presence-owner invariant extends to the runtime mode: the new AI-agnostic runtime unit joins the demo|live mutual-exclusion set in reachy/service/manager.py, so any sequence of enables still leaves at most one presence unit enabled
  - honesty: with the runtime mode enabled, systemctl --user is-enabled shows exactly one presence unit; enabling any sibling disables it, proven by the ServiceManager seam tests
- the state pillar launches with joints + pose only: SDK 1.9 exposes get_current_joint_positions / head_pose but contains ZERO battery references (verified by grep over the installed reachy_mini wheel) — no runtime surface, rule example, or inhibition may reference battery until a future SDK exposes it; c14's premise is corrected accordingly
  - honesty: no code path, rule schema field, doc example, or test in the effort references battery; runtime status reports joints/pose read from the SDK on a live box
- a rejected rules config never crash-loops the boot presence: under the units' Restart=on-failure, a rules file that fails validation at boot degrades to the built-in base presence (feel-alive) with a loud [SENSE] rejection line naming every reason — while a CLI-invoked validation stays a clean exit-1/2 CliError; boot resilience and CLI strictness are different failure surfaces
  - honesty: a deliberately broken rules file + the real unit's Restart=on-failure semantics yields a running base presence and a logged rejection, not a restart loop — covered by a test driving the same entry path the unit uses
- the rule engine ships containment primitives against runaway/oscillating rules: per-rule cooldown and hysteresis as data-only schema fields, so two rules cannot ping-pong (react loop) and one rule cannot re-fire every tick — precedent: the pat false-fire loop fixed in #66 (reaction -> transit -> re-trigger), the same class of loop a naive when/run rule reintroduces
  - honesty: a two-rule ping-pong fixture and an every-tick-firing rule fixture both settle (cooldown/hysteresis honored) in a bounded --ticks deterministic run
- rule evaluation is observable, never silent: every rule fire, inhibition, suppression, and cooldown-skip emits a [SENSE stage=rule ...] line with the rule id and reason, extending the existing senselog contract (a drop always names its reason)
  - honesty: grepping a run's stderr for [SENSE stage=rule] reconstructs every fire/suppress decision with its reason; a silent rule action is a test failure

## Honesty conditions

- unplugging every external AI service leaves a fully alive robot: boot, idle presence, sound/pat/sleep reflexes, and declarative rules all pass with no reachable endpoint
- the [project.dependencies] list in pyproject.toml is byte-identical before and after the whole effort (numpy + harmonics-cli only), and the runtime package imports nothing outside the stdlib
- teken cli doctor . --strict stays green; every new verb ships with --json, an explain catalog ENTRIES key, and a bounded deterministic test (injected clock / --ticks)
- each client class has a demonstrated entry path: a human via CLI verbs, a script via --json + exit codes, an agent via tools/seams — each shown in the operating guide
- on a box with all AI services stopped, enabling the runtime presence boots to sustained presence, and attaching cognition later requires no unit edit and no loop restart
- a sustained presence run with rules firing consumes zero LLM tokens — only agent-initiated turns spend tokens; verifiable from the runtime's own logs/export feed
- the offline test lane exercises exactly the success list (boot, breathe, orient-to-sound, pat, sleep/wake, rules) and fails if any of those paths requires a reachable service

## Success signals

- with lobes/model-gear stopped, the robot boots, breathes, orients to sound, reacts to pats, sleeps/wakes, and runs declarative rules — and attaching an agent adds cognition without restarting the loop; an offline test lane proves it in CI

## Scope / boundaries

- base runtime dependencies stay numpy + harmonics-cli only; the symbolic runtime layer is pure stdlib like reachy/behavior/ — no new base dep, no rules-engine or scheduler package (pyproject.toml's own comment: do NOT promote any engine package to base dependencies)
- new runtime verbs follow the established agent-first CLI contract: register(sub) wiring, --json on every verb, CliError error contract, overview verb on noun groups, explain catalog ENTRIES key, teken rubric gate green, injected clock/--ticks determinism seams, version bump per PR

## Non-goals

- no external AI leg is deleted: lobes/model-gear legs remain optional plug-ins with graceful degradation, following the patterns already shipped (CognitionEngine audio_optional TTS latch-off, REACHY_ENGAGE_HEURISTIC classifier fallback, FaceHook/SceneHook logged-skip on missing extra, forge failure disabling only the tool)

## Assumptions

- the symbolic runtime grows out of reachy/behavior/ (engine.py 50 Hz compose loop, arbitration.py per-channel contention, library.py parametric behaviors, sense.py DoA seam, control.py command spool, supervisor.py) rather than a new parallel runtime package — the heartbeat/behavior-runner/arbitration pillars of issue #70 already exist there, pure stdlib
- reducing 3rd-party dependencies means decoupling SERVICE reliance (defaults, boot posture, degradation), not pip deps — lobes appears nowhere in pyproject.toml; all six AI legs are env-pointed stdlib-urllib HTTP: llm.py REACHY_OPENAI_URL_BASE (:8001 cortex/muse), tts.py REACHY_TTS_URL (:9000 Chatterbox) + gateway /v1/audio/speech, stt.py + sleep/wakeword.py REACHY_STT_URL (:9002 Parakeet), stash/embeddings.py /v1/embeddings, vision/scene.py REACHY_VISION_MODEL_ID VLM, forge/client.py FORGE_BASE_URL (:8001 qwen3)
- the 20 ms tick budget holds: one 50 Hz tick absorbs rules evaluation + perception fan-out + spool drain + pose compose on the robot box — plausible because rules are data lookups and the engine already composes at 50 Hz, but unmeasured with rules and full perception aboard
- the runtime's act-in seam extends behavior/control.py's existing file-spool IPC (atomic-rename command files, single reader per tick, many independent writers, no locking, results + state.json read-back) rather than adding a socket, port, or thread — agents, scripts, and CLI invocations all inject intents through the same spool

## Scope exploration

- `s1` — `reachy/behavior/ (engine, arbitration, library, sense, control, supervisor)`: a pure-stdlib 50 Hz heartbeat engine already exists: holds active behaviors, arbitrates one owner per channel by contention class, composes a complete pose every tick, streams via TargetSink; command-spool IPC lets behaviors be added/stopped while running; feel-alive is seeded as a passive base layer. Issue #70's runtime/heartbeat/behavior-runner/arbitration pillars are an extension job, not greenfield
  - seeds: `c2`
- `s2` — `external service touchpoints (speech/llm.py, speech/tts.py, speech/stt.py, sleep/wakeword.py, stash/embeddings.py, vision/scene.py, forge/client.py)`: exactly six external AI service legs, all optional env-pointed HTTP with stdlib urllib, none a Python package dependency; the lobes/model-gear coupling lives in defaults (localhost ports) and in what the deployed live loop wires in, not in install-time deps
  - seeds: `c3`
- `s3` — `pyproject.toml`: dependency hygiene the issue wants already holds at the package layer: base deps are numpy + harmonics-cli (pure wheels, offline); reachy-mini, opencv, openwakeword are lazy extras with clean exit-2 degradation. The reduction target is service coupling, not the dependency manifest
  - seeds: `c4`
- `s4` — `offline deterministic subsystems (motion/listen.py tiers, motion/listen_pat.py, sleep/, alive.py + motion/idle.py, behavior/, speech/harmonic.py, cli vision noun, vision/face.py YuNet+SFace local)`: the deterministic embodiment layer already runs with no external services: listen's two tiers, PatHook, sleep Tier-1 wake, idle presence, the 50 Hz behavior engine, the offline harmonic voice, pixel motion/light vision, and local opencv face recognition. Only cognition, transcription words, scene description, stash search, and forge need services
  - seeds: `c5`
- `s5` — `existing symbolic config surfaces (speech/expressions.toml emoji-keyed poses, stash/record.py data-only StashRecord + no-exec gate, behavior/library.py parametric LibraryEntry, demo_config.py)`: declarative surfaces already exist and set the pattern (data-only, validated, no code), but issue #70's react/when/run rule engine, inhibit config, and mode switching have no counterpart anywhere — the rule engine is the genuinely new build
  - seeds: `c6`
- `s6` — `perception surfaces (behavior/sense.py Sense DoA-only, motion/sense_sample.py SenseSample, motion/listen_hooks.py HookChain, speech/events.py EventBuffer, senselog.py)`: perception is fragmented across three families: the behavior engine's Sense (DoA only), the live listen loop's per-tick SenseSample fanned out through HookChain, and cognition's EventBuffer. Issue #70's event-bus pillar means unifying these; the [SENSE stage=...] log layer already spans them
  - seeds: `c7`
- `s7` — `AI layer (speech/agent_turn.py AgentTurnEngine, speech/tools.py ToolRegistry, forge/ hot-registration)`: agents already consume capabilities through tools (speak/harmonics/apply_pose + forged skills, registry read fresh every round), which is half of the issue's principle; what is missing is the runtime as a durable intention-holder — today nothing persists an agent's intent between turns except the motion queue's in-flight moves
  - seeds: `c8`
- `s8` — `degradation patterns (speech/cognition.py audio_optional, speech/engagement.py DEGRADE fallback + REACHY_ENGAGE_HEURISTIC, motion/listen_face.py + listen_scene.py logged-skip, forge tool-only disable)`: the codebase already has a consistent degrade-not-crash idiom for every external leg; reduce-dependencies work should extend this idiom (make absence the tested default) rather than remove the legs
  - seeds: `c9`
- `s9` — `CLI contract (cli/__init__.py _build_parser, cli/_errors.py, cli/_output.py, explain/catalog.py, teken rubric in CI, 2150 tests across 114 files)`: any new noun (e.g. runtime/rules) or extension of behavior must satisfy the rubric-gated contract and the determinism bar the 2150-test suite sets (injected clocks, bounded --ticks, no-robot demo verbs)
  - seeds: `c10`
- `s10` — `reachy/service/units.py live unit ExecStart`: the deployed boot presence is python -m reachy listen run --live --transcribe --cognition agent --voice-engine harmonic — the boot DEFAULT wires the LLM agent into the loop, so without lobes the robot's boot identity is a degraded agent, not a self-sufficient symbolic runtime; changing that default is a product decision
- `s11` — `per-leg reduce-vs-remove decision (stash/embeddings.py hard-requires the gateway for search; forge/client.py; speech/engagement.py classifier-by-default under --transcribe)`: three legs are load-bearing by default today: stash search cannot run at all without /v1/embeddings, the engagement gate calls the classifier per utterance unless REACHY_ENGAGE_HEURISTIC is set, and forge is advertised whenever --cognition agent runs; each needs an explicit keep/reduce/remove decision
- `s12` — `two motion families under the single-SDK-owner model (motion/queue.py + motion/server.py goto family vs behavior/engine.py TargetSink streaming; CLAUDE.md single-SDK-owner section)`: the behavior engine runs as its own supervised process and streams poses at 50 Hz, while the live listen loop drives the serial goto MotionQueue — they cannot coexist as two processes against the one SDK client, and no design exists for arbitrating goto one-shots against streamed contributions in one loop
- `s13` — `state pillar (issue #70 state: battery/posture/joints/attention vs the codebase)`: no battery or joint-state surface exists anywhere in the package today; pose read-back (head_pose) and the active-flag files are the only state primitives; daemon/SDK battery+joint exposure needs a live-box probe before the state pillar is specced
- `s14` — `challenge pass / adjacent-systems lens: reachy/service/manager.py single-presence invariant + docs/export-schema.md + reTerminal bridge`: two adjacent consumers found: the ServiceManager mutual-exclusion set must admit the new runtime mode (seeded c20), and the reTerminal panel consumes the documented thinking/message/emotion JSONL feed — when cognition detaches, WHO publishes that feed is undecided (routed as q3)
  - seeds: `c20`
- `s15` — `challenge pass / unstated-assumptions lens: installed reachy_mini 1.9 wheel (io/abstract.py get_current_joints, reachy_mini.py get_current_joint_positions)`: cheap probe: grep for battery across the installed SDK returns nothing; joints and pose read-back exist. Counter-evidence to c14's premise that the SDK exposes battery — state pillar must launch battery-free
  - seeds: `c21`
- `s16` — `challenge pass / unstated-assumptions lens: behavior/engine.py 50 Hz loop vs the added per-tick work`: nobody has argued the other side of the tick budget; captured as explicit assumption c22 rather than letting the frame lean on it silently
  - seeds: `c22`
- `s17` — `challenge pass / failure-modes lens: service units Restart=on-failure x rules validation; PR #66 pat false-fire loop as oscillation precedent`: two concrete failure modes: bad config + on-failure restart = crash loop (seeded c23); naive reactive rules re-create the #66 oscillation class (seeded c24)
  - seeds: `c23`, `c24`
- `s18` — `challenge pass / observability lens: reachy/senselog.py contract vs the new rule engine`: the senselog stage/drop convention extends cleanly to rule evaluation; without it a mis-firing rules file is undebuggable on a headless box
  - seeds: `c25`
- `s19` — `challenge pass / concurrency lens: behavior/control.py spool (read), robot/transport.py TargetSink (read)`: the spool's single-reader/atomic-rename design absorbs concurrent writers safely (seeded c26); the REAL concurrency risk is goto-vs-stream preemption mechanics inside the unified loop (e.g. a stop mid-goto), which v1's resolution defers to design — parked as v4 follow_up for plan-side risk
  - seeds: `c26`
- `s20` — `challenge pass / security lens: state-dir spool + rules config + forge AST gate`: clean pass at current boundary: act-in spool and rules config are local-filesystem surfaces under state_dir, same local-user trust boundary as today's flags/spool; rules are data-only (c6 no-exec gate); forge's AST gate is unchanged by this effort. Residual: any future NETWORK act-in seam would change the trust boundary and needs its own pass
- `s21` — `challenge pass / reversibility + operations lens: service enable modes, deployed-box upgrade`: the boot flip is reversible: c9 keeps every leg, so the old agent-folded mode (listen run --live --cognition agent) stays invocable and service enable can restore it; the deployed box's unit-file migration (install + daemon-reload + re-enable) changes HOW we ship, not WHAT — deferred plan-side per the c18 spec/plan coaching

## Decisions

- the live runtime loop is AI-agnostic by default: lobes is NOT a dependency of the loop; AI may be injected at seams / as code snippets, but the loop itself only collects data (perception, state) — an agent processes that data and acts through the seams, and the loop stays agnostic to whether or which AI is attached
- keep the stash embeddings leg (/v1/embeddings) for now — quick semantic code/seam fetch stays; it is not removed in this effort
- motion-family unification lands in the single SDK-owner process: the runtime owns the one SDK client, and both motion families (serial MotionQueue gotos and 50 Hz streamed behaviors) fold into that one process
- the state pillar relies on SDK-exposed state (battery/joints) for now — no daemon-route or homegrown state source
- export feed ownership after cognition detaches: the external agent publishes its OWN thinking/message/emotion feed directly to consumers (e.g. the reTerminal panel); the runtime's export feed carries only runtime events (perception, rules, intents, motion) — two feeds, each with its own contract
- rules config lives under state_dir (like behavior state) and edits apply via an explicit CLI reload verb at a deterministic point — hot-reload deferred; a rejected reload keeps the last good config

## Open / follow-up

- goto-vs-stream arbitration mechanics inside the unified loop (preemption of an in-flight goto by a streamed or stopping behavior, busy-horizon vs per-tick compose) — the design task v1's resolution deferred; lands as a plan risk when /spec-to-plan seeds

## Resolved vagueness

- [unknown_blocking] how the two motion families unify: the serial MotionQueue goto family (listen/think/pat/sleep/stash-apply, one move at a time, busy_until horizon) versus the behavior engine's 50 Hz TargetSink pose streaming — one process must own both under the single-SDK-owner model, and arbitration between one-shot gotos and streamed contributions has no existing design — resolved: resolved by user: SDK it is — unify inside the single SDK-owner process (c13); remaining arbitration mechanics between gotos and streamed contributions become a design task, no longer a blocking unknown
- [unknown_nonblocking] state pillar coverage: grep finds zero battery references anywhere in reachy/; whether the reachy-mini daemon/SDK exposes battery/joint state is unverified — the issue's state examples (battery, joints) may not be obtainable, so the state surface may launch with posture/attention only — resolved: resolved by user: the SDK exposes battery/joint state — rely on it for now (c14)
