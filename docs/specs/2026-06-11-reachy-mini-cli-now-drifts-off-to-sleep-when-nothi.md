# reachy-mini-cli now drifts off to sleep when nothing has engaged it for a while — its liveness fades stage by stage until it goes still — and wakes when you speak to it or say its wake word

> reachy-mini-cli now drifts off to sleep when nothing has engaged it for a while — its liveness fades stage by stage until it goes still — and wakes when you speak to it or say its wake word

## Audience

- Operators running reachy-mini-cli on a Reachy Mini — both in demo mode (the always-alive idle loop) and real mode (a running listen/think session). Secondary: anyone watching the robot, for whom 'asleep' should read as unmistakably asleep.

## Before → After

- Before: Today the robot is always equally alive: the demo/idle loop wanders at constant energy forever and listen/think hold full liveness no matter how long nothing happens. It never rests, never visibly winds down — stillness is only ever the 'focused' think posture, never genuine rest.
- After: When nothing has engaged the robot for a while, its liveness fades in graduated stages (alert -> drowsy -> asleep) rather than flipping off, until it settles into a quiet sleep posture (slow rocking + gentle antenna breathing). Speaking to it — or saying its wake word — brings it back up through a wake gesture to full liveness.

## Why it matters

- A robot that is always identically alive reads as a machine on a loop; one that tires when ignored and stirs when addressed reads as a creature. Sleep makes idleness legible (you can SEE it lost interest) and gives a natural, affectionate re-engagement beat — and as a bonus it quiets motor noise and wear during long unattended stretches.

## Requirements

- Boredom is an attention-decay timer: each tick with no qualifying stimulation advances a monotonic idle clock; any qualifying sense event resets it to zero. Crossing tunable thresholds steps the state alert->drowsy->asleep. Defaults roughly mirror reachy_nova's feel (order ~1-2 min to drowsy, a bit more to asleep) but are CLI-tunable; tiny --idle-timeout values make it test-fast.
  - honesty: Unit test: any qualifying event resets the monotonic idle clock to zero; with a tiny --idle-timeout the thresholds fire deterministically under a fake clock; defaults are CLI-overridable.
- Qualifying stimulation = the sense events the repo already produces: a DoA shift / speech_detected from the daemon, a SnapDetector loud transient, or a pat deviation. The robot's OWN tts/think output must NOT count (reuse think's self-mute window) so it never keeps itself awake by speaking.
  - honesty: Unit test: a DoA/speech/snap/pat event resets the timer, while a self-voiced audio sample captured inside think's self-mute window does NOT reset it (the robot cannot keep itself awake by speaking).
- The sleep posture is produced the same way every other motion is: a low-energy producer enqueues calm moves onto the shared serial MotionQueue, drained by the standard _MotionExecutor/motion.server.run background thread. Drowsy = AliveConfig with progressively scaled-down energy; asleep = a dedicated near-still 'sleep breathe' (slow body rock + gentle antenna breathing), mirroring reachy_nova's SLEEP_ROCK/antenna-breath but expressed through the existing idle pose vocabulary. Motion errors degrade silently.
  - honesty: Unit test: drowsy enqueues progressively lower-energy AliveConfig moves and asleep enqueues the near-still sleep-breathe, all onto the shared MotionQueue drained by the standard executor; an injected transport error during a move does not kill the loop (degrades silently).
- A cross-noun 'asleep' signal flag (sleep_signal.py writing sleep_active.flag under state_dir(), exactly mirroring cognition_signal/pat_signal) lets the listen/think idle layers see sleep is in effect and yield to it — the strongest idle interrupt, above pat and think-focused. While asleep the normal idle wander is fully suppressed; the sleep producer owns motion.
  - honesty: sleep_signal.py mirrors pat_signal/cognition_signal byte-for-byte in shape (write/clear/contextmanager/is_active under state_dir()); a unit test shows the listen idle producer treats sleep_active.flag as the strongest interrupt — yielding above pat and think-focused.
