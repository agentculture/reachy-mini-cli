# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.34.1] - 2026-07-17

### Fixed

- forge validator: fail-closed call-target check — calls through subscripts, lambdas, or chained attributes on a non-allowed base now reject instead of silently passing; __builtins__ added to the forbidden-names list (Qodo finding)
- forge activate: wrap_executor now runs a forged execute(params, ctx) on a bounded daemon worker thread (default 10s, injectable) so a runaway skill (e.g. time.sleep(1e9)) can never wedge the cognition turn loop; a timeout returns an error tool-result and logs senselog.drop reason=skill-timeout (Qodo finding)
- forge client: the forge auth resolution now treats the literal api key "EMPTY" as no-auth for both FORGE_API_KEY and the REACHY_OPENAI_API_KEY fallback, matching the repo-wide convention in reachy/speech/llm.py instead of sending Authorization: Bearer EMPTY (Qodo finding)

## [0.34.0] - 2026-07-17

### Added

- Event-based senses pipeline: pre-roll ring buffer + measured onset in TranscribeHook — utterances now include up to 2 s of audio from before the speech flag flips (leading words no longer lost)
- `[SENSE stage=<stage> source=<source> event=<event>]` structured sense-stage logging (reachy/senselog.py) across capture/onset/cue/turn/action, with loud `dropped reason=<reason>` lines — plus a real logging handler: --log-level / REACHY_LOG_LEVEL (default INFO) on listen/think/sleep run (reachy/cli/_logging.py)
- Vision events reach cognition: VisionHook feeds EventBuffer.feed_vision with per-episode coalescing (issue #32)
- Basic face recognition behind the NEW [vision] extra (opencv-python-headless): YuNet + SFace engine (reachy/vision/face.py), FaceStore temp/permanent tiers, folded FaceHook feeding `saw <name>` cues (30 s re-announce cooldown), scripts/face_enroll.py
- Scene description: reachy/vision/scene.py describe path (Gemma4 via REACHY_VISION_MODEL_ID), periodic SceneHook (default 30 s) + on-demand describe_scene agent tool
- qwen3 forge — runtime self-extension: forge agent tool -> FORGE_BASE_URL coder endpoint -> AST-only fail-closed validator -> validator-gated auto-activation, hot-registered and callable on the next turn; staged/rejected artifacts under state_dir()/forge (reachy/forge/)
- Single-session composition proof suite (tests/test_live_single_session.py): one media session, one shared frame grabber, one EventBuffer across all sense hooks

### Changed

- [sdk]/[daemon] extras pin reachy-mini>=1.9.0,<1.10 — the camera frame path is repaired: SDK >=1.9 reads frames over the daemon IPC endpoint; the guessed is_local_camera_available/media_manager.camera seam replaced with the real media.get_frame()/media.camera surface (issue #28); scripts/camera_soak.py is the live health check
- Forge auth falls back to REACHY_OPENAI_API_KEY when FORGE_API_KEY is unset (one gateway, one key)
- docs/operating-reachy.md gains the Event-based senses pipeline section; CLAUDE.md noun internals updated (FaceHook/SceneHook, forge package, [vision] extra)

### Fixed

- Direction invariants pinned by regression suite: raw DoA cues stay off under --transcribe, direction rides transcripts

## [0.33.0] - 2026-07-17

### Added

- `EventBuffer.feed_pat` — head-pat detections become sense cues (`felt a gentle scratch on the head`); under `--live` the folded `PatHook` feeds one cue per reaction cycle into the shared cognition buffer, bypassing the engagement gate — touch is inherently addressed (#66)
- 😊 contentment pose in `expressions.toml` — the natural answer to being petted; passes the distinctness check with margin
- `apply_pose` advertises the expression catalog as a JSON-schema `enum` generated from the loaded TOML keys, so a new entry reaches the model with no code change; an unknown emoji returns an error tool-result naming the valid keys instead of silently no-oping to neutral (#67)
- The agent system prompt names touch among the robot's perceptions

### Fixed

- Pat false-fire loop: `PatHook` skips sensing while a commanded move is in flight (`server.run` publishes its `busy_until` horizon) and re-baselines the detector after suspensions — the robot's own goto transit no longer reads as external force (147 false detections in 51 untouched minutes on the dev box), and real pats are no longer masked by wall-to-wall reaction windows (#66)

## [0.32.0] - 2026-07-17

### Added

- `listen run --live --cognition {marker,agent}` (env `REACHY_COGNITION`) — agent mode swaps the
  folded marker cognition for `AgentTurnEngine`, a tool-use loop acting through the new
  `ToolRegistry` (`speak`, `harmonics`, `apply_pose`) on the same ThinkHook seam, EventBuffer,
  engagement gate, self-mute wrapper, and export sinks; no new process, no second media session
- OpenAI tool-calling in the stdlib LLM client (`reachy/speech/llm.py`): `tools=`/`tool_choice=`
  payload support, streamed `tool_calls` delta assembly (`stream_turn`/`complete_turn` returning
  `TurnResult`), gateway-verified live (streaming and non-streaming)
- `reachy/speech/tools.py` — agent tool registry with injected seams; `apply_pose` proven
  action-identical to the `*emoji*` marker path; both voices (TTS + harmonic) always registered
  as separate tools in agent mode
- `reachy/stash/` — behavior stash: declarative LibraryEntry-shaped records (free-form code
  refused), explanations embedded via the lobes gateway `/v1/embeddings` (stdlib urllib), numpy
  cosine top-k search, atomic JSON index under the state dir, and `apply.py` sampling fetched
  records into bounded MotionQueue goto keyframes
- Gateway TTS route: `synthesize(route="openai")` / `REACHY_TTS_ROUTE` targets the lobes gateway
  `POST /v1/audio/speech` (probe-verified WAV @ 24 kHz; bare-PCM opt-in), Chatterbox route
  unchanged as default
- Gateway-gated integration tests: cortex full tool round trip (prompt → tool_calls → tool
  results → final text) and a cortex+muse parametrized run with per-model skip guards and
  latency bounds (deviation d1)
- Operator docs: agent-cognition section with two on-robot demos, agent model choice (cortex
  local default / muse proxied from thor, tool-capable per lobes-cli#139 partial fix, audio-in
  still absent)

### Changed

- The deployed `live` boot unit ExecStart is now
  `listen run --live --transcribe --cognition agent --voice-engine harmonic` — the boot presence
  reasons via tool calls by default
- CLAUDE.md noun catalog + listen/say/think/service sections updated for the agent mode and the
  stash package

## [0.31.0] - 2026-07-17

### Added

- Harmonic voice: a second, non-TTS speech engine — each spoken sentence renders in-process to a note melody in Reachy's own identity signature (harmonics-cli, offline, deterministic, PCM16 @ 16 kHz) and plays through the existing playback leg
- --voice-engine {tts,harmonic} on say run, think run, think demo, and listen run (--live only), plus REACHY_VOICE_ENGINE env; tuning via REACHY_HARMONIC_IDENTITY / REACHY_HARMONIC_ARTICULATION
- think status --json reports voice_engine; think/listen startup banners name the active engine
- New reachy/speech/harmonic.py backend and reachy/speech/voice.py engine resolver; explain catalog + README + operating guide document the harmonic voice

### Changed

- harmonics-cli>=0.8 joins numpy as a base runtime dependency (pure-stdlib wheel, zero transitive deps; deviation d1 updated the three base-dep guard tests)
- reachy-live.service boot unit ExecStart now runs listen run --live --transcribe --voice-engine harmonic — the robot boots into its harmonic voice
- Self-mute clip-duration math derives from the active engine sample rate (16 kHz harmonic clips mute correctly)

## [0.30.0] - 2026-07-17

### Added

- **Vendored four devague chain skills — `scope`, `challenge`, `deviate`, and
  `summarize-delivery`** (cite-don't-import; origin = devague, broadcast via
  guildmaster) — completing the idea→delivery chain around the three already
  here. `scope` is the optional opening move (idea→scope: survey the surfaces an
  idea touches and seed the coming frame with cited boundary/non-goal/assumption
  claims); `challenge` runs a risk-scaled blind-spot pass between `think` and
  `spec-to-plan`, routing findings back as proposed-only content the human
  adjudicates; `deviate` stops an in-flight `assign-to-workforce` run when
  execution must diverge from the confirmed plan and records the divergence as a
  first-class append-only record instead of silent drift; `summarize-delivery`
  closes the loop with a planned-versus-actual accountability artifact (and runs
  on failed runs too — failure is reported faithfully, never smoothed over). The
  full chain is now `scope` → `think` → `challenge` → `spec-to-plan` →
  `assign-to-workforce` → `summarize-delivery`, with `deviate` as the mid-run
  escape hatch; `CLAUDE.md`'s skills section is updated to match.
- **Memory-discipline "Conventions and workflow" section in `CLAUDE.md`** — a
  per-task *recall-before / remember-after* convention (scope localized to this
  repo's nick) so the vendored `remember` / `recall` skills are actually used,
  not just present: `/recall` before non-trivial work to build on prior
  decisions instead of re-deriving them, and `/remember` when a non-obvious
  decision, constraint, fix-and-why, or hard-won gotcha surfaces. The section
  documents this repo's memory as **in-repo and public** — records resolve to
  `<repo-root>/.eidetic/memory` (committed, team- and mesh-shared). Inserted
  idempotently (skipped if already present), slotted under an existing
  "Conventions and workflow" heading when one exists, else appended.

### Changed

- **Refreshed the `remember` + `recall` wrappers from eidetic-cli 0.10.0**
  (cite-don't-import) — picks up eidetic's **project-local store default**: the
  files backend now resolves per record by visibility — PUBLIC records inside a
  git repo go to `<repo-root>/.eidetic/memory` (committed, team-shared), PRIVATE
  records (or any record outside a repo) go to `$HOME/.eidetic/memory` (never
  committed), an explicit `EIDETIC_DATA_DIR` still wins, and recall reads both
  stores and merges. Also carries the 0.9.3 hardening (interactive-stdin guard,
  `help` as a search term, SIGPIPE-safe suffix parsing). **Recipe policy
  override (the wrappers here are NOT byte-verbatim):** the injected default
  visibility is flipped from eidetic's `private` to **`public`**, so a plain
  `/remember` lands the note in `./.eidetic/memory` in this repo, kept as part
  of the repo — pass `--visibility private` to route a record to `$HOME`
  instead. `remember` drives `eidetic remember` (idempotent upsert of one JSON
  record or an NDJSON batch on stdin); `recall` drives `eidetic recall` with
  four search modes (exact / approximate / keyword / hybrid). Each `SKILL.md` is
  localized only in the illustrative `--scope <nick>` examples (Provenance keeps
  "First-party to eidetic-cli"). Runtime dep: the `eidetic` CLI on PATH (else a
  local eidetic-cli checkout with `uv`) — **`eidetic >= 0.10.0`** for the
  in-repo routing; on an older CLI the public records still work but are stored
  in `$HOME/.eidetic/memory` instead of in-repo. Propagated by rollout-cli's
  `eidetic-memory` recipe.

### Fixed

- **Green CI on `main` — refreshed the stale `uv.lock`.** v0.29.0 bumped
  `pyproject.toml` without re-running `uv lock`, so the lock still pinned
  `reachy-mini-cli` at `0.28.2`. That mismatch makes `uv sync` re-resolve the
  whole graph instead of installing from the lock, and a re-resolve has to build
  `pycairo` (via the `[daemon]` extra's `reachy-mini` → `pygobject` chain) from
  an sdist against a system `cairo` that CI does not have — so **every** `test`
  and `lint` job on `main` and on open PRs died in `uv sync` before running a
  single test. The lock is regenerated here (resolution is unchanged — only the
  version string moved), which restores `uv sync` to a lock-install and unbreaks
  the branch. **Always run `uv lock` in the same commit as a version bump**; the
  `version-bump` skill does not do it for you.

## [0.29.0] - 2026-06-23

### Added

- **Vendored the `remember` + `recall` memory skills from eidetic-cli**
  (cite-don't-import) — the write/read halves of eidetic's shared
  `~/.eidetic/memory` surface, so this agent (Claude and its colleague backend)
  can persist facts across sessions and recall them later, sharing one store.
  `remember` drives `eidetic remember` (idempotent upsert of one JSON record or
  an NDJSON batch on stdin, dedup by id + content hash); `recall` drives
  `eidetic recall` with four search modes — exact / approximate / keyword /
  hybrid — each hit carrying text, full provenance metadata, a relevance score,
  and a freshness signal. The `.sh` wrappers are byte-verbatim from eidetic-cli
  (their first-party origin); each `SKILL.md` is localized only in the
  illustrative `--scope <nick>` examples (Provenance keeps "First-party to
  eidetic-cli"). Both default to this agent's PRIVATE scope, reading the suffix
  from `culture.yaml`. Runtime dep: the `eidetic` CLI on PATH (else a local
  eidetic-cli checkout with `uv`). Propagated by rollout-cli's `eidetic-memory`
  recipe.

## [0.28.2] - 2026-06-22

### Changed

- `reachy/speech/name_match.py`: extracted the per-word/per-name guard ladder into a
  flat `_word_matches_name()` helper so `is_name_match()` is a single `any(...)` over
  word/name pairs — behaviour byte-identical, Cognitive Complexity dropped from 18 to
  within the 15 limit (SonarCloud maintainability). The initial guard now reads
  `not word.startswith(name[:1])` instead of the slice comparison `word[:1] != name[:1]`
  (SonarCloud `S6659`), behaviour-preserving for all real inputs.
- `reachy/motion/listen_transcribe.py`: removed the unused `clock=` constructor seam
  (it was never injected by any caller and `self._clock` was assigned but never read),
  bringing `TranscribeHook.__init__` to 13 parameters (SonarCloud `S107`). The mute gate
  already uses the tick's own `t`, so no behaviour changes.

## [0.28.1] - 2026-06-22

### Fixed

- `reachy/speech/llm.py`: the non-streaming `complete()` (engagement classifier) now
  sends `Accept: application/json` instead of the streaming `Accept: text/event-stream`,
  so an OpenAI-compatible server can no longer reply with an SSE body that breaks the
  `json.loads` and degrades the classifier for no reason (Qodo review #2).
- `reachy/motion/listen.py`: the one-shot engaged latch (`set_engaged`) is now consumed
  **only** on a tick that carries a usable `doa_angle`. A transient `doa_angle is None`
  tick — silence right after an addressed utterance, or a degraded DoA read — no longer
  swallows the latch, so the deliberate engaged turn-toward-the-speaker is never silently
  lost (Qodo review #3).

## [0.28.0] - 2026-06-21

### Added

- Layered engagement gate under `listen run --live --transcribe`: fuzzy name fast-path
  (`reachy/speech/name_match.py`) recognises "reachy"/"robot" and common STT mishearings
  ("richie", "reachie") with an initial-letter guard, engaging immediately with no LLM
  call; for nameless utterances a single-shot LLM classifier
  (`reachy/speech/engagement.py`, `EngagementClassifier`) judges "is this addressed to
  the robot, given recent conversation?" — ENGAGE on yes, DROP on ambient chatter.
- `REACHY_ENGAGE_HEURISTIC=1` escape hatch: set to bypass the LLM classifier and run
  the original coherent-sentence-in-window heuristic for the full process lifetime.
- DEGRADE graceful degradation: if the classifier errors or times out, the gate silently
  falls back to the heuristic so the hearing loop never stalls.
- `reachy/speech/llm.py` non-streaming `complete()` — single-shot completion used by
  the classifier (tight ~5 s timeout, same `REACHY_OPENAI_*` endpoint as cognition).

### Changed

- 3-tier motion ladder under `--transcribe` replaces the previous blanket turn
  suppression: ambient noise → Tier-1 antenna lean; detected speech → bounded head-only
  orienting nudge toward DoA; engaged utterance (gate ENGAGE) → deliberate head/body
  turn toward the speaker's DoA, clamped to a minimum duration to prevent SDK
  `goto` planner faults. The robot now faces you when you speak to it.

## [0.27.0] - 2026-06-21

### Added

- listen --live --transcribe transcribes whole utterances (endpointing on a pause) instead of 1.5s rolling-window fragments, so cognition hears full sentences
- Engagement gate: --live --transcribe responds only to clear sentences addressed by name (reachy/robot) or continuing an ongoing conversation; ambient noise and short fragments are ignored
- Transcript cues now carry the speaker direction — heard someone say (from the left): ...
- reachy.speech.stt.Transcriber.transcribe_once() — single-POST full-utterance transcription

### Changed

- TTS client retargeted from Magpie to model-gear Chatterbox: JSON {text,voice} body, voice:null default, 24 kHz
- listen --live cognition speech plays through the daemon (HTTP) instead of opening a second ReachyMini client (single-SDK-owner)
- listen --live --transcribe drives cognition from transcribed WORDS only (raw DoA/loudness cues no longer feed cognition) and suppresses the Tier-2 head/body auto-turn (antenna lean still reacts to sound)

### Fixed

- SDK speaker playback resamples PCM to the device output rate (16 kHz) — TTS no longer plays slow/low-pitched (latent bug for any non-16kHz TTS)
- Self-feedback loop: the robot no longer reacts to its own TTS as loud sound and chatters
- Self-mute window now covers the full spoken-clip duration so the robot never transcribes its own (long) voice

## [0.26.0] - 2026-06-21

### Added

- `listen run --live --transcribe`: optional STT transcribes nearby speech (model-gear / Parakeet at `REACHY_STT_URL`) and feeds the recognised WORDS into live cognition, so the robot reasons about what was said, not just that a sound came from a direction. Off by default; requires `--live` + the `sdk` transport. A self-mute window stops the robot transcribing its own voice; an unreachable STT degrades to no-words and never stalls the loop. Not a dialogue/turn-taking assistant.
- `reachy/speech/stt.py` shared `Transcriber` — the Parakeet `/v1/audio/transcriptions` WAV-multipart leg (stdlib urllib + numpy), returning transcript text.
- `SenseSample.audio` optional raw per-tick mic chunk; `EventBuffer.feed_transcript` transcript cue; `reachy/motion/listen_transcribe.py` `TranscribeHook` (rides the shared sample, opens no second media session).

### Changed

- `sleep` wake-word `HttpSttBackend` now delegates transcription to the shared `Transcriber` (one STT client, no duplicated WAV/multipart/urllib stack).
- the systemd `live` boot unit runs `listen run --live --transcribe`, so the on-robot presence hears words by default (CLI default stays off).

## [0.25.0] - 2026-06-21

### Added

- listen run --live --export -/--export-blocks — the folded live loop now streams the same thinking/message/emotion JSONL feed as think run --export, so the boot-persistent presence loop can publish what the robot is thinking to any subscriber (reTerminal panel, log, audio renderer) over the one documented wire contract. Built on a shared reachy/cli/_export.py used by both think and listen.
- CognitionEngine(audio_optional=...) — TTS/playback is now a degradable output: in audio-optional mode a synth failure degrades to no-speech (logged once, clip skipped) instead of crashing the cognition worker, and latches off after consecutive failures so a wedged TTS never throttles cognition. The folded listen --live engine opts in, so a dead TTS endpoint no longer silently kills live thinking.

### Changed

- Factored the --export/--export-blocks wiring out of think.py into the shared reachy/cli/_export.py (build_export_hook + add_export_args); think run is behaviourally unchanged.

### Fixed

- listen --live cognition no longer dies when the TTS endpoint is unreachable/wedged (it previously raised TimeoutError out of the cognition worker and stopped thinking for the life of the process).
- test_motion.py now isolates REACHY_STATE_DIR so the pure idle-producer tests no longer read the real *_active flags a running listen --live service toggles — removing an intermittent pytest -n auto flake on the robot box.

## [0.24.0] - 2026-06-21

### Added

- think/cognition LLM endpoint is now configured by the canonical REACHY_OPENAI_URL_BASE / REACHY_OPENAI_API_KEY / REACHY_OPENAI_MODEL_ID environment variables (OpenAI-compatible naming)

### Changed

- LLM config resolution prefers the REACHY_OPENAI_* names, keeping the legacy REACHY_LLM_BASE_URL / REACHY_LLM_API_KEY / REACHY_LLM_MODEL names working as a non-breaking fallback; help text, explain catalog, and the operating-guide env table updated to the new names
- LLM env precedence is by presence, not truthiness: a set-but-empty `REACHY_OPENAI_*` variable (or an explicit empty `--llm-*` override) wins over the legacy name and the default, so an empty `REACHY_OPENAI_API_KEY` means "no auth" instead of silently sending a stale `REACHY_LLM_API_KEY`

## [0.23.0] - 2026-06-20

### Added

- `reachy service` noun — make the robot boot-persistent in **exactly one** presence mode (demo idle, or the live folded sense loop) via systemd `--user` units: `service enable demo|live` / `disable` / `status` / `install` / `uninstall`. Enabling one mode disables the sibling (the single-presence-owner invariant), and the daemon (`reachy-daemon.service`) is a boot dependency the presence units `Requires=` / `After=`. Backed by `reachy/service/units.py` (unit-text renderers) + `reachy/service/manager.py` (`ServiceManager`).
- `listen run --live` — the folded live sense loop: `think`, `vision`, and `sleep` run *inside* `listen`'s single loop alongside `pat` (via `reachy/motion/listen_hooks.py` `HookChain` + the shared `reachy/motion/sense_sample.py` `SenseSample` provider), so all senses share one SDK media session and one motion queue, arbitrated by the `sleep > pat > think` flags — no second media session, no ~1 Hz contention. New hooks: `reachy/motion/listen_{think,vision,sleep}.py`.
- Reboot-survival integration test (`tests/test_service_integration.py`) proving the single-presence-owner invariant and daemon-first ordering across a simulated re-login.

### Changed

- Live presence is now the CLI-generated `reachy-live.service` running `listen run --live`, retiring the hand-authored `reachy-listen.service`.
- README + CLAUDE.md noun catalogs and `docs/operating-reachy.md` document the `service` noun + the `--live` folded loop; the `explain` catalog gains a `service` entry.

### Fixed

- The SDK `listen` loop no longer leaks file descriptors: per-tick `head_pose` reads, per-move `move_goto`, and (in `--live`) per-frame `get_frame` now ride the loop's ONE open `ReachyMini` client through `MediaSession` instead of opening a fresh client per call. Each per-call `ReachyMini` construction leaked fds via the SDK's `GStreamerAudio` teardown, exhausting the process fd limit (`Too many open files`) and crash-looping `reachy-listen.service` every ~5 minutes (issue #51). A shared `_goto_kwargs` helper + a `_SessionBoundTransport` proxy route the loop's reads through the held session; tick-invariance and one-client-per-loop tests guard the regression.

## [0.22.0] - 2026-06-15

### Added

- docs/operating-reachy.md: a coherent operating guide — what Reachy can do, the single-SDK-owner model (with a mermaid diagram + conflict matrix), live bring-up, transports, verification, the ~/.asoundrc mic-array gotcha, a full environment-variable reference table, a troubleshooting table, and a per-noun technical reference (#44)
- README: a complete noun map covering every robot noun, and a prominent pointer to the single-SDK-owner model

### Changed

- README reorganized into a lean front door (overview + noun map + quickstart + links into the operating guide); cross-cutting install/transport/daemon detail now lives once in the guide
- CLAUDE.md architecture section restructured for navigability (overview map, Core CLI contract, single-SDK-owner contributor note, noun catalog table, per-noun internals headings) and updated to reflect #43 (pat folded into listen)

### Fixed

- CLAUDE.md no longer claims the repo is an unmodified template with no robot functionality (the framing was stale)

## [0.21.0] - 2026-06-14

### Added

- `.claude/skills/ask-colleague/` — first-party **ask-colleague** skill (origin: colleague). Drives the `colleague` CLI to hand a scoped repo task to a *different* backend/model (a second, independent mind) and fold the answer back: `review` (diverse second opinion on a committed diff), `explore` (fresh read-only read of an area), `write` (preview-by-default implementation; `--apply`/`--pr` to land), `feedback` (grade a finished work item — the ROI loop), and `clean` (reap stale `colleague/*` branches/artifacts a crashed run left behind). `explore`/`review` are read-only via throwaway-worktree isolation. Added via the mass-update skill (PR #46).

## [0.20.0] - 2026-06-14

### Added

- `listen` now detects head **pats** inside its sdk loop (motion + pat in one mode): each tick reads `head_pose` back through the loop's own fast sdk client and feeds a `PatDetector`, enqueuing a lean→nuzzle→settle `PatReaction` and raising the `pat_active` flag on a press. A separate `pat` process can't (sdk contention throttles head_pose to ~1Hz). New `--pat/--no-pat` (default on, sdk-only) + `--press-threshold`/`--min-presses`; new `on_tick` seam on `reachy.motion.server.run`; new `reachy/motion/listen_pat.py`.

## [0.19.0] - 2026-06-14

### Added

- `think run --export -` / `--export-blocks`: export the robot's thinking / message / emotion blocks as a live newline-delimited JSON feed on stdout for an external display (e.g. the reTerminal). New `reachy/export/` package (event model + `to_jsonl`, block-selection parser, broken-pipe-safe stdout exporter); a passive cognition export hook taps the raw LLM turn stream (thinking.text) before the MarkerParser discard. stdlib `json` only — no new dependency; the renderer stays out of the repo. Schema: `docs/export-schema.md`.

## [0.18.1] - 2026-06-14

### Added

- Spec: export reachy's thinking/message/emotion blocks as a live stdout JSONL feed for an external reTerminal display (`docs/specs/`, via /think) — `think run --export -`; renderer stays out of the repo; transport is stdout-only for v1.

## [0.18.0] - 2026-06-12

### Changed

- sleep wake-word HTTP STT backend now speaks the real model-gear / NVIDIA Parakeet contract: `POST /v1/audio/transcriptions` as a multipart WAV upload (was a guessed `/v1/audio/transcribe` raw-PCM POST). Matches the wake phrase against the OpenAI/Parakeet response `text` field (legacy `transcript`/`detected`/`phrase` still honoured). Default `REACHY_STT_URL` is now `http://localhost:9002` (Parakeet on the same box); new `REACHY_STT_LANGUAGE` env var.
- `HttpSttBackend` now accumulates a rolling ~1.5 s audio window and throttles POSTs (one tick mic chunk is too short to transcribe a phrase); the real mic sample rate from the SDK transport is carried into the WAV header. `window_seconds`/`min_interval`/`clock` are injectable seams for tests.

## [0.17.0] - 2026-06-12

### Added

- sleep run --no-audio-wake (alias --wake pat): pat always wakes a sleeping robot; this flag disables audio-wake so only a physical head pat rouses it — requires the SDK transport (pat reads head_pose; http raises a clean exit-2 CliError)
- sleep run --wake-word (+ --wake-word-kind {http,openwakeword}, --wake-phrase): opt-in Tier-2 wake-word detection with a pluggable backend — external HTTP STT service (default, stdlib urllib; REACHY_STT_URL / REACHY_STT_PHRASE / REACHY_STT_TIMEOUT, mirrors the Magpie TTS pattern) or on-box openwakeword under the [cpu] extra (lazy-loaded)
- reachy/sleep/wakeword.py resolve_backend: pluggable wake-word backend resolver — http (external STT service, no extra required) or openwakeword ([cpu] extra, lazy import)
- reachy/sleep/patwake.py PatWakeDetector: pat-based wake detector that measures head-pose deviation against the MOVING sleep-breathe commanded pose (not a fixed baseline), reusing reachy/motion/pat.py PatDetector (numpy + stdlib only)

### Changed

- [gpu] extra no longer implies an on-box STT model — it is a generic compute-class pin for future GPU-accelerated features; wake-word on GPU is not a current use case and the [gpu] comment is updated accordingly

## [0.16.0] - 2026-06-12

### Added

- sleep noun: graduated alert->drowsy->asleep idle-decay state machine with injected-clock seam (reachy/sleep/state.py)
- sleep mode wakes on speech/snap (Tier-1, zero new base dep) plus an optional wake-word phrase behind generic [cpu]/[gpu] compute-class extras (Tier-2, lazy + graceful degrade)
- sleep run/start/stop/restart/status/demo/overview verbs (reachy/cli/_commands/sleep.py); demo walks the full arc headless with no robot
- SleepProducer drives a drowsy energy-fade then a near-still sleep-breathe (slow rock + antenna breathe) and a wake re-engagement gesture onto the shared MotionQueue (reachy/motion/sleep.py)
- cross-noun sleep_active.flag (reachy/motion/sleep_signal.py): the listen idle layer fully yields to it as the strongest interrupt, above pat and think-focused
- qualifying-stimulation classifier with self-mute exclusion so the robot cannot keep itself awake by speaking (reachy/sleep/stimulus.py)

### Changed

- listen idle producer now treats sleep as the top-priority interrupt (full wander suppression while asleep)

### Fixed

- sleep-breathe ramp now measures from ASLEEP entry (not producer lifetime), so every sleep cycle eases in softly even after long uptime (reachy/motion/sleep.py)
- sleep supervisor clears the pid file when a spawned loop exits during the startup grace window, so status/stop no longer report a stale pid (reachy/sleep/supervisor.py)
- sleep status reports idle_seconds as null instead of a fabricated 0.0 — the live idle timer lives in the loop process and is not observable across processes (reachy/cli/_commands/sleep.py)
- SleepStateMachine.reset() clamps backwards ticks, matching update() and the documented contract (reachy/sleep/state.py)
- WakeDetector.reset() rebuilds the SnapDetector from its own retained config instead of SnapDetector private attributes (reachy/sleep/wake.py)
- refactor run_sleep_arc into small helpers (_doa_shifted/_advance/_sync_sleep_flag/_call_bool/_call_float) to cut cognitive complexity below the gate; dropped the unused sense/snap/sound_present params from SleepProducer.update; merged the Tier-2 wake nested-if (SonarCloud)

## [0.15.0] - 2026-06-11

### Added

- **`pat` noun — proprioceptive touch + snuggle.** Scratch Reachy Mini's
  head (pitch press) or nudge it sideways (yaw press) and it leans/snuggles
  into your hand — detected with NO touch sensor by comparing the commanded
  head pose against the actual pose read back from the SDK
  (`get_current_head_pose()`). Ported + improved from `reachy_nova`'s
  `PatDetector`.
- `reachy/motion/pat.py` — `PatDetector`: EMA-baselined commanded-vs-actual
  deviation on pitch (scratch) and yaw (side-nudge), press/release hysteresis,
  press-count window, level1/level2 state machine with cooldowns (pure numpy,
  deterministic-testable via injected clock).
- `reachy/motion/pat_reaction.py` — `PatReaction`: enqueues a
  lean→nuzzle→settle gesture (a soft body-yaw lean toward the hand, antenna
  affection, and a settling sigh) onto the shared serial `MotionQueue`.
- Transport `head_pose()` readback (`reachy/robot/`): SDK reads the live 4×4
  head pose, extracted to (pitch, yaw) degrees in pure numpy (no scipy);
  http/base raise a clean exit-2.
- `reachy-mini-cli pat` CLI noun — `run` (foreground loop), `demo` (no robot),
  `overview`; `--json` everywhere; sdk-first with `--transport http` fallback.
- A pat **breaks the idle stillness**: `reachy/motion/pat_signal.py` writes
  `pat_active.flag`; the `listen` idle loop fully suppresses its wander for the
  whole reaction (counterpart to `think`'s focused-idle `think_active.flag`).
  The `run` loop routes all motion through the single serial executor and pauses
  sensing while the lean plays, so the robot's own motion never self-triggers.

### Changed

- Reduced cognitive complexity of `PatDetector.update` and the `pat run` loop
  (SonarCloud `S3776`): the detector's per-axis press tracking and two-level
  state machine are split into `_track_pitch` / `_track_yaw` / `_advance_state`
  helpers, and the run loop's sense→detect→react step into
  `_sense_and_maybe_react`. Pure refactor — no behavior change.

## [0.14.0] - 2026-06-10

### Added

- `think` now **thinks with its body**: the cognition LLM interleaves `*emoji*`
  expression markers and `"quoted"` speech in its output. Only quoted text is
  spoken aloud; each `*emoji*` drives one calm expression move on the robot.
  Parsing is handled by a streaming `MarkerParser` (`reachy/speech/markers.py`)
  that feeds `MarkerEvent` / `SpeechEvent` values into the cognition pipeline.
- `reachy/speech/expressions.toml` — an emoji-keyed, editable data file mapping
  each emoji to a target head/antenna/body pose. Loaded via stdlib `tomllib`
  (no new dependency). Starter set: 🤔 😮 🙂 👂 😐 🎉 😔 + neutral fallback.
  Tune expressions by editing this file; no code change needed.
- `reachy/speech/expressions.py` — `Catalog` / `ExpressionPose` / `load_catalog`
  / `get_pose` API wrapping the TOML file. `ExpressionProducer`
  (`reachy/motion/expression.py`) enqueues calm one-shot expression moves onto
  the serial `MotionQueue` from the cognition thread.
- `reachy/speech/distinctness.py` — weighted Euclidean pose-distance scorer that
  detects catalog entries too similar to be meaningfully distinct.
- `think expressions` sub-noun — two catalog tooling verbs (both `--json`-ready):
  - `reachy-mini-cli think expressions` / `reachy-mini-cli think expressions list`
    — list every catalog emoji with a generated pose descriptor.
  - `reachy-mini-cli think expressions check` — flag expression pairs whose poses are too
    similar to tell apart (exit 0; `ok` field is the machine-readable signal).
- **Focused idle while thinking:** while `think run` is active it writes a
  `think_active.flag` file under `$REACHY_STATE_DIR` via `cognition_signal`
  (`reachy/speech/cognition_signal.py`). A co-running `listen`/idle loop reads
  this flag on each tick and drops to a low-energy "focused breathe" — the body
  quiets, reducing wander amplitude so stillness becomes the thinking posture.
- **Self-mute guard:** `think run` mutes the sense feed for `--mute-after-speak`
  seconds (default 2.5 s) after each playback clip to prevent the robot from
  reacting to its own voice through the shared USB audio device.

### Fixed

- `MotionQueue` is now thread-safe: an internal lock guards the pending list and
  a new atomic `pop_if` removes the head only when it is still the dispatched
  action. This closes a race `think` introduced by draining the queue on the
  motion-executor thread while the cognition thread submits gestures — a blind
  `pop` could otherwise drop a gesture that coalesced in mid-dispatch.
- Hardened the cognition system prompt to instruct the LLM to emit nothing
  outside `*emoji*` markers and `"quoted"` speech (unquoted text is discarded,
  not spoken), reducing the chance of an unquoted lead-in being voiced.

## [0.13.0] - 2026-06-10

### Added

- `say` noun — dumb TTS pipe: text → Magpie-style TTS synthesis → robot speaker
  playback. No LLM, no senses. Verbs: `run` (text or stdin `-`) and `overview`,
  each with `--json`. TTS via `REACHY_TTS_URL` / `REACHY_TTS_VOICE`; playback via
  SDK (default) or HTTP daemon transport (`REACHY_TRANSPORT`).
- `think` noun — sentence-streamed continuous cognition loop: snapshots live senses
  (DoA + mic loudness) into an event buffer, streams a short spoken thought from the
  LLM, and plays each sentence while the LLM generates the next. SDK-first (same
  two-transport model as `listen`). Verbs: `run` / `start` / `stop` / `restart` /
  `status` / `overview`, each with `--json`. LLM via `REACHY_LLM_BASE_URL` /
  `REACHY_LLM_API_KEY` / `REACHY_LLM_MODEL` (pure `urllib` streaming, no new base
  dep); TTS/playback reuses `say`'s speech leg. Managed by its own supervisor
  (`reachy/speech/supervisor.py`).
- `explain` catalog entries for `say` and `think` (noun roots + all verbs).
- README and CLAUDE.md architecture docs for `say` and `think` with env-var reference.

## [0.12.0] - 2026-06-10

### Added

- `vision` noun — a pixel-based, low-compute visual sense (motion via frame differencing + light via brightness/centroid) that orients the head toward the strongest visual event, mirroring `listen` on the serial motion queue. Local-profile only (frames via the SDK/IPC camera path); no ML/GPU. Verbs: run/start/stop/restart/status/specs/overview, each with --json. Camera frame access added to the transport layer (SdkTransport.get_frame / HttpTransport.camera_specs).

## [0.11.0] - 2026-06-10

### Added

- `quickstart` verb — prints the copy-paste install + start-real-mode sequence in text or `--json`, available on any install profile (no daemon needed); resolvable via `explain quickstart`.

### Changed

- Front-door text now describes the Reachy Mini robot CLI instead of the cloned agent template: the `--help` description + a getting-started epilog pointing at `quickstart`/`learn`, the `learn` purpose paragraph + an Install block, and the `explain` root entry.
- README now leads with `uv tool install 'reachy-mini-cli[daemon]'` as the primary install path and relabels the old Quickstart as the Developer quickstart.

### Fixed

- `explain` root listed the robot nouns but omitted `listen` — added it.

## [0.10.0] - 2026-06-06

### Added

- `listen` is now always-alive: between sounds the robot keeps gently breathing,
  gaze-wandering, and swaying its antennas around its *current* heading instead of
  freezing. New `--idle-energy` (0 disables, restoring hold-still) and `--drift-speed`
  knobs, threaded through the background supervisor.

### Changed

- After turning toward a sound, `listen` now *stays rotated* and keeps the idle motion
  around that heading, then drifts the head+body slowly home over `--recenter-after`
  seconds rather than hard-snapping back to front. The hold window no longer freezes the
  robot — the idle layer keeps it alive even right after a turn. The shared idle-pose
  generator (`AliveConfig`/`next_pose`) moved to `reachy/motion/idle.py` (re-exported
  from `reachy.alive` for back-compat).

### Fixed

- `listen` right-antenna lean direction: the right antenna now perks toward a right-side
  sound instead of leaning the wrong way (its joint sign is mirrored from the left).

## [0.9.0] - 2026-06-06

### Added

- `reachy-cli` is published as a transitional alias distribution
  (`packaging/reachy-cli/`, metadata-only) that depends on `reachy-mini-cli` at the
  matching version and forwards the `[daemon]`/`[sdk]` extras — `pip install
  reachy-cli` keeps working. The publish workflow now builds and publishes both
  names via Trusted Publishing.

### Changed

- Renamed the distribution to `reachy-mini-cli` (canonical PyPI name); the console
  command is now installed as both `reachy` and `reachy-mini-cli`. The import
  package stays `reachy`. `__version__` now reads the `reachy-mini-cli` metadata,
  and all install hints/docs point at the new name.

## [0.8.0] - 2026-06-06

### Added

- Two-tier `reachy listen`: Tier-1 near-side antenna lean toward faint sound; Tier-2 head->body "turn to see" on detected speech or a loud RMS snap transient.
- Real mic loudness via a `SnapDetector` (RMS spike, algorithm cited from reachy_nova `detect_snap`), fed from the SDK `media_session()` audio stream.
- SDK-based daemon/robot liveness (`is_robot_live`) that stays correct across a daemon restart (#21).
- New `listen` tuning flags: --antenna-gain/--antenna-max/--body-yaw-max/--body-speed/--head-only-band/--snap-ratio/--snap-floor.
- `ANTENNA_KEY` coalesce key in the motion queue so antenna leans coalesce independently of head moves.

### Changed

- `listen` is now SDK-first: the SDK transport is listen's default (real DoA + mic loudness in-process), with `numpy` as a base dependency for the RMS detector. `reachy-mini` stays a `[sdk]`/`[daemon]` extra (its cairo/gstreamer stack can't be a base dep without breaking bare/CI installs); running the `sdk` transport without it gives a clean exit-2 hint. The HTTP transport remains an optional remote profile via `--transport http`.
- Latched-DoA guard: a head turn fires only on live speech/snap, never on a frozen DoA angle (the daemon latches the last direction at rest).

## [0.7.0] - 2026-06-06

### Added

- `listen` noun group — a standalone, smooth sound-orienting loop. Reads the mic array's Direction of Arrival (DoA) from the daemon and turns the head toward a *sustained, off-axis* sound (deadband + dwell), holds there briefly, then eases back to center after silence. Verbs: `run`, `start`, `stop`, `restart`, `status`, `overview` — each with `--json`; tune the feel with `--dwell` / `--hold` / `--speed` / `--deadband` / `--gain` / `--recenter-after` / `--speech-only`. Process-managed like `demo-mode` (PID + log under the state dir). Degrades gracefully: no mic / no daemon DoA ⇒ no reaction, no crash.
- `reachy/motion/` — a serial motion subsystem: a coalescing `MotionQueue`, an executor that runs interpolated daemon `goto` moves strictly one at a time (never overlapping or resetting each other), and the `ListenProducer` (the DoA→look decision). The smooth trajectory is the daemon's minjerk planner.

### Changed

- Sound-orienting now drives the daemon's smooth minjerk `goto` planner via `reachy listen`, instead of the behavior engine's immediate `set_target` stream (jerky for big reorienting turns).
- HTTP transport maps the CLI's `--interpolation ease` to the daemon's `ease_in_out` (the daemon rejected `ease` with HTTP 422), matching the SDK transport.

### Removed

- The `listen` **behavior** (the PR #20 `behavior run listen`, a 50 Hz `set_target` streamer) — superseded by the `reachy listen` loop above. The engine keeps its general sensor-input capability (`wants_sense`, abstention, DoA in `behavior status`), but ships no built-in sensor behavior.

## [0.6.0] - 2026-06-06

### Added

- behavior run listen — sound-reactive behavior that orients the head (and optionally the body, via --set body_gain) toward the sound Direction of Arrival read from the daemon; reacts to any sound by default (--set speech_only=1 for speech only), and degrades gracefully when the mic is unavailable
- reachy/behavior/sense.py: Sense snapshot, DoaPoller (throttled, error-tolerant DoA reader), and HttpTransport.doa() over GET /api/state/doa

### Changed

- Behavior contribution signature is now fn(t, params, sense); the engine arbitration is abstention-aware — a behavior that returns None for a claimed channel yields it to the next-priority claimant, so listen falls back to feel-alive when there is no sound
- behavior status now reports the live (resolved) per-channel ownership plus the latest DoA snapshot

## [0.5.0] - 2026-06-05

### Added

- `behavior` noun group: compose robot behaviors on a persistent 50 Hz loop. Push one-shot ("look up-and-aside, hold 5s") or looping ("speak: bob the head for N seconds or until stopped") behaviors onto a running engine; a per-channel contention model decides who drives `head` / `antennas` / `body_yaw` when they conflict. Verbs: `list`, `run`, `stop`, `status`, `engine start|stop|status|run`, `overview` — each with `--json`.
- Four-class contention model (`passive` / `stoppable` / `unstoppable` / `stopping`): a `stopping` behavior evicts the `stoppable` ones on its channels, an `unstoppable` holds its channels until it finishes, and the `passive` base layer only drives a channel nothing else claims. Same-channel conflicts resolve by class priority, then most-recent.
- `feel-alive` runs as a **passive base layer** (default on; `--no-base-layer` to disable) — a continuous idle motion (breathing, slow gaze wander, antenna sway), so an idle robot stays alive on any channel no behavior has taken. This generalizes `demo-mode`; the existing `demo-mode` noun is unchanged (migration is future work).
- `reachy.behavior` package: `model` (channels, classes, lifetimes, the pure `Behavior`), `arbitration` (the pure `arbitrate`/`admit` core), `library` (built-in parametric behaviors: gaze-hold, nod, shake, speak, thoughtful, antenna-sway, body-turn-hold, feel-alive), `engine` (the 50 Hz compose loop), `control` (a command-spool + state-file IPC under the state dir), and `supervisor` (a PID-file process manager). Stdlib only — no new base runtime dependency.
- Immediate-target streaming on the transport: `Transport.set_target(...)` (`POST /api/move/set_target` for http; `ReachyMini.set_target` for sdk) and a `streaming()` session that holds one robot connection open for the whole loop — so the 50 Hz stream pays the open/close cost once, not per pose.

### Changed

- Extracted the signal-stoppable / interruptible-sleep loop helpers from `reachy.alive` into a shared `reachy.looputil` (used by both demo-mode and the behavior engine), with a configurable sleep slice for high-rate loops.

### Notes

- While the engine runs it streams immediate targets and **owns robot motion exclusively** — don't drive the robot with `move goto` / `demo-mode` at the same time (the daemon ignores `set_target` while an interpolated move is running).

## [0.4.0] - 2026-05-30

### Added

- `demo-mode` noun group: a continuously-running, managed loop that makes the Reachy Mini *feel alive* with gentle idle motion (breathing oscillation, occasional glances, antenna sway). Verbs: start/stop/restart/status/run, config, install/enable/disable/uninstall, overview — each with --json.
- `reachy.alive` module: pure idle-motion generator (`next_pose`, `AliveConfig`, `neutral_pose`), a signal-clean foreground `run_loop` that tolerates transient daemon errors and eases the robot to neutral on stop, and a PID-file process supervisor (start/stop/restart/status) mirroring `reachy.daemon` — stdlib only, no new base runtime dependency.
- `reachy.demo_config` — persisted JSON tuning at `$XDG_CONFIG_HOME/reachy/demo-mode.json`, read by `run`/`start` (CLI flags override; precedence flag > config > default). `demo-mode config [--init] [--set key=value …]` shows/scaffolds/sets it.
- `reachy.demo_service` — systemd `--user` supervision so the loop runs always-on (auto-restart on crash, start on boot via linger): `demo-mode install/enable/disable/uninstall`. Stdlib-only `systemctl`/`loginctl` (graceful exit-2 when absent).
- `demo-mode restart` applies an update: restarts the systemd service if active, else relaunches the background loop — re-importing the latest motion code and re-reading config.
- Motion tuning: --interval (tempo), --energy (liveliness multiplier), --interpolation, --seed (reproducible idle motion).

## [0.3.0] - 2026-05-30

### Added

- `daemon` noun group (`start`/`stop`/`status`/`overview`): bring the local `reachy-mini-daemon` process up and down — background spawn + PID/log under `$XDG_STATE_HOME/reachy`, health-poll on `GET /api/daemon/status`, idempotent start, SIGTERM-then-SIGKILL stop.
- `reachy/daemon.py` — stdlib-only daemon process-lifecycle module (no new runtime dependency).
- `[daemon]` optional-dependencies extra (`reachy-mini>=1.0`) — the recommended default install, providing the `reachy-mini-daemon` binary.

### Changed

- Inverted the install model: `pip install 'reachy-cli[daemon]'` is now the default (bundles the daemon); the bare `pip install reachy-cli` is the HTTP-only *remote* profile. Base stays zero-runtime-deps.
- The `http` transport's daemon-unreachable hint now points at `reachy daemon start` and the `[daemon]` install.
- README + CLAUDE.md document the daemon noun, the install profiles, and the daemon-up wake-up flow.

## [0.2.0] - 2026-05-30

### Added

- `device` noun group: `status` (daemon status), `state` (live robot state)
- `app` noun group: `list`, `status`, `start <name>`, `stop`
- `move` noun group: `goto` (mm + degrees; `--antennas`/`--body-yaw`/`--duration`/`--interpolation`), `wake`, `sleep`
- Robot transport layer with two selectable flavors: `http` (stdlib-only daemon REST client, default) and `sdk` (optional `reachy_mini` client behind the `[sdk]` extra), via `--transport` / `REACHY_TRANSPORT`
- `explain` catalog entries and `overview`/`learn` command maps for the new robot nouns

### Changed

- README documents robot operations, transports, and the [sdk] optional extra

## [0.1.2] - 2026-05-30

### Changed

- Replaced the CLAUDE.md bootstrap seed with a full runtime prompt (ran /init): documents the agent-first CLI architecture, the verb/noun registration pattern, the structured-error and stdout/stderr contracts, the zero-runtime-dependency and version-bump-every-PR constraints, and flags that the repo is still the unmodified culture-agent-template clone (no Reachy robot functionality yet) plus the reachy vs reachy-mini-cli console-script naming drift.

### Fixed

- Added a `reachy` (console-script name) entry to the explain catalog so `explain reachy` resolves. The agent-first rubric's `explain_self` check derives the tool name from `[project.scripts]` (`reachy`), which the `reachy-mini-cli`-keyed catalog did not cover — the `lint` job's rubric gate failed on it. Does not touch the broader `reachy` vs `reachy-mini-cli` display-name drift (still documented in CLAUDE.md as a deferred decision).
- Re-synced uv.lock with pyproject.toml — the lockfile still carried a stale reachy-mini-cli editable package entry; it now matches the actual distribution name reachy-cli.

## [0.1.1] - 2026-05-26

### Changed

- **CI gates on the SonarCloud quality gate**
  ([issue #3](https://github.com/agentculture/reachy-mini-cli/issues/3)) —
  added `sonar.qualitygate.wait=true` to `sonar-project.properties` so a failing
  gate fails the `test` job when `SONAR_TOKEN` is set. Token-less repos and fork
  PRs remain green (the scan step is guarded by `if: env.SONAR_TOKEN != ''`).

## [0.1.0] - 2026-05-26

### Added

- **Onboarded into the AgentCulture mesh** ([issue #1](https://github.com/agentculture/reachy-mini-cli/issues/1)).
- **Agent-first CLI** cited from teken's (`afi-cli`) `python-cli` reference
  (`teken cli cite`) — verbs `whoami`, `learn`, `explain`, `overview`, `doctor`,
  and the `cli` noun group. Runtime is self-contained (`dependencies = []`);
  `teken>=0.8` is a dev dependency only. Passes the seven-bundle agent-first
  rubric (`teken cli doctor . --strict`). `doctor` checks the agent-identity
  invariants (prompt-file-present, backend-consistency, skills-present).
- **Mesh identity**: `culture.yaml` (`suffix: reachy-mini-cli`,
  `backend: claude`) and the matching `CLAUDE.md` prompt file.
- **Canonical guildmaster skill kit** (11 skills) vendored under
  `.claude/skills/` (cite-don't-import): `agent-config`, `assign-to-workforce`,
  `cicd`, `communicate`, `doc-test-alignment`, `pypi-maintainer`, `run-tests`,
  `sonarclaude`, `spec-to-plan`, `think`, `version-bump`. Every `SKILL.md`
  carries `type: command` (load-bearing for the culture/claude backend);
  `cicd` / `communicate` consumer-identifying prose adapted, all script bodies
  verbatim. Provenance in `docs/skill-sources.md`. Three skills (`think`,
  `spec-to-plan`, `assign-to-workforce`) originate in `devague`, re-broadcast
  via guildmaster.
- **Build + deploy baseline**: `pyproject.toml` (hatchling), `tests/` (pytest,
  xdist, coverage), `.github/workflows/{tests,publish}.yml` (CI rubric/lint gate,
  PyPI Trusted Publishing), `.flake8`, `.markdownlint-cli2.yaml`,
  `sonar-project.properties`, and `.claude/skills.local.yaml.example`.

### Changed

### Fixed
