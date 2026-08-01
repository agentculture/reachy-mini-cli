# Cited findings — `agentculture/embodiment`, and why we did not import it

Recorded 2026-08-01 during the `embodiment-layer` build (wave 1). The sibling
repo `agentculture/embodiment` (v0.11.0, on PyPI) surfaced while reading the t2
video probe's cross-references. It advertises "the agentic loop that gives an
app an embodied AI presence — supply a model seam plus IO callbacks", which is
structurally what our t10 turn engine and t11 composition root build. So the
reuse question was asked properly before those tasks start.

## Decision: cite, don't import

The plan is **unchanged** — no `/deviate` was needed. Three independent
disqualifiers, any one sufficient:

1. **The turn model is wrong for us.** Its entry point `run()` is one-shot per
   `Task` with a step budget. Its own code states that an arriving operator
   message does *not* enter the model conversation — the loop routes it to a
   presence sink, and whether it becomes model guidance is "the host's call".
   Our turns are *cue-triggered*: an utterance or a rule firing IS the trigger.
   We would call `run()` once per cue and use almost none of it.
2. **The streaming we need is not in the artifact you would install.** Its
   `pyproject.toml` ships `packages = ["embodiment"]`, so `examples/` — where
   all the SSE transport work lives — is not in the wheel. The packaged model
   seam is `CompleteFn = Callable[[list[dict]], ModelResponse]`, which buffers a
   whole turn by contract. Nothing in the library helps get first-token audio
   out.
3. **The dependency weight is disqualifying.** Its base deps pull neo4j,
   pymongo, numpy, httpx, paho-mqtt and the Docker SDK. Our base is three pure
   wheels (`numpy`, `harmonics-cli`, `events-cli`) under a documented
   keep-base-light rule. Their own README records that this already blocks
   `colleague` — the repo embodiment was *extracted from* — from adopting it.

Naming is not a conflict: their README explicitly carves out that
"`reachy-mini-cli` owns the physical robot", states no intent to drive
hardware, and asks that any change to that be "a stated goal agreed with the
robot siblings". Our package is `reachy/embody/` behind the `agent embody`
verb, which stays distinguishable from their `embodiment` package.

## What we take instead — two findings, both verified here

### 1. The streaming delta field is `reasoning`, not `reasoning_content`

Their `docs/live-test-results/streaming-probe.md` reports that the SSE stream
carries `delta.reasoning`, while vLLM documents `delta.reasoning_content` — so
a consumer written against the documented name silently sees zero reasoning and
concludes the model does not stream its thinking, which is the opposite of the
truth.

**Independently reproduced against our own gateway** (`localhost:8001`,
`model=cortex`, `stream=true`, 73 chunks):

```text
delta keys observed: ['content', 'reasoning', 'role']
  'role'      -> 'assistant'
  'reasoning' -> 'Here'
```

Consequence for this arc: `reachy/speech/llm.py` consumes no `reasoning` field
at all today (grep is empty). The embodiment layer's export contract has a
`thinking` block, so **t10 must read `delta.reasoning`** to fill it. Writing it
against the documented name would produce a permanently empty thinking feed
with nothing in the record to flag it.

### 2. Streaming converts a total deadline into an inter-chunk idle bound

Same probe: the read timeout is *per-read*, so a streaming consumer bounds the
gap *between* chunks rather than the whole request. Measured there: with
thinking on, the first **content** delta did not arrive until 43.2 s — a 43 s
wall of silence to a non-streaming client, and the same shape that killed two
calls at a 600 s total deadline — while the largest gap *between* chunks was
0.124 s.

This is the mechanism behind our spec's streaming requirement (c6) and its
honesty condition (h6, "a stalled stream resolves as a named timeout drop,
never a hang"): the drop must be armed on **inter-chunk idle**, not on total
elapsed time, or a long think will be killed as if it were a stall.

### 3. Video route — two independent probes agree

Their `docs/live-test-results/video-perception-probe.md` and our own
`2026-08-01-probe-video-wire-format.md` were run separately and concur on the
mechanism: the content-part **type** selects the multi-frame decode path, and a
GIF sent as `image_url` flattens to a single frame while the same bytes in a
`video_url` part decode as motion. They add that the declared MIME is sniffed
and effectively ignored, and that `video_url` is *cheaper* than a single still
(86 vs 132 prompt tokens), implying aggressive server-side frame sampling.

They also note `/capabilities` is a poor predictor of what a route can do —
their cortex advertises no vision and reads video fine. Ours advertised
`video_understanding` on `worker` and it worked, so we have no disagreement to
resolve, but the caution is worth carrying: **probe the delivery path, don't
trust the advert.**
