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

- `reachy-mini-cli wireless <verb>` — find a Reachy Mini on the LAN, remember
  which unit is yours, pin it to a stable name, and log in.
- `reachy-mini-cli daemon <verb>` — start/stop/check the local daemon process.
- `reachy-mini-cli device <verb>` — daemon/robot status and live state.
- `reachy-mini-cli app <verb>` — list/start/stop Reachy Mini apps.
- `reachy-mini-cli move <verb>` — runtime motion (goto, wake, sleep).
- `reachy-mini-cli demo-mode <verb>` — start/stop a background loop that makes
  the robot feel alive (idle breathing, glances, antenna sway).
- `reachy-mini-cli behavior <verb>` — compose behaviors on a 50 Hz loop
  (`list`, `run`, `stop`, `status`, `engine`), including orienting toward sound.

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
    reachy-mini-cli behavior engine run         # the presence loop (Ctrl-C to stop)
    reachy-mini-cli daemon stop                 # when you are done

## Remote / HTTP-only — no local robot

    uv tool install reachy-mini-cli             # numpy-only, no daemon binary
    export REACHY_BASE_URL=http://reachy.local:8000
    reachy-mini-cli device status --transport http

The bare install omits `reachy-mini` (its pycairo/gstreamer/pyaudio stack needs
system libraries a bare box lacks); the `[daemon]` extra adds the daemon binary
and SDK. See `reachy-mini-cli explain daemon` and `reachy-mini-cli explain behavior`.

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

## Pettable presence and sensing

`feel-alive` moves for a jittered 8–12 seconds, settles smoothly, then keeps the
complete commanded pose bit-for-bit still for a four-second pettable window.
The `pet-reaction` entry is the built-in sensor-driven exception to the pure
motion entries: a pat rule admits it as one stoppable owner of `head`,
`antennas`, and `body_yaw`.

The live `pat_state` is additive beside the unchanged legacy `pat`
`[touch_type, level]` value. It distinguishes available sampling, commanded-pose
blocking, and an unavailable reader. Only available samples can claim release or
advance interaction state. Intensity means level plus fresh-press recency; it
uses the existing discrete level and is not a calibrated force value.

Direction is side-only signed robot-frame yaw: opposite labelled side pats make
opposite bounded head/body targets. A non-directional scratch gets a distinct
pitch pose; there is no front/back directional claim. The reaction moves into a
contact pose, then holds the complete commanded pose so sensing can reopen.
Contentment starts after four seconds of credible contact, warning by eight, and
a coordinated done gesture moves head, body, and antennas no later than 12
seconds. Observed release starts within one second of the last fresh press;
blocked or unavailable sensing gets bounded grace and is never called physical
release. The behavior self-completes and also has a finite lifetime backstop.

This symbolic path owns motion only through engine arbitration. It does not run
the legacy queue reaction or create a second `MotionQueue` owner. Orienting
toward sound is one of its own library behaviors (`orient-to-sound`) rather than
a separate noun: it turns the head — and, past the head-only band, the body —
toward the bearing of an addressed voice, on the same arbitrated 50 Hz loop.

## Verbs

- `reachy-mini-cli behavior list` — the built-in catalog (names, channels,
  default class, parameters). No robot needed.
- `reachy-mini-cli behavior run <name> [--set k=v ...] [--class CLASS]
  [--channels ...] [--once|--loop] [--duration N]` — push a behavior onto the
  engine (auto-starts it). Reports what it admitted / evicted / is blocked on.
- `reachy-mini-cli behavior stop <id|name|all>` — stop a running behavior
  (`all` keeps the passive base layer).
- `reachy-mini-cli behavior status` — active behaviors, per-channel ownership,
  engine/daemon state, rules-file health (path + counts), and — once the
  engine has published one — the live agent-intents view (goal/inhibitions/
  mode; see "Rules" below).
- `reachy-mini-cli behavior reload` — reload `rules.toml` in the running
  engine, applied between ticks (see "Rules" below).
- `reachy-mini-cli behavior goto [--x ..] [--y ..] [--z ..] [--roll ..]
  [--pitch ..] [--yaw ..] [--antennas RIGHT LEFT] [--body-yaw ..]
  [--duration N] [--interpolation {minjerk,linear,ease,cartoon}] [--label ..]`
  — submit a goto through the intents spool (see "Goto" below). Only the
  channels you pass end up in the payload; naming none is refused.
- `reachy-mini-cli behavior rules` (alias `rules list`) — render the loaded
  `rules.toml` (react/inhibit rules, modes, active mode). A missing file is
  not an error; a malformed one is a clean exit-1 naming every reason. Reads
  the file directly — no running engine needed.
- `reachy-mini-cli behavior rules check` — validate `rules.toml` as a linter:
  a malformed file reports `ok=false` with reasons but still exits 0; only an
  unreadable path (permissions, a vanished mount, ...) is a clean exit-2. Also
  warns (`ok=false`, still exit 0) on any rule keyed to a sense field nothing
  in the current composition feeds (see `reachy.behavior.sense
  .FED_SENSE_FIELDS`) — such a rule validates cleanly and then silently never
  fires, so the linter names the field, the rule, and why.
- `reachy-mini-cli behavior expressions` (alias `expressions list`) — list the
  expression pose catalog, each emoji with a generated pose descriptor (see
  "Expressions" below).
- `reachy-mini-cli behavior expressions check` — flag catalog poses too
  similar to be meaningfully distinct; a warning, not a gate (always exits 0).
- `reachy-mini-cli behavior engine start|stop|status|run` — manage the 50 Hz
  engine process (start/stop in the background, or `run` in the foreground).
