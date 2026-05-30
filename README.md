# reachy-mini-cli

Agent and CLI for operating the Reachy Mini expressive robot — device setup, app management, and runtime ops.

## What you get

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`) — the runtime package has no third-party dependencies.
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  prompt file (`CLAUDE.md` for `backend: claude`).
- **The canonical guildmaster skill kit** (11 skills) under `.claude/skills/`,
  vendored cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

## Quickstart

```bash
uv sync --extra daemon                # default: + the local reachy-mini-daemon
# uv sync                             # remote profile: HTTP-only, no daemon deps
uv run pytest -n auto                 # run the test suite
uv run reachy whoami                  # identity from culture.yaml
uv run reachy learn                   # self-teaching prompt (add --json)
uv run teken cli doctor . --strict    # the agent-first rubric gate CI runs
```

## CLI

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, and model from `culture.yaml`. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## Robot operations

The `daemon`, `device`, `app`, and `move` noun groups operate the Reachy Mini.

### Install profiles

The Reachy daemon (`reachy-mini-daemon`) and the in-process SDK ship in
`reachy-mini`. Choose your install by where the daemon runs:

- **Default — with the daemon:** `pip install 'reachy-cli[daemon]'`. Bundles
  `reachy-mini`, so `reachy daemon start` can bring the daemon up locally. This
  is the profile for a machine with a robot attached.
- **Remote — without the daemon:** `pip install reachy-cli` (bare). The base
  install keeps **zero runtime dependencies** (the `http` transport and the
  `daemon status`/`stop` verbs use only the stdlib). Use it on a control box that
  only talks to a daemon running elsewhere via `--base-url` / `REACHY_BASE_URL`.
  `daemon start` here exits `2` with a hint to install the `[daemon]` extra.

`[sdk]` (also `reachy-mini`) adds the in-process `--transport sdk` client.

### Bring the daemon up

`device`/`app`/`move` are clients of a running daemon; `daemon` is the other
half — it manages the local `reachy-mini-daemon` process.

| Verb | What it does |
|------|--------------|
| `daemon start` | Spawn `reachy-mini-daemon` in the background, then poll its health route until ready. Idempotent. |
| `daemon stop` | Stop the daemon this CLI started (SIGTERM, then SIGKILL). |
| `daemon status` | Reconcile the tracked process (running/stopped/stale) with the HTTP health check. |

`reachy-mini-daemon` defaults to `--wake-up-on-start`, so `daemon start` already
wakes the robot. Forward daemon args after `--`, e.g.
`reachy daemon start -- --sim --no-wake-up-on-start`. State (PID + log) lives
under `$XDG_STATE_HOME/reachy` (`~/.local/state/reachy`).

### Transports

The `device`, `app`, and `move` verbs talk to a running daemon through a
selectable **transport flavor**:

- **`http`** (default) — the Reachy daemon's REST API. Uses only the Python
  standard library, so the default install keeps **zero runtime dependencies**.
  Point it at a daemon with `--base-url` or `REACHY_BASE_URL` (default
  `http://localhost:8000`).
- **`sdk`** — the in-process `reachy_mini` client. Install the optional extra:
  `pip install 'reachy-cli[sdk]'`. Covers motion/state; daemon and app verbs
  still require `http`.

Select per command with `--transport {http,sdk}` (or `REACHY_TRANSPORT`). Action
verbs also accept `--timeout`. If no daemon is reachable, the command exits `2`
with a clean `error:`/`hint:` pair — never a traceback.

| Verb | What it does |
|------|--------------|
| `device status` | Daemon status: state, version, wireless/lite, simulation, IP. |
| `device state` | Live robot state: head pose, antenna positions, body yaw. |
| `app list` | Available apps (installed and installable). |
| `app status` | The currently running app, if any. |
| `app start <name>` | Start an installed app by name. |
| `app stop` | Stop the currently running app. |
| `move goto` | Move head/antennas (mm + degrees); see `reachy explain move` for flags. |
| `move wake` | Play the wake-up animation. |
| `move sleep` | Play the go-to-sleep animation. |

Each noun also exposes `overview` (e.g. `reachy move overview`).

```bash
uv run reachy daemon start            # bring the local daemon up (and wake the robot)
uv run reachy device status           # now answers instead of exit-2
uv run reachy app list --json
uv run reachy move goto --z 10 --pitch -5 --duration 2
uv run reachy move wake
uv run reachy daemon stop             # put it back down when you're done
```

## Make it your own

1. Rename the package `reachy/` and the `reachy-mini-cli`
   CLI/dist name throughout `pyproject.toml`, the package, `tests/`, and
   `sonar-project.properties`.
2. Edit `culture.yaml` with your `suffix` and `backend`.
3. Rewrite `CLAUDE.md` for your agent and run `/init`.
4. Re-vendor only the skills you need from guildmaster (see
   [`docs/skill-sources.md`](docs/skill-sources.md)).

See [`CLAUDE.md`](CLAUDE.md) for the full conventions (version-bump-every-PR,
the `cicd` PR lane, deploy setup).

## License

MIT — see [`LICENSE`](LICENSE).
