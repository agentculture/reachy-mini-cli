# Implementation split map — runtime senses + bounded rules + goto lane (75–78)

Companion to the converged plan
(`2026-07-18-runtime-senses-bounded-rules-goto-lane-75-78.md`): the
human-approved implementation split — every task's content, wave, worktree,
and owner — presented at workforce gate 2 and recorded here as a durable
artifact. Run mechanics for this fan-out: **max 3 agents concurrent**, one
isolated git worktree per task, TDD gate (task tests pass before AND after)
on every merge into `spec/runtime-senses-75-78`, `/ask-colleague review` on
the full committed diff before the PR, deviations self-decided by the main
agent under the operator's standing approval — each recorded via `/deviate`
and opened as a repo issue for review.

## Ownership table

| Task | Wave | Owner | Worktree | Files |
|------|------|-------|----------|-------|
| t1 pose seam | 1 | sonnet subagent | `agent/t1` | `reachy/behavior/engine.py`, new `reachy/behavior/pose_feed.py` |
| t2 held state client | 1 | sonnet subagent | `agent/t2` | new `reachy/robot/state_reader.py` |
| t4 bounded react rules | 1 | sonnet subagent | `agent/t4` | `reachy/behavior/rules.py`, `reachy/behavior/rule_engine.py` |
| t5 intent bounding | 1 | sonnet subagent | `agent/t5` | `reachy/behavior/intents.py` |
| t6 goto intent kind | 1 | sonnet subagent | `agent/t6` | new `reachy/behavior/goto_intent.py` |
| t9 #78 design note | 1 | sonnet subagent | `agent/t9` | new `docs/design/runtime-feed-export.md` |
| t3 pat sense provider | 2 | **opus** subagent | `agent/t3` | new `reachy/behavior/pat_sense.py` |
| t7 runtime composition | 3 | **opus** subagent | `agent/t7` | `reachy/cli/_commands/behavior.py` (`_compose_run_seam`) |
| t8 `behavior goto` verb | 4 | sonnet subagent | `agent/t8` | `reachy/cli/_commands/behavior.py`, `reachy/explain/catalog.py` |
| t10 docs + version | 5 | sonnet subagent | `agent/t10` | `CLAUDE.md`, `docs/operating-reachy.md`, `docs/export-schema.md`, `CHANGELOG.md` |
| t11 live-test (PR gate) | 5 | **main agent + operator** | — (deployed box) | none (evidence only) |

Wave 1's six tasks are file-disjoint and run as two batches of three under
the concurrency cap. t8 is serialized after t7 because both touch
`_commands/behavior.py`. Wave 5's two tasks are disjoint (docs vs on-box).

## Task content (summary · acceptance criteria · instruction · covers)

### Wave 1

**t1 — Composed-pose seam.** TickContext gains the streamed pose + a
`LastPoseHolder` TickBus driver (new `reachy/behavior/pose_feed.py`) that
stashes it for non-seam consumers to peek — one holder serving both the pat
comparison and `goto_lane`'s `start_pose_provider` continuity.

- TickContext carries `pose` (the exact dict streamed this tick); every
  existing tick_seam rider and test passes unchanged (additive extension).
- `LastPoseHolder.peek()` returns the pose streamed on the previous tick;
  before any tick it returns `None` and consumers degrade.
- GotoLane wired with the holder as `start_pose_provider` interpolates from
  the live pose (test: no neutral-snap C0 discontinuity).
- *Instruction:* add `pose=tick['pose']` in `engine._invoke_seam` + a pose
  field on TickContext (document: the exact dict streamed this tick, AFTER
  streaming). The holder is a plain bus driver (`callable(ctx)`) stashing
  `ctx.pose`. For goto continuity check `goto_lane`'s expected provider
  return type (a Contribution) and provide an adapter. Style:
  `test_behavior_engine_seam.py` patterns.
- *Covers:* c3, h3.

**t2 — Held media-free SDK state client.** ONE
`ReachyMini(media_backend='no_media')` held for the process lifetime,
constructed lazily with bounded retry/backoff, explicit idempotent close,
injected import seam for tests (new `reachy/robot/state_reader.py`).

- Construction passes `media_backend='no_media'` and happens at most once
  per retry window; a read NEVER constructs per call (test asserts single
  construction across N reads).
- Daemon-down at start: reads return `None` + one logged warning per state
  change, retry succeeds after the backoff without any restart (injected
  clock test).
- `close()` is idempotent and releases the client; a missing `[sdk]` extra
  degrades to a permanently-None reader with one warning (no crash, no
  retry storm).
- *Instruction:* mirror `SdkTransport._import`'s injected-import pattern;
  reuse `sdk_transport._euler_pitch_yaw` for the 4x4→pitch/yaw mapping;
  senselog one-per-state-change (connected/lost/retrying). Reproduce the
  challenge probe's evidence in tests.
- *Covers:* c4, c21, c23, h4, h17, h19.

