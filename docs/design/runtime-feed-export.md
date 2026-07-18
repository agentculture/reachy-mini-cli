# Runtime Feed Export — Issue #78

> The reTerminal e-paper panel has no feed since the runtime flip: the boot
> unit `reachy-runtime.service` runs `behavior engine run` with no `--export`
> flag, so the deterministic runtime's JSONL event stream (sense/rule/intent/
> motion blocks) is never delivered to a file or pipe the panel can consume.
> This note designs a path to export that feed without hanging boot, breaking
> stdout purity, or violating the single-presence invariant.

## Problem

Before the runtime flip (issue #70), the boot presence was the folded live
loop (`listen run --live --transcribe --cognition agent --voice-engine
harmonic`). That loop carried `--export -` which piped a cognition JSONL feed
(thinking/message/emotion) to the reTerminal e-paper panel. After the flip,
the boot unit is `reachy-runtime.service` whose `ExecStart` is:

```bash
<python> -m reachy behavior engine run
```

No `--export` flag. The runtime *can* export a runtime-events JSONL feed
(sense/rule/intent/motion blocks — see `reachy/export/runtime.py` and
`docs/export-schema.md`), but both builders in `reachy/cli/_export.py`
(`build_export_hook` and `build_runtime_export_consumer`) refuse any `--export`
target except `-` (stdout). The boot unit deliberately carries no `--export`
flag, so the panel has nothing to tail.

The panel is dark. We need a way to get the runtime feed to a file or named
pipe the panel can consume, without breaking boot or the existing contracts.

## Constraints

These are verified findings from the codebase and deployment configuration.
Any candidate must satisfy all of them.

### C1 — FIFO `open()` blocks at boot; naive named-FIFO export is forbidden

A named FIFO opened for writing with `open(path, "w")` blocks until a reader
opens the other end. If the panel process has not started yet (or crashes
during boot), the runtime's `--export` target open blocks, and the boot unit
hangs. systemd sees the unit stuck and, under `Restart=on-failure`, may
restart it — creating a crash loop.

Any FIFO candidate **must** use `O_NONBLOCK` open semantics: `open()` returns
immediately with `ENXIO` (no reader) rather than blocking. The exporter must
self-disable on `ENXIO` and retry periodically.

### C2 — Reuse `JsonlExporter`'s disconnect-safe self-disable pattern

`reachy/export/exporter.py` `JsonlExporter` already implements the pattern:
catch `BrokenPipeError`/`OSError`/`ValueError` on write, log once to stderr,
set `_broken = True`, and make all subsequent `emit()` calls no-ops. Any
candidate must reuse this class (via its injectable `serialize` parameter)
rather than duplicating the logic.

### C3 — Stdout purity

When exporting to stdout, the feed is pure JSONL on stdout and all
diagnostics go to stderr. A file or FIFO sink must not break this invariant:
diagnostics (including the self-disable warning) must still go to stderr, and
the feed must not leak into stdout when the sink is a file or FIFO.

### C4 — Single-presence: no second engine process

The behavior engine owns motion exclusively — the single-SDK-owner model
means two `behavior engine run` processes cannot coexist against the one SDK
client. A candidate that spawns a second `behavior engine run --export -`
process to feed the panel violates this invariant. The export must come from
the *same* process that runs the engine.

### C5 — Deployed box: drop-in overrides the unit

On the deployed box, a machine-local systemd drop-in (`panel.conf`) overrides
the unit's `ExecStart`. Any new flag must land in the drop-in, not only in
the repository's unit text (`reachy/service/units.py`). The drop-in is the
authoritative source for what the deployed box runs.

### C6 — Runtime feed carries runtime events only

Per decision c27 of the symbolic-runtime spec, the runtime feed carries
runtime events only (sense/rule/intent/motion). Cognition blocks
(thinking/message/emotion) belong to an externally attached agent's own feed.
The export mechanism must not mix the two.

## Candidates

### Candidate A: `--export-file PATH` with O_NONBLOCK FIFO semantics

Add a new flag `--export-file PATH` to `behavior engine run` (and the
cognition feed commands for symmetry). The flag accepts a filesystem path
that may be a regular file or a named FIFO.

**FIFO semantics:**

1. Open the path with `os.open(path, os.O_WRONLY | os.O_CREAT | os.O_NONBLOCK, 0o666)`.
2. If `open()` raises `FileNotFoundError` or `OSError` with `errno.ENXIO`
   (FIFO with no reader), the exporter self-disables (logs once to stderr)
   and schedules a periodic retry (e.g. every 5 seconds) to re-open.
3. On successful open, wrap the fd in a `TextIOWrapper` and pass it to
   `JsonlExporter` with the appropriate `serialize` function.
4. `JsonlExporter`'s existing self-disable handles reader disconnect
   (`BrokenPipeError`/`EPIPE`).

**Pros:**

- Direct: the runtime process writes the feed itself — no second process
  (satisfies C4).
- Reuses `JsonlExporter`'s self-disable pattern (satisfies C2).
- O_NONBLOCK open avoids boot hang (satisfies C1).
- Works with both regular files and named FIFOs — the panel can `tail -f`
  a file or read a FIFO.
- Stdout purity preserved: the feed goes to the file/FIFO, diagnostics to
  stderr (satisfies C3).
- The flag lands in the drop-in `ExecStart` override (satisfies C5).
- Runtime-only feed stays runtime-only (satisfies C6).

**Cons:**

- New CLI flag surface on `behavior engine run` (and symmetrically on the
  cognition commands).
- Periodic retry adds a tiny background check (one `os.open` every 5 seconds
  when no reader is present — negligible cost).
- File sink needs rotation or size caps to avoid unbounded growth (see
  Candidate B for the rotation design).

### Candidate B: Plain-file append with rotation and size caps

A variant of Candidate A where the path is always a regular file. The
exporter opens the file in append mode and writes JSONL lines. A rotation
policy (e.g. 1 MB max, keep 3 rotated copies) prevents unbounded growth.

**Pros:**

- Simpler than FIFO: no `O_NONBLOCK`/`ENXIO` dance, no periodic retry.
- The panel can `tail -f` the file.
- Reuses `JsonlExporter` (satisfies C2).
- Stdout purity preserved (satisfies C3).
- No boot hang risk (satisfies C1 — regular file open never blocks).

**Cons:**

- No self-healing: if the panel process dies and restarts, it must seek to
  the end of the file (or the file must be truncated). A FIFO reconnects
  cleanly on the next open.
- Rotation logic is new code (not in `JsonlExporter` today).
- Less elegant than a FIFO for a "live feed" use case — the file accumulates
  history the panel may not need.

### Candidate C: Side-channel reader process tailing journald

Do not touch the runtime unit at all. Instead, run a small sidecar process
(e.g. a systemd service or a shell script) that tails
`journalctl -u reachy-runtime -f` and filters JSONL lines from the runtime's
log output, writing them to a named FIFO or file for the panel.

**Pros:**

- Zero changes to the runtime unit or CLI — the boot unit stays bare.
- No boot-hang risk (the sidecar starts after the runtime).
- The runtime process is untouched, so no single-presence concern.

**Cons:**

- Fragile: relies on parsing `journalctl` output, which is not a stable API
  and may include interleaved log lines from other sources.
- Latency: `journalctl -f` has non-trivial buffering and polling delay
  compared to a direct pipe.
- The runtime would need to emit the JSONL feed to its stdout *and* have
  journald capture it — but the current runtime unit has no `--export` flag,
  so there is nothing to tail. This candidate requires adding `--export -`
  to the unit anyway, which brings us back to the same problem.
- Does not satisfy C5 cleanly: if the unit *does* get `--export -`, its
  stdout goes to journald, but the panel still needs a reader to prevent
  back-pressure issues.

## Recommendation

**Candidate A** (`--export-file PATH` with O_NONBLOCK FIFO semantics) is the
working favorite. It satisfies all six constraints directly, reuses existing
infrastructure (`JsonlExporter`), and provides the cleanest integration
between the runtime process and the panel consumer.

The design is:

1. Add `--export-file PATH` to `behavior engine run` (and symmetrically to
   `think run` / `listen run --live` for consistency).
2. The path resolver distinguishes FIFO from regular file via `stat()`.
3. FIFO: open with `O_NONBLOCK`; on `ENXIO`, self-disable + periodic retry.
4. Regular file: open in append mode with rotation caps.
5. Both paths use `JsonlExporter` with the appropriate `serialize` function.
6. The deployed box's `panel.conf` drop-in adds `--export-file
   /var/run/reachy/runtime-feed.fifo` to the `ExecStart` override.
7. The panel process opens the FIFO for reading; if it restarts, the
   runtime's periodic retry re-connects.

This is a **proposal**, not an implementation plan. The concrete mechanics
(retry interval, rotation policy, exact flag registration) belong in a
follow-up devague think/challenge pass.

## Status

**This is design only.** No implementation is included. Implementation is
deferred to its own devague think/challenge pass. The design addresses
GitHub issue [#78](https://github.com/AgentCulture/reachy-mini-cli/issues/78).
