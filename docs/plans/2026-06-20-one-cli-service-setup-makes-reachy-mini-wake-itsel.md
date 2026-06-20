# Build Plan — One CLI 'service' setup makes Reachy Mini wake itself after every machine reset: enable a single chosen presence mode (demo idle motion, or live senses) as a boot-persistent, auto-restarting systemd --user service, mutually exclusive so the two modes never fight over the single-SDK-owned robot.

slug: `one-cli-service-setup-makes-reachy-mini-wake-itsel` · status: `exported` · from frame: `one-cli-service-setup-makes-reachy-mini-wake-itsel`

> One CLI 'service' setup makes Reachy Mini wake itself after every machine reset: enable a single chosen presence mode (demo idle motion, or live senses) as a boot-persistent, auto-restarting systemd --user service, mutually exclusive so the two modes never fight over the single-SDK-owned robot.

## Tasks

### t1 — Generate systemd --user unit text for daemon, demo, and live presence (reachy/service/units.py)

- covers: c2, h2, c3
- acceptance:
  - rendered unit matches the reachy-listen.service shape: ExecStart uses the venv python -m reachy ..., Restart=on-failure, RestartSec=5, After=network-online.target, WantedBy=default.target
  - the live presence unit ExecStart runs the folded live loop (listen run --live); the daemon unit runs the reachy-mini-daemon binary; both presence units declare Requires= and After= the daemon unit
  - pure functions with no systemctl side effects; unit tests assert the rendered text field-by-field

### t2 — Composite on_tick HookChain so multiple per-tick sense hooks coexist in listen's loop (reachy/motion/listen_hooks.py)

- covers: c8, h8
- acceptance:
  - HookChain runs N hooks per tick in priority order with the on_tick signature (transport, queue, t, commanded_head); an exception in one hook is swallowed and the remaining hooks still run
  - an empty chain is a no-op; close() fans out to every hook's close(); the existing single PatHook still works as a one-element chain (regression test)

### t3 — Fold think's cognition trigger into a per-tick hook reusing the loop's shared sense sample (reachy/motion/listen_think.py)

- depends on: t2
- covers: c8, h8
- acceptance:
  - ThinkHook consumes the loop's shared DoA/RMS/speech sample (NO second media_session) and drives the cognition engine; raises and clears think_active.flag
  - mirrors PatHook structure (on_tick signature, silent degradation, injected clock); unit-tested with a fake cognition engine + sense sample

### t4 — Fold vision's motion/light detection into a per-tick hook reading the camera through the shared SDK client (reachy/motion/listen_vision.py)

- depends on: t2
- covers: c8, h8
- acceptance:
  - VisionHook reads a frame via the shared client get_frame() (NO new media session) and runs the motion/light detector, enqueueing reactions on the shared MotionQueue
  - degrades silently when no frame is available (guards the #28 live-frame hang); unit-tested with an injected fake frame source + detector

### t5 — Fold sleep's decay/wake state machine into a per-tick hook against the shared sample + commanded pose (reachy/motion/listen_sleep.py)

- depends on: t2
- covers: c8, h8, h5
- acceptance:
  - idle decay transitions ALERT->DROWSY->ASLEEP and raises sleep_active.flag (strongest idle interrupt); a wake stimulus (speech/snap/pat) clears it
  - uses the loop's shared sense sample (NO new media session) and an injected clock; unit-tested walking the state machine deterministically

### t6 — Compose pat+think+vision+sleep into listen's loop behind a --live flag = the live folded sense loop (reachy/cli/_commands/listen.py)

- depends on: t2, t3, t4, t5
- covers: c8, h8, c5, h5
- acceptance:
  - listen run --live builds a HookChain of all four hooks arbitrated by sleep>pat>think; exactly one media_session is opened for the whole loop
  - a bounded listen run --live --ticks N drives all four hooks (assert each invoked); default listen run (no --live) is behaviorally unchanged (regression test)

### t7 — Service manager: enable/disable/status with mutual exclusion + daemon dependency over systemd --user (reachy/service/manager.py)

- depends on: t1
- covers: c3, h3, h5, c5
- acceptance:
  - enable('live') enables the daemon unit + live presence unit and DISABLES the demo presence unit; enable('demo') flips it; the invariant 'at most one presence unit enabled' always holds
  - status() reports the single enabled mode + daemon health; disable() stops the presence unit; all systemctl calls go through an injected runner so tests assert the exact command sequence with no real systemd

### t8 — New 'reachy service' noun: overview/enable/disable/status/install/uninstall wired to the manager (reachy/cli/_commands/service.py + register in reachy/cli/__init__.py)

- depends on: t1, t7
- covers: c1, h1, c2, h2, c6, h6
- acceptance:
  - reachy service overview describes the noun; enable demo|live / disable / status / install / uninstall dispatch to the manager; every verb supports --json with the results->stdout / errors->stderr split
  - nested parse errors keep the structured CliError contract (parser_class=type(p)); a missing systemctl raises a clean exit-2 CliError; the per-noun run/start/stop verbs are untouched

### t9 — Document the service noun (explain catalog, README + CLAUDE.md noun catalog, operating guide, live=folded-loop before/after) + bump version & CHANGELOG

- depends on: t8
- covers: c4, h4, c6, h6
- acceptance:
  - reachy/explain/catalog.py gains a service ENTRIES key and test_every_catalog_path_resolves passes; README + CLAUDE.md noun catalog list 'service'; operating guide documents enable/disable/status and the before->after (hand-authored listen unit retired)
  - pyproject version bumped with a CHANGELOG entry; markdownlint-cli2 clean on changed md

### t10 — End-to-end verification of the success signal: exactly one presence active, daemon-first ordering, clean demo<->live switch (simulated re-login)

- depends on: t6, t8
- covers: c7, h7, h1
- acceptance:
  - an integration test enables live -> asserts live + daemon enabled and demo disabled; switches to demo -> asserts the flip; asserts the daemon unit is ordered before the presence unit (Requires/After)
  - h7's reboot is simulated via a systemctl --user re-evaluation (true reboot noted as a manual on-box check); the test fails if two presence units are ever co-enabled

## Risks

- [unknown_nonblocking] Folding think/vision/sleep into one loop without a second media_session is the highest-uncertainty work: each sense engine currently assumes it owns a media session, so the hooks must consume the loop's shared per-tick sample instead. The #43 PatHook precedent proves the on_tick seam works, but pat only reads head_pose — audio/camera senses are heavier.
- [unknown_nonblocking] vision's live SDK frame path had a hang (#28): VisionHook must confirm in-loop get_frame() works or degrade silently — do not let a camera stall block the sense loop. (task t4)
- [follow_up] h7 true-reboot verification cannot run in CI: tests simulate it via systemctl --user re-evaluation; a real machine reboot is a manual on-box check after merge. (task t10)
