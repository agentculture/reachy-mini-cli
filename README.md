# reachy-mini-cli

Agent and CLI for operating the **Reachy Mini** expressive robot — device setup,
app management, and live runtime ops.

```bash
# Real mode (local robot: daemon binary + SDK):
uv tool install 'reachy-mini-cli[daemon]'
reachy-mini-cli quickstart      # copy-paste install + bring-up sequence
reachy-mini-cli daemon start    # bring the daemon up (wakes the robot)
reachy-mini-cli listen run      # orient the head toward sound (Ctrl-C to stop)
```

The installed command is `reachy-mini-cli` (short alias: `reachy`).
**New here? Read the [Operating Reachy Mini guide](docs/operating-reachy.md)** —
it covers bring-up, verification, and the one model you must understand before
running two behaviors at once.

## What Reachy Mini can do

Reachy Mini is an expressive desk robot — a movable head, two antennas, a
rotating body, a USB mic array (with direction-of-arrival), a camera, and a
speaker. `reachy-mini-cli` exposes each capability as a **noun** you run from a
shell or an agent loop: hold the hardware (`daemon`), feel alive when idle
(`demo-mode`), orient to sound (`listen`) or sight (`vision`), speak (`say`),
think out loud and move in step with its thoughts (`think`), feel a head pat
(`pat`), and fall asleep when left alone (`sleep`).

## Noun map

The complete robot surface. Every noun supports `--json`; run
`reachy-mini-cli explain <noun>` for the full flag reference.

