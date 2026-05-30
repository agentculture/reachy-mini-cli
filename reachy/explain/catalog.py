"""Markdown catalog for ``reachy-mini-cli explain <path>``.

Each entry is verbatim markdown. Keys are command-path tuples. The empty tuple,
``("reachy",)`` (the installed console-script name, from ``[project.scripts]``),
and ``("reachy-mini-cli",)`` (the display name used throughout the help text)
all resolve to the root entry. The agent-first rubric's ``explain_self`` check
runs ``explain <script-name>``, so the ``("reachy",)`` key is load-bearing.

Keep bodies self-contained: an agent reading one entry should get enough
context without chaining reads.
"""

from __future__ import annotations

_ROOT = """\
# reachy-mini-cli

A clonable template for AgentCulture mesh agents. It carries an agent-first CLI
(cited from the teken `python-cli` reference), a mesh identity (`culture.yaml` +
`CLAUDE.md`), the canonical guildmaster skill kit under `.claude/skills/`, and a
buildable/deployable package baseline. Clone it, rename the package, edit
`culture.yaml`, and you have a new agent.

## Verbs

- `reachy-mini-cli whoami` — identity probe from `culture.yaml`.
- `reachy-mini-cli learn` — structured self-teaching prompt.
- `reachy-mini-cli explain <path>` — markdown docs for any noun/verb.
- `reachy-mini-cli overview` — descriptive snapshot of the agent.
- `reachy-mini-cli doctor` — check the agent-identity invariants.
- `reachy-mini-cli cli overview` — describe the CLI surface.

## Robot nouns

- `reachy-mini-cli daemon <verb>` — start/stop/check the local daemon process.
- `reachy-mini-cli device <verb>` — daemon/robot status and live state.
- `reachy-mini-cli app <verb>` — list/start/stop Reachy Mini apps.
- `reachy-mini-cli move <verb>` — runtime motion (goto, wake, sleep).

The `device`/`app`/`move` verbs speak to the Reachy daemon over a transport
flavor (`--transport http` by default, `sdk` optional); a missing daemon yields a
clean exit-2 error, never a traceback. `daemon` is the other half — it brings the
local `reachy-mini-daemon` process up so those verbs have something to talk to.

## Exit-code policy

- `0` success
- `1` user-input error
- `2` environment / setup error
- `3+` reserved

## See also

- `reachy-mini-cli explain whoami`
- `reachy-mini-cli explain doctor`
"""

_WHOAMI = """\
# reachy-mini-cli whoami

Reports the agent's identity from `culture.yaml`: nick (`suffix`), backend,
served model, and the package version. Read-only.

## Usage

    reachy-mini-cli whoami
    reachy-mini-cli whoami --json
"""

_LEARN = """\
# reachy-mini-cli learn

Prints a structured self-teaching prompt covering purpose, command map,
exit-code policy, `--json` support, and the `explain` pointer.

## Usage

    reachy-mini-cli learn
    reachy-mini-cli learn --json
"""

_EXPLAIN = """\
# reachy-mini-cli explain <path>

Prints markdown documentation for any noun/verb path. Unlike `--help` (terse,
positional), `explain` is global and addressable by path.

## Usage

    reachy-mini-cli explain reachy-mini-cli
    reachy-mini-cli explain whoami
    reachy-mini-cli explain --json <path>
"""

_OVERVIEW = """\
# reachy-mini-cli overview

Read-only descriptive snapshot of the agent: identity (from `culture.yaml`), the
verb surface, and the sibling-pattern artifacts the template carries. Accepts an
ignored `target` so a stray path never hard-fails.

## Usage

    reachy-mini-cli overview
    reachy-mini-cli overview --json
"""

_DOCTOR = """\
# reachy-mini-cli doctor

Checks the agent-identity invariants `steward doctor` verifies:
prompt-file-present and backend-consistency (`claude` → `CLAUDE.md`), plus a
skills-present check. Exits 1 when unhealthy.

## Usage

    reachy-mini-cli doctor
    reachy-mini-cli doctor --json
"""

_CLI = """\
# reachy-mini-cli cli

Noun group for CLI-surface introspection. `cli overview` describes the CLI
itself (distinct from the global `overview`, which describes the agent).

## Usage

    reachy-mini-cli cli overview
    reachy-mini-cli cli overview --json
"""

_TRANSPORTS = """\
## Transports

Robot verbs reach the robot through a selectable flavor:

- `http` (default) — the Reachy daemon's REST API (default
  `http://localhost:8000`, override with `--base-url` or `REACHY_BASE_URL`).
  Uses only the standard library, so the CLI stays dependency-free.
- `sdk` — the in-process `reachy_mini` client; needs the optional `[sdk]` extra
  (`pip install 'reachy-cli[sdk]'`). Covers motion/state; daemon and app verbs
  require `http`.

Select with `--transport {http,sdk}` (or `REACHY_TRANSPORT`). If the daemon is
unreachable, the command exits 2 with an `error:`/`hint:` pair — no traceback.
"""

# Placeholder spliced into each robot-noun body so the shared transport block is
# defined once (see ``_TRANSPORTS``).
_TRANSPORTS_SLOT = "{transports}"

