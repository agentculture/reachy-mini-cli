# runtime senses + bounded rules + goto lane (75-78)

> The boot presence feels a pat and reacts through its rules, react rules can no longer hold a channel forever, gotos submit into the live running engine, and the reTerminal repoint has a safe feed design — all live-verified on the robot before PR.
> instruction: Deploy the branch build to the uv tool env (uv tool install --force '<repo>[daemon]'), restart reachy-runtime.service, run the three-lane live check (pat / goto / bounded-rule expiry) watching the runtime feed and [SENSE] lines, then open the PR via cicd with the evidence

## Audience

- Ori and the mesh agents operating the Reachy Mini, plus the deployed boot presence itself — the reachy-runtime.service process whose rules gain a live pat sense, bounded reactions, and a goto path

## Before → After

- Before: The runtime senses only doa/speech (DoaPoller over the daemon route); the deployed pat-acknowledge rule is armed but dormant; any looping rule target holds its channel permanently (the nod incident); the goto lane is built+tested but unwired (no live submission path); the reTerminal panel has been dark since the #74 flip
- After: Patting the booted robot makes it react through a rule (sense pat -> rule fire -> bounded gesture, visible on the runtime feed); react rules can no longer hold a channel forever (duration_s + fail-closed validation); a goto submits into the live engine through the intent spool and a behavior goto verb; the #78 panel-feed design is recorded with its boot-hang constraints

## Why it matters

- This is the symbolic-runtime thesis applied to its first new sense: a reaction becomes a rule you write (when pat -> run gesture) instead of hardcoded reflex logic — and the bounded-lifetime fix turns the rules file from a footgun into a safe operator surface

## Requirements

- A pat provider feeds Sense.pat_event in the runtime process: a new provider module reuses reachy/motion/pat.py PatDetector semantics (scratch=pitch press, side_pat=yaw nudge, two levels) against a MOVING commanded pose — the precedent is reachy/sleep/patwake.py PatWakeDetector — and is wired via reachy/behavior/sense.py SenseProviders.pat_event so read_perception composes it over the DoaPoller base in _compose_run_seam
  - honesty: With the runtime deployed, a physical pat yields a Sense.pat_event reading and the armed pat-acknowledge rule admits thoughtful — and the provider is a true peek: it never opens a media session, and an SDK failure degrades to 'no reading' (never a tick crash)
- The engine's composed pose becomes readable by seam riders: _drive already holds tick['pose'] (engine.py:444) but TickContext (engine.py:70-113) does not carry it — extend the seam (a pose field on TickContext, or a last-composed-pose holder driver on the TickBus) so pat compares commanded-vs-actual with zero busy_until guessing; the SAME holder serves goto_lane.py's documented start_pose_provider seam for continuity (no neutral-snap C0 discontinuity)
  - honesty: The seam extension is additive: every existing tick_seam rider and test passes unchanged, and the pose a provider reads equals the pose the engine streamed on the previous tick (no busy_until guessing anywhere)
- The pat provider reads actual head pose through a HELD in-process SDK client: head_pose is sdk-only (transport.py:70 — the daemon REST API has no read-back route) and SdkTransport.head_pose() opens a short-lived ReachyMini session PER CALL (sdk_transport.py:394) which is unusable at provider rates — the same fresh-client-per-read failure issue #73 root-caused for vision; the runtime unit keeps streaming over its http default transport
  - honesty: Exactly ONE ReachyMini client is opened for the provider's process lifetime — no per-call client churn (fd count stays flat over a soak run) — and a missing [sdk] extra or dead SDK leaves the runtime running with the pat field simply unfed (one logged warning, not a crash loop)
