# Reachy Mini CLI — Export Feed Schema

This document is the **authoritative contract** for external consumers of the
`reachy-mini-cli` export feeds (e.g. a reTerminal renderer, a logging pipeline,
or any downstream tool). You need only this document — no Python import from
the package is required to implement a compatible reader.

There are **two, separate** feeds. This first half of the document covers the
**cognition feed** (`thinking` / `message` / `emotion`) — whose one and only
producer is `agent attach --export -`, the attached tool-use agent publishing
what it perceived, said, and expressed. The **runtime feed**
(`sense` / `rule` / `intent` / `motion`, produced by `behavior engine run
--export -`) has its own section below —
[Runtime Event Feed (`behavior engine run --export -`)](#runtime-event-feed-behavior-engine-run---export--).
The two are never mixed on one stream: a cognition-feed consumer never sees a
runtime block, and vice versa.

## Wire Format

The feed is **newline-delimited JSON** (NDJSON): one self-contained JSON object
per line, written to stdout. Each object begins with two mandatory keys:

| Key | Type   | Description                                         |
|-----|--------|-----------------------------------------------------|
| `t` | string | Block type: `"thinking"`, `"message"`, or `"emotion"` |
| `ts`| float  | Unix timestamp in fractional seconds (e.g. `1718362800.123`) |

These two keys always appear **first**, so a stream parser can dispatch on
block type before reading the rest of the object.

## Block Types

### `"thinking"` — internal reasoning turn

Emitted **exactly once per agent turn**, after the turn's tool loop drains — so
it is the *last* block of a turn, arriving after every `message` / `emotion`
block that turn produced.

| Key    | Type            | Description                                            |
|--------|-----------------|--------------------------------------------------------|
| `t`    | `"thinking"`    | Block-type discriminator                               |
| `ts`   | float           | Unix timestamp                                         |
| `cues` | array of string | Sense cues that triggered this turn (see the note below) |
| `text` | string          | The turn's raw text — per LLM round, that round's assistant content plus a `name(arguments_json)` rendering of each tool call it made (space-joined); rounds are joined by newlines |

Example line:

```json
{"t":"thinking","ts":1718362800.1,"cues":["speech from the left"],"text":"apply_pose({\"emoji\": \"🤔\"}) speak({\"text\": \"I heard something.\"})"}
```

### `"message"` — speech segment

Emitted when the agent calls a speech tool (`speak` or `harmonics`), at the
moment that call is dispatched — **before**, and independently of, any synthesis
or playback.

**A `message` block is an intent to speak, not proof of sound.** `agent attach`
composes its built-in speech tools **publish-only**: the attached agent is an
external client, the running runtime owns the robot, and so the client's
`synthesize` and `play` seams are inert by design. The block records what the
agent chose to say; making the robot audible is the runtime's job (a react
rule's `say` field). A consumer rendering "what the robot said" is rendering the
agent's utterance, which no speaker in this process reproduces.

| Key    | Type        | Description                       |
|--------|-------------|-----------------------------------|
| `t`    | `"message"` | Block-type discriminator          |
| `ts`   | float       | Unix timestamp                    |
| `text` | string      | The text the agent chose to say   |

Example line:

```json
{"t":"message","ts":1718362800.5,"text":"I heard something."}
```

### `"emotion"` — body-expression trigger

Emitted when the agent calls the `apply_pose` tool to adopt an emotional pose,
at the same dispatch-time point and with the same "intent, not proof" semantics
as a `"message"` block: `agent attach`'s `express` seam is publish-only too, so
the block names the expression the agent chose, not a head that moved.

| Key    | Type              | Description                                                   |
|--------|-------------------|---------------------------------------------------------------|
| `t`    | `"emotion"`       | Block-type discriminator                                      |
| `ts`   | float             | Unix timestamp                                                |
| `emoji`| string            | The emoji that triggered the expression (e.g. `"🤔"`)         |
| `pose` | object or `null`  | 9-axis pose snapshot (head mm/deg, antenna deg, body_yaw deg), or `null` when the emoji is unknown |

Example line:

```json
{"t":"emotion","ts":1718362800.2,"emoji":"🤔","pose":{"head_pitch":-5.0,"antenna_l":30.0,"antenna_r":-30.0}}
```

## Reading the Feed

A minimal Python reader that dispatches on block type:

```python
import json, sys

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    obj = json.loads(line)
    t = obj["t"]
    ts = obj["ts"]
    if t == "thinking":
        print(f"[{ts:.1f}] THINKING cues={obj['cues']} text={obj['text']!r}")
    elif t == "message":
        print(f"[{ts:.1f}] SAY {obj['text']!r}")
    elif t == "emotion":
        print(f"[{ts:.1f}] EMOTION {obj['emoji']} pose={obj['pose']}")
```

## Notes

- All JSON objects use compact separators (no spaces around `,` or `:`).
- `emoji` and other non-ASCII characters appear **literally** in the JSON
  (not escaped as `\uXXXX`).
- The `pose` field in an `"emotion"` block is `null` (JSON null) when the
  emoji is not in the expression catalog — not absent.
- The `cues` array is **never empty in practice**: a turn only runs when the
  agent has at least one sense cue to reason about, so every `"thinking"` block
  the shipped producer emits carries the cues that triggered it. The field is
  still declared as a possibly-empty array — a reader should tolerate `[]`
  rather than assume `cues[0]` exists.
- Consumers should treat unknown `t` values as forward-compatible extensions
  and skip them gracefully.
- **Block ordering within a turn is stable.** A turn emits its `"message"` and
  `"emotion"` blocks in tool-call order as each call is dispatched, then exactly
  one `"thinking"` block once the turn's tool loop drains. A consumer can use
  the `"thinking"` block as an end-of-turn marker.
- **`thinking.text` includes all LLM output** — the `text` field of a `"thinking"`
  block is the **full raw turn text**: every round's assistant content plus a
  rendering of the tool calls that round made. Because a speech tool call renders
  as `speak({"text": …})`, the text of every `"message"` block in a turn also
  appears inside that turn's `thinking.text`. Do not assume `thinking.text` and
  the set of `"message"` blocks for the same turn are disjoint.

## Runtime Event Feed (`behavior engine run --export -`)

This is a **separate wire contract** from the cognition feed above. It carries
the deterministic `behavior` engine's OWN events — perception, rule decisions,
sustained intents, and motion admissions — produced by the 50 Hz
`reachy.behavior.engine` loop and its rule evaluator
(`reachy.behavior.rule_engine`), independent of any LLM.

**Decision c27** (the `symbolic-runtime-70` design): when an external agent
attaches to the running engine (`reachy-mini-cli agent attach` — see
[`docs/operating-reachy.md`'s symbolic-runtime
chapter](operating-reachy.md#the-symbolic-runtime)), it publishes its OWN
cognition feed through the family documented above (`thinking` / `message` /
`emotion`) — it does **not** write into this feed, and this feed never carries
a cognition block. The two feeds are how a human, a script, and an attached AI
agent can all observe the SAME robot from two different, non-overlapping
angles: "what did the deterministic runtime do" versus "what is the agent
thinking."

This separation is also what makes a rules-only run's **zero-token property**
directly verifiable: no block type in this schema can represent an LLM call
(there is no `thinking`/`message`/`emotion` type here at all), so asserting
that every line's `t` is one of `sense` / `rule` / `intent` / `motion` is a
complete proof that the run made zero LLM calls — no log-grepping required.

### Wire Format

Same NDJSON shape as the cognition feed: one compact JSON object per line,
written to stdout, `t` and `ts` always first. Every object additionally
carries `tick` — the engine's 1-based tick counter at publish time — so a
consumer can correlate a `rule` decision with the `sense` snapshot that
triggered it.

| Key    | Type   | Description                                          |
|--------|--------|-------------------------------------------------------|
| `t`    | string | Block type: `"sense"`, `"rule"`, `"intent"`, or `"motion"` |
| `ts`   | float  | Unix timestamp in fractional seconds                  |
| `tick` | int    | The engine's 1-based tick counter                     |

### Block Types

#### `"sense"` — perception snapshot

Published whenever the engine's perception snapshot changes (always once, on
the first tick, to establish a baseline) — not every tick, so a steady 50 Hz
loop does not flood the feed with an identical reading every 20 ms.

| Key                | Type            | Description                                        |
|--------------------|-----------------|------------------------------------------------------|
| `t`                | `"sense"`       | Block-type discriminator                             |
| `ts`, `tick`       | float, int      | As above                                              |
| `doa`              | float or `null` | Sound direction of arrival, radians (`null` = no reading) |
| `speech`           | bool            | Whether speech was detected this reading              |
| `rms`              | float or `null` | Mic loudness (`null` = not sampled)                   |
| `pat`              | array or `null` | `[kind, level]` from a head-pat detection, or `null`  |
| `face`             | string or `null`| A recognised face's name, or `null`                   |
| `frame_available`  | bool            | Whether a camera frame was available to peek           |
| `pat_state`        | object          | The event-stable pat-interaction state (see below); additive and **may be absent** |

`pat_state` is an additive parallel object carrying the pat interaction's
event-stable detail, alongside the legacy one-tick `pat` pair. The live
`behavior engine run --export -` feed always includes it; it is omitted when
the underlying reading carries no pat state at all, so a reader must tolerate
its absence.

| Key                | Type             | Description                                          |
|--------------------|------------------|------------------------------------------------------|
| `availability`     | string           | `"available"` / `"blocked"` / `"unavailable"`         |
| `contact`          | bool             | Whether a hand is currently in contact                |
| `touch_type`       | string or `null` | `"scratch"` / `"side_pat"`, or `null`                 |
| `level`            | string or `null` | `"level1"` / `"level2"`, or `null`                    |
| `yaw_deg`          | float or `null`  | Side-pat yaw deflection in degrees, or `null`         |
| `phase`            | string           | `"idle"` / `"receptive"` / `"contentment"` / `"warning"` / `"released"` / `"enough"` / `"cooldown"` |
| `phase_started_at` | float or `null`  | When the current phase began, or `null`               |
| `last_press_at`    | float or `null`  | When the last press was seen, or `null`               |

Example line:

```json
{"t":"sense","ts":1718362800.0,"tick":1,"doa":null,"speech":false,"rms":null,"pat":null,"face":null,"frame_available":false,"pat_state":{"availability":"unavailable","contact":false,"touch_type":null,"level":null,"yaw_deg":null,"phase":"idle","phase_started_at":null,"last_press_at":null}}
```

**Not every engine sense field reaches this block.** The engine's internal
perception snapshot also carries `transcript` (the words a nearby person said),
`self_moving`, and `rms_ratio`; none of the three is exported here. A consumer
that needs to know *what was said* cannot get it from this feed today — that is
tracked as issue #93 and is unchanged by any recent work.

#### `"rule"` — a rule engine decision

A passthrough of every `reachy.behavior.rule_engine` fire/suppress decision —
the same events the `[SENSE stage=rule ...]` log lines report, in structured
form.

| Key       | Type              | Description                                                    |
|-----------|-------------------|------------------------------------------------------------------|
| `t`       | `"rule"`          | Block-type discriminator                                         |
| `ts`, `tick` | float, int     | As above                                                          |
| `action`  | `"fire"` or `"suppress"` | Whether the rule fired or was suppressed this tick        |
| `rule`    | string            | The rule's `id`                                                  |
| `kind`    | `"react"` or `"inhibit"` | Which rule kind fired                                      |
| `field`   | string            | The sense field the rule's predicate tests (e.g. `"speech"`)     |
| `op`      | string            | The predicate's comparator (e.g. `"is_true"`, `"gt"`)             |
| `reason`  | string            | `"fired"` on a fire; `"cooldown"` / `"rearming"` / `"inhibited"` / `"already-active"` on a suppress |
| `behavior`| string or `null`  | The admitted behavior name (react fire only; `null` otherwise)    |
| `disable` | array of string   | Evicted behavior names (inhibit fire only; `[]` otherwise)        |

Example lines:

```json
{"t":"rule","ts":1718362800.3,"tick":15,"action":"fire","rule":"hear","kind":"react","field":"speech","op":"is_true","reason":"fired","behavior":"nod","disable":[]}
{"t":"rule","ts":1718362800.5,"tick":25,"action":"suppress","rule":"hear","kind":"react","field":"speech","op":"is_true","reason":"cooldown","behavior":null,"disable":[]}
```

#### `"intent"` — a sustained symbolic goal

Emitted when a symbolic goal is declared, updated, or cleared through the
intent-tools spool.

**Producer status:** live. The intent-tools spool
(`reachy/speech/intent_tools.py`, the four tools `reachy-mini-cli agent
attach` carries) and its engine-side consumer
(`reachy.behavior.intents.IntentDriver`) are both built and tested
(`tests/test_speech_intent_tools.py`, `tests/test_behavior_intents.py`), and
`IntentDriver` is composed into `behavior engine run`'s tick bus
(`reachy/cli/_commands/behavior.py::cmd_engine_run`), so a live
`behavior engine run --export -` process emits this block when intents land.
The driver's own `intent.applied` / `intent.blocked` status emissions map to
`action: "applied"` / `"blocked"` (with the intent kind as `name`), alongside
the `declare`/`update`/`clear` vocabulary.

| Key       | Type                              | Description                        |
|-----------|------------------------------------|-------------------------------------|
| `t`       | `"intent"`                         | Block-type discriminator            |
| `ts`, `tick` | float, int                      | As above                            |
| `action`  | `"declare"` / `"update"` / `"clear"` / `"applied"` / `"blocked"` | What happened to the intent |
| `name`    | string                              | The intent's name (or kind, for status actions) |
| `payload` | object                              | Declarative data the intent tool attached |

Example line:

```json
{"t":"intent","ts":1718362801.0,"tick":50,"action":"declare","name":"stay-alert","payload":{"mode":"focus"}}
```

#### `"motion"` — a behavior admission/eviction or goto

Emitted for the engine's active-set churn.

**Producer status:** live. The goto lane
(`reachy.behavior.goto_lane.GotoLane`) is built and tested
(`tests/test_behavior_goto_lane.py`) and is composed into `behavior engine
run`'s tick bus (`reachy/cli/_commands/behavior.py::_compose_run_seam`),
seeded with a live start-pose provider
(`reachy.behavior.pose_feed.LastPoseHolder`) so a goto interpolates from the
robot's current composed pose instead of snapping to neutral. Its lifecycle
emissions (`goto.admitted` / `goto.done` / `goto.cancelled`) map into this
block as `action: "goto"` with `detail.phase` carrying the lifecycle phase. A
goto reaches the lane either through the `behavior goto` CLI verb or through
any client (e.g. a live tool-use agent) submitting a `goto`-kind command into
the intents spool — both paths run through the same fail-closed validator
(`reachy.behavior.goto_intent`).

| Key        | Type                            | Description                          |
|------------|-----------------------------------|---------------------------------------|
| `t`        | `"motion"`                        | Block-type discriminator              |
| `ts`, `tick` | float, int                      | As above                              |
| `action`   | `"admit"` / `"evict"` / `"goto"`  | What happened                         |
| `behavior` | string or `null`                  | The affected behavior name            |
| `channels` | array of string                   | Claimed/released channels             |
| `detail`   | object                            | Action-specific extras (e.g. a goto's target pose) |

Example line:

```json
{"t":"motion","ts":1718362801.2,"tick":52,"action":"admit","behavior":"nod","channels":["head"],"detail":{}}
```

### Reading the Runtime Feed

```python
import json, sys

for raw_line in sys.stdin:
    line = raw_line.strip()
    if not line:
        continue
    obj = json.loads(line)
    t, ts, tick = obj["t"], obj["ts"], obj["tick"]
    if t == "sense":
        print(f"[{ts:.1f} tick={tick}] SENSE doa={obj['doa']} speech={obj['speech']}")
    elif t == "rule":
        print(f"[{ts:.1f} tick={tick}] RULE {obj['action']} {obj['rule']} ({obj['reason']})")
    elif t == "intent":
        print(f"[{ts:.1f} tick={tick}] INTENT {obj['action']} {obj['name']}")
    elif t == "motion":
        print(f"[{ts:.1f} tick={tick}] MOTION {obj['action']} {obj['behavior']}")
```

### Runtime Feed Notes

- All JSON objects use compact separators (no spaces around `,` or `:`).
- `--export-blocks` on `behavior engine run` selects among `sense`, `rule`,
  `intent`, `motion` — the cognition feed's `thinking` / `message` / `emotion`
  names are **not valid here** and are rejected as an unknown block type.
- Consumers should treat unknown `t` values as forward-compatible extensions
  and skip them gracefully — the same rule as the cognition feed.
- **No LLM-shaped block exists in this schema.** A rules-driven run's zero-token
  property can be verified directly from the feed: capture it and assert every
  line's `t` is one of `sense` / `rule` / `intent` / `motion`.

## Nervous System Bus (MQTT)

This is a **third, additive channel** — not a replacement for either NDJSON
feed above. `behavior engine run` composes `reachy.export.mqtt.NervousPublisher`
(`reachy/export/mqtt.py`) to republish the same runtime-feed events, plus the
engine's standing `state.json`, onto an MQTT-shaped event bus, so an external
service (the reTerminal panel, a logging pipeline, a future subscriber) can
observe Reachy's senses without touching the SDK, without importing any Python
from this package, and without contending for the single SDK media session the
runtime holds.

**The broker and its client are not shipped by this repo.** They belong to the
sibling **events-cli** project (requirements tracked at
`agentculture/events-cli#3`); this repo ships no MQTT library and no wire code —
it declares only the narrow client surface it needs (`connected`, `will_set`,
`connect`, `disconnect`, `publish`, and the optional `set_on_connect`) and a
composition step binds the real events-cli client at process start. Without a
client injected — or with one that does not satisfy that surface, or an
unreachable broker — the publisher degrades to one named, greppable drop and
every publish becomes a silent no-op; the 50 Hz tick is never affected. The
eventual client is events-cli's own importable package — never `paho-mqtt` (or
any other MQTT library) added to this repo.

### Broker location

The client reaches the broker at `REACHY_MQTT_URL`, defaulting to
`localhost:1883` — a loopback default; a remote consumer is an explicit,
documented opt-in, never the default.

### Topic map

Two trees under one root (`reachy` by default, injectable at composition):

| Topic                            | Retained | QoS | Contents                                     |
|-----------------------------------|----------|-----|-----------------------------------------------|
| `reachy/events/{source}/{type}`   | No       | 0   | One message per runtime-feed event             |
| `reachy/state/{key}`              | Yes      | 0   | One message per top-level `state.json` key     |
| `reachy/state/online`             | Yes      | 0   | Publisher-owned availability (`true`/`false`)  |

Every publish on both trees is **QoS 0** (at-most-once): a dropped message
under load matches the drop-don't-block ethos the rest of the runtime follows,
and the retained-state topics are self-healing by retention — a late
subscriber gets the current value regardless of a missed message in between.

#### `reachy/events/{source}/{type}` — the runtime feed, republished

`{source}` is the runtime-feed block type (`sense`, `rule`, `intent`, `motion`
— see [Runtime Event Feed](#runtime-event-feed-behavior-engine-run---export--)
above) and `{type}` is that block's action:

| `{source}` | `{type}` values                                          |
|------------|-----------------------------------------------------------|
| `sense`    | `snapshot` (a `sense` block carries no action of its own) |
| `rule`     | `fire`, `suppress`                                        |
| `intent`   | `declare`, `update`, `clear`, `applied`, `blocked`         |
| `motion`   | `admit`, `evict`, `goto`                                  |

The **payload is `runtime_to_jsonl(event)`'s output, verbatim** — the exact
same serializer, byte-identical, that produces every line of the stdout
`behavior engine run --export -` feed documented above. One serializer, two
transports: a topic's payload and the corresponding NDJSON line for the same
event cannot drift, because nothing here re-derives the wire shape. A consumer
that already parses the stdout runtime feed can parse this topic's payload
with the exact same code.

Topic segments are **sanitised**: `/`, `+`, `#`, and any other character
outside `[0-9A-Za-z._-]` are replaced with `-` before a segment is built, so a
malformed rule id or behavior name can never smuggle a wildcard or a separator
into the topic tree and publish outside its own subtree. An event whose `t`
value is not one of `sense` / `rule` / `intent` / `motion` never reaches a
topic at all — it is dropped by name (`reason=unknown-event-type`) instead of
silently skipped.

Not retained: the event stream is a stream, so a subscriber that connects
mid-run sees nothing until the next event.

#### `reachy/state/{key}` — retained standing state

One retained message per top-level key of the engine's `state.json` payload —
today that is `updated`, `compose_hz`, `active`, `ownership`, `doa`, `intents`,
and (since #120b) `senses`. The publisher never derives this state itself: it
wraps the ONE existing writer (`CommandSpool.write_state`, fed by
`Engine.state()` and the `intents`/`senses` seam riders), so the bus is a
transport for the same truth `behavior status` reads off disk, never a second
source of it. Each value is published as compact JSON
(`json.dumps(value, ensure_ascii=False, separators=(",", ":"))`) under
`reachy/state/{key}`, retained, so a subscriber that connects mid-run
immediately receives the current value of every key without waiting for the
next change. A value that fails to serialize (e.g. a stray binary field) is
dropped by name (`reason=unserializable-payload`) without losing the other
keys in the same snapshot.

#### `reachy/state/online` — availability, publisher-owned and reserved

A retained `true`/`false` topic the publisher itself owns:

- `true`, retained, published the moment a broker session is live — on first
  connect, and again on every reconnect.
- The publisher configures an MQTT **Last Will** on this same topic — payload
  `false`, retained — **before** it connects, so an ungraceful death
  (`kill -9`, a severed link) flips the topic to `false` even though the
  runtime never got the chance to say so itself.
- `false`, retained, on a graceful shutdown too, so a clean stop and a crash
  both leave the same honest value.

This is how a consumer distinguishes *live* state from *stale retained
state*: every other `reachy/state/*` topic keeps serving its last known value
forever once its publisher goes away, and `online` is the one topic that says
whether to trust them right now.

`online` is a **reserved** key name: a `state.json` payload that ever contains
a top-level `online` key has that key refused (named drop,
`reason=state-key-reserved`) rather than allowed to publish over the
publisher's own availability topic and shadow it.

### No media on the bus — text references only

**Events and state never carry inline media.** A `sense`/`rule`/`intent`/
`motion` payload, and every `state.json` value mirrored onto
`reachy/state/{key}`, may reference media only as **text** — a file path, a
URL, or a memory-link handle (e.g. `/var/lib/reachy/frames/0042.jpg` or
`memlink://reachy/audio/9f2a`) — never as inline binary or a base64 blob.
Camera frames and mic audio move out-of-band; the bus only ever announces
*where* they are.

This is a **structural** guarantee, not a convention an event author has to
remember: no runtime event field is binary-typed, serialization is
`json.dumps` with no `default=` (so a `bytes` value cannot be encoded at all —
it fails and is dropped by name, `reason=unserializable-payload`, before it
reaches a client), and neither the event model nor the publisher module
imports a media codec (`base64`, `binascii`, an imaging library). An external
consumer can validate a captured payload against this rule directly: a plain
path/handle string passes; a `data:` media URI, a long uninterrupted base64
run, or a raw binary value all fail it. `tests/test_nervous_media_boundary.py`
enforces all three layers (schema, wire, and seam) and is the authority if
this section and the code ever disagree.

### Degradation

Ported from `reachy_nova`'s `nova_mqtt.py` design (an unreachable broker makes
every publish a silent no-op and the app runs unaffected), tightened with this
repo's senselog discipline: nothing here may ever raise into the 50 Hz tick
thread, and nothing here may ever fail *silently*.

Every fault path resolves to one
`[SENSE stage=nervous source=mqtt event=<id>] dropped reason=<reason>`
line — greppable, and named from a fixed
vocabulary (`no-client`, `client-incompatible`, `connect-failed`,
`broker-unreachable`, `publish-failed`, `unserializable-payload`,
`state-key-reserved`, `state-not-a-mapping`, `unknown-event-type`,
`disconnect-failed`). Each distinct reason is reported **once per session**;
the report latch clears on the next successful connect, so a second outage in
a later session is named again rather than swallowed by the first one's
report.

**Connect and reconnect are reported as `senselog.stage` lines** (`"connected"`
the first time, `"reconnected — republishing retained state"` on every
subsequent one) — **but an in-run loss of the broker session is reported as a
`senselog.drop`**, naming `reason=broker-unreachable`. Do not assume all three
transitions get the same "something happened, and it's fine" stage-line
treatment: a session going down is, by construction, exactly what turns every
subsequent publish into a no-op, so it carries the same shape as every other
degrade path instead. Both share the fixed `[SENSE …]` line format, so a
consumer grepping for `stage=nervous` sees the whole lifecycle either way. A
*graceful* shutdown (the publisher's own `stop()`) is a third, separate case:
it publishes the retained `false` availability message, calls the client's
`disconnect()` (a raising `disconnect()` is itself a named drop,
`reason=disconnect-failed`, never an exception), and then emits its own
`"publisher stopped"` stage line — deliberate shutdown reports as a stage
event; an unplanned session loss reports as a drop.

### Reading the bus

A minimal sketch using any MQTT client library (not shipped by this repo —
there is none to import):

```python
def on_message(client, userdata, msg):
    if msg.topic == "reachy/state/online":
        live = msg.payload.decode() == "true"
        print("ONLINE" if live else "OFFLINE (stale retained state below)")
    elif msg.topic.startswith("reachy/events/"):
        # payload is one runtime-feed line, byte-identical to the stdout feed
        event = json.loads(msg.payload)
        print(f"[{event['ts']:.1f}] {msg.topic} {event}")
    elif msg.topic.startswith("reachy/state/"):
        key = msg.topic.rsplit("/", 1)[-1]
        print(f"STATE {key} = {json.loads(msg.payload)}")

client.subscribe("reachy/events/#")
client.subscribe("reachy/state/#")
```

### Nervous Bus Notes

- The topic root is injectable at composition (`reachy` by default) — a
  multi-robot deployment can give each robot its own root.
- QoS is 0 everywhere on this bus; there is no QoS 1/2 delivery guarantee
  anywhere in it.
- Every payload on both trees is compact JSON (`ensure_ascii=False`,
  `separators=(",", ":")`), matching the stdout feeds' formatting.
- The event-tree payload vocabulary is exactly the [Runtime Event
  Feed](#runtime-event-feed-behavior-engine-run---export--) schema above —
  this section adds transport and topic structure, not a new wire format.
- Nothing in this module imports an MQTT/transport library
  (`paho`/`gmqtt`/`asyncio_mqtt`/`aiomqtt`/`amqtt`/`hbmqtt`/`socket`/`ssl`);
  the transport is entirely the injected client's responsibility.
