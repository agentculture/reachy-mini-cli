# reachy listen goes SDK-first: it uses the Reachy SDK in-process for real DoA and real mic loudness, reacting in two tiers — a faint noise leans the near antenna toward the sound; speech or a loud sound (RMS snap) makes Reachy slowly turn head-then-body to face the source. reachy-mini becomes a base dependency, which also lets the CLI track daemon liveness across restarts more simply (issue #21).

> reachy listen goes SDK-first: it uses the Reachy SDK in-process for real DoA and real mic loudness, reacting in two tiers — a faint noise leans the near antenna toward the sound; speech or a loud sound (RMS snap) makes Reachy slowly turn head-then-body to face the source. reachy-mini becomes a base dependency, which also lets the CLI track daemon liveness across restarts more simply (issue #21).

## Audience

- A Reachy Mini owner running 'reachy listen', and the maintainer evolving motion/listen.py + the SDK transport + daemon-liveness.

## Before → After

- Before: Today listen is HTTP-first: it reads the daemon's /api/state/doa {angle, speech_detected} only (NO loudness), turns head-yaw only, and the daemon is hand-managed as a background OS process (PID/log/health-poll in reachy/daemon.py) that is fragile across restarts (#21). reachy-mini is an optional [sdk]/[daemon] extra; base deps are [].
- After: listen is SDK-first by default: real DoA + real mic RMS loudness in-process (reachy_mini.media). Two graded tiers — TIER 1 faint noise: near-side antenna leans toward it; TIER 2 speech OR a loud RMS snap: a slow head-then-body turn to face the source. reachy-mini is a BASE dependency; the CLI checks daemon/robot liveness via the SDK, robust across restarts. HTTP stays as an optional remote profile.

## Why it matters

- Real loudness delivers the true 'stronger noise' reaction (a clap/loud sound snaps attention), not a persistence-proxy approximation; SDK-first removes the dep-free contortions and the fragile daemon-process babysitting; it follows reachy_nova's proven in-process approach.

## Requirements

- SDK default path: raw mic audio (media.get_audio_sample, AEC ch0) is RMS'd and a spike (>5x rolling avg, >floor, prev-quiet edge) escalates Tier-2 toward the current DoA direction.
  - honesty: Unit test on synthetic audio arrays: a quiet→loud spike (>5x rolling avg, >floor) yields a snap escalation toward DoA; steady ambient yields none.
- The producer commits/holds a direction only on speech_detected or a fresh snap and expires it after a hold timeout; a constant/latched angle with no speech and no snap commits no turn and recenters.
  - honesty: Unit test: a constant/latched angle with no speech and no snap commits zero turns and recenters after the hold; only a speech_detected transition or a snap commits.
- Tier-1: only the near-side antenna leans toward a faint off-axis sound (ANTENNA_KEY coalescing in the motion queue, far antenna neutral, head held).
  - honesty: tests/test_motion.py: a faint single-direction feed yields exactly a near-side-only antenna action (far ~neutral, head None); two ANTENNA_KEY actions coalesce independently of LOOK_KEY.
- Tier-2 head->body escalation re-centers the head via a slow body turn when the source persists off-axis, folding the antenna pose into the committing turn action (no stale backlog).
  - honesty: tests/test_motion.py: a persistent far-side feed produces a body-turn action with head re-centering and the antenna pose folded into the same action; no queued antenna action is left behind.
- listen defaults to the SDK transport + tiered behavior; HTTP remains selectable as a remote profile; every existing tuning flag still parses and new tier/RMS knobs parse.
  - honesty: tests/test_listen_cli.py: 'reachy listen run' with no flags selects the SDK transport + tiered producer; --transport http still works; every pre-existing flag and each new knob parses.
- Daemon/robot liveness is determined via the SDK and stays correct across a daemon restart (#21), reducing the hand-rolled PID/health-poll logic.
  - honesty: Unit test with a stubbed SDK: the liveness check reflects a simulated daemon restart correctly (down→up), without the old PID/health-poll path.
- reachy-mini is declared a base runtime dependency in pyproject; CLAUDE.md + README are updated to describe the SDK-first default (HTTP optional remote).
  - honesty: A test/grep confirms pyproject lists reachy-mini in base [project.dependencies], and CLAUDE.md + README describe the SDK-first default with HTTP as an optional remote profile.

## Honesty conditions

- On the live robot the two tiers are visibly distinct and loudness-driven: a faint tap → antenna lean only; a clap/loud sound → a snap turn toward it; speech → a slow head-then-body turn.
- The change is localized to motion/listen.py + the SDK transport + daemon-liveness; the observer is an owner running 'reachy listen'.
- Verifiable against today's code: listen is HTTP-first (DoA {angle,speech} only, head-yaw only) and reachy/daemon.py hand-manages the daemon process; reachy-mini is an optional extra, base deps [].
- Demonstrable: with the SDK default, a faint feed yields an antenna-only action and a loud/speech feed yields a head(/body) turn; pyproject base deps include reachy-mini.
- Observable: a real clap snaps attention (loudness-driven) where the proxy could not; daemon liveness stays correct across a restart without manual fiddling.
- Greppable: no camera/ML dependency added; the HTTP transport remains selectable (--transport http); RMS is a scalar energy, not classification.
- An on-robot/producer acceptance check: tap→antenna-only; clap→snap turn; talk→slow head-then-body turn; daemon restart→liveness reported correctly.

## Success signals

- On the live robot: a faint off-axis tap → antenna lean only; a clap/loud sound → a snap turn toward it; talking from the side → a slow head-then-body turn-to-see; and after a daemon restart the CLI reports liveness correctly without manual fiddling.

## Scope / boundaries

- Not vision: no camera/ML; 'turn to see' is orienting only (a future camera frames the source; /api/camera/specs already exists). Not a full behavior-engine rewrite. HTTP/remote profile is kept, not removed. RMS is a plain energy scalar, not audio classification.

## Assumptions

- Antenna 'near-side lean' sign/axis to be confirmed on hardware (the DoA convention gives the side).
- The installed reachy-mini SDK exposes media.audio.get_DoA(), media.get_audio_sample(), get_input_audio_samplerate()/channels() as reachy_nova uses them; exact shapes to verify against the installed version.

## Decisions

- SDK-first: reachy-mini becomes a BASE dependency and the SDK transport is listen's DEFAULT (real DoA + RMS in-process). HTTP stays an optional remote profile. CLAUDE.md's zero-base-dep rule + the README's dep-free pitch are relaxed/updated to the SDK-first profile.
- Tier-2 'stronger noise' trigger = speech_detected OR a real RMS snap — CITE reachy_nova detect_snap: rms=sqrt(mean(audio^2)), rolling energy deque, fire when rms>5x rolling_avg AND rms>~0.02 floor AND previous chunk quiet. No persistence proxy.
- Latched-DoA handling: (re)commit a direction ONLY on speech_detected or a fresh snap; expire a held direction after a hold timeout — cite reachy_nova speech-gated update_doa + 3s speaker_hold. A frozen angle must never read as 'sustained sound'.
- Tier-1 antenna gesture = only the NEAR-side antenna leans toward the sound (far antenna neutral).
- Tier-2 turn = escalating head->body: head yaw first; if the source persists off-axis, a slow body turn brings it toward front while the head re-centers.
- Fold issue #21 in (BOUNDED): use the SDK to determine robot/daemon liveness robustly across restart, replacing the fragile hand-rolled PID/health-poll dance — NOT a full daemon-management rewrite.
- Self-audio handled by the XMOS mic-array AEC channel 0 (reachy_nova preprocess_mic_audio aec_channel=0); no 'am I speaking?' gate needed in listen.
- Provenance: CITE (copy, don't import) reachy_nova/tracking.py detect_snap + update_doa; the DoA->yaw mapping (pi/2 - angle; 0=left) already matches reachy_nova.
