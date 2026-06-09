# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.11.0] - 2026-06-10

### Added

- `quickstart` verb — prints the copy-paste install + start-real-mode sequence in text or `--json`, available on any install profile (no daemon needed); resolvable via `explain quickstart`.

### Changed

- Front-door text now describes the Reachy Mini robot CLI instead of the cloned agent template: the `--help` description + a getting-started epilog pointing at `quickstart`/`learn`, the `learn` purpose paragraph + an Install block, and the `explain` root entry.
- README now leads with `uv tool install 'reachy-mini-cli[daemon]'` as the primary install path and relabels the old Quickstart as the Developer quickstart.

### Fixed

- `explain` root listed the robot nouns but omitted `listen` — added it.

## [0.10.0] - 2026-06-06

### Added

- `listen` is now always-alive: between sounds the robot keeps gently breathing,
  gaze-wandering, and swaying its antennas around its *current* heading instead of
  freezing. New `--idle-energy` (0 disables, restoring hold-still) and `--drift-speed`
  knobs, threaded through the background supervisor.

### Changed

- After turning toward a sound, `listen` now *stays rotated* and keeps the idle motion
  around that heading, then drifts the head+body slowly home over `--recenter-after`
  seconds rather than hard-snapping back to front. The hold window no longer freezes the
  robot — the idle layer keeps it alive even right after a turn. The shared idle-pose
  generator (`AliveConfig`/`next_pose`) moved to `reachy/motion/idle.py` (re-exported
  from `reachy.alive` for back-compat).

### Fixed

- `listen` right-antenna lean direction: the right antenna now perks toward a right-side
  sound instead of leaning the wrong way (its joint sign is mirrored from the left).

## [0.9.0] - 2026-06-06

### Added

- `reachy-cli` is published as a transitional alias distribution
  (`packaging/reachy-cli/`, metadata-only) that depends on `reachy-mini-cli` at the
  matching version and forwards the `[daemon]`/`[sdk]` extras — `pip install
  reachy-cli` keeps working. The publish workflow now builds and publishes both
  names via Trusted Publishing.

### Changed

- Renamed the distribution to `reachy-mini-cli` (canonical PyPI name); the console
  command is now installed as both `reachy` and `reachy-mini-cli`. The import
  package stays `reachy`. `__version__` now reads the `reachy-mini-cli` metadata,
  and all install hints/docs point at the new name.

## [0.8.0] - 2026-06-06

### Added

- Two-tier `reachy listen`: Tier-1 near-side antenna lean toward faint sound; Tier-2 head->body "turn to see" on detected speech or a loud RMS snap transient.
- Real mic loudness via a `SnapDetector` (RMS spike, algorithm cited from reachy_nova `detect_snap`), fed from the SDK `media_session()` audio stream.
- SDK-based daemon/robot liveness (`is_robot_live`) that stays correct across a daemon restart (#21).
- New `listen` tuning flags: --antenna-gain/--antenna-max/--body-yaw-max/--body-speed/--head-only-band/--snap-ratio/--snap-floor.
- `ANTENNA_KEY` coalesce key in the motion queue so antenna leans coalesce independently of head moves.

### Changed

- `listen` is now SDK-first: the SDK transport is listen's default (real DoA + mic loudness in-process), with `numpy` as a base dependency for the RMS detector. `reachy-mini` stays a `[sdk]`/`[daemon]` extra (its cairo/gstreamer stack can't be a base dep without breaking bare/CI installs); running the `sdk` transport without it gives a clean exit-2 hint. The HTTP transport remains an optional remote profile via `--transport http`.
- Latched-DoA guard: a head turn fires only on live speech/snap, never on a frozen DoA angle (the daemon latches the last direction at rest).

## [0.7.0] - 2026-06-06

### Added

- `listen` noun group — a standalone, smooth sound-orienting loop. Reads the mic array's Direction of Arrival (DoA) from the daemon and turns the head toward a *sustained, off-axis* sound (deadband + dwell), holds there briefly, then eases back to center after silence. Verbs: `run`, `start`, `stop`, `restart`, `status`, `overview` — each with `--json`; tune the feel with `--dwell` / `--hold` / `--speed` / `--deadband` / `--gain` / `--recenter-after` / `--speech-only`. Process-managed like `demo-mode` (PID + log under the state dir). Degrades gracefully: no mic / no daemon DoA ⇒ no reaction, no crash.
- `reachy/motion/` — a serial motion subsystem: a coalescing `MotionQueue`, an executor that runs interpolated daemon `goto` moves strictly one at a time (never overlapping or resetting each other), and the `ListenProducer` (the DoA→look decision). The smooth trajectory is the daemon's minjerk planner.

