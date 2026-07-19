# Build Plan — Expressive sustained pat reaction (#82, completion of #70)

slug: `expressive-sustained-pat-reaction-82-completion-of` · status: `exported` · from frame: `expressive-sustained-pat-reaction-82-completion-of`

> Reachy Mini naturally settles into pettable moments, leans its head and body toward the hand while scratching continues, shows enjoyment, and clearly signals enough without inventing touches.

## Tasks

### t1 — Run the labelled side-scratch and reacquisition hardware gate

- instruction: Use a disposable live probe rather than shipping diagnostic code; commit only labelled evidence fixtures and their replay test, with labels chosen before looking at signal sign.
- acceptance:
  - Two labelled equal-and-opposite side-scratch recordings contain monotonic timestamps, commanded and actual pose, an unambiguous physical hand label, and enough press edges to measure robot-frame sign plus natural inter-press cadence.
  - A replay check proves the conditioned yaw signs are opposite, pins physical-hand to robot-frame to reaction-target mapping, and records whether a bounded entry move plus the 0.5-second safe hold can reacquire sensing inside the 1-second release budget; failure stops implementation for a spec decision.
  - No physical left or right name, reaction sign constant, or reacquisition timing constant is implemented before this gate passes.

### t2 — Preserve signed contact-gated press evidence in PatDetector

- instruction: Change reachy/motion/pat.py and focused detector tests first; retain legacy scratch, side_pat, level1, and level2 event compatibility for listen, sleep, pat CLI, and runtime callers.
- depends on: t1
- covers: c15, h15
- acceptance:
  - PatDetector keeps every existing update return tuple and current caller compatible while exposing signed robot-frame yaw evidence, touch type, level, and fresh-press time through a separate deterministic snapshot seam.
  - Equal-and-opposite fixtures preserve opposite signs; same-side repetition is stable; alternating or deadband inputs follow one documented recency or dominance rule without naming physical left or right inside the detector.
  - With a fake clock, one level1 followed by silence cannot become level2 even when its threshold is below the gap, while repeated fresh presses can escalate and cooldown or full release clears stale evidence.

### t3 — Define the event-stable PatState and provider contract

- instruction: Keep reachy/behavior/sense.py a lightweight dependency leaf; define wire-friendly enums or literals there and do not import numpy, the SDK, or PatDetector.
- depends on: t1
- acceptance:
  - A frozen PatState value represents availability, contact presence, touch type, discrete level, signed robot-frame yaw, lifecycle phase, and transition or last-press anchors without any derived age field that changes every tick.
  - Sense and SenseProviders carry pat_state with safe defaults; a missing or raising state provider reports unavailable rather than observed no-contact; pat_event remains the same optional two-string tuple.
  - Existing DoA-only and pat_event-only Sense construction stays source-compatible and focused tests pin equality and peek semantics.

### t4 — Make PatSenseDriver full-pose gated and publish the persistent interaction state

- instruction: Make commanded state authoritative instead of owner name. Keep one persistent interaction clock in PatSenseDriver so behavior instance completion cannot erase cooldown or contact history.
- depends on: t2, t3
- covers: c3, h3, c5, h5, c22, h17
- acceptance:
  - Movement on any of six head axes, body_yaw, or either antenna closes sensing before the actual-pose reader is called; every command or ownership edge clears press pairing and re-seeds filters once, and any owner becomes sense-safe only after the complete pose is constant for 0.5 seconds.
  - A fake-clock state trace covers start, fresh-contact sustain, enjoyment at 4 seconds, warning by 8 seconds, a deterministically injected enough deadline no later than 12 seconds, observed release after 1 second without a fresh press, and a 5-second enough cooldown with no stale state after suspension.
  - Blocked and unavailable samples never count as fresh contact or advance escalation; recovery cannot pair presses across a gap; the one-tick legacy pat_event latch remains byte-compatible.
  - All four shipped hardware replays retain zero ghosts and still-petting detections, while full-pose and reaction-command replays prove zero self-contact and exactly one rebaseline per blocked-to-safe edge.

### t5 — Export pat_state additively without runtime-feed churn

