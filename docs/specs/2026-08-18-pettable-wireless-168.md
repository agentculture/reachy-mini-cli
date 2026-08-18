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
- The stillness gate becomes dt-normalized: `_commanded_still` judges max per-axis |delta-cmd|/dt (deg/s) against an eps in deg/s, default 1.75 (= 0.035 deg/tick x 50 Hz, the #82 design intent), hold unchanged at 1.0 s. Simulated open fraction: 12.2% / 12.5% / 13.3% at 50 / 22.8 / 6.8 Hz — the gate's behavior stops depending on tick cadence entirely
  - instruction: In reachy/behavior/`pat_sense.py`: `_commanded_still` computes max per-axis |delta|/dt (deg/s) against a new `DEFAULT_STILL_EPS_DEG_S` = 1.75; dt from consecutive ctx.now clamped like the existing filters; composition (`_commands`/behavior.py) reads `REACHY_PAT_STILL_EPS_DEG_S` and warns-and-ignores a set `REACHY_PAT_STILL_EPS` (never reinterpret units). Pin equivalence at clean 50 Hz by test; update docs/operating-reachy.md pat section + CLAUDE.md's 'ONE operating point' note to the deg/s vocabulary
  - honesty: At a clean 50 Hz the deg/s gate with eps=1.75 is behavior-identical to the shipped per-tick gate (same restamp decisions on the same trajectory), proven by a test; and on the live Wireless a petting session produces a Pat event + pat-acknowledge fire in the journal (nova can run it), with sampled availability showing a nonzero open fraction
- Offline downsample-replay tests join tests/`test_behavior_pat_sense_hardware.py`: the committed fixtures replayed at stride 2-4 (11.4 / 7.6 / 5.7 Hz) must keep detecting petting (measured today: 6/6 events at 7.6 Hz, 5 at 5.7 Hz) with zero events on the untouched recording — pinning detector cadence-robustness in CI so the wireless plant class cannot regress silently
  - instruction: In tests/`test_behavior_pat_sense_hardware.py`: parametrized stride replays (2/3/4 = 11.4/7.6/5.7 Hz) over the existing fixtures — petted event counts must meet the measured baseline (6/6/5), untouched must stay 0; offline mark, no robot
  - honesty: The stride replays run as ordinary offline pytest cases against the committed fixtures — no robot, no network — and fail if the petted counts drop or any untouched event appears

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

## Decisions

- Ori's architectural direction, recorded for issue #97 and explicitly OUT of #168's scope: neutralize Hz by separating EMIT from PROCESS per event type — the loop emits events, consumers process asynchronously under explicit per-type batch/drop policies. Thread-level separation is already the house style (AudioPump, face worker, SpeechActuator, tee/clip legs) and is exhausted on the Wireless (GIL + scheduler starvation at load ~10); the strong form is PROCESS-level fan-out of the heavy senses (face/clip) over the AudioTee socket pattern. Per-type policies: camera frames droppable, audio lossless-in-order (the #108/#115 lesson), the 50 Hz motion stream neither batchable nor droppable (the irreducible realtime core).

## Hard questions

- Should the existing `REACHY_PAT_STILL_EPS` env var be REINTERPRETED as deg/s (breaking any deployed per-tick override, none known beyond the retired Lite drop-in) or should a new `REACHY_PAT_STILL_EPS_DEG_S` be added with the old name refused/warned? One name must not mean two units on different versions — the reachy-pat-env-activation-trap taught what silent unit drift does (resolved: New env var `REACHY_PAT_STILL_EPS_DEG_S` carries the deg/s value; the old `REACHY_PAT_STILL_EPS` is NOT reinterpreted — if set it is ignored with one named warning at composition, so one name never means two units across versions (the reachy-pat-env-activation-trap lesson). Claude's recommended default, applied under Ori's blanket confirmation — veto if you prefer reinterpretation.)

## Resolved vagueness

- [unknown_blocking] Root cause on the Wireless is unmeasured: candidate mechanisms are (a) effective tick dt/jitter on the on-box host inflating per-tick command deltas, (b) observation-clock gaps >0.2 s, (c) ownership churn from another live rule/intent. Nova offered to run any measurement on the unit — a per-tick histogram of max |delta-cmd| and dt over a few warp periods would separate them in minutes — resolved: MEASURED on the unit 2026-08-19 (read-only SSH probe, runtime live): the root cause is tick-cadence collapse, not tuning. journalctl shows 818 overrun lines/10 min, p50 120 ms p90 167 ms max 1075 ms against the 20 ms budget; the tick counter advanced 4086 ticks in 598 s = ~6.8 Hz effective (mean dt ~147 ms). Host is CPU-saturated: load ~10 on 4 cores (aarch64), runtime process at 200% CPU (~10 runnable threads: face/clip/audio/GStreamer workers), daemon at ~85%. At dt ~147 ms the swing dwell's per-tick command delta is ~0.09-0.1 deg, ~3x `still_eps`, so the gate mathematically never opens; ticks near/above 0.2 s also trip `MAX_OBSERVATION_GAP_S`. Ownership churn is ruled out (state.json: feel-alive-1 owns all three channels). This is open issue #97 (fixed-period sleep, 23 Hz even on the dev box) compounded by the Pi-class host.
