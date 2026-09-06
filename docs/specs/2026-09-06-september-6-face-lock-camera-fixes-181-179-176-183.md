# September-6 face/lock/camera fixes (#181 #179 #176 #183 #184 #185)

> The robot's face lock centres a face in one or two detection cycles instead of settling a third of the way; the face detector runs only while the head is still, so the CM4 keeps its tick rate; the runtime notices a camera that went silently dead and re-acquires it; feel-alive comes back on its own after a face lock releases; the export feed carries a sense line only when a reading changed; and the suite passes on a box with the \[vision\] extra. (issues #181 #179 #176 #183 #184 #185)
> instruction: one PR per issue (c23), each bumping the version; a live acceptance note per PR under docs/evidence/ before the issue is closed

## Audience

- Operators of a deployed Reachy Mini (the Wireless on the CM4 first, the Lite on the dev box second) and the reachy-nova harness that drives `lock_face` / reads state.json and the MQTT feed
  - instruction: each evidence note names the box and the harness build it was checked against

## Before → After

- Before: Today a face lock settles about a third of the way to the face and chases it for 3-4 s per movement; the face detector runs while the head slews, dropping the CM4's tick rate from ~50 to ~7 Hz; a camera that dies silently stays 'available' for 46 min with no re-acquire; the first lock of a session leaves the robot rigid until a restart (antennas included); the sense feed emits lines whose only change is a clock reading; and the suite fails on any box with the \[vision\] extra
  - instruction: the fix PR for each issue links the reproduction (a test that fails on main, or the issue's measured evidence)
- After: A lock centres the face in two detection cycles (damping 0.7) through the camera's FOV and holds it with the antennas still swaying; the detector runs only while the head is still; state.json's senses block carries `last_frame_age_s` and live per sense and the keeper re-acquires media when live stays false; the base layer returns when an inhibition on it clears, while a by-name stop holds and is reported as base layer stopped; sense lines fire only on a changed reading; the suite passes with or without cv2
  - instruction: the exported spec's requirements section is the checklist; the delivery summary ticks each one

## Why it matters

- The face lock is the body half of a conversation (reachy-nova #25/#26): a lock that never centres, a robot that goes still after it, and eyes that report 'available' while blind each make the feature unacceptable live, and the peer harness has been carrying workarounds (revive feel-alive, derive liveness from availability) that only exist because the runtime does not keep its own promises
  - instruction: confirm with the reachy-nova maintainer on #25/#26 once #183 and #176 are deployed

## Requirements

- \#181: `face_lock`.`__call__` (reachy/behavior/`face_lock.py`:257-261) sets an ABSOLUTE target -(cx-0.5)\*2\*gain with `YAW_GAIN_DEG`=20/`PITCH_GAIN_DEG`=12, so on the Wireless camera (~87° HFOV, ~57° VFOV from /api/camera/specs K) it settles at ~0.31 of the face angle; make it INCREMENTAL: target = current commanded yaw - (cx-0.5)\*`fov_h`\*damping, pitch likewise with `fov_v`, clamps unchanged
  - instruction: target = `base_yaw` - (cx-0.5)\*`fov_h`\*damping where `base_yaw` is the commanded yaw at the frame's capture time (a small ring indexed by `face_age_s`); same for pitch with `fov_v`; damping default 0.7 (c24); clamps and slew unchanged; unit test with a simulated camera model asserting convergence in two cycles and no overshoot
  - honesty: with a face held 30 deg off the camera axis, the commanded yaw reaches within 2 deg of the face bearing within two detection cycles and does not oscillate
- \#181: the face-lock library entry (reachy/behavior/library.py:329-370) gains `fov_h` / `fov_v` Params (deg, minimum>0) replacing the meaning of `yaw_gain`/`pitch_gain`; defaults are the Wireless camera's measured FOV, and a peer (reachy-nova PR #26) can override per call
  - instruction: replace `yaw_gain`/`pitch_gain` with `fov_h` (default the Wireless HFOV ~87), `fov_v` (~57) and damping (0.7, min 0, max 1); update library tests and docs/operating-reachy.md's face-lock section
  - honesty: `fov_h`/`fov_v`/damping are validated Params on the face-lock entry, refused fail-closed for non-finite or non-positive values, and a `lock_face` payload naming them reaches the gaze
- \#179: FaceSenseDriver gains an injected `self_moving` peek (SelfMotionDriver.`is_moving`, reachy/behavior/`self_motion.py`:181 — the same seam `rms_sense`'s moving floor consumes) and `_worker_tick` (`face_sense.py`:1085-1099) skips submission while moving plus a settle after motion stops; composition wires `self_motion`.`is_moving` in `_compose_run_seam` beside the rms wiring (behavior.py:2622-2630)
  - instruction: FaceSenseDriver(moving=`self_motion`.`is_moving`, `still_settle_s`=...) — skip `_worker_tick` submission while moving or inside the settle; wire in `_compose_run_seam` beside the rms moving-floor wiring; a named senselog gate line on open/close like rms's moving-floor
  - honesty: with the head commanded to slew continuously, the face worker submits zero detections; within settle seconds of the slew ending it submits again
- \#179: while a face-lock is HELD the gate degrades to a slow cadence during motion (not none) and `FACE_LOST_AFTER_S` (`face_lock.py`:136, 3.0 s) / `DEFAULT_FACE_BBOX_TTL_S` (1.5 s) are re-checked against slew+settle+one detect interval so a held lock on a walking person reports face-lost no more often than today (`test_behavior_face_lock_lifecycle.py` keeps passing)
  - instruction: while a lock is held, motion degrades the cadence to a slow interval (e.g. 1.5 s) instead of none; re-derive `FACE_LOST_AFTER_S` and `DEFAULT_FACE_BBOX_TTL_S` against slew + settle + one detect interval and document the derivation in the constants' comments
  - honesty: a held face-lock on a person walking slowly across the frame reports face-lost no more often than before the gate (lifecycle tests unchanged and a new test with a moving head + intermittent bbox)
- \#184: SenseSnapshotDriver (reachy/export/runtime.py:590-595) compares Sense by frozen-dataclass equality; `pat_sense.py` rewrites PatState.`phase_started_at`/`last_press_at` to now on ~8 paths (`pat_sense.py`:721,862-908), so a tick can emit a sense line whose only change is a clock reading; compare a scrubbed view (clock fields excluded) so a sense event means a reading changed
  - instruction: SenseSnapshotDriver compares a scrubbed key (Sense with the two clock fields nulled) instead of the raw dataclass; the emitted payload is unchanged; unit test for the timestamp-only case and for a real phase change
  - honesty: two consecutive Sense snapshots differing only in `pat_state`.`phase_started_at`/`last_press_at` emit exactly one sense event
- \#183: Engine.`seed_base_layer` (engine.py:147-161) runs once at start (engine.py:581) and IntentDriver.`_enforce_inhibitions` (intents.py:508-518) evicts feel-alive while inhibited; on the no-longer-inhibited edge the base layer must be re-seeded (same energy, `is_base`=True, recorded in `_base_ids` so stop all still keeps it) — mirroring `_sustain_goal`'s re-admit for declared goals
  - instruction: TickContext gains an `ensure_base` capability backed by Engine.`seed_base_layer`; IntentDriver tracks the previous inhibited set and calls it on the edge where feel-alive leaves the set; one senselog line per re-seed
  - honesty: after `lock_face` then `release_face` (or any `set_inhibition` that named feel-alive and then cleared), feel-alive is active again with `is_base`=True within one tick, and stop all still keeps it
- \#185: tests/`test_behavior_clip_rider_composition.py`:222 asserts cv2 is absent; skip when importlib.util.`find_spec`('cv2') is truthy, or simulate absence through `clip_rider`.`build_clip_encoder`'s injectable `find_spec` seam (`clip_rider.py`:275-290) so the test states its premise
  - instruction: prefer the `find_spec` injection seam of `build_clip_encoder` so the test simulates absence; keep a second test that runs the real path only when cv2 is genuinely absent (skip otherwise)
  - honesty: the test passes on a checkout with and without the \[vision\] extra
- \#176 ask 1: the runtime ACTS on liveness: when live stays false for a sustained window (seconds), the keeper re-acquires the media (`warm_up` / media re-acquire) rather than only after a reported GStreamer error; `_HolderKeeper` today polls only the connected predicate (client is not None) so a silently dead pipeline never re-warms
  - instruction: `_HolderKeeper` additionally reads the face sense's liveness (live=false for N s, N default ~15) and calls the held client's release + `warm_up` (media re-acquire); a named senselog line per attempt with backoff; measure the recovery live and record it
  - honesty: with the camera pipeline killed silently on the Wireless, the runtime re-acquires media and `frame_available` returns true without a process restart, within the liveness window plus warm-up time
- \#176 ask 3: the WirePlumber camera-grab boot race and its fix (~/.config/wireplumber/wireplumber.conf.d/99-reachy-no-camera.conf disabling monitor.libcamera/monitor.v4l2) plus the runtime-restart recovery go into docs/operating-reachy.md's camera-path repair section (line ~1372); optionally 'service install' writes the drop-in on the Wireless — nothing in reachy/ or docs/ mentions wireplumber/pipewire today
  - instruction: add a subsection under 'The camera-path repair'; no service-noun change in this arc (a drop-in writer is a follow-up decision)
  - honesty: docs/operating-reachy.md documents the WirePlumber drop-in, the restart order, and the runtime-restart recovery, and markdownlint passes
- While a face lock holds the head still, the antennas keep moving (user, 2026-09-06).
  - instruction: verify on the Wireless and record ownership from state.json in the evidence note
  - honesty: with a lock held on a live box, the antenna sway is visible and state.json ownership shows antennas owned by feel-alive and head+`body_yaw` by the lock
- \#183/#181 antennas: face-lock stops inhibiting feel-alive and instead CLAIMS head + `body_yaw` (holding `body_yaw` so the base layer's slow body wander cannot rotate the camera off the face); arbitration.py is per-channel with abstention and face-lock is STOPPABLE above the PASSIVE base layer, so the lock takes head and `body_yaw` and feel-alive keeps only the antennas — no eviction, no re-seed needed for the lock case
  - instruction: face-lock entry channels = {head, `body_yaw`}; FaceLockGaze contributes `body_yaw` (held at its value at lock time or 0); drop feel-alive from `LOCK_INHIBITS`; the lock's inhibition snapshot/restore logic handles one name
  - honesty: with the lock active, arbitration gives head and `body_yaw` to face-lock and antennas to feel-alive; feel-alive is never evicted by a lock; the camera stays on the face (`body_yaw` held)
- \#183 stays as seam (a) for the GENERIC case: a mind's `set_inhibition` naming feel-alive still evicts the base layer, and the re-seed fires when that inhibition clears — the lock simply no longer exercises it
  - instruction: same mechanism as c13; a dedicated intents test for the spool path
  - honesty: a spool `set_inhibition` naming feel-alive then an empty `set_inhibition` leaves feel-alive active with `is_base`=True
- \#184 (challenge): Sense carries two CONTINUOUS clock-derived fields the payload never emits — `face_age_s` (sense.py:201, refreshed from the provider every tick while a face is held) and `doa_age_s` (sense.py:196, replace()d every tick from the DoaPoller) — so with a face or a DoA reading present the snapshot differs EVERY tick and SenseSnapshotDriver emits at tick rate whatever `pat_state` does; the change detector must compare the EMITTED payload (or a scrub that nulls `face_age_s`, `doa_age_s`, `phase_started_at`, `last_press_at`), not the raw dataclass
  - instruction: build the payload dict first and compare it (minus ts/tick) to the last emitted payload; emit only on inequality; test with a held face and a live DoA
  - honesty: with a face held in view and a steady DoA for 100 ticks, exactly one sense event is emitted
- \#176 (challenge): the senses block's liveness must be published as STABLE values — `last_frame_at` (a timestamp, null when never seen) and live (bool) — not a per-tick age: SenseAvailabilityDriver.`_publish` (`sense_availability.py`:348) writes state.json whenever the block differs and `_report` logs every changed entry, so a continuously changing `last_frame_age_s` would write the file and emit a senselog line on every tick; a consumer derives the age from `last_frame_at`, and the change/report gate keys on live flips only; the live threshold is `face_sense`.`DEFAULT_STREAM_STALE_S` (10 s), one constant, not a second number
  - instruction: extend SenseAvailability with live/`last_frame_at` (validation: live requires `last_frame_at`); feed from FaceSenseDriver.`_last_frame_at`; compare and report on (available, reason, live) only; retained reachy/state/senses mirrors it
  - honesty: with the camera live for 60 s the availability rider performs zero state.json writes after the first and logs exactly one line per live flip
- \#183 (challenge): Engine.state() (engine.py:314) has no notion of a stopped base layer — 'base layer stopped' must be an explicit state.json/behavior status field, e.g. `base_layer`: {seeded: bool, active: bool, `stopped_by`: 'stop'|'inhibition'|null}, so an operator and the peer can tell intentional stillness from an inhibition and from a crashed engine
  - instruction: engine tracks the base id's presence and the last removal cause; additive key, documented in docs/export-schema.md's state.json section and the operating guide
  - honesty: after 'behavior stop feel-alive', 'behavior status --json' reports `base_layer`.active false and `stopped_by` 'stop'; after a lock's release it reports active true
- \#176 (challenge): the liveness-driven re-acquire must NOT mutate a CONNECTED holder from the keeper thread — `_HolderKeeper`'s safety argument (behavior.py:1815-1845) is that only a DISCONNECTED holder is inert on the tick thread while the AudioPump thread and the tick thread read the same client. Route it through the existing drop path instead: the tick-thread side (the face sense's staleness check, which already owns the 10 s latch) asks the held client to DROP itself exactly as a raising read does (`media_client.py`:401 'a read that raises drops the client and schedules a retry'), and the keeper's unchanged connected==False poll re-warms
  - instruction: add HeldMediaClient.drop(reason) (tick-thread only, idempotent) invoked from FaceSenseDriver.`_check_stream_staleness` when the latch trips and a frame has EVER arrived; keeper untouched
  - honesty: no code path calls `warm_up`/release on a holder whose connected predicate is True from any thread other than the one that drops it; the audio pump never observes a half-torn client (test with a fake client and a concurrent reader thread)
- \#181 (challenge): FaceObservation (`face_sense.py`:554) carries no capture timestamp — `face_age_s` counts from PUBLISH, and a YuNet+SFace pass on the CM4 takes an unmeasured but non-trivial time on a 640 px frame; the observation gains `captured_at` (the worker's clock at `_input`.take()) so `face_age_s` and the lock's base-yaw ring both measure from capture
  - instruction: record clock() before `_detect_once` and carry it on FaceObservation; the driver's age provider uses it
  - honesty: `face_age_s` on the snapshot equals now - `captured_at`, and a test with an injected 300 ms detection latency still converges in two cycles

## Honesty conditions

- each of the six issues is closed by a merged PR whose acceptance was measured on the Wireless, not only by the offline suite
- `face_lock.py` imports no transport and no `reachy_mini` after the change
- measured on the Wireless with the camera live and the runtime idle: snapshot rate stays above 40 Hz for a 60 s window
- no GStreamer element, caps string or videorate appears in reachy/ after the change
- docs/export-schema.md's sense block diff is empty and `test_h20`'s fold helper is still present
- `test_behavior_face_lock`\*.py assert `LOCK_INHIBITS` == ('orient-to-sound',) and the later-wins tests pass with the single name
- behavior rules check output is byte-identical before and after on the shipped `default_rules.toml`
- tests/`test_zero_llm_boundary.py` and tests/`test_embody_redteam.py` pass unmodified
- orient-to-sound admitted during a lock does not take the head
- the acceptance notes are taken on the Wireless (CM4) and the harness side is verified against reachy-nova PR #26's expectations (`lock_face` params, state.json senses.live, MQTT feed)
- each of the six before-state defects is reproduced by a failing test or a cited live measurement before its fix lands
- after the arc the peer harness can delete its feel-alive revive and its availability-derived liveness guess with no loss of function
- every after-state sentence maps to a confirmed requirement (c2 c3 c6 c7 c10 c13 c16 c17 c18 c20 c27 c28 c31) and none of them is unfulfilled at export time
- a box with `camera_available` False never emits a media-reacquire; a re-acquire emits exactly one named drop and the transcript session survives the gap (its own reconnect discipline)
- docs/operating-reachy.md names the side effect and the reversal command

## Success signals

- \#179: with the runtime idle (feel-alive only) and the camera live, the CM4's reachy/events/sense/snapshot rate stays near 50 Hz (was 501 -> 69 per 10 s); a face during a 1 s deliberate slew is detected within 1 s after the slew ends
  - instruction: sample reachy/events/sense/snapshot over MQTT for 60 s before and after; record both numbers in docs/evidence/

## Scope / boundaries

- \#181: reading the FOV from the daemon at runtime is NOT done inside `face_lock.py` — it is a leaf that imports no transport (the http transport's `camera_specs`() exists at reachy/robot/`http_transport.py`:138 and is used only by vision.py); if per-camera FOV is wanted, composition (`_compose_run_seam`) resolves it once and injects a param default
  - instruction: if per-camera FOV is wanted, `_compose_run_seam` reads transport.`camera_specs`() once and passes the derived FOV as the entry's param default; a failed read keeps the shipped default with one senselog line
- \#179 comment item 1 (videorate/framerate decimation before the decoder) lives in the SDK's receive pipeline (`reachy_mini`/media/`webrtc_client_gstreamer.py`, `media_server.py`), not in this repo — reachy/robot/`media_client.py` wraps the SDK and builds no GStreamer element; decimation is an upstream ask unless the SDK exposes an fps knob
  - instruction: file the decimation ask upstream on `reachy_mini` (`webrtc_client_gstreamer.py`) and link it from the issue; no change here
- \#184: the sense block's documented keys in docs/export-schema.md (`phase_started_at` and `last_press_at` stay in the payload) do not change; only the emit-on-change predicate does, and tests/`test_behavior_nervous_composition.py`'s h20 fold (lines 165-198, PR #182's test-side hardening) is kept, not reverted
  - instruction: leave the schema and the PR #182 test fold untouched; add a note in the schema doc that a sense line implies a changed reading
- \#183: the lock's orient-to-sound inhibition and the v0.51.1 later-wins release semantics are unchanged; the ONLY change to `LOCK_INHIBITS` is feel-alive leaving it (c28) — the lock never evicts the base layer, and the edge re-seed (c31) covers every other inhibitor
  - instruction: update the 27 test references and CLAUDE.md:592's sentence
- reachy/behavior/sense.py `_COMPOSED_PROVIDER_FIELDS` and rules.`SENSE_FIELDS` are untouched: no new rule-visible sense field is added by any of the six; `face_bbox`/`face_age_s` stay continuous readings off the snapshot (CLAUDE.md's stated exemption)
  - instruction: do not touch `_COMPOSED_PROVIDER_FIELDS` or `SENSE_FIELDS`
- tests/`test_zero_llm_boundary.py` and tests/`test_embody_redteam.py` pins are untouched: every change stays inside reachy/behavior, reachy/export, reachy/robot, `_commands`/behavior.py composition, docs and tests — no new import edge to reachy.speech/vision/forge, no `reachy_mini` import outside robot/
  - instruction: keep every new import inside reachy/behavior, reachy/export, reachy/robot and the composition function
- orient-to-sound STAYS in `LOCK_INHIBITS`: it is STOPPABLE like the lock, and a later admission wins the head on the recency tie-break, so arbitration alone cannot keep it off the face — only the feel-alive entry leaves the set
  - instruction: keep orient-to-sound in `LOCK_INHIBITS`; existing lifecycle tests cover it
- \#176 (challenge): a liveness re-acquire (1) fires only when `camera_available` is True AND a frame has ever arrived (`face_sense`'s `_last_frame_at` is None exemption — a robot with no camera or one still warming must never loop re-acquires), (2) backs off like `warm_up`'s own retry, and (3) costs the MIC too: the held client is one media session, so dropping it silences rms/transcript/tee/speech for the warm-up (~1.0-1.2 s measured, #94) — named by one senselog drop ('media-reacquire') so an audio gap in the journal is attributable
  - instruction: gate on `camera_available` and `last_frame_at`; reuse `warm_up` backoff; one senselog line per attempt
- \#176 (challenge): the WirePlumber drop-in (monitor.libcamera and monitor.v4l2 disabled) removes EVERY camera from the user session's PipeWire graph, not only the daemon's — any browser or PipeWire-based tool on the box loses camera access; the docs must state this and that it is a per-box operator change, reversible by deleting the file and restarting wireplumber
  - instruction: one paragraph beside the drop-in text

## Non-goals

- \#176 'sticky media ownership' (a second SDK client with a media profile yanking the daemon's media away from the runtime's held client) is daemon/SDK behaviour (`reachy_mini` 1.9 /ws/sdk release on connect) — not fixable in this repo; file upstream, and at most log who released (peer address is not exposed to the client today)

## Assumptions

- \#181: the bbox is detected in an OLDER head frame (detection latency up to `REACHY_FACE_DETECT_INTERVAL`=1.0 s on the box, plus the slew); an incremental target must be based on the commanded yaw at capture time (`face_age_s` is on the Sense snapshot, so a short ring of past commanded angles suffices) or it double-counts motion while slewing — #179's still-only detection removes the hazard when both land together
- \#184/#179: the snapshot is also published to MQTT as reachy/events/sense/snapshot on every emitted sense event (reachy/export/mqtt.py `SENSE_EVENT_TYPE`); change-only emission is the cheap CM4 win, and a fixed 5-10 Hz publish cap (#179 comment item 2) is a separate, optional follow-up not in this frame
- delivery is per-issue PRs, each bumping the version (version-check CI): #185 and #184 are one-file fixes; #183 is engine+intents; #181 and #179 both touch the face lock's timing and should land as one pair (or #179 first) because the still-only gate is what makes the incremental aim's capture-frame assumption hold; #176 splits into a docs PR now and a keeper-trigger PR after the c17 decision
- a by-name 'behavior stop feel-alive' stills the antennas too — intentional stillness (decision q2) is the whole base layer, not the head alone; 'antennas keep moving' applies to the lock's head hold
- \#183/#181 (challenge): the `lock_face` response's inhibited list (`face_lock.py`:630, sorted(self.`_added`)) shrinks from two names to one; reachy-nova PR #26 is assumed to treat it as an opaque list, not to assert 'feel-alive' in it

## Scope exploration

- `s1` — `reachy/behavior/face_lock.py __call__ + YAW_GAIN_DEG/PITCH_GAIN_DEG/SLEW_DEG_S`: the aim is open-loop: target derived from bbox offset alone, never from the current commanded angle; the lock owns the head channel (`LOCK_INHIBITS` evicts feel-alive/orient-to-sound) so self.`_yaw` IS the head yaw and an incremental target is well-defined
  - seeds: `c2`
- `s2` — `reachy/behavior/library.py face-lock LibraryEntry params`: `yaw_gain`/`pitch_gain` are declared Params with minimum=0.0 and are reachable from `run_behavior`/`lock_face` payloads and a rule's params; renaming or re-meaning them is a validated-surface change (`validate_param_value` on both override paths)
  - seeds: `c3`
- `s3` — `reachy/behavior/face_sense.py DEFAULT_FACE_BBOX_TTL_S / face_age_s; face_lock.py _is_stale(age, params)`: `face_age_s` is already read every tick by the lock (`max_age` param); no history of commanded angles exists anywhere in the lock today
  - seeds: `c4`
- `s4` — `reachy/robot/http_transport.py camera_specs + reachy/cli/_commands/vision.py:212`: GET /api/camera/specs is already wrapped as transport.`camera_specs`(); the only caller is the vision noun; `face_lock.py` is import-free of transports by design (leaf module, registered by composition)
  - seeds: `c5`
- `s5` — `reachy/behavior/face_sense.py _worker_tick cadence gate`: detection is gated only on `detect_interval`; frames are taken and detected regardless of commanded motion; the worker reads the freshest frame via `_input`.take()
  - seeds: `c6`
- `s6` — `reachy/behavior/self_motion.py SelfMotionDriver / rms_sense.py peek_moving + _MovingFloorGate`: `is_moving` is a non-consuming, never-raising peek already typed as SelfMovingProvider; `DEFAULT_TAIL_S`=0.25 s is the actuator-noise tail, NOT a camera-blur settle — the face gate needs its own settle constant
  - seeds: `c6`
- `s7` — `reachy/behavior/face_lock.py FACE_LOST_AFTER_S + face_sense.py DEFAULT_FACE_BBOX_TTL_S`: a still-only gate lengthens bbox gaps exactly while the lock is slewing; the lock holds its last target through absence and reports face-lost at 3.0 s — the numbers must be re-derived together, issue #179 option (a)+(b)
  - seeds: `c7`
- `s8` — `issue #179 acceptance + comment (top -H on the Wireless)`: runtime ~77% of a core: tick 37%, GStreamer receive threads ~36 points, python workers the rest; the face worker is the new load
  - seeds: `c8`
- `s9` — `reachy/robot/media_client.py + installed reachy_mini/media/*.py`: no Gst element or caps string exists in reachy/; grep for videorate/framerate= hits only the SDK's `webrtc_client_gstreamer.py` and `media_server.py`
  - seeds: `c9`
- `s10` — `reachy/export/runtime.py SenseSnapshotDriver.__call__ + reachy/behavior/sense.py PatState`: equality is the whole change detector; PatState carries two pure-clock fields; the payload still emits them (docs/export-schema.md:351 documents both keys) so the WIRE SHAPE need not change
  - seeds: `c10`
- `s11` — `tests/test_behavior_nervous_composition.py _fold/_scrub + docs/export-schema.md sense block`: PR #182 already folds consecutive scrubbed-identical sense events in the test; the product fix makes the fold a no-op but the guard stays
  - seeds: `c11`
- `s12` — `reachy/export/mqtt.py SENSE_EVENT_TYPE=snapshot`: every sense runtime event fans out to the bus; nothing rate-limits it below tick rate today
  - seeds: `c12`
- `s13` — `reachy/behavior/engine.py seed_base_layer/_base_ids/stop('all') + intents.py _enforce_inhibitions/_sustain_goal`: eviction of the base layer is real and unrecovered; `_sustain_goal` is the existing re-admit pattern but only for a declared goal; ctx.`active_names`()/ctx.evict()/ctx.admit() are the TickContext seams available to a driver — `seed_base_layer` is NOT reachable from ctx today
  - seeds: `c13`
- `s14` — `issue #183 comment: 'behavior stop feel-alive' evicts by name and the harness revives it`: a by-name stop of the base layer is currently permanent too; whether that is a feature (operator asked for stillness) or the same bug decides seam (a) vs (b)
  - seeds: `c14`
- `s15` — `reachy/behavior/face_lock.py LOCK_INHIBITS + v0.51.1 later-wins`: the eviction while locked is the intended behaviour (feel-alive would drag the head off the face); only the post-release edge is missing
  - seeds: `c15`
- `s16` — `tests/test_behavior_clip_rider_composition.py:222 + reachy/behavior/clip_rider.py build_clip_encoder(find_spec=...)`: the rider already exposes a `find_spec` injection seam used by `face_sense`.`vision_unavailable_reason`; the test bypasses it and relies on the runner's package set
  - seeds: `c16`
- `s17` — `reachy/cli/_commands/behavior.py _HolderKeeper + reachy/robot/media_client.py connected/warm_up/_acquire_media`: re-warm is driven solely by connected==False; a client with a dead pipeline stays 'connected'; `face_sense`'s staleness detector deliberately never calls `warm_up` (issue #138 boundary) — lifting that boundary is the decision
  - seeds: `c17`
- `s18` — `reachy/behavior/sense_availability.py module docstring + runtime_probes`: the block's contract forbids transient facts; `face_sense` already holds `_last_frame_at` and a 10 s staleness latch — the fact exists, only its placement in state.json is undecided
  - seeds: `c18`
- `s19` — `reachy/robot/media_client.py media-lifecycle docstring (acquire refcount, 'released: true once the last consumer lets go')`: the daemon's release is triggered by another client; our ledger is one bit (did WE acquire) and nothing on the client side can refuse a foreign release
  - seeds: `c19`
- `s20` — `docs/operating-reachy.md 'The camera-path repair (SDK >= 1.9)' + reachy/service/units.py`: grep for wireplumber/pipewire across reachy/, docs/, README.md is empty; service's `_PRESENCE` stays demo/runtime and units are pure renderers, so a WirePlumber drop-in would be a new, destructive-class file write needing its own decision
  - seeds: `c20`
- `s21` — `reachy/behavior/sense.py _COMPOSED_PROVIDER_FIELDS`: none of the six adds a provider a rule predicate can name, so the linter's source of truth stays as is
  - seeds: `c21`
- `s22` — `tests/test_zero_llm_boundary.py + tests/test_embody_redteam.py`: the six touch the symbolic loop's leaves and riders; `face_sense` already lazy-imports cv2 and `media_client` already owns the `reachy_mini` edge
  - seeds: `c22`
- `s23` — `git log origin/main (v0.53.0 at 0817403; #180 merged as v0.52.1) + CLAUDE.md 'Every PR bumps the version'`: all six are unfixed on main as of 21847ac; the deployed Wireless runs 0.52.1 on branch wireless-motor-enable, so any fix ships through that branch's next rebase
  - seeds: `c23`
- `s24` — `reachy/behavior/arbitration.py arbitrate() + feel_alive.py _raw_motion + library.py feel-alive channels`: ownership is decided per CHANNEL by (class priority, recency); feel-alive claims {head, antennas, `body_yaw`} and its `body_yaw` term (energy\*p\['`body_yaw`'\]\*sin(0.07t)) would rotate the camera, which is why the lock inhibits the whole layer today instead of letting arbitration split it
  - seeds: `c28`
- `s25` — `reachy/behavior/face_lock.py LOCK_INHIBITS=('feel-alive','orient-to-sound') + tests/test_behavior_face_lock*.py (27 references) + CLAUDE.md:592`: the pair is pinned by 27 test lines across two files and one CLAUDE.md sentence ('the two behaviors that would drag the head off the face'); dropping feel-alive from the set is a test+docs change, and the later-wins release logic (v0.51.1) shrinks to one name
  - seeds: `c29`
- `s26` — `decision q2 wording ('asking for a still robot')`: the antenna requirement is stated for head stillness; a by-name stop is a still ROBOT — read as antennas included, flagged for the user
  - seeds: `c30`
- `s27` — `reachy/behavior/intents.py _enforce_inhibitions`: `set_inhibition` from the spool can name feel-alive independently of the lock, so the edge re-seed remains necessary even once the lock stops inhibiting
  - seeds: `c31`
- `s28` — `challenge pass / adjacent-systems lens: reachy/behavior/sense.py Sense fields vs reachy/export/runtime.py payload`: the payload has no age key (grep 'age' in the emit dict = 0) while the equality key has two per-tick ages — the issue's diagnosis (`pat_state` timestamps) is one of three per-tick clock fields; this also explains the ~tick-rate snapshot spam measured in #179 whenever a face is visible
  - seeds: `c36`
- `s29` — `challenge pass / observability lens: reachy/behavior/sense_availability.py write-on-change + _report, and the retained reachy/state/senses mirror`: the block's write and log policy are equality on the whole entry; decision q3's '`last_frame_age_s`' as written would defeat both; publishing the timestamp instead keeps the policy intact
  - seeds: `c37`
- `s30` — `challenge pass / overlooked-lifecycle lens: reachy/cli/_commands/behavior.py cmd_run + library.resolve_lifetime + engine.stop(target)`: a by-name stop removes the base ActiveBehavior but leaves its id in `_base_ids`; nothing today can re-create an `is_base`=True behavior after start; the decision's 'until run feel-alive' has no implementing verb yet
  - seeds: `c38`
- `s31` — `challenge pass / concurrency lens: reachy/cli/_commands/behavior.py _HolderKeeper + reachy/robot/media_client.py + reachy/behavior/audio_pump.py`: the holders are documented not thread-safe; the keeper's whole safety case is inert-when-disconnected; a keeper that re-acquires a live client races the pump's audio() and the tick's frame()
  - seeds: `c39`
- `s32` — `challenge pass / failure-mode lens: face_sense._check_stream_staleness exemptions + media_client single session`: the staleness latch already exempts never-seen cameras; the audio consumers all hang off the same client, so a camera-motivated drop is an audio event too
  - seeds: `c40`
- `s33` — `challenge pass / operations-and-reversibility lens: #176 comment's wireplumber.conf.d fix`: the fix is session-wide by construction (it disables the monitors, it does not exclude one device); reversible; not yet written anywhere in the repo
  - seeds: `c41`
- `s34` — `challenge pass / hidden-dependency lens: reachy/behavior/face_sense.py FaceObservation + _worker_tick`: the age provider anchors on publish time; the incremental aim's ring lookup depends on capture time; unmeasured detect latency on the CM4 is the gap
  - seeds: `c42`
- `s35` — `challenge pass / overlooked-actors lens: face_lock.py lock_face response + reachy-nova PR #26 (not read; out of repo)`: the harness side was not read; the response shape is stable, the contents change
  - seeds: `c43`
- `s36` — `challenge pass / security lens: reachy/embody/tools.py + intents spool`: no new agent-facing tool or kind is added; `fov_h`/`fov_v`/damping are Params validated by `validate_param_value`; the closed action set is unchanged — clean pass
- `s37` — `challenge pass / adjacent-systems lens: reachy/behavior/pat_sense.py ownership gate`: pat detection already suspends while a non-base behavior owns the head; a lock claiming `body_yaw` too changes nothing for the pat gate — clean pass
- `s38` — `challenge pass / lifecycle lens: engine.stop('all') during a held lock`: stop all keeps `_base_ids` and evicts face-lock; the eviction watchdog releases; with feel-alive never lock-evicted the post-stop state is simply the base layer — simpler than today, clean pass
- `s39` — `challenge pass / migration lens: tests/test_zero_llm_boundary.py + deployed branch wireless-motor-enable`: no schema or store changes; state.json keys are additive; the deployed branch carries motor-enable + enroll commits not on main, so each fix rebases there — recorded in c23, no new finding

## Decisions

- \#183: re-seed the base layer on the edge where an inhibition naming feel-alive CLEARS (a TickContext capability the intent driver calls, seam a), never on a by-name stop; engine status reports a by-name-stopped base layer as 'base layer stopped' until 'run feel-alive' or a restart
- \#176 ask 2: `sense_availability.py`'s senses block gains per-sense liveness beside the structural verdict — `last_frame_age_s` (null when never seen) and live (age under a threshold) — fed from `face_sense`'s `_last_frame_at`; available keeps its #120b structural meaning; `frame_available` stays the per-tick source of truth and camera-stream-ended stays the event
- \#181: incremental aim damping default is 0.7 — the face centres in two detection cycles with no overshoot; 1.0 is reachable via the param
- \#183: seam (a), edge-triggered. The bug is eviction BY INHIBITION, so the base layer is re-seeded exactly when an inhibition naming feel-alive CLEARS, and only then. A by-name 'behavior stop feel-alive' is intentional stillness (an operator or a rule asking for a still robot): it holds until 'run feel-alive' or an engine restart, and engine status reports it as base layer stopped. The harness revive (reachy-nova PR #26) cannot tell the two apart and is removed by the peer once the runtime carrying #183 is on the robot; until then a by-name stop is treated as unavailable.
- \#176: add liveness to the senses block, as a DIFFERENT fact from availability (availability is composition, liveness is a reading): per sense, `last_frame_age_s` (null when never seen) plus live (age under a threshold). `frame_available` on the per-tick snapshot stays the source of truth; the camera-stream-ended drop stays the human-readable event. The value is also what the runtime ACTS on: re-acquire the media when live stays false for some seconds, not only after a reported GStreamer error. The peer reads the block for status and keeps its MQTT watch as the event source.
- \#183: un-stop is option (a) — 'behavior run feel-alive' with no lifetime flag re-seeds the base layer proper (`is_base`=True, recorded in `_base_ids`); with --duration it stays an ordinary bounded run. Over the spool, `run_behavior` feel-alive with no lifetime means the same re-seed — the unbounded-lifetime refusal is carved out for `BASE_LAYER_NAME` only and stays for every other looping-default entry. No 'behavior base' noun for now (user, 2026-09-06).

## Open parks

- [unknown_nonblocking] \#181: the Lite's camera FOV is unmeasured — the `fov_h`/`fov_v` defaults are Wireless numbers until /api/camera/specs is read on the Lite (localhost:8000 on the dev box)
- [unknown_nonblocking] \#179: the camera-blur settle after a slew stops is unmeasured; `DEFAULT_TAIL_S` (0.25 s) is the actuator-noise tail and `pat_sense`'s `still_hold_s` (1.0 s) is the servo-settle tail — the face gate's settle needs its own number
- [unknown_nonblocking] \#183/#181 (challenge, reversibility lens): on release the head and `body_yaw` channels hand over from the lock's held pose to feel-alive's CURRENT wander value with no easing (`_compose_pose` takes the owner's contribution directly, engine.py:349) — a jump bounded by the amplitudes (`gaze_yaw` 12 deg, `body_yaw` 6 deg at energy 1.0; feel-alive's clock keeps running under the lock now that it is not evicted). Today's release also jumps (re-seed at t=0), so the class is unchanged, but the magnitude may differ; measure on the box and decide whether the lock should ease out

## Resolved vagueness

- [unknown_blocking] \#176: whether a silent pipeline death is recoverable by `warm_up`() in-process (versus needing a daemon media re-acquire) is unprobed — the live recurrence recovered on a RUNTIME restart, which suggests yes, but no in-process retry has been tried — resolved: decision q3: the runtime re-acquires media on sustained live=false; whether an in-process `warm_up` recovers a silent pipeline death is measured on the Wireless as the first acceptance step of that task, with a daemon media re-acquire as the fallback if it does not
