# Expressive sustained pat reaction (#82, completion of #70)

> Reachy Mini naturally settles into pettable moments, leans its head and body toward the hand while scratching continues, shows enjoyment, and clearly signals enough without inventing touches.
> instruction: Verify the shipped claim in one default-presence live session: Reachy offers a pettable still moment, leans head and body toward both labeled left and right contact, sustains only while credible scratching continues, performs the agreed enough response, releases cleanly, and remains ghost-free during the hands-off soak.

## Audience

- People physically petting Reachy Mini in its default presence, plus operators who need truthful no-ghost sensing and data-only reaction configuration.
  - instruction: Validate both the felt interaction and the operator observability/configuration path.

## Before → After

- Before: After PR #83 the runtime detects real pats only during commanded-still windows, emits a one-tick (touch_type, level) cue, and the deployed rule admits the fixed three-second direction-blind thoughtful head gesture; default feel-alive rarely offers a still window.
  - instruction: Verify against issue #82, PatSenseDriver, Sense, RuleEngine, the behavior library, and the operating guide.
- After: In the default symbolic runtime, Reachy deliberately becomes still often enough to invite touch; a real pat starts one sensor-driven interaction that leans toward the signed contact, sustains enjoyment only while contact remains credible, escalates to a bounded enough response, and then returns every motion channel to normal presence.
  - instruction: Implement this as one behavior-engine state machine over an explicit pat interaction snapshot, with deterministic timing, channel arbitration, release/cancel cleanup, and a live default-presence acceptance pass.

## Why it matters

- Detection alone is not the experience: a fixed, direction-blind three-second gesture can visibly contradict the hand that touched Reachy, while moving reactions can blind the very sense meant to sustain them. The fix must make the physical exchange feel contingent on the person without weakening the zero-ghost truth established by PR #83.
  - instruction: Keep product behavior and sensing truth coupled in acceptance criteria: direction, sustain, release, enough, pettable cadence, and hands-off zero-ghost evidence must pass together.

## Requirements

- The reaction receives signed touch direction and intensity from proprioception all the way through the runtime sense seam; left and right side scratches cannot collapse into the same event.
  - instruction: Prove with detector and runtime tests that opposite signed yaw deflections remain opposite reaction inputs while existing scratch/side_pat and level semantics remain compatible or are deliberately migrated.
  - honesty: Labelled equal-and-opposite yaw probes preserve sign through detector, driver, Sense, and behavior target; compatibility tests pin the existing touch_type and level surface or an explicitly approved migration.
- The runtime exposes continuous pat interaction state — recent contact, ongoing contact, escalation level, and release — because the current one-tick level1/level2 event cannot tell a behavior to keep holding or when to let go.
  - instruction: Specify a backward-compatible runtime representation and prove start, sustain, level escalation, gap release, and cooldown timing with deterministic tests.
  - honesty: Fake-clock tests prove one deterministic start, sustain across credible contact, level escalation, release after the agreed gap, and no stale ongoing state after release or suspension.
- A coherent petting interaction coordinates natural receptive stillness, directional head plus body lean, affectionate antenna motion, sustained hold, and an escalating enough withdrawal instead of independent fixed gestures.
  - instruction: Model and test the full settle -> receptive -> lean/hold -> enough/release lifecycle, including interruption and channel cleanup.
  - honesty: Engine composition tests and a live pass show one active behavior coordinates head, body_yaw, and antennas through the full lifecycle and releases all owned channels on release, enough, stop, and exception.
- The enough policy is bounded and observable: sustained contact progresses through enjoyment to a clear withdrawal or shake-off, releases every claimed channel, and cannot hold the robot indefinitely.
  - instruction: Define human-approved timing and escalation thresholds, then test timeout, release, re-entry cooldown, and active-set cleanup.
  - honesty: With a fake clock, uninterrupted contact reaches the human-approved enough threshold within a finite bound, releases every channel, observes cooldown, and cannot leave an active behavior indefinitely.
