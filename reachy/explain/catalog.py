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

CLI and agent for operating the Reachy Mini expressive robot — device setup, app
management, runtime motion, higher-level behaviors, and sound orienting. Commands
talk to the `reachy-mini-daemon` over an HTTP transport (default) or the
in-process `reachy_mini` SDK. Install the daemon binary with the `[daemon]` extra
(`uv tool install 'reachy-mini-cli[daemon]'`), then run `reachy-mini-cli
quickstart` for the install-and-start-real-mode sequence.

## Verbs

- `reachy-mini-cli quickstart` — copy-paste install + start-real-mode steps.
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
- `reachy-mini-cli demo-mode <verb>` — start/stop a background loop that makes
  the robot feel alive (idle breathing, glances, antenna sway).
- `reachy-mini-cli behavior <verb>` — compose behaviors on a 50 Hz loop
  (`list`, `run`, `stop`, `status`, `engine`).
- `reachy-mini-cli listen <verb>` — orient the head toward sound on a two-tier
  SDK-first loop (`run`, `start`, `stop`, `restart`, `status`).

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

_QUICKSTART = """\
# reachy-mini-cli quickstart

Prints the copy-paste sequence to install the CLI and start "real mode" (a live
Reachy Mini with its daemon up), the HTTP-remote profile, and the agent-first
commands that work with no robot attached. Read-only; supports `--json`.

## Real mode — local robot (recommended)

    uv tool install 'reachy-mini-cli[daemon]'   # CLI + daemon binary + SDK
    reachy-mini-cli daemon start                # wakes the robot
    reachy-mini-cli device status               # verify it answers
    reachy-mini-cli listen run                  # orient to sound (Ctrl-C to stop)
    reachy-mini-cli daemon stop                 # when you are done

## Remote / HTTP-only — no local robot

    uv tool install reachy-mini-cli             # numpy-only, no daemon binary
    export REACHY_BASE_URL=http://reachy.local:8000
    reachy-mini-cli device status --transport http

The bare install omits `reachy-mini` (its pycairo/gstreamer/pyaudio stack needs
system libraries a bare box lacks); the `[daemon]` extra adds the daemon binary
and SDK. See `reachy-mini-cli explain daemon` and `reachy-mini-cli explain listen`.

## Usage

    reachy-mini-cli quickstart
    reachy-mini-cli quickstart --json
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
  (`pip install 'reachy-mini-cli[sdk]'`). Covers motion/state; daemon and app verbs
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

    pip install 'reachy-mini-cli[daemon]'

The bare `pip install reachy-mini-cli` is the HTTP-only *remote* profile (no daemon):
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


_DEMO = """\
# reachy-mini-cli demo-mode

Make the robot *feel alive*. A continuously-running loop streams gentle idle
motion to the robot — a slow breathing oscillation, the occasional glance to a
new gaze target, and a little antenna sway — so an otherwise idle robot looks
present rather than frozen. The motion is a stream of `move goto` calls over the
transport, so it needs a running daemon (`reachy-mini-cli daemon start`).

It is meant to run always-on and improve over time, so it has three layers:
a tracked **process** (start/stop/restart), a persisted **config** file, and an
optional systemd `--user` **service**.

## Process verbs

- `reachy-mini-cli demo-mode start` — spawn the loop in the background, recording
  its PID + log under the state dir. For `--transport http` it first preflights
  the daemon's health route so it never spawns a loop with nothing to drive.
  Idempotent: reports `already-running` if a tracked loop is alive.
- `reachy-mini-cli demo-mode stop` — SIGTERM the loop this CLI started (so it
  eases the robot back to neutral before exiting), escalating to SIGKILL past
  `--timeout`.
- `reachy-mini-cli demo-mode restart` — apply an update. If the systemd service
  is active it is restarted; otherwise the background loop is stopped and
  relaunched. Either way the new process re-imports the latest motion code and
  re-reads the config.
- `reachy-mini-cli demo-mode status` — the loop's process state (running /
  stopped / stale), the systemd unit state, and whether the daemon answers.
- `reachy-mini-cli demo-mode run` — run the loop in the foreground (what `start`
  and the service launch). Ctrl-C stops it. `--max-ticks N` runs a fixed number
  of poses.
- `reachy-mini-cli demo-mode overview` — this summary.

## Config