- `reachy-mini-cli behavior overview` — the verb summary.

## Rules

`behavior engine run` optionally drives the engine from a declarative
`rules.toml` file (default: `<state dir>/behavior/rules.toml`) — `[[react]]` /
`[[inhibit]]` rules over the live sense snapshot, plus named `[modes.<name>]`
parameter sets. It is loaded once at boot:

- a MISSING file is fine — "no rules configured yet", the engine runs on
  `feel-alive` alone;
- a PRESENT but malformed file is REJECTED without crashing the process — the
  engine falls back to bare base presence (`feel-alive` only, no rule seam)
  and logs the rejection (naming every reason) as a
  `[SENSE stage=rule source=rules event=boot]` line — an operator's typo never
  takes the robot's presence down, let alone loops a service restart.

`reachy-mini-cli behavior reload` asks the running engine to re-read
`rules.toml` at a deterministic point between ticks, with the same last-good
retention: a rejected reload keeps whatever rules were already running and
reports why; an accepted reload swaps in immediately, with no restart.

## Goto

`reachy-mini-cli behavior goto` drives the same smooth minjerk `goto` planner
`move goto` uses, but as a **one-shot behavior arbitrated by the engine**
instead of a direct daemon call — so it composes cleanly with everything else
running on the 50 Hz loop instead of fighting it for a channel. It submits
into the **intents spool** (`reachy.behavior.control`, namespace `intents`) —
the exact command path a live tool-use agent's `run_behavior` /
`declare_goal` / ... actions already write into — so this verb exercises
precisely what an agent exercises, nothing bespoke.

The submit is **async**: the engine applies the goto on its next drain, not
during the CLI call. `behavior goto` waits up to `--await-timeout` (default
1.0s) for the engine to confirm:

- confirmed **admitted** — reports the goto's id, claimed channels, and
  duration;
- confirmed **rejected** — the engine's own validation (`reachy.behavior.
  goto_intent`) refuses out-of-range axes, an unknown field, a runaway
  duration (> 10s), or a goto naming no channel at all — **refuses, never
  clamps** — surfaced here as a clean exit-1, same as any other CLI
  validation error;
- **no confirmation in time** — reports `submitted: <cmd id>` and degrades
  gracefully (exit 0): the command is still on disk, so a later-started
  engine still applies it.

A bare `behavior goto` (no channel flags at all) fails fast client-side,
before ever touching the spool.

## Expressions

`reachy-mini-cli behavior expressions` inspects the emoji-keyed expression
pose catalog (`reachy.speech.expressions`, backed by `expressions.toml`) and
runs its geometric distinctness check (`reachy.speech.distinctness`). Neither
is LLM-coupled — a TOML table and a distance function — so this catalog
tooling lives here rather than in a cognition noun: it is the same catalog
`agent attach`'s `apply_pose` tool drives, and `behavior` is its only CLI
inspection surface.

- `reachy-mini-cli behavior expressions` (alias `expressions list`) — every
  catalog emoji plus a generated pose descriptor: the pose's non-zero axes and
  their signed magnitudes (`neutral` itself is excluded — it is the universal
  fallback, not an expression).
- `reachy-mini-cli behavior expressions check` — flag catalog pairs too
  similar to be meaningfully distinct. A flagged pair is a warning, not an
  error — the catalog still works — so the exit code stays 0; `--json`'s `ok`
  field is the machine-readable signal.
- `reachy-mini-cli behavior expressions overview` — this sub-noun's summary.

These verbs read the catalog file directly — no running engine needed.

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
    reachy-mini-cli behavior reload                      # picks up an edited rules.toml
    reachy-mini-cli behavior goto --yaw 10 --duration 2 --json
    reachy-mini-cli behavior expressions check --json    # lint the pose catalog
    reachy-mini-cli behavior engine stop                 # eases robot to neutral
""".replace(_TRANSPORTS_SLOT, _TRANSPORTS)


_VISION = """\
# reachy-mini-cli vision

Orient the robot toward what it *sees* in real time. `vision` is **SDK-first** and
**local-profile only**: frames come from the camera via the in-process `reachy_mini`
SDK (the `sdk` transport is the default). No frames are streamed over HTTP — running
with `--transport http` gives camera-metadata-only access (`vision specs`); `vision run`
and the background process (`start`/`stop`/`restart`) require the local `sdk` transport.

**Pixel-based; no ML and no GPU.** Detection is pure pixel math that runs on any
hardware without a GPU:

- **Motion (primary cue) — frame differencing:** consecutive frames are subtracted
  and thresholded; the centroid of the motion-heavy region is mapped to a yaw offset
  and drives a head turn toward the moving object.
- **Light (fallback cue) — brightness/centroid:** when no motion fires, the weighted
  brightness centroid of the frame is computed; a significant shift in the centroid
  triggers a softer look toward the bright region.

Like `pat` and `sleep`, `vision` mirrors the serial-motion-queue design: both tiers drive the
daemon's smooth minjerk `goto` planner strictly one move at a time, so turns are soft
and never conflict. The loop runs only when the daemon is reachable and a camera frame
is available; if either is absent it exits cleanly (exit 2) rather than crashing.

## Verbs

- `reachy-mini-cli vision run` — run the loop in the foreground; Ctrl-C stops it.
  `--max-ticks N` runs a fixed number of ticks. Eases to center on start and on stop.
- `reachy-mini-cli vision start` — spawn the loop in the background, recording its
  PID + log under the state dir. Idempotent: reports `already-running` if a tracked
  loop is alive.
- `reachy-mini-cli vision stop` — SIGTERM the loop this CLI started (so it eases
  back to center before exiting), escalating to SIGKILL past `--timeout`.
- `reachy-mini-cli vision restart` — stop the tracked loop and relaunch it, so the
  new process re-reads the tuning and the latest code.
- `reachy-mini-cli vision status` — the loop's process state (running / stopped /
  stale) and whether the daemon answers.
- `reachy-mini-cli vision specs` — report camera metadata (resolution, name,
  intrinsics). This verb is remote-safe: it works with `--transport http` because
  the daemon REST API serves camera metadata without streaming frames.
- `reachy-mini-cli vision overview` — the verb summary.

## Tuning

Feel knobs (each defaults to a tuned value; unset keeps it):

- `--gain X` — direction-to-head-yaw scaling factor.
- `--max-yaw DEG` — maximum head yaw toward a visual target.
- `--deadband DEG` — ignore targets within this angle of the current heading.
- `--hold SECONDS` — after a turn, stay there this long before reconsidering.
- `--speed DEG_PER_S` — slew speed for turns and for easing back to center.
- `--motion-threshold X` — minimum motion magnitude to trigger a head turn; lower =
  more sensitive; higher = only large moves fire.

## Transport

The `sdk` transport (default) reads camera frames via `reachy_mini` in-process —
requires the `[sdk]` / `[daemon]` extra. The `http` transport polls the daemon's
camera-metadata endpoint; use it with `--transport http` or `REACHY_TRANSPORT=http`
for a remote control box or to run `vision specs` without the SDK installed.

## Notes

- Camera was previously unused by the CLI — this is a net-new perception channel.
- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`: `vision.pid`
  and `vision.log`.
