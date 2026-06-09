# Reachy Mini reacts to what it sees: a lightweight on-board vision sense detects motion, light, and nearby objects from the camera in realtime on a Raspberry Pi 4 — simple pixel math, no ML — and feeds those events into the behavior/motion system so the robot orients toward what it sees.

> Reachy Mini reacts to what it sees: a lightweight on-board vision sense detects motion, light, and nearby objects from the camera in realtime on a Raspberry Pi 4 — simple pixel math, no ML — and feeds those events into the behavior/motion system so the robot orients toward what it sees.

## Audience

- Reachy Mini operators running the on-board CLI, plus the behavior/listen subsystems that consume sense events to drive motion.

## Before → After

- Before: Today the CLI is sightless — it hears (listen/DoA) and moves, but the on-board camera is unused; perception is audio-only.
- After: The robot has a realtime visual sense: it detects motion and brightness/light changes from the local camera and emits events that orient the head/body toward what it sees. Object proximity is a parked stretch follow-up.

## Why it matters

- Visual reactivity makes the robot feel present and alive, complements audio DoA, and enables gaze-following — without heavy compute, ML, or a GPU.

## Requirements

- Detect motion via frame differencing on downsampled grayscale frames (numpy abs-diff + threshold + blob centroid for direction).
  - honesty: Frame differencing on downsampled grayscale reliably flags real motion (a hand wave at ~1-2 m) while rejecting sensor noise and lighting flicker via a tunable threshold.
- Detect light via per-frame brightness and bright-region centroid (mean luma + thresholded max region), reacting to changes rather than absolute level.
  - honesty: A brightness/centroid pass locates the dominant bright region and its direction, distinguishing a real light change from a uniform exposure shift.
- Expose a 'vision' noun (run/start/stop/restart/status + overview, --json) that mirrors 'listen': its own loop drives the head/body through the existing serial motion queue. Behavior-engine Sense-channel integration is a follow-up.
  - honesty: The vision loop drives the same serial motion queue as listen, so visual orients are serialized with (do not fight) listen and demo-mode moves.

## Honesty conditions

- On a Raspberry Pi 4 the full capture->downsample->detect->emit loop sustains >=10 FPS with CPU headroom left for the motion loop.
- The on-board operator and the listen/motion subsystems are the real consumers — vision events matter to a robot reacting in place, not to a remote dashboard.
- The camera is genuinely unused by the current CLI (no existing vision verb), so this is net-new perception, not a duplicate of something present.
- A robot that visibly reacts to motion/light reads as more present than one reacting only to sound — the added liveliness is perceptible to a bystander.
- Motion and light can be detected usefully with pixel math alone, so dropping ML/GPU does not gut the feature.
- The local SDK/IPC camera path is present whenever the [sdk]/[daemon] extra is installed, so 'local-profile only' still covers the normal on-robot deployment.
- On a Pi 4 the loop holds the target frame rate and a motion/light event yields a visibly smooth head orient within a fraction of a second — measurable on hardware.
- Once shipped, waving a hand or moving a light in view turns the head toward it, with no ML model loaded and CPU to spare.

## Success signals

- Runs realtime on a Raspberry Pi 4 (target ~10-15 FPS) within a modest CPU budget on downsampled frames, and a detected motion/light event drives a smooth head orient through the existing serial motion queue (like listen).

## Scope / boundaries

- Not ML: no object recognition, face ID, classification, tracking-by-detection, SLAM or depth mapping; no neural models, no cloud, no GPU. Rudimentary pixel math only.
- Local-profile only: frames come from the SDK/IPC local camera path; the pure-HTTP remote profile (camera metadata only, per issue #22) cannot run detection.

## Assumptions

- The reachy_mini SDK local camera path (is_local_camera_available) delivers frames fast enough on the Pi 4 to downsample and diff in realtime.

## Decisions

- Build on issue #22's frame-grab substrate (vision specs/status/snapshot via the local SDK/IPC camera path); this feature is the perception+reaction layer (#22's follow-up).
- v1 scope = motion + light via a standalone 'vision' loop mirroring listen (serial motion queue); proximity and behavior-engine Sense-channel integration are explicit follow-ups.
