# Probe — does the gateway stream `reasoning` with our shipped defaults?

Run 2026-08-02 against the deployed lobes gateway (`localhost:8001`), during
wave 3 of the `embodiment-layer` build. This one settles an open question the
t10 build raised about itself, and it corrects an earlier finding of ours that
was true but incomplete.

## Why it was run

`docs/evidence/2026-08-01-cited-findings-from-embodiment-sibling.md` recorded
that the streaming delta field is `reasoning`, not the documented
`reasoning_content`, and concluded that the turn engine "must read
`delta.reasoning`" to fill the export contract's `thinking` block.

That finding was reproduced honestly, but the probe behind it did **not** send
`chat_template_kwargs.enable_thinking` at all — while `reachy/speech/llm.py`
sends `{"enable_thinking": false}` on every request by default. So the observed
condition was never the shipped condition. t10 flagged exactly this gap in its
own report rather than letting it pass, which is what prompted this probe.

## Method

One streaming `POST /v1/chat/completions` per cell, same prompt, counting delta
keys that carry a non-empty value, and timing the first `content` delta, the
first `reasoning` delta, and the largest gap *between* consecutive deltas.
Script: `scripts/probe_thinking_deltas.py`.

## Result

| model | `enable_thinking` | delta keys | first `content` | first `reasoning` | max inter-chunk gap | chunks |
|---|---|---|---|---|---|---|
| worker | `false` (**shipped**) | `content`, `role` | 0.22 s | — none — | 0.150 s | 77 |
| worker | `true` | `content`, `reasoning`, `role` | 9.72 s | 0.23 s | 0.229 s | 263 |
| worker | *omitted* | `content`, `reasoning`, `role` | 5.43 s | 0.11 s | 0.111 s | 180 |
| cortex | `false` (**shipped**) | `content`, `role` | 0.27 s | — none — | 0.270 s | 62 |
| cortex | `true` | `content`, `reasoning`, `role` | 17.96 s | 0.28 s | 0.275 s | 165 |
| cortex | *omitted* | `content`, `reasoning`, `role` | 18.46 s | 0.27 s | 0.269 s | 171 |

## What it establishes

1. **With the shipped default there is no `reasoning` key at all.** Not an empty
   string, not a null — absent, on both models. The earlier finding's *field
   name* is right (`reasoning`, never `reasoning_content`), and its parsing is
   right; it simply never fires under our own defaults. Omitting the field
   entirely gives thinking-on behaviour, which is what the original probe
   measured.

2. **Thinking costs 9-18 seconds before the first spoken or acted output.** That
   is the real number for the layer's decision, and it is per turn. For a
   harness whose purpose is realtime conversation, that is disqualifying — so
   `enable_thinking` stays `False` and the exported `thinking` block carries
   cues, reply text, tool calls and results, but no model reasoning. The seam is
   correct and **dormant, not broken**; one flag fills it.

3. **The inter-chunk-idle design (h6) is confirmed with fresh numbers, and the
   margin is enormous.** Worst observed time-to-first-content is 17.96 s while
   the worst gap *between* deltas anywhere in the table is 0.275 s — a factor of
   65. Any total-elapsed deadline tight enough to catch a real stall would kill
   a healthy long think; an inter-chunk bound of even one second would not.

4. **A thinking stream is never silent.** Reasoning deltas start at 0.11-0.28 s
   in every thinking-on row, so the first *read* returns promptly even when
   content is 18 s away. This materially reduces the risk in t10's own caveat
   that `idle_timeout_s` also covers time-to-first-token: the only remaining
   exposure is a genuinely cold model load, not thinking.

## What it does not establish

- Only two roles (`worker`, `cortex`) and one short prompt were measured. A
  long tool-calling turn may behave differently, and a cold model load was not
  induced — every call here hit a warm server.
- Nothing about tool-call deltas: the prompt was plain text with no tools
  attached, so `lobes-cli#161` (a tool call on a no-tools request) is still
  unverified live, as t10 reported.
- These are single samples per cell, not distributions. The 5.43 s vs 9.72 s
  spread in the two worker thinking-on rows is unexplained and could be
  ordinary variance or scheduling.
