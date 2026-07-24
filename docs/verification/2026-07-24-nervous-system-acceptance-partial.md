# Nervous-system acceptance — partial run, 2026-07-24

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
