# Build Plan — embodiment arc followups

slug: `embodiment-arc-followups` · status: `exported` · from frame: `embodiment-arc-followups`

> reachy-mini-cli closes out the embodiment-layer arc's findings: the layer treats runtime events as context it reads on its next turn rather than a trigger each (only alerts and heard speech trigger), the repo's 296 fake lint suppressions are gone, and every known runtime defect from the arc is fixed, named in the journal, or explicitly deferred with its cause on record

## Tasks

### t1 — \#133: enforce `MAX_SAY_CHARS` in speech/tools.py speak/harmonics text validation, fail-closed

- instruction: Edit reachy/speech/tools.py ONLY (plus its test). Import `MAX_SAY_CHARS` from reachy.behavior.rules at module scope — the precedent is reachy/embody/tools.py:108, and the scope pass verified rules.py's transitive closure stays inside behavior + cli.`_errors` + daemon, so every boundary assertion in tests/`test_speech_tools.py` holds. Enforce the cap in `_require_text` (or a sibling helper) REFUSING fail-closed with rules.py's message shape — never truncate. Do NOT touch embody/tools.py or behavior/rules.py. Add a sys.modules-scan test proving the new import drags in no motion/vision/llm/events/forge.
- covers: c9, h4
- acceptance:
  - dispatch('speak', text of `MAX_SAY_CHARS`+1) refuses naming the cap, never truncates or synthesizes; `test_speech_tools.py` boundary assertions stay green with the rules import (sys.modules scan shows no motion/vision/llm/events/forge)

### t2 — \#134: name the 404 handshake 'realtime-lane-unavailable' in the transcriber hearing leg

- instruction: Edit reachy/speech/realtime.py ONLY (plus tests/`fake_realtime_server.py` scenario + its test). PORT the existing name — reachy/speech/`realtime_duplex.py`:312-314 already ships `REASON_LANE_UNAVAILABLE`='realtime-lane-unavailable' with the full rationale in its docstring at lines 148-155. `_ws_connect` (realtime.py:826-832) already surfaces the HTTP status precisely so the caller can name it; the transcriber's caller currently folds 404 into `REASON_HANDSHAKE_REFUSED`. Detail text must point at GET /v1/capabilities stt.feasible. Do NOT change backoff, latch, or reconnect cadence, and do NOT edit `realtime_duplex.py`.
- covers: c10, h5
- acceptance:
  - a `fake_realtime_server` 404 scenario yields ONE latched realtime-lane-unavailable drop whose detail names /v1/capabilities stt.feasible; non-404 refusals still report handshake-refused; backoff and reconnect cadence unchanged

### t3 — \#136: exact-token/basename PID guards for alive.py and daemon.py via procsup; close #136

- instruction: Edit reachy/procsup.py, reachy/alive.py and reachy/daemon.py (plus tests). PROBED FACT: the live daemon's /proc cmdline is \[/home/spark/.local/share/uv/tools/reachy-mini-cli/bin/python3, /home/spark/.local/share/uv/tools/reachy-mini-cli/bin/reachy-mini-daemon\] — argv\[0\] is the INTERPRETER, so an argv\[0\]-only basename rule MISSES the real daemon. Add a basename-of-any-argv-token predicate to procsup beside `has_argv_tokens`. alive.py:207-223 (demo-mode stop guard) migrates to `has_argv_tokens`; daemon.py:129-141 (`is_alive`/`DAEMON_BINARY` guard) migrates to the basename predicate — today its '`reachy_mini`' in cmdline substring is satisfied by the sibling checkout /home/spark/git/`reachy_mini`. Regression test per site: spawn a REAL innocent process from this project's venv whose argv carries sibling-checkout-style paths and assert stop leaves it alone; plus a fake cmdline shaped like the measured live daemon that MUST still match. Leave the four already-fixed supervisors alone.
- covers: c11, h6, c34, h31
- acceptance:
  - procsup gains a basename-aware predicate: a fake cmdline shaped like the measured live daemon (\[python3, .../bin/reachy-mini-daemon\]) matches; a real innocent process spawned with sibling-checkout-style argv survives 'demo-mode stop' AND 'daemon stop' in regression tests; no substring cmdline check remains outside procsup

### t4 — \#132: executable model defaults name gateway roles — forge qwen3->cortex, scene served-id->senses; close #132

