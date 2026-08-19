# Delivery Summary — pettable-wireless-168

plan: `pettable-wireless-168` · run: `complete` · date: `2026-08-18`
baseline: `devague summary skeleton`

## Intent

Make the Reachy Mini Wireless pettable under `feel-alive` (issue #168). The
measured root cause was tick-cadence collapse (~6.8 Hz vs the 50 Hz design
point — issue #97 compounded by the Pi-class host) hitting a per-tick
stillness-gate tolerance that could then never open. The plan executed the
challenged spec's remedy: a dt-normalized (deg/s) gate, an env seam that
never reinterprets units, named blocked causes on the feed, CI stride-replay
pins, docs, and live acceptance on both units.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — t1 The deg/s stillness gate: `_commanded_still` judges max per-axis
  |delta-cmd|/dt against `DEFAULT_STILL_EPS_DEG_S` = 1.25 in
  reachy/behavior/`pat_sense.py`
- `t2` — t2 Env seam: `REACHY_PAT_STILL_EPS_DEG_S` read at composition; a set
  legacy `REACHY_PAT_STILL_EPS` is warned-and-ignored
- `t3` — t3 `blocked_reason`: PatState gains an additive named-cause field
  carried through the snapshot export
- `t4` — t4 Stride-replay hardware tests: cadence-robustness pinned in CI
  over the committed fixtures
- `t5` — t5 Docs: operating guide + CLAUDE.md move to the deg/s vocabulary;
  the #97 boundary stated
- `t6` — t6 Wireless live acceptance: deploy to the unit, publish the
  protocol on #168, verify end to end
- `t7` — t7 Lite no-ghost soak: the loosened gate must not regress the proven
  unit

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `262fde7` (merge `16ac10a`): `eps_deg_s` gate, `DEFAULT_STILL_EPS_DEG_S = 1.25`, dedicated `_still_last_now` stash cleared on re-arm, per-tick kwarg/constant retired outright; equivalence-or-tighter test `tests/test_pat_stillness_gate_deg_per_second.py` |
| `t2` | delivered | `76ecff5` (merge `9eecb00`): `REACHY_PAT_STILL_EPS_DEG_S` additive override; a set legacy `REACHY_PAT_STILL_EPS` never read, one `legacy-eps-ignored` senselog line; 9 new composition tests |
| `t3` | delivered | `eec2d46` (merge `db083f8`) + integration fix `e3977c8`: `PatState.blocked_reason` (`stillness`/`ownership`/`clock-gap`/`no-command`), threaded through every gap path, rendered by `SenseSnapshotDriver`; `PatAvailability` Literal pinned unchanged |
| `t4` | delivered | `b01e489` (merge `f7818f4`): 24 stride tests (11.4/7.6/5.7 Hz), both random seams injected, wander both-zero at 1.25, boot-warmup case |
| `t5` | delivered | `edc0545` (merge `2710599`): operating guide stillness-gate rewrite, CLAUDE.md operating-point paragraph, `export-schema.md` `blocked_reason` row, #97 boundary stated; markdownlint clean |
| `t6` | delivered | Deployed by merging the branch into the unit's checkout (its motor-enable commits kept); protocol + results posted on #168; gate open 6.4% live (was 0 %), all blocked samples cause-named, operator petting → `side_pat`+`scratch` detected → `pat-acknowledge` fired → `pet-reaction`, cooldown honored; overnight 0 ghosts/7.6 h (`docs/evidence/2026-08-19-t6-wireless-live-acceptance.md`) |
| `t7` | delivered | 10 min hands-off soak at ~49.8 Hz: 0 events; +4 min bonus untouched window: 0 events; petting check detects and fires (`docs/evidence/2026-08-19-t7-lite-no-ghost-soak.md`); boot service restored after |

## Mid-work Decisions

No `/deviate` records exist and none were needed — the plan completed as
written. Decisions made in-flight, none departing from a task's contract:

- t1's agent raised two unit tests' synthetic ramp (0.5 → 5.0 deg/s) because
  the old ramp legitimately fell below the new default — preserving each
  test's *intent* (fast motion keeps the gate closed), documented in its
  commit.
- t3's additive field broke one exact-dict assertion in the t2-owned
  composition test file; applied as a one-line main-agent integration commit
  (`e3977c8`) at the wave boundary, exactly as t3's agent flagged.
- t4's agent pinned petted counts as `>= 3` per stride (18-cell grid) rather
  than exact values; determinism verified by consecutive identical runs.
- r1 resolved at execution: the unit runs a git checkout with its own venv,
  so deployment was `git merge` + service restart (no pip step); an ephemeral
  git identity was needed for the merge commit.
- r2 resolved by observation instead of intervention: `nova-face-noticed`
  never fired during the acceptance windows (no face in frame), so no rule
  tombstone or harness idling was required.
- t7's Lite was found powered off (daemon: "No motors detected"); the
  operator restored power mid-session and a daemon restart recovered it — a
  timing hiccup, not a plan change.
- One full-suite run mid-wave-1 showed a single transient failure that
  vanished on two consecutive re-runs (a documented loaded-runner flake class
  in the composition test's own comments); recorded here for honesty.

## Drift From Plan

No drift: all seven tasks delivered to their confirmed contracts, no
deviation records, backed by the task-by-task accounting above. (The plan's
four risks r1–r4 all resolved without amendment: r1/r2 as above, r3 did not
materialize — gentle pats landed at 1.25 deg/s on both units, and r4's
cadence floor was never approached.)

## Evidence

- tests: full suite `uv run pytest -n auto` — **5453 passed, 7 skipped**
  (pre-existing env skips), green after every one of the five merges;
  baseline before the run 5404 passed
- tests: `tests/test_pat_stillness_gate_deg_per_second.py`,
  `tests/test_behavior_pat_blocked_reason.py` (10),
  stride tests in `tests/test_behavior_pat_sense_hardware.py` (24) — pass
- lint: `black --check` / `isort --check-only` / `flake8` — clean per task;
  `markdownlint-cli2` clean on t5's files
- commits: `262fde7..8d2f454` on `spec/pettable-wireless-168` (5 task
  commits, 5 merge commits, 1 integration commit, 2 evidence commits, plus
  the earlier spec/plan commits `bde99f3`, `2442e84`, `7058a80`)
- issues: #168 (diagnosis comment `#issuecomment-5335239551`, verification
  comment `#issuecomment-5338760811`), #97 (measurement + emit/process
  direction comment `#issuecomment-5334440634`)
- live evidence: `docs/evidence/2026-08-19-t6-wireless-live-acceptance.md`,
  `docs/evidence/2026-08-19-t7-lite-no-ghost-soak.md`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The stillness gate is cadence-invariant (deg/s) and 1.25 is equivalent-or-tighter at clean 50 Hz | high | commit `262fde7` · test `tests/test_pat_stillness_gate_deg_per_second.py` |
| The Wireless can be petted end to end at its real ~6.8 Hz | high | operator-confirmed petting session in `docs/evidence/2026-08-19-t6-wireless-live-acceptance.md` · #168 comment |
| No ghost regression on either unit | high | 7.6 h overnight (Wireless) + 10 min soak (Lite), both 0 events — evidence docs |
| Blocked samples are cause-attributed on the feed | high | live sample: 5289/5289 named `stillness` · `tests/test_behavior_pat_blocked_reason.py` |
| A stale legacy `REACHY_PAT_STILL_EPS` cannot silently change units | high | commit `76ecff5` tests · live absence of the warn line on a clean unit |
| Low-cadence detection cannot regress silently in CI | high | 24 stride tests, deterministic, in the merged suite |
| The Wireless's cadence itself (~6.8 Hz) is fixed | unverified | out of scope by boundary c19 — issue #97 remains open, measurement recorded there |

## Remaining Work / Follow-up

- Version bump + PR to main (gate 3) — next step, via the cicd flow.
- Issue #97 — the cadence deficit itself; carries the Wireless measurement
  and Ori's emit/process-separation direction (process-level fan-out of heavy
  senses over the tee pattern).
- The Lite's boot service still runs the previous installed build (restored
  as found); it picks the fix up on the next release install after the PR
  merges. The Wireless keeps the branch merged into its checkout — after the
  PR merges, its checkout should be re-pointed at main.
- Frame q2's observability split shipped in-session (t3); no other open
  question remains on the frame.
