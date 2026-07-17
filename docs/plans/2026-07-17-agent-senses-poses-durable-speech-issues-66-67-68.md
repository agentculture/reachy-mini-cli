# Build Plan — agent senses + poses + durable speech (issues 66/67/68)

slug: `agent-senses-poses-durable-speech-issues-66-67-68` · status: `exported` · from frame: `agent-senses-poses-durable-speech-issues-66-67-68`

> Reachy's agent cognition closes its three post-0.32 gaps: touch reaches the brain (a pat becomes a sense cue the agent can answer), the pose catalog is advertised to the model (enum + a contentment pose, so tool choices stop silently no-oping), and speech survives deployment (gateway-route TTS reachable out of the box, an audio latch that recovers, and a mute state an operator can see)

## Tasks

### t1 — PR A t1: kill the pat false-fire loop — gate sensing on motion-in-flight + re-baseline after reactions

- covers: c20
- acceptance:
  - with a simulated in-flight goto (actual pose mid-transit toward the commanded target), the detector receives no update (or a re-baselined commanded pose) and no detection fires (fake clock, no robot)
  - the first sensing pass after a reaction window closes re-baselines so the settled pose reads as zero deviation — the post-reaction idle-resume move cannot re-trigger
  - a genuine external-deviation sequence (steady commanded pose, actual pose pressed) still detects at the same thresholds as today
  - files: reachy/motion/listen_pat.py (+ the minimal on_tick/queue seam it needs, e.g. reachy/motion/server.py), tests/test_listen_pat.py

### t2 — PR A t2: EventBuffer.feed_pat — touch becomes a sense cue

- covers: c2
- acceptance:
  - feed_pat(kind='scratch'|'side_pat', level='level1'|'level2') appends exactly one cue via the thread-safe _append, phrased like 'felt a gentle scratch on the head' / 'felt a firm sideways nudge', intensity carried in the wording
  - an unknown kind or level degrades to no cue and never raises
  - files: reachy/speech/events.py, tests/test_speech_events.py
  - docstring feed-methods list updated (module header names the new feed alongside feed_doa/feed_vision/feed_transcript)

### t3 — PR A t3: PatHook feeds the cue — one per reaction cycle, reflex untouched

- depends on: t1, t2
- covers: c2, h2, c5, h5
- acceptance:
  - with a fake detector forcing one detection, exactly one felt-a-pat cue lands in the injected buffer and the PatReaction lean is enqueued independent of (and before) any LLM work
  - a continuous-stroke fake-clock sequence yields at most one cue per reaction window
  - a PatHook constructed without a buffer behaves byte-identically to today: no cue, no crash, reflex unchanged
  - files: reachy/motion/listen_pat.py, tests/test_listen_pat.py (serialized after t1 — same files)

### t4 — PR A t4: thread the shared EventBuffer into _build_pat_hook

- depends on: t3
- covers: c3, h3, c12, h12
- acceptance:
  - under listen run --live --cognition agent, a composition test asserts the SAME EventBuffer object (identity) is shared by PatHook, ThinkHook, and TranscribeHook
  - pat cues reach the buffer without passing decide_engagement, and no change lands in reachy/speech/engagement.py or the TranscribeHook gate path
  - bare listen run (non-live) still builds PatHook without a buffer; --no-pat still suppresses the hook entirely
  - files: reachy/cli/_commands/listen.py, tests/test_listen_cognition_agent.py

### t5 — PR A t5: the agent system prompt perceives touch

- covers: c6, h6
- acceptance:
  - DEFAULT_AGENT_SYSTEM_PROMPT names touch/being patted among the perceptions (string assertion)
  - an integration test with a fake turn_fn shows a lone pat cue (no words in the buffer) firing an agent turn
  - files: reachy/speech/agent_turn.py, tests/test_agent_turn.py

### t6 — PR A t6: apply_pose advertises the catalog (enum) + unknown keys error informatively

- covers: c7, h7, c9, h9, c13, h13
- acceptance:
  - the published apply_pose parameters carry an enum equal to the loaded catalog keys; a test loading a temp TOML with an extra key sees that key appear in the published schema with no code change
  - dispatching apply_pose with an emoji absent from the catalog returns an error tool-result naming the valid keys and never calls the express seam
  - the import boundary holds: no import of reachy.speech.llm, reachy.speech.events, or reachy.motion appears in reachy/speech/tools.py (existing boundary test stays green)
  - files: reachy/speech/tools.py, tests/test_speech_tools.py

### t7 — PR A t7: a contentment pose joins expressions.toml