`demo-mode config` reads/writes the persisted tuning at
`$XDG_CONFIG_HOME/reachy/demo-mode.json`. `run`/`start` read it; CLI flags
override per-invocation (precedence: flag > config file > built-in default).

- `reachy-mini-cli demo-mode config` — show the resolved config + its path.
- `reachy-mini-cli demo-mode config --init` — write a default config file.
- `reachy-mini-cli demo-mode config --set energy=0.8 interval=3` — set keys.

Keys: `transport`, `base_url`, `timeout`, `interval`, `energy`, `interpolation`,
`seed`, `wake`, `settle`. Tuning meaning:

- `interval` — seconds between poses (tempo; default 2.5).
- `energy` — liveliness multiplier scaling every amplitude (default 1.0;
  `0` is nearly still, `>1` is bigger motion).
- `interpolation` — `{minjerk,linear,ease,cartoon}` curve between poses.
- `seed` — make the idle motion reproducible (`none` for random).
- `wake` / `settle` — wake on start / ease to neutral on stop (override with
  the `--no-wake` / `--no-settle` flags).

## Service (systemd --user)

Run it always-on, auto-restarting on crash and starting on boot:

- `reachy-mini-cli demo-mode install` — write the `reachy-demo-mode.service` unit
  (ExecStart re-invokes `demo-mode run --config <path>`).
- `reachy-mini-cli demo-mode enable` — `systemctl --user enable --now` + enable
  linger so it survives logout/reboot (`--no-linger` to skip).
- `reachy-mini-cli demo-mode disable` — `systemctl --user disable --now`.
- `reachy-mini-cli demo-mode uninstall` — remove the unit file.

Without a systemd user session these exit `2` with a hint; use start/stop instead.

{transports}

## Notes

- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`:
  `demo-mode.pid` + `demo-mode.log`.

## Usage

    reachy-mini-cli daemon start                       # something for the loop to drive
    reachy-mini-cli demo-mode config --set energy=0.7  # tune it
    reachy-mini-cli demo-mode start                    # robot starts feeling alive
    reachy-mini-cli demo-mode restart                  # apply config/code updates
    reachy-mini-cli demo-mode install && reachy-mini-cli demo-mode enable  # always-on
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)


_BEHAVIOR = """\
# reachy-mini-cli behavior

Compose robot behaviors on a 50 Hz control loop. A persistent **engine** holds a
set of active behaviors; you push behaviors onto it from separate commands, and a
per-channel contention model decides who drives each part of the robot when they
conflict. `feel-alive` runs as a passive base layer, so an idle robot keeps
breathing on any channel nothing else claims.

The engine streams *immediate* `set_target` poses, so **it owns motion
exclusively while running** — don't drive the robot with `move goto` / `demo-mode`
at the same time. The http transport needs a running daemon
(`reachy-mini-cli daemon start`).

## Channels and contention

Behaviors claim one or more **channels** — `head`, `antennas`, `body_yaw` — and
carry a contention **class**:

- `passive` — drives a channel only when nothing else claims it; yields instantly.
- `stoppable` — drives, but a newly-started `stopping` behavior removes it.
- `unstoppable` — holds its channels until it finishes on its own; never removed.
- `stopping` — on start, stops the `stoppable` behaviors sharing its channels.

Same-channel conflicts resolve by class priority
(`unstoppable` > `stopping` > `stoppable` > `passive`), then by most-recent.

## Lifetime

- one-shot (`--once`) — runs once for `--duration` seconds, then expires;
- looping (`--loop`) — repeats until `--duration` elapses, or forever (no
  duration) until stopped.

Each behavior has a natural default (e.g. `gaze-hold` is one-shot, `speak` loops).

## Sensing

All built-in behaviors are pure motion. For **sound-orienting**, see the dedicated
`reachy-mini-cli listen` noun (`reachy-mini-cli explain listen`): it drives the
daemon's smooth minjerk `goto` planner instead of the engine's `set_target`
stream, which is jerky for big reorienting turns. (The engine keeps a general
capability to feed a sensor-driven behavior a live reading, but ships none today.)

## Verbs

- `reachy-mini-cli behavior list` — the built-in catalog (names, channels,
  default class, parameters). No robot needed.
- `reachy-mini-cli behavior run <name> [--set k=v ...] [--class CLASS]
  [--channels ...] [--once|--loop] [--duration N]` — push a behavior onto the
  engine (auto-starts it). Reports what it admitted / evicted / is blocked on.
- `reachy-mini-cli behavior stop <id|name|all>` — stop a running behavior
  (`all` keeps the passive base layer).
- `reachy-mini-cli behavior status` — active behaviors, per-channel ownership,
  and engine/daemon state.
- `reachy-mini-cli behavior engine start|stop|status|run` — manage the 50 Hz
  engine process (start/stop in the background, or `run` in the foreground).
- `reachy-mini-cli behavior overview` — the verb summary.

{transports}

## Notes

- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`:
  `behavior/engine.pid`, `behavior/engine.log`, and a command spool +
  `state.json` the CLI and engine talk through.