**t4 — React rules bounded lifetime.** Validated `duration_s` field on react
rules; fail-closed refusal in `RulesConfig.from_dict` of a looping-default
target without it; `_build` applies the bounded Lifetime over library
defaults (`rules.py` + `rule_engine.py`).

- `duration_s <= 0` or non-numeric is refused; `duration_s=N` on a looping
  target admits `Lifetime(looping=True, duration=N)` (unit test on
  `_build`).
- A react rule targeting a looping-default entry WITHOUT `duration_s` is
  refused with a CliError naming the rule id, the entry, and the remedy —
  from `from_dict`, so boot and reload enforce identically.
- Every rules file valid before this change that uses only bounded targets
  still loads unchanged (the box's deployed rules.toml content as a
  fixture).
- *Instruction:* add `duration_s` to `_REACT_FIELDS` + the Rule dataclass
  (react-only), validate positive number; the library entry is already
  fetched in react validation — refuse `entry.looping` with
  `default_duration None` and no `duration_s`. `_build`: `duration_s`
  overrides the library default.
- *Covers:* c6, c7, h5, h6.

**t5 — run_behavior intent bounded refusal.** `_validated_lifetime`
(`intents.py`) refuses a looping-default entry when the payload carries no
explicit bounded lifetime — the same defect class as #76, agent surface.

- `run_behavior` naming a looping-default entry with no lifetime payload
  returns an error result (not admitted) naming the remedy; with
  `lifetime {duration: 5}` it admits and expires (driver test).
- Bounded-entry submissions and the other three kinds are byte-identical in
  behavior (existing intent tests green).
- *Instruction:* refuse when the RESULTING lifetime is unbounded
  (`looping=True`, `duration=None`) — equivalent to the payload rule but
  catches every path. Do NOT touch the other three kind handlers.
- *Covers:* c25, h21.

**t6 — Goto intent kind with boundary validation.** A KindRegistry handler
(new `reachy/behavior/goto_intent.py`) that validates channels/duration AND
refuses out-of-range targets against defined per-axis limits, builds a
GotoSpec, submits to an injected GotoLane — registered at composition, no
`control.py` edits.

- An out-of-range target (any axis) is refused with a specific error result
  naming the axis and limit — never submitted (test asserts `lane.submit`
  not called).
- A valid payload submits a GotoSpec matching the payload and returns the
  goto id in the applied result; unknown payload fields and
  non-positive/absurd durations are refused.
- Tests assert `control.py` and `intents.py` are NOT modified by this
  module (import-boundary test, mirroring the forge pattern).
- *Instruction:* handler `(payload, ctx) -> dict` closing over an injected
  GotoLane; per-axis clamp constants mirror `library.py`'s amplitude clamps
  with each limit's source cited; refuse (never silently clamp); duration
  `> 0` and `<= ~10 s`.
- *Covers:* c24, h20.

**t9 — #78 design note.** `docs/design/runtime-feed-export.md`: the
`--export-file` candidate with O_NONBLOCK FIFO semantics, JsonlExporter
self-disable reuse, unit-flag + panel.conf drop-in interaction, and the
FIFO boot-hang failure mode it must never reintroduce — design only,
implementation deferred to its own think/challenge pass.

- The note names the failure mode concretely (FIFO `open()` blocking at
  boot with no reader) and every candidate preserves stdout-purity +
  self-disable and needs no second engine process.
- The note states its design-only status and links #78; markdownlint green.
- *Covers:* c20, h10.

### Wave 2

**t3 — Pat sense provider.** New `reachy/behavior/pat_sense.py`:
PatDetector (`reachy/motion/pat.py` semantics) fed composed-vs-actual per
tick, OWNERSHIP-GATED — suspends while any non-base behavior owns the head
channel and re-baselines on resume; publishes `(kind, level)` via a
one-tick latch compatible with `SenseProviders.pat_event`'s peek contract.
Deps: t1, t2.

- A synthetic pat sequence (commanded steady, actual deviating in
  pitch/yaw) yields the expected `(scratch|side_pat, level)` event exactly
  once (one-tick latch, identical peeks within the tick).
