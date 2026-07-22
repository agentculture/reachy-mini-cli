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
`device`, `app`, `move`), idle presence (`demo-mode`), the **symbolic runtime**
that composes every sense onto one 50 Hz tick (`behavior engine run`), the
standalone single-sense verbs (`vision`, `pat`, `sleep`), speech (`say`), boot
persistence (`service`), the external agent client (`agent attach`) and the
runtime's two JSONL feeds (`behavior engine run --export` and `agent attach
--export`). When you build a new robot feature you are extending a
working agent — follow the existing nouns as the model, summarized in the
[Noun catalog](#noun-catalog) below.

**The arc that got here.** Three surfaces were deleted on purpose: the `think`
noun, `listen run --live`'s folded cognition, and finally the `listen` noun
itself. Their capabilities were **ported before deletion** — sound orienting
into `reachy/behavior/orient.py`, the pat sense into
`reachy/behavior/pat_sense.py`, hearing into
`reachy/behavior/transcript_sense.py`, cognition out to `agent attach`. Do not
re-add a sense loop as a noun; extend `_compose_run_seam` in
`_commands/behavior.py` instead.

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
uv run reachy whoami                                 # run the CLI (`reachy-mini-cli` also works)
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
  propagates to) at `behavior engine run` / `sleep run` entry — the only two
  call sites — level from `--log-level` (`add_log_level_arg`) or
  `REACHY_LOG_LEVEL` (default `INFO`); a repeated call reuses the same handler
  (no duplicate lines). Stderr-only by construction, so `--export -`'s stdout
  stays pure JSONL.
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

The hardware has **two single resources**, and every `sdk` owner shares them —
this is the constraint behind several design choices below. The
operator-facing explanation, the conflict matrix, and the diagram live in
[the operating guide](docs/operating-reachy.md#the-single-sdk-owner-model); the
contributor summary:

- **One SDK client (and its single-consumer media session).** Every `sdk` noun
  runs against one in-process `ReachyMini` client. `SdkTransport.media_session()`
  opens against the *one* `ReachyMini` media subsystem and is single-consumer
  (`reachy/robot/sdk_transport.py`, `MediaSession`). The `behavior` runtime
  (via `robot/media_client.py`) and `sleep` each open a media session; `vision`
  reads camera frames (`get_frame()` gated
  by `media.camera is not None` — the real SDK ≥1.9 surface; the old
  `media_manager.camera`/`is_local_camera_available()` guess never existed) and
  `pat` reads `head_pose()` — both through that same one SDK client. On the
  pinned SDK (`reachy-mini>=1.9.0,<1.10`), those camera frames arrive over
  **the daemon's local IPC endpoint** (`GStreamerCamera`, the LOCAL media
  backend) — the daemon always owns the physical camera, so it must be running
  for `vision` (and the runtime's face sense) to see anything.
- **One head (motion).** Every move flows through one serial `MotionQueue`
  (`reachy/motion/queue.py`), drained one move at a time — except in the
  `behavior` runtime, which streams a composed pose at 50 Hz through its own
  channel arbitration instead.

| Two `sdk`-sense processes | Result |
|---|---|
| any two of `behavior engine run`/`sleep`/`vision`/`pat` | Contend for the single-consumer SDK client; the loser throttles to ~1 Hz |

Two consequences for code you write:

- **Compose senses onto ONE tick seam, never as two processes.** That seam is
  `_compose_run_seam` in `_commands/behavior.py`. This is why the folded
  `listen --live` hooks and the standalone sense nouns were retired rather
  than multiplied — a second process cannot get the media session it needs.
- **Nothing arbitrates the head on a flag file any more.** `pat run` and
  `sleep run` call `reachy/behavior/liveness.py`'s `refuse_if_engine_live()`
  FIRST — before constructing a transport — so a foreground verb beside a live
  engine is a clean exit-1 refusal, not a silently useless process. The
  `*_active.flag` files that used to coordinate this are now, at most, per-noun
  bookkeeping (see the [flag asymmetry](#the-two-flag-files-are-asymmetric)
  below). Do not build new cross-process arbitration on a flag file: a flag
  cannot expire, while the engine's `state.json` heartbeat does.

#### The two flag files are asymmetric

Their one cross-process reader was `listen`'s idle layer, which t22 deleted
with the noun. Keep the asymmetry straight when you touch either module:

- **`pat_active.flag`** (`reachy/motion/pat_signal.py`) — bench-local
  bookkeeping. Written by `pat run` while a reaction is enqueued, read back
  only by `pat run` itself for idempotent cleanup. `pat run` is a bench-test
  verb; live patting reaches the robot through `behavior/pat_sense.py`.
- **`sleep_active.flag`** (`reachy/motion/sleep_signal.py`) — **still
  load-bearing.** `cmd_sleep_status` reads it across processes, and it is the
  only way to observe a parked robot, because the state machine and its idle
  timer live inside the loop process. Written only while the machine is ASLEEP
  (not DROWSY). `sleep run` is a wanted capability ("park the robot"), not a
  bench verb — which is why the flag was kept where `think_active.flag` was
  deleted outright (t21 removed its writer, its reader and
  `reachy/speech/cognition_signal.py` in one pass; a stale file on a deployed
  box is inert).

### Noun catalog

Every noun → its command module, key engine module(s)/classes, and default
transport. Deep notes for the non-trivial nouns follow in
[Noun internals](#noun-internals).

| Noun | Command module | Engine / key pieces | Transport |
|---|---|---|---|
| `daemon` | `_commands/daemon.py` | `reachy/daemon.py` (process mgmt, `is_robot_live`) | none |
| `device`/`app`/`move` | `_commands/{device,app,move}.py` | `reachy/robot/*` transports | `http` default |
| `demo-mode` | `_commands/demo_mode.py` | `reachy/alive.py`, `reachy/motion/idle.py`, `demo_config.py`, `demo_service.py` | `sdk`/`http` |
| `behavior` | `_commands/behavior.py` | 50 Hz engine (`behavior/engine.py`) + rules/intents (`rules.py`/`rule_engine.py`/`intents.py`/`control.py`); composes the full sense stack — proprioceptive pat (`pat_sense.py` + `robot/state_reader.py`), loudness (`rms_sense.py`), heard words (`transcript_sense.py`), face + frame availability (`face_sense.py`), all reading the one held `robot/media_client.py` — a fail-closed live `goto` (`goto_intent.py` + `goto_lane.py`, seeded via `pose_feed.py`), and the background-worker voice (`speech_act.py`, reached from a rule's `say`) onto the same tick seam | `sdk`/`http` |
| `vision` | `_commands/vision.py` | pixel motion/light detectors, serial MotionQueue | `sdk` default |
| `say` | `_commands/say.py` | `reachy/speech/{tts,harmonic,voice,playback}.py` | `sdk` default |
| `pat` | `_commands/pat.py` | `reachy/motion/{pat,pat_reaction,pat_signal}.py` | `sdk` only |
| `sleep` | `_commands/sleep.py` | `reachy/sleep/{state,stimulus,wake,patwake,wakeword,supervisor}.py`, `reachy/motion/{sleep,sleep_signal}.py` | `sdk` default |
| `service` | `_commands/service.py` | `reachy/service/{units,manager}.py` (`ServiceManager`, systemd `--user`) | none (systemd) |
| `agent` | `_commands/agent.py` | `reachy/speech/{agent_turn,tools,intent_tools,events}.py` + `reachy/forge/*`, over `--feed` + the intents spool | none (feeds + spool) |

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

### `behavior` noun — pat sense, bounded lifetimes, and a live `goto`

`reachy/cli/_commands/behavior.py::_compose_run_seam` composes every runtime
sense/act piece onto the engine's ONE `TickBus`: `[rules_driver,
intent_driver, pat_driver, transcript_driver, face_driver, holder, goto_lane]`
(plus a `SenseSnapshotDriver` when exporting). Every piece below is import-safe
without `reachy_mini` and composed UNCONDITIONALLY — a bare box (no `[sdk]` /
`[vision]` extra) runs unchanged except for permanently-quiet sense fields.

**The two held clients, and the warm-up pair.** The runtime process owns
exactly TWO SDK clients (the single-SDK-owner model): `HeldStateReader`
(`ReachyMini(media_backend='no_media')`, the pose read-back) and
`HeldMediaClient` (the default profile — mic + camera, `reachy/robot/
media_client.py`). Both are built with `allow_inline_connect=False` and warmed
synchronously **during composition, before the first tick** — a pair that must
never be split, because construction blocks for **425-1213 ms** and charging
that to the tick thread is a measured 21x-61x tick-budget overrun on every
runtime start (`docs/verification/2026-07-20-retire-old-flow-baseline.md`
section 3). The flag alone silently disables the sense (reads never construct);
the warm-up alone leaves a mid-run fault free to rebuild inline and reproduce
the stall later. Warming on a background thread *after* ticking starts only
relocates the stall. A failed warm is a NORMAL daemon-not-up-yet outcome, so
`_HolderKeeper` (a background daemon thread) polls each holder's free
`connected` predicate and re-warms off-thread for the life of the run — the
tick thread never constructs. `_RuntimeResources` is what `cmd_engine_run`
closes at shutdown (both clients + the worker-owning sense drivers + the
audio pump); an unclosed client hangs the process at interpreter exit.

**Sense providers — all six wired.** `SenseProviders` carries `pat_event` /
`pat_state` (two peeks of the ONE `PatSenseDriver`), `rms`
(`behavior/rms_sense.py`), `transcript` (`behavior/transcript_sense.py`, a
background worker streaming mic audio to the lobes `/v1/realtime` session
(`speech/realtime.py`, server-side VAD — see below) + the #54/#56 engagement
gate) and `face` / `frame_available` (`behavior/face_sense.py`, a background
YuNet/SFace worker).
`rms` and the transcript driver are two consumers of ONE *consuming*
audio read. Since #100 that read is `AudioPump.take()` — a background daemon
thread (`behavior/audio_pump.py`) owns ALL `media.audio()` I/O, drains the
SDK appsink's `drop=True, max-buffers=500` FIFO at production pace, and
discards any standing backlog before going live (pulled at tick rate that
FIFO serves seconds-stale audio, and its empty-queue `get_sample` blocks
20 ms on the tick thread). `_AudioTap` swaps the pump's latch once per tick
(at the top of `sense_reader`) and fans the concatenated chunk out — taking
it twice would hand each consumer half the audio. Every audio read — the
pump's, the transcript driver's, and `HeldMediaClient.audio()` at the SDK
boundary — is coerced through `reachy/robot/audio_shape.py` `to_mono`, NOT a
bare `.reshape(-1)`: SDK 1.9 documents `get_audio_sample()` as `(N, 2)`, and
flattening that interleaves both channels into one double-length stream the WAV
header then mislabels. `to_mono` selects `AEC_CHANNEL` (0, `reachy_nova`'s
choice) and passes a 1-D read through untouched. Measurement over 829 archived
uploads says the deployed box delivers 1-D today, so this is a closed
portability hazard, not a fixed bug. **When you wire a new provider here you MUST extend
`reachy/behavior/sense.py`'s `_COMPOSED_PROVIDER_FIELDS` in the same change** —
it is the one declared source of truth `behavior rules check` lints against, so
a stale value makes the linter lie in one direction or the other.

- **Transcript sense — capture moved server-side (issue #115); the #108
  lesson survives its own machinery.** `transcript_sense.py` used to decide
  itself WHEN an utterance started and ended: a rolling pre-roll ring, a local
  energy VAD (`_is_speech`), a silence-hold timer, a measured onset, a
  wall-clock span floor — then one `POST /v1/audio/transcriptions` per
  finished clip. **All of that capture machinery is gone, not retuned.** The
  driver now streams every mic chunk, in order, exactly once, to ONE
  long-lived lobes `/v1/realtime` WebSocket session
  (`reachy/speech/realtime.py`'s `RealtimeTranscriber`, base64
  `input_audio_buffer.append` JSON TEXT events — never a binary frame,
  RFC 6455 primitives hand-rolled in `reachy/speech/realtime_wire.py`, cited
  from lobes-cli's `scripts/realtime-smoke.py`), and the server's own
  `server_vad` decides where the sentence ended
  (`speech_started`/`speech_stopped`/`transcription.completed`). There is
  **no local fallback** (a confirmed operator decision, spec claim c17): a
  down session is a quiet, latched `session-down` drop, never a reversion to
  local endpointing.

  **What #108 taught still applies, one level up — the category error, not
  the number, was the defect.** An energy predicate is a LOCATOR (it may say
  *when* to start listening), never a *content filter* (it may never decide
  *which audio is worth keeping*). The old code appended only the chunks that
  individually cleared an RMS threshold, so every stop closure and inter-word
  gap *inside* a sentence was excised and the survivors glued edge to edge —
  reproduced live: contiguous audio gave `'Richie, are you there?'`, the same
  phrase gated at the live background gave `'Reaching there.'`, then
  `'Return.'`, then `'Yeah.'` as the room got louder. The `0.02` constant was
  cited from `reachy_nova`'s onset threshold and then quietly repurposed from
  locator to filter across the port — that inversion is what recurs if a
  future capture path (local or otherwise) ever re-derives a threshold from a
  donor without re-deriving *what the donor used it for*. A second, related
  defect (issue #111) showed the fixed threshold itself was too high — 3x a
  drifted night background landed at 0.102, above a normal voice at ~2 m, so
  no utterance ever opened — and THAT is what #115's server-side move
  actually resolves: not a third threshold value, but removing the decision
  from the robot entirely. See [the operating guide's hearing
  section](docs/operating-reachy.md#hearing--server-side-vad-replaces-local-endpointing)
  for the full before/after evidence.

  `tests/test_behavior_transcript_contiguous.py` was **re-scoped, not
  deleted** (named in the t4/t6 PRs): it no longer asks "is the submitted clip
  a contiguous slice?" (there is no clip any more) but pins the property that
  outlived the local capture path — everything the microphone produces
  reaches the session in order exactly once, an utterance boundary changes
  nothing about the stream, self-mute is the one deliberate withholding (never
  a filter), and structurally no energy predicate survives anywhere in the
  capture path. `tests/fake_realtime_server.py` is the offline harness behind
  this and every other realtime test (`test_realtime_wire.py`,
  `test_realtime_client.py`, `test_behavior_transcript_realtime.py`,
  `test_behavior_realtime_composition.py`) — an in-process, scriptable
  loopback server covering the happy sequence and every named failure mode
  (refused handshake, mid-stream close, a missed pong, a malformed event,
  `vad_unavailable`, `stt_forward_failed`), stdlib-only and safe under
  `pytest -n auto`.
- **Pat sense** — `reachy/robot/state_reader.py` `HeldStateReader` holds ONE
  `ReachyMini(media_backend='no_media')` client for the process lifetime
  (construct-on-first-read, explicit idempotent `close()` — an unclosed
  `no_media` client hangs the process at interpreter exit) and reads the
  ACTUAL head pose back at tick rate with flat fd usage — a fresh client per
  read (what `SdkTransport.head_pose` does) is unusable at this rate.
  `reachy/behavior/pat_sense.py` `PatSenseDriver` runs at the END of each
  tick: commanded pose from `ctx.pose`, actual pose from the reader, both fed
  to a `reachy.motion.pat.PatDetector` (the same detector `pat run` and
  `sleep`'s `PatWakeDetector` use), and
  the result LATCHED (one-tick, cleared before every tick) for the next
  tick's single sense read. **Ownership-gated** (generalizes the #66
  false-fire fix): while a non-base behavior (a rule-admitted gesture, a
  goto) owns the head channel, detection suspends and re-baselines on
  resume, so the engine's own commanded motion can never read as a phantom
  pat. **Stillness-gated** (#80, the constraint that actually makes the sense
  work): detection runs only after the COMMANDED pose has been SLOW for
  `still_hold_s` — a per-tick VELOCITY tolerance, not exact constancy, which is
  why it can open inside the swinging idle's decelerate-pause window at all. Hands-on calibration measured the real plant in four
  recordings — still/wandering x untouched/petted, all six DOF — and found the
  separation between a pat and the noise floor is **12-20x with the head still**
  but **0.7-2.0x while it wanders**, on every axis including the ones
  `feel-alive` never commands (roll/x/y get dragged ~11x noisier by mechanical
  coupling). The residual is servo hunting, not lag: it is uncorrelated with
  commanded velocity, a fitted 40-tap FIR plant model removes only 1.1x of it,
  and it collapses to a 0.07-0.11 deg floor the moment the command stops moving.
  So the ghost class (#79) is closed structurally — a moving robot declines to
  guess. The recordings live in `tests/data/pat_*.csv` and back
  `tests/test_behavior_pat_sense_hardware.py`.

  **Shipped tuning is ONE operating point (v0.41.0)** — `still_eps` 0.035,
  `still_hold_s` 1.0 s, press 1.2 deg, `release_after_s` 2.5 s, `hp_tau` 0.8.
  These move TOGETHER or not at all. The sensitive 0.5 deg press belongs with a
  tight gate that only opens at a dead stop (the freeze-era pairing measured
  above, where the untouched residual is 0.07-0.11 deg); the looser gate senses
  inside the swing's slow window where that residual is 0.70 deg against a
  petted 2.52 deg, so it needs the blunter press to stay above it. Mixing the
  two — a loose gate with a sensitive press, or vice versa — is a phantom-pat
  or a dead sense respectively. Cost of the shipped pairing, stated plainly: on
  a head held genuinely still the petting p90 is 0.85-1.90 deg, so 1.2 misses
  the gentlest pats there; a caller driving a STATIC commanded pose should
  inject the sensitive detector rather than take the defaults.

  `hp_tau` is the one value a deployed box must never override downward: it is
  a high-pass TIME CONSTANT, so 0.08 s passes only fast transients while a pet
  is a SUSTAINED push lasting ~0.5-2 s. A box-local drop-in setting it silenced
  the sense entirely — the gate opened normally and the detector simply never
  saw the press, which reads in the journal as a bare `Pat level1!` with no
  `pat-acknowledge` fire.
- **`reachy/behavior/pose_feed.py` `LastPoseHolder`** — a `TickBus` driver
  stashing each tick's `ctx.pose` (now carried on `TickContext`) so a later
  rider can read "the robot's current pose" without re-deriving it from
  ownership + contributions. `as_start_pose_provider()` adapts that stash
  into `GotoLane`'s `start_pose_provider`, so a goto interpolates from the
  LIVE pose instead of snapping to neutral at `t=0`.
- **`reachy/behavior/goto_intent.py`** — the `goto` `KindRegistry` handler:
  fail-closed validation (unknown field / out-of-range axis / non-numeric
  value / runaway duration is REFUSED, never clamped) turns a payload into a
  `GotoSpec` and calls `GotoLane.submit`. Per-axis bounds are module
  constants cited against precedent; duration is capped at `MAX_DURATION_S =
  10.0`s. Registered into the intent driver's OWN `KindRegistry` at
  composition, never into `control.py`/`intents.py` (see the module's
  "Import boundary" docstring), so `behavior goto` (the CLI front,
  `_commands/behavior.py`) and an agent's equivalent call share one
  admission path.
- **`reachy/behavior/speech_act.py` `SpeechActuator`** — the runtime's VOICE,
  and the only genuinely BLOCKING side effect in the loop. Reached from a
  react rule's optional `say: str` (validated in `rules.py`, capped at
  `MAX_SAY_CHARS = 500`, refused fail-closed never truncated), dispatched by
  `RuleEngine._speak` through an INJECTED `speech(text)` seam — so
  `rule_engine.py` never imports the audio stack, and a missing seam is a
  named `no-speech-actuator` drop rather than silence. `say()` is O(1) on the
  tick thread (validate → `put_nowait` on a depth-2 bounded queue); a worker
  thread does synthesis + playback. A full queue, a wedged TTS or a dead
  speaker all resolve to a named drop, never backpressure onto the 20 ms
  budget — this is the same defect class as the 425-1213 ms startup overruns
  t27/t28 removed, and it must not come back. Three defaults are product
  decisions, not conveniences: the voice is `harmonic` (in-process, offline —
  deliberately NOT `speech/voice.py`'s own `tts` default, so a box with
  nothing reachable still speaks); playback defaults to the daemon's `http`
  route (the media-profile SDK client is unconstructable on the robot today,
  issue #94 — `REACHY_SPEECH_TRANSPORT=sdk` flips it back with no code
  change); and a persistently dead sink latches off for 30 s and RETRIES
  (time-bounded, unlike `AgentTurnEngine`'s permanent latch, because a
  boot-persistent robot must survive a daemon restart un-muted).
  `mute_until()` is wired at composition into `TranscriptSenseDriver`'s
  `mute_until` seam, so the robot cannot transcribe its own voice and answer
  itself. The library's `speak` entry stays PURE MOTION (a 50 Hz head bob with
  no sound) — pairing `run = "speak"` with `say = "..."` is the visible +
  audible halves of one reaction; giving that entry a side effect would put
  synthesis back on the tick thread.

**The bounded-lifetime invariant — enforced on BOTH admission surfaces.** A
background incident (a react rule admitting looping `nod` with library
defaults; the head oscillated until manually stopped) motivated refusing any
UNBOUNDED admission, fail-closed:

- **React rules** (`rules.py` `RulesConfig.from_dict` + `rule_engine.py`
  `RuleEngine._build`) — an optional, validated `duration_s: float > 0`. A
  rule targeting a looping-default entry (`nod`/`shake`/`speak`/
  `antenna-sway`/`feel-alive`) with no `duration_s` is refused at
  load/reload, naming the rule id and the fix; `_build` uses `duration_s` as
  the admitted `Lifetime`'s duration when present.
- **`run_behavior` intents** (`intents.py` `_validated_lifetime`) — the SAME
  defect class on the agent-facing surface: a resulting `looping=True,
  duration=None` lifetime — explicit or silent from a looping-default
  entry's own defaults — is refused. `declare_goal`'s STANDING, indefinite
  lifetime is intentionally exempt (the documented indefinite-intent
  surface).

See [the operating guide's pat sense](docs/operating-reachy.md#the-pat-sense),
[bounded reactions](docs/operating-reachy.md#bounded-reactions-no-more-permanent-holds)
and [speech](docs/operating-reachy.md#speech--the-say-field-gives-a-rule-a-voice)
sections for the operator-facing walkthrough and the deployed `rules.toml`
example.

### `reachy/motion/listen.py` — the surviving `ListenProducer` (no longer a noun)

**There is no `listen` noun.** t20 deleted `think`, t21 deleted the
`listen run --live` composition root together with `HookChain`, all seven
`reachy/motion/listen_{think,vision,sleep,face,scene,transcribe,hooks}.py`
modules and `sense_sample.py`, and t22 deleted `_commands/listen.py`,
`motion/listen_pat.py` (`PatHook`) and `motion/supervisor.py`. The `--live` /
`--transcribe` / `--cognition` / `--voice-engine` / `--export` flag family went
with them. **Do not re-add a sense loop as a noun** — extend
`_compose_run_seam` in `_commands/behavior.py`.

What survives, and why:

- **`reachy/motion/listen.py` `ListenProducer` is KEPT as a library.** It is the
  DONOR whose maths `reachy/behavior/orient.py` cites knob for knob (a drift
  guard in `tests/test_behavior_orient.py` pins `OrientParams` against
  `ListenParams`), and `tests/test_offline_lane.py` exercises it directly as
  the orienting success-list entry. It has **no CLI entry point** and no
  process: nothing constructs it in `reachy/` outside its own module.
  `test_the_surviving_listen_producer_is_the_one_t22_kept` in
  `tests/test_zero_llm_boundary.py` pins the `listen*.py` glob to exactly this
  one file, so re-adding a `listen_<sense>.py` fails CI.
- Its `_idle` path still calls `pat_signal.is_active()` / `sleep_signal.is_active()`.
  Those reads are **dead in production** — no process runs this producer — and
  they are the reason the flag modules were kept rather than deleted. Do not
  read either flag from new code.
- **`reachy/motion/snap.py` `SnapDetector`** (RMS above `snap_ratio × floor`,
  cited from `reachy_nova`'s `TrackingManager.detect_snap`) stays: `sleep`'s
  stimulus classifier uses it, and `reachy/vision/light.py` mirrors its design.
  Its RMS kernel is factored into `reachy/motion/rms.py` `compute_rms`, the ONE
  formula `behavior/rms_sense.py` also delegates to.
- **`reachy/motion/{queue,server}.py`** stay: `pat`, `sleep`, `vision`,
  `expression.py` and `stash/apply.py` all drive the serial `MotionQueue`. The
  `behavior` engine does **not** — it streams a composed pose through its own
  arbitration.

The ladder itself now lives in `reachy/behavior/orient.py` (`OrientTier`
`NONE`/`NOISE`/`SPEECH`/`ENGAGED`, `plan_orient`, `OrientToSound`), reached
from the shipped `look-toward-sound` rule. Two things to know before you touch
it:

- **The ENGAGED tier keys on `sense.transcript`, not a callback.**
  `TranscriptSenseDriver` has an `on_engage` seam, but composition does not
  wire it — an addressed utterance reaches the gate as a sense field, which is
  what makes it a fast-path (no dwell, no loudness, no promotion).
- **Shipped behaviour is antenna-only** (verified live: 8 admissions, including
  3 s of continuous speech, promoted to tier 2 zero times). The turn path is
  ported, tested and reachable via a rule's `params` or `REACHY_ORIENT_*` — it
  is simply not defaulted on, because a head that keeps turning can never feel
  a pat. Do not "fix" this by lowering the promotion thresholds; the successor
  is vision/mic bearing corroboration.

### The engagement gate — `reachy/speech/engagement.py`

The decision stack that keeps ambient human-to-human chatter out of the robot's
attention. Its ONE consumer today is `reachy/behavior/transcript_sense.py`
`TranscriptSenseDriver` (it also served `listen --live`'s retired
`TranscribeHook`); the state lives in `ConversationGate`, never reimplemented
per driver. A transcribed utterance passes through it cheapest-first, after the
built-in self-mute and min-utterance shortcuts:

1. **Fuzzy name fast-path** (`is_name_match` in `reachy/speech/name_match.py`) — checks
   every word in the utterance against the canonical names (`reachy`/`robot`) and a set
   of common STT mishearings (`richie`, `reachie`, `richy`). The matcher uses a combined
   `difflib_ratio × length_ratio` score with four structural guards (prefix, superstring,
   initial-letter, and **phonetic**). The phonetic guard (#104) requires the word to share
   the name's Soundex consonant skeleton, because an STT mishearing is a *phonetic*
   confusion and preserves it (`richie`/`reachie`/`richy` → R200) while an ordinary word
   that merely shares letters does not (`really` R400, `reality` R430, `root` R300). The
   three orthographic guards alone were pairwise-blind to this and let a large family of
   everyday `r`-words through — `really`, `reality`, `ready`, `reason`, `record`, `room`,
   `route`, `robust` — engaging the robot with **no classifier call to catch it**. An
   exact or close-enough match → ENGAGE immediately, **zero classifier calls**, and it
   OPENS the conversation. `tests/test_name_match.py` keeps a growing `_COLLISION_TABLE`
   of real-world collisions; this defect class recurs, so add to it rather than re-deriving.
2. **Short-utterance rule** (#105) — a context-only engagement needs `min_words` (3) words.
   A name match is exempt, so a bare "Reachy!" still engages. **Zero classifier calls.**
3. **Warm-window rule** (#105) — a nameless utterance is judged only while a conversation
   is live (within `engage_window_s`, 20 s, of the last accepted turn); only a NAME can
   open one from cold. **Zero classifier calls** when closed.
4. **Single-shot LLM classifier** (`EngagementClassifier`, backed by the
   non-streaming `reachy/speech/llm.py` `complete()`) — judges "is this
   addressed to the robot, given the recent conversation?" against up to the last 6
   accepted turns, each expiring after the warm window. Verdict YES → ENGAGE; NO → DROP.
   At most one `REACHY_OPENAI_*` endpoint call per utterance
   (`DEFAULT_CLASSIFIER_TIMEOUT = 5 s`).
5. **DEGRADE fallback** — if the classifier raises (network error / timeout / unparseable
   response), the gate returns `Decision.DEGRADE` and the driver's `_decide` falls back
   to `_should_engage` (the coherent-sentence-in-window heuristic, characterized in
   `tests/test_transcript_engagement_heuristic.py`), reporting an accept back via
   `note_engaged` so degraded turns keep the conversation warm. The hearing loop never
   stalls; classifier failures are logged once.
6. Anything else is **DROP** — ambient human-to-human chatter never reaches a rule.

- **Why 2 and 3 are structural.** `ConversationGate` replaced an accept-only history that
  was a **one-way ratchet**: only ENGAGEs entered the classifier's "recent conversation",
  so a single false accept planted a six-turn mid-conversation context and each accept
  re-seeded it. Measured live over 45 min with the name never spoken: 199 correct drops,
  39 accepts, *all wrong*. Rules 2/3 are deliberately **structural** (control flow,
  provable by a test) rather than advisory (feeding DROPs into the prompt), since the
  model had already said YES 36/36 times.
- **Every outcome is NAMED** and the label is used verbatim as the `senselog.drop` reason:
  `name` / `context` on an engage, `not-addressed` / `not-addressed-short` /
  `not-addressed-cold` on a drop. The shared `not-addressed` prefix keeps the old grep
  working.
- **Escape hatch:** `REACHY_ENGAGE_HEURISTIC=1` (or `true`/`yes`/`on`) forces the pure
  heuristic gate (`_should_engage`) throughout the process lifetime — no gate and no
  classifier is even built. Useful for debugging or when the LLM endpoint is unavailable
  at boot. Omitting the `classifier` argument gives the same guarantee.

The runtime's own STT leg is no longer `reachy/speech/stt.py` — since the
realtime arc (issue #115) `transcript_sense.py` speaks to
`reachy/speech/realtime.py`'s `RealtimeTranscriber` instead (see [the
`behavior` noun's transcript-sense
notes](#behavior-noun--pat-sense-bounded-lifetimes-and-a-live-goto) above).
`reachy/speech/stt.py`'s `Transcriber` (the model-gear / Parakeet
`/v1/audio/transcriptions` leg, external behind `REACHY_STT_URL`, default
`localhost:9002`, no on-box model bundled) survives as `sleep`'s wake-word
backend ONLY (`HttpSttBackend`) — nothing under `reachy/behavior/` imports it
any more.

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
(see the [cognition stack](#reachyspeech-cognition-stack--no-longer-its-own-noun)
below for the shared `reachy/speech/{harmonic,voice}.py` module notes). The TTS-only flags (`--voice`/`--speed`/`--tts-url`) are
accepted but ignored, and documented as such, under `--voice-engine harmonic`.

In `agent attach`'s tool registry, `say`'s TTS leg and the harmonic leg are not
an either/or choice: `reachy/speech/tools.py` registers **both** as separate
tools — `speak` (TTS) and `harmonics` (the melodic voice) — reusing exactly the
same `synthesize` + `play_audio` seams this noun uses. The agent picks per
utterance instead of the process picking one engine for the whole run. (In
`agent attach` those two tools are wired **publish-only**, so they emit
`message` blocks without touching the robot; the runtime's own voice is
`reachy/behavior/speech_act.py`.)

### The zero-LLM boundary — machine-checked, so do not re-widen it

`tests/test_zero_llm_boundary.py` (AST-based, 23 tests) turns the arc's central
claim into something CI enforces. Read it before you add an import to
`reachy/behavior/` or a module-scope import to any `_commands/` module.

**The accurate statement — use this wording, do not overclaim.** The presence
runtime's DECISION LOOP is symbolic and model-free: the engine, rule engine,
rules, intents, arbitration, goto lane and pat sense reach **nothing** in
`reachy.speech` / `reachy.vision` / `reachy.forge` transitively. The runtime
DOES own a voice and ears — deliberately ported — so it imports speech
*synthesis*, *playback* and *transcription*; `_BEHAVIOR_SPEECH_ALLOW` is the
explicit allow-list and each entry states why it is not a language model. A
companion test fails on a **dead** allow-list entry, so it cannot quietly
re-widen. "The runtime contains no LLM" is **false** and a test says so.

**The realtime arc moved which module carries transcription.**
`reachy.speech.stt` LEFT `_BEHAVIOR_SPEECH_ALLOW` (nothing under
`reachy/behavior/` imports it any more — it survives only as `sleep`'s
wake-word backend, outside this boundary) and `reachy.speech.realtime` took
its place, carrying the same justification (speech-to-text with the VAD
upstream; it reports what was said and when, and decides nothing). This is
exactly the "dead allow-list entry" failure mode the companion test guards:
the swap had to update both the allow-list AND its justification map in the
same change, or the dead-entry test would have caught the stale `stt` line.

**Exactly one LLM edge survives inside the runtime**, pinned by EQUALITY so the
suite fails in both directions: `reachy/speech/engagement.py`'s single-shot "is
this addressed to me?" classifier, reachable via `transcript_sense.py`. Adding a
second importer fails the test; removing this one also fails it — at which point
the right move is to tighten the expectation to the empty set and delete the
exception, never to loosen anything. Four bounds keep it out of the decision
loop and each is asserted: it runs on the transcript worker thread (not the
20 ms tick), it gates only admission of heard words, it fails open to the
`difflib` heuristic, and `REACHY_ENGAGE_HEURISTIC=1` (or omitting the
`classifier` argument) means no gate object is built at all.

**`_build_parser()` must stay cognition-free.** It imports every command module,
so ONE module-scope import puts the LLM client in the import path of *every*
invocation — `say run`, `daemon status`, `--help`. That is not cosmetic: it
broke `tests/test_say.py::test_say_e2e_no_llm_no_senses_via_main` (say's
dumb-pipe boundary) deterministically in a fresh worker and survived `-n auto`
only by import-order luck. Two fixes hold it: `EngagementClassifier` takes
`complete_fn=None` and resolves `llm.complete` inside `__init__` (a default
ARGUMENT would be evaluated at class-definition time, which is what forced the
module-scope import), and `_commands/agent.py` uses `from __future__ import
annotations` + a `TYPE_CHECKING` import + a `_sense_cue()` accessor.
`test_building_the_cli_parser_loads_no_cognition_module` pins the forbidden set
(`llm`, `events`, `agent_turn`, `tools`, `forge`) by equality and names the
offender on failure. If you need a cognition symbol in a command module, import
it inside the function.

Two more structural pins worth knowing: `test_no_folded_listen_hook_module_survives`
requires the `reachy/motion/listen_*.py` glob to stay EMPTY, and
`test_cognition_survives_only_behind_the_agent_noun` requires the LLM client to
stay reachable from `agent attach` — the bans constrain the runtime, not the repo.

### `reachy/speech/` cognition stack — no longer its own noun

**The `think` noun is gone (t20), and so is `listen run --live`'s folded
cognition (t21).** Deleted across the two: `reachy/cli/_commands/think.py`, its
supervisor (`reachy/speech/supervisor.py`, the `think.pid`/`think.log` pair),
its `think.voice` sidecar, its twelve `explain` catalog entries, and then
`reachy/speech/{cognition,markers,cognition_signal}.py` — the marker-parsing
`CognitionEngine`, its streaming `MarkerParser`, and the `think_active.flag`
signal, none of which had a caller left once the `--live` composition root
went. `think expressions` was re-homed onto `behavior expressions` (t18) and is
documented under the `behavior` noun above. A box that ran the old flow still
has orphaned `think.pid` / `think.log` / `think.voice` / `think_active.flag`
(and `listen.pid` / `listen.log`) files under the state dir — they are inert,
nothing reads or writes them (see [the operating
guide](docs/operating-reachy.md#the-state-dir-inert-leftovers-and-the-two-live-flags)).

What survives is the tool-use engine and the shared speech legs, reached from
ONE composition root — `agent attach` — plus `say` and the `behavior`
runtime's `SpeechActuator`:

- `reachy/speech/llm.py` — pure `urllib` streaming + non-streaming LLM client
  (`REACHY_OPENAI_URL_BASE` / `REACHY_OPENAI_API_KEY` / `REACHY_OPENAI_MODEL_ID`,
  with the legacy `REACHY_LLM_*` names honoured as a fallback; no OpenAI SDK, no
  new base dep). Used by `agent_turn.py` and by the engagement classifier.
- `reachy/speech/agent_turn.py` — `AgentTurnEngine`: the surviving cognition
  engine. A serialized tool-use loop over an injected `ToolRegistry` and a
  `snapshot()`-only cue buffer, `audio_optional=True`, with the same
  `thinking`/`message`/`emotion` export contract the retired marker engine had.
  Composed by `reachy/cli/_commands/agent.py`.
- `reachy/speech/tools.py` — `ToolRegistry` + the built-in `speak` /
  `harmonics` / `apply_pose` tool definitions, plus the optional
  `describe_scene` seam. Its import boundary (no `reachy.motion`, no
  `reachy.speech.llm`/`events`, no `reachy.forge`, no `reachy.vision`) is
  asserted in `tests/test_speech_tools.py`.
- `reachy/speech/intent_tools.py` — the four intent tools (`run_behavior`,
  `declare_goal`, `set_mode`, `set_inhibition`) that turn an agent turn into an
  atomic intents-spool command.
- `reachy/speech/tts.py` + `reachy/speech/playback.py` — shared with `say` and
  with the runtime's `SpeechActuator`.
- `reachy/speech/voice.py` + `reachy/speech/harmonic.py` — `resolve_voice_engine`
  selects the `synthesize` callable + playback samplerate; `harmonic` is a
  pure-stdlib, offline note-melody voice (identity/articulation tunable via
  `REACHY_HARMONIC_IDENTITY` / `REACHY_HARMONIC_ARTICULATION`). `say run
  --voice-engine {tts,harmonic}` (env `REACHY_VOICE_ENGINE`) is the one CLI
  flag left that picks between them; the runtime defaults to `harmonic`
  regardless (see `behavior/speech_act.py`).
- `reachy/speech/events.py` — `EventBuffer` / `SenseCue`: the cue vocabulary
  (`feed_pat` / `feed_transcript` / `feed_face` / `feed_scene` / `feed_forge`
  / …) `agent attach` maps runtime-feed lines into.
- `reachy/speech/marker_events.py` — the frozen `MarkerEvent` / `SpeechEvent`
  dataclasses + the `Event` union. t2 moved them out of the (now deleted)
  `markers.py` precisely so `reachy/motion/expression.py` — part of the
  surviving `apply_pose` tool path — could keep importing them. The streaming
  `MarkerParser` that produced them is gone; a caller builds the events
  directly. `tests/test_marker_events_relocation.py` is the guard.
- **Expression catalog** — `reachy/speech/expressions.toml`: emoji-keyed TOML
  tables, each mapping to a 9-axis `ExpressionPose` (head mm/deg, antenna deg,
  body_yaw deg). Loaded via stdlib `tomllib` (no new dep). `NEUTRAL_KEY =
  "neutral"` is the all-zeros fallback for unknown emoji. `Catalog` (thin
  wrapper), `load_catalog`, and `get_pose` in `reachy/speech/expressions.py`.
  Starter set: 🤔 😮 🙂 👂 😐 🎉 😔 😊 and neutral. Edit this file to tune poses
  without any code change. Its CLI inspection surface is `behavior
  expressions` — the ONE home since t20; `agent attach`'s `apply_pose` tool
  reads the same catalog.
- **`ExpressionProducer`** (`reachy/motion/expression.py`) — enqueues calm
  one-shot expression moves onto a shared serial `MotionQueue` from a cognition
  thread; a background executor thread drains the queue to the robot. Motion
  errors degrade silently so a transport drop never kills the cognition loop.
- **`reachy/speech/distinctness.py`** — `find_too_similar(catalog, threshold)`
  computes weighted Euclidean pose distances (normalised by per-axis amplitude
  σ) and returns pairs below the threshold. The neutral entry is excluded from
  pairwise comparison. Default threshold `0.5`; starter catalog passes cleanly.
  Driven by `behavior expressions check`.
- **`reachy/speech/realtime.py` + `realtime_wire.py`** — the runtime's hearing
  leg since issue #115: `RealtimeTranscriber` holds ONE long-lived WebSocket
  session against the lobes `/v1/realtime` route, streams mic audio as base64
  `input_audio_buffer.append` JSON TEXT events (never binary), and surfaces
  already-endpointed `Utterance`s once the server's `server_vad` says a
  sentence ended — replacing the local energy-VAD capture path
  `transcript_sense.py` used to own. `realtime_wire.py` is the pure-function
  RFC 6455 framing + base64 append-event codec, hand-rolled and
  cite-don't-import ported from lobes-cli's `scripts/realtime-smoke.py` (no
  new dependency; `pyproject.toml` is untouched by this module). Every
  failure — a refused handshake, a dead gateway, a malformed event, a named
  server error (`vad_unavailable`, `stt_forward_failed`) — resolves to a named,
  latched `senselog.drop` and a backoff reconnect, never an exception on the
  caller's thread, mirroring `reachy.speech.stt.Transcriber`'s own
  never-raise ethos. Consumed by `reachy/behavior/transcript_sense.py` only.
- **`reachy/speech/stt.py` + `engagement.py` + `name_match.py`** — `stt.py`'s
  `Transcriber` (one external `/v1/audio/transcriptions` POST per utterance)
  is now consumed ONLY by `reachy/sleep/wakeword.py`'s wake-phrase backend —
  nothing under `reachy/behavior/` reaches it any more. `engagement.py` +
  `name_match.py` are unchanged and still the layered engagement gate
  documented in [its own section
  above](#the-engagement-gate--reachyspeechengagementpy), still consumed by
  `reachy/behavior/transcript_sense.py`: admission of an already-arrived
  transcript is exactly the surface the realtime arc left alone.
- **`--export` / `--export-blocks` stdout JSONL sink** — `agent attach
  --export -` writes a live newline-delimited JSON (NDJSON) feed to stdout.
  Each line is one JSON object: `t` (block type), `ts` (unix timestamp), plus
  type-specific fields. Three block types: `thinking` (sense cues + the full raw
  LLM turn text), `message` (text spoken aloud), `emotion` (emoji + 9-axis pose
  snapshot or `null`). `--export-blocks` accepts a comma-separated subset (e.g.
  `thinking,message`; default: all three). The sink lives in `reachy/export/`
  (`events.py` event model + `to_jsonl`, `blocks.py` `Selection` /
  `parse_blocks`, `exporter.py` `JsonlExporter`), wired through the shared
  `reachy/cli/_export.py` `build_export_hook`. The exporter is a passive tap on
  the cognition loop — it catches `BrokenPipeError`/`OSError`/`ValueError`, logs
  once to stderr, and silently disables itself so a disconnecting consumer never
  kills the loop. Only `-` (stdout) is supported in this version. See
  `docs/export-schema.md` for the full wire-format contract. The complementary
  RUNTIME feed (`sense`/`rule`/`intent`/`motion`) is `behavior engine run
  --export -`, built by the same module's `build_runtime_export_consumer`.

### `pat` noun — bench-only proprioceptive touch + snuggle (SDK-first)

**Scope first: `pat run` is a bench check, not the live path.** Live patting
reaches the robot through `reachy/behavior/pat_sense.py` inside the runtime.
`pat run` calls `refuse_if_engine_live("pat run")` at entry — before it
constructs a transport — so it exits 1 rather than contending with a live
engine for the head.

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
`_MotionExecutor`/`reachy.motion.server.run` background-thread pattern `sleep`
and `vision` use (motion errors degrade silently). SDK-first by default; the
`http` transport cannot read `head_pose`. A missing `[sdk]` extra raises a clean
exit-2 `CliError`; `demo` works with no robot. While a reaction is enqueued,
`pat` writes `pat_active.flag` via `reachy/motion/pat_signal.py` — **nothing
else reads it**; `pat run` reads it back only to clear it idempotently after a
crash (see [the flag asymmetry](#the-two-flag-files-are-asymmetric)).
Determinism seams for tests: `PatDetector.update`
takes `now=` and the constructor takes `level2_threshold_fn`; `pat run` takes a
bounded `--ticks N` and injects the transport via `get_transport`.

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
  speech or a loud RMS snap transient (`SnapDetector`, the same detector the
  retired `listen` loop's Tier 2 used).
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
  log as `sleep.pid`/`sleep.log` under `$REACHY_STATE_DIR`). Each noun that has
  a background form tracks its own process this way (`demo_service.py`,
  `vision/supervisor.py`, `behavior/supervisor.py`).
- `reachy/motion/sleep.py` — `SleepProducer`: drowsy fade on the way down,
  quiet sleep-breathe cycle while ASLEEP, wake gesture on resumption; enqueued
  onto the same shared serial `MotionQueue` as `pat` and `vision`.
- `reachy/motion/sleep_signal.py` — `sleep_active.flag`, written **only while
  the machine is ASLEEP** (`_sync_sleep_flag`, not during DROWSY). Unlike
  `pat_active.flag` it keeps a genuine CROSS-PROCESS reader: `cmd_sleep_status`
  reports ASLEEP-vs-ALERT from it, and it is the ONLY way to observe a parked
  robot because the state machine and its idle timer live inside the loop
  process (`idle_seconds` is reported `null` for exactly that reason). See
  [the flag asymmetry](#the-two-flag-files-are-asymmetric).

`sleep run` calls `refuse_if_engine_live("sleep run")` at entry, so it will not
start beside a live behavior engine. SDK-first by default; the `http` transport
is available for non-pose ops. A missing `[sdk]` extra raises a clean exit-2
`CliError`. Determinism seams for tests: `SleepState` timer takes an injected
clock; `sleep run` takes a bounded `--ticks N` and injects the transport via
`get_transport`; `demo` needs no robot.

### `service` noun — boot-persistent single-presence (systemd `--user`)

`reachy/cli/_commands/service.py` exposes `enable {demo|runtime}` /
`disable` / `status` / `install` / `uninstall` / `overview`. It is the operator
front for making the robot survive a reboot in **exactly one** presence mode —
the idle `demo-mode` loop or the symbolic `behavior engine run` runtime, never
both — the single-SDK-owner model expressed across reboots. Like `daemon`, it does **not**
use a transport: it talks to **systemd** (`systemctl --user`), so it never calls
`_robot.get_transport` / `noun_overview` and its `overview` is hand-built.

- **Units (`reachy/service/units.py`).** Pure unit-text renderers (every function
  returns a `str`, no side effects) for the three units, with their canonical
  names exported as the cross-module contract `DAEMON_UNIT` / `DEMO_UNIT` /
  `RUNTIME_UNIT` (`reachy-daemon.service` / `reachy-demo-mode.service` /
  `reachy-runtime.service`). All share `Type=simple` + `Restart=on-failure` +
  `RestartSec=5` (so a crash auto-restarts) and `WantedBy=default.target`. The
  presence units additionally `Requires=` / `After=` the daemon unit — **the
  daemon is a boot dependency**, started first. The runtime unit's `ExecStart`
  is `<python> -m reachy behavior engine run`, with `REACHY_TTS_ROUTE` baked in
  as an `Environment=` directive (the box's only route config used to live in a
  drop-in belonging to the retired live unit, which the migration deletes).
- **Retiring a unit is a one-line change, and `RETIRED_UNITS` is authoritative
  over the catalog.** Move the name OUT of the catalog above and INTO
  `units.RETIRED_UNITS` (today: `reachy-listen.service`, `reachy-live.service`).
  This matters because a name leaving the catalog does not make it leave the
  deployed robot — nothing rewrites unit files on `pip upgrade`, and every
  install/enable path only touches units still IN the catalog — so a retired
  unit survives with an `ExecStart` naming a subcommand that no longer parses,
  and `Restart=on-failure` + `RestartSec=5` turns that into a 5-second crash
  loop rather than a quiet no-op. `ServiceManager.cleanup_retired_units` walks
  the tuple on every ordinary `enable` / `install` / `uninstall`,
  unconditionally `disable --now`ing each name, unlinking its unit file and
  removing its `.d/` drop-in directory; the names it actually removed come back
  as `retired_removed`. `status()` probes the retired names too and reports
  `mode="retired"` (`RETIRED_MODE`) with a warning when one is still enabled,
  rather than the lie of `mode=None`. **Never list a unit that is still a live
  presence mode** — the migration would disable it out from under the operator.
  Retired names are plain strings on purpose: a retired unit must not have a
  constant some future catalog tuple can import back in. And the cleanup is
  **destructive** — say so in operator docs (hand-authored drop-ins are not
  reproducible from this repo).
- **Manager (`reachy/service/manager.py` `ServiceManager`).** Enforces the
  **single-presence-owner invariant**: `enable(mode)` writes + `enable --now`s the
  daemon and the chosen presence unit and **always `disable --now`s the sibling**,
  so any sequence of enables leaves at most one presence enabled. Naming a
  retired mode is refused as an exit-1 user error listing the live ones.
  `disable()` stops only the enabled presence and **leaves the daemon enabled**
  (explicit, reported as `daemon="left-enabled"` — other clients depend on it).
  `status()` reads `is-enabled` / `is-active` per unit + folds a daemon-health
  probe. Every side effect goes through injected seams (`run` / `unit_dir` /
  `daemon_health`), so it is exhaustively testable without real systemd.
- The command module's `install` / `uninstall` write/remove **all three** unit
  files + `daemon-reload` without enabling anything (so a separate `enable` chooses
  the mode), and run the retired-unit purge too. A missing `systemctl` on PATH
  raises a clean exit-2 `CliError`; an
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

### `reachy/forge/` package — qwen3 self-extension (wired under `agent attach`)

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
callables. Composition (`reachy/cli/_commands/agent.py`'s `_activate_forge`)
builds the `ForgedSkillContext` over the same `speak_engine`/`harmonic_engine`/
`play`/`express` seams the built-in tools use, and wires `ForgeActivator.publish`
as `ForgeClient`'s `publish`. The `forge` tool is only advertised where a
`ToolRegistry` is built — `agent attach` — and a missing/broken forge stack
disables only the tool; cognition keeps running.

## Hard constraints

- **Base runtime dependencies — SDK-first, but installable.** Two packages are
  **base** runtime dependencies (`pyproject.toml`): `numpy` (the RMS loudness
  detector) and `harmonics-cli>=0.8` (the harmonic voice backend, import
  package `harmonics` — see the `say` noun internals above).
  Both are pure wheels that install everywhere; `harmonics-cli` additionally
  has **zero transitive runtime deps** and is org-owned (AgentCulture), which
  is why it earns a base-dep exception — that exception does NOT extend to any
  other engine package (see the `[cpu]`/`[gpu]` note below). The SDK transport
  is the **default** for `behavior engine run` and the sense nouns, but
  `reachy-mini` stays an **extra** (`[sdk]` /
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
  JPEG-encode leg): lazy-imported, absent by default, and a missing extra leaves
  `behavior/face_sense.py` permanently quiet (one logged warning, `face` /
  `frame_available` never populated) instead of crashing the loop —
  `pip install 'reachy-mini-cli[vision]'` to enable it.
  The pixel-only `vision` noun (motion/light orienting) needs no extra —
  numpy-only, unaffected. The `behavior` engine's pat-sense pose reader
  (`reachy/robot/state_reader.py` `HeldStateReader`) adds no new dependency
  either: it lazy-imports `reachy_mini` and degrades to a permanently-`None`
  reader (one logged warning) when `[sdk]` is absent, so `_compose_run_seam`
  composes the pat stack unconditionally rather than gating on an SDK-import
  probe.
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

**Git worktrees live in `../.worktrees.reachy-mini-cli/<name>/`.** ALL
worktrees of this repo, without exception — workforce fan-out lanes,
`ask-colleague` throwaways, scratch checkouts — go in that one repo-named
directory beside the checkout, one subfolder per worktree. Never `/tmp`, never
a shared sibling folder, never anywhere else; `git worktree list` should show
the main checkout and nothing outside this directory:

```bash
git worktree add ../.worktrees.reachy-mini-cli/<name> -b <branch>
```

Do **not** use a shared `../worktrees/` directory. This workspace holds many
sibling projects, and a generic shared folder accumulates orphaned trees from
several repos at once with nothing indicating who owns which — someone
clearing stale trees cannot tell yours from junk, and a `rm -rf` on the shared
folder takes your lane with it. (This is not hypothetical: it happened during
the retire-old-flow fan-out.) The repo-named, dot-prefixed folder makes
ownership unambiguous and keeps the sweep-up safe. An unowned worktree is a
stale worktree — so make sure the name says who the owner is.

Use a branch prefix scoped to the work (`retire/t2`, not `agent/t2`): plain
`agent/*` names collide with leftovers from earlier fan-outs and `git worktree
add -b` fails on an existing branch. Prune with `git worktree prune` (which is
safe and sufficient); never `rm -rf` a worktree directory you did not create.

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
