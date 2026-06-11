# Reachy Mini now feels your touch: scratch its head or nudge it sideways and it leans, snuggles, and softens into your hand — a proprioceptive 'pat mode' that works in both demo and real mode, no touch sensor required.

> Reachy Mini now feels your touch: scratch its head or nudge it sideways and it leans, snuggles, and softens into your hand — a proprioceptive 'pat mode' that works in both demo and real mode, no touch sensor required.

## Audience

- Reachy Mini owners/operators running the reachy-mini-cli who want the robot to feel alive and affectionate during hands-on interaction — both on real hardware (sdk/http transport) and in demo mode (no robot present).

## Before → After

- Before: Today reachy-mini-cli has listen/think/say/vision senses but no touch/proprioceptive sense; the robot cannot tell when it is being physically touched, so affectionate hands-on interaction (scratching its head) produces no reaction. The pat/snuggle logic only exists in reachy_nova, coupled to its own main loop and SDK access.
- After: Running 'reachy pat' (a new noun) makes the robot continuously watch for head-scratch (pitch press) and side-nudge (yaw press) via commanded-vs-actual pose deviation, and react by leaning/snuggling into the hand with antenna affection — in both demo and real mode.

## Why it matters

- Physical affection is the most direct way a desk robot 'bonds' with a person; reacting to a head-scratch by leaning in is the single highest-warmth interaction Reachy can offer, and it costs no extra sensor — it reuses the servo pose readback already present.

## Requirements

- Pat detection is a proprioceptive PatDetector ported from reachy_nova: an EMA-baselined deviation = actual_pose - commanded_pose on pitch (scratch) and yaw (side-nudge), with press/release thresholds, a press-count window, and a level1/level2 state machine with cooldowns. It consumes commanded pose (what the CLI sent) and actual pose read back from the SDK each tick.
  - honesty: The reachy_mini SDK exposes a per-tick actual head pose readback (get_current_head_pose() or equivalent) AND the CLI can know the pose it last commanded, so a meaningful commanded-vs-actual deviation is computable in-process on the sdk transport.
- The reaction is a 'lean/snuggle into the hand' motion enqueued on the existing serial MotionQueue/goto planner (the same one listen/think use), so pat reactions never conflict with other motion. The reaction leans toward the detected touch axis (down for scratch, toward the nudge for side-pat) plus an antenna affection overlay, then settles back.
  - honesty: Enqueuing a lean reaction onto the existing MotionQueue while a continuous pat-watch loop is also reading pose does not deadlock or starve the serial motion executor (the reaction move and the watch loop coexist on one transport).
- Pat mode is a new CLI noun 'pat' following the agent-first pattern: run (foreground loop) + overview, --json everywhere, sdk-first transport (default) with http fallback, plus a 'demo' verb that drives the reaction with no robot. It registers via register(sub) in _build_parser and gets an explain/catalog ENTRIES key.
  - honesty: A new 'pat' noun with run/demo/overview verbs, --json, and an explain ENTRIES key passes 'teken cli doctor . --strict' (the rubric CI gate) the same way listen/think do.
- Works in both modes: 'sdk' transport reads real get_current_head_pose() per tick for live detection; demo mode (no robot / no SDK extra) synthesizes pat events on a timer so the reaction is demonstrable, matching how vision/listen degrade. Missing SDK extra raises a clean exit-2 CliError pointing at [sdk], never a traceback.
  - honesty: Demo mode produces a visible, correct lean/snuggle reaction with NO reachy-mini extra installed (pure-stdlib + numpy), and the sdk path raises exit-2 CliError (not a traceback) when the extra is absent.

## Honesty conditions

- Touch reaction works end-to-end in both demo and real mode and reads as affectionate (leans/snuggles into the hand), not as an error tremor.
- The feature is usable by an operator with no robot attached (demo) and one with a live robot on sdk/http transport — both paths are exercised.
- 'reachy pat' continuously detects pitch-press (scratch) and yaw-press (side-nudge) and emits a distinct lean reaction per axis.
- Before this, no proprioceptive/touch sense exists in reachy-mini-cli — confirmed by absence of any pose-readback consumer in reachy/motion or reachy/cli/_commands.
- The reaction reuses the existing servo pose readback / motion planner and adds no new base runtime dependency beyond numpy.
- No new hardware, no LLM, and no persistent emotional-state machine are introduced; pat mode is a self-contained sense+reaction.
- All four success signals (fast detection, demo without robot, axis discrimination, teken doctor green) are independently checkable.

## Success signals

- On real hardware, scratching the head triggers a detectable pat within ~1-2 presses and the robot leans into it; 'reachy pat demo' visibly performs the lean/snuggle reaction with no robot attached; the proprioceptive detector distinguishes scratch (pitch) from side-nudge (yaw); teken cli doctor stays green (overview verb, error contract, --json).

## Scope / boundaries

- Not a new physical touch sensor or capacitive hardware; detection is purely proprioceptive (commanded vs actual head pose). Not a full emotion/mood engine like reachy_nova's — pat mode reacts to touch, it does not run an LLM or persistent emotional state. Does not require [sdk]/[daemon] to demo: demo mode simulates pat events.

## Non-goals

- Not porting reachy_nova's gesture catalog wholesale, its mood/emotional-state machine, purr/nuzzle voice, or its YOLO/face tracking. Pat mode is a standalone touch sense + reaction, not the whole nova personality.

## Decisions

- Process model: 'pat' ships as a foreground 'run' loop + 'demo' verb, AND composes into the idle layer — a detected pat interrupts the listen/think idle wander (reusing the cognition-signal pattern) so a scratch breaks stillness. Persistent background start/stop/status is deferred to a follow-up.
- Reaction scope: faithful nova lean/snuggle + antenna affection overlay with two-level (initial pat -> sustained snuggle), PLUS an improvement — soft body-yaw lean toward the hand and a settling 'sigh' on release. Poses are tunable; exact 'snuggliness' choreography is refined during the build.
- Real-mode detection is feasible: the pinned reachy_mini SDK exposes get_current_head_pose() (verified), plus get_current_joint_positions() and get_present_antenna_joint_positions() as fallbacks — so commanded-vs-actual deviation is computable in-process on the sdk transport.

## Open / follow-up

- Whether pat-watch should be its own foreground noun only, OR also a background start/stop/status supervised process like think (and whether it should compose with listen/think idle so a pat interrupts the idle wander).
