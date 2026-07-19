# Build Plan — no-freeze pat sense

slug: `no-freeze-pat-sense` · status: `exported` · from frame: `no-freeze-pat-sense`

> Reachy Mini feels your hand while it is still moving — the freeze is gone

## Tasks

### t1 — EXECUTION GATE — capture the labelled gentle-motion pair and prove separation

- covers: h1, c18, h7, c13, h18, c14, h19
- acceptance:
  - Two recordings exist under tests/data/ driven by the SAME candidate gentle profile — one untouched, one petted — each carrying monotonic timestamps, commanded and actual pose on all six head axes plus body_yaw and both antennas, and an unambiguous label chosen before any signal is inspected
  - A committed replay check proves the pair separates at a SINGLE press threshold: zero events on the untouched capture and repeated events on the petted capture, using the existing PatDetector with still_hold_s=0.0
  - If no threshold separates the pair, the gate FAILS and every dependent task stops for a spec decision; the failure is recorded, not tuned away
  - The probe that drives the robot is disposable and is NOT shipped; only the labelled fixtures and their replay test are committed
  - The replay re-runs the four existing pat_*.csv fixtures unchanged, proving the new evidence is additive and the prior falsification (press 0.5-6.0 sweep) still reproduces

### t2 — Expose a stillness tuning surface so the gate and future experiments are runnable

- covers: c3, h12
- acceptance:
  - still_hold_s and still_eps are reachable from the behavior engine composition without editing source — a flag or env, defaulting to today's 0.5 / 0.01 so current behavior is byte-identical when unset
  - A test asserts the default path constructs PatSenseDriver with exactly today's values, and that an override reaches the driver
  - REACHY_PAT_SENSE's existing four-way on/off semantics are unchanged

### t3 — Verify the sibling pat consumers are untouched by this work

- covers: c8, h15
- acceptance:
  - A test drives listen_pat.py PatHook and sleep/patwake.py PatWakeSource against a moving-pose fixture and observes detections, confirming both sense through motion with no stillness gate present
  - A boundary assertion proves neither module imports pat_sense's stillness helpers, so this plan cannot regress them

### t4 — Fix the quantitative gentle-motion contract from the captured evidence

- depends on: t1
- covers: c20, c9, h16, c15, h5
- acceptance:
  - A new module states the profile contract as explicit bounds — peak amplitude per axis, peak commanded velocity in deg/tick, and jerk continuity — every number derived from the t1 recording that separated, never guessed
  - The press threshold pat_sense needs under this profile is MEASURED against the t1 fixtures, not copied from PatHook's 2.5 deg; the chosen value is asserted by a test over both captures
  - A validator rejects a candidate trajectory that exceeds any bound, so a future profile change cannot silently leave the sensible-through envelope
  - The module imports neither feel_alive nor pat_sense, so it stays a dependency leaf both can cite

### t5 — Replace feel-alive's dead-still hold with the continuous gentle profile

- depends on: t4
- covers: c20, c4, h13, c22, h9
- acceptance:
  - feel_alive emits a continuously-moving trajectory with NO segment whose complete commanded pose is constant; make_feel_alive's public factory name and its energy/params meaning are unchanged
  - The emitted trajectory satisfies the t4 validator on every tick of a full multi-cycle replay
  - Independence holds: with pat_sense untouched, a replay of the four existing fixtures through PatSenseDriver is byte-identical to before this task, proving the cadence and the gate are separate levers
  - Existing feel_alive tests for jitter, easing continuity and per-instance state isolation still pass unchanged

### t6 — Re-establish ghost-freedom as a measured CI gate

- depends on: t5
- covers: c6, h2, c25, h4
- acceptance:
  - A replay test over the t1 untouched gentle-motion capture asserts ZERO pat events at the chosen threshold, and fails the suite on regression rather than waiting for a soak
  - The petted capture asserts repeated detections at the same threshold, so sensitivity and ghost-freedom are pinned by the same committed fixtures
  - The gate runs in the normal CI lane against committed data with no hardware attached

### t7 — Rewrite the freeze-as-intent hardware tests with their new intent stated

- depends on: t6
- covers: c12, h17
- acceptance:
  - test_behavior_pat_sense_hardware.py:162 (currently asserting pat_wander detects nothing, docstring 'a moving head cannot feel pats') is rewritten to state what is now true, with the old intent named in the docstring so a reviewer sees the promise change
  - The :152 inverse guard asserting gate-off fires ghosts on untouched full-amplitude wander is PRESERVED — full-amplitude wander remains unsensable and this plan does not claim otherwise
  - No assertion is deleted or silently relaxed; every change either states a new intent or is justified in the docstring

### t8 — Resolve whether pet-reaction's own entry move still blinds the sense

- depends on: t5
- covers: c5, h14, c23, h10
- acceptance:
  - A replay drives pet_reaction's own commanded slew through the sense under the new profile and reports whether contact survives its entry move
  - If contact does not survive, the t12 sustain failure is explicitly DEFERRED with the measurement recorded — never silently assumed fixed by the motion change
  - If contact does survive, a replay asserts accrued contact passes CONTENTMENT_AFTER_S=4.0 s, the threshold t12 measured at 0.82 s
  - pet_reaction's slew constants are only changed if the measurement demands it, and any change keeps every axis inside its documented limit

### t9 — Rewrite the operator-facing pat sense documentation in the same change

- depends on: t5
- covers: c16, h6, c21, h8, c24, h11
- acceptance:
  - docs/operating-reachy.md no longer promises 'the runtime does not infer contact during arbitrary motion' or derives 3.5 s of pettability from a 0.5 s gate inside a 4 s hold; the replacement states the real new envelope
  - The doc states plainly what is still NOT sensable — full-amplitude wander — so the new promise is bounded by the evidence rather than overclaiming
  - Whatever pat_state reports during gentle motion is documented for the operator, including whether the 'blocked' availability value survives
  - The explain catalog entry for the behavior noun matches the new wording

### t10 — Live acceptance: no visible freeze, contact felt while moving, ghost-free soak

- depends on: t6, t7, t8, t9
- covers: c1, h3
- acceptance:
  - In one live session a bystander observes NO dead-still pause used as a sensing window, and a hand is felt while the robot is moving — both in the same session, not inferred from replay
  - An uninterrupted hands-off soak of at least 180 s on the default presence produces zero pat events, zero active pat_state contact and zero reaction admissions
  - One dated evidence bundle records the applied config, the runtime feed interval, transition timestamps, replay commands and results, and the full soak start and end; any missing item fails delivery
  - On failure the session is recorded as not met with its measurements, following the t12 precedent, rather than tuned until it passes

## Risks

- [unknown_nonblocking] Exact quantitative bounds for 'gentle and predictable' are unknown until t1 captures the pair; frame hard-question q1 is unresolved, so t4 cannot state its numbers until the gate produces them (task t4)
- [unknown_nonblocking] pet_reaction's entry slew is 0.5 deg/tick, 10-17x the wander's own 0.03-0.06 deg/tick, so the new profile may leave the t12 sustain failure completely untouched; t8 must report this honestly rather than assume the motion change fixed it (task t8)
- [unknown_nonblocking] Removing the dead-still hold reopens the #79 phantom-pat class if the profile does not actually separate; t6 is the measured gate that must pass before the freeze leaves the default presence (task t6)
- [follow_up] De-duplicating pat_sense and listen_pat's detector conditioning (opposite rebaseline policies, no shared helper) is deliberately out of this plan