- The engine tick rate is `--compose-hz` (default 50); the base layer's
  liveliness is `--energy`; disable the base layer with `--no-base-layer`.

## Usage

    reachy-mini-cli daemon start                         # something to drive
    reachy-mini-cli behavior engine start                # bring the 50 Hz loop up
    reachy-mini-cli behavior run speak --duration 8      # head bobs like speech
    reachy-mini-cli behavior run antenna-sway --loop --class stopping \\
        --channels antennas body_yaw                     # sway + seize the body yaw
    reachy-mini-cli behavior status --json
    reachy-mini-cli behavior stop all
    reachy-mini-cli behavior engine stop                 # eases robot to neutral
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)


_LISTEN = """\
# reachy-mini-cli listen

Orient the robot toward sound in real time. `listen` is **SDK-first**: it streams
live audio from the mic array via the `reachy_mini` SDK (the `sdk` transport is the
default), so DoA and loudness are computed in-process — no round-trip to the daemon.
The HTTP transport remains available via `--transport http` / `REACHY_TRANSPORT=http`
for a control box talking to a remote daemon.

## Two-tier reaction

The loop runs two tiers simultaneously:

**Tier 1 — antenna lean (always on):** A lightweight, near-continuous lean of the
*antennas* (and head holds position) toward the incoming DoA on every tick. This gives
the robot a subtle "perked ear" reaction to any live sound, even faint ambient noise,
without moving the body.

**Tier 2 — head→body turn (speech or loud snap):** On detected *speech* or a loud
RMS transient ("snap") — a sudden noise spike above a ratio × floor threshold — the
robot executes a slow, smooth head→body turn:

1. The head turns toward the source first.
2. If the source is beyond `--head-only-band` degrees from center, the body rotates
   to face the source and the head re-centers, so the whole robot is re-oriented.

A **latched-DoA guard** prevents stale angles from triggering a spurious turn: the
daemon's DoA angle freezes at rest, so Tier-2 fires only on live speech/snap, never
on the last angle left over from a previous sound.

**Always-alive idle (between sounds):** when nothing reactive fires, the robot
keeps gently moving — breathing, a slow gaze wander, and antenna sway — *around its
current heading*, so it is never frozen. A robot that turned toward a sound stays
rotated and keeps wandering there; after `--recenter-after` seconds of silence the
head and body drift slowly back toward front (`--drift-speed` deg/s) rather than
hard-snapping home. `--idle-energy 0` restores the old hold-still behaviour.

The `SnapDetector` (`reachy/motion/snap.py`) implements the RMS spike detection,
algorithm cited from `reachy_nova`'s `TrackingManager.detect_snap`.

Unlike the behavior engine — which streams immediate `set_target` poses at 50 Hz
(jerky for big reorienting turns) — this loop drives the smooth minjerk `goto`
planner and runs moves strictly one at a time through a serial motion queue.

It degrades gracefully: no mic, a DoA error, or (with `--speech-only`) no speech ⇒
no reaction, no crash.

## Verbs

- `reachy-mini-cli listen run` — run the loop in the foreground (what `start` and
  the process launch run). Ctrl-C stops it; `--max-ticks N` runs a fixed number of
  ticks. Eases to center on start (preflight) and on stop.
- `reachy-mini-cli listen start` — spawn the loop in the background, recording its
  PID + log under the state dir. For `--transport http` it first preflights the
  daemon's health route. Idempotent: reports `already-running` if a tracked loop
  is alive.
- `reachy-mini-cli listen stop` — SIGTERM the loop this CLI started (so it eases
  back to center before exiting), escalating to SIGKILL past `--timeout`.
- `reachy-mini-cli listen restart` — stop the tracked loop and relaunch it, so the
  new process re-reads the tuning and the latest code.
- `reachy-mini-cli listen status` — the loop's process state (running / stopped /
  stale) and whether the daemon answers.
- `reachy-mini-cli listen overview` — the verb summary.

## Tuning

Feel knobs (each defaults to a tuned value; unset keeps it):

- `--gain X` — DoA-to-head-yaw scaling factor.
- `--deadband DEG` — ignore sound within this angle of the current heading.
- `--hold SECONDS` — after a Tier-2 turn, stay there this long before reconsidering.
- `--speed DEG_PER_S` — slew speed for Tier-2 turns and for easing back to center.
- `--recenter-after SECONDS` — silence grace before the head/body start drifting home.
- `--idle-energy X` — liveliness of the always-alive idle motion (0 = hold still).
- `--drift-speed DEG_PER_S` — how fast the head/body drift home after silence.
- `--speech-only` — Tier-2 reacts only to detected speech (Tier-1 still runs).
- `--antenna-max DEG` — maximum antenna lean angle for Tier-1.
- `--body-yaw-max DEG` — maximum body yaw for Tier-2 body rotation.
- `--head-only-band DEG` — source angles within this band stay head-only (no body
  rotation); outside it the body turns and the head re-centers.
- `--snap-ratio X` — RMS spike must be this many times the floor to count as a snap.
- `--snap-floor RMS` — minimum floor RMS below which snap detection is suppressed.

## Transport

The `sdk` transport (default) streams mic audio via `reachy_mini` in-process —
`reachy-mini` and `numpy` are base runtime dependencies. The `http` transport polls
the daemon's DoA endpoint instead; use it with `--transport http` or
`REACHY_TRANSPORT=http` on a control box that talks to a remote daemon.

## Notes

- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`: `listen.pid`
  and `listen.log`.
- Only one thing should drive the robot at a time — don't run `listen` alongside
  `demo-mode` or the behavior engine.

## Usage

    reachy-mini-cli daemon start                              # bring the daemon up
    reachy-mini-cli listen run                                # foreground, SDK transport (default)
    reachy-mini-cli listen run --transport http               # foreground, HTTP transport
    reachy-mini-cli listen start --hold 3 --speech-only       # background, speech only
    reachy-mini-cli listen start --antenna-max 25 --snap-ratio 4
    reachy-mini-cli listen status --json
    reachy-mini-cli listen stop                               # eases back to center
"""