- instruction: Update reachy/export/runtime.py and focused export tests; do not add issue-78 transport or reTerminal-specific code.
- depends on: t3
- covers: c24, h19
- acceptance:
  - Legacy sense serialization preserves the existing pat list exactly; pat_state serializes as a parallel documented object and old raw sense shapes still parse without raising.
  - A long stable hold emits one baseline or meaningful transition rather than 50 events per second; fresh presses and availability, direction, level, or phase transitions each emit a bounded observable update.
  - The test suite exercises the selected compatibility path for existing consumers and malformed or unknown fields remain failure-isolated.

### t6 — Add generic sensor-driven behavior self-completion

- instruction: Make the smallest generic change in behavior/model.py and engine.py, with a dedicated completion test file; do not teach the engine about pats or petting phases.
- depends on: t1
- acceptance:
  - A behavior contribution can explicitly mark itself complete; the engine removes it before same-tick arbitration, reports the completion, and releases every claimed channel so the passive base can own them immediately.
  - An abstaining contribution remains active, preserving existing behavior, while a completed behavior disappears from active_names so a later rule admission is not suppressed.
  - All existing lifetime expiry, stop, arbitration, and pure behavior tests remain unchanged, and the completion seam adds no executable rule data or reaction-specific branch to the engine.

### t7 — Build deterministic pettable windows for feel-alive

- instruction: Implement the stateful generator in a new reachy/behavior/feel_alive.py module and focused tests so it can be built in parallel with detector, sense, and engine completion work.
- depends on: t1
- acceptance:
  - A fresh per-instance generator moves for an injected deterministic-jittered 8 to 12 seconds, settles with continuous bounded slew, and then keeps the complete head, body_yaw, and antenna command vector exactly constant for 4 seconds.
  - At least 3.5 seconds of each hold remains sense-eligible after the 0.5-second gate, maximum time to a pettable window is bounded, and passive arbitration plus the energy parameter retain their existing meaning.
  - Fake seeds and clocks reproduce cadence and smooth boundaries; production seed choice is isolated behind the factory and no cross-process repeatability promise leaks into the public contract.

### t8 — Build the dog-like pet reaction state machine and bounded motion

- instruction: Implement in a new reachy/behavior/pet_reaction.py with an injected jitter seam and focused fake-clock tests. Port motion intent from legacy pat_reaction.py but never import or enqueue MotionQueue.
- depends on: t2, t3, t6
- covers: c12, h12, c23, h18, c26, h21
- acceptance:
  - A side pat smoothly moves head and body toward the signed robot-frame target, a scratch uses a distinct non-side-specific pitch pose, antennas enter a receptive pose, and the complete contact pose then holds exactly constant for sensing.
  - The behavior maps PatState phases to receptive, contentment, warning, observed release, and one coordinated done gesture; at enough it closes sensing, wiggles head and body, reorients antennas, marks itself complete, and never outlives a finite safety backstop.
  - Observed release starts within 1 second of the last fresh press; blocked or unavailable sensing never escalates and its bounded grace completes safely without claiming physical release; enough completion observes the persistent 5-second cooldown.
  - Opposite signs produce opposite bounded head and body targets; alternating and deadband evidence cannot chatter; every axis stays inside documented limits with bounded per-tick slew; stop, exception, release, and enough free all three channels.

### t9 — Register the new stateful presence generators in the behavior library

- instruction: Keep reachy/behavior/library.py as registry and parameter glue over the two new modules; add a focused library-registration test rather than expanding unrelated behavior tests.
- depends on: t7, t8
- acceptance:
  - feel-alive uses a fresh make_fn instance with its existing public name and parameters, and pet-reaction is a fresh wants_sense behavior claiming head, antennas, and body_yaw with stoppable arbitration and a finite safety-backstop lifetime.
  - Two built instances share no timing or interaction state; behavior list, parameter validation, base seeding, and existing library entries remain compatible.
  - The library imports neither MotionQueue nor the legacy PatReaction, and no second motion owner is created.

### t10 — Compose pat_state, static rule admission, self-completion, and runtime evidence end to end

