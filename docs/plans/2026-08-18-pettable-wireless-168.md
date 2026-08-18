# Build Plan — pettable-wireless-168

slug: `pettable-wireless-168` · status: `exported` · from frame: `pettable-wireless-168`

> The Reachy Mini Wireless can be petted while feel-alive runs: the pat sense's sustained-slow gate opens on this plant's real tick cadence, a hand on the head lands as a pat event, and pat-acknowledge/pet-reaction fire — measured on the unit, not assumed from the Lite

## Tasks

### t1 — t1 The deg/s stillness gate: `_commanded_still` judges max per-axis |delta-cmd|/dt against `DEFAULT_STILL_EPS_DEG_S` = 1.25 in reachy/behavior/`pat_sense.py`

- instruction: reachy/behavior/`pat_sense.py`: replace `_commanded_still`'s per-tick compare with velocity = max per-axis |delta|/dt vs a new `eps_deg_s` constructor param (`DEFAULT_STILL_EPS_DEG_S` = 1.25 module constant; delete/deprecate `DEFAULT_STILL_EPS` + `still_eps` kwarg in one pass). dt = now - `prev_now` clamped \[0, 0.2\]; keep a dedicated `_prev_still_now` stash and CLEAR it in `_rearm_stillness_hold` so a gap never computes velocity across itself; clockless behavior unchanged (gate open). Equivalence-or-tighter test: replay one synthetic clean-50 Hz swing trajectory through both gate predicates and assert the deg/s-blocked tick set is a superset of the per-tick-blocked set. Update the SenseProviders/`_compose_run_seam` call site minimally (pass nothing = default). Do NOT touch `DEFAULT_MAX_OBSERVATION_GAP_S` or `DEFAULT_STILL_HOLD_S`.
- covers: c24, c7
- acceptance:
  - `_commanded_still` computes deg/s with dt from consecutive ctx.now clamped \[0, 0.2\] s; a missing clock keeps today's gate-open degradation; `MAX_OBSERVATION_GAP_S` and the hold (1.0 s) are untouched
  - A test proves the deg/s gate at 1.25 is equivalent-or-TIGHTER than the shipped per-tick gate over a synthetic clean-50 Hz trajectory: no tick exists where per-tick blocks and deg/s admits
  - Existing pat-sense unit tests updated deliberately for the new constructor parameter (eps in deg/s), never silently weakened; the composition call site in `_commands`/behavior.py is updated minimally so the full suite stays green

### t2 — t2 Env seam: `REACHY_PAT_STILL_EPS_DEG_S` read at composition; a set legacy `REACHY_PAT_STILL_EPS` is warned-and-ignored

- instruction: reachy/cli/`_commands`/behavior.py near the existing `_STILL_EPS_ENV` block (~line 1035): add `_STILL_EPS_DEG_S_ENV` = '`REACHY_PAT_STILL_EPS_DEG_S`' via the same `_pat_float_env` pattern, wire into `pat_kwargs` as `eps_deg_s`. If os.environ has the LEGACY '`REACHY_PAT_STILL_EPS`' set: do not read its value into anything; emit one senselog line, stage=pat, event=legacy-eps-ignored, naming both vars. Tests: env set -> driver got default AND the line was logged; new var set -> value reaches the driver; neither -> 1.25.
- depends on: t1
- covers: c24
- acceptance:
  - `REACHY_PAT_STILL_EPS_DEG_S` follows the existing `_pat_float_env` read-at-composition pattern and reaches the driver; unset means the 1.25 default
  - A set `REACHY_PAT_STILL_EPS` is IGNORED with exactly one named \[SENSE\] senselog warning (never reinterpreted as deg/s); a test asserts both the ignore and the line text

### t3 — t3 `blocked_reason`: PatState gains an additive named-cause field carried through the snapshot export

- instruction: reachy/behavior/sense.py: PatState gains `blocked_reason`: str | None = None (values 'stillness'/'ownership'/'clock-gap'/'no-command'; None when availability=='available'). reachy/behavior/`pat_sense.py`: thread a reason arg through `_begin_gap`/`_blocked_edge`/`_suspend_interaction` callers — `_stillness_open`->'stillness', `_ownership_changed` edge->'ownership', `_observation_clock_gapped`->'clock-gap', `_commanded_pose` None->'no-command', reader None keeps availability='unavailable' with reason None. Preserve reason across replace() calls while a gap persists. reachy/export/runtime.py SenseSnapshotDriver: render the field beside availability. PatAvailability Literal must stay byte-identical.
- depends on: t1
- covers: c23, h13
- acceptance:
  - PatState.`blocked_reason` is one of stillness/ownership/clock-gap/no-command while blocked and None when available; the PatAvailability Literal is byte-identical (no consumer change)
  - Every `_begin_gap` call site passes its cause and SenseSnapshotDriver renders it on the feed; a test covers each of the four cause labels end to end

### t4 — t4 Stride-replay hardware tests: cadence-robustness pinned in CI over the committed fixtures

