# Probe — video wire format over the deployed lobes gateway (t2)

Task `t2` of `docs/plans/2026-08-01-embodiment-layer.md`: does sending a real
short clip as a video content part to `model=worker` over the deployed
gateway's `/v1/chat/completions` (streaming) actually work, and if so, what is
the exact wire shape? This is a probe producing evidence, not a feature — no
library code was written; everything below runs through the **existing**
`reachy.speech.llm` client (`stream_chat_completion`), unmodified.

- Date: 2026-08-01
- Gateway: `http://localhost:8001` (`REACHY_OPENAI_URL_BASE`, already set in
  the shell environment on this box)
- Model requested: **`"worker"`** — the role name, resolved server-side by
  `resolve_model`, never a hardcoded served id (per issue #132)
- `GET /capabilities` at probe time: `worker` role = `unsloth/Qwen3.6-35B-A3B-NVFP4`,
  hosted on Thor (proxied), responsibilities include `image_understanding` and
  `video_understanding`
- Client: `reachy.speech.llm.stream_chat_completion` (this repo's pure-stdlib,
  streaming, OpenAI-compatible chat-completions client) — reused as-is, `model="worker"`,
  `timeout=300.0` (generous, per the task's cold-load warning; see
  [Timeouts / cold-load](#timeouts--cold-load) below for what was actually observed)

## VERDICT

**YES — video content parts work through the relay, streamed, with a
recognizably correct description, and the wire shape is exactly OpenAI's
`video_url` content part carrying a base64 data URI.**

**Working shape, one line:**
`{"type": "video_url", "video_url": {"url": "data:video/mp4;base64,<...>"}}` inside a
`messages[].content` list, alongside a `{"type": "text", ...}` part, with `"model": "worker"` and `"stream": true`.

The clip pipeline is **not gated off** by this probe — t5/t10 clip work may proceed.

## Method — building a real, checkable clip

No `ffmpeg` binary and no `sudo` were available on this box directly (only
`libavcodec`/`libavformat` *library* packages are installed, not the CLI, and
`apt-get install ffmpeg` needs a password this session does not have). Network
egress to PyPI was available, so a static `ffmpeg` binary was obtained via
`pip install imageio-ffmpeg` into a **throwaway venv outside the repo**
(`/tmp/.../scratchpad/ffmpeg_venv`) — not added to `pyproject.toml`, not a
runtime dependency of `reachy-mini-cli`, used only to generate the probe's
test artifact.

The clip is a **synthesized moving shape** (the task's explicitly-sanctioned
fallback when no camera clip is at hand): a 140×140 px solid red square
translating across a 480×360 white canvas over 24 frames (3 s @ 8 fps), built
frame-by-frame with Pillow (`ImageDraw.rectangle`, system `python3`, not added
to this repo either) and muxed with `ffmpeg -framerate 8 -i frame_%03d.png
-c:v libx264 -crf 20 -movflags +faststart`. Two clips were built — **forward**
(left-to-right) and a **reversed control** (right-to-left) — so a correct
direction answer cannot be a coin-flip prior; genuine temporal decoding must
flip its answer between the two. This mirrors the methodology the sibling
`embodiment` repo's `docs/live-test-results/video-perception-probe.md` and
`lobes-cli`'s own `docs/evidence/2026-07-31-accept-worker-thor.txt` (worker's
own acceptance run, real webcam footage) already used successfully against
this exact gateway/model — see [Prior evidence](#prior-evidence-this-probe-independently-reproduces) below.

## A wrong result, caught and corrected (kept in, not hidden)

The first two clips built used ffmpeg's `drawbox` filter directly with a
time-varying position expression: `drawbox=x='(iw-60)*t/4':...:t=fill`. That
expression is broken — `t` is **also** `drawbox`'s own `thickness` option
name, so `t=fill` at the end of the filter string collides with the `t`
*variable* used inside the `x=` expression. A `-vf "fps=2,tile=6x1"` contact
sheet of the resulting file showed the bug immediately: **every frame was
solid black** — the box never rendered at all.

Sent to the model anyway (before the contact-sheet check caught it), those
broken clips produced exactly the confused, inconsistent answers a genuinely
blank/near-blank video should produce: `"A black circle moves from left to
right."`, then on the *reversed* clip (should have flipped) `"A black circle
moves from left to right across the frame."` again, then on a re-run of the
*forward* clip, `"A black square is stationary."` One high-quality re-encode
(bigger box, lower crf) even got an honest `"The video is completely blank...
no visible shape, color, or movement."` — which, given the contact sheet, was
**correct**: that video *was* blank.

This was a **test-artifact defect**, not a wire-format or model failure — the
same shape lesson this repo's CLAUDE.md already documents elsewhere in this
plan's neighborhood: verify the transport before generalizing about a
capability. The fix was switching from `drawbox` expressions to rendering
frames directly with Pillow (full control, no filter-expression ambiguity)
and re-muxing. All results below are from the corrected clips, verified by an
`ffmpeg -vf "fps=2,tile=6x1"` contact sheet showing the square visibly at six
different x-positions before any clip was sent to the model.

## Request shapes tried

| # | Shape | Content-part(s) | Result |
|---|---|---|---|
| 1 | `video_url` data URI, MP4 (h264, honest `video/mp4` MIME) | one `text` + one `video_url` | **WORKS** — correct shape, color, direction; flips correctly on the reversed control |
| 2 | `video_url` data URI, GIF (honest `image/gif` MIME, *inside* `video_url`) | one `text` + one `video_url` | **WORKS** — same accuracy, larger payload (166 KB vs 5 KB for the same 24 frames) |
| 3 | Frame sequence as N separate `image_url` parts (4 stills at even spacing) | one `text` + 4× `image_url` | **WORKS** — correctly reads direction from the sequence, but needs the caller to pre-sample frames (more client-side work for no accuracy gain here) |
| 4 | Single `image_url` still (one frame, no sequence) | one `text` + one `image_url` | Works as a **control**, not a video test — correctly describes the static shape/color and (correctly) makes no motion claim |
| 5 | Animated GIF sent as a single `image_url` part (not `video_url`) | one `text` + one `image_url` | **Flattens to one frame** — no motion detected, confirming `video_url` (not the container format) is what selects the multi-frame decode path |

No other content-part shape was tried — shapes 1–3 all worked on the first
attempt each, so there was no failure streak to chase down alternates for
(per the task's "3 failures of the same kind, then stop" guidance; here it
was 0 failures of the target shape).

## The exact working shape

```json
{
  "model": "worker",
  "stream": true,
  "temperature": 0.2,
  "max_tokens": 200,
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "You are shown a short video clip. In one sentence, describe: the shape, its color, and the direction it moves (left-to-right, right-to-left, up-down, or stationary)."
        },
        {
          "type": "video_url",
          "video_url": {
            "url": "data:video/mp4;base64,<...base64-encoded mp4 bytes...>"
          }
        }
      ]
    }
  ]
}
```

Built and sent via the existing client, unmodified:

```python
from reachy.speech import llm

messages = [{"role": "user", "content": [
    {"type": "text", "text": "..."},
    {"type": "video_url", "video_url": {"url": f"data:video/mp4;base64,{clip_b64}"}},
]}]

for delta in llm.stream_chat_completion(messages, model="worker", max_tokens=200,
                                         temperature=0.2, timeout=300.0):
    ...  # deltas arrive incrementally, exactly as for a text-only request
```

No change to `llm.py`'s request-building path was needed — `messages` already
accepts an arbitrary `content` list per the OpenAI wire contract, and
`_build_request` serializes it verbatim.

## The model's actual response text

**Forward clip** (`clip_ltr_v2.mp4`, 4959 bytes, red square, left-to-right):

> "The shape is a rectangle, it is red in color, and it moves from left-to-right."

**Reversed control** (`clip_rtl_v2.mp4`, 4702 bytes, identical shape/color,
right-to-left):

> "The red square moves from right to left."

Both are **recognizably correct** — shape (square/rectangle), color (red),
and, critically, the direction **flips correctly between the two clips**,
which rules out a lucky prior (video description defaults would plausibly
guess "left-to-right" for both; it did not).

**GIF-as-`video_url`** (`clip_ltr_v2.gif`, 166801 bytes, same 24 frames):

> "A red square moves from left to right."

**4-frame sequence as `image_url` parts** (frames 0, 7, 15, 23 of the forward clip):

> "A red square moves from left to right across the frames."

**Single still, `image_url`, no sequence** (sanity control — must not claim motion):

> "The image shows a solid red square centered on a white background."

**Animated GIF as a single `image_url` part** (flattening control):

> "The image shows a red rectangular shape with a textured surface and a thin
> yellow-orange border along its top edge, appearing static with no
> indication of motion or direction."

## Limits discovered

- **Encoding.** Both `video/mp4` (h264, `yuv420p`, `+faststart`) and
  `image/gif` decoded correctly when sent as `video_url`. The **declared MIME
  is not authoritative** — consistent with the `embodiment` repo's prior
  finding (round 2/round 3 of their probe: a GIF mislabelled `video/mp4`
  decoded identically to one honestly labelled `image/gif`). Not re-tested
  here (no reason to lie about the MIME); noted for completeness since it
  affects what "the wire format" means — the **content part type**
  (`video_url` vs `image_url`) is what selects the decode path, not the data
  URI's declared MIME.
- **Size.** Every size tried worked: 2.1 KB (first, buggy clip), 4.7–5.0 KB
  (corrected MP4s), 166.8 KB (GIF). No upper bound was probed in this
  session — pushing toward a real limit would need a much longer/higher-res
  clip and was not necessary to answer the gate question. `lobes-cli`'s own
  worker acceptance evidence (`docs/evidence/2026-07-31-accept-worker-thor.txt`
  in that repo, external, not part of this one) independently reports a real
  78 KB webcam-captured MP4 producing an accurate, detailed multi-sentence
  scene description — corroborating that this is not a toy-clip-only result.
- **Frame count.** 24 frames (3 s @ 8 fps) worked cleanly and cheaply. Prompt
  token cost was not instrumented in this session's client output (the
  `reachy.speech.llm.stream_chat_completion` content-only reader does not
  surface `usage`); the sibling `embodiment` probe measured 86 prompt tokens
  for a 6-frame GIF via `video_url` versus 132 for the same content flattened
  through `image_url` — i.e. video decode is cheaper than a single still,
  not just cheaper than a frame sequence. Not re-measured here; flagged as
  consistent with, not independently re-derived from, this run.
- **Request timeout.** Not stress-tested at the boundary. `timeout=300.0`
  (5 min) was used defensively per the task's cold-load warning; see next
  section for what was actually observed.

## Timeouts / cold-load

No multi-minute first-call latency was observed in this session — every
request (including the very first, a non-streaming `complete_turn`
sanity check before the video probes) returned in under 3 seconds. This is
consistent with `worker` already being warm: `lobes-cli`'s own acceptance run
against this exact deployment happened the same day
(`2026-07-31-accept-worker-thor.txt`), and this repo's own sanity call
(`complete_turn(model="worker")` → `"OK"`, 2.2 s) landed before any video
probe. The task's warning about first-call load latency (`ready: true,
loaded: false` at the time the plan was written) is still the right default
posture for any caller that cannot assume a warm model — hence keeping the
generous `timeout=300.0` in the shape above rather than reverting to the
client's normal streaming default (120 s) — but it did not fire here.

## Streaming

Confirmed genuinely incremental, not client-side chunking of a buffered
response: every probe's deltas arrived across multiple `time.time()`
timestamps spread between the request start and the total elapsed time
(e.g. the forward-clip probe: 9 chunks arriving between 0.84 s and 1.22 s
after request start; the GIF-as-`image_url` control: a single larger delta at
0.72 s). `stream_chat_completion` sends `"stream": true` and parses
`text/event-stream` `data:` lines off the socket one at a time
(`_iter_sse_chunks`/`_iter_sse_deltas` in `reachy/speech/llm.py`) — unchanged
by this probe.

## Prior evidence this probe independently reproduces

This is a fresh, live probe run today against the deployed gateway through
this repo's own client — not a citation standing in for one. It happens to
land on the same conclusion two **external, sibling** repos already recorded
against the same infrastructure, which is worth naming because it raises
confidence this isn't a fluke of one clip:

- `lobes-cli`'s `docs/evidence/2026-07-31-accept-worker-thor.txt` (worker
  role's own acceptance run, real 78 KB webcam MP4, `video_url` data URI,
  1572 prompt tokens, accurate multi-sentence description).
- `embodiment`'s `docs/live-test-results/video-perception-probe.md` (an
  animated GIF sent as `video_url` vs `image_url`, on both `cortex` and
  `worker`, with the same round-1-wrong/round-2-corrected structure this
  document also went through independently — see
  [above](#a-wrong-result-caught-and-corrected-kept-in-not-hidden)).

Neither is part of `reachy-mini-cli` and neither is cited-in (no code import,
no vendoring) — they are read-only cross-repo context that shaped which shape
to try first, exactly as the task suggested checking `lobes-cli`'s docs for.

## What this unblocks

Per the plan's risk register, this probe **gates t5/t10 clip work by
policy**. The result is a clear pass: `reachy/behavior/clip_rider.py` (t5) and
the embody turn engine's clip-description tool (t10) may proceed using
`video_url` data URIs as their wire format, `model="worker"`, streamed. No
`/deviate` is needed.
