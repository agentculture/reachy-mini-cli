# Nervous-system acceptance — 2026-07-24 (session 1 partial, session 2 live)

Partial execution of plan task **t12** (the PR gate) for
`docs/plans/2026-07-23-reachy-nervous-system.md`.

**Why partial.** The robot's motor bus was dead for the whole session, so every
criterion needing a live runtime is deferred. The criteria that exercise the
broker, the client binding, the Last Will and the `doctor` sense-extras check do
not need motors, and were run to completion. Each result below is either PASS
with its evidence or DEFERRED with the reason — nothing is claimed that was not
observed.

## Box under test (h22)

| | |
|---|---|
| host | `spark-f8a9` |
| branch | `spec/nervous-system` |
| commit | `65f5706` |
| version | `0.44.0` |
| broker | `events-mosquitto`, `eclipse-mosquitto:2.1.2-alpine`, events-cli 0.9.0 |
| client | `events-cli==0.9.0` + `paho-mqtt==2.1.0` (both venvs) |

The box runs this checkout through the **editable** `uv tool` install, so the
deployed service executes the working tree.

## The hardware block

`reachy-mini-daemon` opened the serial bus and scanned it successfully; **every**
motor was absent:

```text
[WARN] Motor 'stewart_2' (ID 12) not found on the bus.   ... 'stewart_3'..'stewart_6'
[WARN] Motor 'right_antenna' (ID 17) not found on the bus.
[WARN] Motor 'left_antenna'  (ID 18) not found on the bus.
ERROR - Failed to start daemon: No motors detected. Check if the power supply is
        connected and turned on!
```

Ruled out, so the diagnosis is the DC supply and not something softer:

- USB enumeration is healthy — `Pollen Robotics Reachy Mini Audio` (ALSA card 2),
  `QinHeng USB Single Serial` → `/dev/ttyACM0`, `Arducam_12MP`, `/dev/video0-3`.
- The invoking user is in `dialout` and `plugdev`.
- A clean `systemctl --user restart reachy-daemon.service` reproduced it
  identically (new PID, same scan result).

