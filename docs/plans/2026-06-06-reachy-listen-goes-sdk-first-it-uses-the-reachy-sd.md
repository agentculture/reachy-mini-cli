# Build Plan — reachy listen goes SDK-first: it uses the Reachy SDK in-process for real DoA and real mic loudness, reacting in two tiers — a faint noise leans the near antenna toward the sound; speech or a loud sound (RMS snap) makes Reachy slowly turn head-then-body to face the source. reachy-mini becomes a base dependency, which also lets the CLI track daemon liveness across restarts more simply (issue #21).

slug: `reachy-listen-goes-sdk-first-it-uses-the-reachy-sd` · status: `exported` · from frame: `reachy-listen-goes-sdk-first-it-uses-the-reachy-sd`

> reachy listen goes SDK-first: it uses the Reachy SDK in-process for real DoA and real mic loudness, reacting in two tiers — a faint noise leans the near antenna toward the sound; speech or a loud sound (RMS snap) makes Reachy slowly turn head-then-body to face the source. reachy-mini becomes a base dependency, which also lets the CLI track daemon liveness across restarts more simply (issue #21).

## Tasks

### t1 — Make reachy-mini (+numpy) a BASE dependency in pyproject (decision c8)

- covers: c22
- acceptance:
  - pyproject.toml moves reachy-mini into [project.dependencies] and adds numpy; the [sdk]/[daemon] extras still resolve; the HTTP/remote profile still imports with no robot hardware present.

### t2 — Add ANTENNA_KEY coalesce key to the motion queue

- covers: c18
- acceptance:
  - reachy/motion/queue.py defines ANTENNA_KEY (!= LOOK_KEY); tests/test_motion.py shows two ANTENNA_KEY actions coalesce while a pending LOOK_KEY action is untouched.

### t3 — SnapDetector: real RMS spike, cite reachy_nova detect_snap (c9/c15)

- depends on: t1
- covers: c16, h2
- acceptance:
  - new reachy/motion/snap.py SnapDetector.feed(audio)->bool: rolling energy deque, fires when rms=sqrt(mean(audio^2)) > 5x rolling_avg AND > ~0.02 floor AND prev chunk quiet (cite reachy_nova.tracking.detect_snap).
  - tests/test_motion.py: quiet->loud synthetic spike fires once; steady ambient never fires; sub-floor noise never fires.

### t4 — SDK transport: real DoA + audio sample, AEC ch0 (assumption c24)

- depends on: t1
- covers: h11
- acceptance:
  - reachy/robot/sdk_transport.py exposes doa() via reachy_mini.media.audio.get_DoA() and get_audio_sample()/samplerate/channels (AEC ch0); HTTP transport keeps DoA, returns no audio.
  - a unit test with a stubbed reachy_mini verifies doa()->{angle,speech_detected} and audio passthrough; no hardware needed.

### t5 — Tier-1 near-side antenna lean in ListenProducer (decision c11)

- depends on: t2
- covers: c18, h4
- acceptance:
  - ListenParams gains antenna knobs; for a usable DoA not (yet) turning, update() returns an ANTENNA_KEY MotionAction with ONLY the near-side antenna leaning (far ~neutral, head=None).
  - tests/test_motion.py: a faint single-direction feed yields exactly a near-side-only antenna action (near magnitude > far).

### t6 — Tier-2 gate: speech OR snap, latched-DoA guard (decisions c9,c10)

- depends on: t5, t3
- covers: c17, h3
- acceptance:
  - ListenProducer commits/holds a direction ONLY on speech_detected or a fresh snap, expiring it after a hold timeout (cite reachy_nova update_doa+speaker_hold); transient/latched angle never commits.
  - tests/test_motion.py: a constant/latched angle with no speech and no snap commits zero turns and recenters after the hold; a speech transition OR a snap commits exactly one turn.

### t7 — Tier-2 escalate head->body, fold antenna into the turn (decision c12)

- depends on: t6
- covers: c19, h5
- acceptance:
  - when a committed source persists off-axis beyond the head-only band, update() emits a slow body_yaw turn re-centering the head, with the antenna pose folded into the committing action (no stale backlog).
  - tests/test_motion.py: a persistent far-side feed produces a body-turn with head re-centering and no leftover queued antenna action.

### t8 — Wire RMS snap into the listen loop (SDK audio -> SnapDetector -> producer)

- depends on: t4, t7
- covers: c1, h1, c4
- acceptance:
  - the listen run loop pulls get_audio_sample() each tick (SDK profile), feeds SnapDetector, and a snap escalates Tier-2 toward the current DoA; HTTP profile (no audio) skips the snap path.
  - an integration test (stubbed SDK feeding audio,DoA): faint->antenna-only; loud spike->snap turn; speech->turn; the two tiers are distinct MotionAction shapes.

### t9 — CLI: SDK default transport + tiered default + tuning flags (decision c8)

- depends on: t8, t4
- covers: c20, h6, c2, h9
- acceptance:
  - reachy/cli/_commands/listen.py + robot default make 'reachy listen run' (no flags) select SDK transport + tiered producer; --transport http remains selectable; overview describes the two tiers + loudness.
  - tests/test_listen_cli.py: default run uses SDK+tiered; --transport http parses; every pre-existing flag and each new tier/RMS knob parses into ListenParams.

### t10 — Daemon/robot liveness via the SDK across restart (issue #21, decision c13)

- depends on: t4
- covers: c21, h7
- acceptance:
  - liveness is determined via the SDK transport (not only PID/health-poll); a restart (down->up) is reflected correctly, bounded to liveness (no full daemon-mgmt rewrite).
  - a unit test with a stubbed SDK simulates a restart and asserts the liveness check transitions down->up correctly.

### t11 — Docs: SDK-first README + CLAUDE.md + explain catalog (decision c8, boundary c6)

- depends on: t9, t10
- covers: c22, h8, c3, h10, c5, h12, c6, h13
- acceptance:
  - README + CLAUDE.md describe the SDK-first default (reachy-mini base dep, HTTP optional remote) and relax the zero-base-dep rule; explain/catalog.py describes the two tiers + loudness; no-vision/no-ML boundary + HTTP-still-selectable stated.
  - a test/grep confirms pyproject lists reachy-mini in base [project.dependencies]; the explain catalog entry for listen resolves (test_every_catalog_path_resolves stays green).

### t12 — Acceptance: end-to-end two-tier behavior + success signals (success_signal c7)

- depends on: t8, t9
- covers: c7, h14
- acceptance:
  - an acceptance/integration test (stubbed SDK) asserts the documented success signals: tap->antenna-only, clap/loud->snap turn, talk->slow head-then-body turn; documented as the on-robot acceptance script.
  - reflects real defaults and would pass on the live unit (mic confirmed working at localhost:8000).

## Risks

- [unknown_nonblocking] SDK API shapes (media.audio.get_DoA, media.get_audio_sample, samplerate/channels) assumed from reachy_nova (c24) — verify against the installed reachy-mini. (task t4)
- [unknown_nonblocking] Antenna near-side sign/axis is hardware-dependent (c23) — confirm on the live unit (mic verified at localhost:8000). (task t5)
- [unknown_nonblocking] Feel/thresholds (snap 5x + 0.02 floor, dwell, hold, head-only band, body speed) are tuning — ship defaults, tune live. (task t6)
- [unknown_nonblocking] Base-dep migration changes the README dep-free pitch; keep the HTTP/remote profile importable with no robot/SDK hardware present. (task t1)
- [unknown_nonblocking] Issue #21 scope is BOUNDED to SDK-based liveness; watch for regressions in existing daemon start/stop/status. (task t10)
- [follow_up] numpy as a base dep adds weight + softens the pure-stdlib ethos — accepted per the SDK-first decision (c8).