- instruction: tests/`test_behavior_pat_sense_hardware.py`: extend the existing `_Replay`/`_Ctx` pattern with a stride parameter (rows\[::stride\]). Inject BOTH seams exactly as `test_still_petting_fires_repeatedly` does (`level2_threshold_fn`=lambda: fixed, `enough_after_fn`=seeded) — unseeded counts jitter run to run. Matrix: strides 2/3/4 x (`base_still`==0, `pat_still`>=seeded baseline); wander at eps 1.25: `base_wander`==0 AND `pat_wander`==0; one `warmup_s`=15.0 `pat_still`-prefix case asserting 0 events inside the first 15 s and >0 after. pytestmark offline stays.
- depends on: t1
- covers: c17, h2
- acceptance:
  - Parametrized replays at strides 2/3/4 (11.4/7.6/5.7 Hz) with BOTH detector random seams injected (`level2_threshold_fn` + `enough_after_fn`); still-untouched 0 events at every stride, still-petted >= a seeded baseline
  - Wander fixtures at eps 1.25: untouched AND petted both 0 events (the class stays fully closed); one warmup-enabled still case (default 15 s) asserts 0 boot ghosts; offline mark, no robot, no network

### t5 — t5 Docs: operating guide + CLAUDE.md move to the deg/s vocabulary; the #97 boundary stated

- instruction: docs/operating-reachy.md 'The pat sense' section + CLAUDE.md behavior-noun pat bullet: rewrite the stillness-gate paragraphs to deg/s (1.25 default, cadence-invariant, why: the wireless 6.8 Hz measurement), document `REACHY_PAT_STILL_EPS_DEG_S` + the ignored legacy var + `blocked_reason` values, and state the #97 boundary (this fix does not restore 50 Hz; emit/process direction lives on #97). Keep the 'these move TOGETHER' pairing warning, restated in deg/s. markdownlint-cli2 green on touched files.
- depends on: t1, t2, t3
- covers: c19, h12
- acceptance:
  - docs/operating-reachy.md pat section and CLAUDE.md's 'ONE operating point' note describe the deg/s gate (1.25), the new env var, the warned-and-ignored legacy var, and `blocked_reason` — no doc line still claims per-tick eps semantics
  - The boundary is stated where operators read it: this fix makes pattability cadence-robust and does NOT restore 50 Hz — issue #97 (with the recorded emit/process direction) owns the cadence; markdownlint green

### t6 — t6 Wireless live acceptance: deploy to the unit, publish the protocol on #168, verify end to end

- instruction: Operator/main-agent task, on-hardware. (1) Decide r1: prefer pip install from the merged main build on the unit (no uv there); rebase wireless-motor-enable only if its 4 commits are still unmerged. (2) Post the protocol to #168 signed '- reachy-mini-cli (Claude)': cadence re-measure (journal overrun stats), tombstone nova-face-noticed (enabled=false overlay entry + behavior reload) or idle the nova harness, pet the head, then restore. (3) Evidence: sample reachy/events/sense/snapshot ~30 s (expect available fraction > 0 and named `blocked_reason` on blocked samples); journalctl grep 'Pat level' then 'event=pat-acknowledge'. Record in docs/evidence/ with date.
- depends on: t2, t3, t4
- covers: c1, h5, c2, h4, c5, h6, c11, h8, c13, h9, c14, h10, c15, h11, h14
- acceptance:
  - The merged fix runs on the unit (deployment mechanism per risk r1, runtime restarted); the pat path matches main (re-check of the c5 branch boundary)
  - The verification protocol is posted to issue #168 signed, runnable by nova unaided: re-measure cadence (fix-independent, reproduces the ~6.8 Hz class), tombstone nova-face-noticed or idle the harness for the session, then pet
  - Evidence collected on the unit: sampled `pat_state` shows a NONZERO available fraction under feel-alive and every blocked sample carries a named cause; the journal shows a Pat event followed by a pat-acknowledge fire

### t7 — t7 Lite no-ghost soak: the loosened gate must not regress the proven unit

- instruction: Operator/main-agent task, on the Lite (localhost daemon). Deploy the same build, restart reachy-runtime, hands-off soak >= 10 min with feel-alive live: journalctl must show ZERO 'Pat level' lines. Then a brief petting check must detect (>=1 Pat + rule fire) — the gate is tighter at real Lite cadence than the proven config, so a dead sense here means a defect, not tuning. Record alongside t6's evidence.
- depends on: t2, t3
- covers: h7, h14
- acceptance:
  - Same build deployed to the Lite; a hands-off soak (>= 10 min, feel-alive live, nobody touching the robot) produces ZERO pat events in the journal
  - A brief petting check on the Lite still detects (the sense is loosened, not silenced) — the same end-to-end evidence shape as the 2026-07-22 verification

## Risks

- [unknown_nonblocking] Deployment path to the unit (frame park v2): it runs 0.48.0 EDITABLE @ wireless-motor-enable, pip-based, no uv — t6 must pick merge-branch vs pip-install-from-build at execution time (task t6)
- [unknown_nonblocking] Verification interference (frame park v3): the overlay rule nova-face-noticed nods on face (head ownership, cooldown 30 s) and nova's harness can submit gotos — the t6 protocol tombstones the rule or idles the harness, else expects short blind windows (task t6)
- [unknown_nonblocking] At 1.25 deg/s the swing's open windows are 0.92 s (6/min) — live petting may miss the gentlest pats; containment is widening `REACHY_PAT_STILL_EPS_DEG_S` on the box and re-running t6, never a code change (task t6)
- [unknown_nonblocking] Issue #97 is unfixed: the Wireless cadence can degrade further under new load; the deg/s gate is cadence-invariant by design but the detector was only validated down to ~5.7 Hz — below that, the 0.2 s observation-gap guard correctly refuses