ENTRIES: dict[tuple[str, ...], str] = {
    (): _ROOT,
    ("reachy",): _ROOT,
    ("reachy-mini-cli",): _ROOT,
    ("whoami",): _WHOAMI,
    ("quickstart",): _QUICKSTART,
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
    ("demo-mode",): _DEMO,
    ("demo-mode", "overview"): _DEMO,
    ("demo-mode", "start"): _DEMO,
    ("demo-mode", "stop"): _DEMO,
    ("demo-mode", "restart"): _DEMO,
    ("demo-mode", "status"): _DEMO,
    ("demo-mode", "run"): _DEMO,
    ("demo-mode", "config"): _DEMO,
    ("demo-mode", "install"): _DEMO,
    ("demo-mode", "enable"): _DEMO,
    ("demo-mode", "disable"): _DEMO,
    ("demo-mode", "uninstall"): _DEMO,
    ("behavior",): _BEHAVIOR,
    ("behavior", "overview"): _BEHAVIOR,
    ("behavior", "list"): _BEHAVIOR,
    ("behavior", "run"): _BEHAVIOR,
    ("behavior", "stop"): _BEHAVIOR,
    ("behavior", "status"): _BEHAVIOR,
    ("behavior", "engine"): _BEHAVIOR,
    ("behavior", "engine", "overview"): _BEHAVIOR,
    ("behavior", "engine", "start"): _BEHAVIOR,
    ("behavior", "engine", "stop"): _BEHAVIOR,
    ("behavior", "engine", "status"): _BEHAVIOR,
    ("behavior", "engine", "run"): _BEHAVIOR,
    ("listen",): _LISTEN,
    ("listen", "overview"): _LISTEN,
    ("listen", "run"): _LISTEN,
    ("listen", "start"): _LISTEN,
    ("listen", "stop"): _LISTEN,
    ("listen", "restart"): _LISTEN,
    ("listen", "status"): _LISTEN,
}
