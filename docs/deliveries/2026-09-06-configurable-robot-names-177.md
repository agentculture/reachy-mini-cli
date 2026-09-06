# Delivery Summary — configurable robot names #177

plan: `configurable-robot-names-177` · run: `partial` · date: `2026-09-06`
baseline: `devague summary skeleton`

## Intent

Let a peer harness configure the names the robot answers to without this repo
ever learning a peer's name: one `names` table in the rules overlay feeds the
transcript engagement gate (live, via `behavior reload`), the classifier's
prompt, the embodiment attention gate and the sleep wake phrases, and being
named becomes a rule-visible sense event, `name_mentioned`. The run executed
the eight-task plan exported from the challenged frame
(`docs/plans/2026-09-06-configurable-robot-names-177.md`): waves 0–3 by six
task agents in isolated worktrees under a tests-before-and-after merge gate,
t7 and t8 by the main agent, delivered as PR #182 (v0.53.0).

## Planned Work

Quoted verbatim from the `devague summary` skeleton (task id + the opening of
each summary; the full text is in the plan export):

- `t1` — t1 name_match: SHIPPED_NAMES = ('reachy','robot') constant in
  reachy/speech/name_match.py; is_name_match's names default imports it;
  re-land the clitic stem match and the four-letter fuzzy floor
  (`_MIN_FUZZY_WORD_LEN`=4) name-agnostically; tests/test_name_match.py gains
  the n-family rows under a CONFIGURED names fixture
- `t2` — t2 rules schema: names table in reachy/behavior/rules.py —
  `_TOP_LEVEL_FIELDS` gains 'names', RulesConfig.names: tuple[str,...]
  (default SHIPPED_NAMES, imported from name_match), from_dict validates
  fail-closed (…), merge_rules unions shipped + overlay in order;
  rules.SENSE_FIELDS gains 'name_mentioned'; default_rules.toml documents the
  table; tests/test_zero_llm_boundary.py's `_BEHAVIOR_SPEECH_ALLOW` gains
  reachy.speech.name_match with its justification
- `t3` — t3 sense field: Sense.name_mentioned: bool = False in
  reachy/behavior/sense.py with a name_mentioned provider on SenseProviders,
  added to `_PROVIDER_PREDICATE_FIELDS` and `_COMPOSED_PROVIDER_FIELDS`;
  reachy/export/runtime.py's hand-listed Sense mirror gains it;
  reachy/runtime_cues.py sense_cues emits 'someone said my name' when true;
  docs/export-schema.md notes the key
- `t4` — t4 the hearing gate follows the names: reachy/speech/engagement.py
  DEFAULT_NAMES = SHIPPED_NAMES (import), ENGAGEMENT_SYSTEM_PROMPT becomes a
  template rendered from the names (…); ConversationGate/EngagementClassifier
  accept a zero-arg names provider; reachy/behavior/transcript_sense.py takes
  names_provider=, its `_should_engage` returns (engaged, by_name), and the
  driver latches name_mentioned for one tick on a 'name' admission (…)
- `t5` — t5 runtime composition + CLI observability in
  reachy/cli/_commands/behavior.py: `_compose_run_seam` binds
  TranscriptSenseDriver's names_provider to the ReloadDriver's
  loader.current.names and wires the name_mentioned provider into
  SenseProviders; behavior rules list / rules check / engine status render
  the merged names (text + --json)
- `t6` — t6 the other two roots read the same file: reachy/embody/attention.py
  DEFAULT_NAMES = SHIPPED_NAMES (import) and `_commands`/agent.py builds
  AttentionGate with load_rules(overlay_rules_path()).names; reachy/sleep/
  wakeword.py gains DEFAULT_PHRASES/_resolve_phrases (one 'hey `<name>`' per
  configured name except 'robot'; …) and reachy/sleep/wake.py +
  `_commands`/sleep.py thread the names from the same load