- Escalation is contact-gated, not elapsed-only: enjoyment and enough advance only while fresh press evidence remains inside the agreed interaction gap, so a single level1 event followed by silence can never become level2 or enough.
  - instruction: Change or wrap the detector state machine so fake-clock tests cover threshold-below-gap silence, repeated-contact escalation, release-before-threshold, and cooldown re-entry.
  - honesty: With level2 threshold below the interaction-gap timeout, a level1 event followed by silence releases without level2; only fresh repeated contact can advance to enjoyment and enough.
- The continuous pat contract distinguishes sampled no-contact from sensing blocked by commanded motion and from an unavailable or failed reader. Blocked or unavailable time never counts as fresh contact or advances escalation; a bounded unavailability grace exits the reaction safely without reporting a physical release that was not observed.
  - instruction: Represent availability explicitly in pat_state and fake-clock test no-contact, motion-blocked, reader-None, reader-raise, recovery, grace expiry, and no stale escalation across every edge.
  - honesty: Fake-clock traces distinguish an observed one-second no-press release from blocked and failed sampling; blocked or failed intervals never advance enjoyment or enough, recovery cannot reuse stale presses, and grace expiry releases all channels safely.
- The additive pat_state wire value is event-stable and bounded-rate: it carries state that changes only on meaningful contact, availability, direction, level, or lifecycle transitions, not a derived age that changes every 50 Hz tick. The existing pat field stays byte-for-byte compatible, and strict older consumers are either proven tolerant of the new key or protected by an explicit compatibility path.
  - instruction: Pin SenseSnapshotDriver emission counts under long holds, keep legacy pat serialization unchanged, round-trip pat_state transitions, and document or test the compatibility behavior for unknown fields.
  - honesty: A long unchanged hold produces no 50 Hz runtime-feed flood, each meaningful pat_state transition is emitted once, the legacy pat JSON remains unchanged, and the chosen unknown-field compatibility path has an executable test.
- Delivery includes an explicit operator migration from the box-local pat-to-thoughtful rule to the new sensor-driven reaction and an immediate reload rollback to the prior bounded rule. Repository tests and the live evidence bundle record both configurations, because code alone cannot change the deployed default reaction.
  - instruction: Document the exact rules.toml edit, validate it before reload, record the active rule and behavior in the feed, and prove one reload restores thoughtful without restarting the engine.
  - honesty: The candidate box-local rule validates and reloads to admit the new reaction on pat, while the recorded rollback rule validates and restores pat-to-thoughtful with one reload and no service restart.
- Signed direction is stable for one interaction: each credible yaw press preserves sign, a documented recency or dominance rule plus deadband prevents mixed or alternating presses from chattering the target across zero, and head plus body targets slew smoothly within the existing motion safety limits.
  - instruction: Test equal-and-opposite presses, same-side repetition, alternating signs, deadband inputs, target slew, and every head, body_yaw, and antenna limit through the behavior path, which currently bypasses goto validation.
  - honesty: Opposite labelled yaw signs yield opposite bounded targets, alternating or near-zero evidence does not chatter direction, and every streamed reaction contribution stays inside the documented head, antenna, and body_yaw limits with bounded per-tick slew.

## Honesty conditions