| Noun | What it does | Transport |
|------|--------------|-----------|
| [`daemon`](docs/operating-reachy.md#bring-reachy-up-live) | Start/stop/status the local `reachy-mini-daemon` process | none (manages the process) |
| `device` | Daemon + live robot state (`status`, `state`) | `http` (default) |
| `app` | List / start / stop daemon apps | `http` |
| `move` | One-shot `goto` / `wake` / `sleep` animations | `http` (default) |
| `demo-mode` | Always-on "feel alive" idle loop (breathe, glances, sway) | `sdk`/`http` |
| `behavior` | 50 Hz engine that composes named behaviors per channel | `sdk`/`http` |
| [`listen`](docs/operating-reachy.md#senses-one-sdk-media-owner-at-a-time) | Two-tier sound orienting (antenna lean → head/body turn) | `sdk` default |
| `vision` | Turn toward motion or light (pure pixel math, no ML) | `sdk` default |
| `say` | Dumb TTS pipe: text → speaker | `sdk` default |
| `think` | LLM cognition loop: speaks + expresses; `--export` JSONL feed | `sdk` default |
| `pat` | Feel a head pat and lean into it (no touch sensor) | `sdk` only |
| `sleep` | Decay to sleep when idle; wake on sound / wake-word / pat | `sdk` default |
| `whoami` `quickstart` `learn` `explain` `overview` `doctor` `cli` | Agent-first introspection — no robot needed | — |

> ⚠️ **Before you run two behaviors at once, read
> [the single-SDK-owner model](docs/operating-reachy.md#the-single-sdk-owner-model).**
> The robot serves one in-process SDK client and one motion queue, each a
> *single resource*: `listen`, `think`, `sleep`, `vision`, and `pat` are
> **mutually exclusive on the `sdk` transport**. This trips up humans and agents
> repeatedly. The conflict matrix and the two ways to compose behaviors anyway
> are in the guide.

## Install

| Profile | Install | For |
|---|---|---|
| **Real mode (recommended)** | `uv tool install 'reachy-mini-cli[daemon]'` | A local robot — pulls `reachy-mini`, so the `sdk` transport and `daemon start` work out of the box. |
| **HTTP remote** | `pip install reachy-mini-cli` | No local robot — `numpy`-only; talk to a daemon elsewhere with `--transport http` + `REACHY_BASE_URL`. |

`reachy-mini` is an **extra**, not a base dep (its pycairo/gstreamer/pyaudio
stack needs system libraries a bare box lacks). Running the `sdk` transport on a
bare install exits `2` with a hint to install `[sdk]` — never a traceback. See
[Install profiles](docs/operating-reachy.md#install-profiles) for the full
rationale. `reachy-cli` remains a transitional alias that pulls in
`reachy-mini-cli`.

## Operating Reachy live

The full operating guide is **[`docs/operating-reachy.md`](docs/operating-reachy.md)**:

- [Bring Reachy up live](docs/operating-reachy.md#bring-reachy-up-live) — install → daemon → verify → behavior
- [The single-SDK-owner model](docs/operating-reachy.md#the-single-sdk-owner-model) — the conflict matrix + how to compose behaviors
- [Transports — `sdk` vs `http`](docs/operating-reachy.md#transports--sdk-vs-http)
- [Verify it's working](docs/operating-reachy.md#verify-its-working)
- [The `~/.asoundrc` mic-array gotcha](docs/operating-reachy.md#the-asoundrc-mic-array-gotcha) — the most common silent failure
- [Environment variables](docs/operating-reachy.md#environment-variables) — every `REACHY_*` var in one table
- [Troubleshooting](docs/operating-reachy.md#troubleshooting) — symptoms → fixes, exit codes
- [Noun reference](docs/operating-reachy.md#noun-reference-technical-layer) — each noun's sense, motion, and transport

### Common commands

```bash
reachy-mini-cli daemon start                                   # bring the daemon up (wakes the robot)
reachy-mini-cli device status                                  # verify it answers
reachy-mini-cli move goto --z 10 --pitch -5 --duration 2       # one motion command
reachy-mini-cli demo-mode start                                # feel-alive idle loop (background)
reachy-mini-cli listen run                                     # orient to sound (sdk; Ctrl-C to stop)
reachy-mini-cli vision run                                     # orient to motion/light (sdk)
reachy-mini-cli say run "Hello from Reachy"                    # text-to-speech
reachy-mini-cli think run                                      # LLM cognition loop (speaks + moves)
reachy-mini-cli pat run                                        # feel a head pat and lean in
reachy-mini-cli sleep run                                      # fall asleep when idle, wake when addressed
reachy-mini-cli daemon stop                                    # put it back down
```

The background nouns (`demo-mode`, `listen`, `think`, `sleep`) also expose
`start` / `stop` / `restart` / `status`; the sense nouns also expose `demo` (no
robot needed). See `reachy-mini-cli explain <noun>`.

## Export feed

`think run --export -` streams a live newline-delimited JSON (NDJSON) feed of
what the robot is **thinking / saying / feeling** — one object per line. The
renderer stays **out of this repo** by design (the export decoupling boundary):
`reachy-mini-cli` emits a documented contract, a separate consumer renders it.

```bash
reachy-mini-cli think run --export -                              # all block types
reachy-mini-cli think run --export - --export-blocks message,emotion
reachy-mini-cli think run --export - | <your renderer>
```

Wire format: [`docs/export-schema.md`](docs/export-schema.md). For the renderer
boundary and the reference `reterminal` consumer, see
[Export feed & the external renderer](docs/operating-reachy.md#export-feed--the-external-renderer).

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

## CLI (introspection)

The agent-first verbs that work with no robot attached:

| Verb | What it does |
|------|--------------|
| `whoami` | Report this agent's nick, version, backend, model from `culture.yaml`. |
| `quickstart` | Print the copy-paste install + bring-up sequence. |
| `learn` | Print a structured self-teaching prompt. |
| `explain <path>` | Markdown docs for any noun/verb path. |
| `overview` | Read-only descriptive snapshot of the agent. |
| `doctor` | Check the agent-identity invariants (prompt-file-present, backend-consistency). |
| `cli overview` | Describe the CLI surface itself. |

Every command supports `--json`. Results go to stdout, errors/diagnostics to
stderr (never mixed). Exit codes: `0` success, `1` user error, `2` environment
error, `3+` reserved.

## Make it your own

1. Rename the package `reachy/` and the `reachy-mini-cli` CLI/dist name
   throughout `pyproject.toml`, the package, `tests/`, and
   `sonar-project.properties`.
2. Edit `culture.yaml` with your `suffix` and `backend`.
3. Rewrite `CLAUDE.md` for your agent and run `/init`.
4. Re-vendor only the skills you need from guildmaster (see
   [`docs/skill-sources.md`](docs/skill-sources.md)).

See [`CLAUDE.md`](CLAUDE.md) for the full conventions (version-bump-every-PR,
the `cicd` PR lane, deploy setup).

## License

MIT — see [`LICENSE`](LICENSE).