- `t7` — t7 docs, boundary test, nova issue, release: docs/operating-reachy.md
  gains a names section (…); explain catalog + CLAUDE.md + README updated; a
  test greps reachy/ for the token 'nova' (none); file the
  OriNachum/reachy-nova issue naming rules_overlay.py's two copied sets;
  version-bump minor + CHANGELOG; PR via cicd to green
- `t8` — t8 bench verification (operator or agent on the dev box's Lite robot
  at localhost:8000, or the wireless unit): write names = ["nova"] into the
  overlay, behavior reload, speak 'nova, are you there' (…) — journal shows a
  stage=transcript admission labelled 'name' and the export shows
  name_mentioned true for one tick; remove the table, reload, the same
  utterance drops 'not-addressed-cold'; excerpt attached to the PR

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `c958992`, merged `7129277`: `SHIPPED_NAMES`, clitic stem, four-letter floor; n-family rows under a configured fixture; 88 name-match tests |
| `t2` | delivered | `873cee5`, merged `e57bf73`: `names` table (letters/3+/≤8, union merge, last-good), `SENSE_FIELDS` gains the field; new `tests/test_behavior_rules_names.py`. Two files beyond the brief's list — see Drift |
| `t3` | delivered | `05bfc53`, merged `0af3e52`: field on `Sense`, both registries, export mirror, cue `someone said my name`, export-schema doc; one file beyond the brief's list — see Drift |
| `t4` | delivered | `f27fa1c`, merged `4013dad`: live names provider through gate + classifier, rendered prompt, `(engaged, by_name)`, one-tick `name_mentioned` latch; new `tests/test_transcript_name_mentioned.py` |
| `t5` | delivered | `39e8728`, merged `93e5a65`: provider bound to `loader.current.names`, `name_mentioned` wired into `SenseProviders`, `_NamesPublisher` state.json rider, `rules list`/`check`/`engine status` report names; new `tests/test_behavior_names_composition.py` |
| `t6` | delivered | `15ccc07`, merged `8efceb9`: attention alias + `set_names`, embody composition reads the overlay (named `names-overlay-refused` fallback), `DEFAULT_PHRASES`/`_resolve_phrases`, sleep threads names |
| `t7` | delivered | `fe2779c`, `3e76485`, merged `3ef503f`: guide names section, README/CLAUDE.md/catalog, `tests/test_no_peer_name.py` (AST value scan), v0.53.0 + CHANGELOG, reachy-nova#27 filed, PR #182 opened and driven to green (Sonar 0 issues, 7/7 review threads resolved) |
| `t8` | blocked | Attempted on the dev box: the Lite daemon has no backend (no serial port → `engine run` exits `503 Backend not running`) and the gateway's speech lane returns 404; the wireless robot has no gateway configured. Evidence and the exact re-run recipe are on PR #182; plan risk r4 |

## Mid-work Decisions

No `devague deviate` records exist for this run; every decision below was
taken by the operator or a task agent and is captured here directly.

- Wave grouping was not the execution order: t3 and t6 were started as soon as
  t1 and t2 merged (their only dependencies) while t4 was still running, and
  the t7 docs draft was written while t5 built — file-disjoint, so the
  parallel result equals the serial one.
- t2 narrowed `test_every_sense_field_is_fed_by_the_current_composition` to
  pin the gap as exactly `{"name_mentioned"}` and handed t3 the job of
  restoring `==`; t3 restored it.
- t2 added ONE named exception (`_DECISION_CORE_SPEECH_LEAF =
  "reachy.speech.name_match"`) to the decision-core zero-speech pin, with a
  companion test that the leaf reaches no first-party module, rather than
  loosening the pin to a category.
- t4 aliased `transcript_sense.DEFAULT_NAMES` through `engagement` rather than
  `name_match`, to avoid a new allow-list entry it was told not to add — same
  object, allow-listed module.
- t6 gave the attention gate its names via a new `set_names` on
  `attention.py` instead of a `Limits` field, because `embody/engine.py` was
  out of its file set.
