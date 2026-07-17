# Build Plan — symbolic-runtime-70

slug: `symbolic-runtime-70` · status: `exported` · from frame: `symbolic-runtime-70`

> reachy-mini-cli is a symbolic runtime for Reachy: a deterministic on-box heartbeat runtime owns state, reactions, and scheduled behaviors with zero external AI services required; humans, scripts, and AI agents all operate it by declaring symbolic goals and rules — AI chooses intentions, the runtime sustains them, and lobes/model-gear become optional plug-ins (issue #70)

## Tasks

### t1 — t1 Perception snapshot: extend behavior/sense.py Sense with speech/RMS/pat/face fields + non-consuming provider seams

- instruction: Extend the frozen Sense dataclass in reachy/behavior/sense.py with None-default fields; providers are injected callables duck-typed like DoaPoller (no reachy_mini import — the module stays a dependency-free leaf). Non-consuming = peek semantics, mirroring VisionHook.latest_frame vs take().
- covers: c7, h4
- acceptance:
  - Sense gains rms, pat_event, face, frame_available fields with None-safe defaults; every existing sense/DoaPoller test stays green
  - providers are injected non-consuming taps: a test proves two consumers read the same tick sample and no provider opens a second media session or camera grabber

### t2 — t2 Rule schema + validation: data-only TOML rules (react/inhibit/mode) with cooldown/hysteresis fields, state_dir placement, last-good semantics

- instruction: New reachy/behavior/rules.py. Mirror reachy/stash/record.py's from_dict single-gate style (refuse-with-reasons, no exec/eval). Load via tomllib from reachy.daemon.state_dir()/behavior/rules.toml. Stdlib only.
- covers: c4, c24
- acceptance:
  - a single from_dict-style gate refuses unknown fields, non-JSON values, unknown behaviors/modes, and anything code-shaped, naming every rejection reason (mirrors stash/record.py)
  - schema includes per-rule cooldown_s and hysteresis fields with validated defaults; battery appears nowhere as field or example
  - loader imports stdlib only (tomllib), reads rules.toml under state_dir/behavior, and keeps the last good config when a candidate fails validation

### t3 — t3 Rule evaluation on the engine tick: react/inhibit/mode compiled onto arbitration, [SENSE stage=rule] lines, ONE injected event-callback seam in engine.py

- instruction: New reachy/behavior/rule_engine.py + exactly ONE hook in engine.py (an injected event-callback seam, a list of callables). Use reachy.senselog stage/drop with stage=rule. Determinism via the engine's existing injectable sleep/now/max_ticks. Rules map run-> library.build admission, inhibit-> eviction/annotation, mode-> config swap.
- depends on: t1, t2
- covers: c6, h3, h16, c25, h17
- acceptance:
  - a rules file demonstrably changes robot behavior in a bounded --ticks run with injected clock and zero LLM or network calls
  - a two-rule ping-pong fixture and an every-tick-refire fixture both settle under cooldown/hysteresis in deterministic runs
  - every rule fire, inhibition, suppression, and cooldown-skip emits a [SENSE stage=rule] line with rule id + reason; a stderr grep reconstructs every decision; a silent action is a test failure
  - engine.py gains exactly one injected event-callback seam that rule, goto, and export consumers ride — downstream tasks add no further engine.py edits

### t4 — t4 Boot resilience + reload verb: invalid rules degrade to base presence, never a crash loop; behavior reload applies between ticks

- instruction: Composition lives in reachy/cli/_commands/behavior.py's run path — no engine.py edits. Boot fallback: catch the CliError from rules load, log the [SENSE] rejection, seed feel-alive as usual. reload = a spool command (control.py atomic-rename idiom) applied between ticks.
- depends on: t2, t3
- covers: c23, h15
- acceptance:
  - a deliberately broken rules file driven through the unit entry path yields running base presence (feel-alive) plus a [SENSE] rejection naming every reason — no Restart=on-failure restart loop
  - behavior reload swaps config at a deterministic between-ticks point; a rejected reload keeps the last good config and reports the rejection; CLI-invoked validation failures stay clean exit-1/2 CliError

### t5 — t5 Goto lane folded into the engine: one-shot minjerk gotos as time-bounded stopping-class contributions under per-channel arbitration

- instruction: New reachy/behavior/goto_lane.py. Reuse the minjerk planning from reachy/motion; express a goto as a Lifetime-bounded stopping-class Behavior per arbitration.py semantics. Provide an adapter so MotionQueue-family callers submit unchanged (serial, one move at a time).
- depends on: t3
- acceptance:
  - a goto submitted via the spool executes as a time-bounded contribution; preemption is pinned by tests — a higher-priority stopping behavior interrupts an in-flight goto and the goto never resumes half-way
  - MotionQueue-family callers reach the engine lane through an adapter with unchanged serial submit semantics, proven in a bounded deterministic run

### t6 — t6 State surface: joints + pose read through the one SDK client seam, battery-free by construction

- instruction: New reachy/behavior/state.py with an injected transport/client seam like sense.py. SDK reads: get_current_joint_positions + head_pose. Add the repo-wide no-battery grep test here.
- covers: c21, h14
- acceptance:
  - a state snapshot module exposes joints (get_current_joint_positions) and head pose (head_pose) via the injected transport seam — mockable, no second SDK client
  - a repo-wide test asserts the word battery appears in no source, schema, or doc example introduced by the effort

### t7 — t7 Intent tools through the act-in spool: declare_goal / run_behavior / set_mode / set_inhibition persist in the runtime

- instruction: Follow reachy/speech/tools.py's JSON-schema tool pattern (apply_pose enum style). New command kinds land in behavior/control.py (atomic-rename spool); the engine applies them in its drain step; state.json is the read-back surface.
- depends on: t3
- covers: c8, h5
- acceptance:
  - each intent tool writes an atomic spool command (control.py idiom); the engine applies it and state.json reflects the sustained intent
  - an intent declared once is still being sustained many ticks later with no further agent calls, observable via behavior status --json

### t8 — t8 Runtime events feed: perception/rule/intent/motion JSONL export riding the engine event seam

- instruction: Extend reachy/export/ (events.py model + exporter.py) with runtime event types; wire through t3's event seam at composition. Keep JsonlExporter's disconnect-safe self-disable pattern and the stdout-pure/stderr-banner split.
- depends on: t3
- covers: h11
- acceptance:
  - behavior run --export - emits a documented runtime-event JSONL feed (stdout pure, banners to stderr), disconnect-safe like the existing exporter
  - a rules-firing run feed plus logs contain zero LLM-call events — the zero-token property is verifiable from the feed alone

### t9 — t9 CLI surface on the behavior noun: rules list/check, reload, extended status — catalog entries + rubric green

- instruction: Follow the _commands verb pattern (whoami.py canonical; cli.py for noun groups — parser_class=type(p), overview verb). Add explain/catalog.py ENTRIES keys for every new path; run teken locally before finishing.
- depends on: t3, t4, t6
- covers: c10, h8
- acceptance:
  - behavior rules, behavior rules check, behavior reload, and extended behavior status each take --json, follow the CliError contract, carry explain catalog ENTRIES keys, and have bounded deterministic tests
  - teken cli doctor . --strict passes

### t10 — t10 Runtime boot unit + three-way single presence: RUNTIME_UNIT in units.py, ServiceManager exclusion set, service enable runtime

- instruction: units.py: pure renderer function + RUNTIME_UNIT canonical-name const, Requires=/After= the daemon unit like the siblings. manager.py: extend the exclusion set; every side effect through the injected run/unit_dir/daemon_health seams; mirror the existing ServiceManager tests.
- depends on: t9
- covers: c19, h6, c20, h13
- acceptance:
  - RUNTIME_UNIT ExecStart runs the AI-agnostic behavior runtime — no LLM flag or REACHY_OPENAI reference anywhere in the unit text
  - after any sequence of enables at most one of demo|live|runtime is enabled and both siblings are disabled, proven via the injected-run seam tests
  - install/uninstall write/remove all four units + daemon-reload without enabling anything

### t11 — t11 External agent client: attach over the seams — read the runtime feed, act via intent tools, publish its own cognition feed

- instruction: New command module composing AgentTurnEngine + the t7 intent tools as its ToolRegistry; subscribe to the runtime feed (spawn behavior run --export - or read the documented stream); publish the agent's own cognition feed via the existing export family. Forge stays composition-injected (never imported by tools/agent_turn).
- depends on: t7, t8
- covers: c16, h10
- acceptance:
  - with the runtime running, the agent client attaches (reads runtime events, acts through intent tools) with no unit edit and no loop restart
  - the agent publishes its own thinking/message/emotion feed; the runtime feed carries no cognition blocks (c27)
  - detaching the agent changes nothing about the loop: runtime ticks and rules continue, proven in a bounded run

### t12 — t12 Offline CI lane: the success list proven with every endpoint unreachable, plus the dep-freeze check

- instruction: pytest marker (e.g. offline) + fixture pointing every REACHY_*_URL/FORGE_BASE_URL at an unreachable port + a guard that fails on any socket connect. New CI job in .github/workflows/tests.yml running only that marker. Dep-freeze: parse pyproject.toml and assert the exact two-element dependency list.
- depends on: t3, t4, t5, t6
- covers: c1, c5, h1, h2, h12, c18, h7
- acceptance:
  - a pytest lane runs with all service endpoints pointed at unreachable addresses: boot, breathe, orient-to-sound, pat, sleep/wake, and rules paths all green; the lane fails if any of those paths performs a network call
  - the lane asserts project.dependencies equals exactly numpy + harmonics-cli

### t13 — t13 Operating guide: the symbolic runtime chapter — three client entry paths, the two-feed contract, the zero-token rationale

- instruction: New chapter in docs/operating-reachy.md (+ runtime-feed section in docs/export-schema.md, README pointer). Three end-to-end client walkthroughs. Keep markdownlint-cli2 green; verify anchors with github-slugger conventions.
- depends on: t9, t11
- covers: c15, h9, c17
- acceptance:
  - the guide demonstrates each client class end-to-end: a human via behavior verbs, a script via --json + exit codes, an agent via the attach client
  - the guide documents the two-feed contract (runtime events vs agent cognition), the kept-legs-as-optional-plugins posture, and the zero-token rationale with its verification recipe

### t14 — t14 Tick-budget observability: measure tick duration, log overruns loudly

- instruction: Tick timing wraps the rule_engine tick via the same injected now seam; emit [SENSE stage=rule event=overrun] with measured ms. Synthetic slow tick = an injected clock that jumps; assert zero overrun lines on a normal bounded run.
- depends on: t5
- acceptance:
  - the engine counts tick overruns and emits a [SENSE] line with the measured duration; a synthetic slow-tick test proves the line appears and a normal bounded run emits none

## Risks

- [unknown_nonblocking] goto-vs-stream preemption mechanics (park v4): interrupting an in-flight minjerk goto with a streamed or stopping contribution has no prior design — t5 pins semantics by tests, but the choice may need hardware iteration (task t5)
- [follow_up] deployed-box migration: the real robot currently runs the agent-folded live unit; shipping requires disable live, install units, daemon-reload, enable runtime, and a manual on-robot reboot verify (task t10)
- [unknown_nonblocking] agent process supervision (park v3): who runs the attached agent — a fourth unit, on-demand invocation, or operator-managed — is decided during t11 and may become its own unit later (task t11)
- [unknown_nonblocking] the 20 ms tick budget on real hardware with rules + full perception aboard is unmeasured (assumption c22): t14 makes an overrun visible, but a real overrun may force rate or rules-eval re-tuning (task t14)
- [follow_up] reTerminal bridge repoint: the out-of-repo panel bridge must move from the folded live feed to the agent-published cognition feed (c27) — a consumer change this repo cannot ship
