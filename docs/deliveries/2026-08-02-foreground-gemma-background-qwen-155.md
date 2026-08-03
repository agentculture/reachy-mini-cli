# Delivery Summary — Foreground Gemma, background Qwen (#155)

plan: `foreground-gemma-background-qwen-155` · run: `partial` · date: `2026-08-02`
baseline: `devague summary skeleton`

## Intent

> Reachy Mini is one embodied presence over two model lanes: the operator talks
> with Gemma — the foreground interlocutor that listens, sees, speaks and
> handles turn-taking — while Qwen follows the conversation in the background,
> reasons over longer horizons, operates tools, and injects compact thinking
> scopes through typed events. Long replies stream as cancellable chunks a human
> can interject over, attention gates the voice as well as the mind, perception
> arrives as compact fresh snapshots that never evict runtime facts, and the
> attention window is an operator knob.

The run executed all sixteen tasks of the converged plan across eight waves,
fanned out to parallel agents in isolated worktrees and TDD-gated at each
merge. **The run is `partial`, and the reason is entirely in the last wave:**
every build task delivered, but t15's live acceptance could not execute the
majority of its scenarios — three are blocked on the deployed box having a
single audio output (issue #139) and one on upstream `agentculture/lobes-cli#170`.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Measure the media and chunk budgets against the deployed stack
- `t2` — Attention window knob: --attention-window + `REACHY_EMBODY_ATTENTION_WINDOW`
- `t3` — Kind-aware context park: latest-wins perception slot beside exact-text cue coalescing
- `t4` — Nested windows m=20/n=60 and the Qwen summary in Limits
- `t5` — Interjection policy module and the typed event family
- `t6` — Chunked cancellable playback in the duplex client
- `t7` — Said/unsaid truth: measured cut offsets and the wanted-to-say artifact
- `t8` — Per-utterance arming: attention gates the voice
- `t9` — Connect-time voice conventions via the `system_prompt` override
- `t10` — conversation.item.create client leg behind a capability check
- `t11` — Layer-curated canonical history and its projections
- `t12` — Cognition scopes and Qwen's governed voice
- `t13` — Perception snapshots: structured, fresh, latest-wins
- `t14` — Model pairing, docs, and the version bump
- `t15` — Live acceptance: the eight #155 scenarios on the deployed robot (PR gate)
- `t16` — Client-side tail cut: an interjection stops the robot inside the post-response.done window

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | partial | Media budget measured against the deployed gateway (`cde8f56`); `docs/evidence/2026-08-02-t1-media-chunk-budget.md`. The **audio half is deferred** — the per-chunk daemon `/media/play` round trip plays sound in the room and was never measured. |
| `t2` | delivered | `--attention-window` + `REACHY_EMBODY_ATTENTION_WINDOW` + `resolve_attention_window_s` (`e36d8cc`) |
| `t3` | delivered | Kind-aware park: perception slot is latest-wins beside exact-text cue coalescing (`65c7243`) |
| `t4` | delivered | `senses_history_maxlen=20` as a strict suffix of `history_maxlen=60` over ONE deque, plus the bounded summary (`65aa49c`) |
| `t5` | delivered | `reachy/embody/interjection.py`: `Authorization` OFF/WARM/PROACTIVE, `InterjectionPolicy.admit`, `WantedToSay` (`6d8fd78`) |
| `t6` | delivered | Generation-stamped chunked playback + `cancel_playback()` in `realtime_duplex.py` (`2c96af2`) |
| `t7` | delivered | `spoken_split()` / `estimate_spoken_prefix` (cited from lobes `_floor.py:323`) + `note_interrupted_reply()` (`c85d076`) |
| `t8` | delivered | Per-utterance arming behind a `session.created` capability check; fails closed with a named drop (`d2cd0e1`). **Inert on the deployed gateway** — see Drift. |
| `t9` | delivered | `resolve_voice_prompt` / `DEFAULT_VOICE_PROMPT` wired into composition (`938ff35`). Required a follow-up commit (`faa150f`) — shipped built-but-unwired first. |
| `t10` | delivered | `send_item()` behind `announces_conversation_items()`; send surface widened 3→4 frame kinds (`36361a8`). **Inert on the deployed gateway.** |
| `t11` | delivered | `floor_reseed()` + `floor_correction()`; the items channel gained its production caller (`b5ebaab`) |
| `t12` | delivered | `reachy/embody/scope.py` + `summary.py`; the layer's own voice seams deleted (`619e5f5`) |
| `t13` | delivered | `PerceptionSlot` / `PerceptionSnapshot` / `submit_perception()` (`c15fc3e`), plus a drift guard added on review (`9ada662`) |
| `t14` | delivered | Two-tempo docs across CLAUDE.md / operating guide / export-schema / README, the `model_pair` doctor check, v0.47.0 + `uv lock` + CHANGELOG (`430560f`) |
| `t15` | **partial** | Live pass run and recorded (`a9a4f91`). **4 of 8 scenarios verified; 1 blocked upstream; 3 blocked on #139.** Found and fixed a real defect (see Mid-work Decisions). |
| `t16` | delivered | Client-side tail cut at the composition layer; `realtime_duplex.py` stayed gate-free (`d139532`) |

## Mid-work Decisions

- `d1` — add task t16, a client-side tail cut — t7 reported `cancel_playback()`
  had zero production callers; investigating confirmed a **real but bounded**
  gap, smaller than first stated. Upstream already paces delivery to the
  playhead (lobes `_conversation.py` `delivery_pause_ms`, `DELIVERY_LEAD_MS=400`)
  precisely to keep barge-in live, so the server-driven `response.interrupted`
  path covers the bulk of a reply and t6 wired it correctly. What upstream
  cannot see is the lag our client adds after receipt, which lands at the
  **tail** of every reply. No planned task owned the cut *trigger*: t6 built the
  primitive, t7 the measurement, t15 only verifies. Approved by the user after
  the overstatement was corrected.
- **The built-but-unwired pattern recurred four times** and was caught each time
  by verifying a production caller existed rather than trusting a green suite:
  t6's `cancel_playback()` (→ `d1`/t16), t9's `resolve_voice_prompt` (sent back,
  fixed in `faa150f`), t10's items channel (deliberately deferred to t11), and
  t13's missing drift guard (added in `9ada662`). No deviation record covers
  this as a class; captured here because it is the run's most transferable
  lesson.
- **A live defect was found by t15 that every offline test missed.** The
  deployed senses model renders our own requested JSON shape back with **padded
  keys** — `{" summary": ...}` for `{"summary": ...}` — so every clip ask
  degraded to a summary-only snapshot carrying the raw fenced blob, losing
  entities and confidence entirely. Fixed in `a9a4f91` via
  `_normalized_perception_keys`, which strips whitespace and case and *nothing
  else*: a genuinely different key (`description`) still misses, because
  tolerating a sloppy rendering of our contract is not the same as guessing at a
  synonym for it.
- **`reachy-runtime.service` was restarted mid-pass** to recover a camera
  pipeline that had EOS'd 11.7 h earlier (issue #138). Not a plan step; without
  it the perception lane had no fresh frames and t15's scenario 1 would have
  been untestable.
- **A reading was corrected mid-pass.** The box's HDMI output briefly reported
  `available: yes` and I believed issue #139's blocker had lifted. It was a
  monitor momentarily awake; minutes later the profile read `available: no`,
  the sink was gone, and `paplay` timed out exactly as #139 documents. The
  correction is recorded in the evidence file so the next pass does not chase it.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t7` (`d1`) | No planned task owned the cut *trigger*; without it acceptance criterion c34/h5 was unreachable on the deployed path. Resolved by adding t16. | `needs-follow-up` |
| `t1` | The audio half (per-chunk daemon `/media/play` round trip) plays sound in the room and was deferred to a moment the operator was present; that moment did not arrive. t6's chunk size therefore ships as a defensible default rather than a measured one. | `needs-follow-up` |
| `t15` | Live acceptance is **incomplete**: 4 of 8 scenarios verified. Three need a second conversational party able to make sound in the room (#139, still open and still accurate); one needs upstream `lobes-cli#170` item 1. The plan's criterion "all eight scenarios executed live" is **not met**. | `needs-follow-up` |
| `t8`, `t10` | Both shipped correct, tested, wired, and **inert on the deployed gateway** — their capability checks fail closed because upstream announces neither one-shot arming nor conversation items. This is the design working, not a defect, but it means #149's user-visible symptom is unchanged in production. | `acceptable` |
| `t12` | `scope_from_event()` — documented as "the front door a caller reading a feed should use" — has no production caller, and `runtime_cues.py` defines no `scope` line type, so no external producer can inject one. Scopes reach the engine only via an admitted interjection, which is `OFF` by default. t12's own acceptance criteria (artifact shape, bounds, expiry, governed voice) all hold. Filed as issue #157. | `needs-follow-up` |

## Evidence

- tests: full suite — **4954 passed, 8 skipped** (the eighth skip is
  environmental: `test_vision_scene_integration.py` skipped itself with
  `vlm-unreachable … timed out` because the gateway was busy serving this pass's
  own clip asks; it passes 4955/7 when free)
- tests: `tests/test_zero_llm_boundary.py` + `tests/test_embody_redteam.py` —
  **62 passed** (the structural pins, unchanged by the arc)
- tests: `tests/test_agent_embody.py::test_parse_perception_answer_tolerates_a_reformatted_key`
  — pass (written failing first)
- tests: `tests/test_agent_embody.py::test_parse_perception_answer_still_refuses_a_genuinely_different_key`
  — pass
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit` / `teken cli doctor . --strict`
  — all green
- lint: `markdownlint-cli2` — 0 errors
- commits: `cc5258f..a9a4f91` (35 commits, 16 task merges)
- runtime diff: `git diff main...HEAD -- reachy/behavior/ reachy/motion/` — **empty**
- evidence files: `docs/evidence/2026-08-03-t15-155-live-acceptance.md`,
  `docs/evidence/2026-08-02-t1-media-chunk-budget.md`
- PRs / issues: #155, #154, #153, #151, #150, #149, #157 (filed), #139, #138,
  `agentculture/lobes-cli#170`

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The arc changed **zero lines** of the presence runtime — the peripheral property is exact, not merely additive | high | `git diff main...HEAD -- reachy/behavior/ reachy/motion/` returns empty; 0 deletions |
| The structural boundaries held: no zero-LLM or redteam pin was loosened | high | 62 passed across `test_zero_llm_boundary.py` + `test_embody_redteam.py` |
| The attention window is tunable without a code edit (#150) | high | flag on `agent embody --help`; live: default `45.0`, env → `7.0`, explicit flag → `3.5`; evidence §4.1 |
| The perception lane yields a structured snapshot on the deployed gateway (#153 mechanism) | high | live gateway call parsed to `('The camera is positioned in a studio apartment…', ('desk','chair','window','fridge'), 0.9)`; evidence §3 |
| t9's voice conventions reach the wire on every connect | high | session URL carries `system_prompt=…` verbatim; evidence §4.2 |
| No AEC self-cut occurred over a 16.9 s reply (plan risk r6) | medium | no `speech started` in the 40 s window `07:25:08→07:25:48`; **one episode, not a law** |
| Ambient speech produces no *audible* reply (#149) | unverified | blocked on `lobes-cli#170` item 1; reproduced live in its unfixed form — evidence §5 |
| Long answers chunk and stop cleanly when interrupted (#151) | unverified | blocked on #139 — needs a second audio output; no scenario executed |
| Verbose perception never displaces runtime facts (#154) | unverified | offline tests only; not exercised live |
| Qwen injects scopes through typed events without owning the mouth | low | the governed-voice routing is tested and holds, but the event-borne route has no caller (#157); scopes arrive only via an interjection, default `OFF` |
| The spoken half of "what can you see" (#153) | unverified | needs a voice in the room |

## Remaining Work / Follow-up

- **`t15` — finish live acceptance.** Attach a USB or Bluetooth speaker (either
  appears as its own PipeWire sink immediately), then re-run scenarios 1, 2,
  4.2, 7 and 8 with a person in the room. Blocking for #151, #154 and the
  spoken halves of #153 and h8. Tracked by **#139**.
- **`t1` — measure the audio half.** The per-chunk daemon `/media/play` round
  trip sets the tail-cut window's width; t6's chunk size is currently a
  defensible default, not a measured one. Same prerequisite as above.
- **#149 — keep open.** The client half is complete, tested and inert. Closing
  depends on `agentculture/lobes-cli#170` item 1 (one-shot arming). The wire
  shape we implemented against is a **guess we posted upstream** and it fails
  closed; if upstream chooses differently we follow theirs.
- **#157 — wire or declare `scope_from_event`.** Either add a `scope` line type
  and a `_routed_scope` branch mirroring `_routed_interjection`, or mark it
  explicitly as a public API for out-of-tree producers so the missing caller is
  a decision rather than an oversight.
- **#138 — camera EOS recurred** during this pass and was worked around by
  restarting the runtime. Unchanged, still open.
- **Issue disposition:** #150 is closable on this evidence. #153 has its
  mechanism verified and a real defect fixed, but its spoken half is unverified.
  #154 and #151 should **not** be closed on this evidence. #155 stays open.
