# embodiment arc followups

> reachy-mini-cli closes out the embodiment-layer arc's findings: the layer treats runtime events as context it reads on its next turn rather than a trigger each (only alerts and heard speech trigger), the repo's 296 fake lint suppressions are gone, and every known runtime defect from the arc is fixed, named in the journal, or explicitly deferred with its cause on record

## Audience

- operators running the embodiment layer beside the live runtime, and maintainers who read the journal when a sense goes quiet

## Before → After

- Before: every cue triggers a turn (187 cues -> 23 turns -> 19 input-queue-full); 296 noqa markers name linters flake8 cannot emit; forge's default model 404s on the live gateway; two substring PID guards survive #140; a 404 handshake reads as a generic refusal; the tick has run 5% over budget for days
- After: a sense-cue flood with no utterance and no rule fire produces zero turns yet stays visible to the next turn; the journal names lane-off, camera-dead and stream-ended states; stop can never signal a process that merely shares a path substring; executable defaults name gateway roles, not served ids; the suite carries no fake suppressions

## Why it matters

- 23 streaming LLM calls in 40 s with zero robot-decided prompts is the measured cost of treating every runtime event as a trigger; the remaining findings are silent-failure classes (a deaf robot that logs a generic refusal, a stop that can SIGKILL an innocent process, a broken forge default) this repo keeps paying for

## Requirements

- issue #143: EmbodyTurnEngine gains a three-class input policy — an utterance or an alert cue (a rule fire) triggers a turn; sense/intent/motion/rule-suppression cues accumulate in a bounded, coalescing context buffer that the next turn drains and that never causes one; replaying the measured 40 s window (187 cues, 0 rule fires, 0 utterances) produces 0 turns
  - honesty: replaying the measured 40 s window (187 cues, 0 rule fires, 0 utterances) through the engine with a fake model fn produces 0 turns, and a subsequent rule-fire cue produces exactly 1 turn whose prompt carries the coalesced context
- issue #143: the cue's class must TRAVEL — reachy/embody/cues.py's `_MAPPERS` knows each cue's runtime-event type (rule/sense/intent/motion) but `submit_cue`(text) erases it into one pending deque; the mapper's return shape (or a parallel classifier) carries alert-vs-context to the engine, and `_commands`/agent.py's intake routes on it
  - honesty: alert-vs-context is decided from the mapper's typed output (never re-sniffed from cue text at the engine), and both intake routes (bus bridge and feed-tail) classify identically
- issue #142: strip the 296 inert noqa markers (reachy/ 176, tests/ 118, scripts/ 2) keeping the prose and the 20 real E402/F401/E731/E704/E712 sites untouched — filter on the code set, never on the noqa token; verification is the issue's own five-step list (flake8/black/isort green, suite at count, diff comment-text-only, Sonar S7632 -> 0)
  - honesty: after the strip: flake8/black/isort green, the full suite at its then-current count, git diff contains comment-text changes only, and the follow-up PR's Sonar S7632 count is 0
- issue #133: reachy/speech/tools.py enforces reachy.behavior.rules.`MAX_SAY_CHARS` in `_require_text` (or a sibling), refusing over-length text fail-closed with rules.py's message shape — never truncating; embody/tools.py already imports the same constant at module scope, so the precedent and the message shape both exist
  - honesty: dispatch('speak', text of `MAX_SAY_CHARS`+1) refuses fail-closed naming the cap without truncating or synthesizing, and every `test_speech_tools.py` boundary assertion stays green with the rules import in place
- issue #134: the runtime's hearing leg (reachy/speech/realtime.py RealtimeTranscriber) names an HTTP-404 handshake with the SAME reason the duplex peer already ships — `realtime_duplex.py`'s `REASON_LANE_UNAVAILABLE` ('realtime-lane-unavailable') — with detail pointing at /v1/capabilities stt.feasible; backoff and latch unchanged; covered by a tests/`fake_realtime_server.py` refusal scenario
  - honesty: a `fake_realtime_server` 404 handshake scenario yields ONE latched realtime-lane-unavailable drop whose detail names /v1/capabilities stt.feasible, the reconnect backoff is unchanged, and a non-404 refusal still reports handshake-refused
