# Live on the robot — the tee wire verified, and a dead clip leg found

Run 2026-08-02 09:47–09:55 IDT against the **deployed, running** robot, before
the planned live-test session (plan tasks t14/t15). Two results: the audio tee's
wire is confirmed end to end against a real producer, and the clip rider is
found to have been silently dead since it deployed.

## Why this could be run early

`reachy-runtime.service` is an **editable** install
(`_editable_impl_reachy_mini_cli.pth` → `/home/spark/git/reachy-mini-cli`), so
the live runtime executes this checkout directly. The process restarted at
**08:08:22** on 2026-08-02, after t4 (audio tee) and t5 (clip rider) merged, so
both additive legs were already live in the robot's own runtime process.

That is worth stating on its own, because it is an operational property of this
box that is easy to forget: **a `systemctl --user restart reachy-runtime` picks
up whatever is on disk in this working tree.** Merging into this branch changes
what the robot runs on its next restart.

## 1. The audio tee's wire — CONFIRMED against a real producer

The t4/t6 wire had never met a real producer. Both ends' tests passed against
each other, which is the exact condition under which two ends of one contract
can agree with themselves and disagree with reality — the failure this arc
already hit once (writer emitted JSON-header float32; reader assumed headerless
int16; and separately, the two resolved *different socket paths*).

So the layer's **own consumer** (`reachy.embody.media.build_media().source`, the
robot profile) was pointed at the **live runtime's** socket. Passive read only —
no actuation, no writes, one 6-second sample:

```text
source sample_rate=16000
first chunk: dtype=float32 shape=(256,) min=-0.0095 max=0.0081
chunks=299 samples=96256 peak_abs=1.0000 elapsed=6.0s
implied rate=16016 Hz vs declared 16000 Hz
amplitude in [-1,1]: True
```

Five separate assumptions confirmed at once:

| assumption | result |
|---|---|
| both ends resolve the SAME socket path | connected — the second t4/t6 defect is closed in reality, not just in a test |
| header parses; stream/version/format understood | yes — `sample_rate` read back as 16000 |
| samples are mono `float32` | `dtype=float32`, `shape=(256,)` — 1-D, so `to_mono` had nothing to flatten |
| the DECLARED rate is the REAL rate | 16016 Hz measured vs 16000 declared — **0.1 %** |
| amplitude honours the `[-1, 1]` contract | `peak_abs = 1.0000`, never exceeded |

The rate check is the one that matters most: a wrong declared rate is precisely
what mis-times a server-side VAD, and it is invisible until speech comes back
wrong. It was previously inferred from the SDK's documentation; it is now
measured against the robot.

One honest note: `peak_abs` is exactly `1.0000`, i.e. at least one sample sat at
full scale during the window. That is *within* contract, but it is also what
clipping looks like. Not investigated here; worth a glance during t14 if the
gateway's transcription quality disappoints.

The runtime stayed healthy throughout (`state.json` heartbeat age 0.0 s,
`reachy-runtime.service` and `reachy-daemon.service` both `active`), which is
also the first live confirmation that the tee's per-consumer, drop-don't-block
fan-out does not disturb the producer.

## 2. The clip rider — DEAD since it deployed, now fixed

`state.json` reported the rider composed but permanently empty:

```json
"clip": {"available": false, "reason": "no-clip-yet", "path": null,
         "ts": null, "duration_s": null, "frame_count": null}
```

The journal showed why, once every 5 seconds for ~1.7 hours:

```text
Aug 02 08:31:44 [SENSE stage=vision source=clip event=clip] dropped reason=encode-refused
Aug 02 08:31:49 [SENSE stage=vision source=clip event=clip] dropped reason=encode-refused
```

`cv2` was present in the deployed interpreter, `frame_available` was `true`, and
no clip file existed anywhere.

### Root cause

`cv2.VideoWriter` selects its container muxer from the filename's **suffix**.
The rider encoded into a temp sibling named `clip.mp4.tmp` — the repo's standard
temp-then-`os.replace` idiom — whose suffix is `.tmp`, matching no known video
format. OpenCV falls back to its image-sequence backend, fails an assertion, and
returns `isOpened() == False`. Measured in the deployed environment (OpenCV
4.13.0, `mp4v`, 640×480):

| path | `isOpened()` | bytes |
|---|---|---|
| `clip.mp4` | True | 1710181 |
| `clip.mp4.tmp` | **False** | — |
| `clip.tmp` | **False** | — |
| `clip.mp4.partial` | **False** | — |

The fourcc was never the problem — `mp4v` writes fine. Only the name was wrong.

### Why no test caught it, and what now does

Every unit test injects a fake encoder (`encode(frames, fps, path) -> bool`), so
the real `VideoWriter` never saw the `.tmp` path; and CI has no `cv2` at all, by
design, since `[vision]` is an optional extra. **No amount of unit testing could
have found this** — it needed the robot.

The fix keeps the atomic write (which is right, and is why a reader never sees a
half-written clip) and only moves the marker before the extension:
`clip.mp4.tmp` → `clip.tmp.mp4`, same directory so `os.replace` stays atomic.

The regression test pins the *invariant* rather than the string, and needs no
`cv2`: the temp path's suffix must equal the real clip's, so any
extension-sniffing encoder behaves identically on both.

Verified with the REAL encoder in the deployed interpreter after the fix:

```text
encoder built: True
final: clip.mp4 | tmp: clip.tmp.mp4
encode returned: True
tmp exists: True bytes: 3320040
after replace -> clip bytes: 3320040
```

### What the design got right

The leg failed **loudly by name**. `senselog.drop(..., "encode-refused")` every
5 s is exactly what made a ten-minute diagnosis possible, versus an unbounded
hunt for why a feature "didn't seem to work". The rule that a drop always names
its reason paid for itself here.

## What this does NOT establish

- The fix is verified by calling the encoder directly, **not** by observing the
  live rider produce a clip. The running runtime still holds the pre-fix code;
  confirming end to end needs a `systemctl --user restart reachy-runtime` and is
  deliberately left to the t15 session rather than done unannounced to a robot
  in use.
- Nothing here tests whether a written clip is actually **consumable by the
  worker model** as a `video_url` part. That is t14's job.
- The tee sample is 6 seconds on one boot. It says nothing about behaviour over
  hours, under a wedged consumer, or about the tick-budget cost of the tee leg
  (h10) — all t15.
- No audio was played, no motion commanded, no rule fired. This was a read.
