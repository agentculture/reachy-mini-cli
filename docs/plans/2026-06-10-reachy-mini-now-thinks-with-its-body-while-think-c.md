# Build Plan — Reachy Mini now thinks with its body: while `think` cognition runs, the robot moves expressively in step with its spoken thoughts, the always-alive idle motion steps back so those expressions read clearly, and the expression vocabulary is adaptive — each named expression is tuned to be visually distinct and to actually convey what it means.

slug: `reachy-mini-now-thinks-with-its-body-while-think-c` · status: `exported` · from frame: `reachy-mini-now-thinks-with-its-body-while-think-c`

> Reachy Mini now thinks with its body: while `think` cognition runs, the robot moves expressively in step with its spoken thoughts, the always-alive idle motion steps back so those expressions read clearly, and the expression vocabulary is adaptive — each named expression is tuned to be visually distinct and to actually convey what it means.

## Tasks

### t1 — Expression catalog: editable emoji-keyed data file + loader

- covers: c15, h11, c2, h2
- acceptance:
  - reachy/speech/expressions.toml ships a starter set keyed by emoji; loader returns head/antenna/body pose params per emoji
  - unknown/absent emoji returns the neutral fallback pose, not an error (unit test)
  - editing an emoji pose entry in the data file changes the loaded pose with no code change (round-trip test)
  - the data file is human-editable (stdlib tomllib, no new dep) and documented so the developer-persona can tune it

### t2 — Marker parser: split *emoji* expression markers from "speech"

- acceptance:
  - parser splits a mixed stream into ordered events: expression markers from *…* and speech text from quoted segments; only quoted text is returned as speech
  - text outside markers/quotes is dropped from speech; malformed/unclosed markers degrade gracefully without crashing (unit test)
  - parser is streaming-friendly (incremental feed) so it composes with the existing sentence-streaming overlap

### t3 — Cognition signal: stdlib file flag under $REACHY_STATE_DIR

- covers: c19, h14
- acceptance:
  - exposes write/clear/is_active over a stdlib file flag under $REACHY_STATE_DIR (no new dependency)
  - a context manager writes the flag on enter and clears on exit; a stale flag from a prior crash is overwritten on next start (unit test)

### t4 — Distinctness check: score + flag too-similar expressions

- depends on: t1
- covers: c10, h8
- acceptance:
  - a distinctness function scores any two catalog expressions over head/antenna/body params and flags pairs below a threshold
  - on a deliberately-duplicated catalog it flags at least one too-similar pair; on the shipped distinct set it reports clean (unit test)

### t5 — ExpressionProducer: marker -> one MotionAction on the serial queue, sparse

- depends on: t1, t2
- covers: c14, h10, c17, h13
- acceptance:
  - maps a parsed expression marker + catalog entry to exactly one MotionAction pushed onto the existing serial goto/minjerk queue (no new motion path)
  - a marked stream with N markers produces at most N expression moves (sparse/rate-limited), not one per sentence (unit test)
  - expression moves use calm low-amplitude defaults so each stands out against stillness

### t6 — Idle reduction: read cognition signal, drop to low 'focused' breathe

- depends on: t3
- covers: c11, h9, c16, h12
- acceptance:
  - when the cognition signal is active, the idle loop scales energy down to a low 'focused' breathe; idle is reduced, not zero (still breathes)
  - measured idle motion amplitude/rate while the signal is active is strictly lower than standalone listen idle (unit test over produced poses)

### t7 — Cognition loop integration: parse markers, speak only quoted text, drive expressions

- depends on: t2, t5
- covers: c4, h4
- acceptance:
  - the cognition run-loop feeds the LLM stream through the marker parser, speaks ONLY the quoted text via TTS, and invokes an expression callback per marker
  - while running, expression moves sourced from think reach the serial motion queue (integration test with a fake transport/queue)

### t8 — think CLI wiring: signal lifecycle, prompt vocabulary, expressions verbs, boundary

- depends on: t1, t3, t4, t5, t7
- covers: c3, h3, c6, h6
- acceptance:
  - think run writes the cognition signal on start and clears it on exit; the LLM prompt advertises the available emoji vocabulary from the catalog
  - adds 'reachy-mini-cli think expressions' (list) and 'reachy-mini-cli think expressions check' (distinctness) verbs with --json, following the existing error/output contract
  - boundary preserved by an import/behaviour test: think drives motion only via the queue/producer (no direct transport.move_* in the cognition path), adds no vision-driven expression and no cross-session mood store, and say stays a dumb TTS pipe

### t9 — Live observer verification + demo entrypoint (manual, on robot)

- depends on: t6, t7, t8
- covers: c1, h1, c5, h5, c7, h7
- acceptance:
  - on a live robot an observer confirms expressive movement is timed to the spoken thoughts and the body is visibly calmer than full idle (manual)
  - a motion-off vs motion-on comparison reads as 'thinking'; distinct expressions are told apart by sight and each matches its thought (manual)
  - a demo entrypoint/script exists to drive a scripted marked stream for this verification

### t10 — Docs + version bump for the think-with-its-body feature

- depends on: t8
- acceptance:
  - README/CLAUDE.md document the *emoji*/quoted-speech convention, the expression catalog file, and the cognition signal; CHANGELOG entry + pyproject version bump per repo policy

## Risks

- [unknown_nonblocking] Exact emoji->pose starter values are unknown — ship a small starter catalog and tune on the robot against the distinctness check; values are not gating. (task t1)
- [unknown_nonblocking] Exact distinctness metric/threshold needs tuning — start with a weighted distance over head/antenna/body params; refine on real expressions. (task t4)
- [unknown_nonblocking] Live observer verification (t9) needs hardware + human judgment — it cannot be CI-gated; it is the operator's manual acceptance gate. (task t9)
- [follow_up] Runtime self-tuning of expressions from a legibility feedback signal is deferred — no on-robot legibility sensor exists today.