- With a non-base owner on the head channel, the SAME deviation yields ZERO
  events, and the detector re-baselines on the first tick after ownership
  returns to base (the #66 false-fire test).
- A raising/None reader degrades to no-event (never an exception out of the
  provider); the pose unit/frame mapping to PatDetector's pitch/yaw degrees
  is documented in the module docstring.
- *Instruction:* prefer a bus DRIVER advancing PatDetector post-tick (ctx
  has ownership + the holder's pose) that stores a latched event;
  `SenseProviders.pat_event` peeks the latch (set at tick N, published in
  tick N+1's sense read, then cleared — exactly one composition sees it).
  Ownership gate: suspend while `ownership['head']` owner is any non-base
  behavior; `PatDetector.reset()` on resume. Precedents:
  `motion/listen_pat.py` PatHook, `sleep/patwake.py`. Resolves risks r1
  (pose mapping) and r2 (cadence) — document both in the module docstring.
- *Covers:* c2, c22, h2, h18.

### Wave 3

**t7 — Compose the runtime.** `_commands/behavior.py` `_compose_run_seam`
only: `SenseProviders(pat_event=provider)` wrapped over the DoaPoller via
`read_perception`; LastPoseHolder + GotoLane (with start-pose continuity)
as bus drivers; goto kind registered into the IntentDriver's registry;
SDK-less boxes degrade to today's behavior byte-identically. Deps: t1, t2,
t3, t6.

- With a fake SDK reader injected, the engine's sense snapshot carries
  pat_event and the feed publishes the sense block on change (composition
  test, no robot).
- A spool-submitted goto reaches GotoLane through the registered kind and
  emits `goto.admitted`/`goto.done` on the bus (composition test); no
  mapper changes anywhere in the diff.
- Without the `[sdk]` extra the composed seam is byte-identical to today;
  the diff contains no media-session open and no LLM client (boundary
  inspection tests).
- *Instruction:* try-import the SDK reader stack; on CliError/ImportError
  compose exactly today's seam. Driver order on the ONE TickBus: rules,
  intents (registry carrying the goto kind bound to the lane), pat driver,
  LastPoseHolder, GotoLane, then SenseSnapshotDriver when exporting.
- *Covers:* c8, h7, c18, h15.

### Wave 4

**t8 — `behavior goto` verb.** `_commands/behavior.py` +
`explain/catalog.py`: operator surface submitting a goto through the
reload-safe spool — target axes, `--duration`, `--json`, clean exit codes,
catalog entry + overview line. Deps: t7 (same file — serialized).

- `behavior goto --json` submits and prints the applied result incl. goto
  id; validation errors are exit-1 CliErrors with hint lines (never
  tracebacks).
- The catalog entry resolves (`test_every_catalog_path_resolves`) and
  `teken cli doctor --strict` stays green.
- *Instruction:* follow the existing behavior sub-verb registration
  pattern; flags mirror GotoSpec's friendly units; submission goes through
  the intent spool so the verb exercises the same path agents use.
- *Covers:* c14, h11.

### Wave 5

**t10 — Docs + version.** CLAUDE.md noun catalog,
`docs/operating-reachy.md`, `docs/export-schema.md` motion status line,
CHANGELOG via the version-bump skill. Deps: t4, t5, t8.

- CLAUDE.md behavior/noun rows and the operating guide describe the pat
  provider, `duration_s` + fail-closed (rules AND run_behavior intents),
  and `behavior goto`; export-schema motion Producer status updated (no
  schema change).
- The docs state that the pat reaction is the rules.toml rule, not code;
  version bumped + CHANGELOG entry via the version-bump skill; markdownlint
  green.
- *Instruction:* run `uv lock` with the version bump (the PR #33 lockfile
  gotcha — CI dies on `uv sync` re-resolve without it).
- *Covers:* c9, h8, c15, h12, c17, h14.

**t11 — Live-test pass on the box (the PR gate).** Main agent + operator at
the robot. Deploy the branch build to the tool env, restart
`reachy-runtime`, run all lanes, record the evidence before the PR opens.
Deps: t3, t4, t5, t7, t8.

- Physical pat: feed shows the sense pat event, rule fire, thoughtful
  admission, and the head visibly reacts — zero config change; gestures
  WITHOUT touching the head yield zero pat events (h18).
- Goto: submitted via spool and via `behavior goto`, admitted→done on the
  feed with real head motion; unbounded looping rule refused at reload with
  the clear error; a bounded rule's hold visibly expires (h5).
- Ops: fd count flat over a soak, unit stops cleanly on SIGTERM,
  daemon-race (runtime up before daemon) ends with pat working,
  single-presence respected throughout; all evidence recorded; any failed
  lane blocks the PR.
- *Instruction:* deploy `uv tool install --force '<repo>[daemon]'`, restart
  the unit (drop-in survives); `journalctl` for `[SENSE]` lines; for feed
  blocks stop the unit and run a bounded foreground
  `behavior engine run --export -` pass, then restore the unit (the
  stop-to-test/restore-after convention). The pat lane needs the operator
  at the robot. Rollback: reinstall the prior PyPI version.
- *Covers:* c1, h1, c12, h9, c16, h13, c19, h16.

## Risks carried into the run

- r1 (t3): pose unit/frame mapping — resolved in t3 against
  `motion/pat.py`'s expectations.
- r2 (t3): provider update cadence — decided in t3 (bus-driver + latch
  preferred).
- r3 (t6): daemon-side set_target clamping unverified — moot for the goto
  path once t6 refuses at the boundary.
- r4 (t11): pat threshold calibration vs the feel-alive amplitude —
  retuning during t11 is expected calibration, not plan drift.
- r5 (t11): the live-test needs a human at the robot and restarts the
  deployed presence; rollback is the prior PyPI version.