_DEVICE = """\
# reachy-mini-cli device

Device setup and status for the Reachy Mini.

## Verbs

- `reachy-mini-cli device status` — daemon status (state, version,
  wireless/lite, simulation, IP). Calls `GET /api/daemon/status`.
- `reachy-mini-cli device state` — live robot state: head pose, antenna
  positions, body yaw, direction-of-arrival. Calls `GET /api/state/full`.
- `reachy-mini-cli device overview` — this summary.

{transports}

## Usage

    reachy-mini-cli device status
    reachy-mini-cli device state --json
    reachy-mini-cli device status --base-url http://reachy.local:8000
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)

_APP = """\
# reachy-mini-cli app

Manage Reachy Mini apps (daemon-side; requires `--transport http`).

## Verbs

- `reachy-mini-cli app list` — available apps, installed and installable.
  Calls `GET /api/apps/list-available`.
- `reachy-mini-cli app status` — the currently running app, if any.
- `reachy-mini-cli app start <name>` — start an installed app by name.
- `reachy-mini-cli app stop` — stop the currently running app.
- `reachy-mini-cli app overview` — this summary.

{transports}

## Usage

    reachy-mini-cli app list
    reachy-mini-cli app start my-app
    reachy-mini-cli app stop --json
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)

_MOVE = """\
# reachy-mini-cli move

Runtime motion. `goto` takes friendly units — millimetres for translation,
degrees for rotation — converted to the daemon's metres + radians.

## Verbs

- `reachy-mini-cli move goto` — move head/antennas to a target. Flags:
  `--x/--y/--z` (mm), `--roll/--pitch/--yaw` (deg), `--antennas RIGHT LEFT`
  (deg), `--body-yaw` (deg), `--duration` (s, default 2.0),
  `--interpolation {minjerk,linear,ease,cartoon}`. Calls `POST /api/move/goto`.
- `reachy-mini-cli move wake` — play the wake-up animation.
- `reachy-mini-cli move sleep` — play the go-to-sleep animation.
- `reachy-mini-cli move overview` — this summary.

{transports}

## Usage

    reachy-mini-cli move goto --z 10 --pitch -5 --duration 2
    reachy-mini-cli move goto --antennas 30 -30 --duration 1
    reachy-mini-cli move wake
    reachy-mini-cli move sleep --json
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)


_DAEMON = """\
# reachy-mini-cli daemon

Local daemon process lifecycle. The `device`/`app`/`move` verbs are *clients* of
a running daemon; this noun is the other half — it brings the local
`reachy-mini-daemon` process up and down.

## Verbs

- `reachy-mini-cli daemon start` — spawn `reachy-mini-daemon` in the background,
  record its PID + log under the state dir, and poll the health route
  (`GET /api/daemon/status`) until it answers. Idempotent: if a daemon already
  runs (tracked or foreign), it reports `already-running` instead of double-spawning.
- `reachy-mini-cli daemon stop` — SIGTERM the daemon this CLI started, escalating
  to SIGKILL if it lingers past `--timeout`.
- `reachy-mini-cli daemon status` — reconcile the tracked process (running /
  stopped / stale pid) with the HTTP health check.
- `reachy-mini-cli daemon overview` — this summary.

## Install

The daemon binary ships in the `[daemon]` extra — the recommended default:

    pip install 'reachy-cli[daemon]'

The bare `pip install reachy-cli` is the HTTP-only *remote* profile (no daemon):
use it on a control box that only talks to a daemon running elsewhere via
`--base-url` / `REACHY_BASE_URL`. If the binary is missing, `daemon start` exits 2
with a hint pointing at the `[daemon]` install.

## Notes

- `reachy-mini-daemon` defaults to `--wake-up-on-start`, so `daemon start` already
  wakes the robot. Forward daemon args after `--`, e.g.
  `reachy-mini-cli daemon start -- --sim --no-wake-up-on-start`.
- Override the launch command with `--daemon-cmd` or `REACHY_DAEMON_CMD`.
- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`
  (`~/.local/state/reachy`): `daemon.pid` + `daemon.log`.

## Usage

    reachy-mini-cli daemon start
    reachy-mini-cli daemon status --json
    reachy-mini-cli daemon start --no-wait -- --sim
    reachy-mini-cli daemon stop
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("reachy",): _ROOT,
    ("reachy-mini-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("learn",): _LEARN,
    ("explain",): _EXPLAIN,
    ("overview",): _OVERVIEW,
    ("doctor",): _DOCTOR,
    ("cli",): _CLI,
    ("cli", "overview"): _CLI,
    ("daemon",): _DAEMON,
    ("daemon", "overview"): _DAEMON,
    ("daemon", "start"): _DAEMON,
    ("daemon", "stop"): _DAEMON,
    ("daemon", "status"): _DAEMON,
    ("device",): _DEVICE,
    ("device", "overview"): _DEVICE,
    ("device", "status"): _DEVICE,
    ("device", "state"): _DEVICE,
    ("app",): _APP,
    ("app", "overview"): _APP,
    ("app", "list"): _APP,
    ("app", "status"): _APP,
    ("app", "start"): _APP,
    ("app", "stop"): _APP,
    ("move",): _MOVE,
    ("move", "overview"): _MOVE,
    ("move", "goto"): _MOVE,
    ("move", "wake"): _MOVE,
    ("move", "sleep"): _MOVE,
}
