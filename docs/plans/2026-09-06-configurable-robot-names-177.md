# Build Plan — configurable robot names #177

slug: `configurable-robot-names-177` · status: `exported` · from frame: `configurable-robot-names-177`

> A peer harness can tell the robot which names it answers to — one configured list feeds every place reachy-mini-cli matches the robot's name (the transcript engagement gate, the embodiment attention gate, the fuzzy matcher, the classifier prompt, the sleep wake phrase) — without this repo ever learning a peer's name (reachy-mini-cli #177, split from #175 / reachy-nova #25)

## Tasks

### t1 — t1 `name_match`: `SHIPPED_NAMES` = ('reachy','robot') constant in reachy/speech/`name_match.py`; `is_name_match`'s names default imports it; re-land the clitic stem match and the four-letter fuzzy floor (`_MIN_FUZZY_WORD_LEN`=4) name-agnostically; tests/`test_name_match.py` gains the n-family rows under a CONFIGURED names fixture

- covers: c6, h5
- acceptance:
  - `is_name_match`('nova') is False by default and True with names=`SHIPPED_NAMES`+('nova',); under that fixture 'now','no','know','nah','not','novel','november','nowhere','nothing','never','not now' reject and 'nova','NOVA','Nova, come here','hey nova',"nova's over here" accept
  - "reachy's here" matches by the stem rule; every pre-existing row in `test_name_match.py` still holds; grep -n '("reachy", "robot")' reachy/speech/`name_match.py` finds only the `SHIPPED_NAMES` line
  - git diff touches only reachy/speech/`name_match.py` and tests/`test_name_match.py`; `name_match.py` still imports only difflib/re (no reachy.speech.engagement, no reachy.behavior)

### t2 — t2 rules schema: names table in reachy/behavior/rules.py — `_TOP_LEVEL_FIELDS` gains 'names', RulesConfig.names: tuple\[str,...\] (default `SHIPPED_NAMES`, imported from `name_match`), `from_dict` validates fail-closed (list of strings; letters only after lower-casing; len>=3; <=8 entries; de-duplicated; blank refused), `merge_rules` unions shipped + overlay in order; rules.`SENSE_FIELDS` gains '`name_mentioned`'; `default_rules.toml` documents the table; tests/`test_zero_llm_boundary.py`'s `_BEHAVIOR_SPEECH_ALLOW` gains reachy.speech.`name_match` with its justification (and the dead-entry test still passes)

- depends on: t1
- covers: c2, h1, c24, h22, c10, h9
- acceptance:
  - `load_rules` over names=\["nova","Nova","nova"\] yields ('reachy','robot','nova'); a missing table yields exactly `SHIPPED_NAMES`; `load_rules`(`include_shipped`=False) over an overlay with no table ALSO yields `SHIPPED_NAMES`
  - names=\["ab"\], \["no va"\], \["nova!"\], \["n0va"\], \[""\], "nova", \[42\] and a 9-entry list each raise CliError naming the entry and the bound; RulesLoader.reload over such a file keeps last-good and sets `last_error`
  - '`name_mentioned`' in rules.`SENSE_FIELDS`; tests/`test_zero_llm_boundary.py` passes with `name_match` in the allow-list and no other expected-set edit; new tests live in tests/`test_behavior_rules_names.py`
  - git diff touches only reachy/behavior/rules.py, reachy/behavior/`default_rules.toml`, tests/`test_behavior_rules_names.py` (new), tests/`test_zero_llm_boundary.py`

### t3 — t3 sense field: Sense.`name_mentioned`: bool = False in reachy/behavior/sense.py with a `name_mentioned` provider on SenseProviders, added to `_PROVIDER_PREDICATE_FIELDS` and `_COMPOSED_PROVIDER_FIELDS`; reachy/export/runtime.py's hand-listed Sense mirror gains it; reachy/`runtime_cues.py` `sense_cues` emits 'someone said my name' when true; docs/export-schema.md notes the key

- depends on: t2
- covers: c14, h12
- acceptance:
  - `EMPTY_SENSE`.`name_mentioned` is False; `read_perception` with a provider returning True yields True and a raising provider degrades to False; '`name_mentioned`' in `FED_SENSE_FIELDS` and in the export snapshot's keys; `sense_cues`({'`name_mentioned`': True}) contains the cue and {'`name_mentioned`': False} does not
  - the registry pin test in the style of tests/`test_behavior_self_motion.py` covers the new field; behavior rules check accepts when = {field = "`name_mentioned`", op = "`is_true`"}
  - git diff touches only reachy/behavior/sense.py, reachy/export/runtime.py, reachy/`runtime_cues.py`, docs/export-schema.md and new/edited tests for those three modules

### t4 — t4 the hearing gate follows the names: reachy/speech/engagement.py `DEFAULT_NAMES` = `SHIPPED_NAMES` (import), `ENGAGEMENT_SYSTEM_PROMPT` becomes a template rendered from the names (`render_engagement_prompt`(names)); ConversationGate/EngagementClassifier accept a zero-arg names provider (a plain tuple still works); reachy/behavior/`transcript_sense.py` takes `names_provider`=, its `_should_engage` returns (engaged, `by_name`), and the driver latches `name_mentioned` for one tick on a 'name' admission (gate label 'name' or heuristic `by_name`), exposing `peek_name_mentioned` / `as_name_mentioned_provider`; tests/`test_transcript_engagement_heuristic.py` imports `SHIPPED_NAMES` instead of its own copy

- depends on: t1
- covers: c3, h2, c4, h3, c15, h13, c26, h24, c19, h18
- acceptance:
  - with a provider returning `SHIPPED_NAMES`+('nova',), the gate admits 'nova, come here' with label 'name' and the driver's `peek_name_mentioned` reads True for exactly one tick then False; a 'context' admission sets transcript but leaves `name_mentioned` False; a provider swapped between two utterances takes effect on the second with no rebuild
  - the rendered classifier prompt contains every configured name and no 'Reachy' literal; an explicit `system_prompt` still wins
  - grep -rn '("reachy", "robot")' tests/ reachy/speech/engagement.py reachy/behavior/`transcript_sense.py` finds no tuple literal; with no provider every existing engagement/transcript test passes unchanged
  - git diff touches only reachy/speech/engagement.py, reachy/behavior/`transcript_sense.py` and their tests (`test_engagement.py`, `test_behavior_transcript_sense.py`, `test_transcript_engagement_heuristic.py`)

### t5 — t5 runtime composition + CLI observability in reachy/cli/`_commands`/behavior.py: `_compose_run_seam` binds TranscriptSenseDriver's `names_provider` to the ReloadDriver's loader.current.names and wires the `name_mentioned` provider into SenseProviders; behavior rules list / rules check / engine status render the merged names (text + --json)

- depends on: t3, t4
- covers: c15, h13, c25, h23, c20, h19
- acceptance:
  - a composition test with a fake loader shows the driver's names change after loader.reload() with no driver rebuild; SenseProviders.`name_mentioned` is the driver's peek
  - behavior rules list --json carries names == the merged tuple; rules check's summary names them; engine status --json reports names from the running engine's state
  - git diff touches only reachy/cli/`_commands`/behavior.py, reachy/behavior/engine.py or supervisor.py only if engine status needs a state key, and their tests

### t6 — t6 the other two roots read the same file: reachy/embody/attention.py `DEFAULT_NAMES` = `SHIPPED_NAMES` (import) and `_commands`/agent.py builds AttentionGate with `load_rules`(`overlay_rules_path`()).names; reachy/sleep/wakeword.py gains `DEFAULT_PHRASES`/`_resolve_phrases` (one 'hey <name>' per configured name except 'robot'; explicit phrase or `REACHY_STT_PHRASE` selects one; .phrase kept as phrases\[0\]) and reachy/sleep/wake.py + `_commands`/sleep.py thread the names from the same load

- depends on: t1, t2
- covers: c16, h14, c5, h4, c9, h8
- acceptance:
  - an agent embody composition test with an injected loader asserts the gate's names include the configured one; attention.py's import-boundary test still passes (`name_match` only); `test_the_layer_answers_to_the_same_names_the_runtime_gate_does` now pins both to `SHIPPED_NAMES`
  - WakeDetector with no phrase/env and names ('reachy','robot','nova') matches 'hey reachy' and 'hey nova' but not 'hey robot'; phrase='hey bob' matches only that; the shipped default is exactly ('hey reachy',)
  - git diff touches none of embody/engine.py:598, speech/`agent_turn.py`:98, `realtime_duplex.py`:752 prompt constants; only attention.py, `_commands`/agent.py, sleep/wakeword.py, sleep/wake.py, `_commands`/sleep.py and their tests

### t7 — t7 docs, boundary test, nova issue, release: docs/operating-reachy.md gains a names section (table example, `name_mentioned` rule example, behavior reload, the `reachy_nova` prerequisite link) and the env-table row for `REACHY_STT_PHRASE` mentions the derived phrases; explain catalog + CLAUDE.md + README updated; a test greps reachy/ for the token 'nova' (none); file the OriNachum/reachy-nova issue naming `rules_overlay.py`'s two copied sets; version-bump minor + CHANGELOG; PR via cicd to green

- depends on: t5, t6
- covers: c7, h6, c8, h7, c17, h15, c21, h20, c18, h17
- acceptance:
  - behavior rules check on the guide's example overlay exits 0; markdownlint clean; the no-peer-name test passes; grep -rn '`REACHY_ROBOT_NAMES`\|--names' reachy/ docs/ finds nothing
  - the reachy-nova issue URL appears in #177, in the guide's names section and in the PR body (satisfying the assumption c12's condition h11); version bumped; uv lock refreshed; CI test/lint/version-check + Sonar green

### t8 — t8 bench verification (operator or agent on the dev box's Lite robot at localhost:8000, or the wireless unit): write names = \["nova"\] into the overlay, behavior reload, speak 'nova, are you there' (or the monitor-speaker test vector) — journal shows a stage=transcript admission labelled 'name' and the export shows `name_mentioned` true for one tick; remove the table, reload, the same utterance drops 'not-addressed-cold'; excerpt attached to the PR (this is assumption c11's condition h10)

- depends on: t7
- covers: c1, h16, c22, h21
- acceptance:
  - two journal excerpts (with and without the table) from ONE runtime process (same PID) attached to the PR; the state.json or --export snapshot line with `name_mentioned`: true quoted
  - if the bench box's lobes realtime session is down (connect-failed in the journal, as on the robot today), the run is recorded BLOCKED with the journal line, not rounded up

## Risks

- [unknown_nonblocking] Soundex residual: 'navy' ties 'nova' (N100) and no guard closes it; accepted per the frame's park, re-checked by t1's tests only in the documented direction. (task t1)
- [follow_up] `reachy_nova` prerequisite: until its `rules_overlay.py` learns the names table and the `name_mentioned` field, a box carrying either makes nova's managed-block writes fail. Sequencing: file the nova issue in t7 and land nova's change before writing a names table on the deployed robot. (task t7)
- [unknown_nonblocking] Bench box: the deployed wireless robot's hearing session showed connect-failed (lobes realtime refused) in today's journal, so t8 may only be runnable on the dev box's Lite, whose transcript leg must also reach a live realtime gateway. If neither hears, t8 is BLOCKED-ON-GATEWAY. (task t8)
