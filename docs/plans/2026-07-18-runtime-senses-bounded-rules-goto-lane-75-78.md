# Build Plan — runtime senses + bounded rules + goto lane (75-78)

slug: `runtime-senses-bounded-rules-goto-lane-75-78` · status: `exported` · from frame: `runtime-senses-bounded-rules-goto-lane-75-78`

> The boot presence feels a pat and reacts through its rules, react rules can no longer hold a channel forever, gotos submit into the live running engine, and the reTerminal repoint has a safe feed design — all live-verified on the robot before PR.

## Tasks

### t1 — Composed-pose seam: TickContext gains the streamed pose + a LastPoseHolder TickBus driver (new reachy/behavior/pose_feed.py) that stashes it for non-seam consumers to peek — the one holder serving both the pat comparison and goto_lane's start_pose_provider continuity

- instruction: Add pose=tick['pose'] in engine._invoke_seam + a pose field on TickContext (document: the exact dict streamed this tick, AFTER streaming). New reachy/behavior/pose_feed.py: LastPoseHolder is a plain bus driver (callable(ctx)) stashing ctx.pose; peek() returns it. For goto continuity check goto_lane's start_pose_provider expected return type (a Contribution) and provide an adapter from the pose dict. Style: test_behavior_engine_seam.py patterns.
- covers: c3, h3
- acceptance:
  - TickContext carries pose (the exact dict streamed this tick); every existing tick_seam rider and test passes unchanged (additive extension)
  - LastPoseHolder.peek() returns the pose streamed on the previous tick; before any tick it returns None and consumers degrade
  - GotoLane wired with the holder as start_pose_provider interpolates from the live pose (test: no neutral-snap C0 discontinuity)

### t2 — Held media-free SDK state client (new reachy/robot/state_reader.py): ONE ReachyMini(media_backend='no_media') held for the process lifetime, constructed lazily with bounded retry/backoff, explicit idempotent close, injected import seam for tests

- instruction: Mirror SdkTransport._import's injected-import pattern; construct ReachyMini(media_backend='no_media') lazily on first read, at most once per backoff window (injected clock). Reuse sdk_transport._euler_pitch_yaw for the 4x4->pitch/yaw mapping. close() tries close/disconnect and tolerates absence. Log via senselog one-per-state-change (connected/lost/retrying). The probe evidence to reproduce in tests: single construction across N reads, None during outage, recovery after backoff.
- covers: c4, c21, c23, h4, h17, h19
- acceptance:
  - Construction passes media_backend='no_media' and happens at most once per retry window; a read NEVER constructs per call (test asserts single construction across N reads)
  - Daemon-down at start: reads return None + one logged warning per state change, retry succeeds after the backoff without any restart (injected clock test)
  - close() is idempotent and releases the client; a missing [sdk] extra degrades to a permanently-None reader with one warning (no crash, no retry storm)

### t3 — Pat sense provider (new reachy/behavior/pat_sense.py): PatDetector (reachy/motion/pat.py semantics) fed composed-vs-actual per tick, OWNERSHIP-GATED — suspends while any non-base behavior owns the head channel and re-baselines on resume; publishes (kind, level) via a one-tick latch compatible with SenseProviders.pat_event's peek contract

