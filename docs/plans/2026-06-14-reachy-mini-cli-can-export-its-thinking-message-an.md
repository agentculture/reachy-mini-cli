# Build Plan — reachy-mini-cli can export its thinking, message, and emotion blocks as a live newline-delimited JSON event feed on stdout, so an external display like the reTerminal can render them — with no reTerminal-specific code or dependency in the repo.

slug: `reachy-mini-cli-can-export-its-thinking-message-an` · status: `exported` · from frame: `reachy-mini-cli-can-export-its-thinking-message-an`

> reachy-mini-cli can export its thinking, message, and emotion blocks as a live newline-delimited JSON event feed on stdout, so an external display like the reTerminal can render them — with no reTerminal-specific code or dependency in the repo.

## Tasks

### t1 — Export event model + JSONL schema doc (reachy/export/events.py + docs/export-schema.md)

- covers: c8, h1, c2, h7
- acceptance:
  - ThinkingEvent/MessageEvent/EmotionEvent serialize via to_jsonl() to a single-line JSON string with stable keys t, ts and a type-specific payload (emotion={emoji,pose}, message={text}, thinking={cues,text}); json.loads round-trips each.
  - to_jsonl() output contains no embedded newline (one object per line) and uses stdlib json only (no new dependency).
  - docs/export-schema.md documents every field of all three event types; a test asserts the doc lists each event t-value and its required keys, so a consumer needs only the doc (no python import).

### t2 — Block-selection parser (reachy/export/blocks.py)

- covers: c9, h2
- acceptance:
  - parse_blocks('thinking,message,emotion') and every subset return the correct set; an unknown token or empty string raises CliError exit-1 with a hint line, never a traceback.
  - Selection.allows(event_type) returns True only for selected block types.

### t3 — JSONL stdout exporter: per-event flush, broken-pipe-safe, block-filtered (reachy/export/exporter.py)

- depends on: t1, t2
- covers: c10, c11, h3, h4, h8, h10
- acceptance:
  - JsonlExporter writes one JSONL line per emitted event to its stream and flushes after each line; a test captures real-time per-event flush.
  - emit_thinking/message/emotion filter by the block selection and write nothing for unselected types.
  - a stream raising BrokenPipeError/OSError on write is swallowed; emit_* never raises, logs to stderr at most once, and later emits become no-ops (passive tap).
  - with a stdout stream a test asserts every written line is json.loads-able and only JSONL reaches stdout; each emit does a single non-blocking write+flush with no sleeps or held locks.

### t4 — Cognition export hook + raw-thought tap (reachy/speech/cognition.py)

- depends on: t1
- covers: c3, h8, c5
- acceptance:
  - CognitionEngine accepts an optional export callback; during a turn it invokes it with an EmotionEvent per MarkerEvent, a MessageEvent per SpeechEvent, and a ThinkingEvent carrying the turn sense cues plus the raw LLM turn text captured before the MarkerParser discard.
  - thinking.text equals the concatenated raw LLM stream for the turn including prose outside the marker/quote delimiters, proven by a test feeding inter-marker text.
  - with no export callback the engine output and timing are unchanged versus before (byte-identical regression test) so robot behavior is unaffected.

### t5 — Wire --export / --export-blocks into 'think run' (reachy/cli/_commands/think.py)

- depends on: t3, t4
- covers: c1, c3, c7, h6, h12
- acceptance:
  - 'think run --export -' builds a stdout JsonlExporter from the block selection and passes it to the engine; an integration test with a stubbed engine asserts valid JSONL with all three block types on stdout.
  - '--export-blocks thinking,message' filters the stream to those types; an invalid value exits 1 with a CliError hint.
  - without --export, 'think run' emits no JSONL on stdout and its behavior is unchanged.

### t6 — Docs + reTerminal-decoupling guard (README.md, CLAUDE.md, guard test)

- depends on: t5
- covers: c4, c6, h9, h11, c2, c5, c1, h6, h7
- acceptance:
  - README and CLAUDE.md document 'think run --export -' and '--export-blocks' and link docs/export-schema.md; markdownlint passes.
  - a test greps reachy/ and pyproject.toml and asserts zero 'reterminal' references, no new network/transport module, and base runtime deps remain numpy-only.
  - a test asserts the only structured thinking/message/emotion export path is the new reachy/export package plus the think wiring (no other module emits these events).
