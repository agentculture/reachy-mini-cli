# t14 — live acceptance: the layer heard, thought, spoke and moved the robot

Run 2026-08-02 10:45–11:05 IDT on the deployed robot, with
`reachy-daemon.service` and `reachy-runtime.service` live throughout and the
layer attached beside them. Artifacts: `2026-08-02-t14-artifacts/`.

**Read the verdict carefully.** The layer's own end-to-end loop is demonstrated
on real hardware, including physical motion. The *two-realtime-APIs-in-sustained-
conversation* demo is **not** achieved, for a concrete reason recorded below.
Rounding that up to "t14 passed" would be false.

## The arrangement (deviation d1)

The plan says bench profile; this ran in the **robot** profile, approved as
deviation `d1`. The dev box has exactly one audio output — Reachy's own USB
speaker — so bench would have put both conversational parties on the same
devices. Robot profile also exercises the deployed path.

The arrangement was validated by measurement before use, and the result is the
useful finding of the session:

| audio path | played by | Reachy's mic (via the tee) hears |
|---|---|---|
| daemon route (`/api/media/*`) | Reachy's own voice | **2.06×** baseline — cancelled by hardware AEC |
| PipeWire (`paplay`) | anything else in the room | **4.72× peak, 5.84× p95** — not cancelled |

So Reachy's echo cancellation is scoped to **the daemon's own playback**, not to
the speaker as a device. A second party can therefore share the one physical
speaker and still be heard. That is what made a single-speaker bench viable at
all, and it is not something the code documents anywhere.

## What was verified live

### The layer comes up against the real runtime ✅

```text
[embody] layer up: profile=robot; ears+mouth on one realtime session, ...
[SENSE stage=embody source=media-robot-source] connected to runtime tee socket
[SENSE stage=embody source=media-robot-source] tee header accepted (format=f32le rate=16000 Hz)
[SENSE stage=duplex source=embody event=sess1] session up url=ws://localhost:8001/v1/realtime?input_sample_rate=16000
[SENSE stage=duplex source=embody event=sess1] session.created rate=16000 vad=server_vad
[SENSE stage=duplex source=embody event=sess1] armed (response.create)
```

The tee wire negotiated correctly against the live producer, and the duplex
session came up armed with server-side VAD.

### Hear → think → speak, out loud ✅

```text
speech started (server vad)
speech stopped (server vad) reason=silence
utterance chars=5
turn done rounds=1 refusals=0 chars=17
response done id=resp_c3613b4eb366496ea9189f2a chars=58 audio=134400B
```

134,400 bytes at 24 kHz PCM16 ≈ **2.8 seconds of speech**, played through
Reachy's speaker. The export feed carried all three block types
(`thinking` / `message` / `emotion`) for the turn:

- thought: `"Okay, got it. 🤔"`
- emotion: `🤔`
- **spoken: "I am ready to help you. What would you like to talk about?"**

### A rule fire produced a reaction — h3 ✅

A `rule` event was injected into the layer's feed, exactly as the runtime would
emit it:

```json
{"t":"rule","ts":...,"rule":"pat-acknowledge","action":"fire","behavior":"nod"}
```

The layer mapped it to a cue and took a six-round turn on it:

```text
[SENSE stage=cue source=runtime] a behavior rule fired (pat-acknowledge): now doing nod
[SENSE stage=turn source=embody] turn done rounds=6 refusals=0 chars=127
```

This is the capability the whole arc was requested for: *the robot's own rule
firing becomes an input the layer reacts to.*

### The robot physically moved on the layer's instruction — h18 (partial) ✅

The layer dispatched actions through the intents spool and the **live runtime
admitted and applied them** — from the runtime's own journal, not the layer's:

```text
[SENSE stage=intent source=run_behavior] applied result={'ok': True, 'op': 'run_behavior',
    'id': 'intent:run:nod:2', 'name': 'nod', 'class': 'stoppable', 'channels': ['head'], ...}
[SENSE stage=intent source=goto] applied result={'ok': True, 'op': 'goto', 'id': 'goto-1',
    'channels': ['antennas', 'head'], 'duration': 2.0}
```

Direct-operation action classes verified end to end on hardware: **`speak`**
(2.8 s of audio out), **`run_behavior`** (nod ×4, admitted), **`goto`**
(×2, antennas + head). Not exercised this session: `harmonics`, `create_rule`.

### The browser harness side ✅ (partially)

`site/index.astro` connected through its proxy, reported
**"credential accepted; stt lane feasible"** — which also confirms the
diagnostic behind issue #134 is meaningful and that the lane was healthy — and
opened an armed session:

```text
session_id=sess_b6889306e9b64f19abb05f6e — 24000Hz, turn_detection=server_vad, aec_mode=none
Armed — response.create sent; every committed turn on this session gets a spoken reply
```

Microphone permission was granted and capture confirmed at the OS level
(a PipeWire `source-output` on the Arducam input).

## What was NOT achieved, and why

**A sustained back-and-forth between the two realtime sessions did not happen.**
Both sides were individually live — Reachy spoke aloud; the browser held an
armed session with a working mic — but they never entered a loop.

The blocking mechanism is concrete: with **one** audio output device, the
browser holds a PipeWire playback stream on Reachy's speaker for the whole
session, and that stream could not be evicted (`pactl kill-sink-input` left it
in place, `paplay` then failed with `Stream error: Timeout`). So the room could
not be seeded with a spoken prompt while the browser was connected, and the one
utterance Reachy did transcribe (5 characters) was ambient noise, not the
browser.

The C270 webcam also exposes **only** a `pro-audio` profile, which the browser
did not open; capture only worked after switching the default source to the
Arducam. Worth knowing before the next attempt.

**What would fix it:** a second audio output (HDMI audio from an awake monitor —
all HDMI profiles reported `available: no` this session — or any USB speaker).
That gives each party its own speaker and removes the contention entirely.

## Other honest gaps

- **`h9` (a clip described correctly by the worker) was not tested.** The clip
  rider is now producing real files, but no clip was handed to the worker model
  in this session.
- **`h4`'s echo half is only half-shown.** Reachy did not answer its own voice,
  but it also never had a long enough exchange to prove a loop cannot form.
- **The three coherent turns h12 asks for did not happen** — there was one
  spoken turn and one cue-driven turn.
- **The agent repeated itself**: given a single nod cue it emitted four
  `run_behavior` nods and two gotos in one six-round turn. Not a failure of the
  contract (every call was admitted and bounded), but the prompt clearly lets it
  loop on a single stimulus, and that is worth tuning.
- The layer needed a held-open FIFO (`--feed`) to stay alive, because the bus
  route does not exist yet (events-cli#14) and an exhausted cue reader ends the
  run. That is t11's documented `should_stop` behaviour meeting reality; with a
  real `behavior engine run --export -` writer it would not arise.

## System state after the run

Restored: the embody process stopped, the site dev server stopped, PipeWire's
default source returned to Reachy's mic, the C270 card profile set back to
`off`. `reachy-runtime.service` and `reachy-daemon.service` remained `active`
throughout and afterwards, with the robot back on its `feel-alive` idle
behaviour and `state.json` fresh.