- A recorded live default-presence pass demonstrates every announced behavior for labelled left and right pats, and its hands-off soak produces neither pat cues nor reactions.
- All four shipped hardware replays retain zero ghosts, and reaction-command replays prove self-motion never creates contact; sensing reopens only during an explicitly constant safe hold and re-baselines at each motion edge.
- The rules schema remains data-only and contains no signed or changing event payload; opposite reaction directions arise from live Sense values inside the same admitted behavior.
- Timestamped runtime-feed evidence plus video-visible live outcomes cover both directions, sustain, release, enough, return to presence, and the agreed hands-off soak with zero ghost reaction.
- The implementation diff and release notes contain no arbitrary-motion pat inference, RMS or face provider, issue 78 transport, symbolic-runtime redesign, or second MotionQueue reaction owner.
- Runtime and composition tests show the new reaction never enqueues the legacy MotionQueue planner and only one engine path owns each claimed motion channel.
- The pre-change code and deployed configuration at commit 6eab58e reproduce a still-only one-tick tuple feeding a direction-blind fixed thoughtful reaction, while feel-alive movement keeps the stillness gate closed.
- Acceptance includes both a person petting Reachy in default presence and an operator inspecting the runtime feed and data-only rule, rather than unit tests alone.
- One default-presence integration trace covers pettable stillness through directional lean, sustained enjoyment, enough or release, and clean return to feel-alive without a ghost cue.
- The fix is accepted only when the physical reaction changes with labelled hand direction and contact duration while the PR 83 zero-ghost evidence remains green; a merely different fixed gesture does not qualify.
- A single evidence bundle contains labelled left/right input, signed sensed state, reaction targets, transition timestamps, all replay results, and the full hands-off soak interval; absence of any one item fails acceptance.
- Synthetic full-pose command tests show that movement on any head axis, body_yaw, or either antenna closes sensing before the actual-pose reader is consulted; the gate reopens only after the full vector is constant for 0.5 seconds, and reaction replays emit zero self-contact.
- The public schema and documentation name no force unit or calibrated strength field; tests prove level and contact freshness drive escalation independently of signed direction.
- The shipped announcement, operator guide, tests, and live evidence claim toward-the-hand direction only for labelled side pats and show scratch as a separate non-side-specific response.
- One uninterrupted timestamped interval of at least 180 seconds in default presence contains no pat event, no active pat_state contact, and no pat-reaction rule fire or admission.

## Success signals

- A live robot pass demonstrates left and right directional lean with head and body, holds while scratching continues, releases when contact ends, escalates to an unmistakable enough response at the agreed limit, and produces zero ghost reactions during a hands-off default-presence soak.
  - instruction: Record event feed and video-visible outcomes for both directions, release, enough, and a hands-off soak; also run the measured hardware replay suite.
- Acceptance passes only when 2 of 2 labelled side directions produce opposite, correct head-and-body lean; contact release begins within 1 second of the last fresh press; warning begins by 8 seconds and enough withdrawal by 12 seconds; every shipped hardware replay remains at zero ghosts; and the recorded hands-off live soak emits zero pat cues and zero reactions.
  - instruction: Gate delivery on timestamped feed plus video evidence for both labelled directions and timing thresholds, the complete hardware replay suite, and a duration-recorded hands-off live soak.
- The hands-off live acceptance soak runs for at least three uninterrupted minutes in default presence and records zero pat cues, zero active pat_state contact, and zero reaction admissions.
  - instruction: Record start and end timestamps, configuration, rule identity, and the complete runtime feed for a continuous duration of at least 180 seconds.

## Scope / boundaries

- Pat detection never guesses during arbitrary commanded motion: the measured stillness gate and zero-ghost guarantees from PR #83 remain load-bearing; any sensing allowed during a reaction is limited to an explicitly safe commanded hold and re-baselines on motion edges.
  - instruction: Keep the four hardware replays green and add reaction-ownership cases proving no self-trigger or post-motion ghost.
- The symbolic rule remains the static decision that a pat starts the reaction; signed direction and changing contact state are interpreted inside a sensor-driven behavior rather than copied into dynamic rule parameters.
  - instruction: Keep rules.toml data-only and show that the reaction behavior reads the live Sense without adding executable or per-event rule configuration.
- A sense-safe hold requires the complete commanded motion vector to remain constant: all six head axes, body_yaw, and both antennas. The existing pitch/yaw-only gate has no evidence that movement on omitted channels is ghost-free; any narrower exception requires labelled hardware proof.
  - instruction: Gate on changes across the full composed pose, clear press pairing and re-seed conditioning at every blocked-to-safe edge, and test fixed pitch/yaw with z, roll, body_yaw, or antenna motion independently.

