# t19 — Soak exit criteria and rollback runbook

Task `t19` of `docs/plans/2026-07-20-retire-the-old-ai-first-flow.md` is a
**gate, not a code task**. Nothing downstream (`t20`–`t26`, every deletion)
merges until it passes.

**Written BEFORE the soak starts**, per the task's own first acceptance
criterion. Criteria written afterwards describe what happened; criteria written
first can fail. Anything added below after the soak begins is marked as such.

- Box: `spark-f8a9`
- Branch under soak: `feat/retire-old-ai-first-flow` (26 tasks merged)
- Baseline it replaces: `reachy-mini-cli` 0.41.0 (`uv tool install`)
- Authored: 2026-07-20, before any install

## 0. Pre-soak finding — the box is already shipped-equivalent

Checked before writing the criteria, because it changes what "clean env" costs.

`reachy-runtime.service.d/pat-sense.conf` is now a **no-op**. Every variable it
sets equals the v0.41.0 shipped default:

| variable | drop-in | shipped default | source |
|---|---|---|---|
| `REACHY_PAT_STILL_EPS` | 0.035 | 0.035 | `behavior/pat_sense.py:277` |
| `REACHY_PAT_STILL_HOLD_S` | 1.0 | 1.0 | `behavior/pat_sense.py:278` |
| `REACHY_PAT_PRESS_DEG` | 1.2 | 1.2 | `motion/pat.py:126` |
| `REACHY_PAT_YAW_PRESS_DEG` | 1.2 | 1.2 | `motion/pat.py:128` |
| `REACHY_PAT_RELEASE_AFTER_S` | 2.5 | 2.5 | `behavior/pat_sense.py:310` |
| `REACHY_PAT_HP_TAU` | *not set* | 0.8 | `behavior/pat_sense.py:216` |

