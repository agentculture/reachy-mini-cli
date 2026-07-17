# Build Plan — event-based senses pipeline

slug: `event-based-senses-pipeline` · status: `exported` · from frame: `event-based-senses-pipeline`

> Reachy's live presence becomes a fully event-based senses pipeline: speech is captured whole and heard correctly (continuous recording with a pre-roll ring buffer that reaches 1-2 seconds back at speech onset, ported from reachy_nova's proven design), and every sense — spoken words, audio direction, touch, camera images, and basic face recognition — lands as an event the agent reasons over; every pipeline stage emits a structured log line so capture can be verified end-to-end; the agent responds seamlessly by speaking, playing its harmonic voice, moving its body, or all at once; and code tasks flow to qwen3 to forge new reaction seams for the agent to use or improve

## Tasks

### t1 — PR A t1: sense-stage log helper (reachy/senselog.py, cited from nova sensory_log.py)

- covers: c4
- acceptance:
  - stage(stage, source, event, detail) emits the fixed parseable shape [SENSE stage=<stage> source=<source> event=<event>] <detail> on a dedicated logger at INFO
  - a drop(reason) variant always names the reason (self-mute, throttle, gate-reject, cooldown) — asserted by test
  - files: reachy/senselog.py, tests/test_senselog.py — no other module touched

### t2 — PR A t2: logging handler + level control at run entry (listen/think/sleep)

- covers: c5, h5
- acceptance:
  - a shared install_logging helper (reachy/cli/_logging.py) attaches ONE stderr StreamHandler with level from --log-level/REACHY_LOG_LEVEL (live default INFO); double-install is a no-op (no duplicate lines, asserted)
  - under --export, stdout stays pure JSONL — handler writes stderr only (export purity test)
  - after the change a foreground listen run shows INFO stage traces on stderr; journalctl shows them under reachy-live.service (live check in the PR A verify)

### t3 — PR A t3: pre-roll ring buffer + measured onset in TranscribeHook

- depends on: t1
- covers: c2, h2
- acceptance:
  - a rolling ~10s chunk ring buffer is fed every tick from SenseSample.audio under --transcribe (before any speech flag), trimmed by samples not chunks
  - on the speech-flag rising edge the onset is measured by scanning 10ms RMS windows back through the buffer; the utterance starts at onset minus pre_roll (default 2.0s, clamped to buffer start) — proven by a fake-clock test with injected chunks showing the emitted utterance audio includes pre-flag samples
  - existing silence_hold/max_utterance endpointing and the engagement gate are unchanged (existing test_listen_transcribe suite stays green)
  - capture and onset [SENSE] lines emitted via senselog

### t4 — PR A t4: [SENSE] instrumentation across event/cue/turn/action stages + loud drops

- depends on: t1
- covers: c4, h4
- acceptance:
  - EventBuffer feed_* methods log a cue [SENSE] line; engine run_turn logs a turn line; tool dispatch logs an action line; every drop path (self-mute window, cooldown, gate-reject, audio-mute) logs its reason — grep-asserted in unit tests
  - files: reachy/speech/events.py, agent_turn.py, cognition.py, tools.py + their test files; tools.py import-boundary tests stay green (senselog is not llm/events/motion)

### t5 — PR A t5: direction invariants regression suite

- depends on: t2, t3
- covers: c9, h8
- acceptance:
  - tests assert feed_doa_cues=False under --transcribe (no raw DoA cues) and direction-tagged transcripts still flow (existing behavior pinned)
  - a fake-clock test documents the nova rate-limit contract (one direction event per 2s unless bearing jumps 15 degrees) for any future direction event — currently exercised against the transcript-tagging path only

### t6 — PR B t6: camera-path repair — SDK-canonical (user direction), version alignment first

- covers: c25, h16
- acceptance:
  - SDK and daemon versions aligned (uv sync the [daemon] extra to 1.9.x; uv.lock updated) — the mismatch warning is gone at client open
  - the guessed _import_camera seam (is_local_camera_available / media_manager.camera) is replaced with the surface that exists: media.get_frame() / media.camera, acquire_media respected; unit tests use the injectable seam, no robot
  - a 30s live frame soak returns non-None frames with a real shape at a sustained rate while the daemon runs; if the aligned SDK still cannot serve frames the task STOPS and the non-SDK route goes back to the user as a question
  - issue 28 updated with the probe + soak evidence (no hang — daemon-owned device + version mismatch)

### t7 — PR B t7: VisionHook feeds cognition (feed_vision wiring + coalescing)

- depends on: t6
- covers: c6, h6
- acceptance:
  - on a motion/light decision VisionHook also calls buffer.feed_vision(direction, brightness_delta), coalesced to one cue per motion episode (fake-clock test)
  - the live composition threads the SAME cognition EventBuffer into VisionHook (object-identity test in test_listen_cognition_agent)
  - a hung get_frame never stalls the tick loop — the existing bounded-join grabber pattern stays, proven by the existing suite

### t8 — PR B t8: face engine module — YuNet+SFace port behind the NEW [vision] extra