- instruction: Edit reachy/forge/client.py:49 and reachy/vision/scene.py:58-59 ONLY (plus tests). PROBED LIVE against the gateway: `DEFAULT_FORGE_MODEL`='qwen3' returns `model_not_found` TODAY (forge dispatch is broken now); roles 'cortex' and 'senses' both resolve 200. Set forge's default to the cortex ROLE and scene's to the senses ROLE. tests/`test_speech_llm_tools_integration.py` already documents this drift history and pins a role name — follow its gateway-gated skip pattern for any live coverage. Sweep reachy/ for any other executable default naming a served id; docs listing verified ids are documentation of a moment and stay. Do NOT touch environment.d (that is t14).
- covers: c12, h7
- acceptance:
  - no executable default in reachy/ names a served id or dead alias (grep-proven); gateway-gated coverage or an archived live probe shows forge dispatch and the scene leg resolving via role names

### t5 — \#135: capture and fix the wedged-consumer tee flake at cause

- instruction: Edit tests/`test_behavior_audio_tee.py` (and reachy/behavior/`audio_tee.py` only if the cause is there). REPRODUCE FIRST with the assertion captured — run the file in a loop under CPU contention with --tb=long until it fails; three sibling flakes in this family were all real test bugs. Candidate cause (reasoned, NOT observed): `audio_tee.py`:725 bumps self.dropped BEFORE emitting the consumer-slow log line, while the test `_waits` on tee.dropped>0 then immediately asserts caplog — a window where the counter is visible and the line is not. If the captured assertion says otherwise, FOLLOW THE EVIDENCE. Do NOT raise `WAIT_BUDGET_S` or mark the test flaky. Prove it with 30 consecutive pytest -n auto runs of the file.
- covers: c13, h8
- acceptance:
  - the racy read is replaced by a wait on the observable condition (candidate: the caplog assertion racing the worker's post-counter log emission, `audio_tee.py`:725); `WAIT_BUDGET_S` untouched; 30 consecutive pytest -n auto runs of the file green; the commit documents the captured-or-reasoned cause

### t6 — \#143a: typed alert-vs-context cue classification in embody/cues.py, routed identically by both intakes

- instruction: Edit reachy/embody/cues.py ONLY (plus tests). Today `cues_for_runtime_event` returns list\[str\] and the class is erased at `submit_cues`(text). Make the classification TRAVEL: the `_MAPPERS` dispatch table (cues.py:184-187) already knows each event's type, so classify AT THE MAPPER — rule fires are ALERT, sense/intent/motion and rule SUPPRESSIONS are CONTEXT. Keep reachy/`runtime_cues.py`'s shared list\[str\] shape untouched so agent attach's caller is unchanged (this is what keeps boundary claim c20 true). Both intake routes must classify identically — the bus bridge and the feed-tail path share `cues_for_line`. Do NOT edit engine.py; t7 consumes what you produce.
- covers: c7, h2
- acceptance:
  - cue classification is decided from the runtime event TYPE at the mapper (rule fires -> alert; sense/intent/motion/suppressions -> context), never re-sniffed from cue text; both intake routes (bus bridge and feed-tail) classify identically; `runtime_cues.py`'s shared list\[str\] shape is untouched and agent attach's caller unchanged

### t7 — \#143b: EmbodyTurnEngine three-class policy — context park, alert containment, observability, replay acceptance

- instruction: Edit reachy/embody/engine.py and reachy/cli/`_commands`/agent.py (intake routing) + tests + docs/export-schema.md if the block shape moves. THE CORE CHANGE: `submit_cue`/`submit_utterance` today funnel into ONE `_pending` deque (engine.py:498-508) and `run_turn` fires on anything pending — that is the 187-cues/23-turns/19-drops defect. Split into THREE classes: an utterance TRIGGERS; an ALERT cue (rule fire, from t6's classification) TRIGGERS; every CONTEXT cue accumulates in a bounded, coalescing park that the next turn DRAINS and that NEVER triggers. reachy/speech/`agent_turn.py` already models the context half correctly (snapshot-only buffer) — cite it, do not import it. ALERT CONTAINMENT (challenge finding): rules.py permits `cooldown_s`=0 and several rules can fire in one tick, so alerts arriving while a turn is pending/running must COALESCE into that turn and a minimum interval must bound alert-triggered turns — otherwise the same flood returns through the front door. OBSERVABILITY: the turn's senselog line and the export thinking block carry what was drained (context=N coalesced-from=M) — a silent coalescer is indistinguishable from a dropper. The buffer's internal discipline (structure, dedup keys, lock granularity) is YOUR call (plan risk r1) but the submit path must stay O(1) and two threads submit (intake + duplex utterance) while `run_turn` drains under `_turn_lock`. Do NOT widen the duplex send surface — exactly three frame kinds leave that socket and a test pins it.
- depends on: t6
- covers: c6, h1, c32, h29, c33, h30, c31, h28
- acceptance:
  - replaying the measured 40 s window (187 cues, 0 rule fires, 0 utterances) with a fake model produces 0 turns; a following rule-fire cue produces exactly 1 turn carrying the coalesced context; a burst of 10 rule fires inside one turn window produces at most 2 turns with every fire visible in drained context; senselog line and export thinking block carry the drained counts; the duplex send-surface test still pins exactly three frame kinds; docs/export-schema.md updated in the same change if the block shape moves

### t8 — \#137: attribute the sustained 5% tick overrun live (FaceSenseDriver composed vs not); archive evidence; fix or re-scope

- instruction: LIVE BOX TASK — serializes with t14, never run beside another live task. Measure, do not guess. Attribute the sustained ~5% tick overrun (`mean_ms` 21.03-21.06 vs 20.00 budget, streak running since at least 2026-07-30, PREDATING this arc). Issue #137 already measured the correlation: 0 overrun ticks in 100 s with the camera dead under both an active AND a wedged tee consumer; a sustained streak with the camera alive. The candidate is the per-tick media.frame() read in reachy/behavior/`face_sense.py`, which that module's own docstring flags as the expensive leg. Run FaceSenseDriver composed vs not on the deployed box, same window, and archive the result under docs/evidence/. If the cause is client-side, ship the fix in this task and show the streak clearing. If it attributes daemon-side, ship NO code — update and re-scope issue #137 with the measurement and say so plainly. Both outcomes satisfy the acceptance criterion; rounding up does not.
- covers: c14, h9
- acceptance:
  - one archived measurement under docs/evidence/ attributes the overrun on the deployed box; a client-side cause ships its fix in this arc and the streak clears; a daemon-side cause updates and re-scopes issue #137 with the measurement, not closing it

### t9 — \#138: named latched camera-stream-ended staleness drop (detect only, no recovery)

- instruction: Edit reachy/behavior/`face_sense.py` (or the media-client seam) + tests. DETECT ONLY — recovery is explicitly out of scope (boundary c21) because nobody has probed whether a GStreamer EOS is recoverable in-process at all. Observed live 2026-08-02: the pipeline EOS'd and no camera frame arrived for 1h45m while the runtime reported itself healthy. HeldMediaClient.connected (`media_client.py`:319) is a CONNECTION-level flag and stays true across a dead pipeline, so `_HolderKeeper`'s re-warm never fires — key the condition on FRAME STALENESS while the camera is believed present, not on the connection flag. Emit ONE latched, named camera-stream-ended drop via senselog. A camera that legitimately never existed must produce NO such drop. Grep must prove no code path you add constructs, rebuilds or restarts a pipeline.
- depends on: t8
- covers: c15, h10, c21, h23
- acceptance:
  - with a fake media client reporting connected while frames stop arriving, ONE latched camera-stream-ended drop appears within the staleness window; a camera that never existed produces no such drop; grep proves no code path constructs, rebuilds or restarts a pipeline in response to staleness

### t10 — \#141/S107: frozen per-engine Limits dataclass for EmbodyTurnEngine and RealtimeDuplexSession bounds

- instruction: Edit reachy/embody/engine.py and reachy/speech/`realtime_duplex.py` + the composition root + tests. EmbodyTurnEngine.`__init__` has 21 params, RealtimeDuplexSession.`__init__` has 23 — both flagged python:S107. The count is a symptom of the seam-injection design, NOT carelessness, so do not collapse the seams: a model function, an export hook, a clock, a sleep, a sink, a cancel predicate each stay EXPLICIT keyword-only parameters. Only the BOUNDS move — `max_tool_rounds`, `history_maxlen`, `max_pending`, `spoken_maxlen`, `utterance_maxsize`, `response_maxsize`, `playback_maxsize`, `max_response_bytes` and the several timeouts — into a frozen per-engine Limits dataclass with every documented default preserved and one documented home. This is a public-ish signature change the composition root and a large test suite bind to; rebind them. Sonar S107 must report 0 on both. You inherit t7's engine.py changes — rebase, do not revert them.
- depends on: t7
- covers: c26, h12
- acceptance:
  - both constructors' bounds move into a frozen per-engine Limits dataclass keeping every documented default; injectable seams stay explicit keyword-only parameters; Sonar S107 reports 0 on both constructors; composition root and tests rebound; full suite green

### t11 — \#139/h9: wire the clip->ask() perception lane as context, never a trigger

- instruction: Edit reachy/embody/engine.py / `_commands`/agent.py + tests. EmbodyTurnEngine.ask() (engine.py:594-603, the tool-less senses lane) has ZERO callers anywhere in reachy/ — designed, never wired. That is exactly what blocks #139's h9 acceptance ('ask the worker model where it is'). The runtime already republishes a rolling clip PATH REFERENCE on state.json's 'clip' key (reachy/behavior/`clip_rider.py`). Wire clip -> ask() and feed the ANSWER in as CONTEXT under t7's policy — it must never trigger a turn. A missing, stale or unreadable clip resolves to a named senselog drop, never a blocked or delayed turn. Tests use a fake ask fn and a fake clip reference; no real gateway, no real camera. You inherit t7's and t10's changes to both files.
- depends on: t7, t10
- covers: c27, h13
- acceptance:
  - with a fake ask fn and a fake clip reference, the layer turns the runtime's clip into context visible to the next triggered turn; ask() gains its first real caller; a missing or stale clip resolves to a named drop, never a blocked turn

### t12 — land scripts/`embody_bus_feed.py` with tests and operating-guide docs

- instruction: Move scripts/`embody_bus_feed.py` from untracked into the repo with tests + operating-guide docs. It is currently UNTRACKED on main and is the ONLY bus intake the layer has: installed `events_cli` 0.9.0 exposes no subscribe surface (verified live), so `resolve_bus_subscriber` degrades to feed-tail and this bridge is what makes the MQTT route work at all. Pin by test: the `O_RDWR`|`O_NONBLOCK` FIFO hold (the bridge never blocks and never EOFs — when it exited during live testing the layer's cue reader hit EOF and the layer DIED); the rule,intent,motion default source filter with `REACHY_BUS_FEED_SOURCES` override; byte-identical payload passthrough (the bus payload already matches the feed line shape, so this is a pipe not a translator); and the events-only topic filter — it subscribes reachy/events/<source>/# and never reachy/state/#, so RETAINED state values can never replay into cues on reconnect. Document the last-writer-EOF lifecycle in the operating guide.
- covers: c16, h11
- acceptance:
  - tests pin the `O_RDWR` FIFO hold (no EOF while the bridge lives), the rule,intent,motion default source filter, byte-identical payload passthrough, and the events-only topic filter (retained reachy/state can never replay into cues); the operating guide documents the last-writer-EOF lifecycle

### t13 — \#142: strip the 296 inert noqa markers, keep the 20 real ones and all prose (runs last, alone)

- instruction: RUNS LAST, ALONE — every code task must be merged first (that is why this depends on t1-t5 and t9-t12). 296 inert '# noqa:' markers name lint codes flake8 7.3.0 (no plugins) CANNOT emit; they suppress nothing and read as tool-acknowledged suppressions. Recounted on current main: 316 total = 296 inert + 20 real, unchanged since #140 merged. Split: reachy/ 176, tests/ 118, scripts/ 2 across 85 files. THE TRAP: E402 (11), F401 (4), E731 (2), E704 (2), E712 (1) are LOAD-BEARING pycodestyle/pyflakes codes — 20 sites that must survive byte-identical. FILTER ON THE CODE SET, never on the presence of the noqa token. Measured good news: ZERO lines carry both a real and an inert code, so every decision is per-line and all-or-nothing. KEEP THE PROSE — it is the valuable half ('# noqa: BLE001 — a cold holder is a null rate, not a crash' becomes '# a cold holder is a null rate, not a crash'). Mind the em-dash separator and trailing whitespace (W291); run black afterwards. Do NOT fix any underlying warning — a documented broad 'except Exception:' stays exactly as it is. Read the whole diff: any line where CODE changed is a mistake. flake8 green is the proof the removed markers were inert.
- depends on: t1, t2, t3, t4, t5, t9, t10, t11, t12
- covers: c8, h3, c22, h24
- acceptance:
  - flake8/black/isort green after the strip (the proof the removed markers were inert); full suite at its then-current count; git diff shows comment-text changes only; the 20 real E402/F401/E731/E704/E712 sites survive byte-identical; the PR's Sonar S7632 count is 0

### t14 — durable enablement on the box: env repoint to role names, operator-local units, linger, guide; live acceptance

- instruction: LIVE BOX TASK — serializes with t8, and it restarts services. Three parts. (1) ENV: ~/.config/environment.d/10-reachy-llm.conf pins `REACHY_OPENAI_MODEL_ID` to the served gemma id, with a now-DEAD Qwen id commented out one line above — proof this drift already bit once. Repoint it to the ROLE name and archive a probe showing the engagement classifier still resolves live. (2) DURABLE ON: operator-local systemd --user units for the embody layer AND the bus bridge (Requires=/After= the runtime chain). The REPO ships no embody unit and must not gain one — service.py's `_PRESENCE` stays the closed demo/runtime pair; these units are operator-local and documented, not repo code. loginctl enable-linger is the boot-at-power-on prerequisite. Verify with a full systemctl --user stop/start cycle simulating a reboot: daemon + runtime + bridge + layer all active, layer having heard or spoken at least once, UN-ATTENDED. (3) DOCS + ROLLBACK: the operating guide must alone suffice to disable and re-enable the layer; exercise ONE presence switch showing 'service enable demo' stop-propagating to the layer units as DOCUMENTED behaviour, not a surprise. Reachy's speaker stays Reachy's — no other client is routed onto it, and the robot sink still names transport='http' literally on every play call.
- depends on: t13
- covers: c28, h14, c35, h32, c36, h33, c29, h27
- acceptance:
  - environment.d names no served id (`REACHY_OPENAI_MODEL_ID` -> role name) and the classifier resolves live (archived probe); after a full systemctl --user stop/start cycle simulating reboot, daemon + runtime + bridge + layer are all active with the layer having heard or spoken at least once, un-attended; the guide alone suffices to disable and re-enable the layer, one presence switch is exercised showing documented stop-propagation; Reachy's speaker is touched by no other client (the robot sink still names transport='http' literally on every play call)

### t15 — arc closure: framing verification, boundary suites unchanged, evidence archived, issues closed

- instruction: Arc closure — verification and accounting, minimal code. Run the full suite and confirm the `agent_turn`, embody redteam and zero-LLM boundary suites pass UNCHANGED with no pin loosened anywhere (if a pin had to be loosened, the change was wrong, not the pin). Verify every after-state fact in the spec maps to a requirement whose test actually ran. Close #132 #133 #134 #135 #136 #142 #143 each linking its evidence; handle #137 and #138 per their MEASURED outcomes (#137 may be re-scoped rather than closed; #138 ships detect-only so state what remains). Close #139 or update it — the second-audio-output blocker dissolved when the operator confirmed they will be the second conversational party in the room. Every success-signal gate must be checkable from archived artifacts by a reader with no private context: test node ids, Sonar counts, probe transcripts, evidence files.
- depends on: t14
- covers: c1, h17, c2, h18, c3, h19, c4, h20, c5, h21, c20, h22, c24, h25, c25, h26
- acceptance:
  - full suite green; the `agent_turn`, embody redteam and zero-LLM boundary suites pass unchanged with no pin loosened; every after-state fact maps to a requirement whose test ran; each closed issue (#132 #133 #134 #135 #136 #142 #143, and #137/#138 per their measured outcomes) links its evidence; every success-signal gate is checkable from archived artifacts by a reader with no private context

## Risks

- [unknown_nonblocking] context-buffer internal discipline (structure, dedup keys, lock granularity, O(1) submit cost) is decided at build time inside t7 — the spec pins behaviour, not structure (task t7)
- [unknown_nonblocking] engine.py and `_commands`/agent.py are touched by t7, t10 and t11 — contention is resolved by the dependency chain (t6->t7->t10->t11), residual merge friction remains if a task strays from its file scope
- [unknown_nonblocking] t8 and t14 run against the ONE live robot and restart services — they must never run beside another live task, and the suite's conftest guards do not protect spawned subprocesses
- [unknown_nonblocking] \#137 may attribute daemon-side, in which case no fix ships from this repo and the issue is re-scoped with the measurement (t8's acceptance covers both outcomes)
- [unknown_nonblocking] t14's spoken-response acceptance leans on the lobes TTS/voice lane being up (user-confirmed expectation); a down lane at acceptance time delays h14's 'heard or spoken' until the lane returns
