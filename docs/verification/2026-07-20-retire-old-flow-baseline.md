# Baseline — before retiring the old AI-first flow (t1)

Evidence captured on the deployed robot **before any code changes**, per task
`t1` of `docs/plans/2026-07-20-retire-the-old-ai-first-flow.md`. Covers claims
`c18` (before_state), `c19` (why_it_matters) and their honesty conditions.

- Box: `spark-f8a9`
- Package: `reachy-mini-cli` 0.41.0 (`uv tool install` from a local checkout)
- Presence: `reachy-runtime.service` active since 2026-07-20 09:07:26 IDT
- Captured: 2026-07-20 ~17:36–17:40 IDT

## 1. Capability ownership — which flow owns what

The load-bearing question for this arc: what would be LOST by deleting the old
flow today.

| Capability | Old flow (`listen --live` / `think`) | Symbolic runtime (`behavior engine`) |
|---|---|---|
| Idle presence / breathe | yes (idle layer) | **yes** (`feel-alive` base layer) |
| Head-pat sensing | yes (`PatHook`, #43) | **yes** (`behavior/pat_sense.py`) |
| Rule-driven reactions | no (hardcoded reflexes) | **yes** (`rules.toml`) |
| Sound direction (DoA) sensing | yes | **yes** (`DoaPoller`) |
| **Orienting toward sound** | **yes** (two-tier ladder) | **no** — senses DoA, cannot sustain a gaze target |
| **Speaking / any audio** | **yes** (tts + harmonic) | **no** — zero audio imports in `behavior/` |
| **Hearing words (STT)** | **yes** (`listen_transcribe`) | **no** — no audio source at all |
| **rms (loudness)** | yes | **no** — provider slot exists, fed by nothing |
| **face / frame_available** | yes (`[vision]`) | **no** — provider slots exist, fed by nothing |
| Sleep / wake | yes (folded hook) | no (standalone `sleep` noun only) |
| Cognition (LLM) | yes, in-loop | no — by design; external via `agent attach` |

Five capabilities sit only in the retiring path (bold "no" rows above minus
sleep, which keeps its own noun). That is the porting workload this arc exists
to do, and the reason deletion cannot come first.

## 2. DoA / `speech_detected` at rest — corrected characterisation

**This corrects an earlier claim.** A 3-sample probe during the challenge pass
showed `speech_detected=True` on every read and was written up as "effectively
stuck True". A 120-sample probe does not support that.

Method: read `~/.local/state/reachy/behavior/state.json` every 0.5 s for 60 s,
quiet room, nobody speaking, robot running its normal runtime presence.

```text
window:  2026-07-20T17:37:29 -> 2026-07-20T17:38:29
samples: 120
speech_detected True: 55/120  (45.8%)
distinct angles: 35   min=0.000 rad   max=3.124 rad
longest consecutive True run: 5 samples (~2.5 s)
most common angles: 1.082 (x12), 1.117 (x10), 1.065 (x9), 1.047 (x8), 0.942 (x8)
```

Honest reading: `speech_detected` is **not** latched-on. It **flickers — true
about 46% of the time with nobody speaking** — while the angle wanders across
essentially the full 0–3.12 rad range.

The operational conclusion is unchanged and, if anything, firmer: a rule keyed
on bare `speech_detected` would fire on roughly a coin-flip with an
uncorrelated direction. The latched-DoA guard is mandatory for the orienting
port (`c10`/`h7`), and no shipped default rule may key on bare
`speech_detected` (`c13`/`h9`). What changes is the mechanism to guard against:
**noisy flicker plus a wandering angle**, not a frozen value.

## 3. Tick budget — a reproducible startup overrun already exists

The spec carried the assumption "0 tick overruns across 8500+ live 50 Hz ticks"
(from the #70 delivery). That is not what the current box shows.

```text
26 overrun lines across the journal's retained restarts
```

Every service start produces exactly one overrun at tick ~447–453, and the
journal names the cause unambiguously:

```text
[SENSE stage=state source=head_pose event=…] connected (media_backend=no_media)
[SENSE stage=rule source=tick event=overrun] overrun tick=449 duration_ms=424.93 budget_ms=20.00
```

Observed durations across restarts: **424.93, 974.39, 990.61, 1102.92,
1212.66 ms** — against a 20 ms budget, i.e. 21x to 61x over.

**Diagnosis:** `HeldStateReader`'s construct-on-first-read lazily builds the
`ReachyMini(media_backend='no_media')` client **on the tick thread**. The first
pose read therefore stalls the 50 Hz loop for up to ~1.2 s.

**Why this matters for this arc:** task `t10` is about to add a *second* held
client — a media client for mic and camera — and a camera pipeline is slower to
warm than a `no_media` handle. Replicating the lazy-construct-on-tick-thread
pattern would add a second, larger stall. The existing overrun is direct
evidence for `h2`'s requirement that construction and I/O stay off the tick
thread; it should be treated as a defect to fix, not a baseline to match.

Steady-state is otherwise healthy: two further overruns (26.42 ms, 64.89 ms)
appeared at tick ~1363186 during this capture, coinciding with the probe
reading `state.json` twice a second — plausibly probe-induced contention rather
than engine jitter, but stated here rather than filtered out.

## 4. Units and drop-ins

```text
reachy-daemon.service     enabled   (active)
reachy-runtime.service    enabled   (active)      <- the presence
reachy-demo-mode.service  disabled
reachy-live.service       disabled
reachy-listen.service     disabled  <- orphan, in no catalog, from June
```

Drop-ins present:

```text
reachy-daemon.service.d/execstart.conf     repairs the bare-name ExecStart (#62)
reachy-runtime.service.d/pat-sense.conf    5 REACHY_PAT_* overrides — load-bearing
reachy-live.service.d/debug.conf
reachy-live.service.d/diag.conf
reachy-live.service.d/forge.conf
reachy-live.service.d/llm.conf
reachy-live.service.d/panel.conf           bridge pipe, hardcoded IP
reachy-live.service.d/tts.conf
```

Two things this confirms:

1. **`reachy-live.service` is already disabled but still carries six drop-ins.**
   Removing the unit without removing its `.d/` directory leaves systemd warning
   about a drop-in for a non-existent unit on every `daemon-reload` — the exact
   cleanup `t4` is building.
2. **`reachy-listen.service` is the negative control** for the orphan class: a
   unit in no catalog that nothing has ever removed.

## 5. Engine state at rest

```json
active:    [{"name": "feel-alive", "class": "passive", "base": true,
             "channels": ["antennas", "body_yaw", "head"], "looping": true}]
ownership: {"head": "feel-alive-1", "antennas": "feel-alive-1", "body_yaw": "feel-alive-1"}
keys:      active, compose_hz, doa, intents, ownership, updated
```

Only the base layer is running. `pat-acknowledge` fires correctly in practice —
the journal shows real pats landing (`Pat level1! type=side_pat` →
`[SENSE stage=rule source=pat event=pat-acknowledge] fired kind=react
run=pet-reaction`), most recently at 15:10 today.

Note `state.json` carries no `t_local` at top level and no explicit staleness
marker — the gap issue #73 item 2 describes.
