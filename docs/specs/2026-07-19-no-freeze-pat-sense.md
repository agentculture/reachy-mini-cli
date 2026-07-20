# no-freeze pat sense

> Reachy Mini feels your hand while it is still moving — the freeze is gone

## Audience

- The person reaching out to pet Reachy Mini, and the operator running the boot presence who has to trust what the runtime reports about contact
  - instruction: Verify with one live session plus the runtime feed: a bystander sees no dead-still pause, and pat_state still distinguishes real contact for the operator

## Before → After

- Before: feel-alive moves for a jittered 8-12 s then holds the complete commanded pose EXACTLY constant for 4 s (HOLD_S), and pat_sense only senses inside that hold after a further 0.5 s gate — so the robot visibly stops to be pettable, and pet_reaction's own entry move re-closes the gate ~0.6 s into contact
  - instruction: Baseline to compare against: git show the pre-change feel_alive.py constants (HOLD_S=4.0, SETTLE_S=1.0, MOVE 8-12 s) and pat_sense still_hold_s=0.5
- After: Idle motion is continuously gentle and predictable with no dead-still hold; a hand is felt DURING that motion, contact sustains through the reaction's own lean, and the robot never freezes to become pettable
  - instruction: Acceptance is a single live session recorded to a runtime feed, showing continuous motion, contact sustained past 4.0 s, and a clean return to idle

## Why it matters

- The freeze is both an expressive defect and a functional one: the operator reports it reads as unnatural, and t12 measured it as the direct cause of the sustain failure — contact never exceeded 0.82 s against a 4.00 s contentment threshold, so the enjoyment ladder the feature exists for is unreachable
  - instruction: Cite the t12 findings doc (max contact 0.82 s vs CONTENTMENT_AFTER_S 4.00 s) as the functional half; the operator report is the expressive half

## Requirements

- still_hold_s is not reachable by any operator surface: behavior.py:838 constructs PatSenseDriver(reader=reader.read) with all defaults, and the only pat env knob is REACHY_PAT_SENSE (full on/off), so the experiment needs a flag or env before it can be run on the box
  - honesty: Verifiable by reading behavior.py:838 and grepping the pat env surface: if any flag or env already reaches still_hold_s, this claim is false and the plan drops its wiring task
- The visible freeze and the sensing gate are TWO independent levers: what the operator sees is feel_alive.py HOLD_S=4.0 plus SETTLE_S=1.0 (a dead-still hold every MOVE_MIN_S=8 to MOVE_MAX_S=12 seconds), while what blocks sensing is pat_sense.py still_hold_s=0.5; removing one does not remove the other
  - honesty: The two levers are independently testable: changing feel_alive's HOLD_S alone must leave pat_sense's gating behaviour byte-identical in replay, and vice versa
- The blind window is arithmetic, not luck: still_eps=0.01 deg means pet_reaction's HEAD_ROTATION_STEP_DEG=0.5 per tick is 50x the tolerance and shuts the gate on the reaction's FIRST tick; blind time then equals entry slew (side yaw ~2.25 deg = 0.09 s; SCRATCH_PITCH_DEG=8.0 = 0.32 s) plus still_hold_s=0.5 s, giving ~0.6-0.8 s against a RELEASE_AFTER_S=1.0 s budget
  - honesty: The arithmetic predicts the measurement: entry slew plus still_hold_s should equal the t12-observed ~0.6 s re-close, and a replay driving pet_reaction's own slew through the gate must reproduce it
- The no-freeze design ALREADY SHIPS elsewhere: no pat consumer outside the behavior engine uses a stillness gate. listen_pat.py PatHook senses every tick INCLUDING mid-move by feeding detector.update an expected pose interpolated start-to-target along minjerk (listen_pat.py:333-348, 374), so per its docstring 'clean transit reads ~0 deviation and a hand reads as external force mid-move'. sleep/patwake.py PatWakeSource (118 lines, zero suppression logic) does the same against the moving sleep-breathe commanded pose. pat_sense.py is the outlier that chose a gate instead
  - honesty: Confirmed by running listen's PatHook and sleep's PatWakeSource against a moving-pose fixture and observing detections with no stillness gate present in either path
- Expectation tracking is not free: PatHook pays for mid-move sensing with FIRMER thresholds (LIVE_PRESS_THRESHOLD_DEG=2.5, LIVE_YAW_PRESS_THRESHOLD_DEG=6.0) versus pat_sense's 0.5/0.2, and with the OPPOSITE rebaseline policy - it calls clear_presses() and deliberately KEEPS the EMA baselines because 'wiping them made the sag read as a fresh press until re-learned, which was the phantom chains true fuel' (listen_pat.py:379-385), where pat_sense reseeds them
  - honesty: The cost is quantified before adoption: if pat_sense moves to expected-pose tracking, the press threshold it needs is measured against the new fixtures rather than copied from PatHook's 2.5 deg
