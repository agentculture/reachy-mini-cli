# Delivery Summary — agent cognition: tool-use senses + behavior stash

plan: `agent-cognition-tool-use-senses-behavior-stash` · run: `complete` · date: `2026-07-17`
baseline: `devague summary skeleton`

## Intent

Make Reachy's live presence an agent: the folded `listen run --live` cognition
gains a tool-use mode (`--cognition agent`) where the LLM acts through
structured tool calls (`speak`, `harmonics`, `apply_pose`) instead of the
`*emoji*`/`"quoted"` marker convention, plus a semantically searchable behavior
stash (declarative LibraryEntry-shaped records + lobes-gateway embeddings) it
can fetch and apply. Executed as the converged 10-task / 4-wave plan via
`/assign-to-workforce` (one agent per task per wave, isolated worktrees,
TDD-gated merges), with one approved mid-run deviation (`d1`).

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — LLM client tool-calling (reachy/speech/llm.py)
- `t2` — Gateway TTS leg — OpenAI-style /v1/audio/speech (reachy/speech/tts.py)
- `t3` — Agent tool registry — speak/harmonics/apply_pose definitions + dispatch (new reachy/speech/tools.py)
- `t4` — Behavior stash store — records, embeddings, semantic fetch (new reachy/stash/ package)
- `t5` — Stash apply adapter — fetched entry onto the live MotionQueue goto path (reachy/stash/apply.py)
- `t6` — Agent turn engine — tool loop + export blocks (new reachy/speech/agent_turn.py + cognition seam)
- `t7` — Live wiring — agent flag on listen --live, engagement gate unchanged (reachy/cli/_commands/listen.py + reachy/motion/listen_think.py)
- `t8` — Cortex role switch — config + end-to-end round trip (docs + gateway-gated integration test)
- `t9` — Boot unit + rollout docs + verification sweep (reachy/service/units.py, docs, CLAUDE.md)
- `t10` — Muse verification + agent-model move option (deviation d1)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `tools=`/`tool_choice=` payload support, streamed `tool_calls` delta assembly (`stream_turn`/`complete_turn` → `TurnResult`); payload byte-identical without `tools`; streaming verified live against cortex (merge `eda11b3`) |
| `t2` | delivered | `REACHY_TTS_ROUTE={chatterbox,openai}` route selection; gateway `/v1/audio/speech` probe-verified (WAV @ 24 kHz default, bare-PCM opt-in); Chatterbox route unchanged as default (merge `8756b88`) |
| `t3` | delivered | `ToolRegistry` with injected seams; `apply_pose` asserted action-identical to the marker path; dispatch never raises; new capability = one `Tool` (merge `58233fe`) |
| `t4` | delivered | `reachy/stash/{record,store,embeddings}.py`: declarative records with two-layer code-smuggling refusal, gateway `/v1/embeddings` via stdlib urllib, numpy cosine top-k, atomic JSON index under the state dir (merge `46a257d`) |
| `t5` | delivered | `plan_keyframes`/`apply_record`: vetted `LIBRARY` `build()` path only, bounded keyframes (≤8; infinite lifetimes capped at 4 s), end-to-end stash→fetch→apply test (merge `da34685`) |
| `t6` | delivered | `AgentTurnEngine`: bounded 6-round tool loop, ThinkHook-compatible `run(stop=...)`, export blocks validated against `docs/export-schema.md`, audio-optional latch; zero edits to existing files (merge `0101d07`) |
| `t7` | delivered | `--cognition {marker,agent}` (env `REACHY_COGNITION`, `--live`-only); engagement gate unchanged (fake-classifier test: ambient chatter → zero agent turns); self-mute wrapper covers tool speech; bare `listen run` byte-identical (merge `8453fa9`) |
| `t8` | delivered | Gateway-gated full round trip (prompt → tool_calls → tool results → final text) against cortex — passed live 6/6 standalone + 3/3 under `-n auto`; operating-guide model/env section (merge `ef16afe`) |
| `t9` | delivered | Live boot unit ExecStart → `listen run --live --transcribe --cognition agent --voice-engine harmonic`; operator agent-cognition section with two on-robot demo scripts; CLAUDE.md updated; full sweep green (merge `902bb02`) |
| `t10` | delivered | Round-trip test parametrized over cortex + muse with per-model skip guards and a 60 s latency bound; docs section became "Agent model choice — cortex or muse" (merge `85ca74e`) |

## Mid-work Decisions

- `d1` — try muse (nvidia/Gemma-4-31B-IT-NVFP4) as the live agent model: add a
  muse tool-round-trip verification task; on pass, document + switch the
  agent-mode model to muse via REACHY_OPENAI_MODEL_ID (environment.d) at
  deploy, keeping cortex as the verified fallback — lobes-cli#139 partially
  landed: 2026-07-17 re-probe shows muse now returns structured tool_calls
  (finish_reason=tool_calls) through the gateway — the tool-parser gap that
  forced the cortex pin is closed; audio-in still 400s (no audio tower) but is
  out of v1 scope (words path uses STT). (Recorded via `/deviate`, approved.)
- t8 pinned its integration test to `temperature=0.0` and added a bounded
  3-attempt retry — at the production default (0.8) the model may legitimately
  decline to call a tool, and concurrent live-gateway load occasionally drops a
  tool call; both are documented inline in the test, not plan content.
