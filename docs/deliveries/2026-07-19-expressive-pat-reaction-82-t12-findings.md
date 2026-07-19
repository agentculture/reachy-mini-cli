# t12 live acceptance — expressive sustained pat reaction (#82)

Date: 2026-07-19 · Version under test: **0.38.0** (PR #84) · Result: **gate not met**

Plan `expressive-sustained-pat-reaction-82-completion-of`, task **t12**, run on the
real robot. t1–t11 shipped in PR #84; this is the hardware gate that closes them.
t12 says to stop on a wrong sign or missed timing rather than tune a failure away,
so this document records the failure instead of a patched result.

## Setup

| | |
|---|---|
| Presence | foreground `behavior engine run --export -` (50 Hz, `http` transport) |
| Rule | `pat-acknowledge` → `pet-reaction` (fixture `docs/fixtures/behavior-rules/pat-pet-reaction.toml`) |
| Sense | `HeldStateReader` attached, `media_backend=no_media` |
| Operator | continuous side scratches, RIGHT first then LEFT, ~15 s each |

## What worked

- **Detection is sound.** 6 detections, all correctly typed `side_pat`, 4 clean rule
  fires through `pat-acknowledge` → `pet-reaction`.
- **Ghost-free.** 188.5 s hands-off before first contact with **zero** pat events —
  the property #80 fought for survives the new stack.
- **Stillness gate + pettable windows** cycle as designed (`blocked` while feel-alive
  wanders, `available` during the 4 s holds).
- **`pat_state` exports additively** alongside the legacy `pat` tuple (t5 holds).
- **The reaction is pleasant.** The operator reported it felt good: head leans toward
  the hand, body yaw follows, antennas engage. The response is real and worth keeping.

## Failure 1 — contact never sustains

Required: contentment ≈4 s, warning by 8 s, coordinated done gesture by 12 s.
Observed: **`receptive` is the only phase ever reached** — `pat_state.phase` across the
session was `idle` (1346 samples) or `receptive` (29). Zero contentment samples.

| | |
|---|---|
| `CONTENTMENT_AFTER_S` | 4.00 s required |
| max observed contact | **0.82 s** (mean 0.39 s) |

Mechanism: `_active_contact_s` accrues only on ticks where sensing succeeded and
contact held. `pet-reaction`'s own entry move takes the head channel ≈0.6 s after
contact begins, sensing goes `blocked`, `_begin_gap` → `_end_interaction` clears the
interaction, phase falls to `idle`, and the next press restarts the counter at zero.
The 4 s clock is reset roughly every 0.6 s, so contentment is **structurally
unreachable**, not marginally missed.

Proof it is the reaction and not the pettable window closing: episode 4 opened a fresh
4 s window at t+228.15 s, contact began at 228.17 s, and sensing went `blocked` at
**228.75 s — 0.6 s into a 4 s window**.

This is the tension #82 predicted ("the sustained lean must itself be a still-enough
commanded pose to keep sensing, or the behavior needs to alternate hold/sense") and it
is a genuine conflict between two shipped requirements, neither of which is wrong:

- **t4** requires a gap to clear press pairing — this is what closed the #66/#79 ghost class.
- **t8** requires contact to survive the reaction's own motion.

## Failure 2 — direction is not resolvable

Required: 2 of 2 labelled sides produce opposite, correct leans.

| | |
|---|---|
| `YAW_DEADBAND_DEG` | 0.75° (direction latches only above this) |
| observed \|yaw\| | **0.55°–0.96°** — straddles the deadband |

The sign also **flips mid-contact in 4 of 6 episodes**:

| # | t+s | dur | yaw signs |
|---|-----|-----|-----------|
| 1 | 188.52 | 0.31 | `++++` |
| 2 | 198.21 | 0.18 | `-++` |
| 3 | 200.94 | 0.82 | `++----+` |
| 4 | 228.17 | 0.55 | `++++++` |
| 5 | 231.35 | 0.25 | `+---+` |
| 6 | 259.28 | 0.20 | `++--` |

`_latch_touch` latches direction **once**, on the first sample clearing the deadband.
With the signal sitting on the threshold and the sign unstable between samples, the
latched direction is effectively a coin flip. RIGHT-first and LEFT-second are
indistinguishable in this data.

Labels were pinned in advance (t1 discipline). Had the sign been mapped after the
fact, this data could have been read as confirming either polarity.

## Why the replay gate (t1) did not catch this

The shipped replays reproduce recorded pose streams; they cannot reproduce
`pet-reaction` **commanding the head mid-contact**. Both failures are properties of
the closed loop — reaction motion feeding back into its own sense — so they are only
observable live. All 13 hardware replays still pass.

## Not yet run

The 180 s formal hands-off soak was not run, because the gate had already failed on
two criteria. The incidental 188.5 s hands-off stretch was ghost-free.

## Follow-ups

1. **Sustain** — decide how a reaction keeps its sense alive through its own motion
   (exempt self-commanded gaps, hold the lean exactly constant and re-sense, or
   alternate hold/sense). A spec decision, not a tuning knob.
2. **Direction** — the side signal is at the noise floor. Either raise usable signal
   or drop the directional claim; the deadband cannot simply be lowered into noise.
3. **Naturalness** — the 4 s freeze that sensing depends on reads as unnatural. Fixing
   the freeze and fixing sustain are the same problem.
4. **Antennas** — one antenna was briefly unresponsive during the session and
   recovered after a power cycle; watch for recurrence.

- reachy-mini-cli (Claude)
