# reachy-mini-cli

Agent and CLI for operating the Reachy Mini expressive robot — device setup, app management, and runtime ops.

## Install

```bash
# Recommended — real mode (local robot: daemon binary + SDK):
uv tool install 'reachy-mini-cli[daemon]'
# or with pip:  pip install 'reachy-mini-cli[daemon]'

# HTTP-only remote profile (no local robot; talk to a daemon elsewhere):
uv tool install reachy-mini-cli
```

The installed command is `reachy-mini-cli` (short alias: `reachy`). Then:

```bash
reachy-mini-cli quickstart     # copy-paste install + start-real-mode sequence
reachy-mini-cli daemon start   # bring the local daemon up (wakes the robot)
reachy-mini-cli device status  # verify it answers
reachy-mini-cli listen run     # orient the head toward sound (Ctrl-C to stop)
```

See [Install profiles](#install-profiles) below for why `reachy-mini` is an extra.

## What you get

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`).
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  prompt file (`CLAUDE.md` for `backend: claude`).
- **The canonical guildmaster skill kit** (11 skills) under `.claude/skills/`,
  vendored cite-don't-import. See [`docs/skill-sources.md`](docs/skill-sources.md).
- **A build + deploy baseline** — pytest, lint, the agent-first rubric gate, and
  PyPI Trusted Publishing wired into GitHub Actions.

## Developer quickstart

For working on the repo itself (an editable checkout, not an end-user install):

```bash
uv sync --extra daemon                # recommended — SDK + the reachy-mini-daemon binary
uv sync                               # bare — numpy only; HTTP remote profile (--transport http)
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

`listen` is **SDK-first** — the `sdk` transport is its default, giving direct
in-process access to the mic array (real DoA + RMS loudness via `reachy_mini`).
`numpy` is a base dependency (the RMS detector; a pure wheel). `reachy-mini` itself
stays an **extra**, not a base dep, because its transitive stack (pycairo /
gstreamer / pyaudio) needs system libraries a bare box / CI lack — so the
recommended install bundles it via an extra.

- **Recommended — with the SDK + daemon:** `uv tool install 'reachy-mini-cli[daemon]'`
  (or `pip install 'reachy-mini-cli[daemon]'` / `[sdk]`). Pulls `reachy-mini`, so the
  `sdk` transport `listen` defaults to works out of the box and `reachy daemon start`
  can bring the daemon up locally.
- **Bare — HTTP remote profile:** `pip install reachy-mini-cli` (no extra). `numpy`-only;
  use `--transport http` (or `REACHY_TRANSPORT=http`) with `--base-url` /
  `REACHY_BASE_URL` to talk to a daemon running elsewhere via its REST API. Running
  the `sdk` transport here exits `2` with a hint to install the `[sdk]` extra.

> `reachy-cli` still works as a transitional alias (`pip install reachy-cli`) — it
> just pulls in `reachy-mini-cli`. Either package installs the same tool, and the
> command is `reachy` (or `reachy-mini-cli`).

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

The `device`, `app`, `move`, and `listen` verbs talk to the robot through a
selectable **transport flavor**:

- **`http`** — the Reachy daemon's REST API. Uses only the Python standard library.
  Point it at a daemon with `--base-url` or `REACHY_BASE_URL` (default
  `http://localhost:8000`). This is the default for `device`, `app`, and `move`.
- **`sdk`** — the in-process `reachy_mini` client (included in the base install).
  Covers motion/state and live audio streaming. **This is the default for `listen`**:
  it streams mic audio directly in-process for real DoA + RMS loudness. Daemon and
  app verbs still require `http`.

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

### Demo mode — make the robot feel alive

`demo-mode` runs a continuous loop that streams gentle idle motion to the robot —
a slow breathing oscillation, the occasional glance to a new gaze target, and a
little antenna sway — so an idle robot looks present rather than frozen. It drives
the robot through the same transport, so it needs a running daemon. It is built to
run always-on and improve over time, so it has three layers.

**Process** (tracked background loop; PID + log under `$REACHY_STATE_DIR` /
`$XDG_STATE_HOME/reachy`):

| Verb | What it does |
|------|--------------|
| `demo-mode start` | Spawn the feel-alive loop in the background (idempotent; preflights the daemon first). |
| `demo-mode stop` | Stop the loop this CLI started (SIGTERM eases the robot back to neutral, then SIGKILL). |
| `demo-mode restart` | Apply an update — restart the service if active, else relaunch the background loop. |
| `demo-mode status` | Loop process state + systemd unit state + whether the daemon answers. |
| `demo-mode run` | Run the loop in the foreground (what `start`/the service launch); Ctrl-C to stop. |

**Config** — persisted tuning at `$XDG_CONFIG_HOME/reachy/demo-mode.json`, read by
`run`/`start` (CLI flags override per-invocation):

```bash
uv run reachy demo-mode config                       # show resolved config
uv run reachy demo-mode config --init                # write defaults
uv run reachy demo-mode config --set energy=0.8 interval=3
```

Keys: `transport`, `base_url`, `timeout`, `interval` (tempo), `energy`
(liveliness `0`–`n`), `interpolation`, `seed`, `wake`, `settle`.

**Service** — run it always-on as a systemd `--user` unit (auto-restart on crash,
start on boot):

```bash
uv run reachy demo-mode install      # write the reachy-demo-mode.service unit
uv run reachy demo-mode enable       # enable --now + linger (survives reboot)
uv run reachy demo-mode disable      # stop + disable
uv run reachy demo-mode uninstall    # remove the unit
```

The full flow:

```bash
uv run reachy daemon start                    # something for the loop to drive
uv run reachy demo-mode config --set energy=0.7
uv run reachy demo-mode start                 # robot starts feeling alive
uv run reachy demo-mode restart               # apply config/code updates
uv run reachy demo-mode stop                  # eases back to neutral
```

As you make the motion richer over time, edit `reachy/alive.py` (or the config)
and `demo-mode restart` to apply it.

### Behaviors — compose motion on a 50 Hz engine

`behavior` runs a 50 Hz engine that **composes** named behaviors on a per-channel
contention model (`head` / `antennas` / `body_yaw`), with `feel-alive` as a passive
base layer. Push behaviors onto the running engine from separate commands:

```bash
uv run reachy behavior engine start            # bring the 50 Hz loop up
uv run reachy behavior run speak --duration 8  # head bobs like speech
uv run reachy behavior status --json
uv run reachy behavior stop all                # keeps the feel-alive base layer
```

See `reachy explain behavior` for the full catalog, channels, and contention model.

### Listen — two-tier sound orienting (SDK-first)

`listen` is **SDK-first** and **two-tier**: it streams live mic audio from the
`reachy_mini` SDK in-process (the `sdk` transport is the default), giving it real
Direction of Arrival and real RMS loudness without polling the daemon.

**Tier 1 — antenna lean:** On every tick, the antennas lean toward the live DoA
(head holds). This gives the robot a subtle "perked ear" reaction to any live sound.

**Tier 2 — head→body turn:** On detected *speech* or a loud RMS "snap" transient
(a sudden noise spike detected by `SnapDetector`), the robot executes a smooth
head→body turn. The head turns first; if the source is beyond `--head-only-band`
degrees from center, the body rotates to face it and the head re-centers. A
latched-DoA guard prevents stale angles from firing a spurious turn at rest.

Both tiers drive the daemon's smooth minjerk `goto` planner through a serial motion
queue, so turns are soft and never conflict. The HTTP transport is available via
`--transport http` / `REACHY_TRANSPORT=http` for a control box connecting remotely.

```bash
uv run reachy daemon start                          # bring the daemon up
uv run reachy listen run                             # foreground, SDK transport (default)
uv run reachy listen run --transport http            # foreground, HTTP transport
uv run reachy listen start                           # background, tracked process
uv run reachy listen status --json
uv run reachy listen stop                            # eases back to center
```

Key tuning flags: `--gain`, `--deadband`, `--hold`, `--speed`, `--recenter-after`,
`--speech-only` (Tier-2 speech only; Tier-1 still runs), `--antenna-max`,
`--body-yaw-max`, `--head-only-band`, `--snap-ratio`, `--snap-floor`.
See `reachy explain listen` for the full reference.

### Vision — pixel-based motion + light orienting

`vision` is the camera counterpart to `listen`: it reads frames from the robot's
camera and orients the head toward the strongest visual event. The camera was
previously unused by the CLI — this is a **net-new perception channel**.

**Pixel-based; no ML and no GPU.** Two detectors run in pure pixel math on every
frame:

- **Motion (primary cue) — frame differencing:** the centroid of pixel change
  between consecutive frames maps to a head-yaw offset.
- **Light (fallback cue) — brightness/centroid:** when no motion fires, the
  weighted brightness centroid of the frame drives a softer look toward the bright
  region.

Like `listen`, `vision` **mirrors the serial motion queue**: both detectors drive the
daemon's smooth minjerk `goto` planner one move at a time, so turns are soft and
never conflict.

**Local-profile only.** Frames come via the in-process `reachy_mini` SDK (the `sdk`
transport is the default); the `http` transport gives camera-metadata-only access
(`vision specs`). The `[sdk]` / `[daemon]` extra is required for `vision run` and the
background process.

```bash
reachy-mini-cli vision specs         # check camera metadata (remote-safe)
reachy-mini-cli daemon start         # bring the daemon up
reachy-mini-cli vision run           # foreground, SDK transport (default)
reachy-mini-cli vision start         # background, tracked process
reachy-mini-cli vision status --json
reachy-mini-cli vision stop          # eases back to center
```

Key tuning flags: `--gain`, `--max-yaw`, `--deadband`, `--hold`, `--speed`,
`--motion-threshold`.
See `reachy explain vision` for the full reference.

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
