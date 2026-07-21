# Build Plan — retire the old AI-first flow

slug: `retire-the-old-ai-first-flow` · status: `exported` · from frame: `retire-the-old-ai-first-flow`

> reachy-mini-cli is a symbolic robot runtime: the robot's presence is the behavior engine driven by rules and configuration, not a hardcoded AI app. Cognition is optional and external — no LLM call happens inside the CLI's presence loop.

## Tasks

### t1 — Capture the pre-change baseline: enumerate the capabilities each flow owns, record the current LIVE box evidence (stuck speech_detected, tick headroom, units, drop-ins)

- instruction: Write docs/deliveries or docs/verification baseline. Re-run the state.json DoA probe (~/.local/state/reachy/behavior/state.json) several times in a quiet room and record it. Capture systemctl --user list-unit-files 'reachy-*', the seven drop-ins under ~/.config/systemd/user/, and current tick metrics. This is evidence, not code.
- covers: c18, h18, c19, h19
- acceptance:
  - a committed baseline doc lists every capability owned by the old flow, by only the runtime, or by both
  - the frozen-DoA probe is reproduced and recorded with timestamps before any code changes

### t2 — Move Event/MarkerEvent out of reachy/speech/markers.py to a surviving module

- instruction: reachy/speech/markers.py defines Event/MarkerEvent; reachy/motion/expression.py:50 imports them. Move the types to a surviving module (expression.py itself, or a small shared events module). Keep shapes identical. Prove it with a test importing reachy.speech.tools + reachy.motion.expression while markers.py is absent.
- covers: c24, h22
- acceptance:
  - importing reachy.speech.tools and reachy.motion.expression succeeds with markers.py absent
  - Event/MarkerEvent keep their current shape; expression.py consumers unchanged

### t3 — Add a bidirectional explain-catalog <-> CLI agreement test

- instruction: tests/test_cli.py:131 test_every_catalog_path_resolves only walks ENTRIES. Add the reverse: walk the argparse tree from _build_parser and assert every registered verb path has an ENTRIES key. Both directions must fail loudly.
- covers: c29, h26
- acceptance:
  - every ENTRIES key resolves to a live argparse path
  - every registered verb has an ENTRIES key; deleting a verb without its entry fails CI

### t4 — Build the retired-unit cleanup migration in ServiceManager

- instruction: reachy/service/{units,manager}.py + cli/_commands/service.py. Add a RETIRED_UNITS constant and a migration that unconditionally disables --now, unlinks the unit file, and removes its .d/ directory. Note h5 forbids the DELETING pr from running enable/disable — this cleanup is its own code path, invoked by service verbs. The orphaned reachy-listen.service on the box is your negative control.
- covers: c25, h23
- acceptance:
  - a box with reachy-live.service enabled ends with it disabled, its file removed, and its .d/ drop-in dir removed
  - service status never reports mode=None while a presence unit is still enabled
  - driven through the injected run/unit_dir seams; no real systemd needed in tests

### t5 — Decide and implement foreground-verb arbitration now that the *_active.flag readers retire

- instruction: Decide first, then implement: does the runtime read pat_active.flag/sleep_active.flag, or do foreground pat/sleep verbs refuse to start while the runtime unit is active? Either is defensible; silence is not. reachy/behavior/ imports none of the signal modules today.
- acceptance:
  - a foreground pat/sleep run and the runtime unit cannot silently fight for the head
  - the chosen mechanism is documented; if flags are dropped, their removal is deliberate not incidental

### t10 — One held media client: mic + camera + pose lifecycles from a single SDK owner, explicitly closed

- instruction: reachy/robot/state_reader.py:29-31 already documents the shape: ONE no_media pose client AND one media client, never shared. Build the media-client holder alongside HeldStateReader with the same construct-on-first-use + explicit idempotent close discipline — an unclosed client HANGS the process at interpreter exit (state_reader.py:20-24).
- acceptance:
  - exactly one media client and one no_media pose client per process; both closed explicitly
  - the process does not hang at interpreter exit
  - no second media client is opened anywhere in the runtime

### t11 — Transcript sense: background STT worker feeding a latched transcript field, engagement gate preserved

- instruction: reachy/motion/listen_transcribe.py is the donor; reachy/speech/stt.py Transcriber does the work. Latch the transcript one-tick like PatSenseDriver (pat_sense.py) does. Keep the engagement gate (engagement.py + name_match.py) as the admission filter.
- depends on: t10
- covers: c11, h3
- acceptance:
  - the tick reads a latched value, never a blocking STT call
  - an unreachable STT leaves the field None and drops no ticks
  - the #54/#56 engagement gate still filters addressed-vs-ambient speech

### t12 — rms sense provider over the shared mic sample

