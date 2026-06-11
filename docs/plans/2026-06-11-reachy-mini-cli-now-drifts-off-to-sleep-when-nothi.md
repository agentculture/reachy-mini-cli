# Build Plan — reachy-mini-cli now drifts off to sleep when nothing has engaged it for a while — its liveness fades stage by stage until it goes still — and wakes when you speak to it or say its wake word

slug: `reachy-mini-cli-now-drifts-off-to-sleep-when-nothi` · status: `exported` · from frame: `reachy-mini-cli-now-drifts-off-to-sleep-when-nothi`

> reachy-mini-cli now drifts off to sleep when nothing has engaged it for a while — its liveness fades stage by stage until it goes still — and wakes when you speak to it or say its wake word

## Tasks

### t1 — Create reachy/motion/sleep_signal.py — cross-noun asleep flag, mirroring pat_signal/cognition_signal byte-for-byte (write/clear/contextmanager asleep()/is_active under state_dir(), flag name sleep_active.flag)

- covers: c11, h11
- acceptance:
  - sleep_signal exposes sleep_flag_path()/write()/clear()/asleep() contextmanager/is_active(), structurally identical to pat_signal.py (same function set + state_dir() location)
  - Unit test: asleep() writes sleep_active.flag on enter and removes it on exit including on exception; is_active() is a pure Path.exists() check

### t2 — Create reachy/sleep/ package (__init__.py) + reachy/sleep/state.py — pure SleepStateMachine: SleepState enum (ALERT/DROWSY/ASLEEP), a monotonic idle clock with now= injection, tunable alert->drowsy and drowsy->asleep thresholds, reset-on-stimulation, and an idle_seconds/state snapshot

- covers: c8, h8, c4, h4
- acceptance:
  - Unit test (fake clock): with no stimulation the machine steps ALERT->DROWSY->ASLEEP at its configured thresholds; a qualifying-event reset returns it to ALERT
  - Unit test: any reset zeroes the monotonic idle clock; thresholds are constructor/CLI-overridable so tiny timeouts fire deterministically; defaults approximate ~1-2 min drowsy feel
  - state.py is pure (no robot/transport/threads): clock is injected via now=, fully testable headless

### t3 — Create reachy/sleep/stimulus.py — qualifying-stimulation classifier: is_stimulus(sense, snap, sound_present, now, mute_until) returns True for a DoA shift / speech_detected / SnapDetector transient / pat deviation, and False for a self-voiced sample captured inside think's self-mute window

- covers: c9, h9
- acceptance:
  - Unit test: a DoA-change / speech_detected / snap / pat event each classify as stimulus (timer would reset)
  - Unit test: a sample whose timestamp falls inside the self-mute window (now < mute_until) does NOT classify as stimulus (robot cannot keep itself awake by speaking)
  - stimulus.py is pure stdlib+numpy, no transport import

### t4 — Create reachy/sleep/wake.py + declare [cpu]/[gpu] extras in pyproject.toml — two-tier wake: Tier-1 always-on speech/snap (zero new base dep, reuses SnapDetector/speech_detected); Tier-2 optional wake-word phrase loaded lazily ONLY when a [cpu] (openWakeWord) or [gpu] (STT/ASR) extra is installed AND wake-word enabled, degrading cleanly to Tier-1 on absent/failed model

- covers: c15, h17
- acceptance:
  - Unit test: with wake-word disabled or no compute-class extra installed, wake() still fires on speech/snap and never raises
  - Unit test (import boundary): the wake-word engine module is imported only inside the enabled+installed path; a base import of reachy.sleep.wake pulls in no [cpu]/[gpu] dependency
  - pyproject.toml declares generic [cpu] and [gpu] optional-dependency extras (deliberately general for future speech/STT), and a missing engine raises no error

### t5 — Create reachy/sleep/supervisor.py — background-process supervisor for the sleep noun (build_run_command/start/stop/restart/status), mirroring reachy/motion/supervisor.py but with its OWN sleep.pid + sleep.log under state_dir(), distinct from listen/think processes

- acceptance:
  - start() spawns 'python -m reachy sleep run ...' detached, records sleep.pid + sleep.log under state_dir(); stop() SIGTERM-then-SIGKILL with PID-reuse guard; status() reports process + daemon health
  - Unit test: PID/log paths are sleep-specific and do not collide with listen.pid/think.pid

