# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this repo is (and is not, yet)

`reachy-mini-cli` is an **AgentCulture mesh agent** whose intended domain is
*operating the Reachy Mini expressive robot — device setup, app management, and
runtime ops* (see `culture.yaml` and the README).

**Today the code is still the unmodified clone of `culture-agent-template`.** The
CLI exposes only the template's agent-first introspection verbs (`whoami`,
`learn`, `explain`, `overview`, `doctor`, `cli`). **No Reachy / robot
functionality exists yet** — there is no device, app, or runtime-ops code. When
you add robot features, you are building the real agent on top of this scaffold,
not modifying a finished product. Treat the existing verbs as the *pattern to
copy*, not the *feature set*.

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
- **`explain` catalog** (`reachy/explain/`): markdown keyed by command-path
  tuples in `catalog.py`'s `ENTRIES`. `test_every_catalog_path_resolves`
  verifies each catalog entry resolves — but nothing fails if you add a verb
  *without* a catalog entry, so add the `ENTRIES` key yourself when you add a
  verb.
- **Identity (`whoami`) & `doctor`:** `whoami` hand-parses `culture.yaml` with a
  line scanner (no YAML library) and walks up from `__file__` to find it.
  `doctor` re-implements the steward invariants (prompt-file-present,
  backend-consistency `claude`→`CLAUDE.md`, skills-present).
- **`daemon` noun & process module:** `device`/`app`/`move` are *clients* of a
  running daemon; `reachy/cli/_commands/daemon.py` + `reachy/daemon.py` are the
  other half — they start/stop/status the local `reachy-mini-daemon` OS process
  (background spawn + PID/log under `$REACHY_STATE_DIR` / `$XDG_STATE_HOME/reachy`,
  health-poll via `GET /api/daemon/status`). Pure stdlib (`subprocess`/`signal`/
  `urllib`); the daemon *binary* comes from the `[daemon]` extra. Its `overview`
  is hand-built (no `--transport sdk` line) — `daemon` does NOT use a transport,
  so it does not call `_robot.noun_overview`/`get_transport`. A missing binary
  raises a clean exit-2 `CliError` pointing at the `[daemon]` install.
  `is_robot_live()` (also in `reachy/daemon.py`) provides SDK-based daemon
  liveness that stays correct across a daemon restart (fixes issue #21).
- **`listen` noun — two-tier `ListenProducer` (SDK-first):** The `listen` loop is
  implemented as a two-tier `ListenProducer`:
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
- **`say` noun — dumb TTS pipe:** `reachy/cli/_commands/say.py` exposes `run`
  (text → TTS → playback) and `overview`. It MUST NOT import `reachy.speech.llm`
  or `reachy.speech.events` — tests assert this boundary. TTS is via
  `reachy.speech.tts.synthesize` (Magpie-style HTTP: `REACHY_TTS_URL` /
  `REACHY_TTS_VOICE`). Playback is via `reachy.speech.playback.play_audio` —
  `sdk` (default, pushes PCM via `reachy_mini.media`) or `http` (daemon
  `/media/play` route). No LLM, no event bus, no senses; safe to compose in
  pipelines.
- **`think` noun — continuous cognition loop (SDK-first):** `reachy/cli/_commands/think.py`
  exposes `run` (foreground) + `start`/`stop`/`restart`/`status` (background
  process) + `demo` (drive a fixed scripted `*emoji*` / `"speech"` stream through
  the real marker→expression+TTS path, no LLM — for on-robot verification) +
  `overview`. The `reachy/speech/` package provides the engine:
  - `reachy/speech/llm.py` — pure `urllib` streaming LLM client
    (`REACHY_LLM_BASE_URL` / `REACHY_LLM_API_KEY` / `REACHY_LLM_MODEL`; no
    OpenAI SDK, no new base dep).
  - `reachy/speech/tts.py` + `reachy/speech/playback.py` — shared with `say`;
    `think` reuses the same TTS + playback leg.
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
    Starter set: 🤔 😮 🙂 👂 😐 🎉 😔 and neutral. Edit this file to tune poses
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
    Both verbs support `--json`.
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
- **`pat` noun — proprioceptive touch + snuggle (SDK-first):**
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
  bounded `--ticks N` and injects the transport via `get_transport`.

- **`sleep` noun — decay-to-sleep + wake (SDK-first):**
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
    `resolve_backend`): `http` (default — external HTTP STT service, stdlib
    `urllib`; `REACHY_STT_URL` / `REACHY_STT_PHRASE` / `REACHY_STT_TIMEOUT`,
    mirrors the Magpie TTS pattern; no extra required) or `openwakeword` (on-box,
    `[cpu]` extra, lazy-loaded). The `[gpu]` extra is a generic compute-class pin
    for future GPU features — it does NOT carry an on-box STT model.
  - `reachy/sleep/patwake.py` — `PatWakeDetector`: pat-based wake detector that
    measures head-pose deviation against the **moving** sleep-breathe commanded
    pose (not a fixed baseline), reusing `reachy/motion/pat.py` `PatDetector`
    (numpy + stdlib only). Used when `--no-audio-wake` is active.
  - `reachy/sleep/wakeword.py` — `resolve_backend(kind)`: factory for the
    pluggable wake-word backend (`http` / `openwakeword`). The `http` backend
    calls an external STT HTTP service (pure stdlib); `openwakeword` is
    lazy-imported from the `[cpu]` extra and degrades gracefully when absent.
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

## Hard constraints

- **Base runtime dependencies — SDK-first, but installable.** `numpy` is the only
  **base** runtime dependency (`pyproject.toml`) — it powers the RMS loudness
  detector and is a pure wheel that installs everywhere. The SDK transport is
  `listen`'s **default**, but `reachy-mini` stays an **extra** (`[sdk]` / `[daemon]`),
  not a base dep, because its transitive stack (pycairo / gstreamer / pyaudio) needs
  system libraries absent on a bare box and in CI — a hard base dep breaks `uv sync`
  on the cairo build (learned the hard way on PR #24). So the **recommended default
  install is `pip install 'reachy-mini-cli[daemon]'`** (pulls `reachy-mini`); a bare
  `pip install reachy-mini-cli` is the HTTP remote profile, and running the `sdk`
  transport without the extra raises a clean exit-2 `CliError` pointing at `[sdk]`.
  The HTTP transport stays available via `--transport http` / `REACHY_TRANSPORT=http`.
  Adding a *new* base runtime dep beyond `numpy` needs an explicit decision (keep the
  base light enough for the remote profile). `teken` remains dev-only; `whoami` still
  hand-rolls YAML; `reachy/daemon.py` still uses stdlib only. The `[cpu]` extra is
  the home for on-box `openwakeword` (lazy-loaded; dep list empty until it gains a
  cp312 wheel). The `[gpu]` extra is a generic compute-class pin for future
  GPU-accelerated features — it does NOT bundle an on-box STT model (heavy STT is
  externally managed behind the HTTP STT service, `REACHY_STT_URL`). Both are
  lazy-loaded and the Tier 1 wake (speech/snap) never requires them — a bare
  `pip install reachy-mini-cli` still gets full Tier 1 wake functionality.
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
  **`agent-config`**, and the devague chain (`think` → `spec-to-plan` →
  `assign-to-workforce`).