## Non-goals

- This fix does not reopen the completed symbolic-runtime architecture, infer pats during arbitrary feel-alive wander, add rms or face providers, implement issue #78 export transport, or preserve the old MotionQueue PatReaction implementation as a second runtime.
  - instruction: Limit implementation to pat sensing state/direction, the behavior library and engine composition needed for petting, focused tests, and operator documentation.

## Assumptions

- The older reachy/motion/pat_reaction.py lean/nuzzle/settle poses are useful motion prior art, but the 50 Hz behavior library should port their intent rather than enqueue MotionQueue actions beside the engine.
  - instruction: Compare the old planner poses and tests with the new behavior contribution contract; keep one live motion owner.

## Scope exploration

- `s1` — `GitHub issues #70, #74, #75, #79, #80, #82 and merged PRs #81/#83`: The symbolic runtime and pat provider are delivered; #82 is the remaining product fix. PR #83 proves sensing is honest only under commanded stillness, while issue #82 records direction, sustain, enjoyment, enough, and default-presence pettable windows.
  - seeds: `c10`
- `s2` — `reachy/motion/pat.py PatDetector`: The detector records only pitch-versus-yaw press axes. Yaw uses abs(deviation), then _classify_touch counts axes and discards sign; output is only (level, touch_type). It has internal recent-press/gap/level state that is not exposed continuously.
  - seeds: `c12`
- `s3` — `reachy/behavior/pat_sense.py PatSenseDriver`: The driver high-passes signed pitch/yaw but converts detector output into a one-tick (touch_type, level) latch. It returns before sensing whenever any non-base behavior owns head and independently gates on 0.5 s commanded stillness, so a new wants_sense reaction would still be blind unless ownership policy gains an explicit safe-hold design.
  - seeds: `c5`
- `s4` — `reachy/behavior/sense.py and reachy/export/runtime.py`: Sense.pat_event and the runtime wire contract are fixed as a two-string tuple/list [kind, level]. Adding signed direction or continuous interaction state touches provider types, snapshot equality/change emission, export serialization, schema docs, and compatibility tests.
  - seeds: `c7`
- `s5` — `reachy/behavior/model.py, engine.py, and library.py`: The engine already supports fresh stateful wants_sense behavior closures, per-tick abstention, and atomic head/antennas/body_yaw arbitration, but no built-in behavior uses live Sense. A pet reaction can fit this seam only if its sensing remains available while it owns a controlled hold.
  - seeds: `c12`
- `s6` — `reachy/behavior/rule_engine.py and rules.py`: Rules are data-only, evaluate pat presence, and admit library behaviors with static params. RuleEngine does not pass the triggering event into params and suppresses re-admission while a behavior name is active; dynamic direction therefore belongs in the live behavior sense contract.
  - seeds: `c8`
- `s7` — `reachy/motion/pat_reaction.py and tests/test_pat_reaction.py`: The older queue planner provides lean/nuzzle/settle pose prior art including body_yaw and antennas, but side_pat always assumes one positive yaw direction, scratch has no body yaw, and the fixed three-action sequence cannot sustain or react to release. It must not run beside the 50 Hz engine.
  - seeds: `c12`
- `s8` — `reachy/behavior/library.py feel-alive base layer and runtime composition in reachy/cli/_commands/behavior.py`: Default feel-alive continuously changes commanded head pose and owns all three channels, so the stillness gate rarely opens. Pettable moments and the reaction lifecycle must coordinate with base presence without globally disabling aliveness or creating a second motion owner.
  - seeds: `c10`
- `s9` — `tests/data/pat_*.csv and tests/test_behavior_pat_sense_hardware.py`: Four real-robot replays pin zero ghosts under wander and repeated detections while still. The petting replay was recorded from many directions but has no directional labels, so it cannot prove signed left/right classification or reaction correctness without a new labelled probe.
  - seeds: `c7`
