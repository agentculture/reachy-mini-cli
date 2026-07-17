# Reachy Mini CLI — Export Feed Schema

This document is the **authoritative contract** for external consumers of the
`reachy-mini-cli` export feeds (e.g. a reTerminal renderer, a logging pipeline,
or any downstream tool). You need only this document — no Python import from
the package is required to implement a compatible reader.

There are **two, separate** feeds. This first half of the document covers the
**cognition feed** (`thinking` / `message` / `emotion`, produced by `think run
--export -` and `listen run --live --export -`). The **runtime feed**
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

Emitted by the cognition loop when the robot processes sense events and
produces an LLM response.

| Key    | Type            | Description                                            |
|--------|-----------------|--------------------------------------------------------|
| `t`    | `"thinking"`    | Block-type discriminator                               |
| `ts`   | float           | Unix timestamp                                         |
| `cues` | array of string | Sense cues that triggered this turn (may be empty `[]`) |
| `text` | string          | Raw LLM output including `*emoji*` / `"speech"` markers |

Example line:

```json
{"t":"thinking","ts":1718362800.1,"cues":["sound","motion"],"text":"*🤔* \"I heard something.\""}
```

### `"message"` — speech segment

Emitted when the robot speaks a sentence aloud (after TTS synthesis).

| Key    | Type        | Description                       |
|--------|-------------|-----------------------------------|
| `t`    | `"message"` | Block-type discriminator          |
| `ts`   | float       | Unix timestamp                    |
| `text` | string      | The text spoken by the robot      |

Example line:

```json
{"t":"message","ts":1718362800.5,"text":"I heard something."}
```

### `"emotion"` — body-expression trigger

Emitted when the robot adopts an emotional pose (driven by an emoji marker
from the cognition loop).

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
- `cues` in a `"thinking"` block may be an empty array `[]` when the
  cognition turn was timer-driven rather than sense-triggered.
- Consumers should treat unknown `t` values as forward-compatible extensions
  and skip them gracefully.
- **`thinking.text` includes all LLM output** — the `text` field of a `"thinking"`
  block is the **full raw LLM turn stream**, including prose that appears before the
  first `*emoji*` or `"speech"` marker. By the engine's existing design, leading
  prose (text before the first delimiter) is also spoken aloud — so such text can
  appear **both** inside `thinking.text` and as a separate `"message"` block. Do
  not assume `thinking.text` and the set of `"message"` blocks for the same turn
  are disjoint.

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

Example line:

```json
{"t":"sense","ts":1718362800.0,"tick":1,"doa":null,"speech":false,"rms":null,"pat":null,"face":null,"frame_available":false}
```

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

**Producer status:** the intent-tools spool
(`reachy/speech/intent_tools.py`, the four tools `reachy-mini-cli agent
attach` carries) and its engine-side consumer
(`reachy.behavior.intents.IntentDriver`) are both built and independently
tested (`tests/test_speech_intent_tools.py`, `tests/test_behavior_intents.py`),
but two things are still open before a live `behavior engine run --export -`
process actually emits this block: (1) `IntentDriver` is not yet composed into
`behavior engine run`'s tick bus — `reachy/cli/_commands/behavior.py`'s
`cmd_engine_run` composes the rules evaluator and a sense-snapshot publisher
only; and (2) `IntentDriver` currently publishes its own `intent.applied` /
`intent.blocked` `ctx.emit` event types rather than the `intent.declare` /
`intent.update` / `intent.clear` shape `to_runtime_event()` maps below, so
reconciling the two vocabularies is follow-up work even once (1) lands.

| Key       | Type                              | Description                        |
|-----------|------------------------------------|-------------------------------------|
| `t`       | `"intent"`                         | Block-type discriminator            |
| `ts`, `tick` | float, int                      | As above                            |
| `action`  | `"declare"` / `"update"` / `"clear"` | What happened to the intent       |
| `name`    | string                              | The intent's name                   |
| `payload` | object                              | Declarative data the intent tool attached |

Example line:

```json
{"t":"intent","ts":1718362801.0,"tick":50,"action":"declare","name":"stay-alert","payload":{"mode":"focus"}}
```

#### `"motion"` — a behavior admission/eviction or goto

Emitted for the engine's active-set churn.

**Producer status:** the goto lane (`reachy.behavior.goto_lane.GotoLane`) is
built and independently tested (`tests/test_behavior_goto_lane.py`), and
publishes `ctx.emit` events for its own lifecycle
(`goto.admitted` / `goto.done` / `goto.cancelled`), but it is not yet composed
into `behavior engine run`'s tick bus, and those raw event-type strings do not
yet match the `motion.admit` / `motion.evict` / `motion.goto` shape
`to_runtime_event()` maps below — reconciling the two is follow-up work, the
same as the `"intent"` block above.

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
