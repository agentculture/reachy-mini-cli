# Probe: can two lobes `/v1/realtime` sessions coexist?

Task **t1** of the `embodiment-layer` plan (`docs/plans/2026-08-01-embodiment-layer.md`).
Investigation only — no library code was written or modified. This is the
before-the-build check the plan's own risk list names: *"Concurrent
`/v1/realtime` sessions unvalidated upstream (`TTS_VOICE_CONCURRENCY`=1
serialises two conversational sessions — only one here is conversational); t1
probes before the build leans on it."*

## What was run

`scripts/probe_concurrent_realtime.py` (this worktree, `embody/t1` branch —
kept because it is genuinely reusable for t15's "dual sessions concurrent"
acceptance and t14's bench acceptance, not thrown away). Full source is in
that file; the design in brief:

- **Session A — transcription-only**, mimicking the runtime's existing
  hearing session *exactly*: it is the literal production class,
  `reachy.speech.realtime.RealtimeTranscriber`, imported and used unmodified
  (no re-derivation). By construction it never sends `response.create` and
  silently ignores any `response.*` event that arrives, counting it in
  `ignored_events`.
- **Session B — armed conversational**, mimicking the embodiment layer's
  future duplex client. No production client for this exists yet (that is
  t3/t9's job) — this is a scratch, hand-rolled client built from the same
  pure-function wire primitives (`reachy.speech.realtime_wire`) that both the
  production client and `tests/fake_realtime_server.py` already use
  (`build_handshake_request`, `parse_response_head`, `verify_accept_key`,
  `build_frame`, `read_frame`, `build_append_event`, `decode_event`). ~2 s
  after connecting it sends one `{"type": "response.create"}` text frame
  (confirmed against `lobes-cli`'s `_conversation.py`: arming is session-level
  and idempotent, no other fields are read from the payload), then logs every
  `response.*` event verbatim.
- **Audio**: real recorded speech, not a synthesized tone (a tone is a weak
  VAD probe per the task brief). Source:
  `/usr/share/sounds/speech-dispatcher/dummy-message.wav` — a 29.43 s, 16 kHz
  mono PCM16 clip shipped by the OS `speech-dispatcher` package (a real
  narrated "dummy output module" message, not silence or a beep). Session A
  is fed the clip **forwards**; Session B is fed the identical samples
  **time-reversed** (`numpy` slice-reverse, no new dependency). Reversing
  preserves the amplitude envelope (so both sessions see equivalent VAD
  onset/offset timing) while making the two streams trivially distinguishable
  in *content* — a real cross-talk bug (session A's socket receiving session
  B's data or vice versa) would show up as a forward-sounding transcript
  turning up on the reversed-audio session, or vice versa. Both streams are
  paced in 20 ms chunks at wall-clock real-time rate.
- Every inbound/outbound event on both sessions is appended to one shared,
  wall-clock-timestamped log (`t=` seconds since the probe's t0). Session A's
  events are captured via a `logging.Handler` attached to the `reachy.sense`
  and `reachy.speech.realtime` loggers (so the exact same `[SENSE …]` lines
  and any "ignoring ears-only violation" debug line the production client
  would emit are captured verbatim); Session B's events are logged directly
  from the hand-rolled client's frame dispatcher.

Invocation used for the archived run:

```bash
uv run python scripts/probe_concurrent_realtime.py --duration 80 --out-dir docs/evidence
```

`--duration 80` feeds both sessions for 80 s, then the harness drains for 5
more seconds waiting for any in-flight response to finish before tearing
both sessions down — **85.0 s** of continuous concurrent-session wall time,
comfortably past the 60 s acceptance bar.

Gateway: `http://localhost:8001` (resolved via
`reachy.speech.llm.LlmConfig`-style precedence,
`reachy.speech.realtime.resolve_realtime_base_url()` /
`resolve_realtime_api_key()`, using `REACHY_OPENAI_URL_BASE` /
`REACHY_OPENAI_API_KEY` already present in the environment — no repo files
touched to configure this). `GET /health` reported
`{"status": "ok", "service": "model-gear-gateway", "version": "0.54.8"}`.
`GET /capabilities` reported the `stt` role (`nvidia/parakeet-tdt-0.6b-v2`,
`realtime_vad_session` in its `responsibilities`) as `ready=true,
loaded=true` — the role `/v1/realtime` needs.

Raw archived output:

- `docs/evidence/2026-08-01-probe-concurrent-realtime-sessions.events.jsonl`
  — every event from both sessions, one JSON object per line, `t`-ordered.
- `docs/evidence/2026-08-01-probe-concurrent-realtime-sessions.summary.json`
  — the run's final counters and utterance texts.

## Raw event sequences observed

### Session A (transcription-only, forward audio) — 4 events of substance

```text
t=0.006  session.created            rate=16000 vad=server_vad
t=0.252  speech started (server vad)
t=30.025 speech stopped (server vad) reason=max_turn
t=30.350 utterance chars=467   "This is the dummy output module. It seems
                                 your speech dispatcher is working, but none
                                 of its output modules is, except me. ..."
t=30.371 speech started (server vad)
t=60.065 speech stopped (server vad) reason=max_turn
t=60.306 utterance chars=468   "Dummy output module. It seems your speech
                                 dispatcher is working, ..."
t=80.001 (drain) both accrued utterances flushed via take_utterance()
```

`client_a.ignored_events == 0` for the entire run — **zero** `response.*` (or
any other unhandled) events ever reached the ears-only session. This is the
direct proof against server-side cross-talk: if the gateway had ever
delivered session B's response traffic onto session A's socket, the
production client's own `_dispatch_event` would have logged
`"ignoring unexpected opcode"`/`"ignoring ears-only violation from the
server: response.*"` at DEBUG and bumped this counter — the log capture would
have shown it (it captures DEBUG on `reachy.speech.realtime`), and it did
not.

### Session B (armed conversational, reversed audio) — full response lifecycle, twice

```text
t=0.006   connected  url=ws://localhost:8001/v1/realtime?input_sample_rate=16000
t=0.006   session.created  session_id=sess_8ed776d4d240432e9196a426
t=0.252   input_audio_buffer.speech_started
t=2.015   sent_response.create                     <- ARMED here
t=30.019  input_audio_buffer.speech_stopped  reason=max_turn
t=30.228  conversation.item.input_audio_transcription.completed
          text="Golar Shapsich Deeps Shall Skull Shall Ser Shapsich ..."
          (garbled — the reversed-audio transcript, as expected)
t=30.238  response.created   response_id=resp_2f12c9b389ca4cfab63e6086
t=30.249  input_audio_buffer.speech_started   (next turn begins)
t=34.659  response.text.done  text="I am sorry, but I cannot understand the
                                     language or words you are using. Could
                                     you please rephrase your request in
                                     English?"
t=40.562..46.875  response.audio.delta  x68 chunks, 4800 bytes each
                                         (326,400 bytes = 163,200 samples
                                          = 6.80 s of PCM16 mono @ 24 kHz —
                                          matches lobes-cli's
                                          TTS_SAMPLE_RATE/DEFAULT_DELTA_CHUNK_BYTES
                                          exactly)
t=46.885  response.done       response_id=resp_2f12c9b389ca4cfab63e6086

t=60.057  input_audio_buffer.speech_stopped  reason=max_turn  (2nd turn)
t=60.194  conversation.item.input_audio_transcription.completed
          text="Such deeps, shall skull, shall search at such deeps. ..."
t=60.204  response.created   response_id=resp_f0a3c8f3560a460498e9df3b
t=60.206  input_audio_buffer.speech_started   (3rd turn begins)
t=65.060  response.text.done  text="I am still unable to understand these
                                     words as they do not form coherent
                                     sentences in English. Please try to
                                     explain what you need in simpler terms."
t=68.474..75.374  response.audio.delta  x74 chunks (471,040 b64 chars =
                                         353,280 bytes = 176,640 samples
                                         = 7.36 s of PCM16 @ 24 kHz)
t=75.374  response.done       response_id=resp_f0a3c8f3560a460498e9df3b
```

`session_b.stats` (final `event_counts`): `session.created`×1,
`input_audio_buffer.speech_started`×3, `input_audio_buffer.speech_stopped`×2,
`conversation.item.input_audio_transcription.completed`×2,
`response.created`×2, `response.text.done`×2, `response.audio.delta`×142,
`response.done`×2. **Zero `error` events.**

## Cross-talk checks

| Check | Result |
|---|---|
| Distinct session IDs | A = `sess_f0d6101b6dfc47db919a08e6`, B = `sess_8ed776d4d240432e9196a426` — different sessions, confirmed by the server itself |
| A ever receives a `response.*` event | No — `ignored_events == 0` for the whole 85 s run |
| A's transcript content matches A's own (forward) audio | Yes — both utterances read as the forward `dummy-message.wav` narration verbatim |
| B's transcript content matches B's own (reversed) audio | Yes — both utterances are the garbled, non-English-sounding reversed-audio transcript (`"Golar Shapsich Deeps Shall Skull..."`), never the forward text |
| B's LLM reply is coherent with B's own (garbled) transcript | Yes — both replies explicitly say the input "does not form coherent sentences," consistent with what the model received (the garbled reversed-speech transcript), not the forward text |
| Any `error` event on either session | None |
| Session count reported by each client | A: `sessions=1` (one connect, no reconnect needed); B: connected once, torn down cleanly by the probe, never disconnected by the server |

Every one of these six checks would have been the first place a real
isolation bug surfaced — a shared session pool, a routing bug, or a global
mutable conversation-history bleed would show up as A receiving B's
`response.*` frames, or one side's transcript/reply carrying the other side's
content. None of that occurred.

## Timing summary

- Both sessions connected within the same 6 ms window (`t=0.006` for both).
- Feed threads ran **80.0 s**, plus a 5 s drain, plus close/teardown — total
  wall time both sessions were simultaneously live: **~85.0 s** (`elapsed_s:
  85.009` in the summary JSON), comfortably clearing the 60 s acceptance bar.
- The server's `VAD_MAX_TURN_MS` default (30 000 ms, confirmed by reading
  `lobes-cli/lobes/realtime/_settings.py`) force-committed each session's
  turn at ~30 s and ~60 s — the longest silent gap the `dummy-message.wav`
  clip actually contains is ~0.5 s (measured by a 50 ms-window RMS scan
  against a 0.01 threshold), under the 600 ms `VAD_SILENCE_MS` default, so
  every stop in this run has `reason=max_turn`, never `reason=silence`. This
  is a property of the test clip, not a limitation of the probe or the
  gateway — silence-confirmed stops are exercised elsewhere (e.g.
  `tests/fake_realtime_server.py`'s `HAPPY_PATH` scenario).
- Session B's response latency: turn-stop → `response.created` was near
  instant (~10-30 ms); `response.created` → `response.text.done` (the
  generate call) took **4.4 s** (first turn) and **4.9 s** (second turn);
  `response.text.done` → first `response.audio.delta` (TTS synthesis) took
  **5.9 s** and **3.4 s**; full audio delivery took **6.3 s** and **7.1 s**.
  None of this blocked or slowed session A in any observable way — A's own
  second `speech stopped`/utterance landed at `t=60.065`/`60.306`, on
  schedule with its own 30 s max-turn cadence, unaffected by B's in-flight
  TTS synthesis happening in the same window.

## What this probe did NOT test (explicitly out of scope, named honestly)

- **Two ARMED (conversational) sessions concurrently.** This run is
  "one ears-only + one armed," exactly as the plan's acceptance criteria
  specify and exactly as the real deployment shape will be (one runtime
  hearing session + one embodiment-layer duplex session). `TTS_VOICE_CONCURRENCY`
  defaults to `1` upstream and gates the **voice** (synthesis) lane
  specifically (`lobes-cli/lobes/realtime/_settings.py`,
  `docs/realtime-pipeline.md` lines ~495-503: *"Both default to `1` — the
  voice lane deliberately claims lane isolation, which is proven, and not
  multi-session throughput, which is not (concurrent sessions remain
  unvalidated)"*). Since only session B ever calls into that lane here, this
  probe cannot and does not exercise that semaphore under contention — two
  simultaneously-armed sessions competing for TTS synthesis remains
  genuinely untested, upstream and here. This matches the plan's own risk
  framing verbatim ("only one here is conversational") and is not a gap this
  task was asked to close.
- **A live `behavior engine run` process.** Session A here is a standalone
  instance of the real `RealtimeTranscriber` class, constructed directly by
  the probe script — not the actual running runtime process. It uses the
  identical code path the runtime uses (same class, same config resolution,
  same wire codec), so this is a faithful proxy for "the runtime's hearing
  session," but it is not literally `reachy behavior engine run` running
  concurrently. That fuller on-box proof (tick-budget-safe, with the daemon
  holding the camera/mic too) is task **t15**'s job ("h8: the runtime
  transcription session and the embody duplex session run concurrently
  against deployed lobes for at least 5 minutes, both healthy").
- **Repeated/soak runs across time-of-day or gateway load.** This is one
  85 s run. No flakiness or intermittent cross-talk was observed, but a
  single clean run is evidence, not a guarantee against a rare race.
- **A real microphone or the daemon's audio tee.** Audio here is a WAV file
  fed via `submit_audio()`/a hand-rolled `input_audio_buffer.append` sender,
  not live mic hardware or the (not-yet-built, t4) runtime audio tee.

## VERDICT

**Concurrency HOLDS.** Two simultaneous `/v1/realtime` sessions against the
real deployed lobes gateway — one transcription-only (the actual production
`RealtimeTranscriber`, ears-only, never arming) and one armed conversational
(a scratch client sending one `response.create`) — coexisted for **~85
seconds** (≥ the 60 s acceptance bar), each receiving exactly its own correct
event family:

- Session A received only `session.created` / `speech_started` /
  `speech_stopped` / `transcription.completed` — **zero** `response.*`
  events reached it (`ignored_events == 0`).
- Session B received its own `session.created` / VAD boundary events /
  transcription events, **plus** two complete `response.created` →
  `response.text.done` → `response.audio.delta`×N → `response.done`
  sequences, matched content-for-content to its own (reversed, garbled)
  audio.
- No cross-talk of any kind was observed: distinct session IDs, session A's
  ears-only ignore-counter stayed at zero for the whole run, and each
  session's transcript/reply content stayed strictly tied to its own
  (content-distinguishable, forward-vs-reversed) audio stream.
- Zero `error` events on either session; zero handshake refusals; zero
  reconnects needed on either side.

This resolves the plan's `t1` risk item for the shape the embodiment layer
actually needs (one ears-only hearing session + one armed conversational
session, run side by side). It does **not** resolve — and does not claim to
resolve — the narrower "two concurrently ARMED sessions" question, which
stays an open, upstream-unvalidated risk exactly as the plan already frames
it (`TTS_VOICE_CONCURRENCY=1`) and is not this task's acceptance target.

No failure or refusal occurred, so there is nothing to route through
`/deviate` on the media plan.