- Expectation tracking was already tried against THIS motion and failed: the #79 lineage (docs/deliveries/2026-07-18-runtime-senses-75-78.md:49-68) records four iterations - lag compensation at tau 0.3 s after measuring plant lag 0.28 s, baseline retention plus warmup, a deviation high-pass after finding the plant overshoots at gain 1.1-1.2x, and a fitted 40-tap FIR plant model that removed only 1.1x of the residual - leaving a 3.3 deg conditioned yaw tail, 'above any threshold a genuine pat could still clear'
  - honesty: The prior-art record is accurate: the four #79 iterations and the 1.1x FIR result are re-readable in the cited delivery doc, so this frame does not re-run an approach already falsified
- Why PatHook succeeds where pat_sense failed is a MOTION difference, not a sensing one: listen's head moves in discrete minjerk gotos whose transit is exactly predictable start-to-target, while feel-alive wanders continuously; PatHook also runs a 5x firmer press threshold (2.5 deg vs 0.5). The #80 numbers say wander residual p99 is 3.3-4.0 deg untouched versus petting p90 2.4-3.1 deg - so raising pat_sense to PatHook's 2.5 deg would still sit inside the wander noise, and the t12 live side signal was only 0.55-0.96 deg
  - honesty: The motion-difference explanation is falsifiable: if a gentle profile is found where pat_sense separates at its own threshold, the explanation holds; if no profile separates at any threshold, the difference was not motion predictability and the direction is wrong
- The most promising no-freeze direction therefore changes the MOTION, not the sensing: make feel-alive's idle predictable enough that expectation tracking works (slower, minjerk-shaped, lower amplitude, or briefly damped on contact) rather than dead-still. pat_sense.py:239 states the current position as 'stillness is a PRECONDITION for the sense, not a tuning knob' - that precondition is about the PLANT being quiet, and a gentle predictable motion may satisfy it where a fast wander does not
  - honesty: The motion-not-sensing direction is testable before it is built: a candidate gentle profile can be replayed offline against a labelled pair and rejected if it does not separate, so the direction can fail cheaply rather than only on hardware
- Threshold-tuning the freeze away is FALSIFIED on the shipped recordings. Replaying all four with still_hold_s=0.0 and sweeping press_threshold gives no operating point where all four columns are correct: at 0.5-2.5 deg the still-petted case detects (7,7,4 events) but untouched WANDER fires ghosts (6,3,1); at 4.0 deg untouched wander goes clean (0) but the STILL-petted recording drops to ZERO detections despite 50 s of continuous petting. A threshold that cannot see a real pat on a quiet head cannot be credited with the 4 events it reports on the wander recording - those are almost certainly plant residual
  - honesty: The falsification is reproducible: the replay sweep over the four committed fixtures can be re-run by anyone from the repo and yields the same table, so the claim rests on committed data rather than on one session's output
- The direction is to change the motion rather than the sensing: replace feel-alive's dead-still 4 s hold with gentle predictable motion the pat sense can see through, so the robot never visibly freezes
  - instruction: Replace feel_alive's dead-still HOLD_S segment with a continuously-moving gentle profile; keep the cadence generator's public factory and params, change only the pose trajectory it emits
  - honesty: A gentle-motion operating point EXISTS: there is at least one idle amplitude/velocity profile where an offline replay of a labelled petted-vs-untouched recording pair separates cleanly at a single press threshold — zero events untouched, repeated events petted. Until such a pair is recorded this is unproven, and the four shipped recordings cannot answer it because none captures a gently-moving head

## Honesty conditions

- The announcement is literally true on hardware: the robot is observed never holding a dead-still pose to become pettable, AND a hand is felt while it is moving — both confirmed in one live session, not inferred from an offline replay
- Ghost-freedom is re-established as a MEASURED gate, not an assumption: the change ships with a replay test over a new untouched gentle-motion recording asserting zero events, plus a live hands-off soak of at least 180 s at zero events, before the freeze is removed from the default presence
- The two tests are rewritten deliberately and visibly in this change, with their new intent stated, rather than deleted or silently relaxed — a reviewer can see the promise change
- The operator-facing docs are rewritten in the SAME change that removes the freeze: docs/operating-reachy.md's 'does not infer contact during arbitrary motion' and the 3.5 s pettability arithmetic are replaced, so no shipped promise outlives the behavior it described
- Both audiences are actually served: the person petting gets a robot that does not stop to be touched, and the operator can still tell from pat_state and the runtime feed whether contact is real — neither is traded for the other
- The before-state is measured, not remembered: HOLD_S=4.0, SETTLE_S=1.0, MOVE 8-12 s and still_hold_s=0.5 are read from the shipped source, and the 0.6 s re-close is the t12 measurement
- The after-state is observable by a bystander, not only in telemetry: someone watching sees continuous gentle motion and a hand being felt during it, with no dead-still pause used as a sensing window
- Both halves of the motivation are evidenced: the unnatural feel is the operator's direct report from the live session, and the functional cost is the t12 measurement of 0.82 s max contact against a 4.00 s threshold
- The success signal is falsifiable before hardware: the replay assertion runs in CI against committed fixtures, so a regression in ghost-freedom fails the suite rather than waiting for a soak