- Wake is two-tier; the heavy tier rides on GENERIC compute-class extras, not wake-specific ones. Tier-1 default (zero new base dep): speech_detected / SnapDetector — every install wakes on voice/sound. Tier-2 optional wake-WORD phrase ('hey reachy') is provided by whichever compute-class extra is installed: [cpu] bundles CPU-appropriate processing (e.g. openWakeWord), [gpu] bundles GPU-appropriate processing (e.g. an STT/ASR model, citing reachy_nova's wake_word.py). These [cpu]/[gpu] extras are deliberately general so they also serve FUTURE compute-tiered features (e.g. on-box speech/STT), not just wake. The wake-word engine loads only when a compute-class extra is installed AND wake-word is enabled; an absent/failed model degrades cleanly to Tier-1, never an error.
  - honesty: Unit test: with the wake-word tier disabled or no compute-class extra installed, a plain install still wakes on speech/snap and never raises; the wake-word engine is imported only when a [cpu] or [gpu] extra is present AND wake-word is enabled (import-boundary asserted), so the base install stays dependency-light.

## Honesty conditions

- A headless 'sleep demo' run (injected sense + fake clock, no robot) demonstrably walks alert->drowsy->asleep and then back to alert on a wake event, observable in its --json output.
- A unit test proves the existing idle layer yields when sleep is in effect: with sleep_active.flag set, the listen/idle producer goes still (returns no wander move), so both demo and real sessions defer to sleep.
- Code inspection confirms today's idle/listen path has no idle-decay timer and no sleep/asleep state or flag — liveness energy is constant — so the 'never rests' before-state is literally true, not rhetorical.
- With an injected clock and injected sense, a unit test drives the state machine through alert->drowsy->asleep on elapsed idle time and back to alert on a qualifying event, asserting each transition at its threshold.
- The asleep sleep-breathe pose is assertably distinct from the alive/focused idle pose (different target axes/amplitudes), and a wake transition emits a distinct re-engagement gesture — both checkable on the produced MotionActions without a robot.
- The implementation adds only a timer + low-energy producer + a flag file: no affect/emotion model, no daemon-suspend/motor-disable/OS call, and no new BASE runtime dependency beyond the existing numpy — verifiable by inspection and the import-boundary tests.
- Every state transition is reportable via the sleep-status --json verb and the full alert->drowsy->asleep->wake arc passes in unit tests using a fake clock + injected sense, with zero robot and zero real wall-clock wait.
- The strict agent-first rubric gate (agentfront/teken cli doctor --strict) stays green with the new noun: sleep exposes run/start/stop/restart/status/demo/overview, every verb takes --json and uses the CliError contract, and run honors a bounded --ticks plus an injected clock seam.

## Success signals

- With a short test timeout, after no stimulation the robot demonstrably transitions alert -> drowsy -> asleep, writes an 'asleep' state flag, and drops to the sleep posture; a speech/snap event (or the wake word) clears the flag and returns it to full liveness within ~1-2 ticks. All transitions are observable headless via --json status and unit-testable with injected sense + a clock seam (no robot, no real timeout).

## Scope / boundaries

- NOT an emotion/affect engine: reachy-mini-cli has no joy/anger model like reachy_nova, so 'boredom' here is a pure attention-decay timer driven by absence of sense stimulation, not derived affect. NOT power management / OS suspend / motor-disable — 'asleep' is a low-energy MOTION posture, the daemon and process stay up. NOT a new always-on background service beyond the loops that already exist.

## Decisions

- Surface = a new first-class 'sleep' noun (run foreground / start|stop|restart|status background / demo no-robot / overview), structured exactly like listen|think|pat: add_robot_args, get_transport injection, a bounded --ticks seam and an injected clock seam for tests, supervised via its own PID+log under state_dir(). It is the single owner of the decay->sleep->wake state machine; listen/think/idle merely YIELD to its sleep_active.flag. No --enable-sleep flag baked into other loops in v1.