- `s10` — `docs/operating-reachy.md and deployed pat-acknowledge rule described in issue #82`: Operator docs currently promise a static pat -> thoughtful rule, manual still moments, and ownership suspension. The final fix must update those truths and retain observable rules/feed behavior; deployment configuration itself is box-local and not a repository implementation surface.
  - seeds: `c10`
- `s11` — `.claude/skills Devague chain plus colleague learn/ask-colleague`: The seven-leg Devague workflow and ask-colleague source already existed for Claude. Codex-native .agents adapters were added for scope, think, challenge, spec-to-plan, assign-to-workforce, deviate, summarize-delivery, and ask-colleague so the same canonical methods can drive this frame without duplicated skill logic.
  - seeds: `c8`
- `s12` — `GitHub issues and merged PR provenance (supplement to s1)`: The issue and PR survey also seeded the interaction lifecycle, stillness boundary, live success, and non-goal claims recorded beside c10.
  - seeds: `c4`, `c5`, `c7`, `c8`, `c10`
- `s13` — `PatDetector provenance links (supplement to s2)`: The discarded sign and transition-only output also seed the signed-direction and continuous-interaction requirements recorded beside the enough requirement.
  - seeds: `c2`, `c3`, `c12`
- `s14` — `PatSenseDriver provenance links (supplement to s3)`: The signed internal signal, one-tick latch, ownership suspension, and stillness gate seed direction, continuity, lifecycle, and no-ghost boundary claims.
  - seeds: `c2`, `c3`, `c4`, `c5`
- `s15` — `Sense and runtime export provenance links (supplement to s4)`: The fixed tuple/list contract seeds signed direction, continuous state, and observable live-success claims.
  - seeds: `c2`, `c3`, `c7`
- `s16` — `Behavior model, engine, and library provenance links (supplement to s5)`: Stateful wants_sense closures and three-channel arbitration seed the interaction lifecycle, stillness/ownership boundary, data-only rule boundary, and bounded enough requirement.
  - seeds: `c4`, `c5`, `c6`, `c12`
- `s17` — `Rule engine provenance links (supplement to s6)`: Static rule parameters and live behavior sensing seed both the data-only rule boundary and the non-goal boundary.
  - seeds: `c6`, `c8`
- `s18` — `Legacy PatReaction provenance links (supplement to s7)`: The fixed queue planner seeds the interaction lifecycle, port-not-reuse assumption, and bounded enough requirement.
  - seeds: `c4`, `c9`, `c12`
- `s19` — `Feel-alive and runtime composition provenance links (supplement to s8)`: Continuous default motion seeds the interaction lifecycle, stillness boundary, live-success criterion, and current before-state.
  - seeds: `c4`, `c5`, `c7`, `c10`
- `s20` — `Hardware replay provenance links (supplement to s9)`: The measured recordings seed signed-direction evidence needs, the no-ghost boundary, and the live-success criterion.
  - seeds: `c2`, `c5`, `c7`
- `s21` — `Operating guide and deployed rule provenance links (supplement to s10)`: Current operator promises seed the data-only rule boundary, live-success criterion, non-goal boundary, and before-state.
  - seeds: `c6`, `c7`, `c8`, `c10`
- `s22` — `.claude/skills/communicate, .agents/skills/communicate, and Culture #general`: The canonical communicate workflow was added as a thin Codex adapter. Its mesh route was used once to ask spark-colleague to fan out the read-only #82 investigation into detector/sense, engine/ownership, and legacy/tests/export lanes, then synthesize and finish; no duplicate GitHub handoff was posted.
- `s23` — `.colleague resume/flight dogfood and agentculture/colleague#354`: The independent review did not produce a finished report: two continuation attempts were stopped after oversized-context/backpressure stalls, no subagents ran, and no Colleague claim was accepted into the product scope. The verified harness behavior and failure evidence were filed as agentculture/colleague#354; the local #82 scope remains grounded in direct repository and hardware-replay evidence.
- `s24` — `reachy/motion/pat.py _advance_level1 and issue #82 level2 premise`: The existing level2 ladder is not sufficient as an honest enough signal: after level1 it advances when elapsed exceeds a random 4-8 s threshold, while silence resets only after a 5 s last-press gap. A 4.x s threshold can therefore emit level2 without any post-level1 press. The fix needs contact-recency gating or a separate interaction state before level2/ enough can mean sustained scratching.
  - seeds: `c3`, `c12`
