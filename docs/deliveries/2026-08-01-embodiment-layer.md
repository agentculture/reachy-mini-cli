# Delivery Summary — embodiment layer

plan: `embodiment-layer` · run: `partial` · date: `2026-08-01`
baseline: `devague summary skeleton`

## Intent

Give Reachy a detachable realtime harness under the `agent` noun — one that
hears through the robot's own microphone, thinks over the streaming
chat-completions lane, speaks through the robot's speaker, and operates the
robot only through a closed direct-operation action set. The load-bearing
constraint was that it runs *beside* the symbolic runtime and never inside it:
enabling or disabling the layer must change nothing about how the robot behaves
on its own.

All sixteen plan tasks were fanned out to isolated worktrees and merged under a
TDD gate (suite green before and after each merge). The run is recorded as
**partial** for one reason, stated plainly rather than rounded up: `t14`'s
headline acceptance — a sustained out-loud conversation between the lobes `site/`
harness and the layer — was **not** achieved, blocked by a hardware limitation
of this box. Everything else in `t14` was demonstrated live.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — PROBE concurrent sessions: hold a transcription-only session and an armed conversational session against the deployed lobes /v1/realtime simultaneously (scratch script; evidence to docs/evidence/)
- `t2` — PROBE video wire-format: send a real short clip as video content parts to model=worker over the deployed gateway /v1/chat/completions (streaming)
- `t3` — Realtime wire response.\* family: extend reachy/speech/`realtime_wire.py` codec + tests/`fake_realtime_server.py` with response.created / response.text.done / response.audio.delta / response.done / response.interrupted and response.create arming
- `t4` — Audio tee: third AudioPump consumer fanning the ONE per-tick chunk to a local unix-socket sink (reachy/behavior/`audio_tee.py` + composition in `_commands`/behavior.py)
- `t5` — Clip rider: rolling last-X-seconds frame ring + bounded clip files under the state dir + text reference published on the bus (reachy/behavior/`clip_rider.py`, composed after the tee lands)
- `t6` — Embody media profiles: injectable audio source/sink seams with bench (dev-box webcam mic + monitor speakers + AEC-on-capture) and robot (tee socket reader + daemon http playback) profiles (new reachy/embody/media.py)
- `t7` — Embody tool registry: the direct-operation action set with containment (new reachy/embody/tools.py) — goto via intents spool, sound via say/harmonics seams, `run_behavior` via spool, create-rule via embody-\* prefixed atomic overlay write + reload spool
- `t8` — Embody cue intake: bus/feed consumer mapping rule fire/suppress, pat, face, intent and motion lines to cues (new reachy/embody/cues.py; events-cli subscribe with feed-tail fallback)
- `t9` — Duplex session client: ONE lobes /v1/realtime session per process — streams tee/bench audio in, surfaces server-VAD utterances and response audio deltas out, arms with response.create (new reachy/speech/`realtime_duplex.py`)
- `t10` — Embody turn engine: streaming cognition loop — cues + utterances in, streaming HTTP chat-completions turns out (model per request: worker/senses), tool dispatch over the embody registry (new reachy/embody/engine.py, reusing AgentTurnEngine seams where they fit)
- `t11` — agent embody verb: the composition root — duplex client + media profile + cue intake + turn engine + export hook wired beside attach (reachy/cli/`_commands`/agent.py + explain catalog entry)
- `t12` — Embody supervisor: start/stop/status/restart with pid + log under the state dir (new reachy/embody/supervisor.py + verbs in `_commands`/agent.py)
- `t13` — Runtime-equivalence proof: with the layer absent/disabled nothing changed — full suite + rubric green, runtime diff limited to the additive tee/clip legs; before-state citation refreshed on merge day
- `t14` — Bench acceptance: the lobes site/ harness converses out loud with embody in bench profile — the two realtime APIs literally speak; every after-state capability demonstrated and archived
- `t15` — On-box robot-path verification: tee tick-budget measurement, daemon-route playback under a live engine, dual sessions concurrent — the live halves of h5/h8/h10
- `t16` — Docs: operating-guide embodiment-layer section, CLAUDE.md noun catalog entry, README; fold in the issue #131 speech-transport drift fix

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | Probe run against the deployed gateway; both sessions held concurrently with zero cross-talk. `docs/evidence/2026-08-01-probe-concurrent-realtime-sessions.md` |
| `t2` | delivered | Clean pass: a GIF sent as a `video_url` part decodes as motion where the same bytes as `image_url` flatten to one frame. `docs/evidence/2026-08-01-probe-video-wire-format.md` |
| `t3` | delivered | `response.*` family added to the wire codec; h13 pinned by an AST scan over every dict literal with a `"type"` key. Fake server gained the arm-and-wait scenarios. |
| `t4` | delivered | `reachy/behavior/audio_tee.py`; `_AudioTap` gained `add_sink()` so the tee is *pushed* the one per-tick chunk and structurally cannot take a second one. |
| `t5` | delivered | `reachy/behavior/clip_rider.py`; `FaceSenseDriver.add_frame_sink()` makes it impossible for the rider to open a second camera read. Bounded ring + overwrite-in-place file. |
| `t6` | delivered | `reachy/embody/media.py` — one `EmbodySource`/`EmbodySink` pair for both profiles, no `isinstance` fork (AST-checked). |
| `t7` | delivered | `reachy/embody/tools.py` — five tools, and deliberately **no** `register` method, so the forge's hot-registration door does not exist here. Red-team suite included. |
| `t8` | delivered | `reachy/embody/cues.py` — bus route resolved via a Protocol with a feed-tail fallback; the events-cli subscribe gap is pinned by a canary test. |
| `t9` | delivered | `reachy/speech/realtime_duplex.py` — one armed session; ungated by construction (no gate in the whole import closure, BFS + vacuity guard); mute seam present and OFF. |
| `t10` | delivered | `reachy/embody/engine.py` — cue-triggered streaming turns, no permanent failure latch, stall bound armed on inter-chunk idle. `llm.py` gained an additive reasoning seam. |
| `t11` | delivered | `agent embody` verb + `explain` entry; h14/h15/h22 pinned, including a fresh-interpreter parser probe and a real-composition `sys.modules` probe. |
| `t12` | delivered | `reachy/embody/supervisor.py` + `start/stop/restart/status`; PID identity matched on exact argv tokens, not a substring of `/proc` cmdline. |
| `t13` | delivered | `docs/evidence/2026-08-02-runtime-equivalence.md` — 3 files, 6 hunks, 1486 insertions, **0 deletions**; citation refreshed on merge day as the criterion asks. |
| `t14` | **partial** | Live: the layer heard, thought, **spoke aloud** (134,400 B ≈ 2.8 s), reacted to a rule-fire cue, **moved the robot** (`run_behavior`, `goto` — admitted by the live runtime), and **wrote a working rule** (`create_rule` → runtime `reload applied … react=3`). **Not** achieved: a sustained two-way conversation with the `site/` harness, and `harmonics` live. |
| `t15` | delivered | h10: **zero** overrun ticks with no consumer, an active consumer (6.72 MB drained) and a wedged one. h8: one session, 5 min, zero reconnects, 9,599,488 B. h5: daemon route plays under a live engine with no throttle — audibility itself deferred to a human ear. |
| `t16` | delivered | New operating-guide chapter, CLAUDE.md noun catalog + internals, README, export-schema. Closes #131 and fixes four further doc-vs-code disagreements found on the way. |