- depends on: t6
- covers: c7
- acceptance:
  - reachy/vision/face.py cited from nova face_recognition.py + face_manager.py: YuNet detect + SFace 128-dim embed, cosine match threshold 0.5, temporary (TTL) vs permanent tiers, models auto-downloaded under state_dir; lazy import, missing [vision] extra raises the clean exit-2 CliError
  - pyproject gains the [vision] extra (opencv-python-headless); base deps unchanged (numpy + harmonics-cli only)
  - a unit suite over stored embeddings proves match/threshold/tiers/cooldown with no robot and no opencv at collection time (skip-marked when the extra is absent)

### t9 — PR B t9: FaceHook + feed_face cue + enrollment seam, folded into the live chain

- depends on: t7, t8
- covers: c7, h7
- acceptance:
  - a FaceHook in the HookChain runs detection on the shared frame holder at a bounded cadence on a background worker and feeds a face cue (named face, re-announce cooldown honored; unknown faces never yield a name claim)
  - an enrollment seam exists (API or verb) storing a permanent embedding; composition threads the shared buffer (identity test)

### t10 — PR B t10: scene description — one shared describe path, on-demand tool + periodic hook (Gemma4 via lobes roles)

- depends on: t9
- covers: c24, h15
- acceptance:
  - a describe path JPEG-encodes the current frame and POSTs a multimodal chat-completion to REACHY_OPENAI_URL_BASE with a vision-model env defaulting to the lobes senses role model; request shape + encode proven by unit test, no network
  - a describe_scene tool registers in ToolRegistry (on-demand); a periodic hook (default 30s, configurable) feeds a scene cue into the shared buffer on a background worker
  - an unreachable/slow VLM degrades loudly (logged reason, no crash) and never stalls the tick loop (fake-transport test); verified early against the LIVE senses route once (multimodal support on the gateway is unproven)

### t11 — PR B t11: single-session composition proof

- depends on: t9, t10
- covers: c12, h18
- acceptance:
  - a composition test asserts the folded live loop constructs exactly ONE media session with every hook (transcribe, pat, vision, face, scene, think, sleep) riding the shared SenseSample tap / frame holder
  - every frame/VLM call site is on a background worker with a bounded join — asserted by test, no robot

### t12 — PR C t12: forge package — qwen3 client + AST-only validator + staged/activated/rejected lifecycle

- covers: c11
- acceptance:
  - reachy/forge/ cited from nova skill_forge.py + forge_validator.py: dispatch(goal, context, improve) on a background thread POSTs chat/completions to FORGE_BASE_URL/FORGE_MODEL/FORGE_API_KEY (default: the lobes gateway cortex route on :8001); SKILL.md + executor fence parsing with content-sniffing fallback
  - the validator is AST-only and fail-closed: import allow-list, forbidden names, restricted ctx attrs, line cap, dunder rejection, execute(params, ctx) required; validator-unavailable REJECTS; a negative test suite covers every rejection class
  - staged fires only after validation; every failure path resolves to a loud rejected event with reasons under state_dir — never an exception, never silent (fake-transport tests)

### t13 — PR C t13: forge tool + validator-gated auto-activation + hot registration

- depends on: t12, t10
- covers: c11, h10
- acceptance:
  - a forge tool registers in ToolRegistry (goal + optional improve) returning immediately; on staged, auto-activate moves the artifact to the active dir and hot-registers the new tool with a restricted ctx exposing only sanctioned seams (speak/harmonics/express/state)
  - the engine-restart note is honored: if the published tool schema is fixed per turn-loop session, activation defers visibly until the next turn/session and says so; [SENSE]/forge lifecycle lines logged
  - tools.py import boundary stays green; generated code never executes without passing the validator (asserted)

### t14 — PR C t14: docs + evidence — operating guide senses section, before-state citations, issue updates

- depends on: t13
- covers: c17, h13, c18, h19
- acceptance:
  - docs/operating-reachy.md + CLAUDE.md gain the senses-pipeline section (pre-roll, [SENSE] log grammar, face/scene, forge lifecycle, [vision] extra install); before-state evidence lines cited (listen_transcribe.py:296 gate, feed_vision zero callers, face grep empty, no handler, dormant stash)
  - issues 28 and 32 updated/closed with evidence; markdownlint green

### t15 — PR C t15: final on-robot acceptance session + delivery summary

- depends on: t3, t4, t5, t11, t13, t14
- covers: c1, h1, c15, h11, c16, h12, c19, h14, c8, h17
- acceptance:
  - one live session demonstrates the full after-state: first-word-complete sentence, named-face greeting, vision cue referenced, touch response, combined speech+movement turn, journal [SENSE] trail, and one real qwen3 forge round-trip producing a validated seam the agent uses
  - each audience benefit checked (person/operator/model/rig); transcript + journal excerpts + CI links recorded in the delivery summary
  - the three PR diffs contain no re-implementation of the 66/67/68 work (feed_pat, pose enum, TTS latch) and the pat suites stay green

## Risks

- [unknown_nonblocking] the in-flight 66/67/68 PRs land first by decision — listen.py/listen_pat.py rebase churn is expected for PR A/B; re-run their suites after every rebase
- [unknown_nonblocking] SDK 1.9 alignment (t6) may shift other media APIs the hearing loop depends on (get_audio_sample, DoA shape) — the full listen suite + a live hearing check must pass in the same PR before vision work builds on it
- [unknown_nonblocking] multimodal (image) support through the lobes gateway senses route is unproven — t10 verifies it live early; if the gateway cannot proxy images the scene leg needs a route fix on the model-gear side first
