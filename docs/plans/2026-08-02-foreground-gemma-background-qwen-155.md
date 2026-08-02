# Build Plan — Foreground Gemma, background Qwen (#155)

slug: `foreground-gemma-background-qwen-155` · status: `exported` · from frame: `foreground-gemma-background-qwen-155`

> Reachy Mini is one embodied presence over two model lanes: the operator talks with Gemma — the foreground interlocutor that listens, sees, speaks and handles turn-taking — while Qwen follows the conversation in the background, reasons over longer horizons, operates tools, and injects compact thinking scopes through typed events. Long replies stream as cancellable chunks a human can interject over, attention gates the voice as well as the mind, perception arrives as compact fresh snapshots that never evict runtime facts, and the attention window is an operator knob.

## Tasks

### t1 — Measure the media and chunk budgets against the deployed stack

- instruction: Probe the deployed gateway (`REACHY_OPENAI_`\* env) with one real clip from the robot's rider; the audio-emitting half plays sound on the deployed robot — coordinate with the operator before running it. Record both numbers in docs/evidence/2026-08-XX-media-chunk-budget.md. No repo code changes.
- acceptance:
  - docs/evidence records token count + latency of one 6s@8fps clip ask against the deployed gateway
  - docs/evidence records the per-chunk daemon HTTP /media/play round-trip for a 1-2s chunk (operator-coordinated: it plays audio on the robot)

### t2 — Attention window knob: --attention-window + `REACHY_EMBODY_ATTENTION_WINDOW`

