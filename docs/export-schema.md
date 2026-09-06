# Reachy Mini CLI — Export Feed Schema

This document is the **authoritative contract** for external consumers of the
`reachy-mini-cli` export feeds (e.g. a reTerminal renderer, a logging pipeline,
or any downstream tool). You need only this document — no Python import from
the package is required to implement a compatible reader.

There are **two, separate** feeds. This first half of the document covers the
**cognition feed** (`thinking` / `message` / `emotion`) — an external mind
publishing what it perceived, said, and expressed. It has **two** producers,
which emit the same block types with the same fields: `agent attach --export -`
(the turn-based tool-use agent, whose voice and pose seams are publish-only)
and `agent embody --export -` (the embodiment layer, which really does speak
in the room — but through its realtime voice, not through the tools whose
calls this feed carries; see the per-block notes below, and
[the operating guide](operating-reachy.md#the-embodiment-layer--agent-embody)).
The **runtime feed**
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
| `cues` | array of string | The perception lines this turn read (see the notes below) |
| `text` | string          | The turn's raw text — per LLM round, that round's assistant content plus a `name(arguments_json)` rendering of each tool call it made (space-joined); rounds are joined by newlines |

Example line:

```json
{"t":"thinking","ts":1718362800.1,"cues":["speech from the left"],"text":"apply_pose({\"emoji\": \"🤔\"}) speak({\"text\": \"I heard something.\"})"}
```

**The block shape is unchanged by `agent embody`'s input policy** (issue #143):
no key was added or removed. What changed is what two existing fields carry,
and only for that producer — see "the two producers differ in what `cues`
means" and "`agent embody` opens `thinking.text` with its drain counts" in the
notes below.

### `"message"` — speech segment

Emitted when the agent calls a speech tool (`speak` or `harmonics`), just
before that call is dispatched — **before**, and independently of, whatever the
call turns out to reach (synthesis and playback for `agent attach`'s inert
seams; an interjection policy for `agent embody`'s). `agent embody` emits one
for a second trigger too: speech its duplex session already produced and played
(see below).

**Whether the block is proof of sound depends on the producer, so a renderer
that captions it must know which one it is reading.**

- **`agent attach` — an intent to speak, not proof of sound.** It composes its
  built-in speech tools **publish-only**: the attached agent is an external
  client, the running runtime owns the robot, and so the client's `synthesize`
  and `play` seams are inert by design. The block records what the agent chose
  to say; making the robot audible is the runtime's job (a react rule's `say`
  field). A consumer rendering "what the robot said" is rendering an utterance
  no speaker in that process reproduces.
- **`agent embody` — one of two things, and only the second is sound.** Since
  the two-tempo split (issue #155) the layer's `speak` / `harmonics` tools no
  longer reach any sink: they are **proposals** from the background mind, which
  the interjection policy may refuse and which the foreground voice may
  re-word or decline. So a `message` from `agent embody` is either
  (a) **a proposal**, emitted *before* dispatch, recording what the mind wanted
  said — the policy's verdict lands in the same turn's `thinking` block,
  verbatim, as the tool result — or (b) **an utterance the duplex session
  already spoke aloud**, recorded after the fact. Emitting the proposal before
  dispatch is deliberate: *what it wanted* and *what it was allowed* are two
  facts, and folding them into one would hide every refusal.

Either way, speech produced by the *runtime itself* (a rule's `say`) carries no
block on any feed — it is logged as `[SENSE stage=speech source=say …]`.

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

`agent embody` emits the same block from a different origin: its action set has
**no** `apply_pose` tool, so the block reports an emoji found in the model's own
reply text, resolved against the shipped expressions catalog. It is an
observation about the reply, never a pose that was applied — and the reply text
is neither consumed nor rewritten by the scan.

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
- **The two producers differ in what `cues` means.** For `agent attach` every
  entry is a cue that triggered the turn. For `agent embody` (issue #143) only
  the leading entries are: an utterance (rendered `heard: "…"`) or an **alert**
  cue — a rule fire. Everything after them is drained **context** — sense
  snapshots, intent changes, motion admissions, rule suppressions — which
  accumulated in a coalescing park and never caused a turn of its own. Repeats
  of one context fact arrive coalesced with a count, e.g. `speech from the left
  (x145)`; a fact seen once carries no count. A consumer that renders `cues` as
  "why the robot thought" should show the leading trigger lines, not the
  background.
- **Three more kinds of line can appear in `agent embody`'s `cues`** since the
  two-tempo split (issue #155). None of them is a new block type and none of
  them changes the field's type; they are additional CONTEXT lines, except
  where noted:
  - a **perception snapshot** — what the camera currently shows, rendered from
    a structured snapshot rather than free prose. It lives in a latest-wins
    slot per source and PERSISTS across turns until superseded or stale (30 s),
    so the same slot can legitimately appear on consecutive turns. An updated
    slot renders `… (updated x180)` where a repeated cue renders `… (x145)` —
    a reader can tell "the same fact happened again" from "this state was
    refreshed";
  - a **wanted-to-say** line — `I was interrupted before saying: "…"` — the
    measured remainder of a reply a human cut off mid-sentence. It expires
    after two turns and is **structurally context-only**: the robot never
    wakes itself to finish an old sentence;
  - an **interjection** line — `<source> suggests saying: "…"` — an authorized
    background source proposing a sentence for the foreground voice. This one
    arrives as an **alert**, so it leads the array rather than trailing it,
    the same lane a rule fire uses. It is off by default: nothing produces
    these unless an operator has authorized a source.
- **Cognition scopes reach the foreground prompt, not this feed.** A
  `cognition.scope` artifact — the compact `{goal, relevant_facts,
  suggested_next_step, priority, expires_after_turns, speakable}` object the
  background mind produces — is rendered into a system message for the
  foreground voice. It is **not** published as a block or a cue in this
  version, so a consumer cannot see one directly; what it CAN see is the tool
  result that created one (below). Raw model reasoning is never in a scope by
  construction, and `thinking.text` carries none either while
  `enable_thinking` stays off.
- **Tool RESULTS are in `thinking.text`, and that is where interjection
  verdicts show up.** Every dispatched tool's result is appended verbatim,
  prefixed with an arrow (`->` and a space). For a governed voice tool that is either
  `{"ok": false, "refusal": "interjection-unauthorized", …, "voice": "speak"}`
  or `{"ok": true, "admitted": "interjection-warm", "interjection": {"t":
  "interjection", "id": …, "source": …, "text": …, "ts": …}, "voice": "speak",
  "chars": …}`. The refusal vocabulary is closed: `interjection-unauthorized`,
  `interjection-source-denied`, `interjection-cold`,
  `interjection-rate-limited`, `interjection-too-long`, `interjection-empty`,
  plus `no-voice-seam` when no interjection route was wired at all. The same
  word appears on the journal as the drop reason — one vocabulary, three
  readers.
- **Only the composition root's failures become blocks; the engine's and the
  wire's are journal-only.** `agent embody` reports a *composition-level*
  failure twice — once as `[SENSE stage=embody …]` on stderr and once as a
  standalone `thinking` block whose single cue is `[drop] <source>: <reason>`
  and whose text is `[drop reason=… source=… …]`. New reasons from the
  two-tempo arc that reach the feed this way: `arm-failed` (the per-utterance
  arm could not be sent), `tail-cut-failed` and `cut-split-unavailable` (the
  client-side interruption path), `floor-push-failed` (a conversation item
  could not be pushed), and `clip-answer-unstructured` (the perception model's
  answer did not parse as the requested structure, so the layer degraded to a
  summary-only snapshot). The *engine's* own drops
  (`perception-sources-full`, `perception-stale`, `scope-park-full`,
  `summary-stale`, `summary-too-long`) and the *duplex wire's*
  (`one-shot-arming-unsupported`, `items-unsupported`, `item-invalid`,
  `item-queue-full`, `reseed-failed`, `playback-cancelled`,
  `voice-prompt-invalid`) are named on the journal only — do not build a
  consumer that expects them on this feed. Two of those journal-only reasons
  are the expected steady state against every gateway shipping today
  (`one-shot-arming-unsupported`, `items-unsupported`); see [the operating
  guide](operating-reachy.md#the-two-tempo-architecture--gemma-speaks-qwen-thinks).
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
- **`agent embody` opens `thinking.text` with its drain counts.** The first
  line of that producer's `text` is a bracketed, non-model annotation naming
  what the turn was built from — `[triggers=1 context=6 coalesced-from=187]` —
  in the same idiom as the `[drop reason=…]` markers the field already carries.
  It is what makes the coalescer auditable: `coalesced-from` is how many raw
  cues the `context` lines stand for, so a reader can tell a park that folded a
  flood from one that quietly threw it away. The same counts appear on the
  journal as `[SENSE stage=turn source=embody …] turn triggers=… context=…
  coalesced-from=…`. A consumer that treats `text` as pure model output should
  skip lines matching `^\[.*\]$`.

## Runtime Event Feed (`behavior engine run --export -`)

This is a **separate wire contract** from the cognition feed above. It carries
the deterministic `behavior` engine's OWN events — perception, rule decisions,
sustained intents, and motion admissions — produced by the 50 Hz
`reachy.behavior.engine` loop and its rule evaluator
(`reachy.behavior.rule_engine`), independent of any LLM.

**Decision c27** (the `symbolic-runtime-70` design): when an external mind
consumes the running engine's events (`reachy-mini-cli agent attach` — see
[`docs/operating-reachy.md`'s symbolic-runtime
chapter](operating-reachy.md#the-symbolic-runtime) — or `agent embody`, which
reads the same lines off this feed or off the MQTT bus), it publishes its OWN
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
| `name_mentioned`   | bool            | Whether the transcript engagement gate admitted an utterance BY NAME this tick (a one-tick event, `false` on a context-only admission) |
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
| `blocked_reason`   | string or `null` | When blocked: `"stillness"` / `"ownership"` / `"clock-gap"` / `"no-command"`; always `null` when available or unavailable |

The four blocked-reason values are: `"stillness"` (commanded pose not quiet
long enough), `"ownership"` (another behavior owns the head), `"clock-gap"`
(logical observation interval exceeded), and `"no-command"` (commanded pose
missing or malformed).

Example line:

```json
{"t":"sense","ts":1718362800.0,"tick":1,"doa":null,"speech":false,"rms":null,"pat":null,"face":null,"frame_available":false,"name_mentioned":false,"pat_state":{"availability":"unavailable","contact":false,"touch_type":null,"level":null,"yaw_deg":null,"phase":"idle","phase_started_at":null,"last_press_at":null}}
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

#### `"motion"` — a behavior admission/eviction, goto, or lock lifecycle

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
| `action`   | `"admit"` / `"evict"` / `"goto"` / `"face-lost"` / `"lock-released"` | What happened |
| `behavior` | string or `null`                  | The affected behavior name            |
| `channels` | array of string                   | Claimed/released channels             |
| `detail`   | object                            | Action-specific extras (e.g. a goto's target pose) |

The **face lock**'s lifecycle (`reachy.behavior.face_lock`) rides this same
block. A lock is an indefinite claim on the head taken on a mind's behalf, so
its whole life is on the wire:

| `action`         | When                                             | `detail`               |
|------------------|--------------------------------------------------|------------------------|
| `face-lost`      | The locked face has been absent/stale for 3 s — emitted **once** per disappearance, re-armed when the face returns. The lock **persists**: this is a report, not an ending. | `{id, absent_s}` |
| `lock-released`  | The lock ended, whichever way                    | `{id, reason}`         |

`detail.reason` is one of `requested` (an explicit `release_face`),
`mind-offline` (the mind's liveness read `false` for the whole grace period —
nobody is left to release it), `max-hold` (the 30-minute ceiling), or `evicted`
(the lock's behavior left the active set without the lock driver asking — a
`behavior stop face-lock`, or a `stop all`; the gaze is already gone, so the
lock state and its inhibitions follow it rather than outliving it).

Example lines:

```json
{"t":"motion","ts":1718362801.2,"tick":52,"action":"admit","behavior":"nod","channels":["head"],"detail":{}}
{"t":"motion","ts":1718362804.4,"tick":210,"action":"face-lost","behavior":"face-lock","channels":["head"],"detail":{"id":"face-lock:lock:1","absent_s":3.02}}
{"t":"motion","ts":1718362814.6,"tick":720,"action":"lock-released","behavior":"face-lock","channels":["head"],"detail":{"id":"face-lock:lock:1","reason":"mind-offline"}}
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
client is events-cli's own importable package — never `paho-mqtt` (or any other
MQTT library) imported here.

Since `events-cli>=0.9` (2026-07-24) that client is a **base dependency**, and
it is **adapted rather than driven directly**: the shipped
`events_cli.EventClient` spells three of those names differently
(`is_connected`, `close`, and a constructor-time Last Will with no
post-construction setter). `reachy/export/events_client.py` is the one module
that knows the difference — it presents the surface above and defers building
the vendor client until `connect()`, the only moment at which the will is known.
So a future events-cli API change costs one file, and the publisher's contract
here is unaffected. Note the degradation is **quiet by design**: a wrong binding
is a `client-incompatible` drop, not a crash, so only a check against a real
broker proves the binding is right — a fake shaped like this contract will
always agree with it.

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
| `motion`   | `admit`, `evict`, `goto`, `face-lost`, `lock-released`     |

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

**A key is published only when its value CHANGED** (issue #126). The engine
rewrites `state.json` every tick, so an ungated mirror republished the whole
tree at 50 Hz — measured live at 520 messages per key per 10 s, of which
`compose_hz` had exactly one distinct value. A retained topic *is* a last
value, so re-sending an identical one is a no-op with a real cost: the broker
persists retained messages, and every subscriber wakes for each one. Two
deliberate exceptions keep that gate honest:

- **`updated` rides along; it never triggers.** It is a per-tick timestamp, so
  a plain per-key gate would let it alone republish at full tick rate and
  defeat the purpose. It is therefore published only when some other key also
  changed — which gives the retained topic a sharper meaning than it had on
  disk: `reachy/state/updated` is *when the state last changed*. Liveness is
  not this topic's job; that is `reachy/state/online` plus the Last Will.
- **Every (re)connect republishes the whole tree, unconditionally.** A fresh
  broker session holds none of our retained values, so "unchanged since I last
  published" is the wrong question there — a late subscriber would otherwise be
  served an empty tree. The first publish of a session is likewise never
  suppressed: a gate that suppresses repeats must not suppress the original.

Consumers need no change either way — this only removes duplicate deliveries of
a value the topic already held.

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