It was load-bearing under 0.40.0, whose shipped `still_eps` was 0.01 — its own
comment measured that as opening the gate **0.0%** of the time under the
swinging idle. Commit `0adbd48` ("Lock the swing-era pat tuning as shipped
defaults") made it redundant and it was never retired.

Likewise the box-local overlay `~/.local/state/reachy/behavior/rules.toml`
carries one rule, `pat-acknowledge`, differing from the newly shipped rule of
the same id only by an explicit `hysteresis = 0.0`.

**Consequence:** removing both moves the box to a genuinely clean env at
near-zero behavioural risk, and it means a fresh install already gets working
pat sensing with no operator config — t15's acceptance criterion 1.

## 1. Capability demonstration — all four in one session

t19's third acceptance criterion. These are the four checks deferred by
`t7`, `t30`, `t9` and `t15`; they were never separate work.

Run on a clean env (§0 removals applied). Each check names the evidence that
settles it — a journal line, not an impression.

### C1 — media acquired, providers feeding (t30 criterion 4)

The whole point of `t30`. Before it, `/api/media/status` read
`{"available": false, "released": true}` **with the runtime active**, so the
four ported senses were wired but permanently dormant.

- **Pass:** with `reachy-runtime` active, `GET /api/media/status` reports
  `available: true, released: false`, and the journal carries
  `[SENSE stage=media source=held_client] media acquired from the daemon`.
- **Fail:** status still reads `released: true` after startup settles, or the
  journal shows a `contended` / `not-ready` line that never resolves.
- **Also fail:** `warm_up()` blocks unit startup (see C5).

### C2 — quiet room, no spurious turning (t9 criterion 1)

The criterion the corrected 45.8%-flicker measurement exists to protect.

- **Method:** room quiet, nobody interacting, **minimum 5 minutes** (the plan
  says "multi-minute"; the measured probe window was 60 s, so 5× that).
- **Pass:** `journalctl --user -u reachy-runtime -f | grep 'stage=orient'`
  shows **no `->SPEECH` and no `->ENGAGED` transitions**. `NOISE` tier
  (antenna lean only) is acceptable and expected in a room with any ambient
  sound. No `latch` line — `LatchedDoaGuard` is expected to stay inert on this
  daemon build (35 distinct angles in 60 s).
- **Fail:** any head-moving tier opens with nobody making sound.
- **Note:** a `latch` line would be *interesting, not failing* — it would mean
  the frozen-feed state we could not reproduce does occur.

### C3 — pat (t15, shipped `pat-acknowledge`)

- **Pass:** a real pat produces `Pat level1!` **and**
  `[SENSE stage=rule source=pat event=pat-acknowledge] fired kind=react run=pet-reaction`,
  with a visible reaction, **from the shipped rule** (overlay moved aside).
- **Fail:** a lone `Pat level1!` with no rule fire (the 2026-07-20 `HP_TAU`
  silence signature), or no detection at all.

### C4 — words heard, and answered audibly (t15 + t7)

Covers t7's deferred audible-output criteria.

- **Pass:** an addressed utterance produces
  `[SENSE stage=rule source=transcript event=greet-when-addressed] fired ... say="I'm here."`
  **and the chirp is actually heard.** Audible output is the point; a fired
  rule with silence is a fail, because silent degradation is exactly what t7
  exists to make impossible to mistake for success.
- **Fail:** rule fires, nothing audible, and no `[SENSE stage=speech]` drop
  naming a reason. A named drop is a *different* outcome — degraded but
  honest — and is recorded, not silently passed.

### C5 — sound, and orienting (t15 + t8)

> **Amended 2026-07-21 (deviation `d6`).** The criterion below originally read
> "an audible sound … and the robot orients toward it". That is no longer the
> shipped behaviour: sound reaction is now a **graded two-tier ladder**, and a
> single transient like a clap is *supposed* to stop at the antennas. Testing
> the old criterion would fail a robot that is behaving as designed.

- **Pass, tier 1 (any sound above the room):**
  `[SENSE stage=rule source=rms event=look-toward-sound] fired ... run=orient-to-sound`
  and `[SENSE stage=orient source=doa event=tier] NONE->NOISE` — the near-side
  antenna leans toward the bearing and **the head does not move**.
- **Tier 2 (head/body turn) is NOT part of this gate** — deviation `d11`,
  2026-07-21. The shipped sound reaction is antenna-only. Live, 8 admissions
  including 3 s of deliberate continuous speech produced **zero** tier-2
  promotions: the NOISE envelope reopens and closes rather than holding, so
  `sustain_s` never accumulates, and ordinary speech does not reach 15× the
  room. Rather than ship a promotion path that exists on paper and never
  fires, the boundary is stated.
- **This does not weaken the port-before-delete argument.** The orienting
  capability is fully ported by `t8`/`t9` — `doa_angle_to_yaw`,
  `CorroboratedGate`, `LatchedDoaGuard` and the whole turn path exist, are
  tested, and are reachable via an operator overlay or `REACHY_ORIENT_*`.
  Only the shipped *default* is antenna-only. Nothing is deleted that has no
  home in the runtime.
- **Successor work:** #107 — spend a head turn only when vision and the mic
  agree on a bearing, which is corroboration loudness alone never provided.
- **Fail:** no antenna response at all to clear sound (tier 1 deaf), or a
  visible snap at the window end (would mean `duration_s` is mis-derived).
- **Watch:** the handover snap t15 flagged — `greet-when-addressed` admitting
  `speak` while the head is held at up to 35°. Record if seen; it is
  pre-existing arbitration behaviour, but these defaults make it reachable.

## 2. Soak exit criteria — the multi-day run

Capability demonstration proves it *can*. The soak proves it *keeps*.

**Duration: minimum 72 hours** of continuous `reachy-runtime` uptime, spanning
at least two overnight periods and one daemon restart.

Evaluated **against what the robot actually did**, from the journal — not
against recollection.

| # | Criterion | Pass condition | Evidence |
|---|---|---|---|
| S1 | Presence survives | No unplanned unit restart; `NRestarts` unchanged | `systemctl --user show reachy-runtime -p NRestarts` |
| S2 | No crash loop | Zero `Restart=on-failure` triggers | journal, `Scheduled restart job` absent |
| S3 | Tick budget holds | **Zero** startup overruns; steady-state overruns not worse than baseline | `grep 'event=overrun'` |
| S4 | Media stays held | No dormancy relapse; no unresolved `contended` | `stage=media` lines |
| S5 | No phantom pats | No `pat-acknowledge` fire with nobody present | `stage=rule source=pat` vs. presence |
| S6 | No spurious turning | No `->SPEECH`/`->ENGAGED` during known-empty hours | `stage=orient` overnight |
| S7 | Speech honest | Every `say` either audible or a named drop; no silent failure | `stage=speech` |
| S8 | Bounded memory/fd | No monotonic growth over 72 h | sampled `RSS`, `ls /proc/<pid>/fd \| wc -l` |
| S9 | Daemon restart survived | Runtime recovers senses without manual help | forced restart + `stage=media` |

**S3 is the one with a known prior.** The 0.41.0 baseline showed a reproducible
**424–1213 ms** startup overrun (21×–61× the 20 ms budget) from constructing
the pose client on the tick thread. `t27`/`t28` fixed it and it was verified
**0 overruns across 1500 ticks** on branch. S3 is a regression guard on a fix
already demonstrated, so any startup overrun is a hard fail, not a tuning
question.

**Any S-criterion failing stops the arc.** The deletions are irreversible in
practice — once `reachy-live.service` and the `think`/`listen` code are gone,
the rollback below stops working.

## 3. Rollback runbook

t19's second acceptance criterion: **executed end-to-end while
`reachy-live.service` still exists.** After the deletions it cannot be
validated, only trusted — which is the point of doing it now.

### 3.1 State to preserve first

```bash
mkdir -p ~/reachy-rollback-2026-07-20
cp -a ~/.config/systemd/user/reachy-*.service.d ~/reachy-rollback-2026-07-20/
cp -a ~/.local/state/reachy/behavior/rules.toml ~/reachy-rollback-2026-07-20/
systemctl --user list-unit-files 'reachy-*' > ~/reachy-rollback-2026-07-20/units.txt
```

The six `reachy-live.service.d/` drop-ins are **hand-authored and not
reproducible from the repo**. `panel.conf` carries a hardcoded bridge IP
(`192.168.1.173`) and an `ExecStart=` override that must stay flag-synced with
the main unit. Losing these means re-deriving them from journal archaeology.

> **Amended 2026-07-21 (deviation `d7`) after executing this runbook
> end-to-end.** As originally written it failed in four ways — the
> corrections are inline below and the failures are recorded in §5.2, because
> a runbook is only worth what its last rehearsal proved.

### 3.2 Downgrade

```bash
systemctl --user stop reachy-runtime.service
uv tool install --force 'reachy-mini-cli[daemon]==0.41.0'   # [daemon] IS REQUIRED
cd /tmp && ~/.local/share/uv/tools/reachy-mini-cli/bin/python3 \
  -c "import reachy; print(reachy.__file__)"                # expect site-packages, NOT the checkout
ls ~/.local/share/uv/tools/reachy-mini-cli/bin/reachy-mini-daemon   # must exist
```

**Why `[daemon]`, and why the version check is gone.** Two failures found by
rehearsal:

- `uv tool install --force 'reachy-mini-cli==0.41.0'` (no extra) removes
  `reachy_mini`, so the sdk-only `listen run --live` cannot start at all —
  and it **deletes `reachy-mini-daemon`**, the binary
  `reachy-daemon.service` execs. The running daemon survives only because
  its process is already resident: the box looks healthy and is one reboot
  from having no robot. A rollback that fails invisibly is worse than one
  that fails loudly.
- `reachy --version` proves nothing here — the branch build and released
  0.41.0 carry the **same** version string (deliberately unbumped mid-arc),
  so only the import path discriminates.

### 3.3 Restore the old presence

```bash
cp -a ~/reachy-rollback-2026-07-20/reachy-live.service.d ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user disable --now reachy-runtime.service
curl -s -X POST http://localhost:8000/api/media/acquire     # REQUIRED — see below
curl -s http://localhost:8000/api/media/status              # expect available:true
systemctl --user enable --now reachy-live.service
```

**Why the acquire step.** Released 0.41.0 carries #94: it never acquires the
daemon's media subsystem, and `released: true` is the daemon's ordinary
resting state once the last consumer disconnects. Without the acquire the
restored presence crash-loops on `ConnectionRefusedError` — observed at
**47 restarts** before the step was added. `t30` is what makes the *runtime*
side self-sufficient; the rolled-back build has no such fix, so the operator
supplies it by hand.

**Leave no orphaned hold.** A hand-issued acquire outlives the process it was
for. After §3.5, `POST /api/media/release` if the runtime reports
`contended: daemon media not released by its current owner` (#98).

### 3.4 Verify the rollback actually worked

Restoring files is not the same as restoring function.

- `systemctl --user is-active reachy-live.service` → `active`
- journal shows the folded loop starting with all four flags
  (`--live --transcribe --cognition agent --voice-engine harmonic`)
- the panel bridge receives export blocks (`panel.conf`'s pipe is intact)
- a pat still produces a reaction

**Runbook passes only if §3.4 passes.** A runbook that restores files but not
behaviour is a runbook that fails when it is needed.

### 3.5 Return to the branch under soak

```bash
systemctl --user disable --now reachy-live.service
uv tool install --force --editable '/home/spark/git/reachy-mini-cli[daemon]'
cd /tmp && ~/.local/share/uv/tools/reachy-mini-cli/bin/python3 \
  -c "import reachy; print(reachy.__file__)"     # expect the CHECKOUT path
systemctl --user enable --now reachy-runtime.service
curl -s http://localhost:8000/api/media/status   # expect available:true within ~30 s
```

`[daemon]` is required here for the same reason as §3.2 — without it this
step silently strips the daemon binary again. Verify from a **neutral cwd**:
run from inside the checkout, `sys.path[0]` shadows the tool venv and the
check passes no matter what is installed (the stale-wheel trap of
2026-07-21).

## 4. Explicitly out of scope for this gate

- **The deletions themselves** (`t20`–`t26`). Gated behind this document.
- **`reachy-listen.service`** — an orphan from June, in no catalog, disabled.
  It is the negative control for the orphan class `t4` addresses; leave it.
- **Retiring `pat-sense.conf`** — §0 shows it is a no-op, but removing it
  permanently is an operator decision, not a soak outcome. Moved aside for the
  clean-env checks, then restored or retired deliberately.

## 5. Results

Filled in as the gate is executed. A dash means not yet run.

### 5.1 Capability demonstration

| Check | Result | Evidence |
|---|---|---|
| C1 media acquired | **PASS** (2026-07-20 + re-verified on every 07-21 restart) | `available: true, released: false` with the runtime active; journal `media acquired from the daemon` → `connected (default media profile, recording)` |
| C2 quiet room | **PASS** (formal, 2026-07-21 ~02:05, 5 min, empty room) | 0 `->SPEECH`, 0 `->ENGAGED`, 0 latch lines. Residual NOISE-tier antenna blinks traced to the drifting mic background — see `2026-07-21-live-verification-night.md` §3–4 and #102 (t36) |
| C3 pat | **PASS** (2026-07-21, operator present, post-`t36`) | `Pat level1! type=side_pat` → `[SENSE stage=rule source=pat event=pat-acknowledge] fired kind=react run=pet-reaction`, from the SHIPPED rule (overlay moved aside), operator observed the antennas contract. **First touch after `t36`** — the same room recorded **zero** detections in 5 min before it |
| C4 words + audible | **PASS** (2026-07-21, operator present, post-`t37`+`t38`) | `heard "Richie, are you there?"` → `greet-when-addressed fired ... say="I'm here."` → `spoke voice=harmonic duration_s=0.72`, **operator confirmed hearing the reply**. Reproduced twice. Capture was contiguous (`span=1.10s clip=6.08s`), engagement took the **name fast-path** on the real STT mishearing `Richie` (Soundex R200 = `reachy`, zero classifier calls), and self-mute correctly discarded the ring afterwards (`buffered=768`) so the robot did not answer itself |
| C5 sound + orient | **PASS** — scope narrowed to tier 1 by `d6` + `d11` | Antenna lean only, by decision: 4 admissions / 100 s, all `NONE->NOISE`, **0 head turns**, down from 203 fires / 8 min; operator verdict *"antennas moved, i liked it"*. The head-turn half is explicitly **out of scope for this arc** (`d11`) — the capability is ported and reachable by configuration, just not defaulted on. Vision-corroborated head turning is the successor: #107 |

### 5.2 Rollback runbook

Rehearsed end-to-end **2026-07-21**, operator present, while
`reachy-live.service` still existed — the one window in which this can be
validated rather than trusted.

| Step | Result | Notes |
|---|---|---|
| 3.1 state preserved | **PASS** | Refreshed to `~/reachy-rollback-2026-07-21/` (units, drop-ins, pre-state) |
| 3.2 downgrade | **FAIL as written → PASS corrected** | Missing `[daemon]` stripped `reachy_mini` **and** `reachy-mini-daemon`; `reachy --version` cannot discriminate. Both fixed above (`d7`) |
| 3.3 restore presence | **FAIL as written → PASS corrected** | 47 restarts on #94 `ConnectionRefusedError`; passes once `POST /api/media/acquire` precedes the start (`d7`) |
| 3.4 **function verified** | **PASS** | `active`; all four flags (`--live --transcribe --cognition agent --voice-engine harmonic`); panel pipe present (`--export - --host 192.168.1.173`); **a real pat produced `Pat level2! type=scratch (sustained 11.6 s)` → `pat fire: scratch/level2` → `[SENSE stage=cue source=pat] felt a firm scratch on the head`** |
| 3.5 return to branch | **PASS** | Editable branch build restored with `[daemon]`; runtime re-acquired media unaided (`media acquired from the daemon (was released)` → `connected` → pump `live after discarding 0 stale chunk(s)`) |

**Verdict: the runbook PASSES only in its amended form.** Executed verbatim
it would have left the box without a daemon binary and with a crash-looping
presence — silently, on the first count. That is the single most valuable
finding of the rehearsal and the reason `t19` required it be done *before*
the deletions rather than trusted afterwards.

**Incidental:** §3.4 also reproduced #96 in released 0.41.0 (every journal
line doubled), confirming `t32` fixed something real rather than cosmetic.

### 5.3 Soak

| # | Criterion | Result | Evidence |
|---|---|---|---|
| S1 | presence survives | — | |
| S2 | no crash loop | — | |
| S3 | tick budget | — | |
| S4 | media stays held | — | |
| S5 | no phantom pats | — | |
| S6 | no spurious turning | — | |
| S7 | speech honest | — | |
| S8 | bounded memory/fd | — | |
| S9 | daemon restart | — | |
