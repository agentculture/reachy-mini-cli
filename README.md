# reachy-mini-cli

Agent and CLI for operating the **Reachy Mini** expressive robot — device setup,
app management, and live runtime ops.

```bash
# Real mode (local robot: daemon binary + SDK):
uv tool install 'reachy-mini-cli[daemon]'
reachy-mini-cli quickstart      # copy-paste install + bring-up sequence
reachy-mini-cli daemon start    # bring the daemon up (wakes the robot)
reachy-mini-cli behavior engine run   # the presence runtime (Ctrl-C to stop)
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
(`demo-mode`), orient to sight (`vision`), speak in a TTS or offline harmonic
voice (`say`), bench-check a head pat (`pat`), and park itself when left alone
(`sleep`). The **symbolic runtime** (`behavior engine run`) is where the senses
come together: one deterministic 50 Hz presence that hears words, feels pats,
leans its antennas toward sound and answers out loud, and that an AI agent
attaches to (`agent attach`) rather than replaces. The **embodiment layer**
(`agent embody`) is the optional other half — ears, a voice and a
cue-triggered mind running *beside* that runtime, switched on and off like a
peripheral. `service` makes one presence mode survive a reboot.

## Noun map

The complete robot surface. Every noun supports `--json`; run
`reachy-mini-cli explain <noun>` for the full flag reference.

| Noun | What it does | Transport |
|------|--------------|-----------|
| [`wireless`](docs/operating-reachy.md#find-the-robot-on-the-network--wireless) | Find a Reachy on the LAN, remember which unit is yours, pin a stable name, log in | none (HTTP probe + `/etc/hosts` + `ssh`) |
| [`daemon`](docs/operating-reachy.md#bring-reachy-up-live) | Start/stop/status the local `reachy-mini-daemon` process | none (manages the process) |
| `device` | Daemon + live robot state (`status`, `state`) | `http` (default) |
| `app` | List / start / stop daemon apps | `http` |
| `move` | One-shot `goto` / `wake` / `sleep` animations | `http` (default) |
| `demo-mode` | Always-on "feel alive" idle loop (breathe, glances, sway) | `sdk`/`http` |
| [`behavior`](docs/operating-reachy.md#the-symbolic-runtime) | The 50 Hz symbolic runtime: every sense on one tick, composed per channel | `sdk`/`http` |
| [`vision`](docs/operating-reachy.md#senses-one-sdk-media-owner-at-a-time) | Turn toward motion or light (pure pixel math, no ML) | `sdk` default |
| `say` | Dumb pipe: text → voice (TTS or offline harmonic) → speaker | `sdk` default |
| `pat` | Bench check: feel a head pat and lean into it (no touch sensor) | `sdk` only |
| `sleep` | Park the robot: decay to sleep when idle; wake on sound / wake-word / pat | `sdk` default |
| [`service`](docs/operating-reachy.md#boot-persistence--one-presence-per-reboot) | Boot-persist exactly one presence mode (`demo` or `runtime`) via systemd `--user` | none (manages systemd) |
| [`agent attach`](docs/operating-reachy.md#agent--attach-over-the-runtime-feed-and-the-intent-spool) | Attach an external AI agent to the running runtime over its feed + intent spool (voice and pose tools publish-only) | none (feeds + spool) |
| [`agent embody`](docs/operating-reachy.md#the-embodiment-layer--agent-embody) | The embodiment layer: ears + a real voice on one realtime duplex session, a background mind over a streaming HTTP lane, and a closed five-tool action set — running beside the runtime | none (tee socket, feeds, spools, daemon `http`) |
| `whoami` `quickstart` `learn` `explain` `overview` `doctor` `cli` | Agent-first introspection — no robot needed | — |

> ⚠️ **Before you run two behaviors at once, read
> [the single-SDK-owner model](docs/operating-reachy.md#the-single-sdk-owner-model).**
> The robot serves one in-process SDK client and one motion queue, each a
> *single resource*: the `behavior` runtime, `sleep`, `vision`, and `pat` are
> **mutually exclusive on the `sdk` transport** — and `pat run` / `sleep run`
> refuse outright to start beside a live engine rather than starve. This trips
> up humans and agents repeatedly. The conflict matrix and the two correct ways
> to compose behaviors anyway are in the guide.

## Install

| Profile | Install | For |
|---|---|---|
| **Real mode (recommended)** | `uv tool install 'reachy-mini-cli[daemon]'` | A local robot — pulls `reachy-mini`, so the `sdk` transport and `daemon start` work out of the box. |
| **HTTP remote** | `pip install reachy-mini-cli` | No local robot — pure-wheel base deps only (`numpy` + `harmonics-cli`); talk to a daemon elsewhere with `--transport http` + `REACHY_BASE_URL`. |

`reachy-mini` is an **extra**, not a base dep (its pycairo/gstreamer/pyaudio
stack needs system libraries a bare box lacks). Running the `sdk` transport on a
bare install exits `2` with a hint to install `[sdk]` — never a traceback. See
[Install profiles](docs/operating-reachy.md#install-profiles) for the full
rationale. `reachy-cli` remains a transitional alias that pulls in
`reachy-mini-cli`.

Optional extras on top, all lazy-imported (absent → the feature goes quiet after
one named warning, never a crash): `[vision]` (OpenCV — face recognition, scene
description, the rolling video clip), `[cpu]` (on-box `openwakeword`), and
`[bench]` (`sounddevice`, needed **only** for the embodiment layer's bench media
profile — the deployed robot path uses a unix socket and `urllib`, both stdlib).

## Find your robot

Driving a robot your box is not hosting? You need its address first — and DHCP
moves it. `wireless` answers *where is it* once and then remembers:

```bash
reachy-mini-cli wireless find     # sweep the LAN; report + remember what answered
reachy-mini-cli wireless list     # what's remembered (registry only, no network)
reachy-mini-cli wireless ssh      # open a shell on it — no address typed, ever
sudo reachy-mini-cli wireless pin # pin it to a stable /etc/hosts alias (the one sudo step)
```

**It needs no extras at all.** Discovery is stdlib-only — a read-only
`GET /api/daemon/status` fanned out over the local IPv4 `/24`s — so the whole
noun works on the bare **HTTP remote** profile with neither `[sdk]` nor
`[daemon]` installed. It never opens an SDK client or a media session either,
so it is safe to run beside a live runtime. Every verb takes `--json`, and
every result carries a ready-made `base_url` an agent can pass straight to
`--base-url` / `REACHY_BASE_URL`.

Units are remembered by the daemon-reported `hardware_id`, never by name or IP,
so a unit that moves address is still found and re-pinned with no manual step.

> ⚠️ The daemon's status route is **unauthenticated** and the unit ships with a
> **factory-default password**, so anyone who can reach it on the LAN can log
> into it. Discovery makes it easier to find — **changing that password is your
> first move.** See
> [Find the robot on the network](docs/operating-reachy.md#find-the-robot-on-the-network--wireless).

## Operating Reachy live

The full operating guide is **[`docs/operating-reachy.md`](docs/operating-reachy.md)**:

- [Find the robot on the network](docs/operating-reachy.md#find-the-robot-on-the-network--wireless) — `wireless` discovery: measured cold/warm timings, the sudo cost, the trusted-network assumption
- [The names table](docs/operating-reachy.md#the-names-table--who-the-robot-answers-to) —
  who the robot answers to: an overlay `names = [...]` table, hot via
  `behavior reload`, and the `name_mentioned` sense event
- [Bring Reachy up live](docs/operating-reachy.md#bring-reachy-up-live) — install → daemon → verify → behavior
- [The single-SDK-owner model](docs/operating-reachy.md#the-single-sdk-owner-model) — the conflict matrix + how to compose behaviors
- [Transports — `sdk` vs `http`](docs/operating-reachy.md#transports--sdk-vs-http)
- [Boot persistence](docs/operating-reachy.md#boot-persistence--one-presence-per-reboot) — make one presence (`demo`/`runtime`) survive a reboot via `service`
- [The symbolic runtime](docs/operating-reachy.md#the-symbolic-runtime) — a deterministic, model-free presence (`behavior` + `rules.toml`) an AI agent can attach to (`reachy-mini-cli agent attach`) instead of replace
- [The embodiment layer](docs/operating-reachy.md#the-embodiment-layer--agent-embody) — the optional conversational mind (`reachy-mini-cli agent embody`) that runs beside it
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
reachy-mini-cli behavior engine run                            # ALL senses in one loop (the symbolic runtime presence)
reachy-mini-cli vision run                                     # orient to motion/light (sdk)
reachy-mini-cli say run "Hello from Reachy"                    # text-to-speech
reachy-mini-cli pat run                                        # bench check: feel a head pat and lean in
reachy-mini-cli sleep run                                      # park the robot; wake when addressed
reachy-mini-cli daemon stop                                    # put it back down
```

