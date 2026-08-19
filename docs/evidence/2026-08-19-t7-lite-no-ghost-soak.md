# t7 — Lite no-ghost soak + petting check (deg/s gate)

Plan: `docs/plans/2026-08-18-pettable-wireless-168.md`, task t7.
Unit: spark-local Reachy Mini Lite (daemon `localhost:8000`,
`wireless_version: false`). Branch code run directly from this checkout
(`uv run reachy behavior engine run`) with the boot
`reachy-runtime.service` stopped for the session and restored after.

## Why the Lite must be checked at all

At the Lite's real cadence (measured this session: ~49.8 Hz — 29 905 ticks
in 600 s) the deg/s gate at 1.25 is **tighter** than the retired per-tick
gate (1.75 deg/s-equivalent), so the ghost direction cannot regress by
construction (also pinned by
`tests/test_pat_stillness_gate_deg_per_second.py`); the live question is the
other direction — that the tighter gate did not silence the sense.

## Soak — PASSED

10 min hands-off (operator confirmed no contact), `feel-alive` live,
`timeout --signal=INT 600`:

```text
Pat events: 0        (zero ghosts in 600 s)
ticks: ~29 905       (~49.8 Hz — the dev box currently holds near-50 Hz)
overrun summaries: 2 (single-tick blips, 21.7 ms worst)
clean shutdown: audio pump reads=37390 dropped=0; tee offers=29905 dropped=0
```

A further untouched 4-minute window later in the session also logged zero
events (an accidental extra control: the operator had not yet reached the
robot).

## Petting check — PASSED

Fresh engine run, operator petting the head:

```text
Pat level1! type=side_pat (2 presses)
[SENSE stage=rule source=pat event=pat-acknowledge] fired kind=react run=pet-reaction
```

The sense is loosened where it was broken (Wireless) and tightened where it
was proven (Lite), and detects on both. Together with
`2026-08-19-t6-wireless-live-acceptance.md` this closes the plan's live
acceptance: h7 (Lite soak zero events), h14 (tighter-at-50 Hz + live
verification on both units).
