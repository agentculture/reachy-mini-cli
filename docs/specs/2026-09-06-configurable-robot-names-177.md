# configurable robot names #177

> A peer harness can tell the robot which names it answers to — one configured list feeds every place reachy-mini-cli matches the robot's name (the transcript engagement gate, the embodiment attention gate, the fuzzy matcher, the classifier prompt, the sleep wake phrase) — without this repo ever learning a peer's name (reachy-mini-cli #177, split from #175 / reachy-nova #25)
> instruction: Add a names table to the rules schema (union-merged, fail-closed), derive every existing names default from RulesConfig.names, feed the transcript driver a live names provider bound to the reload loader, add the `name_mentioned` one-tick sense field across the five registries, read the same file in agent embody and sleep, re-land the name-agnostic matcher guards, document the table and the field, then bench-verify with behavior reload.

## Audience

- A peer harness (`reachy_nova` today) that wants the robot to answer to its own persona's name, and the operator editing the box overlay; secondarily rule authors, who gain a `name_mentioned` field to react to.

## Before → After

- Before: The robot answers only to 'reachy' and 'robot', spelled in four places in this repo with no configuration path (the names= seams exist but composition never passes them); a rule can key on transcript (any admitted words) but cannot tell 'someone said my name' from 'someone continued a conversation'; the sleep wake phrase is fixed at 'hey reachy'.
- After: An operator or peer adds a names table to <`state_dir`>/behavior/rules.toml and runs behavior reload; from the next tick the transcript gate, its heuristic and its classifier prompt answer to the added names too, a rule may key on `name_mentioned`, the snapshot export and the embodiment cue show it, and on their next start agent embody's attention gate and sleep's wake phrases follow the same file. Nothing in this repo names a peer.

## Why it matters

- The runtime already treats rules.toml as the one operator-owned, hot-reloadable, peer-writable configuration surface; a second channel (env var, flag) for one value would be a second thing to document, deploy and get out of sync, while a name that only the transcript text could reveal leaves rule authors re-deriving the gate's own decision.

## Requirements

- The names live in the rules file: a new top-level names table in rules.toml (shipped `default_rules.toml` declares the shipped pair; the box overlay ADDS more). rules.py's `_TOP_LEVEL_FIELDS` gains it, RulesConfig gains a names: tuple\[str,...\] field, `from_dict` validates it fail-closed (a list of non-blank single words, lower-cased, de-duplicated, no whitespace/punctuation — a bad entry refuses the whole file exactly like a bad rule), and `merge_rules` UNIONS shipped + overlay in order so a configured list extends the shipped pair. RulesConfig.names is the ONLY place the shipped tuple is spelled; the three `DEFAULT_NAMES` tuples and `is_name_match`'s default become derived from it.
  - honesty: `load_rules` over a file containing names = \["nova", "Nova", "nova"\] yields ('reachy','robot','nova'); names = \["two words"\] / \[""\] / \[42\] / "nova" each refuse the WHOLE file with a CliError naming the entry; a missing table yields exactly the shipped pair; grep -rn '("reachy", "robot")' reachy/ finds only `default_rules.toml`'s counterpart, no Python tuple literal.
- The resolved names are PASSED at composition into every seam that already accepts them and is never fed today: TranscriptSenseDriver(names=) at `_commands`/behavior.py:2394 (which forwards to ConversationGate(names=) at `transcript_sense.py`:410 and to its own `_should_engage` heuristic at :765), the AttentionGate(names=) built in `_commands`/agent.py, and `is_name_match`'s default. The three duplicated `DEFAULT_NAMES` tuples (engagement.py:121, `transcript_sense.py`:226, attention.py:113) and `name_match.py`:237's literal become imports of the resolver's shipped default, so the drift-guard test (`test_embody_attention.py`:423) pins one source instead of two copies.
  - honesty: With names = \["nova"\] in the overlay, TranscriptSenseDriver's gate admits 'nova, come here' with label 'name', AttentionGate opens from cold on it, and `is_name_match`('nova') is True when passed the resolved names; with no overlay all three behave exactly as today (pinned by the existing suites unchanged).
- The engagement classifier's system prompt (engagement.py:166 `ENGAGEMENT_SYSTEM_PROMPT`, hardcoded 'named Reachy') is rendered from the resolved names, so the LLM judge and the fast-path agree on who the robot is.
  - honesty: EngagementClassifier's rendered system prompt contains every configured name and no hardcoded 'Reachy' literal; a test renders it with names ('reachy','robot','nova') and asserts all three appear.
- The sleep wake phrase follows the names: with no --wake-phrase and no `REACHY_STT_PHRASE`, the default becomes one 'hey <name>' per configured name (a tuple of phrases, the donor commit a8535a2's `DEFAULT_PHRASES`/`_resolve_phrases` shape); an explicit phrase or `REACHY_STT_PHRASE` still selects exactly one, unchanged.
  - honesty: WakeDetector built with no phrase and no `REACHY_STT_PHRASE` against names ('reachy','robot','nova') matches transcripts containing 'hey reachy' OR 'hey nova' (and not 'hey robot' unless decided otherwise — see the question); with phrase='hey bob' it matches only that.
- The matcher hardening from the dropped commit a8535a2 is re-landed name-agnostically in `name_match.py`: the clitic stem match (a token 'nova's' matches on its stem) and the four-letter fuzzy floor (`_MIN_FUZZY_WORD_LEN`=4), with that commit's n-family rows ('now','no','know','nah','not','novel','november','nowhere','nothing','never' reject; 'nova','Nova, come here','nova's over here' accept) added to tests/`test_name_match.py` as a CONFIGURED-name case, never as a default.
  - honesty: tests/`test_name_match.py` carries the n-family rows under a CONFIGURED names=('reachy','robot','nova') fixture: 'now','no','know','nah','not','novel','november','nowhere','nothing','never' reject and 'nova','Nova, come here',"nova's over here" accept; every pre-existing row still holds under the default names; 'reachy's' also matches by the same stem rule.
- Operator surface is the overlay file itself plus behavior reload: no --names flag and no `REACHY_ROBOT_NAMES` env var. docs/operating-reachy.md's rules.toml walkthrough (#the-rulestoml-walkthrough) documents the names table and the `name_mentioned` field; behavior rules check lints a names entry the same way it lints a rule; the explain catalog entry for behavior rules mentions both.
  - honesty: docs/operating-reachy.md's rules.toml walkthrough shows a names table example and a react rule keyed on `name_mentioned`; behavior rules check on that example exits 0; the explain catalog's behavior rules entry mentions both.
- Being named is a SENSE EVENT: Sense gains `name_mentioned`: bool (one-tick latch, like face and `pat_event`), set by TranscriptSenseDriver when an utterance was admitted BY NAME (ConversationGate's 'name' label, or the heuristic's whole-word hit — `_should_engage` returns which), never on a 'context' admission. Wired in the same change everywhere the registry demands: `_COMPOSED_PROVIDER_FIELDS` + `_PROVIDER_PREDICATE_FIELDS` in sense.py, rules.`SENSE_FIELDS` (so a rule may key when = { field = "`name_mentioned`", op = "`is_true`" }), the snapshot export's hand-listed Sense fields in export/runtime.py, and `runtime_cues`.`sense_cues` (a closed-vocabulary cue such as 'someone said my name') so the embodiment layer and the feed both see it.
  - honesty: Sense.`name_mentioned` defaults False; after one admitted-by-name utterance it reads True for exactly one tick then False; a 'context' admission leaves it False while transcript is set; `name_mentioned` is in `FED_SENSE_FIELDS`, rules.`SENSE_FIELDS`, the export snapshot's keys and produces a sense cue; `test_behavior_self_motion`-style registry pins cover it.
- The names take effect LIVE: TranscriptSenseDriver (and through it ConversationGate, the heuristic and the classifier prompt) reads the names through an injected zero-arg provider bound at composition to the ReloadDriver's loader.current.names, so behavior reload changes who the robot answers to between ticks with no restart — the same path a rule edit already takes. The gate's prompt is re-rendered when the names change.
  - honesty: A test builds the driver with a names provider, swaps the provider's return from ('reachy','robot') to ('reachy','robot','nova') between two utterances, and the second 'nova …' utterance is admitted by name with no driver rebuild; the rendered classifier prompt after the swap names nova.
- The two other roots read the SAME file: agent embody builds its AttentionGate with names from `load_rules`(overlay).names at start (it already imports reachy.behavior.rules in embody/tools.py; hot reload there is a non-goal), and sleep's default wake phrases become one 'hey <name>' per configured name from the same load (an explicit --wake-phrase or `REACHY_STT_PHRASE` still selects exactly one).
  - honesty: agent embody composition passes `load_rules`(overlay).names into AttentionGate (a test injects a fake loader and asserts the gate's names); sleep's WakeDetector default phrases derive from the same load; neither root imports anything new from reachy.speech.engagement.

## Honesty conditions

- End to end on a bench box: write names = \["nova"\] into the overlay, run behavior reload, say 'nova, are you there' into the mic (or the monitor-speaker test vector) — the journal shows a transcript admission labelled 'name', the snapshot export shows `name_mentioned` true for one tick, and a react rule keyed on `name_mentioned` fires; delete the table, reload, repeat — the same utterance is dropped 'not-addressed-cold'.
- A test greps reachy/ (source only) for the token 'nova' and finds nothing; tests and docs use it only as a configured value.
- git diff of the PR touches none of the three persona prompt constants.
- tests/`test_zero_llm_boundary.py` passes unchanged (no edit to its expected sets), and tests/`test_embody_attention.py`'s import-boundary test still finds no engagement/llm/stt module in attention's closure.
- The bench run (behavior engine run on the dev box or the robot with the overlay) shows a \[SENSE stage=transcript\] line with reason/label 'name' for a 'nova …' utterance after behavior reload, with no process restart between the two overlays.
- The operating guide's names section states the `reachy_nova` prerequisite (its overlay validator must accept the names table) and links the reachy-nova issue filed for it.
- An issue exists on OriNachum/reachy-nova naming `rules_overlay.py`'s `_TOP_LEVEL_FIELDS` copy and the names key, linked from #177 and from the operating guide.
- `reachy_nova` needs to write one TOML table and call behavior reload — no new intent kind, no new env var, no new CLI flag — and its issue for the validator copy is the only change on its side.
- On main before the change: grep finds four spellings of the pair, no composition root passes names=, rules.`SENSE_FIELDS` has no `name_mentioned`, and wakeword.`DEFAULT_PHRASE` is a single string — each is asserted by the tests that will flip.
- Pinned by h1 (the file), h2 (the three gates), h12 (the field), h13 (hot reload), h14 (the other two roots) and h7 (no peer name) together; no claim in the after-state lacks one of those tests.
- The operating guide's names section states the one-surface rationale and points at behavior reload; no `REACHY_`\*NAMES env var appears anywhere in reachy/ or docs/.
- uv run pytest -n auto exits 0 (bar the pre-existing live-gateway scene test), uv run teken cli doctor . --strict exits 0, black/isort/flake8/bandit/markdownlint clean, and the bench journal excerpt is attached to the PR.

## Success signals

- Offline: a rules file with names = \["nova"\] loads, merges to ('reachy','robot','nova'), and a driver-level test with the gate admits 'nova, are you there' by name and latches `name_mentioned` for exactly one tick; a malformed entry refuses the file and RulesLoader keeps last-good; behavior rules check accepts when = {field = "`name_mentioned`"}; the tuple drift test now pins one source. Bench: behavior engine run with that overlay plus behavior reload changes the accepted name with no restart, visible as a \[SENSE stage=transcript\] 'name' admission in the journal. Full suite, teken doctor and lint green.

## Scope / boundaries

- This repo's source and tests contain no peer name: 'nova' appears only as a CONFIGURED value inside test cases and docs examples, never in a default, a prompt or a tuple literal (a grep-based test pins it).
- The three persona prompts that say 'You are Reachy Mini' (embody/engine.py:598, speech/`agent_turn.py`:98, `realtime_duplex.py`:752 — the last already overridable via `REACHY_EMBODY_VOICE_PROMPT`) are NOT rewritten: they describe the body, not the gate, and a peer that wants a persona name supplies its own prompt (`reachy_nova` already does: config/persona/nova.md).
- engagement.py keeps its single LLM edge and `transcript_sense.py` stays its only importer: the resolver is a leaf with no LLM import, so tests/`test_zero_llm_boundary.py`'s equality pins are untouched; attention.py keeps importing `name_match`, never engagement.
- `reachy_nova`'s harness/`rules_overlay.py` copies this repo's `_TOP_LEVEL_FIELDS` and refuses a rules file with an unknown top-level table, so a names table in the overlay makes ITS managed-block writes fail until that copy learns the key. That is a `reachy_nova` change (an issue on OriNachum/reachy-nova, filed from this plan), not something this repo can paper over; the docs name it as the peer's prerequisite.

## Non-goals

- No env var and no CLI flag for names (rules.toml is the single source, hot-reloadable); no per-name mishearing lists (the matcher stays generic); the persona prompts are not rewritten.

## Assumptions

- `reachy_nova`'s round-25 review (its .devague/reviews, claim c15) found the runtime's name gate is NOT on the robot's live path today: greet-when-addressed is tombstoned in the box overlay and the sleep lane is not running. So the success signal is the offline suite plus a bench run of the gate with a names table containing nova, not a live robot behaviour change — though the new `name_mentioned` event gives the box a reason to re-enable a greet rule keyed on it.
- `reachy_nova` consumes this by writing a names table into the runtime's overlay (its harness/`rules_overlay.py` already edits that file, preserving the operator's head/tail around its managed block) and running behavior reload; one documented TOML key is the contract between the repos.

## Scope exploration

- `s1` — `reachy/speech/{engagement,name_match}.py, reachy/behavior/transcript_sense.py, reachy/embody/attention.py`: Four spellings of ('reachy','robot'): three `DEFAULT_NAMES` tuples (one pinned equal to another by `test_embody_attention.py`:423) plus `is_name_match`'s default argument. All four sit behind a names= parameter that no composition root passes. The classifier prompt hardcodes 'named Reachy' at engagement.py:168; the transcript heuristic at :765 does a whole-word match on the same tuple.
  - seeds: `c2`, `c3`, `c4`
- `s2` — `reachy/cli/_commands/behavior.py:2394 and _commands/agent.py (composition roots)`: TranscriptSenseDriver is built with media/realtime/classifier/`mute_until` only; AttentionGate is built without names. `resolve_attention_window_s` (embody/engine.py:876) is the precedent: explicit > process env > default, env read per call, never environment.d, flag declared on embody AND start/restart with SUPPRESS when inherited.
  - seeds: `c3`, `c7`
- `s3` — `reachy/sleep/wakeword.py + wake.py + _commands/sleep.py:574`: One phrase: `DEFAULT_PHRASE` 'hey reachy', `_resolve_phrase`(override) > `REACHY_STT_PHRASE` > default; the CLI already threads a `wake_phrase` arg. The donor commit a8535a2 turned this into `DEFAULT_PHRASES` + `_resolve_phrases` with .phrase kept for back-compat — reusable shape.
  - seeds: `c5`
- `s4` — `dropped commit a8535a2 (reachable from feat/reachy-nova-25-names-select-face reflog)`: Its `name_match.py` hardening (clitic stem, four-letter fuzzy floor) is name-agnostic and tested; its tuple edits are the part #177 refuses. Known residual it documents: 'navy' ties 'nova' on Soundex N100 and score — no guard closes it.
  - seeds: `c6`
- `s5` — `reachy_nova (harness/supervisor.py --env-file, harness/bus.py overlay writer, .devague/reviews round-25 c15/c16)`: The peer already configures the runtime by env file + overlay writes; its own c16 planned to hardcode 'nova' in this repo, which #175's operator decision overrode. Its c15 evidence: the runtime name gate has not fired in two days on the deployed box — the knob is latent there.
  - seeds: `c11`, `c12`
- `s6` — `reachy/behavior/rules.py:459 (fail-closed top-level fields), default_rules.toml overlay contract`: A \[names\] table in rules.toml would need a schema extension but would be hot-reloadable via behavior reload and peer-writable today; deferred as a second config surface — recorded as the alternative.
  - seeds: `c13`
- `s7` — `docs/operating-reachy.md:743-759 env table, README.md:271, explain catalog`: Every knob is documented in the env table with its module; --attention-window is the model row. No names row exists.
  - seeds: `c7`
- `s8` — `reachy/behavior/rules.py (_TOP_LEVEL_FIELDS:222, RulesConfig:428, from_dict:459-505, merge_rules, RulesLoader.reload:1001)`: Schema is fail-closed on unknown top-level keys, so names must be declared there; modes is the precedent for a non-rule top-level table with its own validator; `merge_rules` is per-rule-id today and needs a union rule for names. RulesLoader.reload keeps last-good on a bad file, so a bad names entry can never take the robot's names away.
  - seeds: `c2`
- `s9` — `reachy/behavior/reload_driver.py (ReloadDriver wraps RulesLoader; loader.current is the live config)`: behavior reload is a spool command applied between ticks; ReloadDriver exposes loader.current, so a names provider bound to it is hot for free — no new reload plumbing.
  - seeds: `c15`
- `s10` — `reachy/behavior/sense.py registry (_COMPOSED_PROVIDER_FIELDS:399, _PROVIDER_PREDICATE_FIELDS:359), rules.SENSE_FIELDS:148, export/runtime.py:124 (hand-listed Sense mirror), runtime_cues.sense_cues:184`: A new rule-visible sense field needs five coordinated edits — provider field, predicate registry, rules `SENSE_FIELDS`, the export mirror, the cue vocabulary — and the linter (behavior rules check) lies in one direction if any is missed; `test_behavior_self_motion.py` is the precedent that pins the set.
  - seeds: `c14`
- `s11` — `reachy/behavior/transcript_sense.py:_handle:675-699 + _decide:720-756 + peek:774`: The engage path already has the gate's label ('name'/'context'/'engaged-heuristic') at the moment the latch is queued, and the latch is adopted once per tick from `_ready` — a `name_mentioned` latch rides the same queue item. The heuristic does not yet say WHY it engaged.
  - seeds: `c14`, `c15`
- `s12` — `reachy_nova/reachy_nova/harness/rules_overlay.py (validate_rules_document:284, _split_overlay:364, _atomic_write:428)`: The peer edits the overlay by managed block, preserving operator head/tail — so a names table survives its writes — but its whole-file validator copies `_TOP_LEVEL_FIELDS` and would refuse the file. Cross-repo prerequisite.
  - seeds: `c17`, `c12`

## Decisions

- Wake phrases derive 'hey <name>' for every configured name except the generic 'robot', so the shipped default remains exactly 'hey reachy'. (Ori, 2026-09-06, resolving q4.)

## Open parks

- [unknown_nonblocking] Fuzzy-match quality for short configured names is bounded by Soundex: 'navy' ties 'nova' and no orthographic guard separates them. Acceptable residual for a name only a peer configures; a proper fix is a per-name explicit mishearing list, a separate change.
