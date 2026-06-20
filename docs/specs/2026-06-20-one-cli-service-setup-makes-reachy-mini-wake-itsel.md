# One CLI 'service' setup makes Reachy Mini wake itself after every machine reset: enable a single chosen presence mode (demo idle motion, or live senses) as a boot-persistent, auto-restarting systemd --user service, mutually exclusive so the two modes never fight over the single-SDK-owned robot.

> One CLI 'service' setup makes Reachy Mini wake itself after every machine reset: enable a single chosen presence mode (demo idle motion, or live senses) as a boot-persistent, auto-restarting systemd --user service, mutually exclusive so the two modes never fight over the single-SDK-owned robot.

## Audience

- the operator deploying Reachy Mini on a box who wants the robot to resume its chosen presence automatically after a reboot, without re-running commands or hand-editing systemd units

## Before → After

- Before: today demo-mode has CLI install/enable/disable systemd verbs but live mode (listen) is a hand-authored unit with no CLI lifecycle; both units can be enabled at once and would contend for the single-SDK-owned robot, and the daemon they depend on has no boot service at all — so the current setup does not actually survive a reboot
- After: a single CLI service surface enables exactly one presence mode (demo idle, or live senses) as a boot-persistent auto-restarting systemd --user service; switching modes is one command and the modes are mutually exclusive, so after every machine reset the robot comes back in the chosen mode and only that mode owns the robot

## Why it matters

- the robot should wake itself the same way every time the machine resets, with no operator babysitting and no risk of two modes fighting over the one SDK client and one head

## Requirements

- live mode = listen + think + vision + sleep folded into ONE sense loop that shares the single SDK client and one serial MotionQueue (extending the #43 PatHook on_tick seam), arbitrated by the existing priority flags (sleep > pat > think) — NOT four separate services, which would each open the single-consumer media session and throttle to ~1 Hz
  - honesty: all four sense behaviors run concurrently in one process with no 1 Hz throttle; the single-consumer media_session is opened exactly once

## Honesty conditions

- the setup is one CLI surface that enables a single boot-persistent, auto-restarting service for the chosen mode, and a reboot demonstrably resumes that mode
- the operator never writes or edits a systemd unit by hand, nor re-runs a start command, after a reboot
- exactly one presence mode is enabled at a time; switching modes is one CLI command; the enabled mode auto-starts on boot and auto-restarts on failure
- verified today: demo-mode has install/enable verbs, listen's unit is hand-authored, both can be enabled at once, and there is no daemon boot service
- two sense modes never own the SDK client or the head simultaneously
- the feature only touches systemd --user and does not remove or change the per-noun run/start/stop dev verbs
- a reboot (or re-login) test shows exactly one mode active, correct status output, and a clean demo<->live switch

## Success signals

- after 'service enable live' and a reboot, exactly one presence service is active and the other is guaranteed inactive; 'service status' reports the enabled mode plus daemon health; switching to demo and rebooting flips cleanly with zero contention and zero hand-edited units

## Scope / boundaries

- not a general process supervisor, multi-robot orchestrator, or cross-platform service manager — systemd --user only; it does not replace the per-noun foreground/background run/start/stop verbs used for dev, and it never runs demo and live at the same time

## Decisions

- live mode is implemented as ONE folded sense loop: think, vision, and sleep are folded into listen's single loop via the on_tick seam (the #43 PatHook pattern), sharing one SDK client and one MotionQueue, arbitrated by the existing sleep>pat>think priority flags (resolves v1)
- the setup is a NEW unified 'reachy service' noun: service enable demo|live / disable / status / install / uninstall, with mutual-exclusion (enabling one disables the other) and the daemon boot-dependency handled in one place (resolves v2)
- on this box, 'live' is the chosen boot mode (the live folded sense loop auto-starts on every reset)

## Open / follow-up

- RESOLVED (→ c9): live mode = ONE folded sense loop (chosen) over four separate ~1 Hz-contending services. Decision recorded as confirmed claim c9.
- RESOLVED (→ c10): a new unified 'reachy service' noun (chosen) over extending per-noun verbs. Decision recorded as confirmed claim c10.
- daemon boot-persistence: the daemon has NO systemd unit today, yet sdk-transport modes need it at localhost:8000 — the setup must also install/enable a daemon service, or the presence unit must Requires=/After= it
