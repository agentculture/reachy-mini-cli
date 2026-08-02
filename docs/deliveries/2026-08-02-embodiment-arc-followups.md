# Delivery Summary — embodiment arc followups

plan: `embodiment-arc-followups` · run: `complete` · date: `2026-08-02`
baseline: `devague summary skeleton`

## Intent

Close out the findings left by the embodiment-layer arc: eleven open issues
spanning a runtime the operator was being bombarded by, two silent-failure
classes on the deployed robot, a stop path that could SIGKILL an innocent
process, a broken model default, and ~300 lint suppressions that suppressed
nothing. The plan converged through the full devague chain (`/scope` →
`/think` → `/challenge` → `/spec-to-plan`) into 15 tasks over 7 waves, fanned
out to parallel agents in isolated worktrees under a TDD merge gate. A
sixteenth task was added mid-run at the operator's request.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — #133: enforce `MAX_SAY_CHARS` in speech/tools.py speak/harmonics text validation, fail-closed
- `t2` — #134: name the 404 handshake 'realtime-lane-unavailable' in the transcriber hearing leg
- `t3` — #136: exact-token/basename PID guards for alive.py and daemon.py via procsup; close #136
- `t4` — #132: executable model defaults name gateway roles — forge qwen3->cortex, scene served-id->senses; close #132
- `t5` — #135: capture and fix the wedged-consumer tee flake at cause
- `t6` — #143a: typed alert-vs-context cue classification in embody/cues.py, routed identically by both intakes
- `t7` — #143b: EmbodyTurnEngine three-class policy — context park, alert containment, observability, replay acceptance
- `t8` — #137: attribute the sustained 5% tick overrun live (FaceSenseDriver composed vs not); archive evidence; fix or re-scope
- `t9` — #138: named latched camera-stream-ended staleness drop (detect only, no recovery)
- `t10` — #141/S107: frozen per-engine Limits dataclass for EmbodyTurnEngine and RealtimeDuplexSession bounds
- `t11` — #139/h9: wire the clip->ask() perception lane as context, never a trigger
- `t12` — land scripts/`embody_bus_feed.py` with tests and operating-guide docs
- `t13` — #142: strip the 296 inert noqa markers, keep the 20 real ones and all prose (runs last, alone)
- `t14` — durable enablement on the box: env repoint to role names, operator-local units, linger, guide; live acceptance
- `t15` — arc closure: framing verification, boundary suites unchanged, evidence archived, issues closed

`t16` is **not** in the plan — it was added mid-run under deviation `d3` and is
accounted for below.

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `MAX_SAY_CHARS` enforced in `_require_text`, fail-closed, never truncating. Import made **function-local** (`d1`). 7 tests. |
| `t2` | delivered | `REASON_LANE_UNAVAILABLE` ported from the duplex peer; 404 named, backoff and latch unchanged. Fake-server `ROLE_INFEASIBLE` scenario already existed. |
| `t3` | delivered | `procsup.has_argv_basename` added; `alive.py` and `daemon.py` migrated. Defect proven end-to-end: pre-fix, `daemon.stop()` delivered SIGTERM **and** SIGKILL to a real innocent bystander. |
| `t4` | delivered | forge `qwen3`→`cortex`, scene served-id→`senses`; both verified against the live gateway. Repo-wide sweep test bans served-id defaults. |
| `t5` | delivered | Flake **captured** (24/24 identical failures over 2400 repeats): the test read `caplog` once after waiting on a counter the worker bumps *before* emitting the line. Test fixed; `audio_tee.py` unchanged. 30/30 clean runs. |
| `t6` | delivered | `CueClass` / `ClassifiedCue` / `classify_runtime_event`; classification decided at the mapper from the event **type**, never re-sniffed from text. `runtime_cues.py` untouched. |
| `t7` | delivered | Three-class policy: triggers deque + coalescing context park keyed on rendered cue text; alert containment (whole-drain + 5 s min interval, alerts **deferred not dropped**); drained counts on journal and export. |
| `t8` | delivered | Overrun **attributed** by natural experiment and fixed: frame read gated at 10 Hz. Live post-fix verification archived. |
| `t9` | delivered (with a defect found and fixed post-merge) | Latched `camera-stream-ended` drop. Its original guard watched the wrong branch and **stayed silent through three real camera deaths**; fixed separately (see Drift). |
| `t10` | delivered | `Limits` + `RequestConfig` frozen dataclasses; both constructors 23/24 → **12** params against this project's real S107 threshold of 13. |
| `t11` | delivered | `_ClipAsker` polls `state.json`'s clip on a background thread; answer enters the **context park**, never a trigger. `ask()` gained its first caller. 5 named drop reasons. |
| `t12` | delivered | Script refactored into testable units and landed with 29 tests + operating-guide section. |
| `t13` | delivered | **300** inert markers stripped (re-measured, not the issue's 296), **20** real ones byte-identical, 86 files, AST-identity proven across the whole diff. |
| `t14` | delivered | `environment.d` repointed to the `senses` role (verified live); operator-local units for layer + bridge; linger already on; full stop/start cycle brings all four units back. |
| `t15` | **partial** | Framing verified, boundary suites unchanged, evidence archived. **Issue closing deferred to PR merge** — the fixes are not on `main` yet, so closing them now would be false. |
| `t16` (unplanned, `d3`) | delivered | `reachy/embody/attention.py` — wake-word gate, 45 s warm window, 24 tests. Wire pins untouched (zero bytes changed in `realtime_duplex.py`). |

## Mid-work Decisions

- `d1` — t1 imports `MAX_SAY_CHARS` function-locally rather than at module
  scope as its brief instructed. The brief's premise was **false and verified
  so**: `reachy.behavior.rules` reaches `reachy.motion.pat` transitively, so a
  module-scope import would have reversed `speech/tools.py`'s documented
  no-motion boundary *and* made the task's own acceptance test unsatisfiable.
  The criterion as written was self-contradicting.
- `d2` — issue #147 fixed inside this arc, outside any planned task. Found
  while bringing the layer up: `agent embody start` could never work in
  background mode, which **blocks t14's durable-enablement acceptance** (the
  systemd unit uses exactly that path). A precondition, not a scope addition.
