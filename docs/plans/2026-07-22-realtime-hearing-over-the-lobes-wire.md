# Build Plan — realtime hearing over the lobes wire

slug: `realtime-hearing-over-the-lobes-wire` · status: `exported` · from frame: `realtime-hearing-over-the-lobes-wire`

> Reachy Mini now hears its surroundings through the lobes /v1/realtime WebSocket: mic audio streams as OpenAI-shaped base64 append events, utterances are endpointed by server-side VAD instead of a locally tuned energy threshold, and the transcript sense feeds rule-action reactions that fire on what was actually said.

## Tasks

### t1 — Wire primitives: hand-rolled RFC 6455 + base64 append-event codec in a new reachy/speech/realtime_wire.py (pure functions, no sockets), cited from lobes-cli scripts/realtime-smoke.py

- covers: c8, h12
- acceptance:
  - Handshake request builder + response parser + Sec-WebSocket-Accept verification round-trip in offline unit tests
  - Frame build/read covers TEXT, PING/PONG and CLOSE, client-to-server masking always on; property test: build->read round-trips payloads across the 7/16/64-bit length encodings
  - build_append_event encodes PCM16 bytes as {type: input_audio_buffer.append, audio: <base64>}; decoder rejects malformed events without raising
  - URL derivation maps http(s)://host[:port] -> ws(s)://host[:port]/v1/realtime
  - git diff on pyproject.toml dependency lists is empty (h12); provenance note cites the smoke-script donor

### t2 — Offline fake realtime server harness for tests (tests/fake_realtime_server.py): an in-process loopback-socket server scriptable per scenario

- depends on: t1
- acceptance:
  - Scripts the happy sequence: accept handshake -> session.created -> speech_started -> speech_stopped -> transcription.completed
  - Scripts every failure mode as a named scenario: refuse handshake (401/426), close mid-stream, never-pong, malformed JSON event, vad_unavailable, stt_forward_failed
  - Runs in-process with stdlib only; each test gets an isolated port; shuts down cleanly under pytest -n auto

### t3 — RealtimeTranscriber session client (new reachy/speech/realtime.py): worker-thread WS session that connects with Bearer + input_sample_rate query param, streams append events from a bounded queue, consumes the event sequence, surfaces utterance events, and reconnects with backoff