### Changed

- Sound-orienting now drives the daemon's smooth minjerk `goto` planner via `reachy listen`, instead of the behavior engine's immediate `set_target` stream (jerky for big reorienting turns).
- HTTP transport maps the CLI's `--interpolation ease` to the daemon's `ease_in_out` (the daemon rejected `ease` with HTTP 422), matching the SDK transport.

### Removed

- The `listen` **behavior** (the PR #20 `behavior run listen`, a 50 Hz `set_target` streamer) — superseded by the `reachy listen` loop above. The engine keeps its general sensor-input capability (`wants_sense`, abstention, DoA in `behavior status`), but ships no built-in sensor behavior.

## [0.6.0] - 2026-06-06

### Added

- behavior run listen — sound-reactive behavior that orients the head (and optionally the body, via --set body_gain) toward the sound Direction of Arrival read from the daemon; reacts to any sound by default (--set speech_only=1 for speech only), and degrades gracefully when the mic is unavailable
- reachy/behavior/sense.py: Sense snapshot, DoaPoller (throttled, error-tolerant DoA reader), and HttpTransport.doa() over GET /api/state/doa

### Changed

- Behavior contribution signature is now fn(t, params, sense); the engine arbitration is abstention-aware — a behavior that returns None for a claimed channel yields it to the next-priority claimant, so listen falls back to feel-alive when there is no sound
- behavior status now reports the live (resolved) per-channel ownership plus the latest DoA snapshot

## [0.5.0] - 2026-06-05

### Added

- `behavior` noun group: compose robot behaviors on a persistent 50 Hz loop. Push one-shot ("look up-and-aside, hold 5s") or looping ("speak: bob the head for N seconds or until stopped") behaviors onto a running engine; a per-channel contention model decides who drives `head` / `antennas` / `body_yaw` when they conflict. Verbs: `list`, `run`, `stop`, `status`, `engine start|stop|status|run`, `overview` — each with `--json`.
- Four-class contention model (`passive` / `stoppable` / `unstoppable` / `stopping`): a `stopping` behavior evicts the `stoppable` ones on its channels, an `unstoppable` holds its channels until it finishes, and the `passive` base layer only drives a channel nothing else claims. Same-channel conflicts resolve by class priority, then most-recent.
- `feel-alive` runs as a **passive base layer** (default on; `--no-base-layer` to disable) — a continuous idle motion (breathing, slow gaze wander, antenna sway), so an idle robot stays alive on any channel no behavior has taken. This generalizes `demo-mode`; the existing `demo-mode` noun is unchanged (migration is future work).
- `reachy.behavior` package: `model` (channels, classes, lifetimes, the pure `Behavior`), `arbitration` (the pure `arbitrate`/`admit` core), `library` (built-in parametric behaviors: gaze-hold, nod, shake, speak, thoughtful, antenna-sway, body-turn-hold, feel-alive), `engine` (the 50 Hz compose loop), `control` (a command-spool + state-file IPC under the state dir), and `supervisor` (a PID-file process manager). Stdlib only — no new base runtime dependency.
- Immediate-target streaming on the transport: `Transport.set_target(...)` (`POST /api/move/set_target` for http; `ReachyMini.set_target` for sdk) and a `streaming()` session that holds one robot connection open for the whole loop — so the 50 Hz stream pays the open/close cost once, not per pose.

### Changed

- Extracted the signal-stoppable / interruptible-sleep loop helpers from `reachy.alive` into a shared `reachy.looputil` (used by both demo-mode and the behavior engine), with a configurable sleep slice for high-rate loops.

### Notes

- While the engine runs it streams immediate targets and **owns robot motion exclusively** — don't drive the robot with `move goto` / `demo-mode` at the same time (the daemon ignores `set_target` while an interpolated move is running).

## [0.4.0] - 2026-05-30

### Added

- `demo-mode` noun group: a continuously-running, managed loop that makes the Reachy Mini *feel alive* with gentle idle motion (breathing oscillation, occasional glances, antenna sway). Verbs: start/stop/restart/status/run, config, install/enable/disable/uninstall, overview — each with --json.
- `reachy.alive` module: pure idle-motion generator (`next_pose`, `AliveConfig`, `neutral_pose`), a signal-clean foreground `run_loop` that tolerates transient daemon errors and eases the robot to neutral on stop, and a PID-file process supervisor (start/stop/restart/status) mirroring `reachy.daemon` — stdlib only, no new base runtime dependency.
- `reachy.demo_config` — persisted JSON tuning at `$XDG_CONFIG_HOME/reachy/demo-mode.json`, read by `run`/`start` (CLI flags override; precedence flag > config > default). `demo-mode config [--init] [--set key=value …]` shows/scaffolds/sets it.
- `reachy.demo_service` — systemd `--user` supervision so the loop runs always-on (auto-restart on crash, start on boot via linger): `demo-mode install/enable/disable/uninstall`. Stdlib-only `systemctl`/`loginctl` (graceful exit-2 when absent).
- `demo-mode restart` applies an update: restarts the systemd service if active, else relaunches the background loop — re-importing the latest motion code and re-reading config.
- Motion tuning: --interval (tempo), --energy (liveliness multiplier), --interpolation, --seed (reproducible idle motion).

## [0.3.0] - 2026-05-30

### Added

- `daemon` noun group (`start`/`stop`/`status`/`overview`): bring the local `reachy-mini-daemon` process up and down — background spawn + PID/log under `$XDG_STATE_HOME/reachy`, health-poll on `GET /api/daemon/status`, idempotent start, SIGTERM-then-SIGKILL stop.
- `reachy/daemon.py` — stdlib-only daemon process-lifecycle module (no new runtime dependency).
- `[daemon]` optional-dependencies extra (`reachy-mini>=1.0`) — the recommended default install, providing the `reachy-mini-daemon` binary.

### Changed

- Inverted the install model: `pip install 'reachy-cli[daemon]'` is now the default (bundles the daemon); the bare `pip install reachy-cli` is the HTTP-only *remote* profile. Base stays zero-runtime-deps.
- The `http` transport's daemon-unreachable hint now points at `reachy daemon start` and the `[daemon]` install.
- README + CLAUDE.md document the daemon noun, the install profiles, and the daemon-up wake-up flow.

## [0.2.0] - 2026-05-30

### Added

- `device` noun group: `status` (daemon status), `state` (live robot state)
- `app` noun group: `list`, `status`, `start <name>`, `stop`
- `move` noun group: `goto` (mm + degrees; `--antennas`/`--body-yaw`/`--duration`/`--interpolation`), `wake`, `sleep`
- Robot transport layer with two selectable flavors: `http` (stdlib-only daemon REST client, default) and `sdk` (optional `reachy_mini` client behind the `[sdk]` extra), via `--transport` / `REACHY_TRANSPORT`
- `explain` catalog entries and `overview`/`learn` command maps for the new robot nouns

### Changed

- README documents robot operations, transports, and the [sdk] optional extra

## [0.1.2] - 2026-05-30

### Changed

- Replaced the CLAUDE.md bootstrap seed with a full runtime prompt (ran /init): documents the agent-first CLI architecture, the verb/noun registration pattern, the structured-error and stdout/stderr contracts, the zero-runtime-dependency and version-bump-every-PR constraints, and flags that the repo is still the unmodified culture-agent-template clone (no Reachy robot functionality yet) plus the reachy vs reachy-mini-cli console-script naming drift.

### Fixed

- Added a `reachy` (console-script name) entry to the explain catalog so `explain reachy` resolves. The agent-first rubric's `explain_self` check derives the tool name from `[project.scripts]` (`reachy`), which the `reachy-mini-cli`-keyed catalog did not cover — the `lint` job's rubric gate failed on it. Does not touch the broader `reachy` vs `reachy-mini-cli` display-name drift (still documented in CLAUDE.md as a deferred decision).
- Re-synced uv.lock with pyproject.toml — the lockfile still carried a stale reachy-mini-cli editable package entry; it now matches the actual distribution name reachy-cli.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/reachy-mini-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/reachy-mini-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: reachy-mini-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
