# Build Plan — Reachy Mini's live hearing now knows when you're talking TO it: under `listen --live --transcribe` it stays quiet through ambient chatter and only speaks up when an utterance is addressed to the robot or is something it can genuinely help with — judged by the LLM in conversational context, not a word-count timer — and it recognises its own name reliably even when the speech-to-text mishears "Reachy".

slug: `reachy-mini-s-live-hearing-now-knows-when-you-re-t` · status: `exported` · from frame: `reachy-mini-s-live-hearing-now-knows-when-you-re-t`

> Reachy Mini's live hearing now knows when you're talking TO it: under `listen --live --transcribe` it stays quiet through ambient chatter and only speaks up when an utterance is addressed to the robot or is something it can genuinely help with — judged by the LLM in conversational context, not a word-count timer — and it recognises its own name reliably even when the speech-to-text mishears "Reachy".

## Tasks

### t1 — Add non-streaming single-shot complete() to reachy/speech/llm.py for the engagement classifier

- covers: c11
- acceptance:
  - complete(messages, *, timeout, model/base_url/api_key) issues a stream:false request reusing LlmConfig.resolve() and the shared request builder, returning the full assistant text as one string
  - honors REACHY_OPENAI_* and legacy REACHY_LLM_* exactly like stream_chat_completion; a unit test with a faked urlopen asserts the request body carries stream:false and returns the parsed text
  - a connection error / timeout surfaces as a catchable error (never a bare hang); test asserts the bounded timeout is threaded through

### t2 — Add stdlib fuzzy name matcher reachy/speech/name_match.py tolerant of STT mishearings of 'Reachy'

- covers: c13
- acceptance:
  - is_name_match(text, names, threshold) returns True for exact 'reachy'/'robot' and close mishearings ('reachie','richy'/'richie' at the tuned threshold), False for 'reach'/'rich'/'preachy' and unrelated words
  - pure stdlib (difflib/edit-distance), no numpy and no new dependency; threshold is a parameter with a documented default
  - a table-driven unit test pins the accept/reject cases above

### t3 — Pin current heuristic behavior + the problem it causes with a shared transcript fixture set

- covers: c4, h8, c5, h9
- acceptance:
  - a new tests/ fixture module encodes labelled lines: ambient human-to-human, named, addressed-follow-up, and mis-heard-name
  - a test asserts today's _should_engage (whole-word name OR coherent-in-window) is exactly the shipped behavior in listen_transcribe.py
  - a test demonstrates the problem: an ambient coherent sentence inside the 20s window engages today's heuristic

### t4 — Build the 3-tier motion ladder in reachy/motion/listen.py (noise->antenna, speech->orient, engaged->turn-to-DoA)

- covers: c15, c14
- acceptance:
  - ListenParams gains graduated tiers so that under --transcribe: ambient noise drives only the Tier-1 antenna lean, detected speech drives a larger orienting move, and an 'engaged' signal drives a deliberate head/body turn toward the utterance DoA
  - the engaged turn is clamped so it never feeds the SDK goto planner a duration/time that trips 'time value out of range [0,1]'; a test exercises the largest escalate angle without raising
  - with no engaged signal the loop reproduces today's antenna-only behavior (no barge-in turn on ambient sound)

### t5 — Build reachy/speech/engagement.py: LLM 'is this aimed at me?' classifier + layered decide_engagement()

- depends on: t1, t2
- covers: c13, c11
- acceptance:
  - an injectable EngagementClassifier calls llm.complete() with a tunable classifier prompt over the utterance + recent conversation context and returns engage=True only when the utterance is addressed to the robot (NOT merely helpable)
  - decide_engagement(text, ctx, *, classifier, names, ...) returns ENGAGE on fuzzy name match (via name_match) OR a positive classifier verdict, DROP otherwise, and the sentinel DEGRADE when the classifier raises/times out — it makes at most one classifier call per call
  - unit tests with a faked complete() assert: addressed fixtures -> ENGAGE, ambient -> DROP, classifier exception/timeout -> DEGRADE; no real network

### t6 — Wire the layered gate into reachy/motion/listen_transcribe.py with the escape hatch and graceful fallback

- depends on: t5, t3
- covers: c1, h1, h10, c12, h2, c15, h12
- acceptance:
  - _should_engage delegates to decide_engagement via an injected classifier seam (default-constructed, like transcriber=); on DEGRADE it falls back to today's coherent-in-window heuristic so the loop never stalls
  - REACHY_ENGAGE_HEURISTIC=1 forces the pure-heuristic gate (no classifier call); a test asserts both the LLM-gate path and the forced-heuristic path
  - the per-utterance decision is observable (logged/exported as engaged-by name|context|dropped); a fixture-driven test over the shared transcript set proves addressed/named engage, ambient stays quiet, and a forced-failing classifier keeps hearing

### t7 — Wire reachy/cli/_commands/listen.py: build the classifier + motion ladder only under --transcribe

- depends on: t6, t4
- covers: c2, h7, c14, h11, h3
- acceptance:
  - under --transcribe the classifier seam and motion-ladder params are constructed and injected; WITHOUT --transcribe no classifier/name-fuzzer is built (bare listen run and --live stay byte-identical) and a test asserts this
  - a test asserts no new base dependency appears in pyproject (classifier reuses stdlib urllib llm client; name match stdlib-only) and the feature lives entirely inside the --transcribe live loop (no new remote API/UI surface)
  - the engaged turn-toward-DoA is triggered from the gate's engaged decision through the motion ladder; the gate never enqueues a barge-in turn on un-addressed speech

### t8 — Docs + version bump: CLAUDE.md listen-noun internals, operating guide, CHANGELOG, pyproject

- depends on: t7
- acceptance:
  - CLAUDE.md listen-noun section + docs/operating-reachy.md describe the layered engagement gate, the REACHY_ENGAGE_HEURISTIC escape hatch, and the 3-tier motion ladder
  - version-bump skill bumps pyproject.toml + uv.lock and prepends a CHANGELOG entry so the version-check CI job passes
  - markdownlint passes on changed docs
