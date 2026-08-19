# t6 — Wireless live acceptance of the deg/s stillness gate (issue #168)

Plan: `docs/plans/2026-08-18-pettable-wireless-168.md`, task t6.
Unit: Reachy Mini Wireless, `hardware_id a89063c05ae79779`, checkout
`~/git/reachy-mini-cli` (branch `wireless-motor-enable` +
`spec/pettable-wireless-168` merged for this acceptance), runtime via
`systemctl --user reachy-runtime.service`.

## Before (the defect, measured 2026-08-19 pre-fix)

- Effective tick cadence ~6.8 Hz (4086 ticks / 598 s); overrun p50 120 ms,
  p90 167 ms, max 1075 ms; host load ~10 on 4 cores (issue #97 compounded by
  the Pi-class host).
- Pat availability: **blocked in every sample ever taken** on this unit
  (nova's 19/19 over 32 s in #168; reproduced before the fix).
- Root cause: `still_eps` was deg-per-TICK, so per-tick command deltas ran
  ~7x design and the sustained-slow gate could never open under `feel-alive`.

## After (2026-08-19, deg/s gate deployed, runtime restarted)

Deployment: `git merge origin/spec/pettable-wireless-168` into the unit's
checkout (clean; its four motor-enable commits untouched), service restart.
`DEFAULT_STILL_EPS_DEG_S = 1.25` imports; overruns continue unchanged
(~30/25 s — #97 is deliberately out of scope).

180 s MQTT sample of `reachy/events/sense/snapshot` (paho, on-box):

```text
samples: 5651
availability: {'blocked': 5289, 'available': 362}   -> 6.4% available
blocked_reason: {'stillness': 5289}
```

- **Available fraction 6.4%** against the pre-fix 0% — and against the
  bimodal-dt simulation's prediction of 6.1% at eps 1.25 (challenge probe,
  spec's Scope exploration). Design intent restored on the real plant.
- **Every blocked sample carries a named cause** (t3's `blocked_reason`,
  first live use): all `stillness` in this window — no ownership churn
  (nova's `nova-face-noticed` never fired: no face in frame), no clock-gap
  restamps caught at snapshot cadence.
- `legacy-eps-ignored`: 0 lines, correctly — the unit sets no
  `REACHY_PAT_*` env.
- `Pat level` / `pat-acknowledge`: 0 lines, correctly — nobody touched the
  robot during the window.

## Petting round — PASSED (2026-08-19 07:33, operator's hand)

Overnight control first: **7.6 h untouched** (23:55 → 07:33) with the new
gate live — **zero pat events**, i.e. ~29 min of cumulative open-gate
exposure with no ghost. The operator then petted the head (confirmed: every
event below is their hand, starting at 07:33:02):

```text
07:33:02  Pat level1! type=side_pat (2 presses)  -> pat-acknowledge fired, pet-reaction ran
07:33:32  Pat level1! type=side_pat (2 presses)  -> pat-acknowledge fired, pet-reaction ran
07:33:34  Pat level1! type=scratch  (2 presses)  -> dropped reason=cooldown (5 s window honored)
07:33:42  Pat level1! type=side_pat (2 presses)  -> pat-acknowledge fired (cooldown expired)
```

Detection, classification (`side_pat` and `scratch`), rule admission, the
audible/visible reaction, and cooldown discipline — the same end-to-end
evidence chain as the Lite's 2026-07-22 verification, now on the Wireless at
its real ~6.8 Hz cadence. Issue #168's headline ("robot can't be petted") is
closed by measurement.