- instruction: Same mic sample t11 already pulls — do not open a second reader. Feed Sense.rms via the SenseProviders slot that already exists (sense.py:231-236).
- depends on: t10
- acceptance:
  - a rule keyed on rms admits a behavior against injected sense
  - covered by the offline lane with the source unreachable

### t13 — face + frame_available providers over the held frame source

- instruction: reachy/vision/{face,face_store}.py exist. Frames come from t10's held media client — issue #73 root-caused the standalone noun's crash to a fresh-client-per-frame path. Guard None/degenerate frames. Missing [vision] extra = one logged warning.
- depends on: t10
- covers: c21, h10
- acceptance:
  - rules keyed on face and frame_available each fire in test
  - a missing [vision] extra is one logged warning, not a crash
  - a None or degenerate frame is skipped rather than raising (the #73 fix shape)

### t14 — Two-layer rules: shipped package resource + box-local overriding overlay

- instruction: reachy/behavior/rules.py:133 default_rules_path + RulesLoader. Ship defaults as a package resource (importlib.resources); box-local state_dir()/behavior/rules.toml becomes an overlay keyed by rule id. Preserve last-good-config semantics across BOTH layers.
- covers: c34, h29
- acceptance:
  - upgrading a box with a tuned rules.toml keeps every local override AND picks up newly shipped rules
  - a local entry can disable a shipped rule
  - a malformed overlay degrades to the shipped layer, not to nothing

### t16 — behavior rules check warns on predicates keyed to unfed sense fields

- instruction: Extend rules check to compare each predicate's field against what the current composition actually feeds. Warn, exit 0 — mirror how think expressions check treats flagged pairs.
- depends on: t14
- covers: c22, h11
- acceptance:
  - a rule keyed on an unfed field warns naming the field and why it cannot fire
  - check still exits 0 (warning not failure); a rule on a fed field never warns

### t17 — Re-home forge onto agent attach (net-new composition, ~60 LOC incl. feed_forge wiring)

- instruction: reachy/cli/_commands/listen.py:658-760 is the donor (_forge_stack_available, _activate_forge, forge_holder, the buffer.feed_forge arming, the active/ boot-reload). agent.py has ZERO forge references today — this is net-new composition. Test the re-homed path end to end; the four isolation tests do not count as evidence.
- depends on: t2
- covers: c26, h24
- acceptance:
  - a forged skill dispatched through agent attach is validated fail-closed, activated, and callable on the NEXT turn
  - the test drives the re-homed path end to end; the four isolation tests are explicitly not sufficient evidence

### t18 — Re-home the expression pose catalog verbs onto a surviving noun

- instruction: think expressions {list,check} are the verbs at risk. Re-home onto a surviving noun (behavior is the natural host). expressions.toml/expressions.py/distinctness.py are not LLM-coupled and stay.
- depends on: t2
- covers: c7, h5
- acceptance:
  - the catalog stays inspectable from the CLI (list + distinctness check) after think retires
  - speech/expressions.toml, expressions.py and distinctness.py keep their current consumers working

### t6 — Speech actuator: a behavior action seam that synthesizes+plays on a BACKGROUND worker

- instruction: DEFAULT is reachy/speech/harmonic.py (in-process, offline, base dep) — TTS is configurable, not default. Dispatch to a background worker; the pattern already exists in reachy/motion/expression.py ExpressionProducer + the _MotionExecutor drain thread. Nothing synthesizes or plays on the tick thread. Depends on t10 for the media client.
- depends on: t10
- covers: c9, h2
- acceptance:
  - no synthesis or playback runs on the engine tick thread
  - tick budget holds: 0 overruns across a sustained run with speech firing repeatedly
  - a wedged or unreachable TTS degrades to silence without stalling a tick
  - the DEFAULT voice is the in-process offline harmonic synth (harmonics-cli, a base dep) — no network round-trip on the default path
  - TTS is a configurable alternative backend, never required for the default install

### t7 — Speech observability: [SENSE] drop line on failed synthesis + explicit TTS route on the runtime unit

- instruction: Emit a [SENSE stage=... event=drop] line naming the reason on any failed clip — reachy/senselog.py's 'a drop always names its reason' discipline. Set the runtime unit's TTS route explicitly in units.py rather than inheriting reachy-live.service.d/tts.conf, which belongs to a unit being deleted.
- depends on: t6
- covers: c27, h25
- acceptance:
  - a failed clip emits a [SENSE] line naming the reason; silence is never indistinguishable from success
  - the runtime unit sets its TTS route explicitly rather than inheriting a drop-in belonging to a deleted unit
  - live verification asserts AUDIBLE output heard on the robot
  - the runtime speaks with NO service reachable: offline lane covers the actuator end to end with every endpoint down
  - a live check confirms audible harmonic output while the TTS route is unset or unreachable

### t8 — Sound-orienting: doa_angle_to_yaw driven into a sustained gaze goal in the runtime

- instruction: reachy/behavior/sense.py:288 doa_angle_to_yaw is the donor; reachy/motion/listen.py:389 shows the old two-tier use. Express as a goal over gaze-hold/body-turn-hold so arbitration.py preempts it normally. Touches _compose_run_seam — serialized behind t6.
- depends on: t6
- covers: c10, h7
- acceptance:
  - orienting participates in arbitration: preempted by a pat reaction and by sleep per the priority model
  - observably equivalent motion to the old two-tier ladder

### t9 — Port the latched-DoA guard so a frozen at-rest angle cannot drive a turn

- instruction: The old flow's latched-DoA guard suppressed the daemon's frozen at-rest angle. Probe evidence (c32): speech_detected reads True at rest on the box RIGHT NOW. Verify by watching a quiet room for minutes, not seconds.
- depends on: t8
- covers: c32, h28
- acceptance:
  - the robot does not turn in a quiet room across a multi-minute live window, given speech_detected reads True at rest today
  - no shipped rule keys on bare speech_detected without a corroborating signal

### t15 — Author the shipped default rules covering pat, voice, hearing and orienting

- instruction: Author the shipped defaults covering pat-acknowledge, a voice reaction, a hearing reaction and orienting. Per c32, do NOT key any rule on bare speech_detected — require a corroborating signal. Verify on a clean env, since the box's pat-sense.conf drop-in may mask shipped-default behavior.
- depends on: t14, t7, t9, t11, t12, t13
- covers: c13, h9
- acceptance:
  - a fresh install + service enable runtime visibly reacts to a pat, a voice and a sound with NO operator-authored config
  - a malformed edit still falls back to base presence with one logged drop and exit 0, never a crash loop

### t19 — SOAK CHECKPOINT: runtime at full capability, reachy-live.service still enableable, rollback runbook validated on the box

- instruction: This is a GATE, not a code task. Write exit criteria BEFORE starting. Run the rollback runbook end to end on the box while reachy-live.service still exists — downgrade, restore unit, re-author the six drop-ins (panel.conf carries a hardcoded bridge IP 192.168.1.173). Nothing downstream merges until this passes.
- depends on: t7, t9, t11, t12, t13, t15, t16, t17, t18
- covers: c35, h30, c12, h8, c16, h16, c17, h17
- acceptance:
  - soak exit criteria are written down BEFORE the soak starts and evaluated against what the robot actually did
  - the rollback runbook is executed end-to-end on the box while reachy-live.service still exists
  - all four runtime capabilities — see, hear words, speak, orient — demonstrated on the robot in one session
  - an operator changes a reaction by editing rules.toml + behavior reload, no code change, no restart
  - each ported capability was live-verified before this checkpoint; no window existed where the robot was less capable than at baseline

### t20 — Delete the think noun, its supervisor, sidecar and catalog entries

- instruction: reachy/cli/_commands/think.py (1083 LOC) + its 12 catalog.py entries (:1355-1366) + reachy/speech/supervisor.py + the think.voice sidecar. t3's bidirectional test catches a stale catalog entry. reachy/speech/cognition.py and markers.py go here too (t2 already moved the shared types out).
- depends on: t19, t3
- acceptance:
  - no think verb remains in the CLI and no stale catalog entry survives (caught by the bidirectional test)
  - orphaned think.pid/think.log/think.voice/think_active.flag are documented as inert leftovers

### t21 — Delete the listen --live composition root and the folded motion/listen_*.py hooks

- instruction: The --live composition root in listen.py plus reachy/motion/listen_{think,vision,sleep,face,scene,transcribe,hooks}.py and sense_sample.py. listen_pat.py is superseded by behavior/pat_sense.py. Bare listen run must still work identically after this — the engine imports are already function-local at listen.py:550/620.
- depends on: t19, t17
- covers: c8, h6
- acceptance:
  - bare listen run behaves identically until t22 retires it; existing non-live listen tests pass unmodified

### t22 — Retire the listen NOUN (cli/_commands/listen.py), keeping reachy/motion/listen.py ListenProducer

- instruction: Delete reachy/cli/_commands/listen.py (the NOUN). KEEP reachy/motion/listen.py (ListenProducer) — tests/test_offline_lane.py:46 imports it and :230 is a named success path. If t8's port makes the producer redundant, move that offline coverage to the runtime equivalent in THIS pr, never drop it.
- depends on: t21, t9
- covers: c28, h31
- acceptance:
  - the offline lane passes unchanged, including its orient_to_sound path that imports ListenProducer directly
  - if the producer becomes redundant, its offline coverage moves to the runtime equivalent in the SAME PR, never dropped

### t23 — Remove LIVE_UNIT and the live presence mode; service offers exactly demo|runtime

- instruction: units.py: remove LIVE_UNIT and live_exec_start; manager.py: remove live from _PRESENCE/_UNIT_TO_MODE/_PRESENCE_UNITS; service.py:263 choices become (demo, runtime). t4's retired-unit migration must already be in place or deployed boxes crash-loop.
- depends on: t22, t4
- covers: c6, h4, c15, h15
- acceptance:
  - single-presence exclusion still leaves at most one presence enabled across ANY sequence of enables
  - no PR in the arc runs service enable/disable as part of its delivery; the box stays on reachy-runtime.service throughout

### t24 — Machine-check the zero-LLM property: import-boundary tests + offline lane coverage

- instruction: Add the AST import-boundary test over reachy/behavior/ (model it on tests/test_speech_tools.py:637). Extend the offline lane to cover voice, hearing, orienting and pat with every endpoint down. Then run the FULL suite and confirm sleep/vision/say/daemon/device/app/move/service tests are untouched — a surviving noun needing test edits means the boundary was drawn wrong (h21).
- depends on: t20, t21, t22
- covers: c1, h1, c2, h12, c3, h13, c20, h20, c23, h21
- acceptance:
  - an AST import-boundary test fails if any behavior/ module imports reachy.speech.* or an LLM client
  - no module reachable from 'behavior engine run' imports reachy.speech.llm
  - no surviving reachy/motion/listen_*.py module imports a cognition engine
  - the offline lane covers voice, hearing, orienting and pat with every endpoint unreachable
  - every out-of-scope noun — sleep, vision, say, daemon, device, app, move, service — keeps its tests passing UNMODIFIED
  - the behavior engine itself is not restructured: engine, rule engine, intents, arbitration, goto lane and pat sense ship unchanged

### t25 — Verify the export/runtime feed contract survives the deletion unchanged

- instruction: docs/export-schema.md:9-10 attributes the feed to 'think run --export -' and 'listen run --live --export -'. Reattribute to agent attach, which already emits the same blocks (agent.py:451). The RUNTIME_BLOCKS contract itself does not change — if it does, escalate.
- depends on: t21
- covers: c4, h14
- acceptance:
  - docs/export-schema.md and RUNTIME_BLOCKS still describe shipped behavior after removal
  - any change to an exported block's shape is escalated as a contract break, not shipped silently

### t26 — Keep docs accurate PER PR, not deferred to a cleanup pass

- instruction: README.md (first-run example at :11 is 'listen run'), CLAUDE.md (whole listen + think noun sections), docs/operating-reachy.md (~500-600 of 1833 lines). Verify the two README-linked anchors still resolve. Do NOT rewrite docs/specs/, docs/plans/ or CHANGELOG.md — those are dated records.
- depends on: t23
- covers: c30, h27
- acceptance:
  - README.md, CLAUDE.md and docs/operating-reachy.md are accurate for the state the code is in at EACH merge
  - the two README-linked anchors in operating-reachy.md still resolve
  - historical docs/specs/, docs/plans/ and CHANGELOG.md are untouched by design

## Risks

- [unknown_blocking] Audio is the first genuinely BLOCKING side effect in a loop whose correctness argument is 20ms tick determinism (0 overruns across 8500+ live ticks). Drawn wrong, the symptom is degraded motion, not a crash. (task t6)
- [unknown_nonblocking] Sonar's gate runs on NEW CODE. Deletion PRs that also edit survivors (agent.py forge wiring, expression.py imports, manager.py cleanup) make those lines new code needing ~80% coverage, on modules whose integration coverage came from the deleted suites.
- [unknown_nonblocking] Every push to main publishes to PyPI and every PR must bump the version, so all six PRs are public releases with no staging — PR3 ships whether or not PR6 lands. The deployed box is a uv tool install from a LOCAL checkout, so published and deployed artifacts follow different paths. (task t19)
- [unknown_nonblocking] reachy-runtime.service.d/pat-sense.conf on the deployed box may be masking whether shipped pat defaults actually work unconfigured — h9 depends on this and the only box that could prove it still carries the override. (task t15)
- [unknown_nonblocking] File-disjointness hazard: t6/t8/t10/t11/t12/t13 all edit cli/_commands/behavior.py::_compose_run_seam, the single composition root. The dependency graph sequences content, not file writes. t10->t6->t8 is now serialized explicitly; t11/t12/t13 remain same-wave seam-writers and must either be merged sequentially by the main agent or folded into one task at fan-out time.