- instruction: Limit shipping composition changes to reachy/cli/_commands/behavior.py and focused runtime integration tests; keep rules declarative and the engine pat-agnostic.
- depends on: t4, t5, t9
- covers: c2, h2, c4, h4, c6, h6, c13, h13
- acceptance:
  - Runtime composition wires both pat_event and pat_state from the one held reader; a data-only pat is_true rule admits pet-reaction with no signed or changing rule parameter.
  - Offline engine traces for the two labelled sides preserve opposite sign through detector, driver, Sense, behavior target, composed head and body pose, and feed while the legacy pat tuple remains unchanged.
  - One active reaction coordinates all three channels through entry, safe hold, contentment, warning, release or enough, self-completes cleanly, and returns ownership to feel-alive with no legacy MotionQueue call.
  - Moving-owner, settled-owner, reader failure, stop, behavior exception, and cooldown re-entry traces leave no stale active name, channel owner, pat_state contact, or self-triggered event.

### t11 — Document and test activation, rollback, compatibility, and operator truth

- instruction: Keep box-local configuration out of repository defaults: ship validated fixtures and exact operator steps, then record the applied live config in delivery evidence.
- depends on: t9
- covers: c10, h10, c25, h20
- acceptance:
  - A repository fixture for the candidate box-local pat-to-pet-reaction rule and the prior pat-to-thoughtful rollback both validate; one behavior reload switches either direction without an engine restart.
  - The operating guide and explain catalog describe pettable cadence, signed side-only direction, non-directional scratch, level plus recency intensity, full-pose safe holds, unavailable degradation, pat_state compatibility, done gesture, finite bounds, and the exact migration and rollback commands.
  - Before-state evidence still reproduces the commit-6eab58e thoughtful reaction and continuously moving feel-alive gate, while release notes claim no front or back direction, arbitrary-motion sensing, RMS or face work, issue-78 transport, or second MotionQueue owner.

### t12 — Run the labelled default-presence acceptance and assemble one evidence bundle

- instruction: This is a hardware and operator gate, not a unit-test substitute. Stop on wrong sign, missed timing, self-motion contact, incomplete cleanup, or any soak ghost; do not tune away a failure without recording a plan deviation.
- depends on: t10, t11
- covers: c1, h1, c7, h7, c11, h11, c14, h14, c21, h16, c29, h24
- acceptance:
  - In one default-presence session, 2 of 2 pre-labelled side directions produce opposite correct head and body leans, scratch produces its distinct pose, continued contact holds the chosen pose, observed release begins within 1 second, warning begins by 8 seconds, and the coordinated done wiggle occurs no later than 12 seconds.
  - The runtime feed and video-visible result show pettable settle, signed state, targets, contact sustain, contentment, warning, release or enough, antenna reorientation, self-completion, 5-second cooldown, and clean return to feel-alive for both the person and the inspecting operator.
  - All four shipped replays and reaction-command replays remain ghost-free, followed by one uninterrupted default-presence hands-off soak of at least 180 seconds with zero pat events, zero active pat_state contact, and zero reaction fires or admissions.
  - One dated evidence bundle contains fixture labels, applied and rollback rules, transition timestamps, complete feed interval, replay commands and results, and the full soak start and end; any missing item fails delivery.

## Risks

- [unknown_nonblocking] Physical hand-to-robot yaw sign, natural inter-press cadence, and move-plus-hold reacquisition margin are not known until the labelled live probe; t1 is an execution gate and every dependent task stops if its evidence cannot satisfy the one-second release budget. (task t1)
- [unknown_nonblocking] Existing recordings do not isolate body_yaw or antenna coupling into sensed head axes; the plan contains this by requiring the full composed pose to hold, and no narrower gate ships without separate labelled evidence. (task t4)
- [unknown_nonblocking] Strict external runtime-feed consumers are not inventoried in this repository; t5 must document and test the selected additive compatibility path, and delivery records any known consumer validation instead of assuming tolerance. (task t5)
- [unknown_nonblocking] Production feel-alive seed identity may differ across restarts; cadence bounds and smoothness are contractual, while cross-process repetition is not, so the generator keeps an injected deterministic seam for tests. (task t7)
- [follow_up] Front-versus-back scratch direction remains unproven and is excluded from names, acceptance, and shipped claims; a future labelled pitch-direction probe may extend the side-only contract.
