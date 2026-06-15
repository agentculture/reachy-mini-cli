# reachy-mini-cli now ships a coherent operating guide: the README opens with what Reachy Mini can do and a single noun map (daemon/listen/think/say/pat/sleep/export), the single-SDK-owner model is documented as a first-class concept, there is a step-by-step 'operate Reachy live' guide, the asoundrc mic-array gotcha and the out-of-repo renderer boundary are written down once, and CLAUDE.md's architecture section is reorganized for navigability

> reachy-mini-cli now ships a coherent operating guide: the README opens with what Reachy Mini can do and a single noun map (daemon/listen/think/say/pat/sleep/export), the single-SDK-owner model is documented as a first-class concept, there is a step-by-step 'operate Reachy live' guide, the asoundrc mic-array gotcha and the out-of-repo renderer boundary are written down once, and CLAUDE.md's architecture section is reorganized for navigability

## Audience

- operators of Reachy Mini: humans bringing the robot up live, AI agents (Culture mesh + Claude Code) that drive the CLI, and contributors who rely on CLAUDE.md to navigate the architecture

## Before → After

- Before: docs accreted feature-by-feature: README is 516 lines of per-noun sections with no top map; CLAUDE.md is a 238-line architecture wall; the single-consumer SDK constraint and the asoundrc fix are tribal knowledge rediscovered the hard way
- After: a new operator goes from install to a live, reacting robot following one README path, and can see at a glance what Reachy can do and which nouns can run at the same time

## Why it matters

- hard-won operational facts (sdk single-owner, asoundrc) are unwritten, so humans and agents repeatedly trip over sdk resource conflicts and audio-source failures

## Requirements

- README opens with a 'what Reachy Mini can do' overview and a single noun-map table covering all seven nouns (daemon/listen/think/say/pat/sleep/export), replacing the accreted per-feature sections as the primary entry point
  - honesty: the noun map is COMPLETE: every robot noun is represented (headline daemon/listen/think/say/pat/sleep/export PLUS the daemon-client nouns device/app/move and the behavior nouns vision/demo-mode/behavior); export gets its own row even though it is a think flag; each entry resolves to a real CLI noun/verb
- the single-SDK-owner model is documented as a first-class concept: one mic media session is single-consumer and the head is one resource, so listen/think/pat/sleep (and any sense feed) are mutually exclusive on the sdk transport; includes an explicit conflict matrix and the #43 'fold pat into listen' pattern as the resolution
  - honesty: the conflict matrix correctly states which noun pairs cannot co-run on the sdk transport, matching the code reality (one single-consumer mic media session + one head driven by a serial MotionQueue)
- a step-by-step 'operate Reachy live / real mode' guide: daemon up -> choose a mode (listen/think/etc on sdk) -> how to switch transports -> how to verify (daemon status, listen-log reactions)
  - honesty: the live-ops guide's commands run as written on a real bring-up: daemon start -> status reports healthy -> a chosen mode visibly reacts -> 'reachy <noun> --transport http' switching works
- the ~/.asoundrc mic-array gotcha is documented once: symptom ('No Reachy Mini Audio Source card found / using default audio source'), cause (PulseAudio/PipeWire not exposing the USB card as a source), and fix (daemon write_asoundrc_to_home -> reachymini_audio_src + restart -> 'Using ALSA device reachymini_audio_src for capture')
  - honesty: the documented asoundrc symptom string and fix match the daemon's ACTUAL log output and its write_asoundrc_to_home behavior (verified against the daemon, not guessed)
- the export feed is documented with a link to docs/export-schema.md, and the renderer (reTerminal bridge) is documented as living OUT OF REPO by design (the export decoupling boundary) with a pointer to where it lives
  - honesty: docs/export-schema.md exists and is linked from the README, and the renderer pointer names the actual out-of-repo location of the reTerminal bridge
- CLAUDE.md's long architecture section is reorganized for navigability (the 238-line wall becomes scannable) and updated to reflect #43 (pat folded into listen)
  - honesty: the CLAUDE.md reorg preserves every architectural fact (restructure, not delete) and its listen section reflects #43 (pat folded into listen)
- the revamp ADDS content beyond relocation: at least one in-repo diagram (mermaid in markdown, renders on GitHub) of the single-SDK-owner / resource-conflict model, plus a richer technical layer that documents each noun's capability AND its key internals (sense source, motion path, transports) navigably as new docs/ pages, cross-linked from the noun map
  - honesty: the diagram is valid mermaid that renders on GitHub and accurately reflects the resource model (one mic session + one head); every new technical doc page is cross-linked from the noun map or operating guide so nothing is orphaned
- a single environment-variable reference table collects every REACHY_* and XDG/state-dir var in one place (REACHY_TRANSPORT/REACHY_BASE_URL, REACHY_TTS_*, REACHY_LLM_*, REACHY_STT_*, REACHY_STATE_DIR/XDG_STATE_HOME, demo-mode.json path), replacing the values scattered across README + CLAUDE.md
  - honesty: the env-var table lists every variable the code actually reads (cross-checked against the source), with default + meaning for each
- the stale 'No Reachy / robot functionality exists yet' framing in CLAUDE.md (and the 'still the unmodified template' premise) is corrected to reflect the now-extensive robot capability set
  - honesty: no sentence in the revised CLAUDE.md claims the repo lacks robot functionality; the scaffold/template framing is replaced with an accurate current-state description
- the operating guide includes a troubleshooting section keyed to the real failure modes: missing [sdk]/[daemon] extra, no daemon running, USB-audio contention (asoundrc), sdk single-owner conflicts, plus the exit-code policy (0 ok / 1 user / 2 env / 3+ reserved)
  - honesty: each troubleshooting entry names the real symptom string or error a user sees and the actual remediation, matching the CliError messages and daemon logs

## Honesty conditions

- the shipped docs deliver every announced piece: README overview + complete noun map, single-SDK-owner concept, live-ops guide, asoundrc gotcha, renderer boundary, reorganized CLAUDE.md, and the diagram + technical layer
- the docs serve all three audiences (human bring-up, agent driving the CLI, contributor navigating CLAUDE.md) without a separate onboarding path
- the cited starting state is accurate at writing time (README ~516 lines, CLAUDE.md architecture section ~238 lines, gotchas only in code/tribal knowledge)
- a reader can identify from the README alone what Reachy can do and which nouns can run at the same time
- the two flagship gotchas (sdk single-owner, asoundrc) are the ones that actually recur in practice for operators
- the PR touches only docs/markdown — no code or CLI-behavior change, no renderer relocation, no generated API reference
- following only the README + operating guide gets a new operator to a live, reacting robot without hitting an undocumented sdk-conflict or audio-source failure

## Success signals

- a new operator brings the robot up live following only the README and the live-ops guide, without hitting an undocumented sdk-conflict or audio-source failure

## Scope / boundaries

- docs-only: no new robot features, no moving the external renderer into the repo, no auto-generated API reference, no changing CLI behavior

## Decisions

- the coherent operating guide lives in a NEW dedicated docs/ file (operating-reachy.md); README gets a lean overview + noun map + quickstart that links into it
- the single-SDK-owner model is documented inline (full, operator-facing) in the operating guide; CLAUDE.md carries a short contributor-facing note plus the conflict matrix that links to the full version
- CLAUDE.md architecture is restructured in place (one file, scannable subsections + tables), not split into docs/architecture/
- done bar = docs accurate against code + a colleague review pass; the live on-robot bring-up verification of the guide's commands (honesty h3/h4) is an explicit follow-up, not a blocker for this PR
