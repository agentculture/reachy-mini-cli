# reachy-mini-cli's sleep mode now wakes by touch when its ears are off: with audio-listening disabled the sleeping robot ignores all sound and only a pat rouses it; and audio wake-word runs on a light, pluggable backend — an external HTTP STT service (default) or on-box openwakeword ([cpu]), no heavy local model

> reachy-mini-cli's sleep mode now wakes by touch when its ears are off: with audio-listening disabled the sleeping robot ignores all sound and only a pat rouses it; and audio wake-word runs on a light, pluggable backend — an external HTTP STT service (default) or on-box openwakeword ([cpu]), no heavy local model

## Audience

- Operators running sleep mode on a Reachy Mini — especially quiet-room or audio-off deployments where the robot should rest and only respond to touch; secondary: anyone who pats the robot to wake it

## Before → After

- Before: Today sleep's wake is unconditional always-on audio: Tier-1 speech_detected + the in-process SnapDetector fire on any sound, so ambient room noise keeps it from resting; pat is NOT wired into the sleep loop (run_sleep_arc has an unused pat seam); and the only wake-word path is in-app openwakeword behind the empty [cpu]/[gpu] extras
- After: sleep run gets an explicit audio-wake toggle (--no-audio-wake / --wake pat): a pat ALWAYS wakes the robot, and the toggle governs the audio half. With audio off it is pat-only (ignores all sound); with audio on, speech/snap wake plus a pluggable wake-word backend

## Why it matters

- A robot that only stirs to touch when its ears are off reads as restful, not twitchy (on-robot test: ambient snap kept it awake); and keeping wake-word light — on-box openwakeword or an external HTTP STT service — avoids bundling heavy, externally-managed STT models on the robot

## Requirements

- In pat-only mode, the live sleep loop wires a pat source that measures commanded-vs-actual head-pose deviation against the MOVING sleep-breathe commanded pose (not a static baseline), reusing reachy/motion/pat.py PatDetector, and audio stimulus/wake is fully disabled
  - honesty: Unit test with an injected head-pose readback: a pat deviation measured against the MOVING sleep-breathe commanded pose triggers wake, while feeding speech/snap does not; the path reuses reachy/motion/pat.py PatDetector and works with a fake clock / no robot
- An explicit operator flag toggles audio wake (e.g. --no-audio-wake, or --wake pat); pat is always a wake source. The flag is the single switch that disables the audio stimulus + audio wake, leaving pat as the sole way to rouse the robot
  - honesty: Unit test: with the audio-wake flag off (--no-audio-wake/--wake pat) the robot wakes ONLY on a pat — feeding speech_detected + snap leaves it ASLEEP; with the flag at its default it still wakes on audio; asserted on the stimulus+wake wiring with an injected sense sequence (fake clock, no robot)
- Wake-word/speech detection is a pluggable backend with exactly two options: an always-available external HTTP STT override (local or remote, stdlib HTTP like Magpie TTS) as the default, and optional on-box openwakeword as the lightweight [cpu] path. NO heavy local STT model is bundled — larger models stay externally managed behind the HTTP service (the [gpu] extra is not used for an on-box model). A configured-but-unreachable/absent backend degrades cleanly to no-wake-word (Tier-1 speech/snap still wakes), never raises
  - honesty: Import-boundary test: the external HTTP STT path imports no openwakeword; openwakeword is imported only under its [cpu] extra; no on-box STT-model dependency is introduced anywhere; a configured-but-unreachable backend returns no-wake-word and never raises (Tier-1 speech/snap still wakes)

## Honesty conditions

- End-to-end: with audio-wake off the sleeping robot ignores a burst of sound and only a pat wakes it; with audio-wake on, a wake-word arrives via the selected backend (external HTTP STT by default); both demonstrable headless (injected sense + fake clock) and on the robot
- An operator can select pat-only via the documented flag, and the sleep noun's overview/help names the quiet-room / audio-off deployment as the intended use — verifiable by running the help/overview
- Code inspection confirms today's run_sleep_arc pat seam is never fed by cmd_sleep_run and Tier-1 audio wake (speech+snap) is unconditional — so the 'always audio-awakeable, pat unwired' before-state is literally true
- A headless 'pat-only' run (injected sense + fake clock, no robot) stays ASLEEP through a burst of speech_detected + snap transients and wakes on a single synthesized pat deviation — observable in --json
- Unit test: --no-audio-wake makes the loop pat-only (speech+snap ignored, a pat wakes) while the default keeps audio wake — the toggle's effect is observable on the run wiring with an injected sense (fake clock, no robot)
- Code inspection: the change touches only the wake/stimulus wiring, the pat source, and a wake-word backend adapter — the SleepStateMachine, the SleepProducer sleep posture, and the sleep_active.flag yield are unchanged (no diff to those modules' behaviour)
- Dependency/import-boundary check: no heavy on-box STT model dependency is introduced; wake-word resolves to either openwakeword (under [cpu]) or the external HTTP STT path, nothing else

## Success signals

- With pat-only wake, a headless test feeding speech_detected + snap transients never wakes it (stays ASLEEP) while a synthesized pat deviation does wake it; with STT wake, the wake path calls the external STT service and importing the sleep/wake module pulls in no openwakeword

## Scope / boundaries

- NOT a change to the decay->drowsy->asleep machine, the sleep posture, or the sleep_active.flag yield; pat reuses the pat noun's PatDetector (no new sensor); does NOT build a new STT engine — wake-word backends are pluggable adapters over existing options