- depends on: t1, t2
- covers: c2, h2, c4, h3, c13, h7, c17, h10
- acceptance:
  - Against the fake server's happy path: only TEXT frames are ever sent (asserted server-side), appends are valid base64 PCM16, and a transcription.completed yields exactly one utterance event with the transcript text
  - Every fake-server failure scenario resolves to a named senselog drop + a scheduled backoff reconnect; no exception ever reaches the caller's thread; vad_unavailable and stt_forward_failed are distinct named drops
  - Session-down is a LATCHED state transition: the drop logs once on entry, not per chunk; a refuse-then-accept fake-server sequence shows hearing resume without restart (h10)
  - Server PINGs are answered with PONGs (fake server asserts it; uvicorn's ~20s cadence documented in the module docstring)
  - Config: with only REACHY_OPENAI_URL_BASE + REACHY_OPENAI_API_KEY set the client targets ws://<gateway>/v1/realtime with Bearer auth; REACHY_REALTIME_URL overrides; both pinned by tests (h7)
  - Never sends response.create; a response.* event arriving is ignored with a debug log (c11 ears-only)
  - close() is idempotent and joins the worker; no thread or socket leaks under pytest

### t4 — TranscriptSenseDriver capture half consumes server VAD events (reachy/behavior/transcript_sense.py + its test family): feed mic chunks to the injected RealtimeTranscriber, take utterance events through the unchanged engagement gate, self-mute at arrival; remove the local energy-endpointing path (no fallback, operator decision q1)

- depends on: t3
- covers: c5, h4, c6, h5, c12, h6, c16, h9, c18, h11, c9, h13
- acceptance:
  - The engagement gate's own tests pass unmodified: the gate receives the same utterance shape as today (h4)
  - Structural test: the tick thread never touches a socket — WS I/O is reachable only from the worker/client threads (h4)
  - Injected-clock test: a transcription.completed landing inside mute_until is discarded with the existing self-mute drop reason (h5)
  - test_zero_llm_boundary.py green with reachy.speech.realtime added to _BEHAVIOR_SPEECH_ALLOW carrying its not-an-LLM justification; the equality pin still names the engagement classifier as the ONE LLM edge (h6)
  - tests/test_behavior_transcript_contiguous.py and every other test asserting the removed local-capture path are re-scoped or retired deliberately, each named in the PR description (h9 test half)
  - A test asserts rules.py refuses content ops over transcript; the rule-schema diff is empty (h11)
  - git diff for rms_sense.py, rms_background.py, orient.py, sleep/wakeword.py is empty (h13, c14)
  - One-tick latch semantics, bounded queues and named drop reasons unchanged — the sense.py provider contract and _COMPOSED_PROVIDER_FIELDS untouched

### t5 — Compose realtime hearing into the runtime seam (reachy/cli/_commands/behavior.py): construct the client at composition, feed it from the _AudioTap fan-out, pass the REAL mic rate into the session config, close it in _RuntimeResources

- depends on: t4
- covers: c10, h14, c22, h17
- acceptance:
  - Structural test: AudioPump remains the only media.audio() consumer; the streamer receives the fanned-out per-tick chunk on the worker queue, never a second SDK read (h14)
  - The session's input_sample_rate query param carries the rate reported by the held media client, not a hard-coded 16000
  - _RuntimeResources.close() closes the realtime client; a run that never connected shuts down clean (no hang at interpreter exit)
  - Composition is unconditional and import-safe without reachy_mini: a bare box runs with the transcript field permanently quiet and ONE logged session-down transition (h17 offline half)
  - Startup stays off the tick thread: client construction + connect happen during composition/worker warm-up, mirroring the HeldMediaClient warm-up discipline

### t6 — Docs move with the capture path: docs/operating-reachy.md + CLAUDE.md hearing/engagement sections describe the WS path, session-down behavior, and the audience/before/after/why narrative with citations

- depends on: t5
- covers: c16, h9, c20, h15, c21, h16, c23, h18
- acceptance:
  - Operating guide hearing section: the realtime session, server-side VAD, named session-down + reconnect, and the #116 dependency for live media (h9 docs half)
  - Before/after/why narrative cites evidence: #111's measured table, #115's strict-cutover statement, the #149 'Ready, she' comparison (h16/h18)
  - Audiences match consumers: rules read sense.transcript, agent attach receives feed_transcript cues, operator drives behavior engine run (h15)
  - No doc claims the surface validated before the evidence file exists (the #108 rule); markdownlint + teken rubric gates green

### t7 — Live acceptance: the recorded five-word run under docs/verification — speech_started -> speech_stopped -> transcription.completed from the robot's own mic at 16 kHz, gate admission, rule fire, zero binary frames

- depends on: t5
- covers: c1, h1, c15, h8, c24, h19, c2, h2, c22, h17
- acceptance:
  - Evidence file in docs/verification with date, box, package + fleet versions, PASS/FAIL per step, and the verbatim transcript (h8/h19)
  - A five-word question spoken at normal volume across the room round-trips with the words present, admits through the engagement gate, and fires a transcript-corroborated rule (h1)
  - The run confirms only JSON text frames on the wire (server-side log or client assertion) at 16 kHz (h2)
  - Runs only after the #94 media path is restored (issue #116 or another fix); until then the task waits — it is never simulated

## Risks

- [unknown_nonblocking] Live acceptance (t7) waits on the deployed box's media path: #94's ECONNREFUSED on the daemon's :8443 signalling server, remediation tracked in #116 (containerized daemon). Offline waves t1-t6 are unaffected. (task t7)
- [follow_up] Deploy coordination: the client lands ready-to-deploy BEFORE the fleet updates (issue #115's recommended posture); against a pre-#151 fleet the append wire is not understood, so the runtime deploy flips only after the lobes-cli release ships the new wire.
- [unknown_nonblocking] The native mic rate is assumed 16 kHz (the lobes evidence calls it reachy's native rate) but t5 must pass the rate the media client actually reports — if the SDK delivers 24/48 kHz the query param, not a client-side resample, carries it (the server resamples). (task t5)