## Mid-work Decisions

- `d1` — t14's bench acceptance runs embody in the **ROBOT** media profile instead of the bench profile — "the dev box exposes exactly one usable speaker/mic pair per party: bench profile would put BOTH conversational parties on the same devices, so each would hear itself and h4's echo test would be testing device sharing rather than AEC. Robot profile gives genuine acoustic separation and additionally exercises the deployed path end to end." User-approved 2026-08-02.
- **The tee's wire is pinned in one place both ends cite.** Wave 1 shipped a writer emitting a JSON header + float32 and a reader assuming headerless int16, *and* the two resolved different socket paths — both sides' tests passed. The reader now imports every wire fact and calls the writer's own `socket_path()`. No deviation record: this was a defect fixed at the merge gate, not a plan change.
- **`enable_thinking` stays OFF**, now backed by measurement rather than instinct: thinking costs 9.7 s (worker) to 18.0 s (cortex) before the first content delta. The exported `thinking` block therefore carries cues, reply text, tool calls and results but no model reasoning — the seam is dormant, not broken.
- **The suite was given an actuator ban.** A boundary probe made the deployed robot speak out loud; TTS (`:9000`) and the daemon (`:8000`) are now refused suite-wide. The gateway (`:8001`) is deliberately still reachable, because the opt-in live tests that hit it are the only thing that has ever caught a served-model drift.
- **`install_logging` gained a third call site** (`cmd_agent_embody`), against a CLAUDE.md claim of exactly two. Without it every named failure the layer must surface is an INFO line with no handler, so h22 would be satisfied on paper and invisible in practice.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t14` (`d1`) | the dev box exposes exactly one usable speaker/mic pair per party: bench profile would put BOTH conversational parties on the same devices, so each would hear itself and h4's echo test would be testing device sharing rather than AEC. Robot profile gives genuine acoustic separation and additionally exercises the deployed path end to end. | `acceptable` |
| `t14` | The headline acceptance (≥3 out-loud turns between the two realtime APIs) was **not** reached. `/dev/snd/pcmC2D0p` is held by the daemon and the runtime together, so PipeWire cannot start the device and a second party cannot make sound in the room. No code fix exists; two parties need two output devices. Setup + pass/fail criteria filed as #139. | `needs-follow-up` |
| `t14` | `harmonics` was never dispatched live, and h9 (the worker describing a clip) was not attempted — the layer does not currently consume the clip rider's output at all. | `needs-follow-up` |
| `t15` | h5's *audibility* half is unverified. The obvious probe is structurally wrong: the tee carries the AEC channel of the robot's own mic, which exists to remove the robot's own speaker. Route, timing and non-contention are established; "it was audible in the room" is not. | `needs-follow-up` |

## Evidence

- suite: `4279 passed, 7 skipped` (`uv run pytest -n auto`) — green at every merge gate; stress-run 6× at `-n 32` after the flake fixes with zero failures
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit -c pyproject.toml -r reachy` — all clean
- rubric: `uv run teken cli doctor . --strict` — exit 0
- markdown: `markdownlint-cli2` over all 87 tracked `.md` files — `0 error(s)`
- commits: `7ea6878..0cb6a8f` (48 commits, 72 files, +22,408/−189)
- new modules: `reachy/embody/{__init__,cues,engine,media,supervisor,tools}.py`, `reachy/speech/realtime_duplex.py`, `reachy/behavior/{audio_tee,clip_rider}.py`
- live evidence: `docs/evidence/2026-08-02-{t14-live-acceptance,t15-on-box-verification,live-tee-and-clip-on-the-robot,runtime-equivalence,probe-thinking-vs-reasoning-deltas}.md`
- issues opened: #132, #133, #134, #135, #136, #137, #138, #139 · closes #131 · `events-cli#14`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The layer hears, thinks and speaks aloud through the robot | high | `docs/evidence/2026-08-02-t14-live-acceptance.md` — 134,400 B of audio out, export feed carrying all three block types |
| A rule firing becomes an input the layer reacts to (h3) | high | same file — `a behavior rule fired (pat-acknowledge)` → 6-round turn |
| The layer moves the robot through the sanctioned spool | high | runtime's own journal: `run_behavior` and `goto` `applied result={'ok': True…}` |
| The layer can teach the robot a new standing rule that outlives it (h26) | high | runtime journal `reload applied … react=3 inhibit=0`; `embody-pat-thanks` in the managed block of `rules.toml` |
| Enabling/disabling the layer changes nothing about the runtime (h1) | high | `docs/evidence/2026-08-02-runtime-equivalence.md` — 6 hunks, **0 deletions**, additive legs only |
| The tee costs the 50 Hz tick nothing, active or wedged (h10) | high | `docs/evidence/2026-08-02-t15-on-box-verification.md` — 0 overrun ticks in all three phases |
| Two realtime sessions coexist against the deployed gateway (h8) | high | same file — 1 session, 5 min, 0 reconnects, 9,599,488 B, runtime drops 0 |
| The tee wire is correct against a real producer | high | `docs/evidence/2026-08-02-live-tee-and-clip-on-the-robot.md` — 16,016 Hz measured vs 16,000 declared (0.1 %), amplitude within `[-1,1]` |
| Tool calls can never travel over the realtime socket (h13) | high | `tests/test_realtime_duplex.py` — AST scan over every constructible event + no `json` import + send-site opcode scan |
| The layer is ungated — it hears all speech (c4) | high | import-closure BFS with vacuity guard + subprocess `sys.modules` probe |
| The two realtime APIs converse out loud (h12, c21) | **unverified** | not achieved — one audio output device; #139 |
| Layer audio is *audible* in the room (h5, second half) | **unverified** | route and timing verified; audibility needs a human ear |
| `harmonics` reaches the speaker | **unverified** | never dispatched live |
| The worker describes a clip correctly (h9) | **unverified** | the layer does not consume the clip rider's output today |