- instruction: Files: reachy/cli/`_commands`/agent.py (flag + env resolution) and the embody supervisor arg forwarding. The #147 class is the trap: start/restart spawn a child — the flag must reach it, add the test FIRST. Env from process env only (the `REACHY_EMBODY_WORKER_MODEL` precedent).
- covers: c5, h4
- acceptance:
  - flag and env reach the child process spawned by start/restart, pinned by test (the #147 defect class)
  - 0 yields name-only-forever, pinned by test; env resolved from process env only, never environment.d

### t3 — Kind-aware context park: latest-wins perception slot beside exact-text cue coalescing

- instruction: File: reachy/embody/engine.py (the context park). Add a fact-kind key, keep dict-of-text coalescing for the closed cue vocabulary byte-identical. The latest-wins slot is a state, not an event — one slot per perception source.
- covers: c13, h11, c7, h6
- acceptance:
  - a synthetic hour of 20s free-text perception updates occupies ONE slot and every runtime-fact class remains representable, pinned by test
  - closed-vocabulary exact-text coalescing behavior is unchanged, pinned by the existing tests still passing

### t4 — Nested windows m=20/n=60 and the Qwen summary in Limits

- instruction: File: reachy/embody/engine.py (Limits + history). All three bounds (m, n, summary cap) in Limits with their `DEFAULT_`\* constants; refuse m>n at construction fail-closed. Summary is Qwen-maintained — this task builds the plumbing and staleness marker, t12 the producer.
- depends on: t3
- covers: c3, h2, c45, h30
- acceptance:
  - m<=n pinned over ONE shared history deque; Gemma's window is a strict suffix; construction refuses m>n fail-closed
  - a 100+ turn growth test shows Gemma's per-call context stays bounded
  - a down worker yields the stale-summary marker in Gemma's context plus a named senselog drop; marker clears on next successful update

### t5 — Interjection policy module and the typed event family

- instruction: NEW file reachy/embody/interjection.py + the event family in reachy/`runtime_cues.py` + classification in embody/cues.py. Never import engagement.py (zero-LLM equality pin). Extend tests/`test_embody_redteam.py` in the same change. The wanted-to-say TYPE lives here; t7 emits it.
- covers: c20, h12, c22, h13, c42, h27, c43, h28
- acceptance:
  - authorization default OFF: an unauthorized interjection from any route resolves to a named drop, never speech, pinned by test
  - warm-only under base level; separate proactive level for cold; a spoken interjection never opens the attention window (test mirrors the `note_spoken` extend-never-open pin)
  - per-source default-deny + rate bound pinned; tests/`test_embody_redteam.py` extends to the interjection event family in the same change
  - wanted-to-say artifact type is bounded, expiring, attributed, and structurally context-only (never enters the trigger path)

### t6 — Chunked cancellable playback in the duplex client

- instruction: File: reachy/speech/`realtime_duplex.py` (playback path only — stay gate-free). Play is already on a dedicated thread; convert accumulate-then-play into a chunk queue with skip-remaining cancel. Harness: tests/`fake_realtime_server.py`. Chunk-size default is a Limits value t1's numbers may retune.
- covers: c12, h10, c6, h5
- acceptance:
  - response audio plays as chunk groups as deltas arrive; skip-remaining cancel empties the queue within one chunk boundary, pinned against the fake realtime server
  - the session pump and keepalive are never starved by playback (play stays on its dedicated thread), pinned by test
  - a reply cancelled before any chunk played is never spoken and never recorded as spoken (existing behavior preserved)

### t7 — Said/unsaid truth: measured cut offsets and the wanted-to-say artifact

- instruction: Files: `realtime_duplex.py` (measured offsets off the chunk queue) + engine.py (recording). Cite lobes-cli lobes/realtime/`_floor.py`:323 `estimate_spoken_prefix` by path in the docstring — cite-don't-import. Wanted-to-say uses t5's type; never a trigger.
- depends on: t6, t5, t4
- covers: c34, h22, c39, h24, c41, h26
- acceptance:
  - the estimator cites lobes `_floor.py` `estimate_spoken_prefix` by path in its docstring and reproduces the word-boundary unsaid bias, pinned with known played-chunk offsets
  - after a mid-playback cut: the said portion is recorded as spoken, the remainder lands as a wanted-to-say artifact readable by the next turn, pinned by test
  - the phase-1 server-history overstatement is documented in the spec/operating guide and the layer's canonical record makes no claim the server matches

### t8 — Per-utterance arming: attention gates the voice

- instruction: Composition in `_commands`/agent.py drives arm() from AttentionGate per admitted utterance; `realtime_duplex.py` gains only a capability probe + one-shot arm API. The wire module must stay gate-free — three structural pins say so. Fake server grows the one-shot mode.
- depends on: t6, t2
- covers: c4, h3, c11, h9, c46, h31
- acceptance:
  - a cold ambient utterance sends NO response.create (no spoken reply), an admitted one arms exactly one response, pinned against a fake server implementing one-shot arming
  - capability check degrades to today's arm-once behavior against a gateway without disarm, with a named drop, pinned by test
  - the arming decision lives at the composition layer reading AttentionGate; `realtime_duplex.py` stays gate-free (structural pin unchanged)

### t9 — Connect-time voice conventions via the `system_prompt` override

- instruction: Connect URL builder in the shared realtime session config. Name the env `REACHY_EMBODY_VOICE_PROMPT` (process env only). The prompt asks for chunk-friendly spoken replies — the operator accepts minor gaps. No new frame kind: assert the three-frame pin still passes IN this task.
- depends on: t8, t7
- covers: c10, h8
- acceptance:
  - the connect URL carries the per-session `system_prompt` (persona + reply-length policy) with no new frame kind; the three-frame pin passes unchanged at this task
  - the prompt content is operator-configurable and documented; absent config falls back to the gateway default

### t10 — conversation.item.create client leg behind a capability check

- instruction: Files: reachy/speech/`realtime_wire.py` + `realtime_duplex.py`. The pin widening to four frame kinds happens HERE and only here (h20 cites decision c28). Extend `fake_realtime_server.py` with the items route per the lobes#170 schema agreement — watch that issue for upstream's answer on context-vs-history items before finalizing the payload shape.
- depends on: t9
- covers: c15, h20, c44, h29, c40, h25
- acceptance:
  - the fourth frame kind lands in the SAME change that widens the duplex send-surface pin from three to four, citing decision c28
  - a gateway without item support yields one named items-unsupported drop and degrades to connect-time `system_prompt` context, pinned against the fake server
  - after a forced session drop, re-seed (m-window + summary) is ordered BEFORE re-arming, pinned by test

### t11 — Layer-curated canonical history and its projections

- instruction: File: reachy/embody/engine.py (canonical history) + the re-seed path. Ordering is the trap: re-seed BEFORE re-arm on every reconnect, pin it. The correction-after-cut path is capability-gated like everything items-shaped.
- depends on: t10, t4, t7
- covers: c38, h23
- acceptance:
  - one source of truth pinned: the canonical history feeds Gemma's m-window (strict suffix), Qwen's n-window, and the floor re-seed content — no second independently-maintained history
  - context items and history turns are kept distinct per the lobes#170 schema agreement, verified against the fake server implementing it BEFORE the first item is sent live
  - the correction-after-cut path updates the floor with the client-measured said portion once items exist

### t12 — Cognition scopes and Qwen's governed voice

- instruction: Files: reachy/embody/engine.py (scope intake + summary producer) + tools.py (speak/harmonics through t5's policy). The structural no-raw-reasoning test mirrors the existing `enable_thinking`=False stance. Scope coalescing keys on kind+goal, not exact text.
- depends on: t5, t4, t7
- covers: c2, h1, c8, h7
- acceptance:
  - the scope artifact carries goal/facts/next-step/priority/expiry/speakable with source attribution; size bounded and enforced; expiry pinned by test
  - a structural test pins that raw model reasoning fields never reach the foreground prompt builder
  - the worker's speak/harmonics tools route through the interjection policy: unauthorized use is a named drop; no code path lets worker text reach TTS/playback directly, pinned structurally

### t13 — Perception snapshots: structured, fresh, latest-wins

- instruction: Files: `_commands`/agent.py (`_ClipAsker` -> snapshot producer) + engine.py (slot consumer). The ask prompt requests structured fields; a parse failure degrades to the summary-only slot with a named drop, never a crash. Freshness reuses the existing clip staleness discipline (30s).
- depends on: t12, t8, t3
- covers: c7, h6
- acceptance:
  - the clip asker produces a snapshot (summary, entities, confidence, capture time/freshness, frame ref) instead of free prose; the latest-wins slot consumes it
  - an offline what-can-you-see flow answers from the latest valid snapshot; a stale snapshot expires rather than lingers, pinned by test

### t14 — Model pairing, docs, and the version bump

- instruction: Docs: operating guide (new two-tempo section), CLAUDE.md noun catalog updates, docs/export-schema.md for new event families. Version bump via the version-bump skill AND uv lock (the PR #33 gotcha). Doctor check for the model pair is additive.
- depends on: t11, t12, t13
- acceptance:
  - doctor/status names the `OPENAI_MODEL` + `REACHY_EMBODY_SENSES_MODEL` pair and warns on divergence
  - operating guide + CLAUDE.md + export-schema document the two-tempo architecture, the interjection policy, the phase-1 limitation, and the non-goals; markdownlint green
  - version bumped with uv lock refreshed (the PR #33 gotcha) and a CHANGELOG entry

### t16 — Client-side tail cut: an interjection stops the robot inside the post-response.done window

- instruction: Deviation d1 created this task — read 'devague deviate --list' first. Files: reachy/cli/`_commands`/agent.py (the policy, beside t8's `_utterance_tap` arming) and a minimal seam in `realtime_duplex.py` if needed (it must stay gate-free; t8 added `arm_once` as the model to copy). The gap is ONLY the tail window: upstream paces delivery to the playhead (lobes `_conversation.py` `delivery_pause_ms`, `DELIVERY_LEAD_MS`=400) so the server covers the bulk of a reply — do NOT rebuild what response.interrupted already does, and make sure the two paths cannot double-record one reply (t7's `_correct_spoken` narrows an already-recorded reply; reuse it). Chunk size and daemon latency set the window's width; t1's audio measurement is still deferred, so do not hard-code an assumption about it.
- depends on: t7
- covers: c34, h22
- acceptance:
  - VAD-verified `speech_started` while the playback queue is non-empty cuts playback within one chunk; a cut with nothing playing is a no-op, pinned by test
  - the cut records the measured said/unsaid split via `spoken_split`() + `note_interrupted_reply`(), so the remainder becomes a wanted-to-say artifact exactly as a server-driven interrupt does
  - the trigger keys on VAD-verified speech only (never raw loudness, per c35) and the policy lives at the composition layer; `realtime_duplex.py` stays gate-free with its structural pins untouched
  - the server-driven response.interrupted path still works and the two paths never double-record one reply, pinned by test

### t15 — Live acceptance: the eight #155 scenarios on the deployed robot (PR gate)

- instruction: Live on the deployed robot: stop-to-test discipline (note which presence unit runs), operator in the room for the spoken scenarios. Evidence file per the docs/evidence/ convention with per-scenario pass/fail. Scenarios blocked on upstream (items live half, one-shot barge-in) are recorded BLOCKED-ON-UPSTREAM, not rounded up. Close #149/#150/#151/#153/#154 with evidence links; #155 stays open if anything is blocked.
- depends on: t14, t1, t16
- covers: c1, h14, c23, h15, c24, h16, c25, h17, c26, h18, c14, h19, c16, h21, c4, h3, c6, h5
- acceptance:
  - all eight scenarios executed live and recorded in docs/evidence/ with per-scenario pass/fail, judged from the room (speaker/mic), failures reported faithfully
  - cold ambient speech produces no audible reply AT THE SPEAKER; a human interjection over audible speech stops the robot within roughly one chunk
  - at PR time: redteam AST pins green, zero-LLM equality pins green, the runtime diff inside reachy/behavior/ + reachy/motion/ is additive-only (re-measured), and the #149/#150/#151/#153/#154 issues receive closing evidence

## Risks

- [unknown_nonblocking] Upstream lobes-cli#170 timing: the items integration (h23 live half) and the one-shot-arming live barge-in check (h31) cannot complete against the deployed gateway until upstream ships; capability checks keep every client task deployable meanwhile, and the graph is already sequenced client-first
- [unknown_nonblocking] Chunk-gap audibility: the per-chunk daemon HTTP round trip may exceed the operator's accepted minor space between chunks — t1 measures; fallback is larger chunks (longer cut latency) or pre-upload pipelining on the daemon route (task t6)
- [unknown_nonblocking] Two-estimator drift: until items land, the floor's wire-based heard-prefix and the client's measured cut disagree after any client-side interruption — the recorded phase-1 limitation (c39), closed by t11's correction path (task t7)
- [unknown_nonblocking] Gemma contention: the floor's per-utterance generate and the layer's clip asks share one Gemma service; latency under conversational load is unmeasured — t1's numbers inform whether the clip poll interval must widen (task t13)
- [follow_up] Mesh-source authentication for event-borne interjections beyond the box's local-trust model — deliberate follow-up (park v5); local per-source default-deny suffices for the deployed box
