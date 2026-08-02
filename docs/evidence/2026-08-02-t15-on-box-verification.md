# t15 — on-box verification: tee tick budget, dual sessions, daemon playback

Run 2026-08-02 10:10–10:35 IDT against the deployed robot, with
`reachy-daemon.service` and `reachy-runtime.service` both live and the runtime
executing this checkout (editable install). Covers the live halves of **h10**,
**h8** and **h5**.

Two of the three are settled here. The third (h5) is settled *as far as an
instrument can settle it*, and the part that needs an ear is handed to t14 —
stated plainly rather than rounded up.

## h10 — the tee costs the tick nothing measurable ✅

Method: count overrun ticks the runtime itself reports
(`[SENSE stage=rule source=tick event=overrun*]`, `reachy/behavior/
tick_metrics.py`) over matched 100-second windows, varying only the tee's
consumer.

| phase | consumer | overrun ticks | streak starts |
|---|---|---|---|
| A | none | **0** | 0 |
| B | **active** — read 6,721,627 B in 105 s | **0** | 0 |
| C | **wedged** — connected, never reads | **0** | 0 |

Phase B's throughput is exactly the expected rate: 6,721,627 B / 105 s ≈ 64 kB/s
= 16 000 samples/s × 4 bytes (float32). So the consumer was genuinely draining a
live stream, not idling.

Phase C is the important one, and the drop-don't-block design behaved exactly as
specified — the runtime named the condition rather than absorbing it:

```text
[SENSE stage=audio source=tee event=tee] dropped reason=consumer-slow count=1 (a consumer is not reading)
```

A wedged consumer therefore costs the tick nothing and loses only its own audio.

### The sustained overrun that exists anyway is PRE-EXISTING

Worth being careful here, because it would be easy to mistake for a regression.
With the camera **alive** the runtime does sit in a continuous overrun streak
(`mean_ms=21.06`, ~5 % over the 20 ms budget). It is not ours:

- with the camera dead (phases A–C) there were **zero** overruns in any
  configuration, including with an active tee consumer;
- the same streak is in the journal for **Jul 30** — days before this arc's first
  commit — at `mean_ms=21.03`, `count=11,281,500`, i.e. continuous since that
  boot;
- today's post-arc figure is `mean_ms=21.06`. Statistically the same number.

So the overrun correlates with **camera frame processing** and predates both
additive legs. Filed separately as issue #137 rather than folded into this arc.

## h8 — both sessions, concurrently, for five minutes ✅

The runtime's transcription session was already live. The layer's duplex session
was started beside it against the same deployed gateway, **muted by
construction** (`play=None`, `arm_on_connect=False`) so this measures
coexistence, not conversation.

```text
[  60.0s] sessions=1 chunks_sent=2593  bytes=1920512 connected=True down=False
[ 120.0s] sessions=1 chunks_sent=5207  bytes=3841024 connected=True down=False
[ 180.0s] sessions=1 chunks_sent=7812  bytes=5760512 connected=True down=False
[ 240.0s] sessions=1 chunks_sent=10421 bytes=7680512 connected=True down=False
FINAL sessions=1 connect_failures=0 chunks_sent=13028 bytes_sent=9599488
      utterances=0 connected=True session_down=False lane_unavailable=False
```

- **One** session for the whole five minutes — `sessions=1` means no reconnect
  ever happened, and `connect_failures=0` means none was needed.
- 9,599,488 B / 300 s = 32,000 B/s = 16 kHz × 2 bytes (PCM16) — the wire carried
  exactly what it should, continuously.
- The runtime side stayed healthy throughout: **zero** `session-down` or hearing
  drops in the same window, `state.json` heartbeat fresh, tick `mean_ms=21.05`
  (i.e. unchanged by the concurrent session).

`utterances=0` is honest, not a failure: nobody spoke to the robot during the
window and the session was deliberately unarmed.

## h5 — the daemon playback route under a live engine ✅ / ⚠️

**Verified:** the layer's own sink (`EmbodySink`, robot profile → daemon http)
played while the engine held the media session, with no contention and no
throttle. The daemon accepted both legs:

```text
POST /api/media/sounds/upload  -> {"status":"ok","path":"/tmp/reachy_mini_sounds/probe.wav"}
POST /api/media/play_sound     -> {"status":"ok"}   HTTP:200
```

`sink.play()` returned in 0.04 s and logged no drop — and the sink's failure path
is a named `playback-failed` drop, so silence there means success, not a
swallowed error. Senses stayed at rate immediately after: all eight sense
providers `available`, `state.json` heartbeat age 0.0 s, tick `mean_ms=21.06`
(unchanged), clip rider still producing. **No ~1 Hz throttle**, which is the
specific hazard h5 names.

**Not verified: audibility.** An instrument in this room cannot settle it, for a
reason worth recording:

> The obvious probe — play a sound, listen on the tee — is structurally wrong.
> The tee carries the **AEC channel** of the robot's own microphone, and Reachy
> has hardware echo cancellation against its own speaker. That channel is
> engineered specifically to *remove* the robot's own output.

Measured anyway, and it behaved exactly as that predicts: peak RMS during
playback was only **2.06×** the silent baseline (0.4396 vs 0.2137), well under
the 3× threshold set in advance. A monitor tap
(`parecord` on the sink's `.monitor` source) was also tried and returned a
44-byte WAV header with zero frames — a suspended sink's monitor yields nothing.

So "the sound was audible in the room" is left to **t14**, where a human is
present. The route, the timing and the non-contention are established here.

## A correction recorded on purpose

The first attempt at the h5 route check `POST`ed to `/media/sounds/upload` and
`/media/play_sound` and got `404 Not Found` — which looked like a broken shipped
code path. It was not. `reachy/speech/playback.py` uses the correct
`/api/media/...` constants; only its **docstrings** still showed the old
unprefixed paths, and those docstrings are where the wrong URLs were copied from.

The code was right and the probe was wrong. The stale docstrings are fixed in
the same commit as this file, because they will mislead the next reader exactly
as they misled this one — which is adjacent to what issue #131 already tracks
about speech-transport documentation drift.

## What this does NOT establish

- **Audibility, as above.** t14.
- **No conversation happened.** The duplex session was unarmed and muted; no
  utterance was transcribed, no turn was taken, no tool was dispatched. Every
  claim here is about transport and timing, not about the robot behaving well.
- **One five-minute window** for h8. It says nothing about an hours-long run, a
  gateway restart mid-session, or the reconnect path (which never fired,
  precisely because nothing went wrong).
- **The camera was restarted mid-session** to recover from an unrelated
  pipeline death (issue #138), so the h10 phases A–C ran camera-dead and phase D
  camera-alive. That is what made the pre-existing overrun attributable, but it
  means no single window exercised the tee and the clip leg together under load.
- **Nothing here was measured under a wedged consumer *and* a live camera
  simultaneously** — the worst case for the tick budget remains untested.
