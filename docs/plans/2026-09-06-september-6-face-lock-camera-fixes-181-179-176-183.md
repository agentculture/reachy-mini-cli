# Build Plan — September-6 face/lock/camera fixes (#181 #179 #176 #183 #184 #185)

slug: `september-6-face-lock-camera-fixes-181-179-176-183` · status: `exported` · from frame: `september-6-face-lock-camera-fixes-181-179-176-183`

> The robot's face lock centres a face in one or two detection cycles instead of settling a third of the way; the face detector runs only while the head is still, so the CM4 keeps its tick rate; the runtime notices a camera that went silently dead and re-acquires it; feel-alive comes back on its own after a face lock releases; the export feed carries a sense line only when a reading changed; and the suite passes on a box with the \[vision\] extra. (issues #181 #179 #176 #183 #184 #185)

## Tasks

### t1 — t1 #185: state the cv2 premise in tests/`test_behavior_clip_rider_composition.py` — simulate absence through `build_clip_encoder`'s `find_spec` seam; keep a real-path test that skips when cv2 is importable

- covers: c16, h13
- acceptance:
  - tests/`test_behavior_clip_rider_composition.py` passes with and without 'uv sync --extra vision' (run both in the task)
  - the absence test injects `find_spec` (no reliance on the runner's package set); the real-path test uses pytest.skip when importlib.util.`find_spec`('cv2') is truthy

### t2 — t2 #184: SenseSnapshotDriver emits on a CHANGED PAYLOAD — build the emit dict first and compare it (minus ts/tick) to the last emitted one; files: reachy/export/runtime.py, new tests/`test_export_sense_snapshot_change.py`, one sentence in docs/export-schema.md

- covers: c10, h9, c11, h10, c36, h26
- acceptance:
  - two consecutive Sense snapshots differing only in `pat_state`.`phase_started_at`/`last_press_at` emit exactly one sense event
  - a face held in view (`face_age_s` advancing) and a live DoA (`doa_age_s` advancing) for 100 ticks emit exactly one sense event; a real phase or face change emits again
  - the sense block's keys in docs/export-schema.md are unchanged (diff of the key table is empty) and `test_h20`'s fold helper still exists; the schema gains one sentence: a sense line implies a changed reading

### t3 — t3 #183: engine base-layer lifecycle — TickContext.`ensure_base` (backed by Engine.`seed_base_layer`, idempotent when a base id is active), Engine.state()/state.json '`base_layer`': {seeded, active, `stopped_by`: 'stop'|'inhibition'|null}, and the un-stop carve-out: Engine.add(`BASE_LAYER_NAME`, looping, duration None) re-seeds the base proper (`is_base`=True) instead of admitting a plain copy; files: reachy/behavior/engine.py, new tests/`test_behavior_engine_base_layer.py`

- covers: c38, h28
- acceptance:
  - after engine.stop('feel-alive'), state()\['`base_layer`'\] is {seeded: True, active: False, `stopped_by`: 'stop'}; after ctx.`ensure_base`() it is active with `stopped_by` None and the new id is in `_base_ids` so stop('all') keeps it
  - an 'add' of feel-alive with looping=True and duration=None produces an ActiveBehavior with `is_base`=True; an add with a duration produces an ordinary bounded one
  - `ensure_base` while a base id is active is a no-op (no second feel-alive on the active set); every re-seed emits one senselog line

### t4 — t4 #181: FaceObservation gains `captured_at` (worker clock at `_input`.take()) and the `face_age_s` provider anchors on it, not on publish; files: reachy/behavior/`face_sense.py`, tests/`test_behavior_face_sense.py`

- covers: c42, h32
- acceptance:
  - with an injected clock and a 300 ms gap between take() and publish, the snapshot's `face_age_s` equals now - `captured_at` (not now - publish)
  - a FaceObservation without `captured_at` (older producer/test fake) still works: age falls back to publish time

### t5 — t5 #176: document the WirePlumber camera-grab boot race, the drop-in (monitor.libcamera/monitor.v4l2 disabled), its session-wide side effect and reversal, the restart order, and the runtime-restart recovery, as a subsection under 'The camera-path repair' in docs/operating-reachy.md

- covers: c20, h15, c41, h31
- acceptance:
  - docs/operating-reachy.md has the subsection with the drop-in path and contents, 'systemctl --user restart wireplumber', the note that every PipeWire camera in the session is removed, and the reversal (delete the file, restart wireplumber)
  - markdownlint-cli2 on the file reports 0 errors; no service-noun code changes in this task

### t6 — t6 #176: HeldMediaClient.drop(reason) — an explicit, idempotent, caller-thread drop that releases what we acquired and leaves the holder disconnected so `_HolderKeeper`'s existing connected==False poll re-warms; `warm_up`'s own backoff bounds retries; files: reachy/robot/`media_client.py`, tests/`test_robot_media_client.py` (or the existing media client test module)

- covers: c39, h29
- acceptance:
  - drop('media-stale') on a connected holder makes connected False, releases media exactly once, emits one named senselog drop, and a second drop is a no-op
  - a concurrent reader thread calling audio()/frame() across a drop never observes an exception or a half-torn client (test with a fake SDK client)
  - grep confirms no code path calls `warm_up`/release on a holder whose connected predicate is True from any thread other than the one calling drop

### t7 — t7 #176: SenseAvailability gains live (bool) + `last_frame_at` (float|None) as a separate fact from available; validation: live requires `last_frame_at`; SenseAvailabilityDriver takes an injected `frame_liveness` provider, compares and reports on (available, reason, live) only; the live threshold is `face_sense`.`DEFAULT_STREAM_STALE_S` imported, not restated; files: reachy/behavior/`sense_availability.py`, tests/`test_behavior_sense_availability.py`

- covers: c37, h27
- acceptance:
  - with a provider whose `last_frame_at` advances every tick for 60 s of fake ticks, the rider performs zero state.json writes after the first and logs zero lines; a live flip writes once and logs exactly one line
  - the block renders {available, reason, live, `last_frame_at`} per sense; a sense with no liveness provider renders live null; SenseAvailability(live=True, `last_frame_at`=None) is refused at construction
  - the retained reachy/state/senses mirror carries the same dict (existing mirror test extended)

### t8 — t8 #181: incremental face-lock aim — FaceLockGaze keeps a short ring of (t, commanded yaw, pitch); target = ring\[capture time\] - (c-0.5)\*fov\*damping per axis; params `fov_h` (default 87), `fov_v` (57), damping (0.7, min 0, max 1) replace `yaw_gain`/`pitch_gain` on the library entry; clamps and slew unchanged; `FACE_LOST_AFTER_S` comment re-derived against slew+settle+detect interval; files: reachy/behavior/`face_lock.py` (FaceLockGaze + constants only), reachy/behavior/library.py (face-lock params only), tests/`test_behavior_face_lock.py`

- covers: c2, h2, c3, h3, c5, h4
- acceptance:
  - simulated pinhole camera (87x57 deg) with a face 30 deg off-axis and a 1.0 s detection cycle: commanded yaw is within 2 deg of the bearing after two cycles and never overshoots (no sign change of the error)
  - with a 300 ms detection latency injected, the ring lookup by `face_age_s` still converges in two cycles
  - `fov_h`/`fov_v`/damping are validated Params: nan, inf, 0 and negative values are refused on both override paths; `yaw_gain`/`pitch_gain` no longer exist on the entry; `face_lock.py` imports no transport and no `reachy_mini` (AST check in the test)

### t9 — t9 #179: still-only detection — FaceSenseDriver takes moving (a SelfMovingProvider peek), `still_settle_s` (own constant, documented as a camera-blur settle distinct from `self_motion`'s 0.25 s tail), and `lock_held` (a peek): `_worker_tick` skips submission while moving or inside the settle; while `lock_held` it degrades to a slow cadence (`DEFAULT_HELD_DETECT_INTERVAL` 1.5 s) instead of none; `DEFAULT_FACE_BBOX_TTL_S` re-derived and its comment states the derivation; adds an `on_stale` callback seam fired by `_check_stream_staleness` when the latch trips AND a frame has ever arrived AND `camera_available` is True; one senselog gate line on open/close like rms's moving-floor; files: reachy/behavior/`face_sense.py`, tests/`test_behavior_face_sense.py`, new tests/`test_behavior_face_gate_lock_interplay.py`

- depends on: t4
- covers: c6, c7, h6, c40
- acceptance:
  - with moving() True for 2 s of fake clock the worker submits zero detections; within `still_settle_s` of moving() turning False it submits again
  - with `lock_held`() True and moving() True, submissions occur at the held cadence (not zero); a FaceLockDriver fed a moving head and bbox gaps of one held cadence reports face-lost no more often than main (existing lifecycle tests unchanged)
  - `on_stale` fires once per silent episode and never when `_last_frame_at` is None or `camera_available` is False; the gate emits exactly one open and one close senselog line per motion episode

### t10 — t10 #183/#181 antennas: face-lock claims head + `body_yaw` (FaceLockGaze contributes `body_yaw` held at its value at lock time), feel-alive leaves `LOCK_INHIBITS` (orient-to-sound stays), the lock's inhibition snapshot/restore handles one name; update the 27 test references and CLAUDE.md:592; files: reachy/behavior/`face_lock.py` (`LOCK_INHIBITS` + `body_yaw` contribution), reachy/behavior/library.py (face-lock channels), tests/`test_behavior_face_lock.py`, tests/`test_behavior_face_lock_lifecycle.py`, CLAUDE.md

- depends on: t8
- covers: c28, h19, c29, h20, c15, h12
- acceptance:
  - with feel-alive seeded and a lock admitted, arbitrate() gives head and `body_yaw` to face-lock and antennas to feel-alive; feel-alive is still on the active set across lock, hold and release
  - `LOCK_INHIBITS` == ('orient-to-sound',); orient-to-sound admitted during a lock does not own the head; the later-wins release tests pass with the single name
  - the lock's `body_yaw` contribution equals the `body_yaw` commanded on the tick before lock and stays constant while held; CLAUDE.md's sentence at the old line 592 names one behavior

### t11 — t11 #183: IntentDriver re-seeds the base layer on the inhibition-clear edge — `_enforce_inhibitions` tracks the previous inhibited set and calls ctx.`ensure_base` when feel-alive leaves it (only then: a by-name stop is never re-seeded); the spool's `run_behavior` unbounded-lifetime refusal is carved out for `BASE_LAYER_NAME` only (an unbounded `run_behavior` feel-alive means re-seed); files: reachy/behavior/intents.py, tests/`test_behavior_intents.py`

- depends on: t3
- covers: c13, h11, c31, h21
- acceptance:
  - `set_inhibition`(\['feel-alive'\]) then `set_inhibition`(\[\]) leaves feel-alive active with `is_base`=True within one tick and stop('all') keeps it; a by-name stop followed by ticks never re-seeds
  - `lock_face` then `release_face` (with the t10 lock no longer inhibiting feel-alive) leaves the base layer untouched throughout; a mind's `set_inhibition` naming feel-alive during a lock, then cleared, re-seeds once
  - `run_behavior` feel-alive with no lifetime is admitted as the base re-seed; `run_behavior` nod with no lifetime is still refused as unbounded

### t12 — t12 composition: wire everything in `_compose_run_seam` — face gate (moving=`self_motion`.`is_moving`, `lock_held`=`face_lock_driver`.locked peek), `on_stale` -> media.drop('media-stale') (tick-thread side; keeper unchanged), availability liveness provider from `face_driver`, `ensure_base` on TickContext; composition tests for each wire; files: reachy/cli/`_commands`/behavior.py, tests/`test_behavior_face_gate_composition.py` (new), existing composition test modules extended

- depends on: t6, t7, t9, t10, t11
- covers: h5, c17, c39, h29, c40, h30, c21, h16, c22, h17
- acceptance:
  - a composed runtime with a fake media client whose frames stop for `DEFAULT_STREAM_STALE_S` drops the client from the tick thread and the keeper re-warms it (`frame_available` returns true) without a process restart; a box with `camera_available` False never drops; exactly one 'media-stale' drop line per episode
  - a composed runtime with the head commanded to slew submits zero detections during the slew and one within settle+interval after it
  - behavior rules check output on `default_rules.toml` is byte-identical to main; tests/`test_zero_llm_boundary.py` and tests/`test_embody_redteam.py` pass unmodified; `_COMPOSED_PROVIDER_FIELDS` is untouched

### t13 — t13 docs: operating guide + schema — face-lock aim (`fov_h`/`fov_v`/damping, two-cycle centring, antennas keep swaying, `body_yaw` claim), still-only detection and its held cadence, senses.live/`last_frame_at` and the media-stale re-acquire, `base_layer` status + the un-stop verb; docs/export-schema.md state.json section for `base_layer` and senses; files: docs/operating-reachy.md, docs/export-schema.md, README noun table if the face-lock row changes

- depends on: t12
- acceptance:
  - every new param, key and senselog reason introduced by t2-t12 appears in the docs by name (grep list in the task)
  - markdownlint-cli2 on both files reports 0 errors; github-slugger-verified anchors for any new cross-links

### t14 — t14 #179: file the GStreamer receive-pipeline decimation ask upstream on `reachy_mini` (`webrtc_client_gstreamer.py`: videorate/caps framerate before the decoder, or an fps knob) and link it from issue #179; no code change here

- covers: c9, h8
- acceptance:
  - an upstream issue URL is posted on #179; grep for videorate/framerate=/Gst in reachy/ is empty

### t15 — t15 live acceptance on the Wireless (branch wireless-motor-enable rebased on the merged fixes): one evidence note under docs/evidence/ per issue with the box, runtime version and harness build; measurements: snapshot rate 60 s idle with camera live; a 30 deg off-axis face centred within two cycles; antennas swaying under a lock with state.json ownership recorded; a silently killed pipeline re-acquired without restart; the release-time jump magnitude; then close each issue and confirm with the reachy-nova maintainer that the revive and the availability-derived liveness can be dropped

- depends on: t12, t13
- covers: c1, h1, c8, h7, h14, c27, h18, c32, h22, c33, h23, c34, h24, c35, h25
- acceptance:
  - docs/evidence/2026-09-\*-<issue>.md exists for each of #181 #179 #176 #183 #184 #185 naming box, version and harness build, with the numbers above
  - snapshot rate > 40 Hz over 60 s idle with the camera live; face centred within 2 deg in two detection cycles; feel-alive owns antennas and face-lock owns head+`body_yaw` in state.json during a lock; `frame_available` returns true after a silent pipeline kill without a runtime restart
  - each issue is closed with a link to its evidence note and the PR; reachy-nova #25/#26 has a comment confirming the two workarounds can be removed

## Risks

- [unknown_nonblocking] release-time handover jump: on `release_face` head and `body_yaw` switch to feel-alive's current wander with no easing (bounded by 12/6 deg at energy 1.0); today's release also jumps; t15 measures it and decides whether the lock should ease out (a follow-up, not this arc)
- [unknown_nonblocking] in-process recoverability of a silently dead pipeline is unmeasured; t12 proves the drop->re-warm path against a fake client, t15 proves it on the box; if the box needs a daemon media re-acquire beyond `warm_up`, that is a deviation to record
- [unknown_nonblocking] the Lite's camera FOV is unmeasured; `fov_h`/`fov_v` defaults are Wireless numbers and a Lite lock may under- or over-shoot until /api/camera/specs is read there (t8 keeps the params overridable; per-camera resolution at composition is a follow-up)
- [unknown_nonblocking] delivery shape: the workforce builds the waves on one integration branch, but the frame commits to one PR per issue (c23); slicing the integration branch into per-issue PRs (t1->#185, t2->#184, t3+t11->#183, t4+t8+t10->#181, t9->#179, t5+t6+t7->#176, t12/t13 split by issue) is the main agent's job after wave 3 and must keep each PR green on its own
- [unknown_nonblocking] reachy/cli/`_commands`/behavior.py is the one file several concerns converge on; it is deliberately touched by t12 only (t3's un-stop lives in Engine.add so `cmd_run` needs no change) — if a wave-1/2 task finds it must edit that file, stop and record a deviation rather than merging a collision