- issue #136 is NOT closable as shipped: two substring PID guards survive #140 — reachy/alive.py:223 ('demo-mode' in cmdline, demo-mode's stop guard) and reachy/daemon.py:141 (`DAEMON_BINARY` or '`reachy_mini`' in cmdline, live hazard: the sibling checkout /home/spark/git/`reachy_mini` satisfies it) — alive.py migrates to procsup.`has_argv_tokens`, daemon.py gets a basename-aware predicate in procsup, each with the spawn-a-real-innocent-sibling regression test, THEN #136 closes
  - honesty: a real innocent process spawned from this project's own venv (path containing the identity tokens) survives 'demo-mode stop' and 'daemon stop' in a regression test, and the daemon guard matches on argv basename, not substring
- issue #132: the sweep found two executable served-id/alias defaults — reachy/forge/client.py:49 `DEFAULT_FORGE_MODEL`='qwen3' is BROKEN today (live gateway: `model_not_found`) and reachy/vision/scene.py:59 pins the served gemma id (still served, drift-prone) — both move to gateway ROLE names (cortex / senses), verified live to resolve; then #132 closes
  - honesty: no executable default in reachy/ names a served model id or dead alias after the change; forge dispatch and the scene leg resolve via role names against the live gateway (gateway-gated test or archived probe)
- issue #135: fix the wedged-consumer tee flake at cause, not by timeout — reproduce with the assertion captured first; the candidate is `audio_tee.py`:725 bumping tee.dropped BEFORE emitting the consumer-slow line while the test asserts caplog immediately after observing the counter (the same cross-thread ordering class as the three chased siblings); the fix waits on the observable condition, never raises `WAIT_BUDGET_S`
  - honesty: the fix waits on the observable condition rather than a snapshot read, `WAIT_BUDGET_S` is untouched, and the commit documents the captured-or-reasoned cause; 30 consecutive -n auto runs of the file stay green
- issue #137: ATTRIBUTE the sustained 5% tick overrun first (the issue's own first step) — camera-alive correlation points at `face_sense`'s per-tick media.frame() read; a FaceSenseDriver-composed-vs-not measurement settles it in one run; fix follows only if the attribution names a client-side cause, else the issue is updated with the measured cause and stays open against the daemon side
  - honesty: one archived measurement (FaceSenseDriver composed vs not, same box, same window) attributes the overrun; the result lands on issue #137 either as a shipped client-side fix or as a measured daemon-side cause with the issue left open and re-scoped
- issue #138 detect leg: a named, latched camera-stream-ended drop when no usable frame arrives for N seconds while the camera is believed present — HeldMediaClient.connected cannot see a dead pipeline, so the condition keys on frame staleness, not the connection flag; recovery is EXCLUDED until the recoverability probe answers (parked)
  - honesty: with a fake media client that reports connected while frames stop arriving, ONE latched camera-stream-ended drop appears within the staleness window; no rebuild or recovery is attempted; a camera that legitimately never existed produces no such drop