- React rules gain a bounded lifetime: rules.py _REACT_FIELDS (line 108) admits a validated duration_s and rule_engine.py _build (line 383) stops taking Lifetime(looping=entry.looping, duration=entry.default_duration) straight from the library — model.py Lifetime ALREADY supports bounded looping (looping=True, duration=N runs N seconds then expires) and library.resolve_lifetime already implements the override logic for the CLI's --once/--loop/--duration, so the machinery exists; only schema + admission lack it
  - honesty: A react rule with duration_s=N on a looping target expires its channel hold after ~N seconds on the live robot, and every previously-valid rules file (including the box's deployed one) still loads byte-for-byte unchanged
- Fail-closed option for unbounded targets: RulesConfig.from_dict already looks up the library entry per react rule during validation, so refusing a looping-default target with no explicit duration_s is a pure validation-gate addition matching the schema's existing posture (single gate, specific CliError, exit-1)
  - honesty: A react rule targeting a looping-default library entry WITHOUT duration_s is refused at load with a CliError naming the rule id, the entry, and the remedy — and the refusal happens in RulesConfig.from_dict (the single gate), so reload and boot both enforce it identically
- A goto command kind registers into the intents spool via the t7 seam: IntentDriver takes registry (intents.py:203-236, explicitly documented as the extension point for a future goto kind — no control.py edits), and GotoLane composes as one more TickBus driver in _compose_run_seam alongside the rules and intent drivers
  - honesty: A goto submitted through the intent spool admits into the LIVE engine and completes its lifecycle (admitted -> done on the feed) with real head motion; the lane's pinned preemption semantics are untouched (existing goto_lane tests green, no control.py diff)
- Live-test before PR on this box (which IS the robot — reachy-daemon + reachy-runtime active): deploy the branch build into the uv tool env (uv tool install --force, the #62 drop-in convention survives), restart the runtime unit, and (a) physically pat the head — the deployed rules.toml already carries the ARMED pat-acknowledge rule (verified on disk: when pat is_true run thoughtful, cooldown 6s) so the provider lights it with ZERO config change; (b) submit a live goto intent and watch admitted/done on the feed plus real head motion; (c) verify a bounded react rule expires its channel hold; single-presence means no second engine run coexists
  - honesty: The live-test is a PR gate, not a post-merge note: if any lane fails on the box the PR waits — and the single-presence invariant is respected during testing (no second engine process; deploy + restart the real unit instead)
- The #78 deliverable in this batch is a committed design note (docs/) for the runtime-feed export path: the --export-file candidate with O_NONBLOCK FIFO semantics, reuse of JsonlExporter's self-disable pattern, the unit-flag + panel.conf drop-in interaction, and the explicit boot-hang failure mode it must never reintroduce — implementation stays a follow-up with its own think/challenge pass
  - honesty: The design note names the failure mode concretely (FIFO open() blocking at boot with no reader) and every candidate in it is compatible with the exporter's existing stdout-purity and self-disable contracts — no candidate requires a second engine process
- The held pat-provider client is constructed MEDIA-FREE: ReachyMini(media_backend='no_media') — probed on the box: the default constructor brings up the WebRTC bidirectional-audio chain (SENDRECV transceiver, audio send chain) and accretes fds 17->96, while no_media construction is fds 4->5 with head-pose reads at ~0.02 ms median, flat over a 500-read soak; the client is closed explicitly on shutdown (the probe process HANGS at interpreter exit without close) and touched only from the engine thread
  - honesty: In the shipped provider: construction logs no WebRTC/audio lines, fd count is flat over a soak, reads never approach the 20 ms tick budget, and reachy-runtime.service stops cleanly on SIGTERM (no stop-timeout escalation) — all re-verified live, not just in the probe
- Pat sensing is OWNERSHIP-GATED against the #66 false-fire class: while any non-base behavior owns the head channel (a rule-admitted gesture, a goto), the detector suspends and re-baselines on resume — TickContext.ownership already exposes the owner, so the gate needs no new plumbing; without it, fast commanded motion the servos lag behind reads as external force (pat -> reaction -> more motion -> oscillation, the exact loop #43/#66 fixed in listen with busy_until gating)
  - honesty: Firing thoughtful and a goto on the live robot WITHOUT touching the head yields zero pat events across several cycles; a real pat during quiet base breathing still fires — both observed in the live-test pass
- The provider's held client is constructed LAZILY with bounded retry/backoff: a transient boot race with the daemon (runtime starts while the daemon is still coming up) leaves the pat field unfed and the runtime alive, then pat starts working within one retry interval of daemon health — never a crash loop, never a permanently-dead sense that needs a unit restart
  - honesty: Starting reachy-runtime with the daemon stopped, then starting the daemon, ends with pat working and no runtime restart — demonstrable on the box
- The goto intent kind validates at the boundary: channels and duration checked AND targets clamped-or-refused against the library's own clamp limits — verified by reading engine._compose_pose + HttpTransport.set_target (NO clamping anywhere on the streaming path; library behaviors self-clamp via library._clamp, but a goto interpolates toward the raw submitted target) and the spool is externally writable, so wild targets from a buggy agent must never stream to the daemon
  - honesty: A spool-submitted goto with an out-of-range target is refused (or clamped) at the kind handler with a specific error result visible on the feed — never POSTed raw to set_target
- The looping-hold fix covers BOTH admission surfaces: the intent spool's run_behavior kind gets the same bounded treatment as react rules — a payload naming a looping-default entry without an explicit bounded lifetime is refused with a specific error result (the existing _validated_lifetime seam is the single place to add it), so no autonomous OR agent-submitted admission can hold a channel forever by default
  - honesty: An agent-submitted run_behavior for nod with no lifetime payload is refused with an error result on the feed (not admitted); the same payload with lifetime {duration: 5} admits and expires — both demonstrable via the spool on the live box; existing bounded submissions are unaffected

## Honesty conditions

- Nothing in this batch merges until the on-box pass holds: a physical pat fires the armed rule, a live goto completes, an unbounded rule target is refused — each observed on the real robot, not only in tests
- Verified by reading reachy/export/runtime.py lines 354/411: goto.* kinds already map to motion blocks — the batch adds NO mapper code and the export-schema doc needs at most a status-line update
- The operator surface stays agent-first: every new verb/flag lands with --json, catalog entries, and rubric compliance so mesh agents drive it the same way Ori does
- Accurately describes the deployed box at scoping time — verified live via systemctl (runtime+daemon active) and the on-disk rules.toml (armed dormant pat rule), not recalled from memory
- Each after-state clause maps to a live-test lane in c12/c19 — none of them is claimable from unit tests alone
- The pat reaction ships as a RULE in rules.toml (data), not as new reflex code in the runtime — if the implementation hardcodes the reaction, this claim is false
- The batch diff contains no media-session open, no LLM client, no second presence unit, and no rms/face provider code — reviewable by inspection of the PR diff
- Every signal is observed and recorded (feed lines, [SENSE] logs, CI runs) before the PR opens — a signal that cannot be demonstrated is reported as failed, not assumed

## Success signals

- The pre-PR live-test pass on the box: a physical pat fires pat-acknowledge end-to-end with zero config change (feed shows the pat sense event, the rule fire, the thoughtful admission, and the head visibly reacts); a submitted goto shows admitted->done on the feed with real head motion; an unbounded looping rule target is refused at load with a clear error; a bounded rule's channel hold visibly expires; suite + teken rubric + lint green and version bumped

## Scope / boundaries

- The runtime feed mapper needs NO changes for gotos: reachy/export/runtime.py already maps goto.admitted/done/cancelled to motion blocks with action='goto' (lines 354, 411, _RAW_MOTION_ACTIONS) — verified, not assumed
- In-batch boundary: pat provider only (rms/face stay filed on #75); #78 ships as design, not implementation; no second presence process, no LLM call, no media-session open anywhere in the runtime; the reTerminal bridge stays out of repo

## Non-goals

- No naive FIFO export on the runtime unit: a FIFO open() with no reader blocks the boot (systemd hang); the exporter is stdout-only today (enforced in BOTH reachy/cli/_export.py builders) and the runtime ExecStart deliberately carries no --export flag (units.py runtime_exec_start) — any file/FIFO target must use O_NONBLOCK semantics plus the exporter's existing broken-pipe self-disable pattern
- The reTerminal bridge itself stays out of this repo (the global reterminal skill owns it, per decision c27 of symbolic-runtime-70): this repo ships only the feed mechanism the panel tails; the runtime feed carries runtime events, cognition blocks belong to an attached agent's own feed

## Assumptions

- Holding an SDK state-read client in the runtime process is safe under the single-SDK-owner model: the live loop is disabled on the box (runtime is the sole enabled presence, verified via systemctl), and the pat path never opens the single-consumer media_session — only the rms sibling would need that
- The rules layer is already pat-ready: SENSE_FIELDS includes pat (rules.py:87), the rule engine evaluates is_true over pat_event presence, and rules over pat validate today — the ONLY missing link is the provider feeding the field (issue #75's exact claim, confirmed in code)

## Scope exploration

- `s1` — `reachy/behavior/sense.py`: SenseProviders + read_perception shipped in #74 but are unwired (module docstring says so explicitly); the provider contract is a zero-arg, failure-tolerant PEEK (_peek swallows any raise -> field default), and Sense.pat_event is (touch_type, level) mirroring EventBuffer.feed_pat — the pat provider slots in with no schema change
  - seeds: `c2`
- `s2` — `reachy/cli/_commands/behavior.py _compose_run_seam (line 570)`: the single composition point: DoaPoller + rules driver + IntentDriver + optional SenseSnapshotDriver ride ONE TickBus wrapped in TickMetrics; providers wrap the sense reader (read_perception over the DoA base) and GotoLane appends as one more driver — both changes are local to this function
  - seeds: `c2`, `c8`
- `s3` — `reachy/behavior/engine.py TickContext + _drive`: TickContext exposes now/tick/sense/ownership/emit/admit/evict/active_names but NOT the composed pose; tick['pose'] is at hand in _drive (line 444) and _invoke_seam already receives the tick dict — plumbing a pose field (or a last-pose holder) is a small, non-invasive seam extension, and the seam runs AFTER streaming so a provider reading it next tick compares against what the head was last commanded
  - seeds: `c3`
- `s4` — `reachy/behavior/goto_lane.py`: fully built + tested in #74: one-in-flight FIFO, pinned no-resume preemption, goto.admitted/done/cancelled emissions, MotionQueue-shaped adapter; its docstring DOCUMENTS the missing-composed-pose gap and defines start_pose_provider for continuity — the same pose holder #75 needs closes this documented limitation too
  - seeds: `c3`, `c8`
- `s5` — `reachy/robot/transport.py + sdk_transport.py head_pose`: head_pose is an sdk-only capability (base + http raise 'not supported'; the daemon REST API has no read-back route per the base docstring) and SdkTransport.head_pose() opens a short-lived ReachyMini session per call — a held-client read seam is required for a per-tick provider, echoing issue #73's vision root-cause (fresh client per frame -> None frames)
  - seeds: `c4`
- `s6` — `issue #73 (open follow-up)`: d1 live-testing root-caused the fresh-client-per-read pattern as the vision noun's crash; the face sibling explicitly interacts with its held-client fix — the pat provider must not repeat the pattern
  - seeds: `c4`
- `s7` — `reachy/behavior/rules.py + rule_engine.py + library.py + model.py`: _REACT_FIELDS has no duration field (rules.py:108); _build takes Lifetime(looping=entry.looping, duration=entry.default_duration) verbatim (rule_engine.py:383); looping entries with default_duration=None are feel-alive/nod/shake/speak/antenna-sway — any of them as a rule target = permanent hold; Lifetime already models bounded looping and library.resolve_lifetime already implements CLI-side override — the fix is schema + admission only
  - seeds: `c6`, `c7`
- `s8` — `reachy/behavior/intents.py IntentDriver`: the registry parameter (lines 203-236) is documented verbatim as 'the extension point a future command kind (e.g. a goto)' — register the goto kind handler into the shared KindRegistry, no control.py edits, exactly as issue #77 predicted (t7 seam confirmed)
  - seeds: `c8`
- `s9` — `reachy/export/runtime.py`: the runtime-feed mapper ALREADY handles the goto lifecycle: _RAW_MOTION_ACTIONS includes 'goto' and kinds starting 'goto.' map to motion blocks with action='goto' + detail.phase (lines 354, 411) — composing the lane needs zero mapper work
  - seeds: `c9`
- `s10` — `reachy/cli/_export.py + reachy/export/exporter.py`: both export builders (cognition + runtime) refuse any target but '-' (stdout); JsonlExporter already implements the broken-pipe self-disable pattern (catch, log once to stderr, disable) — the reusable half of a safe O_NONBLOCK file/FIFO sink for #78
  - seeds: `c10`
- `s11` — `reachy/service/units.py runtime_exec_start`: the runtime unit's ExecStart is bare '<python> -m reachy behavior engine run' — no --export, no --transport (http default per _robot.py), deliberately LLM-free per decision c19; adding an export flag means touching unit text + the machine-local panel.conf drop-in gotcha (drop-ins override ExecStart — flags must land THERE, from the harmonic-voice deploy lesson)
  - seeds: `c10`
- `s12` — `live box state (systemctl --user + ~/.local/state/reachy/behavior/rules.toml)`: this dev box IS the robot: reachy-daemon + reachy-runtime active, live/demo disabled (three-way exclusion holding); the deployed rules.toml carries the ARMED-but-dormant pat-acknowledge rule (when pat is_true run thoughtful, cooldown 6s) plus comments documenting the looping-hold gotcha — the pat live-test needs zero config, and the #76 fix should also update these on-box comments
  - seeds: `c12`, `c13`, `c5`
- `s13` — `challenge pass / adjacent-systems lens: reachy/behavior/intents.py _apply_run_behavior + _validated_lifetime`: the intent run_behavior kind has the SAME looping-hold defect #76 fixes for rules — admits with library defaults when the payload carries no lifetime; the payload CAN express a bounded one, so the gap is default-behavior, not capability; routed as q5 (user decision: intents are deliberate agent actions with a stop path, unlike autonomous rules)
- `s14` — `challenge pass / observability lens: reachy/export/runtime.py SenseSnapshotDriver`: CLEAN — the sense block already publishes pat/rms/face/frame_available (lines 477-490), so the live-test's 'feed shows the pat sense event' evidence needs zero new code; change-only emission means a pat event always publishes (frozen-dataclass inequality)
  - seeds: `c12`
- `s15` — `challenge pass / adjacent-systems lens: reachy/behavior/reload_driver.py + rules.RulesLoader`: CLEAN — reload drains its own spool, loader.reload() never raises, a rejected candidate keeps the last-good config, and both boot and reload validate through the ONE RulesConfig.from_dict gate, so c7's fail-closed check applies identically on both paths by construction
  - seeds: `c7`
- `s16` — `challenge pass / counter-evidence probe: held ReachyMini client (probes A/A', scratchpad, run against the live daemon while the runtime streamed)`: default ReachyMini() is NOT state-only — it brings up WebRTC bidirectional audio and accretes fds 17->96; media_backend='no_media' construction is fds 4->5, reads ~0.02 ms median / 0.096 max (tick budget untouched), fd-flat over 500 reads; the process HANGS at exit without an explicit close; SDK 1.9.0 on the box matches the repo pin; coexistence with the streaming runtime confirmed live
  - seeds: `c21`, `c23`
- `s17` — `challenge pass / failure-mode lens: engine._compose_pose + HttpTransport.set_target + goto_lane.build_goto_behavior`: no clamping anywhere on the streaming path — _compose_pose passes contributions raw, set_target unit-converts and POSTs; library behaviors self-clamp (library._clamp) so today every source is bounded, but a goto interpolates toward the raw submitted target and the intent spool makes that externally reachable; ALSO confirmed: the daemon ignores set_target while an interpolated move runs (http_transport comment), reinforcing the in-engine goto design; seeded c24
  - seeds: `c24`
- `s18` — `challenge pass / concurrency lens: the #66 oscillation class vs 50 Hz commanded-pose comparison`: the spec's 'zero busy_until guessing' is true but incomplete — knowing the commanded pose exactly does not remove physical tracking lag during FAST commanded motion (a rule-admitted gesture, a goto); deviation during such motion reads as external force and can re-trigger the #66 loop; TickContext.ownership provides the gate for free; seeded c22
  - seeds: `c22`
- `s19` — `challenge pass / lifecycle lens: boot ordering + unit stop path`: runtime Requires=/After= the daemon unit, but SDK client construction at engine start can still race daemon readiness (transient failure) — seeded c23 (lazy retry); the probe's exit-hang shows the held client also affects the STOP path (SIGTERM -> stop-timeout escalation risk without explicit close) — folded into c21's close requirement
  - seeds: `c23`, `c21`
- `s20` — `challenge pass / security+ops lens: intent spool as an external input surface + deploy rollback`: spool payloads dispatch through KindRegistry handlers that validate (unknown name/param/lifetime all produce error results, verified in intents.py) — the goto kind must match that bar (c24); deploy rollback for the live-test is reinstalling the prior PyPI version into the tool env (documented practice from the #70 flip); no new secrets, no network surface beyond localhost
  - seeds: `c24`, `c12`
