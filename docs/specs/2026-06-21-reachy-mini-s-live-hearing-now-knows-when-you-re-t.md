# Reachy Mini's live hearing now knows when you're talking TO it: under `listen --live --transcribe` it stays quiet through ambient chatter and only speaks up when an utterance is addressed to the robot — by name, or clearly aimed at it in an ongoing conversation as judged by the LLM in context (not a word-count timer) — and it recognises its own name reliably even when the speech-to-text mishears "Reachy".

> Reachy Mini's live hearing now knows when you're talking TO it: under `listen --live --transcribe` it stays quiet through ambient chatter and only speaks up when an utterance is addressed to the robot — by name, or clearly aimed at it in an ongoing conversation as judged by the LLM in context (not a word-count timer) — and it recognises its own name reliably even when the speech-to-text mishears "Reachy".

## Audience

- The person near the robot speaking in its vicinity (whose ambient chatter should be ignored but whose direct address should land), and the operator running the boot-persistent `listen run --live --transcribe` presence loop.

## Before → After

- Before: Today the gate is a pure heuristic (`_should_engage` in listen_transcribe.py): whole-word name-match on 'reachy'/'robot', OR a coherent sentence (>= min_words=3) within engage_window_s=20s of the last accepted turn. It cannot tell 'pass the salt' (to a human) from 'what time is it?' (to the robot), and STT mishearings of 'Reachy' silently miss the name path.
- After: Under --transcribe an endpointed utterance reaches cognition only when it is AIMED AT THE ROBOT: either it names Reachy/Robot (fuzzy, tolerant of STT mishearings), OR — within an ongoing conversation — the LLM judges in context that the message is addressed to the robot. Ambient human-to-human chatter is dropped silently and the robot stays quiet. It does NOT proactively engage on un-addressed speech merely because it could help.

## Why it matters

- PR #54 made the robot hear WORDS, but the heuristic reacts to too much — any coherent ambient sentence inside the 20s window engages it. The user's actual ask was 'respond when it makes sense in context.' Only an LLM reading the utterance in context can separate noise-not-for-me from addressed-or-helpable, which is the whole point of a robot that's pleasant to have in the room.

## Requirements

- Graceful degradation is mandatory: the classifier runs with a bounded timeout and a guaranteed fallback to the existing heuristic (coherent-in-window). A slow, unreachable, or erroring LLM degrades to 'use the heuristic' and NEVER blocks or stalls the hearing loop — same resilience contract as the audio_optional cognition path.
  - honesty: A test proves an unreachable/timing-out classifier degrades to the heuristic path with bounded latency and no exception escaping the gate — the hearing loop continues either way.
- Backward compatibility: bare `listen run` and `listen run --live` WITHOUT --transcribe are byte-identical (no classifier, no name-fuzzing built). Under --transcribe the LLM gate is the new default, with an env/flag escape hatch to force the old pure-heuristic gate.
  - honesty: A test asserts no classifier/name-fuzzer is constructed without --transcribe (byte-identical), and that the escape-hatch env/flag forces the pure-heuristic gate under --transcribe.

## Honesty conditions

- On a small hand-built transcript fixture set, the gate engages on addressed/helpable lines and stays quiet on ambient human-to-human lines; AND with the classifier forced to fail the loop keeps running on the heuristic.
- The build adds no new surface for anyone beyond the near-robot speaker and the boot-live operator — no remote API, no UI; the feature lives entirely inside the --transcribe live loop.
- The described current behavior is the shipped code: _should_engage in listen_transcribe.py does whole-word name-match OR (len(words)>=min_words AND within engage_window_s) — verifiable by reading the function.
- The problem reproduces: with today's gate, an ambient coherent sentence inside the 20s window engages cognition — demonstrable from the existing code path / a fixture transcript.
- The engage decision is observable per utterance (logged and/or exported as engaged-by name | context | dropped), so the addressed-vs-ambient behavior is verifiable on the robot and in tests.
- A test asserts no new base dependency in pyproject, that STT/classifier only construct under --transcribe, and that the gate never enqueues a barge-in / proactive-help engagement on un-addressed speech.
- A fixture test set encodes these exact ambient-vs-addressed-vs-named lines plus the LLM-down fallback, and the engagement decisions match the success-signal expectations.
- The LLM call fires at most once per endpointed utterance (never per tick) and only for the ambiguous middle — the name fast-path and the self-mute/min-utterance shortcuts are proven (by test) to bypass it.
- The engaged turn-toward-DoA is clamped so it never feeds the SDK goto planner a duration/time that trips 'time value out of range [0,1]'; a test exercises the largest escalate angle without raising.

