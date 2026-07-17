# Delivery Summary — symbolic-runtime-70

plan: `symbolic-runtime-70` · run: `complete` · date: `2026-07-17`
baseline: `devague summary skeleton`

## Intent

Execute the converged symbolic-runtime-70 plan (14 tasks, 5 dependency waves,
seeded from the challenged frame for issue #70) via /assign-to-workforce:
turn the CLI into a deterministic, AI-agnostic symbolic runtime — rules,
intents, a runtime events feed, an external agent attach client, and a
boot unit — with an offline CI lane proving the zero-service property, then
live-verify on the real robot (deviation `d1`) before the PR.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — t1 Perception snapshot: extend behavior/sense.py Sense with speech/RMS/pat/face fields + non-consuming provider seams
- `t2` — t2 Rule schema + validation: data-only TOML rules (react/inhibit/mode) with cooldown/hysteresis fields, state_dir placement, last-good semantics
- `t3` — t3 Rule evaluation on the engine tick: react/inhibit/mode compiled onto arbitration, [SENSE stage=rule] lines, ONE injected event-callback seam in engine.py
- `t4` — t4 Boot resilience + reload verb: invalid rules degrade to base presence, never a crash loop; behavior reload applies between ticks
- `t5` — t5 Goto lane folded into the engine: one-shot minjerk gotos as time-bounded stopping-class contributions under per-channel arbitration
- `t6` — t6 State surface: joints + pose read through the one SDK client seam, battery-free by construction
- `t7` — t7 Intent tools through the act-in spool: declare_goal / run_behavior / set_mode / set_inhibition persist in the runtime
- `t8` — t8 Runtime events feed: perception/rule/intent/motion JSONL export riding the engine event seam
- `t9` — t9 CLI surface on the behavior noun: rules list/check, reload, extended status — catalog entries + rubric green
- `t10` — t10 Runtime boot unit + three-way single presence: RUNTIME_UNIT in units.py, ServiceManager exclusion set, service enable runtime
- `t11` — t11 External agent client: attach over the seams — read the runtime feed, act via intent tools, publish its own cognition feed
- `t12` — t12 Offline CI lane: the success list proven with every endpoint unreachable, plus the dep-freeze check
- `t13` — t13 Operating guide: the symbolic runtime chapter — three client entry paths, the two-feed contract, the zero-token rationale
- `t14` — t14 Tick-budget observability: measure tick duration, log overruns loudly

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `Sense` gained `rms`/`pat_event`/`face`/`frame_available` + `SenseProviders` peek seams; 15 tests |
| `t2` | delivered | `reachy/behavior/rules.py` — stash-style no-exec gate, cooldown/hysteresis fields, last-good `RulesLoader`; 51 tests |
| `t3` | delivered | `reachy/behavior/rule_engine.py` + the single engine seam (`TickContext`/`TickBus`); ping-pong and refire fixtures settle; 39 tests |
| `t4` | delivered | `ReloadDriver` + `behavior reload`; broken rules boot to base presence with a named `[SENSE]` rejection; 17 tests |
| `t5` | delivered | `reachy/behavior/goto_lane.py` — stoppable time-bounded gotos, force-evict no-resume preemption, MotionQueue-shaped adapter; 18 tests |
| `t6` | delivered | `reachy/behavior/state.py` (joints + pose via injected seam) + repo-wide no-battery guard; 137 tests |
| `t7` | delivered | Namespaced spool + `KindRegistry` in `control.py`, `IntentDriver`, four JSON-schema intent tools; 55 tests |
| `t8` | delivered | `reachy/export/runtime.py` + `behavior engine run --export -`; runtime feed disjoint from cognition blocks; 43 tests |
| `t9` | delivered | `behavior rules`/`rules check`/`rules overview`, extended `status` (rules health + live intents); teken 26/26; 26 tests |
| `t10` | delivered | `reachy-runtime.service` (LLM-free ExecStart proven by test) + three-way single presence, 9-transition exclusion matrix; 33 tests |
| `t11` | delivered | `agent` noun — attach client over feed + intent spool, publish-only speech tools, own cognition feed; 21 tests |
| `t12` | delivered | `pytest -m offline` lane (socket guard, endpoints unreachable, success list green) + dep-freeze; CI `offline` job passes on PR #74 |
| `t13` | delivered | Operating-guide chapter (three client walkthroughs, two-feed contract, zero-token recipe) + export-schema runtime section |
| `t14` | delivered | `TickMetrics` seam wrapper + `[SENSE ... event=overrun]`; wired into `cmd_engine_run`; 11 tests |

## Mid-work Decisions

- `d1` — add a pre-PR live-testing stage on the real robot: three escalating
  passes — sonnet subagent, then opus subagent, then the main agent — each
  exercising motion control (safe per operator), camera frame capture (may be
  black due to lighting), harmonic voice + microphone, motor/joint state
  read-back, and the new runtime surfaces (rules, intents, reload, export
  feed) — the offline CI lane alone does not prove the build against the
  physical robot; live verification must precede the PR. Recorded in the
  delivery ledger, documented in issue #72, executed in full.
- Post-wave composition commit (no `dN` record; orchestrator reconcile): t13's
  empirical doc audit found three integration gaps between independently
  green tasks — `behavior engine run` wired no live sense source, did not
  compose `IntentDriver` onto its bus, and never installed logging; the
  intent/goto emit names also missed the feed mapper. Closed in one commit
  with six end-to-end composition tests (`tests/test_behavior_engine_composition.py`).
- `control.py` single-ownership was assigned to `t7` mid-wave (with `t5`
  redesigned as a pure seam driver) to prevent a same-wave file collision the
  plan's file-disjointness review had missed.
- `t11` registered `speak`/`harmonics`/`apply_pose` publish-only (inert
  actuation seams) so the external client can emit cognition blocks without
  becoming a second SDK owner — all real actuation goes through the intent
  spool.
- `t7` placed intent commands in a separate namespaced spool
  (`behavior/intents/`) because the base spool drains straight into
  `Engine.apply`, which would silently reject unknown ops — a hard
  single-reader conflict, not a style choice.
- Two pre-existing repo issues were fixed/handled in passing with evidence:
  three untracked files swept in by a `git add -A` were removed from the
  branch (commit `f84071f`), and a pre-existing `MD050` lint break on main
  (bare `__builtins__` in the 0.34.0 changelog entry) was backticked
  (commit `02e100f`).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t12` (`d1`) | operator instruction mid-run: the offline CI lane alone does not prove the build against the physical robot; live verification must precede the PR | acceptable |
| `t8` | export flags landed on `behavior engine run` (the actual 50 Hz loop) rather than the acceptance criterion's literal `behavior run` (a one-shot spool submit with no loop to export) — deliberate interpretation, reported by the task agent | acceptable |
| `t7` | one additive method (`RuleEngine.set_active_mode`) was added to frozen-for-the-wave `rule_engine.py` via the brief's explicit escape hatch — `set_mode` otherwise had nothing to act on | acceptable |
| `t9` | an explicit `rules list` verb (and catalog entry) was added beyond the named surface, mirroring the `think expressions` sub-noun pattern the brief cited | acceptable |

No other task diverged from its contract; the task-by-task accounting above
covers all 14.

## Evidence

- tests: full suite `uv run pytest -n auto` — **2809 passed, 1 skipped** (the
  skip is a pre-existing, documented live-model quirk); baseline before the
  run was 2318 passed
- tests: `uv run pytest -m offline` — 10 passed (and the `offline` CI job
  passes on PR #74)
- tests: `tests/test_behavior_engine_composition.py` — 6 passed (the
  composition-gap closure)
- rubric: `uv run teken cli doctor . --strict` — 26/26
- lint: black / isort / flake8 / bandit — clean; `markdownlint-cli2` — clean
  after the `MD050` fix
- live (deviation `d1`, three passes): rules fired on real ambient speech;
  `behavior reload` hot-swapped a rule's behavior mid-run and rejected a
  broken file by name while running; `set_mode`/`declare_goal` intents
  applied live (`ok: true`) and were visible in `behavior status` during the
  run; harmonic voice played; mic RMS/DoA alive; joint/pose read-back real;
  **0 tick overruns across ~8500+ real 50 Hz ticks** (live evidence for
  assumption c22 / risk r4)
- commits: `be1c1e6..02e100f` on `feat/symbolic-runtime-70` (spec+plan, 14
  `--no-ff` task merges, composition, version 0.35.0, lint fixes)
- PRs / issues: PR #74 · issues #70 (the feature), #72 (deviation d1),
  #73 (live-test findings)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A declarative rules file changes real robot behavior with zero LLM calls, live-reloadable without restart | high | live pass 2 (rule fired `nod`, then `antenna-sway` after reload, on real ambient sound) · `tests/test_behavior_rule_engine.py` · `tests/test_behavior_engine_composition.py` |
| Agents act through durable intents the runtime sustains; attach/detach changes nothing about the loop | high | live pass 3 (`declare_goal` → `ok: true`, antennas swaying) · `tests/test_agent.py` · `tests/test_behavior_intents.py` |
| The runtime runs with every AI service unreachable | high | `pytest -m offline` under a socket guard, green locally and in CI (PR #74) |
| The runtime feed carries only runtime events; cognition publishes separately (c27) | high | `tests/test_export_runtime.py` (block sets disjoint) · live pass 3 feed (`intent`/`sense`/`rule` only) |
| The 20 ms tick budget holds on real hardware (assumption c22) | high | 0 overruns across ~8500+ live 50 Hz ticks (passes 2+3), `TickMetrics` counting |
| The boot unit is AI-agnostic and single presence holds three ways | high | `tests/test_service_units_runtime.py` (no-LLM-substring test) · `tests/test_service_manager_runtime.py` (9-transition matrix) |
| The deployed robot boots into the runtime by default | unverified | the unit ships but is not enabled on the box — the deployed-box migration (plan risk r2) is a manual on-robot step, not claimed done |

## Remaining Work / Follow-up

- Deployed-box migration (plan risk r2): `service install` + disable live +
  `enable runtime` + reboot verify on the real robot — manual operator step.
- `rms`/`pat`/`face` live providers for the standalone runtime process (the
  SDK media session currently belongs to the `listen --live` loop; the
  `SenseProviders` seams are the designed attach point) — documented in the
  guide's Status callouts.
- A live goto submission path (the goto lane ships as adapter + seam driver;
  nothing composes it into `behavior engine run` yet).
- Issue #73: the pre-existing `vision run` None-frame crash (root-caused),
  a `behavior status` staleness/liveness marker, and two wrong `--transport`
  help-text defaults.
- reTerminal bridge repoint to the agent's own cognition feed (plan risk r5 —
  an out-of-repo consumer change).
- Agent process supervision (plan risk r3): who runs the attached agent
  (a fourth unit / on-demand / operator) — decided when the attach client
  gets a deployment story.