- instruction: Cadence decision (risk r2): prefer a bus DRIVER advancing PatDetector post-tick (ctx has ownership + the holder's pose) that stores a latched event; SenseProviders.pat_event peeks the latch (set at tick N, published in tick N+1's sense read, then cleared — exactly one composition sees it). Ownership gate: suspend while ownership['head'] owner is any non-base behavior; PatDetector.reset() on resume (the #66 test). Precedents: motion/listen_pat.py PatHook, sleep/patwake.py. Resolve risk r1 (pose dict head axes -> detector pitch/yaw degrees) in the module docstring.
- depends on: t1, t2
- covers: c2, c22, h2, h18
- acceptance:
  - A synthetic pat sequence (commanded steady, actual deviating in pitch/yaw) yields the expected (scratch|side_pat, level) event exactly once (one-tick latch, identical peeks within the tick)
  - With a non-base owner on the head channel, the SAME deviation yields ZERO events, and the detector re-baselines on the first tick after ownership returns to base (the #66 false-fire test)
  - A raising/None reader degrades to no-event (never an exception out of the provider); the pose unit/frame mapping to PatDetector's pitch/yaw degrees is documented in the module docstring

### t4 — React rules bounded lifetime (reachy/behavior/rules.py + rule_engine.py): validated duration_s field on react rules; fail-closed refusal in RulesConfig.from_dict of a looping-default target without it; _build applies the bounded Lifetime over library defaults

- instruction: rules.py: add duration_s to _REACT_FIELDS + Rule dataclass (react-only), validate positive number; in from_dict's react validation the library entry is already fetched — refuse entry.looping and entry.default_duration None without duration_s (name rule id, entry, remedy). rule_engine._build: duration_s overrides -> Lifetime(looping=entry.looping, duration=rule.duration_s or entry.default_duration). Fixture: the box's deployed rules.toml verbatim (bounded targets only) must load unchanged.
- covers: c6, c7, h5, h6
- acceptance:
  - duration_s <= 0 or non-numeric is refused; a react rule with duration_s=N on a looping target admits Lifetime(looping=True, duration=N) (unit test on _build)
  - A react rule targeting a looping-default entry WITHOUT duration_s is refused with a CliError naming the rule id, the entry, and the remedy — from from_dict, so boot and reload enforce identically (existing loader tests still green)
  - Every rules file valid before this change that uses only bounded targets still loads unchanged (the box's deployed rules.toml content as a test fixture)

### t5 — run_behavior intent bounded refusal (reachy/behavior/intents.py): _validated_lifetime refuses a looping-default entry when the payload carries no explicit bounded lifetime — same defect class as #76, agent surface

- instruction: intents.py _validated_lifetime: refuse when the RESULTING lifetime is unbounded (looping=True, duration=None) — equivalent to c25's payload rule but catches every path; error result names the entry and the bounded-lifetime remedy. Do NOT touch the other three kind handlers.
- covers: c25, h21
- acceptance:
  - run_behavior naming a looping-default entry with no lifetime payload returns an error result (not admitted) naming the remedy; with lifetime {duration: 5} it admits and expires (driver test)
  - Bounded-entry submissions and the other three kinds are byte-identical in behavior (existing intent tests green)

### t6 — Goto intent kind with boundary validation (new reachy/behavior/goto_intent.py): a KindRegistry handler that validates channels/duration AND clamps-or-refuses targets against defined per-axis limits, builds a GotoSpec, submits to an injected GotoLane — registered at composition, no control.py edits

- instruction: New reachy/behavior/goto_intent.py: handler(payload, ctx)->dict closing over an injected GotoLane. Define per-axis clamp constants mirroring reachy/behavior/library.py's amplitude clamps (head mm/deg, antennas deg, body_yaw deg) — cite each limit's source in a comment. Refuse (do not clamp silently) out-of-range targets: error result naming axis + limit. Duration: >0 and <= a sane cap (~10s). Import-boundary test: control.py and intents.py unmodified (forge-pattern assertion).
- covers: c24, h20
- acceptance:
  - An out-of-range target (any axis) is refused with a specific error result naming the axis and limit — never submitted to the lane (test asserts lane.submit not called)
  - A valid payload submits a GotoSpec matching the payload and returns the goto id in the applied result; unknown payload fields and non-positive/absurd durations are refused
  - tests assert reachy/behavior/control.py and intents.py are NOT modified by this module (import-boundary test, mirroring the forge pattern)

### t7 — Compose the runtime (_commands/behavior.py _compose_run_seam): SenseProviders(pat_event=provider) wrapped over the DoaPoller via read_perception; LastPoseHolder + GotoLane (with start-pose continuity) as bus drivers; goto kind registered into the IntentDriver's registry; SDK-less boxes degrade to today's behavior byte-identically

- instruction: _compose_run_seam only: try-import the SDK reader stack; on CliError/ImportError compose exactly today's seam (byte-identical, test asserts). Wrap: sense_reader = lambda t: read_perception(providers, base=doa_poller(t)). Driver order on the ONE TickBus: rules, intents (with registry carrying the goto kind bound to the lane), pat driver, LastPoseHolder, GotoLane, then SenseSnapshotDriver when exporting. Boundary tests: no media_session call, no reachy.speech.llm import anywhere in the new composition.
- depends on: t1, t2, t3, t6
- covers: c8, h7, c18, h15
- acceptance:
  - With a fake SDK reader injected, the engine's sense snapshot carries pat_event and the feed publishes the sense block on change (composition test, no robot)
  - A spool-submitted goto reaches GotoLane through the registered kind and emits goto.admitted/goto.done on the bus (composition test); no mapper changes anywhere in the diff
  - Without the [sdk] extra the composed seam is byte-identical to today (no provider, no holder consumers crash); the diff contains no media-session open and no LLM client (boundary inspection tests)

### t8 — behavior goto verb (_commands/behavior.py + explain/catalog.py): operator surface submitting a goto through the reload-safe spool — target axes, --duration, --json, clean exit codes, catalog entry + overview line

- instruction: Follow the existing behavior sub-verb registration pattern in _commands/behavior.py; flags mirror GotoSpec's friendly units (head axes, antennas, body_yaw, --duration); submission goes through the intent spool (control.CommandSpool submit) so the verb exercises the same path agents use. catalog.py ENTRIES key ('behavior','goto') + overview line; run teken cli doctor . --strict locally before done.
- depends on: t7
- covers: c14, h11
- acceptance:
  - behavior goto --json submits and prints the applied result incl. goto id; validation errors are exit-1 CliErrors with hint lines (never tracebacks)
  - The catalog entry resolves (test_every_catalog_path_resolves) and teken cli doctor --strict stays green

### t9 — #78 design note (docs/design/runtime-feed-export.md): the --export-file candidate with O_NONBLOCK FIFO semantics, JsonlExporter self-disable reuse, unit-flag + panel.conf drop-in interaction, and the FIFO boot-hang failure mode it must never reintroduce — design only, implementation deferred to its own think/challenge pass

- instruction: Location docs/design/runtime-feed-export.md. Cover: --export-file with O_NONBLOCK FIFO open (ENXIO -> self-disable + periodic retry), plain-file rotation/size-cap alternative, JsonlExporter self-disable reuse, unit flag vs the machine-local panel.conf drop-in (drop-ins override ExecStart — the flag must land THERE on the box), and the hard constraint: never a blocking open on the boot path. State design-only + link #78. markdownlint-cli2 clean.
- covers: c20, h10
- acceptance:
  - The note names the failure mode concretely (FIFO open() blocking at boot with no reader) and every candidate preserves stdout-purity + self-disable and needs no second engine process
  - The note states its design-only status and links #78; markdownlint green

### t10 — Docs + version (CLAUDE.md noun catalog, docs/operating-reachy.md, docs/export-schema.md motion status line, CHANGELOG via version-bump): document the pat sense, bounded rules (both surfaces), the goto path + verb, and the rule-not-code thesis

- instruction: CLAUDE.md: behavior noun row + Hard-constraints touchpoints for the pat provider + bounded admissions; docs/operating-reachy.md: pat sense section (rule-not-code thesis), bounded-rules section (both surfaces), goto path + verb; docs/export-schema.md: motion block Producer status only. Version: use the version-bump skill AND run uv lock (the PR #33 lockfile gotcha — CI dies on uv sync re-resolve without it).
- depends on: t4, t5, t8
- covers: c9, h8, c15, h12, c17, h14
- acceptance:
  - CLAUDE.md behavior/noun rows and the operating guide describe pat provider, duration_s + fail-closed (rules AND run_behavior intents), and behavior goto; export-schema motion Producer status updated (no schema change)
  - The docs state that the pat reaction is the rules.toml rule, not code; version bumped + CHANGELOG entry via the version-bump skill; markdownlint green

### t11 — Live-test pass on the box (PR gate): deploy branch build to the tool env, restart reachy-runtime, run all lanes — physical pat (armed rule, zero config), spool + verb goto, unbounded-rule refusal, bounded expiry, fd/SIGTERM soak, daemon-race — and record the evidence before the PR opens

- instruction: Deploy: uv tool install --force '<repo-checkout>[daemon]' then systemctl --user restart reachy-runtime (drop-in survives). Evidence: journalctl --user -u reachy-runtime -f for [SENSE] lines; for feed blocks stop the unit and run a bounded foreground 'behavior engine run --export -' pass, then restore the unit (the stop-to-test/restore-after convention). Pat lane needs Ori at the robot. fd soak: watch /proc/<pid>/fd count. SIGTERM: time systemctl stop. Daemon race: stop daemon, restart runtime, start daemon, verify pat revives. Record every lane's evidence in the delivery notes BEFORE the PR opens; any failure blocks the PR (h9).
- depends on: t3, t4, t5, t7, t8
- covers: c1, h1, c12, h9, c16, h13, c19, h16
- acceptance:
  - Physical pat: feed shows the sense pat event, rule fire, thoughtful admission, and the head visibly reacts — zero config change; gestures WITHOUT touching the head yield zero pat events (h18)
  - Goto: submitted via spool and via behavior goto, admitted->done on the feed with real head motion; unbounded looping rule refused at reload with the clear error; a bounded rule's hold visibly expires (h5)
  - Ops: fd count flat over a soak, unit stops cleanly on SIGTERM, daemon-race (runtime up before daemon) ends with pat working, single-presence respected throughout; all evidence recorded; any failed lane blocks the PR

## Risks

- [unknown_nonblocking] Pose unit/frame mapping (park v1): the engine's composed pose dict vs PatDetector's pitch/yaw degrees — t3 must define the mapping against motion/pat.py's expectations before thresholds mean anything (task t3)
- [unknown_nonblocking] Provider update cadence (park v2): detector advance inside the once-per-tick sense read vs a dedicated bus driver stashing for a pure peek — t3 decides; read_perception re-reads providers if called twice in a tick (task t3)
- [unknown_nonblocking] Daemon-side set_target clamping is unverified (park v3) — deliberately not probed (would command real motion); t6's boundary validation makes it moot for the goto path (task t6)
- [unknown_nonblocking] Pat threshold calibration vs the feel-alive base layer's amplitude (park v4): only tunable on live hardware — retuning during t11 is expected calibration, not plan drift (task t11)
- [unknown_nonblocking] The live-test needs a human physically at the robot (the pat lane) and restarts the deployed presence; rollback is reinstalling the prior PyPI version into the tool env (task t11)
