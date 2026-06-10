# Reachy Mini now thinks with its body: while `think` cognition runs, the robot moves expressively in step with its spoken thoughts, the always-alive idle motion steps back so those expressions read clearly, and the expression vocabulary is adaptive — each named expression is tuned to be visually distinct and to actually convey what it means.

> Reachy Mini now thinks with its body: while `think` cognition runs, the robot moves expressively in step with its spoken thoughts, the always-alive idle motion steps back so those expressions read clearly, and the expression vocabulary is adaptive — each named expression is tuned to be visually distinct and to actually convey what it means.

## Audience

- Someone running `reachy-mini-cli think` on a live Reachy Mini who wants the robot to look like it's thinking, not just talking — plus the developer tuning the expression vocabulary.

## Before → After

- Before: `think` is audio-only: it streams spoken thoughts via TTS but produces zero movement. Any ambient motion comes from a separately-running `listen`/demo-mode idle loop that knows nothing about what think is saying, so the robot's body and its thoughts are uncoordinated.
- After: While `think` runs, it drives expressive movement in step with each spoken thought, and the always-alive idle motion is quieted so those expressions are legible rather than drowned out by ambient breathing/gaze wander.

## Why it matters

- A talking head that doesn't move reads as dead; idle motion that ignores the speech reads as random. Coordinated, legible expression is what makes the robot feel like it's actually thinking.
- Stillness IS the thinking posture. Like a person thinking hard, the robot should move LESS while `think` runs — quieter, more focused, turned inward. This is the governing principle: reducing idle is not just for legibility, it's because thinking looks like calm. Expressions are sparse, deliberate punctuation against that stillness, not a stream of constant gesturing.

## Requirements

- The catalog ships a distinctness check: a tool/command that measures whether two expressions are visually different enough, so 'distinct enough' is verifiable rather than asserted.
  - honesty: A `think expressions check` command exists and flags at least one too-similar pair on a deliberately-duplicated catalog, and reports clean on the shipped distinct set.
- While think is active, the always-alive idle motion is reduced (lower energy / amplitude), not eliminated — the robot still breathes but the ambient wander backs off so expressions dominate.
  - honesty: While think is active the robot still breathes (idle not zero) but at a measurably lower amplitude/rate than standalone listen idle.
- think becomes a motion producer while active: each parsed *…* marker maps to an expression that is pushed onto the existing serial goto/minjerk motion queue (head/antenna/body pose), driven in step with the spoken "…" it accompanies.
  - honesty: Each *…* marker yields exactly one expression move on the existing serial goto/minjerk queue (no new motion path); unit test feeds a marked stream and asserts the queued MotionActions.
- Expression catalog is an editable data file keyed by emoji (with optional name aliases), mapping each emoji to head/antenna/body pose params. The cognition prompt advertises the available emoji vocabulary so the LLM stays in-catalog; an unknown/absent marker falls back gracefully (neutral) rather than erroring.
  - honesty: The catalog round-trips: editing an emoji->pose entry changes the performed pose with no code change, and an unknown/absent marker yields the neutral fallback; asserted by test.
- Expressive movement while thinking is sparse and rate-limited: only emitted on *…* markers (not every sentence), with calm low-amplitude defaults, so the baseline is stillness and each expression stands out. The idle baseline drops to a low-energy 'focused' breathe rather than the full alive-wander.
  - honesty: A marked stream with N markers produces at most N expression moves and the idle baseline is the low 'focused' breathe; asserted by test.
- The cognition signal is a simple stdlib file flag (write/read/clear) consistent with how daemon.py and the supervisors already track state under $REACHY_STATE_DIR — no new dependency, robust to think crashing (stale-flag tolerated/cleared on next start).
  - honesty: The cognition flag is a stdlib file under $REACHY_STATE_DIR written on start and cleared on exit; a stale flag from a crash is overwritten on next start; asserted by test.

## Honesty conditions

- Demoing `reachy-mini-cli think` on a live robot, an observer sees expressive movement timed to the spoken thoughts, a visibly calmer body than full idle, and can name distinct expressions.
- Both personas are served without a code change for the developer one: a runtime user just runs think; a developer tunes expressions by editing the catalog data file.
- Verifiable in-repo: think.py issues no transport.move_* calls today and all ambient motion lives in listen/demo-mode/idle.
- While think runs, the serial motion queue receives expression moves sourced from think, and the idle baseline is measurably lower-energy than standalone listen idle.
- A motion-off vs motion-on comparison makes the motion-on run read as 'thinking' to an observer.
- The shipped surface contains no vision-driven expression and no cross-session mood store — scope stays within think's own spoken output.
- Observer test passes: distinct expressions are told apart by sight AND each matches its thought, and idle does not visibly compete.
- Measured: total motion amplitude/rate while think runs is lower than idle-alone; expression moves are the exception against a still baseline.

## Success signals

- An observer watching `think` run can tell distinct expressions apart by sight, and each expression visibly matches the thought it accompanies; the idle motion no longer competes with them.

## Scope / boundaries

- Not a general emotion/affect model and not vision-driven expression. Scope is: think drives a small, named expression vocabulary in step with its own spoken output, and tones down idle while it does. No facial recognition, no mood persistence across sessions.

## Decisions

- Output convention: the cognition LLM interleaves *…* markers (an emoji, e.g. *🤔*, or a short action) for expression and "…" for speech. think speaks ONLY the quoted text; each *…* marker drives an expression. This reuses the existing sentence-streaming overlap — markers are parsed/stripped from the stream like tags.
- Motion ownership: while `think` is active it owns expressive motion AND writes a shared 'cognition active' signal under $REACHY_STATE_DIR (e.g. cognition.on). A separately-running `listen`/idle loop reads that signal and drops its idle energy to a low 'focused' level — so when thinking, the body quiets even if listen is also running. think clears the signal on exit.

## Open / follow-up

- Runtime self-tuning of expressions from a legibility feedback signal — deferred; no on-robot legibility sensor exists today. Revisit as a follow-up.
