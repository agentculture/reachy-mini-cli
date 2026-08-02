# t8 — attributing the sustained tick-budget overrun (#137)

**Date:** 2026-08-02 · **Box:** the deployed robot (`spark-f8a9`) · **Method:** a
natural experiment in the live journal, no intervention required.

## Result

The sustained overrun streak is caused by **camera frame processing on the tick
thread**, and specifically by `FaceSenseDriver._update_frame` reading
`media.frame()` **once per tick (50 Hz)** while no consumer of those frames needs
faster than 8 fps. The cause is client-side, so the fix ships here (issue #145).

## The measurement

No probe was needed: the runtime process that has been up since 10:17:25 today
recorded both conditions itself, with nothing else changing between them.

| event | time | source |
|---|---|---|
| runtime `pid 1839808` starts | 10:17:25 | `systemctl --user show reachy-runtime -p ActiveEnterTimestamp` |
| overrun streak **ends** — `count=31090 mean_ms=21.06 max_ms=28.73` | **10:30:59** | runtime journal, `event=overrun-summary` |
| camera **dies** — last clip ever written | **10:31:04** | `clip.mp4` mtime |
| … 7 h later, still no clip; `state.json` fresh | 17:24 | filesystem |

**The streak ended five seconds before the camera stopped producing frames, in
the same process, with no restart and no configuration change.** From 10:31
onward the tick has been healthy: 43 overruns for the whole day, every one an
isolated `count=1` event of ~27–28 ms, several hours apart — no streak at all.

Two earlier streaks the same day show the steady-state figure is stable across
processes:

```text
08:04:45  pid 3238      count=1828654  mean_ms=20.98  max_ms=141.33
08:32:18  pid 1495987   count=53707    mean_ms=21.13  max_ms=103.18
10:30:59  pid 1839808   count=31090    mean_ms=21.06  max_ms=28.73
```

So the sustained condition is ~5 % over a 20 ms budget (≈47.5 Hz achieved rather
than 50 Hz), present whenever frames flow and absent when they do not — matching
#137's own camera-alive/camera-dead table exactly, now with the transition
captured *within a single process* rather than across restarts.

### A methodological note worth keeping

A first pass counted `event=overrun]` lines in the camera-alive window and found
**zero**, which reads as a refutation. It is an artifact: streak logging emits
one `event=overrun]` at the *start* of a streak and one `overrun-summary` at the
*end*, deliberately collapsing 31,090 events into two lines. Counting the start
marker measures how many streaks began, not how many ticks overran. The
summary line is the one that carries the population.

## Why the mechanism fits

`reachy/behavior/face_sense.py::_update_frame` calls `media.frame()` on every
tick. Every downstream consumer needs far less:

| consumer | cadence actually needed |
|---|---|
| the rolling clip (`clip_rider`, PUSH via `add_frame_sink`) | `DEFAULT_ENCODE_FPS` = 8 fps |
| face detection / recognition (already off-thread) | `DEFAULT_DETECT_INTERVAL` = 0.5 s → 2 fps |
| the `frame_available` condition | TTL-held (`DEFAULT_FRAME_TTL_S` = 1.0 s) — designed not to pulse |

`DEFAULT_DETECT_INTERVAL` throttles the recognition *worker*, not the read. The
read itself had no interval, so it ran 6×–25× faster than anything consuming it.
An unthrottled per-tick IPC read is a mechanism that produces a small *constant*
per-tick cost — which is the shape observed (a sustained mean 1.06 ms over
budget), rather than the spikes a heavier but rarer operation would produce.

The heavy YuNet/SFace legs are already correctly off-thread and are **not**
implicated; this is the cheap-looking read beside them.

## The fix

Gate the read on `DEFAULT_FRAME_INTERVAL_S = 0.1` (10 Hz — above the fastest real
consumer at 8 fps, and well inside the 1.0 s `frame_available` TTL so the
condition cannot flap or go stale). The interval is injectable like every other
cadence in the module, so tests pin it without wall-clock.

## Relationship to the other findings

- **#138** — the camera being dead since 10:31 is why the tick looks healthy
  right now. The two issues are one phenomenon seen from opposite sides, and the
  camera death is what made this attribution possible at all.
- **#144** — the daemon logging every HTTP request at INFO (3.26 M journald
  lines/day, ~59 `set_target` POSTs/s from this same runtime) is a *separate*
  candidate on the motion leg. It is not implicated by this measurement: the
  motion POSTs continued unchanged across 10:30:59 while the streak stopped.
- **#145** — the read-cadence issue this evidence closes.