- `d3` — wake-word attention gate added as new scope at the operator's explicit
  mid-run request. The layer's ungated ear turned 6 utterances into 49 turns on
  a live conversation. It composes with t7's three-class policy (makes the
  *utterance* trigger conditional) rather than reworking it.

Decisions no deviation record covers:

- **t9's detector was found not to work, post-merge, and was fixed in-arc.**
  Issue #138's own text predicted a dead pipeline would "still look connected";
  the daemon in fact reports `camera_available: **false**`, so the detector's
  believed-present guard excluded the one failure it was built for. That branch
  also cleared `_last_frame_at`, erasing the evidence a stream ever existed.
  Fixed with two regression tests. Recorded here rather than as a deviation
  because it is a defect repair inside `t9`'s own contract, not a departure
  from it.
- **t13's diff is not literally comment-text-only.** `black` collapsed four
  previously comment-length-driven line wraps. Verified **AST-identical** across
  all 86 changed files, so zero semantic change — but the acceptance criterion
  said "comment-text changes only" and this is reported rather than rounded up.
- **t11's clip-question helper lives at the composition root, not in the
  engine.** `test_the_engine_reads_no_file_and_writes_no_environment_variable`
  is a whole-module AST scan; a file read anywhere in `engine.py` trips it.
- **t7 found issue #143's own numbers disagree** — "187 cues" versus a cue mix
  of 145 + 44 = 189. The spec's honesty condition names 187, so that was made
  authoritative and the discrepancy stated in the test module rather than
  silently reconciled.