Consequence: `behavior engine run` exits 2 on the daemon's `503 Backend not
running` about 2 s into each start, and `Restart=on-failure` cycles it every
~16 s. That is the designed boot-persistence behaviour — it self-heals when power
returns, with no manual step.

## PASS — h19, the broker binds loopback-only and is the only one

```text
LISTEN 0 4096 127.0.0.1:1883 0.0.0.0:*          # not 0.0.0.0
events-mosquitto   127.0.0.1:1883->1883/tcp     # exactly one broker
192.168.1.157:1883 -> ConnectionRefusedError    # non-loopback refused
192.168.1.118:1883 -> ConnectionRefusedError
```

The pre-existing nova broker that bound `0.0.0.0` anonymously is gone, per the
replace decision.

## PASS — h18, `kill -9` flips availability while standing state persists

Driven by a publisher composed exactly as the runtime composes it
(`NervousPublisher` + `EventsCliClient` + the live broker), under its own
`reachyacc/` root so the robot's tree stayed clean. **Not** the runtime process
itself — that is the deferred half.

| step | `state/online` | `state/pose`, `state/ownership` |
|---|---|---|
| alive | `true` | retained |
| `kill -9` | **`false`** (Last Will fired) | **persist** |
| restart | `true` | republished |
| `kill -9` again | `false` | persist |

One methodology note worth keeping: the first attempt killed the `uv run`
wrapper rather than the Python child, which orphaned a live session and made the
Last Will look broken. Kill the real process, or this check lies.

## PASS — the binding reaches a real broker end to end

The reason this needed a live check at all: the publisher degrades **quietly**
(a mismatched client is a named `client-incompatible` drop, not a crash), and
every unit test runs against a fake built from our own declared protocol, so it
agrees with us by construction. Only a real client against a real broker
distinguishes a correct binding from a quiet bus.

A subscriber observed the full expected sequence — retained `online true`, the
retained state keys, a `events/sense/snapshot` event carrying the same bytes as
the stdout `--export` feed, then `online false` on clean stop.

## PASS — h2 (doctor half), the `[vision]` flip both ways

| venv | `sense_extras` |
|---|---|
| repo `.venv` (no cv2) | `passed: false` — "face/frame_available senses stay permanently unavailable (issue #120)", remediation names both `pip install` and the `uv tool install --force --editable ".[daemon,vision]"` form |
| deployed tool venv (cv2 4.13.0) | `passed: true` — "[vision] extra (opencv) installed; face/frame_available senses available", `healthy: true` |

So the deployed box already has the face sense enabled; #120's silent-disable is
now visible in `doctor` from both directions.

## DEFERRED — needs the motor bus

| criterion | why |
|---|---|
| transcript event + `face`/`frame_available` flip on the broker (c1, h1) | needs a live runtime with senses |
| monitor-speaker greeting answered aloud, no self-answer loop (c24, h12) | needs the runtime's voice + hearing |
| reTerminal panel rendering live events (c27, h15) | needs live events on the bus |
| broker stopped mid-run leaves tick cadence unchanged, one named drop (h4) | the load-bearing half is tick cadence |
| 30-min soak, O(10) overrun lines with `.overruns` exact (h6 live half) | needs a ticking engine |
| `state.json` `senses` block flip (h2 live half) | needs the runtime to write state |

## Defect found during this session

**#125** — `NervousPublisher.start()` reports `broker-unreachable` on every
*healthy* boot, because it checks liveness microseconds after an asynchronous
connect. Self-corrects on the first tick (~20 ms later, logging `connected`), so
it is cosmetic in effect — but it puts a false reason in the one layer whose
discipline is "a drop always names a true reason". Filed rather than folded in:
it touches merged t6 code and the right validation is the deferred live run.

---

## Session 2 — the motor bus came up, 19:00-19:12

The power button had not been pressed. Once it was, `is_torque_enabled()`
returned cleanly, the daemon reached `state: running`, and the runtime started
with `NRestarts=0`. Everything below was then observed live.

### PASS — the sensorium reaches the bus (c1, h1)

All eight senses report `available: true` in retained `reachy/state/senses`,
including `face` (the `[vision]` extra is installed on the deployed venv, so issue
issue #120's silent-disable is closed there). A live `events/sense/snapshot` carried
real perception:

```json
{"t":"sense","tick":5429,"doa":0.9599310755729675,"speech":true,
 "frame_available":true,"pat_state":{"availability":"blocked",...}}
```

Sound bearing, speech detection and camera liveness, all on the broker, all
consumable without touching the SDK.

### PASS — hearing, engagement, voice, and no self-answer loop (c24, h12)

One spoken sentence drove the whole chain:

```text
engagement: name :: "Richie, are you there?"
[SENSE stage=rule source=transcript event=greet-when-addressed] fired kind=react run=speak say="I'm here."
[SENSE stage=speech source=say event=utt1] spoke voice=harmonic chars=9 duration_s=0.72
[SENSE stage=capture source=speech event=stream] dropped reason=self-mute
```

Four things worth naming:

- The STT heard **"Richie"**, not "Reachy". The fuzzy name fast-path matched it
  anyway on the Soundex consonant skeleton (both R200) — issue #104's phonetic
  guard earning its keep on the first real sentence, with **zero classifier
  calls**.
- The robot answered **aloud**, confirmed by ear, via the **harmonic** voice.
  That default is load-bearing rather than stylistic: `model-gear-chatterbox` is
  down (`RuntimeError: No CUDA GPUs are available`), so the TTS route was
  unavailable for the whole session. The offline voice is the only reason the
  robot could speak at all — exactly the case `speech_act.py` documents.
- **Self-mute fired**, so its own voice never re-entered hearing. Exactly one
  fire, one utterance, no loop.
- A later attempt produced `"Legique, are you there?"` and was correctly
  REJECTED (`not-addressed-cold`) — the matcher discriminating, not just
  accepting. Mic RMS peaked ~0.002 while speaking, which is low; STT quality is
  the weak link in this chain, not the gate.

### PASS — a rule fire reaches the broker, no STT involved (c1)

A physical pat, through the proprioceptive sense, published as a first-class
event:

```json
reachy/events/rule/fire {"t":"rule","tick":20614,"action":"fire",
  "rule":"pat-acknowledge","kind":"react","field":"pat","op":"is_true",
  "reason":"fired","behavior":"pet-reaction","disable":[]}
