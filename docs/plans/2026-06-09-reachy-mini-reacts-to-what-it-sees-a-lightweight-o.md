# Build Plan — Reachy Mini reacts to what it sees: a lightweight on-board vision sense detects motion, light, and nearby objects from the camera in realtime on a Raspberry Pi 4 — simple pixel math, no ML — and feeds those events into the behavior/motion system so the robot orients toward what it sees.

slug: `reachy-mini-reacts-to-what-it-sees-a-lightweight-o` · status: `exported` · from frame: `reachy-mini-reacts-to-what-it-sees-a-lightweight-o`

> Reachy Mini reacts to what it sees: a lightweight on-board vision sense detects motion, light, and nearby objects from the camera in realtime on a Raspberry Pi 4 — simple pixel math, no ML — and feeds those events into the behavior/motion system so the robot orients toward what it sees.

## Tasks

### t1 — Camera frame access in the transport layer

- covers: c7, h10
- acceptance:
  - SdkTransport.get_frame() returns a numpy frame from the local SDK/IPC camera path (is_local_camera_available); a fake SDK in tests yields a frame of the expected shape.
  - HttpTransport exposes camera_specs() via GET /api/camera/specs and raises a clean exit-2 CliError (pointing at the [sdk]/[daemon] extra) when asked for frames.
  - No new base runtime dependency; camera/SDK access stays behind the [sdk]/[daemon] extra; black/isort/flake8/bandit green.

### t2 — Pixel-based motion detector (frame differencing)

- covers: c9, h2
- acceptance:
  - MotionDetector.feed(frame) downsamples to grayscale, abs-diffs vs the previous frame, thresholds, and returns a normalized horizontal direction + magnitude (or None below threshold).
  - A synthetic blob moving left-to-right yields centroids on the correct side; uniform noise below threshold yields no event (tunable threshold).
  - Pure numpy, no ML; tests/test_vision_motion.py passes.

### t3 — Pixel-based light detector (brightness/centroid)

- covers: c10, h3
- acceptance:
  - LightDetector.feed(frame) computes mean luma and the centroid of the brightest thresholded region, returning bright-region direction + a change flag driven by change, not absolute level.
  - A synthetic bright-spot fixture yields its centroid/direction; a uniform exposure shift does NOT fire a change event.
  - Pure numpy; tests/test_vision_light.py passes.

### t4 — VisionProducer loop: detectors -> serial-motion-queue head orient

- depends on: t1, t2, t3
- covers: c1, c8, c14, h1, h5, h11, h12
- acceptance:
  - VisionProducer consumes a frame stream and per tick emits at most one head-orient goto toward the strongest motion/light event via the existing serial motion-queue planner (no concurrent moves), mirroring ListenProducer.
  - A hand-wave fixture orients toward the correct side; with no event + deadband it holds; turns are smooth minjerk gotos serialized with listen/demo-mode.
  - A bounded offline test (fake transport, --max-ticks) runs within a per-tick compute budget consistent with >=10 FPS on a Pi 4; tests/test_vision_producer.py passes.

### t5 — 'vision' CLI noun mirroring 'listen' (run/start/stop/restart/status/specs/overview)

- depends on: t1, t4
- covers: c2, c15, h6
- acceptance:
  - reachy/cli/_commands/vision.py exposes register(sub) wired in reachy/cli/__init__.py; 'vision run' runs the loop foreground (eases head to center), 'vision start/stop/restart/status' manage a tracked background process like listen, 'vision specs' reports camera specs; every verb takes --json.
  - Running the sdk path without a local camera raises a clean exit-2 CliError; 'vision overview' exists so the noun satisfies teken cli doctor --strict; results->stdout, errors->stderr.
  - tests/test_cli_vision.py covers overview/status/specs (exit 0 + --json) and the missing-camera exit-2 path.

### t6 — explain catalog, docs, and release for the vision noun

- depends on: t5
- covers: c4, c5, c6, h7, h8, h9
- acceptance:
  - reachy/explain/catalog.py gains a _VISION entry + ('vision',...) ENTRIES keys; 'reachy explain vision' resolves and test_every_catalog_path_resolves passes; the text states the no-ML/no-GPU and local-profile-only boundaries and the listen-mirroring design.
  - README documents the vision noun (net-new perception, not a duplicate) under Robot operations; CHANGELOG entry added and pyproject version bumped (minor) per version-check CI.
  - Full suite green: pytest -n auto, teken cli doctor . --strict, black/isort/flake8/bandit, markdownlint.

## Risks

- [unknown_nonblocking] Pi 4 realtime feasibility (the c13 assumption) is unverified until measured on real hardware; the loop must hold >=10 FPS with CPU headroom for motion. (task t4)
- [unknown_nonblocking] Exact reachy_mini SDK local-camera frame API (is_local_camera_available / media_manager.camera shape) not yet verified in code; t1 must confirm against the installed SDK. (task t1)
- [follow_up] Proximity / 'close objects' detection deferred to a follow-up (apparent-size/optic-flow); not in v1.
- [follow_up] Behavior-engine Sense-channel integration deferred; v1 vision drives the serial motion queue directly like listen.