- Only one thing should drive the robot at a time — don't run `vision` alongside
  `demo-mode` or the behavior engine.

## Usage

    reachy-mini-cli vision specs                               # check camera metadata
    reachy-mini-cli daemon start                              # bring the daemon up
    reachy-mini-cli vision run                                # foreground, SDK transport (default)
    reachy-mini-cli vision run --motion-threshold 0.02        # more sensitive
    reachy-mini-cli vision start --hold 2 --speed 30          # background
    reachy-mini-cli vision status --json
    reachy-mini-cli vision stop                               # eases back to center
"""


_SAY = """\
# reachy-mini-cli say

Synthesize text and play it through the robot speaker. A *dumb pipe*: text →
TTS synthesis → playback. No LLM, no senses, no event bus — `say` is
deliberately boundary-clean so agents can compose it into pipelines without
pulling in the heavier speech stack.

Pass `"-"` as the text argument to read from stdin (e.g.
`echo "hello" | reachy-mini-cli say run -`).

## Verbs

- `reachy-mini-cli say run <text>` — synthesize the given text (or stdin with
  `"-"`) and play it through the robot speaker.
- `reachy-mini-cli say overview` — this summary.

## Voice engine

`--voice-engine {tts,harmonic}` picks which speech backend voices the text
(default `tts`; override with `REACHY_VOICE_ENGINE`):

- `tts` — the Chatterbox HTTP endpoint described below.
- `harmonic` — an in-process, offline, non-speech voice: text renders to a short
  note melody in Reachy's own identity signature, played through the same
  speaker leg. No TTS service needed. Tune with `REACHY_HARMONIC_IDENTITY`
  (default `reachy`) / `REACHY_HARMONIC_ARTICULATION` (default `smooth`).

`--voice`, `--speed`, and `--tts-url` are **tts-engine only** — accepted but
silently ignored under `--voice-engine harmonic`.

## TTS

The TTS step calls a Magpie-style HTTP endpoint (default `http://localhost:9000`,
override with `--tts-url` / `REACHY_TTS_URL`). The voice identifier can be set
with `--voice` / `REACHY_TTS_VOICE`. The `--speed` flag is accepted (forwarded
to the server) for forward compatibility.

## Playback transport

- `sdk` (default) — pushes PCM audio frames to the robot speaker via the
  in-process `reachy_mini` SDK. Requires the `[sdk]` / `[daemon]` extra.
- `http` — sends a single POST to the daemon's `/media/play` route. Use with
  `--transport http` / `REACHY_TRANSPORT=http` for a remote control box.

`--base-url` / `REACHY_BASE_URL` sets the daemon URL for `http` playback.

## Boundary invariant

`say` MUST NOT import `reachy.speech.llm` or `reachy.speech.events`. Tests
assert this. Keep `say` as a pure TTS → playback pipe.

## Usage

    reachy-mini-cli say run "Hello from Reachy"
    echo "Hello from stdin" | reachy-mini-cli say run -
    reachy-mini-cli say run "Test" --voice en_US --tts-url http://localhost:9000
    reachy-mini-cli say run "Remote" --transport http --base-url http://reachy.local:8000
    reachy-mini-cli say run "JSON check" --json
    reachy-mini-cli say run "Beep boop" --voice-engine harmonic  # offline harmonic voice
"""


_PAT = """\
# reachy-mini-cli pat

Feel a head pat and lean into it. A proprioceptive reactive loop: the robot holds
a neutral baseline head pose, reads the *actual* head pose back each tick, and feeds
the commanded-vs-actual deviation to a `PatDetector`. When the detector recognises a
pat it fires an event and `PatReaction` enqueues a calm lean→nuzzle→settle gesture
onto the shared serial `MotionQueue`, drained one move at a time to the robot by a
background motion executor — the same architecture as `sleep` and `vision`.

Two **touch types**:

- `scratch` — a head-press (pitch deviation): the robot dips its head into the touch.
- `side_pat` — a sideways nudge (yaw deviation): the robot turns toward the hand.

