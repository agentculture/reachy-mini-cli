# pettable-wireless-168

> The Reachy Mini Wireless can be petted while feel-alive runs: the pat sense's sustained-slow gate opens on this plant's real tick cadence, a hand on the head lands as a pat event, and pat-acknowledge/pet-reaction fire — measured on the unit, not assumed from the Lite
> instruction: Fix lands in reachy/behavior/`pat_sense.py` (`_commanded_still` + eps constant/env semantics) + downsample-replay tests + the operating guide's pat section; verify live on the Wireless (petting -> pat-acknowledge) and no-ghost soak on the Lite; issue #97 stays open for the cadence itself

## Audience

- Anyone petting the Wireless robot (the robot's whole point of touch), Ori as operator of both units, and nova — the on-box agent that filed the measurement and can verify the fix live

## Before → After

- Before: The Wireless runtime ticks at ~6.8 Hz, not 50: measured live 2026-08-19 (4086 ticks/598 s; overrun p50 120 ms, p90 167 ms, max 1.08 s; 818 overrun logs/10 min), on a CPU-saturated host (load ~10 on 4 cores; runtime 200%, daemon 85%). Under that cadence the per-tick stillness gate cannot open, so `pat_state` is blocked continuously and the robot cannot be petted — issue #97's cadence deficit amplified by the Pi-class host
- After: While feel-alive runs on the Wireless, the stillness gate opens a nonzero fraction of the time on the unit's REAL cadence, a hand on the head lands as a pat event, and pat-acknowledge/pet-reaction fire — verified on the unit, with the Lite's proven operating point untouched

## Requirements

- Issue #168's measured defect is the target: on the Wireless (0.48.0 editable @ wireless-motor-enable), with feel-alive as base, reachy/events/sense/snapshot showed `pat_state`.availability=blocked 19/19 over ~32 s and a real petting session produced zero pat cues, zero rule fires
  - honesty: The root-cause measurement is reproducible on the unit: journal overrun stats and tick-counter cadence (~6.8 Hz) as recorded in the resolved v1 note, before any fix lands
- Offline downsample-replay tests join tests/`test_behavior_pat_sense_hardware.py`: the committed fixtures replayed at stride 2-4 (11.4 / 7.6 / 5.7 Hz) must keep detecting petting (measured today: 6/6 events at 7.6 Hz, 5 at 5.7 Hz) with zero events on the untouched recording — pinning detector cadence-robustness in CI so the wireless plant class cannot regress silently
  - instruction: In tests/`test_behavior_pat_sense_hardware.py`: parametrized stride replays (2/3/4 = 11.4/7.6/5.7 Hz) over the existing fixtures. MUST inject BOTH detector random seams (`level2_threshold_fn` + `enough_after_fn`) exactly as the existing tests do — un-seeded replay counts jitter run to run (observed in the challenge probe). Matrix: still-untouched 0 events at every stride; still-petted >= a seeded baseline; wander-untouched 0 events at the chosen eps; wander-petted expectation follows the q3 eps decision; plus one warmup-enabled still case (15 s default) asserting 0 boot ghosts. Offline mark, no robot
  - honesty: The stride replays run as ordinary offline pytest cases against the committed fixtures — no robot, no network — and fail if the petted counts drop or any untouched event appears
- Per Ori (this session): `pat_state`'s 'blocked' splits into NAMED causes — PatState gains a `blocked_reason` field (stillness / ownership / clock-gap / no-command) carried through the SenseSnapshotDriver export, so the next plant regression is diagnosable from the feed without an on-box probe. The PatAvailability Literal itself is unchanged (consumers inspecting availability keep working); the reason is additive
  - instruction: In reachy/behavior/sense.py: add `blocked_reason`: str | None = None to PatState (values: stillness / ownership / clock-gap / no-command; None when available); `pat_sense.py`'s `_begin_gap` callers pass the cause; reachy/export/runtime.py SenseSnapshotDriver renders it. PatAvailability Literal unchanged. Verify by re-running nova's 32 s snapshot sampling on the unit — every blocked sample carries a named cause
  - honesty: Re-running nova's 32 s snapshot sampling on the unit after the fix attributes every blocked sample to a named cause; the availability Literal is byte-identical so no existing consumer changes
- The stillness gate becomes dt-normalized: `_commanded_still` judges max per-axis |delta-cmd|/dt (deg/s) against `DEFAULT_STILL_EPS_DEG_S` = 1.25, hold unchanged at 1.0 s. Challenge-probe evidence: cadence-invariant (bimodal wireless dt = clean 50 Hz at every eps), wander class fully closed at 1.25 (0 events, ~0% open on both wander fixtures — every #80 pinned test keeps its meaning), swing opens ~6.3% in 0.92 s windows 6x/min (~3x the open time of the configuration proven live on the Lite), and on the Wireless's measured ~6.8 Hz the gate now opens where the per-tick gate opened never
  - instruction: In reachy/behavior/`pat_sense.py`: `_commanded_still` computes max per-axis |delta|/dt (deg/s) against new `DEFAULT_STILL_EPS_DEG_S` = 1.25; dt from consecutive ctx.now clamped \[0, 0.2\] like the existing filters; composition reads `REACHY_PAT_STILL_EPS_DEG_S`, and a set `REACHY_PAT_STILL_EPS` is ignored with one named \[SENSE\] senselog warning (never reinterpreted). Pin per-tick-vs-deg/s equivalence-or-tighter at clean 50 Hz by test; update docs/operating-reachy.md + CLAUDE.md's 'ONE operating point' note to the deg/s vocabulary
  - honesty: At a clean 50 Hz the deg/s gate at 1.25 is strictly TIGHTER than the shipped per-tick gate (1.75 deg/s-equivalent), so the Lite's ghost exposure cannot grow; live: a Wireless petting session produces a Pat event + pat-acknowledge fire, and a hands-off soak on either unit produces zero events

## Honesty conditions

- Proven only by live acceptance: a petting session on the Wireless (runtime live, feel-alive base) produces a Pat event + pat-acknowledge fire in its journal, and the Lite stays unregressed
- git diff origin/main...origin/wireless-motor-enable lists no file on the pat path (`pat_sense.py`, `feel_alive.py`, motion/pat.py) — re-checkable at any time
- The deg/s gate is behavior-equivalent to the per-tick gate at a clean 50 Hz (pinned by an offline test) and a hands-off Lite soak shows zero pat events
- The cadence numbers are reproducible on the unit before the fix lands: journal overrun stats (p50 120 ms / p90 167 ms) and tick-counter rate (~6.8 Hz), as recorded in resolved v1
- nova (the on-box agent that filed #168) can run the live verification unaided from the issue thread; no verification step requires hardware only Ori can reach
- Verified on the unit's REAL measured cadence (~6.8 Hz), not on a simulator or the dev box: sampled `pat_state`.availability shows a nonzero available fraction and a petting session detects
- Both signals are observable with existing tooling only — the events snapshot feed and journalctl — no new instrumentation is a prerequisite for verification
- A hands-off soak on the Lite with the deg/s gate (untouched robot, feel-alive live) produces zero pat events — same protocol as the #80 verification's 3-minute soak
- Issue #97 stays open and carries the Wireless measurement (comment posted 2026-08-19 with the ~6.8 Hz numbers and the emit/process direction); nothing in this spec claims to change the cadence

## Success signals

- On the Wireless with the runtime live: (1) sampled `pat_state`.availability shows a nonzero 'available' fraction under feel-alive, and (2) a human petting session produces a Pat event followed by a pat-acknowledge rule fire in the journal — the same end-to-end evidence the Lite's 2026-07-22 verification set

## Scope / boundaries

- This is not a wireless-motor-enable branch regression: git diff origin/main...origin/wireless-motor-enable touches only `face_sense.py`, intents.py, `media_client.py` and `sdk_transport.py` — `pat_sense.py` and `feel_alive.py` are identical to main, so the fix targets main and the cause is environmental (host/plant/tick cadence), not the deployed branch
- The shipped v0.41.0 operating point is PROVEN LIVE on the Lite (spark-f8a9, 2026-07-22: Pat level1! -> pat-acknowledge fired -> pet-reaction, cooldown honored, no env overrides) — so the fix must not silently move the shipped defaults out from under the working Lite deployment; a plant- or box-specific remedy (per-box values, or a dt-normalized gate that is provably equivalent at a clean 50 Hz) is required
- Issue #97 (the engine's cadence deficit: fixed-period sleep + tick work over budget, compounded on the Wireless by a CPU-saturated host at load ~10) stays a separate open issue: this spec makes pattability cadence-robust, it does NOT restore 50 Hz on the Wireless or shed the Pi's sense-stack load

## Non-goals

- `MAX_OBSERVATION_GAP_S` stays 0.2 s: the Wireless's p90 tick (167 ms) fits under it, only the rare tail tick (max 1.08 s) restamps the hold — acceptable, because a genuine 1 s stall SHOULD invalidate temporal pairing; raising it would trade ghost-safety for marginal open time

## Assumptions

- The Lite has effectively been running a TIGHTER gate than designed (0.035 deg/tick at its real ~23 Hz = 0.8 deg/s = 2.3% open); the deg/s default of 1.75 restores design intent and LOOSENS the Lite's gate — safe per the #80 physics (the 0.70-vs-2.52 deg separation was measured exactly at the sustained-slow operating point 1.75 deg/s admits), but it needs a live no-ghost soak on the Lite to confirm

## Scope exploration

- `s1` — `GitHub issue #168 (nova's live measurement)`: 19/19 snapshot samples blocked over ~32 s under feel-alive; #82's expected 10-15% open-fraction predicts ~95% chance of catching an open gate in 19 samples — measured 0%; human petting produced nothing in the journal
  - seeds: `c2`
- `s2` — `reachy/behavior/pat_sense.py (_commanded_still, DEFAULT_STILL_EPS/HOLD)`: `still_eps` is a per-tick velocity tolerance (deg/tick), not deg/s: at 50 Hz 0.035 deg/tick = 1.75 deg/s, at an effective 25 Hz it halves to the same deg/tick but doubles in per-tick delta terms; one tick above eps anywhere in the 1.0 s hold restamps the clock, so tick jitter is amplified
  - seeds: `c3`
- `s3` — `reachy/behavior/feel_alive.py (swing_time, WARP_DEPTH/PERIOD, _raw_motion amplitudes)`: the swing was designed to open the gate 10-15% of the time at 50 Hz; margin analysis from the shipped amplitudes shows antenna sway is the binding axis and the dwell clears eps by only ~2.7x — also the pre-#82 dead-still `HOLD_S` machinery was deliberately REMOVED (docstring: freezing 'reads as stopping'), so option 2 re-adds a rejected design and must say so
  - seeds: `c4`
- `s4` — `git origin/wireless-motor-enable (4 commits over main)`: branch adds motor-enable + `wake_up` + enroll-intent only; the entire pat path is unchanged from main
  - seeds: `c5`
- `s5` — `reachy/cli/_commands/behavior.py (_STILL_*_ENV, _PRESS_THRESHOLD_ENV, _HP_TAU_ENV — the REACHY_PAT_* seam)`: the six-knob env override surface exists and is read at composition; issue #168's 'env-overridable would let boxes tune' is satisfied today — the deliverable is wireless-plant values (or a structural fix), plus the known trap that `HP_TAU` must never go down (0.08 silenced the sense on the Lite, bisected on hardware)
  - seeds: `c6`
- `s6` — `eidetic recall: reachy-pat-needs-a-still-head + pat-sense-stillness-physics + reachy-pat-env-activation-trap`: same tuning, same feel-alive base, gate opens and pats land end-to-end on the Lite; the physics dataset (tests/data/`pat_`\*.csv) and the calibration protocol from #80 exist and nova offered to re-run the four-recording protocol on the Wireless
  - seeds: `c7`
- `s7` — `tests/data/pat_*.csv + tests/test_behavior_pat_sense_hardware.py`: the #80 hands-on calibration protocol is reproducible and its artifacts are committed test fixtures; a wireless recalibration follows the same shape
  - seeds: `c8`
- `s8` — `reachy/behavior/default_rules.toml + library.py (pet-reaction, orient-to-sound tombstone note)`: rules are the deployed surface for 'be still when a human engages'; the sense already supports sensing under a held non-base pose (owner ids are never allowlisted, stillness is what matters)
  - seeds: `c9`
- `s9` — `reachy/behavior/sense.py (PatAvailability) + reachy/export/runtime.py (SenseSnapshotDriver)`: blocked is one label for stillness-closed, ownership-edge and clock-gap; the export feed cannot currently distinguish them, which is exactly the measurement gap an on-box probe must close first
  - seeds: `c10`
- `s10` — `challenge pass / adjacent-systems lens: reachy/behavior/rules.py two-layer merge + the unit's live overlay`: overlay MERGES per rule id, so shipped pat-acknowledge/pet-reaction ARE live on the wireless (its overlay adds only nova-face-noticed); the face->nod rule is a periodic head-ownership interference during petting, routed to park v3
- `s11` — `challenge pass / adjacent-systems lens: reachy/sleep/patwake.py + reachy/cli/_commands/pat.py`: neither uses `_commanded_still`, so the deg/s change cannot affect them; PatWakeDetector has its own per-update cadence sensitivities if sleep ever runs on the Pi-class host — noted, out of this spec's scope
- `s12` — `challenge pass / counter-evidence lens: tests/data wander fixtures replayed through a deg/s gate`: the #80 no-ghost guarantee (untouched wander: 0 events) holds at every eps tested up to 1.75; what changes at 1.75 is wander PETTING becoming partially detectable (2-4 events), flipping one pinned test — surfaced as the q3 eps decision with the full tradeoff table (eps sweep + open-stretch lengths, challenge probes 1-3)
- `s13` — `challenge pass / failure-mode lens: bimodal tick distribution + boot warmup`: sim with a wireless-shaped bimodal dt matches clean-50 Hz open fractions at every eps (the deg/s gate does not alias on mixed dt); 15 s warmup at 7.6 Hz produces 0 boot ghosts on the fixtures — both clean
- `s14` — `challenge pass / migration+ops lens: deployed env overrides on BOTH boxes`: probed live: neither the Lite (no runtime drop-ins at all — the old pat-sense.conf is gone) nor the Wireless sets any `REACHY_PAT_`\* variable, so the warn-and-ignore migration path for the old eps var has zero known live instances; the warning must still be a named \[SENSE\] senselog line so an ignored override is visible in the journal
- `s15` — `challenge pass / concurrency+security lens: pat_sense.py gate internals`: clean pass — the deg/s computation adds one float and one prior-now stash on the tick thread, no new shared state, no new I/O, no new env read after composition; dt is clamped like the existing lag/hp filters so a clock hiccup cannot amplify
- `s16` — `challenge pass / reversibility lens: containment if 1.75-or-1.25 ghosts on some future plant`: the new env var tightens per-box without code change; full rollback is the previous release since the old per-tick var stays ignored (a deliberate one-name-one-unit choice); residual risk recorded in q3's dial

## Decisions

- Ori's architectural direction, recorded for issue #97 and explicitly OUT of #168's scope: neutralize Hz by separating EMIT from PROCESS per event type — the loop emits events, consumers process asynchronously under explicit per-type batch/drop policies. Thread-level separation is already the house style (AudioPump, face worker, SpeechActuator, tee/clip legs) and is exhausted on the Wireless (GIL + scheduler starvation at load ~10); the strong form is PROCESS-level fan-out of the heavy senses (face/clip) over the AudioTee socket pattern. Per-type policies: camera frames droppable, audio lossless-in-order (the #108/#115 lesson), the 50 Hz motion stream neither batchable nor droppable (the irreducible realtime core).

## Open parks

- [unknown_nonblocking] Deployment path for live verification: the unit runs 0.48.0 EDITABLE @ wireless-motor-enable (pip-based, no uv on the box) while the fix lands on main — verifying on the unit requires rebasing/merging that branch or a pip install from a build; owner and mechanism are a plan-side decision, not spec content
- [unknown_nonblocking] Verification-protocol interference on the unit: the overlay rule nova-face-noticed (face -> nod 2 s, cooldown 30 s) takes head ownership up to twice a minute with a human in frame, and nova's harness can submit gotos — each suspends pat detection and re-arms the 1 s hold. The petting session should tombstone that rule or idle the harness, or expect short blind windows; a plan-side protocol detail

## Resolved vagueness

- [unknown_blocking] Root cause on the Wireless is unmeasured: candidate mechanisms are (a) effective tick dt/jitter on the on-box host inflating per-tick command deltas, (b) observation-clock gaps >0.2 s, (c) ownership churn from another live rule/intent. Nova offered to run any measurement on the unit — a per-tick histogram of max |delta-cmd| and dt over a few warp periods would separate them in minutes — resolved: MEASURED on the unit 2026-08-19 (read-only SSH probe, runtime live): the root cause is tick-cadence collapse, not tuning. journalctl shows 818 overrun lines/10 min, p50 120 ms p90 167 ms max 1075 ms against the 20 ms budget; the tick counter advanced 4086 ticks in 598 s = ~6.8 Hz effective (mean dt ~147 ms). Host is CPU-saturated: load ~10 on 4 cores (aarch64), runtime process at 200% CPU (~10 runnable threads: face/clip/audio/GStreamer workers), daemon at ~85%. At dt ~147 ms the swing dwell's per-tick command delta is ~0.09-0.1 deg, ~3x `still_eps`, so the gate mathematically never opens; ticks near/above 0.2 s also trip `MAX_OBSERVATION_GAP_S`. Ownership churn is ruled out (state.json: feel-alive-1 owns all three channels). This is open issue #97 (fixed-period sleep, 23 Hz even on the dev box) compounded by the Pi-class host.
