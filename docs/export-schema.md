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
