# Reachy Mini CLI — Export Feed Schema

This document is the **authoritative contract** for external consumers of the
`reachy-mini-cli` export feed (e.g. a reTerminal renderer, a logging pipeline,
or any downstream tool). You need only this document — no Python import from
the package is required to implement a compatible reader.

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