- `s25` — `challenge pass / adjacent-systems lens: pat_sense.py, sense.py, and export/runtime.py`: The same new state crosses detector cadence, provider equality, runtime serialization, and external feed consumers. None distinguishes blocked sampling from observed no-contact today, and per-tick age fields would defeat change-only export.
  - seeds: `c23`, `c24`
- `s26` — `challenge pass / unstated-assumptions and counter-evidence lens: issue 82 plus claims c1, c2, and c20`: Issue 82 requests side direction and duration-dependent enjoyment but no calibrated force magnitude; front/back is only ideal and the labelled evidence is absent. This seeded explicit intensity and directional-claim decisions.
  - seeds: `c27`, `c28`
- `s27` — `challenge pass / overlooked lifecycle and failure-mode lens: PatSenseDriver gate, reader degradation, and reaction lifecycle`: Reader None, reader failure, ownership blocking, commanded-motion blocking, and true absence all collapse to no event today. A sustained reaction needs availability separate from physical release and a bounded safe exit.
  - seeds: `c23`
- `s28` — `challenge pass / security, migration, concurrency, operations, and reversibility lens: box-local rules.toml plus reload path`: No credential, user-data, destructive migration, or new concurrent writer surface appears. The operational gap is that the repository cannot replace the deployed pat-to-thoughtful rule; activation and rollback are explicit box-local reload steps.
  - seeds: `c25`
- `s29` — `challenge pass / observability, containment, rollback, and recovery lens: SenseSnapshotDriver and runtime rule events`: Frozen Sense equality is the feed dedupe boundary, so continuously derived ages would flood at 50 Hz. Stable pat_state transitions, explicit availability, rule identity, and a reload rollback provide bounded observability and recovery.
  - seeds: `c23`, `c24`, `c25`
- `s30` — `challenge pass / cheap-probe lens: four hardware CSV replays and hardware replay harness`: The recordings retain actual roll, x, y, and z but only commanded pitch and yaw; they do not isolate z, roll, body_yaw, or antenna commands, and natural petting is unlabelled. The conservative full-pose gate and new labelled live probe avoid treating missing evidence as safety.
  - seeds: `c22`
- `s31` — `challenge pass / hardware safety lens: engine streaming path and goto_intent limits`: The engine streams library contributions directly with no common clamp, while the documented axis limits are enforced only for goto intents. The new sensor-driven behavior must enforce bounded targets and slew in its own path.
  - seeds: `c26`
- `s32` — `challenge pass / hidden-state and restart lens: feel-alive library entry and LibraryEntry.make_fn`: Current feel-alive is a pure time function; deterministic 8-12 second jitter needs fresh per-instance state through make_fn or an equivalent injected deterministic sequence. Restart seed identity is not specified, but smooth per-instance boundaries are testable.
  - seeds: `c18`

## Decisions

- The pat sense becomes commanded-state gated rather than owner-name gated: sensing is closed while the head command moves, every motion or ownership edge clears press pairing and re-seeds conditioning, and any owner may be sensed after the existing 0.5-second constant-command hold. No behavior-name allowlist weakens or duplicates this rule.
  - instruction: Reorder or replace PatSenseDriver's blanket non-base-owner return so the stillness gate is authoritative; add non-base moving/settled and reaction self-trigger replay tests before live use.
- The default contact ladder releases after 1 second without a fresh press, reads as enjoyment at 4 seconds of contact-gated interaction, begins an obvious warning at 8 seconds, withdraws or shakes off at 12 seconds, then enforces a 5-second cooldown; a full release resets the ladder.
  - instruction: Define named timing constants and fake-clock boundary tests at just-before/at/just-after every transition, including silence after level1 and cooldown re-entry.
