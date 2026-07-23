# Build Plan — reachy nervous system

slug: `reachy-nervous-system` · status: `exported` · from frame: `reachy-nervous-system`

> Reachy's nervous system ships: vision, hearing and voice all work on the deployed robot (issues 94/97/120/121/122 resolved), and the runtime exposes its senses and media - camera frames, mic audio, DoA, sense events - on an event-based surface other services subscribe to, so a heavy sense never slows the 50 Hz tick and no consumer ever contends for the single SDK session

## Tasks

### t1 — Port the #99 episode-suppression pattern into TickMetrics (#121): streak state, first-tick emit, closing summary, >5x-budget immediate report, shutdown flush

- covers: c18, h6
- acceptance:
  - a simulated 30-min run at 77 percent overrun rate emits O(10) lines (test with fake clock); first overrun of a streak emits the existing line; closing summary carries count/mean/max vs budget
  - TickMetrics.overruns still equals the true per-tick overrun count (exactness test)
  - a tick exceeding 5x budget reports on the tick it occurs even mid-streak (spike-bypass test)
  - an episode still open at shutdown is flushed by the close path (shutdown-flush test); files touched: reachy/behavior/tick_metrics.py + its tests only

### t2 — Add the sense_extras doctor check (#120a): cv2 import-probe with remediation naming both pip and uv tool install forms

- covers: c3, h2
- acceptance:
  - doctor --json on an env without cv2 reports sense_extras failed, severity warn, remediation containing BOTH pip install reachy-mini-cli[vision] and the uv tool install spec form (subprocess-probed test, sys.modules-safe)
  - doctor --json with cv2 present reports sense_extras passed; text mode renders ok/FAIL lines; files touched: reachy/cli/_commands/doctor.py + tests only

### t3 — Publish per-sense availability into state.json (#120b): face_sense exposes its vision-extra-absent reason; a senses block lands via the seam-rider path (c39)

- covers: c3, h2
- acceptance:
  - on a box without [vision], state.json contains senses.face.available false with reason vision-extra-absent; with the extra, available true reason null — no code change between the two (env-driven test)
  - the senses block covers every _COMPOSED_PROVIDER_FIELDS entry plus pat; placement is the seam-rider state augmentation (engine.py stays sense-agnostic) unless the build proves engine.state cleaner — decision recorded in the PR
  - files touched: reachy/behavior/face_sense.py, the rider module, _commands/behavior.py composition + tests; engine.py untouched or a one-line hook only

### t4 — Dispose issues #94 and #97 on GitHub: close #94 with the 2026-07-23 evidence, restate #97 soak criterion S3 around cadence stability

- covers: c25, c28
- acceptance:
  - #94 is closed with a comment citing the measured warm-up evidence and naming the acquire-before-construct fix; its collapse-repeated-failures concern is noted as folded into the #121 fix
  - #97 carries a comment restating S3 (cadence stability + no >5x spikes, never per-tick budget compliance) and recording the under-20ms half as retired by operator decision; issue left open only for the optional apportionment run

### t5 — Correct the stale #94-premise docs and record the new decisions: ALSA-sharing fact, events-cli base-dep decision, monitor-speaker test vector

