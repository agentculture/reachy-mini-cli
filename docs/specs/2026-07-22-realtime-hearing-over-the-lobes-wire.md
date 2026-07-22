# realtime hearing over the lobes wire

> Reachy Mini now hears its surroundings through the lobes /v1/realtime WebSocket: mic audio streams as OpenAI-shaped base64 append events, utterances are endpointed by server-side VAD instead of a locally tuned energy threshold, and the transcript sense feeds rule-action reactions that fire on what was actually said.
> instruction: Do not claim shipped until the live acceptance transcript (c15) is in docs/verification.

## Audience

- The operator running the boot-persistent presence on the deployed robot; the runtime's rules (transcript-corroborated reactions) and the attached agent consuming heard words; coordinated with the lobes fleet operator on the other side of the #115 wire break.

## Before → After

- Before: Hearing is client-endpointed: an energy threshold tuned against a drifting background decides when an utterance starts, so a normal voice across the room never starts one (#111) — and the wire this client streams to is being cut over: an un-adapted reachy cannot stream to an updated fleet at all.
- After: Mic audio streams continuously to the lobes /v1/realtime session as base64 append events; the server's VAD endpoints whole utterances; the transcript sense receives them through the unchanged engagement gate and transcript-corroborated rules fire; a down session is a named quiet state that reconnects on its own.

## Why it matters

- The fleet cutover is strict with no dual-format grace — without this the robot goes deaf on the new wire; with it the robot also stops being deaf across the room, because endpointing moves to the side that can hear (the #149 measurement: the five-word question that shattered to 'Ready, she' locally arrived whole server-side).

## Requirements

- The client streams mic audio to /v1/realtime as JSON text frames — {type: input_audio_buffer.append, audio: <base64 PCM16 mono LE>} — at 16000 Hz via the connect-URL query param, with Bearer auth on the handshake, answering WS pings; NO binary audio frames anywhere (issue #115 acceptance criteria 1+3; strict cutover, no dual-format grace window).
  - instruction: Cite the append-event codec from lobes-cli scripts/realtime-smoke.py (build_append_event); verify with a live session, not by inspection.
  - honesty: Our client's session carries ONLY JSON text frames — config via connect-URL query params, Bearer on the handshake, audio as base64 append events — and completes the transcription sequence at 16 kHz against the live gateway.
- A new realtime transcription client module (e.g. reachy/speech/realtime.py) joins reachy/speech/: connect, stream append events, consume session.created / speech_started / speech_stopped / transcription.completed and the named error events (vad_unavailable, stt_forward_failed), surface transcripts + speech boundaries to the transcript sense; every failure degrades cleanly (reconnect/backoff), never raises into the loop — the same never-raise ethos as Transcriber.
  - instruction: New module reachy/speech/realtime.py; hand-rolled stdlib RFC 6455 cited from the smoke script; every failure a named senselog drop, never an exception on the caller's thread.
  - honesty: Offline tests drive the client against a scripted fake server through every failure path — refused handshake, mid-stream close, missed ping, malformed event, vad_unavailable, stt_forward_failed — and each resolves to a named drop + scheduled reconnect; no path raises into the caller.
- Utterance endpointing moves server-side: TranscriptSenseDriver stops deciding when an utterance starts/ends from the local energy threshold and instead consumes the session's speech_started/speech_stopped/transcription.completed sequence; the tick thread stays socket-free (all WS I/O on the existing worker-thread pattern), and the engagement gate (name fast-path / short-utterance / warm-window / single-shot classifier) still judges every transcript exactly as today — admission is unchanged, only capture changes.
  - instruction: Rework TranscriptSenseDriver's capture half to consume speech_started/speech_stopped/transcription.completed; keep the one-tick latch, bounded queues, and named drop reasons exactly as documented.
  - honesty: The tick thread never touches the socket (WS I/O lives on the worker thread, asserted structurally) and the engagement gate sees the same utterance shape it sees today — the gate's own tests pass unmodified.
- Self-mute stays client-side: the server's VAD cannot know when the robot itself is speaking, so SpeechActuator's mute_until window must still suppress transcripts client-side — transcription.completed events that land inside the mute window are discarded with the existing named self-mute drop reason.
  - instruction: Apply the mute check where transcripts ARRIVE (the event consumer), not where audio is captured — the server cannot know when the robot speaks.
  - honesty: A transcription.completed event landing inside SpeechActuator's mute_until window is discarded with the existing self-mute drop reason — pinned by a test with an injected clock.
- The zero-LLM boundary holds: the new realtime module lands on _BEHAVIOR_SPEECH_ALLOW with a stated justification (streaming transcription is speech-to-text, not a language model); the single pinned LLM edge stays the engagement classifier, and test_zero_llm_boundary.py's equality pins are updated in the same change — never loosened.
  - instruction: Extend the allow-list and its justification map in the same commit that adds the import; never loosen the equality pins.
  - honesty: test_zero_llm_boundary.py passes with the realtime module added to _BEHAVIOR_SPEECH_ALLOW carrying its not-an-LLM justification, and the equality pin still names the engagement classifier as the ONE LLM edge.
- Config derives from what the box already exports: the realtime URL comes from the gateway base (REACHY_OPENAI_URL_BASE=<http://localhost:8001>, the same host lobes endpoint stt resolves; http(s) mapped to ws(s) + /v1/realtime) and auth reuses REACHY_OPENAI_API_KEY as the Bearer key, with a dedicated override env var for a split deployment.
  - instruction: Map http(s)->ws(s) from REACHY_OPENAI_URL_BASE; Bearer from REACHY_OPENAI_API_KEY; add REACHY_REALTIME_URL as the split-deployment override.
  - honesty: On a box exporting only today's env (REACHY_OPENAI_URL_BASE + REACHY_OPENAI_API_KEY) the client derives ws://<gateway>/v1/realtime and authenticates; the dedicated override env var is exercised by a test.
- Acceptance is the issue #115 sequence live on this fleet: a five-word spoken question round-trips speech_started -> speech_stopped -> transcription.completed with the words present, streamed from the robot's own mic as append events at 16 kHz — and the transcript then admits through the engagement gate and fires a transcript-corroborated rule (the hear-then-react loop, end to end).
  - instruction: Script the run like lobes-cli's acceptance evidence: date, box, versions, PASS/FAIL per step, verbatim transcript; wait on the v1 park (#94/#116) for the mic leg.
  - honesty: The live acceptance transcript lands in docs/verification BEFORE any doc claims the surface validated (the #108 evidence rule), and the run shows gate admission + rule fire, not just transcription.
- Tests and docs move with the capture path: tests/test_behavior_transcript_contiguous.py pins invariants of CLIENT-side capture (contiguous slice, watermark ring, wall-clock min_utterance_s) that stop describing the server-VAD path — they are re-scoped to whatever local capture survives, not deleted blindly; docs/operating-reachy.md and CLAUDE.md's hearing/engagement sections are updated in the same change.
  - instruction: Treat tests/test_behavior_transcript_contiguous.py as the checklist of invariants to re-home; update docs/operating-reachy.md and CLAUDE.md in the same PR.
  - honesty: After the change no test asserts the removed local-capture path; the contiguous-capture tests are re-scoped or retired deliberately and named in the PR; operating guide + CLAUDE.md hearing sections describe the WS path; lint + rubric gates stay green.
- Session-down behavior (operator decision, q1): when the realtime session is unavailable — fleet not yet updated, gateway down, mid-run disconnect — hearing goes quiet with a named senselog drop reason and the client reconnects in the background; the local-endpointing + HTTP capture path is removed, not maintained as a fallback.
  - instruction: Latch the drop on state transition; reconnect with backoff on the worker thread; test with a fake server that refuses then accepts.
  - honesty: With the gateway stopped mid-run the journal shows ONE named session-down drop (a transition, not per-tick — the #99 discipline) and hearing resumes without restart when the gateway returns.

## Honesty conditions

- Both halves hold in one live run: the client streams to a post-cutover fleet AND a whole phrase spoken at normal volume across the room is heard — recorded under docs/verification per the #108 evidence rule.
- pyproject.toml's dependency lists (base deps and every extra) are unchanged by this arc — git diff proves it.
- git diff for rms_sense.py, rms_background.py and orient.py is empty.
- AudioPump remains the only media.audio() consumer — the streamer takes the fanned-out chunk, pinned by a structural test.
- behavior rules check still refuses any content op over transcript; the rule-schema diff for this arc is empty.
- The named audiences actually consume the surface: rules read sense.transcript, the attached agent receives feed_transcript cues, the operator drives behavior engine run — no invented audience.
- The before-state is evidenced, not asserted: #111's measured table (0.102 gate vs a normal voice across the room) and #115's strict-cutover statement.
- The after-state is demonstrated by the live acceptance run plus offline fake-server tests for the down-session path — both recorded, neither assumed.
- The urgency is real: #115 states no dual-format grace window, and the deafness fix is the measured #149 comparison (whole utterance server-side vs 'Ready, she' locally).
- The signal is a recorded artifact under docs/verification (date, versions, PASS/FAIL per step, verbatim transcript) — the #108 evidence rule, same bar as h8.

## Success signals

- One live run, recorded in the repo: a five-word question spoken across the room round-trips speech_started -> speech_stopped -> transcription.completed from the robot's own mic at 16 kHz over append events (zero binary frames), admits through the engagement gate, and fires a transcript-corroborated rule.

## Scope / boundaries

- No new WebSocket dependency: base runtime deps stay numpy + harmonics-cli only (CLAUDE.md hard constraint). The WS leg is hand-rolled stdlib RFC 6455, cite-don't-import from lobes-cli scripts/realtime-smoke.py — handshake + Sec-WebSocket-Accept verification, client-to-server masking, text frames, PING/PONG keepalive (uvicorn closes a peer that never pongs, ~20 s cadence), and the base64 append-event codec; its pure pieces are already unit-tested offline in lobes-cli with no socket.
  - instruction: Vendor the RFC 6455 + append-codec pieces (cite-don't-import, provenance note in docs/skill-sources.md style); pyproject dependency list unchanged.
- The local RMS loudness sense stays: rms_sense.py / rms_background.py / orient.py power look-toward-sound and rms-corroborated rules independent of STT — server-side VAD replaces utterance CAPTURE, never the loudness sense or its #102 background-relative admission.
  - instruction: Zero diff in rms_sense.py / rms_background.py / orient.py.
- The audio pump stays the ONE consuming media.audio() reader: the WS streamer consumes the same per-tick chunk _AudioTap already fans out (via the worker queue) — it must NOT open a second SDK audio read, which would halve the audio per consumer (the #100 single-take rule).
  - instruction: Feed the WS streamer from the _AudioTap fan-out on the worker queue; never a second media.audio() reader.
- Rule scope (operator decision, q2): transcript predicates stay boolean (is_true/is_false) — a rule reacts to THAT something was said; reacting to WHAT was said remains the attached agent's job and is out of scope.
  - instruction: Leave rules.py's comparator sets untouched; add a refusal test for content ops over transcript if none exists.

## Non-goals

- Ears-only: the client never sends response.create and never consumes response.audio.delta / barge-in — the robot's voice remains reachy/behavior/speech_act.py (harmonic default, TTS option) and the say noun stays a dumb pipe. The new conversation surface on the wire is strictly opt-in and we do not opt in.
  - instruction: Never send response.create; ignore/refuse response.* events if they arrive.
- sleep's wake-word leg stays on HTTP: reachy/sleep/wakeword.py's HttpSttBackend keeps reusing Transcriber's /v1/audio/transcriptions POST — the sleep loop is a separate low-duty surface and migrating it is not needed for the wire break or for better runtime hearing.
  - instruction: Zero diff in reachy/sleep/wakeword.py; Transcriber's HTTP leg stays for the wake phrase.
- Daemon containerization is its own arc, tracked as issue #116 (lobes-style compose: pinned GStreamer/webrtcsink image, device passthrough, durable logs) — the chosen remediation direction for the #94 park. This frame's build + offline tests proceed independently; only the live five-word acceptance waits on #94 being resolved, by #116 or any other fix.

## Assumptions

- No realtime WS client exists in reachy-mini-cli today — reachy/speech/stt.py's Transcriber (stdlib-urllib multipart WAV POST to /v1/audio/transcriptions) is the only STT leg; the 'current client' issue #115 describes is prospective, validated so far only by lobes-cli's own smoke script at reachy's native 16 kHz mic rate (lobes-cli docs/evidence/2026-07-21-accept-realtime-spark.txt).
  - instruction: Build the client fresh from the issue #115 contract + the smoke-script donor; there is no legacy binary-frame code to migrate.
- This idea structurally resolves issue #111 (deaf across a room): the locally tuned start threshold (3x drifted background = 0.102, above a normal voice at ~2 m) stops deciding capture at all — #111's own option 4 names exactly this ('defer to server-side VAD when lobes#149 lands, and stop making this decision locally'), and the lobes acceptance showed the #149 motivating five-word question arriving as ONE utterance where the client threshold had shattered it into 'Ready, she'.
  - instruction: Close #111 via this arc's live acceptance; do not tune speech_ratio further in the meantime.

## Scope exploration

- `s1` — `agentculture/reachy-mini-cli#115 (the coordinated wire break)`: lobes#151 migrates /v1/realtime audio-in from raw binary WS frames to OpenAI-shaped base64 input_audio_buffer.append JSON text events; connect URL/query-param config, Bearer auth, and every consumed event (session.created, speech_started/stopped, transcription.completed, named errors) are unchanged; ears-only stays the default; the break is a strict cutover coordinated with this repo
  - seeds: `c2`
- `s2` — `reachy/speech/stt.py (the current HTTP STT leg)`: Transcriber is stdlib urllib only: rolling-window/throttle transcribe() plus transcribe_once() for callers doing their own endpointing; hand-rolled multipart, degrades to None on every failure, default REACHY_STT_URL=localhost:9002 (direct Parakeet). The realtime WS client is a sibling leg, not an edit of this class — and the realtime container still exposes /v1/audio/transcriptions (per the 2026-07-21 acceptance's REST-path probe), so the HTTP leg remains a viable degrade path
  - seeds: `c3`, `c4`
- `s3` — `reachy/behavior/transcript_sense.py (client-side endpointing + engagement)`: 1089 lines: tick thread does mic-chunk read, pre-roll ring, energy VAD (the #108 locator-not-filter rule), then hands finished utterances to a worker thread for the STT POST + engagement gate; every queue bounded, every put non-blocking. Server-side VAD replaces the ring/energy-VAD capture half; the worker-thread discipline, one-tick latch, engagement gate, and named senselog drop reasons all stay
  - seeds: `c5`, `c6`
- `s4` — `issues #108/#111/#102 (the hearing-defect lineage)`: #108 (spliced capture) is fixed by the contiguous-slice rule; #111 records the residual defect — the RELATIVE start threshold (#102) lands at 0.102 against the measured night background, above a normal voice across a room, so no utterance ever starts; #111 option 4 explicitly defers to server-side VAD as the likely end state. #102's relative threshold remains correct for what stays local (orienting/rms admission)
  - seeds: `c7`
- `s5` — `lobes-cli scripts/realtime-smoke.py (the citable WS donor)`: Hand-rolled stdlib-only RFC 6455 client whose docstring states the design intent verbatim: #149 exists to keep heavyweight deps off the reachy-mini-cli robot client, and a smoke test needing websocket-client/websockets would undercut that. Implements handshake, accept-key computation, masking, frame build/parse, PING/PONG, and the #151 base64 append/delta codec; pure pieces unit-tested in tests/test_realtime_smoke_helpers.py
  - seeds: `c8`
- `s6` — `reachy/behavior/{rms_sense,rms_background,orient}.py (the loudness sense)`: Loudness and orienting consume the same fanned-out audio chunk but are independent of STT; #102's relative threshold stays correct for them. Nothing in the realtime migration touches these modules
  - seeds: `c9`
- `s7` — `reachy/behavior/audio_pump.py + the _AudioTap fan-out`: Since #100, AudioPump.take() on a background thread owns ALL media.audio() I/O and _AudioTap swaps the latch once per tick, fanning ONE chunk to rms + transcript; taking it twice hands each consumer half the audio. The realtime streamer must be a third fan-out consumer of the same chunk
  - seeds: `c10`
- `s8` — `issue #115's ears-only clause + reachy/behavior/speech_act.py`: The wire's new conversation surface (response.create, audio-out, barge-in) is strictly opt-in per session; a client that never sends response.create gets exactly the transcription-only stream. The runtime's voice is speech_act.py (harmonic, offline) — no reason to route it through the realtime session
  - seeds: `c11`
- `s9` — `tests/test_zero_llm_boundary.py (_BEHAVIOR_SPEECH_ALLOW + equality pins)`: The runtime's speech imports are an explicit allow-list where each entry states why it is not a language model, with a companion test failing on dead entries and the one LLM edge (engagement classifier) pinned by equality in both directions — a new speech module the runtime imports MUST be added there deliberately
  - seeds: `c12`
- `s10` — `live box env + lobes endpoint stt/cortex`: Both roles resolve to the gateway <http://localhost:8001>, and the box already exports REACHY_OPENAI_URL_BASE=<http://localhost:8001> + REACHY_OPENAI_API_KEY; REACHY_STT_URL's default localhost:9002 is the DIRECT Parakeet container, but the realtime session goes through the gateway — the WS URL should derive from the gateway base, not from REACHY_STT_URL
  - seeds: `c13`
- `s11` — `reachy/sleep/wakeword.py (the other STT consumer)`: HttpSttBackend shares Transcriber's HTTP leg for the wake phrase only — low duty cycle, boolean outcome; nothing in the wire break forces it to move
  - seeds: `c14`
- `s12` — `reachy/behavior/rules.py (what rules can do with a transcript)`: SENSE_FIELDS includes transcript as a corroborating field, but the only legal ops over it are boolean is_true/is_false (plus absent_for); the shipped default_rules.toml fires on transcript is_true. Content-level reaction (matching what was said) does not exist in the rule schema today
- `s13` — `lobes-cli docs/evidence/2026-07-2{1,2}-accept-realtime*.txt (the live server leg)`: The server side is validated on THIS box's fleet: 7/7 checks at 16 kHz (reachy's native mic rate) on 2026-07-21, and the NEW base64 append wire 7/7 ears-only on 2026-07-22 (lobes-cli 0.54.0 bridge, gateway :8001 unchanged); the #149 motivating five-word question arrived as one whole utterance where the client-side threshold gave 'Ready, she'
  - seeds: `c15`
- `s14` — `tests/test_behavior_transcript_contiguous.py + docs/operating-reachy.md + CLAUDE.md`: Three tests pin the #108 contiguous-capture invariants of the client-side ring; the operating guide and CLAUDE.md document the energy-VAD capture and engagement stack in detail — all describe the path this idea partially replaces, so they move in the same change
  - seeds: `c16`
- `s15` — `reachy_mini SDK media chain (media_server.py + webrtc_client_gstreamer.py, installed 1.9.0)`: The daemon starts a GstMediaServer (GStreamer webrtcsink + signalling server); the SDK media client connects to ws://<host>:8443 (default signaling_port=8443). In the #94 state the daemon's HTTP half answers while :8443 refuses — the media half never started; suspect class is environment fragility (gst-plugins-rs webrtcsink, init_respeaker_usb, the asoundrc requirement), the same transitive-system-libs fragility that made reachy-mini an extra (PR #24)
  - seeds: `c19`
- `s16` — `lobes-cli lobes/templates/docker-compose.yml + init/serve/fleet verbs (the containerization model)`: lobes scaffolds a compose deployment (pinned image, .env, durable logs via mg-logwrap tee, restart policy, health checks) and the CLI drives docker compose without becoming a daemon — the citable pattern for a containerized reachy daemon; filed as issue #116 with device-passthrough and service-noun requirements
  - seeds: `c19`

## Resolved vagueness

- [unknown_blocking] issue #94: the media-profile SDK client cannot construct on the deployed box (ECONNREFUSED) — the runtime's ears are wired but dormant there. If still true at build time, no mic audio exists to stream regardless of the wire, and the live five-word acceptance (issue #115 criterion 2) cannot run. Must be re-verified on the box before acceptance. — resolved: Re-scoped by operator decision: #94's remediation is tracked in issue #116 (containerized daemon, lobes-style compose); the live five-word acceptance (c15/h8) is the tracked verification step that waits on it — matching the #103 soak precedent (tracked verification, not an in-session blocker). Build + offline tests proceed independently.
