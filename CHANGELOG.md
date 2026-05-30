# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
