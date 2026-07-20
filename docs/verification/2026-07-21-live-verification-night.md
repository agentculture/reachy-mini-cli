# Live verification night — the fix wave on hardware (2026-07-21)

Continues `docs/verification/2026-07-20-t19-soak-exit-criteria.md` (§5 rows
C1/C2 updated there). Branch `feat/retire-old-ai-first-flow`; operator present
until ~01:30, robot alone after. Every number below is from the journal or a
measurement script — none from recollection.

## 1. The deploy that wasn't (a gotcha now in memory)

`uv tool install --force '.[daemon]'` from the unbumped checkout REUSED A
CACHED WHEEL (version string unchanged mid-arc, by design), so the first
"verification" session ran pre-wave code while the checkout carried the
fixes — and the in-band check was fooled too (imports verified from the repo
cwd, which shadows the tool's site-packages on `sys.path[0]`). Every fix
initially looked "failed on hardware."

**Rule:** deploy branch builds with `uv tool install --force --editable
'.[daemon]'` (tracks the checkout; restart = deploy) and verify imports from
a NEUTRAL cwd only.

## 2. The wave, live-confirmed (t31–t35 + t34)

| Fix | Live evidence |
|---|---|
| #96 duplicate lines (t32) | Zero doubled lines; only the SDK's own 6 startup lines carry the foreign prefix |
| #99 drop flood (t32) | 12 entry lines + 10 summaries / 3 min (was 194 per 10 s) |
| #97 scheduler (t33) | 44.0 Hz achieved (tick 100→1200 in exactly 25.0 s; was 23.2) |
| #95 gate (t31) | `moving-floor` transitions breathe with the idle rhythm |
| #100 stale audio (t34) | pump `started` → `live` (0 stale); a clap answered same-second (01:23:25 burst); STT stopped transcribing the past |
| t35 envelope | click-train transitions: 63 → 2 per episode (test-measured) |

The remaining ~20.7 ms tick work is NOT audio (pump moved it off-thread; the
number didn't move), NOT the pose read-back (`HeldStateReader.read()` benched
at ~0.00 ms), NOT DoA (throttled poller, non-blocking read). Apportionment
still open; a py-spy attempt timed out (see §5).

## 3. C2 — formal PASS, and the residual beyond it

**Formal C2 (gate doc §1): PASS.** 5 min, empty room: 0 `->SPEECH`,
0 `->ENGAGED`, 0 latch lines. The head never moved on phantom sound.

The operator's stricter bar (zero antenna motion) exposed a residual, then a
root cause. The empty-room 5-minute iterations:

| build | NOISE opens | fires |
|---|---|---|
| t31–t34, tail 0.25 s | 49 | 4 |
| + t35 envelope | 32 | 13 (held leans make MORE noise per episode) |
| + tail 0.75 s | 13 | 9 |

## 4. The root cause under the residual: the background itself moved

Unattended measurement (~02:00, robot alone; 3 phases + 2 A/B probes):

| condition | still-room rms | ≥ 0.02 |
|---|---|---|
| daytime baseline (2026-07-20, no target streaming) | p50 0.004, max 0.0095 | 0 % |
| night, no streaming | p50 0.0207, p90 0.053 | 51.7 % |
| night, 50 Hz target streaming (the runtime's normal state) | p50 0.034, p99 0.085 | 99.1 % |
| post-motion settle, 0–4 s after stop | p50 0.07–0.13 | 100 % (never decays) |

The background drifts **~25×** across conditions the same robot lives in
within 24 h. The deployed absolute floor (0.02) sits UNDER the night/hold
background; any value above the night state deafens the daytime robot. The
admission predicate must be RELATIVE (ratio over a rolling background — the
`SnapDetector` shape the 0.02 was originally extracted from as `min_rms`).
Filed as #102; plan task t36 (proposed d4). Drift mechanism deliberately
unresolved (AGC vs degraded pipeline vs hold-hum — the fix needs none of
them picked); `GStreamer Internal data stream error` observed twice.

## 5. Also observed

- **Fresh-daemon acquire race:** a just-restarted daemon reports media held
  for ~90 s during init; an acquire inside that window defers (correctly,
  fail-closed). Context for #98's escape-hatch design.
- **Informal S9 evidence:** the runtime, started after a daemon restart,
  recovered senses unaided — acquire → connect → pump live, 0 stale chunks.
- **py-spy profiling attempt:** `py-spy record -- … --max-ticks 1200` timed
  out at 3 min (1200 ticks should take ~27 s); no profile written; media was
  released cleanly on kill. Unexplained; retry when apportioning the 20.7 ms.
- **Box state at close:** runtime ACTIVE overnight (t35 build), temporary
  drop-in `reachy-runtime.service.d/self-motion-tail.conf` sets
  `REACHY_SELF_MOVING_TAIL_S=0.75` (winner ships as code default or is
  removed when t36 lands); `pat-sense.conf` + overlay `rules.toml` still
  moved aside in `~/reachy-rollback-2026-07-20/`.

## 6. What the morning inherits

1. t36 (adaptive ratio admission) — the #95 root closure; d4 + task proposed,
   awaiting confirm.
2. The 20.7 ms tick-work apportionment (work under budget → true 50 Hz → the
   overrun flood ends).
3. C3/C4/C5 capability checks + the rollback rehearsal (operator present).
4. Tail default decision (0.75 s measured better than 0.25; t36 may subsume).
5. The soak clock starts only after the above.
