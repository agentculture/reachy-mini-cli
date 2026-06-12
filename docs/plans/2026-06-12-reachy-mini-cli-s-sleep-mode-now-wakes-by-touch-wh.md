# Build Plan — reachy-mini-cli's sleep mode now wakes by touch when its ears are off: with audio-listening disabled the sleeping robot ignores all sound and only a pat rouses it; and when listening is on, wake-word/speech rides the same external STT service the robot already uses for hearing — no in-app openwakeword

slug: `reachy-mini-cli-s-sleep-mode-now-wakes-by-touch-wh` · status: `exported` · from frame: `reachy-mini-cli-s-sleep-mode-now-wakes-by-touch-wh`

> reachy-mini-cli's sleep mode now wakes by touch when its ears are off: with audio-listening disabled the sleeping robot ignores all sound and only a pat rouses it; and when listening is on, wake-word/speech rides the same external STT service the robot already uses for hearing — no in-app openwakeword

## Tasks

### t1 — Gate audio stimulus in is_stimulus behind an audio_wake flag

- covers: c14, h7
- acceptance:
  - is_stimulus(..., audio_wake=False) ignores doa_shift/speech/snap and resets/wakes only on pat; audio_wake=True preserves today's behavior
  - unit tests in test_sleep_stimulus.py cover both flag states with an injected sense

### t2 — Pluggable wake-word backend module: external HTTP STT (default) + openwakeword [cpu], no local model

- covers: c17, h10, c16, h14
- acceptance:
  - reachy/sleep/wakeword.py exposes resolve_backend() returning a backend with update()->bool; external HTTP STT uses stdlib urllib; an unreachable/absent backend returns False and never raises
  - import-boundary test: importing the wake path imports no openwakeword (openwakeword only under [cpu]); no on-box STT-model dependency is introduced

### t3 — Sleep pat-wake source: head_pose deviation vs the moving sleep-breathe commanded pose

- covers: c9, h2
- acceptance:
  - reachy/sleep/patwake.py provides a pat source that feeds reachy/motion/pat.py PatDetector from (head_pose readback vs current commanded sleep pose); a deviation vs the MOVING pose triggers detection while zero deviation does not
  - tested with a fake head_pose readback + injected commanded pose, fake clock, no robot

### t4 — Wire the audio-wake toggle + pat source + wake-word backend into sleep run

- depends on: t1, t2, t3
- covers: c1, c2, h11, c11, h12, c14, h7, c3, h4
- acceptance:
  - sleep run gains --no-audio-wake (and --wake pat alias); with it set the loop is pat-only (speech+snap ignored, a pat wakes), default keeps audio wake; observable on the run wiring with an injected sense (fake clock, no robot)
  - run_sleep_arc threads audio_wake into is_stimulus, skips the audio wake path when off, wires the t3 pat source + the t2 resolved wake-word backend; sleep overview/help name the quiet-room / audio-off deployment

### t5 — Thread the audio-wake flag through the sleep supervisor

- depends on: t4
- covers: c14
- acceptance:
  - supervisor.build_run_command forwards --no-audio-wake when set; test_sleep_supervisor.py asserts the spawned argv carries the flag

### t6 — Headless pat-only end-to-end + boundary-unchanged tests

- depends on: t4
- covers: c7, h5, h9, c13, h13
- acceptance:
  - a new test (test_sleep_patonly_e2e.py) drives a pat-only run that stays ASLEEP through a burst of speech_detected+snap and wakes on a single synthesized pat, observable in --json
  - an inspection test asserts the SleepStateMachine, the SleepProducer sleep posture, and the sleep_active.flag yield are unchanged by the feature

### t7 — Docs, extras, and version bump for pat-wake

- depends on: t4
- covers: c16
- acceptance:
  - README + CLAUDE.md document --no-audio-wake / pat-only + the pluggable wake-word backend; pyproject extras adjusted so [gpu] no longer implies an on-box STT model
  - version bumped (minor) with a CHANGELOG entry and uv.lock regenerated per the version-check gate

## Risks

- [unknown_nonblocking] Exact external HTTP STT service interface (endpoint, env-var contract, wake-word vs full-transcript API) — resolve at build time against the model-gear service (task t2)
- [follow_up] Pat-vs-sleep-motion sensitivity: detecting a pat against the moving sleep-breathe pose needs on-robot threshold tuning (mirrors #35 r2 wake-tuning) (task t3)
