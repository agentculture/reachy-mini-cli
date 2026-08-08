# Changelog

All notable changes to this project will be documented in this file.

Format follows [Keep a Changelog](https://keepachangelog.com/). This project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [0.48.0] - 2026-08-08

### Added

- **The `wireless` noun** — find a Reachy Mini on the LAN in one command, remember which unit is yours across DHCP moves, pin it to a stable `/etc/hosts` alias, and open a shell on it. Verbs: `find` / `list` / `ssh` / `authorize` / `pin` / `unpin` / `forget` / `overview`, every one with `--json`, and every `find` result carries a ready-made `base_url`. Like `daemon` and `service` it uses no robot transport, so the whole noun works on the bare HTTP remote profile with neither the `[sdk]` nor the `[daemon]` extra installed.
- `reachy/discover/probe.py` — the identity probe: one read-only `GET /api/daemon/status`, stdlib `urllib`, never raising to the caller. It arms no motors and claims no media session, so discovery is safe to run beside a live `behavior engine run` — the single-SDK-owner model does not apply to it.
- `reachy/discover/sweep.py` — the bounded concurrent sweep. Interfaces are enumerated with `socket.if_nameindex` plus two `SIOCGIF*` ioctls (no new dependency, no subprocess that could hang); a prefix wider than `/24` is refused by construction before a single host is materialised, alongside interface-name, address-class and `/31`-`/32` filters, so the dev box's seven Docker `/16` bridges (~459 000 hosts) can never be expanded. One worker cap and one overall deadline, with `deadline_reached` reported honestly.
- `reachy/discover/registry.py` — the per-user unit registry at `state_dir()/units.json`, keyed on the daemon-reported `hardware_id` (never a name, an IP or a MAC). MAC is opportunistic enrichment via `ip neigh` and `None` is always fine. Missing/empty/truncated/invalid all degrade to 'start fresh'; writes go through a unique-per-call `tempfile.mkstemp` plus `os.replace`, so a human shell and a mesh agent running discovery at once can never leave a torn file.
- `reachy/discover/resolve.py` — fast path first (probe the remembered address, verify `hardware_id`, return without any sweep), escalate on a miss or an identity mismatch, then re-pin the record to the new address. Ambiguity is refused with a `CliError` naming every candidate, never guessed — both a Wireless and a co-resident Lite report `robot_name=reachy_mini`.
- `reachy/discover/hosts.py` — the recoverable `/etc/hosts` managed block (`# BEGIN reachy-mini-cli` / `# END reachy-mini-cli`), pinning `<ip> reachy-mini reachy-mini.local`. Pre-write backup of the exact bytes, atomic temp-file rename preserving mode and ownership, post-write re-read and verification that `localhost` still resolves, automatic restore on any failure, and an idempotent re-pin.
- `reachy/discover/ssh.py` — argv-only login plus the separate, explicitly-confirmed `ssh-copy-id` key install. Every invocation carries `-o HostKeyAlias=reachy-mini`, so stable host-key identity holds with no `/etc/hosts` pin and no privilege. `authorize` is structurally unreachable from the shell-opening path and demands an affirmative confirmation; the unit's factory-default password prompt is owned end to end by `ssh-copy-id`, so no typed secret passes through Python.
- `tests/test_discover_boundary.py` — an AST boundary test in the style of `test_zero_llm_boundary.py`: every module under `reachy/discover/` imports only the standard library, the first-party `reachy.*` edges out of the package are pinned by equality with a named reason each (plus a dead-entry companion), and the obvious accelerators (`zeroconf`, `netifaces`, `psutil`, `requests`/`httpx`) are named and refused. `pyproject.toml`'s base dependency list is unchanged.
- A sixth autouse guard in `tests/conftest.py` (`_no_live_lan_sweep`): `sweep.read_interfaces` is neutralised process-wide, so no test can enumerate the real box's NICs or sweep the live LAN — filed before it could do the damage the five guards beside it each cost once.
- Docs: a `Find the robot on the network` section in `docs/operating-reachy.md` (the measured before-state, the cold/warm timings, the sweep's edges, the sudo cost, and the trusted-network assumption stated plainly), a `wireless` noun-internals section and catalog row in `CLAUDE.md`, a README subsection, three new troubleshooting rows, and `REACHY_WIRELESS_UNIT` / `REACHY_WIRELESS_SSH_USER` in the environment table.

## [0.47.0] - 2026-08-03

### Added

- **The two-tempo architecture** (issue #155): the embodiment layer now runs a FOREGROUND interlocutor (the lobes realtime floor — the only thing that speaks) beside a BACKGROUND mind (the layer's worker lane) that follows the same conversation, reasons over longer horizons, operates the tools, and reaches the room ONLY through typed, inspectable events. The event is the background mind's output; the speech stays the foreground voice's.
- `reachy/embody/scope.py` — cognition scopes: a compact, attributed, expiring `cognition.scope` artifact (goal, relevant facts, suggested next step, priority, expiry in turns, speakable flag) that the background mind puts in front of the voice. Never raw model reasoning, structurally: `from_event` reads only the fields it knows. Parked latest-wins on `(kind, goal)`, never on free text, and context-only — there is no parameter that could make one a trigger.
- `reachy/embody/interjection.py` — the interjection policy and its typed event family. Six named checks cheapest-first, one decision point for every route (worker tool call, mesh peer, external system). Ships OFF, with an EMPTY source allow-list, enforced in the configuration object rather than in documentation; rate-bounded at 3 per 60 s per source. Also home to the `WantedToSay` artifact: the measured remainder of a reply a human cut off, attributed, expiring in turns, and structurally context-only.
- `reachy/embody/summary.py` — `SummaryProducer`, the ONE production writer of the layer's rolling summary (pinned by AST). A failed maintenance pass keeps the last summary, marks it stale in the foreground context and names a `summary-stale` drop — never a silent narrowing of the voice's memory.
- Nested conversation windows over ONE history (issue #154 decision c30): the worker replays 60 turns, the foreground voice the last 20 as a strict suffix, everything older covered by the summary. `senses_history_maxlen > history_maxlen` is refused fail-closed at construction.
- Chunked, cancellable playback in `reachy/speech/realtime_duplex.py`: a reply plays in chunks (a shorter first chunk, then ~1 s at a time), so 'stop talking' means 'do not send the next chunk'. `cancel_playback()` / `playback_progress()` / `spoken_split()`.
- The measured said/unsaid split: `estimate_spoken_prefix` cited (not imported) from lobes-cli `lobes/realtime/_floor.py`:323, fed MEASURED played-chunk offsets from the sink rather than the server's wire estimate — exact to the chunk boundary, estimated only inside it, with the still-playing chunk counted as NOT said.
- The client-side tail cut (deviation d1): a VAD-verified `speech_started` while the playback queue is non-empty cuts the mouth in the window after `response.done`, records the split, and keeps the remainder as a wanted-to-say artifact. Keys on verified speech only, never raw loudness.
- Per-utterance arming (issue #149) — attention now gates the VOICE, not only the mind. Behind a capability check that FAILS CLOSED (`session.created`'s `config.arming == "one_shot"`); against a gateway without it the layer degrades to arm-once with one named `one-shot-arming-unsupported` drop.
- `conversation.item.create` — the FOURTH outbound frame kind, widening the duplex send surface from three to four exactly once and on purpose (decision c28), behind its own capability check. With it the layer curates ONE canonical history and projects it: `floor_reseed()` (the voice's window as curated history turns plus the summary as one ephemeral context item, ordered BEFORE re-arming on every reconnect) and `floor_correction()`.
- Structured perception snapshots (summary, entities, confidence, capture time, frame reference) in a latest-wins slot per source that PERSISTS across turns until superseded or stale (30 s) — so 'what can you see?' between two camera polls is answerable. An unparseable answer degrades to a summary-only snapshot with a named drop.
- `--attention-window` / `REACHY_EMBODY_ATTENTION_WINDOW` (issue #150) — the attention window is an operator knob, reaching the child `embody start`/`restart` spawns; `0` still means name-only forever.
- `REACHY_EMBODY_VOICE_PROMPT` — connect-time voice conventions via the realtime session's `system_prompt` override, with no new frame kind. Blank or over-long is refused, never silently repaired.
- `doctor` gains a `model_pair` check: it names the realtime service's `OPENAI_MODEL` (the voice), `REACHY_EMBODY_SENSES_MODEL` (perception) and `REACHY_EMBODY_WORKER_MODEL` (the background mind, expected to differ) and warns only on genuine divergence between the first two.

### Changed

- **`agent embody`'s `speak` / `harmonics` are PROPOSALS, not playback.** The layer's own voice seams were deleted: the tool registry now holds no audio seam at all, imports no synthesis and no playback, and there is no code path — authorized or not — from a tool call to a speaker. `no-voice-seam` accordingly changed meaning: it now names a missing interjection ROUTE, refused before the policy is consulted.
- A kind-aware context park (issue #154): free-text perception lands in a latest-wins slot instead of the text-keyed cue park, so verbose sightings can no longer evict runtime facts. Exact-text coalescing is unchanged for the closed cue vocabulary.
- `reachy/runtime_cues.py` gains the two layer-authored line types (`interjection`, `wanted_to_say`) and their phrasing, so the robot cannot end up described two ways.
- Docs: CLAUDE.md, `docs/operating-reachy.md` (a new two-tempo section) and `docs/export-schema.md` describe the split, the interjection policy, the phase-1 limitation and the non-goals. Several statements that the arc made FALSE were corrected in place: 'exactly three frame kinds' (now four), 'the session is armed once and the server answers every committed utterance' (now the degraded path, and said so), and 'the layer's voice tools are real'.

### Fixed

- `_CueReader`'s docstring named `classified_cues_for_line`; the production path is `parse_runtime_line` + `classified_cues_for_runtime_event`.

### Known limitations

- **None of the two-tempo arc has been judged from the room yet.** It is proven by the offline suite and one probe against the deployed gateway (`docs/evidence/2026-08-02-t1-media-chunk-budget.md`); the live acceptance run is separate work.
- **Two pieces cannot pass yet and are recorded blocked, not rounded up.** Per-utterance arming and the `conversation.item.create` channel both wait on agentculture/lobes-cli#170. Against every gateway shipping today the layer degrades — the room is still answered aloud (`one-shot-arming-unsupported`) and the gateway's own conversation history still OVERSTATES what was heard after a client-side interruption (`items-unsupported`). `floor_correction()` APPENDS a correction where items exist; it does not rewrite, because the schema has no rewrite operation, so the overstated turn stays.
- **Interjection ships with no operator surface** — the policy, the event family and the enforced default-OFF state are real, but no CLI flag or environment variable turns it on in this release.
- **The per-chunk daemon `/media/play` round trip is still unmeasured** (it plays audio on the deployed robot), so the chunk sizes are defensible, injectable defaults rather than tuned numbers.

## [0.46.0] - 2026-08-02

### Added

- `reachy/embody/attention.py` — a wake-word attention gate for the embodiment layer (#148). Saying "reachy" opens the ear; a 45 s warm window, refreshed by both utterances heard and answers spoken, keeps it open; silence returns it to name-only. A rule fire still triggers while cold — attention gates the ear, not the robot's own reactions. The gate lives in the layer and uses the pure `name_match` matcher, so `realtime_duplex.py` stays ungated by construction and no LLM edge is added.
- A named, latched `camera-stream-ended` drop when the camera stops producing frames while the runtime reports itself healthy (#138). Keyed on frame STALENESS, not on the connection flag, which stays true across a dead GStreamer pipeline. Detect only — recovery stays out until an in-process EOS recovery is probed.
- `scripts/embody_bus_feed.py` lands in-repo with tests and operating-guide docs — the MQTT-to-FIFO bridge that is the layer's only bus intake while events-cli ships no subscribe surface. Its O_RDWR FIFO hold, source filter and events-only topic filter are now pinned rather than folklore.
- The clip -> `ask()` perception lane (#139 h9): the runtime's rolling clip reference reaches the senses model on a background poller, and the answer enters the turn as CONTEXT — never a trigger. `ask()` gains its first caller.

### Changed

- `EmbodyTurnEngine` no longer treats every runtime event as a turn trigger (#143). Three input classes: an utterance or an ALERT cue (a rule fire) triggers a turn; sense, intent, motion and rule suppressions accumulate in a bounded, coalescing context park the next turn drains. Alerts coalesce into a pending turn and are bounded by a minimum interval, so the flood cannot return through the front door. Replaying the measured 40 s window (187 cues, 0 rule fires) now produces 0 turns instead of 23.
- The camera frame is read at 10 Hz instead of once per 50 Hz tick (#137, #145). No consumer needs faster than 8 fps, and the per-tick read sustained the runtime ~5% over its 20 ms budget for as long as frames flowed — attributed live and verified gone.
- Both fat constructors group their bounds into frozen `Limits` / `RequestConfig` dataclasses (#141): `EmbodyTurnEngine` and `RealtimeDuplexSession` drop from 24 and 23 parameters to 12 each. Injectable seams stay explicit keyword-only parameters — the count was a symptom of the seam-injection design, not carelessness.
- 300 inert `# noqa:` markers naming lint codes this repo's flake8 cannot emit are gone (#142); the 20 load-bearing ones survive byte-identical and every word of explanatory prose is kept. Verified AST-identical across all 86 changed files.
- Executable model defaults name gateway ROLES, not served model ids (#132): forge's default moves from the dead `qwen3` alias to `cortex`, and the scene leg's pinned Gemma id to `senses`. A role name survives a model promotion; a served id does not.

### Fixed

- `agent attach`'s `speak` and `harmonics` tools enforce `MAX_SAY_CHARS`, refusing fail-closed rather than truncating (#133) — the one surface that bypassed a cap enforced everywhere else on the same action.
- A 404 handshake on the runtime's hearing leg is named `realtime-lane-unavailable` instead of a generic refusal (#134), pointing at `/v1/capabilities` `stt.feasible`. A configuration state now reads as one, not as a flaky gateway.
- `demo-mode stop` and `daemon stop` can no longer signal an unrelated process (#136). Both guards matched `/proc` cmdline SUBSTRINGS, so any process launched from a path containing the project name satisfied them — reproduced pre-fix delivering SIGTERM and SIGKILL to a real innocent bystander.
- `agent embody start` no longer silently discards `--feed` and `--media-profile` written before the subcommand (#147). Argparse applied the sub-parser's defaults over the parent's parsed values, so the layer spawned reading stdin — `/dev/null` for a detached process — connected its tee, armed a realtime session, and exited with every log line reading as success.
- The wedged-consumer audio-tee test no longer flakes (#135). The assertion was captured 24/24 under load: the test waited on a counter the writer bumps BEFORE emitting the line it then asserted on. Fixed at cause; the tee itself was never at fault and no timeout was widened.

## [0.45.0] - 2026-08-02

### Added

- `agent embody` — the embodiment layer: a detachable realtime harness that gives Reachy a voice and a cue-triggered mind running BESIDE the symbolic runtime, never inside it. Ears and mouth on one armed lobes /v1/realtime duplex session; cognition on the streaming chat-completions lane; actions confined to a closed five-tool set (goto, run_behavior, speak, harmonics, create_rule) reaching the robot only through the sanctioned intents spool and rules overlay.
- `agent embody start|stop|restart|status` — a background supervisor (pid + log under the state dir), one command each way. Rules the layer wrote survive a stop by design.
- Audio tee (`reachy/behavior/audio_tee.py`) — a THIRD consumer of the runtime's one per-tick mic chunk, fanned to a local unix socket so an external process can hear without opening a second SDK media session it could never win.
- Clip rider (`reachy/behavior/clip_rider.py`) — a rolling last-X-seconds camera ring encoded to one bounded file, published on the bus as a path reference only.
- `reachy/speech/realtime_duplex.py` — the duplex peer of the runtime's ears-only realtime client: audio in, server-VAD utterances and response audio out, over one socket.
- `[bench]` extra (`sounddevice`) for the layer's dev-box media profile. The deployed robot profile needs none of it.

### Changed

- `reachy/speech/llm.py` gains an additive, opt-in streamed-reasoning seam (the gateway sends `delta.reasoning`, not the documented `reasoning_content`). The request payload is byte-identical when unused.
- `FaceSenseDriver` gains `add_frame_sink()`, so camera frames reach new consumers by push and it remains the only caller of `media.frame()`.
- Docs: a new operating-guide chapter for the layer, plus CLAUDE.md noun catalog and internals.

### Fixed

- The clip rider wrote nothing on the robot: cv2.VideoWriter picks its container from the filename suffix, so the atomic temp name `clip.mp4.tmp` opened no muxer. Now `clip.tmp.mp4`; found on hardware, invisible to the suite (#137-adjacent).
- Runtime speech playback was documented as http-default when it has defaulted to sdk-via-held-client since 7ea6878 (closes #131).
- The test suite could reach the robot's actuators: TTS (:9000) and the daemon (:8000) are now refused suite-wide, after a boundary probe made the deployed robot speak out loud.
- Four intermittent test failures, three of which were genuine cross-thread ordering bugs in the tests rather than slow machines.
- A live-model integration test pinned a served model id, so a gateway promotion broke the suite (#132).

## [0.44.1] - 2026-07-24

### Fixed

- **The retained `reachy/state/*` tree no longer republishes at tick rate (issue #126).** The engine rewrites `state.json` every tick, so the bus mirror inherited that rate and resent the whole retained tree at 50 Hz — measured on the live robot at 520 messages per key per 10 s, of which `compose_hz` had exactly ONE distinct value and `active` had twenty. Retained messages are persisted by the broker and delivered to every subscriber, so those were disk writes and consumer wake-ups spent re-sending a value the topic already held. A key is now published only when its serialized value changed. Measured live after the fix: state traffic fell from ~3584 to 55 messages per 10 s (~65x), and `state/active` publishes exactly 20 times — equal to the distinct-payload count measured independently beforehand, so the gate emits precisely the real changes.
- Two deliberate exceptions keep that gate honest. `updated` is a per-tick timestamp, so it RIDES ALONG with a substantive change rather than triggering one — otherwise it alone would have republished at full tick rate and defeated the purpose; this also sharpens the retained topic's meaning to *when state last changed*, with liveness left to `state/online` + the Last Will. And every (re)connect republishes the whole tree unconditionally, as does the first publish of a session: a fresh broker session holds none of our retained values, so a gate that suppresses repeats must never suppress the original.

## [0.44.0] - 2026-07-24

### Added

- **The nervous-system bus is bound to a real client.** `events-cli>=0.9` shipped on 2026-07-24 and is now the **third base runtime dependency**, joining `numpy` and `harmonics-cli` — all pure wheels. `behavior engine run` now publishes the runtime feed to `reachy/events/{source}/{type}` and retained standing state to `reachy/state/{key}` on a live broker, verified end to end against the deployed Mosquitto.
- **`reachy/export/events_client.py`** — the ONE module that names the vendor. The shipped `events_cli.EventClient` does not match the surface `reachy/export/mqtt.py` declares (`is_connected` not `connected`, `close` not `disconnect`, and the Last Will is a constructor argument with no post-construction setter), so `EventsCliClient` adapts one to the other and defers building the vendor client until `connect()` — the only moment at which the will is known. A future vendor API change costs this one file. `behavior.py`'s `EVENTS_CLIENT_IMPORT` is an alias of its `VENDOR_IMPORT`, not a second copy, and a test pins the single-namer rule.
- `tests/test_export_events_client.py` — 26 tests covering the adapter, including the check a fake cannot make: the REAL vendor class driven through the real paho machinery against a dead port, asserting it satisfies `missing_client_members()`. A fake shaped like our own protocol agrees with us by construction, which is exactly why the vendor mismatch was invisible until the wheel shipped.

### Changed

- The base dependency set is now `numpy` + `harmonics-cli` + `events-cli`. `paho-mqtt` enters the resolved tree as events-cli's OWN base dep — the recorded 2026-07-24 decision working as intended, not a breach of it. This repo still names no MQTT library and imports none, now checked two ways: the dependency pin and a source scan for MQTT imports.
- CLAUDE.md and `docs/export-schema.md` updated from "the wheel does not exist yet" to the shipped binding, including the warning that a wrong binding degrades **quietly** (a named `client-incompatible` drop, not a crash), so only a live-broker check can prove it right.

### Fixed

- **The test suite no longer publishes onto a real event broker.** The moment events-cli became a base dependency, every composition test built a live client against `REACHY_MQTT_URL` (default `localhost:1883`) — a full run wrote the suite's synthetic ticks onto the deployed robot's own `reachy/events/**` tree and left RETAINED `reachy/state/*` values behind, which outlive the test process and are what a late subscriber reads on connect. `tests/conftest.py`'s new `_no_live_event_broker` autouse guard pins the broker at a dead loopback, alongside the existing daemon-media and realtime-gateway guards.

## [0.43.0] - 2026-07-22

### Added

- `reachy/speech/realtime_wire.py` — hand-rolled stdlib RFC 6455 framing plus the OpenAI-shaped base64 `input_audio_buffer.append` codec, cited from lobes-cli `scripts/realtime-smoke.py` (no new dependency).
- `reachy/speech/realtime.py` — `RealtimeTranscriber`, a worker-thread WebSocket session against the lobes `/v1/realtime` endpoint: streams mic audio as base64 append events, consumes server-side VAD boundaries and transcripts, and reconnects with bounded backoff. Never raises into its caller; every failure is a named `senselog` drop.
- `tests/fake_realtime_server.py` — an in-process, loopback-socket fake `/v1/realtime` server with scripted happy and failure scenarios, so the whole hearing path is testable offline.
- `REACHY_REALTIME_URL` / `REACHY_REALTIME_API_KEY` — explicit endpoint and key for the hearing session, falling back to `REACHY_OPENAI_URL_BASE` / `REACHY_OPENAI_API_KEY`.

### Changed

- Utterance endpointing moved server-side (issues #115, #111): `TranscriptSenseDriver` no longer decides when an utterance starts and ends from a local energy threshold — it streams audio to the realtime session and consumes endpointed transcripts. Admission is unchanged: the engagement gate still judges every utterance exactly as before.
- The runtime streams mic audio as base64 `input_audio_buffer.append` JSON text frames; no binary audio frames are sent anywhere (the coordinated lobes#151 wire break).
- `behavior engine run` composes and starts one `RealtimeTranscriber` at composition time (never on the tick thread), carries the mic-reported sample rate into the session, feeds it from the existing `_AudioTap` fan-out, and closes it with the rest of the runtime resources.
- Zero-LLM boundary: `reachy.speech.stt` left `_BEHAVIOR_SPEECH_ALLOW` (nothing under `reachy/behavior/` imports it now) and `reachy.speech.realtime` took its place; the engagement classifier remains the one LLM edge.
- `REACHY_STT_URL` no longer affects the runtime's hearing — it is now only `sleep`'s wake-word backend.

### Fixed

- A normal speaking voice across the room no longer goes unheard (#111): the local start threshold that landed at 0.102 against a drifted background no longer decides capture at all.
- `RealtimeTranscriber._connect` is fully guarded — an unexpected fault costs one reconnect attempt rather than killing the session worker permanently while still reporting a latched session-down.

## [0.42.0] - 2026-07-21

### Added

- The AI-first flow is retired: the robot's presence is now the symbolic `behavior` runtime — rules and configuration, not a model in the loop. Five capabilities were PORTED into it and live-verified on hardware before anything was deleted: voice (a background-worker speech actuator), sound-orienting, hearing words, mic loudness, and face/frame availability.
- A machine-checked zero-LLM boundary (`tests/test_zero_llm_boundary.py`): an AST import-boundary suite proves the engine, rule engine, rules, intents, arbitration, goto lane and pat sense reach nothing in the speech, vision or forge stacks. The runtime owns a voice and ears, so speech synthesis/playback/transcription are permitted by an explicit allow-list where each entry states why it is not a language model.
- `reachy/behavior/rms_background.py` — a rolling-median estimate of the room's own mic floor, so 'loud' is a comparison against the current room rather than a fixed number.
- `reachy/behavior/audio_pump.py` — a background worker owning all mic acquisition, draining the SDK appsink at production pace so the tick thread does zero audio I/O.
- `reachy/robot/audio_shape.py` — explicit mic-array channel selection, shape-agnostic over (N,), (N,1) and (N,C), replacing a blind reshape.
- `tests/test_export_schema_doc.py` — the export wire contract is now pinned against the live parser: a retired noun left in the schema doc fails CI instead of quietly becoming a lie.

### Changed

- REMOVED the `think` noun, the `listen --live` composition root with its folded hooks, the `listen` noun, and the cognition/marker engines. `reachy/motion/listen.py`'s `ListenProducer` is KEPT — the offline lane and the runtime's orient donor path both use it.
- REMOVED the `live` presence mode. `service` now offers exactly `demo` and `runtime`; a box still carrying `reachy-live.service` is purged by the next `service enable`/`install`/`uninstall` (reported as `retired_removed`). The purge is destructive — back up `~/.config/systemd/user/reachy-*.service*` first.
- Sound reaction is a graded two-tier ladder and is ANTENNA-ONLY by default: tier 1 leans the antennas toward a sound standing above the room; tier 2 (head/body turn) requires sustained or loud-relative-to-background sound. The turn path remains implemented and reachable via an operator overlay or `REACHY_ORIENT_*`. A head that keeps turning is a head that can never feel a pat.
- rms admission is now RELATIVE (ratio over a rolling background) rather than an absolute 0.02 floor. Measured on the deployed robot, the mic background drifts ~25x across conditions the same robot lives in within 24 h, so no absolute value is correct in both a daytime and a night room.
- Cognition is demoted, not deleted (issue #70): `agent attach` remains the external, optional AI surface and keeps its own cognition export feed.
- The export feed's `message` block means what the agent PROPOSED saying — `agent attach` composes its speech tools publish-only. Audible speech comes from a rule's `say` and carries no block of its own.

### Fixed

- The robot could not hear a spoken sentence (#108). The capture path appended only chunks that individually cleared the rms threshold and discarded the rest, so STT received the loud frames butt-spliced together with every unvoiced consonant and inter-word gap excised. Capture now submits one contiguous clip per utterance. Verified live: the same phrase transcribed correctly where the spliced path returned 'Return.'
- The robot answered conversation it was never part of (#104, #105). `is_name_match` false-triggered on common words (`really`, `reality`, `ready`, `reason`, `record`, `room`, `root`, `rachel`) — an STT mishearing preserves the consonant skeleton, a look-alike word does not, so a phonetic guard now filters the fuzzy match. And the engagement history was a one-way ratchet: only accepted utterances fed the classifier's context, so one false accept made the next more likely, with no decay. A warm window only a name can open replaces it.
- The runtime heard its own actuators and sustained an orient loop (#95), and read seconds-stale audio from a standing appsink backlog (#100).
- The behavior engine achieved 23 Hz rather than 50 (#97, partial): a fixed-period sleep ignored work time. Now absolute-deadline scheduled. The remaining tick-work overrun is still open.
- Every runtime journal line was doubled (#96), and rule drops logged per tick rather than per episode (#99).
- Building the CLI parser loaded cognition modules, so `say run`, `daemon status` and even `--help` imported an LLM client — and `say`'s dumb-pipe boundary test failed in a fresh pytest worker, surviving `-n auto` only by import order. Both module-scope imports are now lazy.
- The daemon media subsystem is acquired before the media client is constructed and released on close (#94), so the ported senses are no longer permanently dormant.

## [0.41.0] - 2026-07-20

### Changed

- `pet-reaction` now SEEKS the touching hand instead of yielding from it. `PatState.yaw_deg` is the signed actual-minus-commanded deviation, so a hand resting on the robot's right pushes the head left; the planner multiplied that by a positive gain and continued WITH the shove. A new `SIDE_SEEK_SIGN = -1.0` presses the head back along the axis it was pushed, so it meets the palm and sustains contact.
- Antenna contraction amplitudes raised against the 20 deg `ANTENNA_LIMIT_DEG` ceiling: `RECEPTIVE_ANTENNAS` 12 -> 15 deg, `CONTENTMENT_ANTENNAS` 17 -> 20 deg. Hardware check on the deployed robot confirmed the antisymmetric pairs do draw the antennas together (`reachy/motion/listen.py`'s mirrored-sign convention is the true one), but the gesture read as understated at 12 deg.
- Side-pat lean retuned from measured hardware data so the turn is legible: `SIDE_HEAD_GAIN` 2.5 -> 6.0, `SIDE_BODY_GAIN` 1.0 -> 2.5, `SIDE_PITCH_DEG` 2.5 -> 4.0. Nine live side-pats captured through the export feed gave `|yaw_deg|` of 1.30-2.08 deg (median 1.70) — a structural consequence of detecting right AT the 1.2 deg press threshold. At the old gain that produced only a 3.3-5.2 deg turn, comparable to the pitch and swamped by `feel-alive`'s swing, which read on the robot as "moving straight forward" rather than turning toward the hand. The median pat now turns ~10.2 deg. These gains are COUPLED to the press threshold through that arithmetic and must be re-derived if it moves.
- `HEAD_ROTATION_STEP_DEG` 0.5 -> 1.0 and `BODY_YAW_STEP_DEG` 0.25 -> 0.5, raised in step with the gain for a structural reason rather than for speed. The entry slew is part of the reaction's blind window (slew + gate re-arm), which `RELEASE_AFTER_S` must outlast or a sustained pet dies mid-gesture and never ladders `receptive` -> `contentment` (the t12 sustain bug). Raising the gain alone doubled the slew to 0.48 s and the offline labelled trace stopped reaching `contentment` at all; doubling the step restores the original timing at the new amplitude.
- Locked the swing-era pat tuning in as SHIPPED defaults, so a fresh box feels a pat with no local config: `DEFAULT_STILL_EPS` 0.01 -> 0.035, `DEFAULT_STILL_HOLD_S` 0.5 -> 1.0, `DEFAULT_PRESS_THRESHOLD` 0.5 -> 1.2, `RELEASE_AFTER_S` 1.0 -> 2.5. `DEFAULT_HP_TAU` deliberately stays 0.8. These four are ONE operating point and must move together — the sensitive 0.5 press belongs with a tight gate that only opens at a dead stop, and the loose gate belongs with the blunter press. Previously they lived only in a box-local systemd drop-in, which is what let an upgrade activate them silently.
- Known cost of that default change, stated plainly: on a head held genuinely still the measured petting p90 is 0.85-1.90 deg, so the 1.2 press threshold now misses the gentlest pats in that regime. The behavior engine runs the swinging `feel-alive` base layer, where the relevant residual is 0.70 deg and 1.2 is the right discriminator — but a caller running the driver against a static commanded pose should inject the sensitive detector rather than take the defaults.

### Fixed

- A reaction no longer aborts mid-gesture as `sensing_lost` during its OWN commanded motion (found in review on #90). `PetReaction` treated any `availability != "available"` past `SENSE_LOSS_GRACE_S` (1.0 s) as a sensing failure, but the reaction is necessarily blind for its entry slew plus the stillness re-arm — 1.40 s under shipped v0.41.0 defaults, where the binding axis is the ANTENNA slew (0.40 s at 20 deg) rather than the head's 0.24 s. `blocked` (this reaction's own motion) now carries a separate `BLOCKED_GRACE_S`, DERIVED from `DEFAULT_STILL_HOLD_S` plus the worst-case slew across every clamp and rate limit, so retuning any of them keeps it correct. `unavailable` (a genuinely dead pose reader) keeps the short 1.0 s budget — the fix deliberately does not blanket-extend every loss path.
- A sustained pet no longer drops back to idle motion under a still-moving hand. Admitting a reaction moves the head, which closes the stillness gate, which landed in `_begin_gap` and `_end_interaction`'d the very contact the reaction was admitted for — so a continuous scratch was chopped into repeated `level1`s that never laddered `receptive` -> `contentment`. A gap now SUSPENDS a live interaction instead of ending it: contact and the phase clock survive while `availability` carries the uncertainty.
- The suspend is deliberately narrow, because this is the machinery guarding the phantom-pat class. `detector.clear_interaction()` still runs on gap entry, so press edges either side of a gap can never pair into a level2 that was never physically sustained. Suspension is bounded — on recovery the blind stretch is charged to the release budget from when the gap opened, so a long blackout cannot masquerade as a hand that never left (the reaction's own ~1.24 s window sits well inside the 2.5 s budget). The `enough` phase still ends rather than suspends, so the lifecycle cooldown is never dropped. Reactions are admitted on the one-tick `pat_event` latch and never on `contact`, so preserving contact cannot manufacture an event or self-retrigger.
- Diagnosed and fixed the pat-sense tuning trap that silenced the reaction on the deployed box, bisected to a single knob on hardware. The `REACHY_PAT_*` env vars were inert until v0.40.0 added the `_pat_float_env` surface that reads them, so a drop-in authored earlier only took effect at upgrade time. The culprit was `hp_tau` 0.8 -> 0.08: `tau` is a high-pass time constant, and at 0.08 s only fast transients survive, while a pet is a SUSTAINED push lasting ~0.5-2 s. The stillness gate opened normally; the detector simply never saw the press. Removing that one override — every other value unchanged — restored detection immediately (`Pat level1! type=side_pat` -> `pat-acknowledge fired run=pet-reaction`, reproduced twice), and the operator confirmed the robot visibly enjoying a scratch.
- Recorded the counter-lesson for the deployed drop-in: reverting the whole block to shipped defaults is WRONG and is worse than the bug. v0.40.0's swinging idle means the shipped `still_eps=0.01` opens the stillness gate 0.0% of the time (measured in af87b1d), so shipped defaults leave the robot unable to feel anything at all. The gate loosening (`still_eps=0.035` + `still_hold_s=1.0`) is load-bearing under the swing and must be kept; tune `press_deg` against the measured residual instead.

## [0.40.0] - 2026-07-20

### Added

- **Live tuning surface for the pat sense** — `REACHY_PAT_HP_TAU` (the frequency
  discriminator: the robot's own motion is slow, a hand's presses are fast and
  jagged), `REACHY_PAT_PRESS_DEG` / `REACHY_PAT_YAW_PRESS_DEG`, and
  `REACHY_PAT_RELEASE_AFTER_S`, alongside t2's `REACHY_PAT_STILL_HOLD_S` /
  `REACHY_PAT_STILL_EPS`. Every override is additive: absent env leaves the
  shipped defaults byte-identical, and none of them occupies a keyword a caller
  may inject itself.
- **Converged spec and plan for the no-freeze work** (`docs/specs/` +
  `docs/plans/`), with two recorded deviations (`d1`, `d2`) capturing where
  hardware contradicted the plan.

### Changed

- **The robot no longer freezes to be pettable** (#82). `feel-alive`'s
  dead-still four-second hold is gone; idle motion is continuous again, at the
  shipped amplitude and speed. The hold existed only so the pat sense's
  stillness gate could open inside it, and on hardware it read as the robot
  stopping.
- **Idle motion swings** (`reachy/behavior/feel_alive.py` `swing_time`). One
  global time-warp makes the whole pose sweep fast through the middle of its arc
  and decelerate, pause and accelerate out of each extreme, like a swing.
  Because the warp is applied to the clock every axis shares, amplitudes,
  periods and total travel are all unchanged -- 330 deg per 40 s either way --
  while the longest sustained-slow window goes from 0.12 s to 3.42 s, present
  about 18% of the time. That window is what makes contact sensing possible
  without stopping.
- **Pat sensing is gated on sustained slowness rather than exact stillness.** No
  new gate was needed: `still_eps` was always a per-tick velocity threshold,
  tuned at 0.01 deg/tick for a dead freeze. Retuned to the swing's slow window
  it opens 10-15% of the time. Measured there, an untouched head's conditioned
  residual is 0.70 deg against a petted 2.52 deg -- a 3.6x separation a
  uniformly moving head never offers.

### Fixed

- **The excited-motion probe can arm again.** It waited for the command vector
  to hold exactly constant across `SETTLED_EDGE_S`, which continuous idle motion
  never provides -- so it never armed and read the actual pose zero times. It
  now falls back to arming on elapsed time after `ARM_FALLBACK_S` and closes a
  fixed `CONTINUOUS_CAPTURE_S` window cleanly instead of running to a refusal. A
  genuine settled edge still takes precedence when one exists.
- **Phantom pat reactions during continuous motion.** Sensing with the gate
  simply disabled reopened the #66 self-triggering loop -- a reaction's own
  motion read as a pat about two seconds later and fired another, indefinitely
  (measured 15-30 fires per 45-50 s hands-off, and raising press thresholds made
  it worse). Gating on the swing's slow window instead gives zero detections in
  75 s hands-off, while a real scratch is detected and classified correctly, and
  reactions stop when the hand leaves.

## [0.39.0] - 2026-07-19

### Added

- **t12 live-acceptance findings for the expressive pat reaction**
  (`docs/deliveries/2026-07-19-expressive-pat-reaction-82-t12-findings.md`,
  #82). The hardware gate that closes t1-t11 was run on 0.38.0 and is recorded
  as **not met**, with the measurements rather than a tuned-away result:
  detection is sound (6/6 correctly typed `side_pat`, 4 clean rule fires, 188.5
  s ghost-free) and the reaction is pleasant, but contact never sustains past
  `receptive` (4.00 s needed for contentment, 0.82 s observed) and the side
  signal (0.55-0.96 deg) straddles the 0.75 deg direction deadband with the sign
  flipping mid-contact in 4 of 6 episodes.
- **Converged spec and plan for #82** under `docs/specs/` and `docs/plans/`, the
  devague frame behind the shipped t1-t11 work.
- **Codex skill adapters** under `.agents/skills/` — thin, Codex-native
  frontmatter over the canonical `.claude/skills/<name>/SKILL.md`, reusing the
  canonical resolver scripts rather than copying them so the two runtimes cannot
  drift.

### Changed

- `docs/skill-sources.md` documents the `.agents/` adapter layer and records the
  devague-origin skills (`scope`, `challenge`, `deviate`, `summarize-delivery`)
  plus `communicate` in the provenance table.

## [0.38.0] - 2026-07-19

### Added

- **Sustained dog-like pet reaction** (#70). A pat is now an *interaction* with
  a lifetime rather than a one-shot twitch: `reachy/behavior/pat_state.py`
  carries a persistent `PatState` ladder (`receptive` -> `contentment` ->
  `warning` -> `enough` -> `released` -> `cooldown`) that
  `reachy/behavior/pet_reaction.py` renders as a graded, bounded response —
  leaning in while contact is welcome, easing off once it has had enough.
- **Pettable feel-alive cadence** — the base presence layer now yields a still,
  invitational posture the pat sense can actually read, so petting and idle
  wander stop fighting each other.
- **Passive CLI-owned excited motion probe**
  (`reachy/behavior/excited_motion_probe.py`). `reachy behavior engine run
  --probe-mode {held,unheld} --probe-output PATH` records a bounded,
  observation-only JSONL capture of one commanded-motion episode (arm -> onset
  -> settled edge) for hardware calibration. It is strictly read-only:
  `ProbeCommandGuard` refuses to let a probe run become a second command owner,
  and the run is refused outright when an already-running CLI engine has a fresh
  heartbeat.
- **Signed pat evidence** + an event-stable pat state contract, so downstream
  consumers see a stable, directional reading instead of re-deriving it from raw
  detector output.
- **Explicit behavior completion seam** — a bounded behavior now reports its own
  completion rather than being inferred as done.

### Changed

- **Cognitive-complexity refactor of four hot paths**, behavior-preserving and
  covered by the existing suite: `ProbeDriver.__call__` is now a thin stage
  spine (`_accept_tick` / `_begin_observation` / `_advance_onset` / `_try_arm` /
  `_phase_for` / `_emit_sample`); `PatSenseDriver._process` reads as the gate
  order it enforces, with per-tick conditioning re-seeding made idempotent by
  `_reseed_once` instead of threading a `reseeded` flag through every branch;
  `_update_pat_state` splits into adopt-press / release-budget / phase-ladder
  stages; and `cmd_engine_run` extracts probe validation, capture-stream setup,
  and the live banner.

### Fixed

- **Pat interaction state no longer goes stale across a sensing gap** (#70).
  Opening a gap (ownership change, observation-clock jump, blocked stillness
  gate, or an unavailable reader) clears the interaction exactly once, and
  cooldown is now *preserved* across that gap instead of being silently
  forgotten — so a pat that has run its course cannot immediately re-trigger.
- **Release is anchored to the last press**, not to whichever later quiet sample
  happened to be observed first, so the release budget measures real quiet
  rather than observation luck.
- **Probe mutual exclusion no longer has a rounding-sized hole.**
  `Engine.state()` writes `round(now, 3)`, which can round *up*, so a live
  engine's heartbeat could read back marginally ahead of the probe's own
  `monotonic()`; the old `0.0 <= age` test then reported "not fresh" and
  admitted a second command owner. A near-future heartbeat now fails **closed**,
  bounded by `_PROBE_HEARTBEAT_SKEW_S` so a `state.json` left over from before a
  monotonic-clock reset (a reboot) still reads as stale instead of locking
  probes out forever.
- **Feel-alive per-tick cost no longer grows with uptime.**
  `_FeelAlive._cycle_at()` scanned its cadence schedule from index 0 every tick,
  so cost climbed continuously on a loop that ticks at 50 Hz for days. Cycle
  ends are contiguous and strictly increasing, so the scan is now an exact
  `bisect_right` — identical results, O(log n).
- **A behavior breaking its return contract can no longer kill the tick.** The
  completion pre-pass in `Engine.compose_tick()` now reads `done` with the same
  tolerance `arbitrate()` already applies, so a malformed contribution is an
  abstention rather than an `AttributeError` inside a boot-persistent presence
  loop.
- **Every probe-output open failure is a structured error.** Only
  `FileExistsError` was converted before, so a missing parent directory or an
  unwritable path escaped the `CliError` contract; other `OSError`s now report
  the concrete errno type with a remediation.

## [0.37.0] - 2026-07-18

### Changed

- **The pat sense works, and ships enabled** (#80). Hands-on calibration on the
  real robot settled the physics that kept it dormant in 0.36.0 (#79): the plant
  is quiet only while it is NOT tracking a moving target. Four recordings
  (still/wandering x untouched/petted, all six DOF) measured **12-20x**
  pat-vs-noise separation with the head held still versus **0.7-2.0x** while it
  wanders — on every axis, including the ones `feel-alive` never commands. The
  residual is servo hunting, not lag: uncorrelated with commanded velocity, and a
  fitted 40-tap FIR plant model removes only 1.1x of it.
- **Stillness gate** (`reachy/behavior/pat_sense.py`): detection runs only after
  the commanded pose has been constant for 0.5 s, so the ghost class is closed
  structurally rather than by threshold tuning. A moving robot reports no pats
  instead of guessing; a still one feels them reliably.
- Detection thresholds returned to sensitive, data-tuned values (press 0.5 deg,
  release 0.2 deg) from the blind 2.0/0.8 that shipped dormant.
- `REACHY_PAT_SENSE` now defaults ON; set it to `0` to opt out.

### Added

- `tests/test_behavior_pat_sense_hardware.py` + `tests/data/pat_*.csv` — the four
  robot recordings as regression fixtures, pinning both the no-ghost guarantee and
  the detection guarantee against measured plant behaviour.

## [0.36.0] - 2026-07-18

### Added

- Proprioceptive pat sense in the runtime process (issue #75): a held, media-free SDK pose reader
  (reachy/robot/state_reader.py HeldStateReader) plus an ownership-gated PatSenseDriver
  (reachy/behavior/pat_sense.py) feed Sense.pat_event on every behavior engine run tick, no listen
  --live needed - the deployed pat-acknowledge rule fires with zero config change
- reachy/behavior/pose_feed.py LastPoseHolder: a TickContext.pose seam so seam riders (the pat
  driver, the goto lane's start-pose provider) read the engine's own composed pose instead of
  guessing from ownership + contributions
- Live goto path (issue #77): reachy/behavior/goto_intent.py's fail-closed goto command kind
  registers into the intent driver's KindRegistry; GotoLane composes into behavior engine run's tick
  bus with live start-pose continuity; new behavior goto CLI verb submits through the same intents
  spool a live tool-use agent uses
- Bounded-lifetime invariant enforced on both admission surfaces (issue #76): react rules gain a
  validated duration_s with fail-closed refusal of a looping-default target
  (nod/shake/speak/antenna-sway/feel-alive) carrying no bound; run_behavior intents refuse any
  resulting unbounded lifetime the same way (declare_goal's standing intent stays intentionally
  exempt)
- docs/design/runtime-feed-export.md: design note for exporting the runtime feed to the reTerminal
  panel post-#70 flip (issue #78) - design only, implementation deferred to its own think/challenge
  pass

### Changed

- docs/operating-reachy.md: single-SDK-owner model notes the held state-read client never touches
  the media session (state reads only, media session stays free); the rules.toml walkthrough's live-
  predicates status corrected now that pat is live in the standalone runtime process; new goto verb
  walkthrough (flags, submit-confirm/degrade contract, example); Status & follow-ups updated
- CLAUDE.md: enriched behavior noun catalog row + new noun-internals section documenting the pat
  sense provider chain, the bounded-lifetime invariant on both surfaces, and the goto path
- docs/export-schema.md: the runtime feed's motion block Producer status is now live for gotos (wire
  format unchanged)

## [0.35.0] - 2026-07-18

### Added

- Symbolic runtime (issue #70): declarative rules.toml (react/inhibit/modes with per-rule cooldown_s + hysteresis, data-only no-exec validation) evaluated on the behavior engine tick via a single injected event seam (TickContext/TickBus)
- behavior rules / rules check / rules overview verbs, behavior reload (live between-ticks hot-swap with last-good semantics), and an additively extended behavior status (rules health + live agent intents)
- Boot-resilient rules load: a malformed rules.toml degrades to base presence with a [SENSE stage=rule] rejection naming every reason — never a Restart=on-failure crash loop
- Goto lane: one-shot minjerk gotos as time-bounded stoppable contributions under per-channel arbitration, with a MotionQueue-shaped adapter and pinned no-resume preemption semantics
- Intent system: run_behavior / declare_goal / set_mode / set_inhibition as atomic spool commands sustained tick-over-tick by an IntentDriver on the engine bus, with agent-facing JSON-schema tools
- Runtime events JSONL feed (behavior engine run --export -): sense/rule/intent/motion blocks — a separate wire contract from the cognition feed (decision c27), documented in docs/export-schema.md
- agent noun: an external attach client that reads the runtime feed, acts through the intent spool, and publishes its own thinking/message/emotion feed (decisions c11/c27) — publish-only speech tools, no second SDK owner
- reachy-runtime.service boot unit (AI-agnostic ExecStart) + three-way single-presence exclusion across demo|live|runtime
- Offline CI lane (pytest -m offline): boot/breathe/orient/pat/sleep-wake/rules proven with every service endpoint unreachable under a socket guard, plus a dep-freeze check
- Tick-budget observability: TickMetrics seam wrapper counting overruns with a [SENSE ... event=overrun] line (0 overruns across 8500+ real 50 Hz ticks in live testing)
- Live perception + logging in behavior engine run: a DoaPoller feeds doa/speech rules from the daemon route, and install_logging makes [SENSE stage=rule] lines visible
- Operating guide chapter: the symbolic runtime — three client walkthroughs (human/script/agent), the two-feed contract, and the zero-token verification recipe
- Battery-free state surface (joints + pose via the SDK seam) with a repo-wide no-battery guard test (challenge finding c21: SDK 1.9 has no battery API)

### Changed

- Sense snapshot extended with rms/pat_event/face/frame_available fields + non-consuming provider seams (peek semantics)
- behavior/control.py spool generalized to namespaced spools with an extensible command-kind registry

## [0.34.1] - 2026-07-17

### Fixed

- forge validator: fail-closed call-target check — calls through subscripts, lambdas, or chained attributes on a non-allowed base now reject instead of silently passing; `__builtins__` added to the forbidden-names list (Qodo finding)
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