- scripts/`embody_bus_feed.py` (untracked, working on the box) lands in the repo with tests: events-cli 0.9.0 ships no subscribe surface (verified live), so the bridge IS the only bus intake the layer has; landing it records the FIFO discipline (`O_RDWR` hold, last-writer-EOF ends the layer's run) instead of leaving it folklore on one dev box
  - honesty: the bridge lands with tests pinning the `O_RDWR` FIFO hold (no EOF while the bridge lives), the source filter default, and the payload passing through byte-identical; the operating guide documents the layer's last-writer-EOF lifecycle
- issue #141 S107 (q1 approved): the bounds of EmbodyTurnEngine.`__init__` (21 params) and RealtimeDuplexSession.`__init__` (23) move into a frozen per-engine Limits dataclass with documented defaults; injectable SEAMS (model fns, hooks, clocks, sinks, predicates) stay explicit keyword-only parameters; composition root and tests rebind
  - honesty: Sonar S107 reports 0 on both constructors, every bound keeps its documented default in the Limits dataclass, and the full suite passes with the composition root rebound
- issue #139 h9 (q2): the clip->perception lane gets wired — the layer consumes the runtime's clip reference (state.json 'clip' key) and puts it through EmbodyTurnEngine.ask() (today: zero callers) so a turn can know what the robot sees; the answer enters the turn as CONTEXT under the #143 policy, never as a trigger
  - honesty: with a fake ask fn and a fake clip reference, the layer turns the clip into context visible to the next triggered turn; ask() gains its first real caller; a missing/stale clip is a named drop, never a blocked turn
- q3: after #143 lands, the layer comes up durably on the box — operator-local systemd --user units for the embody layer and the bus bridge (Requires/After the runtime chain), surviving a reboot un-attended; the repo ships documentation in the operating guide, not units (h16 stands)
  - honesty: after a reboot (or a full systemctl --user stop/start cycle simulating one), daemon + runtime + bridge + layer are all active with the layer having heard or spoken at least once, un-attended; the setup steps are reproducible from the operating guide alone
- challenge finding (#143 alert flood): the alert class gets its own containment — alerts arriving while a turn is pending or running COALESCE into the next turn rather than queueing one turn each, and a minimum interval between alert-triggered turns bounds a cooldown-0 or multi-rule burst; the measured flood must be unreproducible through the alert class too (rules.py: `cooldown_s`=0 is legal, several rules can fire in one tick)
  - honesty: a replayed burst of 10 rule-fire cues inside one turn window produces at most 2 turns, with every fire visible in the drained context of those turns
- challenge finding (#143 observability): the context park is VISIBLE — each turn's senselog line counts what it drained (context=N coalesced-from=M), the export thinking block carries the drained context, and docs/export-schema.md is updated in the same change if the block shape moves
  - honesty: the replay test asserts the coalesced counts appear in the turn's senselog line and in the export thinking block; any wire-shape change ships with the schema-doc diff in the same commit
- challenge finding (#136 predicate, probed live): the daemon guard matches basename-of-any-argv-token == reachy-mini-daemon — the live daemon's cmdline is \[.../bin/python3, .../bin/reachy-mini-daemon\], so an argv\[0\]-only or substring rule is wrong in opposite directions; the regression test's innocent process carries the sibling-checkout path /home/spark/git/`reachy_mini` that satisfies today's substring check
  - honesty: the regression test spawns a real process whose argv contains /home/spark/git/`reachy_mini`-style paths and asserts daemon stop leaves it alone, while a fake cmdline shaped like the measured live daemon still matches
- challenge finding (#132 ops half, probed live): ~/.config/environment.d/10-reachy-llm.conf pins `REACHY_OPENAI_MODEL_ID` to the served gemma id — and the commented-out dead Qwen id above it shows this exact drift already happened once; the durable-on work repoints it to the role name and the guide names roles as the only sanctioned env values
  - honesty: after the repoint the engagement classifier and agent attach resolve via the role name on the live box (one archived probe), and no environment.d line names a served id
- challenge finding (c28 ops lifecycle): the guide documents that 'service enable demo' stop-propagates to the layer units (they Requires/After the runtime), the one-command rollback (disable --now both operator units leaves the runtime untouched), and the loginctl enable-linger prerequisite for boot-at-power-on
  - honesty: the guide alone suffices to disable and re-enable the layer; the acceptance window exercises one presence switch and shows the layer stopping with the runtime as documented, not as a surprise

## Honesty conditions

- every issue named in the announcement ends the arc closed or explicitly re-scoped with its measured cause on record, and the layer runs durably with the new turn policy live
- the operating guide's embodiment section addresses the operator's setup end to end, and every failure state added by this arc is a named journal line a maintainer can grep
- the 40 s flood figures and each silent-failure class cited in the spec trace verbatim to the archived measurement or the line-numbered code read in the scope entries
- each before-state fact traces to a live measurement or a line-numbered scope entry, none to recollection
- each after-state fact maps to at least one requirement claim whose own honesty condition tests it
- python -c 'import reachy.speech.tools' followed by a sys.modules scan shows no reachy.motion / reachy.vision / reachy.speech.llm / reachy.speech.events / reachy.forge entries
- the replay test runs offline under pytest -n auto with every conftest guard active, reaching no gateway, broker, robot or audio device
- no diff in the arc touches AgentTurnEngine's snapshot-only buffer or its permanent latch; `test_agent_turn.py` semantics unchanged
- no code path added by the arc constructs, rebuilds or restarts a media pipeline in response to staleness — grep-provable on the diff
- the #142 diff contains comment-text changes only; any line whose code changed is a defect in the pass
- the existing redteam and boundary test suites pass unchanged — the arc loosens no pin anywhere
- each listed gate is checkable from archived evidence (test node ids, Sonar counts, probe transcripts) by a reader with no private context
- the robot sink keeps naming transport='http' literally on every play call and no arc change routes any other client onto Reachy's device
- the duplex send-surface test still pins exactly three frame kinds leaving the socket; no context-injection frame is added by the arc

## Success signals

- the replay test: the measured 40 s window (187 cues, 0 rule fires, 0 utterances) drives 0 turns and the next triggered turn's prompt carries the coalesced context; plus each issue's own gate — Sonar S7632 at 0, forge dispatch resolving live, a fake-server lane-off scenario green, the innocent-sibling kill test green, and #132/#133/#134/#135/#136/#142/#143 closed

## Scope / boundaries

- agent attach's cognition is untouched: AgentTurnEngine keeps its snapshot-only cue buffer (it already models the context half correctly) and its permanent failure latch; only the embodiment layer's trigger policy changes
- no automatic camera recovery ships in this arc — whether a GStreamer EOS is recoverable in-process at all is unprobed; the detect leg (naming the state) is the whole #138 deliverable here
- issue #142 fixes no underlying warnings — a documented broad 'except Exception:' stays exactly as it is; the diff is comment text only
- the embodiment layer's standing constraints hold unchanged: no `reachy_mini` or reachy.daemon import under reachy/embody/, function-local cognition imports in `_commands`/, the h13 closed send surface, and the suite never reaches a real gateway, broker, robot, audio device or actuator
- Reachy's speaker is available to Reachy and must not be taken by any other device — the layer's voice reaches it only through the daemon route the robot profile already hard-names; no PipeWire/browser client gets routed onto it (user statement 2026-08-02)
- the context park drains into ENGINE turns only (both trigger classes); the duplex session's gateway-side voice response stays audio-context-only — its send surface keeps the pinned three frame kinds (session config, audio append, response.create); context-aware speech reaches the room via the engine's speak/harmonics tools, never by widening the realtime wire

## Non-goals

- adopting ruff is NOT this arc (it would make the stripped codes real and re-litigate #142's premise); it stays a tracked decision on #141 unless the user says otherwise

## Assumptions

- importing `MAX_SAY_CHARS` from reachy.behavior.rules into speech/tools.py keeps every boundary test green: rules.py's closure (behavior.library -> `feel_alive`/model/orient/`pet_reaction`/sense + cli.`_errors` + daemon.`state_dir`) reaches no reachy.motion, reachy.vision, speech.llm, speech.events or reachy.forge — the sets `test_speech_tools.py` pins
- issue #143's replay acceptance is testable offline: the measured 40 s window is reproducible as feed lines through `cues_for_line` into the engine with a fake model fn — no gateway, no robot, consistent with the suite's never-reach-a-real-service guards
- the lobes TTS lane should be working — the gateway's voice leg is expected up for the layer's spoken responses (user statement 2026-08-02; the runtime's own harmonic voice needs no gateway and spoke on a pat earlier the same day)

## Scope exploration

- `s1` — `reachy/embody/engine.py:498-533 (_offer / run_turn)`: `submit_cue` and `submit_utterance` funnel into ONE `_pending` deque and `run_turn` fires whenever anything is pending — every cue is a trigger by construction; the 187-cue/23-turn/19-drop live measurement follows directly
  - seeds: `c6`
- `s2` — `reachy/embody/cues.py _MAPPERS + reachy/cli/_commands/agent.py:639-641,902`: the cue's runtime-event class (rule/sense/intent/motion) is known at mapping time and erased at `submit_cues`(text) — the intake thread feeds plain strings, so alert-vs-context cannot be decided engine-side today
  - seeds: `c7`
- `s3` — `reachy/speech/agent_turn.py (the snapshot-only buffer)`: agent attach already models the context half correctly — a snapshot()-only cue buffer that never triggers; #143 is the embody engine converging back toward that shape for the non-alert classes
  - seeds: `c20`
- `s4` — `reachy/speech/tools.py:159 _require_text + tests/test_speech_tools.py boundary asserts + reachy/behavior/library.py imports`: `_require_text` checks only non-empty; the boundary tests pin no-motion/no-vision/no-llm/no-events/no-forge, and rules.py's transitive closure stays inside behavior + cli.`_errors` + daemon — the `MAX_SAY_CHARS` import is legal
  - seeds: `c9`
- `s5` — `reachy/speech/realtime.py:422-424,826-832 + reachy/speech/realtime_duplex.py:148-155,312-314`: `_ws_connect` already surfaces the HTTP status precisely so a caller can name it; the duplex caller ships `REASON_LANE_UNAVAILABLE` for 404 with full rationale — the transcriber caller folds the same 404 into generic handshake-refused; the fix is porting the existing name
  - seeds: `c10`
- `s6` — `reachy/{sleep,vision,behavior,embody}/supervisor.py + reachy/alive.py:207-223 + reachy/daemon.py:129-141 + reachy/demo_service.py`: the four supervisors all route through procsup.`has_argv_tokens` (the #140 fix holds), but alive.py's demo-mode stop guard and daemon.py's stop guard still substring-match /proc cmdline — and `demo_service.py`, which #136's table names, is systemd-only and never had a pid guard; the real fifth site is alive.py
  - seeds: `c11`
- `s7` — `live lobes gateway probe (GET /v1/models + role/alias completions, read-only)`: served today: Qwen3.6-27B/35B, gemma-4-12B, embeddings; model 'qwen3' -> `model_not_found` (forge's default is broken NOW); roles 'cortex' and 'senses' both resolve — confirming #132's role-name contract live
  - seeds: `c12`
- `s8` — `reachy/forge/client.py:49 + reachy/vision/scene.py:58-59`: the two executable defaults naming a served id or dead alias: `DEFAULT_FORGE_MODEL`='qwen3' (broken), `DEFAULT_VISION_MODEL` pins the served gemma id (works today, breaks on the next promotion); tests/`test_speech_llm_tools_integration.py` already pins the role name and documents the drift history
  - seeds: `c12`
- `s9` — `tests/test_behavior_audio_tee.py::test_a_wedged_consumer_never_blocks_the_offering_thread + reachy/behavior/audio_tee.py:725`: the test `_waits` on tee.dropped>0 then immediately asserts caplog; the worker bumps dropped one line BEFORE emitting the consumer-slow log — a window where the counter is visible and the line is not, matching the chased sibling flakes' cross-thread ordering class
  - seeds: `c13`
- `s10` — `reachy/behavior/face_sense.py module docstring + issue #137's condition table`: the heavy detection leg is off-tick by design, but the per-tick media.frame() read is the module's own flagged expensive leg; overrun correlates with camera-alive (0 overruns in 100 s with camera dead under active AND wedged tee consumers) — attribution, then fix
  - seeds: `c14`
- `s11` — `reachy/robot/media_client.py (connected@319, get_frame) + issue #138's live journal`: connected is a connection-level flag; a pipeline that EOS'd still looks connected, so `_HolderKeeper`'s re-warm never fires — the detect condition must key on frame staleness while the camera is believed present, exactly as the issue reasons
  - seeds: `c15`
- `s12` — `installed events_cli 0.9.0 (live import) + scripts/embody_bus_feed.py (untracked) + tests' subscribe canary`: `events_cli` exposes no subscribe surface, so `resolve_bus_subscriber` degrades to feed-tail and the untracked bridge is the ONLY bus intake; the layer died when the bridge exited (last-writer EOF -> `should_stop`) — the script and its FIFO discipline must land in-repo or stay one-box folklore
  - seeds: `c16`
- `s13` — `reachy/embody/engine.py:594-603 ask() (the senses lane)`: ask() has ZERO callers anywhere in reachy/ — the clip->perception-question leg is designed but unwired, which is exactly #139's h9 blocker; wiring it is a code task this arc could take or explicitly drop
  - seeds: `c25`
- `s14` — `issue #142's own measurement (296 inert / 20 real / 0 mixed, flake8 7.3.0 no plugins)`: the issue already contains the executable spec including the E402/F401/E731/E704/E712 trap and the verified strip experiment — the scope pass adds nothing except sequencing (after #140, which has merged)
  - seeds: `c8`
- `s15` — `issue #141 (both halves) + sonar-project.properties`: the noqa half is decided by executing #142 (option 1, the honest end state); the S107 half and ruff adoption are genuine user decisions — routed as q1 and folded into the non-goal boundary respectively, not silently taken
  - seeds: `c17`
- `s16` — `the live box, by ear (user, 2026-08-02)`: a pat fired the rule and Reachy spoke harmonically through its own speaker — the runtime's pat sense, rule engine, SpeechActuator and speaker path are all verified working live by the operator's own ear
  - seeds: `c29`, `c30`
- `s17` — `challenge pass / adjacent-systems lens: reachy/runtime_cues.py (shared with agent attach)`: the typed alert-vs-context classification lands in embody/cues.py's own dispatch table, which already knows each event's type — `runtime_cues`' shared return shape stays list\[str\], so agent attach's caller is untouched and c20 holds without tension against c7
  - seeds: `c7`
- `s18` — `challenge pass / adjacent-systems lens: reachy/service/{units,manager}.py vs operator-local embody units`: `cleanup_retired_units` and install/enable purges walk only catalog + `RETIRED_UNITS` names, so operator-local embody units are never touched by them; but presence switching stop-propagates through Requires= — seeded the ops-lifecycle doc requirement
  - seeds: `c36`
- `s19` — `challenge pass / counter-evidence lens: noqa recount on current main`: grep counts 316 markers = the issue's 296 inert + 20 real exactly, so #142's snapshot survived #140's merge unchanged; the sweep still re-verifies at execution time by running flake8 after the strip
  - seeds: `c8`
- `s20` — `challenge pass / failure-mode lens: reachy/behavior/rules.py cooldown floor + multi-rule fires`: `cooldown_s`=0 is legal (nonneg validator, which also passes inf/nan per #133's side note) and several rules can fire in one tick — so the alert class can flood exactly like the sense class did; seeded the alert-containment requirement
  - seeds: `c32`
- `s21` — `challenge pass / failure-mode lens: scripts/embody_bus_feed.py topic filter`: the bridge subscribes reachy/events/{source}/# only — never reachy/state/# — so RETAINED state values can never replay into cues on (re)connect; the landing tests pin the filter
  - seeds: `c16`
- `s22` — `challenge pass / probe: /proc/<daemon-pid>/cmdline on the live box`: measured argv: \[.../uv/tools/reachy-mini-cli/bin/python3, .../bin/reachy-mini-daemon\] — argv\[0\] is the interpreter, so the correct predicate is basename-of-any-argv-token; an argv\[0\]-only rule would MISS the real daemon and a substring rule keeps the sibling-checkout hazard
  - seeds: `c34`
- `s23` — `challenge pass / probe: ~/.config/environment.d/10-reachy-llm.conf`: `REACHY_OPENAI_MODEL_ID` pins the served gemma id, with the previous (now-dead) served Qwen id commented out one line above — the #132 drift class is live in ops config and has already bitten once
  - seeds: `c35`
- `s24` — `challenge pass / concurrency lens: EmbodyTurnEngine._turn_lock + intake thread + duplex utterance thread`: the future context buffer takes submits from two threads while `run_turn` drains under the turn lock; examined — the coalescing structure and its O(1) submit-path cost are build-time decisions, parked as residual risk for the plan
- `s25` — `challenge pass / reversibility lens: #142's 85-file sweep + the layer's enablement`: the sweep is one atomic comment-text-only PR (git revert restores it cleanly); the layer's rollback is disabling two operator-local units with the runtime untouched — both reversible by one command, seeded into the ops doc requirement
  - seeds: `c36`
- `s26` — `challenge pass / security lens: reachy/embody redteam suite + the duplex closed send surface`: no arc claim loosens a pin — c24/h25 already require the redteam and boundary suites to pass unchanged, and c31/h28 pin the three-frame-kind send surface; clean pass, residual risk only if a build task tries to widen the wire (which h28 would catch)

## Decisions

- issue #141's noqa half resolves as its option 1 — drop the fake prefix, keep the prose (the honest end state) — executed by #142; the ruff-adoption alternative and the S107 seam-vs-bounds constructor question remain open decisions routed to the user as questions on this frame

## Open parks

- [unknown_nonblocking] issue #137's root cause is unmeasured until the attribution run — it may land daemon-side (the IPC frame fetch), in which case no fix ships from this repo and the issue is updated, not closed
- [unknown_nonblocking] issue #138: whether a GStreamer EOS is recoverable in-process (re-warm the media client) or only by restarting the daemon's pipeline — needs a live probe nobody has run
- [unknown_nonblocking] issue #135's failing assertion has never been captured — the ordering candidate is reasoned from `audio_tee.py`:725, not observed; if the reproduce loop surfaces a different assertion, the fix follows the evidence
- [unknown_nonblocking] the context buffer's internal discipline (structure, dedup keys, lock granularity, submit-path cost) is a build-time decision — lands as a plan risk when /spec-to-plan seeds, not as spec text
- [unknown_nonblocking] engine.py is touched by four separate claims (c6/c7 policy, c26 Limits, c27 clip lane, c32 alert containment) — same-file contention across plan tasks is a sequencing risk the plan must resolve by dependency, not parallelism
