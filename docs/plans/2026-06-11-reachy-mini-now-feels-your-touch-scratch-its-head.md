# Build Plan — Reachy Mini now feels your touch: scratch its head or nudge it sideways and it leans, snuggles, and softens into your hand — a proprioceptive 'pat mode' that works in both demo and real mode, no touch sensor required.

slug: `reachy-mini-now-feels-your-touch-scratch-its-head` · status: `exported` · from frame: `reachy-mini-now-feels-your-touch-scratch-its-head`

> Reachy Mini now feels your touch: scratch its head or nudge it sideways and it leans, snuggles, and softens into your hand — a proprioceptive 'pat mode' that works in both demo and real mode, no touch sensor required.

## Tasks

### t1 — PatDetector: proprioceptive detector in reachy/motion/pat.py (pure numpy)

- covers: c9, h7
- acceptance:
  - reachy/motion/pat.py exposes PatDetector.update(commanded_pitch, actual_pitch, commanded_yaw, actual_yaw) ported from reachy_nova: EMA-baselined deviation, pitch press=scratch / yaw press=side_pat, press/release thresholds, press-count window, level1/level2 state machine with cooldowns
  - tests/test_pat_detector.py: feeding a pitch-press impulse sequence yields a level1 ('scratch') event; a yaw-press sequence yields ('side_pat'); sub-threshold deviation yields no event; cooldown suppresses re-fire
  - module imports with only numpy + stdlib (no reachy_mini, no new base dep)

### t2 — Transport head-pose readback: add head_pose() across transports

- covers: h1, c4, h8
- acceptance:
  - reachy/robot/transport.py base Transport gains head_pose() raising the standard _unsupported CliError by default
  - reachy/robot/sdk_transport.py implements head_pose() via ReachyMini().get_current_head_pose() returning (pitch_deg, yaw_deg) or the 4x4; http_transport leaves it unsupported (clean exit-2, not a traceback)
  - tests/test_transport_pose.py asserts base raises CliError and a stub SDK readback maps to pitch/yaw degrees; establishes the first pose-readback consumer (was absent before)

### t3 — PatReaction: lean/snuggle motion planner in reachy/motion/pat_reaction.py

- covers: c10, h2
- acceptance:
  - reachy/motion/pat_reaction.py exposes PatReaction that enqueues a lean-into-touch goto sequence onto the existing serial MotionQueue: lean down for scratch, toward the nudge for side_pat, soft body-yaw lean toward the hand, antenna affection overlay, then a settling 'sigh' on release
  - tests/test_pat_reaction.py: a 'scratch' event enqueues a pitch-down-then-settle move set; a 'side_pat' enqueues a yaw-toward + body-yaw move set; moves are enqueued one at a time (serial), never blocking the caller
  - pure-planner: uses only the existing goto/MotionQueue API + numpy; no new base dep

### t4 — pat CLI noun: run + demo + overview in reachy/cli/_commands/pat.py

- depends on: t1, t2, t3
- covers: c1, c3, c11, c12, h3, h4
- acceptance:
  - reachy/cli/_commands/pat.py exposes register(sub): a 'run' verb (foreground loop: each tick command a pose, read actual via transport.head_pose(), feed PatDetector, fire PatReaction on event), a 'demo' verb (synthesize pat events on a timer, no robot), and an 'overview' verb; --json on every verb; registered via one import + pat.register(sub) in reachy/cli/__init__._build_parser
  - sdk-first transport (default) with --transport http / REACHY_TRANSPORT=http fallback; running the sdk path with the reachy_mini extra absent raises a clean exit-2 CliError pointing at [sdk], never a traceback
  - tests/test_cli_pat.py: 'reachy pat demo --json' exits 0 with no robot and emits a structured reaction event; 'reachy pat overview --json' lists the verbs; missing-SDK sdk path exits 2 with error:/hint: lines

### t5 — explain catalog + rubric: ENTRIES key for pat so teken cli doctor stays green

- depends on: t4
- acceptance:
  - reachy/explain/catalog.py gains an ENTRIES key for the ('pat',) command path (and verbs) with markdown describing scratch/side-nudge detection + lean reaction
  - uv run teken cli doctor . --strict passes (pat has overview, error contract, --json); test_every_catalog_path_resolves stays green

### t6 — Idle composition: a pat breaks the listen/think idle stillness

- depends on: t4
- covers: h7
- acceptance:
  - a pat-active signal (mirroring reachy/speech/cognition_signal.py) is set by pat during a reaction; reachy/motion/listen.py idle tick pauses/suppresses its wander while the signal is present, so a scratch visibly breaks the idle wander
  - tests assert the idle producer reads the signal and yields to the pat reaction; signal is removed on reaction end (including on error)

### t7 — Integration, docs, version bump: end-to-end pat affection verified

- depends on: t4, t5, t6
- covers: c2, c5, c6, c7, h5, h6, h9, h10, h11
- acceptance:
  - tests/test_pat_integration.py: end-to-end 'pat demo' produces an affectionate lean/snuggle reaction (not an error tremor); the detector distinguishes scratch (pitch) from side-nudge (yaw) end-to-end; both demo and a stubbed sdk path are exercised
  - no new base runtime dependency beyond numpy (pyproject base deps unchanged); README.md + CLAUDE.md document the pat noun
  - version bumped via version-bump skill AND uv.lock re-locked (uv lock) so CI uv sync does not re-resolve into the pycairo build; CHANGELOG entry added

## Risks

- [follow_up] Bumping pyproject version without re-running 'uv lock' makes CI 'uv sync' re-resolve and die on the pycairo/cairo build (bit PR #33). The version-bump task MUST also commit an updated uv.lock. (task t7)
- [unknown_nonblocking] Exact head_pose() return shape from the pinned reachy_mini get_current_head_pose() (4x4 matrix vs euler) needs on-SDK verification during t2; nova uses a 4x4 + scipy Rotation, but base env has no scipy — t2 must extract pitch/yaw without adding scipy as a base dep. (task t2)