## Remaining Work / Follow-up

- **`t14` completion** — needs a second audio output (USB speaker, Bluetooth, or a monitor with live HDMI audio; every HDMI profile currently reports `available: no`). Setup split between operator and agent, with pass/fail criteria, in **#139**.
- **h9 / clip consumption** — the clip rider produces files, but nothing in `reachy/embody/` reads them. Either wire the clip path into a turn or drop h9 explicitly; do not leave it implied.
- **`harmonics` live** — one dispatch during the #139 session closes the last action class.
- **#137** — the runtime has been ~5 % over its 20 ms tick budget continuously since at least 2026-07-30. Pre-existing, not from this arc (proven both ways), but real.
- **#138** — the camera pipeline can EOS and never recover; every camera sense goes silently dead until a manual restart. Seen once during this run.
- **#136** — the four sibling supervisors' PID-reuse guard can be satisfied by a directory name. The layer's own supervisor is already fixed.
- **#135** — a latent tee flake seen twice, assertion never captured. Filed honestly as "no diagnosis yet".
- **#134** — the runtime's hearing leg still reports a 404 (STT lane off) as a generic refusal; the layer's duplex client now names it.
- **#133**, **#132** — pre-existing gaps found while working here.
- **`events-cli#14`** — the bus client is publish-only, so cue intake runs on the feed-tail fallback. When a subscribe surface lands, the `--feed` FIFO workaround disappears.
