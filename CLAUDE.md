# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is

`reachy-mini-cli` is an **AgentCulture mesh agent** whose domain is *operating
the Reachy Mini expressive robot — device setup, app management, and live
runtime ops* (see `culture.yaml`, the [README](README.md), and the
[operating guide](docs/operating-reachy.md)).

It began as a clone of `culture-agent-template`, and the template's agent-first
introspection verbs (`whoami`, `quickstart`, `learn`, `explain`, `overview`,
`doctor`, `cli`) are still the *pattern to copy* when you add a verb. But the
robot agent is now real and extensive: the CLI drives the daemon (`daemon`,
`device`, `app`, `move`), idle presence (`demo-mode`, `behavior`), the senses
(`listen`, `vision`, `think`, `pat`, `sleep`), speech (`say`), and a `think
--export` JSONL feed. When you build a new robot feature you are extending a
working agent — follow the existing nouns as the model, summarized in the
[Noun catalog](#noun-catalog) below.

## Critical naming gotcha

The half-rename has been resolved — the names now agree on `reachy-mini-cli`:

| Thing | Value |
|-------|-------|
| Installed console scripts (what you actually run) | **`reachy`** and **`reachy-mini-cli`** (both → `reachy.cli:main`) |
| Import package | `reachy` (unchanged — short and ergonomic) |
| Distribution / PyPI name | `reachy-mini-cli` (`__version__` reads this) |
| Transitional alias dist | `reachy-cli` — a metadata-only wheel that just depends on `reachy-mini-cli` (`packaging/reachy-cli/`) |
| `prog=` and every help/`learn`/`explain`/README string | `reachy-mini-cli` |

So `uv run reachy whoami` and `uv run reachy-mini-cli whoami` **both work**, and
`pip install reachy-mini-cli` / `pip install reachy-cli` install the same tool
(the alias pulls in the canonical dist). The import package stays `reachy` on
purpose. If you ever rename again, do it as one deliberate pass across
`pyproject.toml` (`name`, `[project.scripts]`), `prog=`, all `_commands/` +
`explain/catalog.py` strings, the README, the alias package, and the test
assertions — never piecemeal.

## Commands

```bash
uv sync                                              # create .venv, install (dev deps incl. teken)
uv sync --extra daemon                               # + reachy-mini (the reachy-mini-daemon binary)
uv run reachy whoami                                 # run the CLI (NOT `reachy-mini-cli`)
uv run reachy daemon start                           # bring the local daemon up (needs [daemon] extra)
uv run pytest -n auto                                # full suite (parallel)
uv run pytest tests/test_cli.py::test_whoami_text    # a single test
uv run pytest --cov=reachy --cov-report=term         # with coverage (CI gate: fail_under=60)
uv run teken cli doctor . --strict                   # the agent-first rubric gate CI enforces
```

Lint stack (CI `lint` job runs all of these; line length is 100 everywhere):

```bash
uv run black --check reachy tests
uv run isort --check-only reachy tests
uv run flake8 reachy tests
uv run bandit -c pyproject.toml -r reachy             # B101/B404/B603 skipped in pyproject
markdownlint-cli2 "**/*.md" "#node_modules" "#.local" "#.claude/skills" "#.teken"
```

## Architecture: the agent-first CLI

Everything routes through `reachy.cli.main()` → `_build_parser()`
(`reachy/cli/__init__.py`). The design exists to satisfy the **teken agent-first
rubric** (`teken cli doctor . --strict`), which gates CI — keep it green when
you touch the CLI.

**Map of this section:**

- [Core CLI contract](#core-cli-contract) — routing, adding a verb, the error
  contract, output split, `explain` catalog, identity/`doctor`.
- [The single-SDK-owner model](#the-single-sdk-owner-model-contributor-note) —
  the one hardware constraint every sense noun shares.
- [Noun catalog](#noun-catalog) — one table: each noun → module, key classes,
  transport.
- [Noun internals](#noun-internals) — the per-noun deep notes.

### Core CLI contract

- **Adding a verb:** create `reachy/cli/_commands/<verb>.py` exposing
  `register(sub)` (add a `--json` flag, `set_defaults(func=...)`), then import it
  and call `<verb>.register(sub)` inside `_build_parser()`. That is the only
  wiring step. Follow `whoami.py` as the canonical example.
- **Noun groups** (a subcommand with its own sub-verbs, like `cli`): when you
  call `p.add_subparsers(...)`, pass `parser_class=type(p)` so nested parse
  errors keep the structured error contract instead of falling back to
  argparse's default `stderr`/exit-2. A noun that has action-verbs must also
  expose an `overview` verb (rubric requirement) — see `cli.py`.
- **Error contract** (`reachy/cli/_errors.py`, `_output.py`): every failure
  raises `CliError(code, message, remediation)`; `_dispatch` catches it and
  wraps *any* other exception so no Python traceback ever leaks. `main()`
  pre-scans argv for `--json` into `_CliArgumentParser._json_hint` so even
  argparse parse-time errors (which fire before `args.json` exists) render as
  JSON when asked. Text errors are always two lines: `error: …` then `hint: …`
  (the `hint:` prefix is rubric-required). Exit policy: `0` success, `1` user
  error, `2` environment error, `3+` reserved.
- **Output split:** `_output.py` enforces results→stdout, errors+diagnostics→
  stderr, **never mixed**, in both text and JSON modes. Every verb takes
  `--json`.
- **Sense-stage logging** (`reachy/senselog.py` + `reachy/cli/_logging.py`): a
  parallel, opt-in observability layer, separate from the `--json`/text output
  contract above. `senselog.stage`/`senselog.drop` emit a fixed, grep-able
  `[SENSE stage=<stage> source=<source> event=<event>] <detail>` line on the
  dedicated `reachy.sense` logger; a drop always names its reason (`self-mute`,
  `min-utterance`, `cooldown`, `vlm-unreachable`, `audio-muted`, `tool-error`, a
  forge validator's joined rejection reasons, …) — never a silent no-op.
  `_logging.install_logging` attaches exactly ONE `stderr` `StreamHandler` to
  the `"reachy"` logger (the common ancestor every `reachy.*` module logger
  propagates to) at `listen`/`think`/`sleep run` entry, level from
  `--log-level` (`add_log_level_arg`) or `REACHY_LOG_LEVEL` (default `INFO`); a
  repeated call reuses the same handler (no duplicate lines). Stderr-only by
  construction, so `--export -`'s stdout stays pure JSONL.
- **`explain` catalog** (`reachy/explain/`): markdown keyed by command-path
  tuples in `catalog.py`'s `ENTRIES`. `test_every_catalog_path_resolves`
  verifies each catalog entry resolves — but nothing fails if you add a verb
  *without* a catalog entry, so add the `ENTRIES` key yourself when you add a
  verb.
- **Identity (`whoami`) & `doctor`:** `whoami` hand-parses `culture.yaml` with a
  line scanner (no YAML library) and walks up from `__file__` to find it.
  `doctor` re-implements the steward invariants (prompt-file-present,
  backend-consistency `claude`→`CLAUDE.md`, skills-present).

### The single-SDK-owner model (contributor note)

The hardware has **two single resources**, and the sense nouns share them — this
is the constraint behind several design choices below (notably #43). The
operator-facing explanation, the conflict matrix, and the diagram live in
[the operating guide](docs/operating-reachy.md#the-single-sdk-owner-model); the
contributor summary:

- **One SDK client (and its single-consumer media session).** Every `sdk` noun
  runs against one in-process `ReachyMini` client. `SdkTransport.media_session()`
  opens against the *one* `ReachyMini` media subsystem and is single-consumer
  (`reachy/robot/sdk_transport.py`, `MediaSession`). `listen` / `think` / `sleep`
  each open a media session; `vision` reads camera frames (`get_frame()` gated
  by `media.camera is not None` — the real SDK ≥1.9 surface; the old
  `media_manager.camera`/`is_local_camera_available()` guess never existed) and
  `pat` reads `head_pose()` — both through that same one SDK client. On the
  pinned SDK (`reachy-mini>=1.9.0,<1.10`), those camera frames arrive over
  **the daemon's local IPC endpoint** (`GStreamerCamera`, the LOCAL media
  backend) — the daemon always owns the physical camera, so it must be running
  for `vision` (and the folded face/scene hooks, below) to see anything.
- **One head (motion).** Every move flows through one serial `MotionQueue`
  (`reachy/motion/queue.py`), drained one move at a time.

| Two `sdk`-sense nouns as separate processes | Result |
|---|---|
| any two of `listen`/`think`/`sleep`/`vision`/`pat` | Contend for the single-consumer SDK client; the loser throttles to ~1 Hz |

Two consequences for code you write:

- **Fold senses into one loop instead of running two processes.** #43 does this:
  `reachy/motion/listen_pat.py` `PatHook` runs head-pat detection *inside*
  `listen`'s loop (via the `on_tick` seam) rather than as a separate `pat`
  process that would contend and get throttled.
- **The `*_active.flag` files coordinate the shared *head*, not the media
  session.** `think_active.flag` / `pat_active.flag` / `sleep_active.flag` let
  `listen`'s idle layer yield the motion channel by priority
  (`sleep` > `pat` > `think`); they do not lift the single-media-session limit.

### Noun catalog

Every noun → its command module, key engine module(s)/classes, and default
transport. Deep notes for the non-trivial nouns follow in
[Noun internals](#noun-internals).

| Noun | Command module | Engine / key pieces | Transport |
|---|---|---|---|
| `daemon` | `_commands/daemon.py` | `reachy/daemon.py` (process mgmt, `is_robot_live`) | none |
| `device`/`app`/`move` | `_commands/{device,app,move}.py` | `reachy/robot/*` transports | `http` default |
| `demo-mode` | `_commands/demo_mode.py` | `reachy/alive.py`, `reachy/motion/idle.py`, `demo_config.py`, `demo_service.py` | `sdk`/`http` |
| `behavior` | `_commands/behavior.py` | 50 Hz engine, per-channel contention | `sdk`/`http` |
| `listen` | `_commands/listen.py` | `reachy/motion/listen.py` `ListenProducer`, `snap.py`, `listen_pat.py` `PatHook` (#43); `--live`: `listen_hooks.py` `HookChain` + `sense_sample.py` + `listen_{think,vision,sleep}.py` + `speech/voice.py` (`--voice-engine`, `--live` only) + `speech/agent_turn.py` `AgentTurnEngine` + `speech/tools.py` `ToolRegistry` (`--cognition agent`, `--live` only); `motion/supervisor.py` | `sdk` default |
| `vision` | `_commands/vision.py` | pixel motion/light detectors, serial MotionQueue | `sdk` default |
| `say` | `_commands/say.py` | `reachy/speech/{tts,harmonic,voice,playback}.py` | `sdk` default |
| `think` | `_commands/think.py` | `reachy/speech/{llm,cognition,events,markers,expressions,distinctness,cognition_signal,harmonic,voice}.py`, `reachy/motion/expression.py`, `reachy/export/*`, `speech/supervisor.py` | `sdk` default |
| `pat` | `_commands/pat.py` | `reachy/motion/{pat,pat_reaction,pat_signal}.py` | `sdk` only |
| `sleep` | `_commands/sleep.py` | `reachy/sleep/{state,stimulus,wake,patwake,wakeword,supervisor}.py`, `reachy/motion/{sleep,sleep_signal}.py` | `sdk` default |
| `service` | `_commands/service.py` | `reachy/service/{units,manager}.py` (`ServiceManager`, systemd `--user`) | none (systemd) |

## Noun internals

### `daemon` noun & process module

`device`/`app`/`move` are *clients* of a running daemon;
`reachy/cli/_commands/daemon.py` + `reachy/daemon.py` are the other half — they
start/stop/status the local `reachy-mini-daemon` OS process (background spawn +
PID/log under `$REACHY_STATE_DIR` / `$XDG_STATE_HOME/reachy`, health-poll via
`GET /api/daemon/status`). Pure stdlib (`subprocess`/`signal`/`urllib`); the
daemon *binary* comes from the `[daemon]` extra. Its `overview` is hand-built (no
`--transport sdk` line) — `daemon` does NOT use a transport, so it does not call
`_robot.noun_overview`/`get_transport`. A missing binary raises a clean exit-2
`CliError` pointing at the `[daemon]` install. `is_robot_live()` (also in
`reachy/daemon.py`) provides SDK-based daemon liveness that stays correct across a
daemon restart (fixes issue #21).

### `listen` noun — two-tier `ListenProducer` (SDK-first)

The `listen` loop is implemented as a two-tier `ListenProducer`:

- *Tier 1 — antenna lean:* On every tick the antennas lean toward the current
  DoA (head holds). Always active; gives a subtle "perked ear" reaction to live
  sound.
- *Tier 2 — head→body turn:* Fires on detected speech or a loud RMS "snap"
  transient. The head turns first; if the DoA is beyond `--head-only-band` the
  body rotates to face the source and the head re-centers. A **latched-DoA guard**
  prevents the daemon's frozen DoA angle (which stays at the last live angle at
  rest) from firing a spurious turn — Tier 2 only fires on live speech/snap.
- `SnapDetector` (`reachy/motion/snap.py`) detects RMS spikes: an RMS value
  above `snap_ratio × floor` triggers a snap. Algorithm cited from
  `reachy_nova`'s `TrackingManager.detect_snap`.
- The `sdk` transport streams mic audio via `reachy_mini.ReachyMini().media` /
  `media_session()` in-process — real DoA + real RMS per tick. This is listen's
  default transport. The `http` transport polls the daemon's DoA endpoint instead;
  use `--transport http` / `REACHY_TRANSPORT=http` for remote control-box
  deployments.
- Both tiers drive the smooth minjerk `goto` planner one move at a time (serial
  motion queue), so turns are soft and never conflict.
- **Pat folded in (#43):** head-pat detection runs *inside* the `listen` loop via
  `reachy/motion/listen_pat.py` `PatHook` (a per-tick `on_tick` seam), not as a
  separate `pat` process — a separate process would contend for the
  single-consumer SDK client and get throttled to ~1 Hz. The hook mirrors `pat`'s
  detect→react logic, measuring deviation against the loop's *commanded* head pose.
  Because `commanded_head` is the *target* of the last dispatched `goto` (the head
  spends >1 s in transit), the hook additionally takes a `motion_busy(t)` probe —
  `server.run` publishes its `busy_until` horizon into a shared dict — and **skips
  sensing while a commanded move is in flight**, re-baselining the detector on the
  first pass after any suspension. Without that gate the robot's own transit reads
  as external force and the reaction→idle-resume→transit cycle re-triggers forever
  (the false-fire loop fixed in #66). Under `--live`, each detection also feeds
  `EventBuffer.feed_pat` (touch reaches cognition — one cue per reaction cycle,
  fault-isolated so a raising buffer never breaks the reflex). See
  [the single-SDK-owner model](#the-single-sdk-owner-model-contributor-note).
- **`--live` — every live sense in one loop:** `listen run --live` folds `think`,
  `vision`, `sleep`, face recognition, and periodic scene description *into*
  `listen`'s loop alongside the `PatHook`, so every live sense rides the **one**
  SDK media session and the **one** `MotionQueue` in **one** process — the same
  single-SDK-owner argument that motivated #43, applied to every sense. The
  folded hooks are `reachy/motion/listen_{think,vision,sleep,face,scene}.py`,
  composed by `reachy/motion/listen_hooks.py` `HookChain` — a single `on_tick`
  callable that fans the seam out across the hook list (per-tick `try/except`
  isolation + per-hook `close`). The audio hooks (`think`, `sleep`) do **not** open
  a second `media_session()`; they read the loop's own per-tick reading through the
  shared `reachy/motion/sense_sample.py` `SenseSample` provider (a per-tick tap on
  the value the loop already pulls). `listen_face.py` `FaceHook` and
  `listen_scene.py` `SceneHook` likewise open **no second camera grabber**: both
  take a `frame_provider` bound to `VisionHook.latest_frame` — a non-consuming
  peek at `VisionHook`'s own background grabber's latest frame (as opposed to
  `VisionHook`'s own `take()`, which consumes it) — so up to three consumers
  (vision, face, scene) share the ONE frame source with no second thread hammering
  `get_frame()`. Hooks run in descending idle-interrupt priority `sleep > pat >
  think` (vision rides last — it competes for nothing the flags arbitrate — with
  face then scene riding after it, sharing its frame grabber). `face`/`scene` also
  need the `[vision]` extra (opencv); absent, each is skipped with a single logged
  warning rather than crashing the loop (see the `[vision]` extra note in Hard
  constraints). Off by default (`sdk` only) — bare `listen run` is unchanged. This
  is the loop the `live` boot-presence service runs (see the `service` noun
  below). Which engine drives the folded `ThinkHook` — the marker path or the
  tool-use agent — is `--cognition`'s job (see its own bullet below); `--live`
  itself is unopinionated about that choice.
- **`--live --export -` — stream what the robot is thinking:** `--live` exposes the
  same `--export`/`--export-blocks` JSONL feed as `think run --export` (built by the
  shared `reachy/cli/_export.py` `build_export_hook`, so the two feeds can't drift).
  The folded `ThinkHook` engine is wired with that export hook, so the boot-persistent
  presence loop can publish `thinking`/`message`/`emotion` blocks to any subscriber
  (a reTerminal panel, a log tail, an audio renderer) over the one documented wire
  contract (`docs/export-schema.md`). `--export` requires `--live` and the `sdk`
  transport (both clean exit-1 errors otherwise); when exporting, stdout is reserved
  for the pure JSONL feed and every banner/action/summary line goes to stderr.
- **`--live` cognition is TTS-resilient:** the folded engine is built
  `audio_optional=True`, so a wedged/unreachable TTS endpoint degrades to "no speech"
  (logged once, the clip skipped) instead of raising out of the cognition worker and
  killing live thinking — and the audio sink latches off after a short run of
  consecutive failures so a hard-down TTS never throttles cognition. Thoughts keep
  flowing to the expression + export sinks regardless (they are driven on the producer
  thread, ahead of and independent of the speak worker). Standalone `think run` /
  `say` keep the strict default (an unreachable TTS is a clean exit-2). The
  `CognitionEngine(audio_optional=...)` flag lives in `reachy/speech/cognition.py`.
- **`--voice-engine {tts,harmonic}` — pick the folded cognition's speech
  backend:** `--live`-only (a bare `--voice-engine` without `--live` is a
  clean exit-1 error, see `_resolve_voice_engine`); default `tts`, env
  `REACHY_VOICE_ENGINE`. `harmonic` swaps in `reachy/speech/harmonic.py`'s
  in-process, offline note-melody synth (resolved via `reachy/speech/voice.py`,
  shared with `say`/`think`) at its own sample rate — self-mute, playback, and
  motion are unchanged downstream. The deployed `live` boot unit passes
  `--voice-engine harmonic` (see the `service` noun below).
- **`--cognition {marker,agent}` — pick the folded live cognition engine:**
  `--live`-only (a bare `--cognition` without `--live` is a clean exit-1 error, see
  `_resolve_cognition`); default `"marker"`, env `REACHY_COGNITION`. `"marker"` is
  the established `CognitionEngine` (`*emoji*`/`"speech"` parsing, unchanged);
  `"agent"` swaps in `reachy/speech/agent_turn.py`'s `AgentTurnEngine` — a tool-use
  loop that acts through `reachy/speech/tools.py`'s `ToolRegistry` (`speak` /
  `harmonics` / `apply_pose`) instead of parsing markers out of free text.
  `apply_pose` advertises the expression catalog as a JSON-schema `enum`
  (generated from the loaded `expressions.toml` keys, so a new TOML entry reaches
  the model with no code change) and rejects an unknown emoji with an error
  tool-result naming the valid keys — the model self-corrects in-turn instead of
  silently no-oping to neutral. Both
  engines share the same folded `ThinkHook` seam, the same `EventBuffer`, and the
  same export sinks, so `agent` is a drop-in with no new process and no second
  media session. In `agent` mode `--voice-engine` is inert (both `tts` and
  `harmonic` are always registered as separate tools — see the `say`/`think` noun
  notes below); it still governs `marker` mode's single speech backend. The
  deployed `live` boot unit passes `--cognition agent` (see the `service` noun
  below) — the boot presence reasons via tool calls by default.
- **`--transcribe` — live cognition hears WORDS, not just sound:** `listen run
  --live --transcribe` (requires `--live`, `sdk`-only) transcribes nearby speech and
  feeds the recognised words into the *same* cognition `EventBuffer` the folded
  `ThinkHook` engine consumes, so the LLM reasons about *what* was said. Off by
  default; when off `SenseSample.audio` stays `None`, no `TranscribeHook` is built,
  and no STT request is made (byte-identical). The path: the loop's **single**
  per-tick mic read (`_audio` in `_run_sdk_loop`) also retains the raw chunk on
  `SenseSample.audio` (no second `get_audio_sample()`); the folded
  `reachy/motion/listen_transcribe.py` `TranscribeHook` rides that shared sample
  (single-SDK-owner — it opens no media session) and hands audio to the shared
  `reachy/speech/stt.py` `Transcriber` (the model-gear / Parakeet
  `/v1/audio/transcriptions` leg, also reused by `sleep`'s wake-word
  `HttpSttBackend`). STT is external behind `REACHY_STT_URL` (default
  `localhost:9002`), no on-box model bundled.
  - **Utterance endpointing, not snippets.** The hook accumulates a whole utterance
    while `speech` holds and transcribes it **once** on a pause (`silence_hold_s`) or
    at `max_utterance_s`, via `Transcriber.transcribe_once` (a single-POST that
    bypasses the rolling-window/throttle of `transcribe`) — so the LLM gets full
    sentences, not 1.5 s fragments. Sub-`min_utterance_s` blips are dropped.
  - **Layered engagement gate (`reachy/speech/engagement.py` + `reachy/speech/name_match.py`).**
    A transcribed utterance passes through a cheapest-first decision stack before reaching
    cognition. After the built-in self-mute and min-utterance shortcuts:
    1. **Fuzzy name fast-path** (`is_name_match` in `reachy/speech/name_match.py`) — checks
       every word in the utterance against the canonical names (`reachy`/`robot`) and a set
       of common STT mishearings (`richie`, `reachie`, `richy`). The matcher uses a combined
       `difflib_ratio × length_ratio` score with three structural guards (prefix guard,
       superstring guard, initial-letter guard) so the ubiquitous word "speech" never
       false-triggers. An exact or close-enough match → ENGAGE immediately, **zero classifier
       calls**.
    2. **Single-shot LLM classifier** (`EngagementClassifier` in `reachy/speech/engagement.py`,
       backed by the new non-streaming `reachy/speech/llm.py` `complete()`) — for a coherent
       utterance with no name hit, judges "is this addressed to the robot, given the recent
       conversation?" against up to the last 6 accepted turns. Verdict YES → ENGAGE;
       NO → DROP. At most one `REACHY_OPENAI_*` endpoint call per utterance (same endpoint
       as cognition, `DEFAULT_CLASSIFIER_TIMEOUT = 5 s`).
    3. **DEGRADE fallback** — if the classifier raises (network error / timeout / unparseable
       response), `decide_engagement` returns `Decision.DEGRADE` and `TranscribeHook._decide`
       falls back to `_should_engage` (the original coherent-sentence-in-window heuristic).
       The hearing loop never stalls; classifier failures are logged once.
    4. Anything else is **DROP** — ambient human-to-human chatter never feeds cognition.
    - **Escape hatch:** `REACHY_ENGAGE_HEURISTIC=1` (or `true`/`yes`/`on`) forces the pure
      heuristic gate (`_should_engage`) throughout the process lifetime — no classifier is
      even built. Useful for debugging or when the LLM endpoint is unavailable at boot.
    - **History context:** on an ENGAGE decision the utterance is appended to a rolling deque
      (`_HISTORY_MAXLEN = 6`) handed to subsequent classifier calls, so ongoing conversation
      context flows forward without accumulating unboundedly.
  - **3-tier motion ladder** (`reachy/motion/listen.py` `ListenProducer`), replacing the
    blanket `turn_enabled=False` suppression that previously muted all head motion under
    `--transcribe`. Reaction is graded by perception level:
    - **Noise** (live sound, no speech flag) → Tier-1 antenna lean only (near-side antenna
      deflects toward DoA — unchanged).
    - **Speech** (detected speech, no engaged signal) → a bounded head-only orienting nudge
      toward the DoA (`speech_orient_gain × clamped_target`, capped at `speech_orient_max`
      degrees, never escalates to body rotation).
    - **Engaged** (gate ENGAGE → `set_engaged()` latch) → the full deliberate head/body
      turn (`_engaged_turn`), identical to Tier-2 except its duration is floored to
      `engaged_min_dur` (default 1.5 s) so the SDK `goto` planner's
      `time value out of range [0,1]` fault can never fire on a large turn angle.
    - `on_engage` wiring: `TranscribeHook.__init__` accepts an `on_engage` callback; the
      composition layer (`_commands/listen.py`) wires it to `producer.set_engaged`, so
      exactly one deliberate turn fires per addressed utterance, independently of cognition
      (the words still reach `EventBuffer` even if the motion callback raises).
  - **Words drive cognition, not raw sound.** Under `--transcribe` the `ThinkHook` is
    built `feed_doa_cues=False`, so raw DoA/loudness cues no longer feed cognition —
    only transcripts do. With `run_turn` cue-gated, the robot stays **quiet until
    someone speaks words** and never reacts to its own TTS as "loud sound" (the
    feedback loop that motivated this). The transcript carries direction:
    `feed_transcript(text, direction=...)` → `heard someone say (from the left): "…"`.
  - **Self-mute covers the whole clip.** The `play_audio` wrapper stamps
    `mute["until"]` for the clip's full play **duration** + a margin, so the robot
    never transcribes its own (possibly long) voice. An unreachable STT degrades to
    "no words" and never stalls the loop.
  The deployed `live` boot unit opts in (`listen run --live --transcribe
  --cognition agent --voice-engine harmonic`). It is still **not** a barge-in
  assistant — words are one more perception, now gated to clear, addressed
  speech by the layered engagement gate.

### `say` noun — dumb TTS pipe

`reachy/cli/_commands/say.py` exposes `run` (text → TTS → playback) and
`overview`. It MUST NOT import `reachy.speech.llm` or `reachy.speech.events` —
tests assert this boundary. TTS is via `reachy.speech.tts.synthesize` — model-gear's
**Chatterbox** HTTP (`POST {REACHY_TTS_URL}/v1/audio/synthesize`, JSON
`{"text","voice"}`, `voice:null` default selects the built-in voice, response is bare
PCM16 mono **24 kHz**; `REACHY_TTS_URL` / `REACHY_TTS_VOICE`). Playback is via
`reachy.speech.playback.play_audio` — `sdk` (default, pushes PCM via
`reachy_mini.media`) or `http` (daemon `/media/play` route). The **sdk** path
resamples the PCM to the speaker's real output rate (16 kHz) before pushing, because
`push_audio_sample` plays at the device rate without resampling — otherwise 24 kHz
audio plays ~0.67× slow/low-pitched. No LLM, no event bus, no senses; safe to compose
in pipelines.

`--voice-engine {tts,harmonic}` (default `tts`; env `REACHY_VOICE_ENGINE`,
resolved by `reachy.speech.voice.resolve_voice_engine`) swaps the whole leg for
`reachy.speech.harmonic.synthesize` — an in-process, offline note-melody voice
(see the `think` noun below for the shared `reachy/speech/{harmonic,voice}.py`
module notes). The TTS-only flags (`--voice`/`--speed`/`--tts-url`) are
accepted but ignored, and documented as such, under `--voice-engine harmonic`.

Under `--cognition agent` (the `listen --live` tool-use engine, below), `say`'s
TTS leg and `think`'s harmonic leg are not an either/or choice: `reachy/speech/tools.py`
registers **both** as separate tools — `speak` (TTS) and `harmonics` (the melodic
voice) — reusing exactly the same `synthesize` + `play_audio` seams this noun and
`think` already use. The agent picks per utterance instead of the process picking
one engine for the whole run.

### `think` noun — continuous cognition loop (SDK-first)

`reachy/cli/_commands/think.py` exposes `run` (foreground) +
`start`/`stop`/`restart`/`status` (background process) + `demo` (drive a fixed
scripted `*emoji*` / `"speech"` stream through the real marker→expression+TTS
path, no LLM — for on-robot verification) + `overview`. The `reachy/speech/`
package provides the engine:

- `reachy/speech/llm.py` — pure `urllib` streaming LLM client
  (`REACHY_OPENAI_URL_BASE` / `REACHY_OPENAI_API_KEY` / `REACHY_OPENAI_MODEL_ID`,
  with the legacy `REACHY_LLM_*` names honoured as a fallback; no OpenAI SDK, no
  new base dep).
- `reachy/speech/tts.py` + `reachy/speech/playback.py` — shared with `say`;
  `think` reuses the same TTS + playback leg.
- `reachy/speech/voice.py` + `reachy/speech/harmonic.py` — `--voice-engine
  {tts,harmonic}` (env `REACHY_VOICE_ENGINE`) selects the `synthesize`
  callable + playback samplerate `CognitionEngine` uses; `harmonic` is a
  pure-stdlib, offline note-melody voice (identity/articulation tunable via
  `REACHY_HARMONIC_IDENTITY` / `REACHY_HARMONIC_ARTICULATION`), a drop-in for
  the TTS leg with zero engine changes. `think demo` honours the same flag
  (a no-LLM on-robot verification path, mirroring how `demo` already verifies
  expressions); `think status --json` reports the running loop's
  `voice_engine` (via a `think.voice` sidecar file written for the run's
  lifetime); the startup banner names the active engine too.
- `reachy/speech/events.py` — `EventBuffer` accumulates per-tick DoA / RMS /
  speech cues; `CognitionEngine.run()` consumes them.
- `reachy/speech/cognition.py` — `CognitionEngine`: calls the LLM with the
  buffer snapshot, streams sentences, synthesizes + plays each sentence while
  the LLM streams the next (sentence-streamed overlap).
- `reachy/speech/supervisor.py` — manages `think`'s background process (PID +
  log under `$REACHY_STATE_DIR`). **Distinct** from `listen`'s
  `reachy/motion/supervisor.py` — they track separate processes.
- Sense feed mirrors `listen`: `sdk` transport opens a `ReachyMini`
  `media_session()` and reads DoA + mic RMS per tick; `http` transport polls
  the daemon's DoA route (no audio source, RMS = 0). Two-noun split: `say` =
  dumb TTS pipe; `think` = cognition loop that reuses `say`'s speech leg.
- **`*emoji*` / `"quoted"` output convention:** the cognition LLM interleaves
  expression markers and speech. `reachy/speech/markers.py` — streaming
  `MarkerParser` state machine: `*…*` → `MarkerEvent(emoji=…)` (drives a body
  expression); `"…"` → `SpeechEvent(text=…)` (spoken aloud). Text outside
  these delimiters is silently discarded. The parser is incremental (char-by-char)
  so split LLM token chunks are assembled correctly; unclosed spans at flush-time
  are silently dropped.
- **Expression catalog** — `reachy/speech/expressions.toml`: emoji-keyed TOML
  tables, each mapping to a 9-axis `ExpressionPose` (head mm/deg, antenna deg,
  body_yaw deg). Loaded via stdlib `tomllib` (no new dep). `NEUTRAL_KEY =
  "neutral"` is the all-zeros fallback for unknown emoji. `Catalog` (thin
  wrapper), `load_catalog`, and `get_pose` in `reachy/speech/expressions.py`.
  Starter set: 🤔 😮 🙂 👂 😐 🎉 😔 😊 and neutral. Edit this file to tune poses
  without any code change.
- **`ExpressionProducer`** (`reachy/motion/expression.py`) — enqueues calm
  one-shot expression moves onto the shared serial `MotionQueue` from the
  cognition thread. `think`'s `_MotionExecutor` runs a dedicated background
  thread that drains the queue to the robot; motion errors degrade silently so
  a transport drop never kills the cognition loop.
- **`reachy/speech/distinctness.py`** — `find_too_similar(catalog, threshold)`
  computes weighted Euclidean pose distances (normalised by per-axis amplitude
  σ) and returns pairs below the threshold. The neutral entry is excluded from
  pairwise comparison. Default threshold `0.5`; starter catalog passes cleanly.
- **`think expressions` sub-noun** (registered in `_register_expressions`):
  - `think expressions` / `think expressions list` — emit each catalog emoji
    with a generated pose descriptor (non-zero axes and signed magnitudes).
  - `think expressions check` — run `find_too_similar`; exit 0 always
    (flagged pairs are warnings); `--json` `ok` is the machine-readable signal.
  - `think expressions overview` — describe the sub-noun (rubric-required).
  - Both verbs support `--json`.
- **Cognition signal** (`reachy/speech/cognition_signal.py`) — `cognition_active()`
  context manager writes `think_active.flag` (under `state_dir()`) on enter and
  removes it on exit (including on exception). `is_active()` is a pure
  `Path.exists()` check with no I/O cost. The `listen` motion producer
  (`reachy/motion/listen.py`) calls `cognition_signal.is_active()` on every
  idle tick and swaps in a low-energy `_focused` `AliveConfig` while the flag
  is present — so the idle wander drops to a quiet "focused breathe" while
  `think` runs, making stillness the thinking posture.
- **Self-mute guard** — `think run` wraps `play_audio` so after each clip it
  stamps `mute["until"] = monotonic() + mute_after` (default 2.5 s). The
  `before_turn` sense feed checks this window and discards any sample captured
  inside it, preventing the robot from reacting to its own voice through the
  shared USB audio device.
- **`--export` / `--export-blocks` stdout JSONL sink** — `think run --export -`
  writes a live newline-delimited JSON (NDJSON) feed to stdout. Each line is one
  JSON object: `t` (block type), `ts` (unix timestamp), plus type-specific fields.
  Three block types: `thinking` (sense cues + full raw LLM turn text, including
  `*emoji*`/`"speech"` markers and any leading prose), `message` (text spoken
  aloud), `emotion` (emoji + 9-axis pose snapshot or `null`). `--export-blocks`
  accepts a comma-separated subset (e.g. `thinking,message`; default: all three).
  The sink lives in `reachy/export/` (`events.py` event model + `to_jsonl`,
  `blocks.py` `Selection` / `parse_blocks`, `exporter.py` `JsonlExporter`); wired
  in `reachy/cli/_commands/think.py`. The exporter is a passive tap on the
  cognition loop — it catches `BrokenPipeError`/`OSError`/`ValueError`, logs once
  to stderr, and silently disables itself so a disconnecting consumer never kills
  `think`. Only `-` (stdout) is supported in this version. See
  `docs/export-schema.md` for the full wire-format contract.

### `pat` noun — proprioceptive touch + snuggle (SDK-first)

`reachy/cli/_commands/pat.py` exposes `run` (foreground proprioceptive loop) +
`demo` (synthesize pat events, NO robot / NO `[sdk]` extra) + `overview`. There
is no touch sensor: the loop holds a baseline head pose, reads the *actual* pose
back via `reachy/robot` `head_pose()` (an SDK-only read-back), and feeds the
commanded-vs-actual deviation to a `PatDetector` (`reachy/motion/pat.py`, cited
from `reachy_nova` — numpy + stdlib only). A downward **pitch** press → `scratch`;
a sideways **yaw** nudge → `side_pat`; two intensities (`level1`/`level2`). On a
detection `PatReaction` (`reachy/motion/pat_reaction.py`) — a pure planner —
enqueues a calm lean→nuzzle→settle gesture (pitch-down for scratch; yaw-toward +
body_yaw for side_pat) onto the shared serial `MotionQueue`, drained by the same
`_MotionExecutor`/`reachy.motion.server.run` background-thread pattern as
`listen`/`think` (motion errors degrade silently). SDK-first by default; the
`http` transport cannot read `head_pose`. A missing `[sdk]` extra raises a clean
exit-2 `CliError`; `demo` works with no robot. While a reaction is enqueued, `pat`
writes `pat_active.flag` via `reachy/motion/pat_signal.py` (the counterpart to
think's `think_active.flag`) — the `listen` idle producer reads it and **pauses
the idle wander entirely** so the snuggle owns the motion (full suppression,
vs. think's focused-breathe). Determinism seams for tests: `PatDetector.update`
takes `now=` and the constructor takes `level2_threshold_fn`; `pat run` takes a
bounded `--ticks N` and injects the transport via `get_transport`. **Standalone
`pat run` is for an isolated bench check** — for live use alongside hearing, the
folded-in `PatHook` in `listen` (#43) is the supported path (see the
[single-SDK-owner model](#the-single-sdk-owner-model-contributor-note)).

### `sleep` noun — decay-to-sleep + wake (SDK-first)

`reachy/cli/_commands/sleep.py` exposes `run` (foreground decay loop) +
`start`/`stop`/`restart`/`status` (background process; `status --json` reports
state + idle timer + health) + `demo` (injected sense + fake clock, walks
ALERT→DROWSY→ASLEEP→wake in `--json`, NO robot / NO `[sdk]` extra) + `overview`.
The sleep subsystem lives in `reachy/sleep/`:

- `reachy/sleep/state.py` — `SleepState` enum (ALERT/DROWSY/ASLEEP) + an
  injected-clock idle timer; wall-clock dependency is fully factored out for
  determinism in tests.
- `reachy/sleep/stimulus.py` — qualifying-stimulation classifier: decides which
  incoming sense events reset the idle timer; includes a self-mute exclusion so
  the robot cannot wake itself from its own speaker output.
- `reachy/sleep/wake.py` — two-tier wake: Tier 1 (default) wakes on detected
  speech or a loud RMS snap transient (same signals as `listen` Tier 2).
  **Audio wake can be disabled** via `--no-audio-wake` (alias `--wake pat`) —
  in that mode only a physical head pat rouses the robot; requires the `sdk`
  transport (`http` raises a clean exit-2). Tier 2 adds optional wake-word
  detection (`--wake-word`) via a pluggable backend (`reachy/sleep/wakeword.py`
  `resolve_backend`): `http` (default — external **OpenAI-compatible** STT,
  stdlib `urllib`, targets the model-gear / NVIDIA **Parakeet** service
  `POST /v1/audio/transcriptions` as a multipart WAV upload; `REACHY_STT_URL`
  default `http://localhost:9002` / `REACHY_STT_PHRASE` / `REACHY_STT_LANGUAGE`
  / `REACHY_STT_TIMEOUT`; no extra required) or `openwakeword` (on-box, `[cpu]`
  extra, lazy-loaded). The `[gpu]` extra is a generic compute-class pin for
  future GPU features — it does NOT carry an on-box STT model. The HTTP backend
  accumulates a rolling ~1.5 s audio window (a single tick's mic chunk is far
  too short to transcribe a phrase) and POSTs at most once per `min_interval`;
  the real mic sample rate (from the SDK transport) is carried in the WAV
  header. Server-side serving is tracked in model-gear#39 (Parakeet GPU) /
  model-gear#40 (realtime facade route).
- `reachy/sleep/patwake.py` — `PatWakeDetector`: pat-based wake detector that
  measures head-pose deviation against the **moving** sleep-breathe commanded
  pose (not a fixed baseline), reusing `reachy/motion/pat.py` `PatDetector`
  (numpy + stdlib only). Used when `--no-audio-wake` is active.
- `reachy/sleep/wakeword.py` — `resolve_backend(kind)`: factory for the
  pluggable wake-word backend (`http` / `openwakeword`). The `http`
  `HttpSttBackend` calls the external OpenAI-compatible STT (Parakeet)
  `/v1/audio/transcriptions` as a multipart WAV upload (pure stdlib), matching
  the wake phrase against the response `text` (OpenAI/Parakeet shape; legacy
  `transcript`/`detected`/`phrase` also honoured). It buffers a rolling audio
  window + throttles POSTs (`window_seconds` / `min_interval`, both injectable);
  `openwakeword` is lazy-imported from the `[cpu]` extra and degrades gracefully
  when absent.
- `reachy/sleep/supervisor.py` — manages `sleep`'s background process (PID +
  log as `sleep.pid`/`sleep.log` under `$REACHY_STATE_DIR`). **Distinct** from
  `listen`'s `reachy/motion/supervisor.py` and `think`'s
  `reachy/speech/supervisor.py` — each noun tracks its own process.
- `reachy/motion/sleep.py` — `SleepProducer`: drowsy fade on the way down,
  quiet sleep-breathe cycle while ASLEEP, wake gesture on resumption; enqueued
  onto the same shared serial `MotionQueue` as `pat`/`listen`/`think`.
- `reachy/motion/sleep_signal.py` — `sleep_active.flag` counterpart to
  `pat_active.flag`/`think_active.flag`. Written while the robot is in DROWSY or
  ASLEEP state. The `listen` idle layer reads this flag as the **strongest idle
  interrupt** — higher priority than `pat_active.flag` (which pauses idle) and
  `think_active.flag` (which drops to focused-breathe) — and yields the motion
  channel entirely to `sleep`'s `SleepProducer`.

SDK-first by default; the `http` transport is available for non-pose ops. A
missing `[sdk]` extra raises a clean exit-2 `CliError`. Determinism seams for
tests: `SleepState` timer takes an injected clock; `sleep run` takes a bounded
`--ticks N` and injects the transport via `get_transport`; `demo` needs no
robot.

### `service` noun — boot-persistent single-presence (systemd `--user`)

`reachy/cli/_commands/service.py` exposes `enable {demo|live}` / `disable` /
`status` / `install` / `uninstall` / `overview`. It is the operator front for
making the robot survive a reboot in **exactly one** presence mode — the idle
`demo-mode` loop or the folded `listen run --live` loop, never both — the
single-SDK-owner model expressed across reboots. Like `daemon`, it does **not**
use a transport: it talks to **systemd** (`systemctl --user`), so it never calls
`_robot.get_transport` / `noun_overview` and its `overview` is hand-built.

- **Units (`reachy/service/units.py`).** Pure unit-text renderers (every function
  returns a `str`, no side effects) for the three units, with their canonical
  names exported as the cross-module contract `DAEMON_UNIT` /
  `DEMO_UNIT` / `LIVE_UNIT` (`reachy-daemon.service` /
  `reachy-demo-mode.service` / `reachy-live.service`). All three share
  `Type=simple` + `Restart=on-failure` + `RestartSec=5` (so a crash auto-restarts)
  and `WantedBy=default.target`. The two presence units additionally `Requires=` /
  `After=` the daemon unit — **the daemon is a boot dependency**, started first.
  The live unit's `ExecStart` is `<python> -m reachy listen run --live
  --transcribe --cognition agent --voice-engine harmonic` — the boot presence
  hears words, reasons through the tool-use agent, and speaks with the offline
  harmonic voice by default (a user decision; see the `listen` noun's
  `--cognition` / `--voice-engine` bullets above).
- **Manager (`reachy/service/manager.py` `ServiceManager`).** Enforces the
  **single-presence-owner invariant**: `enable(mode)` writes + `enable --now`s the
  daemon and the chosen presence unit and **always `disable --now`s the sibling**,
  so any sequence of enables leaves at most one presence enabled. `disable()`
  stops only the enabled presence and **leaves the daemon enabled** (explicit,
  reported as `daemon="left-enabled"` — other clients depend on it). `status()`
  reads `is-enabled` / `is-active` per unit + folds a daemon-health probe. Every
  side effect goes through injected seams (`run` / `unit_dir` / `daemon_health`),
  so it is exhaustively testable without real systemd.
- The command module's `install` / `uninstall` write/remove **all three** unit
  files + `daemon-reload` without enabling anything (so a separate `enable` chooses
  the mode). A missing `systemctl` on PATH raises a clean exit-2 `CliError`; an
  invalid mode is an exit-1 user error. Every verb supports `--json`. Boot at
  machine power-on (vs. first login) needs `loginctl enable-linger`; a true
  reboot check is a manual on-robot step.

### `reachy/stash/` package — behavior stash (not yet a noun)

A persistent, semantically searchable store of body behaviors, for the agent
tool-use path to fetch and adapt later (`docs/operating-reachy.md`'s "Behavior
stash" section has the operator-facing walkthrough). Not wired to any CLI verb
or agent tool yet — today it is driven via its Python API (`StashRecord`,
`StashStore`, `apply_record`) directly, e.g. from a script or REPL.

- `reachy/stash/record.py` `StashRecord` — a `reachy.behavior.library.LibraryEntry`-
  shaped record: a name, a natural-language `explanation` (the text embedded for
  search), a `generator` (must name an existing `reachy.behavior.library.LIBRARY`
  entry), typed `params`, `channels`, `stop_class`, `lifetime` — **declarative data
  only**. `StashRecord.from_dict` is the single validation gate and refuses
  anything smelling of code (an extra field, a non-JSON value, an unknown
  generator/channel/stop_class) with a clean `CliError`, by design — there is no
  `exec`/`eval` anywhere in this package.
- `reachy/stash/store.py` `StashStore` — `add(record)` embeds the explanation via
  the lobes gateway `/v1/embeddings` route (`reachy/stash/embeddings.py`, stdlib
  `urllib`, independent of `reachy/speech/llm.py`) and persists it; `search(query,
  k)` returns the top-k cosine-nearest records (`numpy` only, already a base dep —
  no new vector-db dependency). The index is one JSON file under
  `<state_dir>/stash/index.json` (`reachy.daemon.state_dir()`), robust to a
  missing/corrupt file (degrades to "start fresh", never raises).
- `reachy/stash/apply.py` `apply_record` / `plan_keyframes` — realizes a fetched
  record via the vetted `reachy.behavior.library.build()` path (the only callable
  source) and samples it into a bounded (`DEFAULT_MAX_KEYFRAMES`, default 8) series
  of `MotionAction` goto keyframes submitted onto a live loop's serial
  `MotionQueue` — the same queue family `ExpressionProducer` drives, not the 50 Hz
  `behavior` engine process.

### `reachy/forge/` package — qwen3 self-extension (wired under `--cognition agent`)

Runtime self-extension: an agent turn can hand a natural-language goal to a
coder model and, if what comes back passes a static safety gate, the robot
gains a new callable tool with no restart. Ported (cite-don't-import) from
`reachy_nova`'s `skill_forge.py` + `forge_validator.py`, split four ways so
dispatch, the safety gate, the disk/event lifecycle, and the activation policy
are independently testable (`docs/operating-reachy.md`'s "The forge loop" has
the operator-facing walkthrough):

- `reachy/forge/client.py` `ForgeClient` — `dispatch(goal, context, improve)`
  runs on a background thread: POSTs an OpenAI-compatible chat-completions
  request to `FORGE_BASE_URL`/`FORGE_MODEL`/`FORGE_API_KEY` (default: the
  lobes gateway cortex route, `http://localhost:8001/v1`, model `qwen3`),
  parses the two fenced blocks the prompt demands (```SKILL.md``` +
  ```executor.py```), and stages them. Every failure (unreachable endpoint,
  timeout, unparseable reply, a missing fence, a bad name, a failed stage, an
  unavailable validator) resolves to a loud rejection — never an exception on
  the caller's thread, never a hang.
- `reachy/forge/validator.py` `validate` — the fail-closed gate: **AST-only**,
  never imports or executes the generated code. Rejects anything outside an
  import allow-list (`numpy`/`math`/`time`/`typing`/`dataclasses`), a
  forbidden-name list (`exec`/`eval`/`os`/`subprocess`/…), dunder attribute
  access, a `ctx.<attr>` outside the sanctioned surface (default `{speak,
  harmonics, express, state_get, state_update}`, injectable), and a 200-line
  cap; requires a top-level `execute(params, ctx)`.
- `reachy/forge/lifecycle.py` — the disk + event layer:
  `<state_dir>/forge/staged/<name>/` → validated but not yet live;
  `<state_dir>/forge/staged/.rejected/<name>/` → where `reject()` quarantines
  a failed artifact (always logs the reason(s) first, never raises);
  `<state_dir>/forge/active/<name>/` → where `activate()` moves a staged
  skill. `stage()` is the ONLY path that emits `forge/staged`, and only ever
  called *after* validation passes.
- `reachy/forge/activate.py` — the runtime half deciding *when* a staged skill
  goes live: **validator-gated auto-activation, no human gate** (a confirmed
  product decision, matching nova). `ForgeActivator.publish` (the
  `ForgeClient`'s `PublishFn`) emits a `[SENSE stage=forge]` line for every
  `forge/*` transition and, on `forge/staged`, re-validates, imports the
  executor via `importlib.util.spec_from_file_location` (never registered in
  `sys.modules`, so one forged skill can never shadow another), wraps it in a
  crash-catching handler, and hot-registers it into the LIVE `ToolRegistry`
  via an injected `register` callback — callable on the **next turn**, no
  restart, because `AgentTurnEngine` reads `registry.tools()` fresh every
  round (contrast nova, whose Nova-Sonic session pins its tool config and
  needs one). `ForgedSkillContext` is the restricted `ctx` a forged `execute`
  receives: exactly `speak`/`harmonics`/`express`/`state_get`/`state_update`,
  each a thin defensive delegation to the SAME seams the built-in
  `speak`/`harmonics`/`apply_pose` tools use — no engine, no buffer, no
  transport reachable. `reload_active()` re-registers everything under
  `active/` at boot.

**Import boundary.** Like `reachy/stash/`, the forge stack is wired in at
composition, never imported by the modules it extends: `reachy/speech/tools.py`
and `reachy/speech/agent_turn.py` never import `reachy.forge` (asserted by
`tests/test_speech_tools.py` / `tests/test_agent_turn.py`) — the `forge`
dispatch seam and the `register`/`announce` callbacks are plain injected
callables. Composition (`reachy/cli/_commands/listen.py`'s `_activate_forge`)
builds the `ForgedSkillContext` over the same `speak_engine`/`harmonic_engine`/
`play`/`express` seams the built-in tools use, and wires `ForgeActivator.publish`
as `ForgeClient`'s `publish`. The `forge` tool is only advertised under
`--cognition agent` (it is part of the `ToolRegistry` the agent engine builds);
`--cognition marker` has no forge tool. A missing/broken forge stack disables
only the tool — cognition keeps running.

## Hard constraints

- **Base runtime dependencies — SDK-first, but installable.** Two packages are
  **base** runtime dependencies (`pyproject.toml`): `numpy` (the RMS loudness
  detector) and `harmonics-cli>=0.8` (the harmonic voice backend, import
  package `harmonics` — see the `say`/`think`/`listen` noun internals below).
  Both are pure wheels that install everywhere; `harmonics-cli` additionally
  has **zero transitive runtime deps** and is org-owned (AgentCulture), which
  is why it earns a base-dep exception — that exception does NOT extend to any
  other engine package (see the `[cpu]`/`[gpu]` note below). The SDK transport
  is `listen`'s **default**, but `reachy-mini` stays an **extra** (`[sdk]` /
  `[daemon]`), not a base dep, because its transitive stack (pycairo /
  gstreamer / pyaudio) needs system libraries absent on a bare box and in CI —
  a hard base dep breaks `uv sync` on the cairo build (learned the hard way on
  PR #24). So the **recommended default install is
  `pip install 'reachy-mini-cli[daemon]'`** (pulls `reachy-mini`); a bare
  `pip install reachy-mini-cli` is the HTTP remote profile — it still gets
  `numpy` + `harmonics-cli`, so `--voice-engine harmonic` works with no
  extra — and running the `sdk` transport without the extra raises a clean
  exit-2 `CliError` pointing at `[sdk]`. The HTTP transport stays available via
  `--transport http` / `REACHY_TRANSPORT=http`. Adding a *new* base runtime dep
  beyond these two needs an explicit decision (keep the base light enough for
  the remote profile). `teken` remains dev-only; `whoami` still hand-rolls
  YAML; `reachy/daemon.py` still uses stdlib only. The `[cpu]` extra is the
  home for on-box `openwakeword` (lazy-loaded; dep list empty until it gains a
  cp312 wheel). The `[gpu]` extra is a generic compute-class pin for future
  GPU-accelerated features — it does NOT bundle an on-box STT model (heavy STT is
  externally managed behind the HTTP STT service, `REACHY_STT_URL`). Both are
  lazy-loaded and the Tier 1 wake (speech/snap) never requires them — a bare
  `pip install reachy-mini-cli` still gets full Tier 1 wake functionality. The
  `[vision]` extra (`opencv-python-headless`) follows the same pattern for face
  recognition + scene description (`reachy/vision/face.py`, `reachy/vision/scene.py`'s
  JPEG-encode leg): lazy-imported, absent by default, and a missing extra
  degrades each folded hook (`FaceHook`/`SceneHook`) to a single logged warning
  instead of a crash — `pip install 'reachy-mini-cli[vision]'` to enable them.
  The pixel-only `vision` noun (motion/light orienting) needs no extra —
  numpy-only, unaffected.
- **Python ≥ 3.12** (uses `X | None`, `tomllib`, etc.).
- **Every PR bumps the version**, even docs/config/CI-only changes — the
  `version-check` CI job blocks the merge otherwise (it compares
  `pyproject.toml` version against `origin/main`). Use the `version-bump` skill;
  it also prepends a `CHANGELOG.md` entry. PyPI publish on push to `main` would
  fail on a duplicate version, hence the rule.

## CI / release

- `.github/workflows/tests.yml`: `test` (pytest + coverage + SonarCloud),
  `lint` (the stack above + the rubric gate), `version-check` (PR-only).
- SonarCloud quality gate (`sonar-project.properties`,
  `sonar.qualitygate.wait=true`) fails the `test` job on a red gate — but only
  when `SONAR_TOKEN` is set; token-less repos and fork PRs skip the scan and
  stay green.
- `publish.yml`: TestPyPI dev build on internal PRs, real PyPI publish on push
  to `main`, both via Trusted Publishing (no stored credentials). It publishes
  **two** dists: the canonical `reachy-mini-cli` (the real package) and the
  transitional `reachy-cli` alias (metadata-only, `packaging/reachy-cli/`, pinned
  to the same version). Both names need a Trusted Publisher configured on PyPI /
  TestPyPI for this repo + workflow + environment.

## Skills (`.claude/skills/`)

Vendored **cite-don't-import** from `guildmaster` (provenance + re-sync
procedure in `docs/skill-sources.md`). **Do not edit skill script bodies** — only
the consumer-identifying prose in `SKILL.md` is adapted; lift real changes
upstream into guildmaster and re-vendor. Most relevant for day-to-day work:

- **`cicd`** — the PR lane (create PR, handle review feedback, poll CI/Sonar
  status). Requires `agex` on PATH.
- **`communicate`** — cross-repo issues + Culture mesh messages. Requires
  `agtag` on PATH. Issue posts auto-sign `- reachy-mini-cli (Claude)`.
- **`version-bump`**, **`run-tests`**, **`sonarclaude`**, **`pypi-maintainer`**,
  **`agent-config`**, and the devague chain (`scope` → `think` → `challenge` →
  `spec-to-plan` → `assign-to-workforce` → `summarize-delivery`, with `deviate`
  as the mid-run escape hatch).

## Conventions and workflow

**Memory discipline — recall before, remember after.** This repo keeps its
eidetic memory **in-repo and public**: records resolve to
`<repo-root>/.eidetic/memory` — committed, and shared with the team and mesh
peers (the `claude` and `colleague` backends both read the same
`reachy-mini-cli` scope), so memory travels with the repo, not a private
home-dir store. Make it a per-task habit:

- **`/recall` before you start.** Search the store for the area you're about
  to touch — prior decisions, gotchas, "have we done this before?" — so you
  build on what's already known instead of re-deriving it. Do this before
  non-trivial tasks, not just when asked.
- **`/remember` when something worth keeping surfaces.** A non-obvious
  decision and its rationale, a constraint, a fix and *why* it was needed, a
  gotcha that cost time, a fact the next session would otherwise re-learn.
  Capture it as it happens, not at the end when it's faded.

A plain `/remember` lands the note in `./.eidetic/memory` in this repo — no
flag needed (the wrappers here default to `--visibility public`; in-repo
routing needs `eidetic >= 0.10.0`, older CLIs keep records in `$HOME`). Keep
something out of the committed store only by passing `--visibility private`
(routes to `$HOME/.eidetic/memory`, never committed); `/recall` reads both
stores and merges. Don't store what the repo already records (code structure,
git history, what's already in this file or `CHANGELOG.md`) — store what you'd
have to re-derive. These are the `recall`/`remember` skills (`.claude/skills/`),
backed by the `eidetic` store.