- Default feel-alive moves for a deterministic-jittered 8-12 seconds, settles smoothly, and then holds its head command constant for 4 seconds so the 0.5-second gate leaves a real pettable window. No separate attention-cue shortcut ships in this fix.
  - instruction: Make the cadence deterministic under an injected seed/clock; test maximum time-to-window, exact constant-command duration, smooth boundaries, and unchanged passive arbitration.
- The existing runtime-feed pat value remains [touch_type, level]. Continuous signed interaction state is added in parallel as pat_state and documented as an additive schema extension rather than replacing or reordering the legacy tuple.
  - instruction: Keep existing pat serialization tests byte-for-byte for that field, add pat_state start/sustain/release serialization tests, and document compatibility behavior for consumers that ignore unknown keys.
- The sensing contract exposes robot-frame signed yaw rather than guessed left/right labels. A labelled live probe is the first implementation-plan gate that pins signed yaw to the physical hand and reaction target; left/right distinction is required, while front/back remains follow-up unless hardware evidence proves it.
  - instruction: Record equal-and-opposite labelled side scratches with commanded and actual pose, pin the sign mapping in fixtures/tests, and refuse to name physical left/right in code before that evidence exists.
- For issue 82, intensity means the existing discrete contact and escalation level together with contact recency; this delivery does not claim a calibrated continuous force magnitude. Robot-frame signed yaw is the separate directional value.
  - instruction: Define pat_state so level and contact freshness are explicit, preserve the conditioned deviation only as detector evidence, and do not expose an uncalibrated force-strength promise.
- Directional acceptance and announcement are limited to labelled left and right side pats in this delivery. A scratch or top contact receives a non-side-specific pitch response; no front or back toward-the-hand claim ships without labelled hardware evidence.
  - instruction: Narrow the announcement and live matrix to side direction plus a distinct scratch response, while retaining front/back as the existing follow-up.
- During active scratching, Reachy moves its head and body into the signed contact pose, may reorient its antennas into a receptive pose, and then holds that chosen pose constant so sensing can resume. Once contentment is reached, the exact ending instant may be deterministically jittered after the warning begins but remains bounded by the existing ladder: warning starts by 8 seconds and, no later than 12 seconds, Reachy closes sensing and performs one coordinated head-and-body wiggle plus antenna reorientation that unmistakably means scratch time is done, then releases and enters cooldown.
  - instruction: Implement entry as a smooth move followed by a full-pose sensing hold; implement the done signal as one bounded state transition after sensing closes, with deterministic injected jitter, safe target limits, complete channel cleanup, and runtime-feed phase timestamps.

## Resolved hard questions

- The generic toward-the-hand announcement is narrowed by c28: directional acceptance covers labelled left and right side pats; scratch uses a distinct non-side-specific pitch response, and front/back remains follow-up work.
- Intensity is resolved by c27 as the existing discrete contact/escalation level plus contact recency. This delivery does not expose an uncalibrated continuous force magnitude.
- The reaction can keep sensing under c16, c22, and c30 only after it reaches a constant safe hold: owner identity no longer grants permission, the full composed pose must remain constant for 0.5 seconds, and every motion or ownership edge clears stale presses and re-seeds conditioning.
- The head-only wording in c16 is conservatively strengthened by c22: head, body_yaw, and antennas all hold while sensing unless a labelled hardware probe proves a narrower safe exception.

## Resolved vagueness

- [unknown_blocking] The shipped real-robot recordings contain natural petting from many directions but no direction labels; signed-direction thresholds and left/right semantics need a labelled replay or a focused live probe before the spec can honestly lock the event contract. — resolved: The spec contract exposes robot-frame signed yaw, not guessed semantic left/right. Plan task zero performs a labelled live probe and pins sign-to-hand and sign-to-target mapping before implementation constants; left/right distinction remains required, front/back does not block (c20).