```

Journal: `Pat level1! type=side_pat (2 presses)`. This is the arc's central
claim demonstrated end to end: touch -> sense -> rule -> bus.

A first, gentle pat did NOT register: the gate was open (556 `available` vs 1129
`blocked` snapshots) but `phase` stayed `idle` in all 1685. That is the
documented cost of the shipped 1.2 deg press threshold, not a fault — CLAUDE.md
states it plainly ("on a head held genuinely still the petting p90 is 0.85-1.90
deg, so 1.2 misses the gentlest pats there"). A firm scratch registered
immediately.

### PASS — h4, the broker dies under a live runtime

```text
19:05:37  broker STOPPED
          [SENSE stage=nervous source=mqtt ...] dropped reason=broker-unreachable (session lost)
          runtime still active; no overrun lines during the outage
19:06:02  broker RESTARTED
19:06:08  [SENSE stage=nervous source=mqtt ...] reconnected — republishing retained state
```

Exactly ONE named drop, tick cadence untouched, self-healing reconnect.

### PASS — h18 against the REAL runtime process

| step | `reachy/state/online` |
|---|---|
| alive | `true` |
| `kill -9 <MainPID>` | **`false`** (Last Will) |
| systemd `Restart=on-failure` | `true` again |

### FINDING — face detected, never recognised (issue #127)

Frames are fine and detection is fine; only matching fails:

```text
f05: FACE bbox=(0.42,0.27,0.53,0.52) match=None   cosine 0.4296
f09: FACE bbox=(0.42,0.27,0.53,0.53) match=None   cosine 0.4241
f12: FACE bbox=(0.41,0.26,0.52,0.54) match=None   cosine 0.4353
                                DEFAULT_MATCH_THRESHOLD = 0.5
```

A consistent ~0.07 near-miss against a **single** enrolled embedding from
2026-07-17. Two separable problems: enrolment quality (operator decision
2026-07-24: this becomes its own sibling tool, a robust face store with
identities) and — ours, and unaffected by any better recognizer — the fact that
a SEEN-but-UNRECOGNISED face is byte-identical to an empty frame, with no
`senselog` line to grep. That is #120's failure shape one level deeper.

### FINDING — the retained state tree floods (issue #126)

`reachy/state/*` republishes at full tick rate, ~96% byte-identical: 520
messages/10 s per key, of which `state/active` had 20 distinct payloads and
`state/compose_hz` had 1. The event stream is correctly change-gated (118/10 s);
the state mirror never got the same treatment. Concretely felt during this run —
a 3-second diagnostic subscription on two topics returned 39 KB.

### Confirmed live — issue #125

Every healthy start logs
`dropped reason=broker-unreachable (no session after connect)` immediately
followed by `connected`. Cosmetic, self-correcting, and exactly as filed.

### Still deferred

| criterion | why |
|---|---|
| reTerminal panel rendering live events (c27, h15) | the t11 subscriber is committed but unpushed/undeployed |
| 30-minute soak, `.overruns` exact (h6 live half) | clock started 19:07:34; the tick runs a steady ~21.1 ms against a 20 ms budget (the accepted slow-tick posture), so overrun lines accrue continuously rather than as O(10) events |