Two **intensities**: `level1` (light touch) and `level2` (sustained/firmer touch).
Each combination produces a distinct lean gesture — the reaction is scaled by level.

Detection is **proprioceptive**: there is no physical touch sensor. The detector
infers a pat from the difference between the commanded pose and the actual pose
reported by the SDK (`head_pose` read-back). A transient pose deviation that matches
the scratch or side-nudge pattern — enough presses within a sliding window above the
press threshold — fires a detection event.

## Verbs

- `reachy-mini-cli pat run` — run the foreground proprioceptive loop (SDK-first by
  default); Ctrl-C stops it. `--ticks N` stops after N loop ticks (useful for
  ops/testing). `--press-threshold DEG` and `--min-presses N` tune the detector.
- `reachy-mini-cli pat demo` — synthesize the scripted pat events through
  `PatReaction` with **no robot and no `[sdk]` extra**; emits the enqueued action
  labels so the lean wiring can be verified in CI or on any machine. `--count N`
  limits the number of scripted events played. `--json` for machine-readable output.
- `reachy-mini-cli pat overview` — this summary.

## Transport

`pat` is **SDK-first**: `head_pose` read-back is an SDK-only capability. The `http`
transport is accepted via `--transport http` / `REACHY_TRANSPORT=http` for non-pose
operations, but attempting a `run` over `http` raises a clean exit-2 `CliError`
("not supported on this transport") — never a traceback. A missing `[sdk]` extra also
raises a clean exit-2 `CliError` pointing at the extra before the loop starts.

## Motion

Lean gestures are enqueued onto a serial `MotionQueue` and drained one move at a
time by a background `_MotionExecutor` thread. A transport drop inside the executor
degrades motion to silent — the pat sensing loop keeps running. The queue is flushed
(best effort) on shutdown so any in-flight lean completes before exit.

## Notes

- `demo` requires no robot and no `[sdk]` extra — safe to run in CI and on a
  plain dev machine to exercise the lean planner end-to-end.
- `--ticks N` is handy for bounded ops runs or automated tests.
- Only one thing should drive the robot at a time — don't run `pat` alongside
  `demo-mode`, the behavior engine, or another motion source.

## Usage

    reachy-mini-cli pat run                          # foreground loop, SDK transport
    reachy-mini-cli pat run --ticks 100              # stop after 100 ticks
    reachy-mini-cli pat run --press-threshold 1.5    # stiffer detection threshold
    reachy-mini-cli pat demo                         # verify lean wiring, no robot
    reachy-mini-cli pat demo --count 2 --json        # first 2 events, JSON output
    reachy-mini-cli pat overview                     # this summary
"""


_SLEEP = """\
# reachy-mini-cli sleep

Drift off when undisturbed, wake on a stimulus. A graduated-wakefulness loop: an
idle timer walks the robot ALERT → DROWSY → ASLEEP the longer it goes
undisturbed. Any qualifying stimulus — detected speech, a sound-direction (DoA)
shift, a loud snap transient, or a pat — snaps it back to ALERT with a single
re-engagement gesture.

Each wakefulness state maps to motion through the `SleepProducer`: full-energy
alive idle when ALERT, a low-energy idle when DROWSY, and a near-still
"sleep breathe" (slow body rock + gentle antenna breathing + a slight head
droop) when ASLEEP. Moves are submitted onto the shared serial `MotionQueue` and
drained one move at a time by a background motion executor — the same
architecture as `pat` and `vision`.

While the robot is ASLEEP the noun keeps the `sleep_active.flag` written (under
the state dir) so other subsystems can quiet themselves; it is cleared the moment
the robot is no longer asleep, and on every exit path.

## Verbs

- `reachy-mini-cli sleep run` — run the decay→sleep→wake loop in the foreground
  (SDK-first by default); Ctrl-C stops it. `--ticks N` stops after N loop ticks
  (useful for ops/testing). `--idle-timeout SECONDS` sets the quiet time before
  sleep (the drowsy threshold is half of it).
- `reachy-mini-cli sleep start` — spawn the loop in the background, recording its
  PID + log under the state dir. Idempotent: reports `already-running` if a
  tracked loop is alive.
- `reachy-mini-cli sleep stop` — SIGTERM the loop this CLI started, escalating to
  SIGKILL past `--timeout`.
- `reachy-mini-cli sleep restart` — stop the tracked loop and relaunch it, so the
  new process re-reads the tuning and the latest code.
- `reachy-mini-cli sleep status` — the current sleep state + idle seconds and the
  loop's process state (running / stopped / stale).
- `reachy-mini-cli sleep demo` — walk the full ALERT→DROWSY→ASLEEP→wake arc
  against a synthetic sense sequence + a fake clock, with **no robot and no
  `[sdk]` extra**; the observed state sequence is reported (use `--json`).
- `reachy-mini-cli sleep overview` — this summary.

## Transport

`sleep` is **SDK-first**: the `sdk` transport (default) opens a `reachy_mini`
media session in-process and reads real DoA + mic loudness per tick — requires
the `[sdk]` / `[daemon]` extra. The `http` transport polls the daemon's DoA route
(no audio source, so the snap cue is inert); use it with `--transport http` /
`REACHY_TRANSPORT=http` for a remote control box. A missing `[sdk]` extra raises a
clean exit-2 `CliError` pointing at the extra — never a traceback. `demo` needs no
transport at all.

## Notes

- `demo` requires no robot and no `[sdk]` extra — safe to run in CI and on a
  plain dev machine to exercise the full state arc end-to-end.