## Success signals

- Two humans talking near the robot ('did you finish the report?') does NOT engage (robot stays quiet); naming it ('Reachy, what's the weather?') engages; a follow-up inside an ongoing conversation clearly aimed at the robot engages even without the name; a mis-heard name ('Richie, look') still engages via fuzzy match; with the LLM endpoint DOWN the loop never stalls (degrades to the heuristic). Motion ladder is observable: noise -> antenna lean; speech -> larger orienting move; engaged -> head turns toward the speaker.

## Scope / boundaries

- NOT a barge-in cloud assistant: words stay one more perception, STT only runs under --transcribe, the robot never interrupts, and the existing utterance-endpointing + self-mute + min-utterance machinery is preserved. No new BASE runtime dependency — the classifier reuses the stdlib urllib LLM client and name-matching is stdlib-only. The robot does NOT proactively engage on un-addressed speech just because it could help. Precise continuous gaze-tracking stays a follow-up (v6).

## Non-goals

- Does NOT replace the cheap pre-filters (self-mute, min-utterance duration, fuzzy name fast-path) with an LLM call on the hot path — the LLM judges only the ambiguous middle, so it runs at most once per endpointed utterance, never per tick.

## Decisions

- Add a non-streaming single-shot completion to reachy/speech/llm.py (today it is streaming-only) for the classifier: a small-token yes/no(+reason) call to the SAME REACHY_OPENAI_* endpoint, with a tight timeout. The classifier is an injectable seam (like the existing `transcriber=` injection in TranscribeHook) so tests run without a live LLM.
- Layered engagement gate: (1) self-mute + min-utterance gate as today; (2) fuzzy name fast-path -> immediate engage (stdlib edit-distance/difflib over the 'reachy'/'robot' name list, tolerating mishearings); (3) for a coherent utterance with NO name, a single-shot LLM classifier judges 'is this addressed to the robot, given the recent conversation?' (NOT 'could I help') -> engage on yes; (4) else dropped silently. The LLM step is the only new network call, runs at most once per endpointed utterance, and is bounded + fallible.
- Graduated motion response tied to perception level, replacing the blanket turn_enabled=False suppression under --transcribe: (a) ambient noise -> Tier-1 antenna lean toward DoA (as today); (b) detected speech -> a larger orienting move toward the speaker; (c) ENGAGED (named or judged-addressed) -> a deliberate head/body turn toward the utterance's DoA. The engaged turn must respect the SDK goto guard (the 'time value out of range [0,1]' fault from #54) so it never trips it.

## Hard questions

- Does a per-utterance LLM round-trip on the local vLLM add perceptible lag before the robot answers, and what timeout budget keeps it acceptable?
- risk: Fuzzy name matching over-fires on 'reach'/'rich'/'preachy'; the 'can-help' judgment makes the robot butt into every problem-sounding sentence. Both need empirical tuning against real transcriptions.
- risk: The classifier shares the same REACHY_OPENAI_* endpoint as cognition; a classifier call mid-conversation could contend with the streaming cognition turn on a single-GPU vLLM.

## Open / follow-up

- Precise / continuous gaze-tracking that keeps the head locked on a moving speaker (beyond the single deliberate engaged-turn-toward-DoA in this spec) — a motion refinement follow-up; the basic engaged head-turn is in scope here.