### t6 — Create reachy/motion/sleep.py — SleepProducer mapping SleepState to motion: DROWSY enqueues progressively lower-energy AliveConfig moves (energy scales toward ~0.2); ASLEEP enqueues a near-still 'sleep breathe' (slow body rock + gentle antenna breathing, citing reachy_nova SLEEP_ROCK/antenna-breath); a wake transition emits one distinct re-engagement gesture; all onto the shared serial MotionQueue, motion errors degrade silently

- depends on: t2
- covers: c10, h10, c5, h5
- acceptance:
  - Unit test: DROWSY enqueues lower-energy AliveConfig moves than ALERT and ASLEEP enqueues the near-still sleep-breathe, all via MotionQueue coalesce keys; an injected transport error during a move does not kill the loop
  - Unit test: the ASLEEP sleep-breathe pose is assertably distinct (different target axes/amplitudes) from the alive/focused idle pose, and the wake transition emits a distinct re-engagement gesture — checkable on produced MotionActions, no robot

### t7 — Wire reachy/motion/listen.py idle layer to yield to sleep: when sleep_signal.is_active() the idle producer returns no wander move (full suppression) as the STRONGEST interrupt, ranked above pat_signal and think-focused; document the before/after (idle previously held constant energy with no rest)

- depends on: t1
- covers: c2, h2, c3, h3
- acceptance:
  - Unit test: with sleep_active.flag set the listen idle producer returns None (goes still); precedence test confirms sleep outranks pat and think-focused
  - Unit test/inspection: before this change the idle path had no decay/rest state (constant energy) — asserted by the precedence/branch ordering and a regression note, so both demo and real sessions now defer to sleep

### t8 — Create reachy/cli/_commands/sleep.py + register it in reachy/cli/__init__._build_parser + add explain/catalog.py ENTRIES — first-class 'sleep' noun: run (foreground decay->sleep->wake loop, bounded --ticks + injected clock seam + --idle-timeout), start/stop/restart/status (via sleep supervisor), demo (no robot, injected sense+fake clock), overview; add_robot_args + get_transport injection; status/demo emit --json; CliError contract; writes sleep_active.flag while asleep

- depends on: t1, t2, t3, t4, t5, t6
- covers: c1, h14, c7, h15
- acceptance:
  - Headless 'sleep demo' (injected sense + fake clock, no robot) walks ALERT->DROWSY->ASLEEP then back to ALERT on a wake event, observable in --json output (covers c1/h14)
  - 'sleep status --json' reports the current state + idle seconds, and an arc unit test drives full ALERT->DROWSY->ASLEEP->wake using a fake clock + injected sense with zero robot and zero real wall-clock wait (covers c7/h15)
  - The noun follows the listen|think|pat scaffold (run/start/stop/restart/status/demo/overview, every verb --json, CliError two-line error contract); 'agentfront/teken cli doctor . --strict' stays green

### t9 — Add tests/test_sleep_boundary.py — boundary/import-guard suite proving the spec's NOT-claims: implementation adds only timer+producer+flag (no affect/emotion model, no daemon-suspend/motor-disable/OS call) and introduces NO new BASE runtime dependency beyond numpy

- depends on: t4, t8
- covers: c6, h6
- acceptance:
  - Test: importing the base sleep path pulls in no new third-party base dependency beyond numpy; no daemon-suspend / motor-disable / OS-power call appears in the sleep modules
  - Test: no affect/emotion model is introduced — 'boredom' is only the attention-decay timer

### t10 — Document the sleep noun in README.md + CLAUDE.md (architecture bullet like the listen/think/pat entries: decay state machine, sleep_active.flag yield precedence, two-tier wake + [cpu]/[gpu] extras, SDK-first transport)

- depends on: t8
- acceptance:
  - README quickstart lists 'reachy sleep' verbs; CLAUDE.md gains a 'sleep noun' architecture bullet consistent with the pat/think bullets and the import/dep constraints
  - markdownlint-cli2 passes on the edited docs

## Risks

- [unknown_nonblocking] Exact package pins + contents of the [cpu]/[gpu] extras (openwakeword vs an STT/ASR model) and the default wake phrase/model env vars are unpinned — settle at build time within t4; the plan fixes the two-tier + generic-extra shape, not the dependency set (task t4)
- [follow_up] Wake-word accuracy and the sleep-posture amplitudes (rock/antenna-breath) can only be tuned/verified ON the robot — like the listen antenna-sign verify, headless tests prove the state machine + motion plumbing but not the on-hardware feel (task t6)
- [unknown_nonblocking] Compute-class detection (which of [cpu]/[gpu] is active) is left to install profile, not auto-detected at runtime in v1 — Pi vs Jetson selection is an install/env decision (task t4)