- covers: c4
- acceptance:
  - no doc claims the http playback default exists because the media-profile SDK client is unconstructable (#94 premise); CLAUDE.md and docs/operating-reachy.md state the ALSA-sharing fact instead
  - CLAUDE.md hard-constraints records the pending events-cli base-dep decision (c42/c43) and the operating guide documents the monitor-speaker AEC-exempt test vector (c30); files touched: CLAUDE.md, docs/operating-reachy.md only

### t6 — Build the MQTT publisher module against an injected client seam: event mapping, retained state mirror, LWT, QoS0, degrade — all fake-client TDD

- covers: c22, h23, c35, c36, h21, c29, h17, c10
- acceptance:
  - a fake client receives runtime-feed events on reachy/events/{source}/{type} and retained state on reachy/state/{key}; payloads match docs/export-schema.md vocabulary; retained state equals the state.json payload from the ONE builder (equality test)
  - the publisher declares the retained reachy/state/online availability topic with LWT config and re-publishes current state on reconnect (fake-client sequence test)
  - a degraded or absent client resolves to ONE named senselog drop and no-op publishes — never an exception on the caller; publish at the seam is an O(1) enqueue (no network I/O on the calling thread, asserted by test)
  - no event schema carries inline binary or base64 media — a schema-level test asserts text-reference-only fields (c29); files touched: NEW reachy/export/mqtt.py + NEW tests only

### t7 — Compose the publisher unconditionally in behavior engine run: lazy events-cli import, REACHY_MQTT_URL env, graceful degrade, additive to --export

- depends on: t3, t6
- covers: c34, h20, c21, h10, c13, h9, c9, h8, h4
- acceptance:
  - the publisher is composed on every engine run (no flag): a missing events-cli package or unreachable broker yields one named drop and an unchanged runtime (composition test, SDK-less)
  - every existing --export runtime/cognition feed test passes unchanged (additive proof, h20); no listening socket is added — the no-server grep stays true (h10); no new media.audio()/get_frame() caller appears (h9); no second SDK media session is constructed (h8, composition assertion)
  - REACHY_MQTT_URL (default localhost:1883) reaches the client seam; files touched: reachy/cli/_commands/behavior.py + composition tests (serialized after t3 on the same file)

### t8 — Extend docs/export-schema.md with the topic map: reachy/events tree, retained reachy/state keys, online/LWT, per-topic QoS

- depends on: t6
- covers: c29, h17, c34, h20
- acceptance:
  - the schema doc gains a topic-map section (events tree, retained state keys, reachy/state/online semantics, QoS 0 policy) as an ADDITION — the stdout wire contract text is byte-unchanged (diff check)
  - the section states the no-media-payloads rule (text references only) so an external consumer can validate; files touched: docs/export-schema.md only

### t9 — LIVE session A — re-enable the voice (#122) and run the c31 audio-push probe

- covers: c4, h3
- acceptance:
  - tombstone removed + behavior reload: a monitor-speaker TTS greeting produces the greet-when-addressed fire line AND audible speech (daemon ALSA line + human ear); the robots own reply does NOT re-enter hearing, including one LONG utterance (self-mute vs server-VAD)
  - the probe result is recorded: whether push_audio_sample tolerates a second thread against the held client while the runtime is live — the artifact that decides t10s shape
  - look-toward-sound stays tombstoned; session transcript + journal excerpts saved for the delivery record, naming the branch the box runs

### t10 — Inject the held media client into SpeechActuator in the shape the t9 probe dictates (direct or pump-style output seam)

- depends on: t7, t9
- covers: c16, h5
- acceptance:
  - _make_speech_actuator gains an injected media-session seam; the sdk playback path reaches the HELD client — no second media client construction (asserted by test)
  - the threading contract holds: either the probe proved the push tolerates the speech worker thread (recorded), or pushes route through a pump-style output seam mirroring AudioPump; a wedged or dead sink still resolves to named drops, never tick backpressure
  - files touched: reachy/behavior/speech_act.py, reachy/cli/_commands/behavior.py (serialized after t7), tests; live audible proof lands in t12

### t11 — Repoint the reTerminal bridge: an MQTT subscriber module in the reterminal-cli sibling repo consuming reachy/events/# + reachy/state/online

- depends on: t6, t8
- covers: c27, h15
- acceptance:
  - reterminal-cli gains a subscriber module (its own repo, own PR, own tests) that renders the panel from broker events per the t8 topic map, honoring reachy/state/online for a live/stale indicator
  - the subscriber imports no reachy code and touches no SDK (h15); the old stdout-bridge scripts remain until the acceptance session proves the broker path, then are retired in that repo

### t12 — LIVE session B — the acceptance run: full sensorium + broker + panel + kill tests + 30-min soak (PR gate)

- depends on: t1, t2, t3, t5, t7, t10, t11
- covers: c1, h1, c10, h4, c23, h11, c32, h19, c24, h12, c25, h13, c26, h14, c27, h15, c28, h16, c35, h18, c40, h22
- acceptance:
  - one session demonstrates: a transcript event and a face/frame_available flip on the broker, a monitor-speaker greeting answered aloud with no self-answer loop, and the reTerminal panel rendering live events (h1, h12)
  - kill -9 the runtime: reachy/state/online flips false via LWT while other retained state persists; restart re-publishes and flips true (h18); stopping the broker mid-run leaves tick cadence and rules unchanged with one named drop (h4)
  - a 30-minute soak produces O(10) overrun lines with .overruns exact (h6 live half); ss shows 1883 loopback-only and a non-loopback connect is refused, exactly one broker on the box (h19, requires events-cli deployed per events-cli#3)
  - doctor + state.json flip demonstrated for [vision] absent vs installed (h2 live half); every recorded check names the branch the box runs (h22); evidence bundle (transcripts, journal excerpts, GitHub links) lands in the delivery summary (h13, h14, h16)

### t13 — Version bump + CHANGELOG + uv lock; the PR closes the arc

- depends on: t12
- acceptance:
  - version bumped via the version-bump skill with a CHANGELOG entry; uv lock regenerated in the same change (the recorded gotcha); CI green including the rubric gate; the PR body links issues #120/#121/#122/#94/#97 and events-cli#3

## Risks

- [unknown_nonblocking] events-cli timing gates t11/t12 broker items and the pyproject base-dep line: the importable client + events up on this box (events-cli#3) must exist before the live acceptance; everything else proceeds behind the seam + graceful degrade (task t12)
- [unknown_nonblocking] the t9 probe outcome flips t10s implementation shape (direct push vs pump-style output seam) — cost is bounded to that one module either way (task t10)
- [unknown_nonblocking] three tasks touch reachy/cli/_commands/behavior.py (t3, t7, t10) — serialized by explicit deps; the merge order is t3 then t7 then t10, never same-wave (task t7)
- [unknown_nonblocking] the box RUNS this checkout via the editable tool install: fan-out lanes stay in ../.worktrees.reachy-mini-cli/, the main checkout stays on a known-good branch during the build, and a systemd restart mid-build picks up whatever is checked out (c40)
- [unknown_nonblocking] model-gear gateway/stt/realtime containers report unhealthy healthchecks while functionally serving — the t12 session depends on lobes hearing being actually up; verify before starting, not from healthcheck color (task t12)
