# reachy-mini-cli can export its thinking, message, and emotion blocks as a live newline-delimited JSON event feed on stdout, so an external display like the reTerminal can render them — with no reTerminal-specific code or dependency in the repo.

> reachy-mini-cli can export its thinking, message, and emotion blocks as a live newline-delimited JSON event feed on stdout, so an external display like the reTerminal can render them — with no reTerminal-specific code or dependency in the repo.

## Audience

- An operator wiring Reachy Mini to an external display (the installed reTerminal), and the separate, out-of-repo renderer app that consumes the feed.

## Before → After

- Before: Today the think loop drives the robot's voice and body, but its thinking/message/emotion events stay in-process — nothing structured leaves the CLI for an external screen.
- After: Running 'reachy think run --export -' emits a JSONL event stream (thinking / message / emotion blocks) on stdout that any consumer can pipe to a display; the display app and its hardware stay entirely outside this repo.

## Why it matters

- You can SEE what the robot is thinking, saying, and feeling on a screen without coupling the repo to any specific display hardware: the feed is a documented contract, the renderer is swappable, and the robot's behavior is unchanged.

## Requirements

- Events are newline-delimited JSON, one object per line, each with a stable 't' (block type: thinking|message|emotion), a 'ts' timestamp, and a type-specific payload — emotion:{emoji,pose}, message:{text}, thinking:{cues:[...],text}. The schema is documented in-repo as the export contract.
  - honesty: A documented JSON schema exists in-repo; every emitted line is valid JSON that validates against it; a malformed/partial line never appears in the stream (events are emitted only on fully-closed marker/speech spans and complete turns).
- The operator can export a SELECTABLE SUBSET of blocks via '--export-blocks <csv>' (default: all three). An invalid/empty selection is a clean CliError with a hint, not a crash.
  - honesty: '--export-blocks thinking,message,emotion' and every subset parse and filter correctly; an empty or unknown token yields exit-1 CliError with a 'hint:' line, never a traceback.
- Export is a PASSIVE tap: a failing or closed sink (e.g. broken pipe when the consumer disconnects) degrades silently and never kills the cognition loop or alters robot motion/voice — same posture as motion errors.
  - honesty: A unit test proves a sink raising on write (broken pipe / closed consumer) is caught, logged to stderr at most once, and the cognition loop continues and the robot keeps moving.
- With '--export -', the JSONL feed is the command's ONLY stdout output and every diagnostic still goes to stderr, so the stream stays machine-parseable (honors the repo's results->stdout / diagnostics->stderr contract).
  - honesty: With '--export -' a test asserts stdout contains ONLY newline-delimited JSON (every line json.loads-able) and all human/diagnostic text is on stderr.

## Honesty conditions

- After 'pip install reachy-mini-cli' (no [reterminal]/display extra — none exists), 'reachy think run --export -' produces the feed; grep of the repo finds zero 'reterminal' imports/deps; the renderer lives entirely outside this repo.
- A consumer needs ONLY the documented JSONL schema — no Python import from reachy-mini-cli — to render the feed; the renderer is fully decoupled.
- A live 'think run --export -' streams thinking/message/emotion events line-by-line in real time (flushed per event, not buffered to end-of-run), so a downstream pipe sees them as they happen.
- Confirmed by inspection: no current code path emits structured thinking/message/emotion outside the process — MarkerEvent/SpeechEvent/SenseCue stay in-process and grep finds no JSONL export today.
- With export on, cognition + motion timing is unchanged vs. off — the export tap adds no blocking work on the cognition/motion threads (write is best-effort, non-blocking-enough).
- grep of the repo after the change finds no 'reterminal' dependency and no new network/transport module — the only new output path is a stdout JSONL writer.
- An automated test runs 'think run --export -' with a stubbed engine and asserts valid JSONL with all three block types on stdout, and that '--export-blocks' filters to the requested subset.
- There is a tap point that captures the raw LLM turn text before MarkerParser discards non-marker/non-speech text, OR the team agrees thinking.text = concatenated raw stream; confirmed before build.

## Success signals

- 'reachy think run --export -' prints valid JSONL — one object per thinking/message/emotion block — to stdout while the robot runs normally; '--export-blocks thinking,message' selects a subset; a broken/closed sink never crashes the loop; no new base dependency (stdlib json only).

## Scope / boundaries

- The repo does NOT ship a reTerminal renderer/UI, does NOT depend on reTerminal, and adds NO network transport for this milestone — export is a stdout JSONL pipe. Getting bytes onto the device (ssh/socat/the reTerminal's own collector) is the operator's job.

## Non-goals

- Not a transcription, telemetry, or logging system; not persistent storage. The feed is an ephemeral live stream — replay/history is the consumer's concern, not the CLI's.

## Assumptions

- 'Raw thought text' for the thinking block is the raw LLM turn output (pre-marker-parse), i.e. the full inner monologue incl. stage directions — which the MarkerParser currently DISCARDS for non-marker/non-speech text; export must tap the stream before that discard.

## Decisions

- Transport for this milestone is stdout JSONL only ('--export -', '-' = stdout per Unix convention). HTTP-push / file-FIFO / SSE sinks are explicitly out of scope for v1, designed-for later via the same --export <target> surface.
- Export is a '--export <target>' sink option on the producer 'think run' (NOT a new noun); the same option pattern can later extend to other producers.
- The 'thinking' block carries the sense cues that seeded the turn PLUS the raw thought text; 'message' = spoken text; 'emotion' = emoji + resolved expression pose.

## Open / follow-up

- HTTP-push / file-FIFO / SSE export sinks for cross-machine or multi-display delivery (this milestone is stdout-only).
- Extending '--export' to other producer nouns (say / sleep / pat / listen) beyond think run.
- Whether the reTerminal renderer app should live in its own sibling repo and whether this repo documents/links it.