- State lives under `$REACHY_STATE_DIR` or `$XDG_STATE_HOME/reachy`: `sleep.pid`,
  `sleep.log`, and the `sleep_active.flag`.
- Only one thing should drive the robot at a time — don't run `sleep` alongside
  `demo-mode`, the behavior engine, or another motion source.

## Usage

    reachy-mini-cli sleep run                          # foreground loop, SDK transport
    reachy-mini-cli sleep run --idle-timeout 60        # nod off after 60s of quiet
    reachy-mini-cli sleep run --ticks 100              # stop after 100 ticks
    reachy-mini-cli sleep demo                         # walk the arc, no robot
    reachy-mini-cli sleep demo --json                  # machine-readable arc
    reachy-mini-cli sleep start                        # background process
    reachy-mini-cli sleep status --json                # state + idle + process
    reachy-mini-cli sleep stop
"""


_SERVICE = """\
# reachy-mini-cli service

Make the robot boot-persistent in **exactly one** presence mode. The robot has a
single presence at a time (the single-SDK-owner model): the idle `demo-mode`
loop or the AI-agnostic symbolic runtime (`behavior engine run`) — never both.
This noun installs systemd `--user` units so that one chosen presence survives a
reboot and auto-restarts on crash, and enabling one mode always disables EVERY
sibling (the single-presence-owner invariant).

Like `daemon`, `service` does **not** drive the robot through a transport — it
talks to **systemd** (`systemctl --user`), so there is no `--transport` flag.

## Three units

- `reachy-daemon.service` — the local `reachy-mini-daemon` process. A boot
  dependency: every presence unit `Requires=` / `After=` it, so the daemon comes
  up first. `disable` leaves the daemon enabled deliberately (other clients of
  the robot depend on it).
- `reachy-demo-mode.service` — the idle `demo-mode run` presence loop.
- `reachy-runtime.service` — the AI-agnostic symbolic runtime (`behavior engine
  run`, the boot default per decision c19): the deterministic 50 Hz engine loads
  `rules.toml`, ticks, and sustains declared intents with zero external AI
  services required. Its `ExecStart` carries no LLM flag and no
  `REACHY_OPENAI_*` reference; an agent attaches to the running loop externally
  afterwards through the `agent` noun, with no unit edit and no loop restart.

## The retired `live` unit

`reachy-live.service` ran the folded `listen run --live` loop. That command no
longer exists, so the unit is an argparse error every `RestartSec=5` — a crash
loop, not a quiet no-op. There is no `enable live` any more: use `runtime`.

An upgrade rewrites no unit files on its own, so a box that ran the live
presence still carries it. **Every `service` verb** (`enable`, `install`,
`uninstall`) now purges it on the way past — `disable --now`, unlink the unit,
remove its `.d/` drop-in directory — and reports what it removed as
`retired_removed`. `status` keeps probing the name too, so a box still carrying
it reports `mode=retired` with a warning rather than a reassuring `mode=null`.

That purge is **destructive and irreversible**: hand-authored drop-ins under
`reachy-live.service.d/` are not reproducible from the repo. Back up
`~/.config/systemd/user/reachy-*.service*` before running a `service` verb on a
box you may need to roll back.

## Verbs

- `reachy-mini-cli service enable demo` — boot-persist the idle demo-mode
  presence; disables the runtime sibling.
- `reachy-mini-cli service enable runtime` — boot-persist the AI-agnostic
  symbolic runtime; disables the demo sibling.
- `reachy-mini-cli service disable` — disable whichever presence unit is enabled
  (the daemon is left enabled, reported as `daemon=left-enabled`).
- `reachy-mini-cli service status` — which presence mode is enabled (or none) +
  per-unit `is-enabled` / `is-active` + daemon health, plus any retired unit
  still enabled.
- `reachy-mini-cli service install` — write all three unit files +
  `daemon-reload`, WITHOUT enabling anything (a separate `enable` chooses the
  mode).
- `reachy-mini-cli service uninstall` — remove the unit files + `daemon-reload`.
- `reachy-mini-cli service overview` — this summary.

## Boot persistence (systemd --user)

The presence runs as a `systemctl --user` service. For it to start at machine
boot (before you log in), the user session needs **linger**
(`loginctl enable-linger $USER`); otherwise it starts at first login. A true
machine-reboot check is therefore a manual on-robot step.

## Notes

- Unit files live under `$XDG_CONFIG_HOME/systemd/user`
  (`~/.config/systemd/user`).
- A missing `systemctl` on PATH raises a clean exit-2 `CliError`; an invalid mode
  is an exit-1 user error — never a traceback.
- Every verb supports `--json`, results to stdout / errors+diagnostics to stderr.

## Usage

    reachy-mini-cli service install                  # write the units, enable nothing
    reachy-mini-cli service enable runtime           # boot-persist the AI-agnostic runtime
    reachy-mini-cli service enable demo              # switch to idle demo
    reachy-mini-cli service status --json            # enabled mode + daemon health
    reachy-mini-cli service disable                  # stop the presence (daemon stays up)
    reachy-mini-cli service uninstall                # remove the units
"""


_AGENT = """\
# reachy-mini-cli agent

Attach an **external AI agent** over the symbolic runtime's seams. The
deterministic 50 Hz loop (`behavior engine run`) is AI-agnostic (decision c11):
it ticks, evaluates its rules, and sustains intents entirely on its own. This
noun is the agent client — a *separate process* that attaches from outside, with
**no unit edit and no loop restart**, and never opens the robot's SDK.

Three composition seams:

- **INPUT** — `--feed <path|->`: read the runtime's own event feed
  (`sense`/`rule`/`intent`/`motion` JSONL, produced by `behavior engine run
  --export -`) from a path (stream/FIFO/file) or `-` for stdin. This client never
  spawns the runtime; it only reads the feed the runtime writes. Each event maps
  to a short first-person perception cue for the agent's turn.
- **COGNITION** — a tool-use engine whose actions are **atomic intent-spool
  writes** (`run_behavior` / `declare_goal` / `set_mode` / `set_inhibition`) the
  running engine drains each tick. The agent moves the robot *through the runtime*
  rather than around it. The built-in `speak`/`harmonics`/`apply_pose` tools are
  present too but publish-only — they feed the cognition feed's
  `message`/`emotion` blocks without the external client touching the robot.
- **SELF-EXTENSION** — the `forge` tool hands a natural-language goal to a coder
  model (`FORGE_BASE_URL` / `FORGE_MODEL`, default the lobes gateway's `cortex` role).
  What comes back is **never trusted**: it must clear a fail-closed, AST-only
  validator — which never imports or executes the code — before it is
  auto-activated and hot-registered, becoming callable on the **next** turn with
  no restart. A forged skill runs over a restricted context exposing exactly
  `speak`/`harmonics`/`express`/`state_get`/`state_update`, wired to the same
  publish-only seams above, so it too never reaches the robot's SDK. Rejected
  code is quarantined under `<state-dir>/forge/staged/.rejected/`; activated
  skills live in `<state-dir>/forge/active/` and are reloaded at every attach.
  A missing or broken forge stack disables only this tool — cognition keeps
  running.
- **OUTPUT** — `--export -` / `--export-blocks`: the agent publishes its OWN
  `thinking`/`message`/`emotion` feed through the shared exporter builder
  (decision c27: the runtime feed carries no
  cognition block). See
  `docs/export-schema.md`.

Like `daemon` / `service`, `agent` does not use a `--transport` — it talks to
feeds + the intent spool, not the robot.

## Verbs

- `reachy-mini-cli agent attach` — read the runtime feed, act via the intent
  spool, publish the agent's own cognition feed. Flags: `--feed <path|->`,
  `--spool-dir DIR` (default: the shared state dir), `--await-timeout SECONDS`,
  `--max-turns N`, `--max-events N`, `--export -` / `--export-blocks`, `--json`.
- `reachy-mini-cli agent embody` — run the **embodiment layer**: the same
  external-client shape, but with ears and a mouth. See
  `reachy-mini-cli explain agent embody`.
- `reachy-mini-cli agent overview` — this summary.

## Usage

    reachy-mini-cli behavior engine run --export - > /tmp/runtime.feed &   # the runtime
    reachy-mini-cli agent attach --feed /tmp/runtime.feed --export -       # the agent

## Exit codes

- `0` success
- `1` user-input error
- `2` environment error (unreadable feed)
"""


_AGENT_EMBODY = """\
# reachy-mini-cli agent embody

Run the **embodiment layer** — a detachable realtime harness that gives Reachy a
voice and a conversational mind *beside* the symbolic runtime, never inside it.
With the layer running the robot holds an out-loud conversation, reacts in voice
when its own rules fire, and takes direct-operation commands. With it stopped the
robot is exactly the symbolic presence it is without it: the runtime's decision
loop and every existing condition are unchanged either way.

`embody` is a sibling of `attach`, not a replacement. `attach` is turn-based,
text-cue-driven and publish-only (its voice tools emit feed blocks and touch
nothing). `embody` hears and speaks for real.

## The four channels

- **EARS + MOUTH** — ONE lobes `/v1/realtime` duplex session. Server-side VAD
  decides where a sentence ended; the session is armed with one `response.create`
  and its spoken reply is played back out. The layer's hearing is **ungated**:
  unlike the runtime's transcript sense it runs no engagement gate, so it hears
  every voice in the room. Only three things ever leave that socket
  (session config, audio append, `response.create`) — no tool call rides it.
- **PERCEPTION** — the runtime's own event feed (`sense`/`rule`/`intent`/`motion`)
  read from `--feed <path|->`, mapped to first-person cues. A **rule firing is the
  headline input**: a scratch the runtime reacted to reaches the layer as a cue,
  which is what lets it answer out loud. The MQTT bus is the intended primary
  route; events-cli is publish-only today, so intake degrades — with one named
  drop — to tailing the feed.
- **COGNITION** — streaming `/v1/chat/completions` turns (`worker` for tool-bearing
  turns, `senses` for perception questions; picked per request from
  `REACHY_EMBODY_WORKER_MODEL` / `REACHY_EMBODY_SENSES_MODEL`, never from
  `environment.d`, which the runtime reads too). Every call streams.
- **ACTION** — a CLOSED five-tool set, each wrapping a surface that already
  validates fail-closed: `goto` (per-axis bounds, 10 s cap), `run_behavior`
  (library + unbounded-lifetime refusal), `speak` / `harmonics` (the shared
  500-character say cap), `create_rule` (the rules overlay, validated, written
  temp+rename, and always `embody-` prefixed). There is no shell, no filesystem
  tool and no way to add a sixth.

## Media profiles — configuration, not a code fork

`--media-profile robot` (default) reads the runtime's audio **tee socket** and
plays through the daemon's **HTTP media route** with `transport=http` passed
explicitly — the sdk fallback would open a second `ReachyMini`, the one forbidden
move. `--media-profile bench` binds the dev-box microphone and speakers so the
whole layer runs on a box with no robot attached. Same classes, same code path;
`REACHY_EMBODY_MEDIA_PROFILE` selects.

## Observability

`--export -` publishes the same `thinking`/`message`/`emotion` NDJSON feed
`agent attach` does (`docs/export-schema.md`), and **every** named failure — a
dead session, a dead LLM, a dead speaker, a refused action, a feed that went
away — appears both as a `[SENSE stage=embody …]` journal line and as a block on
that feed. A consumer that disconnects mid-conversation never kills the layer.

## Flags

`--feed <path|->`, `--media-profile {robot,bench}`, `--spool-dir DIR`,
`--await-timeout SECONDS`, `--turn-interval SECONDS`, `--mute-during-playback`
(the AEC fallback; off by default), `--max-turns N`, `--max-events N`,
`--export -` / `--export-blocks`, `--log-level LEVEL`, `--json`.

## Process supervision — start / stop / restart / status

`agent embody` (bare) runs the layer in the FOREGROUND. `agent embody start`
runs it DETACHED in the background instead, tracked with a pid + log file
under the state dir (`embody.pid` / `embody.log`) — the same shape
`sleep`/`vision`/`behavior engine` already use. One command each way for the
operator:

- `agent embody start` — spawn the layer detached (idempotent: a second
  `start` while one is already running reports `already-running` rather than
  spawning a duplicate). Accepts the same operating flags as the foreground
  verb (`--feed`, `--media-profile`, `--spool-dir`, `--await-timeout`,
  `--turn-interval`, `--mute-during-playback`) — never `--max-turns` /
  `--max-events` / `--export` / `--log-level`, which are foreground-only.
- `agent embody stop` — SIGTERM, then SIGKILL if it lingers (`--timeout`,
  default 10s). Signals ONLY the pid this CLI tracked — a stale or reused pid
  is detected and left untouched, never blindly signalled — so a sibling
  runtime/daemon process is never at risk. Touches nothing on disk: any
  `embody-*` rule the layer authored via `create_rule` PERSISTS in the rules
  overlay after stop (the robot keeps what it was taught) and stays
  enumerable by its `embody-` prefix.
- `agent embody restart` — stop the tracked layer (if any), then start a fresh
  one — picks up new code/flags with one command.
- `agent embody status` — report the process state (`running` / `stale` /
  `stopped`) plus the pid and log path.

No systemd unit ships for the layer in this version — it is a plain process,
never wired into `reachy.service`'s presence pair (`demo` / `runtime`), which
stays exactly that closed pair either way.

## Usage

    reachy-mini-cli behavior engine run --export - > /tmp/runtime.feed &
    reachy-mini-cli agent embody --feed /tmp/runtime.feed --export -

    # on a dev box with no robot:
    reachy-mini-cli agent embody --media-profile bench --feed -

    # as a background process instead:
    reachy-mini-cli agent embody start --feed /tmp/runtime.feed
    reachy-mini-cli agent embody status
    reachy-mini-cli agent embody stop

## Exit codes

- `0` success
- `1` user-input error (an unknown media profile)
- `2` environment error (unreadable feed; failed to launch/stop the background
  process)
"""

_WIRELESS = """\
# reachy-mini-cli wireless

Find a Reachy Mini on the local network, remember which unit is yours across IP
changes, pin it to a stable name, and log in — so the address stops being
something a human retypes every session.

Unlike `device`/`app`/`move` there is **no transport flag**. This noun speaks
plain HTTP to a *candidate* daemon's `/api/daemon/status` route, plus
`/etc/hosts` and `ssh`, so it works on a bare install with neither the `[sdk]`
nor the `[daemon]` extra present.

## Verbs

- `reachy-mini-cli wireless find` — sweep the local IPv4 subnets (or probe one
  `--address`) and report every unit found, remembering each one.
- `reachy-mini-cli wireless list` — the remembered units, from the registry
  alone. No network.
- `reachy-mini-cli wireless ssh` — resolve one unit and open a shell on it.
- `reachy-mini-cli wireless authorize` — the separate, explicitly-confirmed
  one-time SSH key install (`ssh-copy-id`).
- `reachy-mini-cli wireless pin` / `unpin` — add or remove a delimited managed
  block in `/etc/hosts` pinning `<ip> reachy-mini reachy-mini.local`. Needs
  sudo; everything else needs no privilege.
- `reachy-mini-cli wireless forget` — drop a remembered unit (`--unit`) or all
  of them (`--all`). No network.
- `reachy-mini-cli wireless overview` — this summary.

## The name describes the DEFAULT, not a limit

`find` filters to units reporting `wireless_version=true`, because that is the
only field observed to reliably tell a Wireless from a Lite. But the same sweep
also finds a **Lite tethered to another box** on the LAN — discoverable, and not
wireless — so `--all` reveals every Reachy daemon the sweep answered for, each
labelled with its `wireless` flag. Nothing is silently discarded.

## Scope: IPv4 and the default port

The sweep covers local **IPv4** `/24`-or-narrower subnets on port **8000**
only. Wider prefixes, loopback and docker/bridge interfaces are excluded by
construction (this box carries seven `172.x/16` bridges — expanding those is
~459 000 hosts).

A unit on **IPv6**, on another subnet, or on a non-default port stays usable by
explicit address:

    reachy-mini-cli wireless find --address 2a0d:6fc2:4:1::756b --json
    reachy-mini-cli wireless find --address 192.168.1.162 --port 8123

`--address` accepts an IPv6 literal and brackets it for the URL itself.

## Feeding the address onward

Every `find` result carries a ready-made `base_url`, so an agent passes it
straight to `--base-url` or `REACHY_BASE_URL` with no reformatting:

    reachy-mini-cli wireless find --json
    {"units": [{"hardware_id": "...", "address": "192.168.1.162",
                "base_url": "http://192.168.1.162:8000", ...}], ...}

Discovery does not move any existing default: `DEFAULT_BASE_URL` is still
`http://localhost:8000` and the transport contract is untouched.

## Which unit? Ambiguity is refused, never guessed

A remembered unit is tried FIRST at its last-known IP and returned as soon as
its `hardware_id` matches — no sweep at all. A dark or reassigned address
escalates to the sweep and the record is re-pinned to the new address.

With more than one unit known (the normal case on a box that also hosts a
Lite — both report `robot_name=reachy_mini`), a verb that needs exactly one
exits non-zero and names every candidate rather than picking one. Choose with
`--unit <hardware_id-or-alias>` or `REACHY_WIRELESS_UNIT`.

## Login and keys

`ssh` passes `-o HostKeyAlias=reachy-mini`, so `known_hosts` keys on a stable
name whether or not `/etc/hosts` was ever pinned — stable host-key identity
never depends on privilege. The account resolves as `--user` >
`REACHY_WIRELESS_SSH_USER` > `pollen` (the documented Pollen default).

`authorize` is **never** a side effect of `find` or `ssh`: the target was chosen
by scanning an unauthenticated LAN service, so it names the alias, the address
and the `hardware_id` and requires an explicit affirmative (`--yes` confirms up
front) before running `ssh-copy-id`, which appends to `authorized_keys` and
never truncates it. It reports plainly when a key was already installed.

## Costs to know

- Discovery assumes a **trusted network**. The daemon's status route is
  unauthenticated, so on a shared LAN a sweep finds robots that are not yours.
- The unit ships with a **factory-default password** for the `pollen` account.
  Discovery makes the robot easier to find, so changing that password is the
  operator's first move.
- `pin`/`unpin` rewrite `/etc/hosts`. The write is confined to a marked block,
  backed up first, verified after (the file must still parse and still resolve
  `localhost`) and rolled back if it does not. A non-writable file is a clean
  exit-2 error naming sudo, never a traceback.

## State

- registry: `<state_dir>/units.json`, keyed by `hardware_id`
- hosts block: `# BEGIN reachy-mini-cli` ... `# END reachy-mini-cli`

## Exit codes

- `0` success
- `1` user-input error (nothing found, an ambiguous registry, a declined
  confirmation, an unknown unit)
- `2` environment error (unwritable `/etc/hosts`, missing OpenSSH client)
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
    ("behavior", "reload"): _BEHAVIOR,
    ("behavior", "goto"): _BEHAVIOR,
    ("behavior", "rules"): _BEHAVIOR,
    ("behavior", "rules", "list"): _BEHAVIOR,
    ("behavior", "rules", "check"): _BEHAVIOR,
    ("behavior", "rules", "overview"): _BEHAVIOR,
    ("behavior", "expressions"): _BEHAVIOR,
    ("behavior", "expressions", "list"): _BEHAVIOR,
    ("behavior", "expressions", "check"): _BEHAVIOR,
    ("behavior", "expressions", "overview"): _BEHAVIOR,
    ("behavior", "engine"): _BEHAVIOR,
    ("behavior", "engine", "overview"): _BEHAVIOR,
    ("behavior", "engine", "start"): _BEHAVIOR,
    ("behavior", "engine", "stop"): _BEHAVIOR,
    ("behavior", "engine", "status"): _BEHAVIOR,
    ("behavior", "engine", "run"): _BEHAVIOR,
    ("vision",): _VISION,
    ("vision", "overview"): _VISION,
    ("vision", "run"): _VISION,
    ("vision", "start"): _VISION,
    ("vision", "stop"): _VISION,
    ("vision", "restart"): _VISION,
    ("vision", "status"): _VISION,
    ("vision", "specs"): _VISION,
    ("say",): _SAY,
    ("say", "overview"): _SAY,
    ("say", "run"): _SAY,
    ("pat",): _PAT,
    ("pat", "overview"): _PAT,
    ("pat", "run"): _PAT,
    ("pat", "demo"): _PAT,
    ("sleep",): _SLEEP,
    ("sleep", "overview"): _SLEEP,
    ("sleep", "run"): _SLEEP,
    ("sleep", "start"): _SLEEP,
    ("sleep", "stop"): _SLEEP,
    ("sleep", "restart"): _SLEEP,
    ("sleep", "status"): _SLEEP,
    ("sleep", "demo"): _SLEEP,
    ("service",): _SERVICE,
    ("service", "overview"): _SERVICE,
    ("service", "enable"): _SERVICE,
    ("service", "disable"): _SERVICE,
    ("service", "status"): _SERVICE,
    ("service", "install"): _SERVICE,
    ("service", "uninstall"): _SERVICE,
    ("wireless",): _WIRELESS,
    ("wireless", "overview"): _WIRELESS,
    ("wireless", "find"): _WIRELESS,
    ("wireless", "list"): _WIRELESS,
    ("wireless", "ssh"): _WIRELESS,
    ("wireless", "authorize"): _WIRELESS,
    ("wireless", "pin"): _WIRELESS,
    ("wireless", "unpin"): _WIRELESS,
    ("wireless", "forget"): _WIRELESS,
    ("agent",): _AGENT,
    ("agent", "overview"): _AGENT,
    ("agent", "attach"): _AGENT,
    ("agent", "embody"): _AGENT_EMBODY,
    ("agent", "embody", "start"): _AGENT_EMBODY,
    ("agent", "embody", "stop"): _AGENT_EMBODY,
    ("agent", "embody", "restart"): _AGENT_EMBODY,
    ("agent", "embody", "status"): _AGENT_EMBODY,
}