The background nouns (`demo-mode`, `vision`, `sleep`) also expose
`start` / `stop` / `restart` / `status`, and `behavior engine` exposes
`start` / `stop` / `status`; `pat` and `sleep` also expose `demo` (no robot
needed). `pat run` and `sleep run` **refuse to start beside a live engine** —
one owner per head. See `reachy-mini-cli explain <noun>`.

### The runtime presence and boot persistence

`behavior engine run` is the **symbolic runtime** — a deterministic 50 Hz loop
that composes every sense (proprioceptive pat, loudness, transcribed words,
faces, camera-frame availability) and drives the head through one arbitrated
motion channel. It is the supported way to run all the senses at once (one
media owner; see the single-SDK-owner model below). An AI agent attaches to it
over its JSONL feed and intent spool (`agent attach`) rather than replacing it.

**Its decision loop is symbolic and model-free, and CI enforces that.** An AST
import-boundary suite proves the engine, rule engine, rules, intents,
arbitration, goto lane and pat sense reach nothing in the speech, vision or
forge stacks. The runtime does own a voice and ears — deliberately ported
capabilities — so it imports speech *synthesis*, *playback* and
*transcription*, none of which is a language model. **Exactly one
language-model call survives inside the runtime**: the engagement gate's
optional single-shot "is this addressed to me?" classifier. It runs on the
transcript worker thread rather than the 20 ms tick, fails open to a
pure-`difflib` heuristic, gates only whether heard words enter the sense
snapshot — and `REACHY_ENGAGE_HEURISTIC=1` removes it entirely, giving a box a
provably zero-LLM presence. See
[the zero-token rationale](docs/operating-reachy.md#the-zero-token-rationale).

Mic audio streams continuously to the lobes `/v1/realtime` WebSocket session
(`REACHY_REALTIME_URL` / `REACHY_OPENAI_URL_BASE`), whose server-side VAD
decides where each utterance starts and stops; the resulting transcript
reaches the rules as a `transcript` sense field, so a rule can react to *what*
was said — not just that a sound came from the left. A self-mute window means the robot never
transcribes its own voice, and a down session degrades to "no words" rather
than stalling the loop — there is no local fallback endpointer. It is **not**
a chat/turn-taking assistant — words are one more perception. One honest
boundary: the shipped reaction to bare **sound** is an antenna lean only — the
head does not turn (the turn path is implemented and reachable by
configuration, just not defaulted on). See [Hearing over the lobes realtime
session](docs/operating-reachy.md#hearing-over-the-lobes-realtime-session) for
the design and [Hearing — server-side VAD replaces local
endpointing](docs/operating-reachy.md#hearing--server-side-vad-replaces-local-endpointing)
for what is and is not evidenced live today.

```bash
reachy-mini-cli behavior engine run                            # the deterministic presence
reachy-mini-cli agent attach --feed - --export -               # an AI agent alongside it
```

The runtime's voice (a rule's `say:` field) is the offline **harmonic**
note-melody engine by default — fully in-process, deterministic, no external
service to reach — so a box with nothing reachable still speaks. `say run`
accepts `--voice-engine {tts,harmonic}` (default `tts`) to pick per
invocation; tune the voice with `REACHY_HARMONIC_IDENTITY` /
`REACHY_HARMONIC_ARTICULATION`. See
[The harmonic voice](docs/operating-reachy.md#the-harmonic-voice) for the full
picture.

```bash
reachy-mini-cli say run "Hello" --voice-engine harmonic        # offline note-melody voice
```

`service` makes one presence boot-persistent via systemd `--user`. Exactly one
mode is enabled at a time — enabling one disables the siblings — and it
auto-restarts on crash. The daemon is a boot dependency of every presence unit.

```bash
reachy-mini-cli service install                                # write the systemd units (enable nothing)
reachy-mini-cli service enable runtime                         # boot-persist the symbolic runtime
reachy-mini-cli service enable demo                            # switch to the idle demo loop
reachy-mini-cli service status --json                          # which mode is enabled + daemon health
reachy-mini-cli service disable                                # stop the presence (daemon stays up)
```

> ⚠️ **Upgrading a box that ran the old `live` presence?** `reachy-live.service`
> is retired: the next `service enable` / `install` / `uninstall` **purges** it
> (disable, unlink the unit, remove its `.d/` drop-in directory) and reports the
> names it removed as `retired_removed`. That purge is destructive and
> irreversible — back up `~/.config/systemd/user/reachy-*.service*` first.

A true machine-reboot check is manual: a `systemctl --user` service starts at
boot only when the user has **linger** enabled (`loginctl enable-linger $USER`).
See [Boot persistence](docs/operating-reachy.md#boot-persistence--one-presence-per-reboot).

### The embodiment layer — a conversational mind you can switch on

The runtime above is symbolic and **mute in conversation**: `agent attach` is
turn-based, has no transcript cue, and composes its voice tools publish-only, so
nothing in that process ever makes a sound. `reachy-mini-cli agent embody` is
the optional other half. It runs as a **separate process beside** the runtime
and gives the robot ears (one lobes `/v1/realtime` duplex session with
server-side VAD, ungated — it hears every voice in the room), a real voice, and
a cue-triggered mind that reacts **in voice** when the robot's own rules fire.
It operates the robot only through a closed five-tool set — `goto`,
`run_behavior`, `speak`, `harmonics`, `create_rule` — each wrapping a validator
that already exists and already refuses fail-closed. There is no shell.

It runs **two models at two tempos over one conversation**: a *foreground*
interlocutor (the lobes realtime floor) that hears, answers and owns the
wording, and a *background* mind that follows along, thinks longer, operates
the tools — and reaches the room **only through typed, inspectable events**,
never by generating speech itself. So `speak` and `harmonics` are proposals
rather than playback, long replies stream as cancellable chunks a human can
talk over, and what the room actually heard is measured at the speaker rather
than assumed. See [the two-tempo
architecture](docs/operating-reachy.md#the-two-tempo-architecture--gemma-speaks-qwen-thinks).

```bash
reachy-mini-cli behavior engine run --export - > /tmp/runtime.feed &   # the runtime
reachy-mini-cli agent embody --feed /tmp/runtime.feed --export -       # the layer
reachy-mini-cli agent embody start   # …or as a tracked background process
reachy-mini-cli agent embody stop
```

**It is genuinely a peripheral**, and that was checked rather than asserted: the
whole arc's footprint inside `reachy/behavior/` is 3 files, 6 diff hunks, 1486
inserted lines and **0 deleted lines** — only two additive export legs (an audio
tee and a rolling-clip rider), each measured at **zero** tick overruns on the
deployed robot even with a wedged consumer. Stop the layer and the robot is
exactly the symbolic presence above. Swapping the mind is configuration too:
models are chosen per request from `REACHY_EMBODY_WORKER_MODEL` /
`REACHY_EMBODY_SENSES_MODEL`, and how long it keeps listening after hearing its
name is `--attention-window` / `REACHY_EMBODY_ATTENTION_WINDOW`.

One thing deliberately survives a stop: rules the layer authored (always
`embody-` prefixed) **persist** in the overlay and keep running — the robot
keeps what it was taught — and stay enumerable and removable by that prefix.

Honest status: on real hardware the layer heard, thought, spoke aloud and moved
the robot (a rule fire became a spoken reaction; `run_behavior` and `goto` were
admitted by the live engine). It has **not** yet held a sustained two-way
conversation — the test box has one audio output, which blocked the
browser-harness acceptance run — and `harmonics`, `create_rule` and the
clip→worker-model leg are not yet exercised live. The **two-tempo split has
not been judged from the room at all** — it is proven by the offline suite and
one gateway probe — and two of its pieces cannot pass yet: per-utterance
arming and the conversation-item channel both wait on upstream lobes-cli#170,
so today the room is still answered aloud and the gateway's own history
overstates after an interruption. See [What is proven live — and
what is not](docs/operating-reachy.md#what-is-proven-live--and-what-is-not).

## Export feed

`agent attach --export -` streams a live newline-delimited JSON (NDJSON)
feed of what the attached **agent** is thinking, proposing to say, and
proposing to express — one object per line. `agent attach` composes its speech
and pose tools **publish-only**, so a `message` block is what the agent
*proposed* saying, not proof of sound; audible speech comes from a rule's `say`
in the runtime and carries no block of its own. `agent embody --export -`
publishes the same three block types from the embodiment layer, where a
`message` is either a proposal the background mind made (the interjection
policy's verdict is in the same turn's `thinking` block) or an utterance the
realtime voice already spoke aloud. `behavior engine run --export -`
streams the complementary *runtime* feed (`sense` / `rule` / `intent` /
`motion`). The renderer stays **out of this repo** by design (the export
decoupling boundary): `reachy-mini-cli` emits a documented contract, a separate
consumer renders it.

```bash
reachy-mini-cli behavior engine run --export - > runtime.jsonl &  # the runtime feed
reachy-mini-cli agent attach --feed runtime.jsonl --export -      # all cognition block types
reachy-mini-cli agent attach --feed runtime.jsonl --export - --export-blocks message,emotion
reachy-mini-cli agent attach --feed runtime.jsonl --export - | <your renderer>
```

Wire format: [`docs/export-schema.md`](docs/export-schema.md). For the renderer
boundary and the reference `reterminal` consumer, see
[Export feed & the external renderer](docs/operating-reachy.md#export-feed--the-external-renderer).

## What you get

- **An agent-first CLI** cited from [teken](https://github.com/agentculture/teken)
  (`afi-cli`).
- **A mesh identity** — `culture.yaml` (`suffix` + `backend`) and the matching
  prompt file (`CLAUDE.md` for `backend: claude`).
- **A vendored skill kit** under `.claude/skills/` (18 skills — mostly from
  guildmaster, plus the devague chain and `ask-colleague`), cite-don't-import.
  See [`docs/skill-sources.md`](docs/skill-sources.md).
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
