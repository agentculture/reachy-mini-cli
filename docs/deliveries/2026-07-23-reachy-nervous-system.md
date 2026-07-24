# Delivery Summary — reachy nervous system

plan: `reachy-nervous-system` · run: `complete` · date: `2026-07-23`
baseline: `devague summary skeleton`

## Intent

Fix five open issues so the deployed robot actually has vision, hearing and
voice — and go further: expose Reachy's senses on an **event-based surface**
other services can consume, so a heavy sense can never slow the 50 Hz tick and
no consumer ever contends for the single-consumer SDK media session. Fourteen
tasks across seven waves, fanned out by `/assign-to-workforce`, closed by two
live sessions on the physical robot.

## Planned Work

Quoted verbatim from the `devague summary` skeleton:

- `t1` — Port the #99 episode-suppression pattern into TickMetrics (#121): streak state, first-tick emit, closing summary, >5x-budget immediate report, shutdown flush
- `t2` — Add the sense_extras doctor check (#120a): cv2 import-probe with remediation naming both pip and uv tool install forms
- `t3` — Publish per-sense availability into state.json (#120b): face_sense exposes its vision-extra-absent reason; a senses block lands via the seam-rider path (c39)
- `t4` — Dispose issues #94 and #97 on GitHub: close #94 with the 2026-07-23 evidence, restate #97 soak criterion S3 around cadence stability
- `t5` — Correct the stale #94-premise docs and record the new decisions: ALSA-sharing fact, events-cli base-dep decision, monitor-speaker test vector
- `t6` — Build the MQTT publisher module against an injected client seam: event mapping, retained state mirror, LWT, QoS0, degrade — all fake-client TDD
- `t7` — Compose the publisher unconditionally in behavior engine run: lazy events-cli import, REACHY_MQTT_URL env, graceful degrade, additive to --export
- `t8` — Extend docs/export-schema.md with the topic map: reachy/events tree, retained reachy/state keys, online/LWT, per-topic QoS
- `t9` — LIVE session A — re-enable the voice (#122) and run the c31 audio-push probe
- `t10` — Inject the held media client into SpeechActuator in the shape the t9 probe dictates (direct or pump-style output seam)
- `t11` — Repoint the reTerminal bridge: an MQTT subscriber module in the reterminal-cli sibling repo consuming reachy/events/# + reachy/state/online
- `t12` — LIVE session B — the acceptance run: full sensorium + broker + panel + kill tests + 30-min soak (PR gate)
- `t13` — Version bump + CHANGELOG + uv lock; the PR closes the arc
- `t14` — Widen the retained state mirror to the seam-rider keys (senses, intents) so the bus fully mirrors state.json (closes h21, plan risk r6)

## Actual Delivery

| Plan task | Status | What actually landed |
|-----------|--------|----------------------|
| `t1` | delivered | `tick_metrics.py` episode suppression. Needed **two** live corrections after the unit tests passed: close-hysteresis (`522a2ae`) because reality alternates rather than forming contiguous blocks, then a periodic checkpoint (`7adb661`) because total silence was equally wrong. Live: ~2 lines/min against 39/s before. |
| `t2` | delivered | `doctor` `sense_extras` check; remediation names both the `pip install` and `uv tool install --force --editable ".[daemon,vision]"` forms. Verified both ways live. |
| `t3` | delivered | `sense_availability.py` + a seam-rider driver; `engine.py` untouched (0 lines). A `senses` block reaches `state.json` and the bus. |
| `t4` | delivered | #94 closed with evidence; #97 rescoped to cadence stability. |
| `t5` | delivered | Stale #94 premise corrected to the ALSA-sharing fact; events-cli decision and monitor-speaker vector recorded. |
| `t6` | delivered | `reachy/export/mqtt.py` — `NervousPublisher` against an injected client seam, TDD on `tests/fake_events_client.py`. |
| `t7` | delivered | Composed unconditionally in `behavior engine run` (load-bearing: the deployed unit's `ExecStart` carries no `--export`). |
| `t8` | delivered | `docs/export-schema.md` topic map, additions only. |
| `t9` | partial → resolved | Box-side half done as planned. Live halves deferred by `d1` on an upstream fault, then **delivered inside t12**: hearing now works end to end. |
| `t10` | delivered | Direct injection of the held media client into `SpeechActuator`, in the shape `d2`'s probe dictated. |
| `t11` | delivered | `reterminal-cli` PR [#20](https://github.com/agentculture/reterminal-cli/pull/20); validated against the live bus and the physical e-paper. |
| `t12` | delivered | Two sessions (hardware blocked the first). Every criterion passed except the soak — see Remaining Work. |
| `t13` | delivered | v0.44.1, CHANGELOG entries, `uv lock` regenerated in the same change. This artifact is its last step before the PR. |
| `t14` | delivered | Retained tree widened to `senses` / `intents`; closes h21 and plan risk r6. |

## Mid-work Decisions

- `d1` — t9's live halves defer to the t12 session; the box-side half stands
  DONE — *"the hearing failure is upstream and outside this repo: lobes
  /v1/realtime loses ~3.8s of speech down to 8 chars while handing the SAME
  audio to the SAME Parakeet that transcribes it fully… The robot is exonerated
  by experiment."* **Now resolved**: model-gear's realtime service was repaired
  independently, and t12 observed full transcriptions live.
- `d2` — t10 uses DIRECT injection, not a pump-style seam — *"198 clean reads,
  ZERO read errors… push_audio_sample buffers and returns in 8ms for a 5.76s
  clip… The 5.76s clip was confirmed AUDIBLE in the room."*
- `d3` — t11's subscriber uses RAW MQTT on loopback, not the `events_cli`
  client — *"events-cli's converged first slice ships a PUBLISH-ONLY importable
  client; durable subscriptions are deferred to their issue #7."*
- **Not covered by any record — the events-cli binding needed an adapter, not
  the predicted one-line swap.** The shipped `events_cli.EventClient` differs
  on three names (`is_connected`, `close`, and a constructor-time Last Will).
  `reachy/export/events_client.py` adapts it and is the one module naming the
  vendor. This was implementation detail inside t7's criteria, not a change to
  them, so no `dN` was raised.
- **Not covered by any record — #126 fixed inside the run.** The live
  acceptance found the retained state tree republishing at tick rate. Fixed on
  operator instruction rather than deferred, because it contradicted the arc's
  own premise.

## Drift From Plan

| Plan item | Reason for divergence | Classification |
|-----------|-----------------------|----------------|
| `t9` (`d1`) | upstream lobes `/v1/realtime` fault; robot exonerated by experiment. The deferred halves later landed in t12 | needs-follow-up → **cleared** |
| `t10` (`d2`) | the c31 probe proved direct injection safe; a pump-style seam would be unnecessary complexity | acceptable |
| `t11` (`d3`) | events-cli's first slice is publish-only; their spec blesses raw loopback MQTT for co-located consumers | acceptable |
| `t12` | the 30-minute soak's `O(10) overrun lines` criterion is unmet **as written** — the tick runs a steady ~21.1 ms against a 20 ms budget, so overruns are continuous, not episodic. The episode-suppression fix (t1) bounds the LOG volume, which was the actual defect; the criterion assumed occasional overruns | needs-follow-up |

## Evidence

- tests: full suite — **3798 passed, 6 skipped** (`uv run pytest -n auto`)
- lint: `black --check`, `isort --check-only`, `flake8`, `bandit`,
  `markdownlint-cli2`, `teken cli doctor . --strict` — all clean
- commits: `54e5fbb..2aeb6e2` (29 commits on `spec/nervous-system`)
- live acceptance record: `docs/verification/2026-07-24-nervous-system-acceptance-partial.md`
- sibling PR: reterminal-cli [#20](https://github.com/agentculture/reterminal-cli/pull/20)
- issues addressed: #120, #121, #122 (closed by this PR), #94 (closed), #97 (rescoped)
- issues filed by this run: #125, #126 (fixed), #127
- upstream dependency: `events-cli>=0.9`, agentculture/events-cli#3

## Delivery Claims

| Claim | Confidence | Evidence |
|-------|------------|----------|
| The runtime publishes sense/rule/intent/motion events and retained state to MQTT | high | live: `reachy/events/rule/fire` observed on the broker from a physical pat, `docs/verification/…-acceptance-partial.md` |
| A rule fire reaches an external consumer with no SDK contention | high | reterminal-cli [#20](https://github.com/agentculture/reterminal-cli/pull/20); physical e-paper read back, operator-confirmed |
| Hearing works end to end (STT → engagement → rule → audible voice) | high | journal: `engagement: name` → `fired … say="I'm here."` → `spoke voice=harmonic`; operator heard it |
| The robot does not answer itself | high | `dropped reason=self-mute` immediately after `spoke`; exactly one fire |
| A dead broker degrades to ONE named drop, tick cadence unchanged | high | live stop/start of the broker under a running runtime |
| Ungraceful death flips retained availability via the Last Will | high | `kill -9` on the real runtime: `online` → `false`, other retained state persists |
| The overrun-log flood is fixed (#121) | high | ~2 lines/min live vs 39/s before; commits `522a2ae`, `7adb661` |
| A missing `[vision]` extra is visible in `doctor` and `state.json` (#120) | high | both venvs exercised live; `sense_extras` flips `passed` |
| The retained state tree publishes only on change (#126) | high | live: 3702 → 203 msgs/10 s; `state/active` = 20 publishes = its distinct-payload count |
| The broker binds loopback only | high | `ss` + refused connects from both routable IPs |
| `events-cli` is the only MQTT path; this repo imports none | high | `test_h10_no_mqtt_library_became_a_direct_dependency` + a source scan |
| Face **recognition** works on the deployed robot | unverified | detection confirmed every frame; matching fails at 0.43 vs 0.50 (#127). **Not claimed done** — moving to a dedicated sibling tool by operator decision |
| The 30-minute soak criterion as written | unverified | see Drift — the criterion's premise does not match the measured steady-state tick |

## Remaining Work / Follow-up

- **#127 — face recognition.** Enrolment/matching quality moves to a new
  sibling tool (operator decision, 2026-07-24). The **observability** half stays
  here: a seen-but-unrecognised face is byte-identical to an empty frame, with
  no line to grep — #120's failure shape one level deeper.
- **#126 follow-through.** Fixed and live-verified; `state/updated`'s new
  "when state last changed" semantic is documented in `docs/export-schema.md`.
- **#125 — the async-connect race.** Cosmetic and self-correcting; deliberately
  not folded in, since it touches merged t6 code.
- **#124 — listening posture.** Operator-requested behaviour (stop, orient to
  DoA, hold still) — a separate arc.
- **`t12` soak criterion.** Re-state it around cadence stability rather than an
  overrun count, matching the #97 rescope t4 already applied.
- **STT quality.** "Reachy" arrived as "Richie" (caught by the Soundex guard)
  and "Legique" (correctly rejected). Mic RMS peaked ~0.002 while speaking.
  Upstream of this repo, but the gate's margin depends on it.
- **reterminal-cli stdout bridge.** Retired only once the broker path has run
  in anger, not on one session.