- covers: c8, h8
- acceptance:
  - the new smiling-face entry stays within the AMPLITUDE GUIDE ranges (antennas gently forward, slight head_z lift, small chin-up)
  - reachy think expressions check exits ok with no too-similar pair involving the new entry (tuned for distance from the slightly-smiling entry)
  - files: reachy/speech/expressions.toml (+ tests/test_expressions.py if a catalog-size assertion exists)

### t8 — PR B t8: recoverable audio latch in CognitionEngine

- covers: c10, h10
- acceptance:
  - consecutive synth/playback failures still latch mute at DEFAULT_AUDIO_MUTE_THRESHOLD, but as a clock-based retry-after (muted_until, injected clock, exponential backoff with a cap) instead of a process-lifetime boolean
  - once muted_until elapses, the next clip attempts one synth; success clears the streak and un-mutes, failure re-latches with the longer backoff (fake clock, no real TTS)
  - strict mode (audio_optional=False) behavior is byte-identical to today
  - files: reachy/speech/cognition.py, tests/test_cognition_audio_optional.py

### t9 — PR B t9: the same recoverable latch in AgentTurnEngine

- depends on: t5, t8
- covers: c10, h10
- acceptance:
  - the retry-after policy from t8 is mirrored in _dispatch_audio/_note_audio_failure: a muted window skips dispatch with the synthetic muted tool-result, an elapsed window attempts one real dispatch, success un-mutes
  - strict mode still aborts the turn with the exit-2 CliError exactly as today
  - files: reachy/speech/agent_turn.py, tests/test_agent_turn.py (serialized after t5 — same files; policy mirrored from t8)

### t10 — PR B t10: mute state is operator-visible

- depends on: t8, t9
- covers: c11, h11
- acceptance:
  - while muted, think status --json reports the muted state via a sidecar (pattern: the think.voice sidecar), cleared on recovery and on run exit
  - the mute warning repeats periodically (every N turns or M minutes) while muted, instead of firing once at latch time
  - files: reachy/cli/_commands/think.py + a small sidecar-write hook in both engines, tests for status --json

### t11 — PR B t11: the lobes gateway route becomes the TTS default

- covers: c14, h14
- acceptance:
  - DEFAULT_TTS_ROUTE == 'openai' in reachy/speech/tts.py; REACHY_TTS_ROUTE=chatterbox restores the old leg unchanged (user decision c26)
  - no change to reachy/cli/_commands/say.py; say's existing boundary tests stay green
  - existing tts tests updated to the new default; the chatterbox leg keeps its own coverage
  - files: reachy/speech/tts.py, tests/test_speech_tts.py

### t12 — docs t12: CLAUDE.md + operating guide + changelog reflect all of the above

- depends on: t3, t4, t6, t7, t10, t11
- acceptance:
  - CLAUDE.md noun internals updated: listen/pat (feed_pat + motion-in-flight gating), think/say (TTS default = gateway route), the mute sidecar in think status
  - docs/operating-reachy.md updated where it names the chatterbox default or the one-shot mute
  - CHANGELOG.md entries + version bumps land per PR via the version-bump skill (uv lock refreshed — the PR #33 gotcha)
  - markdownlint-cli2 green on every touched doc

### t13 — verify t13: on-robot acceptance + delivery evidence

- depends on: t12
- covers: c1, h1, h15, c21, h16, c22, h17, c23, h18, c24, h19, c25, h20
- acceptance:
  - journal excerpt: 30 untouched idle minutes with zero pat detections, then one real pat detects and reacts (h15)
  - live session: a pat yields an agent-turn response (speech or pose); an unknown pose key self-corrects from the error tool-result; TTS stop shows mute in status --json, TTS start recovers speech with no service restart
  - delivery summary records each success signal with its evidence (journal excerpt, session transcript, CI links for suite + teken rubric)
  - manual on-robot step — requires the live box and the operator present

## Risks

- [unknown_nonblocking] cross-PR file overlap: t5 (PR A) and t9 (PR B) both touch reachy/speech/agent_turn.py + tests/test_agent_turn.py — handled by the explicit t9->t5 dependency, but if the two PRs are built as independent branches off main, PR B must rebase after PR A merges
- [unknown_nonblocking] t1 may need to extend the on_tick seam (reachy/motion/server.py) so PatHook can see motion-in-flight — the seam is shared by every folded hook; keep the extension optional/backward-compatible so the other hooks are untouched
- [unknown_nonblocking] gemma-family models can return an empty final assistant message at temperature 0 (seen on muse; the deployed senses pin is also gemma) — agent turns in t13 may end with empty content; carried over from frame vagueness v2
- [unknown_nonblocking] t13 is a manual on-robot step needing the operator present and the live box healthy (daemon + lobes gateway + STT up); schedule it with the user rather than the workforce