## Success signals

- On the new idle motion, an offline replay of a labelled gentle-motion recording pair shows ZERO events on the untouched capture and repeated events on the petted capture at one press threshold; live, contact sustains past CONTENTMENT_AFTER_S=4.0 s and a hands-off soak of at least 180 s produces zero pat events
  - instruction: Ship as a replay test over the new labelled gentle-motion pair (zero events untouched, repeated events petted at one threshold) plus a 180 s hands-off live soak at zero events

## Scope / boundaries

- Ghost-freedom is the property that must not regress: CLAUDE.md records that hands-on calibration measured pat-vs-noise separation of 12-20x with the head still but only 0.7-2.0x while it wanders, that roll/x/y get dragged ~11x noisier by mechanical coupling, and that a fitted 40-tap FIR plant model removed only 1.1x of the residual — so the wander-band ghost class (#79) was closed structurally by the gate, not by thresholds
- Two shipped tests encode the freeze as INTENT, not incidentally: test_behavior_pat_sense_hardware.py:162 asserts _Replay('pat_wander').run() == 0 with the docstring 'a moving head cannot feel pats, so it reports none', and :152 is an inverse guard asserting _Replay('base_wander').run(still_hold_s=0.0) > 0 - i.e. the suite already MEASURES that removing the gate reintroduces ghosts on untouched wandering data. Removing the freeze inverts a test's intent rather than relaxing a threshold
- docs/operating-reachy.md carries explicit operator promises that a no-freeze change would falsify and must be rewritten in the same change, not left stale: :1113-1118 'The runtime does not infer contact during arbitrary motion', :1146-1148 'makes no arbitrary-motion sensing promise', and the :1108-1112 pettable-cadence arithmetic deriving '3.5 seconds of honest pettability' from the 0.5 s gate inside a 4 s hold

## Non-goals

- Lowering DEFAULT_PRESS_THRESHOLD (0.5 deg) to compensate for a removed gate is out of scope: the t12 evidence already shows the live side signal at 0.55-0.96 deg sitting on the noise floor, so trading the gate for a lower threshold buys sensitivity by spending exactly the ghost-freedom this boundary protects
- Touching the other three pat consumers is out of scope: listen_pat.py PatHook, sleep/patwake.py PatWakeSource and listen_sleep.py already sense through motion and are unaffected by pat_sense's gate - every stillness helper is private to pat_sense.py with zero external importers, so this work stays inside the behavior engine

## Assumptions

- The stillness gate already has a built-in off switch: reachy/behavior/pat_sense.py:830 returns True unconditionally when still_hold_s <= 0.0, so the sensing half of the freeze can be disabled with no new gating code
- Production blast radius is one line, test blast radius is ~30: every stillness helper is private to pat_sense.py with zero external importers, and behavior.py:838 is the only production constructor; but ~30 test call sites pass still_hold_s= as a kwarg, so deleting the PARAMETER (rather than just the gate body) is a TypeError sweep
- A petted-while-wandering fixture ALREADY EXISTS: tests/data/pat_pat_wander.csv (45 s, feel-alive idle motion WITH operator petting, all six DOF). Any no-freeze proposal can therefore be falsified offline against real data before it reaches hardware - paired with pat_base_wander.csv (35 s wandering untouched) as the ghost control
- The detector is not a pure amplitude threshold - PatDetector requires min_presses=2 within pat_window=3.0 s plus pat_cooldown - so temporal clustering does work the #80 p99-vs-p90 amplitude analysis did not credit; the sweep above shows that structure is still not enough to separate wander residual from contact

## Scope exploration

- `s1` — `reachy/behavior/pat_sense.py (stillness gate)`: still_hold_s<=0 already disables the gate at line 830; constants DEFAULT_STILL_EPS=0.01, DEFAULT_STILL_HOLD_S=0.5, DEFAULT_PRESS_THRESHOLD=0.5, DEFAULT_LAG_TAU=0.3, DEFAULT_HP_TAU=0.8
  - seeds: `c2`, `c5`
- `s2` — `reachy/cli/_commands/behavior.py:838 (composition)`: PatSenseDriver is constructed with defaults only; REACHY_PAT_SENSE is a four-way on/off token and carries no stillness tuning, so no operator surface reaches still_hold_s today
  - seeds: `c3`
- `s3` — `reachy/behavior/feel_alive.py (cadence)`: MOVE_MIN_S=8.0 MOVE_MAX_S=12.0 SETTLE_S=1.0 HOLD_S=4.0 SENSE_GATE_S=0.5 — the visible freeze is this 4 s hold, authored deliberately so the 0.5 s gate leaves 3.5 s of sensing; it is separate from the gate itself
  - seeds: `c4`
- `s4` — `reachy/behavior/pet_reaction.py (entry slew)`: HEAD_ROTATION_STEP_DEG=0.5, ANTENNA_STEP_DEG=1.0, BODY_YAW_STEP_DEG=0.25 per 0.02 s tick; SENSE_LOSS_GRACE_S=1.0, RELEASE_AFTER_S=1.0 in pat_sense — the reaction's own slew is what trips still_eps=0.01
  - seeds: `c5`
- `s5` — `CLAUDE.md (pat sense contributor notes) + docs/deliveries/2026-07-19 t12 findings`: the 12-20x still vs 0.7-2.0x wandering separation and the FIR-residual result are the recorded basis for the gate; t12 measured live |yaw| 0.55-0.96 deg against a 0.5 deg press threshold
  - seeds: `c6`, `c7`
- `s6` — `reachy/motion/listen_pat.py + reachy/sleep/patwake.py (sibling consumers)`: neither uses a stillness gate; both sense through motion via expected-pose tracking (minjerk interpolation / moving sleep-breathe pose), with PatHook at LIVE_PRESS_THRESHOLD_DEG=2.5 keeping EMA baselines rather than reseeding - the logic is duplicated from pat_sense, not shared
  - seeds: `c8`, `c9`
- `s7` — `tests/data/pat_pat_wander.csv + tests/test_behavior_pat_sense_hardware.py:143,152,162`: a 45 s petted-while-wandering recording exists for offline falsification, and the suite already contains an inverse guard proving gate-off fires ghosts on untouched wander data
  - seeds: `c11`, `c12`
- `s8` — `docs/deliveries/2026-07-18-runtime-senses-75-78.md:49-68 (#79 iteration history)`: lag compensation, baseline retention, high-pass, and a 40-tap FIR were each tried and each failed against feel-alive's wander; the FIR removed only 1.1x of a 3.3 deg residual tail
  - seeds: `c13`, `c14`
- `s9` — `docs/operating-reachy.md:1085-1148 (pat sense chapter)`: the chapter promises no arbitrary-motion sensing and derives its pettability arithmetic from the 0.5 s gate; these become false under a no-freeze change
  - seeds: `c16`
- `s10` — `offline replay sweep over all four tests/data/pat_*.csv with still_hold_s=0.0`: press 0.5/1.2/2.5/4.0/5.0/6.0 -> untouched-still 0,0,0,0,0,0; petted-still 7,7,4,0,0,0; untouched-wander 6,3,1,0,0,0; petted-wander 8,8,7,4,1,0. No threshold satisfies both petted columns while keeping both untouched columns at zero
  - seeds: `c18`

## Hard questions

- What does 'gentle and predictable' mean QUANTITATIVELY? #80 measured per-tick commanded change of 0.03-0.06 deg/tick at 50 Hz during wander and set still_eps=0.01 against it. The new profile needs a stated bound on amplitude, peak velocity, and jerk — not just 'slower' — or it cannot be implemented or verified
- Does pet_reaction's OWN entry move still blind the sense under the new motion? Its slew is 0.5 deg/tick (HEAD_ROTATION_STEP_DEG), which is 10-17x the wander's own 0.03-0.06 deg/tick. If gentle idle motion is sensible-through but the reaction's lean is not, the t12 sustain failure survives this change unchanged
- Does the pettable-window concept survive or is it deleted? If sensing works continuously there is no window, which removes SENSE_GATE_S/HOLD_S arithmetic AND the pat_state 'blocked' availability value that operators are currently told to expect during wander

## Open / follow-up

- Whether pat_sense keeps a stillness gate at all, or is refactored onto listen_pat.py's expected-pose model so the two stop duplicating detector conditioning with opposite rebaseline policies

## Resolved vagueness

- [unknown_blocking] No labelled recording of petting a GENTLY-MOVING head exists. All four shipped fixtures are still-or-full-wander, so the central claim of this frame — that a gentle predictable profile separates pat from plant — cannot be falsified offline until that pair is captured. This gates implementation, not framing — resolved: Carried into the plan as an EXECUTION GATE rather than resolved in the frame (the pattern #82's plan used for its t1). The plan's first task captures the labelled gentle-motion pair — same profile untouched, then petted — and every dependent task stops if that pair does not separate cleanly at a single press threshold. The unknown stays first-class and still blocks the build; it just blocks inside the plan, where it can be executed, instead of holding the frame in drafting.