- **t10 queried SonarCloud live rather than assuming the rule default.** The
  project's real S107 threshold is **13**, not the language default of 7 —
  which mattered, because moving only the bounds #141 named would have left
  `EmbodyTurnEngine` at 17 and still failing.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t1` (`d1`) | the brief's premise was false: `rules.py` reaches `reachy.motion` transitively, so the instructed module-scope import would have broken a documented boundary and the task's own acceptance test | acceptable |
| `t14` (`d2`) | #147 (embody `start` discarding its flags) blocked t14's acceptance path; fixing it was a precondition | acceptable |
| `t7` (`d3`) | wake-word attention added as new scope at the operator's mid-run request; composes with the three-class policy rather than reworking it | acceptable |
| `t9` | the detector shipped against a false premise from #138 and did not fire on the real failure; repaired in-arc with regression tests, and the repair was then **confirmed live** (19:39:07). The criterion was satisfied by tests and not by reality until the repair — which is the finding worth keeping | acceptable |
| `t13` | four line-wrap collapses by `black` mean the diff is "comment text + whitespace-only reformatting", not literally comment-text-only; AST-identity verified | acceptable |
| `t15` | issue closing deferred to PR merge — closing issues whose fixes are not yet on `main` would be false | needs-follow-up |

## Evidence

- tests: full suite `uv run pytest -n auto` — **4520 passed, 7 skipped**
  (skips pre-existing: no `cv2`, unreachable gateway)
- tests: `tests/test_embody_input_policy.py::test_replaying_the_measured_forty_second_window_produces_no_turns` — pass
- tests: `tests/test_procsup.py::test_daemon_stop_spares_a_real_bystander_from_the_sibling_checkout` — pass
- tests: `tests/test_behavior_face_sense.py::test_stream_ended_fires_when_the_camera_goes_unavailable_after_streaming` — pass
- tests: `tests/test_agent_embody.py::test_embody_operating_flags_survive_being_written_before_the_subcommand` — pass
- lint: `black --check` / `isort --check-only` / `flake8 reachy tests scripts` — all clean (347 files)
- lint: remaining `# noqa:` markers — **20** (the load-bearing set), down from 320
- commits: `6dff242..98986a6` — 13 merge commits, 114 files, +6268/−596
- evidence files: `docs/evidence/2026-08-02-t8-tick-overrun-attribution.md`
- live: `systemctl --user` stop/start cycle across all four units — all returned `active`; layer reconnected its tee and armed
- issues fixed: #132 #133 #134 #135 #136 #137 #141 #142 #143 #145 #147 #148
- issues opened during the run: #144 #145 #146 #147 #148 #149 #150 #151
- issues updated: #138 (consolidated as the single camera issue)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| A sense-cue flood produces zero turns and stays visible to the next turn | high | test `tests/test_embody_input_policy.py::test_replaying_the_measured_forty_second_window_produces_no_turns` |
| Alerts cannot reproduce the flood through the front door | high | test `…::test_a_burst_of_ten_rule_fires_inside_one_turn_window_produces_at_most_two_turns` |
| The sustained tick overrun is attributed to the per-tick camera read | high | `docs/evidence/2026-08-02-t8-tick-overrun-attribution.md` — streak ended 10:30:59, camera died 10:31:04, same process |
| The overrun fix works on the deployed robot | high | same file, post-fix section — 578 s camera-alive, **zero** sustained streaks vs `count=31090` before |
| `stop` can no longer SIGKILL an unrelated process | high | test `tests/test_procsup.py::test_daemon_stop_spares_a_real_bystander_from_the_sibling_checkout`; defect reproduced pre-fix |
| No executable default in `reachy/` names a served model id | high | test `tests/test_gateway_role_model_defaults.py::test_no_reachy_source_names_a_banned_served_id_as_a_default` |
| The tee flake is fixed at cause, not out-waited | high | assertion captured 24/24; `WAIT_BUDGET_S` untouched; 30/30 clean runs |
| 300 inert markers removed with zero semantic change | high | AST-identity across 86 files; `flake8` green; suite count unchanged |
| Both fat constructors clear SonarCloud's S107 | medium | test `…::test_the_constructor_clears_this_projects_configured_s107_threshold`; Sonar itself not yet re-run on this branch |
| The layer hears, thinks and speaks aloud on the deployed robot | high | live journal: `utterance chars=68` → `turn done` → `response done … audio=255360B` (≈5.3 s) |
| The wake-word gate refuses ambient speech and opens on the name | high | live journal: `dropped reason=not-addressed-cold ("Yeah.")` and `attention open (name) for 45s` |
| The full stack survives a stop/start cycle un-attended | high | all four units returned `active`; layer re-armed; `Linger=yes` |
| The camera-stream-ended detector fires on the real failure | high | observed live 19:39:07 on the deployed robot — `[SENSE stage=vision source=face event=stream] dropped reason=camera-stream-ended`, the first time a camera death has been named in the journal instead of being silent |
| The clip→`ask()` lane makes the robot able to describe its room | unverified | plumbing tested, but the **voice** is the realtime lane, which receives no context — see #149; and the camera dies within minutes (#138) |
| SonarCloud `python:S7632` count is 0 | unverified | requires the PR scan — not yet run |
| A true reboot (power cycle) brings the stack back | unverified | only a `systemctl` stop/start cycle was exercised |

## Remaining Work / Follow-up

- `t15` — close #132 #133 #134 #135 #136 #137 #141 #142 #143 #145 #147 #148
  **after the PR merges**, each linking its evidence. Deferred deliberately.
- `t9` follow-up — **done**: the detector fired live at 19:39:07, naming a real
  camera death for the first time. No remaining work on the detect half.
- **#138 (camera)** — consolidated as the single camera issue. Root cause open:
  lifetime collapsed from 610 min to 4.5 min across one day. Recovery still
  unprobed. This is the highest-value open item — at 4–18 min per process the
  robot is blind for most of its uptime.
- **#149** — the attention gate stops the layer *thinking* about ambient
  speech, but the realtime session still *answers* it out loud. Needs an
  upstream lever; the wire's send surface is pinned to three frame kinds.
- **#151** — conversational naturalness (long input, chunked long replies,
  interjection). Chunked speech is the keystone: playback is whole-clip and
  un-cancellable, so chunking converts "cancel audio in flight" into "don't
  send the next chunk".
- **#144** — the daemon writes 3.26 M journald lines/day at INFO, burying every
  real signal. May also be a second contributor to tick cost.
- **#146** — three more served-model-id defaults; one has been silently
  skipping its integration tests.
- **#150** — no CLI/env knob for the 45 s attention window.
- **#139** — the second-audio-output blocker **dissolved**: the operator is the
  second conversational party. Criteria want updating rather than the hardware.
- Deviations `d1`, `d2`, `d3` are all `proposed` and want the operator's
  `devague deviate --confirm` to become approved ground truth.
