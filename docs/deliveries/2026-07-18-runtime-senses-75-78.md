# Delivery Summary — runtime-senses-bounded-rules-goto-lane-75-78

plan: `runtime-senses-bounded-rules-goto-lane-75-78` · run: `complete with
deviation` · date: `2026-07-18`

## Intent

Execute the converged 11-task / 5-wave plan (issues #75 #76 #77 #78, seeded
from the challenged frame) via /assign-to-workforce: give the boot presence a
pat sense (proprioception through a held media-free SDK client), bound
behavior lifetimes on BOTH admission surfaces (react rules + run_behavior
intents), compose the goto lane into the live engine with a fail-closed
intent kind and a `behavior goto` verb, record the #78 panel-feed design, and
live-verify on the robot before the PR.

## Planned vs actual

Every task built, TDD-gated, and merged; suite 2796 → **2965** (+169), teken
rubric 26/26, lint stack clean, v0.36.0:

- `t1` pose seam (sonnet) — as planned. TickContext.pose + LastPoseHolder +
  goto start-pose adapter with an empirical no-snap test.
- `t2` held state client (sonnet) — as planned. `no_media` construction,
  lazy retry, idempotent close; the probe evidence reproduced in 20 tests.
- `t3` pat provider (opus) — built as planned (ownership gate, one-tick
  latch, r1/r2 resolved); **its motion model was then falsified live** (see
  Deviations) and hardened through four evidence-driven iterations.
- `t4` bounded react rules (sonnet) — as planned, plus flagged fixture
  fallout in 7 sibling test files (inherent to the new fail-closed gate).
- `t5` intent bounding (sonnet) — as planned; refuses the unbounded RESULT
  (stronger than the payload rule); `declare_goal` exemption preserved.
- `t6` goto intent kind (sonnet) — as planned; refuse-don't-clamp with cited
  per-axis limits.
- `t7` composition (opus) — as planned, one flagged adaptation: the merged
  IntentDriver only self-registers kinds into its OWN registry, so GOTO is
  registered into `intent_driver.registry` post-construction (same shared
  registry, plan intent met, intents.py untouched).
- `t8` `behavior goto` verb (sonnet) — as planned; submit + await through the
  exact spool path agents use; catalog + overview + rubric green.
- `t9` #78 design note (**colleague**, Qwen3.6-27B) — as planned; merged
  as-authored after an operator gate (step budget expired mid-self-review).
- `t10` docs + version — split across two minds per the operator's direction:
  colleague wrote the operator-guide pat/bounded sections (5/5 rating);
  sonnet did CLAUDE.md, export-schema, the goto docs, v0.36.0 + `uv lock`.
- `t11` live-test — **partially complete by operator decision** (see below).

## Deviations

**d1 (approved, needs-follow-up → issue #79): the pat sense's motion model
was falsified on the real robot,** and the plan's confirmed design (ownership
gate + EMA baseline absorbing offsets) was insufficient. Four iterations, each
driven by measured evidence, are in the branch history:

1. **Lag compensation** (`a1da9fd`) — commanded low-passed at tau 0.3 s after
   measuring plant lag ≈ 0.28 s.
2. **Baseline retention + boot warmup** (`5a1d33d`) — resume edges use
   `clear_presses()` (pat.py's own docstring names full `reset()` as the
   phantom re-seeding chain); boot gets a one-time warmup mute.
3. **Deviation high-pass** (`9897264`) — a 2250-tick commanded-vs-actual
   recording showed the plant also OVERSHOOTS (gain 1.1–1.2×); the offline
   replay of the exact pipeline reproduced the live defect (6 fires/50 s) and
   a (hp_tau × press) grid picked a centred config validated to zero ghosts +
   6/6 synthetic pats on that recording.
4. **Opt-in gate** (`3b18737`) — a 330 s recording put the conditioned
   residual tail at **3.3°** (yaw): above any threshold a genuine pat could
   still clear. Amplitude discrimination cannot separate pats from this
   plant's wander response without real pat data. The full chain ships
   dormant behind `REACHY_PAT_SENSE=1`.

**Soak-round history (the honest record):** rounds 1–3 (24, 25, 29 rule fires
per ~4–5 min) were all invalidated by a deploy gotcha discovered afterwards —
`uv tool install --force` reuses its cached wheel for an unchanged version,
so all three soaks ran the ORIGINAL unfixed code. This made the "fixes don't
work" verdicts wrong and the evidence internally consistent only after the
cache-busted redeploy (round 4, file-identity verified), which showed warmup
working, fires starting at +68 s from the wander tail — leading to the 330 s
recording and the final opt-in decision.

## Live evidence (t11 lanes, deployed v0.36.0 on the box)

- **Goto**: `behavior goto` → applied result with goto id; spool-submitted
  gotos admitted and completed with real head motion (multiple runs).
- **Unbounded-rule refusal**: live `behavior reload` of a `speech → nod`
  rule without `duration_s` refused with the exact rule-id + remedy error;
  last-good config retained.
- **Intent bounding**: unbounded `run_behavior nod` refused with remediation;
  bounded nod admitted → owned the head → expired on schedule.
- **Held reader**: connected `media_backend=no_media`, fds flat at 4, reads
  ~0.02 ms, one-time lazy-construction overrun at tick 1 only (600–680 ms,
  caught by tick metrics), zero overruns after boot in every soak.
- **No-ghost (unhappy flow)**: with the sense enabled the ghosts were
  reproduced, diagnosed, and progressively suppressed (see d1); with the
  shipped default (sense off) the boot presence is calm — 90 s check: zero
  pat lines, zero overruns, clean unit stop/start.
- **NOT demonstrated (reported per h16, not assumed)**: a physical pat firing
  the armed rule end-to-end (happy flow) — the operator could not be at the
  robot; deferred with full instructions to the #79 hands-on follow-up,
  together with re-verifying h18's gesture-no-ghost with the sense enabled.

## Remaining work

- The #79 hands-on calibration session (happy + unhappy pat flows, real pat
  recordings, threshold tuning, possibly a calm-sensing mode), instructions
  in the follow-up issue.
- reTerminal repoint implementation (#78) — design note shipped, its own
  think/challenge pass pending.
- rms/face sense providers (#75 remains open for the siblings).

## Lessons

- `uv tool install --force` does NOT rebuild an unchanged version — deploy
  work-in-progress builds with `uv cache clean <pkg>` + `--reinstall`, and
  verify deployed-file identity before trusting live evidence.
- Same-size source mutations can leave stale `.pyc`; clear `__pycache__`
  after mutation testing.
- Offline replay of recorded commanded-vs-actual series is the highest-leverage
  live-tuning tool this repo has — it reproduced the live defect exactly and
  made threshold sweeps cheap; the recording scripts live in the session
  scratchpad and should graduate to a maintained harness during #79.