- t10 discovered muse reproducibly returns an **empty final message at
  temperature 0.0** (greedy-EOS quirk; content returns at ≥ 0.7 — production
  default is 0.8; 10/10 sequential + 12/12 concurrent reproductions). Handled
  as a lenient `pytest.skip` carrying the finding verbatim rather than a
  weakened assertion; no deviation record covers this — captured here.
- t3's worktree ran at `../worktrees/agent-t3-w` because a leftover root-owned
  `.data/` dir occupied the canonical path — ops detail, no code impact.
- Post-PR, the SonarCloud gate flagged 16 issues (none caught by the per-merge
  gates: complexity, redundant excepts, a duplicated literal, an implicit
  string concat, float equality, multi-call exception tests). All 16 fixed in
  `8f7a4a3`; the `store.py` float-equality item was triaged FIX by a dedicated
  subagent at the operator's direction (epsilon guard also hardens the
  denormal-norm case).

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t8` (`d1`) | lobes-cli#139 partially landed: 2026-07-17 re-probe shows muse now returns structured tool_calls (finish_reason=tool_calls) through the gateway — the tool-parser gap that forced the cortex pin is closed; audio-in still 400s (no audio tower) but is out of v1 scope (words path uses STT) | needs-follow-up |
| `t10` (`d1`) | task added mid-run by the same approved deviation — not in the plan the split gate approved | needs-follow-up |

No other task drifted: t1–t7 and t9 delivered exactly their contracted
acceptance criteria (see the task-by-task accounting above).

## Evidence

- tests: `uv run pytest -n auto -q` — **1904 passed, 1 skipped** (the skip is
  t10's documented muse temp-0.0 finding); baseline before the run was 1744
- tests (live, gateway-gated): `tests/test_speech_llm_tools_integration.py`,
  `tests/test_agent_turn_cortex_integration.py` (cortex case) — passed live;
  muse case exercised live, converges structurally, empty-content finding
  surfaced as documented skip
- lint: `black --check` / `isort --check-only` / `flake8` / `bandit -c pyproject.toml -r reachy` — all clean
- rubric: `uv run teken cli doctor . --strict` — 26/26 PASS
- commits: `9766b28..8f7a4a3` (spec, plan, 10 task merges, deviation d1
  re-export, v0.32.0 bump + `uv.lock`, Sonar fixes)
- PRs / issues: PR [#64](https://github.com/agentculture/reachy-mini-cli/pull/64) ·
  [agentculture/lobes-cli#139](https://github.com/agentculture/lobes-cli/issues/139)
  (muse gaps; partial-fix verification commented)
- SonarCloud: first scan ERROR (16 issues) → all fixed in `8f7a4a3`; re-scan
  **pass** (all PR #64 checks green: SonarCloud Code Analysis, test ×2,
  test-publish, lint, version-check, GitGuardian)

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The live loop has a working agent-cognition mode behind `--cognition agent`, engagement-gated, single process / single media session | high | merge `8453fa9` · `tests/test_listen_cognition_agent.py` (19 tests) |
| The LLM client does OpenAI tool calling, streaming included, against the real gateway | high | merge `eda11b3` · `tests/test_speech_llm_tools_integration.py` (passed live) |
| A full agent tool round trip works live against cortex | high | `tests/test_agent_turn_cortex_integration.py` (6/6 live standalone) |
| The behavior stash stores, semantically fetches, and applies declarative behaviors onto the live motion path — no code execution surface | high | merges `46a257d`, `da34685` · `tests/test_stash_{record,store,apply}.py` · end-to-end test `test_end_to_end_stash_fetch_and_apply` |
| The boot presence comes back in agent mode after reboot | medium | unit renderer + tests (merge `902bb02`); actual on-robot reboot not exercised in this run |
| Muse is tool-capable at comparable latency and usable as the agent model | medium | t10 live runs (~3.6–5.6 s round trips); qualified by the temp-0.0 empty-message finding (production runs at 0.8) |
| The two on-robot demos (addressed utterance; stash round trip) behave as documented | unverified | documented as manual steps in `docs/operating-reachy.md` — not run on hardware in this run |
| The muse model switch is deployed on the box | unverified | deploy-time `environment.d` step per `d1` — not performed in this run |

## Remaining Work / Follow-up

- Deploy on the robot box: `reachy service enable live` picks up the new
  agent-mode ExecStart; choose `REACHY_OPENAI_MODEL_ID` (cortex default vs the
  `d1` muse move) in `~/.config/environment.d/10-reachy-llm.conf` and restart —
  then run the two documented on-robot demos (the manual verification steps).
- Muse temp-0.0 empty-final-message quirk — consider reporting upstream
  (lobes-cli or thor serving) if it reproduces at higher temperatures under
  load; today it only constrains greedy-decoding tests.
- lobes-cli#139 asks 2–3 remain open upstream: audio-tower checkpoint (or
  honest catalog) and served-config-accurate `/capabilities`.
- Follow-up ideas parked in the plan risks: reranker-ordered stash retrieval
  (r-nonblocking), a vetted mini-DSL if declarative-only stash records prove
  restrictive, muse full adoption for reaction duty.
- The stash has no CLI noun yet (`reachy/stash/` is a library surface consumed
  by the agent tools); a `stash` noun with `list`/`add`/`search` verbs is a
  natural next feature if operators want direct access.
