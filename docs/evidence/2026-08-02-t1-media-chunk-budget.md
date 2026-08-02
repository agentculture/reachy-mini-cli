# t1 — the media budget, measured

Task t1 of `docs/plans/2026-08-02-foreground-gemma-background-qwen-155.md`. The
plan made this measurement its first task because every media-budget choice in
issue #154 trades against a number nobody had. Here is the number.

Probe: one real clip from the deployed runtime's own rider
(`~/.local/state/reachy/behavior/clip.mp4`, 4.2 s, 38 frames, 1 049 229 bytes)
packed by `reachy.cli._commands.agent.build_clip_question` — the exact content
shape the layer sends — and posted to the senses model on the deployed gateway
(`http://localhost:8001`, `coolthor/gemma-4-12B-it-NVFP4A16`). Non-streaming, so
the server's own `usage` accounting is readable. No audio was emitted; the
per-chunk playback half of this task is deferred (see "Not yet measured").

## The measurements

| Ask | Request bytes | Prompt tokens | Latency | Answer |
|---|---|---|---|---|
| Clip ask (`video_url` data-URI) | 1 399 339 | **2 446** | 4.26 s | "I am in a room with a window and a table, and I am reaching for an object on the table." |
| Text-only baseline (same question, no media) | 268 | **47** | 4.06 s | "I do not have a physical body, a camera, or a location…" |
| 20-turn conversation window + the question | 1 691 | **448** | 4.54 s | "I am in a room and looking at a camera." |

Derived: the clip itself costs **2 399 prompt tokens**; twenty turns of ordinary
spoken exchange cost **401**.

## What this settles

**Media dominates Gemma's context, as assumed — 98% of the clip ask's prompt
tokens are the clip.** The premise behind #154's "measure the media budget
first" holds.

**Latency is not a media problem.** The clip ask is 0.2 s slower than a 47-token
text-only ask against the same model, on a payload 5 200× larger in bytes. Round
-trip time here is model overhead, not payload transfer or decode. So the media
budget is a *context* question exclusively; shrinking the clip to save latency
would buy nothing.

**A correction to a recorded finding.** Issue #154's operator comment reasoned
that the base64 payload is "orders of magnitude larger" than a 20-turn text
window, and the scope pass recorded that as "the text window is nearly free next
to the media". In **bytes** that is right (827×). In **tokens** — which is what
actually fills a context window — it is **6×**: 401 against 2 399. The text
window is *cheap*, not *free*: adding it to a clip ask grows the prompt by about
16%. The nested-window decision (m=20 / n=60, decision c30) survives this
comfortably, which is why it is a correction and not a deviation. Scope entry s2
is amended to carry the measured ratio rather than the estimated one.

**Incidental, and worth keeping: the text-only baseline reproduces #153
verbatim.** Asked what it can see with no media and no context attached, the
senses model answers *"I do not have a physical body, a camera, or a location in
the physical world."* That is the exact symptom that opened issue #153 — a robot
that can see, saying it cannot, because the lane answering was never told about
the camera. The measurement was not designed to demonstrate it; it fell out.

## What it implies for the build

- **The 20 s clip poll interval needs no widening on token grounds** (plan risk
  r4 asked). At one ask per 20 s the media cost is bounded and paid once per
  poll; the pressure #154 identified was never throughput, it was the *park*
  filling with near-duplicate prose — which task t3 fixes structurally.
- **Room to spend on the summary.** Decision c30's rolling summary can afford to
  be optimised for usefulness rather than brevity; a few hundred tokens of
  summary is small beside the media it accompanies and comparable to the turn
  window itself.
- **If media ever does need cutting**, the lever is frame count or resolution,
  not clip duration per se — 2 399 tokens across 38 frames is ≈63 tokens/frame,
  so halving the frame rate roughly halves the cost. Nothing measured here
  argues for doing so yet.

## Not yet measured

The per-chunk daemon-HTTP `/media/play` round trip (the other half of t1, and
plan risk r2 — whether the gap between spoken chunks is audible). It plays audio
on the deployed robot, so it is deferred to a moment the operator is present.
Task t6 therefore ships a defensible default chunk size and leaves it injectable;
this number retunes it rather than blocking it.

Probe source: `scratchpad/probe_clip_budget.py` (session-local, not committed —
it reads the live gateway and the deployed state dir, and is reproducible from
the description above).
