# Delivery Summary — retire the old AI-first flow

plan: `retire-the-old-ai-first-flow` · run: `complete` · date: `2026-07-20`
baseline: `devague summary skeleton`

## Intent

Remove the AI-first flow so the robot's presence is the **symbolic behavior
runtime** — rules and configuration, not a compiled-in AI app — with cognition
demoted to an optional, external `agent attach` (issue #70 asks to *demote*, not
delete).

The load-bearing finding from the spec pass shaped everything: **this is a
porting job, not a deletion job.** `reachy/behavior/` was already LLM-free, but
five capabilities existed ONLY in the retiring path — voice, sound-orienting,
hearing words, rms, and face/frame_available. Every one had to move and be
live-verified on the deployed robot *before* anything was deleted. The plan's
dependency graph encodes exactly that: every port is upstream of the `t19` gate,
every deletion downstream.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Capture the pre-change baseline: enumerate the capabilities each flow owns, record the current LIVE box evidence (stuck speech_detected, tick headroom, units, drop-ins)
- `t2` — Move Event/MarkerEvent out of reachy/speech/markers.py to a surviving module
- `t3` — Add a bidirectional explain-catalog <-> CLI agreement test
- `t4` — Build the retired-unit cleanup migration in ServiceManager
- `t5` — Decide and implement foreground-verb arbitration now that the *_active.flag readers retire
- `t6` — Speech actuator: a behavior action seam that synthesizes+plays on a BACKGROUND worker
- `t7` — Speech observability: [SENSE] drop line on failed synthesis + explicit TTS route on the runtime unit
- `t8` — Sound-orienting: doa_angle_to_yaw driven into a sustained gaze goal in the runtime
- `t9` — Port the latched-DoA guard so a frozen at-rest angle cannot drive a turn
- `t10` — One held media client: mic + camera + pose lifecycles from a single SDK owner, explicitly closed
- `t11` — Transcript sense: background STT worker feeding a latched transcript field, engagement gate preserved
- `t12` — rms sense provider over the shared mic sample
- `t13` — face + frame_available providers over the held frame source
- `t14` — Two-layer rules: shipped package resource + box-local overriding overlay
- `t15` — Author the shipped default rules covering pat, voice, hearing and orienting
- `t16` — behavior rules check warns on predicates keyed to unfed sense fields
- `t17` — Re-home forge onto agent attach (net-new composition, ~60 LOC incl. feed_forge wiring)
- `t18` — Re-home the expression pose catalog verbs onto a surviving noun
- `t19` — SOAK CHECKPOINT: runtime at full capability, reachy-live.service still enableable, rollback runbook validated on the box
- `t20` — Delete the think noun, its supervisor, sidecar and catalog entries
- `t21` — Delete the listen --live composition root and the folded motion/listen_*.py hooks
- `t22` — Retire the listen NOUN (cli/_commands/listen.py), keeping reachy/motion/listen.py ListenProducer
- `t23` — Remove LIVE_UNIT and the live presence mode; service offers exactly demo|runtime
- `t24` — Machine-check the zero-LLM property: import-boundary tests + offline lane coverage
- `t25` — Verify the export/runtime feed contract survives the deletion unchanged
- `t26` — Keep docs accurate PER PR, not deferred to a cleanup pass
- `t27` — Fix HeldStateReader's tick-thread construction stall (the live defect measured in t1)
- `t28` — Composition root warms BOTH held clients before the first tick
- `t29` — Give ForgedSkillContext an intent-submitting effector so forged skills can still reach the robot
- `t30` — HeldMediaClient acquires the daemon media subsystem before constructing, and releases on close
- `t31` — Self-motion-conditioned rms floor: self_moving sense field + moving-floor gate breaking the actuator self-noise admission loop (#95)
- `t32` — Logging honesty: the reachy logger stops propagating to foreign root handlers; rule drops log per transition, not per tick (#96, #99)
- `t33` — Deadline-based tick scheduler + per-tick timing seam: 50 Hz achieved when work fits the budget (#97)
- `t34` — Background audio pump: drain the SDK appsink at production pace off the tick thread; live rms + lossless transcript feed (#100)
- `t35` — Attack/release envelope on the orient NOISE tier: one smooth lean per sound episode, never per-tick flapping (#95 polish)
- `t36` — Adaptive rms admission: ratio over a rolling background estimate replaces the absolute 0.02 floor (#95 root closure)
- `t37` — Close the engagement-gate leak: phonetic name guard (#104) + ConversationGate warm window (#105)
- `t38` — One contiguous clip per utterance: stop splicing the audio sent to STT (#108)

## Actual Delivery

All 38 tasks accounted for. Every merge was TDD-gated (full suite before **and**
after), and every deletion accounted for its test-count delta task by task.

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `docs/verification/2026-07-20-retire-old-flow-baseline.md` — corrected a 3-sample probe with a 120-sample run: `speech_detected` is not stuck, it FLICKERS true 45.8 % of the time in a quiet room. That correction killed the "dwell alone is safe" design |
| `t2` | delivered | `Event`/`MarkerEvent` re-homed to `reachy/speech/marker_events.py` (`f0fdce1`) |
| `t3` | delivered | Bidirectional explain-catalog ↔ CLI agreement test (`d9d3154`) — the tripwire that caught stale catalog entries in `t20` |
| `t4` | delivered | `RETIRED_UNITS` + `cleanup_retired_units()` migration (`7722ec3`) — reused verbatim by `t23` |
| `t5` | delivered | Foreground-verb arbitration as a hard REFUSAL: `refuse_if_engine_live()` (`2dde68b`) |
| `t6` | delivered | `reachy/behavior/speech_act.py` — O(1) on the tick thread, worker does synthesis + playback (`de3cf09`) |
| `t7` | delivered | Named `[SENSE stage=speech]` drops; a wedged TTS is a named drop, never silence (`b7ff434`) |
| `t8` | delivered | `reachy/behavior/orient.py` — `doa_angle_to_yaw` into a sustained gaze goal (`93fe65d`) |
| `t9` | delivered | `LatchedDoaGuard` ported (`fbefa67`) |
| `t10` | delivered | `HeldMediaClient` — one SDK owner for mic + camera + pose (`35c3ee3`) |
| `t11` | delivered | `TranscriptSenseDriver` + engagement gate preserved (`749e890`) |
| `t12` | delivered | rms provider over the shared mic sample (`6bffb6d`) |
| `t13` | delivered | face + `frame_available` providers (`20e770e`) |
| `t14` | delivered | Two-layer rules: shipped package resource + box-local overlay (`0f8c8b6`) |
| `t15` | delivered | Three shipped rules — `pat-acknowledge`, `look-toward-sound`, `greet-when-addressed` (`28cc081`) |
| `t16` | delivered | `behavior rules check` warns on predicates keyed to unfed sense fields (`22be077`) |
| `t17` | delivered | forge re-homed onto `agent attach` (`63c8f81`) |
| `t18` | delivered | Expression pose catalog verbs re-homed onto `behavior` (`3f4b35a`) |
| `t19` | **delivered (split)** | Gate doc authored BEFORE the gate ran. **C1–C5 all PASS**; rollback runbook **rehearsed and repaired**. The 72 h soak leg was split out to #103 by `d5` |
| `t20` | delivered | think noun, supervisor, sidecar, 12 catalog keys deleted (`a844234`) |
| `t21` | delivered | `--live` root + 11 folded modules + cognition/marker engines deleted (`b6a9949`) |
| `t22` | delivered | listen noun retired; `ListenProducer` KEPT (`6218c40`) |
| `t23` | delivered | `LIVE_UNIT` removed; `service` offers `demo\|runtime` (`aee839f`) |
| `t24` | delivered | AST import-boundary suite + offline hearing lane (`266b863`) |
| `t25` | delivered | Export contract pinned by `tests/test_export_schema_doc.py` (`9be1a81`) |
| `t26` | delivered | README / CLAUDE.md / operating-reachy.md converged (`6d1e83f`) |
| `t27` | delivered | Tick-thread construction stall fixed (`5f1eeee`) |
| `t28` | delivered | Both held clients warmed before the first tick (`91fbc27`) |
| `t29` | delivered | `ForgedSkillContext` intent-submitting effector (`9d78e58`) |
| `t30` | delivered | Daemon media acquired before construction, released on close (`65f12bf`) |
| `t31` | delivered | `self_moving` sense + motion-conditioned rms floor (`38cbffd`) |
| `t32` | delivered | `propagate=False` + per-episode drop lines (`75f0b6c`) |
| `t33` | delivered | Absolute-deadline scheduler — 23 Hz → 44 Hz (`9531732`) |
| `t34` | delivered | `AudioPump` background worker (`c86e48b`) |
| `t35` | delivered | Attack/release envelope on the NOISE tier (`8e6bcda`) |
| `t36` | delivered | `RmsBackground` rolling-median + the `d6` two-tier ladder (`15ed453`) |
| `t37` | delivered | Phonetic Soundex name guard + `ConversationGate` (`6b0145d`) |
| `t38` | delivered | One contiguous clip per utterance + explicit channel select (`c475e37`) |

## Mid-work Decisions

- `d1` — insert a pre-soak fix wave `t31`–`t33` — the live `t19` gate session found three blockers (self-noise rms loop, duplicate logging, a 23 Hz scheduler); the soak and every deletion stay blocked until they land and C2–C5 re-verify.
- `d2` — add `t34`, the background audio pump — the SDK appsink is a FIFO built for a 50 Hz consumer; one-pull-per-tick left a standing backlog so every reading was seconds stale.
- `d3` — add `t35`, an attack/release envelope — a per-tick rms predicate flaps at tick rate on transient trains, commanding a sharp lean and snap-back *per tick*.
- `d4` — add `t36`, ratio-over-rolling-background admission — the measured 25× background drift makes any absolute value wrong somewhere the robot lives.
- `d5` — split `t19`: the 72 h soak is parked to #103; the operator-present legs (rollback rehearsal, C3–C5) stay hard pre-deletion gates. The soak proves it *keeps* working and reverts cleanly; the rollback rehearsal cannot be validated after the deletions remove `reachy-live.service`.
- `d6` — the shipped sound reaction becomes a graded two-tier ladder (antenna lean; head turn only for sustained or loud-relative-to-background sound). A reflexive turn toward an unintelligible sound is noise-chasing — and a head that keeps turning is a head that can never feel a pat.
- `d7` — amend `t19`'s rollback runbook. Executed end-to-end and it failed in four ways, one **silently**: the documented install command deletes the `reachy-mini-daemon` binary that `reachy-daemon.service` execs, leaving a box that looks healthy until the next reboot.
- `d8` — promote `reachy-mini` from an extra to a base runtime dependency and flip speech playback to the SDK path (operator decision; classified `risky` because the constraint being reversed is measured — a hard base dep broke CI on the pycairo build in PR #24).
- `d9` — #104/#105 BLOCK the deletions: `greet-when-addressed` is a shipped default that answered human-to-human conversation.
- `d10` — LOCK `t36`'s tuning as the shipped operating point and retire the box-local `REACHY_SELF_MOVING_TAIL_S` override.
- `d11` — for THIS change the shipped sound reaction is **antenna-only**; the head does not orient toward sound. The capability stays ported and config-reachable — only the default changes.
- `d12` — narrow `d9` from "before the deletions" to "before the merge": the fixes and deletions are file-disjoint and nothing reaches a robot until the PR lands.

Decisions **not** covered by a deviation record:

- `t22`'s prose docs (~65 references) were deferred to `t26` rather than rewritten three times across `t22`/`t23`/`t26`. Stated explicitly in `t22`'s commit message; the CLI-visible explain catalog *was* updated there because the bidirectional test gates it.
- The two idle flags were treated **asymmetrically** rather than as one decision: `pat_active.flag` became bench-local bookkeeping (its only cross-process reader was `listen`'s idle wander), while `sleep_active.flag` was KEPT because `cmd_sleep_status` reads it cross-process (`reachy/cli/_commands/sleep.py:817`) and it is the only way to observe a parked robot.
- Two module-scope imports were made lazy after `t24` reported them — a production change outside `t24`'s test-only lane, sequenced by the main agent.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t19` (`d5`) | the soak is 72 h of wall-clock the build arc cannot hold open; the rollback rehearsal cannot be validated after the deletions remove `reachy-live.service`, so it stays a gate while the multi-day soak becomes tracked work | needs-follow-up |
| `t19` (`d7`) | the rollback runbook was executed and FAILED in four ways, one silently (the install command deletes the daemon binary) | needs-follow-up |
| `t19` (`d12`) | `d9`'s blocking condition narrowed from "before the deletions" to "before the merge" — nothing reaches a robot until the PR lands | acceptable |
| `t15` (`d9`) | the shipped `greet-when-addressed` rule answered conversation the robot was never addressed in; must not ship | needs-follow-up |
| `t15`/`t8` (`d6`, `d11`) | shipped sound reaction is antenna-only; tier 2 promoted zero times across 8 admissions including 3 s of continuous speech, so the boundary was stated rather than shipping a path that never fires | acceptable |
| `t36` (`d10`) | tuning locked as the shipped operating point, with its honest cost recorded: the head essentially never turns toward sound | acceptable |
| `t12`/`t8` (`d1`–`d4`) | four fix tasks inserted mid-run that the plan never contained — each traced to a defect the live gate exposed | acceptable |
| `t26` | absorbed `t22`'s deferred prose (~65 references) plus `t23`'s stale-line list, rather than each task editing the same paragraphs | acceptable |
| `t11` | `t37`/`t38` were BUILT before being registered as plan tasks — a process gap; both were retro-recorded and confirmed, but for a period two substantial changes had no plan record | needs-follow-up |
| plan-wide (`r8`) | the plan can no longer converge: `plan converge` demands coverage of `c43`–`c49` while `plan cover` rejects them as unknown (targets frozen at seeding). Filed upstream as agentculture/devague#90, still open in 0.20.1 | needs-follow-up |

## Evidence

- tests: full suite `uv run pytest -n auto -q` — **3442 passed, 6 skipped**
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit -c pyproject.toml -r reachy` — all clean
- rubric: `uv run teken cli doctor . --strict` — **26/26 PASS**
- markdown: `markdownlint-cli2` over tracked files — 27 files, 0 errors
- commits: `b9e70471..d7644c8` (89 commits)
- live gate: `docs/verification/2026-07-20-t19-soak-exit-criteria.md` §5 (C1–C5 + rollback), `docs/verification/2026-07-21-live-verification-night.md`
- issues closed by this arc: #94, #95, #96, #99, #100, #102, #104, #105, #108
- issues opened by this arc: #103, #106, #107, #109, #110, #111, #112, #113; upstream agentculture/lobes-cli#149
- deviation ledger: `d1`–`d12`, all approved

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The robot hears a spoken sentence and answers audibly, from the shipped rule | high | gate doc §5.1 C4 — `heard "Richie, are you there?"` → `greet-when-addressed fired` → `spoke voice=harmonic`; reproduced twice, operator confirmed hearing the reply |
| A pat produces a reaction from the shipped rule | high | gate doc §5.1 C3 — `Pat level1! type=side_pat` → `pat-acknowledge fired run=pet-reaction`, operator observed the antennas contract |
| Media is acquired and the ported senses feed | high | gate doc §5.1 C1 — `available: true, released: false` with the runtime active |
| A quiet room produces no spurious head turning | high | gate doc §5.1 C2 — 0 `->SPEECH`, 0 `->ENGAGED` over 5 min |
| Sound produces an antenna lean and never a head turn | high | gate doc §5.1 C5 — 4 admissions / 100 s, all `NONE->NOISE`, 0 tier-2 promotions |
| The rollback runbook restores **function**, not just files | high | gate doc §5.2 — `reachy-live.service` active, all four flags, and a real pat producing `Pat level2!` on the rolled-back 0.41.0 build |
| The presence runtime's decision loop reaches no LLM | high | `tests/test_zero_llm_boundary.py` — AST import-boundary suite, mutation-tested |
| Exactly one LLM call survives in the runtime (the engagement classifier), removable via `REACHY_ENGAGE_HEURISTIC=1` | high | `test_the_only_llm_edge_in_the_presence_runtime_is_the_engagement_gate` — pinned by equality |
| Capture submits one contiguous clip per utterance | high | `tests/test_behavior_transcript_contiguous.py`; live `span=1.10s clip=6.08s contiguous` |
| The export wire contract survives the deletions unchanged | high | `tests/test_export_schema_doc.py` (14 tests, mutation-tested) |
| `service` offers exactly `demo\|runtime`, single-presence invariant holds | high | `tests/test_service_live_retirement.py` — exhaustive over all 30 enable sequences up to length 4 |
| Building the CLI parser loads no cognition module | high | `test_building_the_cli_parser_loads_no_cognition_module` — pinned by equality, mutation-verified |
| The runtime survives a daemon restart and re-acquires media unaided | medium | observed twice live (`media acquired from the daemon (was released)` → `connected` → pump `live`); not yet inside a soak window |
| Tick work fits the 20 ms budget | **unverified** | NOT claimed — measured `p50=20.58 ms`, `max=23.65`; #97 remains open |
| The runtime holds up over 72 h continuous uptime | **unverified** | soak not run — parked to #103 behind the #113 stabilisation pass |
| Hearing works at conversational distance across a room | **unverified** | verified at close range only; #111 open, agreed fix is server-side VAD |

## Remaining Work / Follow-up

- **#113 — stabilisation pass.** Live the runtime unhurried until it feels right, in a separate session. Every defect that mattered in this arc surfaced from an operator watching the robot, not from the 3442-test suite.
- **#103 — the 72 h soak.** Criteria written; clock starts after #113, against the shipped build.
- **#111 — capture start threshold.** A normal voice across a room may not open an utterance at all. Decided fix: server-side VAD via lobes-cli#149, deliberately not interim threshold tuning.
- **#97 — tick work at `p50=20.58 ms`.** Audio, pose read-back and DoA all exonerated; cause unapportioned, and the overrun line floods the journal at ~39 lines/s.
- **#106 (`d8`) — `reachy-mini` as a base dependency.** Approved; gating work is CI system libs. One sub-decision open: does the light HTTP-remote install profile survive as a slim extra, or die?
- **#109 — scene description.** Both framings agreed, easier first: wire `describe_scene=` onto `agent attach`, then a periodic runtime sense.
- **#110 — parking as a runtime capability.** Wake condition decided: pat, not sound.
- **#107 — vision-corroborated head turning.** The successor to `d11`'s antenna-only default.
- **agentculture/devague#90 (`r8`)** — the plan cannot re-converge; this repo's arc offered upstream as a test case.
- **The export `message` block** means what the agent *proposed* saying; audible runtime speech carries no block. Reviewed and accepted as-is.