- t5 kept `engine status`'s names on a change-gated state.json rider modelled
  on the availability driver, not a new engine/supervisor key.
- The PR's Sonar S107 finding (14 parameters on `_compose_embody_seam`) was
  resolved by making the overlay loader a module-level seam resolved at call
  time (the discovery package's `read_interfaces` pattern) rather than a
  keyword; the two tests monkeypatch it.
- The review's suggestion to route the no-classifier heuristic through the
  fuzzy matcher was DECLINED: `test_misheard_name_does_not_engage_when_idle`
  pins that an STT mishearing must not engage the robot with no context to
  judge; only clitic stems were adopted.
- The openwakeword finding was accepted as pre-existing and made loud (a
  WARNING at engine load naming the configured phrases it ignores) rather
  than fixed — steering the on-box engine by phrase needs per-phrase models.
- The no-peer-name test was rewritten from a plain grep to an AST value scan
  after the grep flagged dozens of provenance comments citing `reachy_nova`
  (cite-don't-import ports); the harness's own MQTT topic is an explicit,
  reasoned allow-list entry.
- CI's `test` job failed on three consecutive heads (`25ae990`, `ca07253`,
  `78efece`) on `tests/test_behavior_nervous_composition.py::test_h20_the_export_feed_is_identical_with_and_without_the_bus`,
  then passed on `ecfd039` (a docs-only change) — a pre-existing clock race:
  the snapshot driver emits on dataclass inequality and `pat_state` carries a
  timestamp the pat sense rewrites on tick-timing-dependent paths, so a slow
  runner can emit a `sense` line identical to its predecessor except in that
  timestamp. Hardened test-only: consecutive scrubbed-identical `sense`
  events are folded before the two feeds are compared; distinct events must
  still match in order. Main's and PR #180's red `Tests` runs are the
  SonarCloud Scan step, unrelated.
- The shared main checkout was switched to another branch twice by a
  parallel session; the whole run was executed from
  `../.worktrees.reachy-mini-cli/names-*` worktrees plus a `names-int`
  integration worktree so no step depended on it.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t2` | Touched `tests/test_behavior_sense_fields.py` (canary narrowed, restored by t3) and two further pins in `tests/test_zero_llm_boundary.py` beyond the allow-list entry — both forced by the new behavior→speech edge and both pinned, not loosened | acceptable |
| `t3` | Touched `reachy/behavior/sense_availability.py`: its probe registry derives from `_COMPOSED_PROVIDER_FIELDS` and refuses construction without a probe for every declared sense — a `name_mentioned` probe was required to keep the suite green | acceptable |
| `t4` | `transcript_sense.DEFAULT_NAMES` aliases via `engagement`, not `name_match` (same object) | acceptable |
| `t7` | The plan's "grep for the token 'nova'" became an AST VALUE scan with a reasoned allow-list; a literal grep is the wrong instrument in a repo whose vision/forge modules cite `reachy_nova` as provenance | acceptable |
| `t7` | Four review-driven fixes landed after the PR opened that the plan did not foresee: the rule engine never READ `name_mentioned` (declared, fed, but unmatched — a real defect the offline suite missed), the classifier was built on the shipped names, `engine status` trusted a dead engine's state file, `isalpha` admitted accented names the ASCII matcher cannot match | acceptable |
| `t8` | Not run: no box in reach has both a live backend and a hearing gateway. Offline coverage stands in; the live process check stays open | needs-follow-up |

## Evidence

- tests: full suite on `ca07253` — `1 failed, 5744 passed, 8 skipped`; the
  failure is `tests/test_vision_scene_integration.py::test_integration_scene_default_model_resolves_via_senses_role`,
  a live-gateway test that fails identically on `origin/main`
- tests: `tests/test_behavior_rules_names.py::test_a_rule_keyed_on_name_mentioned_fires_and_only_on_that_tick` — pass
- tests: `tests/test_transcript_name_mentioned.py` (6) · `tests/test_behavior_names_composition.py` (17) · `tests/test_no_peer_name.py` (3) — pass
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit -c pyproject.toml -r reachy`, `markdownlint-cli2 "**/*.md" …`, `teken cli doctor . --strict` — all clean on `ca07253`
- CI on PR #182 head `ecfd039`: test / lint / offline / version-check / test-publish / GitGuardian — pass (`test` was red on `25ae990`..`78efece` from the flake above); SonarCloud quality gate OK, 0 open issues, 0 hotspots; review threads 7/7 resolved
- commits: `07187f1..ca07253` on `spec/configurable-robot-names-177` (43 files, +3102/−137 vs `main`); per-task: `c958992` `873cee5` `05bfc53` `f27fa1c` `39e8728` `15ccc07` `fe2779c` `3e76485`; review: `08d4ca1` `1d848db` `25ae990` `ca07253`
- PRs / issues: #182 (this delivery), #177 (closed by it), #175 / #178 (origin of the split), OriNachum/reachy-nova#27 (peer prerequisite)
- bench: PR #182 comment "Bench (plan task t8) — BLOCKED" with the journal lines; plan risk r4

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The shipped pair is spelled once and every default derives from it | high | `reachy/speech/name_match.py` · `tests/test_no_peer_name.py::test_the_shipped_pair_is_spelled_once` |
| A `names` table in the overlay extends the pair, validated fail-closed, last-good on a bad file | high | `873cee5` · `tests/test_behavior_rules_names.py` |
| The runtime answers to the configured names LIVE after `behavior reload`, no rebuild | high (offline) | `39e8728` · `tests/test_behavior_names_composition.py` (loader rebinding) — not observed in a live process (t8) |
| The classifier's prompt lists the configured names | high | `25ae990` · `test_the_engagement_classifier_is_built_on_the_live_names_provider` |
| An admission BY NAME latches `name_mentioned` for one tick; a rule keyed on it fires | high | `f27fa1c` `05bfc53` `25ae990` · `test_a_rule_keyed_on_name_mentioned_fires_and_only_on_that_tick` |
| The export snapshot and the embodiment cue carry `name_mentioned` | high | `05bfc53` · `tests/test_export_runtime.py` · `tests/test_runtime_cues.py` |
| `agent embody`'s attention gate and `sleep`'s wake phrases read the same table at start | high | `15ccc07` · `tests/test_agent_embody.py` · `tests/test_sleep_wakeword.py` |
| `rules list` / `rules check` / `engine status` report the names in force, with `names_source` | high | `39e8728` `25ae990` · `tests/test_behavior_names_composition.py` |
| No peer name is a value anywhere under `reachy/` | high | `tests/test_no_peer_name.py` |
| Saying a configured name to the robot is admitted by name on a live box | unverified | t8 blocked — not claimed done |
| The openwakeword backend honours configured phrases | unverified | pre-existing limitation, now logged; not claimed |

## Remaining Work / Follow-up

- `t8` — run the live bench on a box with a live backend AND a gateway whose
  `stt` lane is on (recipe on PR #182); post the two journal excerpts and the
  `name_mentioned: true` export line; then close plan risk r4. Owner: operator.
- reachy-nova#27 — the harness's overlay writer must learn the `names` table
  and the `name_mentioned` field BEFORE a names table is written on the robot;
  until then its managed-block writes would fail. Owner: reachy_nova.
- PR #180 (v0.52.1, another session) is open against `main`; whichever of
  #180/#182 merges second needs a one-line CHANGELOG/version reconcile.
- openwakeword: if the on-box wake-word leg should honour configured phrases,
  file it as its own issue (needs per-phrase models).
- The parallel session's worktree `/home/spark/git/reachy-mini-cli-worktrees/fbr25-int`
  and branch `agent/fbr25-t16` are outside the sanctioned worktree directory
  and were left untouched.
